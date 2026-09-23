#!/usr/bin/env python3
"""Pod helper launcher: runs the package bundled beside this script. It installs nothing.

Deliberately importable by any Python 3, so an old interpreter reports a clear
prerequisite instead of a syntax error. Nothing from the Pod package or PyYAML is
imported until the prerequisites hold.
"""

import importlib.util
import json
import os
import sys

MIN_PYTHON = (3, 13)
REQUIRED = ("__init__.py", "cli.py", "internal.py", "SKILL.md")
PYYAML_STEP = "python3 -m pip install --user 'PyYAML>=6.0.2,<7'"
REINSTALL = "npx skills add j3w1/pod --skill pod   (or re-run: pod setup)"


def bundle_dir(script=None):
    """The skill/package directory that contains this script's parent."""
    target = __file__ if script is None else script
    return os.path.dirname(os.path.dirname(os.path.realpath(target)))


def _yaml_importable():
    try:
        import yaml  # noqa: F401
    except Exception:
        return False
    return True


def preflight(version_info=None, find_spec=None, bundle=None):
    """Return None when the helper can run, else one actionable failure record.

    Nothing from the Pod package is imported until every prerequisite holds, so a
    missing one produces guidance rather than a traceback.
    """
    version = sys.version_info if version_info is None else version_info
    if tuple(version[:2]) < MIN_PYTHON:
        running = ".".join(str(part) for part in version[:3])
        return {"code": "python_too_old",
                "message": ("Python %d.%d or newer is required; this is %s at %s. Re-run with a "
                            "newer interpreter, for example: python3.13 %s"
                            % (MIN_PYTHON[0], MIN_PYTHON[1], running, sys.executable, os.path.realpath(__file__)))}
    root = bundle_dir() if bundle is None else bundle
    missing = [name for name in REQUIRED if not os.path.isfile(os.path.join(root, name))]
    if missing:
        return {"code": "bundle_incomplete",
                "message": ("Pod bundle at %s is incomplete (missing %s). Reinstall it: %s"
                            % (root, ", ".join(missing), REINSTALL))}
    probe = _yaml_importable if find_spec is None else find_spec
    if not probe():
        return {"code": "pyyaml_missing",
                "message": ("PyYAML is not importable by %s. One user-space step installs it: %s "
                            "(or install your distribution's python-yaml package). Pod installs nothing itself."
                            % (sys.executable, PYYAML_STEP))}
    return None


def load_pod(bundle):
    """Import the bundled package as `pod`, ahead of any installed copy."""
    if sys.modules.get("pod") is not None:
        return sys.modules["pod"]
    spec = importlib.util.spec_from_file_location(
        "pod", os.path.join(bundle, "__init__.py"), submodule_search_locations=[bundle])
    if spec is None or spec.loader is None:
        raise SystemExit("pod: bundle at %s cannot be loaded" % bundle)
    module = importlib.util.module_from_spec(spec)
    sys.modules["pod"] = module
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    sys.dont_write_bytecode = True
    arguments = list(sys.argv[1:] if argv is None else argv)
    root = bundle_dir()
    failure = preflight(bundle=root)
    if failure is not None:
        if "--json" in arguments:
            print(json.dumps({"schema": "pod-cli/v2", "status": "blocked", "error": failure},
                             indent=2, sort_keys=True))
        else:
            print("pod: " + failure["message"], file=sys.stderr)
        return 2
    try:
        load_pod(root)
        if arguments[:1] == ["internal"]:
            from pod.internal import main as internal_main

            entry = internal_main
        else:
            from pod.cli import main as cli_main

            entry = cli_main
    except ImportError as exc:
        # The name check above covers the files needed to start. A module missing deeper in
        # the package would otherwise surface as a traceback, which is the one thing a
        # prerequisite failure must never look like.
        failure = {"code": "bundle_incomplete",
                   "message": ("Pod bundle at %s is incomplete (%s). Reinstall it: %s"
                               % (root, exc, REINSTALL))}
        if "--json" in arguments:
            print(json.dumps({"schema": "pod-cli/v2", "status": "blocked", "error": failure},
                             indent=2, sort_keys=True))
        else:
            print("pod: " + failure["message"], file=sys.stderr)
        return 2
    return entry(arguments[1:] if arguments[:1] == ["internal"] else arguments)


if __name__ == "__main__":
    raise SystemExit(main())
