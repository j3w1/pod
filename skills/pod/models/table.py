"""Turn rows into aligned text: the same lines for the plain table and for the workspace.

A line is a list of (text, style) segments. Styles are names; only the workspace maps them to
colours, and every state is also a word, so nothing depends on colour.
"""

from . import view

# (key, title, width, align). Narrow terminals drop columns from the end of DROP_ORDER first.
COLUMNS = (("state", "State", 9, "<"), ("mark", "Pick", 5, "<"), ("name", "Model and effort", 0, "<"),
           ("index", "Index", 6, ">"), ("cost", "$/task", 9, ">"), ("time", "Min/task", 8, ">"),
           ("first", "1st ans s", 10, ">"), ("context", "Context", 7, ">"), ("public", "Public", 6, "<"),
           ("adapter", "Adapter", 10, "<"), ("provider", "Provider", 10, "<"))
DROP_ORDER = ("provider", "adapter", "public", "context", "first", "time", "mark")
NAME_MIN = 24
STATE_STYLE = {"enabled": "good", "disabled": "off", "routable": "", "unknown": "warn", "no route": "dim"}
UNAVAILABLE = "n/a"
LEGEND = "* high value (benchmark-relative)   e estimated index   n/a unavailable"


def columns_for(width: int) -> list:
    """The columns that fit, dropping in a fixed order and never below state, name, index and cost."""
    chosen = list(COLUMNS)
    for key in DROP_ORDER:
        if sum(column[2] + 1 for column in chosen) + NAME_MIN <= width:
            break
        chosen = [column for column in chosen if column[0] != key]
    return chosen


def tokens(value) -> str:
    if value is None:
        return UNAVAILABLE
    if value >= 1_000_000:
        return f"{value / 1_000_000:g}M"
    return f"{value / 1000:g}k"


def _number(value, pattern: str) -> str:
    return UNAVAILABLE if value is None else pattern.format(value)


def cells(row: view.Row) -> dict:
    metrics = row.metrics
    mark = "P+Pin" if row.preferred and row.pinned else "Pref" if row.preferred else "Pin" if row.pinned else ""
    index = _number(metrics["intelligence_index"], "{:g}") + ("e" if row.estimated else "")
    cost = _number(metrics["cost_per_task_usd"], "${:.2f}") + ("*" if row.value else "")
    return {"state": row.state, "mark": mark, "name": row.name, "index": index, "cost": cost,
            "time": _number(metrics["time_per_task_min"], "{:.1f}"),
            "first": _number(metrics["first_answer_token_s"], "{:.1f}"),
            "context": tokens(metrics["context_window_tokens"]),     # the public value or n/a, never the adapter's
            "public": "yes" if row.observed else "no", "adapter": row.adapter, "provider": row.provider}


def _fit(text: str, width: int, align: str) -> str:
    if len(text) > width:
        text = text[:max(0, width - 1)] + "…" if width > 1 else text[:width]
    return f"{text:{align}{width}}"


def _widths(columns: list, width: int) -> dict:
    fixed = sum(column[2] + 1 for column in columns if column[0] != "name")
    return {column[0]: (column[2] or max(NAME_MIN, width - fixed - 1)) for column in columns}


def header_line(columns: list, width: int, sort: str | None = None) -> list:
    widths = _widths(columns, width)
    text = " ".join(_fit(title + ("↓" if key == sort else ""), widths[key], align) for key, title, _w, align in columns)
    return [(text, "head")]


def row_line(row: view.Row, columns: list, width: int) -> list:
    widths, values, line = _widths(columns, width), cells(row), []
    for key, _title, _w, align in columns:
        style = STATE_STYLE[row.state] if key == "state" else "value" if key == "cost" and row.value else \
            "dim" if values[key] == UNAVAILABLE or row.state == "no route" else ""
        line.append((_fit(values[key], widths[key], align) + " ", style))
    return line


def detail(row: view.Row, shot: dict | None) -> list[str]:
    """The one detail panel: each state with its source, the measurements with units, and why."""
    units = (shot or {}).get("units") or {}
    lines = [row.name, f"  State: {row.state}" + (f" ({row.reason})" if row.reason else "")]
    if row.route:
        lines.append(f"  Route: {row.route}")
    lines.append("  Observed publicly: " + ("yes, on the leaderboard snapshot" + (
        f" ({shot.get('methodology') or 'methodology not stated'})" if shot else "") if row.observed
        else "no row on the leaderboard snapshot"))
    lines.append(f"  Installed adapter: {row.adapter}" + (
        f"; context window stated by the adapter's catalog: {tokens(row.adapter_context)}"
        if row.adapter_context else ""))
    lines.append("  Registry: " + ("routable" if row.routable else "not routable"))
    preference = row.preference or "not set, so not eligible"
    picks = "".join(label for label, held in ((", Preferred", row.preferred), (", Pin", row.pinned)) if held)
    lines.append(f"  Your preference: {preference}{picks}")
    for metric, value in row.metrics.items():
        shown = UNAVAILABLE if value is None else str(value) if isinstance(value, int) else f"{value:g}"
        note = " (estimated)" if metric == "intelligence_index" and row.estimated else ""
        lines.append(f"  {metric}: {shown}{note}" + (f"  [{units[metric]}]" if metric in units else ""))
    if row.fallback:
        lines.append("  Measured with a fallback profile; left out of the high-value comparison.")
    lines += ["  " + note for note in row.notes]
    if row.value:
        lines.append("  " + view.VALUE_EXPLANATION)
    lines.append("  " + view.COST_STATEMENT)
    return lines


def plain(model: view.View, rows: list, width: int = 120) -> str:
    """The whole table as text, for a pipe or a file."""
    columns = columns_for(width)
    lines = list(model.header())
    lines.append("".join(text for text, _style in header_line(columns, width)).rstrip())
    lines += ["".join(text for text, _style in row_line(row, columns, width)).rstrip() for row in rows]
    reasons = [f"  {row.name}: {row.reason}" for row in rows if row.reason and row.provider in model.providers]
    if reasons:
        lines += ["", "No route, with the reason:"] + reasons
    lines += ["", LEGEND, view.COST_STATEMENT]
    return "\n".join(lines) + "\n"
