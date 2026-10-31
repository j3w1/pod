"""One safe automatic retry of a delivery failure, then diagnosis of the receiving session.

A delivery failure is an attempt whose assignment never reached the receiving agent: Orca failed it at
agent readiness before any input was written. Every decision is made from a fresh exact native read; no
counter or marker is stored. The first attempt's failure earns the one retry, a retry's failure earns the
diagnosis, and a visibly blocked receiver is diagnosed at once and never re-instructed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from .errors import PodError

READINESS_STAGE = "agent_readiness"
_TOKEN = re.compile(r"[a-z][a-z_]{0,63}")
_BLOCKED = re.compile(r"Agent startup blocked:\s*(\S*)")
_REASON = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
# What the receiver's rendered screen shows, as a closed vocabulary; the screen text itself is never kept.
_SCREEN = (("update_prompt", re.compile(r"skip for now")),
           ("trust_prompt", re.compile(r"do you trust|trust (the contents of )?this (folder|directory)")),
           ("login_required", re.compile(r"(please|must|need to) (log|sign) ?in|not logged in|login required|"
                                         r"invalid api key|authentication (failed|required)")),
           ("usage_limit", re.compile(r"usage limit|rate limit|quota (exceeded|reached)|out of credits")))


def receiver(shown: dict) -> dict:
    """What one exact read shows about whether the assignment reached the receiving session."""
    from .abandoned import _harmless, observed_start
    result = shown["result"]
    worker, observation = result["worker"], result.get("observation")
    effects = worker.get("effects")
    if not isinstance(effects, list) or any(not isinstance(row, dict) for row in effects):
        engagement = "unknown"
    elif any(row.get("kind") == "dispatch_input" for row in effects) or observed_start(shown):
        engagement = "possible"
    elif not all(_harmless(row) for row in effects):
        engagement = "unknown"  # An effect still running, failed or unclassified is not proof nothing started.
    else:
        engagement = "none"
    wait = observation.get("agentWait") if isinstance(observation, dict) else None
    message = worker.get("lastError")
    blocked = _BLOCKED.match(message) if isinstance(message, str) else None
    reason = None
    if blocked:  # A blocked start is recognized by Orca's prefix; the reason is shown only when it is a short token.
        reason = blocked.group(1) if _REASON.fullmatch(blocked.group(1)) else "unrecognized"
    terminal = worker.get("agentTerminalHandle")
    return {"state": _token(worker.get("state")), "stage": _token(worker.get("stage")), "engagement": engagement,
            "waiting": wait is not None, "blocked": reason,
            "terminal": terminal if isinstance(terminal, str) and terminal else None}


def _token(value: object) -> str | None:
    """Only a short lowercase native enum reaches a diagnosis; anything else is shown as absent."""
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def screen_cause(port, facts: dict) -> str | None:
    """Classify the receiver's rendered screen by one bounded read-only native read; None when it is unavailable."""
    read = getattr(port, "read_screen", None)
    if read is None or facts.get("terminal") is None:
        return None
    rows = read(facts["terminal"])
    if rows is None:
        return "unreadable"
    text = " ".join(rows).lower()
    return next((name for name, pattern in _SCREEN if pattern.search(text)), "unrecognized")


def diagnosis(cause: str, facts: dict, attempts: list[dict], next_action: str, screen: str | None = None) -> dict:
    """A bounded cause from native fields and the screen class only; no provider text and no worker content."""
    detail = {key: facts[key] for key in ("state", "stage", "engagement", "blocked") if facts.get(key) is not None}
    if screen is not None:
        detail["screen"] = screen
    return {"action": "diagnosis", "cause": cause, "receiver": detail, "attempts": attempts,
            "next_action": next_action}


def _attempt(row: dict) -> dict:
    return {"admission": row["admission_id"], "dispatch": row["native_binding"]["dispatchId"]}


_WAIT_NEXT = ("The receiving session is parked on a prompt only a person can answer; Pod sent and will send no second "
              "instruction and answers nothing. Read it with `orca terminal read --screen` on the worker's terminal and "
              "have its owner or policy decide")
_BLOCKED_NEXT = ("Agent startup is blocked by a provider prompt in the receiving session; Pod answers and retries nothing. "
                 "Resolve it there (the recognized Codex software-update menu has the guarded opt-out in recovery "
                 "guidance), then admit again with the same retry_of")
_SECOND_NEXT = ("Two attempts did not reach the receiver; no third start. Correct the cause above in the receiving "
                "session or provider setup, then admit explicitly with the same retry_of. Orca's own retry "
                "circuit-breaker still applies and is never routed around with a new Task or Run")
_EXECUTION_NEXT = ("Input reached the receiver or its execution cannot be excluded, so nothing was retried. Read the "
                   "exact worker output through Orca before any replacement")
_STOP_NEXT = ("The attempt's exit and resources are not proved, so nothing was retried. Complete any stop or release "
              "through Orca, then repeat this admission")
_STAGE_NEXT = ("The attempt failed before input at a stage Pod does not recognize as a readiness failure, so nothing "
               "was retried. Inspect the exact worker through Orca")


def follow_up(project: Path, objective: str, *, admission: dict, port, state: dict,
              relaunch: Callable[[str], dict]) -> dict | None:
    """Decide the delivery step after one bound attempt; None when there is nothing to report.

    `relaunch` re-enters the full admission for the same frozen packet with `retry_of`, so every fence of a
    first start, including the current candidate and packet, applies again to the retry.
    """
    from .ledger import admission_identity
    from .operations import _assignment_evidence
    if "abandoned_accounting" in admission:
        return None  # An abandoned attempt is accounted by its own claim rules, never retried here.
    binding = admission["native_binding"]
    try:
        shown = port.show_worker(binding["dispatchId"])
        evidence = _assignment_evidence(shown, admission)
    except PodError as exc:
        return {"action": "hold", "code": exc.code, "next_action": "Read the exact native Dispatch; nothing was retried"}
    facts = receiver(shown)
    parent = admission.get("retry_of")
    attempts = [_attempt(admission)] if parent is None else [_attempt(state["admissions"][parent]), _attempt(admission)]
    if facts["waiting"]:
        return diagnosis("receiver_waiting", facts, attempts, _WAIT_NEXT, screen_cause(port, facts))
    if facts["state"] != "failed":
        return None  # A stop is the coordinator's own act, not a delivery failure.
    if facts["blocked"] is not None:
        return diagnosis("startup_blocked", facts, attempts, _BLOCKED_NEXT, screen_cause(port, facts))
    if facts["engagement"] != "none":
        return {"action": "none", "reason": "execution_not_excluded", "receiver": {"engagement": facts["engagement"]},
                "next_action": _EXECUTION_NEXT}
    if facts["stage"] != READINESS_STAGE:
        return diagnosis("unrecognized_failure_stage", facts, attempts, _STAGE_NEXT, screen_cause(port, facts))
    if evidence["retry_eligible"] is not True or evidence.get("claim") is not None:
        return {"action": "hold", "reason": "stop_not_proved", "next_action": _STOP_NEXT}
    if parent is not None:
        return diagnosis("second_delivery_failure", facts, attempts, _SECOND_NEXT, screen_cause(port, facts))
    own = admission_identity(objective=objective, run_id=admission["run_id"], task_id=admission["task_id"],
                             retry_of_dispatch_id=binding["dispatchId"])
    others = [row for key, row in state["admissions"].items() if row.get("retry_of") == admission["admission_id"] and key != own]
    if others:
        return {"action": "none", "reason": "retry_exists", "admission": others[0]["admission_id"]}
    try:
        child = relaunch(admission["admission_id"])
    except PodError as exc:
        from .ledger import read
        if own in read(project, objective)["admissions"]:
            # The retry's row was reserved before the native call: it may have started, so it is neither
            # refused nor repeated. Repeating this admission recovers that one attempt by its request.
            return {"action": "retry_unresolved", "admission": own, "code": exc.code,
                    "next_action": "Repeat this identical admission to recover the retry by its request; no second "
                                   "start is made", "attempts": attempts}
        return {"action": "retry_refused", "code": exc.code, "message": str(exc),
                "next_action": (exc.detail or {}).get("next_action") if isinstance(exc.detail, dict) else None,
                "attempts": attempts}
    result = {"action": "retried", "admission": child["admission"]["admission_id"], "status": child["status"],
              "attempts": attempts}
    if child.get("delivery") is not None:
        result["delivery"] = child["delivery"]
    return result
