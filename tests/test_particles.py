"""音符盒粒子特效对接自检脚本。

不依赖 pytest，直接 `python tests/test_particles.py` 运行。

覆盖：
1. 每个被选中的分组生成一个**脉冲**命令方块，命令以 `/mpe noteblock chain ~~~` 开头。
2. 坐标偏移 = 音符盒内部绝对坐标 − 命令方块坐标。
3. delay[0] 与 delay[i] 符合时序公式（中继器 1 档 = 2 游戏 tick，命令方块自身 1 tick）。
4. 同一 tick 多个并排音符盒随机只取一个。
5. 未选择轨道时不产生任何命令。
"""

import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from miaunoteblock.constants import DIR_OFFSET
from miaunoteblock.layout import compute_offsets
from miaunoteblock.generation import (
    COMMAND_MAX_LEN,
    build_all_commands_virtual,
    warn_oversized_commands,
)
from miaunoteblock.particles import build_particle_commands


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


def base_config():
    return {"layout_style": "flat", "flat_spacing": 3, "master_group": None}


def particle_config(layers):
    return {
        "particles_enabled": True,
        "particles_layers": layers,
        "particles_trail": 24,
        "particles_curve": "arc",
        "particles_height": 3,
        "particles_color": "",
        "particles_gradient": "",
        "particles_linger": 40,
    }


def run_generation(vl, l2g, direction="east", max_tick=10):
    groups = sorted(set(l2g.values()))
    offsets = compute_offsets(groups, base_config(), len(groups))
    note_events = []
    commands, activation = build_all_commands_virtual(
        vl, l2g, offsets, (0, 0, 0), direction, max_tick,
        note_events=note_events,
    )
    return note_events, activation, commands


CMD_RE = re.compile(r'Command:("(?:[^"\\]|\\.)*")')
SETBLOCK_RE = re.compile(r'^setblock\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)')
SETBLOCK_FULL_RE = re.compile(r'^setblock\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+)$')
FILL_RE = re.compile(r'^fill\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+)$')
DELAY_RE = re.compile(r"delay=(\d+)")


def occupied_cells(commands):
    """把生成器的 setblock / fill 命令展开为被占用的方块坐标集合。"""
    cells = set()
    for cmd in commands:
        cmd = cmd.strip()
        if cmd.startswith("#"):
            continue
        m = SETBLOCK_FULL_RE.match(cmd)
        if m:
            cells.add((int(m.group(1)), int(m.group(2)), int(m.group(3))))
            continue
        m = FILL_RE.match(cmd)
        if m:
            x1, y1, z1, x2, y2, z2 = (int(m.group(i)) for i in range(1, 7))
            for x in range(min(x1, x2), max(x1, x2) + 1):
                for y in range(min(y1, y2), max(y1, y2) + 1):
                    for z in range(min(z1, z2), max(z1, z2) + 1):
                        cells.add((x, y, z))
    return cells


def parse_particle_command(cmd):
    """解析粒子命令方块，返回 (anchor 坐标, [(x,y,z,delay), ...], 原始命令)。"""
    m = CMD_RE.search(cmd)
    assert m, f"无法解析命令方块: {cmd}"
    text = json.loads(m.group(1))
    sm = SETBLOCK_RE.match(cmd)
    assert sm, f"无法解析 setblock: {cmd}"
    anchor = (int(sm.group(1)), int(sm.group(2)), int(sm.group(3)))
    assert text.startswith("/mpe noteblock chain ~~~"), text
    notes = []
    for seg in text.split()[4].split("|"):
        if not seg:
            continue
        x, y, z, d = seg.split(",")
        notes.append((int(x), int(y), int(z), int(d)))
    return anchor, notes, text


def make_layers():
    """两个音乐组（各一条红石链）。"""
    layer0 = [FakeNote(t, layer=0) for t in (0, 5, 10)]
    layer1 = [FakeNote(t, layer=1, key=50) for t in (5, 10)]
    vl = [
        {"vid": 0, "original_id": 0, "side": None, "notes": layer0, "inst": 0, "cat": "钢琴"},
        {"vid": 1, "original_id": 1, "side": None, "notes": layer1, "inst": 0, "cat": "钢琴"},
    ]
    return vl, {0: 0, 1: 1}


# ── 测试用例 ──────────────────────────────────────────────────

def test_command_blocks():
    """选中两条轨道 → 两个脉冲命令方块，命令格式正确。"""
    vl, l2g = make_layers()
    note_events, activation, _cmds = run_generation(vl, l2g)
    cmds = build_particle_commands(note_events, activation, "east",
                                   particle_config([0, 1]), rng=random.Random(0))
    assert len(cmds) == 2, f"应有 2 个命令方块，实际 {len(cmds)}"
    for cmd in cmds:
        assert cmd.startswith("setblock "), cmd
        assert "chain_command_block" not in cmd, f"必须是脉冲型: {cmd}"
        assert "minecraft:command_block[facing=up]" in cmd, cmd
        assert "/mpe noteblock chain ~~~" in cmd, cmd
    print("  [command-block] OK  通过断言 8 条")


def test_offsets_and_delay():
    """坐标偏移与 delay 时序公式。"""
    vl, l2g = make_layers()
    note_events, activation, _cmds = run_generation(vl, l2g)
    selected = [0, 1]
    cmds = build_particle_commands(note_events, activation, "east",
                                   particle_config(selected), rng=random.Random(0))

    dx, _, dz = DIR_OFFSET["east"]
    global_first = min(ev[2] for ev in note_events)

    by_gid = {}
    for gid, vid, tick, x, y, z in note_events:
        if vid in selected:
            by_gid.setdefault(gid, []).append((tick, x, y, z))

    assert len(cmds) == len(by_gid), f"命令数 {len(cmds)} != 组数 {len(by_gid)}"
    checked = 0
    for cmd, gid in zip(cmds, sorted(by_gid)):
        anchor, notes, _ = parse_particle_command(cmd)
        ax, ay, az = activation[gid]
        expect_anchor = (ax - dx, ay, az - dz)
        assert anchor == expect_anchor, f"锚点 {anchor} != {expect_anchor}"

        cands = sorted(by_gid[gid])
        assert len(cands) == len(notes), f"音符数 {len(notes)} != {len(cands)}"
        prev = None
        for i, (note, (tick, x, y, z)) in enumerate(zip(notes, cands)):
            px, py, pz, delay = note
            assert (px, py, pz) == (x - anchor[0], y - anchor[1], z - anchor[2]), \
                f"偏移 {(px, py, pz)} != {(x - anchor[0], y - anchor[1], z - anchor[2])}"
            expect_delay = (1 + 2 * (tick - global_first)) if i == 0 else 2 * (tick - prev)
            assert delay == expect_delay, f"delay {delay} != {expect_delay}"
            prev = tick
            checked += 1
    print(f"  [offset/delay] OK  通过断言 {checked * 3} 条")


def test_duplicate_tick_random():
    """同一 tick 两个并排音符盒 → 随机取一个，同组只生成一颗球。"""
    a = [FakeNote(0, layer=0), FakeNote(5, layer=0)]
    b = [FakeNote(0, layer=1, key=50), FakeNote(5, layer=1, key=52)]
    vl = [
        {"vid": 0, "original_id": 0, "side": None, "notes": a, "inst": 0, "cat": "钢琴"},
        {"vid": 1, "original_id": 1, "side": None, "notes": b, "inst": 0, "cat": "钢琴"},
    ]
    l2g = {0: 0, 1: 0}  # 同一组 -> 同一 tick 两个音符盒并排
    note_events, activation, _cmds = run_generation(vl, l2g)

    cmds = build_particle_commands(note_events, activation, "east",
                                   particle_config([0, 1]), rng=random.Random(1))
    assert len(cmds) == 1, f"同组只应生成一颗球，实际 {len(cmds)}"
    anchor, notes, _ = parse_particle_command(cmds[0])
    assert len(notes) == 2, f"两个 tick 各取一个，应为 2 条，实际 {len(notes)}"

    by_tick = {}
    for _gid, _vid, tick, x, y, z in note_events:
        by_tick.setdefault(tick, set()).add((x - anchor[0], y - anchor[1], z - anchor[2]))
    for i, (px, py, pz, _delay) in enumerate(notes):
        tick = sorted(by_tick)[i]
        assert (px, py, pz) in by_tick[tick], f"tick {tick} 落点 {(px, py, pz)} 不在候选 {by_tick[tick]}"

    # 两个 tick（0、5）之间：首条 delay=1，次条 = 2*(5-0)=10
    assert notes[0][3] == 1, notes
    assert notes[1][3] == 10, notes
    print("  [random-pick] OK  通过断言 6 条")


def test_only_selected():
    """只选一条轨道 → 只生成它所在组的命令。"""
    vl, l2g = make_layers()
    note_events, activation, _cmds = run_generation(vl, l2g)
    cmds = build_particle_commands(note_events, activation, "east",
                                   particle_config([0]), rng=random.Random(0))
    assert len(cmds) == 1, f"只选 1 条轨道应生成 1 个命令方块，实际 {len(cmds)}"
    print("  [only-selected] OK  通过断言 1 条")


def test_no_selection():
    """未选择任何轨道 → 不产生命令。"""
    vl, l2g = make_layers()
    note_events, activation, _cmds = run_generation(vl, l2g)
    assert build_particle_commands(note_events, activation, "east",
                                   particle_config([])) == []
    assert build_particle_commands([], activation, "east",
                                   particle_config([0, 1])) == []
    print("  [no-selection] OK  通过断言 2 条")


def test_anchor_no_overlap():
    """粒子命令方块位置（链起点上游一格）不得与红石链已有方块/支撑填充重叠。"""
    checked = 0
    for direction in ("east", "west", "south", "north"):
        vl, l2g = make_layers()
        note_events, activation, cmds = run_generation(vl, l2g, direction=direction)
        pcmds = build_particle_commands(note_events, activation, direction,
                                        particle_config([0, 1]), rng=random.Random(0))
        assert len(pcmds) == 2, f"{direction}: 应生成 2 个命令方块，实际 {len(pcmds)}"
        occupied = occupied_cells(cmds)
        for cmd in pcmds:
            anchor, _notes, _ = parse_particle_command(cmd)
            assert anchor not in occupied, f"{direction}: 粒子命令方块 {anchor} 与已有方块重叠"
            checked += 1
    print(f"  [no-overlap] OK  通过断言 {checked} 条")


def test_timing_alignment():
    """delay 必须与红石链的真实受电时间对齐。

    模型：红石块在 t0 放置 → 起始中继器 delay=1 → 第一个事件格在 t0+2 发声。
    链上「中继器 delay 累加和」记作 power，则某个音符盒发声时刻 = 2×power（相对 t0）。
    粒子命令方块与红石块相邻，在 t0+1 执行，因此 delay 应 = 2×power − 1。
    """
    notes = [FakeNote(t, layer=0) for t in (0, 5, 10)]
    vl = [{"vid": 0, "original_id": 0, "side": None, "notes": notes, "inst": 0, "cat": "钢琴"}]
    note_events, activation, cmds = run_generation(vl, {0: 0}, direction="east", max_tick=10)
    pcmds = build_particle_commands(note_events, activation, "east",
                                    particle_config([0]), rng=random.Random(0))
    assert len(pcmds) == 1, f"应生成 1 个命令方块，实际 {len(pcmds)}"
    anchor, out_notes, _ = parse_particle_command(pcmds[0])

    # 还原链：按链轴排序，中继器累加 delay，其余位置记录当前 power
    blocks = {}
    for cmd in cmds:
        m = SETBLOCK_FULL_RE.match(cmd.strip())
        if m:
            blocks[(int(m.group(1)), int(m.group(2)), int(m.group(3)))] = m.group(4)
    chain_y = anchor[1]
    power = 0
    times = {}
    for coord, block in sorted([(c, b) for c, b in blocks.items() if c[1] == chain_y],
                               key=lambda it: it[0][0]):
        if "repeater" in block:
            dm = DELAY_RE.search(block)
            power += int(dm.group(1)) if dm else 1
        else:
            times[coord] = power

    cum = 0
    for px, py, pz, delay in out_notes:
        coord = (anchor[0] + px, anchor[1] + py, anchor[2] + pz)
        assert coord in times, f"音符盒 {coord} 不在链上"
        # delay 是相对上一个音符的增量，累加后才等于「命令执行后经过的 tick 数」
        cum += delay
        expect = 2 * times[coord] - 1
        assert cum == expect, f"{coord}: 累计 delay {cum} != {expect}（power={times[coord]}）"
    print(f"  [timing] OK  通过断言 {len(out_notes)} 条")


def test_anchor_vs_launch_pillar():
    """粒子命令方块不得与 app.py 生成的启动柱命令方块同格（否则会覆盖启动柱）。"""
    checked = 0
    for direction in ("east", "west", "south", "north"):
        for style in ("flat", "circle"):
            cfg = base_config()
            cfg["layout_style"] = style
            cfg["layout_radius"] = 5
            vl, l2g = make_layers()
            groups = sorted(set(l2g.values()))
            offsets = compute_offsets(groups, cfg, len(groups))
            note_events = []
            _c, activation = build_all_commands_virtual(
                vl, l2g, offsets, (0, 0, 0), direction, 10, note_events=note_events)
            pcmds = build_particle_commands(note_events, activation, direction,
                                            particle_config([0, 1]), rng=random.Random(0))
            dx, _dy, dz = DIR_OFFSET[direction]
            on_x = direction in ("east", "west")
            cb1 = (-5 * dx, 0, 0 if on_x else -5 * dz)
            cb2 = (-7 * dx, 0, 0 if on_x else -7 * dz)
            pillars = set()
            for i in range(len(activation)):
                pillars.add((cb1[0], cb1[1] + i, cb1[2]))
                pillars.add((cb2[0], cb2[1] + i, cb2[2]))
            for cmd in pcmds:
                anchor, _n, _t = parse_particle_command(cmd)
                assert anchor not in pillars, \
                    f"{direction}/{style}: 粒子方块 {anchor} 与启动柱重叠"
                checked += 1
    print(f"  [vs-launch] OK  通过断言 {checked} 条")


def test_oversized_warning():
    """命令文本 ≥ 32500 字符的命令方块必须告警，未超限的不告警。"""
    long_text = "/mpe noteblock chain ~~~ " + "1,0,0,1|" * 4200
    assert len(long_text) >= COMMAND_MAX_LEN, len(long_text)
    long_cmd = ('setblock 1 2 3 minecraft:command_block[facing=up]'
                f'{{Command:{json.dumps(long_text)}}}')
    short_cmd = ('setblock 1 2 4 minecraft:command_block[facing=up]'
                 '{Command:"/mpe noteblock chain ~~~ 1,0,0,1"}')
    assert warn_oversized_commands([long_cmd, short_cmd]) == 1, "应报告 1 个超长命令方块"
    assert warn_oversized_commands([short_cmd]) == 0, "未超限不应报告"
    print("  [oversized] OK  通过断言 2 条")


def main():
    print("测试 1：粒子命令方块生成（脉冲型 + 命令格式）")
    test_command_blocks()
    print("测试 2：坐标偏移与 delay 时序")
    test_offsets_and_delay()
    print("测试 3：同一 tick 多音符盒随机取一个")
    test_duplicate_tick_random()
    print("测试 4：只选单条轨道")
    test_only_selected()
    print("测试 5：未选择轨道时不产生命令")
    test_no_selection()
    print("测试 6：命令方块位置不与红石链方块重叠（四个方向）")
    test_anchor_no_overlap()
    print("测试 7：delay 与红石链真实受电时间对齐")
    test_timing_alignment()
    print("测试 8：粒子命令方块不与启动柱重叠（四方向 × 平铺/圆形）")
    test_anchor_vs_launch_pillar()
    print("测试 9：命令方块文本长度 ≥ 32500 时告警")
    test_oversized_warning()
    print("\n全部通过")


if __name__ == "__main__":
    main()
