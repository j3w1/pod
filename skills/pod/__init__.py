"""Pod: bounded local policy helpers for an in-session Orca coordinator."""

from pathlib import Path
import re


def _version() -> str:
    """The repository VERSION, which every installed copy carries beside this file."""
    try:
        text = Path(__file__).with_name("VERSION").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return "unknown"
    return text if re.fullmatch(r"\d+\.\d+\.\d+", text) else "unknown"


__version__ = _version()
