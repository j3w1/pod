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
    root = _exact(data, {"schema", "models", "reference_benchmark"}, "catalog")
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
    benchmark = _exact(root["reference_benchmark"], {"url", "captured", "models"}, "benchmark")
    if _url(benchmark["url"], "benchmark URL") != "https://artificialanalysis.ai/leaderboards/models":
        raise PodError("invalid_catalog", "Benchmark URL differs from its attribution")
    _date(benchmark["captured"], "benchmark capture date")
    references = benchmark["models"]
    if not isinstance(references, dict) or set(references) != set(IDS):
        raise PodError("invalid_catalog", "Benchmark needs exactly six model groups")
    for model_id, item in references.items():
        _exact(item, {"reference_variant", "variants"}, "benchmark model")
        _text(item["reference_variant"], "reference variant", limit=80)
        variants = item["variants"]
        if not isinstance(variants, list) or not variants:
            raise PodError("invalid_catalog", "Benchmark model needs variants")
        labels = set()
        for variant in variants:
            _exact(variant, {"profile", "intelligence", "usd_per_task", "first_chunk_s"}, "variant")
            profile = _text(variant["profile"], "variant profile", limit=80)
            if profile in labels:
                raise PodError("invalid_catalog", "Benchmark profile is duplicated")
            labels.add(profile)
            score = variant["intelligence"]
            if type(score) is not int or not 0 <= score <= 100:
                raise PodError("invalid_catalog", "Intelligence must be a score from 0 to 100")
            for field in ("usd_per_task", "first_chunk_s"):
                value = variant[field]
                if value is not None and (type(value) not in (float, int) or value < 0 or value > 100000):
                    raise PodError("invalid_catalog", f"{field} must be nonnegative or null")
        if item["reference_variant"] not in labels or item["reference_variant"] != variants[0]["profile"]:
            raise PodError("invalid_catalog", "Reference profile must name the first full variant")
    return root


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
    return {model_id: next(row for row in entry["variants"] if row["profile"] == entry["reference_variant"])
            for model_id, entry in doc["reference_benchmark"]["models"].items()}


def ranks(document: dict | None = None) -> dict[str, int]:
    scores = {model_id: row["intelligence"] for model_id, row in reference_rows(document).items()}
    return {model_id: 1 + sum(other > score for other in scores.values())
            for model_id, score in scores.items()}


def age(document: dict | None = None, *, today: date | None = None) -> int:
    doc = load() if document is None else document
    return ((today or date.today()) - date.fromisoformat(doc["reference_benchmark"]["captured"])).days


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pod.catalog")
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        load()
    except PodError as exc:
        print(f"Catalog validation failed: {exc}")
        return 1
    print("Catalog is valid: six supported models and dated reference rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
