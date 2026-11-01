"""按歌曲路径保存配置的自检脚本。

不依赖 pytest，直接 `python tests/test_config_store.py` 运行。

覆盖内容：
1. 一个歌曲路径 → 一个固定配置文件（相对 / 绝对写法指向同一个文件）。
2. 不同歌曲（哪怕文件名相同）互不覆盖。
3. 保存 → 读取往返，配置不变。
4. 旧版本字段被丢弃，缺少的字段用默认值补齐。
5. 配置文件损坏时回退默认配置，不抛异常。
6. JSON 里记录了歌曲绝对路径与曲名，方便人工排查。
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from miaunoteblock.config import (
    CONFIG_FORMAT_VERSION,
    DEFAULT_CONFIG,
    config_path_for,
    default_config,
    load_song_config,
    save_song_config,
)


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


# ── 测试 1：路径 → 配置文件 ──────────────────────────────────

def test_path_mapping():
    print("测试 1：一个歌曲路径对应一个配置文件")
    chk = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        relative = config_path_for("song.nbs", tmp)
        absolute = config_path_for(os.path.join(os.getcwd(), "song.nbs"), tmp)
        quoted = config_path_for('  "song.nbs"  ', tmp)
        chk.ok(relative == absolute, f"相对/绝对路径映射不一致: {relative} != {absolute}")
        chk.ok(relative == quoted, f"带引号的路径映射不一致: {relative} != {quoted}")
        chk.ok(relative.parent == config_path_for("x.nbs", tmp).parent,
               "配置文件应都放在同一个 configs 目录")
        chk.ok(relative.suffix == ".json", f"配置应为 json: {relative.name}")
        chk.ok("song" in relative.name, f"文件名应包含歌曲名便于识别: {relative.name}")

        # 同名不同目录 → 不同配置
        other = config_path_for(os.path.join(tmp, "sub", "song.nbs"), tmp)
        chk.ok(other != relative, f"同名不同目录的歌曲不应共用配置: {other.name} == {relative.name}")
    return chk.report("path")


# ── 测试 2：保存与恢复 ───────────────────────────────────────

def test_roundtrip():
    print("测试 2：保存后重新读取，配置不变")
    chk = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        config = default_config()
        chk.ok(load_song_config("song.nbs", tmp) == DEFAULT_CONFIG,
               "没有配置时应返回默认配置")

        config["layout_style"] = "circle"
        config["layout_radius"] = 9
        config["uniform_repeater_mode"] = True
        config["group_staircase_modes"] = {"0": "staircase"}
        config["lyrics_color"] = "#FFD700"
        config["lyrics_char_threshold"] = 8
        config["lyrics_break_on_punctuation"] = True
        config["lyrics_import"] = [{"tick": 10, "text": "起", "duration": 4}]
        config["particles_enabled"] = True
        config["particles_layers"] = [0, 2]
        config["particles_trail"] = 12

        written = save_song_config("song.nbs", config, "失眠", tmp)
        chk.ok(written is not None and written.exists(), "配置文件未写出")
        restored = load_song_config("song.nbs", tmp)
        chk.ok(restored == config, "读取回来的配置与保存的不一致")
        chk.ok(restored["lyrics_import"] == [{"tick": 10, "text": "起", "duration": 4}],
               f"歌词数据丢失: {restored['lyrics_import']}")

        # 再次保存会覆盖同一个文件，而不是新建
        config["layout_style"] = "square"
        save_song_config("song.nbs", config, "失眠", tmp)
        chk.ok(load_song_config("song.nbs", tmp)["layout_style"] == "square",
               "再次保存未覆盖旧值")
        files = list(config_path_for("song.nbs", tmp).parent.glob("*.json"))
        chk.ok(len(files) == 1, f"同一首歌应只有一个配置文件: {[f.name for f in files]}")

        # JSON 里记录了歌曲信息
        with open(written, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        chk.ok(payload.get("formatVersion") == CONFIG_FORMAT_VERSION, "缺少格式版本")
        chk.ok(os.path.isabs(payload.get("songPath", "")), f"songPath 应为绝对路径: {payload}")
        chk.ok(payload.get("songName") == "失眠", f"曲名未记录: {payload.get('songName')}")
        chk.ok(isinstance(payload.get("config"), dict), "缺少 config 字段")
    return chk.report("roundtrip")


# ── 测试 3：不同歌曲互不影响 ─────────────────────────────────

def test_per_song_isolation():
    print("测试 3：不同歌曲各用各的配置")
    chk = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        first = default_config()
        first["layout_style"] = "circle"
        save_song_config("a.nbs", first, "A", tmp)

        second = default_config()
        second["layout_style"] = "square"
        save_song_config(os.path.join(tmp, "sub", "b.nbs"), second, "B", tmp)

        chk.ok(load_song_config("a.nbs", tmp)["layout_style"] == "circle", "A 的配置被影响")
        chk.ok(load_song_config(os.path.join(tmp, "sub", "b.nbs"), tmp)["layout_style"] == "square",
               "B 的配置被影响")
        chk.ok(load_song_config("c.nbs", tmp)["layout_style"] == DEFAULT_CONFIG["layout_style"],
               "陌生歌曲不应读到别人的配置")
    return chk.report("isolation")


# ── 测试 4：未知 / 缺失字段 ──────────────────────────────────

def test_unknown_and_missing_keys():
    print("测试 4：旧字段丢弃、缺失字段补默认")
    chk = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        path = config_path_for("song.nbs", tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"formatVersion": 1, "config": {
                "layout_style": "circle",
                "lyrics_stream": True,        # 旧版本字段，已移除
                "lyrics_duration": 20,        # 旧版本字段，已移除
            }}, handle)

        config = load_song_config("song.nbs", tmp)
        chk.ok(config["layout_style"] == "circle", "有效字段未恢复")
        chk.ok("lyrics_stream" not in config, f"旧字段未被丢弃: {sorted(config)}")
        chk.ok("lyrics_duration" not in config, "旧字段未被丢弃")
        chk.ok(config.keys() == DEFAULT_CONFIG.keys(), "配置键集合应与默认配置一致")
        chk.ok(config["lyrics_char_threshold"] == DEFAULT_CONFIG["lyrics_char_threshold"],
               "缺失字段未补默认值")
    return chk.report("keys")


# ── 测试 5：损坏的配置文件 ───────────────────────────────────

def test_broken_file():
    print("测试 5：配置文件损坏时回退默认配置")
    chk = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        path = config_path_for("song.nbs", tmp)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON")
        chk.ok(load_song_config("song.nbs", tmp) == DEFAULT_CONFIG, "损坏文件未回退默认配置")

        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"formatVersion": 1, "config": ["不是对象"]}, handle)
        chk.ok(load_song_config("song.nbs", tmp) == DEFAULT_CONFIG, "错误结构未回退默认配置")

        # 修复后能正常读回
        good = default_config()
        good["flat_spacing"] = 7
        save_song_config("song.nbs", good, "song", tmp)
        chk.ok(load_song_config("song.nbs", tmp)["flat_spacing"] == 7, "修复后仍未恢复")
    return chk.report("broken")


def main():
    results = [
        test_path_mapping(),
        test_roundtrip(),
        test_per_song_isolation(),
        test_unknown_and_missing_keys(),
        test_broken_file(),
    ]
    print()
    if all(results):
        print("全部通过")
        return 0
    print("存在失败项")
    return 1


if __name__ == "__main__":
    sys.exit(main())
