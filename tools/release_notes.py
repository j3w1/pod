#!/usr/bin/env python3
"""Print the CHANGELOG section for one version; it is the GitHub Release body.

The rule lives here once: a heading `## <version> — <date>` opens a section and the next
`## ` heading closes it. A missing or empty section fails, so a version bump without notes
is caught before anything is tagged or published.
"""

from __future__ import annotations

import pathlib
import re
import sys

CHANGELOG = pathlib.Path(__file__).resolve().parents[1] / "CHANGELOG.md"


def section(text: str, version: str) -> str:
    heading = re.compile(rf"^## {re.escape(version)}(?:\s|$)", re.M)
    match = heading.search(text)
    if match is None:
        return ""
    line_end = text.find("\n", match.start())
    rest = text[line_end + 1:] if line_end != -1 else ""
    following = re.search(r"^## ", rest, re.M)
    body = rest[:following.start()] if following else rest
    return body.strip() + "\n" if body.strip() else ""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Name exactly one version.", file=sys.stderr)
        return 2
    version = argv[1]
    notes = section(CHANGELOG.read_text(encoding="utf-8"), version)
    if not notes:
        print(f"CHANGELOG.md has no section for {version}", file=sys.stderr)
        return 1
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
