"""音符盒粒子特效对接（MiauParticleEffects 模组）。

为选中的虚拟层音轨所在的红石链生成一个脉冲命令方块，
写入 `/mpe noteblock chain ~~~ x,y,z,delay|...`，
由模组按纯时间轴驱动弹力球在音符盒之间跳动。
"""

import json
import random
from typing import Dict, List, Optional, Tuple

from .console import ask_number
from .constants import Ansi, DIR_OFFSET

MPE_CHAIN_PREFIX = "/mpe noteblock chain ~~~"
CURVE_CHOICES = ("arc", "sine", "line")
DEFAULT_TRAIL = 24
DEFAULT_CURVE = "arc"
DEFAULT_HEIGHT = 3
DEFAULT_LINGER = 40

# 风格预设：拖尾 / 跳动高度 / 停留
PRESETS = {
    "light":  {"label": "轻", "trail": 12, "height": 2, "linger": 20},
    "normal": {"label": "中", "trail": 24, "height": 3, "linger": 40},
    "strong": {"label": "强", "trail": 48, "height": 5, "linger": 60},
}


def _clean_option_value(value) -> str:
    """颜色值里出现空白或分隔符会把命令拆坏，直接剔除。"""
    text = str(value or "")
    for ch in " ;|":
        text = text.replace(ch, "")
    return text.strip()


def _format_options(config: dict) -> str:
    """拼出 trail= / curve= / height= / color= / gradient= / linger= 选项串。"""
    try:
        trail = int(config.get("particles_trail", DEFAULT_TRAIL))
    except (TypeError, ValueError):
        trail = DEFAULT_TRAIL
    opts = [f"trail={max(2, min(256, trail))}"]

    curve = str(config.get("particles_curve", DEFAULT_CURVE) or DEFAULT_CURVE).lower()
    if curve not in CURVE_CHOICES:
        curve = DEFAULT_CURVE
    opts.append(f"curve={curve}")

    try:
        height = float(config.get("particles_height", DEFAULT_HEIGHT))
    except (TypeError, ValueError):
        height = float(DEFAULT_HEIGHT)
    height = max(0.5, min(64.0, height))
    # 整数时省略小数位，避免模组按整数解析 height 时失败（如 "3.0"）
    opts.append(f"height={int(height) if height == int(height) else height}")

    color = _clean_option_value(config.get("particles_color"))
    if color:
        opts.append(f"color={color}")
    gradient = _clean_option_value(config.get("particles_gradient"))
    if gradient:
        opts.append(f"gradient={gradient}")

    try:
        linger = int(config.get("particles_linger", DEFAULT_LINGER))
    except (TypeError, ValueError):
        linger = DEFAULT_LINGER
    opts.append(f"linger={max(-1, linger)}")
    return " ".join(opts)


def build_particle_commands(
    note_events: List[Tuple[int, int, int, int, int, int]],
    activation_positions: Dict[int, Tuple[int, int, int]],
    direction: str,
    config: dict,
    rng: Optional[random.Random] = None,
) -> List[str]:
    """为选中的虚拟层所在红石链生成粒子命令方块。

    note_events 每项为 (gid, vid, tick, x, y, z)，是音符盒的内部绝对坐标，
    由 generation.build_all_commands_virtual 收集。
    activation_positions 为 {gid: 红石块坐标}，命令方块放在其沿链反方向前一格。

    时序：红石块放置 t0 → 起始中继器 delay=1 → 第 0 个事件格在 t0+2 发声；
    粒子命令方块与红石块相邻，被激活后 t0+1 执行。因此
        delay[0] = 1 + 2 × (该组首音符 tick − 全局最小音符 tick)
        delay[i] = 2 × (tick_i − tick_{i-1})   （中继器 1 档 = 2 游戏 tick）
    返回 setblock 命令列表（每个参与的组一个脉冲命令方块）。
    """
    if rng is None:
        rng = random.Random()
    # 容错：配置被手改后可能含非法项，跳过无法解析的 vid 而不是中断生成
    selected = set()
    for value in (config.get("particles_layers") or []):
        try:
            selected.add(int(value))
        except (TypeError, ValueError):
            continue
    if not selected or not note_events:
        return []

    dx, _, dz = DIR_OFFSET[direction]
    global_first_tick = min(event[2] for event in note_events)

    # 按组收集候选音符盒：{gid: [(tick, x, y, z), ...]}
    by_group: Dict[int, List[Tuple[int, int, int, int]]] = {}
    for gid, vid, tick, x, y, z in note_events:
        if vid not in selected:
            continue
        by_group.setdefault(gid, []).append((tick, x, y, z))

    commands = []
    total_balls = 0
    for gid in sorted(by_group):
        if gid not in activation_positions:
            continue
        candidates = sorted(by_group[gid], key=lambda item: item[0])
        ax, ay, az = activation_positions[gid]
        anchor = (ax - dx, ay, az - dz)  # 沿链反方向前一格，与红石块相邻

        # 同一 tick 多个并排音符盒：随机取一个（一颗球同刻只能在一处）
        picked: List[Tuple[int, int, int, int]] = []
        i = 0
        while i < len(candidates):
            tick = candidates[i][0]
            same = []
            while i < len(candidates) and candidates[i][0] == tick:
                same.append(candidates[i])
                i += 1
            picked.append(rng.choice(same))

        parts = []
        prev_tick = None
        for idx, (tick, x, y, z) in enumerate(picked):
            delay = 1 + 2 * (tick - global_first_tick) if idx == 0 else 2 * (tick - prev_tick)
            prev_tick = tick
            parts.append(f"{x - anchor[0]},{y - anchor[1]},{z - anchor[2]},{delay}")

        command = f"{MPE_CHAIN_PREFIX} {'|'.join(parts)} {_format_options(config)}"
        command_json = json.dumps(command, ensure_ascii=False)
        commands.append(
            f"setblock {anchor[0]} {anchor[1]} {anchor[2]} "
            f"minecraft:command_block[facing=up]{{Command:{command_json}}}"
        )
        total_balls += 1

    if total_balls:
        print(Ansi.success(f"粒子特效命令方块已添加 {total_balls} 个（每个对应一颗弹力球）"))
    return commands


# ── 粒子特效设置 UI ──────────────────────────────────────────

def _selected_vids(config: dict) -> List[int]:
    vids = []
    for value in (config.get("particles_layers") or []):
        try:
            vids.append(int(value))
        except (TypeError, ValueError):
            continue
    return vids


def _particle_status(config: dict, layer_to_group: Dict[int, int]) -> str:
    selected = _selected_vids(config)
    if not config.get("particles_enabled"):
        return Ansi.dim("未启用") + (f"（已选 {len(selected)} 条轨道）" if selected else "")
    if not selected:
        return Ansi.dim("已启用，但还没有选择任何轨道")
    groups = sorted({layer_to_group.get(vid, vid) for vid in selected})
    return (f"{len(selected)} 条轨道 → {len(groups)} 颗弹力球 "
            f"（组 {', '.join(str(g + 1) for g in groups)}）")


def _print_particle_params(config: dict) -> None:
    print(Ansi.dim(f"  trail={config.get('particles_trail')} "
                   f"curve={config.get('particles_curve')} "
                   f"height={config.get('particles_height')} "
                   f"color={config.get('particles_color') or '自动'} "
                   f"gradient={config.get('particles_gradient') or '无'} "
                   f"linger={config.get('particles_linger')}"))


def apply_particle_preset(config: dict, name: str) -> bool:
    preset = PRESETS.get(name)
    if not preset:
        return False
    for key in ("trail", "height", "linger"):
        config[f"particles_{key}"] = preset[key]
    config["particles_preset"] = name
    return True


def _ask(label: str, current, cast, minimum=None, maximum=None):
    """高级参数里的数值输入（缩进两级，统一走 console.ask_number）。"""
    return ask_number(label, current, cast, minimum, maximum, indent="  ")


def _edit_advanced(config: dict) -> None:
    """高级参数子菜单：只在需要调参时才进来。"""
    while True:
        print(Ansi.title("\n=== 粒子特效 · 高级参数 ==="))
        _print_particle_params(config)
        print("  [1] 拖尾长度 trail")
        print("  [2] 竖直起伏 curve（arc 弧线 / sine 正弦 / line 直线）")
        print("  [3] 跳动高度 height")
        print("  [4] 纯色 color（#RRGGBB / rainbow，0=交给模组分配）")
        print("  [5] 渐变 gradient（#RRGGBB,#RRGGBB / rainbow，0=不用）")
        print("  [6] 末音符后停留 linger（-1=常驻）")
        print("  [0] 返回")
        choice = input(Ansi.prompt("选择: ")).strip()
        if choice == "1":
            config["particles_trail"] = _ask(
                "拖尾长度", config.get("particles_trail"), int, 2, 256)
        elif choice == "2":
            curve = input(Ansi.prompt(
                f"  curve arc/sine/line ({config.get('particles_curve')})：")).strip().lower()
            if not curve:
                continue
            if curve in CURVE_CHOICES:
                config["particles_curve"] = curve
            else:
                print(Ansi.error("只能是 arc / sine / line"))
        elif choice == "3":
            config["particles_height"] = _ask(
                "跳动高度", config.get("particles_height"), float, 0.5, 64.0)
        elif choice == "4":
            color = input(Ansi.prompt(
                f"  纯色 ({config.get('particles_color') or '自动分配'}，0=清除): ")).strip()
            if color:
                config["particles_color"] = "" if color == "0" else _clean_option_value(color)
        elif choice == "5":
            gradient = input(Ansi.prompt(
                f"  渐变 ({config.get('particles_gradient') or '无'}，0=清除): ")).strip()
            if gradient:
                config["particles_gradient"] = "" if gradient == "0" else _clean_option_value(gradient)
        elif choice == "6":
            config["particles_linger"] = _ask(
                "停留 tick", config.get("particles_linger"), int, -1)
        elif choice in ("", "0", "q"):
            return
        else:
            print(Ansi.error("无效选项"))


def _toggle_layers(config: dict, virtual_layers: List[dict], layer_to_group: Dict[int, int]) -> None:
    """按编号切换轨道开关（输入多个编号，all=全选，c=清空）。"""
    if not virtual_layers:
        print(Ansi.error("没有可用轨道"))
        return
    order = [layer["vid"] for layer in virtual_layers]
    selected = set(_selected_vids(config))
    raw = input(Ansi.prompt("要切换的轨道编号（空格分隔，all=全选，c=清空，回车=不改）: ")).strip()
    if not raw:
        return
    low = raw.lower()
    if low in ("all", "*"):
        selected = set(order)
    elif low in ("c", "clear", "none"):
        selected = set()
    else:
        for token in raw.replace(",", " ").split():
            try:
                index = int(token) - 1
            except ValueError:
                print(Ansi.error(f"忽略无效编号: {token}"))
                continue
            if not 0 <= index < len(order):
                print(Ansi.error(f"编号超出范围: {token}"))
                continue
            vid = order[index]
            if vid in selected:
                selected.discard(vid)
            else:
                selected.add(vid)
    config["particles_layers"] = [vid for vid in order if vid in selected]


def edit_particle_settings(config: dict, virtual_layers: List[dict],
                           layer_to_group: Dict[int, int]) -> None:
    """粒子特效子菜单：根菜单显示轨道开关，细参数收进高级子菜单。"""
    while True:
        print(Ansi.title("\n=== 音符盒粒子特效（MiauParticleEffects）==="))
        print(f"  状态: {_particle_status(config, layer_to_group)}")
        _print_particle_params(config)
        if virtual_layers:
            print(Ansi.dim("  轨道开关（[x] 表示参与粒子）："))
            selected = set(_selected_vids(config))
            for index, layer in enumerate(virtual_layers, start=1):
                vid = layer["vid"]
                mark = "x" if vid in selected else " "
                group = layer_to_group.get(vid, vid)
                print(f"    {index:>2}. [{mark}] vid={vid} 组={group + 1} "
                      f"{layer.get('cat', '?')} {len(layer.get('notes', []))} 音符")
        print("  [1] 启用 / 禁用")
        print("  [2] 切换轨道")
        print("  [3] 风格预设（轻 / 中 / 强）")
        print("  [4] 高级参数（trail / curve / height / color / gradient / linger）")
        print("  [0] 返回")
        choice = input(Ansi.prompt("选择: ")).strip()

        if choice == "1":
            enabled = not config.get("particles_enabled")
            config["particles_enabled"] = enabled
            if enabled and not _selected_vids(config) and virtual_layers:
                # 首次启用时自动把所有轨道都选上，省掉一次选择
                config["particles_layers"] = [layer["vid"] for layer in virtual_layers]
                print(Ansi.info("已自动选中全部轨道（可在 [2] 里关掉不需要的）"))
            print(Ansi.success(f"粒子特效应已{'启用' if enabled else '禁用'}"))
        elif choice == "2":
            _toggle_layers(config, virtual_layers, layer_to_group)
        elif choice == "3":
            print("  预设：" + " / ".join(
                f"{index}={preset['label']}" for index, preset in enumerate(PRESETS.values(), start=1)))
            raw = input(Ansi.prompt(
                f"  选择预设（当前 {PRESETS.get(config.get('particles_preset'), PRESETS['normal'])['label']}，回车不改）: ")).strip()
            if raw:
                names = list(PRESETS)
                if raw in names:
                    apply_particle_preset(config, raw)
                elif raw.isdigit() and 1 <= int(raw) <= len(names):
                    apply_particle_preset(config, names[int(raw) - 1])
                else:
                    print(Ansi.error("无效预设"))
                if config.get("particles_preset"):
                    print(Ansi.success(
                        f"已应用预设：{PRESETS[config['particles_preset']]['label']}"))
        elif choice == "4":
            _edit_advanced(config)
        elif choice in ("", "0", "q"):
            return
        else:
            print(Ansi.error("无效选项"))
