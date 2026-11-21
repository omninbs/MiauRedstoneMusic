"""排版偏移与嵌套层数计算。"""

import math
from typing import Dict, List, Tuple

from .constants import Ansi

def calc_nest_layers(num_groups: int, max_radius: int, layer_gap: int = 4, min_spacing: int = 4) -> int:
    max_layers = 0
    r = max_radius
    while r >= 1:
        capacity = int(2 * math.pi * r / min_spacing)
        if capacity >= 1:
            max_layers += 1
            r -= layer_gap
        else:
            break
    return max(1, max_layers)

def compute_min_radius(num_groups: int, style: str, has_master: bool = False) -> int:
    n = num_groups if not has_master else num_groups - 1
    if n <= 1:
        return 1
    if style in ("circle", "nest_circle"):
        return max(1, math.ceil(2 / math.sin(math.pi / n)))
    if style in ("square", "nest_square"):
        return max(1, math.ceil(n / 2))
    if style == "semicircle":
        return max(1, math.ceil(2 / math.sin(math.pi / (2*(n-1)))))
    if style == "semisquare":
        return max(1, math.ceil(n * 4 / 6))
    return 1

def get_min_radius_for_nest_circle(num_groups: int, num_layers: int, layer_gap: int, min_spacing: int = 4) -> int:
    """计算能容纳 num_groups 个组、num_layers 层（圆形）所需的最小最大半径"""
    low, high = 1, max(1, num_groups * min_spacing // 2)
    best = high
    while low <= high:
        mid = (low + high) // 2
        r = mid
        remaining = num_groups
        for _ in range(num_layers):
            if r < 1:
                capacity = 0
            else:
                capacity = int(2 * math.pi * r / min_spacing)
            if capacity <= 0:
                break
            alloc = min(capacity, remaining)
            remaining -= alloc
            r -= layer_gap
        if remaining <= 0:
            best = mid
            high = mid - 1
        else:
            low = mid + 1
    return best

def get_min_radius_for_nest_square(num_groups: int, num_layers: int, layer_gap: int, min_spacing: int = 4) -> int:
    """计算能容纳 num_groups 个组、num_layers 层（方形）所需的最小最大半径"""
    low, high = 1, max(1, num_groups * min_spacing // 2)
    best = high
    while low <= high:
        mid = (low + high) // 2
        r = mid
        remaining = num_groups
        for _ in range(num_layers):
            if r < 1:
                capacity = 0
            else:
                capacity = int(8 * r / min_spacing)
            if capacity <= 0:
                break
            alloc = min(capacity, remaining)
            remaining -= alloc
            r -= layer_gap
        if remaining <= 0:
            best = mid
            high = mid - 1
        else:
            low = mid + 1
    return best

def distribute_groups_to_circle_layers(num_groups: int, max_radius: int, num_layers: int,
                                       layer_gap: int = 4, min_spacing: int = 4) -> List[Tuple[int, int]]:
    """
    圆形嵌套：将组数均匀分配到各层，确保每个有容量的层都能分到组（尽可能）。
    返回 [(半径, 该层组数), ...]，半径从大到小（外层先）。
    """
    # 计算各层最大容量
    layers = []
    r = max_radius
    for _ in range(num_layers):
        if r < 1:
            capacity = 0
        else:
            capacity = int(2 * math.pi * r / min_spacing)
        layers.append([r, 0, capacity])
        r -= layer_gap
    # 均匀分配：从内向外（反转），让每层获得近似相等的组数
    remaining = num_groups
    layers_rev = list(reversed(layers))
    ideal = num_groups // num_layers
    rem = num_groups % num_layers
    for i, layer in enumerate(layers_rev):
        target = ideal + (1 if i < rem else 0)
        alloc = min(target, layer[2])
        layer[1] = alloc
        remaining -= alloc
    # 若有剩余（容量不足），从外层开始补
    if remaining > 0:
        for layer in layers:
            if remaining <= 0:
                break
            avail = layer[2] - layer[1]
            if avail > 0:
                add = min(avail, remaining)
                layer[1] += add
                remaining -= add
    # 极端情况：全部塞给最大半径层
    if remaining > 0:
        layers[0][1] += remaining
    # 过滤空层
    result = [(r, cnt) for r, cnt, _ in layers if cnt > 0]
    if not result:
        result = [(layers[0][0], num_groups)]
    return result

def distribute_groups_to_square_layers(num_groups: int, max_radius: int, num_layers: int,
                                       layer_gap: int = 4, min_spacing: int = 4) -> List[Tuple[int, int]]:
    layers = []
    r = max_radius
    for _ in range(num_layers):
        if r < 1:
            capacity = 0
        else:
            capacity = int(8 * r / min_spacing)
        layers.append([r, 0, capacity])
        r -= layer_gap
    remaining = num_groups
    layers_rev = list(reversed(layers))
    ideal = num_groups // num_layers
    rem = num_groups % num_layers
    for i, layer in enumerate(layers_rev):
        target = ideal + (1 if i < rem else 0)
        alloc = min(target, layer[2])
        layer[1] = alloc
        remaining -= alloc
    if remaining > 0:
        for layer in layers:
            if remaining <= 0:
                break
            avail = layer[2] - layer[1]
            if avail > 0:
                add = min(avail, remaining)
                layer[1] += add
                remaining -= add
    if remaining > 0:
        layers[0][1] += remaining
    result = [(r, cnt) for r, cnt, _ in layers if cnt > 0]
    if not result:
        result = [(layers[0][0], num_groups)]
    return result

# ── 排版偏移计算（修复嵌套圆/方）────────────────────────────────
def compute_offsets(
    groups: List[int],
    config: dict,
    num_groups: int
) -> List[Tuple[int, int]]:
    style = config.get("layout_style", "flat")
    radius = config.get("layout_radius", 5)
    master = config.get("master_group")
    has_master = (master is not None and master < num_groups)

    pos_dict = {}

    if style == "flat":
        spacing = config.get("flat_spacing", 3)
        start = - (num_groups - 1) * spacing // 2
        for i, gid in enumerate(groups):
            pos_dict[gid] = (0, start + i * spacing)

    elif style == "nest_circle":
        max_radius = radius
        num_layers = config.get("nest_layers", 1)
        layer_gap = config.get("layer_gap", 4)
        remaining_groups = [g for g in groups if g != master] if has_master else groups[:]
        need_radius = get_min_radius_for_nest_circle(len(remaining_groups), num_layers, layer_gap, min_spacing=4)
        if max_radius < need_radius:
            print(Ansi.prompt(f"警告：当前半径 {max_radius} 不足以容纳 {len(remaining_groups)} 个组分成 {num_layers} 层，已自动调整为 {need_radius}"))
            max_radius = need_radius
            config["layout_radius"] = max_radius
        layers_info = distribute_groups_to_circle_layers(len(remaining_groups), max_radius, num_layers, layer_gap, min_spacing=4)
        if has_master:
            pos_dict[master] = (0, 0)
        idx = 0
        for r, cnt in layers_info:
            for i in range(cnt):
                angle = 2 * math.pi * i / cnt if cnt > 0 else 0
                y = round(r * math.cos(angle))
                side = round(r * math.sin(angle))
                gid = remaining_groups[idx]
                pos_dict[gid] = (y, side)
                idx += 1

    elif style == "nest_square":
        max_radius = radius
        num_layers = config.get("nest_layers", 1)
        layer_gap = config.get("layer_gap", 4)
        remaining_groups = [g for g in groups if g != master] if has_master else groups[:]
        need_radius = get_min_radius_for_nest_square(len(remaining_groups), num_layers, layer_gap, min_spacing=4)
        if max_radius < need_radius:
            print(Ansi.prompt(f"警告：当前半径 {max_radius} 不足以容纳 {len(remaining_groups)} 个组分成 {num_layers} 层，已自动调整为 {need_radius}"))
            max_radius = need_radius
            config["layout_radius"] = max_radius
        layers_info = distribute_groups_to_square_layers(len(remaining_groups), max_radius, num_layers, layer_gap, min_spacing=4)
        if has_master:
            pos_dict[master] = (0, 0)
        idx = 0
        for r, cnt in layers_info:
            per_side = max(1, (cnt + 3) // 4)
            points = []
            # 上边
            for i in range(per_side):
                x = -r + (2 * r) * i / (per_side - 1) if per_side > 1 else 0
                points.append((r, round(x)))
            # 右边
            for i in range(per_side):
                y = r - (2 * r) * i / (per_side - 1) if per_side > 1 else 0
                points.append((round(y), r))
            # 下边
            for i in range(per_side):
                x = r - (2 * r) * i / (per_side - 1) if per_side > 1 else 0
                points.append((-r, round(x)))
            # 左边
            for i in range(per_side):
                y = -r + (2 * r) * i / (per_side - 1) if per_side > 1 else 0
                points.append((round(y), -r))
            unique = []
            seen = set()
            for pt in points:
                if pt not in seen:
                    seen.add(pt)
                    unique.append(pt)
            unique = unique[:cnt]
            if len(unique) < cnt:
                # 半径小、组数多时采样点不够，循环复用避免索引越界
                unique += [unique[i % len(unique)] for i in range(cnt - len(unique))]
            for i in range(cnt):
                y, side = unique[i]
                gid = remaining_groups[idx]
                pos_dict[gid] = (y, side)
                idx += 1

    elif style == "circle":
        if has_master and master in groups:
            pos_dict[master] = (0, 0)
            other = [g for g in groups if g != master]
            n = len(other)
            for i, gid in enumerate(other):
                angle = 2 * math.pi * i / n
                y = round(radius * math.cos(angle))
                side = round(radius * math.sin(angle))
                pos_dict[gid] = (y, side)
        else:
            for i, gid in enumerate(groups):
                angle = 2 * math.pi * i / len(groups)
                y = round(radius * math.cos(angle))
                side = round(radius * math.sin(angle))
                pos_dict[gid] = (y, side)

    elif style == "square":
        n = len(groups) - (1 if has_master else 0)
        if n <= 0:
            for gid in groups:
                pos_dict[gid] = (0, 0)
        else:
            side_len = 2 * radius
            per_side = max(1, int(math.ceil(n / 4)))
            top = [(radius, -radius + i * side_len // (per_side - 1)) for i in range(per_side)] if per_side > 1 else [(radius,0)]
            right = [(-radius + i * side_len // (per_side - 1), radius) for i in range(per_side)] if per_side > 1 else [(0,radius)]
            bottom = [(-radius, radius - i * side_len // (per_side - 1)) for i in range(per_side)] if per_side > 1 else [(-radius,0)]
            left = [(radius - i * side_len // (per_side - 1), -radius) for i in range(per_side)] if per_side > 1 else [(0,-radius)]
            points = top + right + bottom + left
            points = points[:n]
            if len(points) < n:
                # 组数多于采样点时补齐，避免索引越界
                points += [points[i % len(points)] for i in range(n - len(points))]
            if has_master:
                pos_dict[master] = (0, 0)
                idx = 0
                for gid in groups:
                    if gid == master:
                        continue
                    pos_dict[gid] = points[idx]
                    idx += 1
            else:
                for i, gid in enumerate(groups):
                    pos_dict[gid] = points[i]

    elif style == "semicircle":
        n = len(groups) - (1 if has_master else 0)
        if n <= 0:
            for gid in groups:
                pos_dict[gid] = (0, 0)
        else:
            angles = [math.pi * i / (n - 1) for i in range(n)] if n > 1 else [0]
            point_list = [(round(-radius * math.sin(a)), round(radius * math.cos(a))) for a in angles]
            if has_master:
                pos_dict[master] = (0, 0)
                idx = 0
                for gid in groups:
                    if gid == master:
                        continue
                    pos_dict[gid] = point_list[idx]
                    idx += 1
            else:
                for i, gid in enumerate(groups):
                    pos_dict[gid] = point_list[i]

    elif style == "semisquare":
        n = len(groups) - (1 if has_master else 0)
        if n <= 0:
            for gid in groups:
                pos_dict[gid] = (0, 0)
        else:
            side_len = 2 * radius
            num_top = max(1, (n + 1) // 2)
            num_side = max(1, (n - num_top + 1) // 2)
            left_side = [(-radius, -radius + i * side_len // (num_side - 1)) for i in range(num_side)] if num_side > 1 else [(-radius,0)]
            top = [(-radius + i * side_len // (num_top - 1), radius) for i in range(num_top)] if num_top > 1 else [(0,radius)]
            right_side = [(radius, radius - i * side_len // (num_side - 1)) for i in range(num_side)] if num_side > 1 else [(radius,0)]
            points = left_side + top + right_side
            points = points[:n]
            if len(points) < n:
                # 组数多于采样点时补齐，避免索引越界
                points += [points[i % len(points)] for i in range(n - len(points))]
            if has_master:
                pos_dict[master] = (0, 0)
                idx = 0
                for gid in groups:
                    if gid == master:
                        continue
                    pos_dict[gid] = points[idx]
                    idx += 1
            else:
                for i, gid in enumerate(groups):
                    pos_dict[gid] = points[i]

    else:
        spacing = config.get("flat_spacing", 3)
        start = - (num_groups - 1) * spacing // 2
        for i, gid in enumerate(groups):
            pos_dict[gid] = (0, start + i * spacing)

    return [pos_dict[gid] for gid in groups]


def apply_offset_overrides(groups: List[int], offsets: List[Tuple[int, int]],
                           config: dict) -> List[Tuple[int, int]]:
    """用配置里的 ``group_offsets`` 覆盖（或微调）每组的起始位置。

    只有「可以手动编辑轨道」的排版才会带这个覆盖；音量控制排版的落点由
    音量公式决定，不允许手动改。
    """
    overrides = config.get("group_offsets") or {}
    if not isinstance(overrides, dict):
        return list(offsets)
    applied = []
    for index, gid in enumerate(groups):
        y_off, side_off = offsets[index]
        raw = overrides.get(str(gid))
        if isinstance(raw, dict):
            try:
                y_off = int(raw.get("y", y_off))
                side_off = int(raw.get("side", side_off))
            except (TypeError, ValueError):
                pass
        applied.append((y_off, side_off))
    return applied
# ── 命令生成（基于虚拟层和组映射）─────────────────────────────────

