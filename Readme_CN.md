<div align="center">

<img src="https://raw.githubusercontent.com/omninbs/MiauRedstoneMusic/main/assets/miauredstonemusic-logo.svg" alt="MiauRedstoneMusic Logo" width="160" />

<h1>MiauRedstoneMusic</h1>

<p><b>把 NBS 音乐转换为可运行的 Minecraft 红石音乐。</b></p>
<p>交互式 Python 生成器，将 <code>.nbs</code> 文件转换为适用于 Minecraft Java 1.21.11 的 <code>.schem</code> 音符盒结构。</p>

<p>
  <a href="README.md"><b>English README</b></a>
</p>

<p>
  <img src="https://img.shields.io/badge/Minecraft%20Java-1.21.11-62B47A?style=for-the-badge&logo=minecraft&logoColor=white" alt="Minecraft Java 1.21.11" />
  <img src="https://img.shields.io/badge/Python-3.x-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.x" />
  <img src="https://img.shields.io/badge/输出-.schem-7B61FF?style=for-the-badge" alt=".schem 输出" />
  <img src="https://img.shields.io/badge/许可证-Apache%202.0-D22128?style=for-the-badge&logo=apache&logoColor=white" alt="Apache License 2.0" />
  <img src="https://img.shields.io/badge/状态-实验性-F2C94C?style=for-the-badge" alt="实验性状态" />
</p>

<p>
  <img src="https://img.shields.io/badge/PR-欢迎-brightgreen?style=flat-square" alt="欢迎 PR" />
  <img src="https://img.shields.io/badge/AI%20协助-约%2070%25-8A63D2?style=flat-square" alt="AI 协助" />
</p>

</div>

---

## 目录

- [项目简介](#项目简介)
- [快速了解](#快速了解)
- [功能特性](#功能特性)
- [排版布局](#排版布局)
- [环境要求](#环境要求)
- [安装](#安装)
- [使用方法](#使用方法)
- [歌词命令方块](#歌词命令方块)
- [项目结构](#项目结构)
- [品牌资源](#品牌资源)
- [注意事项与限制](#注意事项与限制)
- [参与贡献](#参与贡献)
- [许可证](#许可证)
- [AI 生成代码声明](#ai-生成代码声明)
- [致谢](#致谢)

---

## 项目简介

**MiauRedstoneMusic** 是一个交互式 Python 工具。它读取 `.nbs` 音乐文件，将其中的音符与乐器数据转换为 Minecraft 红石音符盒链，并保存为 `.schem` 结构文件，供 Minecraft Java Edition 使用。

生成器围绕 Minecraft Java **1.21.11** 工作流设计，并可选生成与 **MiauParticleEffects** Fabric 模组同步的**歌词命令方块轨道**。

源歌曲可使用在线 NBS 站点 [webnbs.com](https://webnbs.com)。本仓库不声明该在线服务与本地工具使用相同的转换流程。

---

## 快速了解

| | |
| :-- | :-- |
| **输入** | Note Block Studio `.nbs` 文件 |
| **输出** | `.schem` 结构（`mcschematic.Version.JE_1_21`） |
| **交互方式** | 终端交互式菜单 |
| **目标环境** | Minecraft Java Edition 1.21.11 |
| **可选功能** | 通过 MiauParticleEffects 显示粒子歌词 |
| **配置文件** | 工作目录下的 `nbs_chain_config.json` |

---

## 功能特性

| 能力 | 说明 |
| :-- | :-- |
| **NBS 转换** | 读取音符与乐器数据，生成可运行的红石链。 |
| **音轨分组** | 将两个或三个虚拟音轨合并为一个生成组。 |
| **自动分组** | 支持按乐器类别自动分组，或最大化合并音轨。 |
| **乐器处理** | 识别乐器并重排音轨，打击乐排在后面。 |
| **立体声** | 支持单声道音轨与指定立体声音轨，立体声会复制并对称排布。 |
| **丰富排版** | 平铺、圆形、方形、半圆、半方形、嵌套圆、嵌套方。 |
| **核心组** | 非平铺排版可选择核心组。 |
| **阶梯模式** | 支持按组选择默认模式或向下阶梯模式。 |
| **计时引擎** | 使用中继器与辅助方块表达时间间隔与信号路径。 |
| **现代乐器** | 将 NBS 乐器映射为音符盒下方方块，包含铜块及蜡封铜块阶段。 |
| **红石灯模式** | 可选用红石灯替代默认的白色羊毛辅助模式。 |
| **歌词轨道** | 可选生成调用 MiauParticleEffects `/mpe text` 的命令方块链。 |

---

## 排版布局

MiauRedstoneMusic 可以为每个分组设置不同的排布形态，让红石结构贴合歌曲本身，而不是排成一条长线。

| 排版 | 说明 |
| :-- | :-- |
| **平铺** | 直线排列，最紧凑也最简单。 |
| **圆形** | 分组沿圆环分布。 |
| **方形** | 分组沿方形分布。 |
| **半圆** | 半圆环排列。 |
| **半方形** | 半方形排列。 |
| **嵌套圆** | 同心圆环，空间利用率高。 |
| **嵌套方** | 同心方形，结构紧凑。 |

---

## 环境要求

**核心依赖**

- Python 3.x
- [`pynbs`](https://pypi.org/) —— NBS 解析
- [`mcschematic`](https://pypi.org/) —— 结构构建与保存

**游戏环境**

- Minecraft Java Edition 1.21.11

**可选歌词显示**

- Minecraft Java 1.21.11 + Fabric 的 MiauParticleEffects
- Fabric Loader 0.16.0 或更高版本
- 对应 1.21.11 的 Fabric API
- Java 21
- 客户端与服务端都需安装本模组；专用服务器需要安装本模组才能检测音符盒事件并广播

<sub>当前仓库没有提供依赖锁定文件或打包安装器，请根据你的 Python 环境和包源安装依赖。</sub>

---

## 安装

**1 · 克隆或下载仓库**

```powershell
git clone https://github.com/omninbs/MiauRedstoneMusic.git
cd MiauRedstoneMusic
```

**2 · 创建并激活虚拟环境（可选）**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**3 · 安装依赖**

```powershell
python -m pip install pynbs mcschematic
```

**4 · 准备输入文件**

将要转换的 `.nbs` 文件放在本地可访问的位置。

<sub>具体可用的包版本可能取决于你的环境。本 README 不声明已经验证过的完整版本矩阵。</sub>

---

## 使用方法

**Windows 启动脚本**

```powershell
pm.bat
```

**直接启动入口**

```powershell
python miau_redstone_music.py
```

**模块方式启动**

```powershell
python -m miaunoteblock
```

### 交互流程

| 步骤 | 操作 |
| :--: | :-- |
| 1 | 输入 `.nbs` 文件路径（默认提示值：`song.nbs`）。 |
| 2 | 选择是否进行垂直压缩并按乐器重排音轨。 |
| 3 | 查看识别出的歌曲元数据与虚拟音轨。 |
| 4 | 配置方向、分组、排版、立体声音轨、中继器计时、阶梯模式和可选歌词。 |
| 5 | 在菜单中直接按回车开始生成结构。 |
| 6 | 输入输出路径，或接受默认路径 `./redstone_music.schem`。 |

生成的文件可以使用兼容 Minecraft 结构文件的编辑器或服务器工具加载。生成器会报告归一化后的方块范围，并提示加载后生成的原点；具体粘贴流程取决于你的 Minecraft 环境。

---

## 歌词命令方块

生成随音乐同步、以粒子渲染的歌词轨道。

1. 导入歌词编辑器导出的歌词 JSON（菜单 `[9]`）；导入歌词即代表启用歌词功能，同一菜单也可以清空。
2. 配置颜色、缩放、显示时长、入场时间、退场时间、垂直偏移、侧向偏移，以及命令方块链的高度（菜单 `[8]`）。
3. 可在导入时启用流式歌词：间隔在设定秒数内的连续文字会累积成一行逐渐变长（`A -> AB -> ABC`），而不是互相顶掉。
4. 生成 `.schem` 文件。

每个命令方块会调用类似以下格式的命令：

```text
/mpe text "歌词段落" ~ ~ ~ color=white scale=10.0 force=true duration=20 enter=5 exit=5 towards=-90,0,0
```

显示坐标以相对各自命令方块位置的 `~~~` 偏移写入，因此结构无论被粘贴到哪里，文字位置都保持正确；`scale=` 可自定义（默认 `10.0`），并始终带上 `force=true`。

`towards=` 会根据轨道方向自动推导，使文字始终正对轨道起点一侧：north `-90`、south `90`、east `0`、west `180`。

启用流式歌词时，每个显示点会先清除上一段文字，再由紧贴事件格下方的链式命令方块显示累积后的整串文字。

MiauParticleEffects 提供 `/mpe text` 入口，并将文字渲染为基于粒子的显示内容。模组环境和命令选项请参阅 [mpe_docs.md](mpe_docs.md)。

---

## 项目结构

| 路径 | 说明 |
| :-- | :-- |
| `miau_redstone_music.py` | 交互式入口。 |
| `pm.bat` | 用于启动入口的 Windows 脚本。 |
| `miaunoteblock/` | 存放生成器逻辑的 Python 包。`app.py` 包含交互流程与入口 `main()`；`constants.py`、`config.py`、`lyrics.py`、`instruments.py`、`layout.py`、`generation.py` 为可复用模块。 |
| `mpe_docs.md` | MiauParticleEffects 的命令与环境说明。 |
| `README.md` | 英文文档。 |
| `LICENSE` | Apache License 2.0。 |

---

## 品牌资源

页首 Logo 为**占位地址**，发布前应替换为最终的 GitHub raw 或 CDN 资源地址。

**推荐 Logo 规格**

| 属性 | 建议 |
| :-- | :-- |
| **格式** | SVG（优先）或 PNG |
| **尺寸** | 512 × 512 px |
| **背景** | 透明 |
| **风格** | 简洁、在小尺寸下仍可识别、在 GitHub 明暗主题下都清晰 |
| **页首显示宽度** | 140–180 px |

建议使用扁平化、单一主体的图形搭配有限配色：这样它既能作为 favicon 与社交预览图，也能在 README 页首保持清晰。

---

## 注意事项与限制

> **注意事项**
> - 输入文件必须包含音符；没有音符的 NBS 文件无法生成红石音乐链。
> - 歌词显示依赖 MiauParticleEffects。未安装该模组时，生成的歌词命令无法实现预期显示。
> - Minecraft Java、Fabric、Fabric API、Java 和 MiauParticleEffects 的版本应与目标环境匹配，尤其是 1.21.11 环境。

> **限制**
> - 较长的歌曲、密集和弦、较小的粒子间距或大量同时显示的特效，可能需要较多 Minecraft 性能余量。
> - 当前工具只生成 `.schem` 结构，不导出独立的命令文本文件或其他结构格式。
> - 工具是交互式程序，并将配置写入当前工作目录；仓库没有定义全局配置路径。
> - 当前仓库没有提供官方发布二进制包、截图或已验证的下载链接。

---

## 参与贡献

欢迎提交贡献。创建 Pull Request 前请注意：

- 说明变更对用户可见的行为。
- 明确保留转换行为和 Minecraft 版本假设。
- 除非变更确实需要，否则请保持现有交互流程。
- 如果修改 NBS 解析、计时、排版、乐器映射或歌词命令，请提供可复现的示例。
- 不要在提交中加入受版权保护的音乐文件、密钥或生成产物。

---

## 许可证

本项目采用 **Apache License 2.0** 授权。完整条款请参阅 [LICENSE](LICENSE)。

在遵守许可证条款的前提下，你可以自由使用、修改和分发本项目。

---

## AI 生成代码声明

> 约 **70%** 的代码由 AI 协助生成。不能保证代码质量完美，但 AI 的表现令人印象深刻。部分代码注释可能不够完善，代码库中也可能存在 AI 生成的无关备注。阅读或审查代码时，可以让 AI 作为额外的辅助工具。

---

## 致谢

| 项目 | 用途 |
| :-- | :-- |
| [`pynbs`](https://pypi.org/) | NBS 文件解析 |
| [`mcschematic`](https://pypi.org/) | 结构构建与保存 |
| MiauParticleEffects | 可选的歌词命令方块工作流 |

<sub>上面的链接指向包索引，并不代表本项目已经提供了特定的上游项目页面或发布文件。</sub>

<div align="center">
<br />
<sub>为 Minecraft Java 红石音乐社区而构建。</sub>
</div>
