"""One checked, manually maintained catalog of supported base models."""

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
    parsed = urlparse(_text(value, name))
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise PodError("invalid_catalog", f"{name} must be an HTTPS source URL")
    return value


def validate(data: object) -> dict:
    if (not isinstance(data, dict) or set(data) - {"schema", "models", "reference_benchmark"}
            or not {"schema", "models"} <= set(data)):
        raise PodError("invalid_catalog", "Catalog has missing or unsupported base fields")
    root = data
    if root["schema"] != "pod-catalog/v1":
        raise PodError("invalid_catalog", "Unsupported catalog schema")
    models = root["models"]
    if not isinstance(models, list) or [m.get("id") if isinstance(m, dict) else None for m in models] != list(IDS):
        raise PodError("invalid_catalog", "Catalog needs exactly the six supported ids in order")
    for model in models:
        _exact(model, {"id", "name", "agent", "provider", "efforts", "native_default",
                       "documented_context_tokens", "guidance", "examples", "sources"}, "model")
        model_id = model["id"]
        agent = "claude" if model_id.startswith("claude-") else "codex"
        provider = "Anthropic" if agent == "claude" else "OpenAI"
        if model["agent"] != agent or model["provider"] != provider:
            raise PodError("invalid_catalog", "Model agent or provider differs from id")
        _text(model["name"], "model name", limit=80)
        efforts = model["efforts"]
        allowed = set(EFFORTS) | ({"ultra"} if model_id in IDS[3:5] else set())
        if (not isinstance(efforts, list) or any(not isinstance(effort, str) for effort in efforts)
                or len(efforts) != len(set(efforts))
                or set(efforts) != allowed or efforts[:5] != list(EFFORTS)
                or model["native_default"] not in efforts):
            raise PodError("invalid_catalog", "Model efforts differ from documented native values")
        context = model["documented_context_tokens"]
        if context is not None and (type(context) is not int or not 1 <= context <= 2_000_000):
            raise PodError("invalid_catalog", "Documented context must be tokens or null")
        if (agent == "claude" and context != 1_000_000) or (agent == "codex" and context is not None):
            raise PodError("invalid_catalog", "Documented context differs from checked sources")
        guidance = _text(model["guidance"], "guidance")
        if not 35 <= len(guidance.split()) <= 55 or not guidance.startswith(provider):
            raise PodError("invalid_catalog", "Guidance needs 35–55 attributed words")
        examples = model["examples"]
        if (not isinstance(examples, list) or not 1 <= len(examples) <= 2
                or any(not isinstance(example, str) for example in examples)):
            raise PodError("invalid_catalog", "A model needs one or two Pod examples")
        for example in examples:
            _text(example, "Pod example", limit=200)
        sources = model["sources"]
        if not isinstance(sources, list) or not sources:
            raise PodError("invalid_catalog", "Model needs an official source")
        for source in sources:
            _exact(source, {"url", "checked"}, "source")
            _url(source["url"], "source URL")
            _date(source["checked"], "checked date")
            allowed_hosts = ("anthropic.com", "claude.com") if agent == "claude" else ("chatgpt.com",)
            host = urlparse(source["url"]).hostname
            if not isinstance(host, str) or not any(
                    host == domain or host.endswith("." + domain) for domain in allowed_hosts):
                raise PodError("invalid_catalog", "Source is not the model provider's documentation")
    # The dated third-party reference is informative and may be incomplete. It is
    # checked separately so a missing score cannot disable a supported route.
    return root


def _variant(row: object) -> dict | None:
    if (not isinstance(row, dict)
            or set(row) != {"profile", "intelligence", "usd_per_task", "first_chunk_s"}):
        return None
    profile, score = row["profile"], row["intelligence"]
    if (not isinstance(profile, str) or not 1 <= len(profile) <= 80
            or any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in profile)
            or type(score) is not int or not 0 <= score <= 100):
        return None
    for field in ("usd_per_task", "first_chunk_s"):
        value = row[field]
        if value is not None and (type(value) not in (float, int) or not 0 <= value <= 100000):
            return None
    return row


def reference_entry(document: dict, model_id: str) -> dict:
    """Return a safe display profile, with unknowns for an incomplete AA row."""
    benchmark = document.get("reference_benchmark")
    groups = benchmark.get("models") if isinstance(benchmark, dict) else None
    item = groups.get(model_id) if isinstance(groups, dict) else None
    empty = {"profile": "Unknown", "intelligence": None,
             "usd_per_task": None, "first_chunk_s": None}
    if not isinstance(item, dict):
        return {"reference_variant": "Unknown", "variants": [empty], "complete": False}
    selected = item.get("reference_variant")
    variants = item.get("variants")
    if (not isinstance(selected, str) or not selected or not isinstance(variants, list)
            or not variants or len(variants) > 32):
        return {"reference_variant": "Unknown", "variants": [empty], "complete": False}
    rows = [_variant(row) for row in variants]
    labels = [row["profile"] for row in rows if row is not None]
    if (any(row is None for row in rows) or len(labels) != len(set(labels))
            or rows[0]["profile"] != selected):
        return {"reference_variant": "Unknown", "variants": [empty], "complete": False}
    return {"reference_variant": selected, "variants": rows, "complete": True}


def benchmark_warnings(document: dict) -> list[str]:
    """Maintenance diagnostics; none are route or catalog-base blockers."""
    benchmark = document.get("reference_benchmark")
    if not isinstance(benchmark, dict):
        return ["reference benchmark is missing"]
    warnings = []
    if benchmark.get("url") != REFERENCE_URL:
        warnings.append("reference benchmark attribution URL is missing or changed")
    try:
        _date(benchmark.get("captured"), "benchmark capture date")
    except PodError:
        warnings.append("reference benchmark capture date is missing or invalid")
    groups = benchmark.get("models")
    if not isinstance(groups, dict):
        warnings.append("reference benchmark model groups are missing")
    else:
        if set(groups) - set(IDS):
            warnings.append("reference benchmark has unsupported model groups")
        for model_id in IDS:
            if not reference_entry(document, model_id)["complete"]:
                warnings.append(f"{model_id}: selected reference row is missing or incomplete")
    return warnings


def load(path: Path = CATALOG_PATH) -> dict:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PodError("invalid_catalog", "Catalog cannot be read") from exc
    if len(raw) > 64 * 1024:
        raise PodError("invalid_catalog", "Catalog exceeds its size limit")
    try:
        document = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PodError("invalid_catalog", "Catalog is not valid JSON") from exc
    return validate(document)


def by_id(document: dict | None = None) -> dict[str, dict]:
    return {row["id"]: row for row in (load() if document is None else document)["models"]}


def reference_rows(document: dict | None = None) -> dict[str, dict]:
    doc = load() if document is None else document
    return {model_id: reference_entry(doc, model_id)["variants"][0] for model_id in IDS}


def ranks(document: dict | None = None) -> dict[str, int | None]:
    scores = {model_id: row["intelligence"] for model_id, row in reference_rows(document).items()}
    if any(score is None for score in scores.values()):
        return {model_id: None for model_id in IDS}
    return {model_id: 1 + sum(other > score for other in scores.values())
            for model_id, score in scores.items()}


def age(document: dict | None = None, *, today: date | None = None) -> int | None:
    doc = load() if document is None else document
    benchmark = doc.get("reference_benchmark")
    captured = benchmark.get("captured") if isinstance(benchmark, dict) else None
    try:
        observed = _date(captured, "benchmark capture date")
    except PodError:
        return None
    return ((today or date.today()) - observed).days


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
    print("Catalog is valid: six supported models and dated reference rows" if not warnings
          else "Catalog models valid; reference benchmark needs maintenance")
    for warning in warnings:
        print("Benchmark warning: " + warning)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
