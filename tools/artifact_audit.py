#!/usr/bin/env python3
"""Audit built distributions and tracked source for private residue.

Artifacts are read as archives, not from the working tree, because a distribution can carry
files the tree does not. Findings name the file, the category and the line, never the
matched text.
"""

from __future__ import annotations

import pathlib
import re
import stat
import subprocess
import sys
import tarfile
import zipfile

# A leak. None of these has a legitimate reason to appear in a published artifact.
FORBIDDEN = (
    ("personal path", re.compile(rb"/home/[a-z0-9_-]+/")),
    ("personal account", re.compile(rb"[A-Za-z0-9._%+-]+@(?!example\.invalid)[A-Za-z0-9.-]+\.[a-z]{2,}")),
    ("live runtime identifier", re.compile(rb"\b(?:ctx|run|task|term|wtr)_[0-9a-f]{12}\b")),
    ("credential-shaped", re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")),
    ("machine-local task state", re.compile(rb"\.local/state/[^/\s]+/tasks/")),
)

def members(path: pathlib.Path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    yield info.filename, archive.read(info)
    elif path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path) as archive:
            for info in archive.getmembers():
                if info.isfile():
                    handle = archive.extractfile(info)
                    if handle is not None:
                        yield info.name, handle.read()


def _line_number(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def audit(paths: list[pathlib.Path]) -> list[str]:
    findings = []
    for artifact in paths:
        for name, data in members(artifact):
            for label, pattern in FORBIDDEN:
                match = pattern.search(data)
                if match:
                    findings.append(f"{artifact.name} :: {name} :: {label} at line "
                                    f"{_line_number(data, match.start())}")
    return findings


def audit_source(root: pathlib.Path) -> list[str]:
    """Scan every tracked file; sanitized fixtures are held to the same rule."""
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
        path = root / name
        try:
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode) or path.stat().st_size > 2_000_000:
                continue
            data = path.read_bytes()
        except OSError:
            findings.append(f"{name} :: tracked source is unreadable")
            continue
        for label, pattern in FORBIDDEN:
            match = pattern.search(data)
            if match:
                findings.append(f"{name} :: {label} at line "
                                f"{_line_number(data, match.start())}")
    return findings


def main(argv: list[str]) -> int:
    arguments = argv[1:]
    source = None
    if arguments[:1] == ["--source"] and len(arguments) >= 2:
        source = pathlib.Path(arguments[1])
        arguments = arguments[2:]
    paths = [pathlib.Path(a) for a in arguments]
    paths = [p for p in paths if p.is_file() and p.name != "SHA256SUMS"]
    if source is None and not paths:
        print("Name the artifacts to audit or pass --source ROOT.")
        return 2
    findings = audit(paths) + (audit_source(source) if source is not None else [])
    if findings:
        print("Artifacts must not be published with these:")
        for finding in findings:
            print("  " + finding)
        return 1
    print(f"{len(paths)} artifact(s) and {'tracked source' if source else 'no source tree'} audited; "
          "no personal path, account, runtime identifier or credential.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
