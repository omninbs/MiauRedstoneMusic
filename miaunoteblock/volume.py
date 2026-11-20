"""音量 / 声像模型。

音符盒在 Java 版里以 fixed volume = 3 发声，可听半径 = 3 × 16 = 48 格，
并且在这个半径内**线性衰减**。因此「音符盒离玩家多远」就等价于「音量百分比」：

    音量百分比 ≈ (1 - 距离 / 48) × 100%

本模块负责把 NBS 的 velocity / panning 换算成：
- ``side``：音符应该放在中心轴的左侧(-1)还是右侧(+1)（由 curve.py 综合声像与平衡决定）；
- ``distance``：音符盒到中心轴的横向距离（整数格），
  使得 ``sqrt(distance² + 垂直偏移²)`` 尽量接近目标三维距离。
"""

SOUND_RANGE = 48.0          # 音符盒可听半径（格）
MIN_DISTANCE = 2            # 离中心轴的最小横向距离（格）：再近就会与中心走廊重叠


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
    """目标音量 → 目标三维距离（格）。"""
    return clamp(sound_range * (1.0 - clamp(volume, 0.0, 1.0)), 0.0, sound_range)


def lateral_from_3d(target: float, vertical: float) -> float:
    """已知目标三维距离与垂直偏移，求需要的横向距离。"""
    squared = target * target - vertical * vertical
    return squared ** 0.5 if squared > 0 else 0.0


def volume_from_distance(distance: float, sound_range: float = SOUND_RANGE) -> float:
    """三维距离 → 相对音量（0~1），与游戏内线性衰减一致。"""
    if sound_range <= 0:
        return 0.0
    return clamp(1.0 - distance / sound_range, 0.0, 1.0)


def distance_3d(ax: float, ay: float, az: float,
                bx: float, by: float, bz: float) -> float:
    return ((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2) ** 0.5


def plan_repeater_count(max_distance: float) -> int:
    """按最远横向距离，决定每条分支固定要串几个中继器。

    固定数量是「所有音符同时发声」的前提：分支里的每级中继器都会带来
    2 个游戏 tick 延迟，只有全曲所有分支延迟一致，各事件之间的相对
    时间才不会漂移。

    可达距离：
        n = 0 → 最远 17 格
        n ≥ 1 → 最远 16n + 2 格
    """
    if max_distance <= 17:
        return 0
    for count in (1, 2, 3):
        if 16 * count + 2 >= max_distance:
            return count
    return 3


def branch_distance_range(repeaters: int) -> tuple:
    """该中继器数量下，横向距离的合法区间（含端点）。"""
    if repeaters <= 0:
        return MIN_DISTANCE, 17
    low = repeaters + 2
    high = 16 * repeaters + 2
    return max(MIN_DISTANCE, low), min(47, high)
