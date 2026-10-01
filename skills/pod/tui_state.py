"""Pure model-workspace state: rows, sorting, filters, dialogs and key handling; no I/O.

Every row comes from the one joined route projection. Sorting, filters, compare marks and
the efficiency frontier are display choices: they never write preferences or rank routes
for routing. Edits leave the reducer as effects that the terminal loop saves.
"""

from __future__ import annotations

from dataclasses import dataclass, field as data_field, replace
from datetime import datetime

EFFORTS = ("low", "medium", "high", "xhigh", "max")
METRICS = ("intelligence", "usd_per_task", "output_tps", "first_response_s", "total_response_s",
           "context_tokens")
SORTS = ("intelligence", "usd_per_task", "output_tps", "first_response_s", "total_response_s",
         "model", "effort", "state")
SORT_LABELS = {"intelligence": "AA index", "usd_per_task": "AA $/task", "output_tps": "AA tokens/s",
               "first_response_s": "AA first response", "total_response_s": "AA total response",
               "model": "Model", "effort": "Effort", "state": "State"}
# Higher is the useful end for these, so they start descending; times and cost start ascending.
DESCENDING_FIRST = ("intelligence", "output_tps")
PROVIDERS = ("all", "Anthropic", "OpenAI")
STATES = ("all", "enabled", "disabled", "not_set")
DISCOVERY = ("supported", "new", "unsupported", "all")
TABS = ("details", "benchmarks", "routing", "sources")
MAX_COMPARE = 4
MAX_QUERY = 80
STATE_ORDER = {"enabled": 0, "disabled": 1, "not_set": 2}
BULK_SCOPES = ("model", "range", "not_set", "reset")
FRONTIER_DIMENSIONS = ("intelligence", "usd_per_task", "first_response_s")


@dataclass(frozen=True)
class Item:
    """One displayed table row: a supported route, a model group header or an observation."""
    key: str
    kind: str
    route: dict | None = None
    model: str | None = None
    observation: dict | None = None


@dataclass(frozen=True)
class Bulk:
    """The dialog's choices and the exact preview the user is shown.

    `changes`, `before`, `conflicts` and `displayed` are captured whenever the preview is built;
    Enter saves exactly them against `displayed`, never a set recomputed from later data.
    """
    scope: str = "model"
    action: str = "enabled"
    model: str | None = None
    low: int = 0
    high: int = len(EFFORTS) - 1
    clear_pin: bool = False
    clear_preferred: bool = False
    field: int = 0
    scroll: int = 0
    changes: dict = data_field(default_factory=dict)
    before: dict = data_field(default_factory=dict)
    conflicts: dict = data_field(default_factory=dict)
    displayed: dict = data_field(default_factory=dict)


@dataclass(frozen=True)
class Setup:
    preview: dict
    pin: int = -1          # index into pin options, len(options) means "clear"; -1 is unchosen
    preferred: int = 0     # 0 means none, otherwise an index + 1 into enabled preview routes
    field: int = 0
    confirming: bool = False
    scroll: int = 0


@dataclass(frozen=True)
class Effect:
    kind: str
    payload: dict = data_field(default_factory=dict)
    label: str = ""
    # The preference values the edit was reviewed against, when not the session's latest read.
    displayed: dict | None = None
    # A bulk dialog to reopen with a fresh preview when its save is refused.
    bulk: Bulk | None = None


@dataclass(frozen=True)
class State:
    projection: dict
    previous: dict | None = None
    focus: str | None = None
    hidden_focus: str | None = None
    sort: str = "intelligence"
    descending: bool = True
    grouped: bool = False
    collapsed: frozenset = frozenset()
    provider: str = "all"
    model: str = "all"
    effort: str = "all"
    state_filter: str = "all"
    discovery: str = "supported"
    query: str = ""
    mode: str = "browse"          # browse | search | palette | help | bulk | setup
    pane: str = "table"           # table | inspector
    tab: str = "details"
    table_scroll: int = 0
    inspector_scroll: int = 0
    help_scroll: int = 0
    table_page: int = 8
    inspector_page: int = 4
    compare: tuple = ()
    frontier: bool = False
    palette_query: str = ""
    palette_index: int = 0
    # The preferences and focus the table showed when the palette opened over it.
    palette_basis: tuple | None = None
    bulk: Bulk | None = None
    setup: Setup | None = None
    notice: str = ""
    error: str = ""
    saved_at: datetime | None = None
    # idle | running | updated | unchanged | checked | refused | superseded | cancelled | failed
    refresh_state: str = "idle"
    refresh_detail: str = ""


# --------------------------------------------------------------------------- reading the projection

def preferences(state: State) -> dict:
    return state.projection.get("preferences") or {}


def editable(state: State) -> bool:
    return preferences(state).get("status") == "valid"


def routes(state: State) -> list[dict]:
    return list(state.projection.get("routes") or [])


def route(state: State, key: str | None) -> dict | None:
    return next((row for row in routes(state) if row["key"] == key), None)


def observation_key(row: dict) -> str:
    return f"obs:{row.get('source')}:{row.get('row')}"


def observations(state: State) -> list[dict]:
    return list(state.projection.get("unmapped") or [])


def model_order(state: State) -> list[str]:
    return list(dict.fromkeys(row["model"] for row in routes(state)))


def model_name(state: State, model_id: str | None) -> str:
    row = next((row for row in routes(state) if row["model"] == model_id), None)
    return row["name"] if row else str(model_id)


def value(row: dict, name: str):
    """A route's metric value, or an observation row's raw value; None when unknown."""
    metric = (row.get("metrics") or {}).get(name)
    return metric.get("value") if isinstance(metric, dict) else metric


def effort_index(effort: object) -> int:
    return EFFORTS.index(effort) if effort in EFFORTS else len(EFFORTS)


def _effort_rank(state: State, row: dict) -> tuple:
    order = model_order(state)
    model = row.get("model")
    return (order.index(model) if model in order else len(order), effort_index(row.get("effort")),
            str(row.get("key") or row.get("row") or ""))


def _sort_key(state: State, row: dict) -> tuple:
    tie = _effort_rank(state, row)
    if state.sort in METRICS:
        number = value(row, state.sort)
        if number is None:
            return (1, 0, tie)
        return (0, -number if state.descending else number, tie)
    if state.sort == "model":
        name = str(row.get("name") or row.get("row") or "").casefold()
        return (0, name, effort_index(row.get("effort")), tie)
    if state.sort == "effort":
        return (0, effort_index(row.get("effort")), tie)
    return (0, STATE_ORDER.get(row.get("state"), 3), tie)


def _ordered(state: State, rows: list[dict]) -> list[dict]:
    rows = sorted(rows, key=lambda row: _sort_key(state, row))
    if state.sort not in METRICS and state.descending:
        # Text orderings reverse as a whole; a metric keeps unknown values last either way.
        rows.reverse()
    return rows


def _matches(state: State, row: dict, *, observation: bool) -> bool:
    provider = row.get("provider") if not observation else row.get("creator")
    if state.provider != "all" and provider != state.provider:
        return False
    if state.model != "all" and row.get("model") != state.model:
        return False
    if state.effort != "all" and row.get("effort") != state.effort:
        return False
    if state.state_filter != "all" and (observation or row.get("state") != state.state_filter):
        return False
    if observation:
        if state.discovery not in ("all", row.get("discovery")):
            return False
    elif state.discovery not in ("all", "supported"):
        return False
    if state.query:
        haystack = " ".join(str(row.get(name) or "") for name in ("name", "model", "key", "effort", "row",
                                                                   "creator", "provider")).casefold()
        if not all(word in haystack for word in state.query.casefold().split()):
            return False
    return True


def shown_routes(state: State) -> list[dict]:
    return _ordered(state, [row for row in routes(state) if _matches(state, row, observation=False)])


def shown_observations(state: State) -> list[dict]:
    rows = [{**row, "key": observation_key(row), "name": row.get("row")}
            for row in observations(state) if _matches(state, row, observation=True)]
    return _ordered(state, rows)


def items(state: State) -> list[Item]:
    """The table rows for the current view, filters and sort."""
    shown = shown_routes(state)
    extra = [Item(row["key"], "observation", observation=row) for row in shown_observations(state)]
    if not state.grouped:
        merged = [Item(row["key"], "route", route=row) for row in shown]
        if extra:
            ordered = _ordered(state, [*(item.route for item in merged), *(item.observation for item in extra)])
            lookup = {item.key: item for item in (*merged, *extra)}
            return [lookup[row["key"]] for row in ordered]
        return merged
    result = []
    for model in model_order(state):
        members = sorted((row for row in shown if row["model"] == model), key=lambda row: effort_index(row["effort"]))
        if not members:
            continue
        result.append(Item("model:" + model, "group", model=model))
        if model not in state.collapsed:
            result.extend(Item(row["key"], "route", route=row) for row in members)
    if extra:
        result.append(Item("group:observations", "group", model=None))
        if None not in state.collapsed:
            result.extend(sorted(extra, key=lambda item: str(item.observation.get("row")).casefold()))
    return result


def visible_keys(state: State) -> list[str]:
    return [item.key for item in items(state)]


def focused_item(state: State) -> Item | None:
    """The visibly focused row; None when no row is shown. Edits never fall back to another row."""
    return next((item for item in items(state) if item.key == state.focus), None)


def focused_route(state: State) -> dict | None:
    item = focused_item(state)
    if item is None:
        return None
    if item.kind == "route":
        return item.route
    return None


def group_members(state: State, model: str | None) -> list[dict]:
    return [row for row in routes(state) if row["model"] == model]


# --------------------------------------------------------------------------- derived comparisons

def profile(row: dict, name: str = "intelligence") -> tuple:
    """What makes two AA numbers like-for-like: source, methodology and AA profile qualifiers."""
    metric = (row.get("metrics") or {}).get(name)
    if not isinstance(metric, dict):
        return (None, None, ())
    return (metric.get("source"), metric.get("methodology"), tuple(metric.get("qualifiers") or ()))


def profile_words(qualifiers: tuple | list) -> str:
    return ", ".join(qualifiers) if qualifiers else "(none)"


def profile_difference(first: tuple, second: tuple, joiner: str = " vs ") -> str:
    """Names how two profiles differ, or "" when they are like-for-like."""
    parts = []
    if first[0] != second[0]:
        parts.append(f"source {first[0] or 'none'}{joiner}{second[0] or 'none'}")
    if first[1] != second[1]:
        parts.append(f"methodology {first[1] or 'not stated'}{joiner}{second[1] or 'not stated'}")
    if first[2] != second[2]:
        parts.append(f"AA profile {profile_words(first[2])}{joiner}{profile_words(second[2])}")
    return "; ".join(parts)


def frontier_scope(row: dict) -> tuple | None:
    """The like-for-like set a route's frontier dimensions belong to, or None when one is missing."""
    scopes = {profile(row, name) for name in FRONTIER_DIMENSIONS}
    if any(value(row, name) is None for name in FRONTIER_DIMENSIONS) or len(scopes) != 1:
        return None
    return scopes.pop()


def frontier_scopes(state: State) -> list[tuple[tuple, int]]:
    """Each like-for-like set among the shown routes with its size, largest first."""
    counts: dict[tuple, int] = {}
    for item in items(state):
        scope = frontier_scope(item.route) if item.kind == "route" else None
        if scope is not None:
            counts[scope] = counts.get(scope, 0) + 1
    return sorted(counts.items(), key=lambda entry: (-entry[1], str(entry[0])))


def frontier(state: State) -> dict[str, str]:
    """AA efficiency frontier among the shown rows: "frontier", "dominated" or "unknown".

    A route is only compared with shown routes of the same source, methodology and AA profile
    qualifiers, so each such set has its own frontier. It is a display marker for the rows on
    screen only; it never ranks routes for routing or changes eligibility.
    """
    marks, scopes = {}, {}
    for item in items(state):
        if item.kind != "route":
            continue
        scope = frontier_scope(item.route)
        if scope is None:
            marks[item.key] = "unknown"
        else:
            scopes.setdefault(scope, []).append(item.route)
    for members in scopes.values():
        for row in members:
            index, cost, first = (value(row, name) for name in FRONTIER_DIMENSIONS)
            dominated = any(value(other, "intelligence") >= index and value(other, "usd_per_task") <= cost
                            and value(other, "first_response_s") <= first
                            and (value(other, "intelligence"), value(other, "usd_per_task"),
                                 value(other, "first_response_s")) != (index, cost, first)
                            for other in members if other is not row)
            marks[row["key"]] = "dominated" if dominated else "frontier"
    return marks


def comparable(base: dict, other: dict, name: str) -> bool:
    return (profile(base, name) == profile(other, name)
            and value(base, name) is not None and value(other, name) is not None)


def delta(base: dict, other: dict, name: str) -> str:
    """A bounded difference: index points, or a percentage with a valid nonzero baseline."""
    if not comparable(base, other, name):
        if value(base, name) is None or value(other, name) is None:
            return "unknown"
        return "not comparable (" + profile_difference(profile(base, name), profile(other, name)) + ")"
    before, after = value(base, name), value(other, name)
    if name == "intelligence":
        change = after - before
        return f"{change:+g} pts" if change else "same"
    if before == 0:
        return "no percentage (zero baseline)"
    change = (after - before) / before * 100
    return f"{change:+.1f}%" if round(change, 1) else "same"


def changes(state: State, key: str) -> list[tuple[str, object, object]]:
    """Metric changes for one route since the previous comparable snapshot."""
    previous = (state.previous or {}).get("routes", {}).get(key)
    current = route(state, key)
    if previous is None or current is None:
        return []
    return [(name, previous.get(name), value(current, name)) for name in METRICS
            if previous.get(name) != value(current, name)]


def profile_change(state: State, key: str) -> str:
    """How a route's AA source, methodology or profile differs from the previous snapshot; "" if not.

    Only a route measured in both snapshots can change profile; a route that gained or lost its
    measurement shows that as an unknown value instead.
    """
    entry = ((state.previous or {}).get("profiles") or {}).get(key)
    current = route(state, key)
    if not entry or current is None:
        return ""
    before = (entry.get("source"), entry.get("methodology"), tuple(entry.get("qualifiers") or ()))
    after = profile(current)
    if before[0] is None or after[0] is None:
        return ""
    return profile_difference(before, after, " -> ")


def methodology_changed(state: State) -> bool:
    before = (state.previous or {}).get("methodology") or {}
    now = {name: block.get("methodology")
           for name, block in ((state.projection.get("observations") or {}).get("sources") or {}).items()}
    return any(name in now and now[name] != label for name, label in before.items())


# --------------------------------------------------------------------------- construction and refreshes

def initial(projection: dict, previous: dict | None = None) -> State:
    state = State(projection=projection, previous=previous)
    rows = items(state)
    return replace(state, focus=rows[0].key if rows else None)


def with_projection(state: State, projection: dict, previous: dict | None = None, *,
                    keep_previous: bool = True) -> State:
    """New data keeps focus identity, view choices, compare marks and hidden selections."""
    changed = replace(state, projection=projection,
                      previous=state.previous if keep_previous and previous is None else previous)
    keys = {row["key"] for row in routes(changed)}
    changed = replace(changed, compare=tuple(key for key in state.compare if key in keys))
    if changed.tab == "compare" and not changed.compare:
        changed = replace(changed, tab="details")
    return _keep_focus(state, changed)


def with_notice(state: State, message: str, *, saved_at: datetime | None = None, error: bool = False) -> State:
    if error:
        return replace(state, notice=message, error=message)
    return replace(state, notice=message, saved_at=saved_at or state.saved_at,
                   error="" if saved_at else state.error)


def with_refresh(state: State, status: str, detail: str = "") -> State:
    return replace(state, refresh_state=status, refresh_detail=detail)


def with_setup(state: State, preview: dict | None) -> State:
    if preview is None:
        return replace(state, setup=None, mode="browse" if state.mode == "setup" else state.mode)
    return replace(state, setup=Setup(preview=preview), mode="setup")


def rebase_setup(state: State, preview: dict | None) -> tuple[State, str]:
    """Rebuild the route setup from freshly read bytes, keeping only choices that still apply.

    Returns the new state and a note for the user; the note is empty when nothing changed.
    """
    old = state.setup
    if preview is None:
        if old is None:
            return state, ""
        return with_setup(state, None), "route setup closed: the preference file is no longer pod/v1"
    if old is None:
        return with_setup(state, preview), ""
    if old.preview.get("revision") == preview.get("revision"):
        return state, ""
    old_pins, old_preferred = setup_options(state)
    rebased = replace(state, setup=Setup(preview=preview))
    new_pins, new_preferred = setup_options(rebased)
    needs_pin = any(row.get("id") == "pin" for row in preview.get("choices", []))
    lost, pin, preferred = [], -1, 0
    if old.pin >= 0:
        if not needs_pin:
            lost.append("the pin choice")
        elif old.pin >= len(old_pins):
            pin = len(new_pins)
        elif old_pins[old.pin] in new_pins:
            pin = new_pins.index(old_pins[old.pin])
        else:
            lost.append("the pin choice")
    if old.preferred:
        key = old_preferred[old.preferred - 1]
        if key in new_preferred:
            preferred = new_preferred.index(key) + 1
        else:
            lost.append("the Preferred choice")
    rebased = replace(rebased, setup=replace(rebased.setup, pin=pin, preferred=preferred))
    note = "the preference file changed, so route setup was re-read; "
    note += ("cleared " + " and ".join(lost) + " because it no longer applies" if lost
             else "your choices still apply")
    return rebased, note


def with_viewport(state: State, *, table: int, table_page: int, inspector: int, inspector_page: int,
                  help_scroll: int | None = None) -> State:
    """Adopt the scroll positions and page sizes the renderer actually used."""
    changed = replace(state, table_scroll=table, table_page=max(1, table_page), inspector_scroll=inspector,
                      inspector_page=max(1, inspector_page),
                      help_scroll=state.help_scroll if help_scroll is None else help_scroll)
    return state if changed == state else changed


def _keep_focus(previous: State, changed: State) -> State:
    """Keep the focused identity visible, remembering one hidden by a view change."""
    after = visible_keys(changed)
    remembered = previous.hidden_focus
    if remembered in after:
        return replace(changed, focus=remembered, hidden_focus=None)
    if changed.focus in after:
        return changed
    if not after:
        return replace(changed, hidden_focus=remembered or previous.focus)
    before = visible_keys(previous)
    index = before.index(previous.focus) if previous.focus in before else 0
    return replace(changed, focus=after[min(index, len(after) - 1)], hidden_focus=remembered or previous.focus,
                   inspector_scroll=0)


def hidden_selections(state: State) -> dict[str, bool]:
    """Whether the Preferred or pinned route is currently hidden by filters, search or grouping."""
    shown = set(visible_keys(state))
    prefs = preferences(state)
    return {name: bool(prefs.get(name)) and prefs.get(name) not in shown for name in ("preferred", "pinned")}


def tabs(state: State) -> tuple[str, ...]:
    return TABS + (("compare",) if state.compare else ())


# --------------------------------------------------------------------------- commands

COMMANDS = (
    ("refresh", "Refresh public model data now", "R"),
    ("toggle", "Enable or disable the focused route", "Space"),
    ("enable", "Enable the focused route", ""),
    ("disable", "Disable the focused route", ""),
    ("unset", "Set the focused route to not set", ""),
    ("preferred", "Make the focused route Preferred, or clear it", "P"),
    ("clear_preferred", "Clear the Preferred route", ""),
    ("pin", "Pin the focused route, or unpin it", "p"),
    ("clear_pin", "Clear the Pin", ""),
    ("bulk", "Bulk edit routes with a preview", "b"),
    ("reset", "Reset routes to the shipped defaults with a preview", ""),
    ("refresh_setting", "Switch automatic data refresh on or off", ""),
    ("setup", "Open the route setup for an earlier preference file", ""),
    *((f"sort:{name}", f"Sort by {SORT_LABELS[name]}", "s") for name in SORTS),
    ("reverse", "Reverse the sort direction", "S"),
    ("grouped", "Switch between ranked and model-grouped views", "g"),
    ("collapse", "Collapse or expand the focused model group", "Enter"),
    ("provider", "Filter by provider (cycle)", "f"),
    ("model", "Filter by model (cycle)", ""),
    ("effort", "Filter by effort (cycle)", ""),
    ("state", "Filter by route state (cycle)", ""),
    ("discovery", "Show supported, new or unsupported observations (cycle)", "o"),
    ("clear_filters", "Clear filters and search", "F"),
    ("search", "Search by name, model id or route key", "/"),
    ("compare", "Add or remove the focused route from comparison", "c"),
    ("compare_clear", "Clear the comparison", "C"),
    ("compare_show", "Show the comparison tab", ""),
    ("frontier", "Show or hide the AA efficiency frontier marker", "e"),
    ("changes", "Show changes since the previous snapshot", ""),
    ("inspector", "Move focus between table and inspector", "Tab"),
    ("next_tab", "Next inspector tab", "]"),
    ("previous_tab", "Previous inspector tab", "["),
    ("help", "Help, keys and units", "?"),
    ("quit", "Quit", "q"),
)


def palette_matches(state: State) -> list[tuple[str, str, str]]:
    words = state.palette_query.casefold().split()
    return [command for command in COMMANDS
            if all(word in (command[1] + " " + command[0] + " " + command[2]).casefold() for word in words)]


def _cycle(options: tuple | list, current: object) -> object:
    options = list(options)
    return options[(options.index(current) + 1) % len(options)] if current in options else options[0]


def _view(state: State, **changes) -> State:
    return _keep_focus(state, replace(state, **changes))


NO_ROW = "No row is shown, so nothing is focused; F clears filters and search; no change saved"
ROW_COMMANDS = ("toggle", "enable", "disable", "unset", "preferred", "pin", "compare")


def _refused(state: State) -> State | None:
    prefs = preferences(state)
    if editable(state):
        return None
    if prefs.get("status") == "setup_required":
        return with_notice(state, "Route setup required before edits — open it with : setup; no change saved")
    reason = ((prefs.get("errors") or [{}])[0].get("message") or "preferences unavailable")
    return with_notice(state, f"Preferences unavailable — read-only ({reason}); no change saved")


def _route_edit(state: State, row: dict | None, target: str | None) -> tuple[State, Effect | None]:
    refused = _refused(state)
    if refused is not None:
        return refused, None
    if row is None:
        return with_notice(state, "Focus a supported route; observations are not routable; no change saved"), None
    if row["state"] == (target or "not_set"):
        return with_notice(state, f"{row['key']} is already {(target or 'not set').replace('_', ' ')}"), None
    label = {"enabled": "Enabled", "disabled": "Disabled", None: "Set to not set"}[target]
    return state, Effect("edit", {"routes": {row["key"]: target}}, f"{label} {row['key']}")


def _selection_edit(state: State, row: dict | None, field_name: str, clear: bool = False) -> tuple[State, Effect | None]:
    refused = _refused(state)
    if refused is not None:
        return refused, None
    current = preferences(state).get(field_name)
    label = "Pin" if field_name == "pinned" else "Preferred"
    if clear:
        if current is None:
            return with_notice(state, f"No {label} to clear"), None
        return state, Effect("edit", {field_name: None}, f"Cleared {label} {current}")
    if row is None:
        return with_notice(state, "Focus a supported route; observations are not routable; no change saved"), None
    if current == row["key"]:
        return state, Effect("edit", {field_name: None}, f"Cleared {label} {row['key']}")
    return state, Effect("edit", {field_name: row["key"]}, f"{label} is now {row['key']}")


def bulk_changes(state: State, bulk: Bulk) -> dict[str, str]:
    """The exact routes this bulk action changes, with their new state; never a wildcard."""
    if bulk.scope == "model":
        candidates = group_members(state, bulk.model)
    elif bulk.scope == "range":
        low, high = sorted((bulk.low, bulk.high))
        candidates = [row for row in shown_routes(state) if low <= effort_index(row["effort"]) <= high]
    elif bulk.scope == "not_set":
        candidates = [row for row in shown_routes(state) if row["state"] == "not_set"]
    else:
        candidates = routes(state)
    action = "enabled" if bulk.scope == "reset" else bulk.action
    return {row["key"]: action for row in candidates if row["state"] != action}


def bulk_conflicts(state: State, bulk: Bulk) -> dict[str, str]:
    """Pin or Preferred routes this action would disable, which must be cleared in the same save."""
    changed = bulk_changes(state, bulk)
    prefs = preferences(state)
    return {name: prefs[name] for name in ("pinned", "preferred")
            if prefs.get(name) and changed.get(prefs[name]) == "disabled"}


def bulk_fields(bulk: Bulk, conflicts: dict) -> list[str]:
    fields = ["scope"] + ([] if bulk.scope == "reset" else ["action"])
    if bulk.scope == "range":
        fields += ["low", "high"]
    fields += [name for name in ("pinned", "preferred") if name in conflicts]
    return fields


def _displayed(state: State) -> dict:
    """The preference values this screen shows, in the shape the targeted compare-and-swap reads."""
    prefs = preferences(state)
    return {"revision": prefs.get("revision"),
            "routes": {row["key"]: row["state"] for row in routes(state) if row["state"] in ("enabled", "disabled")},
            **{name: prefs.get(name) for name in ("pinned", "preferred", "refresh", "max_active")}}


def bulk_preview(state: State, bulk: Bulk) -> Bulk:
    """Capture the exact preview for the current data: changes, prior states, clear targets, values."""
    changed = bulk_changes(state, bulk)
    return replace(bulk, changes=changed,
                   before={key: (route(state, key) or {}).get("state", "not_set") for key in changed},
                   conflicts=bulk_conflicts(state, bulk), displayed=_displayed(state))


def bulk_stale(state: State) -> bool:
    """Whether the data changed so that the shown bulk preview is no longer what Enter would save."""
    bulk = state.bulk
    if bulk is None:
        return False
    live = bulk_preview(state, bulk)
    return (live.changes, live.before, live.conflicts) != (bulk.changes, bulk.before, bulk.conflicts)


def _rebuilt(state: State, shown: Bulk, changed: Bulk) -> Bulk:
    """A fresh preview of `changed`; consent to clear survives only for the target `shown` named."""
    fresh = bulk_preview(state, changed)
    return replace(fresh, scroll=0,
                   clear_pin=changed.clear_pin and fresh.conflicts.get("pinned") == shown.conflicts.get("pinned"),
                   clear_preferred=(changed.clear_preferred
                                    and fresh.conflicts.get("preferred") == shown.conflicts.get("preferred")))


def reopen_bulk(state: State, bulk: Bulk, message: str) -> State:
    """Show a fresh preview after a refused bulk save; consent to clear survives only for the same target."""
    refused = _refused(state)
    if refused is not None:
        return with_notice(replace(refused, mode="browse", bulk=None), message, error=True)
    return with_notice(replace(state, mode="bulk", bulk=_rebuilt(state, bulk, bulk)), message, error=True)


CHANGED_ELSEWHERE = "Not saved - changed elsewhere; review again; file unchanged"


def _open_bulk(state: State, scope: str = "model") -> State:
    refused = _refused(state)
    if refused is not None:
        return refused
    item = focused_item(state)
    if scope == "model" and item is None:
        return with_notice(state, NO_ROW)
    model = item.route["model"] if item and item.kind == "route" else (item.model if item and item.kind == "group"
                                                                        else (model_order(state) or [None])[0])
    return replace(state, mode="bulk", bulk=bulk_preview(state, Bulk(scope=scope, model=model)))


def _bulk_key(state: State, key: str) -> tuple[State, Effect | None]:
    bulk = state.bulk
    conflicts = bulk.conflicts
    fields = bulk_fields(bulk, conflicts)
    current = fields[min(bulk.field, len(fields) - 1)]
    if key == "ESC":
        return replace(state, mode="browse", bulk=None, notice="Bulk edit cancelled; no change saved"), None
    if key in ("DOWN", "TAB", "j"):
        return replace(state, bulk=replace(bulk, field=(fields.index(current) + 1) % len(fields))), None
    if key in ("UP", "k"):
        return replace(state, bulk=replace(bulk, field=(fields.index(current) - 1) % len(fields))), None
    if key in ("PAGE_DOWN", "PAGE_UP"):
        step = state.inspector_page if key == "PAGE_DOWN" else -state.inspector_page
        return replace(state, bulk=replace(bulk, scroll=max(0, bulk.scroll + step))), None
    if key in ("LEFT", "RIGHT", "SPACE", "h", "l"):
        step = -1 if key in ("LEFT", "h") else 1
        if current in ("pinned", "preferred"):
            # Consent to clear applies to the shown target only, so it never rebuilds the preview.
            name = "clear_pin" if current == "pinned" else "clear_preferred"
            return replace(state, bulk=replace(bulk, **{name: not getattr(bulk, name)})), None
        if current == "scope":
            changed = replace(bulk, scope=BULK_SCOPES[(BULK_SCOPES.index(bulk.scope) + step) % len(BULK_SCOPES)],
                              field=0, scroll=0, clear_pin=False, clear_preferred=False)
        elif current == "action":
            changed = replace(bulk, action="disabled" if bulk.action == "enabled" else "enabled",
                              clear_pin=False, clear_preferred=False, scroll=0)
        else:
            # A range edit rebuilds the preview from live data, so consent follows only an unchanged target.
            changed = replace(bulk, **{current: max(0, min(len(EFFORTS) - 1, getattr(bulk, current) + step))})
            return replace(state, bulk=_rebuilt(state, bulk, changed)), None
        return replace(state, bulk=bulk_preview(state, changed)), None
    if key == "ENTER":
        if bulk_stale(state):
            return reopen_bulk(state, bulk, CHANGED_ELSEWHERE), None
        changed = bulk.changes
        if not changed:
            return with_notice(state, "Nothing to change for this scope; no change saved"), None
        blocked = [name for name in conflicts if not (bulk.clear_pin if name == "pinned" else bulk.clear_preferred)]
        if blocked:
            names = " and ".join(("Pin " if name == "pinned" else "Preferred ") + conflicts[name] for name in blocked)
            return with_notice(state, f"Not saved — this disables {names}; choose to clear it in this action "
                                      "or narrow the scope", error=True), None
        payload = {"routes": dict(changed)}
        if bulk.clear_pin and "pinned" in conflicts:
            payload["pinned"] = None
        if bulk.clear_preferred and "preferred" in conflicts:
            payload["preferred"] = None
        label = ("Reset" if bulk.scope == "reset" else "Bulk") + f" saved {len(changed)} routes"
        return replace(state, mode="browse", bulk=None), Effect("edit", payload, label, displayed=bulk.displayed,
                                                                bulk=bulk)
    return state, None


def setup_options(state: State) -> tuple[list[str], list[str]]:
    """Pin options (exact routes of the earlier pin) and Preferred options (enabled preview routes)."""
    preview = state.setup.preview if state.setup else {}
    pin = next((row for row in preview.get("choices", []) if row.get("id") == "pin"), None)
    enabled = [key for key, value in ((preview.get("document") or {}).get("routes") or {}).items()
               if value == "enabled"]
    return (list(pin["options"]) if pin else []), enabled


def setup_choices(state: State) -> dict | None:
    """The explicit choices for the apply call, or None while a required choice is missing."""
    setup = state.setup
    pins, preferred = setup_options(state)
    needs_pin = any(row.get("id") == "pin" for row in setup.preview.get("choices", []))
    choices = {}
    if needs_pin:
        if setup.pin < 0:
            return None
        choices["pin"] = pins[setup.pin] if setup.pin < len(pins) else None
    choices["preferred"] = preferred[setup.preferred - 1] if setup.preferred else None
    return choices


def _setup_key(state: State, key: str) -> tuple[State, Effect | None]:
    setup = state.setup
    pins, preferred = setup_options(state)
    fields = (["pin"] if any(row.get("id") == "pin" for row in setup.preview.get("choices", [])) else []) + ["preferred"]
    current = fields[min(setup.field, len(fields) - 1)]
    if setup.confirming:
        if key in ("y", "Y"):
            return replace(state, setup=replace(setup, confirming=False)), Effect(
                "setup_apply", {"choices": setup_choices(state), "revision": setup.preview.get("revision")},
                "Route setup saved")
        if key in ("n", "N", "ESC"):
            return replace(state, setup=replace(setup, confirming=False)), None
        return state, None
    if key == "ESC":
        return replace(state, mode="browse", notice="Route setup not saved; delegation stays blocked until it is"), None
    if key in ("DOWN", "TAB", "j"):
        return replace(state, setup=replace(setup, field=(fields.index(current) + 1) % len(fields))), None
    if key in ("UP", "k"):
        return replace(state, setup=replace(setup, field=(fields.index(current) - 1) % len(fields))), None
    if key in ("PAGE_DOWN", "PAGE_UP"):
        step = state.inspector_page if key == "PAGE_DOWN" else -state.inspector_page
        return replace(state, setup=replace(setup, scroll=max(0, setup.scroll + step))), None
    if key in ("LEFT", "RIGHT", "SPACE", "h", "l"):
        step = -1 if key in ("LEFT", "h") else 1
        if current == "pin":
            count = len(pins) + 1
            position = setup.pin if setup.pin >= 0 else (-1 if step > 0 else count)
            return replace(state, setup=replace(setup, pin=(position + step) % count)), None
        return replace(state, setup=replace(setup, preferred=(setup.preferred + step) % (len(preferred) + 1))), None
    if key == "ENTER":
        if setup_choices(state) is None:
            return with_notice(state, "Choose an exact effort for the earlier pin, or choose to clear it"), None
        return replace(state, setup=replace(setup, confirming=True)), None
    return state, None


def _palette_key(state: State, key: str) -> tuple[State, Effect | None]:
    matches = palette_matches(state)
    if key == "ESC":
        return replace(state, mode="browse", palette_query="", palette_index=0, palette_basis=None), None
    if key in ("UP", "DOWN"):
        if not matches:
            return state, None
        step = -1 if key == "UP" else 1
        return replace(state, palette_index=(state.palette_index + step) % len(matches)), None
    if key == "ENTER":
        if not matches:
            return state, None
        command = matches[min(state.palette_index, len(matches) - 1)][0]
        closed = replace(state, mode="browse", palette_query="", palette_index=0, palette_basis=None)
        if _palette_moved(state, command):
            return with_notice(closed, CHANGED_ELSEWHERE, error=True), None
        return run_command(closed, command)
    if key == "BACKSPACE":
        return replace(state, palette_query=state.palette_query[:-1], palette_index=0), None
    text = " " if key == "SPACE" else key
    if len(text) == 1 and text.isprintable() and len(state.palette_query) < MAX_QUERY:
        return replace(state, palette_query=state.palette_query + text, palette_index=0), None
    return state, None


# Palette commands that write preferences or act on the focused row the palette covers.
WRITE_COMMANDS = ("toggle", "enable", "disable", "unset", "preferred", "pin", "clear_preferred", "clear_pin",
                  "refresh_setting")


def _palette_moved(state: State, command: str) -> bool:
    """Whether a writing or row command would act on preferences or a focus the user did not see."""
    if state.palette_basis is None:
        return False
    shown, focus = state.palette_basis
    return (command in ROW_COMMANDS and focus != state.focus
            or command in WRITE_COMMANDS and shown != preferences(state))


def run_command(state: State, command: str) -> tuple[State, Effect | None]:
    """One named action, shared by the palette and the shortcut keys."""
    row = focused_route(state)
    item = focused_item(state)
    if command == "quit":
        return state, Effect("quit")
    if command in ROW_COMMANDS and item is None:
        return with_notice(state, NO_ROW), None
    if command == "refresh":
        if state.refresh_state == "running":
            return with_notice(state, "A refresh is already running"), None
        return with_refresh(state, "running"), Effect("refresh")
    if command == "toggle":
        if row is None:
            return _route_edit(state, None, None)
        return _route_edit(state, row, "disabled" if row["state"] == "enabled" else "enabled")
    if command in ("enable", "disable", "unset"):
        return _route_edit(state, row, {"enable": "enabled", "disable": "disabled", "unset": None}[command])
    if command in ("preferred", "pin"):
        return _selection_edit(state, row, "preferred" if command == "preferred" else "pinned")
    if command in ("clear_preferred", "clear_pin"):
        return _selection_edit(state, row, "preferred" if command == "clear_preferred" else "pinned", clear=True)
    if command == "bulk":
        return _open_bulk(state), None
    if command == "reset":
        return _open_bulk(state, "reset"), None
    if command == "refresh_setting":
        refused = _refused(state)
        if refused is not None:
            return refused, None
        target = "manual" if preferences(state).get("refresh") == "automatic" else "automatic"
        return state, Effect("edit", {"refresh": target}, f"Automatic data refresh {'off' if target == 'manual' else 'on'}")
    if command == "setup":
        if state.setup is None:
            return with_notice(state, "Route setup is only needed for an earlier pod/v1 preference file"), None
        return replace(state, mode="setup"), None
    if command.startswith("sort:"):
        name = command.split(":", 1)[1]
        return _view(state, sort=name, descending=name in DESCENDING_FIRST, table_scroll=0), None
    if command == "reverse":
        return _view(state, descending=not state.descending), None
    if command == "grouped":
        return _view(state, grouped=not state.grouped), None
    if command == "collapse":
        if item is None or item.kind != "group":
            if row is not None and state.grouped:
                return _view(state, collapsed=state.collapsed | {row["model"]}, focus="model:" + row["model"]), None
            return state, None
        key = item.model
        collapsed = state.collapsed - {key} if key in state.collapsed else state.collapsed | {key}
        return _view(state, collapsed=collapsed), None
    if command == "provider":
        return _view(state, provider=_cycle(PROVIDERS, state.provider)), None
    if command == "model":
        return _view(state, model=_cycle(["all", *model_order(state)], state.model)), None
    if command == "effort":
        return _view(state, effort=_cycle(("all", *EFFORTS), state.effort)), None
    if command == "state":
        return _view(state, state_filter=_cycle(STATES, state.state_filter)), None
    if command == "discovery":
        return _view(state, discovery=_cycle(DISCOVERY, state.discovery)), None
    if command == "clear_filters":
        return _view(state, provider="all", model="all", effort="all", state_filter="all", discovery="supported",
                     query=""), None
    if command == "search":
        return replace(state, mode="search", query=""), None
    if command == "compare":
        if row is None:
            return with_notice(state, "Only supported routes can be compared"), None
        if row["key"] in state.compare:
            remaining = tuple(key for key in state.compare if key != row["key"])
            return replace(state, compare=remaining, tab=state.tab if remaining or state.tab != "compare"
                           else "details"), None
        if len(state.compare) >= MAX_COMPARE:
            return with_notice(state, f"Compare holds up to {MAX_COMPARE} routes; press c on one to remove it"), None
        return replace(state, compare=state.compare + (row["key"],)), None
    if command == "compare_clear":
        return replace(state, compare=(), tab="details" if state.tab == "compare" else state.tab), None
    if command == "compare_show":
        if not state.compare:
            return with_notice(state, "Mark routes with c to compare them"), None
        return replace(state, tab="compare", inspector_scroll=0), None
    if command == "frontier":
        return replace(state, frontier=not state.frontier), None
    if command == "changes":
        return replace(state, tab="benchmarks", inspector_scroll=0), None
    if command == "inspector":
        return replace(state, pane="inspector" if state.pane == "table" else "table"), None
    if command in ("next_tab", "previous_tab"):
        names = tabs(state)
        index = names.index(state.tab) if state.tab in names else 0
        step = 1 if command == "next_tab" else -1
        return replace(state, tab=names[(index + step) % len(names)], inspector_scroll=0), None
    if command == "help":
        return replace(state, mode="help", help_scroll=0), None
    return state, None


SHORTCUTS = {" ": "toggle", "SPACE": "toggle", "R": "refresh", "P": "preferred", "p": "pin", "b": "bulk",
             "s": None, "S": "reverse", "g": "grouped", "f": "provider", "o": "discovery", "F": "clear_filters",
             "/": "search", "c": "compare", "C": "compare_clear", "e": "frontier", "TAB": "inspector",
             "]": "next_tab", "[": "previous_tab", "?": "help", "q": "quit"}


def _move(state: State, step: int, *, absolute: int | None = None) -> State:
    keys = visible_keys(state)
    if not keys:
        return state
    index = keys.index(state.focus) if state.focus in keys else 0
    target = absolute if absolute is not None else index + step
    target = max(0, min(len(keys) - 1, target))
    if keys[target] == state.focus:
        return state
    return replace(state, focus=keys[target], hidden_focus=None, inspector_scroll=0)


def reduce(state: State, key: str) -> tuple[State, Effect | None]:
    """Apply one key. Afterwards focus always rests on a shown row whenever any row is shown."""
    changed, effect = _reduce(state, key)
    if changed.focus not in visible_keys(changed):
        changed = _keep_focus(state, changed)
    return changed, effect


def _reduce(state: State, key: str) -> tuple[State, Effect | None]:
    if key == "CTRL_C":
        return state, Effect("quit")
    if key == "RESIZE":
        return state, None
    if state.mode == "palette":
        return _palette_key(state, key)
    if state.mode == "bulk" and state.bulk is not None:
        return _bulk_key(state, key)
    if state.mode == "setup" and state.setup is not None:
        if key == "q" and not state.setup.confirming:
            return state, Effect("quit")
        return _setup_key(state, key)
    if state.mode == "search":
        if key in ("ESC",):
            return _view(state, mode="browse", query=""), None
        if key == "ENTER":
            return replace(state, mode="browse"), None
        if key == "BACKSPACE":
            return _view(state, query=state.query[:-1]), None
        text = " " if key == "SPACE" else key
        if len(text) == 1 and text.isprintable() and len(state.query) < MAX_QUERY:
            return _view(state, query=state.query + text), None
        return state, None
    if state.mode == "help":
        if key in ("ESC", "?", "q"):
            return replace(state, mode="browse"), None if key != "q" else Effect("quit")
        if key in ("UP", "k", "DOWN", "j", "PAGE_UP", "PAGE_DOWN", "HOME"):
            step = {"UP": -1, "k": -1, "DOWN": 1, "j": 1, "PAGE_UP": -state.inspector_page,
                    "PAGE_DOWN": state.inspector_page, "HOME": -10_000}[key]
            return replace(state, help_scroll=max(0, state.help_scroll + step)), None
        return state, None
    if key in ("CTRL_P", ":"):
        return replace(state, mode="palette", palette_query="", palette_index=0,
                       palette_basis=(preferences(state), state.focus)), None
    if key == "ESC":
        if state.pane == "inspector":
            return replace(state, pane="table"), None
        if state.query:
            return _view(state, query=""), None
        return replace(state, notice=""), None
    if key == "s":
        return run_command(state, "sort:" + SORTS[(SORTS.index(state.sort) + 1) % len(SORTS)])
    if key == "ENTER":
        item = focused_item(state)
        if item is not None and item.kind == "group":
            return run_command(state, "collapse")
        return replace(state, pane="inspector"), None
    if state.pane == "inspector":
        if key in ("LEFT", "h"):
            return run_command(state, "previous_tab")
        if key in ("RIGHT", "l"):
            return run_command(state, "next_tab")
        steps = {"UP": -1, "k": -1, "DOWN": 1, "j": 1, "PAGE_UP": -state.inspector_page,
                 "PAGE_DOWN": state.inspector_page}
        if key in steps:
            return replace(state, inspector_scroll=max(0, state.inspector_scroll + steps[key])), None
        if key == "HOME":
            return replace(state, inspector_scroll=0), None
    else:
        steps = {"UP": -1, "k": -1, "DOWN": 1, "j": 1, "PAGE_UP": -state.table_page,
                 "PAGE_DOWN": state.table_page}
        if key in steps:
            return _move(state, steps[key]), None
        if key == "HOME":
            return _move(state, 0, absolute=0), None
        if key == "END":
            return _move(state, 0, absolute=len(visible_keys(state)) - 1), None
        if key in ("LEFT", "RIGHT") and state.grouped:
            item = focused_item(state)
            model = item.model if item and item.kind == "group" else (item.route["model"] if item and item.route else None)
            if item is not None and (item.kind == "group" or key == "LEFT"):
                collapsed = state.collapsed | {model} if key == "LEFT" else state.collapsed - {model}
                return _view(state, collapsed=collapsed, focus=("model:" + model) if key == "LEFT" and model
                             else state.focus), None
    command = SHORTCUTS.get(key)
    if command:
        return run_command(state, command)
    return state, None
