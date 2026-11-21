"""红石命令生成与 .schem 结构保存。"""

import json
import os
import re
from collections import defaultdict
from typing import Dict, List, Tuple

import mcschematic

from .constants import (
    DIR_OFFSET,
    INSTRUMENT_BLOCK_MAP,
    OPPOSITE_FACING,
    SIDE_AXIS,
    Ansi,
)

COMMAND_MAX_LEN = 32500  # Minecraft 命令方块单条命令文本长度上限（达到即无法执行）
_COMMAND_TEXT_RE = re.compile(r'\{Command:("(?:[^"\\]|\\.)*")')


def warn_oversized_commands(commands) -> int:
    """生成前检查所有命令方块：命令文本长度 ≥ COMMAND_MAX_LEN 时打印警告。

    返回超限的命令方块数量。
    """
    setblock_pattern = re.compile(r'^setblock\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+)$')
    oversized = []
    for cmd in commands:
        cmd = cmd.strip()
        if not cmd or cmd.startswith('#'):
            continue
        m = setblock_pattern.match(cmd)
        if not m:
            continue
        cm = _COMMAND_TEXT_RE.search(m.group(4))
        if not cm:
            continue
        try:
            cmd_text = json.loads(cm.group(1))
        except (ValueError, TypeError):
            cmd_text = cm.group(1)
        if len(cmd_text) >= COMMAND_MAX_LEN:
            coord = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            oversized.append((coord, len(cmd_text)))

    if oversized:
        print(Ansi.error(f"警告：{len(oversized)} 个命令方块的命令文本长度达到/超过 "
                         f"{COMMAND_MAX_LEN} 字符，游戏可能无法执行："))
        for coord, length in oversized:
            print(Ansi.error(f"  命令方块 @ {coord}  长度 {length} 字符"))
    return len(oversized)


def note_to_pitch(key: int) -> int:
    return (key - 33) % 25

def get_note_key(note) -> int:
    return note.key if hasattr(note, 'key') else note.pitch

def split_to_repeaters(total_ticks: int) -> List[int]:
    if total_ticks <= 0:
        return []
    result = []
    remaining = total_ticks
    while remaining > 0:
        d = min(remaining, 4)
        result.append(d)
        remaining -= d
    return result


def find_structure_bottom(commands) -> int:
    """返回命令集合中出现的最低 Y 坐标，用于把歌词链放到整个结构下方。

    解析 setblock / fill 命令的 y 值；没有任何方块命令时返回 0。
    """
    min_y = None
    for cmd in commands:
        cmd = cmd.strip()
        if cmd.startswith("setblock "):
            parts = cmd.split()
            try:
                y = int(parts[2])
            except (IndexError, ValueError):
                continue
        elif cmd.startswith("fill "):
            parts = cmd.split()
            try:
                y = min(int(parts[2]), int(parts[5]))
            except (IndexError, ValueError):
                continue
        else:
            continue
        if min_y is None or y < min_y:
            min_y = y
    return 0 if min_y is None else min_y


def chain_valid_ticks(virtual_layers: List[Dict], max_tick: int) -> List[int]:
    """主音乐链实际使用的 tick 列表（与 :func:`build_all_commands_virtual` 一致）。"""
    ticks = sorted({note.tick for layer in virtual_layers
                    for note in layer.get("notes", []) if 0 <= note.tick <= max_tick})
    if max_tick not in ticks:
        # 所有音符 tick 都 ≤ max_tick，直接追加即可保持有序
        ticks.append(max_tick)
    return ticks


def event_offsets(valid_ticks: List[int],
                  uniform_repeater_mode: bool = False) -> List[Tuple[int, int]]:
    """每个事件格相对链起点的链轴格偏移：``[(tick, 格数), ...]``。

    必须与 :func:`build_all_commands_virtual` 的布局保持一致：
    - 每个事件格占 1 格，其后紧跟一个 delay=1 中继器（1 格）；
    - 空白 tick 用 [羊毛 + 中继器] 对填充：
      均匀模式下每个空白 tick 一对（2 格），
      合并模式下最多 4 个空白 tick 并进一个 delay=4 中继器（仍占 2 格）。
    因此「合并模式」的链条明显更短，调用方不能再假设「每 tick 2 格」。
    """
    if not valid_ticks:
        return []
    result: List[Tuple[int, int]] = [(valid_ticks[0], 0)]
    cursor = 0
    previous = valid_ticks[0]
    for tick in valid_ticks[1:]:
        blank = max(0, tick - previous - 1)
        pairs = blank if uniform_repeater_mode else -(-blank // 4)
        cursor += 2 + 2 * pairs
        result.append((tick, cursor))
        previous = tick
    return result


# ── .schem 结构保存 ──────────────────────────────────────────

SETBLOCK_PATTERN = re.compile(r'^setblock\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+)')
FILL_PATTERN = re.compile(r'^fill\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+)')


def generate_schematic_from_commands(commands, output_path):
    # 生成前统一检查命令方块文本长度，超限时给出警告
    warn_oversized_commands(commands)
    schem = mcschematic.MCSchematic()
    total_blocks = 0
    min_x = min_y = min_z = float('inf')
    max_x = max_y = max_z = float('-inf')

    def update_bounds(x, y, z):
        nonlocal min_x, min_y, min_z, max_x, max_y, max_z
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        min_z = min(min_z, z)
        max_x = max(max_x, x)
        max_y = max(max_y, y)
        max_z = max(max_z, z)

    def set_block(x, y, z, block_str):
        schem.setBlock((x, y, z), block_str)
        update_bounds(x, y, z)

    for cmd in commands:
        cmd = cmd.strip()
        if cmd.startswith('#') or not cmd:
            continue

        m = SETBLOCK_PATTERN.match(cmd)
        if m:
            x, y, z = int(m.group(1)), int(m.group(2)), int(m.group(3))
            block = m.group(4)
            set_block(x, y, z, block)
            total_blocks += 1
            continue

        m = FILL_PATTERN.match(cmd)
        if m:
            x1, y1, z1 = int(m.group(1)), int(m.group(2)), int(m.group(3))
            x2, y2, z2 = int(m.group(4)), int(m.group(5)), int(m.group(6))
            block = m.group(7)
            x_start, x_end = sorted([x1, x2])
            y_start, y_end = sorted([y1, y2])
            z_start, z_end = sorted([z1, z2])
            for x in range(x_start, x_end + 1):
                for y in range(y_start, y_end + 1):
                    for z in range(z_start, z_end + 1):
                        set_block(x, y, z, block)
                        total_blocks += 1
            continue

    if total_blocks == 0:
        print(Ansi.error("没有找到任何方块放置命令，无法生成 schematic"))
        return False

    offset_x, offset_y, offset_z = min_x, min_y, min_z
    print(Ansi.info(f"结构文件包含 {total_blocks} 个方块，范围: X{min_x}~{max_x}, Y{min_y}~{max_y}, Z{min_z}~{max_z}"))
    print(Ansi.dim(f"放置时建议使用 //schem load 后，以世界坐标 ({offset_x},{offset_y},{offset_z}) 为原点粘贴"))

    dir_name = os.path.dirname(output_path)
    base_name = os.path.basename(output_path)
    if base_name.endswith('.schem'):
        base_name = base_name[:-6]
    if not dir_name:
        dir_name = "."
    try:
        os.makedirs(dir_name, exist_ok=True)
        schem.save(dir_name, base_name, mcschematic.Version.JE_1_21)
        print(Ansi.success(f"结构文件已保存: {os.path.join(dir_name, base_name + '.schem')}"))
        return True
    except Exception as e:
        print(Ansi.error(f"保存 schematic 失败: {e}"))
        return False

def build_all_commands_virtual(
    virtual_layers: List[Dict],
    layer_to_group: Dict[int, int],
    group_offsets: List[Tuple[int, int]],
    base: Tuple[int, int, int],
    direction: str,
    max_tick: int,
    use_lamp: bool = False,
    uniform_repeater_mode: bool = False,   # 原 repeater_gap_fill
    group_staircase_modes: Dict[str, str] = None,  # 每组生成模式
    note_events: List[Tuple[int, int, int, int, int, int]] = None,  # 收集 (gid, vid, tick, x, y, z)
    support_block: str = "minecraft:white_wool",
    empty_block: str = "minecraft:oak_planks",
) -> Tuple[List[str], Dict[int, Tuple[int, int, int]]]:
    dx, dy, dz = DIR_OFFSET[direction]
    facing = OPPOSITE_FACING[direction]
    side_axis = SIDE_AXIS[direction]
    groups = sorted(set(layer_to_group.values()))
    if group_staircase_modes is None:
        group_staircase_modes = {}
    base_block = "minecraft:redstone_lamp" if use_lamp else support_block

    # 收集每个组在每个tick的音符（同时记录音符所属的虚拟层 vid，供粒子特效定位）
    if note_events is None:
        note_events = []
    group_tick_notes = defaultdict(lambda: defaultdict(list))
    for vlayer in virtual_layers:
        gid = layer_to_group.get(vlayer['vid'])
        if gid is None:
            # 未参与分组的音轨直接跳过，避免 KeyError 中断生成
            continue
        for note in vlayer['notes']:
            if 0 <= note.tick <= max_tick:
                group_tick_notes[gid][note.tick].append((note, vlayer['vid']))

    valid_ticks = chain_valid_ticks(virtual_layers, max_tick)

    base_x, base_y, base_z = base
    activation_positions = {}
    group_pos = {}

    # 初始化各组参数
    for i, gid in enumerate(groups):
        y_off, side_off = group_offsets[i]
        if side_axis == 'z':
            x0 = base_x
            z0 = base_z + side_off
        else:
            x0 = base_x + side_off
            z0 = base_z
        y0 = base_y + y_off

        relay_x = x0 - dx
        relay_y = y0
        relay_z = z0 - dz

        input_x = relay_x - dx
        input_y = relay_y
        input_z = relay_z - dz
        activation_positions[gid] = (input_x, input_y, input_z)

        group_pos[gid] = (relay_x, relay_y, relay_z)

    all_cmds = ["# 红石音乐生成器 (中继器链 + 辅助方块)"]
    dropped_notes = 0

    def add_block(x, y, z, block):
        all_cmds.append(f"setblock {x} {y} {z} {block}")

    def add_relay(x, y, z, delay_val):
        """中继器 + 正下方一块支撑方块。"""
        add_block(x, y - 1, z, support_block)
        add_block(x, y, z, f"minecraft:repeater[facing={facing},delay={delay_val}]")

    def add_empty(x, y, z):
        """空 tick 占位方块 + 正下方支撑。"""
        add_block(x, y - 1, z, support_block)
        add_block(x, y, z, empty_block)

    def add_note(x, y, z, note, vid, tick):
        """音符盒 + 正确底座（乐器方块或支撑方块）。"""
        pitch = note_to_pitch(get_note_key(note))
        inst = note.instrument
        add_block(x, y, z, f"minecraft:note_block[note={pitch}]")
        note_events.append((gid, vid, tick, x, y, z))
        if inst == 0:
            add_block(x, y - 1, z, support_block)
        else:
            add_block(x, y - 1, z, INSTRUMENT_BLOCK_MAP.get(inst, 'minecraft:stone'))
            if inst == 3:
                add_block(x, y - 2, z, support_block)

    # 放置起始中继器
    for gid in groups:
        x, y, z = group_pos[gid]
        add_relay(x, y, z, 1)

    prev_valid_tick = None
    for valid_tick in valid_ticks:
        gap = 0 if prev_valid_tick is None else valid_tick - prev_valid_tick

        if gap > 0:
            if prev_valid_tick is None:
                blank_ticks = 0
            else:
                blank_ticks = valid_tick - prev_valid_tick - 1
            
            if blank_ticks < 0:
                blank_ticks = 0   # 安全保护
            
            if uniform_repeater_mode:
                delays = [1] * blank_ticks   # 每个空白 tick 一个 delay=1 中继器
            else:
                delays = split_to_repeaters(blank_ticks)

            for gid in groups:
                x, y, z = group_pos[gid]
                for idx, delay_val in enumerate(delays):
                    # 放置辅助方块（支撑方块），将被当前中继器充能
                    connector_x = x + dx
                    connector_z = z + dz if side_axis == 'z' else z
                    add_block(connector_x, y - 1, connector_z, support_block)
                    add_block(connector_x, y, connector_z, support_block)

                    # 在辅助方块前方放置下一个中继器
                    next_repeater_x = connector_x + dx
                    next_repeater_z = connector_z + dz if side_axis == 'z' else connector_z
                    add_relay(next_repeater_x, y, next_repeater_z, delay_val)
                    # 更新位置
                    x, y, z = next_repeater_x, y, next_repeater_z
                group_pos[gid] = (x, y, z)

        # ---------- 当前 tick 的音符盒放置 ----------
        for gid in groups:
            x, y, z = group_pos[gid]
            note_x = x + dx
            note_z = z + dz if side_axis == 'z' else z

            notes = group_tick_notes[gid].get(valid_tick, [])
            note_count = len(notes)
            is_stair = group_staircase_modes.get(str(gid), "default") == "staircase"

            if note_count == 0:
                add_empty(note_x, y, note_z)
            elif note_count == 1:
                note, vid = notes[0]
                add_note(note_x, y, note_z, note, vid, valid_tick)
            else:
                if note_count > 3:
                    # 一个事件格最多摆 3 个音符盒，多余的会被丢弃（生成结束后统一提示）
                    dropped_notes += note_count - 3
                sorted_pairs = sorted(notes, key=lambda nv: nv[0].layer)
                side_step = 1
                side_y = y - 1 if is_stair else y  # 阶梯模式：侧边音符下降一格
                if side_axis == 'z':
                    left_pos = (note_x, side_y, note_z - side_step)
                    right_pos = (note_x, side_y, note_z + side_step)
                else:
                    left_pos = (note_x - side_step, side_y, note_z)
                    right_pos = (note_x + side_step, side_y, note_z)

                if note_count == 2:
                    # 中间放 base_block 作为支撑/连接，两侧各一个音符盒
                    add_block(note_x, y - 1, note_z, support_block)
                    add_block(note_x, y, note_z, base_block)
                    pairs = list(zip([left_pos, right_pos], sorted_pairs))
                else:
                    mid_note, mid_vid = sorted_pairs[1]
                    add_note(note_x, y, note_z, mid_note, mid_vid, valid_tick)
                    pairs = list(zip([left_pos, right_pos],
                                     [sorted_pairs[0], sorted_pairs[-1]]))

                for pos, (note, vid) in pairs:
                    add_note(pos[0], pos[1], pos[2], note, vid, valid_tick)

            # 后续中继器（延续信号）
            next_relay_x = note_x + dx
            next_relay_z = note_z + dz if side_axis == 'z' else note_z
            add_relay(next_relay_x, y, next_relay_z, 1)
            group_pos[gid] = (next_relay_x, y, next_relay_z)

        prev_valid_tick = valid_tick

    if dropped_notes:
        print(Ansi.prompt(f"提示：有 {dropped_notes} 个音符与同一 tick 的其它音符挤在一格，"
                          f"已跳过（一个事件格最多放 3 个音符盒）。"
                          f"可以改用「垂直压缩」或把音轨拆到不同分组。"))
    return all_cmds, activation_positions

# ── 歌词轨道特殊处理 ─────────────────────────────────────────

