"""从本地 Minecraft 客户端资源里取方块纹理。

优先顺序：
1. 版本目录里的客户端 jar（``versions/<版本>/<版本>.jar``）；
2. 资源索引 + 对象库（``assets/indexes/*.json`` + ``assets/objects``）。

方块 id → 纹理的解析遵循原版 ``blockstates`` → ``models``（含 parent 继承）
→ ``textures`` 的链路；解析不到时按同名纹理兜底，最后退回纯色方块。
"""

import hashlib
import json
import os
import struct
import threading
import zipfile
import zlib
from pathlib import Path
from typing import Dict, Optional, Tuple

# 纹理键的优先级：整块 → 顶面 → 侧面 → 端面 → 其它
_TEXTURE_KEYS = (
    "all", "top", "side", "end", "front", "back", "bottom",
    "texture", "particle", "cross", "torch", "lit",
)
_BLOCK_ID_RE = None  # 延迟编译，避免重复 import re

# 方块名与纹理名对不上的特殊情况
_ALIASES = {
    "redstone_wire": ("redstone_dust_line0", "redstone_dust_dot"),
    "redstone_torch": ("redstone_torch_on", "redstone_torch"),
    "wall_torch": ("torch",),
    "soul_wall_torch": ("soul_torch",),
    "powered_rail": ("powered_rail_on", "powered_rail"),
    "rail": ("rail_normal", "rail"),
    # 功能方块：资源里的纹理名与方块 ID 不完全一致
    "redstone_lamp": ("redstone_lamp_on", "redstone_lamp"),
    "command_block": ("command_block", "command_block_front", "command_block_side"),
    "chain_command_block": ("chain_command_block", "chain_command_block_front",
                            "chain_command_block_side"),
    "repeating_command_block": ("repeating_command_block",
                                "repeating_command_block_front",
                                "repeating_command_block_side"),
    "grass_block": ("grass_block_top", "grass_block_side"),
    "mycelium": ("mycelium_top", "mycelium_side"),
    "podzol": ("podzol_top", "podzol_side"),
    "crafting_table": ("crafting_table_top", "crafting_table_front",
                       "crafting_table_side"),
    "furnace": ("furnace_front", "furnace_side", "furnace_top"),
    "carved_pumpkin": ("carved_pumpkin", "pumpkin_top", "pumpkin_side"),
    "jack_o_lantern": ("jack_o_lantern", "pumpkin_top", "pumpkin_side"),
    "soul_lantern": ("soul_lantern",),
    "sea_lantern": ("sea_lantern",),
    "glowstone": ("glowstone",),
    "shroomlight": ("shroomlight",),
    "note_block": ("note_block", "noteblock"),
    "jukebox": ("jukebox_top", "jukebox_side"),
    "observer": ("observer_front", "observer_side", "observer_top"),
    "piston": ("piston_top", "piston_side"),
    "sticky_piston": ("piston_top_sticky", "piston_side"),
    "dispenser": ("dispenser_front", "dispenser_side", "dispenser_top"),
    "dropper": ("dropper_front", "dropper_side", "dropper_top"),
    "hopper": ("hopper_outside", "hopper_top"),
}


def _block_name(block: str) -> str:
    """``minecraft:note_block[note=3]`` → ``note_block``。"""
    raw = str(block or "").strip()
    if "[" in raw:
        raw = raw.split("[", 1)[0]
    if ":" in raw:
        raw = raw.split(":", 1)[1]
    return raw.lower()


def _safe_rel(path: str) -> Optional[str]:
    """把资源相对路径限制在 assets/minecraft 下，阻止目录穿越。"""
    text = str(path).replace("\\", "/").lstrip("/")
    if ".." in text.split("/"):
        return None
    return text


class TextureLibrary:
    """线程安全的方块纹理读取器（带内存缓存）。"""

    def __init__(self, assets_dir: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._zip: Optional[zipfile.ZipFile] = None
        self._zip_path: Optional[Path] = None
        self._index_objects: Dict[str, str] = {}
        self._object_root: Optional[Path] = None
        self._texture_cache: Dict[str, bytes] = {}
        self._real_cache: Dict[str, bool] = {}
        self._model_cache: Dict[str, dict] = {}
        self.source = "无"
        self.version = ""
        self.assets_dir: Optional[Path] = None
        self._load(assets_dir)

    # ── 定位资源 ─────────────────────────────────────────────
    def _load(self, assets_dir: Optional[str]) -> None:
        root = None
        if assets_dir:
            candidate = Path(os.path.expanduser(str(assets_dir)))
            if candidate.name.lower() == "assets":
                root = candidate.parent
                self.assets_dir = candidate
            elif candidate.exists():
                root = candidate
                self.assets_dir = candidate / "assets"
        self._open_jar(root)
        if self._zip is None:
            self._open_objects(root)

    def _open_jar(self, root: Optional[Path]) -> None:
        if root is None:
            return
        versions = root / "versions"
        if not versions.is_dir():
            return
        jars = []
        for folder in versions.iterdir():
            if not folder.is_dir():
                continue
            for jar in folder.glob("*.jar"):
                if jar.is_file():
                    jars.append((folder.name, jar))
        if not jars:
            return
        # 优先 1.21.11（项目目标版本），否则取体积最大的客户端
        preferred = [item for item in jars if "1.21.11" in item[0]]
        pool = preferred or jars
        pool.sort(key=lambda item: item[1].stat().st_size, reverse=True)
        for name, path in pool:
            try:
                archive = zipfile.ZipFile(path)
                if "assets/minecraft/textures/block/note_block.png" in archive.namelist():
                    self._zip = archive
                    self._zip_path = path
                    self.version = name
                    self.source = f"jar · {name}"
                    return
                archive.close()
            except (OSError, zipfile.BadZipFile):
                continue

    def _open_objects(self, root: Optional[Path]) -> None:
        if root is None:
            return
        assets = root / "assets"
        indexes = assets / "indexes"
        objects = assets / "objects"
        if not indexes.is_dir() or not objects.is_dir():
            return
        candidates = sorted(indexes.glob("*.json"), key=lambda p: p.stat().st_mtime,
                            reverse=True)
        for path in candidates:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            entries = data.get("objects")
            if not isinstance(entries, dict):
                continue
            if "minecraft/textures/block/note_block.png" not in entries:
                continue
            self._index_objects = {
                key: value.get("hash", "")
                for key, value in entries.items()
                if isinstance(value, dict) and value.get("hash")
            }
            self._object_root = objects
            self.version = path.stem
            self.source = f"objects · {path.name}"
            self.assets_dir = assets
            return

    @property
    def available(self) -> bool:
        return self._zip is not None or bool(self._index_objects)

    def status(self) -> dict:
        return {
            "available": self.available,
            "source": self.source,
            "version": self.version,
            "assetsDir": str(self.assets_dir) if self.assets_dir else "",
        }

    # ── 读取资源文件 ─────────────────────────────────────────
    def _read_resource(self, path: str) -> Optional[bytes]:
        rel = _safe_rel(path)
        if not rel:
            return None
        with self._lock:
            if self._zip is not None:
                try:
                    return self._zip.read(rel)
                except (KeyError, OSError, zipfile.BadZipFile):
                    return None
            if self._index_objects:
                entry = self._index_objects.get(rel)
                if not entry or self._object_root is None:
                    return None
                obj = self._object_root / entry[:2] / entry
                try:
                    return obj.read_bytes()
                except OSError:
                    return None
        return None

    def _read_json(self, path: str) -> dict:
        if path in self._model_cache:
            return self._model_cache[path]
        raw = self._read_resource(path)
        data: dict = {}
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8-sig"))
                if isinstance(parsed, dict):
                    data = parsed
            except (ValueError, UnicodeDecodeError):
                data = {}
        self._model_cache[path] = data
        return data

    # ── 方块 → 纹理 ──────────────────────────────────────────
    def texture_png(self, block: str) -> bytes:
        name = _block_name(block)
        with self._lock:
            cached = self._texture_cache.get(name)
        if cached is not None:
            return cached
        png = self._lookup_texture(name)
        if png is None:
            png = _fallback_png(name)
        with self._lock:
            self._texture_cache[name] = png
        return png

    def has_texture(self, block: str) -> bool:
        """方块是否能在本地资源里找到真正的纹理（不含纯色兜底）。"""
        name = _block_name(block)
        with self._lock:
            if name in self._real_cache:
                return self._real_cache[name]
        found = self._lookup_texture(name) is not None
        with self._lock:
            self._real_cache[name] = found
        return found

    def _lookup_texture(self, name: str) -> Optional[bytes]:
        model = self._first_model(name)
        textures = self._model_textures(model) if model else {}
        ref = _pick_texture(textures)
        candidates = []
        if ref:
            candidates.append(_texture_path(ref))
        candidates.extend(self._name_candidates(name))
        for path in candidates:
            if not path:
                continue
            raw = self._read_resource(path)
            if raw:
                return raw
        return None

    @staticmethod
    def _name_candidates(name: str) -> list:
        """模型缺省时的兜底：同名纹理、方向后缀、按钮/压力板对应的基础材质。"""
        base = []
        for alias in _ALIASES.get(name, ()):
            base.append(f"assets/minecraft/textures/block/{alias}.png")
        base.extend([f"assets/minecraft/textures/block/{name}.png",
                     f"assets/minecraft/textures/item/{name}.png"])
        for suffix in ("_front", "_side", "_top", "_end", "_back", "_still"):
            base.append(f"assets/minecraft/textures/block/{name}{suffix}.png")
        for suffix in ("_button", "_pressure_plate"):
            if name.endswith(suffix):
                material = name[:-len(suffix)]
                wood = ("oak", "spruce", "birch", "jungle", "acacia", "dark_oak",
                        "mangrove", "cherry", "bamboo", "crimson", "warped", "pale_oak")
                if material in wood:
                    material += "_planks"
                base.append(f"assets/minecraft/textures/block/{material}.png")
        return base

    def _first_model(self, name: str) -> Optional[str]:
        data = self._read_json(f"assets/minecraft/blockstates/{name}.json")
        if data:
            variants = data.get("variants")
            if isinstance(variants, dict) and variants:
                for value in variants.values():
                    ref = _model_ref(value)
                    if ref:
                        return ref
            multipart = data.get("multipart")
            if isinstance(multipart, list):
                for part in multipart:
                    if isinstance(part, dict):
                        ref = _model_ref(part.get("apply"))
                        if ref:
                            return ref
        # 没有 blockstate（例如纯方块）时，按同名模型兜底
        return f"minecraft:block/{name}"

    def _model_textures(self, model: str, depth: int = 0) -> Dict[str, str]:
        if depth > 8 or not model:
            return {}
        rel = _normalize_ref(model, "models")
        if not rel:
            return {}
        path = f"assets/minecraft/models/{rel}.json"
        data = self._read_json(path)
        if not data:
            return {}
        textures: Dict[str, str] = {}
        parent = data.get("parent")
        if isinstance(parent, str):
            if parent.startswith("builtin/"):
                if parent in ("builtin/generated", "builtin/generated_item"):
                    parent = model
                else:
                    parent = None
            if parent:
                textures.update(self._model_textures(parent, depth + 1))
        own = data.get("textures")
        if isinstance(own, dict):
            for key, value in own.items():
                if isinstance(value, str):
                    textures[str(key)] = value
        return textures


def _model_ref(value) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return _model_ref(value[0])
    if isinstance(value, dict):
        return _model_ref(value.get("model"))
    return None


def _normalize_ref(ref: str, folder: str) -> Optional[str]:
    text = str(ref or "").strip()
    if not text:
        return None
    if ":" in text:
        namespace, path = text.split(":", 1)
        if namespace != "minecraft":
            return None
    else:
        path = text
    path = path.replace("\\", "/").lstrip("/")
    if ".." in path.split("/"):
        return None
    if path.startswith(f"{folder}/"):
        path = path[len(folder) + 1:]
    return f"{folder}/{path}"


def _texture_path(ref: str) -> Optional[str]:
    rel = _normalize_ref(ref, "textures")
    if not rel:
        return None
    return f"assets/minecraft/{rel}.png"


def _pick_texture(textures: Dict[str, str]) -> Optional[str]:
    if not textures:
        return None

    def resolve(value: str, depth: int = 0):
        if depth > 6:
            return None
        if isinstance(value, str) and value.startswith("#"):
            target = textures.get(value[1:])
            return resolve(target, depth + 1) if isinstance(target, str) else None
        return value

    for key in _TEXTURE_KEYS:
        if key in textures:
            resolved = resolve(textures[key])
            if resolved:
                return resolved
    for value in textures.values():
        resolved = resolve(value)
        if resolved:
            return resolved
    return None


def _fallback_png(name: str) -> bytes:
    """没有纹理时生成一个稳定配色的方块图标（16×16）。"""
    digest = hashlib.sha1(name.encode("utf-8")).digest()
    base = (70 + digest[0] % 120, 70 + digest[1] % 120, 70 + digest[2] % 120)
    edge = tuple(max(0, channel - 28) for channel in base)
    rows = []
    for y in range(16):
        row = bytearray()
        for x in range(16):
            inside = 1 <= x <= 14 and 1 <= y <= 14
            color = base if inside else edge
            if inside and (x + y) % 9 == 0:
                color = edge
            row.extend((color[0], color[1], color[2], 255))
        rows.append(b"\x00" + bytes(row))
    return _encode_png(16, 16, b"".join(rows))


def _encode_png(width: int, height: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
