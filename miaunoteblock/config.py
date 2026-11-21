"""运行期配置默认值，以及「按歌曲路径」持久化。

配置以**歌曲文件路径**为键保存：同一个路径永远对应 ``configs/`` 下的同一个 JSON，
下次打开这首歌时自动恢复上次的设置；不同歌曲互不影响。
``configs/`` 放在程序安装目录（本包的上一级）下。
"""

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Optional

from .constants import Ansi

DEFAULT_CONFIG = {
    "layout_style": "flat",
    "layout_radius": 5,
    "master_group": None,
    "flat_spacing": 3,
    "nest_layers": 1,
    "layer_gap": 4,
    "stereo_layers": [],
    "uniform_repeater_mode": False,
    "group_staircase_modes": {},  # 每组生成模式：{"0": "staircase", "1": "default", ...}
    # ── 曲线音量排版（layout_style = "curve"）──────────────────
    "curve_lane_spacing": 3,          # 普通车道之间的垂直间距（格）
    "curve_minecart": True,           # 生成隐形矿车跟随玩家的命令方块链
    "curve_sound_range": 48.0,        # 音符盒可听半径（音量公式的分母）
    "curve_master_layers": [],        # 主音轨（原始音轨 id，固定满音量、放中心轴上）
    "curve_master_spacing": 3,        # 主音轨之间的垂直间距（格）
    "curve_corridor_height": 2,       # 玩家走廊高度（格）
    # ── 歌词（/mpe text chain 逐字粒子字幕）──────────────────────
    "lyrics_color": "white",
    "lyrics_scale": 10.0,
    "lyrics_char_threshold": 4,  # 断句阈值：duration 严格大于它就断句（与 duration 同单位）
    "lyrics_max_chars": 18,      # 单句字数上限：写满这么多字也断句（0 = 不限）
    "lyrics_break_on_punctuation": False,  # 标点（，。！？等）也作为句末，默认关闭
    "lyrics_start_lead_ticks": 4,  # 歌词相对音乐提前多少**歌曲** tick 显示（1 tick = 2 游戏 tick）
    "lyrics_enter": 0,           # 每个字的入场动画时长（tick），逐字模式建议 0~2
    "lyrics_exit": 3,            # 每个字的退场动画时长（tick）
    "lyrics_anim_in": "scale:enlarge",  # 入场动画（类别:样式），空 = 无动画
    "lyrics_anim_out": "",       # 出场动画，空 = 交给模组按入场镜像
    "lyrics_y_offset": 5,        # 歌词文字相对主轨道的高度（方块）
    "lyrics_side_offset": None,  # 歌词文字额外的侧向偏移
    "lyrics_lead_distance": 16,  # 文字相对「当前激活中继器」沿延展方向的前方距离（方块）
    "lyrics_jitter_x": 0.0,      # 文字位置的随机范围（±方块，0.5 步进；0 = 不随机）
    "lyrics_jitter_y": 0.0,
    "lyrics_jitter_z": 0.0,
    "lyrics_follow": True,       # 文字是否随激活位置沿链条前进
    "lyrics_import": [],         # 导入的歌词：[{"tick": ..., "text": ..., "duration": ...}]
    "lyrics_chain_y_offset": 1,  # 歌词命令方块相对结构最低方块向下的层数
    # ── 音符盒粒子特效（MiauParticleEffects 对接）────────────────
    "particles_enabled": False,   # 总开关
    "particles_layers": [],       # 参与粒子的虚拟层 vid 列表
    "particles_trail": 24,        # 拖尾长度（粒子数）
    "particles_curve": "arc",     # 竖直起伏：arc / sine / line
    "particles_height": 3,        # 跳动最大高度（方块）
    "particles_color": "",        # 纯色 #RRGGBB 或 rainbow；空 = 交给模组自动分配
    "particles_gradient": "",     # 渐变 #RRGGBB,#RRGGBB 或 rainbow；空 = 不指定
    "particles_linger": 40,       # 末音符后停留 tick；-1 = 常驻
    "particles_preset": "normal",  # 风格预设：light / normal / strong（仅 UI 用）
}


def default_config() -> dict:
    """返回一份全新的默认配置（深层拷贝，避免会话之间互相污染）。"""
    return copy.deepcopy(DEFAULT_CONFIG)


# ── 按歌曲路径持久化 ─────────────────────────────────────────

CONFIG_DIR_NAME = "configs"
CONFIG_FORMAT_VERSION = 1


def _clean_song_path(song_path: str) -> str:
    """用户输入的歌曲路径 → 绝对路径（去掉引号，展开 ~）。"""
    raw = str(song_path or "").strip().strip('"').strip("'")
    return os.path.abspath(os.path.expanduser(raw or "song.nbs"))


def config_root(directory: Optional[os.PathLike] = None) -> Path:
    """配置目录：默认是程序安装目录下的 ``configs/``。"""
    if directory is not None:
        return Path(directory)
    return Path(__file__).resolve().parent.parent / CONFIG_DIR_NAME


def config_path_for(song_path: str, directory: Optional[os.PathLike] = None) -> Path:
    """某首歌对应的配置文件：同一路径永远映射到同一个文件。

    文件名用「歌曲文件名 + 路径哈希」：可读，且同名不同目录的歌曲不会互相覆盖。
    """
    absolute = _clean_song_path(song_path)
    digest = hashlib.sha1(os.path.normcase(absolute).encode("utf-8")).hexdigest()[:10]
    stem = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", Path(absolute).stem).strip("_")[:40]
    return config_root(directory) / f"{stem or 'song'}_{digest}.json"


def _sanitize(saved: dict) -> dict:
    """以默认配置为底，只接受默认里存在的键（旧版本残留字段会被丢掉）。"""
    config = default_config()
    for key in DEFAULT_CONFIG:
        if key in saved:
            config[key] = copy.deepcopy(saved[key])
    return config


def load_song_config(song_path: str, directory: Optional[os.PathLike] = None) -> dict:
    """读取该歌曲上次保存的配置；没有、损坏或格式不对时回退到默认配置。"""
    path = config_path_for(song_path, directory)
    if not path.exists():
        return default_config()
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(Ansi.error(f"读取配置失败（{path.name}）：{exc}，本次使用默认配置"))
        return default_config()
    saved = data.get("config") if isinstance(data, dict) else None
    if not isinstance(saved, dict):
        print(Ansi.error(f"配置文件格式不正确（{path.name}），本次使用默认配置"))
        return default_config()
    return _sanitize(saved)


def save_song_config(song_path: str, config: dict, song_name: str = "",
                     directory: Optional[os.PathLike] = None) -> Optional[Path]:
    """把这个路径的配置写回 JSON；失败只告警（不打断生成），返回写入的文件。"""
    path = config_path_for(song_path, directory)
    payload = {
        "formatVersion": CONFIG_FORMAT_VERSION,
        "songPath": _clean_song_path(song_path),
        "songName": str(song_name or ""),
        "config": {key: config.get(key, default) for key, default in DEFAULT_CONFIG.items()},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(Ansi.error(f"保存配置失败（{path}）：{exc}"))
        return None
    return path
