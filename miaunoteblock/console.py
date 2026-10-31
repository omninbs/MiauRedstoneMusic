"""终端输入小工具：清屏、显示宽度与数值输入。"""

import os
import shutil

from .constants import Ansi


def clear_screen() -> None:
    os.system('cls' if os.name == 'nt' else 'clear')


def terminal_columns(fallback: int = 100) -> int:
    try:
        return shutil.get_terminal_size((fallback, 24)).columns
    except OSError:
        return fallback


def ask_number(label: str, current, cast, minimum=None, maximum=None,
               indent: str = "", suffix: str = ""):
    """读一个数值，回车（或格式错误）时保留原值。

    cast 为 int / float；minimum / maximum 用于夹紧结果。
    """
    raw = input(Ansi.prompt(f"{indent}{label} ({current}{suffix}，回车不改): ")).strip()
    if not raw:
        return current
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        print(Ansi.error(f"{label} 格式不对，未修改"))
        return current
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value
