"""Terminal-safe text and small capabilities shared by the TUI and installer."""

from __future__ import annotations

from dataclasses import dataclass
import os
import unicodedata


@dataclass(frozen=True)
class Capabilities:
    ascii_only: bool
    color: bool


def capabilities(env: dict[str, str] | None = None, *, has_colors: bool = False) -> Capabilities:
    values = os.environ if env is None else env
    locale = next((values[key] for key in ("LC_ALL", "LC_CTYPE", "LANG") if values.get(key)), "C")
    base = locale.split("@", 1)[0].upper()
    term = values.get("TERM", "").lower()
    ascii_only = base in ("C", "POSIX") or term == "dumb" or term == "linux" or term.startswith("vt")
    return Capabilities(ascii_only=ascii_only,
                        color=bool(has_colors and "NO_COLOR" not in values and term != "dumb"))


def clean(value: object) -> str:
    """Strip controls, ESC, DEL, bidi formatting and every other format character."""
    text = str(value) if value is not None else ""
    return "".join(character for character in text
                   if unicodedata.category(character) not in ("Cc", "Cf", "Cs"))


def safe_text(value: object, caps: Capabilities) -> str:
    text = clean(value)
    if not caps.ascii_only:
        return text
    text = (text.replace("—", "-").replace("–", "-").replace("·", "|")
            .replace("↑", "Up").replace("↓", "Down"))
    return unicodedata.normalize("NFKD", text).encode("ascii", "replace").decode("ascii")


def glyph(caps: Capabilities, name: str) -> str:
    symbols = {
        "focus": (">", "▸"), "preferred": ("*", "★"),
        "available": ("+", "✓"), "disabled": ("x", "⊘"),
        "dash": ("-", "—"), "dot": (" | ", " · "),
        "rule": ("-", "─"), "down": ("v", "↓"),
    }
    pair = symbols[name]
    return pair[0] if caps.ascii_only else pair[1]


def display_width(value: object) -> int:
    width = 0
    for character in clean(value):
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in ("F", "W") else 1
    return width


def clip(value: object, columns: int, *, ellipsis: bool = False, ascii_only: bool = False) -> str:
    if columns <= 0:
        return ""
    source = clean(value)
    if display_width(source) <= columns:
        return source
    mark = ("..." if ascii_only else "…") if ellipsis else ""
    allowance = max(0, columns - display_width(mark))
    out = ""
    for character in source:
        if display_width(out + character) > allowance:
            break
        out += character
    return out + mark


def pad(value: object, columns: int, *, ascii_only: bool = False) -> str:
    text = clip(value, columns, ellipsis=True, ascii_only=ascii_only)
    return text + " " * max(0, columns - display_width(text))


def elide_middle(value: object, columns: int, *, ascii_only: bool = False) -> str:
    """Keep both ends of a path visible without consuming another screen row."""
    source = clean(value)
    if display_width(source) <= columns:
        return source
    mark = "..." if ascii_only else "…"
    room = columns - display_width(mark)
    if room <= 0:
        return clip(mark, columns)
    left = (room + 1) // 2
    right = room - left
    prefix = clip(source, left)
    suffix = ""
    for character in reversed(source):
        if display_width(character + suffix) > right:
            break
        suffix = character + suffix
    return prefix + mark + suffix


def wrap(value: object, columns: int) -> list[str]:
    """Greedy word wrapping by cell width, including a safe long-word fallback."""
    if columns <= 0:
        return [""]
    words = clean(value).split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        while display_width(word) > columns:
            if current:
                lines.append(current)
                current = ""
            part = clip(word, columns)
            if not part:
                part = "?"
                word = word[1:]
                lines.append(part)
                continue
            lines.append(part)
            word = word[len(part):]
        if not word:
            continue
        candidate = f"{current} {word}" if current else word
        if display_width(candidate) <= columns:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]
