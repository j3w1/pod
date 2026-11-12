"""Leave the environment clean when an objective ends, using only what Orca and Git already do.

Pod keeps no list of resources and plans nothing. When an objective closes or is dropped it
reads its workers from Orca and asks Orca to release each settled worker's terminal and to
remove each worktree the objective itself asked Orca to create. Git decides whether a worktree
is disposable: its tree is clean and every commit it holds was delivered or is on the target.
Anything running, kept open, changed, unmerged, shared or unreadable is left exactly as it is
and named with the reason. A removal that fails changes nothing about the delivery; running
the same command again tries again.

Orca keeps the terminal of a worker that got its own worktree open by default and reports it
exactly as it reports one a person kept open. Such a terminal therefore goes with its worktree:
it is released only when the worktree itself may be removed. In any other worktree a kept
terminal is never released.
"""

from pathlib import Path

from . import gitio, orca
from .errors import PodError
from .state import State


def tidy(writer, root: Path, state: State, *, target_head: str | None) -> dict:
    """Release and remove what this objective created and no longer needs. Never raises."""
    run = state.objective.get("run")
    rows = [row for row in state.assignments.values() if row.get("dispatch")]
    if not run or not rows:
        return {}
    try:
        workers = {row["dispatch"]: row for row in orca.workers(run)}
    except PodError as exc:
        return {"cleanup": {"kept": [f"everything: Orca could not be read ({exc.code})"]},
                "next": "run the same command again to clean up"}
    delivered = [effect["intent"]["head"] for effect in state.merges() if state.merged(effect)]
    safe = delivered + ([target_head] if target_head else [])
    release, remove, kept = [], [], []
    for row in rows:
        worker = workers.get(row["dispatch"])
        name = f"{row['id']} ({row['title'][:40]})"
        if worker is None or orca.live(worker):
            kept.append(f"{name}: {'still running' if worker else 'Orca does not list it'}")
            continue
        place = worker.get("worktree")
        made = str(row.get("worktree", "")).startswith("new-") and place and Path(place) != root   # Orca made it for this assignment
        if made and Path(place).is_dir() and not any(place == path for path, _, _ in remove):
            reason = _why_keep(Path(place), safe, worker, list(workers.values()))
            if reason:
                kept.append(f"worktree {place}: {reason}")
            else:
                remove.append((place, gitio.branch(Path(place)), gitio.commit(Path(place))))
                if worker["terminal"] in ("reclaimable", "retained") and worker["owned"]:
                    release.append(row["dispatch"])       # its terminal goes with its worktree
                continue
        if worker["terminal"] == "reclaimable" and worker["owned"]:
            release.append(row["dispatch"])
        elif worker["terminal"] not in ("released", "reclaimable") and not (made and Path(place).is_dir()):
            kept.append(f"{name}: its terminal is {worker['terminal'] or 'in an unknown state'}"
                        + (f" ({worker['retained']})" if worker.get("retained") else ""))
    if not release and not remove:
        return {"cleanup": {"kept": kept}} if kept else {}
    ident = state.next_id("k", state.effects)
    writer.append({"kind": "effect", "effect": "cleanup", "phase": "intent", "id": ident, "release": release,
                   "remove": [place for place, _, _ in remove]})
    released, removed = [], []
    for dispatch in release:
        try:
            orca.call("orchestration", "worker-release", "--dispatch", dispatch)
            released.append(dispatch)
        except PodError as exc:
            kept.append(f"worker {dispatch}: Orca did not release it ({exc.code})")
    for place, branch, head in remove:
        try:
            orca.call("worktree", "rm", "--worktree", f"path:{place}")     # never forced: Orca and Git refuse what is not clean
            removed.append(place)
        except PodError as exc:
            kept.append(f"worktree {place}: Orca did not remove it ({exc.code}: {str(exc)[:120]})")
            continue
        if branch and gitio.known(root, f"refs/heads/{branch}") == head:    # its commits are delivered: the name is all that is left
            gitio.git(root, "branch", "-D", branch, check=False)
    writer.append({"kind": "effect", "effect": "cleanup", "phase": "outcome", "id": ident, "released": released,
                   "removed": removed, "kept": kept})
    result = {"cleanup": {key: value for key, value in (("released", released), ("removed", removed), ("kept", kept)) if value}}
    if len(released) < len(release) or len(removed) < len(remove):
        result["next"] = "run the same command again to finish cleaning up; the delivery itself is unaffected"
    return result


def _why_keep(place: Path, safe: list[str], worker: dict, workers: list) -> str | None:
    """Why a worker worktree must stay, or None when Git and Orca show nothing would be lost."""
    try:
        if any(other is not worker and other.get("worktree") == str(place) and orca.live(other) for other in workers):
            return "another worker is running in it"
        if gitio.dirty(place):
            return "it has uncommitted or untracked files"
        head = gitio.commit(place)
        if not any(gitio.is_ancestor(place, head, commit) for commit in safe):
            return "it holds commits that were not delivered"
        open_ = orca.call("terminal", "list", "--worktree", f"path:{place}").get("terminals") or []
        if any(terminal.get("handle") != worker.get("handle") for terminal in open_):
            return "a terminal other than its worker's is open in it"
    except PodError as exc:
        return f"its state could not be read ({exc.code})"
    return None
