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

KEYS = ("space on/off · E/D whole model on/off · p Preferred · P Pin · Enter detail · "
        "s sort · r reverse · f provider · / search · R refresh · q quit")
ESCAPE_WAIT_MS = 25   # how long the rest of an escape sequence may take to arrive after its ESC


class Workspace:
    def __init__(self, refresh=snapshot.refresh):
        self._refresh = refresh
        self.sort, self.reverse, self.provider, self.search = "index", False, "routed", ""
        self.cursor = self.top = 0
        self.detail = self.searching = False
        self.message = ""
        self.reload()

    # ---------------------------------------------------------------- state

    def reload(self, keep: str | None = None) -> None:
        self.models = registry.load()
        self.view = view.build(models=self.models)
        path = self.view.preferences.path
        self.raw = path.read_bytes() if self.view.preferences.status == "ok" else b""
        self._select(keep)

    def _select(self, keep: str | None = None) -> None:
        keep = keep or (self.current.name if getattr(self, "rows", None) else None)
        self.rows = view.select(self.view, provider=self.provider, search=self.search, sort=self.sort,
                                reverse=self.reverse)
        names = [row.name for row in self.rows]
        self.cursor = names.index(keep) if keep in names else min(self.cursor, max(0, len(self.rows) - 1))

    @property
    def current(self):
        return self.rows[self.cursor] if self.rows else None

    def filters(self) -> list:
        return ["routed", "all", *self.view.providers]

    # ---------------------------------------------------------------- changes

    def _apply(self, done: str, **changes) -> None:
        keep = self.current.name if self.current else None
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
        if key in moves:
            self.cursor = max(0, min(len(self.rows) - 1, self.cursor + moves[key]))
        elif key in ("home", "g"):
            self.cursor = 0
        elif key in ("end", "G"):
            self.cursor = max(0, len(self.rows) - 1)
        elif key == " ":
            self.toggle()
        elif key in ("E", "D"):
            self.bulk("enabled" if key == "E" else "disabled")
        elif key in ("p", "P"):
            self.pick("preferred" if key == "p" else "pinned")
        elif key in ("\n", "\r", "enter"):
            self.detail = not self.detail
        elif key == "s":
            self.sort = view.SORTS[(view.SORTS.index(self.sort) + 1) % len(view.SORTS)]
            self._select()
        elif key == "r":
            self.reverse = not self.reverse
            self._select()
        elif key == "f":
            choices = self.filters()
            self.provider = choices[(choices.index(self.provider) + 1) % len(choices)]
            self._select()
        elif key == "/":
            self.searching = True
        elif key == "R":
            self.message = "Refreshing the public snapshot: one request, no retries…"
            return "refresh"
        elif key in ("q", "Q"):
            return "quit"
        return None

    # ---------------------------------------------------------------- drawing

    def lines(self, width: int, height: int) -> list:
        """Screen lines as (segments, selected). Pure: tests read exactly what the terminal shows."""
        columns = table.columns_for(width)
        top = [([(text[:width], "warn" if "failed" in text or "invalid" in text else "")], False)
               for text in self.view.header()]
        status = (f"Showing {len(self.rows)} rows · provider: {self.provider} · sort: {self.sort}"
                  f"{' reversed' if self.reverse else ''}" + (f" · search: {self.search}" if self.search else ""))
        if self.searching:
            status = f"Search: {self.search}_   (Enter keeps it, Esc clears it)"
        top.append(([(status[:width], "")], False))
        top.append((table.header_line(columns, width, self.sort), False))
        panel = []
        if self.detail and self.current is not None:
            wrapped = [piece for text in table.detail(self.current, self.view.snapshot)
                       for piece in textwrap.wrap(text, max(20, width - 1), subsequent_indent="    ") or [""]]
            panel = [([(text, "")], False) for text in wrapped[:max(4, height - len(top) - 8)]]
        failed = self.message.startswith(("Not changed", "Refresh failed", "No route"))
        bottom = [([(self.message[:width], "warn" if failed else "good")], False),
                  ([(table.LEGEND[:width], "dim")], False),
                  *[([(text, "head")], False) for text in textwrap.wrap(KEYS, max(20, width - 1))]]
        room = max(1, height - len(top) - len(panel) - len(bottom))
        self.top = min(max(self.top, self.cursor - room + 1), self.cursor)
        body = [(table.row_line(row, columns, width), index == self.cursor)
                for index, row in enumerate(self.rows[self.top:self.top + room], self.top)]
        if not self.rows:
            body = [([("No rows match. Press f to change the provider filter or / to change the search.", "dim")],
                     False)]
        return top + body + panel + bottom


_NAMES = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_PPAGE: "pageup", curses.KEY_NPAGE: "pagedown",
          curses.KEY_HOME: "home", curses.KEY_END: "end", curses.KEY_ENTER: "enter", curses.KEY_BACKSPACE: "backspace"}


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
    styles = {"": 0, "head": curses.A_BOLD, "dim": curses.A_DIM, "good": curses.A_BOLD, "value": curses.A_BOLD,
              "warn": curses.A_BOLD, "off": 0}
    try:
        if curses.has_colors():
            curses.use_default_colors()
            for number, (name, colour) in enumerate((("good", curses.COLOR_GREEN), ("value", curses.COLOR_GREEN),
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
                    screen.addstr(y, x, text, styles.get(style, 0) | (curses.A_REVERSE if selected else 0))
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
