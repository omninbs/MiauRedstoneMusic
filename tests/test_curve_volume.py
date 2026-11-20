"""曲线音量排版 + 隐形矿车 + 红石模拟 的自检脚本。

不依赖 pytest，直接运行：

    python tests/test_curve_volume.py

覆盖内容：
1. 分支几何：固定中继器数下，红石线段 ≤ 15 格、第一个分支元件紧贴 tap 方块。
2. 模拟器语义：用旧的线性排版验证「事件格正好在 2×(1+tick) 游戏 tick 受电」，
   说明模拟器的中继器 / 方块充能 / 音符盒触发规则和项目既有计时一致。
3. 曲线排版端到端：生成 → 保存 .schem → 重新加载 → 模拟 → 逐 tick 比对，
   要求任意 t 都满足 Fnbs(t) == Fmc(t)（音色 + 音高），并统计音量误差。
4. 立体声对称：复制音符关于延伸轴对称（左右距离相同）。
5. 矿车跟随：命令方块链驱动矿车前进，每个音符发声时玩家都在对应 x 上。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from miaunoteblock.constants import INSTRUMENT_BLOCK_MAP
from miaunoteblock.curve import (
    _branch_parts,
    build_curve_commands,
    plan_curve,
)
from miaunoteblock.generation import (
    build_all_commands_virtual,
    generate_schematic_from_commands,
)
from miaunoteblock.simulate import (
    Simulator,
    compare,
    format_report,
    load_schem,
    world_from_commands,
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

    def __repr__(self):
        return f"Note(t={self.tick},v={self.velocity},pan={self.panning})"


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
        "curve_branch_repeaters": -1,
        "curve_minecart": True,
        "curve_sound_range": 48.0,
        "uniform_repeater_mode": False,
        "group_staircase_modes": {},
        "stereo_layers": [],
        "lyrics_import": [],
        "particles_enabled": False,
    }
    config.update(overrides)
    return config


# ── 测试 1：分支几何 ────────────────────────────────────────

def test_branch_geometry():
    print("测试 1：分支几何（中继器数量 / 红石线段长度）")
    chk = Checks()
    for repeaters in range(0, 4):
        low, high = (2, 17) if repeaters == 0 else (repeaters + 2, 16 * repeaters + 2)
        for distance in range(max(2, low), min(47, high) + 1):
            parts = _branch_parts(distance, repeaters)
            chk.ok(parts[-1] == (distance, "note"), f"n={repeaters} d={distance}: 末格不是音符盒")
            positions = [pos for pos, _ in parts]
            chk.ok(positions == sorted(set(positions)),
                   f"n={repeaters} d={distance}: 位置重复或乱序 {positions}")
            chk.ok(parts[0][1] == ("repeater" if repeaters else ("dust" if distance > 2 else "note")),
                   f"n={repeaters} d={distance}: 首格类型错误")
            chk.ok(sum(1 for _, kind in parts if kind == "repeater") == repeaters,
                   f"n={repeaters} d={distance}: 中继器数量不符")
            # 红石线段（相邻两个中继器之间）不能超过 15 格
            run = 0
            for _, kind in parts:
                if kind == "dust":
                    run += 1
                else:
                    chk.ok(run <= 15, f"n={repeaters} d={distance}: 红石线长 {run} > 15")
                    run = 0
            chk.ok(run <= 15, f"n={repeaters} d={distance}: 末段红石线 {run} > 15")
    return chk.report("branch")


# ── 测试 2：模拟器语义（线性排版）──────────────────────────

def test_simulator_semantics():
    print("测试 2：模拟器语义（旧线性链应在 2×(1+tick) 受电）")
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


# ── 测试 3：曲线排版端到端 ─────────────────────────────────

def make_curve_layers():
    """三个分组：钢琴（力度/声像变化）、立体声吉他、铜号角和弦。"""
    piano = [
        FakeNote(0, 0, key=45, instrument=0, velocity=100, panning=0),
        FakeNote(1, 0, key=47, instrument=0, velocity=80, panning=-60),
        FakeNote(2, 0, key=50, instrument=0, velocity=55, panning=100),
        FakeNote(3, 0, key=52, instrument=0, velocity=30, panning=0),
        FakeNote(5, 0, key=54, instrument=0, velocity=15, panning=-100),
        FakeNote(8, 0, key=57, instrument=0, velocity=100, panning=0),
    ]
    guitar = [
        FakeNote(0, 1, key=40, instrument=5, velocity=100),
        FakeNote(0, 1, key=40, instrument=5, velocity=100),   # 立体声副本
        FakeNote(4, 1, key=43, instrument=5, velocity=70),
        FakeNote(4, 1, key=43, instrument=5, velocity=70),
        FakeNote(8, 1, key=45, instrument=5, velocity=100),
        FakeNote(8, 1, key=45, instrument=5, velocity=100),
    ]
    horn_a = [FakeNote(0, 2, key=60, instrument=16, velocity=90),
              FakeNote(2, 2, key=62, instrument=16, velocity=60),
              FakeNote(6, 2, key=64, instrument=16, velocity=30),
              FakeNote(7, 2, key=65, instrument=16, velocity=100, panning=100)]
    horn_b = [FakeNote(0, 3, key=67, instrument=16, velocity=40),
              FakeNote(2, 3, key=69, instrument=16, velocity=80),
              FakeNote(6, 3, key=71, instrument=16, velocity=100),
              FakeNote(7, 3, key=72, instrument=16, velocity=70, panning=100)]
    horn_c = [FakeNote(7, 4, key=74, instrument=16, velocity=40, panning=100)]
    virtual_layers = [
        {"vid": 0, "original_id": 0, "side": None, "notes": piano, "inst": 0, "cat": "钢琴"},
        {"vid": 1, "original_id": 1, "side": "stereo", "notes": guitar, "inst": 5, "cat": "吉他"},
        {"vid": 2, "original_id": 2, "side": None, "notes": horn_a, "inst": 16, "cat": "铜号角"},
        {"vid": 3, "original_id": 3, "side": None, "notes": horn_b, "inst": 16, "cat": "铜号角"},
        {"vid": 4, "original_id": 4, "side": None, "notes": horn_c, "inst": 16, "cat": "铜号角"},
    ]
    layer_to_group = {0: 0, 1: 1, 2: 2, 3: 2, 4: 2}
    return virtual_layers, layer_to_group


def _simulate(commands, meta, max_tick):
    world = world_from_commands(commands)
    player = meta["player"]
    initial = (player["x0"] + 0.5, player["y"], player["z"])
    simulation = Simulator(world).run(
        list(meta["activations"].values()),
        max_ticks=2 * (max_tick + 2) + 2 * meta["plan"]["branch_repeaters"] + 6,
        initial_player=initial)
    return simulation


def test_curve_end_to_end():
    print("测试 3：曲线排版端到端（Fnbs(t) == Fmc(t)）")
    chk = Checks()
    virtual_layers, layer_to_group = make_curve_layers()
    config = base_config()
    commands, activations, meta = build_curve_commands(
        virtual_layers, layer_to_group, (0, 64, 0), 8, config)
    plan = meta["plan"]

    chk.ok(not meta["conflicts"], f"方块冲突 {meta['conflicts'][:3]}")
    chk.ok(plan["dropped"] == 0, f"有 {plan['dropped']} 个音符没放下")
    simulation = _simulate(commands, meta, 8)
    result = compare(plan, simulation, sound_range=plan["sound_range"])
    print(format_report(result).replace("\n", "\n    "))
    stats = result["stats"]
    chk.ok(stats["passed"], f"逐 tick 不一致：缺失 {stats['missing_ticks'][:8]}")
    chk.ok(stats["matched_ticks"] == stats["ticks"],
           f"一致 tick {stats['matched_ticks']}/{stats['ticks']}")
    chk.ok(stats["volume_max_error"] < 1e-6,
           f"实际音量与设计音量不符：{stats['volume_max_error']:.4f}")
    chk.ok(stats["nbs_volume_mean_error"] < 0.12,
           f"对 NBS 力度平均误差过大：{stats['nbs_volume_mean_error']:.4f}")
    chk.ok(stats["left_notes"] > 0 and stats["right_notes"] > 0, "左右声道没有同时出现")

    # 立体声轨必须左右成对且距离相同
    stereo_pairs = {}
    for entry in plan["notes"]:
        if virtual_layers[1]["notes"] and entry["vid"] == 1:
            stereo_pairs.setdefault(entry["tick"], {})[entry["side"]] = entry["distance"]
    for tick, sides in stereo_pairs.items():
        chk.ok(sides.get(-1) is not None and sides.get(1) is not None,
               f"tick {tick}: 立体声没有左右对称 {sides}")
        chk.ok(sides.get(-1) == sides.get(1),
               f"tick {tick}: 左右距离不一致 {sides}")

    # 矿车跟随：每个音符发声时，玩家 x 与音符 x 对齐
    track = {entry["game_tick"]: entry["pos"] for entry in simulation["player_track"]}
    chk.ok(len(track) > 1, f"矿车只移动了 {len(track)} 次")
    aligned = 0
    for event in simulation["events"]:
        position = track.get(event["game_tick"])
        if position is None:
            continue
        if abs(position[0] - (event["pos"][0] + 0.5)) < 1e-6:
            aligned += 1
    chk.ok(aligned == len(simulation["events"]),
           f"矿车与音符未对齐：{aligned}/{len(simulation['events'])}")

    # 保存 / 读取 .schem 后模拟结果一致
    tempdir = tempfile.mkdtemp(prefix="miau_curve_")
    path = os.path.join(tempdir, "curve.schem")
    chk.ok(generate_schematic_from_commands(commands, path), "保存 .schem 失败")
    loaded = load_schem(path)
    simulation2 = Simulator(loaded).run(
        list(activations.values()),
        max_ticks=2 * (8 + 2) + 2 * plan["branch_repeaters"] + 6,
        initial_player=(meta["player"]["x0"] + 0.5, meta["player"]["y"], meta["player"]["z"]))
    chk.ok([(e["game_tick"], e["pos"], e["pitch"], e["instrument"]) for e in simulation2["events"]]
           == [(e["game_tick"], e["pos"], e["pitch"], e["instrument"]) for e in simulation["events"]],
           ".schem 回读后的模拟结果与命令生成不一致")

    # 强制不同分支中继器档位时，时序也必须完全一致
    for forced in (0, 1, 2):
        case_config = base_config(curve_branch_repeaters=forced)
        case_cmds, _, case_meta = build_curve_commands(
            virtual_layers, layer_to_group, (0, 64, 0), 8, case_config)
        case_sim = _simulate(case_cmds, case_meta, 8)
        case_result = compare(case_meta["plan"], case_sim,
                              sound_range=case_meta["plan"]["sound_range"])
        chk.ok(case_result["stats"]["passed"],
               f"强制分支中继器={forced}: 逐 tick 不一致 "
               f"{case_result['stats']['missing_ticks'][:5]}")

    # 关闭矿车时，音符时序仍然必须正确
    no_cart = base_config(curve_minecart=False)
    cart_cmds, _, cart_meta = build_curve_commands(
        virtual_layers, layer_to_group, (0, 64, 0), 8, no_cart)
    chk.ok(cart_meta["minecart"] is None, "关闭矿车后仍生成了矿车结构")
    cart_sim = _simulate(cart_cmds, cart_meta, 8)
    chk.ok(compare(cart_meta["plan"], cart_sim,
                   sound_range=cart_meta["plan"]["sound_range"])["stats"]["passed"],
           "无矿车时音符时序不一致")

    # app.build_commands 集成：曲线排版要带上启动链、并跳过歌词/粒子
    from miaunoteblock.app import build_commands
    config = base_config()
    config["lyrics_import"] = [{"tick": 0, "text": "测", "duration": 4}]
    commands_int = build_commands(config, None, virtual_layers, layer_to_group, 8)
    chk.ok(any("redstone_block" in command for command in commands_int), "启动链缺失")
    chk.ok(not any("/mpe text chain" in command for command in commands_int),
           "曲线排版不应生成歌词链")
    return chk.report("curve")


# ── 测试 4：中继器数量自动选择 ─────────────────────────────

def test_auto_branch_repeaters():
    print("测试 4：分支中继器自动档位")
    chk = Checks()
    for max_distance, expected in ((0, 0), (10, 0), (17, 0), (18, 1), (34, 2), (35, 3), (47, 3)):
        config = base_config(curve_branch_repeaters=-1)
        notes = [FakeNote(0, 0, key=45, velocity=100 - int(max_distance / 48 * 100))]
        virtual_layers = [{"vid": 0, "original_id": 0, "side": None,
                           "notes": notes, "inst": 0, "cat": "钢琴"}]
        plan = plan_curve(virtual_layers, {0: 0}, (0, 64, 0), 0, config)
        chk.ok(plan["branch_repeaters"] == expected,
               f"最远 {max_distance} 格 → 中继器 {plan['branch_repeaters']}（期望 {expected}）")
    return chk.report("auto")


def main():
    results = [
        test_branch_geometry(),
        test_simulator_semantics(),
        test_curve_end_to_end(),
        test_auto_branch_repeaters(),
    ]
    print()
    if all(results):
        print("全部测试通过")
        return 0
    print("存在失败项")
    return 1


if __name__ == "__main__":
    sys.exit(main())
