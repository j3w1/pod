#!/usr/bin/env python3
"""Prove the tracked product carries no unsupported-platform implementation.

Pod's supported execution environment is Linux. This audit distinguishes an
operating-system reference from unrelated vocabulary: a quota time window is not
Windows, and a verbatim capture of another tool's output is evidence of that
tool's API, not a Pod compatibility promise.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
# `Windows` names the operating system; a lowercase `windows` identifier is a quota
# time window, which this product uses throughout and must keep.
PATTERNS = (
    re.compile(r"\bWindows\b"),
    re.compile(r"windows-latest|windows_latest"),
    re.compile(r"\bWSL\b|\bwsl\b", re.I),
    re.compile(r"\bpowershell\b|\bpwsh\b", re.I),
    re.compile(r"\bmsvcrt\b|\bwinreg\b|\bntpath\b|WinDLL|windll"),
    re.compile(r"\bLOCALAPPDATA\b|\bAPPDATA\b|\bUSERPROFILE\b"),
    re.compile(r"\.ps1\b|\bcmd\.exe\b|\bwin32\b|PureWindowsPath"),
    re.compile(r"os\.name\s*==\s*[\"\']nt[\"\']"),
    re.compile(r"sys\.platform\s*(?:==|\.startswith\()\s*[\"\']win"),
    re.compile(r"os\.sep\s*==\s*[\"\']\\\\|\bimport\s+nt\b|\bnt\.[a-z]"),
    re.compile(r"Scripts/python|Scripts\\\\pod|pod\.exe"),
)
# A line that proves an environment is refused is evidence of exclusion, not support.
MARKER = "platform-audit: refusal"
ALLOWED = (re.compile(re.escape(MARKER)),)
# Verbatim third-party captures and the history of the retired product.
EXEMPT_PREFIXES = ("tests/fixtures/", "docs/history/")


def tracked() -> list[str]:
    listing = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                             capture_output=True, text=True, check=True)
    return [line for line in listing.stdout.splitlines() if line.strip()]


def main() -> int:
    findings = []
    for name in tracked():
        if name.startswith(EXEMPT_PREFIXES) or name == "tools/platform_audit.py":
            continue
        path = ROOT / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if any(pattern.search(line) for pattern in ALLOWED):
                continue
            for pattern in PATTERNS:
                if pattern.search(line):
                    findings.append(f"{name}:{number}: {line.strip()[:160]}")
                    break
    if findings:
        print("Unsupported-platform references remain in the tracked product "
              f"(mark a deliberate refusal with `{MARKER}`):")
        for finding in findings:
            print("  " + finding)
        return 1
    print(f"Supported execution environment: Linux. {len(tracked())} tracked files audited.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
