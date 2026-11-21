"""曲线音量排版（curve）：轨道自己拐弯，左右声道叠加还原音量。

模型
----
- **主音轨**（用户在菜单里多选）：固定满音量，无视 NBS 力度；放在中心轴
  （走廊）的上方/下方，上下数量尽量均匀。走廊默认 2 格高，主音轨离玩家
  至少 3 格，实际听感约 94%，但不再随力度衰减。
- **其余音轨**：左右各放一个同音高、同 tick 的音符盒，每个承担 V/2，
  距离 ``d = 48 × (1 − V/2)``（V=100% → 24 格，V=0 → 48 格）。
  两侧叠加后就是 NBS 的目标音量。
- 轨道每个歌曲 tick 沿 +X 前进 3 格，横向位置跟着目标距离走，看起来像
  会自己拐弯的半圆弧线；红石线瞬时传导，每 tick 固定 1 个中继器，
  所以拐弯不影响时间。
- 单段红石线 ≤ 15 格，因此相邻 tick 横向最多移动 13 格；空白 tick 会
  提前分摊位移，更剧烈的跳变会被平滑并在统计里报告。
- 所有红石线/中继器/命令方块下方垫羊毛，粘贴后不会掉落；结构自带启动
  红石块，粘贴即自动播放。
- 玩家沿 ``z = 0.5`` 走廊前进；普通轨道最近 24 格，主音轨在走廊上下，
  走廊永远为空。
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
TRUNK_FACING = "west"          # 沿 +X 前进：facing=west 时输出朝 +X
MINECART_TAG = "miau_cart"

STEP = 3                       # 每个歌曲 tick 沿 +X 前进的格数
MAX_LATERAL_STEP = 13          # 相邻 tick 横向位移上限（保证红石线 ≤15 格）
MIN_DUAL_DISTANCE = 24.0       # 左右叠加满音量时的单盒距离
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


def _unique_notes(virtual_layers: List[dict], max_tick: int, song=None) -> Dict[int, List[dict]]:
    """按 tick 收集音符，去掉立体声复制出来的重复项。"""
    by_tick: Dict[int, List[dict]] = defaultdict(list)
    seen = set()
    for virtual in virtual_layers:
        original_id = virtual.get("original_id", virtual["vid"])
        for note in virtual["notes"]:
            if not (0 <= note.tick <= max_tick):
                continue
            signature = (note.tick, get_note_key(note), note.instrument, note.velocity)
            if signature in seen:
                continue
            seen.add(signature)
            by_tick[note.tick].append({
                "note": note,
                "vid": virtual["vid"],
                "volume": note_velocity(note) * layer_volume(song, original_id),
            })
    return by_tick


def _smooth_distances(desired: Dict[int, float], total: int,
                      low: float, high: float) -> Tuple[List[int], int]:
    """把每 tick 的期望距离做插值 + 横向速度限制，返回 (距离列表, 平滑次数)。"""
    distances: List[int] = []
    smoothed = 0
    previous: Optional[float] = None
    previous_index: Optional[int] = None
    for index in range(total):
        if index in desired:
            value = desired[index]
        else:
            next_index = next((j for j in range(index + 1, total) if j in desired), None)
            if previous is not None and next_index is not None:
                start = previous_index if previous_index is not None else index
                span = max(1, next_index - start)
                ratio = (index - start) / span
                value = previous + (desired[next_index] - previous) * ratio
            elif previous is not None:
                value = previous
            elif next_index is not None:
                value = desired[next_index]
            else:
                value = low
        distance = int(round(clamp(value, low, high)))
        if previous is not None:
            limited = int(round(clamp(distance, previous - MAX_LATERAL_STEP,
                                      previous + MAX_LATERAL_STEP)))
            if limited != distance:
                smoothed += 1
            distance = limited
        distance = int(round(clamp(distance, low, high)))
        distances.append(distance)
        previous = distance
        previous_index = index
    return distances, smoothed


def plan_curve(virtual_layers: List[dict], layer_to_group: Dict[int, int],
               base: Tuple[int, int, int], max_tick: int, config: dict,
               song=None) -> dict:
    """计算主音轨与普通轨道每个歌曲 tick 的落点。"""
    groups = sorted(set(layer_to_group.values()))
    if not groups:
        return {"groups": [], "lanes": {}, "notes": [], "lane_y": {},
                "player_y": base[1], "sound_range": 48.0, "spacing": 3,
                "smoothed": 0, "base": base}

    spacing = max(3, int(config.get("curve_lane_spacing", 3)))
    sound_range = float(config.get("curve_sound_range", 48.0))
    corridor = max(2, int(config.get("curve_corridor_height", 2)))
    master_spacing = max(3, int(config.get("curve_master_spacing", 3)))
    master_ids = set(config.get("curve_master_layers") or [])
    base_x, base_y, base_z = base
    player_y = base_y

    # 每个歌曲 tick 都要有节点（包括休止），否则时值会被压缩
    ticks_all = list(range(0, max_tick + 1))
    total = len(ticks_all)

    master_layers: List[dict] = []
    group_layers: Dict[int, List[dict]] = defaultdict(list)
    for virtual in virtual_layers:
        original_id = virtual.get("original_id", virtual["vid"])
        if original_id in master_ids:
            master_layers.append(virtual)
        else:
            gid = layer_to_group.get(virtual["vid"])
            if gid is not None:
                group_layers[gid].append(virtual)

    lanes: Dict[object, List[dict]] = {}
    placed: List[dict] = []
    smoothed = 0

    # ── 主音轨：中心轴走廊上方/下方，固定满音量 ─────────────
    up = down = 0
    for index, virtual in enumerate(master_layers):
        if index % 2 == 0:
            y = base_y + corridor + 1 + up * master_spacing
            up += 1
        else:
            y = base_y - corridor - 1 - down * master_spacing
            down += 1
        notes_by_tick = _unique_notes([virtual], max_tick, song)
        vertical = abs(y + 0.5 - player_y)
        nodes = []
        for i, tick in enumerate(ticks_all):
            entries = []
            for offset, entry in enumerate(notes_by_tick.get(tick, [])):
                distance = ((offset) ** 2 + vertical ** 2) ** 0.5
                entries.append({**entry, "offset": offset, "distance": distance,
                                "volume_nbs": 1.0,
                                "volume_plan": volume_from_distance(distance, sound_range)})
                placed.append({
                    "gid": None, "vid": virtual["vid"], "tick": tick,
                    "note": entry["note"], "side": 0,
                    "distance": distance, "primary": offset == 0,
                    "volume_nbs": 1.0,
                    "volume_plan": volume_from_distance(distance, sound_range),
                    "pos": (base_x + 1 + STEP * i, y, base_z - offset),
                    "master": True,
                })
            nodes.append({
                "tick": tick, "x": base_x + 1 + STEP * i,
                "z_cell": base_z + 1, "sign": -1, "y": y,
                "distance": vertical, "entries": entries, "master": True,
            })
        lanes[("master", virtual["vid"])] = nodes

    # ── 普通音轨：左右各一条，叠加还原音量 ─────────────────
    lane_y = {gid: base_y + spacing * (i - (len(groups) - 1) // 2)
              for i, gid in enumerate(groups)}
    for gid in groups:
        layers = group_layers.get(gid, [])
        if not layers:
            continue
        notes_by_tick = _unique_notes(layers, max_tick, song)
        vertical = abs(lane_y[gid] + 0.5 - player_y)
        desired: Dict[int, float] = {}
        for i, tick in enumerate(ticks_all):
            entries = notes_by_tick.get(tick, [])
            if entries:
                loudest = max(entry["volume"] for entry in entries)
                desired[i] = lateral_from_3d(
                    dual_distance(loudest, sound_range), vertical)
        distances, count = _smooth_distances(
            desired, total, MIN_DUAL_DISTANCE, MAX_DISTANCE)
        smoothed += count

        for side in (-1, 1):
            nodes = []
            for i, tick in enumerate(ticks_all):
                distance = distances[i]
                self_distance = (distance ** 2 + vertical ** 2) ** 0.5
                entries = []
                for offset, entry in enumerate(notes_by_tick.get(tick, [])):
                    item_distance = ((distance + offset) ** 2 + vertical ** 2) ** 0.5
                    entries.append({**entry, "offset": offset,
                                    "distance": item_distance,
                                    "volume_nbs": entry["volume"],
                                    "volume_plan": volume_from_distance(
                                        item_distance, sound_range)})
                    placed.append({
                        "gid": gid, "vid": entry["vid"], "tick": tick,
                        "note": entry["note"], "side": side,
                        "distance": item_distance, "primary": offset == 0,
                        "volume_nbs": entry["volume"],
                        "volume_plan": volume_from_distance(item_distance, sound_range),
                        "pos": (base_x + 1 + STEP * i, lane_y[gid],
                                base_z + side * (distance + offset)),
                    })
                nodes.append({
                    "tick": tick, "x": base_x + 1 + STEP * i,
                    "z_cell": base_z + side * (distance - 1),
                    "sign": side, "y": lane_y[gid],
                    "distance": self_distance, "entries": entries, "master": False,
                })
            lanes[(gid, side)] = nodes

    return {
        "groups": groups,
        "lanes": lanes,
        "notes": placed,
        "lane_y": lane_y,
        "player_y": player_y,
        "spacing": spacing,
        "corridor": corridor,
        "master_spacing": master_spacing,
        "sound_range": sound_range,
        "base": base,
        "smoothed": smoothed,
        "dropped": 0,
    }


def build_curve_commands(virtual_layers: List[dict], layer_to_group: Dict[int, int],
                         base: Tuple[int, int, int], max_tick: int, config: dict,
                         direction: str = "east", song=None,
                         minecart: Optional[bool] = None) -> Tuple[List[str], dict, dict]:
    """生成曲线轨道 + 隐形矿车跟随的完整命令列表。"""
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
        label = (f"m{key[1]}" if isinstance(key, tuple) and key[0] == "master"
                 else f"g{key[0]}{'R' if key[1] > 0 else 'L'}")
        writer.set((base_x, y, nodes[0]["z_cell"]), _repeater())
        writer.set((base_x, y - 1, nodes[0]["z_cell"]), WOOL)
        activations[label] = (base_x - 1, y, nodes[0]["z_cell"])

        for index, node in enumerate(nodes):
            x = node["x"]
            z = node["z_cell"]
            writer.set((x, y, z), DUST)
            writer.set((x, y - 1, z), WOOL)

            if index > 0:
                previous = nodes[index - 1]
                forward_x = previous["x"] + 1
                writer.set((forward_x, y, previous["z_cell"]), DUST)
                writer.set((forward_x, y - 1, previous["z_cell"]), WOOL)
                if z != previous["z_cell"]:
                    step = 1 if z > previous["z_cell"] else -1
                    for wire_z in range(previous["z_cell"] + step, z + step, step):
                        writer.set((forward_x, y, wire_z), DUST)
                        writer.set((forward_x, y - 1, wire_z), WOOL)
                writer.set((x - 1, y, z), _repeater())
                writer.set((x - 1, y - 1, z), WOOL)

            entries = sorted(node["entries"], key=lambda item: item["note"].layer)
            for offset, entry in enumerate(entries):
                note_z = z + node["sign"] * (offset + 1)
                note = entry["note"]
                writer.set((x, y, note_z),
                           f"minecraft:note_block[note={note_to_pitch(get_note_key(note))}]")
                for depth, block in enumerate(_instrument_blocks(note.instrument), start=1):
                    writer.set((x, y - depth, note_z), block)

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
            command_x = base_x + 1 + 2 * index
            command = (f"tp @e[type=minecart,tag={MINECART_TAG},limit=1] "
                       f"~{node['x'] + 0.5 - command_x:.1f} ~ ~{base_z + 0.5 - cart_z:.1f}")
            writer.set((command_x, player_y, cart_z),
                       f"minecraft:command_block[facing=up]"
                       f"{{Command:{json.dumps(command)}}}")
            writer.set((command_x, player_y - 1, cart_z), WOOL)
            writer.set((command_x + 1, player_y, cart_z), _repeater())
            writer.set((command_x + 1, player_y - 1, cart_z), WOOL)

        summon = (f"summon minecart ~1.5 ~-1 ~{base_z + 0.5 - cart_z:.1f} "
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
