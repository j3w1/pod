"""The source issue: what it asks for, and the one update Pod makes to it at delivery.

Document types get light recognition only. An EEL or SEAL is not something to deliver, so it
is refused by its title; a Proof of Done checklist, where one exists, is read as the criteria.
"""

import hashlib
import re

from .errors import PodError

BULLET = r"\s*(?:[-*+]|\d{1,9}[.)])\s+"
BOX = re.compile(r"^(" + BULLET + r"\[)([ xX])(\]\s+)(.*?)\s*$")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*$")
REFERENCE = re.compile(r"^(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#?(?P<number>\d+)$")
URL = re.compile(r"^https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/issues/(?P<number>\d+)")
NOT_DELIVERABLE = {"eel": "an EEL records evidence and is not delivered", "seal": "a SEAL plans several deliveries; "
                   "open one of its execution issues instead"}


def reference(text: str, default_repository: str | None) -> tuple[str, int]:
    match = URL.match(text.strip()) or REFERENCE.match(text.strip())
    repository = (match and match["repo"]) or default_repository
    if not match or not repository:
        raise PodError("issue_reference_invalid", f"{text!r} does not name an issue.",
                       fix="use a number, owner/name#number or the issue URL")
    return repository, int(match["number"])


def digest(body: str) -> str:
    return hashlib.sha256((body or "").encode()).hexdigest()


def kind(title: str) -> str | None:
    match = re.match(r"\s*(EEL|SEAL|PES)\s*:", title or "", re.IGNORECASE)
    return match[1].lower() if match else None


def refuse_undeliverable(title: str) -> None:
    found = kind(title)
    if found in NOT_DELIVERABLE:
        raise PodError("document_not_deliverable", f"This issue is titled as {NOT_DELIVERABLE[found]}.",
                       fix="open the issue that states the work to deliver")


def _prose(lines: list[str]) -> list[bool]:
    """Per line, whether it is ordinary text: a line inside a fenced code block or an HTML comment is
    neither a heading nor a box.

    A fence opens with three or more backticks or tildes (a backtick opener carries no further
    backtick) and closes with at least as many of the same character and nothing after them. A
    comment runs from `<!--` to the next `-->`, across lines.
    """
    flags, fence, comment = [], None, False
    for raw in lines:
        line = raw.rstrip("\r")
        if comment:
            comment = "-->" not in line
            flags.append(False)
            continue
        mark = re.match(r"\s*(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            if mark and mark[1][0] == fence[0] and len(mark[1]) >= len(fence) and not mark[2].strip():
                fence = None
            flags.append(False)
            continue
        if mark and not (mark[1][0] == "`" and "`" in mark[2]):
            fence = mark[1]
            flags.append(False)
            continue
        opened = line.rfind("<!--")
        comment = opened >= 0 and "-->" not in line[opened:]
        flags.append(not (comment and not line[:opened].strip()))     # text before an opening comment still counts
    return flags


def criteria(body: str) -> list[dict]:
    """Unchecked checklist items, from the Proof of Done section when there is one, else the whole body."""
    lines = (body or "").split("\n")
    prose = _prose(lines)
    start, end, level = 0, len(lines), None
    for index, line in enumerate(lines):
        heading = HEADING.match(line.rstrip("\r")) if prose[index] else None
        if not heading:
            continue
        if level is None and re.search(r"proof of done", heading[2], re.IGNORECASE):
            start, level = index + 1, len(heading[1])
        elif level is not None and len(heading[1]) <= level:
            end = index
            break
    found = []
    for index in range(start, end):
        box = BOX.match(lines[index].rstrip("\r")) if prose[index] else None
        if box and box[2] == " " and box[4]:
            found.append({"text": box[4], "line": index})
    return found


def tick(body: str, lines: list[int]) -> str:
    """The body with those checklist lines ticked and not one other character changed."""
    rows = body.split("\n")
    for index in lines:
        if 0 <= index < len(rows):
            rows[index] = re.sub(r"^(" + BULLET + r"\[) (\])", r"\1x\2", rows[index], count=1)
    return "\n".join(rows)
