#!/usr/bin/env python3
"""MiauRedstoneMusic —— NBS 转红石音乐结构（.schem）。

文本控制台菜单：
- 音符 → 中继器链 + 音符盒（立体声 / 平铺 / 圆 / 方 / 半圆 / 嵌套）
- 歌词 → 一个脉冲命令方块里的 ``/mpe text chain``（MiauParticleEffects 粒子字幕）
- 粒子 → 每个参与组的 ``/mpe noteblock chain`` 弹力球

配置按**歌曲文件路径**保存到安装目录的 ``configs/``（一个路径一个 JSON），
再打开同一首歌时自动恢复。
"""

import json
import re
import struct
import sys
from collections import defaultdict
from typing import Dict, List, Tuple

import pynbs


def _patched_read_string(self):
    """pynbs 读取字符串：优先 UTF-8，失败时回退 cp1252（部分 NBS 用本地编码）。"""
    length_bytes = self.fileobj.read(4)
    if len(length_bytes) < 4:
        return ""  # 文件末尾，安全处理
    length = struct.unpack('<i', length_bytes)[0]
    raw = self.fileobj.read(length)
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('cp1252', errors='replace')


pynbs.Parser.read_string = _patched_read_string

from pynbs import read as nbs_read

from .config import (
    CONFIG_DIR_NAME,
    config_path_for,
    load_song_config,
    save_song_config,
)
from .console import ask_number, clear_screen, terminal_columns
from .constants import Ansi, DIR_OFFSET, VERSION
from .generation import (
    build_all_commands_virtual,
    find_structure_bottom,
    generate_schematic_from_commands,
)
from .instruments import (
    analyze_track_instrument,
    auto_group_by_mode_on_layers,
    rearrange_tracks_by_instrument,
    vertical_compress,
)
from .layout import calc_nest_layers, compute_min_radius, compute_offsets
from .lyrics import build_lyrics_chain, edit_lyrics_menu
from .particles import build_particle_commands, edit_particle_settings

LAYOUT_STYLES = {
    "1": ("flat", "平铺"),
    "2": ("circle", "圆形"),
    "3": ("square", "方形"),
    "4": ("semicircle", "半圆"),
    "5": ("semisquare", "半方形"),
    "6": ("nest_circle", "嵌套圆"),
    "7": ("nest_square", "嵌套方"),
}
STYLE_LABELS = {key: label for key, label in LAYOUT_STYLES.values()}

# 结构固定沿 +X 延展（不再提供方向切换）
STRUCTURE_DIRECTION = "east"

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def _visible_len(text: str) -> int:
    return len(ANSI_ESCAPE.sub('', text))


# ── 音轨展示 ─────────────────────────────────────────────────

def _layer_label(song, virtual_layer: dict) -> Tuple[str, str]:
    """返回 (显示名, 音色分类)；立体声副本会标注 (左)/(右)/(立体声)。"""
    original_id = virtual_layer['original_id']
    layer_obj = next((layer for layer in song.layers if layer.id == original_id), None)
    name = layer_obj.name if layer_obj and layer_obj.name else f"音轨{original_id}"
    side = virtual_layer.get('side')
    if side == 'stereo':
        name += " (立体声)"
    elif side == 'left':
        name += " (左)"
    elif side == 'right':
        name += " (右)"
    return name, virtual_layer.get('cat', '?')


def _group_lines(virtual_layers: List[dict], layer_to_group: Dict[int, int],
                 group_staircase_modes: Dict[str, str], song) -> List[str]:
    groups = defaultdict(list)
    for vid, gid in layer_to_group.items():
        groups[gid].append(vid)
    info_by_vid = {layer['vid']: layer for layer in virtual_layers}
    modes = group_staircase_modes or {}

    lines = []
    for gid in sorted(groups):
        parts = []
        for vid in sorted(groups[gid]):
            info = info_by_vid[vid]
            name, cat = _layer_label(song, info)
            text = Ansi.colorize(name, Ansi.WHITE)
            if cat != "钢琴":
                text += Ansi.colorize(f"({cat})", Ansi.BLUE)
            parts.append(text)
        header = Ansi.colorize(f"[组{gid + 1}]", Ansi.CYAN, bold=True)
        if modes.get(str(gid)) == "staircase":
            header += Ansi.colorize(" [阶梯↓]", Ansi.YELLOW)
        lines.append(header + " " + ", ".join(parts))
    return lines


def show_tracks_virtual(virtual_layers: List[dict], layer_to_group: Dict[int, int],
                        group_staircase_modes: Dict[str, str], song) -> None:
    lines = _group_lines(virtual_layers, layer_to_group, group_staircase_modes, song)
    if not lines:
        return
    print(Ansi.title("──────── 音轨分组 ────────"))
    width = terminal_columns()
    col_width = max(_visible_len(line) for line in lines) + 2
    columns = max(1, width // col_width)
    rows = (len(lines) + columns - 1) // columns
    for row in range(rows):
        cells = []
        for column in range(columns):
            index = row + column * rows
            if index < len(lines):
                line = lines[index]
                cells.append(line + ' ' * (col_width - _visible_len(line)))
        print(''.join(cells).rstrip())


# ── 分组编辑 ─────────────────────────────────────────────────

def edit_groups_interactive_virtual(virtual_layers: List[dict], song) -> Dict[int, int]:
    print(Ansi.title("\n=== 音轨分组编辑 ==="))
    for layer in virtual_layers:
        name, cat = _layer_label(song, layer)
        print(f"  {layer['vid']}: {Ansi.colorize(name, Ansi.WHITE)} ({cat})")
    print(Ansi.dim("输入 2~3 个虚拟层序号合并成一组（空格分隔），auto=自动分组，回车=结束"))

    pairs = []
    valid_vids = {layer['vid'] for layer in virtual_layers}
    while True:
        raw = input(Ansi.prompt("合并虚拟层 (回车结束): ")).strip()
        if not raw:
            break
        if raw.lower() == "auto":
            print("  自动分组模式：")
            print("    [1] 同乐器优先")
            print("    [2] 最大化合并")
            print("    [3] 顺序同音色合并（相邻同音色才合并，每组最多 3 条）")
            mode = input(Ansi.prompt("模式 (回车=1): ")).strip() or "1"
            layer_to_group = auto_group_by_mode_on_layers(
                virtual_layers, int(mode) if mode in ('1', '2', '3') else 1)
            print(Ansi.success(f"自动分组完成，共 {len(set(layer_to_group.values()))} 组"))
            return layer_to_group
        tokens = raw.split()
        if len(tokens) not in (2, 3):
            print(Ansi.error("请输入 2 或 3 个序号"))
            continue
        try:
            ids = [int(token) for token in tokens]
        except ValueError:
            print(Ansi.error("序号必须是数字"))
            continue
        if not all(vid in valid_vids for vid in ids):
            print(Ansi.error("序号不存在"))
            continue
        if len(set(ids)) != len(ids):
            print(Ansi.error("序号重复"))
            continue
        pairs.append(tuple(sorted(ids)))

    layer_to_group: Dict[int, int] = {}
    assigned = set()
    group_id = 0
    for ids in pairs:
        if any(vid in assigned for vid in ids):
            print(Ansi.error(f"重复分配，跳过: {ids}"))
            continue
        for vid in ids:
            layer_to_group[vid] = group_id
        assigned.update(ids)
        group_id += 1
    for layer in virtual_layers:
        if layer['vid'] not in assigned:
            layer_to_group[layer['vid']] = group_id
            group_id += 1
    print(Ansi.success(f"分组更新完毕，共 {group_id} 组"))
    return layer_to_group


# ── 排版 ─────────────────────────────────────────────────────

def edit_layout(num_groups: int, config: dict, layer_to_group: Dict[int, int],
                virtual_layers: List[dict], song) -> None:
    print(Ansi.title("\n=== 排版设置 ==="))
    current_style = config.get("layout_style", "flat")
    for key, (style, label) in LAYOUT_STYLES.items():
        mark = "*" if style == current_style else " "
        print(f"  {mark}[{key}] {label}")
    choice = input(Ansi.prompt(
        f"样式 (1-7，回车保持 {STYLE_LABELS.get(current_style, current_style)}): ")).strip()
    if not choice:
        style = current_style
    elif choice in LAYOUT_STYLES:
        style = LAYOUT_STYLES[choice][0]
        config["layout_style"] = style
    else:
        print(Ansi.error("无效样式，未修改"))
        return

    if style == "flat":
        config["flat_spacing"] = ask_number(
            "平铺间距", config.get("flat_spacing", 3), int, 1)
        config["master_group"] = None
        print(Ansi.success("排版已更新"))
        return

    groups = sorted(set(layer_to_group.values()))
    print(Ansi.info("当前分组："))
    for line in _group_lines(virtual_layers, layer_to_group,
                             config.get("group_staircase_modes", {}), song):
        print("  " + line)

    master = config.get("master_group")
    hint = (f"1-{len(groups)}，0=无核心组，回车保持"
            + (f"当前 {master + 1}" if master is not None else "当前无"))
    raw = input(Ansi.prompt(f"核心组编号 ({hint}): ")).strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            print(Ansi.error("请输入数字，核心组未修改"))
        else:
            if value == 0:
                config["master_group"] = None
            elif 1 <= value <= len(groups):
                config["master_group"] = groups[value - 1]
            else:
                print(Ansi.error("编号超出范围，核心组未修改"))

    has_master = config.get("master_group") is not None
    min_radius = compute_min_radius(num_groups, style, has_master)
    config["layout_radius"] = ask_number(
        f"半径/边长（最小 {min_radius}）", config.get("layout_radius", 5), int, min_radius)

    if style in ("nest_circle", "nest_square"):
        max_layers = calc_nest_layers(num_groups, config["layout_radius"],
                                      config.get("layer_gap", 4))
        config["nest_layers"] = ask_number(
            f"嵌套层数（最大 {max_layers}）", config.get("nest_layers", 1), int, 1, max_layers)
        config["layer_gap"] = ask_number("层间距", config.get("layer_gap", 4), int, 1)

    print(Ansi.success("排版已更新"))


# ── 立体声 ───────────────────────────────────────────────────

def edit_stereo_layers(song, config: dict) -> None:
    print(Ansi.title("\n=== 立体声音轨 ==="))
    print(Ansi.info("选中的音轨会在左右两条链上各放一次音符，并在排版中对称分布。"))
    notes_by_layer = defaultdict(list)
    for note in song.notes:
        notes_by_layer[note.layer].append(note)
    for layer_id in sorted(notes_by_layer):
        layer_obj = next((layer for layer in song.layers if layer.id == layer_id), None)
        name = layer_obj.name if layer_obj and layer_obj.name else f"音轨{layer_id}"
        _, cat = analyze_track_instrument(song, layer_id, notes_by_layer[layer_id])
        print(f"  {layer_id}: {Ansi.colorize(name, Ansi.WHITE)} ({cat})")
    print(Ansi.dim(f"当前立体声轨: {config.get('stereo_layers') or '无'}"))
    raw = input(Ansi.prompt("要设为立体声的音轨 ID（空格分隔，c=清空，回车=不改）: ")).strip()
    if not raw:
        return
    if raw.lower() in ("c", "clear"):
        config['stereo_layers'] = []
        print(Ansi.success("已清除立体声设置（所有音轨为单声道）"))
        return
    available = set(notes_by_layer)
    stereo = []
    for token in raw.replace(",", " ").split():
        try:
            layer_id = int(token)
        except ValueError:
            print(Ansi.error(f"忽略无效音轨 ID: {token}"))
            continue
        if layer_id in available:
            stereo.append(layer_id)
        else:
            print(Ansi.error(f"忽略不存在的音轨 ID: {layer_id}"))
    config['stereo_layers'] = sorted(set(stereo))
    if config['stereo_layers']:
        print(Ansi.success(f"已设置立体声音轨: {config['stereo_layers']}"))
    else:
        print(Ansi.info("已清除立体声设置（所有音轨为单声道）"))


# ── 分组生成模式 ─────────────────────────────────────────────

def edit_group_staircase_modes(config: dict, layer_to_group: Dict[int, int],
                               virtual_layers: List[dict], song) -> None:
    print(Ansi.title("\n=== 分组生成模式 ==="))
    print(Ansi.dim("默认 = 所有音符在同一水平面；阶梯↓ = 侧边音符向下降一格"))
    modes = config.get("group_staircase_modes", {})
    groups = sorted(set(layer_to_group.values()))
    for gid in groups:
        label = Ansi.colorize(f"[组{gid + 1}]", Ansi.CYAN, bold=True)
        state = "阶梯↓" if modes.get(str(gid)) == "staircase" else "默认"
        members = [_layer_label(song, layer)[0]
                   for layer in virtual_layers if layer_to_group.get(layer['vid']) == gid]
        print(f"  {label} {state}  ({', '.join(members)})")

    raw = input(Ansi.prompt("切换模式的组编号（空格分隔，回车=跳过）: ")).strip()
    if not raw:
        return
    for token in raw.split():
        try:
            gid = int(token) - 1
        except ValueError:
            print(Ansi.error(f"无效输入: {token}"))
            continue
        if gid not in groups:
            print(Ansi.error(f"组{gid + 1} 不存在，跳过"))
            continue
        current = modes.get(str(gid), "default")
        modes[str(gid)] = "default" if current == "staircase" else "staircase"
        state = "阶梯↓" if modes[str(gid)] == "staircase" else "默认"
        print(Ansi.success(f"组{gid + 1} 已切换为 {state}"))
    config["group_staircase_modes"] = modes


# ── 虚拟层构建 ───────────────────────────────────────────────

def build_virtual_layers(song, stereo_layers: List[int]) -> List[dict]:
    """把 NBS 音轨转成虚拟层；立体声音轨的每个音符复制一份。

    按音符实际出现的音轨来遍历，音轨表里缺失或多余的条目都不会丢音符。
    """
    stereo = set(stereo_layers)
    notes_by_layer = defaultdict(list)
    for note in song.notes:
        notes_by_layer[note.layer].append(note)

    virtual_layers = []
    for layer_id in sorted(notes_by_layer):
        notes = notes_by_layer[layer_id]
        if not notes:
            continue
        inst, cat = analyze_track_instrument(song, layer_id, notes)
        if layer_id in stereo:
            # 每个 tick 复制出一个相同音符，排版时会左右对称摆放
            stereo_notes = []
            for note in notes:
                stereo_notes.append(note)
                stereo_notes.append(note)
            stereo_notes.sort(key=lambda note: note.tick)
            virtual_layers.append({
                'vid': len(virtual_layers),
                'original_id': layer_id,
                'side': 'stereo',
                'notes': stereo_notes,
                'inst': inst,
                'cat': cat,
            })
        else:
            virtual_layers.append({
                'vid': len(virtual_layers),
                'original_id': layer_id,
                'side': None,
                'notes': notes,
                'inst': inst,
                'cat': cat,
            })
    return virtual_layers


# ── 配置总览 ─────────────────────────────────────────────────

def print_summary(config: dict, virtual_layers: List[dict], layer_to_group: Dict[int, int],
                  song) -> None:
    style = config.get("layout_style", "flat")
    style_text = STYLE_LABELS.get(style, style)
    if style == "flat":
        style_text += f" 间距={config.get('flat_spacing', 3)}"
    elif style in ("nest_circle", "nest_square"):
        style_text += (f" 半径={config.get('layout_radius')} 层数={config.get('nest_layers')}"
                       f" 间距={config.get('layer_gap')}")
    else:
        style_text += f" 半径={config.get('layout_radius')}"
    master = config.get("master_group")
    if style != "flat" and master is not None:
        style_text += f" 核心组={master + 1}"

    mode = "均匀" if config.get("uniform_repeater_mode") else "合并"
    song_name = getattr(song.header, "song_name", None) or "未知"
    print(Ansi.title("══════ 当前配置 ══════"))
    print(f"  曲目 {song_name} · 音符 {len(song.notes)} · 虚拟层 {len(virtual_layers)} · "
          f"分组 {len(set(layer_to_group.values()))}")
    print(f"  排版 {style_text} · 中继器{mode}模式 · 延展方向 +X")

    stereo = config.get('stereo_layers') or []
    stair_groups = [str(int(key) + 1) for key, value in
                    (config.get("group_staircase_modes") or {}).items() if value == "staircase"]
    print(f"  立体声 {stereo if stereo else '无'} · "
          f"阶梯组 {', '.join(stair_groups) if stair_groups else '无'}")

    lyrics_count = len(config.get("lyrics_import") or [])
    if lyrics_count:
        max_chars = int(config.get("lyrics_max_chars") or 0)
        char_limit = f" +{max_chars}字" if max_chars > 0 else ""
        print(f"  歌词 {lyrics_count} 条 · "
              f"断句阈值 {config.get('lyrics_char_threshold')} tick"
              f"{' +标点' if config.get('lyrics_break_on_punctuation') else ''}"
              f"{char_limit} · 提前 {config.get('lyrics_start_lead_ticks', 4)} tick · "
              f"颜色 {config.get('lyrics_color')} · 字号 {config.get('lyrics_scale')} · "
              f"保持距离 {config.get('lyrics_lead_distance')}")
    else:
        print(Ansi.dim("  歌词 未导入（[5] 导入即启用）"))

    selected = config.get("particles_layers") or []
    if config.get("particles_enabled") and selected:
        groups = sorted({layer_to_group.get(vid, vid) for vid in selected})
        print(f"  粒子 已启用 · {len(selected)} 条轨道 → {len(groups)} 颗球 · "
              f"trail={config.get('particles_trail')} height={config.get('particles_height')} "
              f"curve={config.get('particles_curve')}")
    elif config.get("particles_enabled"):
        print(Ansi.dim("  粒子 已启用但未选轨道（[6] 选择）"))
    else:
        print(Ansi.dim("  粒子 未启用（[6] 设置）"))


def print_menu(config: dict) -> None:
    print(Ansi.title("──────── 菜单 ────────"))
    print("  [1] 音轨分组")
    print("  [2] 排版")
    print("  [3] 立体声")
    print("  [4] 分组生成模式（默认 / 阶梯）")
    print("  [5] 歌词字幕")
    print("  [6] 粒子特效")
    print(f"  [7] 中继器模式：{'合并' if not config.get('uniform_repeater_mode') else '均匀'}（切换）")
    print("  [8] 退出")
    print(Ansi.dim("  回车 = 按当前配置生成结构"))


# ── 生成 ─────────────────────────────────────────────────────

def _rel(value: int) -> str:
    return "~" if value == 0 else f"~{value}"


def _launch_chain(targets: List[Tuple[object, Tuple[int, int, int]]],
                  start: Tuple[int, int, int], direction: str) -> List[str]:
    """生成启动链：两列朝上的命令方块，分别放置与清除每个启动红石块。

    targets 为 [(gid|None, 红石块坐标), ...]，顺序即启动顺序：
    第 i 项在第 i 环（t0+i+1）放置红石块，第 0 环是脉冲命令方块。
    """
    dx, _, dz = DIR_OFFSET[direction]
    start_x, start_y, start_z = start
    place_x, place_z = start_x - 5 * dx, start_z - 5 * dz
    clear_x, clear_z = start_x - 7 * dx, start_z - 7 * dz

    chain = []
    for index, (_, (ax, ay, az)) in enumerate(targets):
        cy = start_y + index
        place_cmd = (f"setblock {_rel(ax - place_x)} {_rel(ay - cy)} {_rel(az - place_z)} "
                     f"minecraft:redstone_block")
        clear_cmd = (f"setblock {_rel(ax - clear_x)} {_rel(ay - cy)} {_rel(az - clear_z)} "
                     f"minecraft:air")
        place_json = json.dumps(place_cmd)
        clear_json = json.dumps(clear_cmd)
        if index == 0:
            chain.append(f"setblock {place_x} {cy} {place_z} "
                         f"minecraft:command_block[facing=up]{{Command:{place_json}}}")
            chain.append(f"setblock {clear_x} {cy} {clear_z} "
                         f"minecraft:command_block[facing=up]{{Command:{clear_json}}}")
        else:
            chain.append(f"setblock {place_x} {cy} {place_z} "
                         f"minecraft:chain_command_block[facing=up,conditional=false]"
                         f"{{Command:{place_json},auto:1b}}")
            chain.append(f"setblock {clear_x} {cy} {clear_z} "
                         f"minecraft:chain_command_block[facing=up,conditional=false]"
                         f"{{Command:{clear_json},auto:1b}}")
    return chain


def build_commands(config: dict, song, virtual_layers: List[dict],
                   layer_to_group: Dict[int, int], max_tick: int) -> List[str]:
    """生成完整的方块命令列表：音乐链 + 启动链 + 歌词 + 粒子。"""
    groups = sorted(set(layer_to_group.values()))
    offsets = compute_offsets(groups, config, len(groups))
    group_offset_dict = {gid: offsets[index] for index, gid in enumerate(groups)}

    print(Ansi.info("正在预计算方块位置..."))
    note_events: List[Tuple[int, int, int, int, int, int]] = []
    direction = STRUCTURE_DIRECTION
    commands, activation_positions = build_all_commands_virtual(
        virtual_layers, layer_to_group, [group_offset_dict[gid] for gid in groups],
        (0, 0, 0), direction, max_tick,
        uniform_repeater_mode=config.get("uniform_repeater_mode", False),
        group_staircase_modes=config.get("group_staircase_modes", {}),
        note_events=note_events,
    )

    # 歌词：一个（或数个）脉冲命令方块，放在结构底部
    lyrics = None
    if config.get("lyrics_import"):
        lyrics = build_lyrics_chain(
            virtual_layers, group_offset_dict, (0, 0, 0), direction, max_tick, config,
            struct_bottom_y=find_structure_bottom(commands),
        )

    # 启动链：歌词命令方块的激活红石块排在前面（越早执行越好），随后每组一格
    targets: List[Tuple[object, Tuple[int, int, int]]] = []
    if lyrics:
        targets.extend((None, position) for position in lyrics["activations"])
    targets.extend(sorted(activation_positions.items(), key=lambda item: item[0]))
    launch = _launch_chain(targets, (0, 0, 0), direction)
    commands = commands[:1] + launch + commands[1:]

    if lyrics:
        commands.extend(lyrics["commands"])

    if config.get("particles_enabled") and (config.get("particles_layers") or []):
        commands.extend(build_particle_commands(
            note_events, activation_positions, direction, config))
    return commands


def generate(config: dict, song, virtual_layers: List[dict], layer_to_group: Dict[int, int]) -> None:
    max_in_file = max((note.tick for note in song.notes), default=0)
    raw = input(Ansi.prompt(f"生成长度（≤{max_in_file} tick，回车=全部）: ")).strip()
    if raw:
        try:
            max_tick = max(0, min(max_in_file, int(raw)))
        except ValueError:
            print(Ansi.error("输入无效，使用全部长度"))
            max_tick = max_in_file
    else:
        max_tick = max_in_file

    commands = build_commands(config, song, virtual_layers, layer_to_group, max_tick)

    schem_path = input(Ansi.prompt("保存路径 (默认 ./redstone_music.schem): ")).strip()
    if not schem_path:
        schem_path = "./redstone_music.schem"
    generate_schematic_from_commands(commands, schem_path)


# ── 主流程 ───────────────────────────────────────────────────

def _load_song(path: str):
    try:
        song = nbs_read(path)
    except Exception as exc:
        print(Ansi.error(f"读取 NBS 失败: {exc}"))
        return None
    if not song.notes:
        print(Ansi.error("NBS 文件里没有任何音符，无法生成红石音乐链"))
        return None
    return song


def _interactive() -> None:
    clear_screen()
    print(Ansi.title("══════ MiauRedstoneMusic · NBS → 红石音乐结构 ══════"))
    print(Ansi.dim(f"作者: COM1919 | 版本: {VERSION}"))

    raw_path = input(Ansi.prompt("NBS 文件路径 (默认 song.nbs): ")).strip().strip('"').strip("'")
    song_path = raw_path or "song.nbs"
    song = _load_song(song_path)
    if song is None:
        return

    # 同一首歌（同一路径）共用一个配置文件，换歌就换配置
    config = load_song_config(song_path)
    config_file = config_path_for(song_path)
    if config_file.exists():
        print(Ansi.success(f"已恢复这首歌的配置：{CONFIG_DIR_NAME}/{config_file.name}"))
    else:
        print(Ansi.info(f"这首歌还没有配置，修改设置后会保存到 "
                        f"{CONFIG_DIR_NAME}/{config_file.name}"))

    saved_snapshot = {"json": json.dumps(config, sort_keys=True, ensure_ascii=False)}

    def save_config() -> None:
        """配置有变化才写盘（按歌曲路径分文件）。"""
        snapshot = json.dumps(config, sort_keys=True, ensure_ascii=False)
        if snapshot == saved_snapshot["json"]:
            return
        written = save_song_config(song_path, config,
                                   getattr(song.header, "song_name", "") or "")
        if written:
            saved_snapshot["json"] = snapshot

    print(Ansi.title("\n=== 音轨预处理 ==="))
    print("  [1] 先垂直压缩，再按乐器排序（打击乐在后，推荐）")
    print("  [2] 保持原始音轨")
    mode = input(Ansi.prompt("选择 (回车=1): ")).strip() or "1"
    if mode == "1":
        song = rearrange_tracks_by_instrument(vertical_compress(song))
        print(Ansi.success(f"已压缩并重排，音轨数 {len(song.layers)}"))

    print(Ansi.info(f"曲名: {song.header.song_name or '未知'}"))
    print(Ansi.info(f"音乐作者: {song.header.original_author or '未知'}"))
    print(Ansi.info(f"音符总数: {len(song.notes)}"))

    virtual_layers = build_virtual_layers(song, config.get('stereo_layers', []))
    if not virtual_layers:
        print(Ansi.error("没有找到任何有音符的音轨，无法生成"))
        return
    print(Ansi.success(f"已创建 {len(virtual_layers)} 个虚拟层"))

    layer_to_group = {layer['vid']: index for index, layer in enumerate(virtual_layers)}
    while True:
        clear_screen()
        print_summary(config, virtual_layers, layer_to_group, song)
        show_tracks_virtual(virtual_layers, layer_to_group,
                            config.get("group_staircase_modes", {}), song)
        print_menu(config)
        choice = input(Ansi.prompt("选择 (回车=生成): ")).strip()

        if choice == "1":
            layer_to_group = edit_groups_interactive_virtual(virtual_layers, song)
        elif choice == "2":
            edit_layout(len(set(layer_to_group.values())), config,
                        layer_to_group, virtual_layers, song)
        elif choice == "3":
            edit_stereo_layers(song, config)
            virtual_layers = build_virtual_layers(song, config.get('stereo_layers', []))
            if not virtual_layers:
                save_config()
                print(Ansi.error("没有可用音轨，无法继续"))
                return
            # 虚拟层变了，分组映射要重建
            layer_to_group = {layer['vid']: index for index, layer in enumerate(virtual_layers)}
            print(Ansi.success(f"立体声设置已更新，重建 {len(virtual_layers)} 个虚拟层"))
        elif choice == "4":
            edit_group_staircase_modes(config, layer_to_group, virtual_layers, song)
        elif choice == "5":
            edit_lyrics_menu(config)
        elif choice == "6":
            edit_particle_settings(config, virtual_layers, layer_to_group)
        elif choice == "7":
            config["uniform_repeater_mode"] = not config.get("uniform_repeater_mode", False)
            if config["uniform_repeater_mode"]:
                print(Ansi.success("已切换为均匀模式：每个空白 tick 一个中继器，"
                                   "时间最直观，但结构会明显变长"))
            else:
                print(Ansi.success("已切换为合并模式：每 4 个空白 tick 并成一个 delay=4 中继器，"
                                   "结构更短（歌词/粒子会自动跟随压缩后的布局）"))
        elif choice == "8":
            save_config()
            print(Ansi.info(f"已退出（配置已按歌曲保存到 "
                            f"{CONFIG_DIR_NAME}/{config_file.name}）"))
            return
        elif not choice:
            generate(config, song, virtual_layers, layer_to_group)
            save_config()
            input(Ansi.prompt("按回车返回菜单..."))
        else:
            print(Ansi.error("无效选项"))
        save_config()


def main() -> None:
    try:
        _interactive()
    except (EOFError, KeyboardInterrupt):
        print()
        print(Ansi.info("已中断"))


if __name__ == "__main__":
    main()
