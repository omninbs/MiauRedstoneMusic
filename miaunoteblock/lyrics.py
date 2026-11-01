"""歌词导入与 /mpe text chain 逐字粒子字幕生成。

生成器只在结构底部放一个（命令过长时多个）**脉冲**命令方块，写入一条

    /mpe text chain ~~~ x,y,z,start,0,文字;chars=0:4,2:4,...|... 选项

模组按 ``chars=`` 时间表把每个字一个个显示出来（逐字模式）。
一份歌词先按**长音**（``duration > 断句阈值``）切成句子，一句 = 一个段，
整首歌仍然只用一条命令。

坐标约定：
- 锚点一律写 ``~~~``（命令方块自身方块坐标），段里的 x/y/z 是相对锚点的偏移，
  所以结构整体平移/复制后无需改数据。
- ``start`` 是相对「命令方块执行那一刻」的绝对 tick（20 tick = 1 秒），
  已补上命令方块自身的执行延迟，并已对齐主音乐链的计时原点。
- ``chars=`` 里的 ``字start`` / ``字duration`` 也换算成游戏 tick：
  歌词数据的一个 NBS tick = 2 游戏 tick（与主音乐链的计时一致）。
"""

import bisect
import json
import random
from typing import Dict, List, Optional, Sequence, Tuple

from .console import ask_number
from .constants import Ansi, DIR_OFFSET, SIDE_AXIS, TEXT_TOWARDS_YAW
from .generation import chain_valid_ticks, event_offsets

# 断句阈值（与歌词 duration 同单位：NBS tick）。duration 严格大于它就断句。
DEFAULT_THRESHOLD = 4
# 单句字数上限：达到这个字数也断句（0 = 不限）。与长音断句叠加生效。
DEFAULT_MAX_CHARS = 18
# 歌词相对音乐**提前**显示多少歌曲 tick（内部换算成游戏 tick，0 = 不提前）
DEFAULT_START_LEAD = 4
DEFAULT_ENTER = 0          # 每个字的入场动画时长（tick），逐字模式下建议 0~2
DEFAULT_EXIT = 3           # 每个字的退场动画时长（tick）
DEFAULT_SCALE = 10.0
DEFAULT_Y_OFFSET = 5
DEFAULT_CHAIN_Y_OFFSET = 1
DEFAULT_LEAD_DISTANCE = 16
DEFAULT_ANIM_IN = "scale:enlarge"
# 逐字模式下与 chars= 时间表冲突、模组会直接报错的动画类别
FORBIDDEN_IN_CHAR_MODE = ("typewriter", "wipe", "spacing", "scatter")
# 「标点断句」可选规则使用的标点（默认关闭）
BREAK_PUNCTUATION = "，。！？、；：,.!?;:…—～"

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

# 可选的入场/出场动画：类别 -> 样式（见 mpe_docs 的「歌词链」一节）。
# 逐字模式下 typewriter / wipe / spacing / scatter 与 chars= 时间表冲突，
# 模组会直接报错，因此不提供给用户选择。
ANIMATION_STYLES = {
    "slide": ["up", "down", "left", "right"],
    "scale": ["shrink", "enlarge"],
    "zoom": ["in", "out"],
    "flip": ["horizontal", "vertical"],
    "rotate": ["roll", "yaw", "pitch"],
    "bounce": ["drop", "rise"],
    "drop": ["down", "up", "left", "right"],
    "clarity": ["clear", "blur"],
}

# §x / &x 颜色码里 x 的合法取值
_COLOR_CODE_CHARS = set("0123456789abcdefklmnorABCDEFKLMNOR")


# ── 歌词数据解析 ─────────────────────────────────────────────

def _is_color_code(text: str, index: int) -> bool:
    """text[index] 起是否是一个颜色码（§x / &x）。"""
    return (text[index] in "§&" and index + 1 < len(text)
            and text[index + 1] in _COLOR_CODE_CHARS)


def strip_color_codes(text: str) -> str:
    """去掉颜色码，返回纯文字（用于字数判断与标点判断）。"""
    out = []
    index = 0
    while index < len(text):
        if _is_color_code(text, index):
            index += 2
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


def visible_length(text: str) -> int:
    """可见字符数：``§x`` / ``&x`` 颜色码不算，空格算一个。"""
    return len(strip_color_codes(text))


def validate_entries(entries: Sequence[Tuple[int, str, int]]) -> List[str]:
    """逐字模式的输入校验，返回问题列表（空 = 合法）。

    要求：一个字 = 一条记录（``duration ≥ 1``，``tick`` 非递减）。
    多字条目无法给出可信的逐字时间表，直接报错让用户改歌词。
    """
    problems: List[str] = []
    previous = None
    for index, (tick, text, duration) in enumerate(entries, start=1):
        label = f"第 {index} 条（tick {tick}，「{text}」）"
        if tick < 0:
            problems.append(f"{label} 的 tick 为负")
        if previous is not None and tick < previous:
            problems.append(f"{label} 的 tick 小于前一条（start 必须非递减）")
        previous = tick
        count = visible_length(text)
        if count != 1:
            problems.append(f"{label} 有 {count} 个可见字符，逐字模式要求一字一条记录")
        if duration < 1:
            problems.append(f"{label} 的 duration 为 {duration}，必须 ≥ 1（至少唱 1 tick）")
    return problems


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
    返回 [(tick, 文字, 时长), ...]，按 tick 升序。

    逐字模式要求**一字一条记录**（空格也算一个字），``duration ≥ 1``；
    不满足时抛出 :class:`ValueError`，并在信息里列出前几条问题。
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
        text = _clean_text(item.get("text", ""))
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
        entries.append((tick, text, duration))
    if ignored_lanes:
        print(Ansi.error(f"警告：lane 1/2 共 {ignored_lanes} 条文本，已忽略（本工具仅识别 lane 0）"))
    entries.sort(key=lambda item: item[0])
    problems = validate_entries(entries)
    if problems:
        raise ValueError(_format_problems(problems))
    return entries


def _clean_text(raw) -> str:
    """去掉换行/制表符，但**保留空格**（空格在逐字模式里也算一个字）。"""
    return str(raw or "").replace("\n", "").replace("\r", "").replace("\t", "")


def _format_problems(problems: List[str]) -> str:
    shown = problems[:5]
    more = f"（还有 {len(problems) - len(shown)} 条）" if len(problems) > len(shown) else ""
    return "歌词数据不符合逐字模式要求：" + "；".join(shown) + more


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
        text = _clean_text(item.get("text"))
        if not text:
            continue
        try:
            duration = int(item.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        entries.append((tick, text, duration))
    entries.sort(key=lambda entry: entry[0])
    return entries


# ── 时间与分段 ───────────────────────────────────────────────

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


def break_sentences(entries: Sequence[Tuple[int, str, int]], threshold: int,
                    punctuation: bool = False,
                    max_chars: int = 0) -> List[List[Tuple[int, str, int]]]:
    """把逐字歌词切成句子（长音规则 + 可选的字数上限）。

    - ``duration > threshold`` 的字是**本句最后一个字**（断句发生在长音之后）；
    - ``punctuation=True`` 时标点（``BREAK_PUNCTUATION``）也作为句末，默认关闭；
    - ``max_chars > 0`` 时，句子累计到这个可见字数后当前字也作为句末
      （``max_chars = 0`` 表示不限字数）；
    - 长音字的 duration 不截断，整句会等它唱完才退场。
    """
    sentences: List[List[Tuple[int, str, int]]] = []
    current: List[Tuple[int, str, int]] = []
    count = 0
    for entry in entries:
        current.append(entry)
        count += max(1, visible_length(entry[1]))
        _, text, duration = entry
        plain = strip_color_codes(text)
        ends_sentence = duration > threshold or (
            punctuation and bool(plain) and plain[0] in BREAK_PUNCTUATION)
        if not ends_sentence and max_chars > 0 and count >= max_chars:
            ends_sentence = True
        if ends_sentence:
            sentences.append(current)
            current = []
            count = 0
    if current:
        sentences.append(current)
    return sentences


def build_char_segments(entries: Sequence[Tuple[int, str, int]], config: dict) -> List[dict]:
    """把逐字歌词条目转成 ``/mpe text chain`` 的段列表（一个段 = 一句歌词）。

    每项为 ``{"tick", "text", "chars", "count", "duration", "span"}``：

    - ``chars`` = ``[(字start, 字duration), ...]``，已换算成游戏 tick，
      ``字start`` 相对段 ``start``（首字 0），``字duration ≥ 1``；
    - ``duration`` 恒为 0：逐字模式让模组按时间表推导整句生命周期；
    - ``span`` = ``max(字start + 字duration)``，便于日志与测试。
    """
    threshold = _int_option(config, "lyrics_char_threshold", DEFAULT_THRESHOLD, 0)
    max_chars = _int_option(config, "lyrics_max_chars", DEFAULT_MAX_CHARS, 0)
    punctuation = bool(config.get("lyrics_break_on_punctuation", False))
    segments: List[dict] = []
    for sentence in break_sentences(entries, threshold, punctuation, max_chars):
        first_tick = sentence[0][0]
        chars = []
        for tick, _, duration in sentence:
            rel = max(0, tick - first_tick)
            chars.append((GAME_TICKS_PER_MUSIC_TICK * rel,
                          GAME_TICKS_PER_MUSIC_TICK * max(1, duration)))
        segments.append({
            "tick": first_tick,
            "text": "".join(sanitize_text(item[1]) for item in sentence),
            "chars": chars,
            "count": len(sentence),
            "duration": 0,
            "span": max(start + length for start, length in chars),
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


def _check_animation(anim: str, label: str) -> str:
    """逐字模式会拒绝与 chars= 冲突的动画，这里提前剔除并提示。"""
    kept = []
    dropped = []
    for item in anim.split(","):
        item = item.strip()
        if not item:
            continue
        if item.split(":")[0] in FORBIDDEN_IN_CHAR_MODE:
            dropped.append(item)
        else:
            kept.append(item)
    if dropped:
        print(Ansi.error(f"警告：逐字模式不支持{label} {'、'.join(dropped)}（模组会报错），已忽略"))
    return ",".join(kept)


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
    anim_in = _check_animation(anim_in, "入场动画")
    anim_out = _check_animation(anim_out, "出场动画")
    if anim_in:
        options.append(f"in={anim_in}")
    if anim_out:
        options.append(f"out={anim_out}")
    return " ".join(options)


def _chars_string(chars: Sequence[Tuple[int, int]]) -> str:
    """``chars=`` 时间表：``字start:字duration`` 用逗号连接。"""
    return ",".join(f"{int(start)}:{int(duration)}" for start, duration in chars)


def _segment_string(segment: dict, start_tick: int,
                    block_pos: Tuple[int, int, int]) -> str:
    """拼一段：x,y,z,start,0,文字;chars=...（坐标相对命令方块自身）。

    逐字模式下段 ``duration`` 固定写 0，由模组按时间表推导整句生命周期。
    """
    px, py, pz = segment["pos"]
    bx, by, bz = block_pos
    return (f"{_fmt_number(px - bx)},{_fmt_number(py - by)},{_fmt_number(pz - bz)},"
            f"{int(start_tick)},0,{segment['text']}"
            f";chars={_chars_string(segment['chars'])}")


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
    rng: random.Random = None,
) -> Optional[dict]:
    """构建逐字模式下的 /mpe text chain 歌词链。

    返回 {"commands", "blocks", "activations", "chain_y", "segments",
    "batches", "end_tick"}；未导入歌词、歌词不合法或没有落在范围内的歌词时返回 None。
    「导入歌词」即等于启用歌词功能。

    - 歌词先按长音（``duration > lyrics_char_threshold``）断句，一句 = 一个段，
      段内每个字带自己的 ``chars=`` 时间表；
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

    problems = validate_entries(entries)
    if problems:
        print(Ansi.error(_format_problems(problems)))
        print(Ansi.dim("逐字模式要求一字一条记录（空格也算一个字）且 duration ≥ 1；"
                       "请在歌词编辑器里改好后重新导入。"))
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

    threshold = _int_option(config, "lyrics_char_threshold", DEFAULT_THRESHOLD, 0)
    max_chars = _int_option(config, "lyrics_max_chars", DEFAULT_MAX_CHARS, 0)
    # 歌词比音乐慢一点，默认提前 4 个歌曲 tick（= 8 游戏 tick）显示
    advance = GAME_TICKS_PER_MUSIC_TICK * _int_option(
        config, "lyrics_start_lead_ticks", DEFAULT_START_LEAD, 0)
    segments = build_char_segments(entries, config)
    if not segments:
        print(Ansi.error("没有可显示的歌词，跳过歌词生成"))
        return None
    for segment in segments:
        segment["pos"] = display_pos(segment["tick"])

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
    oversized = 0
    # start 用 6 位数字估算，宁可高估也不要让真实命令超限
    for segment in segments:
        base_len = len(_segment_string(segment, 999999, probe_pos))
        if base_len + overhead > MAX_CHAIN_COMMAND_LEN:
            # 单句就塞不进一个命令方块（阈值太小 / 句子太长），跳过并提示
            oversized += 1
            continue
        piece_len = base_len + (1 if current else 0)
        if current and length + piece_len + overhead > MAX_CHAIN_COMMAND_LEN:
            batches.append(current)
            current = []
            length = 0
            piece_len = base_len
        current.append(segment)
        length += piece_len
    if current:
        batches.append(current)
    if oversized:
        print(Ansi.error(f"警告：{oversized} 句歌词单句就超过命令长度上限，已跳过；"
                         f"请调大断句阈值（当前 {threshold} tick）让句子更短"))
    if not batches:
        print(Ansi.error("歌词句子全部超过命令长度上限，跳过歌词生成"))
        return None

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
            max(0, GAME_TICKS_PER_MUSIC_TICK * (segment["tick"] - origin_tick) + bias - advance),
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

    total_chars = sum(segment["count"] for segment in segments)
    print(Ansi.success(f"逐字歌词链已添加：{len(segments)} 句 / {total_chars} 字 -> "
                       f"{batch_count} 个脉冲命令方块，链层 Y={chain_y}"))
    char_limit = f"、每句≤{max_chars} 字" if max_chars > 0 else ""
    print(Ansi.dim(f"最长命令 {longest} 字符（上限 {MAX_CHAIN_COMMAND_LEN}）；"
                   f"断句阈值 {threshold} tick"
                   f"{'（含标点断句）' if config.get('lyrics_break_on_punctuation') else ''}"
                   f"{char_limit}；提前 {advance} 游戏 tick 显示；"
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
    threshold = _int_option(config, "lyrics_char_threshold", DEFAULT_THRESHOLD, 0)
    punctuation = bool(config.get("lyrics_break_on_punctuation"))
    if validate_entries(entries):
        return (f"{len(entries)} 条歌词 · " + Ansi.dim("数据不符合逐字模式（一字一条 / duration ≥ 1）"))
    max_chars = _int_option(config, "lyrics_max_chars", DEFAULT_MAX_CHARS, 0)
    sentences = len(break_sentences(entries, threshold, punctuation, max_chars))
    char_limit = f" +{max_chars}字上限" if max_chars > 0 else ""
    follow = "跟随" if config.get("lyrics_follow", True) else "固定"
    return (f"{len(entries)} 条歌词 / {sentences} 句 · 断句阈值={threshold} tick"
            f"{' +标点' if punctuation else ''}{char_limit} · "
            f"提前={config.get('lyrics_start_lead_ticks', DEFAULT_START_LEAD)} tick · "
            f"颜色={config.get('lyrics_color', 'white')} · 字号={config.get('lyrics_scale')} · "
            f"保持距离={config.get('lyrics_lead_distance')} · "
            f"随机={_jitter_text(config)} · 位置={follow}")


def import_lyrics_json(config: dict) -> None:
    """导入歌词 JSON（导入即启用歌词功能）。"""
    print(Ansi.title("\n=== 导入歌词 JSON ==="))
    print(Ansi.dim("支持编辑器导出的 noteblock-web-lyrics（formatVersion 2），只读取 lane 0。"))
    print(Ansi.dim("逐字模式要求一字一条记录（空格也算一个字）且 duration ≥ 1。"))
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
    threshold = _int_option(config, "lyrics_char_threshold", DEFAULT_THRESHOLD, 0)
    segments = build_char_segments(entries, config)
    longest = max((segment["count"] for segment in segments), default=0)
    print(Ansi.info(f"逐字歌词：按 {threshold} tick 阈值断成 {len(segments)} 句"
                    f"（最长一句 {longest} 字），每句一份 chars= 时间表"))
    print(Ansi.dim("配色 / 动画 / 断句阈值等可在 [3] 显示参数里调整。"))


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
    """歌词显示参数：外观 / 动画时长 / 动画 / 位置 / 断句。"""
    while True:
        style = config.get("layout_style", "flat")
        master = config.get("master_group")
        threshold = _int_option(config, "lyrics_char_threshold", DEFAULT_THRESHOLD, 0)
        punctuation = "开" if config.get("lyrics_break_on_punctuation") else "关"
        print(Ansi.title("\n=== 歌词显示参数（逐字模式）==="))
        print(f"  颜色 {config.get('lyrics_color')} · 字号 {config.get('lyrics_scale')} · "
              f"入场 {config.get('lyrics_enter')} · 出场 {config.get('lyrics_exit')}")
        print(f"  入场动画 {config.get('lyrics_anim_in') or '无'} · "
              f"出场动画 {config.get('lyrics_anim_out') or '入场镜像'}")
        max_chars = _int_option(config, "lyrics_max_chars", DEFAULT_MAX_CHARS, 0)
        char_limit = f"{max_chars} 字" if max_chars > 0 else "不限"
        print(f"  断句阈值 {threshold} tick · 标点断句 {punctuation} · 每句上限 {char_limit}")
        print(f"  相对音乐提前 {config.get('lyrics_start_lead_ticks')} tick 显示")
        print(f"  保持距离 {config.get('lyrics_lead_distance')} · 高度 {config.get('lyrics_y_offset')} · "
              f"侧向偏移 {config.get('lyrics_side_offset') if config.get('lyrics_side_offset') is not None else '无'} · "
              f"随机 {_jitter_text(config)}")
        print(f"  {'跟随前进' if config.get('lyrics_follow', True) else '固定在结构中点'} · "
              f"链层 -{config.get('lyrics_chain_y_offset')}")
        if style != "flat" and master is not None:
            print(Ansi.dim(f"  文字锚定在核心组（组{master + 1}）上方"))
        print("  [1] 外观（颜色 / 字号）")
        print("  [2] 时间（每个字的入场 enter / 出场 exit / 相对音乐提前量）")
        print("  [3] 入场动画（编号选择）")
        print("  [4] 出场动画（编号选择）")
        print("  [5] 位置（保持距离 / 高度 / 侧向 / 随机范围 / 跟随 / 链层）")
        print("  [6] 断句（长音阈值 / 标点断句 / 每句字数上限）")
        print("  [0] 返回")
        choice = input(Ansi.prompt("选择: ")).strip()
        if choice == "1":
            color = input(Ansi.prompt(f"  颜色（{config.get('lyrics_color')}，如 #FFD700 / white / rainbow）: ")).strip()
            if color:
                config["lyrics_color"] = color.replace(" ", "")
            config["lyrics_scale"] = _ask(
                "字号 scale", config.get("lyrics_scale"), float, 0.05, 64.0)
        elif choice == "2":
            print(Ansi.dim("  逐字模式下这是**每个字**的动画时长，建议入场 0~2、出场 2~4。"))
            config["lyrics_enter"] = _ask(
                "入场时长 enter", config.get("lyrics_enter"), int, 0)
            config["lyrics_exit"] = _ask(
                "出场时长 exit", config.get("lyrics_exit"), int, 0)
            print(Ansi.dim("  提前量：歌词比音乐**早**多少显示（单位歌曲 tick，1 tick = 2 游戏 tick）；"
                           "0 = 同时。"))
            config["lyrics_start_lead_ticks"] = _ask(
                "相对音乐提前（歌曲 tick）",
                config.get("lyrics_start_lead_ticks"), int, 0)
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
            print(Ansi.dim("  字 duration 严格大于阈值 → 该字是本句最后一个字（长音断句）；"
                           "阈值越大句子越长。"))
            config["lyrics_char_threshold"] = _ask(
                "断句阈值（tick，与歌词 duration 同单位）",
                config.get("lyrics_char_threshold"), int, 0)
            print(Ansi.dim("  每句字数上限：写满这么多字也断句（与长音规则叠加），0 = 不限。"))
            config["lyrics_max_chars"] = _ask(
                "每句字数上限（0=不限）",
                config.get("lyrics_max_chars"), int, 0)
            current = config.get("lyrics_break_on_punctuation")
            ans = input(Ansi.prompt(
                f"  标点（，。！？等）也断句 (y/n，回车={'保持开启' if current else '保持关闭'}): ")).strip().lower()
            if ans in ("y", "yes"):
                config["lyrics_break_on_punctuation"] = True
            elif ans in ("n", "no"):
                config["lyrics_break_on_punctuation"] = False
            entries = imported_entries(config)
            if entries and not validate_entries(entries):
                segments = build_char_segments(entries, config)
                print(Ansi.info(f"  当前设置会断成 {len(segments)} 句"))
        elif choice in ("", "0", "q"):
            return
        else:
            print(Ansi.error("无效选项"))


def edit_lyrics_menu(config: dict) -> None:
    """歌词子菜单：导入 / 清空 / 显示参数。"""
    while True:
        print(Ansi.title("\n=== 歌词字幕（/mpe text chain 逐字）==="))
        print(f"  {_lyrics_status(config)}")
        print("  [1] 导入歌词 JSON")
        print("  [2] 清空歌词")
        print("  [3] 显示参数")
        print("  [0] 返回")
        choice = input(Ansi.prompt("选择: ")).strip()
        if choice == "1":
            import_lyrics_json(config)
        elif choice == "2":
            clear_lyrics(config)
        elif choice == "3":
            edit_lyrics_display_settings(config)
        elif choice in ("", "0", "q"):
            return
        else:
            print(Ansi.error("无效选项"))
