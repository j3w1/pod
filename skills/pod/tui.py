"""A thin curses loop around pure model state and frames."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import curses
import hashlib
import locale
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading

from .catalog import load as load_catalog
from .config import _read_bytes, load as load_preferences, set_mode, set_model
from .errors import PodError
from .orca import MAX_OUTPUT, _envelope, executable
from .term import Capabilities, capabilities, display_width
from .tui_render import Frame, frame
from .tui_state import State, initial, reduce, refresh, with_notice, with_runtime

TICK_MS = 200
PROBE_TIMEOUT_S = 2.0


def _snapshot(project: Path) -> dict:
    try:
        return load_preferences(project)
    except PodError as exc:
        from .config import personal_path
        return {"path": str(personal_path(project)), "revision": None, "mode": None,
                "saved": {}, "effective": {}, "eligible": [], "max_active": 0,
                "errors": [{"code": exc.code, "message": str(exc)}]}


def _fingerprint(path: Path) -> str | None:
    try:
        data = _read_bytes(path)
    except PodError as exc:
        return "error:" + exc.code
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _probe_runtime(results: queue.SimpleQueue[str]) -> None:
    """One bounded native status read, with no curses calls and no account access."""
    try:
        native = executable()
    except PodError:
        results.put("Unknown")
        return
    try:
        process = subprocess.Popen([str(native), "status", "--json"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, start_new_session=True)
        try:
            output, _ = process.communicate(timeout=PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            results.put("Offline")
            return
        if process.returncode or len(output) > MAX_OUTPUT:
            results.put("Offline")
            return
        result = _envelope(output)["result"]
        runtime = result.get("runtime")
        if not isinstance(runtime, dict):
            results.put("Offline")
            return
        advertised = runtime.get("capabilities")
        if not isinstance(advertised, list):
            results.put("Unsupported")
            return
        supported = "orchestration.worker-launch-preferences.v1" in advertised
        results.put("Not checked" if supported else "Unsupported")
    except (OSError, ValueError, AttributeError, PodError):
        results.put("Offline")


def _key(raw: int) -> str:
    if raw == curses.KEY_UP:
        return "UP"
    if raw == curses.KEY_DOWN:
        return "DOWN"
    if raw == curses.KEY_NPAGE:
        return "PAGE_DOWN"
    if raw == curses.KEY_PPAGE:
        return "PAGE_UP"
    if raw in (curses.KEY_ENTER, 10, 13):
        return "ENTER"
    if raw in (curses.KEY_BACKSPACE, 8, 127):
        return "BACKSPACE"
    if raw == 27:
        return "ESC"
    if raw == 3:
        return "CTRL_C"
    if raw == curses.KEY_RESIZE:
        return "RESIZE"
    if raw == 32:
        return "SPACE"
    return chr(raw) if 32 <= raw < 127 else ""


def _styles(caps: Capabilities) -> dict[str, int]:
    roles = ("body", "heading", "label", "value", "metric", "advisory", "error",
             "badge_preferred", "badge_available", "badge_disabled", "badge_unset",
             "key", "title", "focus")
    bold = {"heading", "error", "badge_preferred", "title", "focus", "key"}
    styles = {role: (curses.A_BOLD if role in bold else 0) for role in roles}
    if caps.color:
        try:
            curses.start_color()
            curses.use_default_colors()
            rich = curses.COLORS >= 256
            if rich:
                fg = {"body": 252, "heading": 117, "label": 110, "value": 255,
                      "metric": 215, "advisory": 222, "error": 203,
                      "badge_preferred": 220, "badge_available": 121,
                      "badge_disabled": 246, "badge_unset": 246,
                      "key": 117, "title": 255, "focus": 255}
                if caps.light_background:
                    fg.update(body=238, heading=24, label=25, value=232,
                              metric=94, advisory=130, error=124,
                              badge_preferred=94, badge_available=22,
                              badge_disabled=242, badge_unset=242,
                              key=24, title=232, focus=232)
                focus_bg = 254 if caps.light_background else 236
            else:
                base = curses.COLOR_BLACK if caps.light_background else curses.COLOR_WHITE
                accent = curses.COLOR_BLUE if caps.light_background else curses.COLOR_CYAN
                fg = {role: base for role in roles}
                for role in ("heading", "label", "key"):
                    fg[role] = accent
                for role in ("metric", "advisory", "badge_preferred"):
                    fg[role] = curses.COLOR_YELLOW
                fg["badge_available"] = curses.COLOR_GREEN
                fg["error"] = curses.COLOR_RED
                focus_bg = curses.COLOR_WHITE if caps.light_background else curses.COLOR_BLUE
            for index, role in enumerate(roles, 1):
                curses.init_pair(index, fg[role], -1)
                styles[role] |= curses.color_pair(index)
            for index, role in enumerate(roles, len(roles)+1):
                curses.init_pair(index, fg[role], focus_bg)
                styles[role + "_focused"] = (curses.A_BOLD if role in bold else 0) | curses.color_pair(index)
        except curses.error:
            pass
    return styles


def _draw(window, picture: Frame, caps: Capabilities, styles: dict[str, int]) -> None:
    window.clear()
    limit = max(0, picture.columns - 1)
    for row, line in enumerate(picture.lines[:picture.rows]):
        focused = bool(line.styled and line.styled[0].role == "focus")
        column = 0
        for span in line.styled:
            if column >= limit:
                break
            attr = styles.get(span.role + ("_focused" if focused else ""), styles.get(span.role, styles["body"]))
            try:
                window.addnstr(row, column, span.text, limit-column, attr)
            except curses.error:
                pass
            column += display_width(span.text)
        if focused and column < limit:
            try:
                window.addnstr(row, column, " " * (limit-column), limit-column,
                               styles.get("body_focused", styles["body"]))
            except curses.error:
                pass
    window.noutrefresh()
    curses.doupdate()


def _apply_effect(state: State, effect, project: Path) -> State:
    if effect is None:
        return state
    path = Path(state.preferences["path"])
    try:
        if effect.kind == "set_model":
            outcome = set_model(path, effect.model_id, effect.value, displayed=state.preferences)
        elif effect.kind == "set_mode":
            outcome = set_mode(path, effect.value, displayed=state.preferences)
        else:
            return state
    except (PodError, OSError) as exc:
        current = refresh(state, _snapshot(project))
        reason = "changed elsewhere — press again" if isinstance(exc, PodError) and exc.code == "config_changed_elsewhere" else str(exc)
        return with_notice(current, f"Not saved — {reason}; file unchanged")
    current = refresh(state, _snapshot(project))
    message = "Saved just now"
    if outcome.get("mode_notice"):
        message += "; " + outcome["mode_notice"]
    if outcome.get("notice"):
        message += "; " + outcome["notice"]
    return with_notice(current, message, save_at=datetime.now(timezone.utc))


def _screen(window, project: Path, document: dict) -> int:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    window.keypad(True)
    window.timeout(TICK_MS)
    caps = capabilities(has_colors=curses.has_colors())
    styles = _styles(caps)
    state = initial(document, _snapshot(project))
    watched = _fingerprint(Path(state.preferences["path"]))
    pending_invalid: str | None = None
    invalid_ticks = 0
    results: queue.SimpleQueue[str] = queue.SimpleQueue()
    threading.Thread(target=_probe_runtime, args=(results,), daemon=True, name="pod-runtime-probe").start()
    quitting = False
    old_handlers = {}
    def stop(_signal, _frame):
        nonlocal quitting
        quitting = True
    for signum in (signal.SIGHUP, signal.SIGTERM):
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, stop)
    try:
        dirty = True
        while not quitting:
            if dirty:
                columns, rows = window.getmaxyx()[1], window.getmaxyx()[0]
                picture = frame(state, columns, rows, caps, datetime.now(timezone.utc))
                _draw(window, picture, caps, styles)
                dirty = False
            try:
                raw = window.getch()
            except curses.error:
                raw = -1
            if raw >= 0:
                key = _key(raw)
                if key:
                    state, effect = reduce(state, key)
                    if effect is not None and effect.kind == "quit":
                        return 0
                    state = _apply_effect(state, effect, project)
                    dirty = True
                    if effect is not None:
                        watched = _fingerprint(Path(state.preferences["path"]))
            try:
                runtime = results.get_nowait()
                if runtime != state.runtime:
                    state = with_runtime(state, runtime)
                    dirty = True
            except queue.Empty:
                pass
            current_hash = _fingerprint(Path(state.preferences["path"]))
            if current_hash != watched:
                candidate = _snapshot(project)
                if candidate["errors"]:
                    if pending_invalid == current_hash:
                        invalid_ticks += 1
                    else:
                        pending_invalid, invalid_ticks = current_hash, 1
                    if invalid_ticks >= 2:
                        state = refresh(state, candidate)
                        state = with_notice(state, "Preferences unavailable — read-only")
                        dirty = True
                        watched = current_hash
                        pending_invalid, invalid_ticks = None, 0
                else:
                    state = refresh(state, candidate)
                    state = with_notice(state, "Preferences changed externally")
                    dirty = True
                    watched = current_hash
                    pending_invalid, invalid_ticks = None, 0
            else:
                pending_invalid, invalid_ticks = None, 0
            if state.save_at and (datetime.now(timezone.utc) - state.save_at).total_seconds() > 2:
                state = replace(state, notice="", save_at=None)
                dirty = True
        return 0
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def run(project: Path | None = None) -> int:
    """Start in the caller's terminal; curses is always restored on exit."""
    project = Path.cwd() if project is None else project
    document = load_catalog()
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        locale.setlocale(locale.LC_ALL, "C")
    curses.set_escdelay(25)
    try:
        return curses.wrapper(_screen, project, document)
    except KeyboardInterrupt:
        return 0
