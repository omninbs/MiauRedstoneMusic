"""歌词导入、流式合并与歌词命令方块轨道生成。"""

import json
from typing import List, Tuple

from .config import save_config
from .constants import Ansi, DIR_OFFSET, OPPOSITE_FACING, SIDE_AXIS, TEXT_TOWARDS_YAW
from .generation import split_to_repeaters

DEFAULT_STREAM_SECONDS = 2.0
DEFAULT_STREAM_MAX_LEN = 18

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

def _anim_options(config: dict) -> Tuple[int, int]:
    return (max(0, int(config.get("lyrics_enter", 5))),
            max(0, int(config.get("lyrics_exit", 5))))

def build_plain_mapping(entries: List[Tuple[int, str, int]], config: dict) -> List[dict]:
    """不使用流式时的映射：每条文字独立显示。"""
    enter, exit_time = _anim_options(config)
    return [
        {"tick": tick, "text": text, "duration": duration,
         "enter": enter, "exit": exit_time, "clear": False}
        for tick, text, duration in entries
    ]

def build_stream_mapping(entries: List[Tuple[int, str, int]], config: dict, tempo: float) -> List[dict]:
    """流式映射：同一段内的事件逐字累积显示。

    段内第 i 条事件显示「从段首累积到当前」的整串文字，并且在显示前清除旧文字，
    因此画面效果是文字不断变长（A -> AB -> ABC），而不是后一个字把前一个字顶掉。
    """
    enter, exit_time = _anim_options(config)
    try:
        max_len = int(config.get("lyrics_stream_max_len", DEFAULT_STREAM_MAX_LEN))
    except (TypeError, ValueError):
        max_len = DEFAULT_STREAM_MAX_LEN
    max_len = max(1, max_len)
    threshold = stream_threshold_ticks(config, tempo)
    mapping = []
    for seg in build_stream_segments(entries, threshold, max_len):
        accumulated = ""
        alone = len(seg) == 1
        for i, (tick, text, duration) in enumerate(seg):
            accumulated += text
            last = i == len(seg) - 1
            if alone:
                item_enter, item_exit, item_duration = enter, exit_time, duration
            elif last:
                # 段尾文字保留到自身时长结束，之后自然淡出
                item_enter, item_exit, item_duration = 0, exit_time, duration
            else:
                # 中间文字紧接着就会被下一条清除，不需要入场/出场动画
                item_enter, item_exit = 0, 0
                item_duration = (seg[i + 1][0] - tick) + 5
            mapping.append({"tick": tick, "text": accumulated, "duration": item_duration,
                            "enter": item_enter, "exit": item_exit, "clear": True})
    return mapping

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
    with open(path, 'r', encoding='utf-8') as f:
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

def build_lyrics_commands(
    lyrics_mapping: List[dict],
    base: Tuple[int, int, int],
    direction: str,
    max_tick: int,
    config: dict,
    use_lamp: bool = False,
    uniform_repeater_mode: bool = False,
    anchor_side: int = None,
    anchor_chain: int = None,
    anchor_y: int = None,
    origin_tick: int = None,
    fixed_tick: int = None,
    chain_y: int = None
) -> List[str]:
    """生成歌词命令方块链。

    lyrics_mapping 为订阅项列表，每项为 dict：
    {"tick", "text", "duration", "enter", "exit", "clear"}；
    duration 为 0 时使用全局设置。
    clear=True 表示流式累积文字：事件格放「清除全部文字」命令，
    紧邻其下的链式命令方块在同一 tick 显示累积文字，避免多个文字重叠。
    anchor_side / anchor_chain / anchor_y 用于把文字固定到主轨道上方：
    - anchor_side：文字在侧向轴（垂直于链条）上的坐标，None 表示跟随命令方块
    - anchor_chain：文字在链条轴上的坐标，None 表示跟随命令方块向前移动
    - anchor_y：文字高度，None 表示以链条起点 Y 为基准加上 lyrics_y_offset
    这些锚点是内部绝对坐标，最终会换算成相对命令方块自身位置的 ~~~ 偏移写入命令，
    因此结构被移动到任何位置，文字都会显示在正确的地方。
    origin_tick：主链条的时间原点（全局第一个 tick），None 表示以歌词自身的第一个 tick 为原点。
    会在该 tick 处补一个空白事件格，使歌词链与音乐链的计时原点一致
    fixed_tick：文字不跟随时，取最接近该 tick 的事件格的链轴坐标作为固定位置
    chain_y：命令方块链所在的红石层 Y；None 表示链条起点下方 3 格
    """
    dx, _, dz = DIR_OFFSET[direction]
    # 中继器 facing 指向「输入端」：信号只能从 facing 一侧输入、反方向输出。
    # 要让信号沿链延伸方向传播，输入端必须落在起点一侧，故 facing 取延伸方向的反方向，
    # 与音乐链（build_all_commands_virtual）保持一致。
    facing = OPPOSITE_FACING[direction]
    side_axis = SIDE_AXIS[direction]
    base_x, base_y, base_z = base
    current_y = chain_y if chain_y is not None else base_y - 3
    base_block = "minecraft:redstone_lamp" if use_lamp else "minecraft:white_wool"
    mapping = {item["tick"]: item for item in lyrics_mapping}

    commands = []
    side_offset = int(config.get("lyrics_side_offset") or 0)
    global_duration = max(0, int(config.get("lyrics_duration", 20)))

    def add_support(x: int, z: int):
        commands.append(f"setblock {x} {current_y - 1} {z} {base_block}")

    def rel_coord(d: int) -> str:
        return "~" if d == 0 else f"~{d}"

    def command_block(x: int, z: int, entry: dict):
        if side_axis == "z":
            chain_coord = anchor_chain if anchor_chain is not None else x
            side_coord = (anchor_side if anchor_side is not None else z) + side_offset
            display_x, display_z = chain_coord, side_coord
        else:
            chain_coord = anchor_chain if anchor_chain is not None else z
            side_coord = (anchor_side if anchor_side is not None else x) + side_offset
            display_x, display_z = side_coord, chain_coord
        display_y = anchor_y if anchor_y is not None else base_y + int(config.get("lyrics_y_offset", 5))
        text_duration = entry["duration"] if entry["duration"] and entry["duration"] > 0 else global_duration
        options = [
            f"color={config.get('lyrics_color', 'white')}",
            f"scale={max(0.05, min(64.0, float(config.get('lyrics_scale', 10.0))))}",
            f"force=true",
            f"duration={text_duration}",
            f"enter={max(0, int(entry.get('enter', 5)))}",
            f"exit={max(0, int(entry.get('exit', 5)))}",
            f"towards={TEXT_TOWARDS_YAW[direction]},0,0"
        ]
        if entry.get("clear"):
            # 流式歌词：事件格先清除全部旧文字，下方链式命令方块同一 tick 显示累积文字。
            # 文字命令的 ~~~ 相对坐标基于链式命令方块自身位置 (x, current_y-1, z) 计算，
            # 这样结构被移动到任何位置文字都能正确显示。
            block_x, block_y, block_z = x, current_y - 1, z
            clear_nbt = json.dumps("/mpe clear text", ensure_ascii=False)
            text_cmd = f"/mpe text {json.dumps(entry['text'], ensure_ascii=False)} {rel_coord(display_x - block_x)} {rel_coord(display_y - block_y)} {rel_coord(display_z - block_z)} {' '.join(options)}"
            nbt = json.dumps(text_cmd, ensure_ascii=False)
            commands.append(f"setblock {x} {current_y} {z} minecraft:command_block[facing=down]{{Command:{clear_nbt}}}")
            commands.append(
                f"setblock {x} {current_y - 1} {z} "
                f"minecraft:chain_command_block[facing=down,conditional=false]{{Command:{nbt},auto:1b}}"
            )
        else:
            # 非流式：文字命令的 ~~~ 相对坐标基于命令方块自身位置 (x, current_y, z) 计算
            block_x, block_y, block_z = x, current_y, z
            text_cmd = f"/mpe text {json.dumps(entry['text'], ensure_ascii=False)} {rel_coord(display_x - block_x)} {rel_coord(display_y - block_y)} {rel_coord(display_z - block_z)} {' '.join(options)}"
            nbt = json.dumps(text_cmd, ensure_ascii=False)
            commands.append(f"setblock {x} {current_y} {z} minecraft:command_block[facing={facing}]{{Command:{nbt}}}")

    # 歌词事件完全由导入的歌词决定（不再依赖某条 NBS 音符轨）
    valid_ticks = sorted({tick for tick in mapping if 0 <= tick <= max_tick})
    if origin_tick is not None and 0 <= origin_tick <= max_tick:
        # 主链条的时间原点：并入有效 tick，使歌词链与音乐链的计时起点一致
        valid_ticks = sorted(set(valid_ticks) | {origin_tick})
        early_ticks = [tick for tick in valid_ticks if tick < origin_tick]
        if early_ticks:
            print(Ansi.error(f"警告：{len(early_ticks)} 个事件早于音乐起点 tick {origin_tick}，已跳过"))
            valid_ticks = [tick for tick in valid_ticks if tick >= origin_tick]
    if max_tick not in valid_ticks:
        valid_ticks.append(max_tick)
    valid_ticks.sort()
    if not valid_ticks:
        return []

    # 固定文字位置：预先按实际链条布局求出最接近 fixed_tick 的事件格坐标
    # （每个空白 tick 的中继器最多覆盖 4 tick、占 2 格，不能按 tick 数直接换算）
    if fixed_tick is not None and anchor_chain is None:
        cursor = 0
        best_offset = 0
        best_dist = None
        prev = None
        for tick in valid_ticks:
            if prev is not None:
                blank_ticks = max(0, tick - prev - 1)
                delays = [1] * blank_ticks if uniform_repeater_mode else split_to_repeaters(blank_ticks)
                cursor += 2 + 2 * len(delays)   # 事件格后的中继器 + 空白中继器对 + 下一个事件格
            dist = abs(tick - fixed_tick)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_offset = cursor
            prev = tick
        if side_axis == "z":
            anchor_chain = base_x + best_offset * dx
        else:
            anchor_chain = base_z + best_offset * dz

    # 与音乐链完全一致的链条结构：
    #   事件格自身占 1 红石 tick（其后紧跟一个 delay=1 中继器），
    #   空白 tick 用 [连接羊毛 + 中继器] 填充（每个中继器最多覆盖 4 tick）
    start_x = base_x - dx
    start_z = base_z - dz
    commands.append(f"setblock {start_x} {current_y} {start_z} minecraft:repeater[facing={facing},delay=1]")
    add_support(start_x, start_z)
    previous_tick = None
    current_x, current_z = start_x, start_z

    for tick in valid_ticks:
        blank_ticks = 0 if previous_tick is None else max(0, tick - previous_tick - 1)
        extra_delays = [1] * blank_ticks if uniform_repeater_mode else split_to_repeaters(blank_ticks)
        for delay in extra_delays:
            connector_x, connector_z = current_x + dx, current_z + dz
            repeater_x, repeater_z = connector_x + dx, connector_z + dz
            add_support(connector_x, connector_z)
            add_support(repeater_x, repeater_z)
            # 链层连接方块：与音乐链一致，中继器之间靠羊毛传导
            commands.append(f"setblock {connector_x} {current_y} {connector_z} minecraft:white_wool")
            commands.append(f"setblock {repeater_x} {current_y} {repeater_z} minecraft:repeater[facing={facing},delay={delay}]")
            current_x, current_z = repeater_x, repeater_z

        event_x, event_z = current_x + dx, current_z + dz
        entry = mapping.get(tick)
        if entry is None:
            add_support(event_x, event_z)
            commands.append(f"setblock {event_x} {current_y} {event_z} {base_block}")
        else:
            # 流式事件格的下方要让位给链式命令方块，跳过支撑方块
            if not entry.get("clear"):
                add_support(event_x, event_z)
            command_block(event_x, event_z, entry)
        current_x, current_z = event_x, event_z

        # 事件格占用 1 红石 tick：与音乐链一致，紧邻其后放一个 delay=1 中继器
        relay_x, relay_z = current_x + dx, current_z + dz
        add_support(relay_x, relay_z)
        commands.append(f"setblock {relay_x} {current_y} {relay_z} minecraft:repeater[facing={facing},delay=1]")
        current_x, current_z = relay_x, relay_z
        previous_tick = tick

    return commands

# ── 歌词链整体构建（供生成流程与测试复用）────────────────────

def build_lyrics_track(
    virtual_layers: List[dict],
    group_offset_dict: dict,
    start: Tuple[int, int, int],
    direction: str,
    max_tick: int,
    config: dict,
    use_lamp: bool = False,
    uniform_repeater_mode: bool = False,
    struct_bottom_y: int = None,
    tempo: float = None
):
    """构建整条歌词命令方块轨道。

    返回 (commands, activation_position, chain_y)；
    未导入歌词时返回 ([], None, None)，即「导入歌词 = 启用歌词功能」。

    歌词链独立计算，不参与排版（不是一条正常音轨）：
    - 侧向位置固定在侧向偏移 0 的「最前面一条线」上；
    - 纵向位置 = 整个结构最低方块 - config["lyrics_chain_y_offset"]（默认 1，紧贴结构下方）；
    - 文字显示位置由内部绝对锚点换算成相对命令方块的 ~~~ 偏移，
      与命令方块链所在高度无关，结构移动后文字仍正确。

    tempo 为歌曲初始速度（note/s），用于把流式合并秒数换算成 tick 阈值。
    """
    imported_lyrics = config.get("lyrics_import") or {}
    if not imported_lyrics:
        return [], None, None

    start_x, start_y, start_z = start
    dx, _, dz = DIR_OFFSET[direction]
    side_axis = SIDE_AXIS[direction]

    # 链条起点固定在侧向偏移 0 的线上，纵向高度后面单独按结构底部计算
    lyr_base = (start_x, start_y, start_z)

    # 时间原点：与音乐链一致，取全局首个有效 tick
    global_ticks = {n.tick for vl in virtual_layers
                    for n in vl['notes'] if 0 <= n.tick <= max_tick}
    if max_tick not in global_ticks:
        global_ticks.add(max_tick)
    origin_tick = min(global_ticks)

    # 先收集全部导入的歌词事件（不按 max_tick 截断上界）
    all_entries = []
    for tick_key, entry in imported_lyrics.items():
        if not isinstance(entry, dict):
            continue
        try:
            tick_value = int(tick_key)
        except (TypeError, ValueError):
            continue
        text = entry.get("text", "")
        if not text:
            continue
        all_entries.append((tick_value, text, int(entry.get("duration") or 0)))
    if not all_entries:
        print(Ansi.error("没有有效歌词，跳过歌词生成"))
        return [], None, None

    # 歌词链的结束 tick 至少覆盖到最后一条歌词：
    # 歌曲最后一个音符之后往往还有歌词，若仍以 max_tick 截断，尾段歌词会整段丢失。
    last_lyric_tick = max(item[0] for item in all_entries)
    lyr_max_tick = max(max_tick, last_lyric_tick)
    if last_lyric_tick > max_tick:
        print(Ansi.info(f"歌词超出歌曲长度（最后一条 tick {last_lyric_tick}），"
                        f"歌词链自动延伸到 tick {lyr_max_tick}"))

    # 只保留会被生成的范围（origin_tick ~ lyr_max_tick）
    dropped_early = [t for t, _, _ in all_entries if t < origin_tick]
    if dropped_early:
        print(Ansi.error(f"警告：{len(dropped_early)} 条歌词早于音乐起点 tick {origin_tick}，已跳过"))
    entries = sorted([item for item in all_entries if item[0] >= origin_tick],
                     key=lambda item: item[0])
    if not entries:
        print(Ansi.error("没有落在生成范围内的歌词，跳过歌词生成"))
        return [], None, None

    # 文字锚点：主轨道上方（非平铺用核心组，平铺用结构侧向中心）
    style = config.get("layout_style", "flat")
    master = config.get("master_group")
    if style != "flat" and master is not None and master in group_offset_dict:
        anchor_y_off, anchor_side_off = group_offset_dict[master]
    else:
        side_list = [off[1] for off in group_offset_dict.values()]
        anchor_y_off = 0
        anchor_side_off = (min(side_list) + max(side_list)) // 2
    anchor_y = start_y + anchor_y_off + int(config.get("lyrics_y_offset", 5))
    if side_axis == "z":
        anchor_side = start_z + anchor_side_off
    else:
        anchor_side = start_x + anchor_side_off
    follow = config.get("lyrics_follow", True)
    fixed_tick = None if follow else origin_tick + (max_tick - origin_tick) // 2

    # 命令方块链纵向位置：默认整个结构最低方块下方（相对结构计算）
    if struct_bottom_y is None:
        struct_bottom_y = min(start_y + off[0] for off in group_offset_dict.values()) - 2
    raw_chain_off = config.get("lyrics_chain_y_offset", 1)
    chain_offset = int(raw_chain_off) if raw_chain_off is not None else 1
    chain_y = struct_bottom_y - chain_offset

    if config.get("lyrics_stream"):
        lyrics_mapping = build_stream_mapping(entries, config, tempo)
        print(Ansi.info(f"流式歌词已启用：{len(entries)} 条事件 -> {len(lyrics_mapping)} 个显示点"))
    else:
        lyrics_mapping = build_plain_mapping(entries, config)
        print(Ansi.info(f"使用导入的歌词，共 {len(lyrics_mapping)} 条"))

    commands = build_lyrics_commands(
        lyrics_mapping, lyr_base, direction, lyr_max_tick, config,
        use_lamp=use_lamp,
        uniform_repeater_mode=uniform_repeater_mode,
        anchor_side=anchor_side,
        anchor_chain=None,
        anchor_y=anchor_y,
        origin_tick=origin_tick,
        fixed_tick=fixed_tick,
        chain_y=chain_y,
    )
    # 歌词链启动点（红石块）：位于链条起手中继器后方两格
    activation = (lyr_base[0] - 2 * dx, chain_y, lyr_base[2] - 2 * dz)
    print(Ansi.success(f"歌词命令方块链已添加 ({len(commands)} 条命令)，链层 Y={chain_y}"))
    return commands, activation, chain_y

# ── 歌词设置 UI ──────────────────────────────────────────────

def import_lyrics_json(config: dict, tempo: float = None):
    """导入歌词 JSON 或清空已导入的歌词（导入即启用歌词功能）"""
    print(Ansi.title("\n=== 歌词导入 / 清空 ==="))
    print(Ansi.info("支持编辑器导出的歌词文件（noteblock-web-lyrics, formatVersion 2）。"))
    print(Ansi.dim("编辑器有 3 个文本轨道（lane），本工具只识别 lane 0，lane 1/2 会被忽略。"))
    print(Ansi.dim("导入歌词即代表启用歌词功能；清空后歌词功能关闭。"))
    current = config.get("lyrics_import") or {}
    if current:
        print(Ansi.info(f"当前已导入 {len(current)} 条歌词"))
    else:
        print(Ansi.dim("当前没有导入歌词"))

    raw = input(Ansi.prompt("请输入 JSON 文件路径（c=清空歌词，直接回车=取消）: ")).strip().strip('"').strip("'")
    if not raw:
        print(Ansi.info("已取消"))
        return
    if raw.lower() in ("c", "clear"):
        if not current:
            print(Ansi.info("当前没有歌词，无需清空"))
            return
        config["lyrics_import"] = {}
        save_config(config)
        print(Ansi.success("已清空歌词，歌词功能已关闭"))
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

    config["lyrics_import"] = {
        str(tick): {"text": text, "duration": duration} for tick, text, duration in entries
    }
    print(Ansi.success(f"已导入 {len(entries)} 条歌词"))
    for tick, text, duration in entries[:5]:
        print(Ansi.dim(f"  tick {tick}: {text} (duration={duration})"))
    if len(entries) > 5:
        print(Ansi.dim(f"  …… 其余 {len(entries) - 5} 条"))

    _ask_stream_settings(config, tempo, entries)
    save_config(config)

def _ask_stream_settings(config: dict, tempo: float, entries: List[Tuple[int, str, int]]):
    """导入歌词后询问是否启用流式歌词及其参数。"""
    print(Ansi.title("\n=== 流式歌词 ==="))
    print(Ansi.info("启用后，间隔很近的连续文字会累积显示（A -> AB -> ABC），"))
    print(Ansi.info("而不是后一个字把前一个字顶掉；间隔较大的文字仍分段显示。"))
    current = config.get("lyrics_stream", False)
    ans = input(Ansi.prompt(f"是否启用流式歌词 (y/n，回车={'启用' if current else '不启用'}): ")).strip().lower()
    if ans in ("y", "yes"):
        config["lyrics_stream"] = True
    elif ans in ("n", "no"):
        config["lyrics_stream"] = False
    if not config.get("lyrics_stream"):
        print(Ansi.dim("未启用流式歌词"))
        return

    seconds = config.get("lyrics_stream_seconds", DEFAULT_STREAM_SECONDS)
    sec_in = input(Ansi.prompt(f"自动合并的时间阈值（秒，回车={seconds}）: ")).strip()
    if sec_in:
        try:
            config["lyrics_stream_seconds"] = max(0.01, float(sec_in))
        except ValueError:
            print(Ansi.error("必须是数字，未修改"))

    limit = config.get("lyrics_stream_max_len", DEFAULT_STREAM_MAX_LEN)
    limit_in = input(Ansi.prompt(f"单段累积文字上限（字，回车={limit}）: ")).strip()
    if limit_in:
        try:
            config["lyrics_stream_max_len"] = max(1, int(limit_in))
        except ValueError:
            print(Ansi.error("必须是整数，未修改"))

    rate = tempo if tempo else 10.0
    threshold = stream_threshold_ticks(config, tempo)
    max_len = int(config.get("lyrics_stream_max_len", DEFAULT_STREAM_MAX_LEN))
    segments = build_stream_segments(entries, threshold, max_len)
    print(Ansi.info(f"阈值 ≈ {config['lyrics_stream_seconds']} 秒 × {rate} note/s = {threshold} tick"))
    print(Ansi.success(f"共分析出 {len(segments)} 段流式歌词"))

def edit_lyrics_settings(config: dict):
    """交互式修改歌词显示参数"""
    print(Ansi.title("\n=== 歌词显示参数 ==="))
    print(Ansi.info("直接回车保留当前值。"))

    follow = config.get("lyrics_follow", True)
    print(Ansi.dim(f"当前文字位置: {'随音乐沿结构上方前进' if follow else '固定在结构纵向中心'}"))
    follow_in = input(Ansi.prompt("文字是否跟随向前移动 (y/n，回车=不修改): ")).strip().lower()
    if follow_in in ("y", "yes"):
        config["lyrics_follow"] = True
    elif follow_in in ("n", "no"):
        config["lyrics_follow"] = False

    color = input(Ansi.prompt(f"颜色 ({config.get('lyrics_color', 'white')}): ")).strip()
    if color:
        config["lyrics_color"] = color

    scale = input(Ansi.prompt(f"字形高度 scale ({config.get('lyrics_scale', 10.0)}): ")).strip()
    if scale:
        try:
            config["lyrics_scale"] = max(0.05, min(64.0, float(scale)))
        except ValueError:
            print(Ansi.error("scale 必须是数字，未修改"))

    duration = input(Ansi.prompt(f"默认显示时长 duration ({config.get('lyrics_duration', 20)}): ")).strip()
    if duration:
        try:
            config["lyrics_duration"] = max(0, int(duration))
        except ValueError:
            print(Ansi.error("duration 必须是整数，未修改"))

    enter = input(Ansi.prompt(f"入场时长 enter ({config.get('lyrics_enter', 5)}): ")).strip()
    if enter:
        try:
            config["lyrics_enter"] = max(0, int(enter))
        except ValueError:
            print(Ansi.error("enter 必须是整数，未修改"))

    exit_in = input(Ansi.prompt(f"出场时长 exit ({config.get('lyrics_exit', 5)}): ")).strip()
    if exit_in:
        try:
            config["lyrics_exit"] = max(0, int(exit_in))
        except ValueError:
            print(Ansi.error("exit 必须是整数，未修改"))

    height = input(Ansi.prompt(f"文字相对主轨道的高度 ({config.get('lyrics_y_offset', 5)}): ")).strip()
    if height:
        try:
            config["lyrics_y_offset"] = int(height)
        except ValueError:
            print(Ansi.error("高度必须是整数，未修改"))

    side = config.get("lyrics_side_offset")
    side_in = input(Ansi.prompt(f"额外的侧向偏移 ({'未设置' if side is None else side}): ")).strip()
    if side_in:
        try:
            config["lyrics_side_offset"] = int(side_in)
        except ValueError:
            print(Ansi.error("侧向偏移必须是整数，未修改"))

    chain_off = config.get("lyrics_chain_y_offset", 1)
    print(Ansi.dim("命令方块链默认放在整个结构最底部（结构最低方块下方）。"))
    chain_in = input(Ansi.prompt(f"命令方块链距结构底部的距离（正=更靠下，当前 {chain_off}）: ")).strip()
    if chain_in:
        try:
            config["lyrics_chain_y_offset"] = int(chain_in)
        except ValueError:
            print(Ansi.error("距离必须是整数，未修改"))

    save_config(config)

