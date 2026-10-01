"""The one joined route projection that CLI, TUI, doctor and status read.

It joins the shipped registry, personal preferences, cached public observations and the
native observation, which stays unknown at runtime. It computes no rank, score or
recommendation; display order is the reader's choice. It never fetches.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .catalog import by_id, load as load_catalog, match, routes as registry_routes
from .config import load as load_preferences
from .errors import PodError

SCHEMA = "pod-routes/v1"
METRICS = ("intelligence", "usd_per_task", "output_tps", "first_response_s",
           "total_response_s", "context_tokens")
CREATORS = ("Anthropic", "OpenAI")
STALE_AFTER = timedelta(days=7)
NATIVE = {"access": "unknown"}
PREFERENCE_FIELDS = ("path", "schema", "status", "revision", "eligible", "preferred", "pinned",
                     "max_active", "refresh", "errors", "setup", "pin_diagnostic",
                     "preferred_diagnostic")


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def observed(now: datetime | None = None) -> dict:
    """Cached observations through `observations.load()`, or unknown when unavailable."""
    try:
        from . import observations
    except ImportError:
        return {"status": "unavailable", "reason": "observations module is unavailable", "sources": {}}
    try:
        view = observations.load(now=now)
    except PodError as exc:
        return {"status": "unavailable", "reason": exc.code, "sources": {}}
    snapshot = view.get("snapshot") if isinstance(view, dict) else None
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("sources"), dict):
        return {"status": "unknown", "origin": view.get("origin") if isinstance(view, dict) else None,
                "diagnostics": list(view.get("diagnostics") or []) if isinstance(view, dict) else [],
                "sources": {}}
    return {"status": "observed", "origin": view.get("origin"), "generation": snapshot.get("generation"),
            "created_at": snapshot.get("created_at"), "stale": view.get("stale"), "age_s": view.get("age_s"),
            "diagnostics": list(view.get("diagnostics") or []), "sources": snapshot["sources"]}


def _provenance(source: str, block: dict, row: dict) -> dict:
    return {"source": source, "row": row.get("name"),
            "qualifiers": list(row.get("qualifiers") or []),
            "methodology": block.get("methodology"),
            "retrieved_at": block.get("retrieved_at"), "published_at": block.get("published_at")}


def _metrics(source: str | None, block: dict | None, row: dict | None) -> dict:
    """Every metric keeps the one row it came from; missing values stay None, never 0."""
    values = row.get("metrics") if isinstance(row, dict) and isinstance(row.get("metrics"), dict) else {}
    origin = _provenance(source, block, row) if row is not None else {
        "source": None, "row": None, "qualifiers": [], "methodology": None,
        "retrieved_at": None, "published_at": None}
    return {name: {"value": values.get(name), **origin} for name in METRICS}


def _source_summary(source: str, block: dict, now: datetime) -> dict:
    retrieved = _when(block.get("retrieved_at"))
    rows = block.get("rows") if isinstance(block.get("rows"), list) else []
    return {"source": source, "url": block.get("url"), "attribution": block.get("attribution"),
            "required": block.get("required"), "status": block.get("status"),
            "retrieved_at": block.get("retrieved_at"), "published_at": block.get("published_at"),
            "methodology": block.get("methodology"), "rows": len(rows),
            "stale": retrieved is None or now - retrieved >= STALE_AFTER,
            "diagnostics": list(block.get("diagnostics") or [])[:16]}


def project(project_dir: Path | None = None, *, preferences: dict | None = None,
            observations: dict | None = None, catalog: dict | None = None,
            now: datetime | None = None) -> dict:
    """Registry × preferences × observations × native, with no routing inputs.

    Route rows carry `key, agent, model, name, effort, provider, state, preferred, pinned,
    supported, discovery, metrics, observations, documented_context_tokens, guide, guidance,
    sources, native`. Mapped observations that are not routes, and unknown Anthropic/OpenAI rows,
    are listed in `unmapped` with `discovery` `unsupported` or `new`; none of them is routable.
    """
    moment = now or datetime.now(timezone.utc)
    document = catalog if catalog is not None else load_catalog()
    snapshot = preferences if preferences is not None else load_preferences(project_dir)
    seen = observations if observations is not None else observed(now=moment)
    models = by_id(document)
    saved = snapshot.get("routes") or {}
    rows = {}
    for route in registry_routes(document):
        model = models[route["model"]]
        rows[route["key"]] = {
            **route, "state": {"enabled": "enabled", "disabled": "disabled"}.get(saved.get(route["key"]), "not_set"),
            "preferred": snapshot.get("preferred") == route["key"],
            "pinned": snapshot.get("pinned") == route["key"],
            "supported": True, "discovery": "supported", "metrics": _metrics(None, None, None),
            "observations": [], "documented_context_tokens": model["documented_context_tokens"],
            "guide": {**model["guide"], "max": document["max_guide"] if route["effort"] == "max" else None},
            "guidance": model["guidance"], "sources": [dict(row) for row in model["sources"]],
            "native": dict(NATIVE)}
    unmapped, sources, measured = [], {}, set()
    for source, block in sorted((seen.get("sources") or {}).items()):
        if not isinstance(block, dict):
            continue
        sources[source] = _source_summary(source, block, moment)
        for row in block.get("rows") if isinstance(block.get("rows"), list) else []:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                continue
            found = match(source, row["name"], document)
            entry = {**_provenance(source, block, row), "metrics": {
                name: (row.get("metrics") or {}).get(name) for name in METRICS}}
            if found is not None and found["route"] in rows:
                target = rows[found["route"]]
                # One row supplies every metric, so a score never pairs with another row's cost.
                if found["route"] not in measured:
                    measured.add(found["route"])
                    target["metrics"] = _metrics(source, block, row)
                target["observations"].append(entry)
            elif found is not None and found["supported"] and found["effort"] is None:
                for target in rows.values():
                    if target["model"] == found["model"]:
                        target["observations"].append(entry)
            elif row.get("creator") in CREATORS or found is not None:
                unmapped.append({**entry, "creator": row.get("creator"),
                                 "model": found["model"] if found else None,
                                 "effort": found["effort"] if found else None,
                                 "discovery": "unsupported" if found else "new", "routable": False})
    routes = list(rows.values())
    return {"schema": SCHEMA,
            "preferences": {key: snapshot.get(key) for key in PREFERENCE_FIELDS},
            "observations": {key: seen.get(key) for key in ("status", "reason", "origin", "generation",
                                                             "created_at", "stale", "age_s")}
                            | {"diagnostics": list(seen.get("diagnostics") or [])[:16], "sources": sources},
            "routes": routes, "unmapped": unmapped, "native": dict(NATIVE),
            "summary": {"routes": len(routes),
                        "enabled": sum(row["state"] == "enabled" for row in routes),
                        "disabled": sum(row["state"] == "disabled" for row in routes),
                        "not_set": sum(row["state"] == "not_set" for row in routes),
                        "preferred": snapshot.get("preferred"), "pinned": snapshot.get("pinned"),
                        "unmapped": len(unmapped)}}
