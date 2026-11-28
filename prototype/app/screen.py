"""Printing text other people wrote, safely (`operator-screen-v1`).

Terminals act on some invisible characters. ESC (`\\x1b`) starts "escape codes" that clear
the screen, move the cursor, hide or recolour text, rename the window and, on some
terminals, write to the clipboard; the 8-bit byte `\\x9b` does the same on others; a
right-to-left mark makes text read backwards; a line break lets one value pretend to be a
line of the tool's own output. Anything a rig, a submitter, a coordinator, a website, an
advisory database or someone else's ledger copy wrote goes through `say` (one value) or
`say_lines` (a whole text view) before it reaches a terminal. JSON output doesn't need it:
JSON writes those characters as `\\u001b` and so on.

Standard library only, so every tool can use it (the site and dependency checkers run
before or without the coordinator's dependencies).
"""
from __future__ import annotations

from typing import Any, Iterable

SCREEN_VERSION = "operator-screen-v1"
# Longest text from the coordinator the worker prints in one message.
MAX_SAY_CHARS = 200
# Longest line a text view prints (`say_lines`). Views cut long fields themselves; this is
# only a backstop.
MAX_SCREEN_LINE_CHARS = 4000


def say(value: Any, limit: int = MAX_SAY_CHARS) -> str:
    """`value` as text that is safe to print to a terminal: control characters (including
    ESC, which starts colour and cursor codes), invisible and right-to-left marks, and
    line breaks become `?`; anything past `limit` characters is cut off."""
    text = value if isinstance(value, str) else repr(value)
    cut = len(text) > limit
    text = "".join(ch if ch.isprintable() else "?" for ch in text[:limit])
    return text + "..." if cut else text


def say_lines(lines: Iterable[Any], limit: int = MAX_SCREEN_LINE_CHARS) -> str:
    """`lines` joined for a terminal, each one through `say`, so a value with ESC codes or
    a line break in it can't act on the screen or fake a line of its own. Ends with a line
    break."""
    return "".join(say(line, limit) + "\n" for line in lines)
