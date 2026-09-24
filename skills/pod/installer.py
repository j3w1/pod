"""One-shot Pod installer and explicit updater. Stdlib only, including before PyYAML exists."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

PIN = "PyYAML==6.0.3"
SKILLS_CLI = "skills@1.7.0"
SOURCE = "https://codeload.github.com/j3w1/pod/tar.gz/refs/heads/main"
REINSTALL = "curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh | sh"
LAUNCHER_MARKER = "# pod-launcher/v1"
PATH_START = "# >>> pod path >>>"
PATH_END = "# <<< pod path <<<"
BANNER = ("         /\\                       /\\           /\\",
          "  ______/  \\___          ______/  \\_      __/ \\_",
          "_/#### (o)    \\__      _/### (o)    \\_    /# (o) \\",
          "\\_  ___  __/\\__  /      \\_  __/\\__  _/    \\_ __/_/",
          "  \\/   \\/      \\/          \\/     \\/         \\/",
          "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")


def _term():
    """Load the shared terminal helpers without importing Pod or PyYAML."""
    try:
        from . import term
        return term
    except ImportError:
        name = "pod_installer_term"
        module = sys.modules.get(name)
        if module is None:
            spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("term.py"))
            if spec is None or spec.loader is None:
                raise InstallError(1, "Terminal helpers are unavailable")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return module


def _clean(value: object) -> str:
    try:
        return _term().clean(value)
    except (OSError, ImportError, InstallError):
        return "Installer terminal helpers are unavailable"


class InstallError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _say(message: str) -> None:
    if not sys.stdout.isatty():
        print("pod-install: " + _clean(message), flush=True)


def _utf8() -> bool:
    setting = next((os.environ[key] for key in ("LC_ALL", "LC_CTYPE", "LANG") if os.environ.get(key)), "C")
    value = setting.upper()
    return os.environ.get("TERM") != "dumb" and ("UTF-8" in value or "UTF8" in value)


def _color() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def _paint(value: str, code: str) -> str:
    return f"\033[{code}m{value}\033[0m" if _color() else value


def _marker(state: str) -> str:
    if _utf8():
        return {"ok": "✓", "warn": "!", "fail": "✗"}[state]
    return {"ok": "[ok]", "warn": "[!]", "fail": "[x]"}[state]


def _stage(label: str, *, state: str = "ok", detail: str | None = None,
           plain: str | None = None) -> None:
    if not sys.stdout.isatty():
        if plain is not None:
            _say(plain)
        return
    marker = _paint(_marker(state), {"ok": "38;5;37", "warn": "38;5;214", "fail": "38;5;160"}[state])
    suffix = f"  {_clean(detail)}" if detail else ""
    print(f"  {marker}  {_clean(label)}{suffix}", flush=True)


def _banner(version: str) -> None:
    if not sys.stdout.isatty():
        return
    import re as _re
    for index, line in enumerate(BANNER):
        if not _color():
            print(line, flush=True)
            continue
        if index == len(BANNER) - 1:
            print(_paint(line, "38;5;30"), flush=True)
            continue
        parts = _re.split(r"(\(o\)|#+)", line)
        print("".join(_paint(part, "38;5;255" if part == "(o)" else
                             "38;5;23" if part.startswith("#") else "38;5;37") for part in parts if part), flush=True)
    print(_paint(f"             Pod {version} / one-shot install", "1;38;5;153"), flush=True)


def _section(title: str, *, stream=None) -> None:
    stream = sys.stdout if stream is None else stream
    width = min(60, max(24, shutil.get_terminal_size((80, 24)).columns - 2))
    heading = "  " + title + " "
    rule = "─" if _utf8() else "-"
    print(_paint(heading + rule * max(0, width - len(heading)), "38;5;30"), file=stream, flush=True)


def _display_path(path: Path, home: Path) -> str:
    try:
        relative = path.relative_to(home)
        value = "~" if not relative.parts else "~/" + relative.as_posix()
    except ValueError:
        value = str(path)
    width = min(52, max(14, shutil.get_terminal_size((80, 24)).columns - 24))
    return _term().elide_middle(_clean(value), width, ascii_only=not _utf8())


def _field(label: str, value: str) -> None:
    print(f"  {_paint(label.ljust(18), '38;5;153')}{_clean(value)}", flush=True)


def _path_label(path_status: str, notes: list[str]) -> str:
    if any("shadows" in note or "symlink" in note or "manually" in note or "malformed" in note
           for note in notes):
        return "manual step"
    return "ready" if "already on PATH" in path_status else "open a new shell"


def _tty_summary(paths: dict[str, Path], prefs: tuple[str, str | None], path_status: str,
                 notes: list[str], duplicates: list[str], preserved: Path | None) -> None:
    home = paths["home"]
    path_label = _path_label(path_status, notes)
    print(file=sys.stdout)
    _section("Installed")
    _field("Codex skill", _display_path(paths["canonical"], home))
    _field("Claude Code skill", _display_path(paths["claude"] / "skills/pod", home))
    _field("pod command", _display_path(paths["launcher"], home))
    state = "needs attention" if prefs[0] == "invalid" else prefs[0]
    _field("Preferences", f"{state}  {_display_path(paths['config'] / 'config.yaml', home)}")
    _field("PATH", path_label)
    if prefs[1]:
        _field("Attention", f"{prefs[1]}; run pod config edit")
    if preserved:
        _field("Preserved", _display_path(preserved, home))
    for duplicate in duplicates:
        _field("Duplicate", _display_path(Path(duplicate), home) + " (inspect before removal)")
    for note in notes:
        _field("Note", note.replace(str(home), "~"))
    _section("Environment")
    print("  " + ("Orca found (connection not checked)" if shutil.which("orca") else
                  "Orca not found (connection not checked)"), flush=True)
    if shutil.which("codex") and shutil.which("claude"):
        print("  codex/claude found (sign-in not checked)", flush=True)
    else:
        for agent in ("codex", "claude"):
            print(f"  {agent} " + ("found" if shutil.which(agent) else "not found") +
                  " (sign-in not checked)", flush=True)
    print("  Existing agent sessions and workers unchanged", flush=True)
    _section("Next")
    if path_label == "open a new shell":
        print("  Open a new shell for the pod command.", flush=True)
    elif path_label == "manual step":
        print("  Resolve the PATH note above before using the pod command.", flush=True)
    if prefs[0] == "invalid":
        print("  Correct preferences with pod config edit before delegation.", flush=True)
    print("  Codex       $pod <objective>", flush=True)
    print("  Claude Code /pod <issue-url>", flush=True)
    print("  Optional    pod  (review models)", flush=True)


def _failure(message: str, code: int) -> None:
    if not sys.stdout.isatty():
        print("pod-install: " + _clean(message), file=sys.stderr, flush=True)
        return
    print(file=sys.stderr)
    _section("Install stopped", stream=sys.stderr)
    mark = _paint(_marker("fail"), "38;5;160")
    display = _clean(message).replace(str(Path.home()), "~")
    width = max(20, shutil.get_terminal_size((80, 24)).columns - 7)
    for index, row in enumerate(_term().wrap(display, width)):
        print(f"  {mark if index == 0 else ' '}  {row}", file=sys.stderr, flush=True)
    if code == 3:
        state = "Skill copy may be incomplete; the receipt remains installing."
    elif code == 130:
        state = "Interrupted; no completed installation is claimed."
    elif code == 2:
        state = "No installation action was accepted."
    else:
        state = "The previous skill and launcher were not promoted."
    print("  State  " + state, file=sys.stderr, flush=True)
    print("  Existing agent sessions and workers unchanged.", file=sys.stderr, flush=True)
    _section("Next", stream=sys.stderr)
    if "unrelated pod command" in message:
        print("  Inspect the unrelated pod command, then rerun:", file=sys.stderr, flush=True)
    elif "foreign" in message or "redirected" in message:
        print("  Inspect the reported path, then rerun:", file=sys.stderr, flush=True)
    else:
        print("  Resolve the reported cause, then rerun:", file=sys.stderr, flush=True)
    try:
        action_rows = _term().wrap(REINSTALL, max(20, shutil.get_terminal_size((80, 24)).columns - 4))
    except (OSError, ImportError, InstallError):
        action_rows = [REINSTALL]
    for row in action_rows:
        print("  " + row, file=sys.stderr, flush=True)
    print(f"  Exit code {code}", file=sys.stderr, flush=True)


def _absolute_env(name: str, fallback: Path) -> Path:
    raw = os.environ.get(name, str(fallback))
    if not raw or len(raw) > 4096 or "\x00" in raw:
        raise InstallError(1, f"{name} must be an absolute directory path")
    path = Path(raw)
    if not path.is_absolute() or (path.exists() and not path.is_dir()):
        raise InstallError(1, f"{name} must be an absolute directory path")
    return path


def _paths() -> dict[str, Path]:
    home = Path.home()
    if not home.is_absolute():
        raise InstallError(1, "HOME must be absolute")
    data = _absolute_env("XDG_DATA_HOME", home / ".local/share") / "pod"
    config = _absolute_env("POD_CONFIG_HOME", _absolute_env("XDG_CONFIG_HOME", home / ".config") / "pod")
    state = _absolute_env("XDG_STATE_HOME", home / ".local/state")
    claude = _absolute_env("CLAUDE_CONFIG_DIR", home / ".claude")
    codex = _absolute_env("CODEX_HOME", home / ".agents")
    if "ZDOTDIR" in os.environ:
        zsh_home = _absolute_env("ZDOTDIR", home)
        if zsh_home.is_symlink():
            raise InstallError(1, "Refuse redirected ZDOTDIR")
    for path in (data, config, state, claude, codex):
        if path.is_symlink():
            raise InstallError(1, f"Refuse redirected directory: {path}")
    return {"home": home, "data": data, "config": config, "state": state,
            "claude": claude, "codex": codex, "canonical": home / ".agents/skills/pod",
            "launcher": home / ".local/bin/pod"}


def _inventory(root: Path) -> tuple[str, ...]:
    source = (root / "bundle.py").read_text(encoding="utf-8")
    if len(source) > 65536:
        raise InstallError(1, "Bundle inventory is oversized")
    entries = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in ("BUNDLE_TEXT", "BUNDLE_MODULES"):
                entries[node.targets[0].id] = ast.literal_eval(node.value)
    if set(entries) != {"BUNDLE_TEXT", "BUNDLE_MODULES"}:
        raise InstallError(1, "Bundle inventory is incomplete")
    names = tuple(entries["BUNDLE_TEXT"]) + tuple(entries["BUNDLE_MODULES"])
    if len(names) != len(set(names)) or any(not isinstance(name, str) or name.startswith("/") or ".." in name.split("/") for name in names):
        raise InstallError(1, "Bundle inventory is unsafe")
    return names


def bundle_digest(root: Path) -> str:
    """Digest the authored bundle inventory, independent of symlink copy shape."""
    digest = hashlib.sha256()
    for name in sorted(_inventory(root)):
        path = root.joinpath(*name.split("/"))
        data = path.read_bytes()
        digest.update(name.encode() + b"\0" + len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def optional_digest(root: Path) -> str | None:
    try:
        return bundle_digest(root) if root.is_dir() else None
    except (OSError, UnicodeError, ValueError, SyntaxError, InstallError):
        return None


def _version(root: Path) -> str:
    try:
        value = (root / "VERSION").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise InstallError(1, "Source VERSION is unreadable") from exc
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise InstallError(1, "Source VERSION is invalid")
    return value


def receipt_path() -> Path:
    return _paths()["data"] / "install.json"


def read_receipt(path: Path) -> dict | None:
    if path.is_symlink():
        raise InstallError(1, "Installation receipt is redirected")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 64 * 1024:
        raise InstallError(1, "Installation receipt is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise InstallError(1, "Installation receipt is unreadable") from exc
    target = value.get("target") if isinstance(value, dict) else None
    previous = value.get("previous") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("schema") != "pod-install/v1"
            or value.get("status") not in ("installing", "installed")
            or not isinstance(target, dict)
            or not re.fullmatch(r"\d+\.\d+\.\d+", str(target.get("version", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(target.get("digest", "")))
            or (previous is not None and (not isinstance(previous, dict)
                                           or not re.fullmatch(r"[0-9a-f]{64}", str(previous.get("digest", "")))))):
        raise InstallError(1, "Installation receipt has an unsupported schema")
    return value


def _atomic(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.is_symlink():
        raise InstallError(1, f"Refuse redirected owned path: {path}")
    fd, name = tempfile.mkstemp(prefix=".pod-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _write_receipt(path: Path, value: dict) -> None:
    _atomic(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def _launcher_template(venv_python: Path, canonical: Path) -> bytes:
    import shlex
    script = canonical / "scripts/pod.py"
    return ("#!/bin/sh\n" + LAUNCHER_MARKER + "\n"
            + "if [ ! -x " + shlex.quote(str(venv_python)) + " ] || [ ! -f "
            + shlex.quote(str(script)) + " ]; then\n"
            + "  echo 'pod: installation incomplete; rerun " + REINSTALL + "' >&2\n"
            + "  exit 2\nfi\n"
            + "exec " + shlex.quote(str(venv_python)) + " -I "
            + shlex.quote(str(script)) + " \"$@\"\n").encode()


def launcher_info(path: Path, template: bytes) -> dict:
    if path.is_symlink():
        return {"owned": False, "reason": "symlink"}
    if not path.exists():
        return {"owned": False, "reason": "missing"}
    if not path.is_file() or path.stat().st_size > 16 * 1024:
        return {"owned": False, "reason": "foreign"}
    actual = path.read_bytes()
    return {"owned": actual == template,
            "reason": "owned" if actual == template else "foreign"}


def _reconcilable_copy(canonical: Path, receipt: dict | None, source_bundle: Path) -> bool:
    """An interrupted skills-CLI copy can be replaced only for its exact target."""
    if (not receipt or receipt.get("status") != "installing" or not canonical.is_dir()
            or canonical.is_symlink() or receipt["target"]["version"] != _version(source_bundle)
            or receipt["target"]["digest"] != bundle_digest(source_bundle)):
        return False
    names = set(_inventory(source_bundle))
    directories = {str(parent) for name in names for parent in Path(name).parents if str(parent) != "."}
    for child in canonical.rglob("*"):
        relative = child.relative_to(canonical).as_posix()
        if child.is_symlink():
            return False
        if child.is_dir():
            if relative not in directories:
                return False
        elif child.is_file():
            if relative not in names:
                return False
        else:
            return False
    return True


def _preflight(stage: Path, paths: dict[str, Path], template: bytes) -> None:
    if platform.system() != "Linux" or os.geteuid() == 0:
        raise InstallError(1, "Run as a non-root user on Linux")
    if sys.version_info < (3, 13):
        raise InstallError(1, "Python 3.13+ is required; install it, then rerun the installer")
    try:
        import ensurepip  # noqa: F401
        import curses  # noqa: F401
    except ImportError as exc:
        raise InstallError(1, "Python venv/ensurepip/curses is required (on Debian, install python3.13-venv)") from exc
    if shutil.which("npx") is None or shutil.which("node") is None:
        raise InstallError(1, "Node and npx are required")
    source_bundle = stage / "skills/pod"
    if not source_bundle.is_dir() or not (source_bundle / "SKILL.md").is_file():
        raise InstallError(1, "Source has no Pod skill")
    _version(source_bundle)
    for name in _inventory(source_bundle):
        if not source_bundle.joinpath(*name.split("/")).is_file():
            raise InstallError(1, f"Source bundle file {name} is missing")
    receipt = read_receipt(paths["data"] / "install.json")
    canonical = paths["canonical"]
    if canonical.is_symlink() or (canonical.exists() and not canonical.is_dir()):
        raise InstallError(1, f"Refuse foreign canonical skill path: {canonical}")
    if canonical.is_dir():
        incomplete = (not (canonical / "SKILL.md").is_file()
                      or receipt is not None and receipt["status"] == "installing"
                      and optional_digest(canonical) is None)
        if incomplete and not _reconcilable_copy(canonical, receipt, source_bundle):
            raise InstallError(1, f"Refuse foreign canonical skill path: {canonical}")
    claude_link = paths["claude"] / "skills/pod"
    if claude_link.exists() or claude_link.is_symlink():
        if not claude_link.is_symlink() or claude_link.resolve(strict=False) != canonical.resolve(strict=False):
            raise InstallError(1, f"Refuse foreign Claude skill path: {claude_link}")
    launcher = launcher_info(paths["launcher"], template)
    prior_template = receipt.get("launcher_digest") if receipt else None
    prior_owned = (paths["launcher"].is_file() and not paths["launcher"].is_symlink()
                   and prior_template == hashlib.sha256(paths["launcher"].read_bytes()).hexdigest())
    if launcher["reason"] not in ("missing", "owned") and not prior_owned:
        raise InstallError(1, f"Refuse unrelated pod command: {paths['launcher']}")
    lock = paths["data"] / ".install.lock"
    if lock.is_symlink():
        raise InstallError(1, "Installation lock is redirected")


def _venv_path(paths: dict[str, Path]) -> Path:
    key = hashlib.sha256((str(_base_python()) + "\0" + PIN).encode()).hexdigest()[:12]
    return paths["data"] / ("venv-" + key)


def _base_python() -> Path:
    return Path(getattr(sys, "_base_executable", sys.executable)).resolve()


def _venv_ready(path: Path) -> bool:
    marker = path / ".pod-venv"
    if not marker.is_file() or marker.is_symlink():
        return False
    try:
        value = json.loads(marker.read_text())
        if value != {"pin": PIN, "python": str(_base_python())}:
            return False
        result = subprocess.run([str(path / "bin/python"), "-B", "-c", "import yaml; print(yaml.__version__)"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        return result.returncode == 0 and result.stdout.strip() == PIN.split("==", 1)[1]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _venv(paths: dict[str, Path]) -> Path:
    path = _venv_path(paths)
    if _venv_ready(path):
        return path / "bin/python"
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise InstallError(1, f"Refuse foreign venv path: {path}")
    building = Path(tempfile.mkdtemp(prefix="venv-build-", dir=paths["data"]))
    try:
        shutil.rmtree(building)
        result = subprocess.run([str(_base_python()), "-m", "venv", str(building)], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise InstallError(1, "Python venv creation failed: " + result.stderr.strip()[:300])
        result = subprocess.run([str(building / "bin/python"), "-m", "pip", "install", "--no-input",
                                 "--disable-pip-version-check", "--only-binary=:all:", PIN],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise InstallError(1, "PyYAML installation failed; check network or wheel source")
        _atomic(building / ".pod-venv", json.dumps({"pin": PIN, "python": str(_base_python())}).encode())
        if not _venv_ready(building):
            raise InstallError(1, "Pinned PyYAML could not be validated")
        if path.exists():
            previous = paths["data"] / (path.name + "-preserved")
            if previous.exists():
                raise InstallError(1, f"Refuse existing preserved venv path: {previous}")
            os.replace(path, previous)
        os.replace(building, path)
    except subprocess.TimeoutExpired as exc:
        raise InstallError(1, "Dependency installation timed out") from exc
    finally:
        if building.exists():
            shutil.rmtree(building)
    return path / "bin/python"


def _check_bundle(python: Path, stage: Path) -> str:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(stage / "skills")
    for command in ([str(python), "-m", "pod.skill_validation", str(stage / "skills/pod")],
                    [str(python), "-m", "pod.catalog", "--check"]):
        result = subprocess.run(command, cwd=stage, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=40)
        if result.returncode:
            raise InstallError(1, "Staged bundle validation failed: " + (result.stderr or result.stdout).strip()[:300])
    return bundle_digest(stage / "skills/pod")


def _preserve(paths: dict[str, Path], current: str | None, receipt: dict | None) -> Path | None:
    canonical = paths["canonical"]
    if not canonical.is_dir():
        return None
    known = (receipt or {}).get("target", {}).get("digest") if receipt and receipt.get("status") == "installed" else None
    if current is not None and current == known:
        return None
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = paths["data"] / "preserved" / ("skill-" + suffix)
    destination = base
    serial = 1
    while destination.exists():
        destination = Path(str(base) + f"-{serial}")
        serial += 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(canonical, destination, symlinks=True)
    return destination


def _duplicates(paths: dict[str, Path]) -> list[str]:
    canonical = paths["canonical"].resolve(strict=False)
    candidates = [paths["codex"] / "skills/pod", paths["home"] / ".codex/skills/pod",
                  paths["home"] / ".claude/skills/pod"]
    found = []
    for path in dict.fromkeys(candidates):
        if path == paths["canonical"] or path == paths["claude"] / "skills/pod":
            continue
        if (path.exists() or path.is_symlink()) and path.resolve(strict=False) != canonical:
            found.append(str(path))
    return found


def _preferences(paths: dict[str, Path], python: Path) -> tuple[str, str | None]:
    config = paths["config"] / "config.yaml"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(paths["canonical"].parent)
    program = ("import json,sys\nfrom pathlib import Path\n"
               "from pod.config import read_yaml,write_defaults\nfrom pod.errors import PodError\n"
               "p=Path(sys.argv[1]); made=False\n"
               "try:\n"
               " if not p.exists(): write_defaults(p); made=True\n"
               " read_yaml(p); print(json.dumps({'status':'created' if made else 'kept'}))\n"
               "except PodError as e: print(json.dumps({'status':'invalid','reason':str(e)}))\n")
    result = subprocess.run([str(python), "-c", program, str(config)], env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    if result.returncode:
        return "failed", "Preferences could not be created or validated"
    try:
        response = json.loads(result.stdout)
    except ValueError:
        return "failed", "Preferences validation returned no result"
    return response["status"], response.get("reason")


def _rc_block() -> str:
    return ("\n" + PATH_START + "\n"
            + 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH"; export PATH ;; esac\n'
            + PATH_END + "\n")


def _path(paths: dict[str, Path]) -> tuple[str, list[str]]:
    launcher_dir = paths["launcher"].parent
    entries = [Path(value).resolve(strict=False) for value in os.environ.get("PATH", "").split(os.pathsep) if value]
    if launcher_dir.resolve(strict=False) in entries:
        outcome = "launcher directory is already on PATH"
    else:
        outcome = "open a new shell after the PATH addition"
    notes = []
    if launcher_dir.resolve(strict=False) not in entries:
        shell = Path(os.environ.get("SHELL", "")).name
        home = paths["home"]
        if shell == "zsh":
            files = [_absolute_env("ZDOTDIR", home) / ".zshrc"]
        elif shell == "bash":
            login = next((home / name for name in (".bash_profile", ".bash_login", ".profile")
                         if (home / name).exists()), home / ".profile")
            files = [home / ".bashrc", login]
        elif shell == "sh":
            files = [home / ".profile"]
        else:
            files = []
            notes.append("Add ~/.local/bin to PATH yourself (fish: fish_add_path -U ~/.local/bin)")
        for path in dict.fromkeys(files):
            if path.is_symlink():
                notes.append(f"PATH file is a symlink and was not edited: {path}")
                continue
            if path.exists() and not path.is_file():
                notes.append(f"PATH file is not regular and was not edited: {path}")
                continue
            try:
                prior = path.read_text(encoding="utf-8") if path.exists() else ""
            except UnicodeError:
                notes.append(f"PATH file is not UTF-8 and was not edited: {path}")
                continue
            if PATH_START in prior or PATH_END in prior:
                if prior.count(PATH_START) != 1 or prior.count(PATH_END) != 1:
                    notes.append(f"PATH block is malformed; edit {path} manually")
                continue
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
            _atomic(path, (prior + _rc_block()).encode(), mode=mode)
    command = shutil.which("pod")
    if command is not None and Path(command).resolve(strict=False) != paths["launcher"].resolve(strict=False):
        notes.append(f"A different pod command shadows this launcher: {command}")
    return outcome, notes


def _print_summary(paths: dict[str, Path], prefs: tuple[str, str | None], path: str,
                   duplicates: list[str], preserved: Path | None,
                   notes: list[str] | None = None) -> None:
    if sys.stdout.isatty():
        _tty_summary(paths, prefs, path, notes or [], duplicates, preserved)
        return
    _say(f"Installed Codex skill: {paths['canonical']}")
    _say(f"Installed Claude Code skill: {paths['claude'] / 'skills/pod'}")
    _say(f"Command: {paths['launcher']} ({path})")
    _say(f"Preferences: {paths['config'] / 'config.yaml'} ({prefs[0]})")
    if prefs[1]:
        _say(f"Preferences need attention: {prefs[1]}; run pod config edit")
    if preserved:
        _say(f"Preserved changed skill at {preserved}")
    for duplicate in duplicates:
        _say(f"Duplicate skill at {duplicate}; inspect and remove it manually if unused")
    _say("In session: $pod <objective>  /  /pod <issue-url>")
    _say("Orca " + ("found (connection not checked)" if shutil.which("orca") else "not found"))
    for agent in ("codex", "claude"):
        _say(agent + (" found (sign-in not checked)" if shutil.which(agent) else " not found"))
    _say("Existing agent sessions and workers are unchanged")


def install(stage: Path) -> int:
    paths = _paths()
    source_bundle = stage / "skills/pod"
    venv_path = _venv_path(paths)
    template = _launcher_template(venv_path / "bin/python", paths["canonical"])
    _banner(_version(source_bundle))
    _preflight(stage, paths, template)
    _stage("Checking prerequisites", plain="Preflight ready")
    paths["data"].mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_file = paths["data"] / ".install.lock"
    with lock_file.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError(1, "Another Pod installer is running") from exc
        for abandoned in paths["data"].glob("venv-build-*"):
            if abandoned.is_dir() and not abandoned.is_symlink():
                shutil.rmtree(abandoned)
        receipt_file = paths["data"] / "install.json"
        receipt = read_receipt(receipt_file)
        _say("Dependencies: pinned PyYAML")
        python = _venv(paths)
        _stage("Preparing Python environment")
        _say("Bundle: validating source")
        target = {"version": _version(source_bundle), "digest": _check_bundle(python, stage)}
        canonical = paths["canonical"]
        current = optional_digest(canonical)
        if (receipt and receipt.get("status") == "installed" and receipt.get("target") == target
                and current == target["digest"] and launcher_info(paths["launcher"], template)["owned"]
                and (paths["claude"] / "skills/pod").resolve(strict=False) == canonical.resolve(strict=False)):
            _stage("Installing skills", detail="Already current", plain="Already current")
            _stage("Installing pod command", detail="already present")
            prefs = _preferences(paths, python)
            if prefs[0] == "failed":
                raise InstallError(1, (prefs[1] or "Preferences could not be prepared") + "; rerun the installer")
            _stage("Preferences", state="warn" if prefs[0] == "invalid" else "ok",
                   detail="needs attention" if prefs[0] == "invalid" else prefs[0])
            path_status = ("already on PATH" if paths["launcher"].parent in
                           map(Path, os.environ.get("PATH", "").split(os.pathsep)) else
                           "existing shell may need a PATH refresh")
            _stage("PATH", state="ok" if "already on PATH" in path_status else "warn",
                   detail=_path_label(path_status, []))
            _print_summary(paths, prefs, path_status, _duplicates(paths), None)
            return 0
        previous = ({"version": _version(canonical), "digest": current}
                    if current and (canonical / "VERSION").is_file() else
                    receipt.get("previous") if receipt and receipt["status"] == "installing" else None)
        record = {"schema": "pod-install/v1", "status": "installing", "previous": previous,
                  "target": target, "launcher": str(paths["launcher"]),
                  "launcher_digest": hashlib.sha256(template).hexdigest(),
                  "updated_at": datetime.now(timezone.utc).isoformat()}
        _write_receipt(receipt_file, record)
        _say("Receipt: installing")
        preserved = _preserve(paths, current, receipt)
        _say("Skills: placing with pinned skills CLI")
        env = os.environ.copy()
        env.pop("CODEX_HOME", None)
        env.update({"DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "CI": "1", "NO_COLOR": "1"})
        try:
            result = subprocess.run(["npx", "--yes", SKILLS_CLI, "add", str(stage), "--skill", "pod",
                                     "-a", "codex", "-a", "claude-code", "-g", "-y"],
                                    cwd=stage, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=240)
        except subprocess.TimeoutExpired as exc:
            raise InstallError(3 if optional_digest(canonical) != current else 1,
                               "Skills CLI timed out; rerun the installer") from exc
        if result.returncode:
            raise InstallError(3 if optional_digest(canonical) != current else 1,
                               "Skills CLI failed; rerun the installer")
        if optional_digest(canonical) != target["digest"]:
            raise InstallError(3, "Placed skill differs from validated source; rerun the installer")
        claude_link = paths["claude"] / "skills/pod"
        if not claude_link.is_symlink() or claude_link.resolve(strict=False) != canonical.resolve(strict=False):
            raise InstallError(3, "Claude skill link differs from canonical source; rerun the installer")
        duplicates = _duplicates(paths)
        _stage("Installing skills", plain="Skills: verified one canonical bundle")
        _atomic(paths["launcher"], template, mode=0o755)
        _stage("Installing pod command", plain="Launcher: installed")
        prefs = _preferences(paths, python)
        if prefs[0] == "failed":
            raise InstallError(3, (prefs[1] or "Preferences could not be prepared") + "; rerun the installer")
        _stage("Preferences", state="warn" if prefs[0] == "invalid" else "ok",
               detail="needs attention" if prefs[0] == "invalid" else prefs[0],
               plain="Preferences: " + prefs[0])
        path_status, notes = _path(paths)
        _stage("PATH", state="warn" if _path_label(path_status, notes) != "ready" else "ok",
               detail=_path_label(path_status, notes), plain="PATH: " + path_status)
        for note in notes:
            _say(note)
        record["status"] = "installed"
        _write_receipt(receipt_file, record)
        _say("Receipt: installed")
        for child in paths["data"].glob("venv-*"):
            if child != venv_path and child.is_dir() and (child / ".pod-venv").is_file():
                shutil.rmtree(child)
        for child in paths["data"].glob("venv-*-preserved"):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
        _print_summary(paths, prefs, path_status, duplicates, preserved, notes)
        return 0


def _extract(archive: Path, destination: Path) -> Path:
    if archive.stat().st_size > 64 * 1024 * 1024:
        raise InstallError(1, "Source archive exceeds its size limit")
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if len(members) > 1024 or sum(member.size for member in members) > 64 * 1024 * 1024:
            raise InstallError(1, "Source archive exceeds its size limit")
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.islnk() or member.isdev():
                raise InstallError(1, "Source archive contains an unsafe path")
        source.extractall(destination, filter="data")
    roots = [path for path in destination.iterdir() if path.is_dir() and (path / "skills/pod/installer.py").is_file()]
    if len(roots) != 1:
        raise InstallError(1, "Source archive has no single Pod bundle")
    return roots[0]


def update() -> int:
    source = os.environ.get("POD_INSTALL_SOURCE", SOURCE)
    if not source.startswith(("https://", "file://", "http://127.0.0.1:", "http://localhost:")):
        raise InstallError(2, "POD_INSTALL_SOURCE must be HTTPS, file, or local loopback HTTP")
    with tempfile.TemporaryDirectory(prefix="pod-update-") as temporary:
        archive = Path(temporary) / "source.tar.gz"
        try:
            with urllib.request.urlopen(source, timeout=120) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
        except (OSError, ValueError) as exc:
            raise InstallError(1, "Source download failed; previous installation remains") from exc
        stage = _extract(archive, Path(temporary))
        staged = stage / "skills/pod/scripts/pod.py"
        result = subprocess.run([str(_base_python()), str(staged), "__install", str(stage)],
                                stdin=subprocess.DEVNULL, timeout=420)
        if result.returncode == 0:
            _stage("Update", detail="Reload active coordinators; running workers unchanged",
                   plain="Reload active coordinators before new starts; running workers are unchanged")
        return result.returncode


def _failure_code(default: int) -> int:
    """Report post-copy failures as recoverable incomplete installations."""
    if default in (2, 3, 130):
        return default
    try:
        paths = _paths()
        receipt = read_receipt(paths["data"] / "install.json")
        if receipt and receipt["status"] == "installing":
            previous = receipt["previous"]["digest"] if receipt["previous"] else None
            if optional_digest(paths["canonical"]) != previous:
                return 3
    except (InstallError, OSError, ValueError):
        pass
    return default


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) == 1 and args[0] == "--update":
            return update()
        if len(args) == 1 and Path(args[0]).is_dir():
            return install(Path(args[0]))
        raise InstallError(2, "Expected one extracted source directory")
    except KeyboardInterrupt:
        if sys.stdout.isatty():
            _failure("Interrupted before completion", 130)
        else:
            _say("Interrupted; rerun the installer to verify or recover the previous bundle")
        return 130
    except InstallError as exc:
        code = _failure_code(exc.code)
        _failure(str(exc), code)
        return code
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        code = _failure_code(1)
        _failure("installation failed; rerun the installer (" + type(exc).__name__ + ")", code)
        return code


if __name__ == "__main__":
    raise SystemExit(main())
