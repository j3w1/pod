"""The keyboard workspace for routes: browse, compare, and change your own preferences.

`Workspace` holds the state and turns keys into changes; `run` is the thin curses loop around
it. Opening the workspace reads local files only. The network is touched by the R key alone,
and the preference file by the keys that say so.
"""

import curses
import locale
import textwrap

from ..errors import PodError
from . import edit, registry, snapshot, table, view

HELP = ("↑↓ / j k   Previous or next model", "←→ / h l   Previous or next profile",
        "Enter   Sources and exact measurements", "/ search   Match model, route or state",
        "space on/off   Toggle this route", "p Preferred   Set or clear preference", "P Pin   Set or clear pin",
        "E/D   Enable or disable all model efforts", "R refresh   Fetch public measurements",
        "s sort   Index, cost, time, first answer, name or state", "r reverse   Reverse order",
        "f provider   Cycle providers", "PgUp/PgDn   Move ten models", "Home/End   First or last model",
        "[ ]   Scroll source details")
ESCAPE_WAIT_MS = 25   # how long the rest of an escape sequence may take to arrive after its ESC


class Workspace:
    def __init__(self, refresh=snapshot.refresh):
        self._refresh = refresh
        self.sort, self.reverse, self.provider, self.search = "index", False, "all", ""
        self.cursor = self.top = 0
        self.detail = self.searching = self.help = False
        self.detail_offset = 0
        self.message = ""
        self.reload()

    # ---------------------------------------------------------------- state

    def reload(self, keep=None) -> None:
        self.models = registry.load()
        self.view = view.build(models=self.models, separate_fallback=True)
        path = self.view.preferences.path
        self.raw = path.read_bytes() if self.view.preferences.status == "ok" else b""
        self._select(keep)

    def _select(self, keep=None) -> None:
        if keep is None and getattr(self, "rows", None):
            keep = self._identity(self.current)
        self.rows = [row for row in view.select(self.view, provider=self.provider, search=self.search,
                                                sort=self.sort, reverse=self.reverse) if row.observed or row.routable]
        releases = {}
        for row in self.view.rows:
            if row.model_id and row.release:
                releases.setdefault(row.model_id, set()).add(row.release)
        grouped = {}
        for i, row in enumerate(self.rows):
            known = releases.get(row.model_id, ())
            release = row.release or (next(iter(known)) if len(known) == 1 else None)
            key = (row.provider, release, None if release else row.model_id or row.model_name)
            grouped.setdefault(key, []).append(i)
        self.groups = list(grouped.values())  # first row represents the group under the active sort
        self.group_for = {index: g for g, members in enumerate(self.groups) for index in members}
        self.cursor = next((i for i, row in enumerate(self.rows) if self._identity(row) == keep), 0)

    @staticmethod
    def _identity(row):
        if row.route:
            return row.route
        if row.release:
            return (row.provider, row.release, row.profile)
        return (row.provider, row.name, row.public_index)

    def profiles(self):
        def order(i):
            row = self.rows[i]
            effort = row.effort or registry.read_name(row.name).effort
            rank = registry.EFFORTS.index(effort) if effort in registry.EFFORTS else len(registry.EFFORTS)
            return rank, row.fallback, row.name
        return sorted(self.groups[self.group_for[self.cursor]], key=order)

    @property
    def current(self):
        return self.rows[self.cursor] if self.rows else None

    def filters(self) -> list:
        observed = {row.provider for row in self.view.rows if row.observed}
        return ["all", *self.view.providers, *sorted(observed - set(self.view.providers))]

    # ---------------------------------------------------------------- changes

    def _apply(self, done: str, **changes) -> None:
        keep = self._identity(self.current) if self.current else None
        try:
            edit.apply(expected=self.raw, models=self.models, **changes)
            self.message = done
        except PodError as exc:
            self.message = f"Not changed: {exc}" + (f" To proceed: {exc.fix}." if exc.fix else "")
        self.reload(keep)

    def toggle(self) -> None:
        row = self.current
        if row is None or row.route is None:
            self.message = f"No route to change: {row.reason}." if row else "Nothing selected."
            return
        state = "disabled" if row.preference == "enabled" else "enabled"
        self._apply(f"{row.route} is now {state}.", routes={row.route: state})

    def bulk(self, state: str) -> None:
        row = self.current
        model = next((item for item in self.models if row and item.id == row.model_id and row.routable), None)
        if model is None:
            self.message = "Select a routable model to change all its efforts."
            return
        self._apply(f"Every {model.name} effort is now {state}.",
                    routes={model.route(effort): state for effort in model.efforts})

    def pick(self, name: str) -> None:
        row = self.current
        label = "Preferred route" if name == "preferred" else "Pin"
        if row is None or row.route is None:
            self.message = f"Select a route to set the {label}."
            return
        held = row.preferred if name == "preferred" else row.pinned
        self._apply(f"{label} cleared." if held else f"{label} is now {row.route}.",
                    **{name: None if held else row.route})

    def refresh(self) -> None:
        attempt = self._refresh()
        self.message = (f"Refreshed: {attempt['rows']} public rows." if attempt["ok"]
                        else f"Refresh failed: {attempt['failure']}. The earlier snapshot was kept.")
        self.reload()

    # ---------------------------------------------------------------- keys

    def handle(self, key) -> str | None:
        """Apply one key. Returns "quit", "refresh" (the caller then calls refresh()) or None."""
        if self.help:
            if key in ("?", "escape", "\x1b", "enter", "\n", "\r"):
                self.help = False
                self.detail_offset = 0
            elif key in ("down", "j", "]", "up", "k", "["):
                self.detail_offset = max(0, self.detail_offset + (1 if key in ("down", "j", "]") else -1))
            elif key in ("q", "Q"):
                return "quit"
            return None
        if self.searching:
            if key in ("\n", "\r", "enter"):
                self.searching = False
            elif key in ("\x1b", "escape"):
                self.searching, self.search = False, ""
            elif key in ("\x7f", "\b", "backspace"):
                self.search = self.search[:-1]
            elif isinstance(key, str) and len(key) == 1 and key.isprintable():
                self.search += key
            self._select()
            return None
        self.message = ""
        moves = {"up": -1, "k": -1, "down": 1, "j": 1, "pageup": -10, "pagedown": 10}
        if key in moves or key in ("home", "g", "end", "G", "left", "right", "l", "h", "/", "f", "r", "s"):
            self.detail_offset = 0
        if key in moves and self.groups:
            position = self.group_for[self.cursor]
            group = max(0, min(len(self.groups) - 1, position + moves[key]))
            if group != position:
                self.cursor = self.groups[group][0]
        elif key in ("home", "g", "end", "G") and self.groups:
            self.cursor = self.groups[0 if key in ("home", "g") else -1][0]
        elif key in ("left", "right", "h", "l") and self.groups:
            profiles = self.profiles()
            offset = 1 if key in ("right", "l") else -1
            self.cursor = profiles[(profiles.index(self.cursor) + offset) % len(profiles)]
        elif key == "?":
            self.help = True
            self.detail_offset = 0
        elif key == " ":
            self.toggle()
        elif key in ("E", "D"):
            self.bulk("enabled" if key == "E" else "disabled")
        elif key in ("p", "P"):
            self.pick("preferred" if key == "p" else "pinned")
        elif key in ("\n", "\r", "enter"):
            self.detail = not self.detail
            self.detail_offset = 0
        elif self.detail and key in ("[", "]"):
            self.detail_offset = max(0, self.detail_offset + (1 if key == "]" else -1))
        elif key == "s":
            self.sort = view.SORTS[(view.SORTS.index(self.sort) + 1) % len(view.SORTS)]
            self._select()
        elif key == "r":
            self.reverse = not self.reverse
            self._select()
        elif key == "f":
            choices = self.filters()
            position = choices.index(self.provider) if self.provider in choices else -1
            self.provider = choices[(position + 1) % len(choices)]
            self._select()
        elif key == "/":
            self.detail = False
            self.searching = True
        elif key == "R":
            self.message = "Refreshing the public snapshot: one request, no retries…"
            return "refresh"
        elif key in ("q", "Q"):
            return "quit"
        return None

    # ---------------------------------------------------------------- drawing

    def _inspector(self, width, compact):
        row = self.current
        cells = table.cells(row)
        state = row.provider + "  ·  " + row.state
        if row.preferred:
            state += " · Preferred"
        if row.pinned:
            state += " · Pin"
        values = []
        if row.metrics["intelligence_index"] is not None:
            values.append("Index " + cells["index"])
        if row.metrics["cost_per_task_usd"] is not None:
            values.append(cells["cost"] + " / task")
        missing = sum(v is None for v in row.metrics.values())
        if not values:
            values.append("Index and cost unavailable" if row.observed else "No published measurements")
        status = "Published" if row.observed else "Unpublished"
        if row.fallback:
            status += " with fallback"
        lines = [(part, "head") for part in textwrap.wrap(row.name, width)]
        lines.append(("   ".join(values), "head"))
        if compact and row.fallback:
            state += " · fallback"
        lines.append((state, table.STATE_STYLE[row.state]))
        if not compact:
            lines.extend((part, "") for part in textwrap.wrap(row.route or row.reason or "", width))
            lines.append((status, "accent"))
        advice = table.guidance(row)
        if advice:
            if not compact:
                lines += [("", ""), ("Guidance", "head")]
            lines.extend((part, "") for part in textwrap.wrap(advice[0], width))
        if compact:
            return lines
        profiles = [self.rows[i] for i in self.profiles()]
        published = [p for p in profiles if p.observed]
        lines += [("", ""), (f"Published profiles   {len(published)} / {len(profiles)}", "head")]
        for profile in published:
            label = profile.profile or "No qualifier"
            data = table.cells(profile)
            metrics = " · ".join(v for v in (data["index"], data["cost"]) if v != table.UNAVAILABLE)
            marker = "▸ " if profile is row else "  "
            lines.extend((part, "accent" if profile is row else "") for part in
                         textwrap.wrap(marker + label + ("   " + metrics if metrics else ""), width))
        unmeasured = [p.profile or "No qualifier" for p in profiles if not p.observed]
        if unmeasured:
            lines.extend((part, "dim") for part in textwrap.wrap("Unpublished: " + ", ".join(unmeasured), width))
        other = [cells["time"] + " min/task" if cells["time"] != table.UNAVAILABLE else "",
                 cells["first"] + " s to first answer token" if cells["first"] != table.UNAVAILABLE else "",
                 cells["context"] + " context" if cells["context"] != table.UNAVAILABLE else ""]
        summary = " · ".join(item for item in other if item)
        if summary:
            lines.append(("", ""))
            lines.extend((part, "") for part in textwrap.wrap(summary, width))
        if 0 < missing < len(row.metrics):
            lines.append((f"{missing} of {len(row.metrics)} measurements unavailable", "dim"))
        return lines

    def lines(self, width: int, height: int) -> list:
        """Lay out the same styled segments used by the live curses screen."""
        width = max(1, width - 1)

        def line(text, style=""):
            return ([(text[:width], style)], False)

        header = self.view.header()
        if len(header[0]) > width and self.view.snapshot:
            shot = self.view.snapshot
            header[0] = f"Public snapshot: {snapshot.age(shot.get('fetched_at'))}"
        top = [line(text, "warn" if "failed" in text or "invalid" in text else "") for text in header]
        if self.help or (self.detail and self.current):
            if self.help:
                title, items = "Keyboard shortcuts", HELP
            else:
                title = "[] scroll · " + self.current.name
                items = table.detail(self.current, self.view.snapshot)[1:]
            top.extend(line(part, "head") for part in textwrap.wrap(title, width))
            footer = line("↑↓ scroll · ? back · q quit" if self.help else "/ search · Enter back · [] scroll · q",
                          "head")
            wrapped = [part for item in items for part in textwrap.wrap(item.strip(), width)]
            room = max(1, height - len(top) - 1)
            notice = [line(self.message, "warn")] if self.message and not self.help else []
            room = max(1, room - len(notice))
            self.detail_offset = min(self.detail_offset, max(0, len(wrapped) - room))
            return (top + [line(t) for t in wrapped[self.detail_offset:self.detail_offset + room]]
                    + notice + [footer])[:height]

        status = f"Models  {len(self.groups)}  ·  {self.provider}  ·  sort: {self.sort}"
        status += " reversed" if self.reverse else ""
        if self.search:
            status += f"  ·  {self.search}"
        top.append(line(f"Search: {self.search}_" if self.searching else status, "head"))
        side = width >= 91
        left = min(62, width // 2) if side else min(width, 68)
        keys = ("↑↓ model · ←→ effort · / search · s sort · Enter sources · ? keys · q quit" if width >= 78
                else "↑↓ model  ←→ effort  Enter sources\n/ search  ? keys  q quit")
        if width >= 128:
            keys = ("↑↓ model · ←→ effort · / search · s sort · r reverse · f provider · Enter sources · R refresh\n"
                    "space on/off · E/D model on/off · p Preferred · P Pin · ? keys · q quit")
        bottom = [line(part, "head") for group in keys.splitlines() for part in textwrap.wrap(group, width)]
        if self.message:
            bottom.insert(0, line(self.message, "warn"))
        if not self.current:
            return (top + [line("No matches. / search · f provider", "dim")] + bottom)[:height]
        right = min(72, width - left - 3) if side else width
        inspector = self._inspector(right, compact=not side)
        room = max(1, height - len(top) - len(bottom))
        browser_height = room if side else max(2, room - min(len(inspector) + 1, max(4, room // 2 + 1)))
        group = self.group_for[self.cursor]
        count = max(1, min(24, browser_height - 1))
        self.top = min(max(self.top, group - count + 1), group, max(0, len(self.groups) - count))
        # Compare one exact profile per model. Arrowing through efforts changes only the focused row.
        bar_width = 7 if left >= 46 else 0
        name_width = max(1, left - bar_width - 19)

        def comparison(name, index, cost, graph, selected=False, high_value=False):
            label = ("▸ " if selected else "  ") + table._fit(name, name_width, "<")
            if left < 22:
                return [(("▸ " if selected else "  ")[:left] + name[:max(0, left - 2)], "focus" if selected else "")]
            bar = (" " + graph.ljust(bar_width)) if bar_width else ""
            return [(label, "focus" if selected else ""), (f" {index:>6}", "head"),
                    (bar, "accent"), (f" {cost:>8}", "value" if high_value else "")]
        browser = [(comparison("Model / profile", "Index", "$/task", ""), False)]
        peak = max((r.metrics["intelligence_index"] or 0 for r in self.rows), default=0)
        for g, members in enumerate(self.groups[self.top:self.top + count], self.top):
            row = self.current if g == group else self.rows[members[0]]
            score = row.metrics["intelligence_index"]
            cells = table.cells(row)
            index = "—" if score is None else f"{score:.1f}".removesuffix(".0") + ("e" if row.estimated else "")
            cost = "—" if row.metrics["cost_per_task_usd"] is None else cells["cost"]
            units = round(8 * bar_width * score / peak) if score and peak else 0
            whole, fraction = divmod(units, 8)
            bar = "█" * whole + " ▏▎▍▌▋▊▉"[fraction].strip()
            browser.append((comparison(row.name, index, cost, bar, g == group, row.value), g == group))
        if side:
            body = []
            for i in range(min(room, max(len(browser), len(inspector)))):
                segments, active = browser[i] if i < len(browser) else ([], False)
                detail = inspector[i] if i < len(inspector) else ("", "")
                pad = left - sum(len(part) for part, _style in segments)
                body.append((segments + [(" " * max(0, pad) + " │ ", "dim"), (detail[0][:right], detail[1])], active))
        else:
            body = browser + [line("─" * min(width, 68), "dim")]
            body += [line(t, style) for t, style in inspector[:max(0, room - len(body))]]
        if side and len(body) < room and len(self.groups) > count:
            last = min(self.top + count, len(self.groups))
            body.append(line(f"{self.top + 1}–{last} of {len(self.groups)} models   ·   PgUp/PgDn", "dim"))
        return (top + body + bottom)[:height]


_NAMES = {curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right", curses.KEY_UP: "up", curses.KEY_DOWN: "down",
          curses.KEY_PPAGE: "pageup", curses.KEY_NPAGE: "pagedown", curses.KEY_HOME: "home", curses.KEY_END: "end", curses.KEY_ENTER: "enter", curses.KEY_BACKSPACE: "backspace"}


def _read_key(screen):
    """One key for `Workspace.handle`, or None when the input was not a key.

    curses hands back an escape sequence it does not recognise one character at a time: ESC,
    then the rest. Whatever follows an ESC without a pause belongs to that sequence, so all of
    it is read and dropped and no part of it can act as a command. An ESC alone is the Esc key.
    """
    try:
        key = screen.get_wch()
    except curses.error:
        return None
    if key == curses.KEY_RESIZE:
        return None
    if key != "\x1b":
        return _NAMES.get(key, key) if isinstance(key, int) else key
    followed = False
    screen.timeout(ESCAPE_WAIT_MS)
    try:
        while True:
            try:
                screen.get_wch()
            except curses.error:
                break
            followed = True
    finally:
        screen.timeout(-1)
    return None if followed else "\x1b"


def _styles() -> dict:
    """Green, red and magenta on the terminal's own background read on dark and on light themes."""
    styles = dict.fromkeys(("accent", "head", "good", "value", "warn"), curses.A_BOLD)
    styles.update({"focus": curses.A_REVERSE | curses.A_BOLD, "dim": curses.A_DIM, "": 0, "off": 0})
    try:
        if curses.has_colors():
            curses.use_default_colors()
            for number, (name, colour) in enumerate((("accent", curses.COLOR_CYAN), ("good", curses.COLOR_GREEN), ("value", curses.COLOR_GREEN),
                                                     ("warn", curses.COLOR_MAGENTA), ("off", curses.COLOR_RED)), 1):
                curses.init_pair(number, colour, -1)
                styles[name] |= curses.color_pair(number)
    except curses.error:
        pass
    return styles


def _loop(screen, space: Workspace) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    styles = _styles()
    while True:
        height, width = screen.getmaxyx()
        screen.erase()
        for y, (segments, selected) in enumerate(space.lines(width, height)[:height]):
            x = 0
            for text, style in segments:
                text = text[:max(0, width - 1 - x)]
                try:
                    screen.addstr(y, x, text, styles.get(style, 0))
                except curses.error:
                    pass
                x += len(text)
        screen.refresh()
        key = _read_key(screen)
        if key is None:
            continue
        action = space.handle(key)
        if action == "quit":
            return
        if action == "refresh":
            try:
                screen.addstr(max(0, height - 3), 0, space.message[:max(0, width - 1)])
                screen.refresh()
            except curses.error:
                pass
            space.refresh()


def run() -> int:
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass    # LANG or LC_ALL names a locale this host has not generated; curses then uses the C locale
    space = Workspace()
    curses.wrapper(_loop, space)
    return 0
