"""结构加载 + 红石模拟 + 验收比对。

用途
----
把生成出来的 ``.schem``（或生成器输出的 setblock 命令）重新读进来，逐游戏 tick
模拟红石：

* 中继器的 1~4 红石刻延迟（2~8 游戏 tick）
* 红石线瞬时传导 + 每格衰减 1、最多 15 格
* 中继器 / 红石块 / 被强充能方块对音符盒、命令方块的激活
* 音符盒只在上电沿触发一次

然后输出每个游戏 tick 的音符事件，并按「玩家（矿车）位置」算真实的三维距离
与音量衰减，用于和 NBS 原始音符逐 tick 比对。

方块语义按 Java 版实现：
- 中继器 ``facing`` 是「输出侧 → 输入侧」的方向，所以输出方向 = facing 的反方向；
- 中继器输出会强充能正前方那一格方块；
- 红石线相邻即可互连（本项目的线路都是平面直线），信号强度每格 -1；
- 音符盒会导电：被激活的音符盒会激活紧邻的音符盒。
"""

import json
import re
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple

from .constants import INSTRUMENT_BLOCK_MAP
from .generation import note_to_pitch
from .volume import LISTENER_EYE, distance_3d, volume_from_distance

Coord = Tuple[int, int, int]

SETBLOCK_RE = re.compile(r"^setblock\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+)$")
DELAY_RE = re.compile(r"delay=(\d+)")
FACING_RE = re.compile(r"facing=([a-z]+)")
NOTE_RE = re.compile(r"note=(\d+)")

FACING_VECTORS = {
    "east": (1, 0, 0),
    "west": (-1, 0, 0),
    "south": (0, 0, 1),
    "north": (0, 0, -1),
    "up": (0, 1, 0),
    "down": (0, -1, 0),
}
NEIGHBORS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))

REVERSE_INSTRUMENT = {value: key for key, value in INSTRUMENT_BLOCK_MAP.items()}


# ── 方块解析 ────────────────────────────────────────────────

def parse_block(text: str) -> dict:
    """``minecraft:repeater[facing=west,delay=2]{...}`` → {id, props, nbt}。"""
    text = text.strip()
    nbt = None
    brace = text.find("{")
    if brace != -1:
        nbt = text[brace:]
        text = text[:brace]
    props: Dict[str, str] = {}
    bracket = text.find("[")
    if bracket != -1:
        end = text.rfind("]")
        body = text[bracket + 1:end] if end > bracket else ""
        for part in body.split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                props[key.strip()] = value.strip()
        text = text[:bracket]
    block_id = text.strip()
    if block_id.startswith("minecraft:"):
        block_id = block_id[len("minecraft:"):]
    return {"id": block_id, "props": props, "nbt": nbt}


def _command_text(nbt: Optional[str]) -> Optional[str]:
    if not nbt:
        return None
    match = re.search(r'Command:("(?:[^"\\]|\\.)*")', nbt)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except (ValueError, TypeError):
        return None


def world_from_commands(commands: List[str]) -> Dict[Coord, dict]:
    world: Dict[Coord, dict] = {}
    for raw in commands:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = SETBLOCK_RE.match(line)
        if not match:
            continue
        coord = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        world[coord] = parse_block(match.group(4))
    return world


FLOATING_IDS = {"redstone_wire"}


def check_structure(world: Dict[Coord, dict]) -> List[str]:
    """检查进游戏会出问题的方块（目前主要查悬空红石线）。

    红石线必须放在实体方块上，否则粘贴后会直接掉成物品；
    中继器/音符盒/命令方块允许悬空，但生成器仍会给它们垫底。
    """
    issues: List[str] = []
    for position, block in world.items():
        if block["id"] not in FLOATING_IDS:
            continue
        below = world.get((position[0], position[1] - 1, position[2]))
        if below is None or below["id"] in ("air", "cave_air", "void_air"):
            issues.append(f"悬空红石线 @ {position}")
    return issues


def _varints(data: bytes):
    index = 0
    total = len(data)
    while index < total:
        value = 0
        shift = 0
        while True:
            if index >= total:
                return
            byte = data[index]
            index += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        yield value


def load_schem(path: str) -> Dict[Coord, dict]:
    """读取 ``.schem``：兼容 WorldEdit v3 与 Sponge v2。"""
    import nbtlib

    root = nbtlib.load(path)
    if "Schematic" in root and "Blocks" in root["Schematic"]:
        return _load_worldedit(root["Schematic"])

    width = int(root["Width"])
    height = int(root["Height"])
    length = int(root["Length"])
    palette = {int(value): str(key) for key, value in root["Palette"].items()}
    block_data = bytes(root["BlockData"])
    metadata = root.get("Metadata") or {}
    offset = (int(metadata.get("WEOffsetX", 0)),
              int(metadata.get("WEOffsetY", 0)),
              int(metadata.get("WEOffsetZ", 0)))

    world: Dict[Coord, dict] = {}
    ids = _varints(block_data)
    for y in range(height):
        for z in range(length):
            for x in range(width):
                try:
                    palette_id = next(ids)
                except StopIteration:
                    break
                if palette_id <= 0:
                    continue
                text = palette.get(palette_id, "minecraft:air")
                block = parse_block(text)
                if block["id"] in ("air", "cave_air", "void_air"):
                    continue
                world[(x + offset[0], y + offset[1], z + offset[2])] = block

    for entity in root.get("BlockEntities") or []:
        try:
            position = entity["Pos"]
            coord = (int(position[0]) + offset[0],
                     int(position[1]) + offset[1],
                     int(position[2]) + offset[2])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        block = world.get(coord)
        if block is None:
            continue
        command = entity.get("Command")
        if command is not None:
            block = dict(block)
            block["nbt"] = "{Command:" + json.dumps(str(command)) + "}"
            world[coord] = block
    return world


def _load_worldedit(schematic) -> Dict[Coord, dict]:
    """WorldEdit ``.schem``（根键 Schematic，方块在 Blocks 复合里）。"""
    width = int(schematic["Width"])
    height = int(schematic["Height"])
    length = int(schematic["Length"])
    blocks_tag = schematic["Blocks"]
    palette = {int(value): str(key) for key, value in blocks_tag["Palette"].items()}
    data = bytes(blocks_tag["Data"])
    offset_tag = schematic.get("Offset") or (0, 0, 0)
    offset = (int(offset_tag[0]), int(offset_tag[1]), int(offset_tag[2]))

    world: Dict[Coord, dict] = {}
    ids = _varints(data)
    for y in range(height):
        for z in range(length):
            for x in range(width):
                try:
                    palette_id = next(ids)
                except StopIteration:
                    break
                text = palette.get(palette_id, "minecraft:air")
                block = parse_block(text)
                if block["id"] in ("air", "cave_air", "void_air"):
                    continue
                world[(x + offset[0], y + offset[1], z + offset[2])] = block

    for entity in blocks_tag.get("BlockEntities") or []:
        try:
            position = entity["Pos"]
            coord = (int(position[0]) + offset[0],
                     int(position[1]) + offset[1],
                     int(position[2]) + offset[2])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        block = world.get(coord)
        if block is None:
            continue
        command = entity.get("Command")
        if command is not None:
            block = dict(block)
            block["nbt"] = "{Command:" + json.dumps(str(command)) + "}"
            world[coord] = block
    return world


# ── 模拟器 ──────────────────────────────────────────────────

class Simulator:
    """逐游戏 tick 的红石模拟。"""

    def __init__(self, world: Dict[Coord, dict], limit_ticks: int = 20000):
        self.world = dict(world)
        self.limit_ticks = limit_ticks
        self.repeaters: Dict[Coord, dict] = {}
        self.notes: Dict[Coord, dict] = {}
        self.commands: Dict[Coord, str] = {}
        self.redstone_blocks = set()
        self.dust = set()
        self.solids = set()

        for position, block in self.world.items():
            block_id = block["id"]
            if block_id == "repeater":
                facing = block["props"].get("facing", "north")
                vector = FACING_VECTORS.get(facing, (0, 0, 1))
                # facing 是「输出侧 → 输入侧」，所以输出方向取反
                out_dir = (-vector[0], -vector[1], -vector[2])
                delay = int(block["props"].get("delay", 1) or 1)
                self.repeaters[position] = {"out": out_dir, "delay": delay}
            elif block_id == "note_block":
                self.solids.add(position)
                pitch = int(block["props"].get("note", 0) or 0)
                below = self.world.get((position[0], position[1] - 1, position[2]))
                instrument = 0
                if below is not None:
                    instrument = REVERSE_INSTRUMENT.get("minecraft:" + below["id"], 0)
                self.notes[position] = {"pitch": pitch, "instrument": instrument}
            elif block_id in ("command_block", "chain_command_block", "repeating_command_block"):
                self.solids.add(position)
                text = _command_text(block.get("nbt"))
                if text:
                    self.commands[position] = text
            elif block_id == "redstone_block":
                self.redstone_blocks.add(position)
            elif block_id == "redstone_wire":
                self.dust.add(position)
            elif block_id not in ("air", "water", "lava"):
                self.solids.add(position)

        # 可被强充能并继续激活相邻元件的「导体方块」
        # 音符盒会导电（参考结构里中继器正是靠被强充能的音符盒续链），
        # 中继器/红石线自身不作为导体
        non_conductors = set(self.repeaters) | self.dust
        self.conductors = self.solids - non_conductors

    # ── 红石线连通分量 ─────────────────────────────────────
    def _components(self):
        comp_id: Dict[Coord, int] = {}
        comps: List[List[Coord]] = []
        for cell in self.dust:
            if cell in comp_id:
                continue
            index = len(comps)
            bucket: List[Coord] = []
            queue = deque([cell])
            comp_id[cell] = index
            while queue:
                current = queue.popleft()
                bucket.append(current)
                for delta in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)):
                    neighbor = (current[0] + delta[0], current[1] + delta[1], current[2] + delta[2])
                    if neighbor in self.dust and neighbor not in comp_id:
                        comp_id[neighbor] = index
                        queue.append(neighbor)
            comps.append(bucket)
        return comp_id, comps

    # ── 主循环 ──────────────────────────────────────────────
    def run(self, launch: List[Coord], max_ticks: Optional[int] = None,
            initial_player: Optional[Tuple[float, float, float]] = None) -> dict:
        comp_id, comps = self._components()
        level: Dict[Coord, int] = {cell: 0 for cell in self.dust}
        dirty = set(range(len(comps)))

        for position in launch:
            self.world[position] = {"id": "redstone_block", "props": {}, "nbt": None}
            self.redstone_blocks.add(position)

        rep_out = {position: False for position in self.repeaters}
        scheduled: Dict[Coord, Tuple[int, bool]] = {}
        pending: Dict[int, List[Tuple[Coord, bool]]] = defaultdict(list)

        note_state = {position: False for position in self.notes}
        command_state = {position: False for position in self.commands}
        events: List[dict] = []
        player_track: List[dict] = []
        player = initial_player
        start_player = initial_player

        limit = max_ticks if max_ticks is not None else self.limit_ticks
        for tick in range(0, limit):
            for position, value in pending.pop(tick, []):
                rep_out[position] = value
                scheduled.pop(position, None)
                for delta in NEIGHBORS:
                    neighbor = (position[0] + delta[0], position[1] + delta[1],
                                position[2] + delta[2])
                    if neighbor in comp_id:
                        dirty.add(comp_id[neighbor])
                front = (position[0] + self.repeaters[position]["out"][0],
                         position[1] + self.repeaters[position]["out"][1],
                         position[2] + self.repeaters[position]["out"][2])
                for delta in NEIGHBORS:
                    neighbor = (front[0] + delta[0], front[1] + delta[1], front[2] + delta[2])
                    if neighbor in comp_id:
                        dirty.add(comp_id[neighbor])

            # 强充能位置：中继器正前方（facing 反方向）
            powered = set()
            for position, state in rep_out.items():
                if state:
                    out = self.repeaters[position]["out"]
                    powered.add((position[0] + out[0], position[1] + out[1], position[2] + out[2]))

            # 红石线电平
            for index in sorted(dirty):
                bucket = comps[index]
                seeds = {}
                for cell in bucket:
                    best = 0
                    if cell in powered:
                        best = 15
                    else:
                        for delta in NEIGHBORS:
                            neighbor = (cell[0] + delta[0], cell[1] + delta[1], cell[2] + delta[2])
                            block = self.world.get(neighbor)
                            if block is None:
                                continue
                            if block["id"] == "redstone_block":
                                best = 15
                                break
                            if neighbor in powered and neighbor in self.conductors:
                                best = 15
                                break
                    if best:
                        seeds[cell] = best
                for cell in bucket:
                    level[cell] = 0
                queue = deque(seeds)
                for cell, value in seeds.items():
                    level[cell] = value
                while queue:
                    cell = queue.popleft()
                    current = level[cell]
                    if current <= 1:
                        continue
                    for delta in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1)):
                        neighbor = (cell[0] + delta[0], cell[1] + delta[1], cell[2] + delta[2])
                        if neighbor in self.dust and level.get(neighbor, 0) < current - 1:
                            level[neighbor] = current - 1
                            queue.append(neighbor)
            dirty.clear()

            # 注意：红石粉只能弱充能「端点正对」的方块，不能侧向充能。
            # 生成器因此不依赖红石粉给任何方块充能：横向移动时红石粉只把
            # 上一个被中继器强充能的音符盒信号送进下一个中继器的背面。

            # 音符盒激活（含音符盒之间的导电传播）
            seeds = set()
            for position in self.notes:
                if position in powered:
                    seeds.add(position)
                    continue
                activated = False
                for delta in NEIGHBORS:
                    neighbor = (position[0] + delta[0], position[1] + delta[1],
                                position[2] + delta[2])
                    if level.get(neighbor, 0) > 0:
                        activated = True
                        break
                    block = self.world.get(neighbor)
                    if block is None:
                        continue
                    if block["id"] == "redstone_block":
                        activated = True
                        break
                    if neighbor in powered and neighbor in self.conductors:
                        activated = True
                        break
                if activated:
                    seeds.add(position)
            active_notes = set(seeds)
            queue = deque(seeds)
            while queue:
                cell = queue.popleft()
                for delta in NEIGHBORS:
                    neighbor = (cell[0] + delta[0], cell[1] + delta[1], cell[2] + delta[2])
                    if neighbor in self.notes and neighbor not in active_notes:
                        active_notes.add(neighbor)
                        queue.append(neighbor)

            for position, state in note_state.items():
                now = position in active_notes
                if now and not state:
                    info = self.notes[position]
                    events.append({
                        "game_tick": tick,
                        "pos": position,
                        "pitch": info["pitch"],
                        "instrument": info["instrument"],
                    })
                note_state[position] = now

            # 命令方块（矿车跟随）
            for position, text in self.commands.items():
                now = position in powered
                if now and not command_state[position]:
                    command_state[position] = True
                    if text.startswith("tp "):
                        target = self._parse_tp_target(position, text)
                        if target:
                            player = target
                            player_track.append({"game_tick": tick, "pos": target})
                else:
                    command_state[position] = now

            # 中继器输入采样
            for position, info in self.repeaters.items():
                out = info["out"]
                back = (position[0] - out[0], position[1] - out[1], position[2] - out[2])
                value = self._input_on(back, position, powered, level, active_notes, rep_out)
                if value != rep_out[position]:
                    previous = scheduled.get(position)
                    if previous is None or previous[1] != value:
                        when = tick + info["delay"] * 2
                        scheduled[position] = (when, value)
                        pending[when].append((position, value))

        return {
            "events": events,
            "player_track": player_track,
            "player": player,
            "initial_player": start_player,
        }

    def _input_on(self, back: Coord, position: Coord, powered: set,
                  level: Dict[Coord, int], active_notes: set,
                  rep_out: Dict[Coord, bool]) -> bool:
        block = self.world.get(back)
        if block is None:
            return False
        block_id = block["id"]
        if block_id == "redstone_block":
            return True
        if block_id == "redstone_wire":
            return level.get(back, 0) > 0
        if back in active_notes:
            return True
        # 只有「被强充能的导体方块」才会给中继器供能；
        # 中继器自身被充能（等待输出）时不会向后供能
        if back in powered and back in self.conductors:
            return True
        # 中继器直连：后方中继器若已激活且输出正对本中继器
        if block_id == "repeater" and rep_out.get(back):
            out = self.repeaters[back]["out"]
            if (back[0] + out[0], back[1] + out[1], back[2] + out[2]) == position:
                return True
        return False

    def _parse_tp_target(self, position: Coord, text: str) -> Optional[Tuple[float, float, float]]:
        parts = text.split()
        if len(parts) < 2:
            return None
        coords = parts[-3:]
        if not all(part.startswith("~") for part in coords):
            return None
        result = []
        for axis, part in zip(position, coords):
            try:
                offset = float(part[1:]) if len(part) > 1 else 0.0
            except ValueError:
                offset = 0.0
            result.append(axis + offset)
        return tuple(result)


# ── 验收比对 ────────────────────────────────────────────────

def _center(position: Coord) -> Tuple[float, float, float]:
    return (position[0] + 0.5, position[1] + 0.5, position[2] + 0.5)


def compare(plan: dict, simulation: dict, sound_range: float = 48.0,
            tick_offset: Optional[int] = None) -> dict:
    """把模拟结果换算成歌曲 tick 并和 plan 里的音符逐 tick 比对。"""
    player = simulation.get("player")
    track = simulation.get("player_track") or []
    track.sort(key=lambda item: item["game_tick"])

    if tick_offset is None:
        first_events = simulation.get("events") or []
        first_ticks = [entry["tick"] for entry in plan.get("notes", [])]
        if first_events and first_ticks:
            tick_offset = first_events[0]["game_tick"] - 2 * min(first_ticks)
        else:
            tick_offset = 0

    def player_at(game_tick: int):
        current = None
        for entry in track:
            if entry["game_tick"] <= game_tick:
                current = entry["pos"]
            else:
                break
        if current is not None:
            return current
        return simulation.get("initial_player") or player

    events = []
    for event in simulation["events"]:
        song_tick = (event["game_tick"] - tick_offset) // 2
        center = _center(event["pos"])
        listener = player_at(event["game_tick"])
        distance = None
        volume = None
        side = 0
        if listener is not None:
            # 听者在耳朵高度，而不是脚底
            distance = distance_3d(center[0], center[1], center[2],
                                   listener[0], listener[1] + LISTENER_EYE, listener[2])
            volume = volume_from_distance(distance, sound_range)
            side = 1 if center[2] > listener[2] else (-1 if center[2] < listener[2] else 0)
        events.append({
            **event,
            "song_tick": song_tick,
            "distance": distance,
            "volume": volume,
            "side": side,
        })

    planned = defaultdict(list)
    for entry in plan.get("notes", []):
        planned[entry["tick"]].append({
            "pitch": note_to_pitch(entry["note"].key if hasattr(entry["note"], "key")
                                   else entry["note"].pitch),
            "instrument": entry["note"].instrument,
            "side": entry.get("side", 0),
            "volume_plan": entry.get("volume_plan"),
            "volume_nbs": entry.get("volume_nbs"),
        })

    simulated = defaultdict(list)
    for event in events:
        simulated[event["song_tick"]].append(event)

    missing = []
    extra = []
    volume_errors = []
    max_tick = max(list(planned.keys()) + list(simulated.keys()), default=0)
    for tick in range(0, max_tick + 1):
        expected = sorted((item["instrument"], item["pitch"]) for item in planned.get(tick, []))
        actual = sorted((item["instrument"], item["pitch"]) for item in simulated.get(tick, []))
        if expected != actual:
            if len(actual) < len(expected):
                missing.append(tick)
            elif len(actual) > len(expected):
                extra.append(tick)
            else:
                missing.append(tick)
        # 音量误差（按 (音色, 音高) 配对）
        pool = list(planned.get(tick, []))
        for event in simulated.get(tick, []):
            for candidate in pool:
                if (candidate["instrument"], candidate["pitch"]) == (event["instrument"], event["pitch"]):
                    if event["volume"] is not None and candidate.get("volume_plan") is not None:
                        volume_errors.append(abs(event["volume"] - candidate["volume_plan"]))
                    pool.remove(candidate)
                    break

    # 实际"听到"的音量：同一 tick、同音高、同音色的左右音符盒会叠加
    heard_sim: Dict[Tuple[int, int, int], float] = defaultdict(float)
    for event in events:
        if event["volume"] is not None:
            heard_sim[(event["song_tick"], event["instrument"], event["pitch"])] += event["volume"]
    heard_plan: Dict[Tuple[int, int, int], float] = defaultdict(float)
    for tick, items in planned.items():
        for item in items:
            if item.get("side", 0) < 0:
                continue          # 左右成对的音符只计一次，避免重复累加
            heard_plan[(tick, item["instrument"], item["pitch"])] += item.get("volume_nbs") or 0.0
    heard_errors = [abs(heard_sim.get(key, 0.0) - value) for key, value in heard_plan.items()]

    matched = sum(1 for tick in range(0, max_tick + 1)
                  if sorted((i["instrument"], i["pitch"]) for i in planned.get(tick, []))
                  == sorted((i["instrument"], i["pitch"]) for i in simulated.get(tick, [])))
    stats = {
        "ticks": max_tick + 1,
        "matched_ticks": matched,
        "missing_ticks": missing,
        "extra_ticks": extra,
        "note_events": len(events),
        "planned_notes": sum(len(v) for v in planned.values()),
        "volume_mean_error": (sum(volume_errors) / len(volume_errors)) if volume_errors else 0.0,
        "volume_max_error": max(volume_errors) if volume_errors else 0.0,
        "heard_mean_error": (sum(heard_errors) / len(heard_errors)) if heard_errors else 0.0,
        "heard_max_error": max(heard_errors) if heard_errors else 0.0,
        "left_notes": sum(1 for event in events if event["side"] < 0),
        "right_notes": sum(1 for event in events if event["side"] > 0),
    }
    stats["passed"] = not missing and not extra
    return {"events": events, "planned": planned, "simulated": simulated, "stats": stats}


def format_report(result: dict) -> str:
    stats = result["stats"]
    lines = [
        f"  歌曲 tick 数      : {stats['ticks']}",
        f"  逐 tick 完全一致  : {stats['matched_ticks']}/{stats['ticks']}",
        f"  模拟音符事件      : {stats['note_events']}（计划 {stats['planned_notes']}）",
        f"  左右声道音符      : 左 {stats['left_notes']} / 右 {stats['right_notes']}",
        f"  音量误差(单盒)    : 平均 {stats['volume_mean_error']:.4f}，最大 {stats['volume_max_error']:.4f}",
        f"  音量误差(左右叠加): 平均 {stats['heard_mean_error']:.4f}，最大 {stats['heard_max_error']:.4f}",
    ]
    if stats["missing_ticks"]:
        lines.append(f"  有缺失的 tick     : {stats['missing_ticks'][:10]}")
    if stats["extra_ticks"]:
        lines.append(f"  有多余的 tick     : {stats['extra_ticks'][:10]}")
    lines.append(f"  结论              : {'通过' if stats['passed'] else '未通过'}")
    return "\n".join(lines)
