# 音符盒粒子特效模组对接 — 实施计划

## Context（为什么做）

生成器目前只产出红石链结构（音符盒发声由 MC 播放），没有可视化。外部模组 **MiauParticleEffects** 提供了
`/mpe noteblock chain ~~~ x,y,z,delay|...` 接口：只要生成器额外产出一份「音符盒位置 + 相邻发声间隔」数据，
并放进一个**脉冲命令方块**，就能让弹力球按纯时间轴在音符盒之间跳动，第一遍播放即准确。

用户需求（已确认）：
1. 可**指定哪些轨道**（虚拟层音轨）带粒子；
2. 同一 tick 有并排多个音符盒时，**随机取其中一个**；
3. 在主菜单**新增可调项**（开关 + 参数）。

目标：生成 `.schem` 时，为每个被选中的分组（红石链）额外放置一个脉冲命令方块，写入该链的粒子数据；
不选任何轨道时不产生任何额外命令，行为与现状完全一致。

## 命令契约要点

- 命令方块必须是**脉冲型**（`minecraft:command_block`），锚点写 `~~~`（取命令方块自身方块坐标）。
- 音符段格式：`x,y,z,delay|x,y,z,delay|...`，坐标是相对命令方块的**整数方块偏移**，delay 单位是**游戏 tick**。
- 可选参数：`trail=` `curve=` `height=` `color=` `gradient=` `linger=`；不写 `color` 时由模组自动分配颜色。

## 时序与延迟模型（已核验）

- 红石块（放在 `activation_positions[gid]`）→ 起始中继器 `delay=1` → 第一个事件格。
- 相对红石块放置时刻 t0：**第 0 个事件格在 2 游戏 tick 发声**；相邻事件格间隔 = `2 × (tick 差)`。
  （音符盒后 relay `delay=1` 贡献 2；空白 tick 由 `split_to_repeaters` 填充，贡献 `2×(Δ−1)`；两种填充模式总延迟一致。）
- 粒子命令方块与红石块相邻（6 邻域），被激活后 **t0+1 执行**。
- 因此：`delay[0] = 1 + 2 × (该组首音符 tick − 全局最小音符 tick)`，`delay[i] = 2 × (tick_i − tick_{i−1})`。

## 改动清单

### 1. `miaunoteblock/generation.py`
- `build_all_commands_virtual` 新增**可选输出参数** `note_events: list = None`（保持现有返回值与签名兼容）。
- 收集处（约 L211-215）：`group_tick_notes[gid][note.tick].append(note)` → 改存 `(note, vlayer['vid'])`，
  以便知道每个音符盒属于哪条虚拟层音轨。
- 同步修改随之受影响的使用点（Plan 代理已核验清单）：
  - 单音符分支解包 `note, vid = notes[0]`（约 L365-369）；
  - `sorted(..., key=lambda n: n.layer)` → `key=lambda nv: nv[0].layer`（约 L397、L418）；
  - `zip([left_pos, right_pos], sorted_notes)` 与 mid/left/right 取值处解包（约 L402-406、L419-421、L430-440）。
- 在**每个音符盒 setblock 之后**追加一条事件到 `note_events`：`(gid, vid, tick, px, py, pz)`。
  覆盖 4 个分支：`note_count == 1 / 2 / >=3`（含阶梯模式 `side_y = y-1` 的侧向音符真实坐标）。

### 2. `miaunoteblock/particles.py`（新建）
与 `lyrics.py` 同构的独立模块，复用 `DIR_OFFSET`、`SIDE_AXIS`、`Ansi`：
- `build_particle_commands(note_events, activation_positions, direction, config, rng=None) -> List[str]`
  1. 按 `gid` 分组，只保留 `vid ∈ config["particles_layers"]` 的事件；无事件的组跳过。
  2. 全局最小音符 tick = 所有事件的 `min(tick)`。
  3. 每组按 tick 升序；**同一 tick 多候选用 `rng.choice` 随机取一个**。
  4. 对每个音符盒算相对偏移：命令方块锚点 = `activation_positions[gid]` 沿链反方向再前一格
     `(ax−dx, ay, az−dz)`（east 时约 x=−3，各 group 靠侧向区分；Plan 代理已核验该格为空且不与启动柱/歌词链冲突）。
  5. 组装 `x,y,z,delay|...`，拼上可选参数，写入
     `setblock <ax-dx> <ay> <az-dz> minecraft:command_block[facing=up]{Command:"/mpe noteblock chain ~~~ ..."}`。
- `edit_particle_settings(config, virtual_layers, layer_to_group)`：交互式编辑，供菜单 [10] 调用。
- 命令超长（> 32500 字符）时打印警告（分段续播属文档第 8 节"待实现"，本次不做）。

### 3. `miaunoteblock/config.py`
`DEFAULT_CONFIG` 新增（旧配置经 `setdefault` 自动补齐）：
```
particles_enabled  : False         # 总开关
particles_layers   : []            # 选中的虚拟层 vid 列表
particles_trail    : 24
particles_curve    : "arc"         # arc / sine / line
particles_height   : 3
particles_color    : ""            # 空 = 交由模组自动分配
particles_gradient : ""            # 空 = 不指定
particles_linger   : 40
```

### 4. `miaunoteblock/app.py`
- 配置概览区新增 `[粒子特效]` 状态行（开关 / 选中轨道数 / 参数）。
- 菜单新增 `[10] 粒子特效（音符盒跳动）`，分支调用 `edit_particle_settings(...)`。
- 生成分支：调用 `build_all_commands_virtual(..., note_events=[])` 收集事件；若 `particles_enabled` 且
  `particles_layers` 非空，则 `commands.extend(build_particle_commands(...))`，并打印生成球数。

### 5. `tests/`（新增 `test_particles.py`）
- 使用固定 `rng`（`random.Random(seed)`）保证可复现。
- 断言：选中轨道 → 恰好生成对应数量的**脉冲**命令方块（非 chain）；命令以 `/mpe noteblock chain ~~~` 开头；
  坐标偏移 = 音符盒绝对坐标 − 命令方块坐标；`delay[0]` 与 `delay[i]` 符合上面的公式；未选中轨道不生成。
- 边界：某组首音符晚于全局起点；`particles_enabled=False` 时不产出任何命令。

## 不修改 / 明确不做
- 不改动红石链本体结构、启动柱逻辑、歌词链逻辑。
- 不实现超长命令的分段续播（文档第 8 节待实现项），仅在超限时告警。

## 验证

1. `python tests/test_chain_output.py` —— 确认现有 8 组测试仍全部通过（`group_tick_notes` 改造无回归）。
2. `python tests/test_particles.py` —— 新增用例全绿。
3. 端到端：`python miau_redstone_music.py`，导入一个含打击乐/沙子的 NBS，在 [10] 里选中 1~2 条轨道并启用，
   生成 `.schem`；用文本检查命令方块内容与 `~~~` 偏移、delay 序列是否自洽，再在游戏内粘贴验证小球与音符盒同步。
