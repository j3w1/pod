"""Pure model-pool frames: labelled AA profiles, sectioned guidance and semantic spans.

Every line is built to fit the drawable width. `frame(strict=True)` raises on an overflow so tests can
prove the layout for every supported size; the interactive path clips instead of crashing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .catalog import age, by_id, format_latency, format_usd, ranks, record, records, reference_rows
from .term import Capabilities, clean, clip, display_width, elide_middle, glyph, pad, safe_text, wrap
from .tui_state import FILTERS, SORTS, State, visible_ids

AA_URL = "https://artificialanalysis.ai/leaderboards/models"
DISCLAIMER = ("AA $/task is AA benchmark cost, not your subscription cost, Pod-run invoice or quota use. "
              "First response is not total task duration.")
SHORT_DISCLAIMER = "$/task is benchmark cost, not your bill or quota; first response is not task duration."
SPLIT_COLUMNS = 140     # table and Details side by side at this terminal width and above
LABEL_WIDTH = 17        # "BENCHMARK SOURCE" plus one space
RUNTIME_TEXT = {"Supported": "supported", "Unsupported": "not advertised", "Offline": "Orca offline",
                "Unknown": "unknown"}


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

    @property
    def plain(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    width: int


def _line(*parts: tuple[str, str]) -> Line:
    spans = tuple(Span(text, role) for text, role in parts if text)
    return Line("".join(span.text for span in spans), spans=spans)


def _fit(value: object, width: int, caps: Capabilities) -> str:
    return clip(safe_text(value, caps), width, ellipsis=True, ascii_only=caps.ascii_only)


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


def _wrapped(value: object, width: int, caps: Capabilities, role: str = "body") -> list[Line]:
    return [Line(row, role) for row in wrap(safe_text(value, caps), max(1, width))]


def _heading(title: str, width: int, caps: Capabilities) -> Line:
    return Line(_fit(title, width, caps), "heading")


def _age(state: State, now: datetime) -> str:
    days = age(state.catalog, today=now.date())
    if days is None:
        return "unknown age"
    return "today" if days == 0 else "1 day old" if days == 1 else f"{days} days old"


def _captured(state: State) -> str:
    return str((state.catalog.get("benchmarks") or {}).get("captured") or "unknown")


def _state_badge(state: State, model_id: str, caps: Capabilities) -> tuple[str, str]:
    value = state.preferences["effective"].get(model_id)
    if value in ("available", "preferred", "disabled"):
        return f"{glyph(caps, value)} {value.capitalize()}", "badge_" + value
    return "? Not set", "badge_unset"


# --------------------------------------------------------------------------- table

def _columns(width: int) -> tuple[Column, ...]:
    """Columns for a drawable width. AA numbers never appear without their profile."""
    state, model = Column("state", "State", 11), Column("model", "Model", 16)
    index, cost = Column("index", "AA index", 8), Column("cost", "AA $/task", 9)
    if width >= 96:
        wide = width >= 110
        profile = Column("profile", "Guide profile", 18)
        first = Column("first", "AA first response", 17) if wide else Column("first", "AA 1st resp", 11)
        fixed = (state, model, profile, index, cost, first)
        used = 1 + sum(column.width for column in fixed) + len(fixed)
        use = Column("use", "Suggested use", min(24, width - used))
        return (state, model, use, profile, index, cost, first)
    profile = Column("profile", "Guide profile", 15)
    if width >= 76:
        return (state, model, profile, index, cost, Column("first", "AA 1st resp", 11))
    if width >= 56:
        return (state, model, profile, index)
    return (state, model)


def _profile_text(profile: str, effort: str | None, width: int) -> str:
    """The exact AA label when it fits, otherwise the effort with its fallback marker."""
    if display_width(profile) <= width or effort is None:
        return profile
    return effort + (" (fallback)" if "with fallback" in profile else "")


def _table_header(columns: tuple[Column, ...], caps: Capabilities) -> Line:
    parts = [(" ", "body")]
    for index, column in enumerate(columns):
        gap = " " if index < len(columns) - 1 else ""
        parts.append((pad(column.header, column.width, ascii_only=caps.ascii_only) + gap, "label"))
    return _line(*parts)


def _table_row(state: State, model_id: str, columns: tuple[Column, ...], caps: Capabilities) -> Line:
    model = by_id(state.catalog)[model_id]
    row = reference_rows(state.catalog)[model_id]
    badge, badge_role = _state_badge(state, model_id, caps)
    values = {"state": (badge, badge_role), "model": (model["name"], "value"),
              "use": (model["guide"]["suggested_use"], "body"),
              "profile": (None, "value"),
              "index": ("unknown" if row["intelligence"] is None else str(row["intelligence"]), "metric"),
              "cost": (format_usd(row["usd_per_task"]), "metric"),
              "first": (format_latency(row["first_chunk_s"]), "metric")}
    focused = state.focus_id == model_id
    parts = [(glyph(caps, "focus") if focused else " ", "focus" if focused else "body")]
    for index, column in enumerate(columns):
        text, role = values[column.key]
        if column.key == "profile":
            text = _profile_text(row["profile"], row["effort"], column.width)
        gap = " " if index < len(columns) - 1 else ""
        parts.append((pad(safe_text(text, caps), column.width, ascii_only=caps.ascii_only) + gap, role))
    return _line(*parts)


def _table(state: State, width: int, caps: Capabilities, ids: list[str]) -> list[Line]:
    columns = _columns(width)
    lines = [_table_header(columns, caps)]
    lines.extend(_table_row(state, model_id, columns, caps) for model_id in ids)
    if not visible_ids(state):
        lines.append(Line(_fit("No models match; Esc clears the search or filter", width, caps), "advisory"))
    return lines


# --------------------------------------------------------------------------- guidance content

def _preference_text(state: State, model_id: str) -> str:
    saved = state.preferences["saved"].get(model_id)
    meaning = {"preferred": "Preferred: a small tie-breaker among suitable eligible models, not a default or quota.",
               "available": "Available: eligible for new worker starts.",
               "disabled": "Disabled: not eligible for new worker starts."}
    text = meaning.get(saved, "Not set: not eligible in My selection; All models makes it available.")
    return text + " The running coordinator's model is unchanged."


def _short_preference(state: State, model_id: str) -> str:
    """One-line form for the stacked layout; the split layout and help give the full sentence."""
    saved = state.preferences["saved"].get(model_id)
    return {"preferred": "Preferred: tie-breaker only; coordinator model unchanged.",
            "available": "Available: eligible for new starts; coordinator unchanged.",
            "disabled": "Disabled: not eligible for new starts; coordinator unchanged."}.get(
        saved, "Not set: not eligible here; coordinator unchanged.")


def _runtime_text(state: State, *, short: bool = False) -> str:
    launch = RUNTIME_TEXT.get(state.runtime, "unknown")
    if short:
        return f"Orca launch: {launch} · model access not verified by Pod"
    return f"Orca worker launch: {launch}. Model access: not verified by Pod."


def _ladder_rows(state: State, model_id: str, caps: Capabilities, *, labelled: bool) -> list[Line]:
    """One row per stage: effort and the AA record for exactly that effort.

    `labelled` rows carry the stage in the inline label column; block rows sit under a column header.
    """
    guide = by_id(state.catalog)[model_id]["guide"]
    marker = ("<" if caps.ascii_only else "◂")
    rows = [] if labelled else [_line(*[(safe_text(text, caps), "label") for text in (
        pad("Stage", 11), pad("Effort", 7), pad("AA", 5), pad("$/task", 9), "First")])]
    for stage, effort in guide["ladder"].items():
        metric = record(state.catalog, model_id, effort)
        score = "unknown" if metric["intelligence"] is None else str(metric["intelligence"])
        guide_mark = effort == guide["profile"]
        if labelled:
            parts = [(pad(stage.upper(), LABEL_WIDTH, ascii_only=caps.ascii_only), "heading"), (pad(effort, 7), "value"),
                     (pad("AA " + score, 7), "metric"), (pad(format_usd(metric["usd_per_task"]) + "/task", 13), "metric"),
                     ("first " + format_latency(metric["first_chunk_s"]), "metric")]
            if guide_mark:
                parts.append((f"  {marker} guide", "advisory"))
        else:
            parts = [(pad(stage.capitalize(), 11), "label"), (pad(effort, 7), "value"), (pad(score, 5), "metric"),
                     (pad(format_usd(metric["usd_per_task"]), 9), "metric"),
                     (format_latency(metric["first_chunk_s"]), "metric")]
            if guide_mark:
                parts.append((f" {marker}", "advisory"))
        rows.append(_line(*[(safe_text(text, caps), role) for text, role in parts]))
    if not labelled:
        rows.append(Line(safe_text(f"{marker} guide profile shown in the table", caps), "advisory"))
    return rows


def _examples(guide: dict) -> list[str]:
    return [f"{example['effort']}: {example['text']}" for example in guide["examples"]]


def _inline(label: str, texts: list[tuple[str, str]], width: int, caps: Capabilities) -> list[Line]:
    """A label column with wrapped text beside it; continuation lines are indented."""
    room = max(1, width - LABEL_WIDTH)
    lines: list[Line] = []
    for text, role in texts:
        for row in wrap(safe_text(text, caps), room):
            prefix = pad(label, LABEL_WIDTH, ascii_only=caps.ascii_only) if not lines else " " * LABEL_WIDTH
            lines.append(_line((prefix, "heading" if not lines else "body"), (row, role)))
    return lines


def _inline_details(state: State, model_id: str, width: int, caps: Capabilities, now: datetime) -> list[Line]:
    guide = by_id(state.catalog)[model_id]["guide"]
    lines = _inline("BEST FOR", [(guide["best_for"], "body")], width, caps)
    lines += _inline("USE WHEN", [(guide["use_when"], "body")], width, caps)
    lines += [_clip_line(row, width, caps) for row in _ladder_rows(state, model_id, caps, labelled=True)]
    lines += _inline("TRADE-OFF", [(guide["trade_off"], "body"), ("Limit: " + guide["limitations"], "advisory")],
                     width, caps)
    lines += _inline("EXAMPLE", [(text, "body") for text in _examples(guide)], width, caps)
    lines += _inline("YOUR PREFERENCE", [(_short_preference(state, model_id), "body")], width, caps)
    lines += _inline("RUNTIME / ACCESS", [(_runtime_text(state, short=True), "body")], width, caps)
    source = f"AA {_captured(state)} ({_age(state, now)}). {SHORT_DISCLAIMER}"
    lines += _inline("BENCHMARK SOURCE", [(source, "advisory")], width, caps)
    return lines


def _block_details(state: State, model_id: str, width: int, caps: Capabilities, now: datetime) -> list[Line]:
    guide = by_id(state.catalog)[model_id]["guide"]
    selected = record(state.catalog, model_id, guide["profile"])
    lines: list[Line] = []

    def block(title: str, *texts: tuple[str, str]) -> None:
        lines.append(_heading(title, width, caps))
        for text, role in texts:
            lines.extend(_wrapped(text, width, caps, role))

    block("BEST FOR", (guide["best_for"], "body"))
    block("USE WHEN", (guide["use_when"], "body"))
    lines.append(_heading("QUICK / NORMAL / HARD / ESCALATION", width, caps))
    lines += [_clip_line(row, width, caps) for row in _ladder_rows(state, model_id, caps, labelled=False)]
    block("TRADE-OFF", (guide["trade_off"], "body"), ("Limit: " + guide["limitations"], "advisory"))
    block("EXAMPLE", *[(text, "body") for text in _examples(guide)])
    block("YOUR PREFERENCE", (_preference_text(state, model_id), "body"))
    block("RUNTIME / ACCESS", (_runtime_text(state), "body"))
    block("BENCHMARK SOURCE", (f"AA, captured {_captured(state)} ({_age(state, now)}); guide profile "
                               f"{selected['profile']}.", "body"), (DISCLAIMER, "advisory"))
    return lines


def _expanded_details(state: State, model_id: str, width: int, caps: Capabilities) -> list[Line]:
    model = by_id(state.catalog)[model_id]
    context = model["documented_context_tokens"]
    rank = ranks(state.catalog)[model_id]
    lines: list[Line] = []

    def block(title: str, *texts: tuple[str, str]) -> None:
        lines.append(_heading(title, width, caps))
        for text, role in texts:
            lines.extend(_wrapped(text, width, caps, role))

    block("PROVIDER DESCRIPTION", (model["guidance"], "body"))
    block("MODEL DETAILS", (f"Full id: {model_id}; native default effort: {model['native_default']}; documented "
                            f"context: {f'{context:,} tokens' if context else 'not stated'}; worker context: "
                            "native default.", "body"))
    block("AA RANK SCOPE", ("AA index rank among Pod's six at their guide profiles: "
                            f"{f'{rank}/6' if rank is not None else 'unknown'}; one benchmark and small "
                            "differences do not establish a winner.", "advisory"))
    lines.append(_heading("ALL AA BENCHMARK RECORDS", width, caps))
    for row in records(state.catalog, model_id):
        score = "unknown" if row["intelligence"] is None else row["intelligence"]
        lines += _wrapped(f"{row['profile']} [{row['effort']}]: AA index {score}; "
                          f"{format_usd(row['usd_per_task'])}/task; first {format_latency(row['first_chunk_s'])}; "
                          f"total {format_latency(row['total_response_s'])}", width, caps, "metric")
    notes = list(dict.fromkeys(row["note"] for row in records(state.catalog, model_id) if row.get("note")))
    for note in notes:
        lines += _wrapped("Note: " + note, width, caps, "advisory")
    block("SOURCES", *[(f"Provider source: {source['url']} (checked {source['checked']})", "body")
                       for source in model["sources"]], (f"AA source: {AA_URL}", "body"))
    block("BENCHMARK CAVEAT", (DISCLAIMER, "advisory"),
          ("A benchmark run 'with fallback' describes AA's harness; it does not authorize Pod to fall back.",
           "advisory"))
    return lines


def _help(state: State, width: int, caps: Capabilities) -> list[Line]:
    path = elide_middle(safe_text(state.preferences["path"], caps), max(1, width - 13), ascii_only=caps.ascii_only)
    lines = [_heading("Help: model pool preferences", width, caps)]
    for text in ("Up/Down or j/k move focus. Space changes the saved state and saves at once: "
                 "Available > Preferred > Disabled.",
                 "r switches My selection and All models; your saved choices come back.",
                 "/ searches model or id; Esc clears. f filters provider. s changes the sort.",
                 "Enter shows more; Page Up/Down scroll; Esc closes; ? help; q quits.",
                 "Sorting, filtering, search, details and help never save or start work.",
                 "Preferred is a small tie-breaker between suitable eligible models, not a default or quota. "
                 "This pool affects new workers only; the running coordinator keeps its model.",
                 "AA metrics describe each row's labelled guide profile and are informational.", DISCLAIMER,
                 "Benchmark source: " + AA_URL, "Preferences: " + path):
        lines.extend(_wrapped(text, width, caps))
    return lines


def _details(state: State, width: int, caps: Capabilities, now: datetime, *, block: bool,
             tiny: bool = False) -> list[Line]:
    if state.help_open:
        return _help(state, width, caps)
    if state.preferences["errors"]:
        error = state.preferences["errors"][0]
        reason = error.get("message") or error.get("code") or "Invalid preferences"
        action = ("Rerun the one-shot installer to create defaults" if error.get("code") == "config_missing"
                  else "Run pod config edit")
        return [_heading("PREFERENCES UNAVAILABLE — READ-ONLY", width, caps),
                *_wrapped("Reason: " + str(reason), width, caps, "error"),
                *_wrapped("Next: " + action, width, caps, "error"),
                *_wrapped("Preferences: " + elide_middle(state.preferences["path"], max(1, width - 13),
                                                         ascii_only=caps.ascii_only), width, caps)]
    visible = visible_ids(state)
    if not visible:
        return [_heading("NO MODELS MATCH", width, caps), Line("Esc clears the search or filter.", "advisory")]
    model_id = state.focus_id if state.focus_id in visible else visible[0]
    if state.expanded:
        return _expanded_details(state, model_id, width, caps)
    if block or (width < 60 and not tiny):
        return _block_details(state, model_id, width, caps, now)
    return _inline_details(state, model_id, width, caps, now)


def _page(lines: list[Line], count: int, width: int, caps: Capabilities, scroll: int) -> list[Line]:
    """A scrolled page; a cut page says how to see the rest instead of appending ellipses."""
    if count <= 0:
        return []
    start = max(0, min(scroll, max(0, len(lines) - 1)))
    page = lines[start:start + count]
    if len(lines) > start + count and count >= 3:
        page[-1] = Line(_fit(("v" if caps.ascii_only else "↓") + " more: Page Down", width, caps), "advisory")
    return page


# --------------------------------------------------------------------------- frame parts

def _title(state: State, width: int, caps: Capabilities) -> Line:
    dot = glyph(caps, "dot")
    errors = bool(state.preferences["errors"])
    mode = "READ-ONLY" if errors else "All models" if state.preferences["mode"] == "all" else "My selection"
    left = [("Pod", "title"), (dot, "label"), (mode, "error" if errors else "value"), (dot, "label"),
            (f"{len(state.preferences['eligible'])} eligible", "value")]
    if state.notice:
        failed = state.notice.startswith(("Not saved", "Preferences unavailable"))
        left += [(dot, "label"), (state.notice, "error" if failed else "advisory")]
    elif state.preferences["mode"] == "all" and not errors:
        left += [(dot, "label"), ("your choices are saved (r restores)", "advisory")]
    controls = f"Sort {SORTS[state.sort_index]}{dot}Filter {FILTERS[state.filter_index]}"
    if state.query or state.searching:
        controls += f"{dot}Search {state.query}{'_' if state.searching else ''}"
    used = sum(display_width(safe_text(text, caps)) for text, _ in left)
    room = width - used - 2
    parts = [(safe_text(text, caps), role) for text, role in left]
    if room >= display_width(safe_text(controls, caps)):
        parts.append((" " * (room - display_width(safe_text(controls, caps)) + 2), "body"))
        parts.append((safe_text(controls, caps), "label"))
    return _clip_line(_line(*parts), width, caps)


def _footer(state: State, width: int, caps: Capabilities) -> Line:
    """Descriptive shortcuts, dropping the least important ones until the line fits."""
    items = [("Space", "change state"), ("s", "sort: " + SORTS[state.sort_index]), ("f", "filter"),
             ("r", "All models" if state.preferences["mode"] != "all" else "My selection"), ("/", "search"),
             ("Enter", "more"), ("?", "help"), ("q", "quit")]
    if state.help_open or state.expanded:
        items = [("Esc", "back"), ("PgDn/PgUp", "scroll"), ("?", "help"), ("q", "quit")]
    drop_order = ["/", "Enter", "f", "r", "s"]
    separator = " " + glyph(caps, "dot").strip() + " "
    while True:
        parts: list[tuple[str, str]] = []
        for index, (key, text) in enumerate(items):
            if index:
                parts.append((separator, "label"))
            parts += [(key, "key"), (" " + text, "body")]
        line = _line(*[(safe_text(text, caps), role) for text, role in parts])
        droppable = [key for key in drop_order if any(item[0] == key for item in items)]
        if display_width(line.text) <= width or not droppable:
            return _clip_line(line, width, caps)
        items = [item for item in items if item[0] != droppable[0]]


def _details_title(state: State, focused: str | None, width: int, caps: Capabilities) -> Line:
    if state.help_open:
        return Line(_fit("Help", width, caps), "title")
    name = by_id(state.catalog)[focused]["name"] if focused else "None"
    parts = [("Details  " + name + (" [expanded]" if state.expanded else ""), "title")]
    if focused:
        badge, role = _state_badge(state, focused, caps)
        parts += [(glyph(caps, "dot"), "label"), (badge, role)]
    return _clip_line(_line(*[(safe_text(text, caps), role) for text, role in parts]), width, caps)


def _pool(state: State, width: int, caps: Capabilities) -> list[Line]:
    """Pool facts for the wide layout's space below the table."""
    prefs = state.preferences
    mode = "All models (saved choices kept)" if prefs["mode"] == "all" else "My selection (your saved states)"
    lines = [_heading("POOL", width, caps)]
    lines += _inline_pair("Mode", mode, width, caps)
    lines += _inline_pair("Eligible", f"{len(prefs['eligible'])} of 6 models; up to {prefs.get('max_active', 0)} "
                          "workers at once", width, caps)
    lines += _inline_pair("States", f"{glyph(caps, 'preferred')} Preferred = small tie-breaker; "
                          f"{glyph(caps, 'available')} Available = eligible; {glyph(caps, 'disabled')} Disabled = "
                          "not eligible", width, caps)
    lines += _inline_pair("Coordinator", "unchanged by this pool; it applies to new workers only", width, caps)
    lines += _inline_pair("AA metrics", "each row's labelled guide profile; informational, not routing",
                          width, caps)
    lines += _inline_pair("Preferences", elide_middle(safe_text(prefs["path"], caps), max(1, width - 13),
                                                      ascii_only=caps.ascii_only), width, caps)
    return lines


def _inline_pair(label: str, text: str, width: int, caps: Capabilities) -> list[Line]:
    room = max(1, width - 13)
    rows = wrap(safe_text(text, caps), room)
    return [_line((pad(label if index == 0 else "", 13, ascii_only=caps.ascii_only), "label"), (row, "body"))
            for index, row in enumerate(rows)]


def _join(left: Line, right: Line, left_width: int, caps: Capabilities) -> Line:
    used = display_width(left.text)
    return _line(*[(span.text, span.role) for span in left.styled], (" " * max(0, left_width - used), "body"),
                 (" | " if caps.ascii_only else " │ ", "label"), *[(span.text, span.role) for span in right.styled])


def _focused(state: State) -> str | None:
    visible = visible_ids(state)
    return state.focus_id if state.focus_id in visible else (visible[0] if visible else None)


def _split(state: State, width: int, rows: int, caps: Capabilities, now: datetime) -> list[Line]:
    right_width = max(44, min(60, width // 3 - 1))
    left_width = width - right_width - 3
    body_rows = rows - 2
    left = _table(state, left_width, caps, visible_ids(state))
    left += [Line("")] + _pool(state, left_width, caps)
    right = [_details_title(state, _focused(state), right_width, caps)]
    detail = _details(state, right_width, caps, now, block=True)
    scroll = state.help_scroll if state.help_open else state.detail_scroll
    right += _page(detail, body_rows - 1, right_width, caps, scroll)
    return [_join(_clip_line(left[index], left_width, caps) if index < len(left) else Line(""),
                  right[index] if index < len(right) else Line(""), left_width, caps)
            for index in range(body_rows)]


def _stacked(state: State, width: int, rows: int, caps: Capabilities, now: datetime) -> list[Line]:
    focused = _focused(state)
    ids = visible_ids(state) if not (state.expanded or state.help_open) else ([focused] if focused else [])
    lines = _table(state, width, caps, ids)
    lines.append(_details_title(state, focused, width, caps))
    room = rows - 2 - len(lines)
    detail = _details(state, width, caps, now, block=False, tiny=room <= 3)
    scroll = state.help_scroll if state.help_open else state.detail_scroll
    lines += _page(detail, room, width, caps, scroll)
    spare = room - len(detail)
    pool = _pool(state, width, caps)
    if not (state.help_open or state.expanded) and spare > len(pool):
        lines += [Line("")] + pool
    return lines


def frame(state: State, cols: int, rows: int, caps: Capabilities, now: datetime, *, strict: bool = False) -> Frame:
    """Render one screen. `strict` raises on any line wider than the drawable width."""
    if cols <= 0 or rows <= 0:
        return Frame((), cols, rows)
    width = cols - 1
    if cols < 40 or rows < 12:
        return Frame((Line(_fit("Terminal too small", width, caps), "error"),
                      Line(_fit("Resize or press q to quit", width, caps))), cols, rows)
    body = (_split if cols >= SPLIT_COLUMNS else _stacked)(state, width, rows, caps, now)
    lines = [_title(state, width, caps), *body]
    lines += [Line("") for _ in range(max(0, rows - 1 - len(lines)))]
    lines = lines[:rows - 1] + [_footer(state, width, caps)]
    overflow = [line.text for line in lines if display_width(line.text) > width]
    if overflow and strict:
        raise ValueError(f"frame line exceeds {width} columns: {overflow[0]!r}")
    return Frame(tuple(_clip_line(line, width, caps) for line in lines), cols, rows)


def summary(preferences: dict) -> str:
    if preferences["errors"]:
        return f"Pod preferences need attention: {clean(preferences['path'])}"
    mode = "All models" if preferences["mode"] == "all" else "My selection"
    return (f"Pod: {len(preferences['eligible'])} eligible models, {mode}, "
            f"maximum {preferences['max_active']} workers\nPreferences: {clean(preferences['path'])}")
