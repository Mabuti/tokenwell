"""Tokenwell's terminal launch mark.

The well frames intentionally live here as plain data so the CLI does not
need to read documentation files from an installed package.
"""

from __future__ import annotations

import os
import sys
import time
from typing import TextIO

from . import __version__


FRAME_DELAY = 0.075
DEFAULT_PORT = 4317
MARK_WIDTH = 19

GOLD_24 = "\x1b[38;2;242;169;59m"
WATER_24 = "\x1b[38;2;63;182;192m"
GOLD_256 = "\x1b[38;5;214m"
WATER_256 = "\x1b[38;5;73m"
RESET = "\x1b[0m"
CLEAR = "\x1b[2J\x1b[H"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"


FRAMES = (
    (
        "", "", "", "", "", "",
        "   ___________", "  [___________]", "   |  ~ ~ ~  |", "   |  ~ ~ ~  |", "   '---------'",
    ),
    (
        "", "", "", "", "", "",
        "   ___________", "  [___________]", "   | ~ ~ ~ ~ |", "   |  ~ ~ ~  |", "   '---------'",
    ),
    (
        "", "", "", "", "", "",
        "   ___________", "  [___________]", "   |  ~(o)~  |", "   | ~ ~ ~ ~ |", "   '---------'",
    ),
    (
        "", "", "", "", "       .-.", "      ( o )",
        "   ____'-'____", "  [___________]", "   |  ~ | ~  |", "   |  ~ ~ ~  |", "   '---------'",
    ),
    (
        "", "", "       .-.", "      ( o )", "       '-'", "      | | |",
        "   ___________", "  [___________]", "   | ~ ~ ~ ~ |", "   |  ~ ~ ~  |", "   '---------'",
    ),
    (
        "       .-.", "      ( o )", "       '-'", "      | | |", "      : : :", "      . . .",
        "   ___________", "  [___________]", "   |  ~ ~ ~  |", "   |  ~ ~ ~  |", "   '---------'",
    ),
    (
        "       .-.", "      ( o )", "       '-'", "      : : :", "      . . .", "",
        "   ___________", "  [___________]", "   |  ~ ~ ~  |", "   |  ~ ~ ~  |", "   '---------'",
    ),
    (
        "     . .-. .", "    * ( o ) *", "     ' '-' '", "      . . .", "", "",
        "   ___________", "  [___________]", "   |  ~ ~ ~  |", "   |  ~ ~ ~  |", "   '---------'",
    ),
    (
        "    .  . .  .", "   *   .o.   *", "    '  ' '  '", "", "", "",
        "   ___________", "  [___________]", "   |  ~ ~ ~  |", "   |  ~ ~ ~  |", "   '---------'",
    ),
    (
        "  .     .     .", " *      .      *", "  '     '     '", "", "", "",
        "   ___________", "  [___________]", "   |  ~ ~ ~  |", "   |  ~ ~ ~  |", "   '---------'",
    ),
    (
        " .             .", "", " '             '", "", "", "",
        "   ___________", "  [___________]", "   |  ~ ~ ~  |", "   |  ~ ~ ~  |", "   '---------'",
    ),
    (
        "", "", "", "", "", "",
        "   ___________", "  [___________]", "   |  ~ ~ ~  |", "   |  ~ ~ ~  |", "   '---------'",
    ),
    (
        "       .-.", "      ( o )", "       '-'", "      | | |", "      ' ' '", "",
        "   ___________", "  [___________]", "   |  ~ ~ ~  |", "   |  ~ ~ ~  |", "   '---------'",
    ),
)

# (frame index, row) -> (start column, end column), copied from the approved
# well-frames specification. End columns are exclusive.
GOLD_SPANS = {
    (2, 8): (7, 10),
    (3, 4): (7, 10),
    (3, 5): (6, 11),
    (3, 6): (7, 10),
    (4, 2): (7, 10),
    (4, 3): (6, 11),
    (4, 4): (7, 10),
    (5, 0): (7, 10),
    (5, 1): (6, 11),
    (5, 2): (7, 10),
    (6, 0): (7, 10),
    (6, 1): (6, 11),
    (6, 2): (7, 10),
    (7, 0): (5, 12),
    (7, 1): (4, 13),
    (7, 2): (5, 12),
    (8, 1): (3, 14),
    (9, 1): (1, 16),
    (12, 0): (7, 10),
    (12, 1): (6, 11),
    (12, 2): (7, 10),
}

BANNER = (
    " _       _                      _ _",
    "| |_ ___| |_____ _ ___ __ _____| | |",
    "|  _/ _ \\ / / -_) ' \\ V  V / -_) | |",
    " \\__\\___/_\\_\\___|_||_\\_/\\_/\\___|_|_|",
)
STATUS = (
    "  tokenwell {version}",
    "  dashboard: {url}",
)


def _supports_truecolor() -> bool:
    colorterm = os.environ.get("COLORTERM", "").lower()
    term = os.environ.get("TERM", "").lower()
    return colorterm in {"truecolor", "24bit"} or "truecolor" in term or "direct" in term


def _colors() -> tuple[str, str]:
    if _supports_truecolor():
        return GOLD_24, WATER_24
    return GOLD_256, WATER_256


def _paint_row(row: str, gold_span: tuple[int, int] | None, color: bool) -> str:
    if not color:
        return row
    gold, water = _colors()
    start, end = gold_span or (-1, -1)
    painted: list[str] = []
    active = None
    for column, character in enumerate(row):
        if character.isspace():
            if active is not None:
                painted.append(RESET)
                active = None
            painted.append(character)
            continue
        wanted = gold if start <= column < end else water
        if wanted != active:
            if active is not None:
                painted.append(RESET)
            painted.append(wanted)
            active = wanted
        painted.append(character)
    if active is not None:
        painted.append(RESET)
    return "".join(painted)


def render_frame(frame: tuple[str, ...], frame_index: int, *, color: bool = True) -> str:
    """Render one exact 11-row mark, optionally with terminal colours."""
    return "\n".join(
        _paint_row(row, GOLD_SPANS.get((frame_index, row_index)), color)
        for row_index, row in enumerate(frame)
    )


def _render_static(port: int, *, color: bool) -> str:
    status = tuple(line.format(
        version=__version__, url=f"http://127.0.0.1:{port}/"
    ) for line in STATUS)
    rows: list[str] = []
    for row_index, row in enumerate(FRAMES[-1]):
        mark = _paint_row(row, GOLD_SPANS.get((12, row_index)), color)
        banner = BANNER[row_index - 4] if 4 <= row_index < 8 else ""
        if banner:
            mark += " " * max(0, MARK_WIDTH - len(row))
            mark += _paint_row(banner, None, color)
        rows.append(mark)
    rows.extend(("", *(
        _paint_row(line, None, color) for line in status
    )))
    return "\n".join(rows)


def _enabled(stream: TextIO, *, quiet: bool) -> bool:
    if quiet or "CI" in os.environ or os.environ.get("TOKENWELL_NO_ANIMATION") == "1":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def show(port: int = DEFAULT_PORT, *, stream: TextIO | None = None, quiet: bool = False) -> bool:
    """Show the launch animation when the output is an interactive terminal.

    Returns whether anything was shown. ``NO_COLOR`` still leaves the static
    mark visible, but emits neither colour nor cursor/clear escape sequences.
    """
    stream = stream or sys.stdout
    if not _enabled(stream, quiet=quiet):
        return False
    if "NO_COLOR" in os.environ:
        stream.write(_render_static(port, color=False) + "\n")
        stream.flush()
        return True

    stream.write(HIDE_CURSOR)
    stream.flush()
    try:
        for frame_index, frame in enumerate(FRAMES):
            stream.write(CLEAR + render_frame(frame, frame_index) + "\n")
            stream.flush()
            time.sleep(FRAME_DELAY)
        stream.write(CLEAR + _render_static(port, color=True) + "\n")
        stream.flush()
    finally:
        stream.write(SHOW_CURSOR)
        stream.flush()
    return True
