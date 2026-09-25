"""Pure model-table state transitions; no I/O or terminal calls."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from .catalog import IDS, reference_rows

SORTS = ("Recommended", "Model", "Intelligence", "Benchmark cost", "First response")
FILTERS = ("All", "Claude", "Codex")
NEXT_STATE = {"available": "preferred", "preferred": "disabled", "disabled": "available"}


@dataclass(frozen=True)
class State:
    catalog: dict
    preferences: dict
    focus_id: str
    hidden_focus_id: str | None = None
    sort_index: int = 0
    filter_index: int = 0
    query: str = ""
    searching: bool = False
    expanded: bool = False
    help_open: bool = False
    help_scroll: int = 0
    detail_scroll: int = 0
    notice: str = ""
    runtime: str = "Unknown"
    save_at: datetime | None = None


@dataclass(frozen=True)
class Effect:
    kind: str
    model_id: str | None = None
    value: str | None = None


def initial(catalog: dict, preferences: dict) -> State:
    return State(catalog=catalog, preferences=preferences, focus_id=IDS[0])


def _entries(state: State) -> dict[str, dict]:
    return {row["id"]: row for row in state.catalog["models"]}


def visible_ids(state: State) -> list[str]:
    entries = _entries(state)
    query = state.query.casefold()
    filter_name = FILTERS[state.filter_index]
    candidates = [model_id for model_id in IDS
                  if (filter_name == "All" or entries[model_id]["agent"] == filter_name.casefold())
                  and (not query or query in entries[model_id]["name"].casefold()
                       or query in model_id.casefold()
                       or query in entries[model_id]["agent"].casefold())]
    rows = reference_rows(state.catalog)
    if state.sort_index == 0:
        return sorted(candidates, key=lambda model_id: (entries[model_id]["guide"]["coding_order"], IDS.index(model_id)))
    if state.sort_index == 1:
        return sorted(candidates, key=lambda model_id: (entries[model_id]["name"].casefold(), IDS.index(model_id)))
    if state.sort_index == 2:
        return sorted(candidates, key=lambda model_id: (rows[model_id]["intelligence"] is None,
                                                        -(rows[model_id]["intelligence"] or 0),
                                                        IDS.index(model_id)))
    key = "usd_per_task" if state.sort_index == 3 else "first_chunk_s"
    return sorted(candidates, key=lambda model_id: (rows[model_id][key] is None,
                                                    rows[model_id][key] if rows[model_id][key] is not None else 0,
                                                    IDS.index(model_id)))


def refresh(state: State, preferences: dict) -> State:
    """External preference reads keep focus identity and presentation choices."""
    return replace(state, preferences=preferences)


def _visible_focus(previous: State, changed: State) -> State:
    """Keep a visible focus while remembering the identity hidden by a view change."""
    before, after = visible_ids(previous), visible_ids(changed)
    remembered = previous.hidden_focus_id
    if remembered in after:
        return replace(changed, focus_id=remembered, hidden_focus_id=None)
    if changed.focus_id in after:
        return changed
    if not after:
        return replace(changed, hidden_focus_id=remembered or previous.focus_id)
    index = before.index(previous.focus_id) if previous.focus_id in before else 0
    return replace(changed, focus_id=after[min(index, len(after) - 1)],
                   hidden_focus_id=remembered or previous.focus_id)


def reduce(state: State, key: str) -> tuple[State, Effect | None]:
    if key == "CTRL_C":
        return state, Effect("quit")
    if state.searching:
        if key == "ESC":
            return _visible_focus(state, replace(state, searching=False, query="", notice="")), None
        if key == "ENTER":
            return state if not state.searching else replace(state, searching=False), None
        if key == "BACKSPACE":
            return _visible_focus(state, replace(state, query=state.query[:-1])), None
        if key == "SPACE" and len(state.query) < 80:
            return _visible_focus(state, replace(state, query=state.query + " ")), None
        if len(key) == 1 and key.isprintable() and len(state.query) < 80:
            return _visible_focus(state, replace(state, query=state.query + key)), None
        return state, None
    if key == "q":
        return state, Effect("quit")
    if key == "ESC":
        if state.help_open:
            return replace(state, help_open=False), None
        if state.expanded:
            return replace(state, expanded=False, detail_scroll=0), None
        if state.query:
            return _visible_focus(state, replace(state, query="", notice="")), None
        return state, None
    if key == "?":
        return replace(state, help_open=not state.help_open, help_scroll=0), None
    if state.help_open:
        if key in ("UP", "k"):
            return replace(state, help_scroll=max(0, state.help_scroll - 1)), None
        if key in ("DOWN", "j"):
            return replace(state, help_scroll=min(40, state.help_scroll + 1)), None
        if key in ("PAGE_DOWN", "PAGE_UP"):
            return replace(state, help_scroll=max(0, state.help_scroll + (8 if key == "PAGE_DOWN" else -8))), None
        return state, None
    if key == "/":
        return replace(state, searching=True, query=""), None
    if key == "ENTER":
        return replace(state, expanded=True, detail_scroll=0), None
    if key in ("PAGE_DOWN", "PAGE_UP"):
        return replace(state, detail_scroll=max(0, state.detail_scroll + (8 if key == "PAGE_DOWN" else -8))), None
    if key == "s":
        return replace(state, sort_index=(state.sort_index + 1) % len(SORTS)), None
    if key == "f":
        return _visible_focus(state, replace(state, filter_index=(state.filter_index + 1) % len(FILTERS))), None
    if key in ("UP", "DOWN", "j", "k"):
        visible = visible_ids(state)
        if not visible:
            return state, None
        direction = -1 if key in ("UP", "k") else 1
        index = visible.index(state.focus_id) if state.focus_id in visible else (-1 if direction > 0 else 0)
        index = (index + direction) % len(visible)
        return replace(state, focus_id=visible[index], hidden_focus_id=None), None
    if key in ("SPACE", "r"):
        if state.preferences["errors"]:
            return replace(state, notice="Preferences unavailable — read-only; no change saved"), None
        if key == "r":
            target = "custom" if state.preferences["mode"] == "all" else "all"
            return state, Effect("set_mode", value=target)
        if not visible_ids(state):
            return replace(state, notice="No models match — Esc clears; no change saved"), None
        if state.focus_id not in visible_ids(state):
            return replace(state, notice="Choose a visible model; no change saved"), None
        focused = state.focus_id
        current = state.preferences["saved"].get(focused)
        if current is None:
            return replace(state, focus_id=focused), Effect("set_model", model_id=focused,
                                                            value="available")
        if current not in NEXT_STATE:
            return replace(state, notice="Model state is invalid — read-only; no change saved"), None
        return replace(state, focus_id=focused), Effect("set_model", model_id=focused,
                                                        value=NEXT_STATE[current])
    return state, None


def with_notice(state: State, message: str, *, save_at: datetime | None = None) -> State:
    return replace(state, notice=message, save_at=save_at)


def with_runtime(state: State, label: str) -> State:
    if label not in ("Unknown", "Offline", "Unsupported", "Not checked"):
        raise ValueError("Unsupported runtime label")
    return replace(state, runtime=label)
