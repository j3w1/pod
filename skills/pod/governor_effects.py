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


def _port(project: Path) -> GitHubPort:
    """The production port: the installed gh and git. Request JSON can never supply one."""
    return GhPort(project)


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
            "conclusion": run.get("conclusion"), "created_at": run.get("created_at"), "event": run.get("event"),
            "attempt": run.get("attempt")}


DISPATCH_EVENT = "workflow_dispatch"
# A branch publication starts `push` runs and, on a branch with an open pull request, pull-request runs.
# Pod cannot tell whether a pull request was open, so a run of either publication kind may be any of these.
PUBLICATION_EVENTS = ("push", "pull_request", "pull_request_target")
RETENTION = "GitHub lists only the runs it still retains; a deleted or expired run is not listed"


def _requested_dispatch(row: dict) -> bool:
    """A workflow_dispatch row the coordinator asked for, as opposed to one a publication started."""
    return row["action"]["kind"] == "workflow_dispatch" and row.get("derived_from") is None


def _row_events(row: dict) -> tuple[str, ...] | None:
    """The provider events a workflow row's own effect starts runs for; a rerun keeps its run's event."""
    if row["action"]["kind"] == "workflow_dispatch" and row.get("derived_from") is not None:
        return PUBLICATION_EVENTS
    if row["action"]["kind"] in ("workflow_dispatch", "remote_diagnostic"):
        return (DISPATCH_EVENT,)
    return None


def _stamp(value: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
    return stamp if stamp is not None and stamp.tzinfo is not None else None


def _pick_run(runs: list[dict], *, since: str, events: tuple[str, ...] | None = None,
              commit: str | None = None) -> dict | None:
    """The newest run created at or after the submission, of one of the events and on the exact commit.

    A run that does not report its creation time, or with an event or commit asked for, does not report
    that event or commit, cannot be shown to match and is not a candidate.
    """
    floor = _stamp(since)
    if floor is not None:
        # GitHub reports run creation to the second; the floor must not be finer.
        floor = floor.replace(microsecond=0)
    candidates = []
    for run in runs:
        if events is not None and run.get("event") not in events:
            continue
        if commit is not None and run.get("head_sha") != commit:
            continue
        stamp = _stamp(run.get("created_at"))
        if stamp is None or floor is None or stamp < floor:
            continue
        candidates.append((stamp, run))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _history(port: GitHubPort, *, workflow: str, commit: str) -> tuple[list[dict], dict]:
    """One bounded run list and the statement of what it covers: scope, limit, truncation, retention."""
    from .github import RUN_LIST_LIMIT
    runs = port.runs(workflow=workflow, commit=commit)
    read = {"scope": f"runs of {workflow} on commit {commit}, filtered by GitHub", "limit": RUN_LIST_LIMIT,
            "listed": min(len(runs), RUN_LIST_LIMIT), "truncated": len(runs) > RUN_LIST_LIMIT,
            "retention": RETENTION}
    return runs[:RUN_LIST_LIMIT], read


def _unlisted(read: dict, *, events: tuple[str, ...] | None, since: str, outcome: str) -> str:
    """Why a lookup bound nothing: missing or partial history is never a failure or a proof of no effect."""
    wanted = " or ".join(events) if events else "any event"
    reason = (f"no {wanted} run created at or after {since} is among the listed {read['scope']} "
              f"({read['listed']} listed, limit {read['limit']})")
    if read["truncated"]:
        reason += "; the list was truncated, so an older matching run may be hidden"
    return f"{reason}; {RETENTION}; the row stays {outcome}"


def _original_attempt(port: GitHubPort, run: dict) -> dict | None:
    """A dispatch or publication binds the attempt its own event started, which is always the first."""
    if run.get("attempt") == 1:
        return run
    first = port.run(run_id=str(run.get("id")), attempt=1)
    return first if first.get("attempt") == 1 and str(first.get("id")) == str(run.get("id")) else None


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
        unread = []
        provider["triggered"] = _triggered_runs(port, row, commit, unread=unread)
        observation_detail = "push landed; triggered runs unreadable for: " + ", ".join(unread) if unread else None
        if kind == "push":
            return "PASS", provider, observation_detail
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
            return "UNKNOWN", provider, "pull request lookup or submission unreadable after the push: " + exc.code
        provider.update({"pr": found.get("number"), "pr_url": found.get("url")})
        return "PASS", provider, observation_detail
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
        # A requested dispatch binds only a workflow_dispatch run of its exact commit, never a push or
        # pull-request run that happens to sit on the same commit, and only the attempt it started.
        try:
            runs, _ = _history(port, workflow=workflow, commit=commit)
            run = _pick_run(runs, since=row["receipt"]["started_at"], events=_row_events(row), commit=commit)
            run = _original_attempt(port, run) if run is not None else None
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


def _triggered_runs(port: GitHubPort, row: dict, commit: str, *, unread: list | None = None) -> dict:
    """The runs a landed publication started, by workflow, as far as a readback can see them.

    Only a publication event's run on the published commit, created at or after the publication and read
    at its first attempt, is one of them.
    """
    triggered = {}
    for effect in row.get("effects") or []:
        if not effect.startswith("workflow:"):
            continue
        workflow = effect[len("workflow:"):]
        try:
            runs, _ = _history(port, workflow=workflow, commit=commit)
            run = _pick_run(runs, since=row["receipt"]["started_at"], events=PUBLICATION_EVENTS, commit=commit)
            run = _original_attempt(port, run) if run is not None else None
        except PodError as exc:
            if not _uncertain(exc) and unread is not None:
                unread.append(workflow)
            continue
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


def _reject_run(project: Path, objective: str, *, owner: str, record_id: str, rejected: dict) -> dict:
    """Record that a workflow row's bound run is not its own; the row neither binds nor counts it."""
    from .governor import _owned_state, _find_row, _read_journal, _record_path, _write_journal
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    with _lock(context_path):
        _owned_state(project, objective, owner, context_path)
        journal = _read_journal(record_path)
        row = _find_row(journal, record_id)
        row["rejected_run"] = dict(rejected)
        _write_journal(record_path, journal)
        return dict(row)


def _rejected_reason(rejected: dict, events: tuple[str, ...] | None) -> str:
    if "head_sha" in rejected:
        return (f"run {rejected['run_id']} ran on commit {str(rejected['head_sha'])[:12]}, not this row's commit; "
                "it is not this row's run")
    return (f"run {rejected['run_id']} was triggered by {rejected.get('event') or 'an unknown event'}, "
            f"not {' or '.join(events or (DISPATCH_EVENT,))}; it is not this row's run")


def _note_readback(project: Path, objective: str, *, owner: str, record_id: str, reason: str,
                   moment: datetime) -> dict:
    """Keep why a readback bound nothing on the row; its outcome stays exactly as it was."""
    from .governor import _owned_state, _find_row, _read_journal, _record_path, _write_journal
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    with _lock(context_path):
        _owned_state(project, objective, owner, context_path)
        journal = _read_journal(record_path)
        row = _find_row(journal, record_id)
        row["readback"] = {"at": moment.isoformat(), "reason": reason[:1024]}
        _write_journal(record_path, journal)
        return dict(row)


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
                      managed=True, native_projection=native_projection, pull_request=pull_request)
    result = {key: value for key, value in admitted.items() if not key.startswith("_")}
    result["cancellations"] = []
    if admitted["decision"] != "ALLOW":
        return result
    unit = admitted["_unit"]
    binding = admitted["_binding"]
    remote = port or _port(project)
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


def _rerun_floor(row: dict, journal: dict, run_id: str) -> int:
    """The newest attempt any other row of this run bound; a rerun binds only an attempt after it."""
    attempts = [(other["receipt"].get("provider") or {}).get("attempt") or 1 for other in journal["actions"]
                if other["record_id"] != row["record_id"]
                and str((other["receipt"].get("provider") or {}).get("run_id")) == run_id]
    return max([int(value) for value in attempts if type(value) is int] or [1])


def _rerun_source(row: dict, journal: dict) -> str | None:
    """The failed run a rerun reruns: its own record, or for a reservation the one execution would pick."""
    run_id = (row["receipt"].get("provider") or {}).get("run_id")
    if run_id:
        return str(run_id)
    failed = [other for other in journal["actions"]
              if other["action"]["unit"] == row["action"]["unit"] and other["decision"] == "ALLOW"
              and other["outcome"] == "FAILED" and other["action"]["target"] == row["action"]["target"]
              and other.get("candidate_id") == row.get("candidate_id") and _bound_run_id(other)]
    return str(_bound_run_id(failed[-1])) if failed else None


def _observe(port: GitHubPort, row: dict, unit: dict, journal: dict) -> dict:
    """Read the provider for one unsettled row and say what it shows, writing nothing.

    The answer carries an outcome, or a hold with its reason; `seen` says whether the provider shows the
    row's effect at all, and `reject` names a bound run that turned out not to be the row's own.
    """
    from .governor import DISPATCH_KINDS, PUBLICATION_KINDS, pushed_branch
    commit = row.get("commit")
    kind = row["action"]["kind"]
    provider = row["receipt"].get("provider") or {}
    since = row["receipt"]["started_at"]
    answer = {"outcome": None, "provider": None, "detail": None, "hold": None, "seen": False,
              "reject": None, "provider_read": None}
    if kind in PUBLICATION_KINDS:
        # The row carries the commit it was admitted for. A superseded generation is looked up
        # by that commit, never by whatever the unit's current candidate happens to be.
        branch = pushed_branch(row, unit)
        head = port.branch_head(remote=branch["remote"], branch=branch["branch"]) if branch else None
        if branch is None:
            answer["hold"] = "the unit no longer names the branch this row pushed"
        elif commit is not None and head == commit:
            answer.update(outcome="PASS", detail="remote branch carries the candidate commit", seen=True,
                          provider={**provider, "remote_head": head, "branch": branch["remote"] + "/" + branch["branch"],
                                    "triggered": _triggered_runs(port, row, commit)})
        elif head is not None and head == unit["published"].get(branch["remote"] + "/" + branch["branch"]):
            answer.update(outcome="FAILED", detail="not_pushed: the remote branch is still at the previously recorded commit")
        else:
            answer["hold"] = "remote branch head is " + (head[:12] if head else "absent")
        return answer
    if kind in DISPATCH_KINDS or kind == "validation_rerun":
        events = _row_events(row)
        rejected = row.get("rejected_run")
        lookup = provider.get("ref_commit") or commit
        run_id = provider.get("run_id") if rejected is None else None
        run = None
        if kind == "validation_rerun":
            # A rerun is the failed run's next attempt, whatever event started that run.
            run_id = _rerun_source(row, journal)
            if run_id is None:
                answer["hold"] = "no failed run with a provider identity binds this rerun"
                return answer
            bound = provider.get("attempt")
            floor = _rerun_floor(row, journal, run_id)
            run = port.run(run_id=run_id, attempt=bound) if bound else port.run(run_id=run_id)
            attempt = run.get("attempt")
            if attempt != bound if bound else type(attempt) is not int or attempt <= floor:
                answer["hold"] = (f"run {run_id} shows attempt {attempt}; this rerun binds "
                                  + (f"attempt {bound}" if bound else f"an attempt after {floor}"))
                return answer
        elif run_id:
            run = port.run(run_id=str(run_id), attempt=1)
            event = run.get("event") if isinstance(run.get("event"), str) else None
            if events is not None and event not in events:
                answer["reject"] = {"run_id": str(run_id), "event": event}
            elif lookup is not None and run.get("head_sha") != lookup:
                head = run.get("head_sha")
                answer["reject"] = {"run_id": str(run_id), "event": event,
                                    "head_sha": head if isinstance(head, str) else None}
            elif run.get("attempt") != 1:
                answer["hold"] = f"run {run_id} did not answer for its first attempt"
                return answer
            if answer["reject"] is not None:
                rejected, run = answer["reject"], None
        if run is None:
            if lookup is None:
                answer["hold"] = "the row carries neither a run identity nor a commit to look up"
                return answer
            runs, read = _history(port, workflow=row["action"]["target"], commit=lookup)
            answer["provider_read"] = read
            run = _pick_run(runs, since=since, events=events, commit=lookup)
            if run is not None:
                run = _original_attempt(port, run)
                if run is None:
                    answer["hold"] = "the matching run did not answer for its first attempt"
                    return answer
            elif rejected is not None:
                answer["hold"] = _rejected_reason(rejected, events)
                return answer
            else:
                answer["hold"] = _unlisted(read, events=events, since=since, outcome=row["outcome"])
                return answer
        outcome, detail = _outcome_from_run(run)
        answer.update(outcome=outcome, detail=detail, seen=True,
                      provider={**({"ref_commit": lookup} if lookup else {}), **_provider_run(run)})
        return answer
    if kind == "cancel_validation":
        run_id = provider.get("run_id")
        if not run_id:
            # A reserved cancellation names the run it would cancel through its target row.
            target = next((other for other in journal["actions"] if other["record_id"] == row["action"]["target"]),
                          None)
            run_id = _bound_run_id(target) if target is not None else None
        if not run_id:
            answer["hold"] = "no run identity binds this cancellation"
            return answer
        run = port.run(run_id=str(run_id))
        if run.get("status") == "completed":
            answer.update(outcome="PASS" if run.get("conclusion") == "cancelled" else "FAILED",
                          provider=_provider_run(run), seen=run.get("conclusion") == "cancelled")
        else:
            answer["hold"] = f"run {run_id} has not completed"
        return answer
    raise PodError("invalid_action", f"{kind} has no provider readback")


def reconcile(project: Path, objective: str, *, owner: str, record_id: str, port: GitHubPort | None = None,
              now: datetime | None = None) -> dict:
    """Settle an UNKNOWN or pending row from provider readback only; never resubmit."""
    from .governor import _owned_state, _find_row, _read_journal, _record_path
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
    moment = now or datetime.now(timezone.utc)
    answer = _observe(port or _port(project), row, unit, journal)
    if answer["reject"] is not None:
        _reject_run(project, objective, owner=owner, record_id=record_id, rejected=answer["reject"])
    result = {"record_id": record_id}
    if answer["provider_read"] is not None:
        result["provider_read"] = answer["provider_read"]
    if answer["hold"] is not None:
        held = _note_readback(project, objective, owner=owner, record_id=record_id, reason=answer["hold"],
                              moment=moment)
        return {**result, "status": held["outcome"], "action": "hold", "reason": answer["hold"],
                "receipt": held["receipt"]}
    settled = _record_execution(project, objective, owner=owner, record_id=record_id, outcome=answer["outcome"],
                                provider=answer["provider"], detail=answer["detail"], now=moment)
    return {**result, "status": settled["outcome"], "action": "readback", "receipt": settled["receipt"]}
