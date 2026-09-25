"""Validated model identities, attributed guidance, and optional AA observations."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlparse

from .errors import PodError

IDS = ("claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5",
       "gpt-6-astra", "gpt-6-sol", "gpt-6-luna")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
CATALOG_PATH = Path(__file__).with_name("catalog.json")
REFERENCE_URL = "https://artificialanalysis.ai/leaderboards/models"
UNKNOWN = {"effort": None, "profile": "Unknown", "intelligence": None,
           "usd_per_task": None, "first_chunk_s": None, "total_response_s": None}


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
    if (not isinstance(value, str) or not value or len(value) > limit
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


def _record(row: object, model: dict) -> dict | None:
    if not isinstance(row, dict) or set(row) not in (
            {"effort", "profile", "intelligence", "usd_per_task", "first_chunk_s", "total_response_s"},
            {"effort", "profile", "intelligence", "usd_per_task", "first_chunk_s", "total_response_s", "note"}):
        return None
    effort = row["effort"]
    if effort not in (*model["efforts"], "none") or (effort == "none") != (row["profile"] == "Non-reasoning"):
        return None
    try:
        _text(row["profile"], "benchmark profile", limit=80)
        if "note" in row:
            _text(row["note"], "benchmark note", limit=200)
    except PodError:
        return None
    if effort != "none" and row["profile"] not in (effort, effort + " with fallback"):
        return None
    score = row["intelligence"]
    if score is not None and (type(score) is not int or not 0 <= score <= 100):
        return None
    for key in ("usd_per_task", "first_chunk_s", "total_response_s"):
        value = row[key]
        if value is not None and (type(value) not in (float, int) or not 0 <= value <= 100000):
            return None
    return row


def _rows(document: dict, model: dict) -> list[dict]:
    block = document.get("benchmarks")
    groups = block.get("models") if isinstance(block, dict) else None
    rows = groups.get(model["id"]) if isinstance(groups, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        return []
    checked = [_record(row, model) for row in rows]
    if any(row is None for row in checked) or len({row["effort"] for row in checked}) != len(rows):
        return []
    return checked


def validate(data: object) -> dict:
    root = _exact(data, {"schema", "models", "benchmarks"}, "catalog")
    if root["schema"] != "pod-catalog/v2":
        raise PodError("invalid_catalog", "Unsupported catalog schema")
    models = root["models"]
    if not isinstance(models, list) or [m.get("id") if isinstance(m, dict) else None for m in models] != list(IDS):
        raise PodError("invalid_catalog", "Catalog needs exactly the six supported ids in order")
    orders = []
    for model in models:
        _exact(model, {"id", "name", "agent", "provider", "efforts", "native_default",
                       "documented_context_tokens", "guidance", "sources", "guide"}, "model")
        model_id = model["id"]
        agent = "claude" if model_id.startswith("claude-") else "codex"
        provider = "Anthropic" if agent == "claude" else "OpenAI"
        if model["agent"] != agent or model["provider"] != provider:
            raise PodError("invalid_catalog", "Model agent or provider differs from id")
        _text(model["name"], "model name", limit=80)
        efforts = model["efforts"]
        allowed = set(EFFORTS) | ({"ultra"} if model_id in IDS[3:5] else set())
        if (not isinstance(efforts, list) or any(not isinstance(effort, str) for effort in efforts)
                or len(efforts) != len(set(efforts)) or set(efforts) != allowed
                or efforts[:5] != list(EFFORTS) or model["native_default"] not in efforts):
            raise PodError("invalid_catalog", "Model efforts differ from documented native values")
        context = model["documented_context_tokens"]
        if (agent == "claude" and context != 1_000_000) or (agent == "codex" and context is not None):
            raise PodError("invalid_catalog", "Documented context differs from checked sources")
        guidance = _text(model["guidance"], "guidance")
        if not 35 <= len(guidance.split()) <= 70 or not guidance.startswith(provider):
            raise PodError("invalid_catalog", "Guidance needs 35–70 attributed words")
        sources = model["sources"]
        if not isinstance(sources, list) or not sources:
            raise PodError("invalid_catalog", "Model needs an official source")
        for source in sources:
            _exact(source, {"url", "checked"}, "source")
            _url(source["url"], "source URL")
            _date(source["checked"], "checked date")
            allowed_hosts = ("anthropic.com", "claude.com") if agent == "claude" else ("chatgpt.com",)
            host = urlparse(source["url"]).hostname
            if not isinstance(host, str) or not any(host == domain or host.endswith("." + domain) for domain in allowed_hosts):
                raise PodError("invalid_catalog", "Source is not the model provider's documentation")
        guide = _exact(model["guide"], {"profile", "suggested_use", "best_for", "use_when", "ladder",
                                        "trade_off", "examples", "limitations", "coding_order"}, "guide")
        if guide["profile"] not in efforts or guide["profile"] == "ultra":
            raise PodError("invalid_catalog", "Guide profile is not a selectable effort")
        for key, limit in (("suggested_use", 80), ("best_for", 220), ("use_when", 260),
                           ("trade_off", 260), ("limitations", 260)):
            _text(guide[key], key, limit=limit)
        ladder = _exact(guide["ladder"], {"quick", "normal", "hard", "escalation"}, "ladder")
        if any(value not in efforts or value == "ultra" for value in ladder.values()):
            raise PodError("invalid_catalog", "Guide ladder has unsupported effort")
        examples = guide["examples"]
        if not isinstance(examples, list) or not 1 <= len(examples) <= 3:
            raise PodError("invalid_catalog", "Guide needs one to three examples")
        for example in examples:
            _exact(example, {"effort", "text"}, "example")
            if example["effort"] not in efforts or example["effort"] == "ultra":
                raise PodError("invalid_catalog", "Example effort is unsupported")
            _text(example["text"], "example text", limit=180)
        order = guide["coding_order"]
        if type(order) is not int or not 1 <= order <= 6:
            raise PodError("invalid_catalog", "Coding order must be 1–6")
        orders.append(order)
        # Optional benchmark data can be missing, but a present profile must resolve.
        rows = _rows(root, model)
    if sorted(orders) != list(range(1, 7)):
        raise PodError("invalid_catalog", "Coding orders must be unique")
    benchmark = root["benchmarks"]
    if benchmark is not None:
        if not isinstance(benchmark, dict) or set(benchmark) != {"source", "captured", "models"}:
            raise PodError("invalid_catalog", "Benchmark block has unsupported fields")
        _url(benchmark["source"], "benchmark source")
        if benchmark["source"] != REFERENCE_URL:
            raise PodError("invalid_catalog", "Benchmark source differs from AA")
        _date(benchmark["captured"], "benchmark capture date")
        groups = benchmark["models"]
        if not isinstance(groups, dict) or set(groups) - set(IDS):
            raise PodError("invalid_catalog", "Benchmark groups have unsupported ids")
    return root


def load(path: Path = CATALOG_PATH) -> dict:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PodError("invalid_catalog", "Catalog cannot be read") from exc
    if len(raw) > 128 * 1024:
        raise PodError("invalid_catalog", "Catalog exceeds its size limit")
    try:
        document = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PodError("invalid_catalog", "Catalog is not valid JSON") from exc
    return validate(document)


def by_id(document: dict | None = None) -> dict[str, dict]:
    return {row["id"]: row for row in (load() if document is None else document)["models"]}


def records(document: dict, model_id: str) -> list[dict]:
    model = by_id(document)[model_id]
    return _rows(document, model)


def record(document: dict, model_id: str, effort: str) -> dict:
    return next((row for row in records(document, model_id) if row["effort"] == effort),
                {**UNKNOWN, "effort": effort, "profile": effort})


def reference_entry(document: dict, model_id: str) -> dict:
    model = by_id(document)[model_id]
    rows = records(document, model_id)
    selected = record(document, model_id, model["guide"]["profile"])
    return {"reference_variant": selected["profile"], "variants": rows or [selected],
            "complete": bool(rows and selected in rows)}


def reference_rows(document: dict | None = None) -> dict[str, dict]:
    doc = load() if document is None else document
    return {model_id: record(doc, model_id, by_id(doc)[model_id]["guide"]["profile"]) for model_id in IDS}


def ranks(document: dict | None = None) -> dict[str, int | None]:
    scores = {model_id: row["intelligence"] for model_id, row in reference_rows(document).items()}
    if any(score is None for score in scores.values()):
        return {model_id: None for model_id in IDS}
    return {model_id: 1 + sum(other > score for other in scores.values()) for model_id, score in scores.items()}


def age(document: dict | None = None, *, today: date | None = None) -> int | None:
    doc = load() if document is None else document
    block = doc.get("benchmarks")
    try:
        observed = _date(block.get("captured") if isinstance(block, dict) else None, "benchmark capture date")
    except PodError:
        return None
    return ((today or date.today()) - observed).days


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


def guide_projection(document: dict | None = None) -> list[dict]:
    """Compact display guidance for the dispatch-free config JSON path."""
    doc = load() if document is None else document
    return [{key: model[key] for key in ("id", "name", "agent", "efforts", "guidance")}
            | {"suggested_use": model["guide"]["suggested_use"], "ladder": model["guide"]["ladder"].copy()}
            for model in doc["models"]]


def benchmark_warnings(document: dict) -> list[str]:
    block = document.get("benchmarks")
    if not isinstance(block, dict):
        return ["benchmark rows are missing"]
    warnings = []
    for model in document["models"]:
        rows = records(document, model["id"])
        if not rows:
            warnings.append(f"{model['id']}: benchmark rows are missing or incomplete")
        elif model["guide"]["profile"] not in {row["effort"] for row in rows}:
            warnings.append(f"{model['id']}: guide profile benchmark row is missing")
    return warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pod.catalog")
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        document = load()
    except PodError as exc:
        print(f"Catalog validation failed: {exc}")
        return 1
    warnings = benchmark_warnings(document)
    print(f"Catalog valid: six models; AA captured {document['benchmarks']['captured'] if document['benchmarks'] else 'unknown'}")
    for warning in warnings:
        print("Benchmark warning: " + warning)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
