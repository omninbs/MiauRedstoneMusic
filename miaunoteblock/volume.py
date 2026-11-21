"""音量模型。

音符盒固定音量、可听半径 48 格并线性衰减：

    单个音符盒的听感音量 ≈ 1 − 距离 / 48

左右声道各放一个同音高、同 tick 的音符盒时，两盒会叠加，所以每个盒子
只需要承担 ``V / 2``：

    距离 d = 48 × (1 − V/2)      （V=100% → 24 格，V=0 → 48 格）

主音轨不参与这套换算：由用户指定并固定按满音量处理。
"""

SOUND_RANGE = 48.0          # 音符盒可听半径（格）
LISTENER_EYE = 1.6          # 玩家站立时耳朵相对脚底的高度（格）


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def note_velocity(note) -> float:
    """NBS 音符力度 → 0~1 的音量（缺省 1.0）。"""
    raw = getattr(note, "velocity", 100)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 100.0
    return clamp(value / 100.0, 0.0, 1.0)


def note_pan(note) -> float:
    """NBS 声像 → -1~1（pynbs 的 panning 已经归一化到 -100~100）。"""
    raw = getattr(note, "panning", 0) or 0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.0
    return clamp(value / 100.0, -1.0, 1.0)


def layer_volume(song, layer_id: int) -> float:
    """取 NBS 音轨音量（0~1）；查不到时按满音量处理。"""
    for layer in getattr(song, "layers", []) or []:
        if getattr(layer, "id", None) == layer_id:
            try:
                return clamp(float(getattr(layer, "volume", 100)) / 100.0, 0.0, 1.0)
            except (TypeError, ValueError):
                return 1.0
    return 1.0


def layer_pan(song, layer_id: int) -> float:
    for layer in getattr(song, "layers", []) or []:
        if getattr(layer, "id", None) == layer_id:
            try:
                return clamp(float(getattr(layer, "panning", 0)) / 100.0, -1.0, 1.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def target_distance(volume: float, sound_range: float = SOUND_RANGE) -> float:
    """单个音符盒要独自达到音量 V 时需要的距离。"""
    return sound_range * (1.0 - clamp(volume, 0.0, 1.0))


def dual_distance(volume: float, sound_range: float = SOUND_RANGE) -> float:
    """左右各一个音符盒、每盒承担 V/2 时需要的距离。"""
    return sound_range * (1.0 - clamp(volume, 0.0, 1.0) / 2.0)


def lateral_from_3d(target: float, vertical: float) -> float:
    """已知目标三维距离与垂直偏移，求需要的横向距离。"""
    squared = target * target - vertical * vertical
    return squared ** 0.5 if squared > 0 else 0.0


def volume_from_distance(distance: float,
                         sound_range: float = SOUND_RANGE) -> float:
    """三维距离 → 单个音符盒的相对音量（0~1），线性衰减。"""
    if sound_range <= 0:
        return 0.0
    return clamp(1.0 - distance / sound_range, 0.0, 1.0)


def distance_3d(ax: float, ay: float, az: float,
                bx: float, by: float, bz: float) -> float:
    return ((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2) ** 0.5
