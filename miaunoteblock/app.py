#!/usr/bin/env python3
"""
NBS 转红石音乐链 - 立体声与歌词命令方块版
- fill 优化提速
- 嵌套排版（圆/方）
- 3 音轨合并
- 乐器识别与自动分组
- 核心音轨组选择
- 音轨排序功能（按乐器归类，打击乐在后）
- 立体声效果：选择特定音轨，左右对称分布，实现双声道
- 歌词通过 MiauParticleEffects 命令方块轨道显示
- 仅生成 .schem 结构文件
"""

import json
import math
import os
import sys
import struct
from collections import defaultdict, Counter
from typing import List, Dict, Tuple, Set

import pynbs

def _patched_read_string(self):
    # 读取 4 字节小端整数作为字符串长度
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

import mcschematic
from pynbs import read as nbs_read
from pynbs import Layer

from .config import load_config, save_config
from .constants import Ansi, DIR_OFFSET
from .instruments import (
    analyze_track_instrument,
    auto_group_by_mode_on_layers,
    rearrange_tracks_by_instrument,
    vertical_compress,
)
from .layout import calc_nest_layers, compute_min_radius, compute_offsets
from .lyrics import build_lyrics_track, edit_lyrics_settings, import_lyrics_json
from .particles import build_particle_commands, edit_particle_settings
from .generation import build_all_commands_virtual, find_structure_bottom, generate_schematic_from_commands

# ── 移除空音轨、垂直压缩（保持不变）───────────────────────────────

def show_tracks_virtual(virtual_layers: List[Dict], layer_to_group: Dict[int, int], group_staircase_modes: Dict[str, str] = None):
    print(Ansi.title("────────── 当前音轨分组（虚拟层）──────────"))
    groups = defaultdict(list)
    for vid, gid in layer_to_group.items():
        groups[gid].append(vid)

    if group_staircase_modes is None:
        group_staircase_modes = {}

    # 构建虚拟层信息查找
    vinfo = {v['vid']: v for v in virtual_layers}

    import re
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    def visible_len(s):
        return len(ansi_escape.sub('', s))

    lines = []
    for gid in sorted(groups):
        vids = sorted(groups[gid])
        parts = []
        for vid in vids:
            info = vinfo[vid]
            orig_id = info['original_id']
            side = info['side']
            layer_obj = next((l for l in song.layers if l.id == orig_id), None)
            name = layer_obj.name if layer_obj and layer_obj.name else f"音轨{orig_id}"
            if side == 'stereo':
                name_str = Ansi.colorize(f"{name} (立体声)", Ansi.WHITE)
            elif side == 'left':
                name_str = Ansi.colorize(f"{name} (左)", Ansi.WHITE)
            elif side == 'right':
                name_str = Ansi.colorize(f"{name} (右)", Ansi.WHITE)
            else:
                name_str = Ansi.colorize(name, Ansi.WHITE)
            cat = info['cat']
            if cat != "钢琴":
                cat_str = Ansi.colorize(f"({cat})", Ansi.BLUE)
                parts.append(f"{name_str}{cat_str}")
            else:
                parts.append(name_str)
        group_header = Ansi.colorize(f"[组{gid+1}]", Ansi.CYAN, bold=True)
        # 显示阶梯模式标签
        mode = group_staircase_modes.get(str(gid), "default")
        if mode == "staircase":
            mode_tag = Ansi.colorize(" [阶梯↓]", Ansi.YELLOW)
        else:
            mode_tag = Ansi.colorize(" [默认]", Ansi.DIM)
        line = group_header + mode_tag + " " + ", ".join(parts)
        lines.append(line)

    if not lines:
        return

    term_width = os.get_terminal_size().columns
    max_line_len = max(visible_len(l) for l in lines)
    col_spacing = 2
    col_width = max_line_len + col_spacing
    cols = max(1, term_width // col_width)
    rows = (len(lines) + cols - 1) // cols

    for row in range(rows):
        line_parts = []
        for col in range(cols):
            idx = row + col * rows
            if idx < len(lines):
                raw = lines[idx]
                raw_len = visible_len(raw)
                padding = col_width - raw_len
                line_parts.append(raw + ' ' * padding)
        print(''.join(line_parts).rstrip())

def edit_groups_interactive_virtual(virtual_layers: List[Dict]) -> Dict[int, int]:
    print(Ansi.title("\n=== 音轨分组编辑（虚拟层） ==="))
    print(Ansi.info("可用虚拟层："))
    for v in virtual_layers:
        orig_id = v['original_id']
        side = v['side']
        name = f"音轨{orig_id}"
        if side == 'stereo':
            name += " (立体声)"
        elif side == 'left':
            name += " (左)"
        elif side == 'right':
            name += " (右)"
        print(f"  {v['vid']}: {Ansi.colorize(name, Ansi.WHITE)} ({v['cat']})")
    print(Ansi.dim("输入2~3个虚拟层序号合并，空格分隔，输入 'auto' 自动分组\n未输入的将单独成组"))
    pairs = []
    while True:
        inp = input(Ansi.prompt("合并虚拟层 (回车结束): ")).strip()
        if not inp:
            break
        if inp.lower() == "auto":
            print("选择自动合并模式：")
            print("    [1] 同乐器优先")
            print("    [2] 最大化合并")
            print("    [3] 顺序系统音色合并（按音轨顺序，相邻同音色才合并，每组最多 3 条）")
            m = input(Ansi.prompt("模式: ")).strip()
            mode = int(m) if m in ('1','2','3') else 1
            layer_to_group = auto_group_by_mode_on_layers(virtual_layers, mode)
            print(Ansi.success(f"自动分组完成，共 {len(set(layer_to_group.values()))} 组"))
            return layer_to_group
        parts = inp.split()
        if len(parts) not in (2, 3):
            print(Ansi.error("请输入2或3个数字"))
            continue
        try:
            ids = [int(p) for p in parts]
        except ValueError:
            print(Ansi.error("无效数字"))
            continue
        if not all(any(v['vid']==i for v in virtual_layers) for i in ids):
            print(Ansi.error("序号不存在"))
            continue
        pairs.append(tuple(sorted(ids)))

    layer_to_group = {}
    group_id = 0
    assigned = set()
    for ids in pairs:
        if any(i in assigned for i in ids):
            print(Ansi.error(f"重复分配，跳过: {ids}"))
            continue
        for i in ids:
            layer_to_group[i] = group_id
        assigned.update(ids)
        group_id += 1
    for v in virtual_layers:
        if v['vid'] not in assigned:
            layer_to_group[v['vid']] = group_id
            group_id += 1
    print(Ansi.success("分组更新完毕"))
    return layer_to_group

def edit_layout(num_groups: int, config: dict, layer_to_group: Dict[int, int], virtual_layers: List[Dict]):
    print(Ansi.title("── 排版样式设置 ──"))
    print("[1] 平铺")
    print("[2] 圆形")
    print("[3] 方形")
    print("[4] 半圆")
    print("[5] 半方形")
    print("[6] 嵌套圆")
    print("[7] 嵌套方")
    style_map = {"1": "flat", "2": "circle", "3": "square", "4": "semicircle",
                 "5": "semisquare", "6": "nest_circle", "7": "nest_square"}
    choice = input(Ansi.prompt("请选择样式 (1-7): ")).strip()
    if choice not in style_map:
        print(Ansi.error("无效，使用当前样式"))
        return
    config["layout_style"] = style_map[choice]
    style = config["layout_style"]

    if style != "flat":
        print(Ansi.info("当前分组（虚拟层）："))
        temp_groups = defaultdict(list)
        for vid, gid in layer_to_group.items():
            temp_groups[gid].append(vid)
        vinfo = {v['vid']: v for v in virtual_layers}
        for gid in sorted(temp_groups):
            vids = sorted(temp_groups[gid])
            parts = []
            for vid in vids:
                info = vinfo[vid]
                orig_id = info['original_id']
                side = info['side']
                layer_obj = next((l for l in song.layers if l.id == orig_id), None)
                name = layer_obj.name if layer_obj and layer_obj.name else f"音轨{orig_id}"
                if side == 'stereo':
                    name_str = Ansi.colorize(f"{name} (立体声)", Ansi.WHITE)
                elif side == 'left':
                    name_str = Ansi.colorize(f"{name} (左)", Ansi.WHITE)
                elif side == 'right':
                    name_str = Ansi.colorize(f"{name} (右)", Ansi.WHITE)
                else:
                    name_str = Ansi.colorize(name, Ansi.WHITE)
                cat = info['cat']
                if cat != "钢琴":
                    cat_str = Ansi.colorize(f"({cat})", Ansi.BLUE)
                    parts.append(f"{name_str}{cat_str}")
                else:
                    parts.append(name_str)
            group_header = Ansi.colorize(f"[组{gid+1}]", Ansi.CYAN, bold=True)
            print(group_header + " " + ", ".join(parts))

        master = input(Ansi.prompt("核心组ID (留空则无): ")).strip()
        if master:
            try:
                config["master_group"] = int(master) - 1
            except:
                config["master_group"] = None
        else:
            config["master_group"] = None

        has_master = config["master_group"] is not None
        min_r = compute_min_radius(num_groups, style, has_master)
        print(Ansi.info(f"最小允许半径/边长: {min_r}"))

        if style in ("nest_circle", "nest_square"):
            max_r = config.get("layout_radius", 10)
            max_layers = calc_nest_layers(num_groups, max_r, config.get("layer_gap", 4))
            print(Ansi.info(f"最大可嵌套层数: {max_layers}"))
            while True:
                try:
                    layers = int(input(Ansi.prompt(f"嵌套层数 (1~{max_layers}): ")))
                    if 1 <= layers <= max_layers:
                        config["nest_layers"] = layers
                        break
                except:
                    pass
                print(Ansi.error("输入无效"))
            try:
                gap = int(input(Ansi.prompt("层间距 (默认4): ") or "4"))
                config["layer_gap"] = max(1, gap)
            except:
                config["layer_gap"] = 4

        while True:
            try:
                radius = int(input(Ansi.prompt(f"半径/边长 (≥{min_r}): ") or str(min_r)))
                if radius < min_r:
                    print(Ansi.error(f"半径不得小于 {min_r}"))
                    continue
                config["layout_radius"] = radius
                break
            except:
                print(Ansi.error("请输入整数"))
    else:
        try:
            spacing = int(input(Ansi.prompt("平铺间距 (默认3): ") or "3"))
            config["flat_spacing"] = spacing
        except:
            pass

    save_config(config)
    print(Ansi.success("排版设置已更新"))

def edit_stereo_layers(song, config):
    """交互式选择立体声音轨"""
    print(Ansi.title("\n=== 立体声轨道设置 ==="))
    print(Ansi.info("选择哪些原始音轨作为立体声（左右声道独立）。"))
    print("立体声轨道的音符会同时出现在左右两条链上，并在排版中对称分布。")
    print(Ansi.dim("当前可用的音轨（原始 layer ID）："))
    # 获取所有非空音轨
    available_layers = sorted({note.layer for note in song.notes})
    for lid in available_layers:
        layer_obj = next((l for l in song.layers if l.id == lid), None)
        name = layer_obj.name if layer_obj and layer_obj.name else f"音轨{lid}"
        inst, cat = analyze_track_instrument(song, lid)
        print(f"  {lid}: {Ansi.colorize(name, Ansi.WHITE)} ({cat})")
    print(Ansi.prompt("请输入要设为立体声的音轨ID，多个用空格分隔，直接回车跳过："))
    inp = input().strip()
    stereo = []
    if inp:
        parts = inp.split()
        for p in parts:
            try:
                lid = int(p)
                if lid in available_layers:
                    stereo.append(lid)
                else:
                    print(Ansi.error(f"忽略无效音轨ID: {lid}"))
            except:
                pass
    config['stereo_layers'] = stereo
    save_config(config)
    if stereo:
        print(Ansi.success(f"已设置立体声音轨: {stereo}"))
    else:
        print(Ansi.info("已清除立体声设置（所有音轨为单声道）"))

def edit_group_staircase_modes(config, layer_to_group, virtual_layers):
    """交互式设置每个分组的生成模式（默认/阶梯向下）"""
    print(Ansi.title("\n=== 分组生成模式设置 ==="))
    print(Ansi.info("为每个分组独立设置生成模式："))
    print("  [默认] - 所有音符在同一水平面（默认模式）")
    print("  [阶梯↓] - 侧边音符向下降一格，形成阶梯结构")
    print()
    group_staircase_modes = config.get("group_staircase_modes", {})
    groups = sorted(set(layer_to_group.values()))

    # 显示当前状态
    vinfo = {v['vid']: v for v in virtual_layers}
    for gid in groups:
        vids = sorted([vid for vid, g in layer_to_group.items() if g == gid])
        parts = []
        for vid in vids:
            info = vinfo[vid]
            orig_id = info['original_id']
            layer_obj = next((l for l in song.layers if l.id == orig_id), None)
            name = layer_obj.name if layer_obj and layer_obj.name else f"音轨{orig_id}"
            parts.append(name)
        current_mode = group_staircase_modes.get(str(gid), "default")
        mode_display = Ansi.colorize("阶梯↓", Ansi.YELLOW) if current_mode == "staircase" else Ansi.colorize("默认", Ansi.DIM)
        group_label = Ansi.colorize(f"[组{gid+1}]", Ansi.CYAN, bold=True)
        print(f"  {group_label} {mode_display}  ({', '.join(parts)})")

    print()
    print(Ansi.prompt("输入要切换模式的组ID（用空格分隔，回车跳过）："))
    inp = input().strip()
    if not inp:
        return

    for part in inp.split():
        try:
            gid = int(part) - 1  # 用户输入1-based，转换为0-based
            if gid in groups:
                current = group_staircase_modes.get(str(gid), "default")
                new_mode = "staircase" if current != "staircase" else "default"
                group_staircase_modes[str(gid)] = new_mode
                display = Ansi.colorize("阶梯↓", Ansi.YELLOW) if new_mode == "staircase" else Ansi.colorize("默认", Ansi.DIM)
                print(Ansi.success(f"组{gid+1} 已切换为 {display}"))
            else:
                print(Ansi.error(f"组{gid+1} 不存在，跳过"))
        except ValueError:
            print(Ansi.error(f"无效输入: {part}"))

    config["group_staircase_modes"] = group_staircase_modes
    save_config(config)

# ── 主函数 ─────────────────────────────────────────────────

def main():
    global song  # 为了让 show_tracks_virtual 访问
    os.system('cls' if os.name == 'nt' else 'clear')
    config = load_config()
    print(Ansi.title("══════ NBS生成器 - MiauRedstoneMusic (阶梯模式版) ══════"))
    print(Ansi.dim("作者: COM1919 | 版本: 3.3 (立体声+阶梯模式)"))

    nbs_path = 0
    try:
      
        nbs_path = input(Ansi.prompt("NBS 文件路径: ") or "song.nbs").strip()
    except:
        nbs_path = input(Ansi.prompt("NBS 文件路径: ") or "song.nbs").strip()

    try:
        song = nbs_read(nbs_path)
    except Exception as e:
        print(Ansi.error(f"读取 NBS 失败: {e}"))
        sys.exit(1)

    if not song.notes:
        print(Ansi.error("错误：NBS 文件中没有任何音符，无法生成红石音乐链。"))
        sys.exit(1)
        
    # 2. 询问用户是否按乐器排序
    print(Ansi.prompt("\n请选择后续处理模式："))
    print("  [1] 压缩后按乐器类型排序（打击乐在后，每个音轨一种乐器）")
    print("  [2] 不处理")
    mode = input(Ansi.prompt("请输入 (1/2): ")).strip()
    if mode == "1":
        print(Ansi.info("正在执行垂直压缩..."))
        song = vertical_compress(song)
        print(Ansi.success(f"压缩完成，原始音轨数：{len(song.layers)}"))
        print(Ansi.info("正在按乐器类型重新排列音轨..."))
        song = rearrange_tracks_by_instrument(song)
        print(Ansi.success("乐器排序完成！"))
        config["merge_groups"] = []
        save_config(config)
    elif mode == "2":
        print(Ansi.info("好的"))
    else:
        print(Ansi.error("无效输入，默认跳过"))

    # 3. 确保有一个空音轨
    #song = ensure_one_empty_layer(song)
    #print(Ansi.info(f"最终原始音轨数量（含空音轨）：{len(song.layers)}"))

    # 4. 输出基本信息
    max_layer = max(note.layer for note in song.notes) if song.notes else 0
    print(Ansi.info(f"曲名: {song.header.song_name or '未知'}"))
    print(Ansi.info(f"音符总数: {len(song.notes)}"))
    print(Ansi.info(f"原始音轨数量: {max_layer+1}"))
    print(Ansi.info(f"音乐作者: {song.header.original_author}"))
    print(Ansi.info(f"描述: {song.header.description}"))

    # 5. 构建虚拟层（基于立体声配置，正确方式：复制音符）
    stereo_set = set(config.get('stereo_layers', []))
    virtual_layers = []
    next_vid = 0
    for orig_layer in song.layers:
        lid = orig_layer.id
        notes = [n for n in song.notes if n.layer == lid]
        if not notes:
            continue  # 跳过空音轨
        inst, cat = analyze_track_instrument(song, lid)
        if lid in stereo_set:
            # 立体声：复制每个音符一次，使每个 tick 有两个相同音符
            stereo_notes = []
            for note in notes:
                stereo_notes.append(note)
                stereo_notes.append(note)      # 复制一份
            stereo_notes.sort(key=lambda n: n.tick)   # 确保同一 tick 的两个音符相邻
            virtual_layers.append({
                'vid': next_vid,
                'original_id': lid,
                'side': 'stereo',
                'notes': stereo_notes,
                'inst': inst,
                'cat': cat
            })
            next_vid += 1
        else:
            virtual_layers.append({
                'vid': next_vid,
                'original_id': lid,
                'side': None,
                'notes': notes,
                'inst': inst,
                'cat': cat
            })
            next_vid += 1

    if not virtual_layers:
        print(Ansi.error("没有找到任何有音符的虚拟层，无法生成。"))
        sys.exit(1)

    print(Ansi.success(f"已创建 {len(virtual_layers)} 个虚拟层（立体声效果：{len(stereo_set)} 个音轨被立体声化）"))

    # 6. 交互循环
    layer_to_group = {v['vid']: idx for idx, v in enumerate(virtual_layers)}
    is_first_show_text = True
    while True:
        if is_first_show_text:
            is_first_show_text = False
        else:
            input("按回车键继续...")
        os.system('cls' if os.name == 'nt' else 'clear')

        print(Ansi.title("══════ 当前配置 ══════"))
        print(Ansi.colorize("[生成参数]", Ansi.MAGENTA))
        print(f"  线程数: {config.get('num_threads', 4)}")
        print(f"  方向: {config['direction']}")
        style = config.get("layout_style", "flat")
        print(f"  排版: {style}", end="")
        if style == "flat":
            print(f" 间距={config.get('flat_spacing', 3)}")
        elif style in ("nest_circle", "nest_square"):
            print(f" 半径={config.get('layout_radius', 5)} 层数={config.get('nest_layers', 1)}")
        else:
            print(f" 半径={config.get('layout_radius', 5)}", end="")
            if config.get("master_group") is not None:
                print(f" 核心组={config['master_group']+1}", end="")
            print()
        print(Ansi.colorize("[立体声]", Ansi.MAGENTA))
        stereo_layers = config.get('stereo_layers', [])
        if stereo_layers:
            print(f"  立体声音轨: {stereo_layers}")
        else:
            print(f"  无立体声")
        print(Ansi.colorize("[分组生成模式]", Ansi.MAGENTA))
        gsm = config.get("group_staircase_modes", {})
        stair_groups = [str(int(k)+1) for k, v in gsm.items() if v == "staircase"]
        if stair_groups:
            print(f"  阶梯模式组: {', '.join(stair_groups)}")
        else:
            print(f"  全部使用默认模式")
        print(Ansi.colorize("[歌词显示]", Ansi.MAGENTA))
        imported_lyrics = config.get("lyrics_import") or {}
        if imported_lyrics:
            print(f"  已导入歌词: {len(imported_lyrics)} 条（导入即启用歌词功能）")
            if config.get("lyrics_stream"):
                print(f"  流式歌词: 已启用（{config.get('lyrics_stream_seconds', 2.0)} 秒 / "
                      f"上限 {config.get('lyrics_stream_max_len', 18)} 字）")
            else:
                print(f"  流式歌词: 未启用")
            print(f"  命令方块链: 整个结构下方向下 {config.get('lyrics_chain_y_offset', 1)} 格，最前面一条线")
        else:
            print(f"  未启用歌词显示（可在 [9] 导入歌词 JSON）")

        print(Ansi.colorize("[粒子特效]", Ansi.MAGENTA))
        p_layers = config.get("particles_layers") or []
        if config.get("particles_enabled") and p_layers:
            ball_groups = sorted({layer_to_group.get(v, v) for v in p_layers})
            print(f"  已启用: {len(p_layers)} 条轨道 -> {len(ball_groups)} 颗弹力球")
            print(f"  trail={config.get('particles_trail', 24)} height={config.get('particles_height', 3)} "
                  f"curve={config.get('particles_curve', 'arc')} "
                  f"color={config.get('particles_color') or '自动分配'}")
        elif config.get("particles_enabled"):
            print(f"  已启用，但未选择任何轨道（可在 [10] 选择）")
        else:
            print(f"  未启用（可在 [10] 设置）")

        show_tracks_virtual(virtual_layers, layer_to_group, gsm)

        print(Ansi.title("──────── 菜单 ────────"))
        print("[1] 修改方向")
        print("[2] 修改音轨分组（虚拟层）")
        print("[3] 修改排版")
        print("[4] 保存并退出")
        print("[5] 设置立体声音轨")
        print("[6] 切换中继器均匀速度模式 " + ("(已启用)" if config.get("uniform_repeater_mode", False) else "(已禁用)"))
        print("[7] 设置分组生成模式（默认/阶梯向下）")
        print("[8] 歌词显示与命令方块链位置")
        print("[9] 导入/清空歌词 JSON（导入即启用歌词）")
        print("[10] 粒子特效（音符盒跳动）")
        choice = input(Ansi.prompt("选择 (回车开始生成): ")).strip()

        if choice == "1":
            d = input(Ansi.prompt(f"方向 ({config['direction']}): ") or config["direction"]).lower()
            if d in DIR_OFFSET:
                config["direction"] = d
                save_config(config)
            else:
                print(Ansi.error("无效方向"))
        elif choice == "2":
            layer_to_group = edit_groups_interactive_virtual(virtual_layers)
            save_config(config)
        elif choice == "3":
            num_groups = len(set(layer_to_group.values()))
            edit_layout(num_groups, config, layer_to_group, virtual_layers)
        elif choice == "4":
            save_config(config)
            print(Ansi.success("再见！"))
            break
        elif choice == "5":
            edit_stereo_layers(song, config)
            # 重新构建虚拟层（使用正确方法）
            stereo_set = set(config.get('stereo_layers', []))
            virtual_layers = []
            next_vid = 0
            for orig_layer in song.layers:
                lid = orig_layer.id
                notes = [n for n in song.notes if n.layer == lid]
                if not notes:
                    continue
                inst, cat = analyze_track_instrument(song, lid)
                if lid in stereo_set:
                    stereo_notes = []
                    for note in notes:
                        stereo_notes.append(note)
                        stereo_notes.append(note)
                    stereo_notes.sort(key=lambda n: n.tick)
                    virtual_layers.append({
                        'vid': next_vid,
                        'original_id': lid,
                        'side': 'stereo',
                        'notes': stereo_notes,
                        'inst': inst,
                        'cat': cat
                    })
                    next_vid += 1
                else:
                    virtual_layers.append({
                        'vid': next_vid,
                        'original_id': lid,
                        'side': None,
                        'notes': notes,
                        'inst': inst,
                        'cat': cat
                    })
                    next_vid += 1
            # 重置分组
            layer_to_group = {v['vid']: idx for idx, v in enumerate(virtual_layers)}
            print(Ansi.success(f"立体声设置已更新，重新创建了 {len(virtual_layers)} 个虚拟层"))
        elif choice == "6":
            current = config.get("uniform_repeater_mode", False)
            config["uniform_repeater_mode"] = not current
            save_config(config)
            print(Ansi.success(f"中继器填充模式已{'启用' if config['uniform_repeater_mode'] else '禁用'}"))
        elif choice == "7":
            edit_group_staircase_modes(config, layer_to_group, virtual_layers)
        elif choice == "8":
            edit_lyrics_settings(config)
        elif choice == "9":
            import_lyrics_json(config, getattr(song.header, "tempo", None))
        elif choice == "10":
            edit_particle_settings(config, virtual_layers, layer_to_group)
        elif not choice:
            max_in_file = max(n.tick for n in song.notes) if song.notes else 0
            try:
                max_tick = int(input(Ansi.prompt(f"希望生成的长度 (≤{max_in_file} 回车默认): ") or str(max_in_file)))
            except:
                print(Ansi.error("输入错误"))
                continue

            # 结构可放在任何位置，统一用 0,0,0 作为内部基准坐标；
            # schematic 保存时会按实际方块范围归一化，文字命令用 ~~~ 相对坐标。
            start_x = start_y = start_z = 0

            # 计算分组
            groups = sorted(set(layer_to_group.values()))
            num_groups = len(groups)

            # 计算偏移量（移除了立体声配对参数）
            offsets = compute_offsets(groups, config, num_groups)
            group_offset_dict = {gid: offsets[i] for i, gid in enumerate(groups)}

            print(Ansi.info("正在预计算方块位置..."))
            use_lamp = config.get("use_redstone_lamp", False)
            particle_note_events = []
            commands, activation_positions = build_all_commands_virtual(
                virtual_layers, layer_to_group, [group_offset_dict[gid] for gid in groups],
                (start_x, start_y, start_z), config["direction"], max_tick,
                use_lamp=use_lamp,
                uniform_repeater_mode=config.get("uniform_repeater_mode", False),
                group_staircase_modes=config.get("group_staircase_modes", {}),
                note_events=particle_note_events,
            )
            # 添加命令方块启动链
            dx, dy, dz = DIR_OFFSET[config["direction"]]
            cb1_x = start_x - 5*dx
            cb1_y = start_y
            cb1_z = start_z if config["direction"] in ("east", "west") else start_z - 5*dz

            cb2_x = start_x - 7*dx
            cb2_y = start_y
            cb2_z = start_z if config["direction"] in ("east", "west") else start_z - 7*dz

            # ─────────────────────────────────────────────────────────────
            # 歌词轨道链（先构建，便于把歌词链的启动点并入启动链）
            # 命令方块链的纵向位置由 build_lyrics_track 按「整个结构最低方块」相对计算，
            # 默认紧贴结构下方；文字显示坐标仍为结构上方的绝对锚点。
            if config.get("lyrics_import"):
                print(Ansi.info("正在处理歌词链..."))
            lyr_cmds, lyr_activation, _lyr_chain_y = build_lyrics_track(
                virtual_layers, group_offset_dict,
                (start_x, start_y, start_z), config["direction"], max_tick, config,
                use_lamp=use_lamp,
                uniform_repeater_mode=config.get("uniform_repeater_mode", False),
                struct_bottom_y=find_structure_bottom(commands),
                tempo=getattr(song.header, "tempo", None),
            )
            # ─────────────────────────────────────────────────────────────

            sorted_groups = sorted(activation_positions.items(), key=lambda x: x[0])
            if lyr_activation is not None:
                sorted_groups.append((-1, lyr_activation))
            cmdblock_cmds = []
            def rel_str(d: int) -> str:
                if d == 0:
                    return "~"
                else:
                    return f"~{d}"
            # 在循环中计算每组命令方块的相对偏移
            for i, (gid, (ax, ay, az)) in enumerate(sorted_groups):
                # 放置柱（第一根柱子）坐标
                if i == 0:
                    cx1, cy1, cz1 = cb1_x, cb1_y, cb1_z
                    cx2, cy2, cz2 = cb2_x, cb2_y, cb2_z
                else:
                    cx1, cy1, cz1 = cb1_x, cb1_y + i, cb1_z
                    cx2, cy2, cz2 = cb2_x, cb2_y + i, cb2_z
            
                # 计算相对于放置柱命令方块的偏移（用于放置红石块的命令）
                rel_x_place = ax - cx1
                rel_y_place = ay - cy1
                rel_z_place = az - cz1
                # 计算相对于清除柱命令方块的偏移（用于清除红石块的命令）
                rel_x_remove = ax - cx2
                rel_y_remove = ay - cy2
                rel_z_remove = az - cz2
            
                # 生成相对坐标命令
                place_cmd = f"setblock {rel_str(rel_x_place)} {rel_str(rel_y_place)} {rel_str(rel_z_place)} minecraft:redstone_block"
                remove_cmd = f"setblock {rel_str(rel_x_remove)} {rel_str(rel_y_remove)} {rel_str(rel_z_remove)} minecraft:air"
            
                place_json = json.dumps(place_cmd)
                remove_json = json.dumps(remove_cmd)
            
                if i == 0:
                    cmdblock_cmds.append(
                        f"setblock {cx1} {cy1} {cz1} minecraft:command_block[facing=up]{{Command:{place_json}}}"
                    )
                    cmdblock_cmds.append(
                        f"setblock {cx2} {cy2} {cz2} minecraft:command_block[facing=up]{{Command:{remove_json}}}"
                    )
                else:
                    cmdblock_cmds.append(
                        f"setblock {cx1} {cy1} {cz1} minecraft:chain_command_block[facing=up,conditional=false]{{Command:{place_json},auto:1b}}"
                    )
                    cmdblock_cmds.append(
                        f"setblock {cx2} {cy2} {cz2} minecraft:chain_command_block[facing=up,conditional=false]{{Command:{remove_json},auto:1b}}"
                    )
            commands = commands[:1] + cmdblock_cmds + commands[1:]
            # 歌词命令方块链（其启动点已并入上面的启动链）
            commands.extend(lyr_cmds)

            # 粒子特效命令方块（音符盒跳动，由 MiauParticleEffects 模组驱动）
            if config.get("particles_enabled") and (config.get("particles_layers") or []):
                commands.extend(build_particle_commands(
                    particle_note_events, activation_positions, config["direction"], config
                ))

            schem_path = input(Ansi.prompt("保存结构文件的路径 (默认 ./redstone_music.schem): ")).strip()
            if not schem_path:
                schem_path = "./redstone_music.schem"
            generate_schematic_from_commands(commands, schem_path)

            input("按回车返回菜单...")
        else:
            print(Ansi.error("无效选项"))

if __name__ == "__main__":
    main()

