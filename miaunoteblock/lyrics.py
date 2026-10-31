"""歌词导入与 /mpe text chain 粒子字幕生成。

生成器只在结构底部放一个（命令过长时多个）**脉冲**命令方块，写入一条

    /mpe text chain ~~~ x,y,z,start,duration,文字[;段内选项]|... 选项

由 MiauParticleEffects 模组按时间轴在指定位置显示歌词。
结构里不再需要整条中继器歌词链：方块占用、断链风险和生成时间都大幅下降。

坐标约定：
- 锚点一律写 ``~~~``（命令方块自身方块坐标），段里的 x/y/z 是相对锚点的偏移，
  所以结构整体平移/复制后无需改数据。
- ``start`` 是相对「命令方块执行那一刻」的绝对 tick（20 tick = 1 秒），
  已补上命令方块自身的执行延迟，并已对齐主音乐链的计时原点。
"""

import bisect
import json
import random
from typing import Dict, List, Optional, Tuple

from .console import ask_number
from .constants import Ansi, DIR_OFFSET, SIDE_AXIS, TEXT_TOWARDS_YAW
from .generation import chain_valid_ticks, event_offsets

DEFAULT_STREAM_SECONDS = 2.0
DEFAULT_STREAM_MAX_LEN = 18
DEFAULT_DURATION = 20
DEFAULT_ENTER = 5
DEFAULT_EXIT = 5
DEFAULT_SCALE = 10.0
DEFAULT_Y_OFFSET = 5
DEFAULT_CHAIN_Y_OFFSET = 1
DEFAULT_LEAD_DISTANCE = 16
DEFAULT_ANIM_IN = "typewriter:left"

CHAIN_ANCHOR = "~~~"
# 命令方块单条命令的长度上限（模组实测 32500，这里留出安全余量）
MAX_CHAIN_COMMAND_LEN = 32000
# 主音乐链每个音乐 tick 沿链条前进的格数（事件格 + 中继器各 1 格，均匀模式）
# 仅用于「超出主音乐链末端」时的外推近似，真实位置一律查 event_offsets
CHAIN_BLOCKS_PER_TICK = 2
# 每个音乐 tick 对应的游戏 tick 数（中继器 delay=1 = 2 游戏 tick），与中继器模式无关
GAME_TICKS_PER_MUSIC_TICK = 2
# 歌词命令方块的链轴位置：结构起点之前的格数
CHAIN_BLOCK_AXIS_BACK = 3
# 多个命令方块沿侧向排开时的间距
CHAIN_BLOCK_SIDE_GAP = 2

# 可选的入场/出场动画：类别 -> 样式（见 mpe_docs 的「歌词链」一节）
ANIMATION_STYLES = {
    "typewriter": ["left", "right", "center"],
    "wipe": ["left", "right", "up", "down"],
    "slide": ["up", "down", "left", "right"],
    "scale": ["shrink", "enlarge"],
    "zoom": ["in", "out"],
    "flip": ["horizontal", "vertical"],
    "rotate": ["roll", "yaw", "pitch"],
    "scatter": ["gather", "burst"],
    "bounce": ["drop", "rise"],
    "drop": ["down", "up", "left", "right"],
    "spacing": ["converge", "disperse"],
    "clarity": ["clear", "blur"],
}


# ── 歌词数据解析 ─────────────────────────────────────────────

def load_lyrics_json(path: str) -> List[Tuple[int, str, int]]:
    """读取编辑器导出的歌词 JSON（formatVersion 2）。

    文件结构：
        {
          "type": "noteblock-web-lyrics",
          "formatVersion": 2,
          "laneCount": 3,
          "tempo": 13,
          "lyrics": [
            {"tick": 92, "lane": 0, "text": "起", "duration": 4},
            ...
          ]
        }

    编辑器支持 3 个文本轨道（lane），本工具只识别 lane 0，
    其它轨道有文本时给出警告并忽略。
    返回 [(tick, 文字, 显示时长), ...]，按 tick 升序；时长为 0 表示使用全局设置。
    """
    # utf-8-sig：兼容 Windows 记事本 / PowerShell 写出的带 BOM 文件
    with open(path, 'r', encoding='utf-8-sig') as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 格式错误: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("根节点必须是 JSON 对象")
    lyrics = data.get("lyrics")
    if lyrics is None:
        raise ValueError("缺少 lyrics 字段")
    if isinstance(lyrics, dict):
        raise ValueError("这是旧版模板（lyrics 为对象），请用编辑器重新导出 formatVersion 2 文件")
    if not isinstance(lyrics, list):
        raise ValueError("lyrics 字段必须是数组（formatVersion 2）")

    entries: List[Tuple[int, str, int]] = []
    ignored_lanes = 0
    for item in lyrics:
        if not isinstance(item, dict):
            continue
        try:
            lane = int(item.get("lane") or 0)
        except (TypeError, ValueError):
            lane = 0
        text = str(item.get("text", "") or "").strip()
        if lane != 0:
            if text:
                ignored_lanes += 1
            continue
        try:
            tick = int(item.get("tick"))
        except (TypeError, ValueError):
            continue
        try:
            duration = int(item.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if not text:
            continue
        entries.append((tick, text, max(0, duration)))
    if ignored_lanes:
        print(Ansi.error(f"警告：lane 1/2 共 {ignored_lanes} 条文本，已忽略（本工具仅识别 lane 0）"))
    entries.sort(key=lambda item: item[0])
    return entries


def imported_entries(config: dict) -> List[Tuple[int, str, int]]:
    """从配置里取出已导入的歌词（[(tick, 文字, 时长), ...]，按 tick 升序）。"""
    raw = config.get("lyrics_import") or []
    if isinstance(raw, dict):  # 兼容旧的内存结构 {tick: {...}}
        raw = [dict(value, tick=key) for key, value in raw.items() if isinstance(value, dict)]
    entries: List[Tuple[int, str, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            tick = int(item.get("tick"))
        except (TypeError, ValueError):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            duration = int(item.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        entries.append((tick, text, max(0, duration)))
    entries.sort(key=lambda entry: entry[0])
    return entries


# ── 时间与分段 ───────────────────────────────────────────────

def stream_threshold_ticks(config: dict, tempo: float) -> int:
    """把「流式合并秒数」按歌曲速度换算成 tick 阈值。

    歌曲速度 tempo 的单位是 note/s（1 秒 = tempo 个 tick），
    相邻两次歌词事件间隔超过该阈值就视为两段，不再累积。
    """
    try:
        seconds = float(config.get("lyrics_stream_seconds", DEFAULT_STREAM_SECONDS))
    except (TypeError, ValueError):
        seconds = DEFAULT_STREAM_SECONDS
    try:
        rate = float(tempo)
    except (TypeError, ValueError):
        rate = 10.0
    if rate <= 0:
        rate = 10.0
    return max(1, int(round(seconds * rate)))


def build_stream_segments(entries: List[Tuple[int, str, int]], threshold_ticks: int, max_len: int):
    """提前分析歌词数据，把可以连读的事件合并成若干「流式段」。

    entries 为 [(tick, 文字, 时长), ...]（已按 tick 升序）。
    - 相邻事件间隔 > threshold_ticks：视为两段歌词，断段
    - 本段累积字数将超过 max_len：断段
    """
    segments = []
    current = []
    current_len = 0
    prev_tick = None
    for tick, text, duration in entries:
        if current:
            gap = tick - prev_tick
            if gap > threshold_ticks or current_len + len(text) > max_len:
                segments.append(current)
                current = []
                current_len = 0
        current.append((tick, text, duration))
        current_len += len(text)
        prev_tick = tick
    if current:
        segments.append(current)
    return segments


def _int_option(config: dict, key: str, default: int, minimum: int = None) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _float_option(config: dict, key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _random_offset(limit: float, rng: random.Random) -> float:
    """在 ±limit 内取一个 0.5 的整数倍（limit ≤ 0 时不偏移）。"""
    if limit <= 0:
        return 0.0
    steps = int(round(limit * 2))
    if steps <= 0:
        return 0.0
    return rng.randint(-steps, steps) / 2


def build_display_segments(entries: List[Tuple[int, str, int]], config: dict,
                           tempo: float = None) -> List[dict]:
    """把歌词条目转成「一条字幕」列表。

    每项为 {"tick", "text", "duration", "enter", "exit", "enter_override"}：
    - 关闭流式：每个条目一条字幕，时长用条目自带值（0 则用 lyrics_duration）。
    - 开启流式：相邻条目合并成一句完整歌词，入场时长占半段（配合 typewriter
      就是逐字打出），避免「后一个字把前一个字顶掉」的老问题。
    """
    global_duration = max(1, _int_option(config, "lyrics_duration", DEFAULT_DURATION, 1))
    enter = _int_option(config, "lyrics_enter", DEFAULT_ENTER, 0)
    exit_time = _int_option(config, "lyrics_exit", DEFAULT_EXIT, 0)
    clean = [(int(tick), str(text), max(0, int(duration)))
             for tick, text, duration in entries if str(text).strip()]
    clean.sort(key=lambda item: item[0])

    if not config.get("lyrics_stream"):
        return [
            {"tick": tick, "text": text,
             "duration": duration if duration > 0 else global_duration,
             "enter": enter, "exit": exit_time, "enter_override": False}
            for tick, text, duration in clean
        ]

    max_len = max(1, _int_option(config, "lyrics_stream_max_len", DEFAULT_STREAM_MAX_LEN, 1))
    threshold = stream_threshold_ticks(config, tempo)
    segments: List[dict] = []
    for group in build_stream_segments(clean, threshold, max_len):
        text = "".join(item[1] for item in group)
        first_tick = group[0][0]
        last_tick, _, last_duration = group[-1]
        span = last_tick + (last_duration if last_duration > 0 else global_duration) - first_tick
        span = max(1, span)
        typing = max(1, span // 2)
        segments.append({
            "tick": first_tick,
            "text": text,
            "duration": max(1, span - typing),
            "enter": typing,
            "exit": exit_time,
            # 流式段的入场时长要按段长单独覆盖（配合 typewriter 才有逐字效果）
            "enter_override": True,
        })
    return segments


# ── 命令文本拼装 ─────────────────────────────────────────────

# 段内不能出现 | ; , 与空格（空格要写成 _），否则会把命令拆错
_TEXT_REPLACEMENTS = {
    "|": "丨",
    ";": "；",
    ",": "，",
    " ": "_",
    "\u3000": "_",
    "\n": "",
    "\r": "",
    "\t": "",
}


def sanitize_text(text: str) -> str:
    """把文字转成 /mpe text chain 允许的形式。"""
    return "".join(_TEXT_REPLACEMENTS.get(ch, ch) for ch in str(text))


def _fmt_number(value: float) -> str:
    """整数不带小数点，其余保留最多 3 位小数（用于相对坐标）。"""
    rounded = round(float(value), 3)
    if abs(rounded - round(rounded)) < 1e-6:
        return str(int(round(rounded)))
    return f"{rounded:.3f}".rstrip("0").rstrip(".")


def _global_options(config: dict, direction: str) -> str:
    color = str(config.get("lyrics_color") or "white").replace(" ", "")
    try:
        scale = float(config.get("lyrics_scale", DEFAULT_SCALE))
    except (TypeError, ValueError):
        scale = DEFAULT_SCALE
    scale = max(0.05, min(64.0, scale))
    enter = _int_option(config, "lyrics_enter", DEFAULT_ENTER, 0)
    exit_time = _int_option(config, "lyrics_exit", DEFAULT_EXIT, 0)
    yaw = TEXT_TOWARDS_YAW.get(direction, 0)
    options = [f"color={color}", f"scale={_fmt_number(scale)}", f"enter={enter}",
               f"exit={exit_time}", f"towards={yaw},0,0", "fade=both", "force=true"]
    anim_in = config.get("lyrics_anim_in")
    anim_in = DEFAULT_ANIM_IN if anim_in is None else str(anim_in).replace(" ", "").strip()
    anim_out = str(config.get("lyrics_anim_out") or "").replace(" ", "").strip()
    if anim_in:
        options.append(f"in={anim_in}")
    if anim_out:
        options.append(f"out={anim_out}")
    return " ".join(options)


def _segment_string(segment: dict, start_tick: int,
                    block_pos: Tuple[int, int, int]) -> str:
    """拼一段：x,y,z,start,duration,文字[;段内选项]，坐标相对命令方块自身。"""
    px, py, pz = segment["pos"]
    bx, by, bz = block_pos
    body = (f"{_fmt_number(px - bx)},{_fmt_number(py - by)},{_fmt_number(pz - bz)},"
            f"{int(start_tick)},{int(segment['duration'])},{sanitize_text(segment['text'])}")
    if segment.get("enter_override"):
        body += f";enter={int(segment['enter'])}"
    return body


def _pulse_command_block(pos: Tuple[int, int, int], command: str) -> str:
    """朝上的脉冲命令方块（被相邻红石块激活，下一 tick 执行）。"""
    x, y, z = pos
    payload = json.dumps(command, ensure_ascii=False)
    return f"setblock {x} {y} {z} minecraft:command_block[facing=up]{{Command:{payload}}}"


def _origin_tick(virtual_layers: List[dict], max_tick: int) -> int:
    """主音乐链的第一个 tick（歌词链与它共用计时原点）。"""
    return chain_valid_ticks(virtual_layers, max_tick)[0]


def _axis_offset_at(offsets: List[Tuple[int, int]], ticks: List[int], tick: int) -> float:
    """tick 时刻「当前激活位置」相对链起点的格数。

    落在事件格上时取精确值；落在两个事件格之间时按 tick 线性插值；
    超出主音乐链末端时按每音乐 tick 2 格继续外推（与均匀模式一致）。
    """
    if not offsets:
        return 0.0
    index = bisect.bisect_right(ticks, tick) - 1
    if index < 0:
        return float(offsets[0][1])
    tick0, offset0 = offsets[index]
    if index >= len(offsets) - 1:
        return float(offset0 + CHAIN_BLOCKS_PER_TICK * (tick - tick0))
    tick1, offset1 = offsets[index + 1]
    span = tick1 - tick0
    if span <= 0:
        return float(offset0)
    return offset0 + (offset1 - offset0) * (tick - tick0) / span


def _display_anchor(config: dict, group_offset_dict: Dict[int, Tuple[int, int]]) -> Tuple[int, int]:
    """文字锚点：主轨道上方（非平铺用核心组，平铺用结构侧向中心）。"""
    style = config.get("layout_style", "flat")
    master = config.get("master_group")
    if style != "flat" and master is not None and master in group_offset_dict:
        return group_offset_dict[master]
    sides = [offset[1] for offset in group_offset_dict.values()] or [0]
    return 0, (min(sides) + max(sides)) // 2


# ── 歌词链构建（供生成流程与测试复用）────────────────────────

def build_lyrics_chain(
    virtual_layers: List[dict],
    group_offset_dict: Dict[int, Tuple[int, int]],
    start: Tuple[int, int, int],
    direction: str,
    max_tick: int,
    config: dict,
    struct_bottom_y: int = None,
    tempo: float = None,
    rng: random.Random = None,
) -> Optional[dict]:
    """构建 /mpe text chain 歌词链。

    返回 {"commands", "blocks", "activations", "chain_y", "segments",
    "batches", "end_tick"}；未导入歌词或没有落在范围内的歌词时返回 None。
    「导入歌词」即等于启用歌词功能。

    - 命令方块放在整个结构下方（``lyrics_chain_y_offset`` 层）起点之前，
      每批一个，彼此沿侧向排开；
    - 每个命令方块旁边都有红石块作为激活源，由调用方并入启动链
      （返回值 ``activations`` 的顺序必须与启动链中的顺序一致）。
    - 文字沿链条跟随时按「当前激活中继器的位置 + lyrics_lead_distance」定位，
      保证文字离跟随拍摄的玩家有一段距离，不会被贴脸。

    rng 只用于文字位置的随机抖动（``lyrics_jitter_*``），传固定种子可复现。
    """
    entries = imported_entries(config)
    if not entries:
        print(Ansi.error("没有有效歌词，跳过歌词生成"))
        return None

    origin_tick = _origin_tick(virtual_layers, max_tick)
    early = [tick for tick, _, _ in entries if tick < origin_tick]
    if early:
        print(Ansi.error(f"警告：{len(early)} 条歌词早于音乐起点 tick {origin_tick}，已跳过"))
    entries = [item for item in entries if item[0] >= origin_tick]
    if not entries:
        print(Ansi.error("没有落在生成范围内的歌词，跳过歌词生成"))
        return None

    last_lyric_tick = max(item[0] for item in entries)
    end_tick = max(max_tick, last_lyric_tick)
    if last_lyric_tick > max_tick:
        print(Ansi.info(f"歌词超出歌曲长度（最后一条 tick {last_lyric_tick}），"
                        f"歌词链自动延长到 tick {end_tick}"))

    start_x, start_y, start_z = start
    dx, _, dz = DIR_OFFSET[direction]
    side_axis = SIDE_AXIS[direction]
    step = dx if side_axis == "z" else dz
    chain_axis_start = start_x if side_axis == "z" else start_z
    base_side = start_z if side_axis == "z" else start_x

    y_off, side_off = _display_anchor(config, group_offset_dict)
    display_y = start_y + y_off + _int_option(config, "lyrics_y_offset", DEFAULT_Y_OFFSET)
    display_side = base_side + side_off + int(config.get("lyrics_side_offset") or 0)

    lead = _int_option(config, "lyrics_lead_distance", DEFAULT_LEAD_DISTANCE)
    jitter = (_float_option(config, "lyrics_jitter_x", 0.0),
              _float_option(config, "lyrics_jitter_y", 0.0),
              _float_option(config, "lyrics_jitter_z", 0.0))
    if rng is None:
        rng = random.Random()

    follow = config.get("lyrics_follow", True)
    fixed_tick = origin_tick + (end_tick - origin_tick) // 2

    # 主音乐链的真实布局（受中继器模式影响），文字位置必须按它来定位
    layout = event_offsets(chain_valid_ticks(virtual_layers, max_tick),
                           config.get("uniform_repeater_mode", False))
    layout_ticks = [tick for tick, _ in layout]

    def display_pos(tick: int) -> Tuple[float, float, float]:
        """文字中心的世界坐标 = 当前激活位置 + 保持距离 + 随机偏移。

        激活位置按真实链条布局查表（合并/均匀中继器模式都适用），
        跟随模式随音乐沿链条前进，固定模式停在结构中点。
        """
        anchor_tick = tick if follow else fixed_tick
        axis = (chain_axis_start
                + _axis_offset_at(layout, layout_ticks, anchor_tick) * step
                + lead * step)
        if side_axis == "z":
            x, y, z = axis, display_y, display_side
        else:
            x, y, z = display_side, display_y, axis
        return (x + _random_offset(jitter[0], rng),
                y + _random_offset(jitter[1], rng),
                z + _random_offset(jitter[2], rng))

    if struct_bottom_y is None:
        struct_bottom_y = min(start_y + offset[0] for offset in group_offset_dict.values()) - 2
    chain_offset = _int_option(config, "lyrics_chain_y_offset", DEFAULT_CHAIN_Y_OFFSET, 1)
    chain_y = struct_bottom_y - chain_offset

    segments = build_display_segments(entries, config, tempo)
    for segment in segments:
        segment["pos"] = display_pos(segment["tick"])
    if not segments:
        print(Ansi.error("没有可显示的歌词，跳过歌词生成"))
        return None

    options = _global_options(config, direction)
    overhead = len(f"/mpe text chain {CHAIN_ANCHOR} ") + len(options) + 48  # 留出 id= 的余量

    block_axis = chain_axis_start - CHAIN_BLOCK_AXIS_BACK * step

    def block_position(index: int) -> Tuple[int, int, int]:
        """第 index 个命令方块的坐标（沿侧向依次排开）。"""
        side_pos = base_side + CHAIN_BLOCK_SIDE_GAP * index
        if side_axis == "z":
            return block_axis, chain_y, side_pos
        return side_pos, chain_y, block_axis

    # 先按长度分批，再按批序号补上 start 偏移（批序号会影响与启动链的对齐）
    batches: List[List[dict]] = []
    current: List[dict] = []
    length = 0
    probe_pos = block_position(0)
    # start 用 6 位数字估算，宁可高估也不要让真实命令超限
    for segment in segments:
        piece_len = len(_segment_string(segment, 999999, probe_pos)) + (1 if current else 0)
        if current and length + piece_len + overhead > MAX_CHAIN_COMMAND_LEN:
            batches.append(current)
            current = []
            length = 0
            piece_len = len(_segment_string(segment, 999999, probe_pos))
        current.append(segment)
        length += piece_len
    if current:
        batches.append(current)

    batch_count = len(batches)
    commands: List[str] = []
    blocks: List[Tuple[int, int, int]] = []
    activations: List[Tuple[int, int, int]] = []
    longest = 0

    for index, batch in enumerate(batches):
        block_pos = block_position(index)

        # 时间对齐：主音乐链 1 个音乐 tick = 2 游戏 tick；
        # 本批命令方块在第 index+1 环被激活，在 t0+index+2 执行，
        # 而第一个音乐组要等全部歌词红石块放完后才轮到，故整体补偿 batch_count+1-index。
        bias = batch_count + 1 - index
        pieces = [_segment_string(
            segment,
            GAME_TICKS_PER_MUSIC_TICK * (segment["tick"] - origin_tick) + bias,
            block_pos)
            for segment in batch]
        command = f"/mpe text chain {CHAIN_ANCHOR} " + "|".join(pieces)
        if batch_count > 1:
            # 同一命令方块多次执行会重置重播，分批时必须用不同 id 区分
            command += f" id=lyr_{block_pos[0]}_{block_pos[1]}_{block_pos[2]}"
        command += " " + options

        commands.append(_pulse_command_block(block_pos, command))
        blocks.append(block_pos)
        activations.append((block_pos[0] + dx, block_pos[1], block_pos[2] + dz))
        longest = max(longest, len(command))

    mode = "流式合并" if config.get("lyrics_stream") else "逐条显示"
    print(Ansi.success(f"歌词链已添加：{len(segments)} 条字幕（{mode}）-> "
                       f"{batch_count} 个脉冲命令方块，链层 Y={chain_y}"))
    print(Ansi.dim(f"最长命令 {longest} 字符（上限 {MAX_CHAIN_COMMAND_LEN}）；"
                   f"位置：{'跟随前进' if follow else '固定在结构中点'}；"
                   f"时间原点 tick {origin_tick}"))
    return {
        "commands": commands,
        "blocks": blocks,
        "activations": activations,
        "chain_y": chain_y,
        "segments": segments,
        "batches": batch_count,
        "end_tick": end_tick,
    }


# ── 歌词设置 UI ──────────────────────────────────────────────

def _ask(label: str, current, cast, minimum=None, maximum=None):
    """歌词菜单里的数值输入（缩进两级，统一走 console.ask_number）。"""
    return ask_number(label, current, cast, minimum, maximum, indent="  ")


def _lyrics_status(config: dict) -> str:
    entries = imported_entries(config)
    if not entries:
        return Ansi.dim("未导入歌词（导入即启用歌词字幕）")
    stream = "开" if config.get("lyrics_stream") else "关"
    follow = "跟随" if config.get("lyrics_follow", True) else "固定"
    return (f"{len(entries)} 条歌词 · 流式={stream} · "
            f"颜色={config.get('lyrics_color', 'white')} · 字号={config.get('lyrics_scale')} · "
            f"保持距离={config.get('lyrics_lead_distance')} · "
            f"随机={_jitter_text(config)} · 位置={follow}")


def import_lyrics_json(config: dict, tempo: float = None) -> None:
    """导入歌词 JSON（导入即启用歌词功能）。"""
    print(Ansi.title("\n=== 导入歌词 JSON ==="))
    print(Ansi.dim("支持编辑器导出的 noteblock-web-lyrics（formatVersion 2），只读取 lane 0。"))
    current = imported_entries(config)
    if current:
        print(Ansi.info(f"当前已导入 {len(current)} 条歌词"))
    raw = input(Ansi.prompt("JSON 文件路径（回车=取消）: ")).strip().strip('"').strip("'")
    if not raw:
        print(Ansi.info("已取消"))
        return
    try:
        entries = load_lyrics_json(raw)
    except FileNotFoundError:
        print(Ansi.error("文件不存在"))
        return
    except ValueError as exc:
        print(Ansi.error(f"解析失败: {exc}"))
        return
    except OSError as exc:
        print(Ansi.error(f"读取失败: {exc}"))
        return
    if not entries:
        print(Ansi.error("文件中没有有效歌词"))
        return

    config["lyrics_import"] = [
        {"tick": tick, "text": text, "duration": duration}
        for tick, text, duration in entries
    ]
    print(Ansi.success(f"已导入 {len(entries)} 条歌词（首条 tick {entries[0][0]}，"
                       f"末条 tick {entries[-1][0]}）"))
    if config.get("lyrics_stream"):
        threshold = stream_threshold_ticks(config, tempo)
        segments = build_stream_segments(
            entries, threshold,
            _int_option(config, "lyrics_stream_max_len", DEFAULT_STREAM_MAX_LEN, 1))
        print(Ansi.info(f"流式歌词：每 {config.get('lyrics_stream_seconds')} 秒（≈{threshold} tick）"
                        f"内的文字会合并成 {len(segments)} 句"))
    print(Ansi.dim("配色 / 字号 / 时长等可在 [3] 显示参数里调整。"))


def clear_lyrics(config: dict) -> None:
    if not imported_entries(config):
        print(Ansi.info("当前没有歌词，无需清空"))
        return
    config["lyrics_import"] = []
    print(Ansi.success("已清空歌词，歌词功能已关闭"))


def _choose_animation(config: dict, key: str, label: str) -> None:
    """用编号选择入场/出场动画，写回 config[key]（`类别:样式`，空 = 不使用）。"""
    categories = list(ANIMATION_STYLES)
    current = str(config.get(key) or "")
    print(Ansi.title(f"\n=== {label}（当前 {current or '无'}）==="))
    if key == "lyrics_anim_out":
        print(Ansi.dim("  留空时，出场动画自动使用入场动画的镜像。"))
    print("  [0] 不使用")
    for index, name in enumerate(categories, start=1):
        styles = ANIMATION_STYLES[name]
        print(f"  [{index}] {name}  ({'/'.join(styles)})")

    raw = input(Ansi.prompt(f"类别编号 (0-{len(categories)}，回车不改): ")).strip()
    if not raw:
        return
    if not raw.isdigit() or not 0 <= int(raw) <= len(categories):
        print(Ansi.error("编号无效，未修改"))
        return
    if int(raw) == 0:
        config[key] = ""
        print(Ansi.success(f"{label} 已设为不使用"))
        return

    category = categories[int(raw) - 1]
    styles = ANIMATION_STYLES[category]
    if len(styles) == 1:
        config[key] = f"{category}:{styles[0]}"
        print(Ansi.success(f"{label}：{config[key]}"))
        return

    print("  样式：" + " / ".join(f"{i}={style}" for i, style in enumerate(styles, start=1)))
    raw_style = input(Ansi.prompt(f"  样式编号 (1-{len(styles)}，回车=取消): ")).strip()
    if not raw_style:
        print(Ansi.info("已取消"))
        return
    if not raw_style.isdigit() or not 1 <= int(raw_style) <= len(styles):
        print(Ansi.error("样式编号无效，未修改"))
        return
    config[key] = f"{category}:{styles[int(raw_style) - 1]}"
    print(Ansi.success(f"{label}：{config[key]}"))


def _jitter_text(config: dict) -> str:
    ranges = [config.get("lyrics_jitter_x"), config.get("lyrics_jitter_y"),
              config.get("lyrics_jitter_z")]
    if not any(float(value or 0) > 0 for value in ranges):
        return "关闭"
    return "±" + "/".join(str(value) for value in ranges)


def edit_lyrics_display_settings(config: dict) -> None:
    """歌词显示参数：外观 / 时长 / 动画 / 位置 / 流式。"""
    while True:
        style = config.get("layout_style", "flat")
        master = config.get("master_group")
        print(Ansi.title("\n=== 歌词显示参数 ==="))
        print(f"  颜色 {config.get('lyrics_color')} · 字号 {config.get('lyrics_scale')} · "
              f"时长 {config.get('lyrics_duration')} · 入场 {config.get('lyrics_enter')} · "
              f"出场 {config.get('lyrics_exit')}")
        print(f"  入场动画 {config.get('lyrics_anim_in') or '无'} · "
              f"出场动画 {config.get('lyrics_anim_out') or '入场镜像'}")
        print(f"  保持距离 {config.get('lyrics_lead_distance')} · 高度 {config.get('lyrics_y_offset')} · "
              f"侧向偏移 {config.get('lyrics_side_offset') if config.get('lyrics_side_offset') is not None else '无'} · "
              f"随机 {_jitter_text(config)}")
        print(f"  {'跟随前进' if config.get('lyrics_follow', True) else '固定在结构中点'} · "
              f"链层 -{config.get('lyrics_chain_y_offset')}")
        print(f"  流式合并 {'开启' if config.get('lyrics_stream') else '关闭'} · "
              f"{config.get('lyrics_stream_seconds')} 秒 / 上限 {config.get('lyrics_stream_max_len')} 字")
        if style != "flat" and master is not None:
            print(Ansi.dim(f"  文字锚定在核心组（组{master + 1}）上方"))
        print("  [1] 外观（颜色 / 字号）")
        print("  [2] 时长（显示时长 / 入场 / 出场）")
        print("  [3] 入场动画（编号选择）")
        print("  [4] 出场动画（编号选择）")
        print("  [5] 位置（保持距离 / 高度 / 侧向 / 随机范围 / 跟随 / 链层）")
        print("  [6] 流式合并（开关 / 秒数 / 单段字数上限）")
        print("  [0] 返回")
        choice = input(Ansi.prompt("选择: ")).strip()
        if choice == "1":
            color = input(Ansi.prompt(f"  颜色（{config.get('lyrics_color')}，如 #FFD700 / white / rainbow）: ")).strip()
            if color:
                config["lyrics_color"] = color.replace(" ", "")
            config["lyrics_scale"] = _ask(
                "字号 scale", config.get("lyrics_scale"), float, 0.05, 64.0)
        elif choice == "2":
            config["lyrics_duration"] = _ask(
                "显示时长 duration", config.get("lyrics_duration"), int, 1)
            config["lyrics_enter"] = _ask(
                "入场时长 enter", config.get("lyrics_enter"), int, 0)
            config["lyrics_exit"] = _ask(
                "出场时长 exit", config.get("lyrics_exit"), int, 0)
        elif choice == "3":
            _choose_animation(config, "lyrics_anim_in", "入场动画")
        elif choice == "4":
            _choose_animation(config, "lyrics_anim_out", "出场动画")
        elif choice == "5":
            config["lyrics_lead_distance"] = _ask(
                "文字相对激活位置的前方距离（越大离玩家越远）",
                config.get("lyrics_lead_distance"), int)
            config["lyrics_y_offset"] = _ask(
                "相对主轨道高度", config.get("lyrics_y_offset"), int)
            side = config.get("lyrics_side_offset")
            config["lyrics_side_offset"] = _ask(
                "横置偏移（0=结构中央）", 0 if side is None else side, int)
            print(Ansi.dim("  随机范围：每段文字在 ±N 方块内随机抖动，0 = 关闭（0.5 步进）"))
            config["lyrics_jitter_x"] = _ask(
                "X 随机范围", config.get("lyrics_jitter_x"), float, 0.0)
            config["lyrics_jitter_y"] = _ask(
                "Y 随机范围", config.get("lyrics_jitter_y"), float, 0.0)
            config["lyrics_jitter_z"] = _ask(
                "Z 随机范围", config.get("lyrics_jitter_z"), float, 0.0)
            follow = input(Ansi.prompt(
                f"  文字是否随音乐前进 (y/n，回车={'是' if config.get('lyrics_follow', True) else '否'}): ")).strip().lower()
            if follow in ("y", "yes"):
                config["lyrics_follow"] = True
            elif follow in ("n", "no"):
                config["lyrics_follow"] = False
            config["lyrics_chain_y_offset"] = _ask(
                "命令方块距结构底部层数（越大越靠下）",
                config.get("lyrics_chain_y_offset"), int, 1)
        elif choice == "6":
            current = config.get("lyrics_stream")
            ans = input(Ansi.prompt(
                f"  是否合并连续歌词 (y/n，回车={'保持开启' if current else '保持关闭'}): ")).strip().lower()
            if ans in ("y", "yes"):
                config["lyrics_stream"] = True
            elif ans in ("n", "no"):
                config["lyrics_stream"] = False
            if config.get("lyrics_stream"):
                config["lyrics_stream_seconds"] = _ask(
                    "合并时间阈值（秒）", config.get("lyrics_stream_seconds"), float, 0.01)
                config["lyrics_stream_max_len"] = _ask(
                    "单段字数上限", config.get("lyrics_stream_max_len"), int, 1)
        elif choice in ("", "0", "q"):
            return
        else:
            print(Ansi.error("无效选项"))


def edit_lyrics_menu(config: dict, tempo: float = None) -> None:
    """歌词子菜单：导入 / 清空 / 显示参数。"""
    while True:
        print(Ansi.title("\n=== 歌词字幕（/mpe text chain）==="))
        print(f"  {_lyrics_status(config)}")
        print("  [1] 导入歌词 JSON")
        print("  [2] 清空歌词")
        print("  [3] 显示参数")
        print("  [0] 返回")
        choice = input(Ansi.prompt("选择: ")).strip()
        if choice == "1":
            import_lyrics_json(config, tempo)
        elif choice == "2":
            clear_lyrics(config)
        elif choice == "3":
            edit_lyrics_display_settings(config)
        elif choice in ("", "0", "q"):
            return
        else:
            print(Ansi.error("无效选项"))
