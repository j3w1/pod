#!/usr/bin/env python3
"""Pod launcher: runs the package bundled beside this script. It installs nothing.

Deliberately importable by any Python 3, so an old or prerelease interpreter reports a clear
prerequisite instead of a syntax error. Nothing from the Pod package is imported until the
prerequisites hold.
"""

import importlib.util
import json
import os
import sys

REINSTALL = "curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh | sh"
# What must exist before the package can even report its own problems.
ENTRY_FILES = ("__init__.py", "cli.py", "errors.py", "VERSION")


def bundle_dir(script=None):
    """The skill/package directory that contains this script's parent."""
    target = __file__ if script is None else script
    return os.path.dirname(os.path.dirname(os.path.realpath(target)))


def preflight(version_info=None, bundle=None):
    """Return None when the package can run, else one actionable failure record."""
    version = sys.version_info if version_info is None else version_info
    if len(version) > 3 and version[3] != "final":
        running = ".".join(str(part) for part in version[:3]) + "-" + str(version[3])
        return {"code": "python_not_stable",
                "message": ("A stable CPython is required; this is the prerelease %s at %s. Re-run with the "
                            "latest stable interpreter: python3 %s"
                            % (running, sys.executable, os.path.realpath(__file__)))}
    root = bundle_dir() if bundle is None else bundle
    missing = [name for name in ENTRY_FILES if not os.path.isfile(os.path.join(root, name))]
    if missing:
        return {"code": "bundle_incomplete",
                "message": ("Pod bundle at %s is incomplete (missing %s). Reinstall it: %s"
                            % (root, ", ".join(missing), REINSTALL))}
    return None


def load_pod(bundle):
    """Import the bundled package as `pod`, ahead of any other copy on the path."""
    if sys.modules.get("pod") is not None:
        return sys.modules["pod"]
    spec = importlib.util.spec_from_file_location(
        "pod", os.path.join(bundle, "__init__.py"), submodule_search_locations=[bundle])
    if spec is None or spec.loader is None:
        raise ImportError("bundle at %s cannot be loaded" % bundle)
    module = importlib.util.module_from_spec(spec)
    sys.modules["pod"] = module
    spec.loader.exec_module(module)
    return module


def _installer(bundle):
    """Load the standard-library installer on its own, without the package."""
    spec = importlib.util.spec_from_file_location("pod_installer", os.path.join(bundle, "installer.py"))
    if spec is None or spec.loader is None:
        raise OSError("installer is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _integrity_failure(bundle):
    """Block an incomplete bundle and an installation that stopped mid-copy, and nothing else.

    A copy in progress is neither its previous bundle nor its target. A place the installer
    would refuse to write to, or a receipt it cannot read, is for `pod doctor` to report: it
    does not say a copy is in progress, so it never stops a command.
    """
    try:
        installer = _installer(bundle)
    except Exception as exc:
        reason = "missing installer.py" if isinstance(exc, FileNotFoundError) else "installer.py cannot be loaded"
        return {"code": "bundle_incomplete",
                "message": "Pod bundle at %s is incomplete (%s). Reinstall it: %s" % (bundle, reason, REINSTALL)}
    try:
        receipt = installer.read_receipt(installer.receipt_path())
    except Exception:
        return None
    if receipt is None or receipt.get("status") != "installing":
        return None
    try:
        actual = installer.optional_digest(__import__("pathlib").Path(bundle))
    except Exception:
        actual = None
    allowed = {item.get("digest") for item in (receipt.get("previous"), receipt.get("target"))
               if isinstance(item, dict)}
    if actual is not None and actual in allowed:
        return None
    return {"code": "install_incomplete", "message": "Installation stopped during skill copy; rerun: " + REINSTALL}


def _blocked(arguments, failure):
    if "--json" in arguments:
        print(json.dumps({"status": "blocked", "error": failure}, indent=2, sort_keys=True))
    else:
        print("pod: " + failure["message"], file=sys.stderr)
    return 2


def main(argv=None):
    sys.dont_write_bytecode = True
    arguments = list(sys.argv[1:] if argv is None else argv)
    root = bundle_dir()
    # Installing and updating need only the installer, so they can repair a damaged bundle.
    if arguments[:1] == ["__install"] or arguments == ["update"]:
        try:
            return _installer(root).main(arguments[1:] if arguments[0] == "__install" else ["--update"])
        except (OSError, ValueError, SyntaxError):
            print("pod-install: installer bundle is incomplete; rerun: " + REINSTALL, file=sys.stderr)
            return 2
    integrity = _integrity_failure(root)
    if integrity is not None:
        return _blocked(arguments, integrity)
    if arguments == ["--version"]:
        try:
            with open(os.path.join(root, "VERSION"), encoding="ascii") as stream:
                print(stream.read().strip())
        except (OSError, UnicodeError):
            print("pod: bundle VERSION is unavailable", file=sys.stderr)
            return 2
        return 0
    failure = preflight(bundle=root)
    if failure is not None:
        return _blocked(arguments, failure)
    try:
        load_pod(root)
        from pod.cli import main as entry
    except ImportError as exc:
        # The entry-file check covers what is needed to start. A module missing deeper in the
        # package would otherwise surface as a traceback, which a prerequisite failure must never be.
        return _blocked(arguments, {"code": "bundle_incomplete",
                                    "message": ("Pod bundle at %s is incomplete (%s). Reinstall it: %s"
                                                % (root, exc, REINSTALL))})
    return entry(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
