"""The shipped route registry: exact model ids, efforts, source aliases and attributed guidance.

The registry holds shipped facts only. Benchmark measurements live in observations, and web
data never extends this file; adding an ordinary model is a registry edit.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlparse

from .errors import PodError

SCHEMA = "pod-catalog/v3"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# A documented non-reasoning variant stays informational until an adapter documents `none`.
INFORMATIONAL_EFFORT = "none"
AGENTS = {"claude": "Anthropic", "codex": "OpenAI"}
PROVIDER_HOSTS = {"Anthropic": ("anthropic.com", "claude.com"), "OpenAI": ("chatgpt.com", "openai.com")}
CATALOG_PATH = Path(__file__).with_name("catalog.json")
MAX_CATALOG = 128 * 1024
_ID = re.compile(r"[a-z0-9][a-z0-9.-]{0,62}[a-z0-9]")
_SOURCE = re.compile(r"[a-z][a-z0-9_]{1,31}")
MODEL_FIELDS = {"id", "name", "agent", "provider", "efforts", "documented_context_tokens",
                "guidance", "sources", "guide", "aliases"}
NOT_ROUTABLE_FIELDS = {"id", "name", "agent", "provider", "note", "aliases"}


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PodError("invalid_catalog", "Catalog has a duplicate key")
        result[key] = value
    return result


def _exact(row: object, fields: set[str], name: str) -> dict:
    if not isinstance(row, dict) or set(row) != fields:
        raise PodError("invalid_catalog", f"{name} has missing or unsupported fields")
    return row


def _text(value: object, name: str, *, limit: int = 2048) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value)):
        raise PodError("invalid_catalog", f"{name} must be bounded printable text")
    return value


def _date(value: object, name: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise PodError("invalid_catalog", f"{name} must be an ISO date")
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise PodError("invalid_catalog", f"{name} is not a real date") from exc
    if day > date.today():
        raise PodError("invalid_catalog", f"{name} cannot be in the future")
    return day


def _url(value: object, name: str) -> str:
    parsed = urlparse(_text(value, name, limit=500))
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise PodError("invalid_catalog", f"{name} must be an HTTPS source URL")
    return value


def _identity(row: dict, name: str) -> None:
    if not isinstance(row["id"], str) or not _ID.fullmatch(row["id"]):
        raise PodError("invalid_catalog", f"{name} id is not an exact native model id")
    if row["agent"] not in AGENTS or row["provider"] != AGENTS[row["agent"]]:
        raise PodError("invalid_catalog", f"{row['id']}: agent or provider is unsupported")
    _text(row["name"], f"{row['id']} name", limit=80)


def _aliases(value: object, owner: str, allowed: tuple[str | None, ...]) -> dict:
    if not isinstance(value, dict) or len(value) > 8:
        raise PodError("invalid_catalog", f"{owner}: aliases map source ids to exact row names")
    for source, rows in value.items():
        if not _SOURCE.fullmatch(source) or not isinstance(rows, dict) or len(rows) > 64:
            raise PodError("invalid_catalog", f"{owner}: alias source {source!r} is invalid")
        for name, effort in rows.items():
            _text(name, f"{owner} alias", limit=120)
            if effort not in allowed:
                raise PodError("invalid_catalog", f"{owner}: alias {name!r} names an unsupported effort")
    return value


def validate(data: object) -> dict:
    root = _exact(data, {"schema", "max_guide", "models", "not_routable"}, "catalog")
    if root["schema"] != SCHEMA:
        raise PodError("invalid_catalog", "Unsupported catalog schema")
    _text(root["max_guide"], "max guide", limit=160)
    models, others = root["models"], root["not_routable"]
    if not isinstance(models, list) or not 1 <= len(models) <= 32:
        raise PodError("invalid_catalog", "Catalog needs a bounded list of supported models")
    if not isinstance(others, list) or len(others) > 32:
        raise PodError("invalid_catalog", "Not-routable ids must be a bounded list")
    seen: set[str] = set()
    aliases: set[tuple[str, str]] = set()
    for model in models:
        _exact(model, MODEL_FIELDS, "model")
        _identity(model, "model")
        model_id, provider = model["id"], model["provider"]
        efforts = model["efforts"]
        if (not isinstance(efforts, list) or not efforts or any(effort not in EFFORTS for effort in efforts)
                or len(efforts) != len(set(efforts))
                or efforts != [effort for effort in EFFORTS if effort in efforts]):
            raise PodError("invalid_catalog", f"{model_id}: efforts must be verified native values in order")
        context = model["documented_context_tokens"]
        if context is not None and (type(context) is not int or not 1 <= context <= 10_000_000):
            raise PodError("invalid_catalog", f"{model_id}: documented context must be null or a token count")
        guidance = _text(model["guidance"], "guidance")
        if not 20 <= len(guidance.split()) <= 70 or not guidance.startswith(provider):
            raise PodError("invalid_catalog", "Provider description needs 20–70 words attributed to the provider")
        sources = model["sources"]
        if not isinstance(sources, list) or not 1 <= len(sources) <= 6:
            raise PodError("invalid_catalog", "Model needs an official source")
        for source in sources:
            _exact(source, {"url", "checked"}, "source")
            _url(source["url"], "source URL")
            _date(source["checked"], "checked date")
            host = urlparse(source["url"]).hostname
            if not isinstance(host, str) or not any(host == domain or host.endswith("." + domain)
                                                    for domain in PROVIDER_HOSTS[provider]):
                raise PodError("invalid_catalog", "Source is not the model provider's documentation")
        guide = _exact(model["guide"], {"efforts", "use"}, "guide")
        _text(guide["efforts"], "guide efforts", limit=80)
        _text(guide["use"], "guide use", limit=120)
        _aliases(model["aliases"], model_id, (*efforts, INFORMATIONAL_EFFORT, None))
        if model_id in seen:
            raise PodError("invalid_catalog", f"{model_id}: duplicate model id")
        seen.add(model_id)
    for row in others:
        _exact(row, NOT_ROUTABLE_FIELDS, "not_routable")
        _identity(row, "not_routable")
        _text(row["note"], f"{row['id']} note", limit=160)
        _aliases(row["aliases"], row["id"], (*EFFORTS, INFORMATIONAL_EFFORT, None))
        if row["id"] in seen:
            raise PodError("invalid_catalog", f"{row['id']}: duplicate model id")
        seen.add(row["id"])
    for row in (*models, *others):
        for source, names in row["aliases"].items():
            for name in names:
                if (source, name) in aliases or name in seen:
                    raise PodError("invalid_catalog", f"Alias {name!r} for {source} is ambiguous")
                aliases.add((source, name))
    return root


def load(path: Path = CATALOG_PATH) -> dict:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PodError("invalid_catalog", "Catalog cannot be read") from exc
    if len(raw) > MAX_CATALOG:
        raise PodError("invalid_catalog", "Catalog exceeds its size limit")
    try:
        document = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PodError("invalid_catalog", "Catalog is not valid JSON") from exc
    return validate(document)


_DEFAULT: tuple[tuple[int, int, int], dict, dict] | None = None


def _default() -> tuple[dict, dict]:
    """The shipped registry and its alias index, reread only when the file changes."""
    global _DEFAULT
    try:
        info = CATALOG_PATH.stat()
    except OSError as exc:
        raise PodError("invalid_catalog", "Catalog cannot be read") from exc
    stamp = (info.st_ino, info.st_size, info.st_mtime_ns)
    if _DEFAULT is None or _DEFAULT[0] != stamp:
        document = load()
        _DEFAULT = (stamp, document, _index(document))
    return _DEFAULT[1], _DEFAULT[2]


def _doc(document: dict | None) -> dict:
    return _default()[0] if document is None else document


def by_id(document: dict | None = None) -> dict[str, dict]:
    """Supported bases, in registry order."""
    return {row["id"]: row for row in _doc(document)["models"]}


def known(document: dict | None = None) -> dict[str, dict]:
    """Supported and no-longer-supported ids, so older records stay readable."""
    doc = _doc(document)
    return {row["id"]: row for row in (*doc["models"], *doc["not_routable"])}


def route_key(agent: str, model: str, effort: str) -> str:
    return f"{agent}/{model}/{effort}"


def parse_route_key(key: object) -> dict:
    """Shape only: `agent/model/effort` with a known agent and effort vocabulary."""
    parts = key.split("/") if isinstance(key, str) and len(key) <= 160 else []
    if (len(parts) != 3 or parts[0] not in AGENTS or not _ID.fullmatch(parts[1])
            or parts[2] not in EFFORTS):
        raise PodError("invalid_route", "A route key is agent/model/effort with a supported effort")
    return {"agent": parts[0], "model": parts[1], "effort": parts[2]}


def routes(document: dict | None = None) -> list[dict]:
    """Every shipped supported route, in registry and effort order."""
    return [{"key": route_key(model["agent"], model["id"], effort), "agent": model["agent"],
             "model": model["id"], "effort": effort, "name": model["name"],
             "provider": model["provider"]}
            for model in _doc(document)["models"] for effort in model["efforts"]]


def route_keys(document: dict | None = None) -> tuple[str, ...]:
    return tuple(row["key"] for row in routes(document))


def supported_route(key: object, document: dict | None = None) -> dict | None:
    """The registry route for an exact key, or None when it is not a supported route."""
    try:
        shape = parse_route_key(key)
    except PodError:
        return None
    model = by_id(document).get(shape["model"])
    if model is None or model["agent"] != shape["agent"] or shape["effort"] not in model["efforts"]:
        return None
    return {"key": key, **shape, "name": model["name"], "provider": model["provider"]}


def _index(document: dict) -> dict:
    supported = {row["id"] for row in document["models"]}
    index = {}
    for row in (*document["models"], *document["not_routable"]):
        routable = row["id"] in supported
        for source, names in row["aliases"].items():
            for name, effort in names.items():
                route = (route_key(row["agent"], row["id"], effort)
                         if routable and effort not in (None, INFORMATIONAL_EFFORT) else None)
                index[(source, name)] = {"route": route, "model": row["id"], "effort": effort,
                                         "informational": route is None, "supported": routable}
        index[(None, row["id"])] = {"route": None, "model": row["id"], "effort": None,
                                    "informational": True, "supported": routable}
    return index


def match(source_id: str, row_name: str, document: dict | None = None) -> dict | None:
    """Exact source-row mapping; no fuzzy matching, and unknown names stay unmapped.

    A row maps through the source's explicit alias, or, for any source, when the row
    name is exactly a registry id. The result is
    `{"route", "model", "effort", "informational", "supported"}`: `route` is a supported
    route key or None; `informational` is true for every non-route match, such as a
    non-reasoning variant, a model-level provider row or a no-longer-supported id.
    """
    index = _default()[1] if document is None else _index(document)
    if not isinstance(source_id, str) or not isinstance(row_name, str):
        return None
    found = index.get((source_id, row_name)) or index.get((None, row_name))
    return dict(found) if found is not None else None


def format_usd(value: int | float | None) -> str:
    if value is None:
        return "unknown"
    if value == 0:
        return "$0.00"
    return f"${value:.2f}" if value >= 0.01 else f"${value:.2g}"


def format_latency(value: int | float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.0f} s" if value >= 100 else f"{value:.1f} s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pod.catalog")
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        document = load()
    except PodError as exc:
        print(f"Catalog validation failed: {exc}")
        return 1
    aliases = sum(len(names) for row in (*document["models"], *document["not_routable"])
                  for names in row["aliases"].values())
    print(f"Catalog valid: {len(document['models'])} supported models, {len(routes(document))} routes, "
          f"{aliases} source aliases, {len(document['not_routable'])} not routable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
