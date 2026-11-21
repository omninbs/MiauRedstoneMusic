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

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from miaunoteblock.curve import (
    MAX_LATERAL_STEP,
    MIN_DUAL_DISTANCE,
    STEP,
    build_curve_commands,
    plan_curve,
)
from miaunoteblock.generation import (
    build_all_commands_virtual,
    generate_schematic_from_commands,
)
from miaunoteblock.simulate import (
    Simulator,
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
        "curve_sound_range": 48.0,
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
        # 严格 ABAB：R 在中继器位上，B 在 +1 位上是音符盒或空音符实心方块
        for index, node in enumerate(nodes):
            relay_z = nodes[index - 1]["z"] if index else node["z"]
            relay = world.get((node["x"], node["y"], relay_z))
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
            chk.ok(abs(entry["pos"][2]) >= MIN_DUAL_DISTANCE - 1,
                   f"tick {entry['tick']}: 普通音符离轴 {abs(entry['pos'][2])} < 24")

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
    chk.ok(stats["heard_max_error"] < 0.08,
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
    nodes = plan["lanes"][("layer", 0, 1)]
    chk.ok(abs(nodes[0]["z"]) == MIN_DUAL_DISTANCE,
           f"满音量距离 {nodes[0]['z']} != {MIN_DUAL_DISTANCE}")
    chk.ok(abs(nodes[1]["z"]) == MIN_DUAL_DISTANCE + MAX_LATERAL_STEP,
           f"横向跳变没有被限制：{nodes[0]['z']} -> {nodes[1]['z']}")
    chk.ok(plan["smoothed"] >= 1, "没有记录到被平滑的跳变")
    return chk.report("smoothing")


def main():
    results = [
        test_volume_model(),
        test_path_geometry(),
        test_simulator_semantics(),
        test_curve_end_to_end(),
        test_lateral_smoothing(),
    ]
    print()
    if all(results):
        print("全部测试通过")
        return 0
    print("存在失败项")
    return 1


if __name__ == "__main__":
    sys.exit(main())
