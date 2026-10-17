"""音符盒粒子特效对接（MiauParticleEffects 模组）。

为选中的虚拟层音轨所在的红石链生成一个脉冲命令方块，
写入 `/mpe noteblock chain ~~~ x,y,z,delay|...`，
由模组按纯时间轴驱动弹力球在音符盒之间跳动。
"""

import json
import random
from typing import Dict, List, Optional, Tuple

from .config import save_config
from .constants import Ansi, DIR_OFFSET

MPE_CHAIN_PREFIX = "/mpe noteblock chain ~~~"
CURVE_CHOICES = ("arc", "sine", "line")
DEFAULT_TRAIL = 24
DEFAULT_CURVE = "arc"
DEFAULT_HEIGHT = 3
DEFAULT_LINGER = 40


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

    # 颜色值不能含空格，否则会把选项串拆成多个参数
    color = str(config.get("particles_color") or "").replace(" ", "").strip()
    if color:
        opts.append(f"color={color}")
    gradient = str(config.get("particles_gradient") or "").replace(" ", "").strip()
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
    # 容错：配置文件被手改后可能含非法项，跳过无法解析的 vid 而不是中断生成
    selected = set()
    for v in (config.get("particles_layers") or []):
        try:
            selected.add(int(v))
        except (TypeError, ValueError):
            continue
    if not selected or not note_events:
        return []

    dx, _, dz = DIR_OFFSET[direction]
    global_first_tick = min(ev[2] for ev in note_events)

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

def edit_particle_settings(config: dict, virtual_layers: List[dict], layer_to_group: Dict[int, int]):
    """交互式修改音符盒粒子特效的开关、参与轨道与显示参数。"""
    print(Ansi.title("\n=== 音符盒粒子特效（MiauParticleEffects）==="))
    print(Ansi.info("为选中的轨道在红石链起点生成脉冲命令方块，模组据此驱动弹力球跳动。"))
    print(Ansi.dim("不选任何轨道则不生成任何粒子命令方块，行为与现状完全一致。"))

    enabled = config.get("particles_enabled", False)
    ans = input(Ansi.prompt(f"是否启用粒子特效 (y/n，回车={'保持启用' if enabled else '保持禁用'}): ")).strip().lower()
    if ans in ("y", "yes"):
        config["particles_enabled"] = True
    elif ans in ("n", "no"):
        config["particles_enabled"] = False

    current = config.get("particles_layers") or []
    if virtual_layers:
        print(Ansi.info("\n可选轨道（虚拟层）："))
        for vl in virtual_layers:
            vid = vl['vid']
            gid = layer_to_group.get(vid, vid)
            mark = "✓" if vid in current else " "
            print(f"  [{mark}] vid={vid}  分组={gid + 1}  音色={vl.get('cat', '?')}  音符={len(vl.get('notes', []))}")
    print(Ansi.dim(f"当前参与粒子的轨道 vid: {current if current else '（无）'}"))
    print(Ansi.dim("输入要参与粒子的 vid（逗号/空格分隔）；all=全部，c=清空，回车=不修改"))
    sel = input(Ansi.prompt("参与粒子特效的轨道: ")).strip()
    if sel:
        low = sel.lower()
        if low in ("all", "*"):
            config["particles_layers"] = [vl['vid'] for vl in virtual_layers]
        elif low in ("c", "clear"):
            config["particles_layers"] = []
        else:
            vids = []
            for token in sel.replace(",", " ").split():
                try:
                    vids.append(int(token))
                except ValueError:
                    print(Ansi.error(f"忽略无效 vid: {token}"))
            valid = {vl['vid'] for vl in virtual_layers}
            missing = sorted(v for v in set(vids) if v not in valid)
            if missing:
                print(Ansi.error(f"以下 vid 不存在，已忽略: {missing}"))
            config["particles_layers"] = sorted(v for v in set(vids) if v in valid)

    trail = input(Ansi.prompt(f"拖尾长度 trail ({config.get('particles_trail', DEFAULT_TRAIL)}): ")).strip()
    if trail:
        try:
            config["particles_trail"] = max(2, min(256, int(trail)))
        except ValueError:
            print(Ansi.error("trail 必须是整数，未修改"))

    curve = input(Ansi.prompt(f"竖直起伏 curve arc/sine/line ({config.get('particles_curve', DEFAULT_CURVE)}): ")).strip().lower()
    if curve:
        if curve in CURVE_CHOICES:
            config["particles_curve"] = curve
        else:
            print(Ansi.error("curve 只能是 arc / sine / line，未修改"))

    height = input(Ansi.prompt(f"跳动最大高度 height ({config.get('particles_height', DEFAULT_HEIGHT)}): ")).strip()
    if height:
        try:
            config["particles_height"] = max(0.5, min(64.0, float(height)))
        except ValueError:
            print(Ansi.error("height 必须是数字，未修改"))

    color = input(Ansi.prompt(f"纯色 color（#RRGGBB 或 rainbow；回车=不修改，c=清空/自动）({config.get('particles_color') or '自动分配'}): ")).strip()
    if color:
        config["particles_color"] = "" if color.lower() == "c" else color

    gradient = input(Ansi.prompt(f"渐变 gradient（#RRGGBB,#RRGGBB 或 rainbow；回车=不修改，c=清空）({config.get('particles_gradient') or '无'}): ")).strip()
    if gradient:
        config["particles_gradient"] = "" if gradient.lower() == "c" else gradient

    linger = input(Ansi.prompt(f"末音符后停留 linger ({config.get('particles_linger', DEFAULT_LINGER)}，-1=常驻): ")).strip()
    if linger:
        try:
            config["particles_linger"] = max(-1, int(linger))
        except ValueError:
            print(Ansi.error("linger 必须是整数，未修改"))

    save_config(config)
    state = "启用" if config.get("particles_enabled") else "禁用"
    print(Ansi.success(f"粒子特效已{state}，参与粒子轨道 {len(config.get('particles_layers') or [])} 条"))
