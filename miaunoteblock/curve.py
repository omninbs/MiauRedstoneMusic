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
    SOUND_RANGE,
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
MIN_Z = 2.0                        # 左右两条轨道至少要离中心轴 2 格：
                                   # 留出内侧侧边音符盒的位置，避免它与下一格横向红石粉重叠
MIN_INNER = 1                      # 侧边音符盒到中轴的绝对下限（不能越过中轴）
MIN_RADIUS = 24.0                  # 单盒到走廊的最小直线距离（满音量）
MAX_NOTES_PER_CELL = 3             # 同一 tick 同一轨道最多 3 个音符盒（中间 + 两侧）
MIN_DUAL_DISTANCE = 24.0           # 左右叠加满音量时的单盒距离
MAX_DISTANCE = 48.0
# 一次横向移动的物理极限：红石线 15 格 + 音符盒本身 1 格
DUAL_TRIGGER_SPAN = 16.0


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


def _effective_note_distance(lane_distance: int, offset: int) -> int:
    """侧边音符盒到中轴的距离。

    向内偏移不能越过中轴（``MIN_INNER``）：当轨道已经贴着中轴时，
    ``-1`` 会与中间音符盒重合，必须改放到外侧，否则这个音符会被覆盖丢失。
    """
    value = lane_distance + offset
    if value < MIN_INNER:
        value = lane_distance + abs(offset)
    return int(round(max(MIN_INNER, value)))


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


def _target_sequence(entries_by_tick: Dict[int, List[dict]], ticks: int,
                     low: float, high: float, vertical: float,
                     sound_range: float, master: bool,
                     cruise_distance: Optional[float] = None
                     ) -> Tuple[List[int], int, int, int]:
    """每个 tick 的横向距离。

    目标音量 → 目标三维距离 → 用勾股定理扣掉垂直分量得到横向距离：
    ``lateral = sqrt(target² − vertical²)``。垂直差本身已经是距离的一部分，
    因此高音轨不会再被「少算一段高度」而放得离走廊更近。

    移动策略（避免为了走线而削平音符音量）：

    - **有音符的 tick** 优先停在理想距离上，保证音量准确；
    - **空音符 tick** 不发声，负责横向移动：提前朝下一个音符的理想位置摆渡。
      空音符同样被中继器强充能，相当于红石线的「能量补给站」，
      所以大跨度可以拆成多段空音符完成，而不是压缩音符本身；
    - 只有两个音符之间没有足够空音符、单段又超过红石线 15 格时，
      才在极限处平滑音符位置（记入 ``smoothed``）。

    返回 ``(distances, smoothed, unreachable, deviations)``：
    - smoothed：因红石线 15 格限制被迫平滑的音符 tick 数；
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

    distances: List[int] = []
    smoothed = 0
    current: Optional[int] = None
    for index in range(ticks):
        if index in desired:
            target = int(round(clamp(desired[index], low, high)))
        else:
            # 空音符：向后看最近的一个音符，提前把轨道摆渡过去
            next_target: Optional[int] = None
            for ahead in range(index + 1, ticks):
                if ahead in desired:
                    next_target = int(round(clamp(desired[ahead], low, high)))
                    break
            if next_target is None:
                fallback = cruise_distance if cruise_distance is not None else (
                    float(current) if current is not None else low)
                next_target = int(round(clamp(fallback, low, high)))
            target = next_target
        if current is None:
            current = target
        else:
            delta = target - current
            if abs(delta) > MAX_LATERAL_STEP:
                if index in desired:
                    smoothed += 1
                current += MAX_LATERAL_STEP if delta > 0 else -MAX_LATERAL_STEP
            else:
                current = target
        distances.append(int(current))

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
    sound_range = SOUND_RANGE            # 游戏固定可听半径，不可配置
    corridor = max(2, int(config.get("curve_corridor_height", 2)))
    master_spacing = max(3, int(config.get("curve_master_spacing", 3)))
    master_ids = set(config.get("curve_master_layers") or [])
    dual_track = bool(config.get("curve_dual_track", False))
    dual_spacing = max(3, int(config.get("curve_dual_spacing", 6)))
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
    multi_ticks: set = set()          # (轨道键, tick)：该 tick 同时触发多个音符盒
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
            offsets = _cell_offsets(entries, tick, 0)
            if len(entries) >= 2:
                multi_ticks.add((("master", virtual["vid"]), tick))
            if len(entries) > len(offsets):
                dropped_total += len(entries) - len(offsets)
            for offset, entry in offsets:
                note_z = z + offset
                note_distance = math.hypot(note_z - base_z, vertical)
                placed.append({
                    "gid": None, "vid": virtual["vid"], "tick": tick,
                    "note": entry["note"], "side": 0, "offset": offset,
                    "distance": note_distance, "primary": offset == 0,
                    "volume_nbs": 1.0,
                    "volume_plan": volume_from_distance(note_distance, sound_range),
                    "pos": (x + 1, y, note_z), "master": True,
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

        # ── 双轨音量控制：动态判断是否真的跨不过去 ──────────────
        # 先把每个音量代入音量公式，得到它需要的直线距离；
        # 如果高低音量的距离差超过一次横向移动的物理极限
        # （红石线 15 格 + 音符盒 1 格 = 16 格），单轨就无法摆渡，
        # 这时才拆成主轨 + 外扩副轨。
        aux_entries: Optional[Dict[int, List[dict]]] = None
        aux_range: Optional[Tuple[int, int]] = None
        distance_span = 0.0
        if dual_track and volumes:
            targets = [dual_distance(volume, sound_range) for volume in volumes]
            distance_span = max(targets) - min(targets)
        if dual_track and distance_span > DUAL_TRIGGER_SPAN:
            cut = (max(volumes) + min(volumes)) / 2.0
            main_entries: Dict[int, List[dict]] = {}
            low_entries: Dict[int, List[dict]] = {}
            for tick, items in merged.items():
                high = [entry for entry in items if entry["volume"] >= cut]
                low = [entry for entry in items if entry["volume"] < cut]
                if high:
                    main_entries[tick] = high
                if low:
                    low_entries[tick] = low
            main_volumes = [entry["volume"] for items in main_entries.values()
                            for entry in items]
            if main_volumes and low_entries:
                mv = sorted(main_volumes)
                radius = dual_distance(mv[len(mv) // 2], sound_range)
                merged = main_entries
                aux_entries = low_entries
                low_ticks = sorted(low_entries)
                aux_range = (low_ticks[0], low_ticks[-1])

        prepared.append({
            "gid": gid,
            "vids": [virtual["vid"] for virtual in group_members[gid]],
            "entries": merged,
            "radius": radius,
            "aux_entries": aux_entries,
            "aux_range": aux_range,
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
        aux_entries = item.get("aux_entries")
        aux_range = item.get("aux_range")
        theta_max = (math.degrees(math.acos(min(1.0, MIN_Z / radius)))
                     if radius > 0 else 0.0)
        base_angle = max(-theta_max, min(theta_max, angle))
        # 以半径 D、走廊为圆心，在圆弧上枚举候选点：若首选角度对应的层已被
        # 占用（会串线），就沿圆周换一个角度，选垂直偏差最小的可用层。
        best: Optional[Tuple[Tuple[float, float], int, int]] = None
        for shift in (0.0, 6.0, -6.0, 12.0, -12.0, 18.0, -18.0,
                      24.0, -24.0, 30.0, -30.0):
            candidate = max(-theta_max, min(theta_max, base_angle + shift))
            theta_c = math.radians(candidate)
            desired_c = int(round(eye_y + radius * math.sin(theta_c)))
            limit_c = radius * math.sin(math.radians(theta_max))
            y_c = _pick_lane_level(desired_c, limit_c, eye_y, used_y, max(3, spacing))
            score = (abs(y_c - desired_c), abs(shift))
            if best is None or score < best[0]:
                best = (score, y_c, desired_c)
        _, y, desired = best
        used_y.add(y)
        vertical = abs(y + 0.5 - eye_y)
        # 横向下限由「直线距离 ≥ 24」反解，而不是固定横向距离
        low = max(MIN_Z, math.sqrt(max(MIN_RADIUS ** 2 - vertical ** 2, 0.0)))
        distances, count_smooth, miss, spread = _target_sequence(
            entries_by_tick, ticks, low, MAX_DISTANCE,
            vertical, sound_range, master=False,
            cruise_distance=max(low, min(
                math.sqrt(max(radius ** 2 - vertical ** 2, 0.0)), MAX_DISTANCE)))
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
                if len(entries) >= 2:
                    multi_ticks.add((("group", gid), tick))
                if len(entries) > len(offsets):
                    dropped_total += len(entries) - len(offsets)
                for offset, entry in offsets:
                    # 侧边音符盒沿半径方向偏移，距离按它自己的位置重新算
                    note_distance = _effective_note_distance(distance, offset)
                    note_distance_3d = math.hypot(vertical, note_distance)
                    placed.append({
                        "gid": gid,
                        "vid": entry["vid"], "tick": tick,
                        "note": entry["note"], "side": side, "offset": offset,
                        "distance": note_distance_3d,
                        "primary": offset == 0,
                        "volume_nbs": entry["volume"],
                        "volume_plan": volume_from_distance(note_distance_3d, sound_range),
                        "pos": (x + 1, y, base_z + side * note_distance), "master": False,
                    })
            lanes[("group", gid, side)] = nodes

            # ── 副音轨：只负责低音量音符，位置至少在主轨外侧一个间距 ──
            if aux_range is not None and aux_entries is not None:
                aux_start, aux_end = aux_range
                aux_raw, aux_smooth, aux_miss, aux_spread = _target_sequence(
                    aux_entries, ticks, low, MAX_DISTANCE,
                    vertical, sound_range, master=False)
                smoothed += aux_smooth
                unreachable += aux_miss
                deviations += aux_spread
                # 副音轨必须整体留在主轨外侧：除了当前 tick，还要压过主轨
                # 下一 tick 的距离，否则两条轨道在同一 x 列的横向红石粉
                # 会在中途重叠，把两轨的信号并成一条（串线）。
                aux_distances = {}
                for tick in range(aux_start, aux_end + 1):
                    floor = distances[tick] + dual_spacing
                    if tick + 1 < len(distances):
                        floor = max(floor, distances[tick + 1] + dual_spacing)
                    aux_distances[tick] = max(aux_raw[tick], floor)
                aux_nodes = []
                for tick in range(aux_start, aux_end + 1):
                    x = base_x + STEP * tick
                    aux_distance = aux_distances[tick]
                    z = base_z + side * aux_distance
                    entries = aux_entries.get(tick, [])
                    aux_nodes.append({
                        "tick": tick, "x": x, "z": z, "y": y, "side": side,
                        "entries": entries, "master": False, "aux": True,
                        "distance": math.hypot(aux_distance, vertical),
                    })
                    next_distance = aux_distances.get(
                        tick + 1, aux_distance)
                    movement = 1 if next_distance > aux_distance else (
                        -1 if next_distance < aux_distance else 0)
                    offsets = _cell_offsets(entries, tick, movement)
                    if len(entries) >= 2:
                        multi_ticks.add((("group", gid, side, 1), tick))
                    if len(entries) > len(offsets):
                        dropped_total += len(entries) - len(offsets)
                    for offset, entry in offsets:
                        note_distance = _effective_note_distance(aux_distance, offset)
                        note_distance_3d = math.hypot(vertical, note_distance)
                        placed.append({
                            "gid": gid, "aux": True,
                            "vid": entry["vid"], "tick": tick,
                            "note": entry["note"], "side": side, "offset": offset,
                            "distance": note_distance_3d,
                            "primary": offset == 0,
                            "volume_nbs": entry["volume"],
                            "volume_plan": volume_from_distance(
                                note_distance_3d, sound_range),
                            "pos": (x + 1, y, base_z + side * note_distance),
                            "master": False,
                        })
                lanes[("group", gid, side, 1)] = aux_nodes

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
        "multi_cells": len(multi_ticks),
        "overflow": smoothed,
        "dropped": dropped_total,
    }


def _can_place_side(writer: BlockWriter, position: Tuple[int, int, int],
                    label: str) -> bool:
    """并排音符盒能否安全落位（不覆盖、不遮挡、不与其它轨道接触）。"""
    x, y, z = position
    if position in writer.blocks:
        return False
    below = (x, y - 1, z)
    if below in writer.blocks and writer.owners.get(below) != label:
        return False
    if (x, y + 1, z) in writer.blocks:
        return False
    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        neighbor = (x + dx, y, z + dz)
        other = writer.owners.get(neighbor)
        if other is None or other == label:
            continue
        other_block = writer.blocks.get(neighbor, "")
        if (other_block == DUST or "repeater" in other_block
                or other_block.startswith("minecraft:note_block")):
            return False
    return True


def _bridge_feasible(donor_y: int, donor_z: int, target_y: int, target_z: int,
                     steps: int) -> bool:
    """线路桥能否在 ``steps`` 个歌曲 tick 内从捐赠轨道走到目标轨道。"""
    if steps < 2:
        return False
    rise = target_y - donor_y
    if rise < 0 or rise > steps:
        return False
    flat = steps - rise
    if flat <= 0:
        return rise == steps and donor_z == target_z
    # 桥的水平段从「上一级中继器的输出格」开始铺粉，起点这一格本身就是粉，
    # 所以一段最多 15 格粉（最后一格强度 1）；换算成位移是 14 格。
    # 若按 15 格位移铺，会得到 16 格粉，末格强度 0，中继器收不到信号。
    return math.ceil(abs(target_z - donor_z) / flat) <= MAX_LATERAL_STEP - 1


def _build_bridge(writer: BlockWriter, label: str, base_x: int,
                  donor_y: int, donor_z: int, target_y: int, target_z: int,
                  steps: int, support: str,
                  side_start: bool = False) -> bool:
    """用红石粉 + 中继器从捐赠轨道「唤醒」被裁剪的轨道。

    节奏对齐：每段红石线跨 1 个 X 格、每个中继器 2 游戏 tick，
    因此沿 +X 每 2 格正好等于 1 个歌曲 tick，与主轨道完全一致。
    红石粉只能跨 1 格高度，所以先按 Y 逐格爬升（强充能方块把信号送到
    上方红石粉），再在目标高度水平横移。
    """
    if not _bridge_feasible(donor_y, donor_z, target_y, target_z, steps):
        return False
    planned: List[Tuple[str, Tuple[int, int, int]]] = []
    y = donor_y
    # 爬升段放在捐赠轨道旁边一列，避免把桥的实心方块压在捐赠轨道
    # 自己的红石粉正上方（那会形成「强充能方块→弱充能方块→红石粉」的错误链路）。
    climb_z = donor_z
    if target_z != donor_z:
        climb_z = donor_z + (1 if target_z > donor_z else -1)
    z = climb_z
    rise = target_y - donor_y

    # ── 爬升段（先爬升，避免在捐赠音符盒所在格铺粉）──────────
    for t in range(rise):
        row_x = base_x + 2 * t + 1
        if t == 0:
            # 从捐赠轨道上方引粉，再横移到旁边一列
            low, high = (donor_z, climb_z) if donor_z <= climb_z else (climb_z, donor_z)
            for wire_z in range(low, high + 1):
                planned.append(("dust", (row_x, donor_y + 1, wire_z)))
        else:
            planned.append(("block", (row_x, y, z)))
            # 强充能方块正上方的红石粉才是有效传播
            planned.append(("dust", (row_x, y + 1, z)))
        y += 1
        if t + 1 < steps:
            planned.append(("repeater", (base_x + 2 * (t + 1), y, z)))

    # ── 水平段 ────────────────────────────────────────────────
    flat = steps - rise
    if flat > 0:
        remaining = target_z - z
        direction = 1 if remaining > 0 else -1
        moving = abs(remaining)
        per_step = math.ceil(moving / flat)
        for step in range(rise, steps):
            move = min(per_step, moving)
            z_next = z + direction * move
            row_x = base_x + 2 * step + 1
            low, high = (z, z_next) if z <= z_next else (z_next, z)
            for wire_z in range(low, high + 1):
                # 第一个水平段要避开捐赠轨道自己的音符盒格；
                # 侧向取电时由调用方预先放好端点粉，这里要包含它。
                if (step == rise and rise == 0 and wire_z == donor_z
                        and not side_start):
                    continue
                planned.append(("dust", (row_x, y, wire_z)))
                planned.append(("support", (row_x, y - 1, wire_z)))
            moving -= move
            z = z_next
            if step + 1 < steps:
                planned.append(("repeater", (base_x + 2 * (step + 1), y, z)))

    if z != target_z or y != target_y:
        return False

    # 先整体检查冲突，再落笔，避免只写一半
    for kind, position in planned:
        below = (position[0], position[1] - 1, position[2])
        below_block = writer.blocks.get(below)
        # 音符盒正上方不能有任何方块（包括红石粉 / 中继器 / 支撑）
        if below_block is not None and below_block.startswith("minecraft:note_block"):
            return False
        existing = writer.blocks.get(position)
        if existing is None:
            continue
        if kind in ("dust", "repeater"):
            return False
        if kind == "block" and writer.owners.get(position) != label:
            return False

    for kind, position in planned:
        if kind == "dust":
            writer.set(position, DUST, label)
            # 红石粉不能悬浮：正下方必须有实体方块
            below = (position[0], position[1] - 1, position[2])
            if below not in writer.blocks:
                writer.set(below, support, label)
        elif kind == "repeater":
            writer.set(position, _repeater(), label)
            below = (position[0], position[1] - 1, position[2])
            if below not in writer.blocks:
                writer.set(below, support, label)
        elif kind == "block":
            writer.set(position, support, label)
        else:
            if position not in writer.blocks:
                writer.set(position, support, label)
    return True


def _audit_layout(writer: BlockWriter, full: bool = True,
                  ignore: Optional[set] = None) -> dict:
    """检查会破坏播放的问题。

    - ``overheads``：音符盒正上方被占用；
    - ``crossing``：红石粉与其它轨道的红石粉 / 中继器相邻（真正的串线）；
    - ``touching``：任意红石元件（含音符盒）与其它轨道接触。

    单组轨道（同一 tick 只有一个音符盒）沿用原有结构，不需要额外的跨轨道检测，
    传入 ``full=False`` 时只检查遮挡。
    """
    overheads = 0
    crossing = set()
    touching = set()
    neighbors = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
    ignore = ignore or set()
    for position, block in writer.blocks.items():
        x, y, z = position
        if position in ignore:
            continue
        is_note = block.startswith("minecraft:note_block")
        if is_note and (x, y + 1, z) in writer.blocks:
            overheads += 1
        if not full:
            continue
        if block != DUST and not is_note:
            continue
        owner = writer.owners.get(position)
        for dx, dy, dz in neighbors:
            other_pos = (x + dx, y + dy, z + dz)
            if other_pos in ignore:
                continue
            other = writer.owners.get(other_pos)
            if other is None or other == owner:
                continue
            other_block = writer.blocks.get(other_pos, "")
            is_wire = other_block == DUST or "repeater" in other_block
            is_other_note = other_block.startswith("minecraft:note_block")
            if block == DUST and is_wire:
                crossing.add(tuple(sorted((position, other_pos))))
            if (block == DUST and (is_wire or is_other_note)) or \
                    (is_note and (is_wire or is_other_note)):
                touching.add(tuple(sorted((position, other_pos))))
    return {"overheads": overheads, "crossing": len(crossing), "touching": len(touching)}


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
    side_notes: List[dict] = []      # 同一 tick 并排的第二个/第三个音符盒，稍后安全落位
    multi_written = 0
    multi_avoided = 0
    lane_left_block = str(config.get("lane_block_left") or "") or empty
    lane_right_block = str(config.get("lane_block_right") or "") or empty
    lane_support_left = str(config.get("lane_support_left") or "") or support
    lane_support_right = str(config.get("lane_support_right") or "") or support
    master_lamp = bool(config.get("curve_master_lamp", False))
    trim_silent = bool(config.get("curve_trim_silent", False))
    # 静音阈值以「秒」配置，再按歌曲速度换算成 tick
    tempo = 10.0
    if song is not None:
        header = getattr(song, "header", None)
        tempo = float(getattr(header, "tempo", 10.0) or 10.0)
    trim_seconds = float(config.get("curve_trim_seconds", 1.0) or 1.0)
    trim_threshold = max(2, int(round(trim_seconds * max(1.0, tempo))))
    bridge_cells: set = set()
    bridge_ok = 0
    bridge_skipped = 0
    trim_aborted = 0
    dual_skipped = 0
    dual_fallback = 0
    bridged_starts: set = set()

    def _lane_support(side: int) -> str:
        if side > 0:
            return lane_support_right
        if side < 0:
            return lane_support_left
        return support

    # ── 预处理：音符范围 / 静音裁剪区间 / 唤醒者 ─────────────
    def _segments(nodes: List[dict]) -> List[Tuple[int, int]]:
        """返回需要生成的 tick 区间；静音裁剪时把长空段整段删掉。"""
        note_ticks = sorted({node["tick"] for node in nodes if node["entries"]})
        if not note_ticks:
            return []
        first_tick, last_tick = note_ticks[0], note_ticks[-1]
        if not trim_silent:
            return [(0, len(nodes) - 1)]
        removed: List[Tuple[int, int]] = []
        if first_tick >= trim_threshold:
            removed.append((0, first_tick))
        for before, after in zip(note_ticks, note_ticks[1:]):
            if after - before - 1 >= trim_threshold:
                removed.append((before + 1, after))
        limit = last_tick if len(nodes) - 1 - last_tick >= trim_threshold \
            else len(nodes) - 1
        result: List[Tuple[int, int]] = []
        cursor = 0
        for low, high in removed:
            if cursor < low:
                result.append((cursor, low - 1))
            cursor = high
        if cursor <= limit:
            result.append((cursor, limit))
        return result

    lane_infos: List[dict] = []
    by_key: Dict[Tuple, dict] = {}
    for key, nodes in plan["lanes"].items():
        if not nodes:
            continue
        note_ticks = [node["tick"] for node in nodes if node["entries"]]
        if not note_ticks:
            continue                 # 整条轨道没有音符：直接不生成
        aux = len(key) > 3 and key[3] == 1
        if aux:
            # 副音轨只覆盖低音量区间（plan 已裁剪过），按绝对 tick 铺成整表
            start_tick = nodes[0]["tick"]
            end_tick = nodes[-1]["tick"]
            padded: List[Optional[dict]] = [None] * (max_tick + 1)
            for node in nodes:
                padded[node["tick"]] = node
            segments = [(start_tick, end_tick)]
            lane_nodes = padded
        else:
            segments = _segments(nodes)
            if not segments:
                continue
            lane_nodes = nodes
        generated = {tick for low, high in segments for tick in range(low, high + 1)}
        info = {
            "key": key, "nodes": lane_nodes, "y": nodes[0]["y"],
            "side": nodes[0].get("side", 0), "aux": aux,
            "dual_of": ("group", key[1], key[2]) if aux else None,
            "segments": segments, "first": min(note_ticks),
            "generated": generated,
        }
        lane_infos.append(info)
        by_key[key] = info

    def _find_candidates(info: dict, start: int,
                         written: List[dict]) -> List[Tuple[dict, int]]:
        """返回所有可尝试的 (唤醒者, 取电 tick)，按优劣排序。"""
        target = info["nodes"][start]
        dual_of = info.get("dual_of")
        if dual_of is not None and dual_of in by_key:
            pool = [by_key[dual_of]]
        else:
            pool = [d for d in written if d is not info and not d.get("aux")]
        found = []
        for donor in pool:
            if donor["y"] > info["y"]:
                continue
            for tap in sorted(donor["generated"]):
                node = donor["nodes"][tap]
                if node is None:
                    continue
                if node["entries"]:
                    # 音符盒正上方不能放粉：从侧面取电，额外消耗 1 个 tick
                    steps = start - tap - 1
                    if steps < 2:
                        continue
                    side_z = node["z"] + (1 if target["z"] >= node["z"] else -1)
                    if not _bridge_feasible(donor["y"], side_z, info["y"],
                                            target["z"], steps):
                        continue
                    distance = math.hypot(side_z - target["z"],
                                          donor["y"] - info["y"])
                else:
                    steps = start - tap
                    if steps < 2:
                        continue
                    if not _bridge_feasible(donor["y"], node["z"], info["y"],
                                            target["z"], steps):
                        continue
                    distance = math.hypot(node["z"] - target["z"],
                                          donor["y"] - info["y"])
                rank = (0 if donor["key"][0] == "master" else 1,
                        0 if not node["entries"] else 1,
                        0 if distance <= MIN_RADIUS else 1, distance, steps)
                found.append((rank, donor, tap))
        found.sort(key=lambda item: item[0])
        return [(donor, tap) for _, donor, tap in found]

    def _try_external_bridge(info: dict, label: str, start: int,
                             written: List[dict]) -> bool:
        """依次尝试候选唤醒者；失败就回滚，绝不留下半截结构。"""
        target_z = info["nodes"][start]["z"]
        lane_support = _lane_support(info["side"])
        for donor, tap in _find_candidates(info, start, written):
            node = donor["nodes"][tap]
            snapshot = dict(writer.blocks)
            owners = dict(writer.owners)
            conflict_count = len(writer.conflicts)

            if node["entries"]:
                side_z = node["z"] + (1 if target_z >= node["z"] else -1)
                side_x = node["x"] + 1          # 音符盒本身所在的 X 列
                dust = (side_x, donor["y"], side_z)
                relay = (side_x + 1, donor["y"], side_z)
                below_dust = (dust[0], dust[1] - 1, dust[2])
                below_relay = (relay[0], relay[1] - 1, relay[2])
                ok = (dust not in writer.blocks and relay not in writer.blocks
                      and not str(writer.blocks.get(below_dust, "")).startswith(
                          "minecraft:note_block")
                      and not str(writer.blocks.get(below_relay, "")).startswith(
                          "minecraft:note_block"))
                if ok:
                    writer.set(dust, DUST, label)
                    writer.set(below_dust, lane_support, label)
                    writer.set(relay, _repeater(), label)
                    writer.set(below_relay, lane_support, label)
                    if info["y"] > donor["y"]:
                        # 中继器输出先强充能一个实心块，再由它上方的粉向上传
                        block_pos = (side_x + 2, donor["y"], side_z)
                        if block_pos in writer.blocks:
                            ok = False
                        else:
                            writer.set(block_pos, lane_support, label)
                    if ok:
                        ok = _build_bridge(
                            writer, label, side_x + 1, donor["y"], side_z,
                            info["y"], target_z, start - tap - 1,
                            lane_support, side_start=True)
            else:
                ok = _build_bridge(
                    writer, label, base_x + STEP * tap, donor["y"], node["z"],
                    info["y"], target_z, start - tap, lane_support)

            if ok:
                return True
            writer.blocks.clear()
            writer.blocks.update(snapshot)
            writer.owners.clear()
            writer.owners.update(owners)
            del writer.conflicts[conflict_count:]
        return False

    def _bridge_into(info: dict, label: str, start: int,
                     prev_end: Optional[int], written: List[dict]) -> bool:
        """把信号桥接进 info 在 start 处的启动中继器。"""
        nodes = info["nodes"]
        target_z = nodes[start]["z"]
        lane_support = _lane_support(info["side"])
        freeze = dict(writer.blocks)
        if prev_end is not None:
            anchor = nodes[prev_end]
            steps = start - prev_end
            ok = _build_bridge(writer, label, anchor["x"], info["y"],
                               anchor["z"], info["y"], target_z,
                               steps, lane_support)
        else:
            ok = _try_external_bridge(info, label, start, written)
        if ok:
            for position in writer.blocks:
                if position not in freeze:
                    bridge_cells.add(position)
        return ok

    def _write_range(info: dict, label: str, start: int, end: int,
                     activate: bool, continuation_z: Optional[int] = None) -> None:
        """写一段连续轨道；activate=True 时登记启动方块。

        ``continuation_z``：段间桥失败、直接补空段时，上一格所在的 Z。
        此时首格也要补上横向红石粉，否则首格中继器没有输入。
        """
        nonlocal multi_written
        nodes = info["nodes"]
        key = info["key"]
        y = info["y"]
        lane_side = info["side"]
        lane_support = _lane_support(lane_side)
        previous_z = nodes[start]["z"]
        if continuation_z is not None and continuation_z != previous_z:
            step = 1 if previous_z > continuation_z else -1
            for wire_z in range(continuation_z + step, previous_z + step, step):
                writer.set((nodes[start]["x"] - 1, y, wire_z), DUST, label)
                writer.set((nodes[start]["x"] - 1, y - 1, wire_z),
                           lane_support, label)
        writer.set((nodes[start]["x"], y, previous_z), _repeater(), label)
        writer.set((nodes[start]["x"], y - 1, previous_z), lane_support, label)
        if activate:
            activations[label] = (base_x - 1, y, previous_z)

        for index in range(start, end + 1):
            node = nodes[index]
            x = node["x"]
            z = node["z"]
            # 本 tick 的中继器与音符盒同 Z：中继器输出端正对音符盒 → 强充能
            if index > start:
                writer.set((x, y, z), _repeater(), label)
                writer.set((x, y - 1, z), lane_support, label)
                # 横向移动：红石线把上一个被强充能的音符盒/空音符的信号
                # 送进本中继器的背面（正对中继器输入端）。
                if z != previous_z:
                    step = 1 if z > previous_z else -1
                    for wire_z in range(previous_z + step, z + step, step):
                        writer.set((x - 1, y, wire_z), DUST, label)
                        writer.set((x - 1, y - 1, wire_z), lane_support, label)
            entries = node["entries"]
            # 段尾也要看整条轨道的下一格：侧边音符盒的“安全侧”取决于
            # 下一 tick 的横向移动，必须与 plan_curve 的 movement 完全一致，
            # 否则计划里有的侧边音符在写入时会被换到另一侧并丢掉。
            next_node = nodes[index + 1] if index + 1 < len(nodes) else None
            next_z = next_node["z"] if next_node is not None else z
            delta_units = (next_z - z) * lane_side
            movement = 1 if delta_units > 0 else (-1 if delta_units < 0 else 0)
            if entries:
                lane_distance = abs(z - base_z)
                for offset, entry in _cell_offsets(entries, node["tick"], movement):
                    note = entry["note"]
                    note_block = (f"minecraft:note_block"
                                  f"[note={note_to_pitch(get_note_key(note))}]")
                    bases = _instrument_blocks(note.instrument, lane_support)
                    if key[0] == "master":
                        note_z = z + offset
                    else:
                        note_distance = _effective_note_distance(lane_distance, offset)
                        note_z = base_z + lane_side * note_distance
                    if offset == 0:
                        writer.set((x + 1, y, note_z), note_block, label)
                        for depth, block in enumerate(bases, start=1):
                            writer.set((x + 1, y - depth, note_z), block, label)
                    else:
                        position = (x + 1, y, note_z)
                        if _can_place_side(writer, position, label):
                            writer.set(position, note_block, label)
                            for depth, block in enumerate(bases, start=1):
                                writer.set((x + 1, y - depth, note_z), block, label)
                            multi_written += 1
                        else:
                            # 先占位失败（桥 / 其它轨道挡住）：留到第二遍，
                            # 允许换到中间音符盒的另一侧，避免直接丢音。
                            side_notes.append({
                                "pos": position,
                                "mirror": (x + 1, y, 2 * z - note_z),
                                "block": note_block,
                                "bases": bases,
                                "label": label,
                            })
            else:
                if key[0] == "master":
                    pad = "minecraft:redstone_lamp" if master_lamp else empty
                elif lane_side > 0:
                    pad = lane_right_block
                else:
                    pad = lane_left_block
                writer.set((x + 1, y, z), pad, label)
                writer.set((x + 1, y - 1, z), lane_support, label)
            previous_z = z

    # 先写未被裁剪的轨道，保证后写的线路桥能检测到捐赠轨道的方块
    write_order = sorted(lane_infos,
                         key=lambda item: 0 if item["segments"][0][0] == 0 else 1)
    written: List[dict] = []
    for info in write_order:
        key = info["key"]
        nodes = info["nodes"]
        aux_flag = len(key) > 3 and key[3] == 1
        if key[0] == "master":
            label = f"m{key[1]}"
        else:
            label = f"v{key[1]}{'R' if key[2] > 0 else 'L'}" + ("A" if aux_flag else "")
        segments = list(info["segments"])
        if not segments:
            continue

        if aux_flag:
            segments = list(info["segments"])
            start, end = segments[0]
            if start > 0:
                # 副音轨与外层主轨共用同一 Y 层、同一 X 列节奏，外部桥的
                # 水平红石粉必然和主轨自己的横向红石粉落在同一列并连成
                # 一条（串线）。这里直接从 tick 0 铺一条独立空链并自行启动，
                # 时序与发声完整，结构上也不会和主轨共信号。
                first = info["nodes"][start]
                for tick in range(0, start):
                    info["nodes"][tick] = {
                        "tick": tick, "x": base_x + STEP * tick,
                        "z": first["z"], "y": info["y"],
                        "side": info["side"], "entries": [],
                        "master": False, "aux": True,
                        "distance": first["distance"],
                    }
                info["generated"].update(range(0, start))
                _write_range(info, label, 0, end, activate=True)
                dual_fallback += 1
            else:
                _write_range(info, label, start, end, activate=True)
            written.append(info)
            continue

        if segments[0][0] > 0:
            if _bridge_into(info, label, segments[0][0], None, written):
                bridge_ok += 1
                bridged_starts.add((key, segments[0][0]))
            else:
                # 唤醒失败：恢复完整开头，绝不留下不会响的轨道
                segments = [(0, len(nodes) - 1)]
                info["segments"] = segments
                trim_aborted += 1

        previous_end = None
        for start, end in segments:
            if start == 0:
                _write_range(info, label, start, end, activate=True)
            elif previous_end is None:
                _write_range(info, label, start, end, activate=False)
            else:
                anchor_tick = previous_end
                # 音符盒正上方不能走线：先补一个空 tick 作为取电点。
                # 这一格如果 Z 也变了，必须同时补横向粉，否则首格中继器没有输入。
                if nodes[previous_end]["entries"]:
                    _write_range(info, label, previous_end + 1,
                                 previous_end + 1, activate=False,
                                 continuation_z=nodes[previous_end]["z"])
                    info["generated"].add(previous_end + 1)
                    anchor_tick = previous_end + 1
                if _bridge_into(info, label, start, anchor_tick, written):
                    bridge_ok += 1
                    bridged_starts.add((key, start))
                    _write_range(info, label, start, end, activate=False)
                else:
                    # 段间桥失败：把空段补回来，保持信号连续；
                    # 首格要补横向粉，否则 z 变化时首格中继器没有输入。
                    bridge_skipped += 1
                    info["generated"].update(range(previous_end + 1, end + 1))
                    _write_range(info, label, previous_end + 1, end,
                                 activate=False,
                                 continuation_z=nodes[previous_end]["z"])
            previous_end = end
        written.append(info)

    # 第二遍：第一遍没能安全落位的并排音符盒，先试原位、再试中间音符盒
    # 的另一侧；两侧都被占才省略并计数（只保留中间音符）。
    for item in side_notes:
        label = item["label"]
        written_side = False
        for position in (item["pos"], item["mirror"]):
            if not _can_place_side(writer, position, label):
                continue
            writer.set(position, item["block"], label)
            x, y, z = position
            for depth, block in enumerate(item["bases"], start=1):
                writer.set((x, y - depth, z), block, label)
            written_side = True
            break
        if written_side:
            multi_written += 1
        else:
            multi_avoided += 1

    # ── 链路连续性复核：生成后逐 lane 检查是否有断链 ─────────
    chain_gaps: List[dict] = []
    for info in lane_infos:
        nodes = info["nodes"]
        lane_y = info["y"]
        lane_key = info["key"]
        generated = sorted(info["generated"])
        for tick in generated:
            node = nodes[tick] if tick < len(nodes) else None
            if node is None:
                continue
            relay = writer.blocks.get((node["x"], lane_y, node["z"]))
            pad = writer.blocks.get((node["x"] + 1, lane_y, node["z"]))
            if relay is None or "repeater" not in relay:
                chain_gaps.append({"lane": lane_key, "tick": tick,
                                   "missing": "repeater"})
            if pad is None:
                chain_gaps.append({"lane": lane_key, "tick": tick, "missing": "pad"})
        for before, after in zip(generated, generated[1:]):
            if after != before + 1:
                continue
            prev = nodes[before] if before < len(nodes) else None
            cur = nodes[after] if after < len(nodes) else None
            if prev is None or cur is None or prev["z"] == cur["z"]:
                continue
            step = 1 if cur["z"] > prev["z"] else -1
            for wire_z in range(prev["z"] + step, cur["z"] + step, step):
                if writer.blocks.get((cur["x"] - 1, lane_y, wire_z)) != DUST:
                    chain_gaps.append({"lane": lane_key, "tick": after,
                                       "missing": "lateral"})
        for tick in generated:
            if (lane_key, tick) not in bridged_starts:
                continue
            node = nodes[tick] if tick < len(nodes) else None
            if node is None:
                continue
            inlet = writer.blocks.get((node["x"] - 1, lane_y, node["z"]))
            if inlet != DUST:
                chain_gaps.append({"lane": lane_key, "tick": tick,
                                   "missing": "bridge-inlet"})

    meta = {
        "plan": plan,
        "player": {"x0": base_x + 1, "y": player_y, "z": base_z},
        "minecart": None,
        "activations": activations,
    }
    want_cart = config.get("curve_minecart", True) if minecart is None else minecart
    any_nodes = next(iter(plan["lanes"].values()), [])
    if want_cart and any_nodes:
        sound_range = float(plan.get("sound_range", 48.0))
        command_side = str(config.get("curve_command_side", "right")).lower()
        if command_side not in ("left", "right", "up", "down"):
            command_side = "right"
        max_distance = max((abs(entry["pos"][2] - base_z) for entry in plan["notes"]),
                           default=MIN_DUAL_DISTANCE)
        clearance = max(3, int(math.ceil(sound_range)))
        # 命令方块链尽量放到 48 格听力范围外，避免遮挡与干扰
        if command_side == "left":
            anchor_z = base_z - max(clearance, max_distance + 3)
            anchor_y = player_y
        elif command_side == "up":
            anchor_z = base_z
            anchor_y = player_y + max(clearance, plan.get("corridor", 2) + 3)
        elif command_side == "down":
            anchor_z = base_z
            anchor_y = player_y - max(clearance, plan.get("corridor", 2) + 3)
        else:
            anchor_z = base_z + max(clearance, max_distance + 3)
            anchor_y = player_y

        # 跟随矿车：沿 +X 每 tick 一个 tp 命令方块。
        # 命令必须用相对坐标（粘贴到任意世界坐标后才能工作），
        # 偏移量按「命令方块 → 玩家走廊」计算，保证落在走廊。
        writer.set((base_x, anchor_y, anchor_z), _repeater())
        writer.set((base_x, anchor_y - 1, anchor_z), support)
        for index in range(len(any_nodes)):
            command_x = base_x + STEP * index + 1
            cart_x = base_x + STEP * index + 1.5
            dx = cart_x - command_x
            dy = player_y - anchor_y
            dz = base_z - anchor_z
            command = (f"tp @e[type=minecart,tag={MINECART_TAG},limit=1] "
                       f"~{dx:.1f} ~{dy:.1f} ~{dz:.1f}")
            writer.set((command_x, anchor_y, anchor_z),
                       f"minecraft:command_block[facing=up]"
                       f"{{Command:{json.dumps(command)}}}")
            writer.set((command_x, anchor_y - 1, anchor_z), support)
            writer.set((command_x + 1, anchor_y, anchor_z), _repeater())
            writer.set((command_x + 1, anchor_y - 1, anchor_z), support)

        # 召唤 / 骑乘：从同一个启动方块分两条支路，
        # ride 支路多两级延迟，确保 summon 先执行、矿车已经存在。
        # 同样用相对坐标，偏移到走廊。
        summon_dx = (base_x + 1.5) - (base_x - 1)
        summon_dy = player_y - anchor_y
        summon_dz = base_z - (anchor_z + 2)
        summon = (f"summon minecart ~{summon_dx:.1f} ~{summon_dy:.1f} "
                  f"~{summon_dz:.1f} "
                  f"{{Invisible:1b,Silent:1b,NoGravity:1b,Tags:[\"{MINECART_TAG}\"]}}")
        ride = f"ride @p mount @e[type=minecart,tag={MINECART_TAG},limit=1]"
        writer.set((base_x - 1, anchor_y, anchor_z + 1),
                   "minecraft:repeater[facing=north,delay=1]")
        writer.set((base_x - 1, anchor_y - 1, anchor_z + 1), support)
        writer.set((base_x - 1, anchor_y, anchor_z + 2),
                   f"minecraft:command_block[facing=up]{{Command:{json.dumps(summon)}}}")
        writer.set((base_x - 1, anchor_y, anchor_z - 1),
                   "minecraft:repeater[facing=south,delay=3]")
        writer.set((base_x - 1, anchor_y - 1, anchor_z - 1), support)
        writer.set((base_x - 1, anchor_y, anchor_z - 2),
                   f"minecraft:command_block[facing=up]{{Command:{json.dumps(ride)}}}")
        activations["minecart"] = (base_x - 1, anchor_y, anchor_z)
        meta["minecart"] = {"z": anchor_z, "y": anchor_y, "side": command_side,
                            "summon": summon, "ride": ride}

    commands = writer.commands()
    audit = _audit_layout(writer, full=plan.get("multi_cells", 0) > 0,
                          ignore=bridge_cells)
    meta["conflicts"] = writer.conflicts
    meta["note_overheads"] = audit["overheads"]
    meta["crossing"] = audit["crossing"]
    meta["touching"] = audit["touching"]
    meta["multi_written"] = multi_written
    meta["multi_avoided"] = multi_avoided
    meta["multi_track"] = plan.get("multi_cells", 0) > 0
    meta["bridge_ok"] = bridge_ok
    meta["bridge_skipped"] = bridge_skipped
    meta["bridge_cells"] = len(bridge_cells)
    meta["trim_aborted"] = trim_aborted
    meta["trim_threshold"] = trim_threshold
    meta["trim_seconds"] = trim_seconds
    meta["dual_skipped"] = dual_skipped
    meta["dual_fallback"] = dual_fallback
    meta["dual_tracks"] = sum(1 for info in lane_infos if info.get("aux"))
    meta["trimmed"] = sum(
        1 for info in lane_infos
        if not info.get("aux") and (
            len(info["segments"]) > 1 or info["segments"][0][0] > 0))
    meta["chain_gaps"] = len(chain_gaps)
    meta["chain_gap_items"] = chain_gaps[:8]
    meta["block_count"] = len(writer.blocks)
    # 用几何规则独立复核一次串线（含线路桥），作为最后一道保险
    try:
        from .simulate import (check_crossing, check_dust_endpoints,
                               check_power_paths, world_from_commands)
        world = world_from_commands(commands)
        crossing_info = check_crossing(world)
        meta["world_crossing"] = crossing_info["count"]
        meta["world_crossing_items"] = crossing_info["items"][:8]
        power = check_power_paths(world)
        meta["unpowered_dust"] = power["components"]
        meta["unpowered_dust_items"] = [item[:4] for item in power["unpowered"][:4]]
        endpoint = check_dust_endpoints(world)
        meta["dust_side_notes"] = endpoint["count"]
        meta["dust_side_note_items"] = endpoint["items"][:8]
        floating = [pos for pos, block in world.items()
                    if block["id"] in ("redstone_wire", "repeater")
                    and (pos[0], pos[1] - 1, pos[2]) not in world]
        covered = [pos for pos, block in world.items()
                   if block["id"] == "note_block"
                   and (pos[0], pos[1] + 1, pos[2]) in world]
        meta["floating_parts"] = len(floating)
        meta["covered_notes"] = len(covered)
    except Exception:
        meta["world_crossing"] = 0
        meta["world_crossing_items"] = []
        meta["unpowered_dust"] = 0
        meta["unpowered_dust_items"] = []
        meta["dust_side_notes"] = 0
        meta["dust_side_note_items"] = []
        meta["floating_parts"] = 0
        meta["covered_notes"] = 0
    return commands, activations, meta
