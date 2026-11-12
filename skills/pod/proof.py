"""What recorded facts prove about a candidate. Derived from Git every time; nothing is declared.

A check fact applies to a candidate with the same tree. A review fact applies to a path while
the candidate's blob at that path equals the reviewed blob, unless one of the review's context
paths changed. A candidate is reviewed when every path it changes against its merge base is
covered by an applicable passing review whose reviewer changed nothing.
"""

from pathlib import Path

from . import gitio
from .errors import PodError
from .state import State


def checks(state: State, tree: str) -> list[dict]:
    return [fact for fact in state.of("check") if fact.get("tree") == tree]


def findings(cwd: Path, state: State) -> list[dict]:
    """Every finding with whether it is still open.

    A finding is closed by a later passing review, from a worker that changed nothing, of content
    that differs from what the finding was raised on: a second opinion on the same content does
    not overrule it, and a carried review never does. Otherwise only a waiver closes it.
    """
    reviews = state.of("review")
    waived = {fact["finding"]: fact for fact in state.of("waiver")}
    rows = []
    for review in reviews:
        later = [other for other in reviews if other["n"] > review["n"] and other["verdict"] == "pass"
                 and not other.get("wrote") and not other.get("carried")]
        for finding in review.get("findings", []):
            wanted = [finding["path"]] if finding.get("path") else review["paths"]
            closing = next((other for other in later if set(wanted) <= set(other["paths"])
                            and _corrected(cwd, review["commit"], other["commit"], wanted)), None)
            closed = (f"waived in entry {waived[finding['id']]['n']}" if finding["id"] in waived
                      else f"passed in entry {closing['n']}" if closing else None)
            rows.append({**finding, "review": review["n"], "open": closed is None, "closed": closed})
    return rows


def _corrected(cwd: Path, then: str, now: str, names: list[str]) -> bool:
    try:
        return gitio.blobs(cwd, then, names) != gitio.blobs(cwd, now, names)
    except PodError:
        return False


def open_required(cwd: Path, state: State) -> list[dict]:
    return [row for row in findings(cwd, state) if row["open"] and row.get("required")]


def coverage(cwd: Path, state: State, candidate: str, base: str, *, final_tree: bool = False) -> dict:
    """Which paths changed since the merge base, and which of them no applicable review covers."""
    changed = gitio.changed(cwd, base, candidate)
    tree = gitio.tree(cwd, candidate)
    current = gitio.blobs(cwd, candidate, changed)
    covered: dict = {}
    for review in state.of("review"):
        if review["verdict"] != "pass" or review.get("wrote") or (final_tree and review.get("tree") != tree):
            continue
        context = review.get("context") or []
        wanted = [name for name in review["paths"] if name in current and name not in covered]
        if not wanted:
            continue
        try:
            then = gitio.blobs(cwd, review["commit"], wanted + context)
        except PodError:         # the reviewed commit is not in this repository: it proves nothing here
            continue
        if context and then_differs(cwd, candidate, then, context):
            continue
        for name in wanted:
            if then[name] == current[name]:
                covered[name] = review["n"]
    return {"changed": changed, "uncovered": [name for name in changed if name not in covered], "covered": covered}


def then_differs(cwd: Path, candidate: str, then: dict, context: list[str]) -> bool:
    now = gitio.blobs(cwd, candidate, context)
    return any(now[name] != then[name] for name in context)


def _authorizations(state: State, repository: str | None, target: str) -> list[dict]:
    return [fact for fact in reversed(state.of("authorization"))
            if fact.get("repository") == repository and fact.get("target") == target]


def withdrawn(state: State, *, repository: str | None, target: str) -> bool:
    """Whether the latest word on merging here is a withdrawal. Only a later explicit authorization lifts it."""
    latest = next(iter(_authorizations(state, repository, target)), None)
    return bool(latest and latest.get("withdrawn"))


def authorization(state: State, *, repository: str | None, target: str, candidate: str) -> dict | None:
    """The latest merge authorization that covers this candidate, unless a later one withdrew it."""
    for fact in _authorizations(state, repository, target):
        if fact.get("withdrawn"):
            return None
        if fact.get("standing") or fact.get("candidate") == candidate:
            return fact
    return None
