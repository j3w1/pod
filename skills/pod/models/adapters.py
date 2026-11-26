"""What the installed agent programs say they offer, read from their own local catalogs.

Codex keeps a local models cache. Antigravity's catalog is cached offline-first after an explicit
refresh (`pod models refresh` or `R`) runs `agy models`; reading the catalog never invokes `agy`
or the network, and nothing here ever enables a route.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .. import paths, routefile

MAX_BYTES = 8 * 1024 * 1024
AGY_SCHEMA = "pod-adapter-antigravity/v1"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_AGY_LINE = re.compile(r"^\s*([a-z][a-z0-9._-]{1,63})\s+(.+?)\s*$")
_EFFORT_PARENS = re.compile(r"\s*\((low|medium|high|xhigh|max|ultra)\)\s*$", re.IGNORECASE)


def _codex_file() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home and Path(home).is_absolute() else Path.home() / ".codex") / "models_cache.json"


def _antigravity_file() -> Path:
    return paths.cache_home() / "models" / "adapter-antigravity.json"


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


def _read_antigravity_cache() -> dict | None:
    path = _antigravity_file()
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema") != AGY_SCHEMA:
            return None
        models = document.get("models")
        if not isinstance(models, dict):
            return None
        cleaned = {}
        for model_id, entry in models.items():
            if not isinstance(model_id, str) or not isinstance(entry, dict):
                continue
            efforts = entry.get("efforts") or []
            if not isinstance(efforts, list):
                continue
            cleaned[model_id[:64]] = {
                "efforts": [item for item in efforts if isinstance(item, str)][:16],
                "name": str(entry.get("name") or model_id)[:80],
                "context_tokens": entry.get("context_tokens") if type(entry.get("context_tokens")) is int and entry["context_tokens"] > 0 else None,
            }
        ignored = document.get("ignored_non_google")
        return {
            "fetched_at": document.get("fetched_at"),
            "models": cleaned,
            "ignored_non_google": [item for item in ignored if isinstance(item, str)] if isinstance(ignored, list) else [],
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def antigravity() -> dict | None:
    """Offline-first Antigravity catalog from cache, or None when absent/unreadable."""
    cached = _read_antigravity_cache()
    return cached["models"] if cached is not None else None


def antigravity_meta() -> dict | None:
    """Cached metadata for the Antigravity catalog, or None when absent."""
    return _read_antigravity_cache()


def parse_antigravity_models(output: str) -> tuple[dict, list[str]]:
    """Parse `agy models` stdout into native Google models and ignored non-Google proxy ids."""
    models: dict[str, dict] = {}
    ignored: list[str] = []
    effort_rank = {effort: idx for idx, effort in enumerate(routefile.EFFORTS)}
    for raw_line in (output or "").splitlines():
        # Drop carriage-return spinner frames and ANSI sequences.
        line = _ANSI.sub("", raw_line.rsplit("\r", 1)[-1]).strip()
        if not line:
            continue
        match = _AGY_LINE.match(line)
        if not match:
            continue
        slug, display = match.group(1), match.group(2).strip()
        if not slug.startswith("gemini-"):
            if slug not in ignored:
                ignored.append(slug)
            continue
        base, sep, suffix = slug.rpartition("-")
        if sep and base and suffix in effort_rank:
            model_id = base[:64]
            clean_name = _EFFORT_PARENS.sub("", display).strip() or model_id
            slot = models.setdefault(model_id, {"efforts": [], "name": clean_name[:80], "context_tokens": None})
            if suffix not in slot["efforts"]:
                slot["efforts"].append(suffix)
        else:
            model_id = slug[:64]
            models.setdefault(model_id, {"efforts": [], "name": display[:80], "context_tokens": None})
    for slot in models.values():
        slot["efforts"].sort(key=lambda item: effort_rank.get(item, len(effort_rank)))
    return models, ignored


def _write_cache(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=1, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def refresh_antigravity(run=None) -> dict:
    """Run `agy models` once and cache native Google models. Isolated from AA refresh; enables nothing."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        if run is not None:
            output = run()
        else:
            done = subprocess.run(["agy", "models"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
            if done.returncode != 0:
                reason = (done.stderr or done.stdout or f"exit {done.returncode}").strip().splitlines()[0][:200]
                return {"ok": False, "installed": True, "attempted_at": stamp, "failure": f"agy models failed: {reason}"}
            output = done.stdout
    except FileNotFoundError:
        return {"ok": False, "installed": False, "attempted_at": stamp, "failure": "agy is not on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "installed": True, "attempted_at": stamp, "failure": "agy models timed out"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "installed": True, "attempted_at": stamp, "failure": f"agy models failed ({type(exc).__name__})"}
    except Exception as exc:
        return {"ok": False, "installed": True, "attempted_at": stamp, "failure": f"agy models failed ({type(exc).__name__})"}
    models, ignored = parse_antigravity_models(output)
    if not models and not ignored:
        return {"ok": False, "installed": True, "attempted_at": stamp, "failure": "agy models returned no readable models"}
    record = {"schema": AGY_SCHEMA, "fetched_at": stamp, "models": models, "ignored_non_google": ignored}
    try:
        _write_cache(_antigravity_file(), record)
    except OSError as exc:
        return {"ok": False, "installed": True, "attempted_at": stamp,
                "failure": f"adapter cache could not be written ({type(exc).__name__})"}
    return {"ok": True, "installed": True, "attempted_at": stamp, "models": len(models), "ignored_non_google": ignored}


def catalogs() -> dict:
    """Agent name -> its catalog, or None when that agent keeps none Pod can read."""
    return {"antigravity": antigravity(), "codex": codex(), "claude": None}
