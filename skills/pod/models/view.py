"""One row per route or public observation, with each of its five states from its one source.

| State                     | Source                                   |
| Observed publicly         | the leaderboard snapshot                 |
| Known to the adapter      | the installed agent's own catalog        |
| Routable                  | the shipped registry                     |
| Enabled                   | the personal preference file             |
| Unknown or no route       | everything else, shown with the reason   |

Building the view reads; it changes nothing. A public observation never makes a route.
"""

from dataclasses import dataclass, field
import re

from . import adapters, registry, snapshot
from .leaderboard import METRICS
from .. import routefile

VALUE_TOLERANCE_POINTS = 2     # a cheaper route within this many index points beats the row
VALUE_FLOOR_QUANTILE = 0.5     # the row's index must reach this quantile of routable routes
VALUE_EXPLANATION = ("High value, benchmark-relative: no routable route costs less while scoring no more than "
                     f"{VALUE_TOLERANCE_POINTS} index points lower, and its index is at or above the routable median. "
                     "Decision support only.")
COST_STATEMENT = ("Cost per task is a public benchmark measurement. It is not a subscription charge, "
                  "a quota or how long your task will take.")
SORTS = ("index", "cost", "time", "first", "name", "state")
_SORT_METRIC = {"index": "intelligence_index", "cost": "cost_per_task_usd", "time": "time_per_task_min",
                "first": "first_answer_token_s"}
_STATE_ORDER = {"enabled": 0, "disabled": 1, "routable": 2, "unknown": 3, "no route": 4}


@dataclass
class Row:
    name: str
    provider: str
    state: str                       # enabled, disabled, routable, unknown or no route
    route: str | None = None         # agent/model/effort when the row is a route or a stored preference
    routable: bool = False           # listed in the shipped registry
    model_id: str | None = None
    effort: str | None = None
    reason: str | None = None        # why there is no route, or why the state is unknown
    observed: bool = False           # on the public snapshot
    adapter: str = "no catalog"      # listed, not listed or no catalog
    adapter_context: int | None = None
    preference: str | None = None    # enabled or disabled in the preference file; None when not set
    preferred: bool = False
    pinned: bool = False
    metrics: dict = field(default_factory=lambda: dict.fromkeys(METRICS))
    estimated: bool = False
    fallback: bool = False
    value: bool = False
    notes: list = field(default_factory=list)
    model_name: str | None = None   # base name for grouping, independent of the profile
    release: str | None = None      # public release identity when the snapshot supplies one
    profile: str | None = None      # published qualifier, or the route effort when unobserved
    public_index: int | None = None # position of this observation in the snapshot


@dataclass
class View:
    rows: list
    snapshot: dict | None
    attempt: dict | None
    preferences: routefile.Preferences
    providers: list                  # providers the registry routes, in registry order

    def header(self) -> list[str]:
        """Always the snapshot's age and the last failure."""
        if self.snapshot is None:
            first = "No public snapshot yet. Refresh with R here or `pod models refresh`; nothing is fetched otherwise."
        else:
            method = self.snapshot.get("methodology") or (
                f"{self.snapshot.get('attribution', 'public source')}, methodology not stated")
            first = (f"Public snapshot: {snapshot.age(self.snapshot.get('fetched_at'))} "
                     f"({self.snapshot.get('fetched_at')}) · {method}")
        lines = [first]
        if self.attempt is not None and not self.attempt["ok"]:
            lines.append(f"Last refresh failed {snapshot.age(self.attempt.get('attempted_at'))}: "
                         f"{self.attempt.get('failure')}. "
                         + ("The snapshot above was kept." if self.snapshot is not None else "There is no earlier snapshot."))
        if self.preferences.status == "invalid":
            lines.append(f"Preference file is invalid ({self.preferences.problem}); no route is eligible and "
                         "nothing can be changed here until it is corrected with `pod config edit`.")
        elif self.preferences.status == "absent":
            lines.append("No preference file yet: no route is enabled. Enabling one here creates it.")
        return lines


def _quantile(values: list, q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def mark_value(rows: list) -> None:
    """Set `value` on the routes the highlight rule selects. Changes nothing else."""
    pool = [row for row in rows if row.routable and row.observed and not row.estimated and not row.fallback
            and row.metrics["intelligence_index"] is not None and row.metrics["cost_per_task_usd"] is not None]
    for row in rows:
        row.value = False
    if not pool:
        return
    floor = _quantile([row.metrics["intelligence_index"] for row in pool], VALUE_FLOOR_QUANTILE)
    for row in pool:
        index, cost = row.metrics["intelligence_index"], row.metrics["cost_per_task_usd"]
        beaten = any(other is not row and other.metrics["cost_per_task_usd"] < cost
                     and other.metrics["intelligence_index"] >= index - VALUE_TOLERANCE_POINTS for other in pool)
        row.value = index >= floor and not beaten


def _adapter_state(catalog: dict | None, model_id: str, effort: str | None) -> tuple[str, int | None]:
    if catalog is None:
        return "no catalog", None
    entry = catalog.get(model_id)
    if entry is None or (effort is not None and effort not in entry["efforts"]):
        return "not listed", entry["context_tokens"] if entry else None
    return "listed", entry["context_tokens"]


def _public_name(entry: dict) -> registry.RowName:
    if "model_name" not in entry:
        return registry.read_name(entry["name"])
    base, profile = entry["model_name"], entry.get("profile")
    if profile is None:
        return registry.RowName(base)
    words = profile.casefold()
    if words.endswith(", default fallback"):
        words = words.removesuffix(", default fallback") + " with fallback"
    name = registry.read_name(f"{base} ({words})")
    return name if name.base == base else registry.RowName(base, qualifier=profile)


def build(*, models=None, shot="load", preferences=None, catalogs=None, attempt="load",
          separate_fallback: bool = False) -> View:
    models = registry.load() if models is None else models
    shot = snapshot.load() if shot == "load" else shot
    attempt = snapshot.last_attempt() if attempt == "load" else attempt
    preferences = routefile.load() if preferences is None else preferences
    catalogs = adapters.catalogs() if catalogs is None else catalogs
    public = list((shot or {}).get("rows", []))
    names = [_public_name(entry) for entry in public]
    matched = [registry.find(models, name.base) for name in names]
    matched = [model if model and model.provider.casefold() == (entry.get("creator") or "").casefold() else None
               for entry, model in zip(public, matched)]
    used: set = set()
    duplicates: set = set()
    rows: list = []
    agents = {model.agent: model.provider for model in models}

    def observe(row: Row, index: int) -> None:
        entry, name = public[index], names[index]
        row.observed, row.fallback, row.estimated = True, name.fallback, bool(entry.get("index_estimated"))
        row.metrics = {metric: entry.get(metric) for metric in METRICS}
        row.model_name = name.base
        row.release = entry.get("release")
        row.public_index = index
        if "model_name" in entry:
            row.profile = entry.get("profile")
        else:
            row.profile = ("non-reasoning" if name.non_reasoning else
                           f"{name.effort} with fallback" if name.fallback else name.effort or name.qualifier)

    for model in models:
        for effort in model.efforts:
            key = model.route(effort)
            preference = preferences.routes.get(key)
            adapter, context = _adapter_state(catalogs.get(model.agent), model.id, effort)
            row = Row(f"{model.name} ({effort})", model.provider, preference or "routable", route=key, routable=True,
                      model_id=model.id, effort=effort, adapter=adapter, adapter_context=context,
                      preference=preference, preferred=preferences.preferred == key, pinned=preferences.pinned == key,
                      model_name=model.name, profile=effort)
            matches = [index for index, name in enumerate(names)
                       if name.effort == effort and matched[index] == model]
            chosen = [index for index in matches if not names[index].fallback]
            if not chosen and not separate_fallback:
                # Plain and JSON keep the historical projection for table-only captures.
                chosen = [index for index in matches if "model_name" not in public[index]]
            if len(chosen) == 1:
                observe(row, chosen[0])
                used.add(chosen[0])
            elif len(chosen) > 1:
                row.notes.append("The leaderboard lists this name more than once. The route has no linked measurements.")
                duplicates.update(chosen)
            if adapter == "not listed":
                row.notes.append(f"The installed {model.agent} does not list this model and effort.")
            rows.append(row)
    for index, entry in enumerate(public):
        if index in used:
            continue
        name, model = names[index], matched[index]
        row = Row(entry["name"], entry.get("creator") or "unknown", "no route")
        observe(row, index)
        if model is None:
            row.reason = "not in the shipped registry"
        else:
            row.model_id = model.id
            row.adapter, row.adapter_context = _adapter_state(catalogs.get(model.agent), model.id, None)
            if index in duplicates:
                row.reason = "the leaderboard lists this profile more than once; measurements shown for information"
            elif name.non_reasoning:
                source = (f"the installed {model.agent} offers" if catalogs.get(model.agent) is not None
                          else "the registry lists")
                row.reason = f"{source} no non-reasoning effort for {model.id}; measurements shown for information"
            elif name.fallback and name.effort in model.efforts:
                row.reason = f"a fallback profile of {model.route(name.effort)}, shown for information"
            elif name.effort is not None:
                row.reason = f"the registry lists no {name.effort} effort for {model.id}"
            else:
                row.reason = f"the row states no effort Pod can route for {model.id}"
        rows.append(row)
    known = registry.routes(models)
    for key, state in preferences.routes.items():
        if key in known:
            continue
        agent, model_id, effort = routefile.split(key)
        rows.append(Row(key, agents.get(agent, agent), "unknown", route=key, model_id=model_id, effort=effort,
                        preference=state, preferred=preferences.preferred == key, pinned=preferences.pinned == key,
                        reason=f"set to {state} in your preference file; not in the shipped registry, "
                               "so Pod cannot say whether it runs"))
    listed = {model.id for model in models}
    for agent, catalog in catalogs.items():
        for model_id, entry in (catalog or {}).items():
            if model_id not in listed:
                rows.append(Row(entry["name"], agents.get(agent, agent), "no route", model_id=model_id,
                                adapter="listed", adapter_context=entry["context_tokens"],
                                reason=f"known to the installed {agent}; not in the shipped registry"))
    mark_value(rows)
    providers = list(dict.fromkeys(model.provider for model in models))
    return View(rows, shot, attempt, preferences, providers)


def select(view: View, *, provider: str = "routed", search: str = "", sort: str = "index",
           reverse: bool = False) -> list:
    """Filter and order rows. `routed` keeps the providers the registry routes; `all` keeps everything."""
    rows = view.rows
    if provider == "routed":
        rows = [row for row in rows if row.route is not None or row.provider in view.providers]
    elif provider != "all":
        rows = [row for row in rows if row.provider == provider]
    needle = search.casefold().strip()
    if needle:
        rows = [row for row in rows
                if needle in f"{row.name} {row.route or ''} {row.provider} {row.state}".casefold()]
    if sort in _SORT_METRIC:
        metric = _SORT_METRIC[sort]
        descending = (sort == "index") != reverse
        have = sorted((row for row in rows if row.metrics[metric] is not None),
                      key=lambda row: (row.metrics[metric], row.name), reverse=descending)
        return have + sorted((row for row in rows if row.metrics[metric] is None), key=lambda row: row.name)
    if sort == "state":
        return sorted(rows, key=lambda row: (_STATE_ORDER[row.state], row.name), reverse=reverse)
    return sorted(rows, key=lambda row: row.name.casefold(), reverse=reverse)


def visible(rows: list) -> list:
    """The human-facing catalog. Routing, diagnostics and JSON consume the full view."""
    # Only the registry and public rows identify a provider. Other rows inherit
    # an agent label for grouping and can refer to third-party models.
    return [row for row in rows if (row.routable or row.observed) and
            (row.provider.casefold() == "openai" or
             (row.provider.casefold() == "anthropic" and
              re.search(r"\bopus\b", row.model_id or row.name, re.IGNORECASE)))]


def as_json(row: Row) -> dict:
    return {"name": row.name, "provider": row.provider, "state": row.state, "route": row.route,
            "routable": row.routable, "reason": row.reason, "observed_publicly": row.observed,
            "adapter": row.adapter, "preference": row.preference, "preferred": row.preferred, "pinned": row.pinned,
            "metrics": row.metrics, "index_estimated": row.estimated, "fallback_profile": row.fallback,
            "high_value": row.value, "notes": row.notes}
