"""A thin curses loop around pure model state and frames."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import curses
import hashlib
import json
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
from .orca import executable
from .term import Capabilities, capabilities, pad
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
        if process.returncode or len(output) > 2_000_000:
            results.put("Offline")
            return
        payload = json.loads(output)
        runtime = payload.get("result", {}).get("runtime") if isinstance(payload, dict) else None
        if not payload.get("ok") or not isinstance(runtime, dict):
            results.put("Offline")
            return
        advertised = runtime.get("capabilities")
        if not isinstance(advertised, list):
            results.put("Unsupported")
            return
        supported = "orchestration.worker-launch-preferences.v1" in advertised
        results.put("Not checked" if supported else "Unsupported")
    except (OSError, ValueError, AttributeError):
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
    styles = {"normal": 0, "focus": curses.A_REVERSE | curses.A_BOLD,
              "title": curses.A_BOLD, "heading": curses.A_BOLD,
              "header": curses.A_BOLD, "footer": curses.A_DIM, "notice": curses.A_BOLD,
              "preferred": curses.A_BOLD, "available": 0,
              "disabled": curses.A_DIM, "unknown": curses.A_DIM}
    if caps.color:
        try:
            curses.start_color()
            curses.use_default_colors()
            rich = curses.COLORS >= 256
            curses.init_pair(1, 30 if rich else curses.COLOR_CYAN, -1)
            curses.init_pair(2, 37 if rich else curses.COLOR_CYAN, -1)
            curses.init_pair(3, 214 if rich else curses.COLOR_YELLOW, -1)
            curses.init_pair(4, 244 if rich else curses.COLOR_WHITE, -1)
            styles["title"] |= curses.color_pair(1)
            styles["heading"] |= curses.color_pair(1)
            styles["header"] |= curses.color_pair(2)
            styles["available"] |= curses.color_pair(1)
            styles["preferred"] |= curses.color_pair(3)
            styles["disabled"] |= curses.color_pair(4)
            styles["unknown"] |= curses.color_pair(4)
        except curses.error:
            pass
    return styles


def _draw(window, picture: Frame, caps: Capabilities, styles: dict[str, int]) -> None:
    window.clear()
    for index, line in enumerate(picture.lines):
        if index >= picture.rows:
            break
        text = line.text
        if line.role == "focus":
            text = pad(text, max(0, picture.columns - 1), ascii_only=caps.ascii_only)
        try:
            window.addnstr(index, 0, text, max(0, picture.columns - 1), styles.get(line.role, 0))
            if line.state and line.state_width:
                start = 1
                state_text = text[start:start + line.state_width]
                style = styles.get(line.state, styles["unknown"])
                if line.role == "focus":
                    style |= curses.A_REVERSE | curses.A_BOLD
                window.addnstr(index, start, state_text, line.state_width, style)
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
