"""曲线音量排版 + 隐形矿车 + 红石模拟 的自检脚本。

不依赖 pytest，直接运行：

    python tests/test_curve_volume.py

覆盖内容：
1. 音量模型：左右各一盒、每盒 V/2 时，100% 音量对应 24 格。
2. 轨道几何：每个歌曲 tick 沿 +X 前进 3 格；相邻 tick 横向位移 ≤13 格；
   所有红石线/中继器都有垫底；普通轨道离轴 ≥24 格。
3. 模拟器语义：旧的线性排版仍然在 2×(1+tick) 受电。
4. 曲线排版端到端：生成 → 保存 .schem → 重新加载 → 模拟 → 逐 tick 比对，
   要求任意 t 都满足 Fnbs(t) == Fmc(t)，并且左右叠加后的音量符合 NBS 力度。
5. 主音轨固定满音量、上下分布；立体声对称；矿车与音符对齐。
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from miaunoteblock.curve import (
    MAX_LATERAL_STEP,
    MIN_DUAL_DISTANCE,
    MIN_Z,
    STEP,
    build_curve_commands,
    plan_curve,
)
from miaunoteblock.generation import (
    build_all_commands_virtual,
    generate_schematic_from_commands,
    note_to_pitch,
)
from miaunoteblock.simulate import (
    Simulator,
    check_crossing,
    check_dust_endpoints,
    check_power_paths,
    check_structure,
    compare,
    format_report,
    load_schem,
    world_from_commands,
)
from miaunoteblock.volume import (
    SOUND_RANGE,
    dual_distance,
    volume_from_distance,
)


class FakeNote:
    def __init__(self, tick, layer=0, key=45, instrument=0, velocity=100, panning=0):
        self.tick = tick
        self.layer = layer
        self.key = key
        self.instrument = instrument
        self.velocity = velocity
        self.panning = panning

    @property
    def pitch(self):
        return self.key


class Checks:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def ok(self, condition, message):
        if condition:
            self.passed += 1
        else:
            self.failed.append(message)

    def report(self, name):
        status = "OK" if not self.failed else "FAIL"
        print(f"  [{name}] {status}  通过断言 {self.passed} 条")
        for message in self.failed:
            print(f"      × {message}")
        return not self.failed


def base_config(**overrides):
    config = {
        "layout_style": "curve",
        "curve_lane_spacing": 3,
        "curve_minecart": True,
        "curve_master_layers": [],
        "curve_master_spacing": 3,
        "curve_corridor_height": 2,
        "uniform_repeater_mode": False,
        "group_staircase_modes": {},
        "stereo_layers": [],
        "lyrics_import": [],
        "particles_enabled": False,
    }
    config.update(overrides)
    return config


# ── 测试 1：音量模型 ────────────────────────────────────────

def test_volume_model():
    print("测试 1：音量模型（左右叠加）")
    chk = Checks()
    chk.ok(abs(dual_distance(1.0) - MIN_DUAL_DISTANCE) < 1e-9,
           f"满音量双盒距离不是 {MIN_DUAL_DISTANCE}")
    chk.ok(abs(dual_distance(0.0) - SOUND_RANGE) < 1e-9, "0 音量双盒距离不是 48")
    chk.ok(abs(dual_distance(0.5) - 36.0) < 1e-9, "50% 音量双盒距离不是 36")
    chk.ok(abs(volume_from_distance(24.0) - 0.5) < 1e-9, "24 格单盒不是 50%")
    chk.ok(abs(volume_from_distance(0.0) - 1.0) < 1e-9, "0 格不是 100%")
    return chk.report("volume")


# ── 测试 2：轨道几何 ────────────────────────────────────────

def make_curve_layers():
    """两组：钢琴作为主音轨；吉他（立体声）与铜号角作为普通轨道。"""
    piano = [
        FakeNote(0, 0, key=45, instrument=0, velocity=20),
        FakeNote(2, 0, key=47, instrument=0, velocity=60),
        FakeNote(3, 0, key=50, instrument=0, velocity=100),
        FakeNote(8, 0, key=57, instrument=0, velocity=40),
    ]
    guitar = [
        FakeNote(0, 1, key=40, instrument=5, velocity=100),
        FakeNote(0, 1, key=40, instrument=5, velocity=100),
        FakeNote(4, 1, key=43, instrument=5, velocity=70),
        FakeNote(4, 1, key=43, instrument=5, velocity=70),
        FakeNote(8, 1, key=45, instrument=5, velocity=100),
        FakeNote(8, 1, key=45, instrument=5, velocity=100),
    ]
    horn_a = [FakeNote(0, 2, key=60, instrument=16, velocity=60),
              FakeNote(2, 2, key=62, instrument=16, velocity=40),
              FakeNote(6, 2, key=64, instrument=16, velocity=30),
              FakeNote(7, 2, key=65, instrument=16, velocity=50)]
    horn_b = [FakeNote(0, 3, key=67, instrument=16, velocity=55),
              FakeNote(2, 3, key=69, instrument=16, velocity=45),
              FakeNote(6, 3, key=71, instrument=16, velocity=35),
              FakeNote(7, 3, key=72, instrument=16, velocity=45)]
    virtual_layers = [
        {"vid": 0, "original_id": 0, "side": None, "notes": piano, "inst": 0, "cat": "钢琴"},
        {"vid": 1, "original_id": 1, "side": "stereo", "notes": guitar, "inst": 5, "cat": "吉他"},
        {"vid": 2, "original_id": 2, "side": None, "notes": horn_a, "inst": 16, "cat": "铜号角"},
        {"vid": 3, "original_id": 3, "side": None, "notes": horn_b, "inst": 16, "cat": "铜号角"},
    ]
    layer_to_group = {0: 0, 1: 1, 2: 2, 3: 2}
    return virtual_layers, layer_to_group


def test_path_geometry():
    print("测试 2：轨道几何（2 格/tick ABAB / 红石线 ≤15 / 无悬空 / 离轴 ≥24）")
    chk = Checks()
    virtual_layers, layer_to_group = make_curve_layers()
    config = base_config(curve_master_layers=[0])
    commands, activations, meta = build_curve_commands(
        virtual_layers, layer_to_group, (0, 64, 0), 8, config)
    plan = meta["plan"]

    chk.ok(not meta["conflicts"], f"方块冲突 {meta['conflicts'][:3]}")
    world = world_from_commands(commands)
    for key, nodes in plan["lanes"].items():
        for index in range(1, len(nodes)):
            chk.ok(nodes[index]["x"] - nodes[index - 1]["x"] == STEP,
                   f"{key} tick {nodes[index]['tick']}: x 步长不是 {STEP}")
            delta = abs(nodes[index]["z"] - nodes[index - 1]["z"])
            chk.ok(delta <= MAX_LATERAL_STEP,
                   f"{key} tick {nodes[index]['tick']}: 横向位移 {delta} > {MAX_LATERAL_STEP}")
        # 严格 ABAB：中继器与音符盒同 Z，红石粉只接中继器背面
        for index, node in enumerate(nodes):
            relay = world.get((node["x"], node["y"], node["z"]))
            chk.ok(relay is not None and relay["id"] == "repeater",
                   f"{key} tick {node['tick']}: X={node['x']} 不是中继器")
            body = world.get((node["x"] + 1, node["y"], node["z"]))
            chk.ok(body is not None and body["id"] in ("note_block", "oak_planks"),
                   f"{key} tick {node['tick']}: X={node['x'] + 1} 不是音符盒/空音符")

    issues = check_structure(world)
    chk.ok(not issues, f"存在悬空/无垫底方块：{issues[:3]}")

    for entry in plan["notes"]:
        if entry.get("master"):
            chk.ok(entry["pos"][2] == 0, f"主音轨音符不在中心轴：{entry['pos']}")
        else:
            chk.ok(abs(entry["pos"][2]) >= MIN_Z - 1,
                   f"tick {entry['tick']}: 普通音符离轴 {abs(entry['pos'][2])} 越过了中心轴")

    # 主音轨应上下分布
    master_y = sorted({node["y"] for key, nodes in plan["lanes"].items()
                       if isinstance(key, tuple) and key[0] == "master"
                       for node in nodes})
    chk.ok(len(master_y) == 1 and master_y[0] >= 64 + 2 + 1,
           f"主音轨高度不对：{master_y}")
    return chk.report("geometry")


# ── 测试 3：模拟器语义（线性排版）──────────────────────────

def test_simulator_semantics():
    print("测试 3：模拟器语义（旧线性链应在 2×(1+tick) 受电）")
    chk = Checks()
    notes = [FakeNote(tick, key=40 + index) for index, tick in enumerate((0, 3, 7, 12))]
    virtual_layers = [{"vid": 0, "original_id": 0, "side": None,
                       "notes": notes, "inst": 0, "cat": "钢琴"}]
    commands, activations = build_all_commands_virtual(
        virtual_layers, {0: 0}, [(0, 0)], (0, 64, 0), "east", 12,
        group_staircase_modes={})
    world = world_from_commands(commands)
    simulation = Simulator(world).run(list(activations.values()), max_ticks=40)
    expected = [2 * (1 + tick) for tick in (0, 3, 7, 12)]
    actual = [event["game_tick"] for event in simulation["events"]]
    chk.ok(actual == expected, f"线性链事件时间 {actual} != {expected}")
    return chk.report("simulator")


# ── 测试 4：曲线排版端到端 ─────────────────────────────────

def _simulate(commands, meta, max_tick):
    world = world_from_commands(commands)
    player = meta["player"]
    initial = (player["x0"] + 0.5, player["y"], player["z"])
    return Simulator(world).run(
        list(meta["activations"].values()),
        max_ticks=2 * (max_tick + 2) + 8,
        initial_player=initial)


def test_curve_end_to_end():
    print("测试 4：曲线排版端到端（Fnbs(t) == Fmc(t)）")
    chk = Checks()
    virtual_layers, layer_to_group = make_curve_layers()
    config = base_config(curve_master_layers=[0])
    commands, activations, meta = build_curve_commands(
        virtual_layers, layer_to_group, (0, 64, 0), 8, config)
    plan = meta["plan"]

    simulation = _simulate(commands, meta, 8)
    result = compare(plan, simulation, sound_range=plan["sound_range"])
    print(format_report(result).replace("\n", "\n    "))
    stats = result["stats"]
    chk.ok(stats["passed"], f"逐 tick 不一致：缺失 {stats['missing_ticks'][:8]}")
    chk.ok(stats["matched_ticks"] == stats["ticks"],
           f"一致 tick {stats['matched_ticks']}/{stats['ticks']}")
    chk.ok(stats["volume_max_error"] < 1e-6,
           f"单盒音量与设计不符：{stats['volume_max_error']:.4f}")
    # 同一 tick 多音符会共用同一半径，允许略大的叠加误差
    chk.ok(stats["heard_max_error"] < 0.10,
           f"左右叠加后的音量对 NBS 误差过大：{stats['heard_max_error']:.4f}")
    chk.ok(stats["left_notes"] > 0 and stats["right_notes"] > 0, "左右声道没有同时出现")
    chk.ok(stats["left_notes"] == stats["right_notes"], "左右音符数量不对称")

    # 普通轨道左右镜像：同 tick 两侧距离相同
    pairs = {}
    for entry in plan["notes"]:
        if entry.get("master"):
            continue
        pairs.setdefault(entry["tick"], {})[entry["side"]] = entry["distance"]
    for tick, sides in pairs.items():
        chk.ok(sides.get(-1) is not None and sides.get(1) is not None,
               f"tick {tick}: 普通音符没有左右成对 {sides}")
        chk.ok(sides.get(-1) == sides.get(1), f"tick {tick}: 左右距离不一致 {sides}")

    # 矿车跟随：每个音符发声时，玩家 x 与音符 x 对齐
    track = {entry["game_tick"]: entry["pos"] for entry in simulation["player_track"]}
    chk.ok(len(track) > 1, f"矿车只移动了 {len(track)} 次")
    aligned = 0
    for event in simulation["events"]:
        position = track.get(event["game_tick"])
        if position is not None and abs(position[0] - (event["pos"][0] + 0.5)) < 1e-6:
            aligned += 1
    chk.ok(aligned == len(simulation["events"]),
           f"矿车与音符未对齐：{aligned}/{len(simulation['events'])}")

    # 保存 / 读取 .schem 后模拟结果一致
    tempdir = tempfile.mkdtemp(prefix="miau_curve_")
    path = os.path.join(tempdir, "curve.schem")
    chk.ok(generate_schematic_from_commands(commands, path), "保存 .schem 失败")
    loaded = load_schem(path)
    simulation2 = Simulator(loaded).run(
        list(activations.values()), max_ticks=2 * (8 + 2) + 8,
        initial_player=(meta["player"]["x0"] + 0.5, meta["player"]["y"], meta["player"]["z"]))
    chk.ok([(e["game_tick"], e["pos"], e["pitch"], e["instrument"]) for e in simulation2["events"]]
           == [(e["game_tick"], e["pos"], e["pitch"], e["instrument"]) for e in simulation["events"]],
           ".schem 回读后的模拟结果与命令生成不一致")

    # 关闭矿车时，音符时序仍然必须正确
    no_cart = base_config(curve_master_layers=[0], curve_minecart=False)
    cart_cmds, _, cart_meta = build_curve_commands(
        virtual_layers, layer_to_group, (0, 64, 0), 8, no_cart)
    chk.ok(cart_meta["minecart"] is None, "关闭矿车后仍生成了矿车结构")
    cart_sim = _simulate(cart_cmds, cart_meta, 8)
    chk.ok(compare(cart_meta["plan"], cart_sim,
                   sound_range=cart_meta["plan"]["sound_range"])["stats"]["passed"],
           "无矿车时音符时序不一致")

    # app.build_commands 集成
    from miaunoteblock.app import build_commands
    config = base_config(curve_master_layers=[0])
    config["lyrics_import"] = [{"tick": 0, "text": "测", "duration": 4}]
    commands_int = build_commands(config, None, virtual_layers, layer_to_group, 8)
    chk.ok(any("redstone_block" in command for command in commands_int), "启动链缺失")
    chk.ok(not any("/mpe text chain" in command for command in commands_int),
           "曲线排版不应生成歌词链")
    return chk.report("curve")


# ── 测试 5：横向平滑 ────────────────────────────────────────

def test_lateral_smoothing():
    print("测试 5：剧烈音量跳变时的横向平滑")
    chk = Checks()
    notes = [FakeNote(0, 0, key=45, velocity=100), FakeNote(1, 0, key=47, velocity=0)]
    virtual_layers = [{"vid": 0, "original_id": 0, "side": None,
                       "notes": notes, "inst": 0, "cat": "钢琴"}]
    plan = plan_curve(virtual_layers, {0: 0}, (0, 64, 0), 1, base_config())
    nodes = plan["lanes"][("group", 0, 1)]
    chk.ok(abs(nodes[0]["z"]) == MIN_DUAL_DISTANCE,
           f"满音量距离 {nodes[0]['z']} != {MIN_DUAL_DISTANCE}")
    chk.ok(abs(nodes[1]["z"]) == MIN_DUAL_DISTANCE + MAX_LATERAL_STEP,
           f"横向跳变没有被限制：{nodes[0]['z']} -> {nodes[1]['z']}")
    chk.ok(plan["smoothed"] >= 1, "没有记录到被平滑的跳变")
    return chk.report("smoothing")


# ── 测试 6：同组多音符同时触发 ─────────────────────────────

def make_multi_layers():
    """两条音轨分到同一组：同 tick 会并排触发两个音符盒。"""
    piano_a = [FakeNote(0, 0, key=45, instrument=0, velocity=60),
               FakeNote(4, 0, key=50, instrument=0, velocity=80)]
    piano_b = [FakeNote(0, 1, key=52, instrument=0, velocity=60),
               FakeNote(4, 1, key=55, instrument=0, velocity=80)]
    virtual_layers = [
        {"vid": 0, "original_id": 0, "side": None, "notes": piano_a,
         "inst": 0, "cat": "钢琴"},
        {"vid": 1, "original_id": 1, "side": None, "notes": piano_b,
         "inst": 0, "cat": "钢琴"},
    ]
    return virtual_layers, {0: 0, 1: 0}


def test_multi_note_cells():
    print("测试 6：同组多音符（强充能带动并排音符盒 + 无串线）")
    chk = Checks()
    virtual_layers, layer_to_group = make_multi_layers()
    commands, activations, meta = build_curve_commands(
        virtual_layers, layer_to_group, (0, 64, 0), 4, base_config())
    plan = meta["plan"]

    chk.ok(plan["multi_cells"] >= 2, f"没有识别出多音符 tick：{plan['multi_cells']}")
    chk.ok(meta["multi_written"] >= 4,
           f"并排音符盒写入不足：{meta['multi_written']}")
    chk.ok(not meta["conflicts"], f"方块冲突：{meta['conflicts'][:3]}")
    chk.ok(meta["crossing"] == 0, f"红石粉串线 {meta['crossing']} 处")
    chk.ok(meta["touching"] == 0, f"跨轨道接触 {meta['touching']} 处")

    world = world_from_commands(commands)
    crossing = check_crossing(world)
    chk.ok(crossing["count"] == 0, f"串线检查未通过：{crossing['items'][:3]}")

    tick0_notes = [entry for entry in plan["notes"] if entry["tick"] == 0]
    chk.ok(len(tick0_notes) == 4,
           f"tick 0 应计划 4 个音符盒（左右各 2），实际 {len(tick0_notes)}")

    simulation = _simulate(commands, meta, 4)
    result = compare(plan, simulation, sound_range=plan["sound_range"])
    stats = result["stats"]
    print(format_report(result).replace("\n", "\n    "))
    chk.ok(stats["passed"], f"逐 tick 不一致：缺 {stats['missing_ticks'][:5]}")
    chk.ok(stats["stray_events"] == 0,
           f"存在串线误触发：{stats['stray_events']} 个")
    tick0_events = [event for event in result["events"] if event["song_tick"] == 0]
    chk.ok(len(tick0_events) == 4,
           f"tick 0 应触发 4 个音符盒，实际 {len(tick0_events)}")
    return chk.report("multi")


# ── 测试 7：串线检测器 ─────────────────────────────────────

def test_crossing_detector():
    print("测试 7：串线检测器")
    chk = Checks()
    wire = lambda: {"id": "redstone_wire", "props": {}, "nbt": None}
    note = lambda: {"id": "note_block", "props": {}, "nbt": None}

    # 两条本该分离的红石线被 Z 方向接在一起（跨 X）
    joined = {
        (0, 64, 0): wire(),
        (0, 64, 1): wire(),
        (1, 64, 1): wire(),
        (2, 64, 1): wire(),
    }
    result = check_crossing(joined)
    chk.ok(result["count"] > 0, "跨 X 的红石线没有被检出")
    chk.ok("dust-component" in result["kinds"], f"类型不对：{result['kinds']}")

    # 红石粉直接贴住另一条轨道的音符盒（不同 X）
    touching = {(0, 64, 0): wire(), (1, 64, 0): note()}
    result2 = check_crossing(touching)
    chk.ok(result2["count"] > 0, "红石粉贴音符盒没有被检出")

    # 正常并排音符单元（同 X、同 Y、Z 相邻）不应报错
    pair = {(1, 64, 0): note(), (1, 64, 1): note()}
    result3 = check_crossing(pair)
    chk.ok(result3["count"] == 0, f"正常并排音符被误报：{result3['items']}")
    return chk.report("crossing")


# ── 测试 8：红石充能方向 ───────────────────────────────────

def test_redstone_charging():
    print("测试 8：红石充能方向（强充能方块上方红石粉有电，下方没电）")
    chk = Checks()
    up = [
        "setblock 0 64 0 minecraft:repeater[facing=west,delay=1]",
        "setblock 1 64 0 minecraft:stone",
        "setblock 1 65 0 minecraft:redstone_wire",
        "setblock 2 65 0 minecraft:redstone_wire",
        "setblock 3 65 0 minecraft:redstone_wire",
        "setblock 4 65 0 minecraft:redstone_wire",
        "setblock 5 65 0 minecraft:repeater[facing=west,delay=1]",
        "setblock 6 65 0 minecraft:note_block[note=0]",
    ]
    upward = Simulator(world_from_commands(up)).run([(-1, 64, 0)], max_ticks=20)
    chk.ok(len(upward["events"]) == 1,
           f"强充能方块上方的红石粉应导通，实际事件 {len(upward['events'])}")

    down = [
        "setblock 0 64 0 minecraft:repeater[facing=west,delay=1]",
        "setblock 1 64 0 minecraft:stone",
        "setblock 1 63 0 minecraft:redstone_wire",
        "setblock 2 63 0 minecraft:redstone_wire",
        "setblock 3 63 0 minecraft:redstone_wire",
        "setblock 4 63 0 minecraft:redstone_wire",
        "setblock 5 63 0 minecraft:repeater[facing=west,delay=1]",
        "setblock 6 63 0 minecraft:note_block[note=0]",
    ]
    downward = Simulator(world_from_commands(down)).run([(-1, 64, 0)], max_ticks=20)
    chk.ok(len(downward["events"]) == 0,
           f"强充能方块下方的红石粉不应导通，实际事件 {len(downward['events'])}")

    # 弱充能方块可以作为中继器输入：音符盒被强充能后，水平相邻的
    # 空音符（导体）变成弱充能方块，再接下一级中继器的背面。
    weak = [
        "setblock 0 64 0 minecraft:repeater[facing=west,delay=1]",
        "setblock 1 64 0 minecraft:note_block[note=0]",
        "setblock 1 64 1 minecraft:oak_planks",
        "setblock 2 64 1 minecraft:repeater[facing=west,delay=1]",
        "setblock 3 64 1 minecraft:note_block[note=1]",
    ]
    weak_sim = Simulator(world_from_commands(weak)).run([(-1, 64, 0)], max_ticks=20)
    # 第一个音符盒被 R1 强充能发声，第二个靠弱充能方块续链发声
    chk.ok(len(weak_sim["events"]) == 2,
           f"弱充能方块应能给中继器输入端供能，实际事件 {len(weak_sim['events'])}")
    return chk.report("redstone")


# ── 测试 9：空音符摆渡大跨度 ───────────────────────────────

def test_empty_note_relay():
    print("测试 9：大音量跨度由空音符摆渡（不削平音符音量）")
    chk = Checks()
    # tick 0 满音量、tick 3 零音量，中间 tick 1/2 是空音符
    notes = [FakeNote(0, 0, key=45, instrument=0, velocity=100),
             FakeNote(3, 0, key=50, instrument=0, velocity=0)]
    virtual_layers = [
        {"vid": 0, "original_id": 0, "side": None, "notes": notes,
         "inst": 0, "cat": "钢琴"},
    ]
    commands, activations, meta = build_curve_commands(
        virtual_layers, {0: 0}, (0, 64, 0), 3, base_config())
    plan = meta["plan"]

    chk.ok(plan["smoothed"] == 0, f"有空音符时不应平滑：{plan['smoothed']}")
    chk.ok(plan["deviations"] == 0, f"音符音量出现偏差：{plan['deviations']}")
    for key, nodes in plan["lanes"].items():
        for index in range(1, len(nodes)):
            delta = abs(nodes[index]["z"] - nodes[index - 1]["z"])
            chk.ok(delta <= MAX_LATERAL_STEP,
                   f"{key} tick {index}: 单段位移 {delta} 超过红石线上限")

    simulation = _simulate(commands, meta, 3)
    result = compare(plan, simulation, sound_range=plan["sound_range"])
    stats = result["stats"]
    chk.ok(stats["passed"], f"逐 tick 不一致：缺 {stats['missing_ticks'][:5]}")
    chk.ok(stats["stray_events"] == 0, f"存在串线误触发：{stats['stray_events']}")
    return chk.report("relay")


# ── 测试 10：静音裁剪 + 线路桥 ─────────────────────────────

def test_trim_and_bridge():
    print("测试 10：开头静音裁剪 + 线路桥唤醒 + 时序对齐")
    chk = Checks()
    dense = [FakeNote(0, 0, key=45, velocity=100),
             FakeNote(40, 0, key=48, velocity=100),
             FakeNote(80, 0, key=50, velocity=100)]
    late = [FakeNote(60, 1, key=52, velocity=100),
            FakeNote(80, 1, key=55, velocity=100)]
    virtual_layers = [
        {"vid": 0, "original_id": 0, "side": None, "notes": dense,
         "inst": 0, "cat": "钢琴"},
        {"vid": 1, "original_id": 1, "side": None, "notes": late,
         "inst": 0, "cat": "钢琴"},
    ]
    config = base_config(curve_trim_silent=True, curve_trim_threshold=16)
    commands, activations, meta = build_curve_commands(
        virtual_layers, {0: 0, 1: 1}, (0, 64, 0), 80, config)
    plan = meta["plan"]

    chk.ok(meta["trimmed"] >= 1, f"没有裁剪静音开头：{meta['trimmed']}")
    chk.ok(meta["bridge_ok"] >= 1, f"没有生成线路桥：{meta['bridge_ok']}")
    chk.ok(meta["crossing"] == 0, f"线路桥造成串线：{meta['crossing']}")
    chk.ok(meta["touching"] == 0, f"线路桥触碰其它轨道：{meta['touching']}")

    simulation = _simulate(commands, meta, 80)
    result = compare(plan, simulation, sound_range=plan["sound_range"])
    stats = result["stats"]
    print(format_report(result).replace("\n", "\n    "))
    chk.ok(stats["passed"], f"逐 tick 不一致：缺 {stats['missing_ticks'][:6]}")
    chk.ok(stats["stray_events"] == 0, f"存在串线误触发：{stats['stray_events']}")
    return chk.report("bridge")


# ── 测试 11：主音轨红石灯 + 命令方块链方位 ────────────────

def test_master_lamp_and_command_side():
    print("测试 11：主音轨红石灯 + 命令方块链方位")
    chk = Checks()
    notes = [FakeNote(0, 0, key=45, velocity=100),
             FakeNote(4, 0, key=48, velocity=100)]
    virtual_layers = [
        {"vid": 0, "original_id": 0, "side": None, "notes": notes,
         "inst": 0, "cat": "钢琴"},
        {"vid": 1, "original_id": 1, "side": None,
         "notes": [FakeNote(2, 1, key=50, velocity=70)], "inst": 0, "cat": "钢琴"},
    ]
    config = base_config(curve_master_layers=[0], curve_master_lamp=True,
                         curve_command_side="left")
    commands, activations, meta = build_curve_commands(
        virtual_layers, {0: 0, 1: 1}, (0, 64, 0), 4, config)
    world = world_from_commands(commands)
    lamps = [pos for pos, block in world.items()
             if block["id"] == "redstone_lamp"]
    chk.ok(len(lamps) >= 1, "主音轨空音符没有换成红石灯")
    chk.ok(meta["minecart"] is not None, "没有生成矿车链")
    chk.ok(meta["minecart"]["side"] == "left", "命令方块链方位没有生效")
    chk.ok(meta["minecart"]["z"] < 0, "left 方位应在负 Z 侧")

    ride_blocks = [block for block in world.values()
                   if block["id"] == "command_block"
                   and "ride" in (block.get("nbt") or "")]
    chk.ok(len(ride_blocks) == 1, "没有 ride 命令方块")

    # 命令方块链无论放在左/右/上/下，矿车都必须被传送到玩家走廊；
    # 而且必须用相对坐标，保证结构粘贴到任意世界坐标后仍然正确。
    def _command_text(nbt):
        marker = "Command:"
        return json.loads(nbt[nbt.index(marker) + len(marker):-1])

    def _resolve(origin, text):
        parts = text.split()
        coords = parts[2:5]
        relative = all(part.startswith("~") for part in coords)
        offsets = [float(part[1:]) if len(part) > 1 else 0.0 for part in coords]
        return relative, (origin[0] + offsets[0], origin[1] + offsets[1],
                          origin[2] + offsets[2])

    for side in ("left", "right", "up", "down"):
        cfg = base_config(curve_master_layers=[0], curve_command_side=side)
        cmds, _, side_meta = build_curve_commands(
            virtual_layers, {0: 0, 1: 1}, (0, 64, 0), 4, cfg)
        side_world = world_from_commands(cmds)
        player_y = side_meta["player"]["y"]
        tp_items = [(position, _command_text(block["nbt"]))
                    for position, block in side_world.items()
                    if block["id"] == "command_block"
                    and "tp @e[type=minecart" in (block.get("nbt") or "")]
        summon_items = [(position, _command_text(block["nbt"]))
                        for position, block in side_world.items()
                        if block["id"] == "command_block"
                        and "summon minecart" in (block.get("nbt") or "")]
        chk.ok(len(tp_items) == 5, f"{side}: tp 命令数量 {len(tp_items)}")
        chk.ok(len(summon_items) == 1, f"{side}: summon 命令数量")
        for position, text in tp_items:
            relative, target = _resolve(position, text)
            chk.ok(relative, f"{side}: tp 必须用相对坐标 {text}")
            chk.ok(abs(target[1] - player_y) < 1e-6
                   and abs(target[2] - 0.0) < 1e-6,
                   f"{side}: tp 没有落在走廊 y/z：{target}")
        for position, text in summon_items:
            relative, target = _resolve(position, text)
            chk.ok(relative, f"{side}: summon 必须用相对坐标 {text}")
            chk.ok(abs(target[0] - 1.5) < 1e-6
                   and abs(target[1] - player_y) < 1e-6
                   and abs(target[2] - 0.0) < 1e-6,
                   f"{side}: summon 没有落在走廊：{target}")
    return chk.report("lamp")


# ── 测试 12：中间静音段裁剪 ───────────────────────────────

def test_internal_gap_trim():
    print("测试 12：中间静音段裁剪（同轨道线路桥续接）")
    chk = Checks()
    notes = [FakeNote(0, 0, key=45, velocity=100),
             FakeNote(10, 0, key=47, velocity=100),
             FakeNote(60, 0, key=52, velocity=100),
             FakeNote(80, 0, key=55, velocity=100)]
    virtual_layers = [
        {"vid": 0, "original_id": 0, "side": None, "notes": notes,
         "inst": 0, "cat": "钢琴"},
    ]
    config = base_config(curve_trim_silent=True, curve_trim_threshold=16)
    commands, activations, meta = build_curve_commands(
        virtual_layers, {0: 0}, (0, 64, 0), 80, config)
    plan = meta["plan"]

    chk.ok(meta["trimmed"] >= 1, f"中间静音段没有裁剪：{meta['trimmed']}")
    chk.ok(meta["bridge_ok"] >= 1, f"没有生成续接桥：{meta['bridge_ok']}")
    # 中间 11~59 这段（X 22~118）不应再出现音符盒，只剩续接桥的粉/中继器
    world = world_from_commands(commands)
    note_xs = [pos[0] for pos, block in world.items() if block["id"] == "note_block"]
    chk.ok(not any(22 <= x <= 118 for x in note_xs),
           f"中间静音段仍生成了音符盒：{[x for x in note_xs if 22 <= x <= 118][:5]}")
    simulation = _simulate(commands, meta, 80)
    result = compare(plan, simulation, sound_range=plan["sound_range"])
    stats = result["stats"]
    chk.ok(stats["passed"], f"逐 tick 不一致：缺 {stats['missing_ticks'][:6]}")
    chk.ok(stats["stray_events"] == 0, f"存在串线误触发：{stats['stray_events']}")
    return chk.report("gap")


# ── 测试 13：秒阈值的静音裁剪 ──────────────────────────────

def test_trim_seconds_and_support():
    print("测试 13：秒阈值裁剪（支撑方块一起删除）")
    chk = Checks()
    notes = [FakeNote(0, 0, key=45, velocity=100),
             FakeNote(80, 0, key=48, velocity=100)]
    virtual_layers = [
        {"vid": 0, "original_id": 0, "side": None, "notes": notes,
         "inst": 0, "cat": "钢琴"},
    ]
    plain, _, plain_meta = build_curve_commands(
        virtual_layers, {0: 0}, (0, 64, 0), 80, base_config())
    trimmed, _, meta = build_curve_commands(
        virtual_layers, {0: 0}, (0, 64, 0), 80,
        base_config(curve_trim_silent=True, curve_trim_seconds=2.0))

    # song=None 时按默认速度 10 tick/秒换算：2 秒 = 20 tick
    chk.ok(meta["trim_threshold"] == 20,
           f"秒→tick 换算错误：{meta['trim_threshold']}")
    chk.ok(meta["trimmed"] >= 1, "没有裁剪开头")
    chk.ok(meta["trim_aborted"] == 0, "裁剪被回退，说明桥没有搭起来")
    chk.ok(meta["bridge_ok"] >= 1, "没有生成唤醒桥")
    # 被删掉的是「空音符方块 + 它下面的支撑」，桥只保留细线
    plain_world = world_from_commands(plain)
    trim_world = world_from_commands(trimmed)
    pad = "oak_planks"
    plain_pads = sum(1 for block in plain_world.values() if block["id"] == pad)
    trim_pads = sum(1 for block in trim_world.values() if block["id"] == pad)
    chk.ok(plain_pads > 100, f"对照组空音符方块太少：{plain_pads}")
    chk.ok(trim_pads < plain_pads * 0.2,
           f"空音符方块没有删除：{trim_pads} / {plain_pads}")
    return chk.report("seconds")


# ── 测试 14：双轨音量控制 ─────────────────────────────────

def test_dual_track():
    print("测试 14：双轨音量控制（按 tick 分流 + 外扩副轨）")
    chk = Checks()
    volumes = [100, 30, 100, 30, 100, 30, 100, 30]
    notes = [FakeNote(index * 2, 0, key=45 + index, instrument=0,
                      velocity=volume)
             for index, volume in enumerate(volumes)]
    steady = [FakeNote(tick, 1, key=60, instrument=0, velocity=80)
              for tick in range(0, 16, 4)]
    virtual_layers = [
        {"vid": 0, "original_id": 0, "side": None, "notes": notes,
         "inst": 0, "cat": "钢琴"},
        {"vid": 1, "original_id": 1, "side": None, "notes": steady,
         "inst": 0, "cat": "钢琴"},
    ]
    config = base_config(curve_dual_track=True, curve_dual_spacing=6)
    commands, activations, meta = build_curve_commands(
        virtual_layers, {0: 0, 1: 1}, (0, 64, 0), 15, config)
    plan = meta["plan"]

    chk.ok(meta["dual_tracks"] >= 2, f"副音轨没有生成：{meta['dual_tracks']}")
    chk.ok(meta["dual_skipped"] == 0,
           f"有 {meta['dual_skipped']} 条副音轨没能接上桥")
    aux_notes = [entry for entry in plan["notes"] if entry.get("aux")]
    chk.ok(len(aux_notes) >= 8, f"副音轨音符不足：{len(aux_notes)}")
    aux_keys = [key for key in plan["lanes"] if len(key) > 3 and key[3] == 1]
    chk.ok(len(aux_keys) >= 2, f"缺少副音轨 lane：{aux_keys}")
    # 只有音量跨度大的组 0 应该拆出副轨，平稳的组 1 不应被波及
    chk.ok(all(key[1] == 0 for key in aux_keys),
           f"平稳音轨被错误拆成双轨：{aux_keys}")

    # 同一侧副轨必须比主轨更靠外
    for key in aux_keys:
        aux_nodes = plan["lanes"][key]
        main_nodes = plan["lanes"][("group", key[1], key[2])]
        for node in aux_nodes:
            main_node = main_nodes[node["tick"]]
            chk.ok(abs(node["z"]) > abs(main_node["z"]),
                   f"tick {node['tick']}: 副轨没有外扩 {node['z']} / {main_node['z']}")
            break

    simulation = _simulate(commands, meta, 15)
    result = compare(plan, simulation, sound_range=plan["sound_range"])
    stats = result["stats"]
    print(format_report(result).replace("\n", "\n    "))
    chk.ok(stats["passed"], f"逐 tick 不一致：缺 {stats['missing_ticks'][:6]}")
    chk.ok(stats["stray_events"] == 0, f"存在串线误触发：{stats['stray_events']}")
    chk.ok(stats["volume_max_error"] < 1e-6,
           f"副轨音量与设计不符：{stats['volume_max_error']:.4f}")

    # 音量 100% / 40% 对应距离 24 / 38.4，差 14.4 < 16：
    # 单轨完全摆渡得过去，不应拆双轨
    mild = [FakeNote(index * 2, 0, key=45 + index, instrument=0,
                     velocity=100 if index % 2 == 0 else 40)
            for index in range(8)]
    _, _, mild_meta = build_curve_commands(
        [{"vid": 0, "original_id": 0, "side": None, "notes": mild,
          "inst": 0, "cat": "钢琴"}],
        {0: 0}, (0, 64, 0), 15, base_config(curve_dual_track=True))
    chk.ok(mild_meta["dual_tracks"] == 0,
           f"距离差未超过 16 却拆了双轨：{mild_meta['dual_tracks']}")
    return chk.report("dual")


# ── 测试 15：红石电源复核 ─────────────────────────────────

def test_power_paths():
    print("测试 15：红石粉电源复核（弱充能方块不得给粉供电）")
    chk = Checks()

    dense = [FakeNote(0, 0, key=45, velocity=100),
             FakeNote(40, 0, key=48, velocity=100),
             FakeNote(80, 0, key=50, velocity=100)]
    late = [FakeNote(60, 1, key=52, velocity=100),
            FakeNote(80, 1, key=55, velocity=100)]
    trim_layers = [
        {"vid": 0, "original_id": 0, "side": None, "notes": dense,
         "inst": 0, "cat": "钢琴"},
        {"vid": 1, "original_id": 1, "side": None, "notes": late,
         "inst": 0, "cat": "钢琴"},
    ]

    dual_volumes = [100, 30, 100, 30, 100, 30, 100, 30]
    dual_layers = [{
        "vid": 0, "original_id": 0, "side": None,
        "notes": [FakeNote(i * 2, 0, key=45 + i, velocity=v)
                  for i, v in enumerate(dual_volumes)],
        "inst": 0, "cat": "钢琴",
    }]

    stress_layers = []
    stress_groups = {}
    for group in range(4):
        notes = []
        for index, tick in enumerate(range(0, 121, 2)):
            volume = 100 if index % 2 == 0 else 25
            notes.append(FakeNote(tick, group, key=45 + group * 3 + index % 5,
                                  instrument=0, velocity=volume))
        stress_layers.append({
            "vid": group, "original_id": group, "side": None,
            "notes": notes, "inst": 0, "cat": "钢琴",
        })
        stress_groups[group] = group

    scenarios = [
        ("trim", trim_layers, {0: 0, 1: 1},
         base_config(curve_trim_silent=True), 80),
        ("dual", dual_layers, {0: 0},
         base_config(curve_dual_track=True), 15),
        ("stress", stress_layers, stress_groups,
         base_config(curve_dual_track=True, curve_trim_silent=True), 120),
    ]
    for name, layers, mapping, config, max_tick in scenarios:
        commands, _, meta = build_curve_commands(
            layers, mapping, (0, 64, 0), max_tick, config)
        world = world_from_commands(commands)
        power = check_power_paths(world)
        endpoints = check_dust_endpoints(world)
        chk.ok(meta.get("unpowered_dust") == 0,
               f"{name}: 生成器自检发现无电源红石粉 "
               f"{meta.get('unpowered_dust_items')}")
        chk.ok(power["components"] == 0,
               f"{name}: 静态复核发现无电源粉 {power['unpowered'][:1]}")
        chk.ok(meta.get("dust_side_notes") == 0,
               f"{name}: 有音符盒只贴着粉的侧面 {meta.get('dust_side_note_items')}")
        chk.ok(endpoints["count"] == 0,
               f"{name}: 端点复核失败 {endpoints['items'][:1]}")
        chk.ok(meta.get("dual_skipped") == 0,
               f"{name}: 有副音轨被跳过")
        chk.ok(meta.get("trim_aborted") == 0,
               f"{name}: 有裁剪被回退")
        chk.ok(meta.get("chain_gaps") == 0,
               f"{name}: 链路存在断点 {meta.get('chain_gap_items')}")

        def solid(pos):
            block = world.get(pos)
            return block is not None and block["id"] not in (
                "air", "cave_air", "void_air", "water", "lava")

        floating_dust = [pos for pos, block in world.items()
                         if block["id"] == "redstone_wire"
                         and not solid((pos[0], pos[1] - 1, pos[2]))]
        floating_relay = [pos for pos, block in world.items()
                          if block["id"] == "repeater"
                          and not solid((pos[0], pos[1] - 1, pos[2]))]
        covered_notes = [pos for pos, block in world.items()
                         if block["id"] == "note_block"
                         and (pos[0], pos[1] + 1, pos[2]) in world]
        chk.ok(not floating_dust,
               f"{name}: 悬空红石粉 {floating_dust[:4]}")
        chk.ok(not floating_relay,
               f"{name}: 悬空中继器 {floating_relay[:4]}")
        chk.ok(not covered_notes,
               f"{name}: 音符盒上方有方块 {covered_notes[:4]}")
    return chk.report("power")


# ── 测试 16：红石粉端点 / 强弱充能规则 ────────────────────

def _sim(world_cmds, launch, ticks=20):
    return Simulator(world_from_commands(world_cmds)).run(launch, max_ticks=ticks)


def test_dust_endpoint_rules():
    print("测试 16：红石粉端点与强弱充能规则")
    chk = Checks()

    # A. 一字形粉的侧面对着音符盒：不应发声
    straight = [
        "setblock 0 64 0 minecraft:repeater[facing=west,delay=1]",
        "setblock 1 64 0 minecraft:redstone_wire",
        "setblock 2 64 0 minecraft:redstone_wire",
        "setblock 3 64 0 minecraft:redstone_wire",
        "setblock 2 64 1 minecraft:note_block[note=0]",
    ]
    events = _sim(straight, [(-1, 64, 0)])["events"]
    chk.ok(len(events) == 0, f"一字粉侧面的音符盒不应发声，实际 {len(events)}")
    world = world_from_commands(straight)
    report = check_dust_endpoints(world)
    chk.ok(report["count"] >= 1, "静态检查没有发现侧面贴粉的问题")

    # B. 粉端点正对音符盒：应发声
    endpoint = [
        "setblock 0 64 0 minecraft:repeater[facing=west,delay=1]",
        "setblock 1 64 0 minecraft:redstone_wire",
        "setblock 2 64 0 minecraft:redstone_wire",
        "setblock 3 64 0 minecraft:redstone_wire",
        "setblock 4 64 0 minecraft:note_block[note=0]",
    ]
    events = _sim(endpoint, [(-1, 64, 0)])["events"]
    chk.ok(len(events) == 1, f"粉端点正对的音符盒应发声，实际 {len(events)}")
    chk.ok(check_dust_endpoints(world_from_commands(endpoint))["count"] == 0,
           "端点正对时不应报错")

    # C. 弱充能方块能激活音符盒，但不能给红石粉供电
    weak_case = [
        "setblock 0 64 0 minecraft:repeater[facing=west,delay=1]",
        "setblock 1 64 0 minecraft:stone",
        "setblock 1 64 1 minecraft:note_block[note=0]",
        "setblock 2 64 1 minecraft:redstone_wire",
        "setblock 3 64 1 minecraft:repeater[facing=west,delay=1]",
        "setblock 4 64 1 minecraft:note_block[note=2]",
    ]
    events = _sim(weak_case, [(-1, 64, 0)])["events"]
    chk.ok(len(events) == 1 and events[0]["pitch"] == 0,
           f"弱充能应只激活相邻音符盒，实际 {events}")

    # D. 强充能方块：上方粉有电、下方粉没电
    charging = [
        "setblock 0 64 0 minecraft:repeater[facing=west,delay=1]",
        "setblock 1 64 0 minecraft:stone",
        "setblock 1 65 0 minecraft:redstone_wire",
        "setblock 2 65 0 minecraft:repeater[facing=west,delay=1]",
        "setblock 3 65 0 minecraft:note_block[note=0]",
        "setblock 1 63 0 minecraft:redstone_wire",
        "setblock 2 63 0 minecraft:repeater[facing=west,delay=1]",
        "setblock 3 63 0 minecraft:note_block[note=2]",
    ]
    events = _sim(charging, [(-1, 64, 0)])["events"]
    chk.ok(len(events) == 1 and events[0]["pos"][1] == 65,
           f"只应有上方音符盒发声，实际 {events}")

    # E. 中继器输入端可以捕获弱充能方块
    weak_input = [
        "setblock 0 64 0 minecraft:repeater[facing=west,delay=1]",
        "setblock 1 64 0 minecraft:stone",
        "setblock 1 64 1 minecraft:stone",
        "setblock 2 64 1 minecraft:repeater[facing=west,delay=1]",
        "setblock 3 64 1 minecraft:note_block[note=0]",
    ]
    events = _sim(weak_input, [(-1, 64, 0)])["events"]
    chk.ok(len(events) == 1, f"弱充能方块应能供能中继器输入端，实际 {len(events)}")
    return chk.report("dust-rules")


# ── 测试 17：音量控制排版也能生成粒子数据 ─────────────────

def test_curve_particles():
    print("测试 17：音量控制排版的粒子特效数据")
    chk = Checks()
    notes = [FakeNote(0, 0, key=45, velocity=100),
             FakeNote(4, 0, key=48, velocity=70),
             FakeNote(8, 0, key=50, velocity=40)]
    virtual_layers = [
        {"vid": 0, "original_id": 0, "side": None, "notes": notes,
         "inst": 0, "cat": "钢琴"},
    ]
    config = base_config(particles_enabled=True, particles_layers=[0],
                         particles_trail=16, particles_curve="sine")
    _, _, meta = build_curve_commands(
        virtual_layers, {0: 0}, (0, 64, 0), 8, config)
    from miaunoteblock.particles import build_curve_particle_commands
    commands, acts = build_curve_particle_commands(
        meta["plan"], config, (0, 0, 0))
    chk.ok(len(acts) == 1, f"粒子命令方块数量不对：{acts}")
    chk.ok(len(commands) == 1 and "/mpe noteblock chain" in commands[0],
           f"没有生成粒子链命令：{commands[:1]}")

    marker = "Command:"
    text = json.loads(commands[0][commands[0].index(marker) + len(marker):-1])
    body = text.split("~~~", 1)[1].strip().split(" ")[0]
    delays = [int(part.split(",")[3]) for part in body.split("|")]
    expected_ticks = sorted({note.tick for note in notes})
    chk.ok(delays == [2 * tick + 1 for tick in expected_ticks],
           f"粒子延迟没有按绝对时间轴对齐：{delays}")
    chk.ok("curve=sine" in text and "trail=16" in text,
           f"粒子参数没有透传：{text}")
    return chk.report("particles")


# ── 测试 18：组合压力与断链检测 ──────────────────────────

def _stress_layers(seed, groups, chord):
    """构造多分组 / 多音符 / 中间静音的组合歌曲。"""
    import random
    rng = random.Random(seed)
    layers = []
    mapping = {}
    step = rng.choice([2, 4])
    ticks = [tick for tick in range(0, 60, step) if not 10 <= tick < 24]
    for group in range(groups):
        volumes = {tick: rng.choice([100, 70, 40, 10]) for tick in ticks}
        for voice in range(chord):
            vid = group * 3 + voice
            notes = [FakeNote(tick, vid, key=45 + group * 2 + voice,
                              instrument=0, velocity=volumes[tick])
                     for tick in ticks]
            layers.append({"vid": vid, "original_id": vid, "side": None,
                           "notes": notes, "inst": 0, "cat": "钢琴"})
            mapping[vid] = group
    return layers, mapping


def test_stress_no_broken_chains():
    print("测试 18：组合压力（多分组 + 多音符 + 裁剪 + 双轨）下的断链审计")
    chk = Checks()
    for case in range(12):
        rng = __import__("random").Random(7000 + case)
        groups = rng.randint(2, 5)
        chord = rng.choice([1, 2, 3])
        layers, mapping = _stress_layers(7000 + case, groups, chord)
        config = base_config(
            curve_lane_spacing=rng.choice([3, 4, 6]),
            curve_master_spacing=rng.choice([3, 5]),
            curve_corridor_height=rng.choice([2, 4]),
            curve_trim_silent=True,
            curve_trim_seconds=rng.choice([0.5, 1.0]),
            curve_dual_track=True,
            curve_dual_spacing=rng.choice([3, 9]),
            curve_master_layers=[rng.randrange(groups) * 3],
        )
        commands, activations, meta = build_curve_commands(
            layers, mapping, (0, 64, 0), 59, config)
        plan = meta["plan"]
        world = world_from_commands(commands)
        simulator = Simulator(world).run(
            list(meta["activations"].values()),
            max_ticks=2 * (59 + 2) + 8,
            initial_player=(meta["player"]["x0"] + 0.5,
                            meta["player"]["y"], meta["player"]["z"]))
        stats = compare(plan, simulator,
                        sound_range=plan["sound_range"])["stats"]
        label = f"案例 {case}（{groups} 组 / {chord} 音符）"

        chk.ok(stats["missing_ticks"] == [],
               f"{label}：计划音符没有全部发声 {stats['missing_ticks'][:6]}")
        chk.ok(meta["chain_gaps"] == 0,
               f"{label}：链路审计发现断点 {meta['chain_gap_items'][:3]}")
        chk.ok(meta["multi_avoided"] == 0,
               f"{label}：有 {meta['multi_avoided']} 个并排音符被丢弃")
        chk.ok(meta["floating_parts"] == 0,
               f"{label}：有 {meta['floating_parts']} 个悬浮红石元件")
        chk.ok(meta["unpowered_dust"] == 0,
               f"{label}：有 {meta['unpowered_dust']} 段无电源红石粉")
        chk.ok(meta["covered_notes"] == 0,
               f"{label}：有 {meta['covered_notes']} 个音符盒上方被挡")
        chk.ok(meta["world_crossing"] == 0,
               f"{label}：串线审计报警 {meta['world_crossing_items'][:3]}")

        # 每条已生成轨道的每一格，中继器都必须真的通过电
        dead = []
        for key, nodes in plan["lanes"].items():
            for tick in range(len(nodes)):
                node = nodes[tick]
                if node is None:
                    continue
                position = (node["x"], node["y"], node["z"])
                block = world.get(position)
                if block is None or block["id"] != "repeater":
                    continue
                if position not in simulator["powered_repeaters"]:
                    dead.append((key, tick, position))
        chk.ok(dead == [], f"{label}：中继器从未通电（断链）{dead[:3]}")
    return chk.report("stress")


def test_broken_chain_is_detected():
    """故意删掉一段横向红石粉，验证审计与模拟都能抓住断链。"""
    print("测试 19：故意断链必须被检出")
    chk = Checks()
    layers, mapping = _stress_layers(4242, 2, 2)
    config = base_config(curve_trim_silent=True, curve_trim_seconds=0.5,
                         curve_dual_track=True)
    commands, activations, meta = build_curve_commands(
        layers, mapping, (0, 64, 0), 59, config)
    plan = meta["plan"]
    simulator = Simulator(world_from_commands(commands)).run(
        list(meta["activations"].values()), max_ticks=2 * (59 + 2) + 8,
        initial_player=(meta["player"]["x0"] + 0.5,
                        meta["player"]["y"], meta["player"]["z"]))
    chk.ok(compare(plan, simulator,
                   sound_range=plan["sound_range"])["stats"]["missing_ticks"] == [],
           "断链前的基准结构就不完整")

    world = world_from_commands(commands)
    # 找一段横向移动，把中间的红石粉拆掉一格
    target = None
    for key, nodes in plan["lanes"].items():
        for index in range(1, len(nodes)):
            prev, cur = nodes[index - 1], nodes[index]
            if prev is None or cur is None or prev["z"] == cur["z"]:
                continue
            step = 1 if cur["z"] > prev["z"] else -1
            cells = [(cur["x"] - 1, cur["y"], z)
                     for z in range(prev["z"] + step, cur["z"] + step, step)]
            if len(cells) >= 3 and all(cell in world for cell in cells):
                target = (key, index, cells[len(cells) // 2])
                break
        if target:
            break
    chk.ok(target is not None, "没有找到可拆的横向红石粉")
    if target:
        key, index, cell = target
        del world[cell]
        broken = Simulator(world).run(
            list(meta["activations"].values()), max_ticks=2 * (59 + 2) + 8,
            initial_player=(meta["player"]["x0"] + 0.5,
                            meta["player"]["y"], meta["player"]["z"]))
        stats = compare(plan, broken,
                        sound_range=plan["sound_range"])["stats"]
        chk.ok(stats["missing_ticks"] != [],
               "拆掉横向红石粉后，模拟仍然认为所有音符都响了")
        nodes = plan["lanes"][key]
        relay = (nodes[index]["x"], nodes[index]["y"], nodes[index]["z"])
        chk.ok(relay not in broken["powered_repeaters"],
               f"断点后的中继器 {relay} 不应通电")
        power = check_power_paths(world)
        chk.ok(power["components"] > 0,
               "电源审计没有发现断开的无源红石粉")
    return chk.report("broken-chain")


def main():
    results = [
        test_volume_model(),
        test_path_geometry(),
        test_simulator_semantics(),
        test_curve_end_to_end(),
        test_lateral_smoothing(),
        test_multi_note_cells(),
        test_crossing_detector(),
        test_redstone_charging(),
        test_empty_note_relay(),
        test_trim_and_bridge(),
        test_master_lamp_and_command_side(),
        test_internal_gap_trim(),
        test_trim_seconds_and_support(),
        test_dual_track(),
        test_power_paths(),
        test_dust_endpoint_rules(),
        test_curve_particles(),
        test_stress_no_broken_chains(),
        test_broken_chain_is_detected(),
    ]
    print()
    if all(results):
        print("全部测试通过")
        return 0
    print("存在失败项")
    return 1


if __name__ == "__main__":
    sys.exit(main())
