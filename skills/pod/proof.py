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


def findings(state: State) -> list[dict]:
    """Every finding with whether it is still open."""
    reviews = state.of("review")
    waived = {fact["finding"]: fact for fact in state.of("waiver")}
    rows = []
    for review in reviews:
        for finding in review.get("findings", []):
            later = [other for other in reviews if other["n"] > review["n"] and other["verdict"] == "pass"
                     and not other.get("wrote")]
            if finding.get("path"):
                closing = next((other for other in later if finding["path"] in other["paths"]), None)
            else:
                closing = next((other for other in later if set(review["paths"]) <= set(other["paths"])), None)
            closed = (f"waived in entry {waived[finding['id']]['n']}" if finding["id"] in waived
                      else f"passed in entry {closing['n']}" if closing else None)
            rows.append({**finding, "review": review["n"], "open": closed is None, "closed": closed})
    return rows


def open_required(state: State) -> list[dict]:
    return [row for row in findings(state) if row["open"] and row.get("required")]


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


def authorization(state: State, *, repository: str | None, target: str, candidate: str) -> dict | None:
    """The latest merge authorization that covers this candidate, unless a later one withdrew it."""
    for fact in reversed(state.of("authorization")):
        if fact.get("repository") != repository or fact.get("target") != target:
            continue
        if fact.get("withdrawn"):
            return None
        if fact.get("standing") or fact.get("candidate") == candidate:
            return fact
    return None
