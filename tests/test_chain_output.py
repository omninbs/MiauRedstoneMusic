"""MiauRedstoneMusic 输出数据自检脚本。

不依赖 pytest，直接 `python tests/test_chain_output.py` 运行。

覆盖内容：
1. `/mpe text chain` 命令结构：单个脉冲命令方块、`~~~` 锚点、段字段完整。
2. 时间对齐：`start = 2×(tick − origin)` + 启动链补偿，与主音乐链同一计时基准。
3. 文字坐标：相对偏移 + 命令方块位置 = 期望的文字锚点（跟随 / 固定）。
4. 文字清洗：空格写 `_`，`|` `;` `,` 换成全角，换行丢弃。
5. 逐字断句：`duration > threshold`（严格大于）断句、长音字属当前句、标点断句开关。
6. `chars=` 时间表：项数 = 可见字符数、字start/字duration 与歌词数据一致（×2 tick）、
   段 `duration` 写 0。
7. 保持距离（文字 = 激活中继器位置 + lead）、XYZ 随机抖动与入场/出场动画选项。
8. 命令方块位置：结构最低方块下方，且不与音乐链坐标重叠。
9. 超长命令分批：多条命令各带唯一 id，长度都在上限内。
10. 编辑器 v2 歌词模板解析与 lane 过滤；多字 / 缺 duration 直接报错。
11. 顺序系统音色合并（按音轨顺序、相邻同音色、每组最多 3 条）。
12. 歌词晚于歌曲结尾时不被截断。
13. Windows GBK 控制台不会因输出符号崩溃。
"""

import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from miaunoteblock.constants import Ansi, DIR_OFFSET, SIDE_AXIS, TEXT_TOWARDS_YAW
from miaunoteblock.layout import compute_offsets
from miaunoteblock.generation import (
    build_all_commands_virtual,
    chain_valid_ticks,
    event_offsets,
    find_structure_bottom,
)
from miaunoteblock.instruments import auto_group_by_mode_on_layers
from miaunoteblock.lyrics import (
    MAX_CHAIN_COMMAND_LEN,
    build_char_segments,
    build_lyrics_chain,
    break_sentences,
    load_lyrics_json,
    sanitize_text,
    visible_length,
)


# ── 测试用对象 ────────────────────────────────────────────────

class FakeNote:
    def __init__(self, tick, layer=0, key=45, instrument=0):
        self.tick = tick
        self.layer = layer
        self.key = key
        self.instrument = instrument

    @property
    def pitch(self):
        return self.key


def make_layers():
    """两个音乐组：layer0/layer1 各成一组。"""
    layer0 = [FakeNote(t, layer=0) for t in (0, 5, 10, 20, 30)]
    layer1 = [FakeNote(t, layer=1, key=50) for t in (0, 3, 30)]
    virtual_layers = [
        {"vid": 0, "original_id": 0, "side": None, "notes": layer0, "inst": 0, "cat": "钢琴"},
        {"vid": 1, "original_id": 1, "side": None, "notes": layer1, "inst": 0, "cat": "钢琴"},
    ]
    layer_to_group = {0: 0, 1: 1}
    return virtual_layers, layer_to_group


def base_config():
    return {
        "layout_style": "flat",
        "flat_spacing": 3,
        "master_group": None,
        "lyrics_color": "white",
        "lyrics_scale": 10.0,
        "lyrics_char_threshold": 4,
        "lyrics_max_chars": 18,
        "lyrics_break_on_punctuation": False,
        "lyrics_start_lead_ticks": 4,
        "lyrics_enter": 0,
        "lyrics_exit": 3,
        "lyrics_anim_in": "scale:enlarge",
        "lyrics_anim_out": "",
        "lyrics_y_offset": 5,
        "lyrics_side_offset": None,
        "lyrics_lead_distance": 16,
        "lyrics_jitter_x": 0.0,
        "lyrics_jitter_y": 0.0,
        "lyrics_jitter_z": 0.0,
        "lyrics_follow": True,
        "lyrics_chain_y_offset": 1,
        "lyrics_import": [],
    }


def lyric_char(index: int) -> str:
    """逐字歌词专用：第 index 个「字」（CJK 单字，含空格由调用方指定）。"""
    return chr(0x4E00 + index)


START = (100, 64, 200)


def make_world(direction="east", lyric_ticks=(0, 5, 10, 20, 30), follow=True,
               texts=None, duration=5, max_tick=30, rng=None, **overrides):
    """生成音乐链 + 歌词链，返回所有中间结果。

    duration 默认 5 > 断句阈值 4，所以每个字都会独立成句。
    """
    virtual_layers, layer_to_group = make_layers()
    config = base_config()
    config.update({"lyrics_follow": follow})
    config.update(overrides)
    if texts is None:
        texts = [lyric_char(index) for index in range(len(lyric_ticks))]
    config["lyrics_import"] = [
        {"tick": tick, "text": text, "duration": duration}
        for tick, text in zip(lyric_ticks, texts)
    ]

    groups = sorted(set(layer_to_group.values()))
    offsets = compute_offsets(groups, config, len(groups))
    group_offset_dict = {gid: offsets[index] for index, gid in enumerate(groups)}

    music_cmds, activation_positions = build_all_commands_virtual(
        virtual_layers, layer_to_group, [group_offset_dict[gid] for gid in groups],
        START, direction, max_tick, group_staircase_modes={},
    )
    bottom = find_structure_bottom(music_cmds)
    result = build_lyrics_chain(
        virtual_layers, group_offset_dict, START, direction, max_tick, config,
        struct_bottom_y=bottom, rng=rng,
    )
    return {
        "config": config,
        "result": result,
        "bottom": bottom,
        "music": music_cmds,
        "offsets": group_offset_dict,
        "activation": activation_positions,
        "virtual_layers": virtual_layers,
    }


# ── 解析工具 ──────────────────────────────────────────────────

SETBLOCK_RE = re.compile(r"^setblock\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+)$")
COMMAND_TEXT_RE = re.compile(r'\{Command:("(?:[^"\\]|\\.)*")')
DELAY_RE = re.compile(r"delay=(\d+)")
CHAIN_PREFIX = "/mpe text chain ~~~ "


def parse_blocks(commands):
    """[(坐标, 方块串), ...]（只解析 setblock）。"""
    blocks = []
    for command in commands:
        match = SETBLOCK_RE.match(command.strip())
        if match:
            coord = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
            blocks.append((coord, match.group(4)))
    return blocks


def block_commands(blocks):
    """{坐标: 命令文本}，只保留带 Command: 的方块。"""
    out = {}
    for coord, block in blocks:
        match = COMMAND_TEXT_RE.search(block)
        if match:
            out[coord] = json.loads(match.group(1))
    return out


def chain_entries(commands):
    """[(坐标, 方块串, 命令文本), ...]，只保留 /mpe text chain。"""
    entries = []
    for coord, block in parse_blocks(commands):
        match = COMMAND_TEXT_RE.search(block)
        if not match:
            continue
        text = json.loads(match.group(1))
        if text.startswith(CHAIN_PREFIX):
            entries.append((coord, block, text))
    return entries


def parse_segments(command):
    """把 /mpe text chain 拆成 (段列表, 选项列表)。"""
    rest = command[len(CHAIN_PREFIX):]
    tokens = rest.split(" ")
    blob, options = tokens[0], tokens[1:]
    segments = []
    for raw in blob.split("|"):
        body, *extra = raw.split(";")
        fields = body.split(",")
        segments.append({
            "x": float(fields[0]),
            "y": float(fields[1]),
            "z": float(fields[2]),
            "start": int(fields[3]),
            "duration": int(fields[4]),
            "text": ",".join(fields[5:]),
            "opts": extra,
        })
    return segments, options


def segment_chars(segment):
    """解析段内 chars= 时间表 -> [(字start, 字duration), ...]。"""
    for option in segment["opts"]:
        if option.startswith("chars="):
            items = []
            for chunk in option[len("chars="):].split(","):
                start, duration = chunk.split(":")
                items.append((int(start), int(duration)))
            return items
    return []


class Checks:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def ok(self, cond, msg):
        if cond:
            self.passed += 1
        else:
            self.failed.append(msg)

    def report(self, name):
        status = "OK" if not self.failed else "FAIL"
        print(f"  [{name}] {status}  通过断言 {self.passed} 条")
        for message in self.failed:
            print(f"      × {message}")
        return not self.failed


# ── 测试 1：命令结构 ─────────────────────────────────────────

def test_command_shape():
    print("测试 1：/mpe text chain 命令结构与脉冲命令方块")
    chk = Checks()
    world = make_world()
    result = world["result"]
    chk.ok(result is not None, "歌词链未生成")
    if not result:
        return chk.report("shape")

    chk.ok(result["batches"] == 1, f"短歌词应只有 1 批，实际 {result['batches']}")
    chk.ok(len(result["commands"]) == 1, f"命令数 {len(result['commands'])} != 1")
    chk.ok(len(result["activations"]) == 1, f"激活点 {len(result['activations'])} != 1")

    entries = chain_entries(result["commands"])
    chk.ok(len(entries) == 1, f"识别到 {len(entries)} 条 /mpe text chain 命令")
    if not entries:
        return chk.report("shape")

    coord, block, text = entries[0]
    chk.ok("minecraft:command_block[facing=up]" in block, f"应为脉冲命令方块: {block[:70]}")
    chk.ok("chain_command_block" not in block, "不应再使用链式命令方块")
    chk.ok("~~~ " in text, "锚点必须是 ~~~")

    segments, options = parse_segments(text)
    chk.ok(len(segments) == 5, f"段数 {len(segments)} != 5")
    chk.ok(all(segment["duration"] == 0 for segment in segments),
           "逐字模式段 duration 必须写 0（由 chars= 推导）")
    chk.ok(all(visible_length(segment["text"]) == 1 for segment in segments),
           f"每段应只有一个字: {[s['text'] for s in segments]}")
    chk.ok(all(any(opt.startswith("chars=") for opt in segment["opts"])
               for segment in segments), "每段都必须带 chars= 时间表")
    chk.ok(all(len(segment_chars(segment)) == 1 for segment in segments),
           "单字句的 chars= 应恰好 1 项")
    chk.ok(f"towards={TEXT_TOWARDS_YAW['east']},0,0" in options,
           f"缺少朝向: {options}")
    chk.ok("force=true" in options, f"缺少 force=true: {options}")
    chk.ok(any(option.startswith("color=") for option in options), f"缺少 color: {options}")
    chk.ok(any(option.startswith("scale=") for option in options), f"缺少 scale: {options}")
    chk.ok("enter=0" in options, f"逐字模式默认 enter 应为 0: {options}")
    chk.ok("exit=3" in options, f"逐字模式默认 exit 应为 3: {options}")
    chk.ok("in=scale:enlarge" in options, f"默认入场动画应为 scale:enlarge: {options}")
    chk.ok("typewriter" not in " ".join(options),
           f"逐字模式不能使用 typewriter: {options}")

    # 命令方块位于结构最低方块下方
    chk.ok(coord[1] == world["bottom"] - 1,
           f"命令方块 Y {coord[1]} != 结构底部下方 {world['bottom'] - 1}")
    # 激活红石块紧邻命令方块（沿链轴 +1），下一 tick 触发
    dx, _, dz = DIR_OFFSET["east"]
    want = (coord[0] + dx, coord[1], coord[2] + dz)
    chk.ok(result["activations"][0] == want,
           f"激活点 {result['activations'][0]} != {want}")
    return chk.report("shape")


# ── 测试 2：时间对齐与坐标（四个方向）────────────────────────

def test_timing_and_position():
    print("测试 2：时间对齐与文字坐标（四个方向）")
    lyric_ticks = (0, 5, 10, 20, 30)
    all_ok = True
    for direction in ("east", "west", "south", "north"):
        chk = Checks()
        world = make_world(direction=direction, lyrics_start_lead_ticks=0)
        result = world["result"]
        chk.ok(result is not None, "歌词链未生成")
        if not result:
            all_ok &= chk.report(direction)
            continue

        coord, _, text = chain_entries(result["commands"])[0]
        segments, options = parse_segments(text)
        side_axis = SIDE_AXIS[direction]
        dx, _, dz = DIR_OFFSET[direction]
        step = dx if side_axis == "z" else dz
        axis_start = START[0] if side_axis == "z" else START[2]
        side_start = START[2] if side_axis == "z" else START[0]
        # 平铺两组的侧向偏移（compute_offsets 会按组数居中）
        sides = [offset[1] for offset in world["offsets"].values()]
        side_want = side_start + (min(sides) + max(sides)) // 2
        y_want = START[1] + 0 + world["config"]["lyrics_y_offset"]

        lead = world["config"]["lyrics_lead_distance"]
        layout = dict(event_offsets(
            chain_valid_ticks(world["virtual_layers"], 30),
            world["config"].get("uniform_repeater_mode", False)))
        for segment, tick in zip(segments, lyric_ticks):
            # 单批时启动链补偿 = 批数 + 1 = 2
            chk.ok(segment["start"] == 2 * tick + 2,
                   f"{direction} tick {tick} start={segment['start']} 应为 {2 * tick + 2}")
            abs_x = coord[0] + segment["x"]
            abs_y = coord[1] + segment["y"]
            abs_z = coord[2] + segment["z"]
            # 文字 = 当前激活位置（按真实链条布局）+ 保持距离
            axis_want = axis_start + layout[tick] * step + lead * step
            if side_axis == "z":
                chk.ok(abs_x == axis_want, f"{direction} tick {tick} 链轴 x {abs_x} != {axis_want}")
                chk.ok(abs_z == side_want, f"{direction} tick {tick} 侧向 z {abs_z} != {side_want}")
            else:
                chk.ok(abs_z == axis_want, f"{direction} tick {tick} 链轴 z {abs_z} != {axis_want}")
                chk.ok(abs_x == side_want, f"{direction} tick {tick} 侧向 x {abs_x} != {side_want}")
            chk.ok(abs_y == y_want, f"{direction} tick {tick} 高度 y {abs_y} != {y_want}")

        chk.ok(f"towards={TEXT_TOWARDS_YAW[direction]},0,0" in options,
               f"{direction} 朝向错误: {options}")
        all_ok &= chk.report(direction)

    # 固定模式：所有段的链轴坐标相同（fixed_tick=15 落在 tick 10 与 20 之间）
    chk = Checks()
    world = make_world(follow=False, lyrics_start_lead_ticks=0)
    coord, _, text = chain_entries(world["result"]["commands"])[0]
    segments, _ = parse_segments(text)
    axis = {coord[0] + segment["x"] for segment in segments}
    chk.ok(len(axis) == 1, f"固定模式链轴坐标应唯一: {sorted(axis)}")
    layout = dict(event_offsets(
        chain_valid_ticks(world["virtual_layers"], 30),
        world["config"].get("uniform_repeater_mode", False)))
    lead = world["config"]["lyrics_lead_distance"]
    low = START[0] + layout[10] + lead
    high = START[0] + layout[20] + lead
    value = sorted(axis)[0]
    chk.ok(low <= value <= high,
           f"固定坐标 {value} 应落在 [{low}, {high}]（tick 10~20 之间）")
    all_ok &= chk.report("fixed")
    return all_ok


# ── 测试 2b：歌词相对音乐提前显示 ────────────────────────────

def test_start_lead():
    print("测试 2b：歌词提前显示（默认 4 歌曲 tick = 8 游戏 tick）")
    chk = Checks()

    def start_of(lead, tick=20):
        world = make_world(lyric_ticks=(tick,), lyrics_start_lead_ticks=lead)
        _, _, text = chain_entries(world["result"]["commands"])[0]
        return parse_segments(text)[0][0]["start"]

    base = start_of(0)
    chk.ok(base == 2 * 20 + 2, f"不提前时的 start 错误: {base}")
    chk.ok(start_of(1) == base - 2, f"提前 1 歌曲 tick 应早 2 游戏 tick: {start_of(1)}")
    chk.ok(start_of(4) == base - 8, f"提前 4 歌曲 tick 应早 8 游戏 tick: {start_of(4)}")
    chk.ok(start_of(4) > 0, "提前后 start 仍应为正数")

    # 来不及提前的句子不能写出负数 tick
    chk.ok(start_of(4, tick=0) == 0, f"tick 0 的句子应被夹到 0: {start_of(4, tick=0)}")
    return chk.report("start-lead")


# ── 测试 3：文字清洗 ─────────────────────────────────────────

def test_text_sanitize():
    print("测试 3：文字清洗（空格 / 分隔符）")
    chk = Checks()
    chk.ok(sanitize_text("第一 句") == "第一_句", f"空格未转换为 _: {sanitize_text('第一 句')}")
    chk.ok(sanitize_text("a|b") == "a丨b", "| 未被替换")
    chk.ok(sanitize_text("a;b") == "a；b", "; 未被替换")
    chk.ok(sanitize_text("a,b") == "a，b", ", 未被替换")
    chk.ok(sanitize_text("a\nb\tc") == "abc", "换行/制表符未丢弃")
    chk.ok(sanitize_text("§c红&6色") == "§c红&6色", "颜色码不应被改动")

    # 命令里不出现裸分隔符（逐字数据仍然是一字一条，只测特殊字符）
    world = make_world(texts=[" ", "|", ";", ",", "a"])
    _, _, text = chain_entries(world["result"]["commands"])[0]
    segments, _ = parse_segments(text)
    chk.ok(len(segments) == 5, f"清洗后段数 {len(segments)} != 5")
    chk.ok(all(" " not in segment["text"] for segment in segments), "段内文字仍含空格")
    chk.ok(all(not ({",", ";", "|"} & set(segment["text"])) for segment in segments),
           f"段内仍有裸分隔符: {[s['text'] for s in segments]}")
    chk.ok(segments[0]["text"] == "_", f"空格应写成 _: {segments[0]['text']}")
    # chars 项数仍按「可见字符」算：空格算 1 个
    chk.ok(all(len(segment_chars(segment)) == 1 for segment in segments),
           "清洗后 chars= 项数应与可见字符数（空格算 1）一致")
    return chk.report("sanitize")


# ── 测试 4：逐字断句与 chars= 时间表 ─────────────────────────

def test_char_sentences():
    print("测试 4：逐字断句（duration > threshold）与 chars= 时间表")
    chk = Checks()
    config = base_config()

    # 文档第 10 节的示例：好(12) 与 睡(20) 是长音 → 只切成 2 句
    entries = [(100, "今", 3), (103, "天", 3), (106, " ", 2), (108, "真", 3),
               (111, "好", 12), (123, "我", 3), (126, "还", 3), (129, "没", 3),
               (132, "睡", 20)]
    sentences = break_sentences(entries, 4)
    chk.ok([[item[1] for item in sentence] for sentence in sentences]
           == [["今", "天", " ", "真", "好"], ["我", "还", "没", "睡"]],
           f"断句结果错误: {[[item[1] for item in s] for s in sentences]}")

    # 严格大于：duration == threshold 不断句，threshold + 1 才断
    chk.ok(len(break_sentences([(0, "a", 4), (2, "b", 4), (4, "c", 4)], 4)) == 1,
           "duration == threshold 不应断句")
    chk.ok(len(break_sentences([(0, "a", 4), (2, "b", 5), (4, "c", 4)], 4)) == 2,
           "duration == threshold + 1 应断句")

    # 阈值很大 → 整首一句
    chk.ok(len(break_sentences(entries, 72000)) == 1, "阈值 72000 应只切成一句")

    # 标点断句默认关闭，可通过开关打开
    punctuated = [(0, "你", 3), (2, "好", 1), (4, "，", 1), (6, "世", 3), (8, "界", 1)]
    chk.ok(len(break_sentences(punctuated, 4, False)) == 1, "标点断句默认应关闭")
    chk.ok(len(break_sentences(punctuated, 4, True)) == 2, "开启后标点应断句")

    # 每句字数上限：写满 N 字也断句（与长音规则叠加）
    long_line = [(index, lyric_char(index), 3) for index in range(10)]
    grouped = [len(group) for group in break_sentences(long_line, 4, False, 4)]
    chk.ok(grouped == [4, 4, 2], f"字数上限断句错误: {grouped}")
    chk.ok(len(break_sentences(long_line, 4, False, 0)) == 1, "0 应表示不限字数")
    # 长音比字数上限更早触发：第 3 字是长音 → 句子在第 3 字结束
    mixed = [(0, "a", 3), (2, "b", 3), (4, "c", 9), (6, "d", 3), (8, "e", 3)]
    chk.ok([len(group) for group in break_sentences(mixed, 4, False, 4)] == [3, 2],
           f"长音与字数上限叠加错误: {[len(g) for g in break_sentences(mixed, 4, False, 4)]}")
    # 默认上限 18：20 个字断成 18 + 2
    twenty = [(index, lyric_char(index), 3) for index in range(20)]
    chk.ok([len(group) for group in break_sentences(
        twenty, 4, False, config["lyrics_max_chars"])] == [18, 2],
        "默认 18 字上限未生效")
    # build_char_segments 同样按配置的字数上限断句
    chk.ok([len(segment["chars"]) for segment in build_char_segments(
        long_line, dict(config, lyrics_max_chars=3))] == [3, 3, 3, 1],
        "build_char_segments 未按字数上限断句")
    # 命令里也应体现字数断句
    world = make_world(lyric_ticks=tuple(range(0, 12, 2)), duration=3,
                       lyrics_char_threshold=4, lyrics_max_chars=2)
    _, _, text = chain_entries(world["result"]["commands"])[0]
    segments_out, _ = parse_segments(text)
    chk.ok([visible_length(segment["text"]) for segment in segments_out] == [2, 2, 2],
           f"命令里的字数断句错误: {[s['text'] for s in segments_out]}")

    # chars= 时间表：字start / 字duration 与歌词数据一致（1 个音乐 tick = 2 游戏 tick）
    segments = build_char_segments(entries, config)
    chk.ok(len(segments) == 2, f"应生成 2 句，实际 {len(segments)}")
    first = segments[0]
    chk.ok(first["text"] == "今天_真好", f"句子文字错误: {first['text']}")
    chk.ok(first["chars"] == [(0, 6), (6, 6), (12, 4), (16, 6), (22, 24)],
           f"第一句 chars 错误: {first['chars']}")
    chk.ok(segments[1]["chars"] == [(0, 6), (6, 6), (12, 6), (18, 40)],
           f"第二句 chars 错误: {segments[1]['chars']}")
    chk.ok(first["span"] == 22 + 24, f"句长错误: {first['span']}")
    chk.ok(all(segment["duration"] == 0 for segment in segments),
           "段 duration 必须为 0（由模组按时间表推导）")
    chk.ok(all(len(segment["chars"]) == visible_length(segment["text"])
               for segment in segments),
           "chars= 项数必须等于句子可见字符数（空格算 1）")

    # 命令里：一句一个段，段 duration 写 0，chars= 在段内
    world = make_world(lyric_ticks=(0, 2, 4), duration=5)
    _, _, text = chain_entries(world["result"]["commands"])[0]
    segments_out, _ = parse_segments(text)
    chk.ok(len(segments_out) == 3, f"每字都是长音 → 3 段，实际 {len(segments_out)}")
    chk.ok(all(segment["duration"] == 0 for segment in segments_out),
           "命令里的段 duration 应写 0")
    chk.ok(all(len(segment_chars(segment)) == 1 for segment in segments_out),
           "单字句的 chars= 应为 1 项")

    # 调大阈值 → 连续字合并成一句，chars 项数随之增加
    world = make_world(lyric_ticks=(0, 2, 4), duration=2,
                       lyrics_char_threshold=2)
    _, _, text = chain_entries(world["result"]["commands"])[0]
    segments_out, _ = parse_segments(text)
    chk.ok(len(segments_out) == 1, f"duration == 阈值 时不应断句，实际 {len(segments_out)} 段")
    chk.ok(segments_out[0]["text"] == "".join(lyric_char(i) for i in range(3)),
           f"合并文字错误: {segments_out[0]['text']}")
    chk.ok(segment_chars(segments_out[0]) == [(0, 4), (4, 4), (8, 4)],
           f"合并后 chars 错误: {segment_chars(segments_out[0])}")

    # 生成侧同样拒绝非法数据（不崩溃，直接跳过歌词生成）
    world = make_world(lyric_ticks=(0, 2), duration=0)
    chk.ok(world["result"] is None, "duration = 0 时应跳过歌词生成")
    return chk.report("chars")


# ── 测试 4b：保持距离 / 随机抖动 / 动画选项 ───────────────────

def test_lead_and_jitter():
    print("测试 4b：保持距离、随机抖动与动画选项")
    chk = Checks()

    # 保持距离：lead 越大，文字离激活位置越远
    def text_x(lead):
        world = make_world(lyric_ticks=(0,), lyrics_lead_distance=lead)
        coord, _, text = chain_entries(world["result"]["commands"])[0]
        segment = parse_segments(text)[0][0]
        return coord[0] + segment["x"]

    chk.ok(text_x(16) == START[0] + 0 + 16, f"默认保持距离错误: {text_x(16)}")
    chk.ok(text_x(40) - text_x(16) == 24, "保持距离未按设置线性变化")

    # 固定模式也带保持距离
    world = make_world(follow=False, lyric_ticks=(0,), lyrics_lead_distance=8)
    coord, _, text = chain_entries(world["result"]["commands"])[0]
    layout = dict(event_offsets(
        chain_valid_ticks(world["virtual_layers"], 30),
        world["config"].get("uniform_repeater_mode", False)))
    value = coord[0] + parse_segments(text)[0][0]["x"]
    chk.ok(START[0] + layout[10] + 8 <= value <= START[0] + layout[20] + 8,
           f"固定模式的保持距离错误: {value}")

    # 随机抖动：范围 ±2/±1/±0.5，且都是 0.5 的整数倍
    world = make_world(lyric_ticks=(0, 10, 20),
                       lyrics_jitter_x=2.0, lyrics_jitter_y=1.0, lyrics_jitter_z=0.5,
                       rng=random.Random(1234))
    coord, _, text = chain_entries(world["result"]["commands"])[0]
    segments = parse_segments(text)[0]
    layout = dict(event_offsets(
        chain_valid_ticks(world["virtual_layers"], 30),
        world["config"].get("uniform_repeater_mode", False)))
    base_x = START[0] + world["config"]["lyrics_lead_distance"]
    sides = [offset[1] for offset in world["offsets"].values()]
    base_z = START[2] + (min(sides) + max(sides)) // 2
    base_y = START[1] + world["config"]["lyrics_y_offset"]
    limits = (2.0, 1.0, 0.5)
    for segment, tick in zip(segments, (0, 10, 20)):
        offsets = (coord[0] + segment["x"] - (base_x + layout[tick]),
                   coord[1] + segment["y"] - base_y,
                   coord[2] + segment["z"] - base_z)
        for value, limit in zip(offsets, limits):
            chk.ok(abs(value) <= limit + 1e-9, f"tick {tick} 抖动 {value} 超出 ±{limit}")
            chk.ok(abs(value * 2 - round(value * 2)) < 1e-9,
                   f"tick {tick} 抖动 {value} 不是 0.5 的倍数")

    # 抖动为 0 时不产生偏移
    world = make_world(lyric_ticks=(0,))
    coord, _, text = chain_entries(world["result"]["commands"])[0]
    segment = parse_segments(text)[0][0]
    chk.ok(float(segment["x"]).is_integer() and float(segment["y"]).is_integer()
           and float(segment["z"]).is_integer(), "未开启抖动时坐标应为整数")

    # 动画选项：全局 in= / out=
    # 逐字模式可用的动画正常写入
    world = make_world(lyrics_anim_in="slide:up,scale:enlarge", lyrics_anim_out="scale:shrink")
    _, options = parse_segments(chain_entries(world["result"]["commands"])[0][2])
    chk.ok("in=slide:up,scale:enlarge" in options, f"入场动画未写入: {options}")
    chk.ok("out=scale:shrink" in options, f"出场动画未写入: {options}")

    # 与 chars= 冲突的动画会被剔除（否则模组直接报错）
    world = make_world(lyrics_anim_in="wipe:left,slide:up", lyrics_anim_out="typewriter:left")
    _, options = parse_segments(chain_entries(world["result"]["commands"])[0][2])
    chk.ok("in=slide:up" in options, f"禁用动画未剔除干净: {options}")
    chk.ok(not any("wipe" in opt or "typewriter" in opt for opt in options),
           f"wipe/typewriter 不应出现在命令里: {options}")
    chk.ok(not any(opt.startswith("out=") for opt in options),
           f"出场动画全是禁用项时不应写入 out=: {options}")

    # 关闭动画时不写 in= / out=
    world = make_world(lyrics_anim_in="", lyrics_anim_out="")
    _, options = parse_segments(chain_entries(world["result"]["commands"])[0][2])
    chk.ok(not any(opt.startswith("in=") for opt in options), f"不应写入 in=: {options}")
    chk.ok(not any(opt.startswith("out=") for opt in options), f"不应写入 out=: {options}")
    return chk.report("lead-jitter")


# ── 测试 4c：中继器合并 / 均匀模式的链条布局与文字位置 ────────

def _expected_chain_offsets(layout, uniform):
    """由 event_offsets 推导链条层上所有方块的链轴偏移（测试用，独立于生成器实现）。"""
    coords = {-1}  # 起手中继器在起点之前一格
    for index, (tick, offset) in enumerate(layout):
        coords.add(offset)      # 事件格
        coords.add(offset + 1)  # 事件格后的 delay=1 中继器
        if index + 1 < len(layout):
            blank = layout[index + 1][0] - tick - 1
            pairs = blank if uniform else -(-blank // 4)
            for k in range(pairs):
                coords.add(offset + 2 + 2 * k)  # 羊毛连接块
                coords.add(offset + 3 + 2 * k)  # 中继器
    return coords


def test_repeater_modes():
    print("测试 4c：中继器合并 / 均匀模式下的链条布局与文字位置")
    chk = Checks()
    ticks = (0, 100, 200)
    max_tick = 200
    last_offsets = {}

    for uniform in (False, True):
        notes = [FakeNote(tick, layer=0) for tick in ticks]
        virtual_layers = [{"vid": 0, "original_id": 0, "side": None,
                           "notes": notes, "inst": 0, "cat": "钢琴"}]
        layer_to_group = {0: 0}
        config = base_config()
        config["uniform_repeater_mode"] = uniform
        config["lyrics_import"] = [{"tick": tick, "text": lyric_char(index), "duration": 5}
                                   for index, tick in enumerate(ticks)]

        music_cmds, _ = build_all_commands_virtual(
            virtual_layers, layer_to_group, [(0, 0)], START, "east", max_tick,
            uniform_repeater_mode=uniform, group_staircase_modes={})
        bottom = find_structure_bottom(music_cmds)
        result = build_lyrics_chain(
            virtual_layers, {0: (0, 0)}, START, "east", max_tick, config,
            struct_bottom_y=bottom)

        # 链条层（组 Y = START[1]）上的方块坐标应与 event_offsets 推导的布局完全一致
        layout = event_offsets(chain_valid_ticks(virtual_layers, max_tick), uniform)
        chain_offsets = {x - START[0] for (x, y, z), _ in parse_blocks(music_cmds)
                         if y == START[1] and x >= START[0] - 1}
        expected = _expected_chain_offsets(layout, uniform)
        chk.ok(chain_offsets == expected,
               f"uniform={uniform}: 链条层坐标与布局不一致，"
               f"多出 {sorted(chain_offsets - expected)[:5]}，缺少 {sorted(expected - chain_offsets)[:5]}")

        last_relay = max(chain_offsets) + START[0]
        last_offsets[uniform] = layout[-1][1]
        chk.ok(last_relay - START[0] == layout[-1][1] + 1,
               f"uniform={uniform}: 链条末端偏移 {last_relay - START[0]} "
               f"!= 事件格 {layout[-1][1]} + 1")

        # 文字位置必须落在链条末端附近（合并模式下不能按「每 tick 2 格」漂走）
        coord, _, text = chain_entries(result["commands"])[0]
        lead = config["lyrics_lead_distance"]
        for segment, tick in zip(parse_segments(text)[0], ticks):
            abs_x = coord[0] + segment["x"]
            want = START[0] + dict(layout)[tick] + lead
            chk.ok(abs_x == want,
                   f"uniform={uniform} tick {tick}: 文字 x {abs_x} != {want}")
            chk.ok(abs_x <= last_relay + lead + 1,
                   f"uniform={uniform} tick {tick}: 文字漂到 {abs_x}（链条末端 {last_relay}）")

        # 时间轴：合并模式只压缩格数，事件格受电时间仍是 2 × (1 + tick - origin)
        powered = {}
        power = 0
        for (x, y, z), block in sorted(parse_blocks(music_cmds),
                                      key=lambda item: item[0][0]):
            if y != START[1] or x < START[0] - 1:
                continue
            if "repeater" in block:
                match = DELAY_RE.search(block)
                power += (int(match.group(1)) if match else 1) * 2
            else:
                powered[x - START[0]] = power
        for tick, offset in layout:
            chk.ok(powered.get(offset) == 2 * (1 + tick),
                   f"uniform={uniform} tick {tick}: 受电时间 {powered.get(offset)} "
                   f"!= {2 * (1 + tick)}")

    # 同一首歌：合并模式链条明显更短，均匀模式约 2 格/音乐 tick
    chk.ok(last_offsets[True] == 2 * max_tick,
           f"均匀模式事件格偏移应为 2×{max_tick}=400: {last_offsets[True]}")
    chk.ok(last_offsets[False] < last_offsets[True],
           f"合并模式链条未变短: {last_offsets}")
    chk.ok(last_offsets[False] < 2 * max_tick // 2,
           f"合并模式偏移应远小于 400: {last_offsets[False]}")
    return chk.report("repeater-modes")


# ── 测试 5：位置与结构 ───────────────────────────────────────

def test_layout_and_placement():
    print("测试 5：命令方块位置（结构下方、不与音乐链重叠）")
    chk = Checks()
    for style, extra in (("flat", {}), ("circle", {"layout_radius": 6}), ("square", {"layout_radius": 6})):
        world = make_world()
        world["config"].update({"layout_style": style, **extra})
        virtual_layers = world["virtual_layers"]
        layer_to_group = {0: 0, 1: 1}
        groups = sorted(set(layer_to_group.values()))
        offsets = compute_offsets(groups, world["config"], len(groups))
        group_offset_dict = {gid: offsets[index] for index, gid in enumerate(groups)}
        music_cmds, _ = build_all_commands_virtual(
            virtual_layers, layer_to_group,
            [group_offset_dict[gid] for gid in groups], START, "east", 30,
            group_staircase_modes={})
        bottom = find_structure_bottom(music_cmds)
        result = build_lyrics_chain(
            virtual_layers, group_offset_dict, START, "east", 30, world["config"],
            struct_bottom_y=bottom)
        chk.ok(result is not None, f"{style}: 歌词链未生成")
        if not result:
            continue
        coords = {coord for coord, _ in parse_blocks(result["commands"])}
        chk.ok(all(coord[1] <= bottom - 1 for coord in coords),
               f"{style}: 歌词命令方块高于结构底部")
        music_coords = {coord for coord, _ in parse_blocks(music_cmds)}
        chk.ok(not (coords & music_coords),
               f"{style}: 与音乐链坐标重叠 {sorted(coords & music_coords)[:3]}")

        # 文字锚点在主轨道上方
        block_coord, _, text = chain_entries(result["commands"])[0]
        segments, _ = parse_segments(text)
        ys = {block_coord[1] + segment["y"] for segment in segments}
        chk.ok(all(y >= START[1] for y in ys), f"{style}: 文字低于结构起始: {sorted(ys)}")
        chk.ok(len(ys) == 1, f"{style}: 文字高度应唯一: {sorted(ys)}")
    return chk.report("placement")


# ── 测试 6：超长命令分批 ─────────────────────────────────────

def test_batching():
    print("测试 6：超过命令长度上限时自动分批")
    chk = Checks()
    # 每个字都是长音 → 一字一句，1200 句足以超过单条命令上限
    ticks = list(range(0, 2400, 2))
    world = make_world(lyric_ticks=ticks, max_tick=30, lyrics_start_lead_ticks=0)
    result = world["result"]
    chk.ok(result is not None, "歌词链未生成")
    if not result:
        return chk.report("batch")

    chk.ok(result["batches"] > 1, f"应分批，实际 {result['batches']} 批")
    entries = chain_entries(result["commands"])
    chk.ok(len(entries) == result["batches"],
           f"命令数 {len(entries)} != 批数 {result['batches']}")
    for _, _, text in entries:
        chk.ok(len(text) <= MAX_CHAIN_COMMAND_LEN,
               f"命令长度 {len(text)} 超过上限 {MAX_CHAIN_COMMAND_LEN}")
        chk.ok(" id=" in text, "分批时必须带 id= 以免互相重置")

    ids = {option for _, _, text in entries for option in text.split(" ") if option.startswith("id=")}
    chk.ok(len(ids) == len(entries), f"分批 id 不唯一: {sorted(ids)}")

    # 段数守恒：所有批次加起来等于原始句子数
    total = sum(len(parse_segments(text)[0]) for _, _, text in entries)
    chk.ok(total == len(ticks), f"段总数 {total} != {len(ticks)}")

    # 分批后第一批的 start 偏移随批数增加，仍保持时间对齐
    first_segments, _ = parse_segments(entries[0][2])
    bias = result["batches"] + 1
    chk.ok(first_segments[0]["start"] == 2 * ticks[0] + bias,
           f"首批 start {first_segments[0]['start']} != {2 * ticks[0] + bias}")
    return chk.report("batch")


# ── 测试 7：v2 歌词模板解析 ──────────────────────────────────

def test_lyrics_json_v2():
    print("测试 7：编辑器 v2 歌词模板解析（数组 + lane 过滤）")
    chk = Checks()

    data = {
        "type": "noteblock-web-lyrics",
        "formatVersion": 2,
        "laneCount": 3,
        "tempo": 13,
        "lyrics": [
            {"tick": 10, "lane": 0, "text": "起", "duration": 4},
            {"tick": 14, "lane": 0, "text": "来", "duration": 12},
            {"tick": 20, "lane": 1, "text": "翻译", "duration": 4},
            {"tick": 24, "lane": 0, "text": "", "duration": 2},
            {"tick": 26, "lane": 2, "text": "注音", "duration": 2},
        ],
    }
    path = _write_temp_json(data)
    try:
        entries = load_lyrics_json(path)
        chk.ok(entries == [(10, "起", 4), (14, "来", 12)],
               f"v2 解析结果错误: {entries}")
    finally:
        os.remove(path)

    # 旧版对象格式（v1）应给出明确报错
    old = _write_temp_json({"type": "noteblock-web-lyrics",
                            "lyrics": {"10": {"text": "x", "duration": 1}}})
    try:
        try:
            load_lyrics_json(old)
            chk.ok(False, "旧版 v1 格式应报错")
        except ValueError as exc:
            chk.ok("formatVersion 2" in str(exc), f"旧版报错信息不清晰: {exc}")
    finally:
        os.remove(old)

    # 一字一条目：多字条目直接报错
    multi = _write_temp_json({"type": "noteblock-web-lyrics", "formatVersion": 2,
                              "lyrics": [{"tick": 0, "lane": 0, "text": "今天", "duration": 4}]})
    try:
        try:
            load_lyrics_json(multi)
            chk.ok(False, "多字条目应报错")
        except ValueError as exc:
            chk.ok("一字一条记录" in str(exc), f"多字报错信息不清晰: {exc}")
    finally:
        os.remove(multi)

    # duration 为 0 也应报错（不再有全局兜底时长）
    zero = _write_temp_json({"type": "noteblock-web-lyrics", "formatVersion": 2,
                             "lyrics": [{"tick": 0, "lane": 0, "text": "今", "duration": 0}]})
    try:
        try:
            load_lyrics_json(zero)
            chk.ok(False, "duration = 0 应报错")
        except ValueError as exc:
            chk.ok("duration" in str(exc), f"duration 报错信息不清晰: {exc}")
    finally:
        os.remove(zero)

    # 空格条目合法（可见字符 1 个），颜色码不占字符
    space = _write_temp_json({"type": "noteblock-web-lyrics", "formatVersion": 2,
                              "lyrics": [{"tick": 0, "lane": 0, "text": " ", "duration": 2},
                                         {"tick": 2, "lane": 0, "text": "§6今", "duration": 2}]})
    try:
        entries = load_lyrics_json(space)
        chk.ok(entries == [(0, " ", 2), (2, "§6今", 2)],
               f"空格 / 颜色码条目被错误处理: {entries}")
    finally:
        os.remove(space)
    return chk.report("v2json")


def _write_temp_json(data):
    import tempfile
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    try:
        json.dump(data, handle, ensure_ascii=False)
    finally:
        handle.close()
    return handle.name


# ── 测试 8：顺序系统音色合并 ──────────────────────────────────

def test_sequential_inst_grouping():
    print("测试 8：顺序系统音色合并（相邻同音色、每组最多 3 条）")
    chk = Checks()
    instruments = [0, 0, 0, 0, 1, 0, 0, 2, 2]
    layers = [{"vid": i, "original_id": i, "side": None, "inst": inst, "cat": f"c{inst}"}
              for i, inst in enumerate(instruments)]
    layer_to_group = auto_group_by_mode_on_layers(layers, 3)

    grouped = {}
    for vid, gid in layer_to_group.items():
        grouped.setdefault(gid, []).append(vid)
    result = [sorted(grouped[key]) for key in sorted(grouped)]
    chk.ok(result == [[0, 1, 2], [3], [4], [5, 6], [7, 8]],
           f"分组结果错误: {result}")
    chk.ok(all(len(members) <= 3 for members in result), "存在超过 3 条的组")
    for members in result:
        chk.ok(len({instruments[vid] for vid in members}) == 1, f"组内音色不一致: {members}")
        chk.ok(members == list(range(members[0], members[0] + len(members))),
               f"组内轨道不连续: {members}")
    return chk.report("seq-group")


# ── 测试 9：歌词晚于歌曲结尾 ─────────────────────────────────

def test_lyrics_beyond_song_length():
    print("测试 9：歌词晚于歌曲结尾时应全部生成")
    chk = Checks()
    lyric_ticks = (0, 5, 10, 20, 30, 40)
    world = make_world(lyric_ticks=lyric_ticks, max_tick=20, lyrics_start_lead_ticks=0)
    result = world["result"]
    chk.ok(result is not None, "歌词链未生成")
    if not result:
        return chk.report("beyond-song")

    chk.ok(result["end_tick"] == 40, f"end_tick {result['end_tick']} != 40")
    _, _, text = chain_entries(result["commands"])[0]
    segments, _ = parse_segments(text)
    chk.ok(len(segments) == len(lyric_ticks),
           f"尾段歌词被截断：只生成 {len(segments)} 条，应为 {len(lyric_ticks)} 条")
    chk.ok(segments[-1]["text"] == lyric_char(len(lyric_ticks) - 1),
           f"最后一条歌词错误: {segments[-1]['text']}")
    # 最后一条仍按 2 tick/音乐 tick 前进
    chk.ok(segments[-1]["start"] == 2 * lyric_ticks[-1] + 2,
           f"末段 start {segments[-1]['start']} 未对齐")
    return chk.report("beyond-song")


# ── 测试 10：GBK 控制台安全 ──────────────────────────────────

def test_console_encoding():
    print("测试 10：Windows GBK 控制台输出安全")
    chk = Checks()
    for label, text in (("success", Ansi.success("ok")), ("error", Ansi.error("bad")),
                        ("title", Ansi.title("标题")), ("prompt", Ansi.prompt("提示"))):
        try:
            text.encode("gbk")
            chk.ok(True, "")
        except UnicodeEncodeError:
            chk.ok(False, f"{label} 输出无法用 GBK 编码: {text!r}")
    return chk.report("encoding")


# ── 测试 11：完整命令序列（启动链 + 歌词 + 粒子）────────────

class FakeHeader:
    song_name = "test"
    original_author = "tester"
    tempo = 13.0


class FakeLayer:
    def __init__(self, layer_id, name):
        self.id = layer_id
        self.name = name
        self.volume = 100
        self.panning = 0


class FakeSong:
    def __init__(self, notes, layers):
        self.header = FakeHeader()
        self.notes = notes
        self.layers = layers


def _abs_rel(token, origin):
    return origin if token == "~" else origin + int(token[1:])


def test_full_structure():
    from miaunoteblock.app import build_commands

    print("测试 11：完整命令序列（启动链 / 歌词 / 粒子一起生成）")
    chk = Checks()
    virtual_layers, layer_to_group = make_layers()
    notes = [note for layer in virtual_layers for note in layer["notes"]]
    song = FakeSong(notes, [FakeLayer(0, "钢琴"), FakeLayer(1, "吉他")])
    config = base_config()
    config["lyrics_import"] = [{"tick": tick, "text": lyric_char(index), "duration": 5}
                               for index, tick in enumerate((0, 10, 20))]
    config["particles_enabled"] = True
    config["particles_layers"] = [0]

    commands = build_commands(config, song, virtual_layers, layer_to_group, 30)

    chains = chain_entries(commands)
    chk.ok(len(chains) == 1, f"歌词命令方块数 {len(chains)} != 1")
    block_map = block_commands(parse_blocks(commands))
    command_texts = list(block_map.values())
    chk.ok(sum(1 for text in command_texts if "/mpe noteblock chain" in text) == 1,
           "粒子命令方块数量不对")
    if not chains:
        return chk.report("full")

    # 启动链：每个目标格一条 setblock redstone_block，外加一条清除用的 air
    place_targets = []
    for coord, text in block_map.items():
        if not text.startswith("setblock ") or "minecraft:redstone_block" not in text:
            continue
        parts = text.split()
        target = tuple(_abs_rel(parts[index], coord[index - 1]) for index in (1, 2, 3))
        place_targets.append((coord, target))
    chk.ok(len(place_targets) == 1 + 2,
           f"启动链环数 {len(place_targets)} != 歌词 1 + 组 2")
    if len(place_targets) != 3:
        return chk.report("full")

    place_targets.sort(key=lambda item: item[0][1])
    dx, _, dz = DIR_OFFSET["east"]
    lyric_coord = chains[0][0]
    lyric_activation = (lyric_coord[0] + dx, lyric_coord[1], lyric_coord[2] + dz)

    # 歌词的激活红石块必须在第一环（最早执行），否则 start 对齐会整体偏移
    chk.ok(place_targets[0][1] == lyric_activation,
           f"第一环放置 {place_targets[0][1]} 不是歌词激活点 {lyric_activation}")
    blocks = dict(parse_blocks(commands))
    first_block = blocks.get(place_targets[0][0], "")
    chk.ok("minecraft:command_block[facing=up]" in first_block,
           f"第一环应为脉冲命令方块: {first_block[:70]}")
    for coord, _ in place_targets[1:]:
        chk.ok("minecraft:chain_command_block" in blocks.get(coord, ""),
               f"后续环应为链式命令方块: {blocks.get(coord, '')[:70]}")

    # 清除用的红石块与放置用的一一对应
    clear_targets = [text for text in command_texts
                     if text.startswith("setblock ") and text.endswith("minecraft:air")]
    chk.ok(len(clear_targets) == len(place_targets),
           f"清除命令 {len(clear_targets)} != 放置命令 {len(place_targets)}")
    return chk.report("full")


def main():
    results = [
        test_command_shape(),
        test_timing_and_position(),
        test_start_lead(),
        test_text_sanitize(),
        test_char_sentences(),
        test_lead_and_jitter(),
        test_repeater_modes(),
        test_layout_and_placement(),
        test_batching(),
        test_lyrics_json_v2(),
        test_sequential_inst_grouping(),
        test_lyrics_beyond_song_length(),
        test_console_encoding(),
        test_full_structure(),
    ]
    print()
    if all(results):
        print("全部通过")
        return 0
    print("存在失败项")
    return 1


if __name__ == "__main__":
    sys.exit(main())
