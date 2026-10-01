"""Execution and provider readback for admitted Governor effects."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import PodError
from .github import GhPort
from .ledger import _lock, _path, _read
from .util import bounded_text

if TYPE_CHECKING:
    from .governor import GitHubPort


def _uncertain(exc: PodError) -> bool:
    return exc.code in ("remote_effect_uncertain", "remote_read_failed")


def _outcome_from_run(run: dict) -> tuple[str, str | None]:
    status = run.get("status")
    conclusion = run.get("conclusion")
    if status != "completed":
        return "pending", None
    if conclusion == "success":
        return "PASS", None
    if conclusion == "cancelled":
        return "CANCELED", None
    if conclusion == "skipped":
        return "FAILED", "skipped: a skipped required check leaves the check pending, not passed"
    return "FAILED", str(conclusion) if conclusion is not None else "completed without a conclusion"


def _provider_run(run: dict) -> dict:
    return {"run_id": str(run.get("id")), "url": run.get("url"), "status": run.get("status"),
            "conclusion": run.get("conclusion"), "created_at": run.get("created_at"), "event": run.get("event")}


DISPATCH_EVENT = "workflow_dispatch"


def _requested_dispatch(row: dict) -> bool:
    """A workflow_dispatch row the coordinator asked for, as opposed to one a publication started."""
    return row["action"]["kind"] == "workflow_dispatch" and row.get("derived_from") is None


def _pick_run(runs: list[dict], *, since: str, event: str | None = None) -> dict | None:
    """The newest run of this workflow for this commit created at or after the submission.

    With an event, only runs whose read-back event is exactly that one are candidates.
    """
    try:
        # GitHub reports run creation to the second; the floor must not be finer.
        floor = datetime.fromisoformat(since).replace(microsecond=0)
    except (TypeError, ValueError):
        floor = None
    candidates = []
    for run in runs:
        if event is not None and run.get("event") != event:
            continue
        created = run.get("created_at")
        try:
            stamp = datetime.fromisoformat(str(created).replace("Z", "+00:00")) if created else None
        except ValueError:
            stamp = None
        if floor is not None and stamp is not None and stamp.tzinfo is not None and floor.tzinfo is not None \
                and stamp < floor:
            continue
        candidates.append((stamp or datetime.min.replace(tzinfo=timezone.utc), run))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _perform(port: GitHubPort, row: dict, unit: dict, journal: dict,
             pull_request: dict | None) -> tuple[str, dict | None, str | None]:
    """Perform the admitted action against the row's own bound commit. Returns outcome, provider, detail."""
    from .governor import DISPATCH_KINDS, PUBLICATION_KINDS, _WORKFLOW, _find_row
    kind = row["action"]["kind"]
    branch = unit.get("branch")
    commit = row.get("commit")
    if branch is None and kind != "cancel_validation":
        raise PodError("unit_branch_required", "Managed execution needs the unit's remote, branch and base")
    if commit is None and kind != "cancel_validation":
        raise PodError("candidate_unbound", "The admitted row carries no candidate commit")
    if kind in PUBLICATION_KINDS:
        target = branch["remote"] + "/" + branch["branch"]
        if row["action"]["target"] != target:
            raise PodError("target_mismatch", f"Publication target must be {target}")
        expected = unit["published"].get(target)
        try:
            pushed = port.push(remote=branch["remote"], branch=branch["branch"], commit=commit, expected=expected)
        except PodError as exc:
            if _uncertain(exc):
                return "UNKNOWN", None, "push response was lost"
            raise
        if pushed.get("status") != "pushed":
            return "FAILED", {"remote_head": pushed.get("remote_head")}, "branch_moved: the remote branch was not at the expected commit"
        provider = {"remote_head": commit, "branch": target}
        provider["triggered"] = _triggered_runs(port, row, commit)
        if kind == "push":
            return "PASS", provider, None
        try:
            found = port.pull_request(head=branch["branch"], base=branch["base"])
            if found is None:
                title = (pull_request or {}).get("title") or row["action"]["reason"]
                body = (pull_request or {}).get("body") or ""
                bounded_text(title, name="title", limit=256)
                if len(body) > 16 * 1024:
                    raise PodError("invalid_pull_request", "Pull request body is too large")
                found = port.open_pull_request(head=branch["branch"], base=branch["base"], title=title, body=body)
                provider["pr_reused"] = False
            else:
                provider["pr_reused"] = True
        except PodError as exc:
            if _uncertain(exc):
                return "UNKNOWN", provider, "pull request state is unknown after the push"
            raise
        provider.update({"pr": found.get("number"), "pr_url": found.get("url")})
        return "PASS", provider, None
    if kind in DISPATCH_KINDS:
        workflow = row["action"]["target"]
        if not _WORKFLOW.fullmatch(workflow):
            raise PodError("invalid_workflow", "Dispatch target must be a workflow file name")
        try:
            port.dispatch(workflow=workflow, ref=branch["branch"], inputs=row["action"].get("inputs") or {})
        except PodError as exc:
            if _uncertain(exc):
                return "UNKNOWN", None, "dispatch response was lost"
            raise
        # A validation run must sit on the candidate commit; a diagnostic ran at whatever the
        # branch carried, which the recorded pushes know. The commit looked up is recorded so
        # a later readback looks in the same place.
        if kind == "remote_diagnostic":
            commit = unit["published"].get(branch["remote"] + "/" + branch["branch"]) or commit
        lookup = {"ref_commit": commit}
        # A requested dispatch binds only a workflow_dispatch run, never a push or pull-request
        # run that happens to sit on the same commit.
        event = DISPATCH_EVENT if _requested_dispatch(row) else None
        try:
            run = _pick_run(port.runs(workflow=workflow, commit=commit), since=row["receipt"]["started_at"],
                            event=event)
        except PodError as exc:
            if _uncertain(exc):
                return "pending", lookup, "dispatched; run identity not read back yet"
            raise
        if run is None:
            return "pending", lookup, "dispatched; no run visible yet"
        return "pending", {**lookup, **_provider_run(run)}, None
    if kind == "validation_rerun":
        failed = [other for other in journal["actions"]
                  if other["action"]["unit"] == unit["name"] and other["decision"] == "ALLOW"
                  and other["outcome"] == "FAILED" and other["action"]["target"] == row["action"]["target"]
                  and other.get("candidate_id") == row.get("candidate_id")
                  and (other["receipt"].get("provider") or {}).get("run_id")]
        if not failed:
            raise PodError("rerun_unbound", "No failed run with a provider identity binds this candidate and workflow")
        run_id = str(failed[-1]["receipt"]["provider"]["run_id"])
        try:
            port.rerun(run_id=run_id, failed_only=True)
        except PodError as exc:
            if _uncertain(exc):
                return "UNKNOWN", {"run_id": run_id}, "rerun response was lost"
            raise
        return "pending", {"run_id": run_id, "rerun": True}, None
    if kind == "cancel_validation":
        target = _find_row(journal, row["action"]["target"])
        run_id = _bound_run_id(target)
        older_same_unit = (target["action"]["unit"] == row["action"]["unit"]
                           and target.get("candidate_id") != row.get("candidate_id"))
        if target["outcome"] != "pending" or not target.get("cancel_safe") or not run_id or not older_same_unit:
            raise PodError("cancel_unsafe", "Only a pending, cancel-safe validation of an older candidate in the same "
                                            "unit, with a run identity, is canceled")
        try:
            port.cancel(run_id=str(run_id))
        except PodError as exc:
            if _uncertain(exc):
                return "UNKNOWN", {"run_id": str(run_id)}, "cancel response was lost"
            raise
        return "PASS", {"run_id": str(run_id), "canceled_record": target["record_id"]}, None
    raise PodError("invalid_action", f"{kind} is decided by the governor but performed by project governance")


def _bound_run_id(row: dict) -> str | None:
    """The run a row is bound to; a run its readback rejected for another event is not one."""
    if row.get("rejected_run") is not None:
        return None
    return (row["receipt"].get("provider") or {}).get("run_id")


def _triggered_runs(port: GitHubPort, row: dict, commit: str) -> dict:
    """The runs a landed publication started, by workflow, as far as a readback can see them."""
    triggered = {}
    for effect in row.get("effects") or []:
        if not effect.startswith("workflow:"):
            continue
        workflow = effect[len("workflow:"):]
        try:
            run = _pick_run(port.runs(workflow=workflow, commit=commit), since=row["receipt"]["started_at"])
        except PodError as exc:
            if _uncertain(exc):
                continue
            raise
        if run is not None:
            triggered[workflow] = run
    return triggered


def _record_run_event(row: dict, provider: dict | None) -> None:
    """Record the event Pod itself read back for a workflow row's bound run.

    Only execution and readback reach here; a caller-supplied provider receipt never sets it.
    """
    from .governor import DISPATCH_KINDS
    if (row["action"]["kind"] in (*DISPATCH_KINDS, "validation_rerun") and provider is not None
            and provider.get("run_id") and "event" in provider):
        row["run_event"] = provider["event"]
        if (row.get("rejected_run") or {}).get("run_id") not in (None, str(provider["run_id"])):
            # A later readback bound the dispatch's own run; the rejected one stays out of it.
            row.pop("rejected_run")


def _reject_run(project: Path, objective: str, *, owner: str, record_id: str, run_id: str,
                event: object) -> dict:
    """Record that a requested dispatch's bound run is another event's; the row neither binds nor counts it."""
    from .governor import _owned_state, _find_row, _read_journal, _record_path, _write_journal
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    with _lock(context_path):
        _owned_state(project, objective, owner, context_path)
        journal = _read_journal(record_path)
        row = _find_row(journal, record_id)
        row["rejected_run"] = {"run_id": run_id, "event": event if isinstance(event, str) else None}
        _write_journal(record_path, journal)
        return dict(row)


def _rejected_reason(rejected: dict) -> str:
    return (f"run {rejected['run_id']} was triggered by {rejected.get('event') or 'an unknown event'}, "
            "not workflow_dispatch; it is not this dispatch's run")


def _record_execution(project: Path, objective: str, *, owner: str, record_id: str, outcome: str,
                      provider: dict | None, detail: str | None, now: datetime) -> dict:
    from .governor import _owned_state, _count, _find_row, _read_journal, _record_path, _settle, _write_journal
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    moment = now.isoformat()
    with _lock(context_path):
        state = _owned_state(project, objective, owner, context_path)
        journal = _read_journal(record_path)
        row = _find_row(journal, record_id)
        if outcome == "pending":
            # A submission whose response was lost and whose run is now visible is running,
            # not unknown; the readback settles the identity, never a resubmission.
            if row["outcome"] == "UNKNOWN":
                row["outcome"] = "pending"
            if provider is not None:
                row["receipt"]["provider"] = {**(row["receipt"].get("provider") or {}), **provider}
            if detail is not None:
                row["receipt"]["detail"] = detail
            _record_run_event(row, provider)
        else:
            _settle(journal, row, outcome=outcome, provider=provider, evidence=[], detail=detail, moment=moment)
            _record_run_event(row, provider)
            if outcome == "PASS" and row["action"]["kind"] == "cancel_validation":
                target = _find_row(journal, row["action"]["target"])
                if target["outcome"] == "pending":
                    _settle(journal, target, outcome="CANCELED", provider=None, evidence=[],
                            detail="superseded by candidate " + str(row.get("candidate_id"))[:12], moment=moment)
                    _count(journal["counters"], "cancellations")
        _write_journal(record_path, journal)
        return dict(row)


def execute(project: Path, objective: str, *, owner: str, action: dict, exception: object = None,
            port: GitHubPort | None = None, pull_request: dict | None = None,
            now: datetime | None = None, native_projection: dict | None = None) -> dict:
    """Admit and perform one action as a single managed step.

    The decision and the execution are one path: the journal records the admitted
    intention before the remote call, the call targets the bound candidate commit rather
    than whatever HEAD points to, and a lost response leaves an UNKNOWN row that only a
    provider readback can settle. The lock is never held across the remote call.
    """
    from .governor import EXECUTABLE_KINDS, _admit, _find_row, _read_journal, _record_path, validate_action
    moment = now or datetime.now(timezone.utc)
    if validate_action(action)["kind"] not in EXECUTABLE_KINDS:
        raise PodError("invalid_action", "Merge, release and deployment are decided here and performed by project governance")
    admitted = _admit(project, objective, owner=owner, action=action, exception=exception, now=moment,
                      managed=True, native_projection=native_projection)
    result = {key: value for key, value in admitted.items() if not key.startswith("_")}
    result["cancellations"] = []
    if admitted["decision"] != "ALLOW":
        return result
    unit = admitted["_unit"]
    binding = admitted["_binding"]
    remote = port or GhPort(project)
    policy = admitted["_policy"]
    later = (lambda: datetime.now(timezone.utc)) if now is None else (lambda: now)
    kind = admitted["_kind"]
    # A cancellation never cancels on its own behalf; supersedence is handled once, by the
    # action that follows it.
    if (kind != "cancel_validation" and policy.get("cancel_superseded_validation")
            and admitted["superseded_validation"]):
        journal = _read_journal(_record_path(project, objective))
        for stale in admitted["superseded_validation"]:
            stale_row = _find_row(journal, stale)
            if not _bound_run_id(stale_row):
                # Nothing to cancel remotely; the row settles by readback or stays superseded.
                result["cancellations"].append({"target": stale, "record_id": None, "decision": "DEFER",
                                                "outcome": None, "detail": "no run identity to cancel"})
                continue
            try:
                canceled = execute(
                    project, objective, owner=owner, port=remote, now=moment,
                    native_projection=native_projection,
                    action={"kind": "cancel_validation", "unit": unit["name"], "candidate": binding["id"],
                            "target": stale, "reason": "superseded by a newer candidate generation",
                            # The same publish consent for this candidate covers cancelling what it supersedes.
                            **({"authorization": action["authorization"]} if "authorization" in action else {})})
                result["cancellations"].append({**canceled, "target": stale})
            except PodError as exc:
                # The cancel's own row is settled by its execute; the primary action proceeds.
                result["cancellations"].append({"target": stale, "record_id": None, "decision": "ALLOW",
                                                "outcome": "FAILED", "detail": exc.code})
            except BaseException:
                _record_execution(project, objective, owner=owner, record_id=admitted["record_id"],
                                  outcome="FAILED", provider=None, detail="not_attempted", now=later())
                raise
    journal = _read_journal(_record_path(project, objective))
    row = _find_row(journal, admitted["record_id"])
    try:
        outcome, provider, detail = _perform(remote, row, unit, journal, pull_request)
    except PodError as exc:
        _record_execution(project, objective, owner=owner, record_id=admitted["record_id"],
                          outcome="UNKNOWN" if _uncertain(exc) else "FAILED", provider=None,
                          detail=exc.code, now=later())
        raise
    except BaseException as exc:
        # A port that answered in an unexpected shape, or a defect here, leaves the remote
        # state unknown; the row must say so rather than stay pending forever.
        _record_execution(project, objective, owner=owner, record_id=admitted["record_id"],
                          outcome="UNKNOWN", provider=None, detail=type(exc).__name__, now=later())
        raise
    settled = _record_execution(project, objective, owner=owner, record_id=admitted["record_id"],
                                outcome=outcome, provider=provider, detail=detail, now=later())
    result.update({"outcome": settled["outcome"], "receipt": settled["receipt"]})
    return result


def reconcile(project: Path, objective: str, *, owner: str, record_id: str, port: GitHubPort | None = None,
              now: datetime | None = None) -> dict:
    """Settle an UNKNOWN or pending row from provider readback only; never resubmit."""
    from .governor import (DISPATCH_KINDS, PUBLICATION_KINDS, _owned_state, _find_row, _read_journal, _record_path,
                           pushed_branch)
    bounded_text(owner, name="owner")
    bounded_text(record_id, name="record_id", limit=128)
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    with _lock(context_path):
        # The authority join may rebind a runtime change; that write needs the objective lock (A2).
        _owned_state(project, objective, owner, context_path)
    journal = _read_journal(record_path)
    row = _find_row(journal, record_id)
    if row["outcome"] not in ("UNKNOWN", "pending"):
        return {"status": row["outcome"], "record_id": record_id, "action": "none", "receipt": row["receipt"]}
    unit = journal["units"].get(row["action"]["unit"])
    if unit is None:
        raise PodError("unit_unbound", "No delivery unit binds this row")
    # The row carries the commit it was admitted for. A superseded generation is looked up
    # by that commit, never by whatever the unit's current candidate happens to be.
    commit = row.get("commit")
    remote = port or GhPort(project)
    kind = row["action"]["kind"]
    provider = row["receipt"].get("provider") or {}
    moment = now or datetime.now(timezone.utc)
    if kind in PUBLICATION_KINDS:
        branch = pushed_branch(row, unit)
        head = remote.branch_head(remote=branch["remote"], branch=branch["branch"]) if branch else None
        if branch is None:
            outcome, detail = "UNKNOWN", "the unit no longer names the branch this row pushed"
        elif commit is not None and head == commit:
            outcome, detail = "PASS", "remote branch carries the candidate commit"
            provider = {**provider, "remote_head": head, "branch": branch["remote"] + "/" + branch["branch"],
                        "triggered": _triggered_runs(remote, row, commit)}
        elif head is not None and head == unit["published"].get(branch["remote"] + "/" + branch["branch"]):
            outcome, detail = "FAILED", "not_pushed: the remote branch is still at the previously recorded commit"
        else:
            outcome, detail = "UNKNOWN", "remote branch head is " + (head[:12] if head else "absent")
        settled = _record_execution(project, objective, owner=owner, record_id=record_id, outcome=outcome,
                                    provider=provider, detail=detail, now=moment) if outcome != "UNKNOWN" else row
        return {"status": settled["outcome"], "record_id": record_id, "action": "readback", "receipt": settled["receipt"]}
    if kind in DISPATCH_KINDS or kind == "validation_rerun":
        rejected = row.get("rejected_run")
        run_id = provider.get("run_id") if rejected is None else None
        lookup = provider.get("ref_commit") or commit
        event = DISPATCH_EVENT if _requested_dispatch(row) else None
        run = None
        if run_id:
            run = remote.run(run_id=str(run_id))
            if event is not None and run.get("event") != event:
                # A run bound before its event was read (a 0.6.6 row) that turns out to be another
                # event's is not this dispatch's run: it is neither bound nor counted, and the row
                # stops attaching identical requests. The row is then read back like one with no run.
                rejected = _reject_run(project, objective, owner=owner, record_id=record_id,
                                       run_id=str(run_id), event=run.get("event"))["rejected_run"]
                run = None
        if run is None and not (run_id and rejected is None):
            if lookup is None:
                return {"status": row["outcome"], "record_id": record_id, "action": "hold",
                        "reason": "the row carries neither a run identity nor a commit to look up",
                        "receipt": row["receipt"]}
            run = _pick_run(remote.runs(workflow=row["action"]["target"], commit=lookup),
                            since=row["receipt"]["started_at"], event=event)
        if run is None and rejected is not None:
            current = _find_row(_read_journal(record_path), record_id)
            return {"status": current["outcome"], "record_id": record_id, "action": "hold",
                    "reason": _rejected_reason(rejected), "receipt": current["receipt"]}
        if run is None:
            return {"status": "UNKNOWN", "record_id": record_id, "action": "hold",
                    "reason": "no run for this workflow and commit is visible since the submission",
                    "receipt": row["receipt"]}
        outcome, detail = _outcome_from_run(run)
        settled = _record_execution(project, objective, owner=owner, record_id=record_id, outcome=outcome,
                                    provider={**({"ref_commit": lookup} if lookup else {}), **_provider_run(run)},
                                    detail=detail, now=moment)
        return {"status": settled["outcome"], "record_id": record_id, "action": "readback", "receipt": settled["receipt"]}
    if kind == "cancel_validation":
        run_id = provider.get("run_id")
        if not run_id:
            return {"status": "UNKNOWN", "record_id": record_id, "action": "hold", "receipt": row["receipt"]}
        run = remote.run(run_id=str(run_id))
        status = run.get("status")
        if status == "completed":
            outcome = "PASS" if run.get("conclusion") == "cancelled" else "FAILED"
            settled = _record_execution(project, objective, owner=owner, record_id=record_id, outcome=outcome,
                                        provider=_provider_run(run), detail=None, now=moment)
            return {"status": settled["outcome"], "record_id": record_id, "action": "readback", "receipt": settled["receipt"]}
        return {"status": "UNKNOWN", "record_id": record_id, "action": "hold", "receipt": row["receipt"]}
    raise PodError("invalid_action", f"{kind} has no provider readback")
