"""音量控制排版（curve）：严格 ABAB 轨道 + 左右叠加音量。

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
- 以走廊中心为听者位置 ``eye_y = base_y + corridor / 2``；
- 每条轨道的垂直偏移 ``vertical = (y + 0.5) - eye_y`` 也计入距离，
  横向距离由勾股定理反解：``lateral = sqrt(target² - vertical²)``，
  其中 ``target = 48 × (1 − V/2)``；
- 若垂直差已经超过目标距离（高音量 + 高轨道），横向取最小值并记录「不可达」警告；
- 普通音轨左右各一条 ABAB 轨道，每盒承担 V/2，左右叠加还原 NBS 力度；
- 主音轨（用户多选）：无视 NBS 力度，固定满音量，放在走廊外上方/下方；
- 所有轨道使用互不相同的 Y 层（间距 ≥ 2 格），保证音符盒上方不会被其它
  轨道占用，红石粉也落在不同层、不会串线。
"""

import json
import math
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .constants import INSTRUMENT_BLOCK_MAP
from .generation import get_note_key, note_to_pitch
from .volume import (
    LISTENER_EYE,
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
MIN_Z = 1.0                        # 左右两条轨道至少要离中心轴 1 格，避免互相重合
MIN_RADIUS = 24.0                  # 单盒到走廊的最小直线距离（满音量）
MAX_NOTES_PER_CELL = 3             # 同一 tick 同一轨道最多 3 个音符盒（中间 + 两侧）
MIN_DUAL_DISTANCE = 24.0           # 左右叠加满音量时的单盒距离
MAX_DISTANCE = 48.0


def _cell_offsets(entries: List[dict], tick: int, movement: int = 0) -> List[Tuple[int, dict]]:
    """同一 tick 多个音符时，返回 (偏移, 音符) 列表。

    偏移以「离中心轴的方向」为单位：0 = 轨道原位，+1 = 向外，-1 = 向内。
    中继器正对的中间音符盒会被**强充能**，它再弱充能相邻的音符盒，
    所以最多 3 个：中间 + 内外两侧。侧边必须放在**下一 tick 不移动的一侧**，
    否则下一 tick 的横向红石线会扫过并覆盖这个音符盒。
    movement: +1 = 半径变大（向外走），-1 = 半径变小（向内走）。
    """
    if not entries:
        return []
    ordered = sorted(entries, key=lambda entry: entry["volume"], reverse=True)
    if len(ordered) == 1:
        return [(0, ordered[0])]
    if movement > 0:
        side = -1
    elif movement < 0:
        side = 1
    else:
        side = 1 if tick % 2 == 0 else -1
    if len(ordered) == 2:
        return [(0, ordered[0]), (side, ordered[1])]
    if movement == 0:
        return [(0, ordered[0]), (-1, ordered[1]), (1, ordered[2])]
    # 会移动时只能保留反向一侧，第三个音符放不下
    return [(0, ordered[0]), (side, ordered[1])]


class BlockWriter:
    """坐标 → 方块串，后写覆盖先写，并记录冲突与方块所属轨道（用于自检）。"""

    def __init__(self) -> None:
        self.blocks: Dict[Tuple[int, int, int], str] = {}
        self.owners: Dict[Tuple[int, int, int], str] = {}
        self.conflicts: List[Tuple[Tuple[int, int, int], str, str]] = []

    def set(self, position: Tuple[int, int, int], block: str,
            owner: Optional[str] = None) -> None:
        old = self.blocks.get(position)
        if old is not None and old != block:
            self.conflicts.append((position, old, block))
        self.blocks[position] = block
        if owner is not None:
            self.owners[position] = owner

    def commands(self) -> List[str]:
        commands = ["# 红石音乐生成器（音量控制排版 + 隐形矿车跟随）"]
        for position in sorted(self.blocks):
            x, y, z = position
            commands.append(f"setblock {x} {y} {z} {self.blocks[position]}")
        return commands


def _instrument_blocks(instrument: int, support: str = WOOL) -> List[str]:
    if instrument == 0:
        return []
    blocks = [INSTRUMENT_BLOCK_MAP.get(instrument, "minecraft:stone")]
    if instrument == 3:
        blocks.append(support)
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
                     sound_range: float, master: bool) -> Tuple[List[int], int, int, int]:
    """每个 tick 的横向距离。

    目标音量 → 目标三维距离 → 用勾股定理扣掉垂直分量得到横向距离：
    ``lateral = sqrt(target² − vertical²)``。垂直差本身已经是距离的一部分，
    因此高音轨不会再被「少算一段高度」而放得离走廊更近。

    返回 ``(distances, smoothed, unreachable, deviations)``：
    - smoothed：因红石线 15 格限制被平滑的 tick 数；
    - unreachable：垂直差已超过目标距离，满音量在几何上不可达的 tick 数；
    - deviations：平滑后实际音量与 NBS 目标偏差超过 6% 的 tick 数。
    """
    reach_floor = math.hypot(low, vertical) if low else 0.0
    desired: Dict[int, float] = {}
    wanted_volume: Dict[int, float] = {}
    unreachable = 0
    for tick in range(ticks):
        entries = entries_by_tick.get(tick, [])
        if not entries:
            continue
        if master:
            desired[tick] = 0.0
            wanted_volume[tick] = 1.0
            continue
        loudest = max(entry["volume"] for entry in entries)
        target = dual_distance(loudest, sound_range)
        if target < reach_floor - 0.5:
            # 垂直差已经超过双盒目标距离：横向即使贴到最小也还原不了这个音量
            unreachable += 1
            desired[tick] = float(low)
        else:
            desired[tick] = lateral_from_3d(target, vertical)
        wanted_volume[tick] = loudest
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
    distances, smoothed = _smooth(raw, low, high)
    if master:
        return distances, smoothed, unreachable, 0
    deviations = 0
    for tick, want in wanted_volume.items():
        actual = volume_from_distance(
            math.hypot(distances[tick], vertical), sound_range)
        # 每盒只承担 V/2，左右两盒叠加才等于 NBS 目标
        if abs(actual - want / 2.0) > 0.06:
            deviations += 1
    return distances, smoothed, unreachable, deviations


def _deltas(max_abs: int) -> List[int]:
    """0, +1, -1, +2, -2 … 由近到远。"""
    values = [0]
    for step in range(1, max_abs + 1):
        values.append(step)
        values.append(-step)
    return values


def _pick_lane_level(desired: int, limit: float, eye: float,
                     used: set, gap: int) -> int:
    """在期望高度附近找一个可用的轨道层。

    ``gap`` 至少 3：上一层在 y+3 时，它的支撑方块落在 y+2，
    不会挡住 y 层音符盒的正上方；同时不同层红石粉也不会相邻串线。
    ``limit`` 是该音轨可用的最大垂直偏移（由基准音量决定）：
    先在这个范围内尽量贴近期望角度，找不到才放宽到任意空层，
    保证不同音轨永远不会落在同一层。
    """
    for delta in _deltas(24):
        candidate = desired + delta
        if abs(candidate + 0.5 - eye) > limit + 1e-6:
            continue
        if all(abs(candidate - other) >= gap for other in used):
            return candidate
    for delta in _deltas(400):
        candidate = desired + delta
        if all(abs(candidate - other) >= gap for other in used):
            return candidate
    return desired


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
    eye_y = base_y + LISTENER_EYE  # 聆听位置：站在走廊里时的耳朵高度
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
    unreachable = 0
    deviations = 0
    dropped_total = 0
    multi_cells = 0
    used_y: set = set()

    # ── 主音轨：走廊外最近的上下层，固定满音量 ─────────────
    up = down = 0
    for index, virtual in enumerate(master_layers):
        if index % 2 == 0:
            y = base_y + corridor + 1 + up * master_spacing
            up += 1
        else:
            y = base_y - corridor - 1 - down * master_spacing
            down += 1
        while y in used_y:
            y += master_spacing if index % 2 == 0 else -master_spacing
        used_y.add(y)
        original_id = virtual.get("original_id", virtual["vid"])
        entries_by_tick, dropped = _unique_notes(virtual["notes"], max_tick, song, original_id)
        dropped_total += dropped
        vertical = abs(y + 0.5 - eye_y)
        distances, count, miss, spread = _target_sequence(
            entries_by_tick, ticks, 0.0, 0.0, vertical, sound_range, master=True)
        smoothed += count
        unreachable += miss
        deviations += spread
        nodes = []
        for tick in range(ticks):
            x = base_x + STEP * tick
            z = base_z
            entries = entries_by_tick.get(tick, [])
            node = {
                "tick": tick, "x": x, "z": z, "y": y, "side": 0,
                "entries": entries, "master": True,
                "distance": math.hypot(z - base_z, vertical),
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

    # ── 普通音轨：按分组合并成一条轨道，沿圆周分配角度 ───────
    # 同组音轨共用一条红石轨道；同一 tick 的多个音符盒并排摆放，
    # 由中继器强充能中间音符盒、再弱充能带动两侧音符盒。
    group_members: Dict[int, List[dict]] = defaultdict(list)
    for virtual in normal_layers:
        gid = layer_to_group.get(virtual["vid"], virtual["vid"])
        group_members[gid].append(virtual)

    prepared = []
    for gid in sorted(group_members):
        merged: Dict[int, List[dict]] = defaultdict(list)
        for virtual in group_members[gid]:
            original_id = virtual.get("original_id", virtual["vid"])
            entries_by_tick, dropped = _unique_notes(
                virtual["notes"], max_tick, song, original_id)
            dropped_total += dropped
            for tick, items in entries_by_tick.items():
                for entry in items:
                    merged[tick].append({
                        "note": entry["note"],
                        "volume": entry["volume"],
                        "vid": virtual["vid"],
                    })
        for tick, items in list(merged.items()):
            if len(items) > MAX_NOTES_PER_CELL:
                items.sort(key=lambda entry: entry["volume"], reverse=True)
                dropped_total += len(items) - MAX_NOTES_PER_CELL
                merged[tick] = items[:MAX_NOTES_PER_CELL]
            else:
                items.sort(key=lambda entry: entry["volume"], reverse=True)
        volumes = [entry["volume"] for items in merged.values() for entry in items]
        if volumes:
            volumes_sorted = sorted(volumes)
            representative = volumes_sorted[len(volumes_sorted) // 2]
            radius = dual_distance(representative, sound_range)
        else:
            radius = MAX_DISTANCE
        prepared.append({
            "gid": gid,
            "vids": [virtual["vid"] for virtual in group_members[gid]],
            "entries": merged,
            "radius": radius,
        })

    # 角度在 [-88°, +88°] 内均匀铺开；音量越大半径越小，可用仰角也越小
    count = max(1, len(prepared))
    angles = [-88.0 + 176.0 * ((index + 0.5) / count) for index in range(count)]
    angles.sort(key=abs)
    prepared.sort(key=lambda item: item["radius"], reverse=True)

    for item, angle in zip(prepared, angles):
        gid = item["gid"]
        entries_by_tick = item["entries"]
        radius = item["radius"]
        theta_max = (math.degrees(math.acos(min(1.0, MIN_Z / radius)))
                     if radius > 0 else 0.0)
        theta = math.radians(max(-theta_max, min(theta_max, angle)))
        desired = int(round(eye_y + radius * math.sin(theta)))
        limit = radius * math.sin(math.radians(theta_max))
        y = _pick_lane_level(desired, limit, eye_y, used_y, max(3, spacing))
        used_y.add(y)
        vertical = abs(y + 0.5 - eye_y)
        # 横向下限由「直线距离 ≥ 24」反解，而不是固定横向距离
        low = max(MIN_Z, math.sqrt(max(MIN_RADIUS ** 2 - vertical ** 2, 0.0)))
        distances, count_smooth, miss, spread = _target_sequence(
            entries_by_tick, ticks, low, MAX_DISTANCE,
            vertical, sound_range, master=False)
        smoothed += count_smooth
        unreachable += miss
        deviations += spread

        for side in (-1, 1):
            nodes = []
            for tick in range(ticks):
                x = base_x + STEP * tick
                distance = distances[tick]
                z = base_z + side * distance
                node_distance = math.hypot(distance, vertical)
                entries = entries_by_tick.get(tick, [])
                nodes.append({
                    "tick": tick, "x": x, "z": z, "y": y, "side": side,
                    "entries": entries, "master": False,
                    "distance": node_distance,
                })
                next_distance = distances[tick + 1] if tick + 1 < ticks else distance
                movement = 1 if next_distance > distance else (
                    -1 if next_distance < distance else 0)
                offsets = _cell_offsets(entries, tick, movement)
                if len(entries) >= 2 and side > 0:
                    multi_cells += 1
                if len(entries) > len(offsets):
                    dropped_total += len(entries) - len(offsets)
                for offset, entry in offsets:
                    # 侧边音符盒沿半径方向偏移，距离按它自己的位置重新算
                    note_distance = int(max(MIN_Z, distance + offset))
                    note_distance_3d = math.hypot(vertical, note_distance)
                    placed.append({
                        "gid": gid,
                        "vid": entry["vid"], "tick": tick,
                        "note": entry["note"], "side": side,
                        "distance": note_distance_3d,
                        "primary": offset == 0,
                        "volume_nbs": entry["volume"],
                        "volume_plan": volume_from_distance(note_distance_3d, sound_range),
                        "pos": (x + 1, y, base_z + side * note_distance), "master": False,
                    })
            lanes[("group", gid, side)] = nodes

    return {
        "lanes": lanes,
        "notes": placed,
        "player_y": player_y,
        "eye_y": eye_y,
        "lane_levels": sorted(used_y),
        "spacing": spacing,
        "corridor": corridor,
        "master_spacing": master_spacing,
        "sound_range": sound_range,
        "base": base,
        "smoothed": smoothed,
        "unreachable": unreachable,
        "deviations": deviations,
        "multi_cells": multi_cells,
        "overflow": smoothed,
        "dropped": dropped_total,
    }


def _audit_layout(writer: BlockWriter) -> dict:
    """检查两类会破坏播放的问题：

    - 音符盒正上方被其它轨道占用（会挡住音符盒 / 让同一格出现两个音符）；
    - 红石粉与其它轨道的红石粉、中继器或音符盒相邻（会串线误触发）。
    """
    overheads = 0
    crossing = set()
    neighbors = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
    for position, block in writer.blocks.items():
        x, y, z = position
        if block.startswith("minecraft:note_block"):
            if (x, y + 1, z) in writer.blocks:
                overheads += 1
        if block != DUST:
            continue
        owner = writer.owners.get(position)
        for dx, dy, dz in neighbors:
            other_pos = (x + dx, y + dy, z + dz)
            other = writer.owners.get(other_pos)
            if other is None or other == owner:
                continue
            other_block = writer.blocks.get(other_pos, "")
            if (other_block == DUST or "repeater" in other_block
                    or other_block.startswith("minecraft:note_block")):
                crossing.add(tuple(sorted((position, other_pos))))
    return {"overheads": overheads, "crossing": len(crossing)}


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
    support = str(config.get("support_block") or WOOL)
    empty = str(config.get("empty_block") or EMPTY)

    for key, nodes in plan["lanes"].items():
        if not nodes:
            continue
        y = nodes[0]["y"]
        lane_side = nodes[0].get("side", 0)
        if key[0] == "master":
            label = f"m{key[1]}"
        else:
            label = f"v{key[1]}{'R' if key[2] > 0 else 'L'}"
        previous_z = nodes[0]["z"]
        # 起手中继器：直接强充能第一个音符盒
        writer.set((base_x, y, previous_z), _repeater(), label)
        writer.set((base_x, y - 1, previous_z), support, label)
        activations[label] = (base_x - 1, y, previous_z)

        for index, node in enumerate(nodes):
            x = node["x"]
            z = node["z"]
            # 本 tick 的中继器与音符盒同 Z：中继器输出端正对音符盒 → 强充能
            if index > 0:
                writer.set((x, y, z), _repeater(), label)
                writer.set((x, y - 1, z), support, label)
                # 横向移动：红石线只负责把「上一个被强充能的音符盒/空音符」
                # 的信号送进本中继器的背面（正对中继器输入端），
                # 不依赖红石粉侧向充能任何方块。
                if z != previous_z:
                    step = 1 if z > previous_z else -1
                    for wire_z in range(previous_z + step, z + step, step):
                        writer.set((x - 1, y, wire_z), DUST, label)
                        writer.set((x - 1, y - 1, wire_z), support, label)
            entries = node["entries"]
            next_z = nodes[index + 1]["z"] if index + 1 < len(nodes) else z
            delta_units = (next_z - z) * lane_side
            movement = 1 if delta_units > 0 else (-1 if delta_units < 0 else 0)
            if entries:
                lane_distance = abs(z - base_z)
                for offset, entry in _cell_offsets(entries, node["tick"], movement):
                    note = entry["note"]
                    note_distance = int(max(MIN_Z, lane_distance + offset))
                    note_z = base_z + lane_side * note_distance
                    writer.set((x + 1, y, note_z),
                               f"minecraft:note_block[note={note_to_pitch(get_note_key(note))}]",
                               label)
                    bases = _instrument_blocks(note.instrument, support)
                    for depth, block in enumerate(bases, start=1):
                        writer.set((x + 1, y - depth, note_z), block, label)
            else:
                writer.set((x + 1, y, z), empty, label)
                writer.set((x + 1, y - 1, z), support, label)
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
        writer.set((base_x, player_y - 1, cart_z), support)
        for index, node in enumerate(any_nodes):
            command_x = base_x + STEP * index + 1
            command = (f"tp @e[type=minecart,tag={MINECART_TAG},limit=1] "
                       f"~0.5 ~ ~{base_z + 0.5 - cart_z:.1f}")
            writer.set((command_x, player_y, cart_z),
                       f"minecraft:command_block[facing=up]"
                       f"{{Command:{json.dumps(command)}}}")
            writer.set((command_x, player_y - 1, cart_z), support)
            writer.set((command_x + 1, player_y, cart_z), _repeater())
            writer.set((command_x + 1, player_y - 1, cart_z), support)

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
    audit = _audit_layout(writer)
    meta["conflicts"] = writer.conflicts
    meta["note_overheads"] = audit["overheads"]
    meta["crossing"] = audit["crossing"]
    meta["block_count"] = len(writer.blocks)
    return commands, activations, meta
