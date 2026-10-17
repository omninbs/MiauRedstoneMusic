"""配置文件默认值、读取与保存。"""

import json
import os

from .constants import Ansi

CONFIG_FILE = "nbs_chain_config.json"
DEFAULT_CONFIG = {
    "direction": "east",
    "merge_groups": [],
    "layout_style": "flat",
    "layout_radius": 5,
    "master_group": None,
    "flat_spacing": 3,
    "nest_layers": 1,
    "layer_gap": 4,
    "num_threads": 4,
    "use_redstone_lamp": False,
    "stereo_layers": [],
    "uniform_repeater_mode": False,
    "group_staircase_modes": {},  # 每组生成模式：{"0": "staircase", "1": "default", ...}
    "lyrics_color": "white",
    "lyrics_scale": 10.0,
    "lyrics_duration": 20,
    "lyrics_enter": 5,
    "lyrics_exit": 5,
    "lyrics_y_offset": 5,        # 歌词文字相对主轨道的高度（方块）
    "lyrics_side_offset": None,  # 歌词文字额外的侧向偏移
    "lyrics_follow": True,       # 文字是否随命令方块沿链条向前移动
    "lyrics_import": {},         # 导入的歌词：{"tick": {"text": ..., "duration": ...}}
    "lyrics_chain_y_offset": 1,  # 命令方块链相对结构最低方块向下的层数（默认紧贴结构底部下方）
    "lyrics_stream": False,      # 流式歌词：连续文字累积显示（A -> AB -> ABC）
    "lyrics_stream_seconds": 2.0,  # 相邻歌词间隔超过该秒数即断段
    "lyrics_stream_max_len": 18,  # 单段累积文字上限（字），超过即断段
    # ── 音符盒粒子特效（MiauParticleEffects 对接）────────────────
    "particles_enabled": False,   # 总开关
    "particles_layers": [],       # 参与粒子的虚拟层 vid 列表
    "particles_trail": 24,        # 拖尾长度（粒子数）
    "particles_curve": "arc",     # 竖直起伏：arc / sine / line
    "particles_height": 3,        # 跳动最大高度（方块）
    "particles_color": "",        # 纯色 #RRGGBB 或 rainbow；空 = 交模组自动分配
    "particles_gradient": "",     # 渐变 #RRGGBB,#RRGGBB 或 rainbow；空 = 不指定
    "particles_linger": 40        # 末音符后停留 tick；-1 = 常驻
}

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            # 迁移旧版本配置：移除已废弃的「歌词轨道 / 手输歌词」键
            legacy = any(key in cfg for key in ("lyrics_track", "lyrics_text"))
            for stale in ("lyrics_track", "lyrics_text"):
                cfg.pop(stale, None)
            # 旧默认 scale=1.0 迁移到新默认 10.0（仅在旧配置里迁移一次，
            # 之后该键已被用户/新默认接管，不会再被改写）
            if legacy and cfg.get("lyrics_scale") == 1.0:
                cfg["lyrics_scale"] = DEFAULT_CONFIG["lyrics_scale"]
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        print(Ansi.success("配置已保存"))
    except Exception as e:
        print(Ansi.error(f"保存配置失败: {e}"))


# ── 常量 ─────────────────────────────────────────────────────

