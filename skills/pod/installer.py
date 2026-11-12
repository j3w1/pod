"""One-shot Pod installer and explicit updater. Standard library only.

It never imports the Pod package, so it runs from a staged source tree before anything is
placed, and it never reads or writes the state directory.
"""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unicodedata
import urllib.request

SKILLS_CLI = "skills@1.7.0"
SOURCE = "https://codeload.github.com/j3w1/pod/tar.gz/refs/heads/main"
REINSTALL = "curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh | sh"
LAUNCHER_MARKER = "# pod-launcher/v1"
PATH_START = "# >>> pod path >>>"
PATH_END = "# <<< pod path <<<"
# The bundle is derived from the skill directory: these kinds of file, plus VERSION.
BUNDLE_SUFFIXES = (".py", ".md", ".json", ".yaml")
REGISTRY = "models/registry.json"
NEVER_DEFAULT = ("ultra",)
ROUTE = re.compile(r"[a-z][a-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9._-]*/[a-z][a-z0-9_-]*")
BANNER = ("         /\\                       /\\           /\\",
          "  ______/  \\___          ______/  \\_      __/ \\_",
          "_/#### (o)    \\__      _/### (o)    \\_    /# (o) \\",
          "\\_  ___  __/\\__  /      \\_  __/\\__  _/    \\_ __/_/",
          "  \\/   \\/      \\/          \\/     \\/         \\/",
          "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")


class InstallError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# Output: plain lines in pipes and logs, a small staged layout on a terminal.

def _clean(value: object) -> str:
    """Strip control, format and surrogate characters before anything reaches a terminal."""
    text = str(value)
    if text.isascii() and text.isprintable():
        return text
    return "".join(char for char in text if unicodedata.category(char) not in ("Cc", "Cf", "Cs"))


def _say(message: str) -> None:
    if not sys.stdout.isatty():
        print("pod-install: " + _clean(message), flush=True)


def _utf8() -> bool:
    setting = next((os.environ[key] for key in ("LC_ALL", "LC_CTYPE", "LANG") if os.environ.get(key)), "C")
    return os.environ.get("TERM") != "dumb" and ("UTF-8" in setting.upper() or "UTF8" in setting.upper())


def _color() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def _paint(value: str, code: str) -> str:
    return f"\033[{code}m{value}\033[0m" if _color() else value


def _marker(state: str) -> str:
    return ({"ok": "✓", "warn": "!", "fail": "✗"} if _utf8() else {"ok": "[ok]", "warn": "[!]", "fail": "[x]"})[state]


def _columns() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def _stage(label: str, *, state: str = "ok", detail: str | None = None, plain: str | None = None) -> None:
    if not sys.stdout.isatty():
        if plain is not None:
            _say(plain)
        return
    marker = _paint(_marker(state), {"ok": "38;5;37", "warn": "38;5;214", "fail": "38;5;160"}[state])
    print(f"  {marker}  {_clean(label)}" + (f"  {_clean(detail)}" if detail else ""), flush=True)


def _banner(version: str) -> None:
    if not sys.stdout.isatty():
        return
    for index, line in enumerate(BANNER):
        if not _color():
            print(line, flush=True)
        elif index == len(BANNER) - 1:
            print(_paint(line, "38;5;30"), flush=True)
        else:
            print("".join(_paint(part, "38;5;255" if part == "(o)" else "38;5;23" if part.startswith("#") else "38;5;37")
                          for part in re.split(r"(\(o\)|#+)", line) if part), flush=True)
    print(_paint(f"             Pod {version} / one-shot install", "1;38;5;153"), flush=True)


def _section(title: str, *, stream=None) -> None:
    heading = "  " + title + " "
    rule = ("─" if _utf8() else "-") * max(0, min(60, max(24, _columns() - 2)) - len(heading))
    print(_paint(heading + rule, "38;5;30"), file=sys.stdout if stream is None else stream, flush=True)


def _display_path(path: Path, home: Path) -> str:
    try:
        relative = path.relative_to(home)
        value = "~" if not relative.parts else "~/" + relative.as_posix()
    except ValueError:
        value = str(path)
    value, width = _clean(value), min(52, max(14, _columns() - 24))
    if len(value) <= width:
        return value
    mark = "…" if _utf8() else "..."
    room = width - len(mark)
    return value[:(room + 1) // 2] + mark + value[len(value) - room // 2:]


def _field(label: str, value: str) -> None:
    print(f"  {_paint(label.ljust(18), '38;5;153')}{_clean(value)}", flush=True)


def _path_label(path_status: str, notes: list[str]) -> str:
    if "manual PATH step" in path_status:
        return "manual step"
    return "ready" if "already on PATH" in path_status else "open a new shell"


def _attention(prefs: tuple[str, str | None]) -> str | None:
    if prefs[0] == "invalid":
        return f"{prefs[1]}; run pod config edit"
    return prefs[1]


def _summary(paths: dict[str, Path], prefs: tuple[str, str | None], path_status: str, notes: list[str],
             duplicates: list[str], preserved: Path | None) -> None:
    home, config = paths["home"], paths["config"] / "config.yaml"
    agents = [name + (" found (sign-in not checked)" if shutil.which(name) else " not found") for name in ("codex", "claude")]
    orca = "Orca found (connection not checked)" if shutil.which("orca") else "Orca not found"
    if not sys.stdout.isatty():
        _say(f"Installed Codex skill: {paths['canonical']}")
        _say(f"Installed Claude Code skill: {paths['claude'] / 'skills/pod'}")
        _say(f"Command: {paths['launcher']} ({path_status})")
        _say(f"Preferences: {config} ({prefs[0]})")
        if _attention(prefs):
            _say(f"Preferences need attention: {_attention(prefs)}")
        if preserved:
            _say(f"Preserved changed skill at {preserved}")
        for duplicate in duplicates:
            _say(f"Duplicate skill at {duplicate}; inspect and remove it manually if unused")
        _say("In session: $pod <objective>  /  /pod <issue-url>")
        for line in (orca, *agents, "Existing agent sessions and workers are unchanged"):
            _say(line)
        return
    label = _path_label(path_status, notes)
    print()
    _section("Installed")
    _field("Codex skill", _display_path(paths["canonical"], home))
    _field("Claude Code skill", _display_path(paths["claude"] / "skills/pod", home))
    _field("pod command", _display_path(paths["launcher"], home))
    _field("Preferences", f"{'needs attention' if prefs[0] == 'invalid' else prefs[0]}  {_display_path(config, home)}")
    _field("PATH", label)
    if _attention(prefs):
        _field("Attention", _attention(prefs))
    if preserved:
        _field("Preserved", _display_path(preserved, home))
    for duplicate in duplicates:
        _field("Duplicate", _display_path(Path(duplicate), home) + " (inspect before removal)")
    for note in notes:
        _field("Note", note.replace(str(home), "~"))
    _section("Environment")
    for line in (orca, *agents, "Existing agent sessions and workers unchanged"):
        print("  " + line, flush=True)
    _section("Next")
    if label == "open a new shell":
        print("  Open a new shell for the pod command.", flush=True)
    elif label == "manual step":
        print("  Resolve the PATH note above before using the pod command.", flush=True)
    if prefs[0] == "invalid":
        print("  Correct preferences with pod config edit before delegation.", flush=True)
    print("  Codex       $pod <objective>", flush=True)
    print("  Claude Code /pod <issue-url>", flush=True)
    print("  Optional    pod models  (review routes)", flush=True)


def _failure(message: str, code: int) -> None:
    if not sys.stdout.isatty():
        print("pod-install: " + _clean(message), file=sys.stderr, flush=True)
        return
    print(file=sys.stderr)
    _section("Install stopped", stream=sys.stderr)
    mark = _paint(_marker("fail"), "38;5;160")
    display = _clean(message).replace(str(Path.home()), "~")
    for index, row in enumerate(textwrap.wrap(display, max(20, _columns() - 7)) or [""]):
        print(f"  {mark if index == 0 else ' '}  {row}", file=sys.stderr, flush=True)
    state = {3: "Skill copy may be incomplete; the receipt remains installing.",
             130: "Interrupted; no completed installation is claimed.",
             2: "No installation action was accepted."}.get(code, "The previous skill and launcher were not promoted.")
    print("  State  " + state, file=sys.stderr, flush=True)
    print("  Existing agent sessions and workers unchanged.", file=sys.stderr, flush=True)
    _section("Next", stream=sys.stderr)
    cause = ("Inspect the unrelated pod command" if "unrelated pod command" in message else
             "Inspect the reported path" if "foreign" in message or "redirected" in message else
             "Resolve the reported cause")
    print(f"  {cause}, then rerun:", file=sys.stderr, flush=True)
    for row in textwrap.wrap(REINSTALL, max(20, _columns() - 4), break_long_words=False):
        print("  " + row, file=sys.stderr, flush=True)
    print(f"  Exit code {code}", file=sys.stderr, flush=True)


# Locations, the derived bundle and the receipt.

def _absolute_env(name: str, fallback: Path) -> Path:
    raw = os.environ.get(name, str(fallback))
    if not raw or len(raw) > 4096 or "\x00" in raw:
        raise InstallError(1, f"{name} must be an absolute directory path")
    path = Path(raw)
    if not path.is_absolute() or (path.exists() and not path.is_dir()):
        raise InstallError(1, f"{name} must be an absolute directory path")
    return path


def _data_home(home: Path) -> Path:
    return _absolute_env("XDG_DATA_HOME", home / ".local/share") / "pod"


def _paths() -> dict[str, Path]:
    """Where everything lives. Reading these places refuses nothing; `_refuse_redirected` guards writing."""
    home = Path.home()
    if not home.is_absolute():
        raise InstallError(1, "HOME must be absolute")
    config = _absolute_env("POD_CONFIG_HOME", _absolute_env("XDG_CONFIG_HOME", home / ".config") / "pod")
    claude = _absolute_env("CLAUDE_CONFIG_DIR", home / ".claude")
    codex = _absolute_env("CODEX_HOME", home / ".agents")
    return {"home": home, "data": _data_home(home), "config": config, "claude": claude, "codex": codex,
            "canonical": home / ".agents/skills/pod", "launcher": home / ".local/bin/pod"}


def _refuse_redirected(paths: dict[str, Path]) -> None:
    """Every directory this installer writes into, checked before anything is changed."""
    places = [(paths["home"], "HOME"), (paths["data"], None), (paths["data"] / "preserved", None),
              (paths["config"], "POD_CONFIG_HOME" if "POD_CONFIG_HOME" in os.environ else None),
              (paths["claude"], "CLAUDE_CONFIG_DIR"),
              (paths["codex"], "CODEX_HOME" if "CODEX_HOME" in os.environ else None),
              (paths["canonical"].parent.parent, None), (paths["canonical"].parent, None),   # ~/.agents, whatever CODEX_HOME is
              (paths["launcher"].parent, None)]
    if "ZDOTDIR" in os.environ:
        places.append((_absolute_env("ZDOTDIR", paths["home"]), "ZDOTDIR"))
    for path, variable in places:
        if path.is_symlink():
            target = os.path.realpath(path)
            remedy = f"Set {variable}={target}" if variable else "Replace the link with a real directory"
            raise InstallError(1, f"Refuse redirected directory: {path} is a symbolic link to {target}. "
                                  f"{remedy}, then rerun the installer")


def bundle_files(root: Path) -> list[str]:
    """Relative names of the bundle's files, read from the directory itself."""
    names = []
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = [name for name in subdirs if name != "__pycache__"]
        names += [(Path(directory) / name).relative_to(root).as_posix() for name in files
                  if name == "VERSION" or name.endswith(BUNDLE_SUFFIXES)]
    return sorted(names)


def bundle_digest(root: Path) -> str:
    """One digest over every bundle file's name and bytes, the same for a linked or copied VERSION."""
    names = bundle_files(root)
    if "SKILL.md" not in names or "VERSION" not in names:
        raise InstallError(1, "Not a Pod bundle")
    digest = hashlib.sha256()
    for name in names:
        data = (root / name).read_bytes()
        digest.update(name.encode() + b"\0" + len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def optional_digest(root: Path) -> str | None:
    try:
        return bundle_digest(root) if root.is_dir() else None
    except (OSError, InstallError):
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
    """The receipt's own location. No other place can keep it from being read."""
    return _data_home(Path.home()) / "install.json"


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
            or value.get("status") not in ("installing", "installed") or not isinstance(target, dict)
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


# The launcher runs the placed bundle with the interpreter this installer verified.

def _python() -> Path:
    return Path(getattr(sys, "_base_executable", None) or sys.executable)


def _launcher_template(python: Path, canonical: Path) -> bytes:
    script = shlex.quote(str(canonical / "scripts/pod.py"))
    return ("#!/bin/sh\n" + LAUNCHER_MARKER + "\n"
            + f"if [ ! -x {shlex.quote(str(python))} ] || [ ! -f {script} ]; then\n"
            + "  echo 'pod: installation incomplete; rerun " + REINSTALL + "' >&2\n  exit 2\nfi\n"
            + f"exec {shlex.quote(str(python))} -I {script} \"$@\"\n").encode()


def launcher_owned(path: Path, digests: set) -> dict:
    """Whether the launcher is the file Pod wrote: its bytes match a digest Pod recorded or would write."""
    if path.is_symlink():
        return {"owned": False, "reason": "symlink"}
    if not path.exists():
        return {"owned": False, "reason": "missing"}
    if not path.is_file() or path.stat().st_size > 16 * 1024:
        return {"owned": False, "reason": "foreign"}
    owned = hashlib.sha256(path.read_bytes()).hexdigest() in digests
    return {"owned": owned, "reason": "owned" if owned else "foreign"}


def _reconcilable_copy(canonical: Path, receipt: dict | None, source_bundle: Path) -> bool:
    """An interrupted skills-CLI copy can be replaced only for its exact target."""
    if (not receipt or receipt.get("status") != "installing" or not canonical.is_dir() or canonical.is_symlink()
            or receipt["target"]["version"] != _version(source_bundle)
            or receipt["target"]["digest"] != bundle_digest(source_bundle)):
        return False
    names = set(bundle_files(source_bundle))
    directories = {str(parent) for name in names for parent in Path(name).parents if str(parent) != "."}
    for child in canonical.rglob("*"):
        relative = child.relative_to(canonical).as_posix()
        if child.is_symlink() or not (child.is_dir() and relative in directories
                                      or child.is_file() and relative in names):
            return False
    return True


def _preflight(stage: Path, paths: dict[str, Path], template: bytes) -> None:
    if platform.system() != "Linux" or os.geteuid() == 0:
        raise InstallError(1, "Run as a non-root user on Linux")
    if sys.version_info.releaselevel != "final":
        raise InstallError(1, "A stable CPython (not a prerelease) is required; install the latest stable one, "
                              "then rerun the installer")
    try:
        import curses  # noqa: F401
    except ImportError as exc:
        raise InstallError(1, "Python curses is required for pod models") from exc
    if shutil.which("npx") is None or shutil.which("node") is None:
        raise InstallError(1, "Node and npx are required")
    _refuse_redirected(paths)
    source_bundle = stage / "skills/pod"
    if not source_bundle.is_dir() or not (source_bundle / "SKILL.md").is_file():
        raise InstallError(1, "Source has no Pod skill")
    _version(source_bundle)
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
    known = {hashlib.sha256(template).hexdigest(), (receipt or {}).get("launcher_digest")}
    if launcher_owned(paths["launcher"], known)["reason"] not in ("missing", "owned"):
        raise InstallError(1, f"Refuse unrelated pod command: {paths['launcher']}")
    if (paths["data"] / ".install.lock").is_symlink():
        raise InstallError(1, "Installation lock is redirected")


def _run_bundle(root: Path, program: str, *arguments: str) -> subprocess.CompletedProcess:
    """Run a few lines against the package under `root` in a separate, isolated interpreter."""
    return subprocess.run([str(_python()), "-I", "-B", "-c", "import sys\nsys.path.insert(0, sys.argv[1])\n" + program,
                           str(root), *arguments], cwd="/", stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=40)


def _check_bundle(stage: Path) -> str:
    result = _run_bundle(stage / "skills", "from pod.skill_validation import main\nraise SystemExit(main([sys.argv[2]]))\n",
                         str(stage / "skills/pod"))
    if result.returncode:
        raise InstallError(1, "Staged bundle validation failed: " + (result.stdout or result.stderr).strip()[:300])
    return bundle_digest(stage / "skills/pod")


def _preserve(paths: dict[str, Path], current: str | None, receipt: dict | None) -> Path | None:
    """Save a canonical copy that differs from what this installer last placed, before it is replaced."""
    canonical = paths["canonical"]
    known = receipt["target"]["digest"] if receipt and receipt.get("status") == "installed" else None
    if not canonical.is_dir() or (current is not None and current == known):
        return None
    base = paths["data"] / "preserved" / ("skill-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    destination, serial = base, 1
    while destination.exists():
        destination, serial = Path(f"{base}-{serial}"), serial + 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(canonical, destination, symlinks=True)
    return destination


def duplicates(paths: dict[str, Path]) -> list[str]:
    canonical = paths["canonical"].resolve(strict=False)
    found = []
    for path in dict.fromkeys((paths["codex"] / "skills/pod", paths["home"] / ".codex/skills/pod",
                               paths["home"] / ".claude/skills/pod")):
        if path in (paths["canonical"], paths["claude"] / "skills/pod"):
            continue
        if (path.exists() or path.is_symlink()) and path.resolve(strict=False) != canonical:
            found.append(str(path))
    return found


# Preferences: written once when absent, then only ever read.

def default_preferences(source_bundle: Path) -> bytes:
    """A first preference file: each registry route enabled, except efforts that are never a default."""
    routes = []
    try:
        registry = json.loads((source_bundle / REGISTRY).read_text(encoding="utf-8"))
        for model in registry["models"]:
            routes += [f"{model['agent']}/{model['id']}/{effort}" for effort in model["efforts"]
                       if effort not in NEVER_DEFAULT]
        if any(not ROUTE.fullmatch(route) for route in routes) or len(set(routes)) != len(routes):
            routes = []
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        routes = []
    lines = ["schema: pod/v2", "routes:" if routes else "routes: {}", *(f"  {route}: enabled" for route in routes),
             "preferred: null", "pinned: null", "merge_authorization: person"]
    return ("\n".join(lines) + "\n").encode()


def _first_install(receipt: dict | None) -> bool:
    """No earlier installation finished here: no receipt, or one from a first attempt that stopped part way."""
    return receipt is None or (receipt.get("status") == "installing" and receipt.get("previous") is None)


def _preferences(paths: dict[str, Path], source_bundle: Path, *, first: bool) -> tuple[str, str | None]:
    """Report the preference file. Only a first install may create it; an update never creates or changes it."""
    config = paths["config"] / "config.yaml"
    absent = not config.exists() and not config.is_symlink()
    created = absent and first
    if absent and not first:
        return "absent", "no preference file, so no route is enabled; choose routes with pod models"
    if created:
        _atomic(config, default_preferences(source_bundle))
    result = _run_bundle(paths["canonical"].parent,
                         "import json\nfrom pathlib import Path\nfrom pod import routefile\n"
                         "p = routefile.load(Path(sys.argv[2]))\n"
                         "print(json.dumps({'status': p.status, 'problem': p.problem, 'enabled': len(p.enabled())}))\n",
                         str(config))
    try:
        report = json.loads(result.stdout) if not result.returncode else None
    except ValueError:
        report = None
    if not isinstance(report, dict):
        return "failed", "Preferences could not be read by the placed bundle"
    if report["status"] != "ok":
        return "invalid", report["problem"] or "the preference file is not readable"
    return ("created" if created else "kept",
            None if report["enabled"] else "no route is enabled; choose routes with pod models")


def _rc_block() -> str:
    return ("\n" + PATH_START + "\n"
            + 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH"; export PATH ;; esac\n'
            + PATH_END + "\n")


def _path(paths: dict[str, Path]) -> tuple[str, list[str]]:
    launcher_dir = paths["launcher"].parent.resolve(strict=False)
    entries = [Path(value).resolve(strict=False) for value in os.environ.get("PATH", "").split(os.pathsep) if value]
    outcome = "launcher directory is already on PATH" if launcher_dir in entries else "open a new shell after the PATH addition"
    notes = []
    if launcher_dir not in entries:
        shell, home = Path(os.environ.get("SHELL", "")).name, paths["home"]
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
            _atomic(path, (prior + _rc_block()).encode(), mode=stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)
    command = shutil.which("pod")
    if command is not None and Path(command).resolve(strict=False) != paths["launcher"].resolve(strict=False):
        notes.append(f"A different pod command shadows this launcher: {command}")
    if notes:
        outcome = "manual PATH step required; inspect the reported path or command"
    return outcome, notes


def _finish(paths: dict[str, Path], source_bundle: Path, *, fail: int, first: bool) -> tuple:
    """Preferences and PATH: the same closing steps for a new copy and a current one."""
    prefs = _preferences(paths, source_bundle, first=first)
    if prefs[0] == "failed":
        raise InstallError(fail, prefs[1] + "; rerun the installer")
    _stage("Preferences", state="ok" if prefs[0] != "invalid" and not prefs[1] else "warn",
           detail="needs attention" if prefs[0] == "invalid" else prefs[0], plain="Preferences: " + prefs[0])
    path_status, notes = _path(paths)
    label = _path_label(path_status, notes)
    _stage("PATH", state="ok" if label == "ready" else "warn", detail=label, plain="PATH: " + path_status)
    for note in notes:
        _say(note)
    return prefs, path_status, notes


def _remove_earlier_environments(data: Path) -> None:
    """Earlier installers kept a Python environment here, each with a Pod marker. Nothing uses one now."""
    for child in data.glob("venv-*"):
        if child.is_dir() and not child.is_symlink() and (child.name.startswith("venv-build-")
                                                          or (child / ".pod-venv").is_file()):
            shutil.rmtree(child, ignore_errors=True)


def install(stage: Path) -> int:
    paths = _paths()
    source_bundle = stage / "skills/pod"
    template = _launcher_template(_python(), paths["canonical"])
    _banner(_version(source_bundle))
    _preflight(stage, paths, template)
    _stage("Checking prerequisites", plain="Preflight ready")
    paths["data"].mkdir(parents=True, exist_ok=True, mode=0o700)
    with (paths["data"] / ".install.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError(1, "Another Pod installer is running") from exc
        receipt_file = paths["data"] / "install.json"
        receipt = read_receipt(receipt_file)
        _say("Bundle: validating source")
        target = {"version": _version(source_bundle), "digest": _check_bundle(stage)}
        canonical, claude_link = paths["canonical"], paths["claude"] / "skills/pod"
        current = optional_digest(canonical)
        launcher_digest = hashlib.sha256(template).hexdigest()
        if (receipt and receipt.get("status") == "installed" and receipt.get("target") == target
                and current == target["digest"] and launcher_owned(paths["launcher"], {launcher_digest})["owned"]
                and claude_link.resolve(strict=False) == canonical.resolve(strict=False)):
            _stage("Installing skills", detail="Already current", plain="Already current")
            _stage("Installing pod command", detail="already present")
            prefs, path_status, notes = _finish(paths, source_bundle, fail=1, first=False)
            _summary(paths, prefs, path_status, notes, duplicates(paths), None)
            return 0
        previous = ({"version": _version(canonical), "digest": current} if current else
                    receipt.get("previous") if receipt and receipt["status"] == "installing" else None)
        record = {"schema": "pod-install/v1", "status": "installing", "previous": previous, "target": target,
                  "launcher": str(paths["launcher"]), "launcher_digest": launcher_digest,
                  "updated_at": datetime.now(timezone.utc).isoformat()}
        _write_receipt(receipt_file, record)
        _say("Receipt: installing")
        preserved = _preserve(paths, current, receipt)
        _say("Skills: placing with pinned skills CLI")
        env = os.environ.copy()
        env.pop("CODEX_HOME", None)
        env.update({"DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "CI": "1", "NO_COLOR": "1"})
        try:
            failed = subprocess.run(["npx", "--yes", SKILLS_CLI, "add", str(stage), "--skill", "pod",
                                     "-a", "codex", "-a", "claude-code", "-g", "-y"], cwd=stage, env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=240).returncode
        except subprocess.TimeoutExpired:
            failed = "timed out"
        if failed:
            raise InstallError(3 if optional_digest(canonical) != current else 1,
                               f"Skills CLI {'timed out' if failed == 'timed out' else 'failed'}; rerun the installer")
        if optional_digest(canonical) != target["digest"]:
            raise InstallError(3, "Placed skill differs from validated source; rerun the installer")
        if not claude_link.is_symlink() or claude_link.resolve(strict=False) != canonical.resolve(strict=False):
            raise InstallError(3, "Claude skill link differs from canonical source; rerun the installer")
        _stage("Installing skills", plain="Skills: verified one canonical bundle")
        _atomic(paths["launcher"], template, mode=0o755)
        _stage("Installing pod command", plain="Launcher: installed")
        prefs, path_status, notes = _finish(paths, source_bundle, fail=3, first=_first_install(receipt))
        record["status"] = "installed"
        _write_receipt(receipt_file, record)
        _say("Receipt: installed")
        _remove_earlier_environments(paths["data"])
        _summary(paths, prefs, path_status, notes, duplicates(paths), preserved)
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
    """Download the current source and run its own installer, exactly as the one-shot script does."""
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
        result = subprocess.run([str(_python()), str(stage / "skills/pod/scripts/pod.py"), "__install", str(stage)],
                                stdin=subprocess.DEVNULL, timeout=420)
        if result.returncode == 0:
            _stage("Update", detail="Reload active coordinators; running workers unchanged",
                   plain="Reload active coordinators before new starts; running workers are unchanged")
        return result.returncode


def _failure_code(default: int) -> int:
    """Report a failure after the copy began as a recoverable incomplete installation."""
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
        if args == ["--update"]:
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
