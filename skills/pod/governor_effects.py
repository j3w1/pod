"""Execution and provider readback for admitted Governor effects."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
            "attempt": run.get("attempt"), "started_at": run.get("started_at")}


DISPATCH_EVENT = "workflow_dispatch"
# A branch publication starts `push` runs and, on a branch with an open pull request, pull-request runs.
# Pod cannot tell whether a pull request was open, so a run of either publication kind may be any of these.
PUBLICATION_EVENTS = ("push", "pull_request", "pull_request_target")
RETENTION = "GitHub lists only the runs it still retains; a deleted or expired run is not listed"
# An effect is read as absent only after its call's time limit and this margin for clock skew and provider
# propagation, since a call still in flight may land (R115).
ABSENCE_MARGIN_S = 600


def absent_after(started: datetime) -> datetime:
    """When an effect submitted at `started` that has not appeared may first be read as absent."""
    from .github import MUTATION_TIMEOUT_S
    return started + timedelta(seconds=MUTATION_TIMEOUT_S + ABSENCE_MARGIN_S)


def absence_floor(row: dict) -> datetime | None:
    """When a carried row's absent effect may first be read as FAILED (R118): a lost coordinator's call may
    still be in flight from the row's start. None when that start is unknown."""
    started = _stamp(row["receipt"].get("started_at"))
    return absent_after(started) if started else None


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


def _created_since(run: dict, since: str) -> bool:
    """Whether the run reports a creation at or after the submission; GitHub reports it to the second."""
    floor = _stamp(since)
    stamp = _stamp(run.get("created_at"))
    return floor is not None and stamp is not None and stamp >= floor.replace(microsecond=0)


def _pick_run(runs: list[dict], *, since: str, events: tuple[str, ...] | None = None,
              commit: str | None = None) -> dict | None:
    """The newest run created at or after the submission, of one of the events and on the exact commit.

    A run that does not report its creation time, or with an event or commit asked for, does not report
    that event or commit, cannot be shown to match and is not a candidate.
    """
    candidates = []
    for run in runs:
        if events is not None and run.get("event") not in events:
            continue
        if commit is not None and run.get("head_sha") != commit:
            continue
        if not _created_since(run, since):
            continue
        candidates.append((_stamp(run.get("created_at")), run))
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
    if read["truncated"]:
        # A truncated list may hide the row's own run, so even a visible match is not bound from it.
        reason = (f"the listed {read['scope']} were truncated at {read['limit']}, so the {wanted} run created at or "
                  f"after {since} that is this row's own cannot be told from the runs it hides")
    else:
        reason = (f"no {wanted} run created at or after {since} is among the listed {read['scope']} "
                  f"({read['listed']} listed, limit {read['limit']})")
    return f"{reason}; {RETENTION}; the row stays {outcome}"


def _original_attempt(port: GitHubPort, run: dict) -> dict | None:
    """A dispatch or publication binds the attempt its own event started, which is always the first."""
    if run.get("attempt") == 1:
        return run
    first = port.run(run_id=str(run.get("id")), attempt=1)
    return first if first.get("attempt") == 1 and str(first.get("id")) == str(run.get("id")) else None


def _perform(port: GitHubPort, row: dict, unit: dict, journal: dict, pull_request: dict | None, *,
             intent=None, clock=None) -> tuple[str, dict | None, str | None]:
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
            runs, read = _history(port, workflow=workflow, commit=commit)
            if read["truncated"]:
                return "pending", lookup, "dispatched; the run list was truncated, so its run is not bound yet"
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
        # The rerun's provenance, recorded before it is submitted and kept on every path: the run, its latest
        # attempt read just before, and the submission start. Readback attributes an attempt to this row only
        # from those facts (R115).
        try:
            latest = port.run(run_id=run_id).get("attempt")
        except PodError as exc:
            raise PodError("rerun_attempt_unread", "The run's latest attempt could not be read, so the rerun's own "
                           "attempt could never be told; nothing was rerun") from exc
        if type(latest) is not int or latest < 1:
            raise PodError("rerun_attempt_unread", "The run reports no attempt, so the rerun's own attempt could "
                           "never be told; nothing was rerun")
        own = {"run_id": run_id, "rerun": True, "before_attempt": latest,
               "submitted_at": (clock() if clock else datetime.now(timezone.utc)).isoformat()}
        if intent is not None:
            intent(own)
        try:
            port.rerun(run_id=run_id, failed_only=True)
        except PodError as exc:
            if _uncertain(exc):
                return "UNKNOWN", own, "rerun response was lost"
            raise
        # GitHub accepted the rerun: only now can a single new attempt be proved this row's own.
        own["accepted"] = True
        if intent is not None:
            intent(own)
        try:
            run = _own_rerun_attempt(port, own)["run"]
        except PodError:
            run = None
        if run is None:
            return "pending", own, "rerun submitted; its own attempt is read back"
        return "pending", {**own, **_provider_run(run)}, None
    if kind == "cancel_validation":
        target = _find_row(journal, row["action"]["target"])
        run_id = _bound_run_id(target)
        older_same_unit = (target["action"]["unit"] == row["action"]["unit"]
                           and target.get("candidate_id") != row.get("candidate_id"))
        if (target["outcome"] != "pending" or not target.get("cancel_safe") or not run_id or not older_same_unit
                or isinstance(target.get("carried_from"), dict)):
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
            runs, read = _history(port, workflow=workflow, commit=commit)
            # A truncated list may hide the publication's own run; the derived row then binds by readback.
            run = None if read["truncated"] else _pick_run(runs, since=row["receipt"]["started_at"],
                                                           events=PUBLICATION_EVENTS, commit=commit)
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
    if rejected.get("reason"):
        return f"run {rejected['run_id']} {rejected['reason']}; it is not this row's run"
    if "head_sha" in rejected:
        return (f"run {rejected['run_id']} ran on commit {str(rejected['head_sha'])[:12]}, not this row's commit; "
                "it is not this row's run")
    return (f"run {rejected['run_id']} was triggered by {rejected.get('event') or 'an unknown event'}, "
            f"not {' or '.join(events or (DISPATCH_EVENT,))}; it is not this row's run")


def _record_intent(project: Path, objective: str, *, owner: str, record_id: str, provider: dict) -> dict:
    """Record what an effect is about to do, before it is submitted; the row's outcome stays as it is."""
    from .governor import _owned_state, _find_row, _read_journal, _record_path, _write_journal
    context_path = _path(project, objective)
    record_path = _record_path(project, objective)
    with _lock(context_path):
        _owned_state(project, objective, owner, context_path)
        journal = _read_journal(record_path)
        row = _find_row(journal, record_id)
        row["receipt"]["provider"] = {**(row["receipt"].get("provider") or {}), **provider}
        row["receipt"]["detail"] = ("submitted and accepted; its own attempt is read back" if provider.get("accepted")
                                    else "submitting; its result is read back")
        _write_journal(record_path, journal)
        return dict(row)


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
                # The cancellation's own observed receipt is settled above. Its target settles CANCELED here only
                # when this journal holds it, pending and not carried: a carried target settles by its own readback,
                # and a carried cancellation's settled target was never copied (R118).
                target = next((other for other in journal["actions"]
                               if other["record_id"] == row["action"]["target"]), None)
                if (target is not None and target["outcome"] == "pending"
                        and not isinstance(target.get("carried_from"), dict)):
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
        _revalidate_relay(project, objective, owner=owner, row=row)
        _revalidate_standing(project, objective, owner=owner, row=row, now=later())
        outcome, provider, detail = _perform(
            remote, row, unit, journal, pull_request, clock=later,
            intent=lambda provider: _record_intent(project, objective, owner=owner, record_id=admitted["record_id"],
                                                   provider=provider))
    except PodError as exc:
        try:
            _record_execution(project, objective, owner=owner, record_id=admitted["record_id"],
                              outcome="UNKNOWN" if _uncertain(exc) else "FAILED", provider=None,
                              detail=exc.code, now=later())
        except PodError as settlement:
            if "standing" in (row["action"].get("authorization") or {}) and settlement.code in (
                    "objective_continued", "objective_retired"):
                # Loss of predecessor authority forbids even an outcome write. Its untouched pending
                # intention belongs to the existing successor/readback path, never a remote retry.
                raise exc from None
            raise
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


def _revalidate_standing(project: Path, objective: str, *, owner: str, row: dict,
                         now: datetime | None = None) -> None:
    """A recorded intention grants no effect after its standing decision or binding changes."""
    consent = row["action"].get("authorization") or {}
    if "standing" not in consent:
        return
    from .governor import _owned_state, _read_journal, _record_path, merge_target_name
    from .ledger import map_of, require_governance_current
    from .standing import state as standing_state
    with _lock(_path(project, objective)):
        context = _read(_path(project, objective))
        decisions = context.get("standing_authorization", {}).get("decisions", [])
        current = _read_journal(_record_path(project, objective))["units"].get(row["action"]["unit"])
        binding = current.get("candidate") if current else None
        if (standing_state(context, now=now) != "active" or not decisions
                or decisions[-1]["id"] != consent["standing"]["id"]
                or not set(consent["scope"]) <= set(decisions[-1]["scope"])
                or binding is None or binding["id"] != row.get("candidate_id")
                or binding["commit"] != consent["candidate"] or binding["tree"] != consent["tree"]
                or binding.get("dirty_paths") or merge_target_name(current) != consent.get("target")):
            raise PodError("standing_authorization_inactive", "Standing authorization or its prepared candidate is no longer active",
                           {"next_action": "obtain current owner consent or reconfirm yolo for this objective"})
        _owned_state(project, objective, owner, _path(project, objective), version=True)
        require_governance_current(project, map_of(context))


def _revalidate_relay(project: Path, objective: str, *, owner: str, row: dict) -> None:
    """Recheck confirmed consent and the current prepared candidate immediately before an effect."""
    consent = row["action"].get("authorization") or {}
    if consent.get("channel") != "relayed":
        return
    from .config import effective
    from .governor import _owned_state, _read_journal, _record_path, _verify_relay
    from .ledger import map_of, require_governance_current
    from .relay import check_use, refusal
    with _lock(_path(project, objective)):
        state = _owned_state(project, objective, owner, _path(project, objective), version=True)
        require_governance_current(project, map_of(state))
        current = _read_journal(_record_path(project, objective))["units"].get(row["action"]["unit"])
        binding = current.get("candidate") if current else None
        if (binding is None or binding["id"] != row.get("candidate_id")
                or binding["commit"] != consent["candidate"] or binding["tree"] != consent["tree"]
                or binding.get("dirty_paths")):
            raise refusal("relay_verifier_mismatch", fields=["candidate"])
        confirmed = _verify_relay(project, objective, consent, binding, current,
                                  effective(project)["preferences"].get("authorization_verifier"), pr=row["action"].get("pr"))
        if confirmed["relay_verification"] != consent.get("relay_verification"):
            raise refusal("relay_verifier_mismatch", fields=["relay_verification"])
        check_use(project, confirmed["relay_verification"], consume=True)


def rerun_unattributable(provenance: dict) -> str | None:
    """Why a rerun's record alone means no readback can ever attribute its attempt (R115), or None.

    The record-only half of `_own_rerun_attempt`'s rule, which a successor's claim also applies (R118): a rerun
    without the provenance recorded before submission, or with an attempt bound without its recorded acceptance
    or other than the single one after the pre-read, holds for good, since attempts on a run only accumulate.
    """
    before = provenance.get("before_attempt")
    bound = provenance.get("attempt")
    if type(before) is not int or _stamp(provenance.get("submitted_at")) is None:
        what = f"attempt {bound} was bound to this rerun" if type(bound) is int else "this rerun was recorded"
        return (f"{what} without the attempt read and start recorded before submission, so its own attempt cannot "
                "be told from another actor's")
    if type(bound) is int and (bound != before + 1 or provenance.get("accepted") is not True):
        return (f"attempt {bound} was bound to this rerun without its recorded acceptance and the single attempt "
                f"after attempt {before}, so it cannot be proved its own")
    return None


def _own_rerun_attempt(port: GitHubPort, provenance: dict, now: datetime | None = None) -> dict:
    """The one rule that attributes a rerun's attempt (R115), on every path that reads one: execution's
    immediate read, readback (reconcile, a reservation's outcome, the merge PASS sweep) and a carried row's.

    `run` is the attempt once proved this rerun's own; otherwise `reason` says why the row holds, and `absent`
    says an unconfirmed rerun is proved never to have taken effect. The proof is the count: with the provenance
    recorded before submission (the run, the attempt read then, and the submission start at full precision) and
    the acceptance recorded right after GitHub accepted the call, Pod's call created an attempt, so exactly one
    attempt after the pre-read is Pod's. The attempt's start only guards against a contradiction, at the
    provider's precision: GitHub sets a rerun's start at the request in whole seconds, so a start in the second
    holding the recorded submission start, or later, agrees, and a start in an earlier second contradicts the
    record and holds. Pod's own rows are never taken as a run's history, so an attempt bound without this
    provenance holds. A proved attempt is bound by number and read again by number, so a later actor's attempt
    never displaces it.
    """
    def held(reason: str) -> dict:
        return {"run": None, "absent": False, "reason": reason}
    unattributable = rerun_unattributable(provenance)
    if unattributable is not None:
        return held(unattributable)
    run_id = provenance["run_id"]
    before = provenance["before_attempt"]
    submitted = _stamp(provenance["submitted_at"])
    bound = provenance.get("attempt")
    accepted = provenance.get("accepted") is True
    own = before + 1
    if type(bound) is int:
        view = port.run(run_id=run_id, attempt=own)
        if view.get("attempt") != own or str(view.get("id")) != run_id:
            return held(f"run {run_id} did not answer for attempt {own}, the one this rerun started")
    else:
        view = port.run(run_id=run_id)
        attempt = view.get("attempt")
        if str(view.get("id")) != run_id or type(attempt) is not int or attempt < before:
            return held(f"run {run_id} did not answer with an attempt at or after attempt {before}, read before the "
                        "rerun was submitted")
        if attempt == before:
            if accepted:
                return held(f"Pod recorded GitHub's acceptance of this rerun, yet run {run_id} still shows no attempt "
                            f"after attempt {before}; the two contradict, so no outcome is recorded")
            floor = absent_after(submitted)
            if (now or datetime.now(timezone.utc)) >= floor:
                return {"run": None, "absent": True,
                        "reason": f"run {run_id} still shows attempt {before}, read before the rerun was submitted, "
                                  f"after {floor.isoformat()}: the unconfirmed rerun did not take effect"}
            return held(f"run {run_id} shows no attempt after attempt {before}, read before the rerun was submitted")
        if attempt != own:
            return held(f"run {run_id} shows attempt {attempt}: more than one new attempt after attempt {before}, read "
                        "before the rerun was submitted, and any of them may be another actor's, so none binds")
        if not accepted:
            return held(f"run {run_id} shows attempt {own}, but Pod's submission was never confirmed accepted, so that "
                        "attempt may be another actor's while Pod's own never landed; none binds")
    started = _stamp(view.get("started_at"))
    if started is None:
        return held(f"GitHub reports no start for attempt {own} of run {run_id}, so it cannot be ordered after the "
                    "rerun's submission")
    # GitHub reports whole seconds, so Pod's own timestamp is floored to its second to compare at that precision,
    # and never further.
    second = submitted.replace(microsecond=0)
    if started < second:
        return held(f"attempt {own} of run {run_id} started at {started.isoformat()}, in a second before the rerun "
                    f"was submitted at {submitted.isoformat()}: that contradicts the record, whether the attempt is "
                    "another actor's or this host's clock and GitHub's disagree, so none binds")
    return {"run": view, "absent": False, "reason": None}


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


def _observe(port: GitHubPort, row: dict, unit: dict, journal: dict, *, project: Path | None = None,
             now: datetime | None = None) -> dict:
    """Read the provider for one unsettled row and say what it shows, writing nothing.

    The answer carries an outcome, or a hold with its reason; `seen` says whether the provider shows the
    row's effect at all, and `reject` names a bound run that turned out not to be the row's own. A carried
    row's absent effect holds until its absence floor (R118).
    """
    answer = _read_back(port, row, unit, journal, project=project, now=now)
    if answer.pop("absence") and isinstance(row.get("carried_from"), dict):
        floor = absence_floor(row)
        if floor is None or (now or datetime.now(timezone.utc)) < floor:
            answer.update(outcome=None, provider=None, hold=f"{answer['detail']}; a carried effect's absence is not "
                          "read as FAILED before " + (floor.isoformat() if floor else "its start is known")
                          + ", as a call already in flight may still land", detail=None)
    return answer


def _carried_pull_request(port: GitHubPort, row: dict, branch: dict, base: str) -> tuple[dict | None, str, bool]:
    """The pull request of a carried row's branch whose head is the row's commit, what the read covered, and
    whether it was truncated: a truncated list settles nothing, as with runs (R115)."""
    from .github import PR_LIST_LIMIT
    found = port.pull_requests(head=branch["branch"], base=base)
    truncated = len(found) > PR_LIST_LIMIT
    mine = [pr for pr in found[:PR_LIST_LIMIT] if pr.get("head_sha") == row.get("commit")]
    scope = (f"pull requests of {branch['branch']} into {base} ({min(len(found), PR_LIST_LIMIT)} listed, "
             f"limit {PR_LIST_LIMIT}" + ("; truncated" if truncated else "") + ")")
    order = {"MERGED": 0, "OPEN": 1, "CLOSED": 2}
    if truncated:
        return None, scope, True
    return (min(mine, key=lambda pr: order.get(pr.get("state"), 3)) if mine else None), scope, False


def _read_back_merge(port: GitHubPort, row: dict, unit: dict, *, project: Path | None, answer: dict) -> dict:
    """A carried merge settles PASS only from a merged pull request whose result passes R93's Git checks."""
    from .governance import carried_merge_context, target_contains_result, verify_merge_result
    from .governor import _fetch_first
    carried = row["carried_from"]
    branch = carried.get("branch") or unit.get("branch")
    if project is None or not isinstance(branch, dict):
        answer["hold"] = "a carried merge is read back from its pull request and Git in its project"
        return answer
    base = row["target_binding"]["base"]
    pr, scope, truncated = _carried_pull_request(port, row, branch, base)
    if pr is None:
        answer["hold"] = (f"the {scope} were truncated, so this merge's pull request cannot be told; the merge stays "
                          "pending" if truncated else
                          f"no pull request with head {row['commit'][:12]} is among the {scope}; the merge stays pending")
        return answer
    unit_view, governance = carried_merge_context(row, unit)
    named = f"pull request #{pr.get('number')}"
    if pr.get("state") == "MERGED" and pr.get("merge_commit"):
        provider = {**(row["receipt"].get("provider") or {}), "merge_commit": pr["merge_commit"]}
        try:
            verified = verify_merge_result(project, {**row, "receipt": {**row["receipt"], "provider": provider}},
                                           unit_view, governance)
        except PodError as exc:
            detail = exc.detail.get("detail") if isinstance(exc.detail, dict) else exc.code
            answer.update(seen=True, hold=f"{named} merged as {pr['merge_commit'][:12]}, but R93's Git checks do not "
                          f"verify it yet ({detail}); {_fetch_first(row)}")
            return answer
        answer.update(outcome="PASS", seen=True, detail=f"{named} merged; R93's Git checks verify the result",
                      provider={**provider, "method": verified["method"], "pr": pr.get("number"),
                                "pr_url": pr.get("url")})
        return answer
    if pr.get("state") == "CLOSED" and target_contains_result(project, row, governance) is False:
        answer.update(outcome="FAILED", absence=True,
                      detail=f"{named} was closed without merging and the target lacks the candidate's result")
        return answer
    answer["hold"] = (f"{named} is {str(pr.get('state')).lower()}; the merge stays pending until it merges"
                      if pr.get("state") != "CLOSED" else
                      f"{named} was closed without merging, but Git cannot show the target lacks the result; "
                      f"{_fetch_first(row)}")
    return answer


def _read_back(port: GitHubPort, row: dict, unit: dict, journal: dict, *, project: Path | None,
               now: datetime | None = None) -> dict:
    from .governor import DISPATCH_KINDS, PUBLICATION_KINDS, pushed_branch
    commit = row.get("commit")
    kind = row["action"]["kind"]
    provider = row["receipt"].get("provider") or {}
    since = row["receipt"]["started_at"]
    answer = {"outcome": None, "provider": None, "detail": None, "hold": None, "seen": False,
              "reject": None, "provider_read": None, "absence": False}
    carried = isinstance(row.get("carried_from"), dict)
    if kind == "merge" and carried:
        return _read_back_merge(port, row, unit, project=project, answer=answer)
    if kind in PUBLICATION_KINDS:
        # The row carries the commit it was admitted for. A superseded generation is looked up
        # by that commit, never by whatever the unit's current candidate happens to be.
        branch = pushed_branch(row, unit)
        if kind == "pr_update" and carried and branch is not None and commit is not None:
            # A carried pull-request update settles PASS only from its pull request, also once it merged and its
            # branch went: the branch head proves a push, not the update (R118).
            pr, scope, truncated = _carried_pull_request(port, row, branch, branch["base"])
            if pr is not None:
                answer.update(outcome="PASS", seen=True, detail=f"pull request #{pr.get('number')} carries the "
                              "candidate commit",
                              provider={**provider, "branch": branch["remote"] + "/" + branch["branch"],
                                        "pr": pr.get("number"), "pr_url": pr.get("url"), "pr_state": pr.get("state"),
                                        "triggered": _triggered_runs(port, row, commit)})
                return answer
            if truncated:
                answer["hold"] = (f"the {scope} were truncated, so this update's pull request cannot be told; the row "
                                  f"stays {row['outcome']}")
                return answer
            head = port.branch_head(remote=branch["remote"], branch=branch["branch"])
            if head != commit and head is not None and head == unit["published"].get(branch["remote"] + "/"
                                                                                       + branch["branch"]):
                answer.update(outcome="FAILED", absence=True,
                              detail="not_pushed: the remote branch is still at the previously recorded commit and no "
                                     "pull request carries the candidate commit")
            else:
                answer["hold"] = (f"no pull request with head {commit[:12]} is among the {scope}"
                                  + ("; the remote branch carries the commit, which proves a push, not the update"
                                     if head == commit else "") + f"; the row stays {row['outcome']}")
            return answer
        head = port.branch_head(remote=branch["remote"], branch=branch["branch"]) if branch else None
        if branch is None:
            answer["hold"] = "the unit no longer names the branch this row pushed"
        elif commit is not None and head == commit:
            answer.update(outcome="PASS", detail="remote branch carries the candidate commit", seen=True,
                          provider={**provider, "remote_head": head, "branch": branch["remote"] + "/" + branch["branch"],
                                    "triggered": _triggered_runs(port, row, commit)})
        elif head is not None and head == unit["published"].get(branch["remote"] + "/" + branch["branch"]):
            answer.update(outcome="FAILED", absence=True,
                          detail="not_pushed: the remote branch is still at the previously recorded commit")
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
            try:
                proof = _own_rerun_attempt(port, {**provider, "run_id": run_id}, now)
            except PodError as exc:
                proof = {"run": None, "absent": False, "reason": f"run {run_id} is not readable ({exc.code})"}
            if proof["absent"]:
                # Recorded as the effect's failure to take place, never as no effect (R116).
                answer.update(outcome="FAILED", detail=proof["reason"])
                return answer
            if proof["run"] is None:
                answer["hold"] = f"{proof['reason']}; the row stays {row['outcome']}"
                return answer
            run = proof["run"]
        elif run_id:
            # A bound run is verified exactly as a newly selected one, whoever bound it (R115).
            run = port.run(run_id=str(run_id), attempt=1)
            event = run.get("event") if isinstance(run.get("event"), str) else None
            if events is not None and event not in events:
                answer["reject"] = {"run_id": str(run_id), "event": event}
            elif lookup is not None and run.get("head_sha") != lookup:
                head = run.get("head_sha")
                answer["reject"] = {"run_id": str(run_id), "event": event,
                                    "head_sha": head if isinstance(head, str) else None}
            elif not _created_since(run, since):
                answer["reject"] = {"run_id": str(run_id), "event": event,
                                    "reason": f"was not created at or after the submission at {since}"}
            elif run.get("attempt") != 1:
                answer["hold"] = f"run {run_id} did not answer for its first attempt"
                return answer
            elif lookup is None:
                answer["hold"] = f"run {run_id}'s workflow cannot be verified: the row names no commit to list"
                return answer
            else:
                # The view names only a display name; exact workflow identity is membership in the list GitHub
                # filters by workflow file and commit.
                runs, read = _history(port, workflow=row["action"]["target"], commit=lookup)
                answer["provider_read"] = read
                if read["truncated"]:
                    # A truncated list settles nothing, even with the bound run visible in it.
                    answer["hold"] = _unlisted(read, events=events, since=since, outcome=row["outcome"])
                    return answer
                if not any(str(item.get("id")) == str(run_id) for item in runs):
                    answer["reject"] = {"run_id": str(run_id), "event": event,
                                        "reason": f"is not among the listed {read['scope']}"}
            if answer["reject"] is not None:
                rejected, run = answer["reject"], None
        if run is None:
            if lookup is None:
                answer["hold"] = "the row carries neither a run identity nor a commit to look up"
                return answer
            runs, read = _history(port, workflow=row["action"]["target"], commit=lookup)
            answer["provider_read"] = read
            if read["truncated"]:
                answer["hold"] = _unlisted(read, events=events, since=since, outcome=row["outcome"])
                return answer
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
        run_id = provider.get("run_id") or (row.get("carried_from") or {}).get("run_id")
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
    answer = _observe(port or _port(project), row, unit, journal, project=project, now=moment)
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
    result = {**result, "status": settled["outcome"], "action": "readback", "receipt": settled["receipt"]}
    if row["action"]["kind"] == "merge" and settled["outcome"] == "PASS":
        # A carried merge read back as merged reads back its unit's unsettled effects too (R117).
        from .governor import _merged_readback
        result["readback"] = _merged_readback(project, objective, owner=owner, row=settled, port=port, now=moment)
    return result
