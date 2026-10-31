"""运行期配置默认值。

配置不再持久化到磁盘（不读也不写 nbs_chain_config.json），
每次启动都从 :data:`DEFAULT_CONFIG` 的副本开始，会话内修改只影响本次生成。
"""

import copy

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
    # ── 歌词（/mpe text chain 粒子字幕）──────────────────────────
    "lyrics_color": "white",
    "lyrics_scale": 10.0,
    "lyrics_duration": 20,       # 单条歌词默认显示时长（tick）
    "lyrics_enter": 5,           # 入场动画时长（tick）
    "lyrics_exit": 5,            # 出场动画时长（tick）
    "lyrics_anim_in": "typewriter:left",  # 入场动画（类别:样式），空 = 无动画
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
    "lyrics_stream": True,       # 流式歌词：连续文字合并成一句
    "lyrics_stream_seconds": 2.0,  # 相邻歌词间隔超过该秒数即断段
    "lyrics_stream_max_len": 18,  # 单段累积文字上限（字），超过即断段
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
