#!/usr/bin/env python3
"""Audit built distributions before they are published.

This reads the artifacts themselves, not the source tree, because a distribution can carry
files the working tree does not. It distinguishes a deliberate historical reference from a
leak: naming the retired product in migration notes is the point; carrying a personal path,
account, runtime identifier or credential never is.
"""

from __future__ import annotations

import pathlib
import re
import sys
import tarfile
import zipfile

# A leak. None of these has a legitimate reason to appear in a published artifact.
FORBIDDEN = (
    ("personal path", re.compile(rb"/home/[a-z0-9_-]+/")),
    ("personal account", re.compile(rb"[A-Za-z0-9._%+-]+@(?!example\.invalid)[A-Za-z0-9.-]+\.[a-z]{2,}")),
    ("live runtime identifier", re.compile(rb"\b(?:ctx|run|task|term|wtr)_[0-9a-f]{12}\b")),
    ("credential-shaped", re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")),
)
# The retired platform, using the source audit's own definitions so there is exactly one.
# `Windows` names the operating system; a lowercase `windows` identifier is a quota window.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from platform_audit import ALLOWED, EXEMPT_PREFIXES, PATTERNS  # noqa: E402

HISTORICAL = ("docs/history/", "CHANGELOG", "pod-migration", "release-notes",
              "PKG-INFO", "METADATA", "README")


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


def audit(paths: list[pathlib.Path]) -> list[str]:
    findings = []
    for artifact in paths:
        for name, data in members(artifact):
            for label, pattern in FORBIDDEN:
                match = pattern.search(data)
                if match:
                    findings.append(f"{artifact.name} :: {name} :: {label}: "
                                    f"{match.group(0)[:60].decode('utf-8', 'replace')}")
            relative = name.split("/", 1)[1] if "/" in name and name.startswith(("pod/", "j3w1_pod-")) else name
            if any(marker in name for marker in HISTORICAL):
                continue
            if relative.startswith(EXEMPT_PREFIXES):
                continue
            for raw in data.splitlines():
                try:
                    line = raw.decode("utf-8")
                except UnicodeError:
                    continue
                if any(allowed.search(line) for allowed in ALLOWED):
                    continue
                if any(pattern.search(line) for pattern in PATTERNS):
                    findings.append(f"{artifact.name} :: {name} :: retired platform: "
                                    f"{line.strip()[:80]}")
                    break
    return findings


def main(argv: list[str]) -> int:
    paths = [pathlib.Path(a) for a in argv[1:]]
    paths = [p for p in paths if p.is_file() and p.name != "SHA256SUMS"]
    if not paths:
        print("Name the artifacts to audit.")
        return 2
    findings = audit(paths)
    if findings:
        print("Artifacts must not be published with these:")
        for finding in findings:
            print("  " + finding)
        return 1
    print(f"{len(paths)} artifact(s) audited; no personal path, account, runtime identifier, "
          "credential or retired-platform reference outside history.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
