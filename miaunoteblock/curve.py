"""曲线音量排版（curve）：严格 ABAB 轨道 + 左右叠加音量。

轨道规律（与参考结构 linear/volum/volum2 一致）
------------------------------------------------
每个歌曲 tick 固定占 2 格 X：

    X:   ...  R   B   R   B   R   B  ...
               ^   ^
               |   +-- B：音符盒（空 tick 用橡木木板，被强充能但不发声）
               +------ R：中继器（delay=1，强充能身后的 B）

- 横向（Z）位置可以逐 tick 变化，但 X 规律不变；
- 横向移动由 B 所在那一列的红石线连接：R 从旧 Z 输出，
  红石线沿 Z 走到新的 Z，末端紧贴音符盒；
- 红石线单段最多 15 格，所以相邻 tick 的横向距离变化 ≤15（加上音符盒本身 = 16 格），
  超过就会被平滑，并在生成时警告（对应音量变化约 66.7%）。

音量模型
--------
- 普通音轨：每个虚拟层左右各一条 ABAB 轨道，每盒承担 V/2，
  ``d = 48 × (1 − V/2)``（V=100% → 24 格，V=0 → 48 格），左右叠加还原 NBS 力度。
- 主音轨（用户多选）：无视 NBS 力度，固定满音量，放在中心轴走廊上方/下方，
  上下数量尽量均匀。
- 所有方块下方垫白色羊毛作为支撑。
"""

import json
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .constants import FALLING_BLOCK_SUPPORT, INSTRUMENT_BLOCK_MAP
from .generation import get_note_key, note_to_pitch
from .volume import (
    clamp,
    dual_distance,
    layer_volume,
    lateral_from_3d,
    note_velocity,
    volume_from_distance,
)

DUST = "minecraft:redstone_wire"
WOOL = "minecraft:white_wool"
EMPTY = "minecraft:oak_planks"     # 空音符：实心方块，导电但不发声
TRUNK_FACING = "west"              # 沿 +X 前进：facing=west 时输出朝 +X
MINECART_TAG = "miau_cart"

STEP = 2                           # 每个歌曲 tick 沿 +X 前进 2 格（R + B）
MAX_LATERAL_STEP = 15              # 红石线单段上限（16 格 - 音符盒本身）
WARN_LATERAL = 16                  # 超过它提示音量变化 >66.7%
MIN_DUAL_DISTANCE = 24.0           # 左右叠加满音量时的单盒距离
MAX_DISTANCE = 48.0


class BlockWriter:
    """坐标 → 方块串，后写覆盖先写，并记录冲突（用于自检）。"""

    def __init__(self) -> None:
        self.blocks: Dict[Tuple[int, int, int], str] = {}
        self.conflicts: List[Tuple[Tuple[int, int, int], str, str]] = []

    def set(self, position: Tuple[int, int, int], block: str) -> None:
        old = self.blocks.get(position)
        if old is not None and old != block:
            self.conflicts.append((position, old, block))
        self.blocks[position] = block

    def commands(self) -> List[str]:
        commands = ["# 红石音乐生成器（曲线音量排版 + 隐形矿车跟随）"]
        for position in sorted(self.blocks):
            x, y, z = position
            commands.append(f"setblock {x} {y} {z} {self.blocks[position]}")
        return commands


def _instrument_blocks(instrument: int) -> List[str]:
    if instrument == 0:
        return []
    blocks = [INSTRUMENT_BLOCK_MAP.get(instrument, "minecraft:stone")]
    if instrument == 3:
        blocks.append(FALLING_BLOCK_SUPPORT)
    return blocks


def _repeater(delay: int = 1) -> str:
    return f"minecraft:repeater[facing={TRUNK_FACING},delay={delay}]"


def _unique_notes(notes, max_tick: int, song=None, original_id: int = 0):
    """按 tick 收集音符并去重；同 tick 多个音符只保留最响的（其余计入丢弃）。"""
    by_tick: Dict[int, List[dict]] = defaultdict(list)
    seen = set()
    dropped = 0
    for note in notes:
        if not (0 <= note.tick <= max_tick):
            continue
        signature = (note.tick, get_note_key(note), note.instrument, note.velocity)
        if signature in seen:
            continue
        seen.add(signature)
        by_tick[note.tick].append({
            "note": note,
            "volume": note_velocity(note) * layer_volume(song, original_id),
        })
    for tick, items in by_tick.items():
        if len(items) > 1:
            items.sort(key=lambda item: item["volume"], reverse=True)
            dropped += len(items) - 1
            by_tick[tick] = items[:1]
    return by_tick, dropped


def _smooth(distances: List[int], low: float, high: float) -> Tuple[List[int], int]:
    """逐 tick 限制横向变化，返回 (新距离, 被平滑次数)。"""
    result: List[int] = []
    previous: Optional[int] = None
    smoothed = 0
    for value in distances:
        current = int(round(clamp(value, low, high)))
        if previous is not None:
            limited = int(round(clamp(current, previous - MAX_LATERAL_STEP,
                                      previous + MAX_LATERAL_STEP)))
            if limited != current:
                smoothed += 1
            current = limited
        result.append(current)
        previous = current
    return result, smoothed


def _target_sequence(entries_by_tick: Dict[int, List[dict]], ticks: int,
                     low: float, high: float, vertical: float,
                     sound_range: float, master: bool) -> Tuple[List[int], int]:
    """每个 tick 的横向距离：有音符取最响，空白 tick 线性插值。"""
    desired: Dict[int, float] = {}
    for tick in range(ticks):
        entries = entries_by_tick.get(tick, [])
        if not entries:
            continue
        if master:
            desired[tick] = 0.0
        else:
            loudest = max(entry["volume"] for entry in entries)
            desired[tick] = lateral_from_3d(
                dual_distance(loudest, sound_range), vertical)
    raw: List[int] = []
    previous: Optional[float] = None
    previous_index: Optional[int] = None
    for index in range(ticks):
        if index in desired:
            value = desired[index]
        else:
            next_index = next((j for j in range(index + 1, ticks) if j in desired), None)
            if previous is not None and next_index is not None:
                start = previous_index if previous_index is not None else index
                span = max(1, next_index - start)
                value = previous + (desired[next_index] - previous) * (
                    (index - start) / span)
            elif previous is not None:
                value = previous
            elif next_index is not None:
                value = desired[next_index]
            else:
                value = low
        raw.append(int(round(clamp(value, low, high))))
        previous = raw[-1]
        previous_index = index
    return _smooth(raw, low, high)


def plan_curve(virtual_layers: List[dict], layer_to_group: Dict[int, int],
               base: Tuple[int, int, int], max_tick: int, config: dict,
               song=None) -> dict:
    """计算每条 ABAB 轨道每个歌曲 tick 的落点。"""
    spacing = max(3, int(config.get("curve_lane_spacing", 3)))
    sound_range = float(config.get("curve_sound_range", 48.0))
    corridor = max(2, int(config.get("curve_corridor_height", 2)))
    master_spacing = max(3, int(config.get("curve_master_spacing", 3)))
    master_ids = set(config.get("curve_master_layers") or [])
    base_x, base_y, base_z = base
    player_y = base_y
    ticks = max_tick + 1

    master_layers: List[dict] = []
    normal_layers: List[dict] = []
    for virtual in virtual_layers:
        original_id = virtual.get("original_id", virtual["vid"])
        if original_id in master_ids:
            master_layers.append(virtual)
        else:
            normal_layers.append(virtual)

    lanes: Dict[Tuple, List[dict]] = {}
    placed: List[dict] = []
    smoothed = 0
    dropped_total = 0

    # ── 主音轨：中心轴走廊上下，固定满音量 ─────────────────
    up = down = 0
    for index, virtual in enumerate(master_layers):
        if index % 2 == 0:
            y = base_y + corridor + 1 + up * master_spacing
            up += 1
        else:
            y = base_y - corridor - 1 - down * master_spacing
            down += 1
        original_id = virtual.get("original_id", virtual["vid"])
        entries_by_tick, dropped = _unique_notes(virtual["notes"], max_tick, song, original_id)
        dropped_total += dropped
        distances, count = _target_sequence(
            entries_by_tick, ticks, 0.0, 0.0, abs(y + 0.5 - player_y),
            sound_range, master=True)
        smoothed += count
        nodes = []
        for tick in range(ticks):
            x = base_x + STEP * tick
            z = base_z
            entries = entries_by_tick.get(tick, [])
            node = {
                "tick": tick, "x": x, "z": z, "y": y, "side": 0,
                "entries": entries, "master": True,
                "distance": ((z - base_z) ** 2 + (y + 0.5 - player_y) ** 2) ** 0.5,
            }
            nodes.append(node)
            for entry in entries:
                placed.append({
                    "gid": None, "vid": virtual["vid"], "tick": tick,
                    "note": entry["note"], "side": 0,
                    "distance": node["distance"], "primary": True,
                    "volume_nbs": 1.0,
                    "volume_plan": volume_from_distance(node["distance"], sound_range),
                    "pos": (x + 1, y, z), "master": True,
                })
        lanes[("master", virtual["vid"])] = nodes

    # ── 普通音轨：每个虚拟层左右各一条 ABAB 轨道 ───────────
    total_layers = max(1, len(normal_layers))
    for index, virtual in enumerate(normal_layers):
        original_id = virtual.get("original_id", virtual["vid"])
        y = base_y + spacing * (index - (total_layers - 1) // 2)
        vertical = abs(y + 0.5 - player_y)
        entries_by_tick, dropped = _unique_notes(virtual["notes"], max_tick, song, original_id)
        dropped_total += dropped
        distances, count = _target_sequence(
            entries_by_tick, ticks, MIN_DUAL_DISTANCE, MAX_DISTANCE,
            vertical, sound_range, master=False)
        smoothed += count

        for side in (-1, 1):
            nodes = []
            for tick in range(ticks):
                x = base_x + STEP * tick
                distance = distances[tick]
                z = base_z + side * distance
                node_distance = (distance ** 2 + vertical ** 2) ** 0.5
                entries = entries_by_tick.get(tick, [])
                nodes.append({
                    "tick": tick, "x": x, "z": z, "y": y, "side": side,
                    "entries": entries, "master": False,
                    "distance": node_distance,
                })
                for entry in entries:
                    placed.append({
                        "gid": layer_to_group.get(virtual["vid"]),
                        "vid": virtual["vid"], "tick": tick,
                        "note": entry["note"], "side": side,
                        "distance": node_distance, "primary": True,
                        "volume_nbs": entry["volume"],
                        "volume_plan": volume_from_distance(node_distance, sound_range),
                        "pos": (x + 1, y, z), "master": False,
                    })
            lanes[("layer", virtual["vid"], side)] = nodes

    return {
        "lanes": lanes,
        "notes": placed,
        "player_y": player_y,
        "spacing": spacing,
        "corridor": corridor,
        "master_spacing": master_spacing,
        "sound_range": sound_range,
        "base": base,
        "smoothed": smoothed,
        "overflow": smoothed,
        "dropped": dropped_total,
    }


def build_curve_commands(virtual_layers: List[dict], layer_to_group: Dict[int, int],
                         base: Tuple[int, int, int], max_tick: int, config: dict,
                         direction: str = "east", song=None,
                         minecart: Optional[bool] = None) -> Tuple[List[str], dict, dict]:
    """生成 ABAB 曲线轨道 + 隐形矿车跟随。"""
    if direction != "east":
        raise ValueError("曲线排版当前只支持 east（+X）延展方向")

    plan = plan_curve(virtual_layers, layer_to_group, base, max_tick, config, song)
    writer = BlockWriter()
    base_x, base_y, base_z = base
    player_y = plan["player_y"]
    activations: Dict[str, Tuple[int, int, int]] = {}

    for key, nodes in plan["lanes"].items():
        if not nodes:
            continue
        y = nodes[0]["y"]
        if key[0] == "master":
            label = f"m{key[1]}"
        else:
            label = f"v{key[1]}{'R' if key[2] > 0 else 'L'}"
        previous_z = nodes[0]["z"]
        # 起手中继器：直接强充能第一个音符盒
        writer.set((base_x, y, previous_z), _repeater())
        writer.set((base_x, y - 1, previous_z), WOOL)
        activations[label] = (base_x - 1, y, previous_z)

        for index, node in enumerate(nodes):
            x = node["x"]
            z = node["z"]
            # 本 tick 的中继器与音符盒同 Z：中继器输出端正对音符盒 → 强充能
            if index > 0:
                writer.set((x, y, z), _repeater())
                writer.set((x, y - 1, z), WOOL)
                # 横向移动：红石线只负责把「上一个被强充能的音符盒/空音符」
                # 的信号送进本中继器的背面（正对中继器输入端），
                # 不依赖红石粉侧向充能任何方块。
                if z != previous_z:
                    step = 1 if z > previous_z else -1
                    for wire_z in range(previous_z + step, z + step, step):
                        writer.set((x - 1, y, wire_z), DUST)
                        writer.set((x - 1, y - 1, wire_z), WOOL)
            entries = node["entries"]
            if entries:
                note = entries[0]["note"]
                writer.set((x + 1, y, z),
                           f"minecraft:note_block[note={note_to_pitch(get_note_key(note))}]")
                for depth, block in enumerate(_instrument_blocks(note.instrument), start=1):
                    writer.set((x + 1, y - depth, z), block)
            else:
                writer.set((x + 1, y, z), EMPTY)
                writer.set((x + 1, y - 1, z), WOOL)
            previous_z = z

    meta = {
        "plan": plan,
        "player": {"x0": base_x + 1, "y": player_y, "z": base_z + 0.5},
        "minecart": None,
        "activations": activations,
    }
    want_cart = config.get("curve_minecart", True) if minecart is None else minecart
    any_nodes = next(iter(plan["lanes"].values()), [])
    if want_cart and any_nodes:
        max_distance = max((abs(entry["pos"][2] - base_z) for entry in plan["notes"]),
                           default=MIN_DUAL_DISTANCE)
        cart_z = base_z + int(max_distance) + 3

        writer.set((base_x, player_y, cart_z), _repeater())
        writer.set((base_x, player_y - 1, cart_z), WOOL)
        for index, node in enumerate(any_nodes):
            command_x = base_x + STEP * index + 1
            command = (f"tp @e[type=minecart,tag={MINECART_TAG},limit=1] "
                       f"~0.5 ~ ~{base_z + 0.5 - cart_z:.1f}")
            writer.set((command_x, player_y, cart_z),
                       f"minecraft:command_block[facing=up]"
                       f"{{Command:{json.dumps(command)}}}")
            writer.set((command_x, player_y - 1, cart_z), WOOL)
            writer.set((command_x + 1, player_y, cart_z), _repeater())
            writer.set((command_x + 1, player_y - 1, cart_z), WOOL)

        summon = (f"summon minecart ~0.5 ~-1 ~{base_z + 0.5 - cart_z:.1f} "
                  f"{{Invisible:1b,Silent:1b,NoGravity:1b,Tags:[\"{MINECART_TAG}\"]}}")
        ride = f"ride @p mount @e[type=minecart,tag={MINECART_TAG},limit=1]"
        writer.set((base_x - 1, player_y + 1, cart_z),
                   f"minecraft:command_block[facing=up]{{Command:{json.dumps(summon)}}}")
        writer.set((base_x - 1, player_y, cart_z + 1),
                   "minecraft:repeater[facing=north,delay=1]")
        writer.set((base_x - 1, player_y, cart_z + 2),
                   f"minecraft:command_block[facing=up]{{Command:{json.dumps(ride)}}}")
        activations["minecart"] = (base_x - 1, player_y, cart_z)
        meta["minecart"] = {"z": cart_z, "summon": summon, "ride": ride}

    commands = writer.commands()
    meta["conflicts"] = writer.conflicts
    meta["block_count"] = len(writer.blocks)
    return commands, activations, meta
