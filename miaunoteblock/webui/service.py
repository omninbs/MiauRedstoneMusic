"""网页工作台的会话状态：加载歌曲、保存配置、预览、导出。

所有耗时操作（解析 NBS、生成命令、写结构）都在这里完成，HTTP 层只负责
JSON 编解码与参数校验。
"""

import json
import os
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..config import (
    DEFAULT_CONFIG,
    default_config,
    load_song_config,
    save_song_config,
)
from ..constants import INSTRUMENT_BLOCK_MAP, INSTRUMENT_NAMES
from ..instruments import auto_group_by_mode_on_layers
from ..layout import apply_offset_overrides, compute_offsets
from .assets import TextureLibrary

MAX_PREVIEW_BLOCKS = 320_000
DEFAULT_PREVIEW_TICKS = 240
MAX_BODY_BYTES = 2 * 1024 * 1024

_BLOCK_RE = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_./-]+$")
_SETBLOCK_RE = re.compile(r"^setblock\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+?)\s*$")
_FILL_RE = re.compile(
    r"^fill\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+?)\s*$")

LAYOUT_STYLES = [
    {"id": "flat", "name": "平铺", "tag": "最快",
     "desc": "所有分组并排成一条直线，结构最紧凑。"},
    {"id": "circle", "name": "圆形", "tag": "均匀",
     "desc": "分组沿圆环展开，听感分布均匀。"},
    {"id": "square", "name": "方形", "tag": "均匀",
     "desc": "分组沿方形四边分布，转角更硬朗。"},
    {"id": "semicircle", "name": "半圆", "tag": "开阔",
     "desc": "半圆环排列，适合放在舞台一侧。"},
    {"id": "semisquare", "name": "半方形", "tag": "开阔",
     "desc": "半方形排列，空间利用率更高。"},
    {"id": "nest_circle", "name": "嵌套圆", "tag": "分层",
     "desc": "同心圆环，多层分组时层次清晰。"},
    {"id": "nest_square", "name": "嵌套方", "tag": "分层",
     "desc": "同心方形，适合大量分组。"},
    {"id": "curve", "name": "音量控制", "tag": "推荐",
     "desc": "用离中心轴的距离还原 NBS 力度，附带隐形矿车跟随。"},
]

PARAM_SPECS = {
    "flat_spacing": {"label": "音轨间距", "type": "int", "min": 1, "max": 32, "unit": "格"},
    "layout_radius": {"label": "半径 / 边长", "type": "int", "min": 1, "max": 128, "unit": "格"},
    "nest_layers": {"label": "嵌套层数", "type": "int", "min": 1, "max": 16, "unit": "层"},
    "layer_gap": {"label": "层间距", "type": "int", "min": 1, "max": 32, "unit": "格"},
    "curve_lane_spacing": {"label": "普通车道间距", "type": "int", "min": 3, "max": 24, "unit": "格"},
    "curve_master_spacing": {"label": "主音轨间距", "type": "int", "min": 3, "max": 24, "unit": "格"},
    "curve_corridor_height": {"label": "玩家走廊高度", "type": "int", "min": 2, "max": 8, "unit": "格"},
    "curve_sound_range": {"label": "可听半径", "type": "float", "min": 8, "max": 128, "unit": "格"},
}

_LAYOUT_PARAMS = {
    "flat": ["flat_spacing"],
    "circle": ["layout_radius", "master_group"],
    "square": ["layout_radius", "master_group"],
    "semicircle": ["layout_radius", "master_group"],
    "semisquare": ["layout_radius", "master_group"],
    "nest_circle": ["layout_radius", "nest_layers", "layer_gap", "master_group"],
    "nest_square": ["layout_radius", "nest_layers", "layer_gap", "master_group"],
    "curve": ["curve_lane_spacing", "curve_master_spacing", "curve_corridor_height",
              "curve_sound_range", "curve_minecart", "curve_master_layers"],
}

# 原版方块画廊：按用途分组，配合输入框让用户既能点选也能填模组 ID
BLOCK_GALLERY = [
    {"id": "wood", "name": "木材", "blocks": [
        {"id": "minecraft:oak_planks", "name": "橡木木板"},
        {"id": "minecraft:spruce_planks", "name": "云杉木板"},
        {"id": "minecraft:birch_planks", "name": "白桦木板"},
        {"id": "minecraft:jungle_planks", "name": "丛林木板"},
        {"id": "minecraft:acacia_planks", "name": "金合欢木板"},
        {"id": "minecraft:dark_oak_planks", "name": "深色橡木木板"},
        {"id": "minecraft:crimson_planks", "name": "绯红木板"},
        {"id": "minecraft:warped_planks", "name": "诡异木板"},
        {"id": "minecraft:oak_log", "name": "橡木原木"},
        {"id": "minecraft:stripped_oak_log", "name": "去皮橡木"},
    ]},
    {"id": "stone", "name": "石材", "blocks": [
        {"id": "minecraft:stone", "name": "石头"},
        {"id": "minecraft:smooth_stone", "name": "平滑石头"},
        {"id": "minecraft:stone_bricks", "name": "石砖"},
        {"id": "minecraft:cobblestone", "name": "圆石"},
        {"id": "minecraft:deepslate", "name": "深板岩"},
        {"id": "minecraft:polished_deepslate", "name": "磨制深板岩"},
        {"id": "minecraft:granite", "name": "花岗岩"},
        {"id": "minecraft:diorite", "name": "闪长岩"},
        {"id": "minecraft:andesite", "name": "安山岩"},
        {"id": "minecraft:blackstone", "name": "黑石"},
        {"id": "minecraft:calcite", "name": "方解石"},
        {"id": "minecraft:tuff", "name": "凝灰岩"},
    ]},
    {"id": "wool", "name": "羊毛与混凝土", "blocks": [
        {"id": "minecraft:white_wool", "name": "白色羊毛"},
        {"id": "minecraft:light_gray_wool", "name": "淡灰色羊毛"},
        {"id": "minecraft:gray_wool", "name": "灰色羊毛"},
        {"id": "minecraft:black_wool", "name": "黑色羊毛"},
        {"id": "minecraft:red_wool", "name": "红色羊毛"},
        {"id": "minecraft:orange_wool", "name": "橙色羊毛"},
        {"id": "minecraft:yellow_wool", "name": "黄色羊毛"},
        {"id": "minecraft:lime_wool", "name": "黄绿色羊毛"},
        {"id": "minecraft:cyan_wool", "name": "青色羊毛"},
        {"id": "minecraft:blue_wool", "name": "蓝色羊毛"},
        {"id": "minecraft:purple_wool", "name": "紫色羊毛"},
        {"id": "minecraft:pink_wool", "name": "粉红色羊毛"},
        {"id": "minecraft:white_concrete", "name": "白色混凝土"},
        {"id": "minecraft:gray_concrete", "name": "灰色混凝土"},
        {"id": "minecraft:black_concrete", "name": "黑色混凝土"},
    ]},
    {"id": "metal", "name": "矿物与功能方块", "blocks": [
        {"id": "minecraft:copper_block", "name": "铜块"},
        {"id": "minecraft:iron_block", "name": "铁块"},
        {"id": "minecraft:gold_block", "name": "金块"},
        {"id": "minecraft:netherite_block", "name": "下界合金块"},
        {"id": "minecraft:quartz_block", "name": "石英块"},
        {"id": "minecraft:smooth_quartz", "name": "平滑石英"},
        {"id": "minecraft:amethyst_block", "name": "紫水晶块"},
        {"id": "minecraft:redstone_block", "name": "红石块"},
        {"id": "minecraft:redstone_lamp", "name": "红石灯"},
        {"id": "minecraft:glass", "name": "玻璃"},
        {"id": "minecraft:tinted_glass", "name": "遮光玻璃"},
        {"id": "minecraft:barrier", "name": "屏障"},
        {"id": "minecraft:packed_ice", "name": "浮冰"},
        {"id": "minecraft:blue_ice", "name": "蓝冰"},
        {"id": "minecraft:obsidian", "name": "黑曜石"},
        {"id": "minecraft:magma_block", "name": "岩浆块"},
    ]},
]

_BLOCK_NAME_OVERRIDES = {
    "minecraft:oak_planks": "橡木木板",
    "minecraft:white_wool": "白色羊毛",
}


def pretty_block_name(block: str) -> str:
    """把方块 ID 转成可读名称；有原版中文名时优先使用。"""
    raw = str(block or "").strip()
    for group in BLOCK_GALLERY:
        for item in group["blocks"]:
            if item["id"] == raw:
                return item["name"]
    if raw in _BLOCK_NAME_OVERRIDES:
        return _BLOCK_NAME_OVERRIDES[raw]
    return raw.split(":")[-1].replace("_", " ")


def is_valid_block_id(value: str) -> bool:
    return bool(_BLOCK_RE.match(str(value or "").strip()))

_INT_RANGES = {
    "flat_spacing": (1, 32),
    "layout_radius": (1, 128),
    "nest_layers": (1, 16),
    "layer_gap": (1, 32),
    "curve_lane_spacing": (3, 24),
    "curve_master_spacing": (3, 24),
    "curve_corridor_height": (2, 8),
    "lyrics_char_threshold": (0, 100000),
    "lyrics_max_chars": (0, 80),
    "lyrics_start_lead_ticks": (0, 200),
    "lyrics_enter": (0, 80),
    "lyrics_exit": (0, 80),
    "lyrics_y_offset": (-32, 64),
    "lyrics_lead_distance": (0, 256),
    "lyrics_chain_y_offset": (-8, 16),
    "particles_trail": (1, 400),
    "particles_height": (0, 64),
    "particles_linger": (-1, 100000),
}


def _clean_path(raw: str) -> str:
    text = str(raw or "").strip().strip('"').strip("'")
    return os.path.abspath(os.path.expanduser(text)) if text else ""


def _clean_block(value: str, fallback: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return fallback
    if "[" in text:
        text = text.split("[", 1)[0]
    if ":" not in text:
        text = f"minecraft:{text}"
    if not _BLOCK_RE.match(text):
        raise ValueError(f"方块 ID 不合法：{value}")
    return text


def _coerce(key: str, value, current):
    """把网页传来的值限制在允许的类型与范围内。"""
    if key in ("empty_block", "support_block"):
        return _clean_block(value, current)
    if key == "layout_style":
        allowed = {item["id"] for item in LAYOUT_STYLES}
        if value not in allowed:
            raise ValueError(f"未知排版：{value}")
        return value
    if key == "master_group":
        if value is None or value == "":
            return None
        return max(0, int(value))
    if key == "group_offsets":
        if not isinstance(value, dict):
            raise ValueError("group_offsets 必须是对象")
        cleaned = {}
        for gid, offset in value.items():
            if not isinstance(offset, dict):
                continue
            try:
                cleaned[str(int(gid))] = {
                    "y": int(offset.get("y", 0)),
                    "side": int(offset.get("side", 0)),
                }
            except (TypeError, ValueError):
                continue
        return cleaned
    if key in ("curve_master_layers", "stereo_layers", "particles_layers"):
        if not isinstance(value, list):
            raise ValueError(f"{key} 必须是数组")
        return sorted({int(item) for item in value})
    if key == "layer_groups":
        if not isinstance(value, list):
            raise ValueError("layer_groups 必须是二维数组")
        cleaned = []
        for members in value:
            if isinstance(members, list):
                cleaned.append([int(item) for item in members])
        return cleaned
    if key == "group_staircase_modes":
        if not isinstance(value, dict):
            raise ValueError("group_staircase_modes 必须是对象")
        return {str(int(k)): ("staircase" if v == "staircase" else "default")
                for k, v in value.items()}
    if key in ("curve_sound_range",):
        return max(8.0, min(128.0, float(value)))
    if key in ("lyrics_jitter_x", "lyrics_jitter_y", "lyrics_jitter_z"):
        return max(0.0, min(32.0, float(value)))
    if key == "lyrics_scale":
        return max(1.0, min(64.0, float(value)))
    if key == "mc_assets_dir":
        return str(value or "").strip()
    if key == "particles_curve":
        text = str(value or "arc").strip().lower()
        return text if text in ("arc", "sine", "line") else "arc"
    if key == "particles_height":
        return max(0.5, min(64.0, float(value)))
    if key in ("particles_color", "particles_gradient"):
        return re.sub(r"[ ;|]", "", str(value or ""))[:64]
    if key == "lyrics_import":
        if not isinstance(value, list):
            raise ValueError(f"{key} 必须是数组")
        return json.loads(json.dumps(value, ensure_ascii=False))
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int):
        number = int(value)
        low, high = _INT_RANGES.get(key, (-1_000_000, 1_000_000))
        return max(low, min(high, number))
    if isinstance(current, float):
        return float(value)
    if isinstance(current, dict):
        if not isinstance(value, dict):
            raise ValueError(f"{key} 必须是对象")
        return value
    if isinstance(current, list):
        if not isinstance(value, list):
            raise ValueError(f"{key} 必须是数组")
        return value
    return str(value)


class Session:
    def __init__(self, textures: TextureLibrary) -> None:
        self.textures = textures
        self.lock = threading.RLock()
        self.song = None
        self.song_path = ""
        self.virtual_layers: List[dict] = []
        self.layer_to_group: Dict[int, int] = {}
        self.config = default_config()
        self.max_tick = 0
        self._preview_cache: Optional[Tuple[str, dict]] = None
        self._config_override: Optional[dict] = None

    # ── 歌曲 ─────────────────────────────────────────────────
    def load_song(self, raw_path: str) -> dict:
        path = _clean_path(raw_path)
        if not path:
            raise ValueError("请先填写 .nbs 文件路径")
        if not os.path.isfile(path):
            raise ValueError(f"文件不存在：{path}")
        if not path.lower().endswith(".nbs"):
            raise ValueError("只支持 .nbs 文件")
        if os.path.getsize(path) > 64 * 1024 * 1024:
            raise ValueError("文件过大（超过 64MB）")

        from ..app import _load_song, build_virtual_layers  # 延迟导入，避免循环依赖

        song = _load_song(path)
        if song is None:
            raise ValueError("NBS 解析失败或文件里没有音符")

        with self.lock:
            self.song = song
            self.song_path = path
            self.config = load_song_config(path)
            self.virtual_layers = build_virtual_layers(
                song, self.config.get("stereo_layers", []))
            self.layer_to_group = self._apply_group_list(
                self.config.get("layer_groups") or [])
            self.max_tick = max((note.tick for note in song.notes), default=0)
            self.config.setdefault("curve_master_layers", [])
            self._invalidate()
        return self.state()

    def _apply_group_list(self, groups) -> Dict[int, int]:
        """把 [[vid, ...], ...] 变成 vid → 组号；没覆盖到的音轨各自成组。"""
        vids = [layer["vid"] for layer in self.virtual_layers]
        valid = set(vids)
        mapping: Dict[int, int] = {}
        gid = 0
        for members in groups or []:
            if not isinstance(members, list):
                continue
            chosen = []
            for item in members:
                try:
                    vid = int(item)
                except (TypeError, ValueError):
                    continue
                if vid in valid and vid not in mapping and vid not in chosen:
                    chosen.append(vid)
            if not chosen:
                continue
            for vid in chosen:
                mapping[vid] = gid
            gid += 1
        for vid in vids:
            if vid not in mapping:
                mapping[vid] = gid
                gid += 1
        return mapping

    def require_song(self):
        if self.song is None:
            raise ValueError("请先加载 NBS 歌曲")
        return self.song

    # ── 配置/分组 ────────────────────────────────────────────
    def _invalidate(self) -> None:
        self._preview_cache = None

    def _persist(self) -> None:
        if self.song is not None and self.song_path:
            name = getattr(getattr(self.song, "header", None), "song_name", "") or ""
            save_song_config(self.song_path, self.config, name)

    def set_config(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ValueError("配置必须是对象")
        with self.lock:
            updated = dict(self.config)
            for key, value in patch.items():
                if key not in DEFAULT_CONFIG:
                    continue
                updated[key] = _coerce(key, value, self.config.get(key))
            self.config = updated
            if "stereo_layers" in patch and self.song is not None:
                self.rebuild_layers()
            self._invalidate()
            self._persist()
        return self.state()

    def set_groups(self, groups: List[List[int]]) -> dict:
        if not isinstance(groups, list):
            raise ValueError("groups 必须是二维数组")
        with self.lock:
            self.layer_to_group = self._apply_group_list(groups)
            ordered: Dict[int, List[int]] = {}
            for vid, gid in self.layer_to_group.items():
                ordered.setdefault(gid, []).append(vid)
            self.config["layer_groups"] = [ordered[gid] for gid in sorted(ordered)]
            self._invalidate()
            self._persist()
        return self.state()

    def auto_group(self, mode: int = 1) -> dict:
        self.require_song()
        mode = max(1, min(3, int(mode)))
        mapping = auto_group_by_mode_on_layers(self.virtual_layers, mode)
        with self.lock:
            self.layer_to_group = mapping
            self._invalidate()
        return self.state()

    def set_offsets(self, offsets: dict) -> dict:
        cleaned = _coerce("group_offsets", offsets, self.config.get("group_offsets"))
        with self.lock:
            self.config["group_offsets"] = cleaned
            self._invalidate()
            self._persist()
        return self.state()

    def reset_offsets(self) -> dict:
        return self.set_offsets({})

    def rebuild_layers(self) -> None:
        """立体声设置变化后重建虚拟层，并保留合法分组。"""
        from ..app import build_virtual_layers

        song = self.require_song()
        with self.lock:
            old = self.layer_to_group
            self.virtual_layers = build_virtual_layers(
                song, self.config.get("stereo_layers", []))
            used: Dict[int, int] = {}
            next_gid = 0
            for layer in self.virtual_layers:
                vid = layer["vid"]
                if vid in old:
                    used[vid] = old[vid]
                else:
                    used[vid] = next_gid
                    next_gid += 1
            self.layer_to_group = used
            self._invalidate()

    # ── 状态 ─────────────────────────────────────────────────
    def state(self) -> dict:
        with self.lock:
            song = self.song
            if song is None:
                return {
                    "song": None,
                    "config": self.config,
                    "groups": [],
                    "tracks": [],
                    "styles": LAYOUT_STYLES,
                    "paramSpecs": PARAM_SPECS,
                    "layoutParams": _LAYOUT_PARAMS,
                    "assets": self.textures.status(),
                    "instrumentBlocks": self._instrument_blocks(),
                    "blockGallery": BLOCK_GALLERY,
                    "defaultOutput": str(Path.cwd() / "redstone_music.schem"),
                }

            layers = {layer.id: layer for layer in getattr(song, "layers", [])}
            master = set(self.config.get("curve_master_layers") or [])
            tracks = []
            for layer in self.virtual_layers:
                original = layers.get(layer.get("original_id"))
                name = getattr(original, "name", "") or f"音轨{layer.get('original_id')}"
                if layer.get("side") == "stereo":
                    name += "（立体声）"
                tracks.append({
                    "vid": layer["vid"],
                    "id": layer.get("original_id"),
                    "name": name,
                    "category": layer.get("cat", ""),
                    "notes": len(layer.get("notes") or []),
                    "group": self.layer_to_group.get(layer["vid"], layer["vid"]),
                    "stereo": layer.get("side") == "stereo",
                    "master": layer.get("original_id") in master,
                    "volume": self._layer_attr(layers.get(layer.get("original_id")), "volume", 100),
                    "pan": self._layer_attr(layers.get(layer.get("original_id")), "panning", 0),
                })

            groups = []
            for gid in sorted(set(self.layer_to_group.values())):
                members = [layer for layer in self.virtual_layers
                           if self.layer_to_group.get(layer["vid"]) == gid]
                groups.append({
                    "gid": gid,
                    "vids": [layer["vid"] for layer in members],
                    "noteCount": sum(len(layer.get("notes") or []) for layer in members),
                })

            header = getattr(song, "header", None)
            return {
                "song": {
                    "path": self.song_path,
                    "name": getattr(header, "song_name", "") or "未命名曲目",
                    "author": getattr(header, "song_author", "") or "未知作者",
                    "notes": len(song.notes),
                    "layers": len(getattr(song, "layers", []) or []),
                    "maxTick": self.max_tick,
                    "seconds": round(self.max_tick / 10.0, 1),
                    "previewTicks": min(self.max_tick, DEFAULT_PREVIEW_TICKS),
                },
                "config": self.config,
                "groups": groups,
                "tracks": tracks,
                "styles": LAYOUT_STYLES,
                "paramSpecs": PARAM_SPECS,
                "layoutParams": _LAYOUT_PARAMS,
                "assets": self.textures.status(),
                "instrumentBlocks": self._instrument_blocks(),
                "blockGallery": BLOCK_GALLERY,
                "defaultOutput": str(Path.cwd() / "redstone_music.schem"),
            }

    @staticmethod
    def _layer_attr(layer, key: str, fallback):
        try:
            return float(getattr(layer, key, fallback)) if layer is not None else fallback
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _instrument_blocks() -> List[dict]:
        return [
            {"id": inst, "name": INSTRUMENT_NAMES.get(inst, str(inst)),
             "block": INSTRUMENT_BLOCK_MAP.get(inst, "minecraft:stone")}
            for inst in sorted(INSTRUMENT_BLOCK_MAP)
        ]

    # ── 标记与预览 ───────────────────────────────────────────
    def markers(self) -> dict:
        with self.lock:
            style = self.config.get("layout_style", "flat")
            if style == "curve" or not self.virtual_layers:
                return {"editable": False, "items": [],
                        "reason": "音量控制排版的落点由音量公式决定，不能手动调整。"}
            groups = sorted(set(self.layer_to_group.values()))
            try:
                offsets = compute_offsets(groups, self.config, len(groups))
            except Exception:  # noqa: BLE001 - 参数异常时退回默认偏移
                offsets = [(0, 0)] * len(groups)
            offsets = apply_offset_overrides(groups, offsets, self.config)
            items = []
            for index, gid in enumerate(groups):
                y_off, side_off = offsets[index]
                members = [layer for layer in self.virtual_layers
                           if self.layer_to_group.get(layer["vid"]) == gid]
                labels = []
                for layer in members:
                    labels.append(layer.get("cat") or f"音轨{layer.get('original_id')}")
                items.append({
                    "gid": gid,
                    "label": f"组 {gid + 1}",
                    "x": -1,
                    "y": int(y_off),
                    "z": int(side_off),
                    "side": int(side_off),
                    "tracks": [layer["vid"] for layer in members],
                    "names": labels[:4],
                    "notes": sum(len(layer.get("notes") or []) for layer in members),
                })
            return {"editable": True, "items": items, "reason": ""}

    def preview(self, max_tick: Optional[int] = None) -> dict:
        self.require_song()
        limit = self.max_tick if max_tick is None else max(0, min(self.max_tick, int(max_tick)))
        key = json.dumps({
            "config": self.config,
            "groups": sorted(self.layer_to_group.items()),
            "tick": limit,
        }, sort_keys=True, ensure_ascii=False)
        with self.lock:
            if self._preview_cache and self._preview_cache[0] == key:
                return self._preview_cache[1]
        if self.config.get("layout_style") == "curve":
            self.rebuild_master_layers()

        from ..app import build_commands

        meta: dict = {}
        commands = build_commands(self.config, self.song, self.virtual_layers,
                                  self.layer_to_group, limit, meta_out=meta)
        blocks, palette, truncated = self._parse_commands(commands)
        warnings = self._warnings(meta, truncated)
        palette_counts = Counter(palette[entry[3]] for entry in blocks)
        bounds = _bounds(blocks)
        markers = self.markers()
        note_blocks = sum(count for block, count in palette_counts.items()
                          if block.startswith("minecraft:note_block"))
        voice_ids = sorted({note.instrument for layer in self.virtual_layers
                            for note in layer.get("notes", [])})
        result = {
            "palette": palette,
            "blocks": blocks,
            "bounds": bounds,
            "counts": [{"block": block, "count": count}
                       for block, count in palette_counts.most_common(24)],
            "stats": {
                "blocks": len(blocks),
                "noteBlocks": note_blocks,
                "voices": len(voice_ids),
                "voiceNames": [INSTRUMENT_NAMES.get(inst, str(inst)) for inst in voice_ids],
                "groups": len(set(self.layer_to_group.values())),
                "tracks": len(self.virtual_layers),
                "ticks": limit,
                "truncated": truncated,
                "style": self.config.get("layout_style", "flat"),
            },
            "markers": markers,
            "warnings": warnings,
        }
        with self.lock:
            self._preview_cache = (key, result)
        return result

    def rebuild_master_layers(self) -> None:
        """主音轨是按原始音轨 ID 配置的，立体声变化后保持配置即可。"""
        return None

    def _warnings(self, meta: dict, truncated: bool) -> List[str]:
        warnings: List[str] = []
        plan = meta.get("plan") or {}
        if plan.get("smoothed"):
            warnings.append(
                f"有 {plan['smoothed']} 个音符的相邻音量跨度超过红石线 15 格限制，"
                f"已只对这些相邻音符做最小幅度平滑。")
        if plan.get("dropped"):
            warnings.append(
                f"同一音轨同一 tick 有 {plan['dropped']} 个音符挤在一格，"
                f"已只保留最响的一个。")
        if plan.get("unreachable"):
            warnings.append(
                f"有 {plan['unreachable']} 个音符因轨道离走廊太远，"
                f"满音量在几何上无法还原，已放到可达的最近位置。")
        if plan.get("deviations"):
            warnings.append(
                f"有 {plan['deviations']} 个 tick 的实际音量与 NBS 目标偏差超过 6%"
                f"（轨道与目标位置冲突），可减小音轨间距或减少轨道层数。")
        if meta.get("note_overheads"):
            warnings.append(
                f"有 {meta['note_overheads']} 个音符盒正上方被占用，可能影响发声。")
        if meta.get("crossing"):
            warnings.append(
                f"检测到 {meta['crossing']} 处红石粉与其它轨道相邻，存在串线风险。")
        if meta.get("conflicts"):
            warnings.append(
                f"有 {len(meta['conflicts'])} 处方块位置被覆盖冲突，结构可能不完整。")
        if truncated:
            warnings.append(
                f"方块数超过预览上限（{MAX_PREVIEW_BLOCKS}），已截断显示；"
                f"缩小预览 tick 范围可看到完整局部。")
        if self.config.get("layout_style") == "curve" and (
                self.config.get("lyrics_import")
                or (self.config.get("particles_enabled")
                    and self.config.get("particles_layers"))):
            warnings.append("音量控制排版暂不支持歌词 / 粒子轨道，生成时会自动跳过。")
        return warnings

    @staticmethod
    def _parse_commands(commands: List[str]) -> Tuple[List[list], List[str], bool]:
        palette: Dict[str, int] = {}
        positions: Dict[Tuple[int, int, int], int] = {}
        truncated = False

        def put(x: int, y: int, z: int, block: str) -> bool:
            if block not in palette:
                palette[block] = len(palette)
            positions[(x, y, z)] = palette[block]
            return len(positions) < MAX_PREVIEW_BLOCKS

        for line in commands:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = _SETBLOCK_RE.match(line)
            if match:
                if not put(int(match.group(1)), int(match.group(2)),
                           int(match.group(3)), match.group(4)):
                    truncated = True
                    break
                continue
            match = _FILL_RE.match(line)
            if match:
                x1, y1, z1 = (int(match.group(1)), int(match.group(2)),
                              int(match.group(3)))
                x2, y2, z2 = (int(match.group(4)), int(match.group(5)),
                              int(match.group(6)))
                block = match.group(7)
                done = True
                for x in range(min(x1, x2), max(x1, x2) + 1):
                    for y in range(min(y1, y2), max(y1, y2) + 1):
                        for z in range(min(z1, z2), max(z1, z2) + 1):
                            if not put(x, y, z, block):
                                truncated = True
                                done = False
                                break
                        if not done:
                            break
                    if not done:
                        break
                if not done:
                    break
        blocks = [[x, y, z, index] for (x, y, z), index in positions.items()]
        return blocks, list(palette), truncated

    # ── 导出 ─────────────────────────────────────────────────
    def generate(self, raw_path: str, max_tick: Optional[int] = None) -> dict:
        self.require_song()
        path = _clean_path(raw_path)
        if not path:
            raise ValueError("请填写输出路径")
        if not path.lower().endswith(".schem"):
            path += ".schem"
        folder = Path(path).parent
        if not folder.is_dir():
            folder.mkdir(parents=True, exist_ok=True)

        limit = self.max_tick if max_tick is None else max(0, min(self.max_tick, int(max_tick)))
        from ..app import build_commands
        from ..generation import generate_schematic_from_commands

        meta: dict = {}
        commands = build_commands(self.config, self.song, self.virtual_layers,
                                  self.layer_to_group, limit, meta_out=meta)
        ok = generate_schematic_from_commands(commands, path)
        if not ok:
            raise ValueError("生成结构失败，请查看控制台日志")
        saved = Path(path)
        if not saved.exists():
            raise ValueError("结构文件没有写成，请检查路径权限")
        blocks, _, _ = self._parse_commands(commands)
        return {
            "path": str(saved),
            "size": saved.stat().st_size,
            "blocks": len(blocks),
            "ticks": limit,
            "warnings": self._warnings(meta, False),
            "layout": self.config.get("layout_style", "flat"),
        }


def _bounds(blocks: List[list]) -> dict:
    if not blocks:
        return {"min": [0, 0, 0], "max": [0, 0, 0], "size": [0, 0, 0]}
    xs = [entry[0] for entry in blocks]
    ys = [entry[1] for entry in blocks]
    zs = [entry[2] for entry in blocks]
    low = [min(xs), min(ys), min(zs)]
    high = [max(xs), max(ys), max(zs)]
    return {"min": low, "max": high,
            "size": [high[i] - low[i] + 1 for i in range(3)]}
