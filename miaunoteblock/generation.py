"""红石命令生成与 .schem 结构保存。"""

import json
import os
import re
from collections import defaultdict
from typing import Dict, List, Tuple

import mcschematic

from .constants import (
    DIR_OFFSET,
    FALLING_BLOCK_SUPPORT,
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


# ── 乐器识别 ─────────────────────────────────────────────────

def generate_fill_commands(
    groups: List[int],
    group_offsets: List[Tuple[int, int]],
    base: Tuple[int, int, int],
    direction: str,
    max_tick: int,
    side_axis: str,
    use_lamp: bool = False
) -> List[str]:
    dx, dy, dz = DIR_OFFSET[direction]
    base_x, base_y, base_z = base
    fill_cmds = []
    for i, gid in enumerate(groups):
        y_off, side_off = group_offsets[i]
        if side_axis == 'z':
            line_z = base_z + side_off
            line_x0 = base_x
        else:
            line_z = base_z
            line_x0 = base_x + side_off

        start_x = line_x0
        end_x = start_x + dx * (max_tick + 2)
        if dx > 0:
            min_x, max_x = min(start_x, end_x), max(start_x, end_x)
        else:
            min_x, max_x = min(start_x, end_x), max(start_x, end_x)

        cur = min_x
        while cur <= max_x:
            seg_end = min(cur + abs(dx) * 31, max_x)
            x1, x2 = (cur, seg_end) if dx > 0 else (seg_end, cur)
            fill_cmds.append(
                f"fill {x1} {base_y + y_off} {line_z} {x2} {base_y + y_off} {line_z} minecraft:white_wool"
            )
            if use_lamp:
                fill_cmds.append(
                    f"fill {x1} {base_y + y_off} {line_z} {x2} {base_y + y_off} {line_z} minecraft:redstone_lamp"
                )
            cur = seg_end + abs(dx)
    return fill_cmds

def generate_schematic_from_commands(commands, output_path, base_name="redstone_music"):
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

    import re
    setblock_pattern = re.compile(r'^setblock\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+)')
    fill_pattern = re.compile(r'^fill\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+)')
    
    for cmd in commands:
        cmd = cmd.strip()
        if cmd.startswith('#') or not cmd:
            continue

        m = setblock_pattern.match(cmd)
        if m:
            x, y, z = int(m.group(1)), int(m.group(2)), int(m.group(3))
            block = m.group(4)
            set_block(x, y, z, block)
            total_blocks += 1
            continue

        m = fill_pattern.match(cmd)
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

    import os
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
    note_events: List[Tuple[int, int, int, int, int, int]] = None  # 收集 (gid, vid, tick, x, y, z)
) -> Tuple[List[str], Dict[int, Tuple[int, int, int]]]:
    dx, dy, dz = DIR_OFFSET[direction]
    facing = OPPOSITE_FACING[direction]
    side_axis = SIDE_AXIS[direction]
    groups = sorted(set(layer_to_group.values()))
    if group_staircase_modes is None:
        group_staircase_modes = {}
    base_block = "minecraft:redstone_lamp" if use_lamp else "minecraft:white_wool"

    if direction in ("east", "west"):
        axis_step = dx
        axis_key = 'x'
    else:
        axis_step = dz
        axis_key = 'z'

    # 收集每个组在每个tick的音符（同时记录音符所属的虚拟层 vid，供粒子特效定位）
    if note_events is None:
        note_events = []
    group_tick_notes = defaultdict(lambda: defaultdict(list))
    for vlayer in virtual_layers:
        gid = layer_to_group[vlayer['vid']]
        for note in vlayer['notes']:
            if 0 <= note.tick <= max_tick:
                group_tick_notes[gid][note.tick].append((note, vlayer['vid']))

    valid_ticks = set()
    for gid in groups:
        valid_ticks.update(group_tick_notes[gid].keys())
    valid_ticks = sorted(valid_ticks)
    if max_tick not in valid_ticks:
        valid_ticks.append(max_tick)

    base_x, base_y, base_z = base
    activation_positions = {}
    group_pos = {}
    fill_cursor = {}

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
        if side_axis == 'z':
            relay_z = z0 - dz
        else:
            relay_z = z0 - dz if direction in ("south", "north") else z0

        input_x = relay_x - dx
        input_y = relay_y
        input_z = relay_z - dz if direction in ("south", "north") else relay_z
        activation_positions[gid] = (input_x, input_y, input_z)

        group_pos[gid] = (relay_x, relay_y, relay_z)

        if axis_key == 'x':
            axis_origin = relay_x
        else:
            axis_origin = relay_z
        fill_cursor[gid] = axis_origin - 2 * axis_step

    all_cmds = ["# 红石音乐生成器 (中继器链 + 辅助方块)"]
    SEGMENT = 32

    def get_fixed_and_y(gid):
        i = groups.index(gid)
        y_off, side_off = group_offsets[i]
        y0 = base_y + y_off
        if axis_key == 'x':
            fixed = base_z + (side_off if side_axis == 'z' else 0)
        else:
            fixed = base_x + (side_off if side_axis == 'x' else 0)
        return fixed, y0

    def ensure_support(gid, target_coord):
        nonlocal fill_cursor
        cursor = fill_cursor[gid]
        if axis_step > 0:
            need = target_coord > cursor
        else:
            need = target_coord < cursor
        if not need:
            return
        cur = cursor + axis_step
        if axis_step > 0:
            seg_end = cur + SEGMENT - 1
        else:
            seg_end = cur - (SEGMENT - 1)
        fixed, y0 = get_fixed_and_y(gid)
        if axis_key == 'x':
            x1, x2 = (cur, seg_end) if cur <= seg_end else (seg_end, cur)
            all_cmds.append(f"fill {x1} {y0-1} {fixed} {x2} {y0} {fixed} {base_block}")
        else:
            z1, z2 = (cur, seg_end) if cur <= seg_end else (seg_end, cur)
            all_cmds.append(f"fill {fixed} {y0-1} {z1} {fixed} {y0} {z2} {base_block}")
        fill_cursor[gid] = seg_end

    # 放置起始中继器
    for gid in groups:
        x, y, z = group_pos[gid]
        target_coord = x if axis_key == 'x' else z
        ensure_support(gid, target_coord)
        all_cmds.append(f"setblock {x} {y} {z} minecraft:repeater[facing={facing},delay=1]")

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
                    # 放置辅助方块（白色羊毛），将被当前中继器充能
                    connector_x = x + dx
                    connector_z = z + dz if side_axis == 'z' else z
                    ensure_support(gid, connector_x if axis_key == 'x' else connector_z)
                    all_cmds.append(f"setblock {connector_x} {y} {connector_z} minecraft:white_wool")

                    # 在辅助方块前方放置下一个中继器
                    next_repeater_x = connector_x + dx
                    next_repeater_z = connector_z + dz if side_axis == 'z' else connector_z
                    ensure_support(gid, next_repeater_x if axis_key == 'x' else next_repeater_z)
                    all_cmds.append(
                        f"setblock {next_repeater_x} {y} {next_repeater_z} "
                        f"minecraft:repeater[facing={facing},delay={delay_val}]"
                    )
                    # 更新位置
                    x, y, z = next_repeater_x, y, next_repeater_z
                group_pos[gid] = (x, y, z)

        # ---------- 当前 tick 的音符盒放置 ----------
        for gid in groups:
            x, y, z = group_pos[gid]
            note_x = x + dx
            note_z = z + dz if side_axis == 'z' else z
            target_coord = note_x if axis_key == 'x' else note_z
            ensure_support(gid, target_coord)

            notes = group_tick_notes[gid].get(valid_tick, [])
            note_count = len(notes)
            is_stair = group_staircase_modes.get(str(gid), "default") == "staircase"

            if note_count == 0:
                # 空 tick：放置 base_block（中继器直接充能该方块）
                if axis_key == 'x':
                    all_cmds.append(f"setblock {note_x} {y} {note_z} {base_block}")
                else:
                    all_cmds.append(f"setblock {note_x} {y} {note_z} {base_block}")
                # 然后放置后续中继器（延续信号）
                next_relay_x = note_x + dx
                next_relay_z = note_z + dz if side_axis == 'z' else note_z
                ensure_support(gid, next_relay_x if axis_key == 'x' else next_relay_z)
                all_cmds.append(f"setblock {next_relay_x} {y} {next_relay_z} minecraft:repeater[facing={facing},delay=1]")
                group_pos[gid] = (next_relay_x, y, next_relay_z)
            elif note_count == 1:
                note, vid = notes[0]
                pitch = note_to_pitch(get_note_key(note))
                inst = note.instrument
                if axis_key == 'x':
                    all_cmds.append(f"setblock {note_x} {y} {note_z} minecraft:note_block[note={pitch}]")
                else:
                    all_cmds.append(f"setblock {note_x} {y} {note_z} minecraft:note_block[note={pitch}]")
                note_events.append((gid, vid, valid_tick, note_x, y, note_z))
                if inst != 0:
                    if axis_key == 'x':
                        all_cmds.append(f"setblock {note_x} {y-1} {note_z} {INSTRUMENT_BLOCK_MAP.get(inst, 'minecraft:stone')}")
                        if inst == 3:
                            all_cmds.append(f"setblock {note_x} {y-2} {note_z} {FALLING_BLOCK_SUPPORT}")
                    else:
                        all_cmds.append(f"setblock {note_x} {y-1} {note_z} {INSTRUMENT_BLOCK_MAP.get(inst, 'minecraft:stone')}")
                        if inst == 3:
                            all_cmds.append(f"setblock {note_x} {y-2} {note_z} {FALLING_BLOCK_SUPPORT}")
                # 放置后续中继器
                next_relay_x = note_x + dx
                next_relay_z = note_z + dz if side_axis == 'z' else note_z
                ensure_support(gid, next_relay_x if axis_key == 'x' else next_relay_z)
                all_cmds.append(f"setblock {next_relay_x} {y} {next_relay_z} minecraft:repeater[facing={facing},delay=1]")
                group_pos[gid] = (next_relay_x, y, next_relay_z)
            elif note_count == 2:
                side_step = 1
                side_y = y - 1 if is_stair else y  # 阶梯模式：侧边音符下降一格
                if side_axis == 'z':
                    left_pos = (note_x, side_y, note_z - side_step)
                    right_pos = (note_x, side_y, note_z + side_step)
                else:
                    left_pos = (note_x - side_step, side_y, note_z)
                    right_pos = (note_x + side_step, side_y, note_z)
                sorted_notes = sorted(notes, key=lambda nv: nv[0].layer)
                if axis_key == 'x':
                    all_cmds.append(f"setblock {note_x} {y} {note_z} {base_block}")
                else:
                    all_cmds.append(f"setblock {note_x} {y} {note_z} {base_block}")
                for pos, (note, vid) in zip([left_pos, right_pos], sorted_notes):
                    px, py, pz = pos
                    pitch = note_to_pitch(get_note_key(note))
                    inst = note.instrument
                    all_cmds.append(f"setblock {px} {py} {pz} minecraft:note_block[note={pitch}]")
                    note_events.append((gid, vid, valid_tick, px, py, pz))
                    if inst != 0:
                        all_cmds.append(f"setblock {px} {py-1} {pz} {INSTRUMENT_BLOCK_MAP.get(inst, 'minecraft:stone')}")
                        if inst == 3:
                            all_cmds.append(f"setblock {px} {py-2} {pz} {FALLING_BLOCK_SUPPORT}")
                next_relay_x = note_x + dx
                next_relay_z = note_z + dz if side_axis == 'z' else note_z
                ensure_support(gid, next_relay_x if axis_key == 'x' else next_relay_z)
                all_cmds.append(f"setblock {next_relay_x} {y} {next_relay_z} minecraft:repeater[facing={facing},delay=1]")
                group_pos[gid] = (next_relay_x, y, next_relay_z)
            else:  # >=3
                sorted_pairs = sorted(notes, key=lambda nv: nv[0].layer)
                mid_pair = sorted_pairs[1] if len(sorted_pairs) > 1 else sorted_pairs[0]
                mid_note, mid_vid = mid_pair
                left_note, left_vid = sorted_pairs[0]
                right_note, right_vid = sorted_pairs[-1]
                side_step = 1
                side_y = y - 1 if is_stair else y  # 阶梯模式：侧边音符下降一格
                if side_axis == 'z':
                    left_pos = (note_x, side_y, note_z - side_step)
                    right_pos = (note_x, side_y, note_z + side_step)
                else:
                    left_pos = (note_x - side_step, side_y, note_z)
                    right_pos = (note_x + side_step, side_y, note_z)
                pitch_mid = note_to_pitch(get_note_key(mid_note))
                inst_mid = mid_note.instrument
                all_cmds.append(f"setblock {note_x} {y} {note_z} minecraft:note_block[note={pitch_mid}]")
                note_events.append((gid, mid_vid, valid_tick, note_x, y, note_z))
                if inst_mid != 0:
                    all_cmds.append(f"setblock {note_x} {y-1} {note_z} {INSTRUMENT_BLOCK_MAP.get(inst_mid, 'minecraft:stone')}")
                    if inst_mid == 3:
                        all_cmds.append(f"setblock {note_x} {y-2} {note_z} {FALLING_BLOCK_SUPPORT}")
                for pos, (note, vid) in zip([left_pos, right_pos], [(left_note, left_vid), (right_note, right_vid)]):
                    px, py, pz = pos
                    pitch = note_to_pitch(get_note_key(note))
                    inst = note.instrument
                    all_cmds.append(f"setblock {px} {py} {pz} minecraft:note_block[note={pitch}]")
                    note_events.append((gid, vid, valid_tick, px, py, pz))
                    if inst != 0:
                        all_cmds.append(f"setblock {px} {py-1} {pz} {INSTRUMENT_BLOCK_MAP.get(inst, 'minecraft:stone')}")
                        if inst == 3:
                            all_cmds.append(f"setblock {px} {py-2} {pz} {FALLING_BLOCK_SUPPORT}")
                next_relay_x = note_x + dx
                next_relay_z = note_z + dz if side_axis == 'z' else note_z
                ensure_support(gid, next_relay_x if axis_key == 'x' else next_relay_z)
                all_cmds.append(f"setblock {next_relay_x} {y} {next_relay_z} minecraft:repeater[facing={facing},delay=1]")
                group_pos[gid] = (next_relay_x, y, next_relay_z)

        prev_valid_tick = valid_tick

    return all_cmds, activation_positions

# ── 歌词轨道特殊处理 ─────────────────────────────────────────

