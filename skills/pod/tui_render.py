"""Pure workspace frames: a ranked route table above a full-width inspector dock.

Every line is built to fit the drawable width. `frame(strict=True)` raises on an overflow so tests
can prove the layout for every supported size; the interactive path clips instead of crashing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import tui_state as ts
from .observations import STALE_S
from .term import Capabilities, clip, display_width, elide_middle, glyph, pad, safe_text, wrap

DISCLAIMER = ("AA benchmark cost is not the user's subscription charge, quota consumption or Pod invoice; "
              "first/total benchmark response is not worker task duration.")
# The longest that fits is shown under the table whenever it has a cost or time column.
SHORT_DISCLAIMERS = (DISCLAIMER,
                     "AA $/task is not your subscription, quota or Pod invoice; benchmark time is not task "
                     "duration.",
                     "AA $/task is not your subscription, quota or Pod invoice; time is not task time",
                     "AA $ is not your bill, quota or Pod invoice; time not task time",
                     "AA $ not your bill/quota; time not task")
# Metrics that read like the user's own cost or wait, so the table never shows them without a disclaimer.
COST_AND_TIME = ("usd_per_task", "first_response_s", "total_response_s")
# AA profile qualifiers marked after the measured values they qualify.
QUALIFIER_MARKS = (("estimated index", "estimated"), ("with fallback", "fallback"))
WIDE, NORMAL = 140, 80
MIN_COLUMNS, MIN_ROWS = 40, 12
TAB_LABELS = {"details": ("Details", "Det"), "benchmarks": ("Benchmarks", "Bench"),
              "routing": ("Routing", "Route"), "sources": ("Sources", "Src"), "compare": ("Compare", "Cmp")}
STATE_TEXT = {"enabled": "enabled", "disabled": "disabled", "not_set": "not set"}
UNITS = {"intelligence": "points on the AA Intelligence Index", "usd_per_task": "USD per AA benchmark task",
         "output_tps": "median output tokens per second", "first_response_s": "seconds to first response",
         "total_response_s": "seconds to total response", "context_tokens": "tokens"}
METRIC_LABELS = {"intelligence": "AA index", "usd_per_task": "AA $/task", "output_tps": "Output speed",
                 "first_response_s": "First response", "total_response_s": "Total response",
                 "context_tokens": "Context window"}


@dataclass(frozen=True)
class Span:
    text: str
    role: str = "body"


@dataclass(frozen=True)
class Line:
    text: str
    role: str = "body"
    spans: tuple[Span, ...] = ()

    @property
    def styled(self) -> tuple[Span, ...]:
        return self.spans or (Span(self.text, self.role),)


@dataclass(frozen=True)
class Frame:
    lines: tuple[Line, ...]
    columns: int
    rows: int
    table_scroll: int = 0
    table_page: int = 1
    inspector_scroll: int = 0
    inspector_page: int = 1
    help_scroll: int = 0
    preset: str = "normal"

    @property
    def plain(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    width: int
    right: bool = False


def _line(*parts: tuple[str, str]) -> Line:
    spans = tuple(Span(text, role) for text, role in parts if text)
    return Line("".join(span.text for span in spans), spans=spans)


def _fit(value: object, width: int, caps: Capabilities) -> str:
    return clip(safe_text(value, caps), max(0, width), ellipsis=True, ascii_only=caps.ascii_only)


def _clip_line(line: Line, width: int, caps: Capabilities) -> Line:
    """Cut a styled line at the drawable width, keeping each span's role."""
    if display_width(line.text) <= width:
        return line
    parts, used = [], 0
    for span in line.styled:
        room = width - used
        if room <= 0:
            break
        text = clip(span.text, room, ellipsis=display_width(span.text) > room, ascii_only=caps.ascii_only)
        parts.append((text, span.role))
        used += display_width(text)
    return _line(*parts)


def _safe(parts: list[tuple[str, str]], caps: Capabilities) -> list[tuple[str, str]]:
    return [(safe_text(text, caps), role) for text, role in parts]


def _wrapped(value: object, width: int, caps: Capabilities, role: str = "body", indent: int = 0) -> list[Line]:
    rows = wrap(safe_text(value, caps), max(1, width - indent))
    return [Line(" " * indent + row, role) for row in rows]


def _labelled(label: str, text: object, width: int, caps: Capabilities, role: str = "body",
              label_width: int = 16) -> list[Line]:
    """A label column with wrapped text beside it, or stacked below it when narrow."""
    if width < 56:
        rows = wrap(safe_text(f"{label}: {text}", caps), max(1, width - 2))
        head = display_width(safe_text(label + ":", caps))
        first = rows[0]
        lines = [_line((first[:head], "label"), (first[head:], role))] if first.startswith(safe_text(label + ":", caps)) \
            else [Line(first, role)]
        return lines + [Line("  " + row, role) for row in rows[1:]]
    rows = wrap(safe_text(text, caps), max(1, width - label_width))
    return [_line((pad(label if index == 0 else "", label_width, ascii_only=caps.ascii_only), "label"), (row, role))
            for index, row in enumerate(rows)]


def _heading(title: str, width: int, caps: Capabilities) -> Line:
    return Line(_fit(title, width, caps), "heading")


# --------------------------------------------------------------------------- values

def number(name: str, value: object, caps: Capabilities) -> str:
    if value is None:
        return glyph(caps, "dash")
    if name == "intelligence":
        return f"{value:g}"
    if name == "usd_per_task":
        return "$0.00" if value == 0 else f"${value:.2f}" if value >= 0.01 else f"${value:.2g}"
    if name == "output_tps":
        return f"{value:.0f}"
    if name == "context_tokens":
        for size, suffix in ((1_000_000, "M"), (1_000, "k")):
            if value >= size:
                return f"{value / size:.3g}{suffix}"
        return str(value)
    return f"{value:.1f}" if value < 100 else f"{value:.0f}"


def _qualifiers(row: dict) -> list[str]:
    metric = (row.get("metrics") or {}).get("intelligence")
    if isinstance(metric, dict):
        return list(metric.get("qualifiers") or [])
    return list(row.get("qualifiers") or [])


def marks_for(qualifiers: list | tuple, name: str, caps: Capabilities) -> str:
    """The profile marks for one metric: ~ an AA estimated index, † (ASCII #) AA's "with fallback" run."""
    return "".join(glyph(caps, mark) for qualifier, mark in QUALIFIER_MARKS
                   if qualifier in qualifiers and (mark != "estimated" or name == "intelligence")
                   and name != "context_tokens")


def metric_text(row: dict, name: str, caps: Capabilities, *, reserve: bool = False) -> str:
    """A metric value with its AA profile marks; `reserve` pads the marks so table digits align."""
    found = ts.value(row, name)
    marks = marks_for(_qualifiers(row), name, caps) if found is not None else ""
    if reserve:
        room = 0 if name == "context_tokens" else 2 if name == "intelligence" else 1
        marks = marks + " " * max(0, room - display_width(marks))
    return number(name, found, caps) + marks


def _index_text(row: dict, caps: Capabilities) -> str:
    return metric_text(row, "intelligence", caps)


def _profile(row: dict) -> str:
    """AA's exact qualifier wording; "with fallback" describes AA's harness, never Pod fallback."""
    return ", ".join(_qualifiers(row))


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def span(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def age_text(value: object, now: datetime) -> str:
    moment = _when(value)
    if moment is None:
        return "unknown age"
    return span(max(0, int((now - moment).total_seconds()))) + " old"


def data_age(state: ts.State, now: datetime) -> dict:
    """The data's age at render time, so an open window keeps ageing and turns stale on its own.

    `age` is the oldest required source's age (None when one has no data), `stale` follows the
    seven-day rule, and `oldest` is the oldest kept data from any source, so mixed ages show.
    """
    seen = state.projection.get("observations") or {}
    blocks = seen.get("sources") or {}
    ages = {}
    for name, block in blocks.items():
        moment = _when(block.get("retrieved_at"))
        ages[name] = None if moment is None else max(0, int((now - moment).total_seconds()))
    required = [ages[name] for name, block in blocks.items() if block.get("required")]
    if required:
        age = None if any(item is None for item in required) else max(required)
    else:
        age = seen.get("age_s")
    kept = [ages[name] for name, block in blocks.items() if ages[name] is not None and block.get("rows")]
    oldest = max(kept) if kept else None
    mixed = oldest is not None and age is not None and span(oldest) != span(age)
    return {"age": age, "stale": age is None or age >= STALE_S, "oldest": oldest if mixed else None}


def _route_label(row: dict) -> str:
    return f"{row['name']} {row['effort']}"


def _shortened(name: str, room: int, strategy: str, caps: Capabilities) -> str:
    if strategy == "family" and " " in name:
        name = name.split(" ", 1)[1]   # "Claude Opus 5.5" -> "Opus 5.5": the family word goes first
    if strategy == "middle":
        return elide_middle(name, room, ascii_only=caps.ascii_only)
    return clip(name, room, ellipsis=True, ascii_only=caps.ascii_only)


def narrow_labels(rows: list[dict], width: int, caps: Capabilities) -> dict[str, str]:
    """Model+effort labels for a narrow column: the model name is shortened, never the effort.

    Full names are kept when they all fit; otherwise the first shortening that keeps every model
    distinct is used, so each label stays one exact route identity.
    """
    room = max(1, width - 1 - max((display_width(row["effort"]) for row in rows), default=0))
    names = {row["model"]: safe_text(row["name"], caps) for row in rows}
    chosen = names
    if any(display_width(name) > room for name in names.values()):
        for strategy in ("family", "full", "middle"):
            chosen = {model: _shortened(name, room, strategy, caps) for model, name in names.items()}
            if len(set(chosen.values())) == len(chosen):
                break
    return {row["key"]: f"{chosen[row['model']]} {row['effort']}" for row in rows}


# --------------------------------------------------------------------------- table

def preset(columns: int) -> str:
    return "wide" if columns >= WIDE else "normal" if columns >= NORMAL else "narrow"


def table_columns(state: ts.State, width: int) -> tuple[Column, ...]:
    """Fixed presets; the column used for sorting always stays visible."""
    marks = 6 if state.frontier else 5
    sort = state.sort
    metric = {"intelligence": Column("intelligence", "Index", 6, True),
              "usd_per_task": Column("usd_per_task", "$/task", 8, True),
              "output_tps": Column("output_tps", "Tok/s", 6, True),
              "first_response_s": Column("first_response_s", "First s", 8, True),
              "total_response_s": Column("total_response_s", "Total s", 8, True),
              "context_tokens": Column("context_tokens", "Context", 7, True)}
    kind = preset(width + 1)
    if kind == "wide":
        fixed = [Column("effort", "Effort", 6), Column("state", "State", 8), Column("provider", "Provider", 9),
                 *(metric[name] for name in ts.METRICS),
                 # Wide enough for "with fallback, estimated index"; the qualifier text is never cut.
                 Column("profile", "AA profile", len("with fallback, estimated index"))]
        used = marks + 1 + sum(column.width + 1 for column in fixed)
        name = Column("name", "Model", 22)
        room = width - used - name.width - 1
        # The route key is also in Details, so it appears here only when it has useful room.
        return (name, *fixed) + ((Column("key", "Route key", room),) if room >= 12 else ())
    elif kind == "normal":
        fixed = [Column("effort", "Effort", 6), Column("state", "State", 8), metric["intelligence"],
                 metric["usd_per_task"], metric["first_response_s"]]
        if sort in metric and metric[sort] not in fixed:
            fixed.append(metric[sort])
    else:
        fixed = [metric["intelligence"]] + ([metric["usd_per_task"]] if width >= 57 and sort != "usd_per_task" else [])
        if sort in metric and sort != "intelligence":
            fixed.append(metric[sort])
        elif sort == "state":
            fixed.insert(0, Column("state", "State", 8))
    used = marks + 1 + sum(column.width + 1 for column in fixed)
    name = Column("name", "Route" if kind == "narrow" else "Model", max(8, width - used))
    return (name, *fixed)


def _marks(state: ts.State, item: ts.Item, caps: Capabilities, frontier: dict) -> str:
    if item.kind == "group":
        return " " * (6 if state.frontier else 5)
    if item.kind == "observation":
        text = glyph(caps, "unknown") + "   " + " "
        return text + (" " if state.frontier else "")
    row = item.route
    state_mark = glyph(caps, row["state"])
    pin = glyph(caps, "pin") if row["pinned"] else " "
    preferred = glyph(caps, "preferred") if row["preferred"] else " "
    compare = str(state.compare.index(row["key"]) + 1) if row["key"] in state.compare else " "
    text = state_mark + pin + preferred + compare
    if state.frontier:
        mark = frontier.get(row["key"], "unknown")
        text += glyph(caps, "frontier") if mark == "frontier" else "?" if mark == "unknown" else " "
    return text + " "


def _cell(state: ts.State, item: ts.Item, column: Column, caps: Capabilities, grouped: bool,
          labels: dict | None = None) -> tuple[str, str]:
    row = item.route or item.observation or {}
    if column.key == "name":
        if item.kind == "group":
            members = ts.group_members(state, item.model) if item.model else []
            closed = item.model in state.collapsed
            marker = glyph(caps, "collapsed" if closed else "expanded")
            if item.model is None:
                count = len(ts.shown_observations(state))
                return f"{marker} New and unsupported observations ({count})", "heading"
            enabled = sum(member["state"] == "enabled" for member in members)
            return f"{marker} {ts.model_name(state, item.model)} ({enabled}/{len(members)} enabled)", "heading"
        if item.kind == "observation":
            return ("  " if grouped else "") + str(row.get("row")), "body"
        name = ("  " + row["effort"]) if grouped else row["name"]
        if column.header == "Route" and not grouped:
            name = (labels or {}).get(row["key"]) or _route_label(row)
        return name, "value"
    if item.kind == "group":
        return "", "body"
    if column.key == "effort":
        return str(row.get("effort") or glyph(caps, "dash")), "value"
    if column.key == "state":
        if item.kind == "observation":
            return str(row.get("discovery")), "advisory"
        return STATE_TEXT[row["state"]], "badge_" + row["state"]
    if column.key == "key":
        return str(row.get("key") if item.kind == "route" else row.get("source") or ""), "label"
    if column.key == "provider":
        return str(row.get("provider") or row.get("creator") or ""), "body"
    if column.key == "profile":
        return _profile(row), "body"
    return metric_text(row, column.key, caps, reserve=True), "metric"


def _header(state: ts.State, columns: tuple[Column, ...], caps: Capabilities) -> Line:
    arrow = glyph(caps, "down" if state.descending else "up")
    parts = [(" " * ((6 if state.frontier else 5) + 1), "label")]
    for index, column in enumerate(columns):
        text = column.header + (arrow if column.key == state.sort or (column.key == "name" and state.sort == "model")
                                else "")
        cell = pad(safe_text(text, caps), column.width, ascii_only=caps.ascii_only)
        if column.right:
            cell = " " * max(0, column.width - display_width(safe_text(text, caps))) + clip(safe_text(text, caps), column.width)
        parts.append((cell + (" " if index < len(columns) - 1 else ""), "label"))
    return _line(*parts)


def _row_line(state: ts.State, item: ts.Item, columns: tuple[Column, ...], caps: Capabilities,
              frontier: dict, labels: dict | None = None) -> Line:
    focused = item.key == state.focus
    active = focused and state.pane == "table" and state.mode == "browse"
    parts = [(glyph(caps, "focus") if focused else " ", "focus" if active else "key" if focused else "body"),
             (_marks(state, item, caps, frontier), "badge_" + item.route["state"] if item.route else "label")]
    for index, column in enumerate(columns):
        text, role = _cell(state, item, column, caps, state.grouped, labels)
        text = safe_text(text, caps)
        cell = (" " * max(0, column.width - display_width(text)) + clip(text, column.width)) if column.right \
            else pad(text, column.width, ascii_only=caps.ascii_only)
        if item.kind == "group" and column.key == "name":
            cell = clip(text, sum(c.width + 1 for c in columns) - 1, ellipsis=True, ascii_only=caps.ascii_only)
            parts.append((cell, role))
            break
        parts.append((cell + (" " if index < len(columns) - 1 else ""), role))
    return _line(*parts)


def frontier_labels(state: ts.State) -> tuple[str, ...]:
    """The frontier's scope, longest first: it is found within each set of one AA profile and methodology."""
    count = len(ts.frontier_scopes(state))
    sets = f"{count} sets" if count != 1 else "1 set"
    return (f"AA frontier within each AA profile and methodology ({sets}); not a recommendation",
            f"frontier per AA profile ({sets}); not a recommendation",
            "frontier per AA profile; not a recommendation", "frontier per profile; not a recommendation")


def _controls(state: ts.State, shown: int, start: int, count: int, width: int, caps: Capabilities,
              now: datetime) -> Line:
    dot = glyph(caps, "dot")
    arrow = glyph(caps, "down" if state.descending else "up")
    filters = [label for label, value in (("provider", state.provider), ("model", state.model),
                                          ("effort", state.effort), ("state", state.state_filter))
               if value != "all"]
    total = len(ts.routes(state))
    head = [(f"Sort {ts.SORT_LABELS[state.sort]} {arrow}", "value")]
    seen = state.projection.get("observations") or {}
    if seen.get("status") == "observed" and data_age(state, now)["stale"]:
        # Stale data stays marked at every size, even when the status line has no room for its age.
        head += [(dot, "label"), ("Data STALE", "error")]
    view = [(dot, "label"), ("Grouped" if state.grouped else "Ranked", "value")]
    tail = [(dot, "label"), (f"{shown} rows" + (f" {start + 1}-{start + count}" if shown > count else ""), "body")]
    if state.discovery != "supported":
        tail += [(dot, "label"), ("Showing " + state.discovery, "advisory")]
    if filters:
        tail += [(dot, "label"), ("Filter " + ", ".join(f"{name}={getattr(state, 'state_filter' if name == 'state' else name).replace('_', ' ')}"
                                                        for name in filters), "advisory")]
    if state.query or state.mode == "search":
        tail += [(dot, "label"), (f"Search {state.query}{'_' if state.mode == 'search' else ''}", "advisory")]
    if state.frontier:
        # The scope label replaces the route count and shortens until it fits; the view name gives way first.
        labels = [f"{glyph(caps, 'frontier')} {text}" for text in frontier_labels(state)]
        for parts in (head + view + tail, head + tail):
            used = sum(display_width(safe_text(text, caps)) for text, _ in parts) + display_width(dot)
            label = next((text for text in labels if used + display_width(safe_text(text, caps)) <= width), None)
            if label is not None:
                break
        return _clip_line(_line(*_safe(parts + [(dot, "label"), (label or labels[-1], "advisory")], caps)), width, caps)
    parts = head + view + tail
    if total and not state.query and not filters and state.discovery == "supported":
        parts += [(dot, "label"), (f"{total} supported routes", "body")]
    return _clip_line(_line(*_safe(parts, caps)), width, caps)


def _scroll(scroll: int, focus: int, count: int, total: int) -> int:
    if count <= 0:
        return 0
    if focus >= 0:
        if focus < scroll:
            scroll = focus
        elif focus >= scroll + count:
            scroll = focus - count + 1
    return max(0, min(scroll, max(0, total - count)))


def disclaimer_line(width: int, caps: Capabilities) -> Line:
    text = next((option for option in SHORT_DISCLAIMERS if display_width(option) <= width), SHORT_DISCLAIMERS[-1])
    return Line(_fit(text, width, caps), "advisory")


def _table(state: ts.State, width: int, height: int, caps: Capabilities,
           now: datetime) -> tuple[list[Line], int, int]:
    rows = ts.items(state)
    columns = table_columns(state, width)
    frontier = ts.frontier(state) if state.frontier else {}
    labels = narrow_labels([item.route for item in rows if item.kind == "route"], columns[0].width, caps) \
        if columns[0].header == "Route" else None
    # A cost or time column is never shown without the disclaimer; a short index-only table may omit it.
    keep_disclaimer = height >= 8 or any(column.key in COST_AND_TIME for column in columns)
    count = max(1, height - 2 - (1 if keep_disclaimer else 0))
    focus = next((index for index, item in enumerate(rows) if item.key == state.focus), -1)
    start = _scroll(state.table_scroll, focus, count, len(rows))
    lines = [_controls(state, len(rows), start, min(count, len(rows)), width, caps, now),
             _header(state, columns, caps)]
    lines += [_clip_line(_row_line(state, item, columns, caps, frontier, labels), width, caps)
              for item in rows[start:start + count]]
    if not rows:
        hidden = ts.hidden_selections(state)
        lines.append(Line(_fit("No rows match; F clears filters and search", width, caps), "advisory"))
        if any(hidden.values()):
            lines.append(Line(_fit("Pin and Preferred stay saved while hidden", width, caps), "advisory"))
    lines = lines[:height - (1 if keep_disclaimer else 0)]
    lines += [Line("") for _ in range(height - len(lines) - (1 if keep_disclaimer else 0))]
    if keep_disclaimer:
        lines.append(disclaimer_line(width, caps))
    return lines, start, count


# --------------------------------------------------------------------------- inspector content

def _metric_summary(row: dict, caps: Capabilities) -> str:
    """Every measured value with its profile marks, led by AA's qualifier wording when there is one."""
    qualifiers = _qualifiers(row)
    lead = f"AA profile {', '.join(qualifiers)}: " if qualifiers and ts.value(row, "intelligence") is not None \
        or qualifiers and any(ts.value(row, name) is not None for name in COST_AND_TIME) else ""
    return (f"{lead}AA index {metric_text(row, 'intelligence', caps)}; "
            f"{metric_text(row, 'usd_per_task', caps)}/task; {metric_text(row, 'output_tps', caps)} tok/s; "
            f"first {metric_text(row, 'first_response_s', caps)} s; total "
            f"{metric_text(row, 'total_response_s', caps)} s; context {metric_text(row, 'context_tokens', caps)}")


def _preference_lines(state: ts.State, width: int, caps: Capabilities) -> list[Line]:
    prefs = ts.preferences(state)
    hidden = ts.hidden_selections(state)
    lines = []
    for name, label in (("pinned", "Pin"), ("preferred", "Preferred")):
        chosen = prefs.get(name)
        text = (chosen or "none") + (" (hidden by the current view; still saved)" if hidden.get(name) else "")
        lines += _labelled(label, text, width, caps, "value")
    lines += _labelled("Preferences", prefs.get("path") or "unknown", width, caps)
    return lines


def _status_lines(state: ts.State, width: int, caps: Capabilities) -> list[Line]:
    prefs = ts.preferences(state)
    status = prefs.get("status")
    if status == "valid":
        return []
    if status == "setup_required":
        return [_heading("ROUTE SETUP REQUIRED - READ-ONLY", width, caps),
                *_wrapped("Your preferences use the earlier pod/v1 shape. No route is eligible until you "
                          "review and confirm the route setup (: then setup).", width, caps, "error")]
    error = (prefs.get("errors") or [{}])[0]
    action = ("Rerun the one-shot installer to create defaults" if error.get("code") == "config_missing"
              else "Run pod config edit")
    lines = [_heading("PREFERENCES UNAVAILABLE - READ-ONLY", width, caps),
             *_wrapped("Reason: " + str(error.get("message") or error.get("code") or "invalid"), width, caps, "error"),
             *_wrapped("Next: " + action, width, caps, "error")]
    for name, label in (("pin_diagnostic", "Pin in file"), ("preferred_diagnostic", "Preferred in file")):
        if prefs.get(name):
            lines += _wrapped(f"{label}: {prefs[name]} (not usable until the file is valid)", width, caps, "advisory")
    return lines


def details(state: ts.State, item: ts.Item | None, width: int, caps: Capabilities, now: datetime) -> list[Line]:
    lines = _status_lines(state, width, caps)
    if state.error:
        lines += _wrapped(state.error, width, caps, "error")
    if item is None:
        lines.append(_heading("NO ROWS MATCH", width, caps))
        lines += _wrapped("F clears filters and search. Your Pin and Preferred stay saved.", width, caps, "advisory")
        return lines + _preference_lines(state, width, caps)
    if item.kind == "group" and item.model is None:
        lines.append(_heading("NEW AND UNSUPPORTED OBSERVATIONS", width, caps))
        lines += _wrapped("Rows from public sources that Pod's registry does not support. They are visible for "
                          "information and are never routable; enabling routes stays an explicit choice.", width, caps)
        return lines
    if item.kind == "group":
        members = ts.group_members(state, item.model)
        lines.append(_heading(ts.model_name(state, item.model), width, caps))
        counts = {name: sum(row["state"] == name for row in members) for name in ts.STATE_ORDER}
        lines += _labelled("Model id", item.model, width, caps, "value")
        lines += _labelled("Routes", f"{len(members)} efforts: {counts['enabled']} enabled, {counts['disabled']} "
                           f"disabled, {counts['not_set']} not set", width, caps)
        lines += _labelled("Bulk", "b edits this model's efforts with an exact preview", width, caps)
        return lines + _preference_lines(state, width, caps)
    if item.kind == "observation":
        row = item.observation
        lines.append(_heading(str(row.get("row")), width, caps))
        meaning = ("New: not in Pod's registry" if row.get("discovery") == "new"
                   else f"Unsupported: {row.get('model')} is outside the normal pool")
        lines += _labelled("Discovery", meaning + "; not routable and never enabled by a refresh", width, caps,
                           "advisory")
        lines += _labelled("Source", f"{row.get('source')} (creator {row.get('creator')})", width, caps)
        lines += _labelled("Measured", _metric_summary(row, caps), width, caps, "metric")
        lines += _labelled("Retrieved", f"{row.get('retrieved_at') or 'unknown'} ({age_text(row.get('retrieved_at'), now)})",
                           width, caps)
        return lines
    row = item.route
    lines.append(_heading(f"{row['name']} {glyph(caps, 'dot').strip()} {row['effort']}", width, caps))
    lines += _labelled("Route key", row["key"], width, caps, "value")
    lines += _labelled("Native", f"agent {row['agent']}, model {row['model']}, effort {row['effort']}, provider "
                       f"{row['provider']}", width, caps)
    meaning = {"enabled": "Enabled: eligible for new Pod-routed assignments",
               "disabled": "Disabled: not eligible for new Pod-routed assignments",
               "not_set": "Not set: not eligible; enabling it is an explicit choice"}[row["state"]]
    lines += _labelled("State", meaning, width, caps, "badge_" + row["state"])
    marks = [label for flag, label in ((row["pinned"], "Pinned (hard)"), (row["preferred"], "Preferred (soft)"))
             if flag]
    if marks:
        lines += _labelled("Selection", ", ".join(marks), width, caps, "value")
    lines += _labelled("Native access", "unknown: Pod does not verify account access; launch readback decides the "
                       "effective route", width, caps, "advisory")
    context = row.get("documented_context_tokens")
    lines += _labelled("Documented", f"context {context:,} tokens (registry)" if context else
                       "context not stated by the registry", width, caps)
    lines += _labelled("AA measures", _metric_summary(row, caps), width, caps, "metric")
    return lines + _preference_lines(state, width, caps)


def _change_text(name: str, before: object, after: object, caps: Capabilities, comparable: bool,
                 marks: tuple[str, str] = ("", "")) -> str:
    text = (f"{METRIC_LABELS[name]} {number(name, before, caps)}{marks[0] if before is not None else ''} -> "
            f"{number(name, after, caps)}{marks[1] if after is not None else ''}")
    if before is None or after is None:
        return text
    if not comparable:
        return text + " (not comparable: the AA methodology or profile changed)"
    if name == "intelligence":
        return text + f" ({after - before:+g} pts)"
    if before == 0:
        return text + " (zero baseline)"
    return text + f" ({(after - before) / before * 100:+.1f}%)"


def benchmarks(state: ts.State, item: ts.Item | None, width: int, caps: Capabilities, now: datetime) -> list[Line]:
    lines = _wrapped(DISCLAIMER, width, caps, "advisory")
    row = item.route if item and item.kind == "route" else item.observation if item and item.kind == "observation" else None
    if row is None:
        lines += _wrapped("Focus a route to see its AA benchmark profile.", width, caps)
        return lines
    metrics = row.get("metrics") or {}
    first = next((metric for metric in metrics.values() if isinstance(metric, dict) and metric.get("row")), None)
    profile = (first or {}).get("row") or row.get("row")
    lines += _labelled("AA profile", profile or "no AA row maps to this route; metrics unknown", width, caps, "value")
    qualifiers = _qualifiers(row)
    if "with fallback" in qualifiers:
        lines += _labelled("Fallback", "AA's harness ran this profile with fallback (values shown with "
                           f"{glyph(caps, 'fallback')}); it never enables Pod fallback", width, caps, "advisory")
    if "estimated index" in qualifiers:
        lines += _labelled("Estimated", f"AA marks this index as estimated (shown with {glyph(caps, 'estimated')})",
                           width, caps, "advisory")
    if first:
        lines += _labelled("Methodology", first.get("methodology") or "not stated by the source", width, caps)
        lines += _labelled("Retrieved", f"{first.get('retrieved_at')} ({age_text(first.get('retrieved_at'), now)}); "
                           f"measured {first.get('published_at') or 'date not published by the source'}", width, caps)
    for name in ts.METRICS:
        lines += _labelled(METRIC_LABELS[name], f"{metric_text(row, name, caps)} ({UNITS[name]})",
                           width, caps, "metric")
    lines += _labelled("Rank scope", "Order among the rows shown in this table only; no global rank. Small "
                       "differences on one benchmark do not establish a winner.", width, caps)
    if state.frontier and item.kind == "route":
        mark = ts.frontier(state).get(row["key"], "unknown")
        scope = (f"shown rows with methodology {first.get('methodology') or 'not stated'} and AA profile "
                 f"{ts.profile_words(tuple(qualifiers))}") if first else "shown rows"
        text = {"frontier": f"on the AA frontier of the {scope}",
                "dominated": f"another of the {scope} is at least as good on index, $/task and first response",
                "unknown": "unknown: a dimension is missing or not comparable"}[mark]
        lines += _labelled("Frontier", text + "; display only, never a recommendation or eligibility rule",
                           width, caps, "advisory")
    if item.kind == "route":
        lines.append(_heading("CHANGES SINCE THE PREVIOUS SNAPSHOT", width, caps))
        if state.previous is None:
            lines += _wrapped("No previous snapshot to compare yet.", width, caps)
        else:
            moved = ts.methodology_changed(state)
            if moved:
                lines += _wrapped("The benchmark methodology changed between snapshots; differences are not "
                                  "model improvement.", width, caps, "advisory")
            shift = ts.profile_change(state, row["key"])
            if shift:
                lines += _wrapped(f"AA measurement changed for this route ({shift}): a methodology or profile "
                                  "change, not a model change, so no difference is shown.", width, caps, "advisory")
            earlier = ((state.previous.get("profiles") or {}).get(row["key"]) or {}).get("qualifiers") or ()
            found = ts.changes(state, row["key"])
            for name, before, after in found:
                comparable = not (moved or shift)
                marks = (marks_for(earlier, name, caps), marks_for(qualifiers, name, caps))
                lines += _wrapped(_change_text(name, before, after, caps, comparable, marks), width, caps, "metric")
            if not found:
                lines += _wrapped("No change for this route.", width, caps)
            changed = sum(bool(ts.changes(state, other["key"])) for other in ts.routes(state))
            lines += _wrapped(f"Previous generation {state.previous.get('generation')} created "
                              f"{state.previous.get('created_at')}; {changed} supported routes changed.", width, caps)
        others = [entry for entry in row.get("observations") or [] if entry.get("source") != (first or {}).get("source")]
        if others:
            lines.append(_heading("OTHER SOURCES FOR THIS MODEL", width, caps))
            for entry in others:
                lines += _wrapped(f"{entry.get('source')}: {entry.get('row')} context "
                                  f"{number('context_tokens', (entry.get('metrics') or {}).get('context_tokens'), caps)} "
                                  f"(retrieved {entry.get('retrieved_at')})", width, caps)
    return lines


def routing(state: ts.State, item: ts.Item | None, width: int, caps: Capabilities, now: datetime) -> list[Line]:
    row = item.route if item and item.kind == "route" else None
    lines = _wrapped("Authored Pod guidance, not a prediction for an unspecified future task.", width, caps, "advisory")
    if row is None:
        return lines + _wrapped("Focus a supported route to see its guidance.", width, caps)
    guide = row.get("guide") or {}
    if guide.get("use"):
        lines += _labelled("Use", guide["use"], width, caps)
    if guide.get("efforts"):
        lines += _labelled("Efforts", guide["efforts"], width, caps)
    if guide.get("max"):
        lines += _labelled("Max effort", guide["max"], width, caps, "advisory")
    if row.get("guidance"):
        lines += _labelled("Provider says", row["guidance"], width, caps)
    lines += _labelled("Preferred", "Soft: chosen when suitable; overriding it needs a material, assignment-specific "
                       "reason.", width, caps)
    lines += _labelled("Pin", "Hard: every new Pod-routed role uses exactly this model and effort or does not "
                       "dispatch; no fallback.", width, caps)
    lines += _labelled("AA data", "May inform efficiency among already suitable routes; it never picks a winner "
                       "or grants eligibility.", width, caps)
    return lines


def sources(state: ts.State, item: ts.Item | None, width: int, caps: Capabilities, now: datetime) -> list[Line]:
    seen = state.projection.get("observations") or {}
    lines = []
    if state.refresh_state != "idle":
        text, role = refresh_label(state)
        lines.append(_heading("LAST REFRESH IN THIS WINDOW", width, caps))
        lines += _wrapped(text, width, caps, role)
    lines.append(_heading("OBSERVATION SNAPSHOT", width, caps))
    if seen.get("status") == "observed":
        stale = data_age(state, now)["stale"]
        lines += _wrapped(f"{seen.get('origin')} snapshot, generation {seen.get('generation')}, created "
                          f"{seen.get('created_at')}" + (" (stale)" if stale else ""), width, caps,
                          "error" if stale else "body")
    else:
        lines += _wrapped(f"Observations {seen.get('status') or 'unknown'}: metrics show as unknown.", width, caps, "advisory")
    for message in seen.get("diagnostics") or []:
        lines += _wrapped(message, width, caps, "advisory")
    for name, block in sorted((seen.get("sources") or {}).items()):
        lines.append(_heading(f"{block.get('attribution') or name} ({'required' if block.get('required') else 'optional'})",
                              width, caps))
        lines += _wrapped(str(block.get("url")), width, caps, "value", indent=2)
        status = block.get("status")
        rows = block.get("rows") or 0
        text = f"Latest attempt {status}; {rows} rows"
        if block.get("retrieved_at"):
            text += f" retrieved {block['retrieved_at']} ({age_text(block['retrieved_at'], now)})"
        if status != "ok" and rows:
            text += "; these rows are from that earlier read"
        lines += _wrapped(text, width, caps, "error" if status != "ok" else "body", indent=2)
        moment = _when(block.get("retrieved_at"))
        stale = moment is None or (now - moment).total_seconds() >= STALE_S
        lines += _wrapped(f"Measured {block.get('published_at') or 'date not published by the source'}; methodology "
                          f"{block.get('methodology') or 'not stated'}" + ("; stale" if stale else ""),
                          width, caps, indent=2)
        for message in (block.get("diagnostics") or [])[:4]:
            lines += _wrapped(message, width, caps, "advisory", indent=2)
    row = item.route if item and item.kind == "route" else None
    if row is not None:
        lines.append(_heading("THIS ROUTE", width, caps))
        mapped = [entry for entry in row.get("observations") or []]
        if mapped:
            for entry in mapped:
                lines += _wrapped(f"{entry.get('source')} row {entry.get('row')!r} maps to this "
                                  f"{'route' if entry.get('row') != row['model'] else 'model'} by an explicit alias",
                                  width, caps)
        else:
            lines += _wrapped("No source row maps to this route; its metrics are unknown.", width, caps)
        for source in row.get("sources") or []:
            lines += _wrapped(f"Registry source {source.get('url')} (checked {source.get('checked')})", width, caps)
    lines.append(_heading("NATIVE EVIDENCE", width, caps))
    lines += _wrapped("Native access is unknown. Pod reads no credential or account state; existing launch "
                      "readback decides the effective route.", width, caps, "advisory")
    return lines


def compare(state: ts.State, width: int, caps: Capabilities, now: datetime) -> list[Line]:
    rows = [ts.route(state, key) for key in state.compare]
    rows = [row for row in rows if row is not None]
    lines = _wrapped(DISCLAIMER, width, caps, "advisory")
    if not rows:
        return lines + _wrapped("Mark up to four routes with c to compare them.", width, caps)
    for index, row in enumerate(rows, 1):
        lines += _wrapped(f"{index} {_route_label(row)}: {_metric_summary(row, caps)}", width, caps, "metric")
    base = rows[0]
    if len(rows) > 1:
        lines.append(_heading(f"DIFFERENCES FROM 1 {_route_label(base)}", width, caps))
        lines += _wrapped("Index differences are points; others are percentages with a valid nonzero baseline "
                          "from the same source, methodology and AA profile.", width, caps)
        for index, row in enumerate(rows[1:], 2):
            if ts.value(base, "intelligence") is not None and ts.value(row, "intelligence") is not None:
                difference = ts.profile_difference(ts.profile(base), ts.profile(row))
                if difference:
                    lines += _wrapped(f"Caveat: 1 and {index} are not like-for-like ({difference}); their AA "
                                      "numbers are not compared.", width, caps, "advisory")
            parts = [f"{METRIC_LABELS[name]} {ts.delta(base, row, name)}" for name in ts.METRICS]
            lines += _wrapped(f"{index} {_route_label(row)}: " + "; ".join(parts), width, caps)
    return lines


INSPECTORS = {"details": details, "benchmarks": benchmarks, "routing": routing, "sources": sources}


def inspector_lines(state: ts.State, width: int, caps: Capabilities, now: datetime) -> list[Line]:
    if state.tab == "compare":
        return compare(state, width, caps, now)
    return INSPECTORS[state.tab](state, ts.focused_item(state), width, caps, now)


def _tab_bar(state: ts.State, width: int, caps: Capabilities) -> Line:
    names = ts.tabs(state)
    focused = state.pane == "inspector" and state.mode == "browse"
    rule = glyph(caps, "rule")
    for short in (False, True):
        parts = [(rule + " ", "label")]
        for name in names:
            label = TAB_LABELS[name][1 if short else 0] + (f"({len(state.compare)})" if name == "compare" else "")
            if name == state.tab:
                parts.append(("[" + label + "]", "title" if focused else "heading"))
            else:
                parts.append((" " + label + " ", "label"))
        hint = " Tab: inspector" if not focused else " Esc: table"
        used = sum(display_width(safe_text(text, caps)) for text, _ in parts)
        if used + display_width(hint) + 2 <= width:
            parts.append((hint + " ", "label"))
            used += display_width(hint) + 1
        if used <= width:
            parts.append((rule * max(0, width - used), "label"))
            return _clip_line(_line(*_safe(parts, caps)), width, caps)
    return _clip_line(_line(*_safe(parts, caps)), width, caps)


def _page(lines: list[Line], count: int, width: int, caps: Capabilities, scroll: int,
          hint: str = "Down or Page Down") -> tuple[list[Line], int, int]:
    """A scrolled page, its clamped start and the step to the next page; a cut page names the keys that scroll it."""
    if count <= 0:
        return [], 0, 1
    start = max(0, min(scroll, max(0, len(lines) - count)))
    page = [_clip_line(line, width, caps) for line in lines[start:start + count]]
    marked = count >= 2 and len(lines) > start + count
    if marked:
        page[-1] = Line(_fit(glyph(caps, "down") + f" more ({len(lines) - start - count + 1} lines): " + hint,
                             width, caps), "advisory")
    return page, start, max(1, count - 1 if marked else count)


# --------------------------------------------------------------------------- overlays

def _help(state: ts.State, width: int, caps: Capabilities) -> list[Line]:
    keys = [("Up/Down j/k", "move in the focused pane; Page Up/Down, Home/End jump"),
            ("Tab, Enter, Esc", "move between table and inspector"),
            ("[ ] or Left/Right", "inspector tabs: Details, Benchmarks, Routing, Sources, Compare"),
            (": or Ctrl+P", "command palette with every action"),
            ("Space", "enable or disable the focused route (saves at once)"),
            ("p / P", "pin or unpin / make Preferred or clear (saves at once)"),
            ("b", "bulk edit with an exact preview; reset is in the palette"),
            ("s / S", "next sort / reverse direction"), ("g, Left/Right", "grouped view; collapse or expand"),
            ("f, o, F", "provider filter, new/unsupported observations, clear filters"),
            ("/", "search name, model id or route key"), ("c / C, e", "compare marks, clear; frontier marker"),
            ("R", "refresh public model data now"), ("? / q", "help / quit")]
    lines = [_heading("HELP: MODEL ROUTES", width, caps)]
    for key, text in keys:
        lines += _labelled(key, text, width, caps, label_width=20)
    lines.append(_heading("MARKS", width, caps))
    lines += _wrapped(f"{glyph(caps, 'enabled')} enabled, {glyph(caps, 'disabled')} disabled, "
                      f"{glyph(caps, 'not_set')} not set; {glyph(caps, 'pin')} Pin; {glyph(caps, 'preferred')} "
                      f"Preferred; 1-4 compare order; {glyph(caps, 'frontier')} AA frontier; "
                      f"{glyph(caps, 'focus')} focus. After an AA value: {glyph(caps, 'fallback')} AA ran this "
                      f"profile with fallback (AA's harness, never Pod fallback); {glyph(caps, 'estimated')} AA "
                      "estimated index.", width, caps)
    lines.append(_heading("UNITS AND SCOPE", width, caps))
    lines += _wrapped("AA index: points. $/task: USD per AA benchmark task. Tok/s: median output tokens per second. "
                      "First s / Total s: benchmark seconds. Context: tokens. Unknown values show as a dash and "
                      "sort last. Order is among the shown rows only; there is no global rank. Values with different "
                      "AA profile marks or methodology are not compared, and the frontier is found within each "
                      "such set.", width, caps)
    lines += _wrapped(DISCLAIMER, width, caps, "advisory")
    lines += _wrapped("Navigation, sorting, filters and comparisons never save, start workers or call models. "
                      "Edits save immediately to your one preference file.", width, caps)
    lines += _wrapped("Preferences: " + str(ts.preferences(state).get("path")), width, caps)
    return lines


def _palette(state: ts.State, width: int, height: int, caps: Capabilities) -> list[Line]:
    matches = ts.palette_matches(state)
    lines = [_heading("COMMAND PALETTE", width, caps),
             Line(_fit(": " + state.palette_query + "_", width, caps), "value")]
    count = max(1, height - 3)
    index = min(state.palette_index, max(0, len(matches) - 1))
    start = _scroll(0, index, count, len(matches))
    for position, (_command, label, shortcut) in enumerate(matches[start:start + count], start):
        focused = position == index
        hint = f" [{shortcut}]" if shortcut else ""
        room = width - 2 - display_width(safe_text(hint, caps))
        parts = [(glyph(caps, "focus") + " " if focused else "  ", "focus" if focused else "body"),
                 (pad(safe_text(label, caps), max(1, room), ascii_only=caps.ascii_only), "value" if focused else "body"),
                 (hint, "key")]
        lines.append(_clip_line(_line(*_safe(parts, caps)), width, caps))
    if not matches:
        lines.append(Line(_fit("No command matches; Backspace edits, Esc closes", width, caps), "advisory"))
    lines.append(Line(_fit(f"{len(matches)} commands; Enter runs, Esc closes", width, caps), "label"))
    return lines


def _field(label: str, text: str, focused: bool, caps: Capabilities, width: int) -> Line:
    parts = [(glyph(caps, "focus") + " " if focused else "  ", "focus" if focused else "body"),
             (pad(label, 16, ascii_only=caps.ascii_only), "label"),
             (("< " if focused else "") + text + (" >" if focused else ""), "value")]
    return _clip_line(_line(*_safe(parts, caps)), width, caps)


def _bulk(state: ts.State, width: int, height: int, caps: Capabilities) -> list[Line]:
    bulk = state.bulk
    conflicts = bulk.conflicts
    fields = ts.bulk_fields(bulk, conflicts)
    current = fields[min(bulk.field, len(fields) - 1)]
    scopes = {"model": f"All efforts of {ts.model_name(state, bulk.model)}",
              "range": "Shown routes in an effort range", "not_set": "Shown routes that are not set",
              "reset": "Reset to shipped defaults"}
    lines = [_heading("BULK EDIT - EXACT PREVIEW, ONE SAVE", width, caps),
             _field("Scope", scopes[bulk.scope], current == "scope", caps, width)]
    if "action" in fields:
        lines.append(_field("Action", "enable" if bulk.action == "enabled" else "disable", current == "action",
                            caps, width))
    if bulk.scope == "range":
        lines.append(_field("From effort", ts.EFFORTS[bulk.low], current == "low", caps, width))
        lines.append(_field("To effort", ts.EFFORTS[bulk.high], current == "high", caps, width))
    for name in ("pinned", "preferred"):
        if name in conflicts:
            flag = bulk.clear_pin if name == "pinned" else bulk.clear_preferred
            label = "Clear Pin" if name == "pinned" else "Clear Preferred"
            lines.append(_field(label, f"{'yes' if flag else 'no'} ({conflicts[name]} would be disabled)",
                                current == name, caps, width))
    if bulk.scope == "reset":
        lines += _wrapped("Enables exactly the supported routes listed below, the first-install default. Pin, "
                          "Preferred, worker limit and refresh setting stay as they are; routes added by a later "
                          "Pod update are not included.", width, caps, "advisory")
    if ts.bulk_stale(state):
        lines += _wrapped("The preferences changed since this preview; Enter saves nothing and shows the new "
                          "preview to review.", width, caps, "error")
    # The captured preview is exactly what Enter saves, so it is shown as captured.
    changed = bulk.changes
    lines.append(_heading(f"CHANGES ({len(changed)})", width, caps))
    body = [Line(_fit(f"{key}: {STATE_TEXT[bulk.before.get(key, 'not_set')]} -> {STATE_TEXT[target]}", width, caps),
                 "metric") for key, target in changed.items()]
    if not body:
        body = [Line(_fit("Nothing to change for this scope", width, caps), "advisory")]
    room = max(1, height - len(lines))
    page, start, _step = _page(body, room, width, caps, bulk.scroll, "Page Down")
    return lines + page


def _setup(state: ts.State, width: int, height: int, caps: Capabilities) -> list[Line]:
    setup = state.setup
    preview = setup.preview
    pins, preferred = ts.setup_options(state)
    needs_pin = any(row.get("id") == "pin" for row in preview.get("choices", []))
    fields = (["pin"] if needs_pin else []) + ["preferred"]
    current = fields[min(setup.field, len(fields) - 1)]
    document = preview.get("document") or {}
    route_states = list((document.get("routes") or {}).values())
    lines = [_heading("ROUTE SETUP FOR YOUR EARLIER POD/V1 PREFERENCES", width, caps),
             *_wrapped("Nothing is saved until you confirm. The original file is kept as config.yaml.pod-v1.",
                       width, caps, "advisory")]
    if needs_pin:
        model = next(row for row in preview["choices"] if row.get("id") == "pin")["model"]
        text = ("choose an effort or clear" if setup.pin < 0 else
                pins[setup.pin] if setup.pin < len(pins) else "clear the pin")
        lines.append(_field("Earlier pin", f"{model}: {text}", current == "pin", caps, width))
    lines.append(_field("Preferred", preferred[setup.preferred - 1] if setup.preferred else "none",
                        current == "preferred", caps, width))
    lines += _wrapped(f"Proposed: {route_states.count('enabled')} routes enabled, {route_states.count('disabled')} "
                      f"disabled; others not set. Worker limit {(document.get('workers') or {}).get('max_active')}; "
                      f"refresh {document.get('refresh')}.", width, caps)
    if setup.confirming:
        lines += _wrapped("Save this route setup now? y saves, n returns.", width, caps, "error")
    notes = []
    for note in preview.get("notes") or []:
        notes += _wrapped("- " + str(note.get("message")), width, caps)
    notes += [Line(_fit(f"{key}: {value}", width, caps), "metric") for key, value in (document.get("routes") or {}).items()]
    room = max(1, height - len(lines))
    page, _start, _step = _page(notes, room, width, caps, setup.scroll, "Page Down")
    return lines + page


# --------------------------------------------------------------------------- frame parts

def _pack(segments: list[tuple[list[tuple[str, str]], bool]], width: int, caps: Capabilities,
          separator: str, reserve: tuple[int, int] = (0, 0)) -> tuple[Line, list[int]]:
    """Fit segments in order; a segment that does not fit is dropped and reported.

    `reserve` is (index, columns): a clippable segment before that index leaves the columns free.
    """
    parts, used, dropped = [], 0, []
    for index, (segment, clipped) in enumerate(segments):
        safe = _safe(segment, caps)
        size = sum(display_width(text) for text, _ in safe) + (display_width(separator) if parts else 0)
        limit = width - (reserve[1] if index < reserve[0] else 0)
        room = limit - used - (display_width(separator) if parts else 0)
        if used + size > limit and clipped and room >= 12:
            # A notice or failure is cut to the room left rather than hidden.
            safe = [(_fit("".join(text for text, _ in safe), room, caps), safe[0][1])]
            size = limit - used
        if used + size <= width:
            if parts:
                parts.append((separator, "label"))
            parts += safe
            used += size
        else:
            dropped.append(index)
    return _clip_line(_line(*parts), width, caps), dropped


def _title(state: ts.State, width: int, caps: Capabilities) -> tuple[Line, list]:
    prefs = ts.preferences(state)
    hidden = ts.hidden_selections(state)
    status = prefs.get("status")
    count = ((f"{sum(row['state'] == 'enabled' for row in ts.routes(state))}/{len(ts.routes(state))} enabled", "value")
             if status == "valid" else ("SETUP REQUIRED" if status == "setup_required" else "READ-ONLY", "error"))
    pin = [("Pin ", "label"), ((prefs.get("pinned") or "none") + (" (hidden)" if hidden["pinned"] else ""), "value")]
    preferred = [("Preferred ", "label"),
                 ((prefs.get("preferred") or "none") + (" (hidden)" if hidden["preferred"] else ""), "value")]
    if width < NORMAL - 1:
        count = (count[0].replace(" enabled", " on"), count[1])
        segments = [([count], False), (pin, False), (preferred, False)]
    else:
        segments = [([("Pod", "title")], False), (pin, False), (preferred, False), ([count], False)]
    line, dropped = _pack(segments, width, caps, glyph(caps, "dot"))
    return line, [segments[index][0] for index in dropped]


def _exact_key(segment: list) -> bool:
    """A title segment naming an exact Pin or Preferred route key (not a "none" placeholder or count)."""
    return (len(segment) == 2 and segment[0][0] in ("Pin ", "Preferred ")
            and not segment[1][0].startswith("none"))


def _status(state: ts.State, width: int, caps: Capabilities, now: datetime, extra: list, *,
            age_first: bool = False, after: list | None = None) -> Line:
    seen = state.projection.get("observations") or {}
    segments = []
    if state.notice and state.notice != state.error:
        segments.append(([(state.notice, "advisory")], True))
    if state.error:
        segments.append(([(state.error, "error")], True))
    later = [(segment, False) for segment in extra] if age_first else []
    if not age_first:
        segments += [(segment, False) for segment in extra]
    refresh = refresh_label(state) if state.refresh_state != "idle" else None
    at = len(segments)
    if seen.get("status") == "observed":
        ages = data_age(state, now)
        age = "unknown age" if ages["age"] is None else span(ages["age"]) + " old"
        data = f"Data {seen.get('origin')} {age}" + (" STALE" if ages["stale"] else "")
        oldest = None if ages["oldest"] is None else span(ages["oldest"])
        # The age comes first; the mixed-ages note shortens, then drops, before the age would.
        variants = ([data + f"; mixed ages (oldest {oldest})", data + f"; mixed (oldest {oldest})", data + "; mixed"]
                    if oldest else []) + [data]
        role = "error" if ages["stale"] else "body"
    else:
        variants, role = ["Data unknown"], "advisory"
    tail = list(later)
    if refresh:
        tail.append(([refresh], True))
    tail.append(([("Native access unknown", "body")], False))
    if state.saved_at:
        tail.append(([("Saved " + state.saved_at.astimezone().strftime("%H:%M:%S"), "body")], False))
    # Placeholders and the count (or its READ-ONLY / SETUP REQUIRED substitute) are the last filler.
    tail += [(segment, False) for segment in after or []]
    separator = glyph(caps, "dot")
    for text in variants:
        line, dropped = _pack(segments + [([(text, role)], False)] + tail, width, caps, separator)
        if at not in dropped:
            break
    else:
        # Even the bare age does not fit beside the notice, so the notice is cut to leave it room.
        reserve = (at, display_width(safe_text(variants[-1], caps)) + display_width(separator))
        line, _dropped = _pack(segments + [([(variants[-1], role)], False)] + tail, width, caps, separator, reserve)
    path = str(ts.preferences(state).get("path") or "")
    room = width - display_width(line.text) - display_width(glyph(caps, "dot")) - len("Config ")
    if path and room >= 16:
        text = "Config " + elide_middle(safe_text(path, caps), room, ascii_only=caps.ascii_only)
        line = _line(*[(span.text, span.role) for span in line.styled], (glyph(caps, "dot"), "label"), (text, "label"))
    return _clip_line(line, width, caps)


FOOTERS = {
    "browse": [("Space", "enable/disable"), (":", "palette"), ("p", "pin"), ("P", "preferred"), ("Tab", "inspector"),
               ("s", "sort"), ("/", "search"), ("c", "compare"), ("b", "bulk"), ("?", "help"), ("q", "quit")],
    "inspector": [("Up/Down", "scroll"), ("[ ]", "tab"), ("Esc", "table"), (":", "palette"), ("?", "help"),
                  ("q", "quit")],
    "search": [("type", "search"), ("Enter", "keep"), ("Esc", "clear")],
    "palette": [("type", "filter"), ("Up/Down", "choose"), ("Enter", "run"), ("Esc", "close")],
    "help": [("Up/Down", "scroll"), ("Esc", "close"), ("q", "quit")],
    "bulk": [("Up/Down", "field"), ("Left/Right", "change"), ("Enter", "save"), ("Esc", "cancel"),
             ("PgDn", "more")],
    "setup": [("Up/Down", "field"), ("Left/Right", "choose"), ("Enter", "review"), ("Esc", "later")],
    "confirm": [("y", "save"), ("n", "back")],
}
FOOTER_DROP = ("b", "c", "/", "s", "Tab", "P", "PgDn", "p", "[ ]", "Up/Down", "Left/Right")
COMPACT = {"Space": "state", ":": "cmds", "Up/Down": "move"}


def _footer(state: ts.State, width: int, caps: Capabilities) -> Line:
    name = state.mode
    if name == "browse" and state.pane == "inspector":
        name = "inspector"
    if name == "setup" and state.setup and state.setup.confirming:
        name = "confirm"
    items = list(FOOTERS[name])
    if width < 60:
        items = [(key, COMPACT.get(key, text)) for key, text in items]
    separator = " " + glyph(caps, "dot").strip() + " "
    while True:
        parts: list[tuple[str, str]] = []
        for index, (key, text) in enumerate(items):
            if index:
                parts.append((separator, "label"))
            parts += [(key, "key"), (" " + text, "body")]
        line = _line(*_safe(parts, caps))
        droppable = [key for key in FOOTER_DROP if any(item[0] == key for item in items)]
        if display_width(line.text) <= width or not droppable:
            return _clip_line(line, width, caps)
        items = [item for item in items if item[0] != droppable[0]]


REFRESH_LABELS = {"running": ("Refreshing data...", "advisory"), "updated": ("Data updated", "advisory"),
                  "unchanged": ("Data refreshed; no changes", "advisory"),
                  "checked": ("Refresh checked; nothing saved", "advisory"),
                  "refused": ("Refresh refused", "error"),
                  "superseded": ("A newer refresh won; data reloaded", "advisory"),
                  "cancelled": ("Refresh cancelled", "advisory"), "failed": ("Refresh failed", "error")}


def refresh_label(state: ts.State) -> tuple[str, str]:
    """The refresh outcome by name, with its diagnostic or error code when there is one."""
    text, role = REFRESH_LABELS.get(state.refresh_state, ("Refresh " + state.refresh_state, "advisory"))
    return text + (": " + state.refresh_detail if state.refresh_detail else ""), role


def dock_height(body: int) -> int:
    """Rows for the bottom inspector, including its tab bar; short terminals keep a compact dock."""
    if body <= 10:
        return 4 if body >= 9 else 3
    return max(5, min(body - 6, round(body * 0.45)))


def frame(state: ts.State, cols: int, rows: int, caps: Capabilities, now: datetime, *, strict: bool = False) -> Frame:
    """Render one screen. `strict` raises on any line wider than the drawable width."""
    if cols <= 0 or rows <= 0:
        return Frame((), cols, rows)
    width = cols - 1
    if cols < MIN_COLUMNS or rows < MIN_ROWS:
        return Frame((Line(_fit("Terminal too small", width, caps), "error"),
                      Line(_fit("Resize or press q to quit", width, caps))), cols, rows)
    title, overflow = _title(state, width, caps)
    # Selections that do not fit the title get their own line when there is height for it.
    selections = None
    if overflow and rows >= 20:
        selections, _dropped = _pack([(segment, False) for segment in overflow], width, caps, glyph(caps, "dot"))
        overflow = []
    # Below that height they share the status line: only exact Pin and Preferred keys may precede the
    # data age; a "none" placeholder and the enabled count follow it, shown when there is room.
    exact = [segment for segment in overflow if _exact_key(segment)]
    minor = [segment for segment in overflow if not _exact_key(segment)]
    body_rows = rows - 3 - (1 if selections else 0)
    table_scroll, table_page = state.table_scroll, state.table_page
    inspector_scroll, inspector_page, help_scroll = state.inspector_scroll, state.inspector_page, state.help_scroll
    if state.mode == "help":
        body, help_scroll, inspector_page = _page(_help(state, width, caps), body_rows, width, caps, state.help_scroll)
    elif state.mode == "palette":
        body = _palette(state, width, body_rows, caps)
    elif state.mode == "bulk" and state.bulk is not None:
        body = _bulk(state, width, body_rows, caps)
    elif state.mode == "setup" and state.setup is not None:
        body = _setup(state, width, body_rows, caps)
    else:
        dock = dock_height(body_rows)
        table, table_scroll, table_page = _table(state, width, body_rows - dock, caps, now)
        hint = "Down or Page Down" if state.pane == "inspector" else "Tab, then Down or Page Down"
        content, inspector_scroll, inspector_page = _page(inspector_lines(state, width, caps, now), dock - 1, width,
                                                          caps, state.inspector_scroll, hint)
        body = table + [_tab_bar(state, width, caps)] + content
    body = body[:body_rows] + [Line("") for _ in range(body_rows - len(body))]
    # Overlays draw no table controls line, so their status line keeps the age (and STALE) ahead of
    # overflowed selections.
    table_view = state.mode not in ("help", "palette", "bulk", "setup")
    lines = [title, *body, *([selections] if selections else []),
             _status(state, width, caps, now, exact, age_first=not table_view, after=minor),
             _footer(state, width, caps)]
    too_wide = [line.text for line in lines if display_width(line.text) > width]
    if too_wide and strict:
        raise ValueError(f"frame line exceeds {width} columns: {too_wide[0]!r}")
    return Frame(tuple(_clip_line(line, width, caps) for line in lines), cols, rows, table_scroll, table_page,
                 inspector_scroll, inspector_page, help_scroll, preset(cols))
