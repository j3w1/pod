"""Pure, width-aware model TUI frames and a plain non-TTY summary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .catalog import age, by_id, ranks, reference_rows
from .term import Capabilities, clean, clip, display_width, elide_middle, glyph, pad, safe_text, wrap
from .tui_state import FILTERS, SORTS, State, visible_ids

AA_CLARIFICATION = ("AA metrics show each model's selected reference benchmark profile and are "
                    "informational only. Pod chooses effort/context independently for real work.")
AA_URL = "https://artificialanalysis.ai/leaderboards/models"
SESSION_LINE = "Runs through your connected Codex / Claude Code sessions."


@dataclass(frozen=True)
class Line:
    text: str
    role: str = "normal"
    state: str | None = None
    state_width: int = 0


@dataclass(frozen=True)
class Frame:
    lines: tuple[Line, ...]
    columns: int
    rows: int

    @property
    def plain(self) -> str:
        return "\n".join(line.text for line in self.lines)


def _fit(value: object, width: int, caps: Capabilities) -> str:
    return clip(safe_text(value, caps), width, ellipsis=True, ascii_only=caps.ascii_only)


def _rule(title: str, width: int, caps: Capabilities) -> Line:
    name = _fit(title, width, caps)
    leftover = max(0, width - display_width(name) - 1)
    return Line(name + (" " + glyph(caps, "rule") * leftover if leftover else ""), "heading")


def _columns(width: int) -> list[tuple[str, int]]:
    # Width includes the common one-cell focus marker at the start of each row.
    fields = [("state", 11), ("model", 17), ("agent", 6), ("rank", 5),
              ("score", 3), ("usd", 8), ("first", 9), ("runtime", 11)]
    while 1 + sum(size for _, size in fields) + len(fields) - 1 > width:
        for drop in ("runtime", "first", "usd", "rank"):
            if any(name == drop for name, _ in fields):
                fields = [(name, size) for name, size in fields if name != drop]
                break
        else:
            break
    if 1 + sum(size for _, size in fields) + len(fields) - 1 > width:
        fields = [("state", 11), ("model", 12), ("agent", 6), ("score", 5)]
    return fields


def _cells(values: dict[str, str], fields: list[tuple[str, int]], caps: Capabilities) -> str:
    numbers = {"rank", "score", "usd", "first"}
    cells = []
    for name, size in fields:
        value = _fit(values[name], size, caps)
        if name in numbers:
            cells.append(" " * max(0, size - display_width(value)) + value)
        else:
            cells.append(pad(value, size, ascii_only=caps.ascii_only))
    return " ".join(cells)


def _table_line(state: State, model_id: str, caps: Capabilities,
                fields: list[tuple[str, int]], width: int) -> Line:
    model = by_id(state.catalog)[model_id]
    metrics = reference_rows(state.catalog)[model_id]
    saved = state.preferences["effective"].get(model_id)
    label = saved.capitalize() if saved in ("preferred", "available", "disabled") else "Not set"
    symbol = glyph(caps, saved) if saved in ("preferred", "available", "disabled") else "?"
    rank = ranks(state.catalog)[model_id]
    tied = sum(value == rank for value in ranks(state.catalog).values()) > 1
    values = {"state": f"{symbol} {label}", "model": model["name"],
              "agent": "Claude" if model["agent"] == "claude" else "Codex",
              "rank": f"{rank}{'=' if tied else ''}/6", "score": str(metrics["intelligence"]),
              "usd": glyph(caps, "dash") if metrics["usd_per_task"] is None else f"${metrics['usd_per_task']:g}",
              "first": glyph(caps, "dash") if metrics["first_chunk_s"] is None else f"{metrics['first_chunk_s']:g}s",
              "runtime": state.runtime}
    focused = state.focus_id == model_id
    content = (glyph(caps, "focus") if focused else " ") + _cells(values, fields, caps)
    return Line(_fit(content, width, caps), "focus" if focused else "normal", saved or "unknown", fields[0][1])


def _table_header(fields: list[tuple[str, int]], caps: Capabilities) -> Line:
    labels = {"state": "State", "model": "Model", "agent": "Agent", "rank": "Rank",
              "score": "AA", "usd": "USD/task", "first": "1st chunk", "runtime": "Runtime"}
    return Line(" " + _cells(labels, fields, caps), "header")


def _metric(value: float | int | None, *, prefix: str = "", suffix: str = "", caps: Capabilities) -> str:
    return glyph(caps, "dash") if value is None else f"{prefix}{value:g}{suffix}"


def _age_label(state: State, now: datetime) -> str:
    days = age(state.catalog, today=now.date())
    return "today" if days == 0 else "1 day" if days == 1 else f"{days} days"


def _rank_detail(state: State, model_id: str, caps: Capabilities) -> str:
    rank = ranks(state.catalog)[model_id]
    tied = sum(value == rank for value in ranks(state.catalog).values()) > 1
    suffix = " (tied)" if tied else ""
    return safe_text(f"{rank} of 6{suffix} — among Pod's six supported models, not AA's overall rank", caps)


def _help_rows(state: State, width: int, caps: Capabilities, now: datetime) -> list[str]:
    benchmark = state.catalog["reference_benchmark"]
    path_label = "Preferences: "
    path = elide_middle(safe_text(state.preferences["path"], caps),
                        max(1, width - display_width(path_label)), ascii_only=caps.ascii_only)
    items = [
        "Help: ↑/↓ or j/k move focus; Space cycles saved Available > Preferred > Disabled.",
        "r switches My selection and All models; your choices are saved.",
        "/ searches by model, id or agent; Esc clears; s sorts; f filters providers.",
        "Enter expands Details; Page Down/Up scrolls; Esc closes; ? toggles help; q quits.",
        AA_CLARIFICATION, AA_URL, SESSION_LINE,
        f"Benchmark reference {glyph(caps, 'dot')}{benchmark['captured']}{glyph(caps, 'dot')}{_age_label(state, now)}",
        path_label + path,
    ]
    return [line for item in items for line in wrap(safe_text(item, caps), width)]


def _bounded(lines: list[Line], budget: int, width: int, caps: Capabilities,
             *, scroll: int = 0) -> list[Line]:
    if budget <= 0:
        return []
    page = lines[max(0, scroll):max(0, scroll) + budget]
    if len(lines) > max(0, scroll) + budget and page:
        mark = "..." if caps.ascii_only else "…"
        last = page[-1]
        # Remove a whole trailing word before the overflow mark.
        words = last.text.split()
        while words and display_width(" ".join(words) + mark) > width:
            words.pop()
        page[-1] = Line((" ".join(words) + mark) if words else mark, last.role)
    return page


def _details(state: State, width: int, budget: int, caps: Capabilities,
             now: datetime) -> list[Line]:
    if budget <= 0:
        return []
    visible = visible_ids(state)
    if state.help_open:
        return _bounded([Line(row, "footer") for row in _help_rows(state, width, caps, now)],
                        budget, width, caps, scroll=state.help_scroll)
    if state.preferences["errors"]:
        error = state.preferences["errors"][0]
        reason = error.get("message") or error.get("code") or "Invalid preferences"
        missing = error.get("code") == "config_missing"
        action = "Rerun the one-shot installer to create defaults" if missing else "Run pod config edit"
        content = ["Preferences unavailable — read-only", "Reason: " + safe_text(reason, caps),
                   "Preferences: " + elide_middle(safe_text(state.preferences["path"], caps),
                                                   max(1, width - len("Preferences: ")),
                                                   ascii_only=caps.ascii_only),
                   "Next: " + action]
        return _bounded([Line(line, "notice") for item in content for line in wrap(item, width)],
                        budget, width, caps)
    if not visible:
        return [Line("No model focused while search has no results", "notice")]
    model_id = state.focus_id if state.focus_id in visible else visible[0]
    model = by_id(state.catalog)[model_id]
    metrics = reference_rows(state.catalog)[model_id]
    reference = state.catalog["reference_benchmark"]["models"][model_id]
    join = glyph(caps, "dot")
    effort = "native default"
    identity = f"ID {model_id}{join}effort auto{join}context {effort}{join}Runtime {state.runtime}"
    aa = (f"AA {reference['reference_variant']}{join}{metrics['intelligence']}"
          f"{join}{_metric(metrics['usd_per_task'], prefix='$', suffix='/task', caps=caps)}"
          f"{join}{_metric(metrics['first_chunk_s'], suffix='s', caps=caps)}"
          f"{join}{state.catalog['reference_benchmark']['captured']}")
    examples = "Pod example: " + model["examples"][0]
    if state.preferences["saved"].get(model_id) is None:
        examples = "Not set (not eligible) | " + examples
    if width < 50 and budget <= 7 and not state.expanded:
        _, suited, purpose = model["guidance"].partition("Suited to ")
        purpose = purpose if suited else model["guidance"]
        context_runtime = f"context native default; Runtime {state.runtime}"
        if display_width(context_runtime) > width:
            context_runtime = f"ctx native default; Runtime {state.runtime}"
        compact = [
            "Purpose: " + purpose.split(".", 1)[0],
            examples,
            f"ID {model_id}; effort auto",
            context_runtime,
            f"AA {reference['reference_variant']} {metrics['intelligence']} "
            f"{state.catalog['reference_benchmark']['captured']}",
        ]
        return [Line(_fit(item, width, caps)) for item in compact[:budget]]
    if state.expanded:
        context = model["documented_context_tokens"]
        guidance = [Line(row) for row in wrap(safe_text(model["guidance"], caps), width)]
        items = [
            examples, identity, aa,
            "Documented context: " + (f"{context:,} tokens" if context else "not stated") + "; worker uses native default.",
            "AA score index; USD/task and first-chunk seconds are reference metrics.",
            "Official source: " + model["sources"][0]["url"],
            "AA source: " + AA_URL,
        ]
        items.extend(f"{variant['profile']}: score {variant['intelligence']}, "
                     f"{_metric(variant['usd_per_task'], prefix='$', caps=caps)}/task, "
                     f"{_metric(variant['first_chunk_s'], suffix='s first', caps=caps)}"
                     for variant in reference["variants"])
        lines = guidance + [Line(row) for item in items for row in wrap(safe_text(item, caps), width)]
        return _bounded(lines, budget, width, caps, scroll=state.detail_scroll)
    mandatory_items = [examples, identity, aa]
    if width >= 50:
        mandatory_items.append(_rank_detail(state, model_id, caps))
    mandatory = [Line(row) for item in mandatory_items
                 for row in wrap(safe_text(item, caps), width)]
    guidance = [Line(row) for row in wrap(safe_text(model["guidance"], caps), width)]
    space = max(0, budget - len(mandatory))
    if space < len(guidance):
        guidance = _bounded(guidance, space, width, caps)
    return (guidance + mandatory)[:budget]


def _footer(state: State, width: int, rows: int, caps: Capabilities, now: datetime) -> list[Line]:
    benchmark = state.catalog["reference_benchmark"]
    indicator = (f"Benchmark reference{glyph(caps, 'dot')}{benchmark['captured']}"
                 f"{glyph(caps, 'dot')}{_age_label(state, now)}")
    keys = safe_text("↑/↓ Space / s f r Enter ? q", caps)
    on_rule = display_width(indicator) + display_width(keys) + 2 <= width
    heading = f"{indicator}  {keys}" if on_rule else indicator
    if state.expanded or state.help_open:
        entries = [heading] if on_rule else [heading, keys]
    elif rows >= 24:
        entries = [heading, *wrap(safe_text(AA_CLARIFICATION, caps), width), AA_URL, SESSION_LINE]
        if not on_rule:
            entries.append(keys)
    elif rows >= 20:
        entries = [heading] if on_rule else [heading, keys]
    else:
        entries = [keys]
    return [_rule(entries[0], width, caps)] + [Line(row, "footer") for item in entries[1:]
                                               for row in wrap(safe_text(item, caps), width)]


def frame(state: State, cols: int, rows: int, caps: Capabilities, now: datetime) -> Frame:
    if rows <= 0 or cols <= 0:
        return Frame((), cols, rows)
    width = max(1, cols - 1)  # curses never writes the bottom-right cell
    if cols < 40 or rows < 12:
        tiny = [Line("Terminal too small", "heading"), Line("Resize, or press q to quit", "footer")]
        return Frame(tuple(Line(_fit(item.text, width, caps), item.role) for item in tiny[:rows]), cols, rows)
    mode = "All models" if state.preferences["mode"] == "all" else "My selection"
    if state.preferences["errors"]:
        mode = "Preferences unavailable — read-only"
    title = (f"{'~' if caps.ascii_only else '≋'} Pod  {mode}  "
             f"{len(state.preferences['eligible'])} eligible")
    if state.notice:
        notice = ("no change saved" if state.notice.endswith("; no change saved")
                  else state.notice)
        remaining = width - display_width(title) - 3
        if remaining > 0:
            title += "  · " + _fit(notice, remaining, caps)
    elif state.preferences["mode"] == "all":
        title += " — your choices are saved (r restores)"
    elif not state.preferences["errors"] and not state.preferences["eligible"]:
        title = (f"{'~' if caps.ascii_only else '≋'} Pod  0 eligible  "
                 "Delegation disabled — coordinator work remains available")
    title = _fit(title, width, caps)
    fields = _columns(width)
    footer = _footer(state, width, rows, caps, now)
    if state.expanded or state.help_open:
        table_height = 1
    elif rows >= 24:
        table_height = 6
    elif rows >= 20:
        table_height = 3
    else:
        table_height = 1
    detail_budget = max(0, rows - 3 - table_height - 1 - len(footer))
    visible = visible_ids(state)
    model_id = state.focus_id if state.focus_id in visible else (visible[0] if visible else None)
    detail_name = by_id(state.catalog)[model_id]["name"] if model_id else "None"
    section = "Help" if state.help_open else f"Details {glyph(caps, 'dot')}{detail_name}"
    if state.expanded and not state.help_open:
        section += " [expanded]"
    controls = f"Models · Sort {SORTS[state.sort_index]} · Filter {FILTERS[state.filter_index]}"
    if not state.preferences["errors"] and not state.preferences["eligible"]:
        controls += f" · {mode}"
    if state.query:
        controls += f" · Search {state.query}"
    result = [Line(title, "title"), _rule(controls, width, caps), _table_header(fields, caps)]
    if not visible:
        message = f"No models match {state.query!r} — Esc clears" if state.query else "No models match this filter — Esc clears"
        result.append(Line(_fit(message, width, caps), "notice"))
    elif state.help_open:
        result.append(Line(""))
    elif state.expanded:
        result.append(_table_line(state, model_id, caps, fields, width))
        if table_height > 1:
            result.append(Line(safe_text(f"{max(0, len(visible) - 1)} more models · Esc returns to table", caps), "footer"))
    else:
        index = visible.index(model_id)
        start = min(max(0, index - table_height + 1), max(0, len(visible) - table_height))
        result.extend(_table_line(state, row, caps, fields, width)
                      for row in visible[start:start + table_height])
    while len(result) < 3 + table_height:
        result.append(Line(""))
    result.append(_rule(section, width, caps))
    details = _details(state, width, detail_budget, caps, now)
    result.extend(details)
    result.extend(Line("") for _ in range(max(0, detail_budget - len(details))))
    result.extend(footer)
    result = result[:rows]
    result.extend(Line("") for _ in range(rows - len(result)))
    assert all(display_width(line.text) <= width for line in result), "TUI line exceeds drawable width"
    return Frame(tuple(result), cols, rows)


def summary(preferences: dict) -> str:
    if preferences["errors"]:
        return f"Pod preferences need attention: {clean(preferences['path'])}"
    mode = "All models" if preferences["mode"] == "all" else "My selection"
    return (f"Pod: {len(preferences['eligible'])} eligible models, {mode}, "
            f"maximum {preferences['max_active']} workers\nPreferences: {clean(preferences['path'])}")
