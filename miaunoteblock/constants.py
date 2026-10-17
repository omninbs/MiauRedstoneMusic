"""终端颜色与全局常量定义。"""

from typing import Dict

class Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"

    @staticmethod
    def colorize(text: str, color: str = "", bold: bool = False) -> str:
        return f"{Ansi.BOLD if bold else ''}{color}{text}{Ansi.RESET}"

    @staticmethod
    def title(text: str) -> str:
        return Ansi.colorize(text, Ansi.CYAN, bold=True)

    @staticmethod
    def success(text: str) -> str:
        return Ansi.colorize(f"✓ {text}", Ansi.GREEN)

    @staticmethod
    def error(text: str) -> str:
        return Ansi.colorize(f"✗ {text}", Ansi.RED)

    @staticmethod
    def prompt(text: str) -> str:
        return Ansi.colorize(text, Ansi.YELLOW)

    @staticmethod
    def info(text: str) -> str:
        return Ansi.colorize(text, Ansi.BLUE)

    @staticmethod
    def dim(text: str) -> str:
        return Ansi.colorize(text, Ansi.GRAY)


# ── 配置管理 ─────────────────────────────────────────────────

INSTRUMENT_BLOCK_MAP: Dict[int, str] = {
    0: "minecraft:dirt",
    1: "minecraft:oak_planks",
    2: "minecraft:stone",
    3: "minecraft:sand",
    4: "minecraft:glass",
    5: "minecraft:white_wool",
    6: "minecraft:clay",
    7: "minecraft:gold_block",
    8: "minecraft:packed_ice",
    9: "minecraft:bone_block",
    10: "minecraft:iron_block",
    11: "minecraft:soul_sand",
    12: "minecraft:pumpkin",
    13: "minecraft:emerald_block",
    14: "minecraft:hay_block",
    15: "minecraft:glowstone",
    16: "minecraft:copper_block",
    17: "minecraft:exposed_copper",
    18: "minecraft:weathered_copper",
    19: "minecraft:oxidized_copper",
    20: "minecraft:waxed_copper_block",
    21: "minecraft:waxed_exposed_copper",
    22: "minecraft:waxed_weathered_copper",
    23: "minecraft:waxed_oxidized_copper"
}

INSTRUMENT_NAMES: Dict[int, str] = {
    0: "钢琴",
    1: "低音提琴",
    2: "底鼓",
    3: "小军鼓",
    4: "击鼓沿",
    5: "吉他",
    6: "长笛",
    7: "钟琴",
    8: "管钟",
    9: "木琴",
    10: "铁木琴",
    11: "牛铃",
    12: "迪吉里杜管",
    13: "芯片",
    14: "班卓琴",
    15: "电钢琴",
    16: "铜号角",
    17: "锈蚀铜号角",
    18: "风化铜号角",
    19: "氧化铜号角",
    20: "蜡封铜号角",
    21: "蜡封锈蚀铜号角",
    22: "蜡封风化铜号角",
    23: "蜡封氧化铜号角"
}

DRUM_INSTRUMENTS = {2, 3, 4}

DIR_OFFSET = {
    "east":  (1, 0, 0),
    "west":  (-1, 0, 0),
    "south": (0, 0, 1),
    "north": (0, 0, -1)
}
OPPOSITE_FACING = {
    "east": "west",
    "west": "east",
    "south": "north",
    "north": "south"
}
SIDE_AXIS = {
    "east":  "z",
    "west":  "z",
    "south": "x",
    "north": "x"
}

# 歌词文字的朝向（/mpe text towards=yaw,pitch,roll）：
# 文字需正对轨道起点一侧（即朝向「轨道延伸方向的反方向」），
# 使站在起点处沿轨道方向看过去能正常阅读。
# 已知：轨道朝 north 延伸时，towards=-90 让文字正面朝 south（起点侧）。
# 由此推算其余方向（每转一个方向差 90°）：
#   north -> -90，south -> 90，east -> 0，west -> 180
TEXT_TOWARDS_YAW = {
    "north": -90,
    "south": 90,
    "east": 0,
    "west": 180
}

SUPPORT_BLOCK = "minecraft:white_wool"
CONNECTION_BLOCK = "minecraft:white_wool"
FALLING_BLOCK_SUPPORT = "minecraft:white_wool"

# ── 歌词处理 ─────────────────────────────────────────────────

