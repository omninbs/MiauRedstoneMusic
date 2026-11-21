"""乐器识别、音轨重排、虚拟层分组与音轨结构变换。"""

from collections import defaultdict
from typing import Dict, List, Tuple

from pynbs import Layer

from .constants import DRUM_INSTRUMENTS, INSTRUMENT_NAMES

def get_instrument_category(inst_id: int) -> str:
    if inst_id in DRUM_INSTRUMENTS:
        return "鼓类"
    return INSTRUMENT_NAMES.get(inst_id, f"乐器{inst_id}")

def analyze_track_instrument(song, layer_id: int, notes: list = None) -> Tuple[int, str]:
    if notes is None:
        notes = [n for n in song.notes if n.layer == layer_id]
    notes = sorted(notes, key=lambda n: n.tick)
    counter = defaultdict(int)
    sample_size = 30
    while sample_size <= 90 and not counter:
        window = notes[:sample_size]
        for note in window:
            counter[note.instrument] += 1
        if not counter and sample_size < len(notes):
            sample_size += 30
        else:
            break
    if not counter:
        return (0, "钢琴")
    main_inst = max(counter, key=counter.get)
    return (main_inst, get_instrument_category(main_inst))


# ── 音轨重排功能 ─────────────────────────────────────────────────
def get_instrument_group_key(inst_id: int) -> tuple:
    return (inst_id in DRUM_INSTRUMENTS, inst_id)

def rearrange_tracks_by_instrument(song):
    notes_by_instrument = defaultdict(list)
    for note in song.notes:
        notes_by_instrument[note.instrument].append(note)

    new_layers = []
    instrument_layer_map = defaultdict(list)

    for inst_id, notes in sorted(notes_by_instrument.items(), key=lambda x: get_instrument_group_key(x[0])):
        notes.sort(key=lambda n: n.tick)
        layer_assignments = []
        for note in notes:
            tick = note.tick
            assigned = False
            for layer_notes in layer_assignments:
                if not layer_notes or layer_notes[-1][0] != tick:
                    layer_notes.append((tick, note))
                    assigned = True
                    break
            if not assigned:
                layer_assignments.append([(tick, note)])
        for _ in layer_assignments:
            instrument_layer_map[inst_id].append(len(new_layers))
            new_layers.append(inst_id)

    new_layer_objects = []
    for idx, inst_id in enumerate(new_layers):
        inst_name = INSTRUMENT_NAMES.get(inst_id, f"乐器{inst_id}")
        count = sum(1 for i in instrument_layer_map[inst_id] if i == idx)
        layer_name = f"{inst_name}_{count}" if count > 0 else inst_name
        new_layer_objects.append(Layer(idx, layer_name, 0, 0))
    song.layers = new_layer_objects

    for inst_id, layer_indices in instrument_layer_map.items():
        notes = notes_by_instrument[inst_id]
        notes.sort(key=lambda n: n.tick)
        layer_assignments = []
        new_id_for_layer = []
        for note in notes:
            tick = note.tick
            assigned = False
            for idx, layer_notes in enumerate(layer_assignments):
                if not layer_notes or layer_notes[-1][0] != tick:
                    layer_notes.append((tick, note))
                    assigned = True
                    break
            if not assigned:
                new_id = layer_indices[len(layer_assignments)]
                layer_assignments.append([(tick, note)])
                new_id_for_layer.append(new_id)
        for layer_notes, new_lid in zip(layer_assignments, new_id_for_layer):
            for (_, note) in layer_notes:
                note.layer = new_lid

    return song

# ── 自动分组（基于虚拟层） ─────────────────────────────────
def auto_group_by_mode_on_layers(layers_info: List[Dict], mode: int) -> Dict[int, int]:
    """
    对虚拟层列表进行自动分组
    layers_info: 列表元素为 {'vid': int, 'original_id': int, 'side': str, 'inst': int, 'cat': str}
    mode: 1=同乐器优先  2=最大化合并  3=顺序系统音色合并  4=按平均音量
    返回 {vid: group_id}
    """
    layer_to_group = {}
    group_id = 0

    if mode == 4:
        def average_volume(layer: Dict) -> float:
            notes = layer.get('notes') or []
            if not notes:
                return 0.0
            total = 0.0
            for note in notes:
                try:
                    total += float(getattr(note, 'velocity', 100) or 0)
                except (TypeError, ValueError):
                    total += 100.0
            return total / len(notes)

        ordered = sorted(layers_info, key=lambda x: (average_volume(x), x['vid']))
        i = 0
        while i < len(ordered):
            base = average_volume(ordered[i])
            group = [ordered[i]['vid']]
            j = i + 1
            while (j < len(ordered) and len(group) < 3
                   and abs(average_volume(ordered[j]) - base) <= 10):
                group.append(ordered[j]['vid'])
                j += 1
            for vid in group:
                layer_to_group[vid] = group_id
            group_id += 1
            i = j
        return layer_to_group

    if mode == 3:
        # 顺序系统音色合并：严格按音轨原有顺序（从上到下），
        # 只合并相邻且音色相同的轨道，每组最多 3 条；
        # 音色不同或已满 3 条就另起一组。
        ordered = sorted(layers_info, key=lambda x: x['vid'])
        i = 0
        while i < len(ordered):
            inst = ordered[i]['inst']
            j = i
            group = []
            while j < len(ordered) and len(group) < 3 and ordered[j]['inst'] == inst:
                group.append(ordered[j]['vid'])
                j += 1
            for vid in group:
                layer_to_group[vid] = group_id
            group_id += 1
            i = j
        return layer_to_group

    if mode == 1:
        sorted_vids = sorted(layers_info, key=lambda x: (x['cat'], x['vid']))
    else:
        sorted_vids = sorted(layers_info, key=lambda x: x['vid'])
    i = 0
    while i < len(sorted_vids):
        group = []
        if mode == 1:
            cat = sorted_vids[i]['cat']
            j = i
            while j < len(sorted_vids) and len(group) < 3 and sorted_vids[j]['cat'] == cat:
                group.append(sorted_vids[j]['vid'])
                j += 1
            i = j
        else:
            group = [item['vid'] for item in sorted_vids[i:i+3]]
            i += len(group)
        for vid in group:
            layer_to_group[vid] = group_id
        group_id += 1
    return layer_to_group

def vertical_compress(song):
    tick_notes = defaultdict(list)
    for note in song.notes:
        tick_notes[note.tick].append(note)

    max_new_layer = 0
    for tick, notes in tick_notes.items():
        notes_sorted = sorted(notes, key=lambda n: n.layer)
        for idx, note in enumerate(notes_sorted):
            note.layer = idx
            if idx > max_new_layer:
                max_new_layer = idx

    new_layer_notes = defaultdict(list)
    for note in song.notes:
        new_layer_notes[note.layer].append(note)

    new_layers = []
    for lid in range(max_new_layer + 1):
        notes_in_layer = new_layer_notes.get(lid, [])
        if notes_in_layer:
            inst = notes_in_layer[0].instrument
            inst_name = INSTRUMENT_NAMES.get(inst, f"乐器{inst}")
            layer_name = f"{inst_name}_{lid}" if lid > 0 else inst_name
        else:
            layer_name = f"空层_{lid}"
        new_layers.append(Layer(lid, layer_name, 100, 0))
    song.layers = new_layers
    return song


# ── UI 交互（支持立体声设置）───────────────────────────────────

