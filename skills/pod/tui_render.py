"""Pure model-pool frames with labelled AA profiles and semantic spans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .catalog import age, by_id, format_latency, format_usd, ranks, record, records, reference_rows
from .term import Capabilities, clean, clip, display_width, elide_middle, glyph, pad, safe_text, wrap
from .tui_state import FILTERS, SORTS, State, visible_ids

AA_URL = "https://artificialanalysis.ai/leaderboards/models"
DISCLAIMER = ("AA $/task is AA benchmark cost, not your subscription cost, Pod-run invoice or quota use. "
              "First response is not total task duration.")
SHORT_DISCLAIMER = "AA $/task: benchmark, not bill/quota; first response is not total task time."


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


def _line(*parts: tuple[str, str]) -> Line:
    spans = tuple(Span(text, role) for text, role in parts if text)
    return Line("".join(span.text for span in spans), spans=spans)


def _fit(value: object, width: int, caps: Capabilities) -> str:
    return clip(safe_text(value, caps), width, ellipsis=True, ascii_only=caps.ascii_only)


def _wrapped(value: object, width: int, caps: Capabilities, role: str = "body") -> list[Line]:
    return [Line(row, role) for row in wrap(safe_text(value, caps), width)]


def _section(title: str, caps: Capabilities) -> Line:
    return Line(safe_text(title, caps), "heading")


def _age(state: State, now: datetime) -> str:
    days = age(state.catalog, today=now.date())
    return "unknown age" if days is None else "today" if days == 0 else "1 day old" if days == 1 else f"{days} days old"


def _columns(width: int) -> list[tuple[str, int]]:
    """Keep a complete profile label whenever AA numbers are visible."""
    if width >= 108:
        return [("state", 12), ("model", 17), ("use", 19), ("profile", 18),
                ("index", 8), ("cost", 9), ("first", 18)]
    if width >= 93:
        return [("state", 12), ("model", 17), ("use", 10), ("profile", 18),
                ("index", 8), ("cost", 8), ("first", 13)]
    if width >= 77:
        return [("state", 12), ("model", 17), ("use", 9), ("profile", 18),
                ("index", 8), ("cost", 8)]
    if width >= 59:
        return [("state", 12), ("model", 17), ("profile", 18), ("index", 8)]
    return [("state", 12), ("model", max(12, width - 14))]


def _table_header(fields: list[tuple[str, int]], caps: Capabilities) -> Line:
    labels = {"state": "State", "model": "Model", "use": "Suggested use", "profile": "Guide profile",
              "index": "AA index", "cost": "AA $/task", "first": "AA first response"}
    return _line((" ", "body"), *((pad(labels[key], size, ascii_only=caps.ascii_only), "label")
                   if index == len(fields)-1 else (pad(labels[key], size, ascii_only=caps.ascii_only)+" ", "label")
                   for index, (key, size) in enumerate(fields)))


def _table_row(state: State, model_id: str, fields: list[tuple[str, int]], caps: Capabilities) -> Line:
    model = by_id(state.catalog)[model_id]
    row = reference_rows(state.catalog)[model_id]
    value = state.preferences["effective"].get(model_id)
    badge = value.capitalize() if value in ("available", "preferred", "disabled") else "Not set"
    symbol = glyph(caps, value) if value in ("available", "preferred", "disabled") else "?"
    vals = {"state": f"{symbol} {badge}", "model": model["name"],
            "use": model["guide"]["suggested_use"], "profile": row["profile"],
            "index": "unknown" if row["intelligence"] is None else str(row["intelligence"]),
            "cost": format_usd(row["usd_per_task"]), "first": format_latency(row["first_chunk_s"])}
    focused = state.focus_id == model_id
    segments = [(glyph(caps, "focus") if focused else " ", "focus" if focused else "body")]
    for index, (key, size) in enumerate(fields):
        role = ("badge_" + (value or "unset")) if key == "state" else "metric" if key in ("index", "cost", "first") else "value" if key == "profile" else "body"
        text = pad(vals[key], size, ascii_only=caps.ascii_only)
        segments.append((text + (" " if index < len(fields)-1 else ""), role))
    return _line(*segments)


def _pref(state: State, model_id: str) -> str:
    saved = state.preferences["saved"].get(model_id)
    if saved is None:
        return "Not set (not eligible) in My selection; All models makes it available."
    return {"preferred": "Preferred: a small tie-breaker among otherwise suitable eligible models; no default or quota.",
            "available": "Available: eligible for new starts.",
            "disabled": "Disabled: not eligible for new starts."}[saved]


def _details(state: State, width: int, caps: Capabilities, now: datetime, *, compact: bool = True) -> list[Line]:
    visible = visible_ids(state)
    if state.help_open:
        path = elide_middle(safe_text(state.preferences["path"], caps), max(1, width-13), ascii_only=caps.ascii_only)
        messages = ["Help: model pool preferences", "Up/Down or j/k move focus; Space change state and save immediately.",
                    "r switches My selection and All models; saved choices return.",
                    "/ search model or id; Esc clear; f filter provider; s sort.",
                    "Enter expands details; Page Up/Down scroll; Esc closes; ? help; q quit.",
                    "Sorting, filtering, search, expansion and help do not save.",
                    "AA metrics are informational for the labelled guide profile.",
                    DISCLAIMER, "Benchmark source: " + AA_URL, "Preferences: " + path]
        return [line for message in messages for line in _wrapped(message, width, caps, "heading" if message.startswith("Help:") else "body")]
    if state.preferences["errors"]:
        error = state.preferences["errors"][0]
        reason = error.get("message") or error.get("code") or "Invalid preferences"
        action = "Rerun the one-shot installer to create defaults" if error.get("code") == "config_missing" else "Run pod config edit"
        return [_section("PREFERENCES UNAVAILABLE — READ-ONLY", caps),
                *_wrapped("Reason: " + str(reason), width, caps, "error"),
                *_wrapped("Next: " + action, width, caps, "error"),
                *_wrapped("Preferences: " + elide_middle(state.preferences["path"], max(1,width-13),ascii_only=caps.ascii_only), width,caps)]
    if not visible:
        return [_section("NO MODELS MATCH", caps), Line("Esc clears the search or filter.", "advisory")]
    model_id = state.focus_id if state.focus_id in visible else visible[0]
    model = by_id(state.catalog)[model_id]
    guide = model["guide"]
    selected = record(state.catalog, model_id, guide["profile"])
    if state.expanded:
        context = model["documented_context_tokens"]
        expanded = [_section("PROVIDER DESCRIPTION", caps)]
        expanded.extend(_wrapped(model["guidance"], width, caps))
        expanded.append(_section("MODEL DETAILS", caps))
        expanded.extend(_wrapped("Full id: " + model_id + "; native default effort: " + model["native_default"] +
                                 "; documented context: " + (f"{context:,} tokens" if context else "not stated") +
                                 "; worker context: native default.", width, caps))
        rank = ranks(state.catalog)[model_id]
        expanded.append(_section("AA RANK SCOPE", caps))
        expanded.extend(_wrapped("AA index rank among Pod's six at guide profiles: " +
                                 (f"{rank}/6" if rank is not None else "unknown") +
                                 "; small differences are not meaningful.", width, caps, "advisory"))
        expanded.append(_section("ALL AA BENCHMARK RECORDS", caps))
        for row in records(state.catalog, model_id):
            info = (f"{row['profile']} [{row['effort']}]: AA index {row['intelligence'] if row['intelligence'] is not None else 'unknown'}; "
                    f"{format_usd(row['usd_per_task'])}/task; first {format_latency(row['first_chunk_s'])}; "
                    f"total {format_latency(row['total_response_s'])}")
            expanded.extend(_wrapped(info, width, caps, "metric"))
            if row.get("note"):
                expanded.extend(_wrapped(row["note"], width, caps, "advisory"))
        for source in model["sources"]:
            expanded.extend(_wrapped("Provider source: " + source["url"] + " (checked " + source["checked"] + ")", width, caps))
        expanded.extend(_wrapped("AA source: " + AA_URL, width, caps))
        expanded.append(_section("BENCHMARK CAVEAT", caps))
        expanded.extend(_wrapped(DISCLAIMER, width, caps, "advisory"))
        return expanded
    if not state.expanded and compact and width < 120:
        def compact_line(title: str, body: str, role: str = "body") -> Line:
            label = title + "  "
            return _line((label, "heading"), (_fit(body, max(0, width-display_width(label)), caps), role))
        compact = [
            compact_line("BEST FOR", guide["best_for"]),
            compact_line("USE WHEN", guide["use_when"]),
            Line("QUICK / NORMAL / HARD / ESCALATION", "heading"),
        ]
        for stage, effort in guide["ladder"].items():
            metric = record(state.catalog, model_id, effort)
            mark = " *" if effort == guide["profile"] else ""
            compact.append(Line(_fit(f"{stage:<10} {effort:<6} AA {metric['intelligence'] if metric['intelligence'] is not None else 'unknown'}  {format_usd(metric['usd_per_task'])}/task  first {format_latency(metric['first_chunk_s'])}{mark}",width,caps), "metric"))
        compact.extend([
            compact_line("TRADE-OFF", guide["limitations"], "advisory"),
            compact_line("EXAMPLE", guide["examples"][0]["effort"] + ": " + guide["examples"][0]["text"]),
            compact_line("YOUR PREFERENCE", {"preferred":"Preferred: tie-breaker, no default/quota; running coordinator unchanged.","available":"Available: eligible; running coordinator unchanged.","disabled":"Disabled: no new starts; running coordinator unchanged."}.get(state.preferences["saved"].get(model_id), "Not set: not eligible in My selection; coordinator unchanged.")),
            compact_line("RUNTIME / ACCESS", "Orca worker launch: " + {"Not checked":"supported","Unsupported":"not advertised","Offline":"Orca offline","Unknown":"unknown"}.get(state.runtime,"unknown") + "; access unverified."),
            compact_line("BENCHMARK SOURCE", "AA " + str((state.catalog.get("benchmarks") or {}).get("captured") or "unknown") + " (" + _age(state,now) + "); guide profile " + selected["profile"] + "; informational.", "advisory"),
            Line(_fit(SHORT_DISCLAIMER,width,caps), "advisory"),
        ])
        return compact
    out: list[Line] = []
    def block(title: str, body: str, role: str = "body") -> None:
        out.append(_section(title, caps))
        out.extend(_wrapped(body, width, caps, role))
    block("BEST FOR", guide["best_for"])
    block("USE WHEN", guide["use_when"])
    out.append(_section("QUICK / NORMAL / HARD / ESCALATION", caps))
    out.append(Line(_fit("Stage       Effort  AA index  AA $/task  First response", width, caps), "label"))
    for stage, effort in guide["ladder"].items():
        metric = record(state.catalog, model_id, effort)
        mark = "*" if effort == guide["profile"] else " "
        out.append(Line(_fit(f"{stage.capitalize():<10} {effort:<6} AA {metric['intelligence'] if metric['intelligence'] is not None else 'unknown'}  {format_usd(metric['usd_per_task'])}  {format_latency(metric['first_chunk_s'])}{mark}", width, caps), "metric"))
    out.append(Line("* guide profile", "advisory"))
    block("TRADE-OFF", guide["trade_off"] + " " + guide["limitations"], "advisory")
    out.append(_section("EXAMPLE", caps))
    for example in guide["examples"]:
        out.extend(_wrapped(example["effort"] + ": " + example["text"], width, caps))
    block("YOUR PREFERENCE", _pref(state, model_id) + " The pool never changes the running coordinator's model.")
    status = {"Not checked": "supported", "Unsupported": "not advertised", "Offline": "Orca offline", "Unknown": "unknown"}.get(state.runtime, "unknown")
    block("RUNTIME / ACCESS", "Orca worker launch: " + status + ". Model access: not verified by Pod.")
    benchmark = state.catalog.get("benchmarks") or {}
    block("BENCHMARK SOURCE", "AA, captured " + str(benchmark.get("captured") or "unknown") + " (" + _age(state, now) + "). " + DISCLAIMER, "advisory")
    return out


def _crop(lines: list[Line], count: int, width: int, caps: Capabilities, scroll: int = 0) -> list[Line]:
    if count <= 0:
        return []
    page = lines[max(0,scroll):max(0,scroll)+count]
    if len(lines) > max(0,scroll)+count and page:
        last = page[-1]
        page[-1] = Line(_fit(last.text, max(1,width-2), caps) + ("..." if caps.ascii_only else "…"), "advisory")
    return page


def _join(left: Line, right: Line, left_width: int, caps: Capabilities) -> Line:
    ltext = pad(left.text, left_width, ascii_only=caps.ascii_only)
    lspans = left.styled
    used = display_width(left.text)
    return _line(*[(span.text, span.role) for span in lspans], (" " * max(0,left_width-used), "body"), (" │ " if not caps.ascii_only else " | ", "label"),
                 *[(span.text, span.role) for span in right.styled])


def frame(state: State, cols: int, rows: int, caps: Capabilities, now: datetime) -> Frame:
    if cols <= 0 or rows <= 0:
        return Frame((), cols, rows)
    width = cols-1
    if cols < 40 or rows < 12:
        return Frame((Line(_fit("Terminal too small",width,caps),"error"),
                      Line(_fit("Resize or press q to quit",width,caps))),cols,rows)
    mode = "All models" if state.preferences["mode"] == "all" else "My selection"
    if state.preferences["errors"]:
        mode = "READ-ONLY"
    title = f"Pod  {mode}  {len(state.preferences['eligible'])} eligible"
    if state.notice:
        title += "  · " + state.notice
    elif state.preferences["mode"] == "all":
        title += "  · your choices are saved (r restores)"
    title = _fit(title,width,caps)
    controls = f"Sort {SORTS[state.sort_index]}  ·  Filter {FILTERS[state.filter_index]}"
    if state.query:
        controls += "  ·  Search " + state.query
    focused = state.focus_id if state.focus_id in visible_ids(state) else (visible_ids(state)[0] if visible_ids(state) else None)
    name = by_id(state.catalog)[focused]["name"] if focused else "None"
    detail_heading = "Help" if state.help_open else "Details  " + name + (" [expanded]" if state.expanded else "")
    foot = "Space change state  ·  s sort: " + SORTS[state.sort_index] + "  ·  f filter  ·  r pool  ·  / search  ·  Enter details  ·  ? help  ·  q quit"
    if width < 100:
        foot = "Space change state  s sort: " + SORTS[state.sort_index] + "  f filter  r pool  ? help  q quit"
    if width < 65:
        foot = "Space state  PgDn more  s sort  r pool  ? help"
    foot = _fit(foot,width,caps)
    result = [Line(title,"title"), Line(_fit(controls,width,caps),"label")]
    visible = visible_ids(state)
    if cols >= 120 and not (state.expanded or state.help_open):
        left_width = 110 if width >= 151 else 93
        right_width = max(1,width-left_width-3)
        fields = _columns(left_width)
        left = [_table_header(fields,caps)]
        left.extend(_table_row(state,model_id,fields,caps) for model_id in visible)
        if not visible:
            left.append(Line("No models match; Esc clears", "advisory"))
        left.append(Line("AA metrics: labelled guide profile; informational", "advisory"))
        right = [Line(_fit(detail_heading,right_width,caps),"title")]
        right.extend(_details(state,right_width,caps,now,compact=False))
        body_count = rows-4
        for i in range(body_count):
            result.append(_join(left[i] if i<len(left) else Line(""),right[i] if i<len(right) else Line(""),left_width,caps))
    else:
        fields = _columns(width)
        result.append(_table_header(fields,caps))
        display_ids = visible if not (state.expanded or state.help_open) else ([focused] if focused else [])
        for model_id in display_ids:
            result.append(_table_row(state,model_id,fields,caps))
        if not visible:
            result.append(Line("No models match; Esc clears", "advisory"))
        if rows > 24:
            result.append(Line(_fit("AA metrics: labelled guide profile; informational",width,caps),"advisory"))
        result.append(Line(_fit(detail_heading,width,caps),"title"))
        remaining = rows-len(result)-1
        detail = _details(state,width,caps,now)
        result.extend(_crop(detail,remaining,width,caps,state.help_scroll if state.help_open else state.detail_scroll))
    result.extend(Line("") for _ in range(max(0,rows-1-len(result))))
    result.append(Line(foot,"key"))
    result = result[:rows]
    assert all(display_width(line.text) <= width for line in result), [(display_width(line.text), line.text) for line in result if display_width(line.text)>width]
    return Frame(tuple(result),cols,rows)


def summary(preferences: dict) -> str:
    if preferences["errors"]:
        return f"Pod preferences need attention: {clean(preferences['path'])}"
    mode = "All models" if preferences["mode"] == "all" else "My selection"
    return (f"Pod: {len(preferences['eligible'])} eligible models, {mode}, "
            f"maximum {preferences['max_active']} workers\nPreferences: {clean(preferences['path'])}")
