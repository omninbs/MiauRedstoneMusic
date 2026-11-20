"""曲线音量排版（curve）。

设计目标
--------
1. 音量由「音符盒到中心轴的横向距离」决定：``音量 ≈ 1 - 距离 / 48``。
2. 立体声关于延伸轴对称：左右两侧距离相同（离轴 < 1 格的中心音符不要求对称）。
3. 所有音符必须在正确的歌曲 tick 发声，因此每条分支的**中继器级数固定**，
   这样每条分支引入的延迟完全一致，事件之间的相对时间不会漂移。

结构拓扑
--------
每个分组占一条「车道」（Y 方向按 ``curve_lane_spacing`` 叠放），车道里：
- ``z = ±1`` 各有一条主干中继器链（只在需要该侧音符时才生成），
  主干每歌曲 tick 前进 2 格，``cell`` 是一个被中继器强充能的羊毛块；
- 从 cell 沿 ±z 伸出分支，分支末端的音符盒就是发声点；
- 分支里固定串 ``n`` 个中继器：中继器负责在 15 格信号衰减前重新充能，
  同时把这一侧的音符整体延后 ``2n`` 游戏 tick —— 因为全曲固定，所以音轨同步。

玩家（隐形矿车）沿 ``z = 0.5`` 的中心走廊前进，z=0 这一列永远是空的，
因此矿车不会被方块卡住。
"""

import json
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .constants import FALLING_BLOCK_SUPPORT, INSTRUMENT_BLOCK_MAP
from .generation import (
    chain_valid_ticks,
    get_note_key,
    note_to_pitch,
    split_to_repeaters,
)
from .volume import (
    branch_distance_range,
    clamp,
    lateral_from_3d,
    layer_pan,
    layer_volume,
    note_pan,
    note_velocity,
    plan_repeater_count,
    target_distance,
)

DUST = "minecraft:redstone_wire"
WOOL = "minecraft:white_wool"
TRUNK_FACING = "west"          # 结构沿 +X 延展：中继器 facing=west 时输出朝 +X
BRANCH_FACING = {1: "north", -1: "south"}   # 分支朝 ±Z：facing 在输出方向的反面
MINECART_TAG = "miau_cart"


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


def _instrument_block(instrument: int) -> List[str]:
    """音符盒下方的音色方块（与线性版保持一致）。"""
    if instrument == 0:
        return []
    blocks = [INSTRUMENT_BLOCK_MAP.get(instrument, "minecraft:stone")]
    if instrument == 3:
        blocks.append(FALLING_BLOCK_SUPPORT)
    return blocks


def _repeater(delay: int) -> str:
    return f"minecraft:repeater[facing={TRUNK_FACING},delay={delay}]"


def _branch_parts(distance: int, repeaters: int) -> List[Tuple[int, str]]:
    """分支上 (离轴格数, 类型) 序列；最后一项一定是音符盒。

    规则：第 1 格是 cell（羊毛），第 2 格起是分支本体；
    固定串 ``repeaters`` 个中继器，第一个紧贴 cell，之后每段红石线 ≤ 15 格。
    """
    if distance < 2:
        distance = 2
    if repeaters <= 0:
        return [(pos, "dust") for pos in range(2, distance)] + [(distance, "note")]

    dust_total = distance - 2 - repeaters
    base = dust_total // repeaters
    extra = dust_total % repeaters
    parts: List[Tuple[int, str]] = [(2, "repeater")]
    pos = 3
    for index in range(repeaters):
        run = base + (1 if index < extra else 0)
        for _ in range(run):
            parts.append((pos, "dust"))
            pos += 1
        if index < repeaters - 1:
            parts.append((pos, "repeater"))
            pos += 1
    parts.append((distance, "note"))
    return parts


def _chain_layout(valid_ticks: List[int], uniform: bool):
    """主干布局：返回 [(cell_offset, tick|None), ...] 与 [(relay_offset, delay), ...]。"""
    cells: List[Tuple[int, Optional[int]]] = []
    relays: List[Tuple[int, int]] = [(0, 1)]
    cursor = 0
    previous: Optional[int] = None
    for tick in valid_ticks:
        blank = 0 if previous is None else tick - previous - 1
        if blank > 0:
            delays = [1] * blank if uniform else split_to_repeaters(blank)
            for delay in delays:
                cells.append((cursor + 1, None))
                cursor += 1
                relays.append((cursor + 1, delay))
                cursor += 1
        cells.append((cursor + 1, tick))
        cursor += 1
        relays.append((cursor + 1, 1))
        cursor += 1
        previous = tick
    return cells, relays


def plan_curve(virtual_layers: List[dict], layer_to_group: Dict[int, int],
               base: Tuple[int, int, int], max_tick: int, config: dict,
               song=None) -> dict:
    """计算每个音符的落点（不做命令生成，便于测试与统计）。"""
    groups = sorted(set(layer_to_group.values()))
    if not groups:
        return {"groups": [], "notes": [], "player_y": base[1]}

    spacing = max(2, int(config.get("curve_lane_spacing", 3)))
    sound_range = float(config.get("curve_sound_range", 48.0))
    base_x, base_y, base_z = base

    group_order = {gid: index for index, gid in enumerate(groups)}
    lane_y = {gid: base_y + spacing * group_order[gid] for gid in groups}
    player_y = base_y + spacing * (len(groups) - 1) // 2

    # 收集每个分组、每个 tick 的音符
    events: Dict[int, Dict[int, List[Tuple[object, int]]]] = defaultdict(lambda: defaultdict(list))
    for virtual in virtual_layers:
        gid = layer_to_group.get(virtual["vid"])
        if gid is None:
            continue
        for note in virtual["notes"]:
            if 0 <= note.tick <= max_tick:
                events[gid][note.tick].append((note, virtual["vid"]))

    # 计算每个音符的目标横向距离（先不量化，用于决定分支中继器数量）
    stereo_vids = {virtual["vid"] for virtual in virtual_layers
                   if virtual.get("side") == "stereo"}
    raw_targets: List[float] = []
    per_note: List[dict] = []
    for gid, ticks in events.items():
        vertical = abs(lane_y[gid] + 0.5 - player_y)
        for tick, items in ticks.items():
            for note, vid in items:
                original_id = next((v.get("original_id", v["vid"]) for v in virtual_layers
                                    if v["vid"] == vid), vid)
                volume = note_velocity(note) * layer_volume(song, original_id)
                pan = note_pan(note) + layer_pan(song, original_id)
                target = target_distance(volume, sound_range)
                lateral = lateral_from_3d(target, vertical)
                raw_targets.append(lateral)
                per_note.append({
                    "gid": gid, "vid": vid, "tick": tick, "note": note,
                    "fixed_side": None if abs(pan) <= 1e-6 else (-1 if pan < 0 else 1),
                    "stereo": vid in stereo_vids,
                    "target": target, "vertical": vertical, "raw": lateral,
                })

    # 分配左右侧：
    # - 立体声轨：复制出的两个音符严格一左一右（关于延伸轴对称）；
    # - 有明确声像的音符：尊重声像；
    # - 居中音符：左右轮流放置，避免同一侧挤两个不同距离的音符丢音量。
    stereo_cursor: Dict[Tuple[int, int], int] = defaultdict(int)
    side_counts: Dict[Tuple[int, int], Dict[int, int]] = defaultdict(lambda: {-1: 0, 1: 0})
    for entry in per_note:
        key = (entry["gid"], entry["tick"])
        if entry["stereo"]:
            cursor_key = (entry["vid"], entry["tick"])
            side = -1 if stereo_cursor[cursor_key] % 2 == 0 else 1
            stereo_cursor[cursor_key] += 1
        elif entry["fixed_side"] is not None:
            side = entry["fixed_side"]
        else:
            counts = side_counts[key]
            side = -1 if counts[-1] < counts[1] else 1
        entry["side"] = side
        side_counts[key][side] += 1

    max_raw = max(raw_targets) if raw_targets else 0.0
    configured = int(config.get("curve_branch_repeaters", -1))
    repeated = plan_repeater_count(max_raw) if configured < 0 else int(clamp(configured, 0, 3))
    low, high = branch_distance_range(repeated)

    # 同一 tick、同一侧的音符共用一个分支：距离取最大（最安静的那个）
    per_branch: Dict[Tuple[int, int, int], float] = {}
    for entry in per_note:
        key = (entry["gid"], entry["tick"], entry["side"])
        per_branch[key] = max(per_branch.get(key, 0.0), entry["raw"])

    branch_distance: Dict[Tuple[int, int, int], int] = {}
    for key, raw in per_branch.items():
        branch_distance[key] = int(round(clamp(raw, low, high)))

    # 逐个分配落点：同侧第 2/3 个音符叠在分支末端外侧
    placed: List[dict] = []
    used: Dict[Tuple[int, int, int], int] = defaultdict(int)
    dropped = 0
    for entry in per_note:
        key = (entry["gid"], entry["tick"], entry["side"])
        index = used[key]
        if index >= 3:
            dropped += 1
            continue
        used[key] += 1
        distance = branch_distance[key] + index
        entry = dict(entry)
        entry["distance"] = distance
        entry["primary"] = (index == 0)
        entry["volume_plan"] = 1.0 - ((distance ** 2 + entry["vertical"] ** 2) ** 0.5) / sound_range
        entry["volume_nbs"] = note_velocity(entry["note"]) * layer_volume(
            song, next((v.get("original_id", v["vid"]) for v in virtual_layers
                        if v["vid"] == entry["vid"]), entry["vid"]))
        placed.append(entry)

    return {
        "groups": groups,
        "lane_y": lane_y,
        "player_y": player_y,
        "spacing": spacing,
        "sound_range": sound_range,
        "branch_repeaters": repeated,
        "distance_low": low,
        "distance_high": high,
        "branch_distance": branch_distance,
        "notes": placed,
        "dropped": dropped,
        "base": base,
    }


def build_curve_commands(virtual_layers: List[dict], layer_to_group: Dict[int, int],
                         base: Tuple[int, int, int], max_tick: int, config: dict,
                         direction: str = "east", song=None,
                         minecart: Optional[bool] = None) -> Tuple[List[str], dict, dict]:
    """生成曲线排版 + 隐形矿车跟随的完整命令列表。"""
    if direction != "east":
        raise ValueError("曲线排版当前只支持 east（+X）延展方向")

    plan = plan_curve(virtual_layers, layer_to_group, base, max_tick, config, song)
    groups = plan["groups"]
    writer = BlockWriter()
    base_x, base_y, base_z = base
    lane_y = plan["lane_y"]
    player_y = plan["player_y"]
    repeated = plan["branch_repeaters"]
    uniform = bool(config.get("uniform_repeater_mode", False))
    valid_ticks = chain_valid_ticks(virtual_layers, max_tick)
    cells, relays = _chain_layout(valid_ticks, uniform)

    # (gid, tick) → {side: [notes...]}
    by_tick: Dict[Tuple[int, int], Dict[int, List[dict]]] = defaultdict(lambda: defaultdict(list))
    for entry in plan["notes"]:
        by_tick[(entry["gid"], entry["tick"])][entry["side"]].append(entry)
    for bucket in by_tick.values():
        for entries in bucket.values():
            entries.sort(key=lambda item: item["distance"])

    # 需要生成的（分组, 侧）组合
    sides = defaultdict(set)
    for entry in plan["notes"]:
        sides[entry["gid"]].add(entry["side"])

    activations: Dict[str, Tuple[int, int, int]] = {}

    def place_branch(x: int, y: int, side: int, entries: List[dict]) -> None:
        distance = entries[0]["distance"]
        z_cell = side
        for offset, kind in _branch_parts(distance, repeated):
            z = side * offset
            if kind == "dust":
                writer.set((x, y, z), DUST)
            elif kind == "repeater":
                writer.set((x, y, z),
                           f"minecraft:repeater[facing={BRANCH_FACING[side]},delay=1]")
        for index, entry in enumerate(entries):
            z = side * (entry["distance"])
            note = entry["note"]
            writer.set((x, y, z), f"minecraft:note_block[note={note_to_pitch(get_note_key(note))}]")
            below = _instrument_block(note.instrument)
            for depth, block in enumerate(below, start=1):
                writer.set((x, y - depth, z), block)

    for gid in groups:
        y = lane_y[gid]
        for side in sorted(sides.get(gid, ())):
            z_cell = side
            label = f"g{gid}_{'R' if side > 0 else 'L'}"
            activations[label] = (base_x - 1, y, base_z + z_cell)
            for offset, tick in cells:
                x = base_x + offset
                writer.set((x, y, base_z + z_cell), WOOL)
                if tick is None:
                    continue
                entries = by_tick.get((gid, tick), {}).get(side, [])
                if entries:
                    place_branch(x, y, side, entries)
            for offset, delay in relays:
                writer.set((base_x + offset, y, base_z + z_cell),
                           f"minecraft:repeater[facing={TRUNK_FACING},delay={delay}]")

    # ── 隐形矿车跟随 ─────────────────────────────────────────
    meta = {
        "plan": plan,
        "player": {"x0": base_x + 1, "y": player_y, "z": base_z + 0.5},
        "minecart": None,
        "activations": activations,
    }
    want_cart = config.get("curve_minecart", True) if minecart is None else minecart
    if want_cart and cells:
        max_distance = max((entry["distance"] for entry in plan["notes"]), default=0)
        cart_z = base_z + max_distance + 4
        cart_layer = 1 + repeated     # 矿车链比音乐链整体晚 2n 游戏 tick
        for offset, tick in cells:
            x = base_x + offset
            command = (f"tp @e[type=minecart,tag={MINECART_TAG},limit=1] "
                       f"~0.5 ~ ~{base_z + 0.5 - cart_z:.1f}")
            writer.set((x, player_y, cart_z),
                       f"minecraft:command_block[facing=up]"
                       f"{{Command:{json.dumps(command)}}}")
        for offset, delay in relays:
            effective = cart_layer if offset == 0 else delay
            writer.set((base_x + offset, player_y, cart_z),
                       f"minecraft:repeater[facing={TRUNK_FACING},delay={max(1, min(4, effective))}]")
        start_x = base_x + 1
        summon = (f"summon minecart ~1.5 ~-1 ~{base_z + 0.5 - cart_z:.1f} "
                  f"{{Invisible:1b,Silent:1b,NoGravity:1b,Tags:[\"{MINECART_TAG}\"]}}")
        ride = f"ride @p mount @e[type=minecart,tag={MINECART_TAG},limit=1]"
        writer.set((base_x - 1, player_y + 1, cart_z),
                   f"minecraft:command_block[facing=up]{{Command:{json.dumps(summon)}}}")
        writer.set((base_x - 1, player_y - 1, cart_z),
                   f"minecraft:command_block[facing=up]{{Command:{json.dumps(ride)}}}")
        activations["minecart"] = (base_x - 1, player_y, cart_z)
        meta["minecart"] = {"z": cart_z, "start_x": start_x,
                            "summon": summon, "ride": ride}

    commands = writer.commands()
    meta["conflicts"] = writer.conflicts
    meta["block_count"] = len(writer.blocks)
    meta["cells"] = cells
    return commands, activations, meta
