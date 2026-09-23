#!/usr/bin/env python3
"""Release helpers for CI: current notes, the pull-request bump check and the publish gate.

Pod publishes a version only from `main`, only once, and only when the committed
`release/evidence.json` passes Pod's own release gate for that exact candidate. Nothing
here performs a release; the workflow does, after `gate` says the candidate may be
published. Exit status 3 from `gate` means "merged but not publishable", never an error.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Paths whose change ships to users, so it needs a version bump and current notes.
PRODUCT = ("skills/pod/", "pyproject.toml", "install.py", "MANIFEST.in")
EVIDENCE_FIELDS = {"schema", "version", "candidate", "tree", "records", "authorization"}
EVIDENCE_SCHEMA = "pod-release-evidence/v1"
BLOCKED = 3


def _git(root: pathlib.Path, *args: str, input_text: str | None = None) -> str:
    completed = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                               check=True, input=input_text)
    return completed.stdout


def notes(text: str, version: str) -> str:
    """The body of release/NOTES.md when its heading names this version, else nothing."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != f"# Pod {version}":
        return ""
    body = "\n".join(lines[1:]).strip()
    return body + "\n" if body else ""


def version_at(root: pathlib.Path, revision: str | None = None) -> str:
    path = "skills/pod/__init__.py"
    text = (root / path).read_text() if revision is None else _git(root, "show", f"{revision}:{path}")
    match = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    if match is None:
        raise ValueError("declared version is unavailable")
    return match.group(1)


def product_tree(root: pathlib.Path, revision: str = "HEAD") -> str:
    """The tree of everything except release/, so evidence binds what it proves, not itself."""
    entries = [line for line in _git(root, "ls-tree", revision).splitlines()
               if line.split("\t", 1)[1] != "release"]
    return _git(root, "mktree", input_text="\n".join(entries) + "\n").strip()


def release_relevant(paths: list[str]) -> list[str]:
    return sorted(path for path in paths if path.startswith(PRODUCT))


def load_evidence(root: pathlib.Path, version: str) -> dict:
    raw = json.loads((root / "release" / "evidence.json").read_text())
    if not isinstance(raw, dict) or set(raw) != EVIDENCE_FIELDS:
        raise ValueError("release/evidence.json has missing or unsupported fields")
    if raw["schema"] != EVIDENCE_SCHEMA:
        raise ValueError("release/evidence.json schema is unsupported")
    if raw["version"] != version:
        raise ValueError(f"release/evidence.json names {raw['version']}, not {version}")
    return raw


def check(root: pathlib.Path, base: str) -> list[str]:
    """What a pull request must fix: product changes need a bump; notes and evidence must be current."""
    problems = []
    current = version_at(root)
    relevant = release_relevant(_git(root, "diff", "--name-only", f"{base}...HEAD").split())
    if relevant and version_at(root, base) == current:
        shown = ", ".join(relevant[:4]) + (", ..." if len(relevant) > 4 else "")
        problems.append(f"product files changed without a version bump: {shown}")
    notes_path = root / "release" / "NOTES.md"
    if not notes(notes_path.read_text() if notes_path.exists() else "", current):
        problems.append(f"release/NOTES.md must open with '# Pod {current}' and describe this version")
    if (root / "release" / "evidence.json").exists():
        try:
            load_evidence(root, current)
        except (ValueError, OSError) as exc:
            problems.append(str(exc))
    return problems


def gate(root: pathlib.Path) -> tuple[bool, str, dict | None]:
    """Whether HEAD may be published: evidence present, bound to this content, and authorized."""
    version = version_at(root)
    if not (root / "release" / "evidence.json").exists():
        return False, f"no release/evidence.json for {version}: merged but unpublished", None
    evidence = load_evidence(root, version)
    head = _git(root, "rev-parse", "HEAD").strip()
    related = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor",
                              str(evidence["candidate"]), head], capture_output=True)
    if related.returncode != 0:
        return False, "evidence names a candidate outside this history: merged but unpublished", None
    tree = product_tree(root)
    if tree != evidence["tree"]:
        return False, (f"evidence binds product tree {str(evidence['tree'])[:12]} but HEAD has "
                       f"{tree[:12]}: merged but unpublished"), None
    sys.path.insert(0, str(root / "skills"))
    from pod.errors import PodError
    from pod.release import release_gate
    try:
        decision = release_gate(evidence["candidate"], evidence["tree"], evidence["records"],
                                evidence["authorization"])
    except PodError as exc:
        raise ValueError(f"release evidence is invalid: {exc.code}") from exc
    if decision["status"] != "authorized":
        outstanding = ", ".join(decision["missing_or_failed"]) or "owner authorization"
        return False, f"release gate {decision['status']} ({outstanding}): merged but unpublished", decision
    return True, f"release gate authorized for {version}", decision


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "notes" and len(argv) == 3:
        body = notes((ROOT / "release" / "NOTES.md").read_text(encoding="utf-8"), argv[2])
        if not body:
            print(f"release/NOTES.md does not describe {argv[2]}", file=sys.stderr)
            return 1
        sys.stdout.write(body)
        return 0
    if command == "check" and len(argv) == 3:
        problems = check(ROOT, argv[2])
        for problem in problems:
            print(f"release check: {problem}")
        return 1 if problems else 0
    if command == "tree" and len(argv) in (2, 3):
        print(product_tree(ROOT, argv[2] if len(argv) == 3 else "HEAD"))
        return 0
    if command == "gate" and len(argv) == 2:
        try:
            publish, reason, decision = gate(ROOT)
        except (ValueError, OSError) as exc:
            print(f"release gate error: {exc}")
            return 1
        print(reason)
        if decision is not None:
            print(json.dumps(decision["gates"], indent=2, sort_keys=True))
        return 0 if publish else BLOCKED
    print("usage: release.py notes VERSION | check BASE | tree [REV] | gate", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
