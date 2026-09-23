#!/usr/bin/env python3
"""Audit every tracked file for private residue and for material Pod no longer has.

Findings name the file, the category and the line, never the matched text. Pod is
distributed only as a skill from `main` through the skills CLI, so its own publication
machinery is a finding here. A governed project's release or deploy step is ordinary
Pod functionality and is not.
"""

from __future__ import annotations

import pathlib
import re
import stat
import subprocess
import sys

# A leak. None of these has a legitimate reason to appear in the repository.
FORBIDDEN = (
    ("personal path", re.compile(rb"/home/[a-z0-9_-]+/")),
    ("personal account", re.compile(rb"[A-Za-z0-9._%+-]+@(?!example\.invalid)[A-Za-z0-9.-]+\.[a-z]{2,}")),
    ("live runtime identifier", re.compile(rb"\b(?:ctx|run|task|term|wtr)_[0-9a-f]{12}\b")),
    ("credential-shaped", re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")),
    ("machine-local task state", re.compile(rb"\.local/state/[^/\s]+/tasks/")),
)
# Mechanisms and documents Pod does not have. These are Pod's own identifiers, never Orca's
# words, so sanitized fixtures are held to them too.
MECHANISM = (
    ("unsupported mechanism", re.compile(
        rb"orchestrate|legacy_hold|state[-_]migrate|\bcapacity_full\b|pod-context/v[12]|pod-governor/v1"
        rb"|docs/history|backwards?[ -]compat|migration_required|pod-migration|pod-progress"
        rb"|platform_audit")),
)
# Wording that describes an earlier Pod rather than this one. Orca's captures may use these
# words for their own reasons, so sanitized fixtures are exempt from this table only.
WORDING = (
    ("trail wording", re.compile(rb"\b(?:retired|legacy|historical|migration|baseline commit)\b", re.I)),
)
# Pod publishing itself: its own schemas, operations and release files, a pinned or packaged
# Pod source, or prose that names Pod as the thing released, tagged or published. A governed
# project's own release vocabulary, its release gate and its gh/git release commands are
# ordinary Pod functionality and never match.
PUBLICATION = (
    ("Pod publication", re.compile(
        rb"release/evidence|release/NOTES|tools/release\.py|internal release-gate|pod-release-"
        rb"|pod\.release\b|from \.release import|github\.com/j3w1/pod/releases|j3w1/pod#"
        rb"|j3w1[-_]pod\b|pod-skill-|--expected-commit|SHA256SUMS"
        rb"|\bPod(?:'s)?(?: own)? (?:releases?|release[-_ ](?:gate|notes|evidence|workflow)|tags?|publication)\b"
        rb"|(?i:\b(?:publish(?:es|ed|ing)?|releas(?:e|es|ed|ing)|tag(?:s|ged|ging)?) Pod\b)")),
)
# Tracked paths that would mean Pod is being packaged or published again.
PUBLICATION_PATHS = ("release/", "CHANGELOG", "pyproject.toml", "setup.py", "setup.cfg",
                     "MANIFEST.in", "install.py", "skills/pod/release.py")
# The guard names its own patterns, so its two files are exempt from every table but FORBIDDEN.
GUARD_FILES = ("tools/source_audit.py", "tests/test_source_audit.py")
FIXTURES = "tests/fixtures/"


def _line_number(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def _scan(name: str, data: bytes) -> list[str]:
    tables = [FORBIDDEN]
    if not name.startswith(GUARD_FILES):
        tables += [MECHANISM, PUBLICATION]
        if not name.startswith(FIXTURES):
            tables.append(WORDING)
    findings = []
    for table in tables:
        for label, pattern in table:
            match = pattern.search(data)
            if match:
                findings.append(f"{label} at line {_line_number(data, match.start())}")
    return findings


def audit_source(root: pathlib.Path) -> list[str]:
    """Scan every tracked file; sanitized fixtures are held to the same leak rule."""
    completed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                               capture_output=True, check=False)
    if completed.returncode:
        return ["tracked source inventory is unavailable"]
    findings = []
    for raw_name in completed.stdout.split(b"\0"):
        if not raw_name:
            continue
        try:
            name = raw_name.decode("utf-8")
        except UnicodeError:
            findings.append("tracked source path is not UTF-8")
            continue
        if name.startswith(PUBLICATION_PATHS) and name != "skills/pod/setup.py":
            findings.append(f"{name} :: Pod publication path")
            continue
        path = root / name
        try:
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode) or path.stat().st_size > 2_000_000:
                continue
            data = path.read_bytes()
        except OSError:
            findings.append(f"{name} :: tracked source is unreadable")
            continue
        findings.extend(f"{name} :: {finding}" for finding in _scan(name, data))
    return findings


def main(argv: list[str]) -> int:
    root = pathlib.Path(argv[1]) if len(argv) == 2 else pathlib.Path(".")
    if len(argv) > 2:
        print("Name at most one repository root.")
        return 2
    findings = audit_source(root)
    if findings:
        print("Tracked source must not carry these:")
        for finding in findings:
            print("  " + finding)
        return 1
    print("Tracked source audited; no personal path, account, runtime identifier, credential, "
          "unsupported mechanism or Pod publication material.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
