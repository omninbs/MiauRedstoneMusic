<div align="center">

<img src="https://raw.githubusercontent.com/omninbs/MiauRedstoneMusic/main/assets/miauredstonemusic-logo.svg" alt="MiauRedstoneMusic logo" width="160" />

<h1>MiauRedstoneMusic</h1>

<p><b>Turn NBS songs into playable Minecraft redstone music.</b></p>
<p>Interactive Python generator that converts <code>.nbs</code> files into <code>.schem</code> note-block structures for Minecraft Java 1.21.11.</p>

<p>
  <a href="Readme_CN.md"><b>中文文档</b></a>
</p>

<p>
  <img src="https://img.shields.io/badge/Minecraft%20Java-1.21.11-62B47A?style=for-the-badge&logo=minecraft&logoColor=white" alt="Minecraft Java 1.21.11" />
  <img src="https://img.shields.io/badge/Python-3.x-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.x" />
  <img src="https://img.shields.io/badge/Output-.schem-7B61FF?style=for-the-badge" alt=".schem output" />
  <img src="https://img.shields.io/badge/License-Apache%202.0-D22128?style=for-the-badge&logo=apache&logoColor=white" alt="Apache License 2.0" />
  <img src="https://img.shields.io/badge/Status-Experimental-F2C94C?style=for-the-badge" alt="Experimental status" />
</p>

<p>
  <img src="https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square" alt="PRs welcome" />
  <img src="https://img.shields.io/badge/AI--assisted-~70%25-8A63D2?style=flat-square" alt="AI-assisted" />
</p>

</div>

---

## Table of Contents

- [Overview](#overview)
- [At a Glance](#at-a-glance)
- [Features](#features)
- [Layouts](#layouts)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [Lyric Subtitles (per-character)](#lyric-subtitles-mpe-text-chain-per-character)
- [Project Structure](#project-structure)
- [Brand Assets](#brand-assets)
- [Notes and Limitations](#notes-and-limitations)
- [Contributing](#contributing)
- [License](#license)
- [AI-Generated Code Notice](#ai-generated-code-notice)
- [Acknowledgements](#acknowledgements)

---

## Overview

**MiauRedstoneMusic** is an interactive Python utility that reads `.nbs` music files, converts their note and instrument data into Minecraft redstone note-block chains, and saves the result as a `.schem` structure file for Minecraft Java Edition.

It is built around the Minecraft Java **1.21.11** workflow and can optionally generate a synchronized **lyric command-block track** powered by the MiauParticleEffects Fabric mod.

For source songs, the online NBS site [webnbs.com](https://webnbs.com) can be used. This repository does not claim that the online service runs the same conversion pipeline as this local tool.

---

## At a Glance

| | |
| :-- | :-- |
| **Input** | Note Block Studio `.nbs` files |
| **Output** | `.schem` structure (`mcschematic.Version.JE_1_21`) |
| **Interface** | Interactive terminal menu |
| **Target** | Minecraft Java Edition 1.21.11 |
| **Optional** | Particle lyrics and bouncing-ball effects via MiauParticleEffects |
| **Config** | Saved per song path under `configs/` in the install directory (one JSON per path); re-opening the same song restores it |

---

## Features

| Capability | What it gives you |
| :-- | :-- |
| **NBS conversion** | Reads notes and instruments, then emits a playable redstone chain. |
| **Track grouping** | Merges two or three virtual layers into a single generated group. |
| **Smart grouping** | Automatic grouping by instrument category, or maximum merging. |
| **Instrument handling** | Detects instruments and reorders tracks, with percussion placed later. |
| **Stereo support** | Mono tracks plus selected stereo tracks, duplicated and distributed symmetrically. |
| **Rich layouts** | Flat, circle, square, semicircle, semisquare, nested-circle, nested-square, plus curve volume with a minecart follower. |
| **Master group** | Choose a core group for non-flat layouts. |
| **Staircase modes** | Per-group default or descending staircase generation. |
| **Timing engine** | Repeater and support blocks represent timing gaps and signal paths. |
| **Repeater modes** | Uniform: one repeater per blank tick, easiest to read. Compact: four blank ticks folded into one `delay=4` repeater, shorter structure (default). Both modes share the same timing; lyrics and particle effects follow the actual layout. |
| **Modern instruments** | Maps NBS instruments to note-block under-blocks, including copper and waxed copper stages. |
| **Lyric subtitles** | Optional single `/mpe text chain` command (one impulse command block) driving **per-character** particle subtitles: long-note sentence breaks plus a `chars=` schedule per sentence. |
| **Particle effects** | Optional `/mpe noteblock chain` bouncing-ball command block per group. |

---

## Layouts

MiauRedstoneMusic can arrange each group with a different footprint, so the redstone structure matches the shape of your song instead of a single long line.

| Layout | Description |
| :-- | :-- |
| **Flat** | Straight rows, the densest and simplest arrangement. |
| **Circle** | Groups distributed around a ring. |
| **Square** | Groups distributed around a square. |
| **Semicircle** | Half-ring arrangement. |
| **Semisquare** | Half-square arrangement. |
| **Nested circle** | Concentric rings that reuse space efficiently. |
| **Nested square** | Concentric squares for compact builds. |
| **Curve volume + minecart** | Encodes each note's volume as its distance from the center axis, keeps the stereo image symmetric, and adds an invisible minecart that follows playback. |

### Curve volume layout

Note blocks always play at a fixed volume with a 48-block linear falloff, so the curve layout
turns volume into geometry:

```
volume ≈ (1 − lateral distance from the center axis / 48)
```

- Every group becomes a **lane** (stacked along Y). Each side of a lane has its own repeater backbone that advances 2 blocks per song tick.
- A tap block on the backbone (wool strongly powered by a repeater) sends a **branch** along ±Z; the note block at the end of the branch is the sound source.
- Each branch carries a **fixed number of repeaters** (0–3, chosen automatically). They refresh the 15-block signal limit and delay the whole side by exactly `2n` game ticks — constant for the entire song, so events never drift.
- Stereo copies are strictly mirrored; centered notes alternate sides so two very different volumes do not share one branch.
- Each note block sits next to the player's corridor at `z = 0.5`, which stays empty. A command-block chain summons an invisible minecart, mounts the nearest player and teleports it once per tick so you always stay on the center axis. The uniform repeater mode (menu `[7]`) gives the smoothest cart motion; the compact mode saves blocks but jumps further during rests.
- After saving, the tool re-loads the `.schem`, simulates redstone tick by tick and prints `Fnbs(t) == Fmc(t)` stats. You can also run `python tests/test_curve_volume.py`.
- Lyrics and particle chains are skipped in this layout because they rely on the linear coordinates.

---

## Requirements

**Core**

- Python 3.x
- [`pynbs`](https://pypi.org/) — NBS parsing
- [`mcschematic`](https://pypi.org/) — structure construction and saving

**In-game**

- Minecraft Java Edition 1.21.11

**Optional lyric display**

- MiauParticleEffects for Minecraft Java 1.21.11 + Fabric
- Fabric Loader 0.16.0 or newer
- The matching Fabric API
- Java 21
- The mod installed on both client and server; a dedicated server needs it to detect note-block events and broadcast them

<sub>No dependency lockfile or packaged installer is currently included. Install dependencies according to your Python environment and package sources.</sub>

---

## Installation

**1 · Clone or download the repository**

```powershell
git clone https://github.com/omninbs/MiauRedstoneMusic.git
cd MiauRedstoneMusic
```

**2 · Create and activate a virtual environment (optional)**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**3 · Install dependencies**

```powershell
python -m pip install pynbs mcschematic
```

**4 · Prepare an input song**

Keep the `.nbs` file you want to convert accessible on your machine.

<sub>The exact package availability and compatible versions may depend on your environment. This README intentionally does not claim a tested package matrix.</sub>

---

## Usage

**Windows launcher**

```powershell
pm.bat
```

**Direct entry point**

```powershell
python miau_redstone_music.py
```

**Module form**

```powershell
python -m miaunoteblock
```

### Interactive Flow

| Step | Action |
| :--: | :-- |
| 1 | Enter the path to an `.nbs` file (default prompt: `song.nbs`). |
| 2 | Choose whether to vertically compress and reorder tracks by instrument. |
| 3 | Review the detected song metadata and virtual layers. |
| 4 | Configure grouping, layout, stereo tracks, repeater timing, staircase modes, lyric subtitles, and particle effects (menus `[1]`–`[7]`). |
| 5 | Press Enter from the menu to generate the structure. |
| 6 | Enter the output path, or accept `./redstone_music.schem`. |

Configuration is **saved per song**: the song's absolute path maps to one fixed file
(`configs/<song-name>_<path-hash>.json`) that is rewritten after every change, so opening the
same path again restores the previous layout, lyric and particle settings. Different paths keep
separate configurations.

The generated file can be loaded with a compatible Minecraft structure workflow, such as a schematic-capable editor or server tool. The generator reports the normalized block bounds and suggests the resulting origin after loading; the exact paste workflow depends on your setup.

---

## Lyric Subtitles (`/mpe text chain`, per-character)

Generate a synchronous lyric subtitle track that renders through particles. A whole song needs only **one impulse command block plus one command** — no repeater lyric chain inside the structure.

Lyrics are split on **long notes**: when a character's `duration` is **strictly greater** than the break threshold (default 4 ticks), that character ends the current sentence and the next character starts a new one. On top of that there is a **per-sentence character limit** (default 18; reaching it also breaks, `0` = unlimited). Each sentence becomes one segment carrying a `chars=` schedule of "appear at / stay for" per character, and the mod reveals them one at a time — a finished sentence is never erased by the next one, it simply fades out after its last character.

Lyrics can also be shown **earlier** than the music: by default 4 song ticks (= 8 game ticks, ~0.4 s) ahead, which compensates for the lyrics feeling slightly late. Adjustable in `[2]`.

1. Import a lyric JSON exported by the lyric editor (menu `[5] → [1]`); importing a lyric file is what enables the feature, and the same menu can clear it. **One record per character** (spaces count as characters) with `duration ≥ 1` is required; otherwise the import fails and lists the offending records.
2. Adjust settings in `[5] → [3]`: `[1]` color/scale, `[2]` per-character enter/exit timing plus the lead, `[3]`/`[4]` enter/exit animation (chosen by number), `[5]` position (lead distance / height / side offset / random range / follow / command-block layer), `[6]` break threshold / punctuation breaking / per-sentence character limit.
3. Generate the `.schem` file.

Generated commands look like:

```text
/mpe text chain ~~~ 78,7,-1,186,0,起来;chars=0:8,8:24|86,7,-1,218,0,不愿;chars=0:8,8:12 color=white scale=10 enter=0 exit=3 towards=-90,0,0 fade=both force=true in=scale:enlarge
```

- The anchor is always `~~~` (the command block's own position); per-segment `x,y,z` are offsets from it, so copying or moving the structure needs no data changes.
- `start` is an absolute tick offset from the moment the command block runs (20 ticks = 1 second). The command block's own 1-tick execution delay is already included, the value is aligned with the music chain's timing origin, and the lead is subtracted (never negative: the first sentence is clamped to tick 0 at worst).
- The segment `duration` is always `0`: in per-character mode the mod derives the sentence lifetime from `chars=`.
- `chars=char_start:char_duration,...` must have exactly one entry per **visible character** (`§x` / `&x` color codes do not count; a space, written `_`, does). `char_start` is relative to the segment `start` (first character is 0) and `char_duration` is how long the character stays before it starts exiting. One song tick equals two game ticks, so every value in the schedule is twice the song-tick value; the lead converts the same way.
- **Breaking**: only `duration > threshold` breaks on duration (`==` does not); the long note stays in the current sentence and plays in full before the whole sentence exits. Larger thresholds give longer sentences (a huge value yields a single sentence for the song). The character limit (default 18, `0` = unlimited) **stacks** with the long-note rule, so a sentence may break earlier on duration. `[6]` can additionally break on punctuation (off by default).
- **Forbidden animations** in per-character mode: `typewriter` / `wipe` / `spacing` / `scatter` (they conflict with the schedule and the mod rejects them). They are not offered in the menu and are stripped with a warning. Recommended: `enter=0 exit=3 fade=both in=scale:enlarge`.
- **Lead distance**: the text X coordinate equals the currently powered position plus `lead distance` (default 16 blocks). The structure always extends along **+X**, so the text stays ahead of the action instead of sticking to the camera. The powered position is looked up from the **real chain layout**: with merged repeaters the chain is shorter and the text follows that compressed layout, so both repeater modes stay aligned.
- **Random jitter**: optional ± range for X/Y/Z (`[5] → random range`, 0 = off), drawn per sentence in 0.5-block steps.
- Spaces are written as `_`; `|`, `;`, and `,` inside the text are replaced with full-width characters so the command cannot be split incorrectly.
- Commands longer than roughly 32000 characters are split into several impulse command blocks (split on sentence boundaries, never inside a sentence), each with a unique `id=`.
- The structure always extends along +X, so `towards=` is fixed at `-90,0,0` (the text faces away from the extension direction, towards the audience on the −X side).

The bouncing-ball effect uses a separate `/mpe noteblock chain` command; pick the participating tracks in menu `[6]`, with detailed parameters in its advanced submenu.

MiauParticleEffects provides the `/mpe` entry point and renders text as particle-based display content. See [mpe_docs.md](mpe_docs.md) for the documented mod environment and command options.

---

## Project Structure

| Path | Purpose |
| :-- | :-- |
| `miau_redstone_music.py` | Interactive entry point. |
| `pm.bat` | Windows launcher for the entry point. |
| `miaunoteblock/` | Python package holding the generator logic. `app.py` contains the interactive flow and `main()`; `constants.py`, `config.py`, `console.py`, `lyrics.py`, `particles.py`, `instruments.py`, `layout.py`, `generation.py`, `volume.py`, `curve.py`, and `simulate.py` hold the reusable modules (the last three implement the curve volume layout, its minecart follower, and the redstone simulator used for verification). |
| `mpe_docs.md` | MiauParticleEffects command and environment notes. |
| `configs/` | Per-song configuration files written at runtime (one per song path, git-ignored). |
| `Readme_CN.md` | Chinese documentation. |
| `LICENSE` | Apache License 2.0. |

---

## Brand Assets

The logo in the header is a **placeholder** and should be replaced with the final GitHub raw or CDN asset URL before publishing.

**Recommended logo specification**

| Property | Recommendation |
| :-- | :-- |
| **Format** | SVG (preferred) or PNG |
| **Size** | 512 × 512 px |
| **Background** | Transparent |
| **Style** | Simple, recognizable at small sizes, readable on both light and dark GitHub themes |
| **Display width** | 140–180 px in the README header |

A flat, single-object mark with a limited palette works best: it stays legible as a favicon, a social preview, and an in-README badge.

---

## Notes and Limitations

> **Notes**
> - The input file must contain notes; empty NBS files cannot produce a music chain.
> - Lyric rendering requires MiauParticleEffects. Without the mod, the generated lyric commands will not provide their intended display.
> - Minecraft Java, Fabric, Fabric API, Java, and MiauParticleEffects versions should match the target environment, especially for 1.21.11.

> **Limitations**
> - Large songs, dense chords, small particle density, or many simultaneous effects may require substantial Minecraft performance headroom.
> - The tool currently generates `.schem` structures only; it does not export a standalone command text file or another structure format.
> - Configuration is saved automatically per **song file path** under `configs/` in the install directory (one JSON per path); delete that JSON to reset a song back to defaults.
> - The repository does not currently provide official release binaries, screenshots, or a verified download URL.

---

## Contributing

Contributions are welcome. Before opening a pull request:

- Explain the user-visible behavior you changed.
- Keep conversion behavior and Minecraft version assumptions explicit.
- Preserve the existing interactive workflow unless the change requires otherwise.
- Include a reproducible example when changing NBS parsing, timing, layout, instrument mapping, or lyric commands.
- Do not include copyrighted music files, secrets, or generated build artifacts in commits.

---

## License

Released under the **Apache License 2.0**. See [LICENSE](LICENSE) for the full text.

You are free to use, modify, and distribute this project in compliance with the license terms.

---

## AI-Generated Code Notice

> Approximately **70%** of the code was generated with AI assistance. Perfect quality cannot be guaranteed, although the AI is impressive. Some code comments may be imperfect, and the codebase may contain unrelated AI-generated notes. When reading or reviewing the code, you may want to use AI assistance as an additional aid.

---

## Acknowledgements

| Project | Role |
| :-- | :-- |
| [`pynbs`](https://pypi.org/) | NBS file parsing |
| [`mcschematic`](https://pypi.org/) | Schematic construction and saving |
| MiauParticleEffects | Optional lyric command-block workflow |

<sub>The links above point to package indexes rather than claiming a specific project page or release artifact.</sub>

<div align="center">
<br />
<sub>Built for the Minecraft Java redstone music community.</sub>
</div>
