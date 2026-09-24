"""Pure, width-aware model TUI frames and a plain non-TTY summary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .catalog import age, by_id, ranks, reference_rows
from .term import Capabilities, clean, clip, glyph, pad, safe_text, wrap
from .tui_state import FILTERS, SORTS, State, visible_ids

AA_CLARIFICATION = ("AA metrics show each model's selected reference benchmark profile and are "
                    "informational only. Pod chooses effort/context independently for real work.")
AA_URL = "https://artificialanalysis.ai/leaderboards/models"
SESSION_LINE = "Runs through your connected Codex / Claude Code sessions."
KEYS = "↑/↓ move  Space state  r all  / find  s/f view  Enter info  ? help  q quit"


@dataclass(frozen=True)
class Line:
    text: str
    role: str = "normal"


@dataclass(frozen=True)
class Frame:
    lines: tuple[Line, ...]
    columns: int
    rows: int

    @property
    def plain(self) -> str:
        return "\n".join(line.text for line in self.lines)


def _columns(width: int) -> list[tuple[str, int]]:
    fields = [("state", 12), ("model", 16), ("agent", 7), ("rank", 6),
              ("score", 5), ("usd", 8), ("first", 7), ("runtime", 10)]
    for drop in ("runtime", "first", "usd", "rank"):
        if sum(size for _, size in fields) + len(fields) - 1 <= width:
            break
        fields = [(name, size) for name, size in fields if name != drop]
    if sum(size for _, size in fields) + len(fields) - 1 > width:
        compact = {"state": 11, "model": 13, "agent": 6, "score": 5}
        fields = [(name, compact[name]) for name, _ in fields if name in compact]
    return fields


def _table_line(state: State, model_id: str, caps: Capabilities,
                fields: list[tuple[str, int]]) -> str:
    model = by_id(state.catalog)[model_id]
    metrics = reference_rows(state.catalog)[model_id]
    saved = state.preferences["effective"].get(model_id)
    label = saved.capitalize() if saved in ("preferred", "available", "disabled") else "Not set"
    symbol = glyph(caps, saved) if saved in ("preferred", "available", "disabled") else "?"
    rank = ranks(state.catalog)[model_id]
    values = {"state": f"{symbol} {label}", "model": model["name"],
              "agent": "Claude" if model["agent"] == "claude" else "Codex",
              "rank": f"{rank}/6", "score": str(metrics["intelligence"]),
              "usd": glyph(caps, "dash") if metrics["usd_per_task"] is None else f"${metrics['usd_per_task']:g}",
              "first": glyph(caps, "dash") if metrics["first_chunk_s"] is None else f"{metrics['first_chunk_s']:g}s",
              "runtime": state.runtime}
    return " ".join(pad(safe_text(values[name], caps), size, ascii_only=caps.ascii_only)
                    for name, size in fields)


def _table_header(fields: list[tuple[str, int]], caps: Capabilities) -> str:
    labels = {"state": "State", "model": "Model", "agent": "Agent", "rank": "Rank",
              "score": "AA", "usd": "USD/task", "first": "First s", "runtime": "Runtime"}
    return " ".join(pad(safe_text(labels[name], caps), size, ascii_only=caps.ascii_only)
                    for name, size in fields)


def _metric(value: float | int | None, *, prefix: str = "", suffix: str = "", caps: Capabilities) -> str:
    return glyph(caps, "dash") if value is None else f"{prefix}{value:g}{suffix}"


def _details(state: State, width: int, budget: int, caps: Capabilities,
             now: datetime) -> list[Line]:
    if budget <= 0:
        return []
    visible = visible_ids(state)
    if not visible:
        return [Line("Details | No model focused while search has no results", "heading")]
    model_id = state.focus_id if state.focus_id in visible else visible[0]
    model = by_id(state.catalog)[model_id]
    metrics = reference_rows(state.catalog)[model_id]
    reference = state.catalog["reference_benchmark"]["models"][model_id]
    rank = ranks(state.catalog)[model_id]
    join = glyph(caps, "dot")
    header = f"Details {join} {model['name']}" + (" [expanded]" if state.expanded else "")
    lines = [Line(header, "heading")]
    if state.help_open:
        benchmark = state.catalog["reference_benchmark"]
        help_rows = [
            "Help: ↑/↓ or j/k move focus; Space cycles saved Available > Preferred > Disabled.",
            "r switches My selection and All models; individual edits restore My selection.",
            "/ searches by model, id or agent; Esc clears; s sorts; f filters providers.",
            "Enter expands Details; Esc closes; ? toggles help; q or Ctrl-C quits.",
            "Preferences: " + state.preferences["path"],
            f"Benchmark reference {benchmark['captured']}; {age(state.catalog, today=now.date())} days old.",
            AA_CLARIFICATION,
            AA_URL,
            SESSION_LINE,
        ]
        expanded = [line for item in help_rows
                    for line in wrap(safe_text(item, caps), max(1, width))]
        for item in expanded[state.help_scroll:state.help_scroll + max(0, budget - 1)]:
            lines.append(Line(item))
        return lines
    effort = ", ".join(e for e in model["efforts"] if e != "ultra")
    identity = (f"ID {model_id}{join}effort auto{join}context native_default"
                f"{join}Runtime {state.runtime}")
    aa = (f"AA {reference['reference_variant']}{join}{metrics['intelligence']}"
          f"{join}{rank} of 6{join}{_metric(metrics['usd_per_task'], prefix='$', suffix='/task', caps=caps)}"
          f"{join}{_metric(metrics['first_chunk_s'], suffix='s', caps=caps)}"
          f"{join}{state.catalog['reference_benchmark']['captured']}")
    examples = "Pod example: " + model["examples"][0]
    if state.preferences["saved"].get(model_id) is None and not state.preferences["errors"]:
        examples = "Not set (not eligible) | Space makes this model Available. " + examples
    if state.expanded and len(model["examples"]) > 1:
        examples += "; " + model["examples"][1]
    # Keep one guidance line and all identity/reference facts even on short terminals.
    identity_lines = wrap(safe_text(identity, caps), max(1, width))
    aa_lines = wrap(safe_text(aa, caps), max(1, width))
    example_lines = wrap(safe_text(examples, caps), max(1, width))
    extras: list[str] = []
    if state.expanded:
        context = model["documented_context_tokens"]
        extras.append(f"Efforts: {effort} (ultra read-only when listed).")
        extras.append(f"Documented context: {f'{context:,} tokens' if context else 'not stated'}; worker uses native default.")
        extras.append("AA score index; USD/task and first-chunk seconds are reference metrics.")
        variants = [f"{variant['profile']}: score {variant['intelligence']}, "
                    f"{_metric(variant['usd_per_task'], prefix='$', caps=caps)}/task, "
                    f"{_metric(variant['first_chunk_s'], suffix='s first', caps=caps)}"
                    for variant in reference["variants"]]
        extras.append(variants[0])
        extras.append("Official source: " + model["sources"][0]["url"])
        extras.append("AA source: " + AA_URL)
        extras.extend(variants[1:])
    mandatory_count = 1 + len(example_lines) + len(identity_lines) + len(aa_lines)
    guidance_budget = max(1, budget - mandatory_count - (min(5, len(extras)) if state.expanded else 0))
    if state.expanded:
        guidance_budget = min(guidance_budget, 2 if width >= 70 else 1)
    guidance_lines = wrap(safe_text(model["guidance"], caps), max(1, width))
    for item in guidance_lines[:guidance_budget]:
        lines.append(Line(item))
    if len(guidance_lines) > guidance_budget and lines:
        last = lines[-1]
        mark = "..." if caps.ascii_only else "…"
        lines[-1] = Line(clip(last.text, max(1, width - len(mark)), ascii_only=caps.ascii_only) + mark)
    for item in example_lines + identity_lines + aa_lines:
        lines.append(Line(item))
    for item in extras:
        if len(lines) >= budget:
            break
        lines.append(Line(safe_text(item, caps)))
    return lines[:budget]


def _footer(state: State, width: int, rows: int, caps: Capabilities, now: datetime) -> list[Line]:
    benchmark = state.catalog["reference_benchmark"]
    age_days = age(state.catalog, today=now.date())
    unit = "day" if age_days == 1 else "days"
    indicator = f"Benchmark reference{glyph(caps, 'dot')}{benchmark['captured']}{glyph(caps, 'dot')}{age_days} {unit} old"
    if rows >= 24:
        entries = [indicator, *wrap(safe_text(AA_CLARIFICATION, caps), width), AA_URL,
                   SESSION_LINE, KEYS]
    elif rows >= 20:
        entries = [indicator, *wrap(safe_text(AA_CLARIFICATION, caps), width), KEYS]
    elif rows >= 15:
        entries = [KEYS]
    else:
        entries = [KEYS]
    return [Line(safe_text(item, caps), "footer") for item in entries]


def frame(state: State, cols: int, rows: int, caps: Capabilities, now: datetime) -> Frame:
    if rows <= 0 or cols <= 0:
        return Frame((), cols, rows)
    if cols < 40 or rows < 12:
        tiny = [Line("Terminal too small", "heading"), Line("Resize, or press q to quit", "footer")]
        return Frame(tuple(Line(clip(safe_text(item.text, caps), cols, ascii_only=caps.ascii_only), item.role)
                           for item in tiny[:rows]), cols, rows)
    mode = "All models" if state.preferences["mode"] == "all" else "My selection"
    if state.preferences["mode"] == "all":
        mode += " — your choices are saved (r restores)"
    if state.preferences["errors"]:
        mode = "Preferences unavailable — read-only"
    count = len(state.preferences["eligible"])
    title = f"POD  |  {mode}  |  {count} eligible"
    path = "Preferences: " + state.preferences["path"]
    controls = (f"Sort {SORTS[state.sort_index]}  |  Filter {FILTERS[state.filter_index]}  |  "
                f"Search {'/' + state.query if state.searching else state.query or '-'}  |  ? help")
    if cols < 60:
        controls = "Space/r edit  / find  s/f view  ? help  q quit"
    if state.notice:
        controls = state.notice + "  |  " + controls
    elif count == 0:
        controls = "Delegation disabled — coordinator work remains available  |  ? help"
    elif cols < 60 and state.preferences["mode"] == "all":
        controls = "Choices saved (r restores)  |  ? help"
    top = [Line(safe_text(title, caps), "title"), Line(safe_text(path, caps)),
           Line(safe_text(controls, caps))]
    footer = _footer(state, cols, rows, caps, now)
    if state.expanded or state.help_open:
        footer = footer[-1:]
        detail_target = 15 if rows >= 24 else 13 if rows >= 20 else 9
    else:
        detail_target = 8 if rows >= 24 else 7 if rows >= 20 else 8
    remaining = rows - len(top) - 1 - len(footer)
    table_height = min(6, max(1, remaining - detail_target))
    if 20 <= rows < 24:
        table_height = min(table_height, 3)
    if cols <= 44 and rows <= 16:
        table_height = 1
    detail_budget = max(1, remaining - table_height)
    fields = _columns(cols)
    result = [*top, Line(_table_header(fields, caps), "header")]
    visible = visible_ids(state)
    if not visible:
        message = f"No models match {state.query!r} — Esc clears" if state.query else "No models match this filter — Esc clears"
        result.append(Line(safe_text(message, caps), "notice"))
        result += [Line("") for _ in range(max(0, table_height - 1))]
    elif state.help_open:
        result += [Line("") for _ in range(table_height)]
    else:
        focus_index = visible.index(state.focus_id) if state.focus_id in visible else 0
        display_focus = visible[focus_index]
        start = max(0, focus_index - table_height + 1)
        start = min(start, max(0, len(visible) - table_height))
        for model_id in visible[start:start + table_height]:
            role = "focus" if model_id == display_focus else "normal"
            marker = glyph(caps, "focus") if role == "focus" else " "
            result.append(Line(marker + _table_line(state, model_id, caps, fields), role))
        result += [Line("") for _ in range(max(0, table_height - min(table_height, len(visible))))]
    result.extend(_details(state, cols, detail_budget, caps, now))
    result += [Line("") for _ in range(max(0, detail_budget - len(result) + len(top) + 1 + table_height))]
    result.extend(footer)
    result = result[:rows]
    result += [Line("") for _ in range(rows - len(result))]
    return Frame(tuple(Line(clip(safe_text(item.text, caps), cols,
                                 ellipsis=False, ascii_only=caps.ascii_only), item.role)
                       for item in result), cols, rows)


def summary(preferences: dict) -> str:
    if preferences["errors"]:
        return f"Pod preferences need attention: {clean(preferences['path'])}"
    mode = "All models" if preferences["mode"] == "all" else "My selection"
    return (f"Pod: {len(preferences['eligible'])} eligible models, {mode}, "
            f"maximum {preferences['max_active']} workers\nPreferences: {clean(preferences['path'])}")
