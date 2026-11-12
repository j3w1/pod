"""`pod doctor` and `pod update`: what is installed, and how to replace it.

Doctor only reads. It makes no network call, starts nothing, and reports what it could not
observe as unknown instead of guessing.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

from . import __version__, bundle, installer, paths, routefile
from .errors import PodError

REPAIR = "rerun the one-shot installer: " + installer.REINSTALL


def _tool(name: str) -> dict:
    """Where a command resolves and the first line it prints for --version; unknown stays None."""
    found = shutil.which(name)
    version = None
    if found:
        try:
            result = subprocess.run([found, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, timeout=5)
            lines = result.stdout.strip().splitlines()
            version = lines[0][:120] if result.returncode == 0 and lines else None
        except (OSError, subprocess.SubprocessError):
            version = None
    return {"path": found, "version": version}


def _installation(root: Path) -> dict:
    """Receipt, placed copy, launcher and links, as the installer left them."""
    try:
        places = installer._paths()
        receipt = installer.read_receipt(places["data"] / "install.json")
    except (installer.InstallError, OSError) as exc:
        return {"status": "needs attention", "healthy": False, "problem": str(exc), "next": REPAIR}
    launcher, canonical = places["launcher"], places["canonical"]
    found = shutil.which("pod")
    report = {"receipt": str(places["data"] / "install.json"), "canonical": str(canonical),
              "launcher": {"path": str(launcher), "resolves_to": found,
                           "shadowed": bool(found and Path(found).resolve() != launcher.resolve())},
              "duplicates": installer.duplicates(places)}
    if receipt is None:
        return {**report, "status": "not installed by the one-shot installer", "healthy": None}
    target = receipt["target"]
    placed = installer.optional_digest(canonical)
    claude = places["claude"] / "skills/pod"
    checks = {"receipt_status": receipt["status"], "receipt_version": target["version"],
              "digest_matches": placed == target["digest"],
              "running_installed_copy": root.resolve() == canonical.resolve(),
              "launcher_owned": installer.launcher_owned(launcher, {receipt.get("launcher_digest")})["owned"],
              "claude_link": claude.is_symlink() and claude.resolve() == canonical.resolve()}
    healthy = (checks["receipt_status"] == "installed" and checks["digest_matches"] and checks["launcher_owned"]
               and checks["claude_link"])
    report.update(checks, status="installed" if healthy else "needs attention", healthy=healthy)
    if not healthy:
        report["next"] = REPAIR
    return report


def _locations() -> dict:
    try:
        return {"config": str(paths.config_home()), "state": str(paths.state_home()), "cache": str(paths.cache_home())}
    except PodError as exc:
        return {"problem": str(exc), "fix": exc.fix}


def doctor() -> dict:
    root = bundle.bundle_root()
    try:
        preferences = routefile.load()
        routes = {"path": str(preferences.path), "status": preferences.status, "problem": preferences.problem,
                  "enabled": len(preferences.enabled()), "preferred": preferences.preferred, "pinned": preferences.pinned,
                  "merge_authorization": preferences.merge_authorization,
                  "authorization_verifier": bool(preferences.authorization_verifier)}
    except PodError as exc:
        routes = {"status": "invalid", "problem": str(exc), "fix": exc.fix}
    return {"version": __version__,
            "python": {"executable": sys.executable, "version": sys.version.split()[0],
                       "stable": sys.version_info.releaselevel == "final"},
            "bundle": {"path": str(root), "digest": bundle.digest(root)},
            "installation": _installation(root),
            "tools": {name: _tool(name) for name in ("orca", "git", "gh")},
            "preferences": routes,
            "locations": _locations()}


def update(argv: list[str] | None = None) -> int:
    """Download the current source and run its installer. Running workers are not touched."""
    if argv:
        print("pod update takes no arguments", file=sys.stderr)
        return 2
    return installer.main(["--update"])
