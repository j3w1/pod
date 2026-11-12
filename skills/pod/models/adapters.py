"""What the installed agent programs say they offer, read from their own local catalogs.

Only Codex keeps one. It is read defensively and never required: a missing, oversized or
unreadable catalog reads as "no catalog", and nothing here makes a route routable.
"""

import json
import os
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024


def _codex_file() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home and Path(home).is_absolute() else Path.home() / ".codex") / "models_cache.json"


def codex() -> dict | None:
    """{model id: {"efforts": [...], "name": str, "context_tokens": int | None}}, or None with no catalog."""
    path = _codex_file()
    try:
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
        result = {}
        for entry in document["models"]:
            if (not isinstance(entry, dict) or not isinstance(entry.get("slug"), str)
                    or entry.get("visibility") == "hide"):
                continue
            levels = entry.get("supported_reasoning_levels") or []
            efforts = [level.get("effort") if isinstance(level, dict) else level for level in levels]
            context = entry.get("context_window")
            result[entry["slug"][:64]] = {
                "efforts": [effort for effort in efforts if isinstance(effort, str)][:16],
                "name": str(entry.get("display_name") or entry["slug"])[:80],
                "context_tokens": context if type(context) is int and context > 0 else None}
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def catalogs() -> dict:
    """Agent name -> its catalog, or None when that agent keeps none Pod can read."""
    return {"codex": codex(), "claude": None}
