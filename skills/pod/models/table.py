"""Turn rows into aligned text: the same lines for the plain table and for the workspace.

A line is a list of (text, style) segments. Styles are names; only the workspace maps them to
colours, and every state is also a word, so nothing depends on colour.
"""

from . import registry, view

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


# The use cases and starting efforts are editorial suggestions. The links give the
# provider's basis for choosing a model, independent of its local availability.
_OPENAI = "https://developers.openai.com/api/docs"
_SELECTION = _OPENAI + "/guides/model-selection"
_CLAUDE = "https://platform.claude.com/docs/en/about-claude/models/choosing-a-model"
_ASTRA = "Try high effort for a bug whose cause spans components. Check the diagnosis against a failing test."
_SOL = "Start a well-specified feature at medium effort. Try high if the implementation misses a dependency."
_OSS = "https://openai.com/index/introducing-gpt-oss/"
_GUIDANCE = {
    "GPT-6 Astra": (_ASTRA, "https://openai.com/index/gpt-6-astra/"),
    "GPT-6.2 Astra": (_ASTRA, _SELECTION),
    "GPT-6 Sol": (_SOL, "https://openai.com/index/introducing-gpt-6-1-sol/"),
    "GPT-6.1 Sol": (_SOL, "https://openai.com/index/introducing-gpt-6-1-sol/"),
    "GPT-6.2 Sol": (_SOL, _SELECTION),
    "GPT-6.1 Terra": ("Try medium effort to trace a UI problem from screenshots. Check each proposed click.",
                      _SELECTION),
    "GPT-6 Luna": ("Try low effort to extract fields from many similar records. Spot-check exceptions first.",
                   _SELECTION),
    "Claude Opus 5.5": ("Start a cross-file review at medium effort. Try high on a change with subtle invariants.",
                        _CLAUDE),
    "Astra for Law 6.2": ("Compare on a legal research question with known authorities. Judge extra effort "
                          "by whether it surfaces relevant contrary cases.", "https://openai.com/index/astra-law-6-2/"),
    "GPT-5.3 Codex": ("Compare at medium effort on a patch that needs several edits and a regression test.",
                      _OPENAI + "/models/gpt-5.3-codex"),
    "GPT-5.5 Instant": ("Compare on a first draft you can check quickly. Switch to a reasoning model "
                        "if the task needs adjustable effort.", "https://openai.com/index/gpt-5-5-instant/"),
    "GPT-5.5 Pro": ("Compare on a difficult analysis. Try higher effort when an unresolved step matters "
                    "more than turnaround time.", _OPENAI + "/models/gpt-5.5-pro"),
    "o3": ("Compare on a multi-step math problem at medium effort. Check the intermediate results.",
           _OPENAI + "/models/o3"),
    "gpt-oss-20b": ("Try the smaller open-weight model when memory is tight. Start at low effort "
                    "on a short, verifiable task.", _OSS),
    "gpt-oss-120b": ("Compare with 20b when you have memory for larger weights. Hold effort at medium "
                     "to see whether size helps on your examples.", _OSS),
}


def guidance(row: view.Row) -> list[str]:
    """Show a suggested use and effort, followed by the public sources for that judgment."""
    name = row.model_name or registry.read_name(row.name).base
    provider = row.provider.casefold()
    if provider == "openai":
        source = _SELECTION
    elif provider == "anthropic":
        source = _CLAUDE
    else:
        return ["Compare this profile on a task with a known answer and record quality, cost, and time."]
    suggestion, source = _GUIDANCE.get(name, (
        "Compare this model on a task with a known answer. Try its lowest supported effort first "
        "if it offers a choice.", source))
    return [suggestion, source]


def detail(row: view.Row, shot: dict | None) -> list[str]:
    """The one detail panel: each state with its source, the measurements with units, and why."""
    units = (shot or {}).get("units") or {}
    lines = [row.name, *("  " + line for line in guidance(row)), f"  State: {row.state}" + (f" ({row.reason})" if row.reason else "")]
    if row.route:
        lines.append(f"  Route: {row.route}")
    lines.append("  Observed publicly: " + ("yes, on the leaderboard snapshot" + (
        f" ({shot.get('methodology') or 'methodology not stated'})" if shot else "") if row.observed
        else "no row on the leaderboard snapshot"))
    if row.observed and row.profile:
        lines.append(f"  Published profile: {row.profile}")
    if row.observed and (shot or {}).get("source"):
        lines.append(f"  Artificial Analysis: {shot['source']}")
    lines.append(f"  Installed adapter: {row.adapter}" + (
        f"; context window stated by the adapter's catalog: {tokens(row.adapter_context)}"
        if row.adapter_context else ""))
    lines.append("  Registry: " + ("routable" if row.routable else "not routable"))
    preference = row.preference or "not set, so not eligible"
    picks = "".join(label for label, held in ((", Preferred", row.preferred), (", Pin", row.pinned)) if held)
    lines.append(f"  Your preference: {preference}{picks}")
    for metric, value in row.metrics.items():
        shown = UNAVAILABLE if value is None else str(value)
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
