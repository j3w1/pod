"""A thin curses loop around the pure workspace state and frames.

`Workspace` holds the session's I/O without curses: it reads the one route projection, saves
explicit edits through the preference writer, runs at most one bounded observation refresh in
a background thread and notices external changes. The loop only draws frames and feeds keys.
"""

from __future__ import annotations

from datetime import datetime, timezone
import curses
import hashlib
import locale
import os
from pathlib import Path
import queue
import signal
import threading
from typing import Callable

from . import config, observations
from . import tui_state as ts
from .errors import PodError
from .routes import METRICS, project as project_routes
from .term import Capabilities, capabilities, display_width
from .tui_render import Frame, frame

TICK_MS = 200
NOTICE_SECONDS = 4
REFRESH_JOIN_S = 0.5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _unavailable(project: Path, exc: PodError) -> dict:
    try:
        path = str(config.personal_path(project))
    except PodError:
        path = "unknown"
    return {"path": path, "revision": None, "schema": None, "status": "invalid", "routes": {}, "eligible": [],
            "preferred": None, "pinned": None, "max_active": 0, "refresh": "manual",
            "errors": [{"code": exc.code, "message": str(exc)}], "setup": None,
            "pin_diagnostic": None, "preferred_diagnostic": None}


def _digest(path: Path) -> str | None:
    try:
        data = config._read_bytes(path)
    except PodError as exc:
        return "error:" + exc.code
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _cache_stamp() -> tuple | None:
    try:
        metadata = (observations.cache_root() / "current.json").stat()
    except (OSError, PodError):
        return None
    return (metadata.st_mtime_ns, metadata.st_size, metadata.st_ino)


class Workspace:
    """One workspace session: projection reads, safe saves, refresh thread and file watching."""

    def __init__(self, project: Path, *, clock: Callable[[], datetime] = _now,
                 refresher: Callable[..., dict] | None = None, automatic: bool = True):
        self.project = project
        self.clock = clock
        self.refresher = refresher or observations.refresh
        self.cancel = threading.Event()
        self.results: queue.SimpleQueue = queue.SimpleQueue()
        self.thread: threading.Thread | None = None
        self.prefs = self._preferences()
        self.state = ts.initial(self._projection(self.prefs), self._previous(self.prefs))
        self.state = ts.with_setup(self.state, self._setup_preview(self.prefs))
        self.watched = (_digest(Path(self.prefs["path"])), _cache_stamp())
        self.pending_invalid: str | None = None
        self.notice_at: datetime | None = None
        if automatic:
            self.maybe_auto_refresh()

    # ----------------------------------------------------------------- reads

    def _preferences(self) -> dict:
        try:
            return config.load(self.project)
        except PodError as exc:
            return _unavailable(self.project, exc)

    def _projection(self, prefs: dict, seen: dict | None = None) -> dict:
        return project_routes(self.project, preferences=prefs, observations=seen, now=self.clock())

    def _previous(self, prefs: dict) -> dict | None:
        """Per-route metrics from the snapshot the latest promotion replaced, through the same projection."""
        older = observations.previous()
        if older is None:
            return None
        seen = {"status": "observed", "origin": "cache", "generation": older["generation"],
                "created_at": older["created_at"], "sources": older["sources"]}
        projected = self._projection(prefs, seen)
        return {"generation": older["generation"], "created_at": older["created_at"],
                "methodology": {name: block.get("methodology") for name, block in older["sources"].items()},
                "routes": {row["key"]: {name: row["metrics"][name]["value"] for name in METRICS}
                           for row in projected["routes"]}}

    def _setup_preview(self, prefs: dict) -> dict | None:
        if prefs.get("status") != "setup_required":
            return None
        try:
            raw = config._read_bytes(Path(prefs["path"]))
            return config.setup_preview(raw) if raw is not None else None
        except PodError:
            return None

    def reload(self) -> None:
        """Re-read preferences, projection and the previous snapshot, keeping view choices."""
        self.prefs = self._preferences()
        state = ts.with_projection(self.state, self._projection(self.prefs), self._previous(self.prefs),
                                   keep_previous=False)
        if self.prefs.get("status") == "setup_required":
            if state.setup is None:
                state = ts.with_setup(state, self._setup_preview(self.prefs))
        elif state.setup is not None:
            state = ts.with_setup(state, None)
        self.state = state
        self.watched = (_digest(Path(self.prefs["path"])), _cache_stamp())

    # ----------------------------------------------------------------- refresh

    def maybe_auto_refresh(self) -> bool:
        """One bounded asynchronous refresh when automatic refresh is on and due; never blocks keys."""
        setting = self.prefs.get("refresh") if self.prefs.get("status") == "valid" else "manual"
        try:
            due = observations.auto_refresh_due(setting, now=self.clock())
        except PodError:
            due = False
        if due:
            self.start_refresh()
        return due

    def start_refresh(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.state = ts.with_refresh(self.state, "running")
        self.thread = threading.Thread(target=self._refresh_worker, daemon=True, name="pod-model-refresh")
        self.thread.start()

    def _refresh_worker(self) -> None:
        """Network and cache work only: no curses calls and no preference lock."""
        try:
            self.results.put(("done", self.refresher(cancel=self.cancel)))
        except PodError as exc:
            self.results.put(("error", f"{exc.code}: {exc}"))
        except Exception as exc:  # A refresh failure must never end the session.
            self.results.put(("error", type(exc).__name__))

    def _refresh_done(self, kind: str, result: object) -> None:
        if kind == "error":
            self.state = ts.with_refresh(self.state, "failed", str(result))
            return
        outcome = result.get("outcome")
        if outcome in ("promoted", "superseded"):
            self.reload()
            self.state = ts.with_refresh(self.state, "updated")
        elif outcome == "cancelled":
            self.state = ts.with_refresh(self.state, "cancelled")
        else:
            detail = next(iter(result.get("diagnostics") or []), "") or outcome
            self.state = ts.with_refresh(self.state, "failed", str(detail))

    # ----------------------------------------------------------------- edits

    def handle(self, key: str) -> bool:
        """Apply one key; True means quit."""
        self.state, effect = ts.reduce(self.state, key)
        if effect is None:
            return False
        if effect.kind == "quit":
            return True
        if effect.kind == "refresh":
            self.start_refresh()
        elif effect.kind == "edit":
            self._save(effect)
        elif effect.kind == "setup_apply":
            self._apply_setup(effect)
        return False

    def _failed(self, exc: Exception) -> None:
        self.reload()
        if isinstance(exc, PodError) and exc.code == "config_changed_elsewhere":
            reason = "changed elsewhere; review and press again"
        else:
            reason = str(exc) or type(exc).__name__
        self.state = ts.with_notice(self.state, f"Not saved - {reason}; file unchanged", error=True)

    def _save(self, effect: ts.Effect) -> None:
        try:
            outcome = config.edit(Path(self.prefs["path"]), displayed=self.prefs, **effect.payload)
        except (PodError, OSError) as exc:
            self._failed(exc)
            return
        self.reload()
        message = effect.label + (f"; {outcome['notice']}" if outcome.get("notice") else "")
        self.state = ts.with_notice(self.state, "Saved: " + message, saved_at=self.clock())
        self.notice_at = self.clock()

    def _apply_setup(self, effect: ts.Effect) -> None:
        try:
            outcome = config.setup_apply(Path(self.prefs["path"]), expected_revision=effect.payload["revision"],
                                         choices=effect.payload["choices"])
        except (PodError, OSError) as exc:
            self._failed(exc)
            return
        self.reload()
        self.state = ts.with_notice(self.state, f"Saved: route setup; original kept at {outcome['backup']}",
                                    saved_at=self.clock())
        self.notice_at = self.clock()

    # ----------------------------------------------------------------- polling

    def poll(self) -> bool:
        """Fold in refresh results and external changes; True when the screen should repaint."""
        dirty = False
        while True:
            try:
                kind, result = self.results.get_nowait()
            except queue.Empty:
                break
            self._refresh_done(kind, result)
            dirty = True
        current = (_digest(Path(self.prefs["path"])), _cache_stamp())
        if current != self.watched:
            candidate = self._preferences()
            if current[0] != self.watched[0] and candidate.get("status") == "invalid" \
                    and self.pending_invalid != current[0]:
                # A file that is invalid on one read may be half written; wait one more tick.
                self.pending_invalid = current[0]
            else:
                self.pending_invalid = None
                prefs_changed = current[0] != self.watched[0]
                self.reload()
                message = ("Preferences changed outside this window" if prefs_changed
                           else "Model data changed by another Pod process")
                if candidate.get("status") == "invalid":
                    message = "Preferences unavailable - read-only"
                self.state = ts.with_notice(self.state, message)
                self.notice_at = self.clock()
                dirty = True
        if self.notice_at and (self.clock() - self.notice_at).total_seconds() > NOTICE_SECONDS:
            self.state = ts.with_notice(self.state, "")
            self.notice_at = None
            dirty = True
        return dirty

    def close(self) -> None:
        """Cancel a running refresh; nothing is promoted after cancellation."""
        self.cancel.set()
        if self.thread is not None:
            self.thread.join(REFRESH_JOIN_S)


# --------------------------------------------------------------------------- curses

def _key(raw: int) -> str:
    names = {curses.KEY_UP: "UP", curses.KEY_DOWN: "DOWN", curses.KEY_LEFT: "LEFT", curses.KEY_RIGHT: "RIGHT",
             curses.KEY_NPAGE: "PAGE_DOWN", curses.KEY_PPAGE: "PAGE_UP", curses.KEY_HOME: "HOME",
             curses.KEY_END: "END", curses.KEY_ENTER: "ENTER", 10: "ENTER", 13: "ENTER",
             curses.KEY_BACKSPACE: "BACKSPACE", 8: "BACKSPACE", 127: "BACKSPACE", 27: "ESC", 3: "CTRL_C",
             16: "CTRL_P", 9: "TAB", curses.KEY_RESIZE: "RESIZE", 32: "SPACE"}
    if raw in names:
        return names[raw]
    return chr(raw) if 32 < raw < 127 else ""


ROLES = ("body", "heading", "label", "value", "metric", "advisory", "error", "badge_enabled", "badge_disabled",
         "badge_not_set", "key", "title", "focus")
BOLD = {"heading", "error", "title", "focus", "key", "badge_enabled"}


def _styles(caps: Capabilities) -> dict[str, int]:
    styles = {role: (curses.A_BOLD if role in BOLD else 0) for role in ROLES}
    if not caps.color:
        return styles
    try:
        curses.start_color()
        curses.use_default_colors()
        if curses.COLORS >= 256:
            fg = {"body": 252, "heading": 117, "label": 110, "value": 255, "metric": 215, "advisory": 222,
                  "error": 203, "badge_enabled": 121, "badge_disabled": 246, "badge_not_set": 246,
                  "key": 117, "title": 255, "focus": 255}
            if caps.light_background:
                fg.update(body=238, heading=24, label=25, value=232, metric=94, advisory=130, error=124,
                          badge_enabled=22, badge_disabled=242, badge_not_set=242, key=24, title=232, focus=232)
            focus_bg = 254 if caps.light_background else 236
        else:
            base = curses.COLOR_BLACK if caps.light_background else curses.COLOR_WHITE
            fg = {role: base for role in ROLES}
            # Many light palettes make every basic hue faint, so a light background keeps the body colour.
            if not caps.light_background:
                for role in ("heading", "label", "key"):
                    fg[role] = curses.COLOR_CYAN
                for role in ("metric", "advisory"):
                    fg[role] = curses.COLOR_YELLOW
                fg["badge_enabled"] = curses.COLOR_GREEN
            fg["error"] = curses.COLOR_RED
            focus_bg = curses.COLOR_WHITE if caps.light_background else curses.COLOR_BLUE
        for index, role in enumerate(ROLES, 1):
            curses.init_pair(index, fg[role], -1)
            styles[role] |= curses.color_pair(index)
        for index, role in enumerate(ROLES, len(ROLES) + 1):
            curses.init_pair(index, fg[role], focus_bg)
            styles[role + "_focused"] = (curses.A_BOLD if role in BOLD else 0) | curses.color_pair(index)
    except curses.error:
        pass
    return styles


def _draw(window, picture: Frame, styles: dict[str, int]) -> None:
    # A full repaint: diff-based erasing can leave stale cells on terminals that skip erase sequences.
    window.clear()
    limit = max(0, picture.columns - 1)
    for row, line in enumerate(picture.lines[:picture.rows]):
        focused = bool(line.styled and line.styled[0].role == "focus")
        column = 0
        for span in line.styled:
            if column >= limit:
                break
            attr = styles.get(span.role + ("_focused" if focused else ""), styles.get(span.role, styles["body"]))
            if focused and not attr & ~curses.A_BOLD:
                attr |= curses.A_REVERSE
            try:
                window.addnstr(row, column, span.text, limit - column, attr)
            except curses.error:
                pass
            column += display_width(span.text)
        if focused and column < limit:
            try:
                window.addnstr(row, column, " " * (limit - column), limit - column,
                               styles.get("body_focused", styles["body"] | curses.A_REVERSE))
            except curses.error:
                pass
    window.noutrefresh()
    curses.doupdate()


def _screen(window, project: Path) -> int:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    window.keypad(True)
    window.timeout(TICK_MS)
    caps = capabilities(has_colors=curses.has_colors())
    styles = _styles(caps)
    workspace = Workspace(project, automatic=False)
    quitting = False
    old_handlers = {}

    def stop(_signal, _frame):
        nonlocal quitting
        quitting = True

    for signum in (signal.SIGHUP, signal.SIGTERM):
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, stop)
    try:
        dirty, started = True, False
        while not quitting:
            if dirty:
                rows, columns = window.getmaxyx()
                picture = frame(workspace.state, columns, rows, caps, _now())
                _draw(window, picture, styles)
                workspace.state = ts.with_viewport(
                    workspace.state, table=picture.table_scroll, table_page=picture.table_page,
                    inspector=picture.inspector_scroll, inspector_page=picture.inspector_page,
                    help_scroll=picture.help_scroll)
                dirty = False
                if not started:
                    # The cached screen is already drawn; only then may an automatic refresh begin.
                    started = True
                    dirty = workspace.maybe_auto_refresh()
                    continue
            try:
                raw = window.getch()
            except curses.error:
                raw = -1
            if raw >= 0:
                key = _key(raw)
                if key:
                    if workspace.handle(key):
                        return 0
                    dirty = True
            dirty = workspace.poll() or dirty
        return 0
    finally:
        workspace.close()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def run(project: Path | None = None) -> int:
    """Start in the caller's terminal; curses is always restored on exit."""
    project = Path.cwd() if project is None else project
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        locale.setlocale(locale.LC_ALL, "C")
    curses.set_escdelay(25)
    original_term = os.environ.get("TERM")
    mono_term = False
    if "NO_COLOR" in os.environ and (original_term or "").startswith("xterm"):
        try:
            curses.setupterm("xterm-mono", fd=1)
        except curses.error:
            pass
        else:
            os.environ["TERM"] = "xterm-mono"
            mono_term = True
    try:
        return curses.wrapper(_screen, project)
    except KeyboardInterrupt:
        return 0
    finally:
        if mono_term and original_term is not None:
            os.environ["TERM"] = original_term
