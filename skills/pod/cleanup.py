"""Leave the environment clean when an objective ends, using only what Orca and Git already do.

Pod keeps no list of resources and plans nothing. When an objective closes or is dropped it
reads its workers from Orca and asks Orca to release each finished worker's terminal and to
remove each worktree the objective itself asked Orca to create. Git decides whether a worktree
is disposable: its tree is clean and every commit it holds was delivered or is on the target.
Anything running, unproven, kept open, changed, unmerged, shared or unreadable is left exactly
as it is and named with the reason. A removal that fails changes nothing about the delivery;
running the same command again tries again.

Orca keeps the terminal of a worker that got its own worktree open by default and reports it
exactly as it reports one a person kept open. Such a terminal therefore goes with its worktree:
it is released only when the worktree itself may be removed, and the worktree is removed only
after Orca confirms the release. In any other worktree a kept terminal is never released.
"""

from pathlib import Path

from . import gitio, log, orca
from .errors import PodError
from .state import State, fold


def tidy(writer, root: Path, state: State, *, target_head: str | None, again: str) -> dict:
    """Release and remove what this objective created and no longer needs. Never raises."""
    run = state.objective.get("run")
    rows = [row for row in state.assignments.values() if row.get("dispatch")]
    if not run or not rows:
        return {}
    retry = f"run `{again}` again to finish cleaning up; the delivery itself is unaffected"
    try:
        workers = {row["dispatch"]: row for row in orca.workers(run)}
    except PodError as exc:
        return {"cleanup": {"kept": [f"everything: Orca could not be read ({exc.code})"]}, "next": retry}
    delivered = [effect["intent"]["head"] for effect in state.merges() if state.merged(effect)]
    safe = delivered + ([target_head] if target_head else [])
    release, remove, kept = [], {}, []
    for row in rows:
        worker = workers.get(row["dispatch"])
        name = f"{row['id']} ({row['title'][:40]})"
        if worker is None or not _finished(worker):
            kept.append(f"{name}: " + ("Orca does not list it" if worker is None else "still running" if orca.live(worker)
                                       else f"Orca does not show that it finished ({worker['status']}, {worker['state']})"))
            continue
        place = worker.get("worktree")
        made = str(row.get("worktree", "")).startswith("new-") and place and Path(place) != root   # Orca made it for this assignment
        if made and Path(place).is_dir() and place not in remove:
            reason = (None if worker["owned"] and worker["terminal"] in ("reclaimable", "retained", "released")
                      else f"its worker's terminal is {worker['terminal'] or 'in an unknown state'}")
            reason = reason or _why_keep(Path(place), safe, worker, list(workers.values()), state.id)
            if reason:
                kept.append(f"worktree {place}: {reason}")
            else:               # its terminal goes with it: released first, and the worktree only if that succeeds
                remove[place] = (gitio.branch(Path(place)), gitio.commit(Path(place)),
                                 row["dispatch"] if worker["terminal"] != "released" else None)
            continue
        if worker["terminal"] == "reclaimable" and worker["owned"]:
            release.append(row["dispatch"])
        elif worker["terminal"] not in ("released", "reclaimable"):
            kept.append(f"{name}: its terminal is {worker['terminal'] or 'in an unknown state'}"
                        + (f" ({worker['retained']})" if worker.get("retained") else ""))
    if not release and not remove:
        return {"cleanup": {"kept": kept}} if kept else {}
    ident = state.next_id("k", state.effects)
    writer.append({"kind": "effect", "effect": "cleanup", "phase": "intent", "id": ident,
                   "release": release + [dispatch for _, _, dispatch in remove.values() if dispatch], "remove": list(remove)})
    released, removed, failed = [], [], 0
    for dispatch in release:
        problem = _release(dispatch)
        released += [] if problem else [dispatch]
        kept += [f"worker {dispatch}: {problem}"] if problem else []
        failed += bool(problem)
    for place, (branch, head, dispatch) in remove.items():
        try:
            problem = _release(dispatch) if dispatch else None
            if problem:
                raise PodError("release", f"its worker's terminal was not released: {problem}")
            released += [dispatch] if dispatch else []
            orca.call("worktree", "rm", "--worktree", f"path:{place}")     # never forced: Orca and Git refuse what is not clean
            removed.append(place)
        except PodError as exc:
            kept.append(f"worktree {place}: " + (str(exc) if exc.code == "release"
                                                 else f"Orca did not remove it ({exc.code}: {str(exc)[:120]})"))
            failed += 1
            continue
        if branch and gitio.known(root, f"refs/heads/{branch}") == head:    # its commits are delivered: the name is all that is left
            gitio.git(root, "branch", "-D", branch, check=False)
    writer.append({"kind": "effect", "effect": "cleanup", "phase": "outcome", "id": ident, "released": released,
                   "removed": removed, "kept": kept})
    result = {"cleanup": {key: value for key, value in (("released", released), ("removed", removed), ("kept", kept)) if value}}
    return {**result, "next": retry} if failed else result


def _finished(worker: dict) -> bool:
    """Whether Orca positively shows the worker reported and ended. A stopped or abandoned one proves nothing."""
    return worker["status"] == "completed" and worker["state"] in ("succeeded", "failed")


def _release(dispatch: str) -> str | None:
    """Ask Orca to release one worker's terminal. Returns why it is not released, read from Orca's own receipt."""
    try:
        state = orca.call("orchestration", "worker-release", "--dispatch", dispatch).get("state")
    except PodError as exc:
        return f"Orca did not release it ({exc.code})"
    return None if state == "released" else f"Orca reports it {state or 'in an unknown state'} after the release was asked"


def _why_keep(place: Path, safe: list[str], worker: dict, workers: list, objective: str) -> str | None:
    """Why a worker worktree must stay, or None when Git, Orca and Pod's own logs show nothing would be lost."""
    try:
        if any(other is not worker and other.get("worktree") == str(place) and not _finished(other) for other in workers):
            return "another worker is using it"
        for other in log.every():
            claim = fold(log.read(other))
            if claim.entries and not claim.closed and claim.id != objective and claim.objective.get("worktree") == str(place):
                return f"objective {claim.id} works in it"
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
