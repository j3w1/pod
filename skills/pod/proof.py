"""What recorded facts prove about a candidate. Derived from Git every time; nothing is declared.

A check fact applies to a candidate with the same tree. A review fact applies to a path while
the candidate's blob at that path equals the reviewed blob, unless one of the review's context
paths changed. A candidate is reviewed when every path it changes against its merge base is
covered by an applicable completed review, by a reviewer who changed nothing, that raised no
required finding on that path.
"""

from pathlib import Path
import re

from . import gitio
from .errors import PodError
from .state import State


def checks(state: State, tree: str) -> list[dict]:
    return [fact for fact in state.of("check") if fact.get("tree") == tree]


def _finding_paths(cwd: Path, review: dict) -> dict:
    """Resolve locations in their original reviewed tree without changing recorded evidence."""
    options = {}
    for finding in review.get("findings", []):
        name = finding.get("path")
        if not name:
            continue
        options[name] = {name}
        for count in (1, 2):
            parts = name.rsplit(":", count)
            if len(parts) == count + 1 and all(re.fullmatch(r"[1-9][0-9]*", p) for p in parts[1:]):
                options[name].add(parts[0])
    try:
        blobs = gitio.blobs(cwd, review["commit"], list({p for names in options.values() for p in names}))
    except PodError:
        return dict.fromkeys(options)  # an unavailable original tree proves no path identity
    result = {}
    for name, names in options.items():
        matches = [p for p in names if blobs[p] is not None]
        if blobs[name] is not None:
            result[name] = name
        else:
            result[name] = matches[0] if len(matches) == 1 else None
    return result


def _clean(review: dict, name: str, locations: dict) -> bool:
    """Whether a review examined this path and left nothing on it that must be fixed.

    The review was completed by a worker that changed nothing, raised no required finding on the
    path and none without a path, and either passed or explained its failure with required
    findings elsewhere. So a full review that fails over two files still covers the others, and
    only the corrected files need another look.
    """
    if not review.get("completed", True) or review.get("wrote"):
        return False
    required = [finding for finding in review.get("findings", []) if finding.get("required")]
    if any(not finding.get("path") or locations[review["n"]][finding["path"]] in (None, name) for finding in required):
        return False
    return review["verdict"] == "pass" or bool(required)


def findings(cwd: Path, state: State, candidate: str) -> list[dict]:
    """Every finding with whether it is open for this candidate.

    A finding is closed by a later completed review, from a worker that changed nothing, that
    raised nothing required on the finding's path, of content that differs from what the finding
    was raised on and that the candidate still holds. A second opinion on the same content does not overrule a finding, a carried review
    never closes one, and bringing the offending content back reopens it. Otherwise only a
    waiver closes it.
    """
    reviews = state.of("review")
    locations = {review["n"]: _finding_paths(cwd, review) for review in reviews}
    waived = {fact["finding"]: fact for fact in state.of("waiver")}
    rows, now = [], {}
    for review in reviews:
        later = [other for other in reviews if other["n"] > review["n"] and not other.get("carried")]
        for finding in review.get("findings", []):
            wanted = [locations[review["n"]][finding["path"]]] if finding.get("path") else review["paths"]
            closing = next((other for other in later if set(wanted) <= set(other["paths"])
                            and all(_clean(other, name, locations) for name in wanted)
                            and _corrected(cwd, review["commit"], other["commit"], wanted, candidate, now)), None)
            closed = (f"waived in entry {waived[finding['id']]['n']}" if finding["id"] in waived
                      else f"passed in entry {closing['n']}" if closing else None)
            rows.append({**finding, "review": review["n"], "open": closed is None, "closed": closed})
    return rows


def _corrected(cwd: Path, raised: str, passed: str, names: list[str], candidate: str, cache: dict) -> bool:
    """The passing review saw different content than the finding did, and the candidate holds that content."""
    try:
        key = tuple(names)
        if key not in cache:
            cache[key] = gitio.blobs(cwd, candidate, names)
        then = gitio.blobs(cwd, passed, names)
        return then != gitio.blobs(cwd, raised, names) and then == cache[key]
    except PodError:
        return False


def open_required(cwd: Path, state: State, candidate: str) -> list[dict]:
    return [row for row in findings(cwd, state, candidate) if row["open"] and row.get("required")]


def coverage(cwd: Path, state: State, candidate: str, base: str, *, final_tree: bool = False) -> dict:
    """Which paths changed since the merge base, and which of them no applicable review covers."""
    changed = gitio.changed(cwd, base, candidate)
    tree = gitio.tree(cwd, candidate)
    current = gitio.blobs(cwd, candidate, changed)
    covered: dict = {}
    locations = {review["n"]: _finding_paths(cwd, review) for review in state.of("review")}
    for review in state.of("review"):
        if final_tree and review.get("tree") != tree:
            continue
        context = review.get("context") or []
        wanted = [name for name in review["paths"] if name in current and name not in covered and _clean(review, name, locations)]
        if not wanted:
            continue
        try:
            then = gitio.blobs(cwd, review["commit"], wanted)
            if context and gitio.identities(cwd, review["commit"], context) != gitio.identities(cwd, candidate, context):
                continue             # something the review depended on changed: it no longer applies
        except PodError:         # the reviewed commit is not in this repository: it proves nothing here
            continue
        for name in wanted:
            if then[name] == current[name]:
                covered[name] = review["n"]
    return {"changed": changed, "uncovered": [name for name in changed if name not in covered], "covered": covered}


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
