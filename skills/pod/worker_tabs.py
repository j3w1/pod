"""Display titles and finished-tab decisions from existing worker-show facts only.

No native reader or lifecycle state lives here. Every consumer supplies its current
readback and records; the projection is also the new-write closure precondition.
"""

from __future__ import annotations

import re
import shlex

from .contracts import bound
from .errors import PodError
from .readiness import STATES

TITLE_LIMIT = 96
ROLE_LABEL_LIMIT = bound("packet", "role_label")
PURPOSE_LIMIT = bound("packet", "purpose")
_ROLES = {"implement": "Implementer", "investigate": "Investigator", "review": "Reviewer"}
_OMIT = frozenset("""status progress outcome pending running working ready completed complete done
    succeeded success failed failure blocked stopped active retained released approved authorized
    finished verified reviewed passing pass fail stale unverifiable disconnected
    authority authorization permission owner user system developer coordinator admin root model effort codex claude gpt
    opus fable sonnet haiku astra sol luna gemini openai anthropic low medium high xhigh max ultra minimal native default secret secrets
    password token credential credentials bearer api key terminal handle run task dispatch admission
    packet id uuid sha sha256 term ctx wtr req sk pk""".split()) | frozenset(STATES)


def _words(value: str) -> str:
    # A title admits ordinary words only, never identifier/path/credential-shaped
    # tokens. Do not split an unsafe token into seemingly safe pieces.
    words = []
    skip_value = False
    for token in value.split():
        token = token.strip(",.;:!?()[]{}\"'")
        if skip_value:
            skip_value = False
            continue
        if token.casefold() in {"model", "effort", "authority", "password", "token", "bearer", "secret",
                                "credential", "key", "id", "terminal", "handle", "run", "task", "dispatch",
                                "admission", "packet"}:
            skip_value = True
            continue
        if (re.fullmatch(r"[A-Za-z]{1,24}(?:-[A-Za-z]{1,24})?", token)
                and all(part.casefold() not in _OMIT for part in token.split("-"))):
            words.append(token)
    return " ".join(words)


def title(body: dict) -> str:
    """One deterministic, nonempty <Role>: <purpose> of the packet body alone."""
    role = _words(body.get("role_label", ""))[:32].strip() or _ROLES.get(body.get("role"), "Worker")
    purpose = _words(body.get("purpose", body.get("responsibility", ""))) or "Assigned work"
    room = TITLE_LIMIT - len(role) - 2
    if len(purpose) > room:
        purpose = purpose[:room + 1].rsplit(" ", 1)[0] if " " in purpose[:room + 1] else purpose[:room]
    return f"{role}: {purpose}"


def rename(title_value: str, handle: str | None, *, started: bool = True) -> dict:
    argv = (["orca", "terminal", "rename", "--terminal", handle, "--title", title_value, "--json"]
            if started and handle else None)
    return {"title": title_value, "rename_command": argv,
            "rename_reason": None if argv else "start has no terminal handle" if started else "worker not started"}


def read_command(dispatch: str) -> list[str]:
    return ["orca", "orchestration", "worker-show", "--dispatch", dispatch, "--json"]


def _keep(state: dict, objective: str, dispatch: str, handle: str) -> dict | None:
    instruction = None
    for row in state.get("constraints", []):
        value = row.get("value")
        if (row.get("kind") == "keep_worker_tab" and row.get("provenance") == "user_direct"
                and row.get("active", True) and isinstance(value, dict)
                and (value.get("objective"), value.get("dispatch"), value.get("terminal"))
                == (objective, dispatch, handle)):
            instruction = row if value.get("keep") is True else None
    return instruction


def retention_reason(state: dict, admission: dict, keep: dict | None) -> str | None:
    if keep:
        return "owner request"
    obligations = (state.get("checkpoint") or {}).get("obligations") or []
    # The result's reviewer can be retained through both correction and delta review.
    children = [ob for ob in obligations if ob.get("kind") == "correction"
                and (ob.get("id") in admission.get("serves", [])
                     or (ob.get("finding") or {}).get("attempt") == admission["admission_id"])]
    if any(ob["state"] not in ("satisfied", "withdrawn") for ob in children):
        return "corrections"
    parents = {ob.get("parent") for ob in children}
    if children and any(ob["id"] in parents and ob["state"] not in ("satisfied", "withdrawn")
                        for ob in obligations):
        return "delta review"
    return None


def project(state: dict, admission: dict, facts: dict, *, settled: bool,
            failure: str | None = None, packet_body: dict | None = None) -> dict:
    """Apply the finished worker table, including exclusions and keep precedence."""
    binding = admission.get("native_binding") or {}
    objective = (state.get("checkpoint") or {}).get("objective")
    handle, dispatch = binding.get("terminalHandle"), binding.get("dispatchId")
    resource = facts.get("terminal_ownership") or {}
    release = facts.get("native_release") or {}
    terminal = facts.get("terminal_readback") or {}
    row = {"admission": admission["admission_id"], "dispatch": dispatch, "terminal": handle,
           "state": None, "row": None, "owner_kept": False,
           "ownership": resource.get("ownershipState") or release.get("state"),
           "release_state": resource.get("releaseState") or release.get("releaseState"),
           "reason": facts.get("retained_reason"), "next_action": facts.get("native_next_action"),
           "native_next_action": facts.get("native_next_action"),
           "command": None, "wording": "", "blocks_closure": False, "listed": False}

    def finish(kind, wording="", **fields):
        return {**row, "row": kind, "wording": wording, **fields}

    # These exclusions precede even unreadable facts and an owner's keep.
    if (not settled or handle == state.get("owner") or facts.get("native_role") in ("coordinator", "setup")
            or admission.get("objective") not in (None, objective)
            or resource.get("worktreeId") not in (None, binding.get("worktreeId"))
            or terminal.get("worktreeId") not in (None, binding.get("worktreeId"))
            or resource.get("terminalHandle") not in (None, handle)
            or terminal.get("handle") not in (None, handle)):
        return finish("excluded")
    if terminal.get("connected") is False:
        return finish("disconnected")
    owner = resource.get("ownerDispatchId") or release.get("ownerDispatchId")
    if owner is not None and owner != dispatch:
        return finish("reused_later")
    if not failure and not resource and not release and not terminal:
        return finish("not_materialized", "This worker has no tab.")
    repeat = read_command(dispatch)
    if (failure or not handle or terminal.get("connected") is not True
            or ("worker_terminal" in facts and facts["worker_terminal"] != handle)
            or owner != dispatch
            or row["ownership"] not in ("owned", "retained", "user_owned", "external", "released")
            or row["release_state"] is None):
        code = failure or "worker_terminal_unverified"
        return finish("unreadable", f"Terminal facts unreadable ({code}); repeat {shlex.join(repeat)}.",
                      state="unknown", listed=True, blocks_closure=True,
                      failed_read={"code": code, "command": repeat})
    row["listed"] = True
    action = row["next_action"]
    argv = action.get("argv") if isinstance(action, dict) else None
    # Orca supplies arguments to the selected CLI, without an executable.
    command = ["orca", *argv] if isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv) else None
    next_step = (shlex.join(command) if command else
                 "Orca gives no next command; repeat " + shlex.join(repeat))
    row["command"] = command
    row["state"] = "releasable"
    timing = "after recorded closure" if (state.get("checkpoint") or {}).get("closure") else "before closure"
    if row["release_state"] not in ("none", "not_requested", "unreleased", "retained", "released"):
        return finish("release_pending", f"Confirm this tab's release {timing}: {next_step}.",
                      blocks_closure=True)
    keep = _keep(state, objective, dispatch, handle)
    reason = retention_reason(state, admission, keep)
    owned = row["ownership"] in ("owned", "retained")
    if row["ownership"] == "released":
        # A connected readback and a released resource disagree; repeat the read.
        return finish("unreadable", f"Released resource still has a connected tab; repeat {shlex.join(repeat)}.",
                      command=None, state="unknown", blocks_closure=True,
                      failed_read={"code": "worker_terminal_unverified", "command": repeat})
    if keep:
        row.update(owner_kept=True, owner_instruction={key: keep[key] for key in
                   ("id", "instruction", "channel", "relay") if key in keep})
    if owned:
        debt = "Release still owed" if (state.get("checkpoint") or {}).get("closure") else "Release owed"
        wording = (f"Owner-kept by instruction {keep['id']}; release available when the owner lets it go: {next_step}."
                   if keep else f"{debt} {timing}: {next_step}.")
        if reason in ("corrections", "delta review"):
            wording += f" Retain only while {reason} of this result are pending; release when the cycle ends."
        wording += " After an accepted report choose exactly one: immediate supported reuse, permitted retention, or release."
        fields = {"blocks_closure": not bool(keep), "retention_reason": reason}
        if reason and packet_body is not None:
            fields["retained_title"] = rename(f"{title(packet_body)} (retained: {reason})", handle)
        return finish("owner_kept_owned" if keep else "retained_owned" if row["release_state"] == "retained" else "owned",
                      wording, **fields)
    row["state"] = "kept"
    wording = ("Orca recorded terminal input on this tab; it will not release it."
               if row["ownership"] == "user_owned" else "Orca does not own the terminal and will not release it.")
    wording += " Do not reuse."
    if keep:
        return finish("owner_kept_unowned", f"Owner-kept by instruction {keep['id']}. " + wording, command=None)
    close = ["orca", "terminal", "close", "--terminal", handle]
    return finish("user_owned" if row["ownership"] == "user_owned" else "external",
                  wording + " Under the owner's standing authorization, close this finished worker tab before the final report: "
                  + shlex.join(close) + ".", command=close)


def rows(state: dict, placements: dict, *, outstanding=(), failure: str | None = None) -> list[dict]:
    result = []
    recorded_settlement = bool((state.get("checkpoint") or {}).get("closure")
                               or state.get("continued_by") or state.get("historical_retirement"))
    for key, admission in state.get("admissions", {}).items():
        if admission.get("state") != "bound":
            continue
        # A failed read cannot prove settlement; an accepted report or recorded
        # closure can identify a finished worker whose tab still needs readback.
        settled = key not in outstanding if not failure else bool(admission.get("report") or recorded_settlement)
        row = project(state, admission, placements.get(key, {}), settled=settled, failure=failure)
        if row["listed"]:
            result.append(row)
    return result


def wording(tabs: list[dict]) -> str:
    return " ".join(f"Worker tab {row['terminal'] or row['dispatch']}: {row['wording']}" for row in tabs)


def finalization(value: dict | None, tabs: list[dict]) -> dict | None:
    if not value or not tabs:
        return value
    text = wording(tabs)
    if value["remaining"]:
        text = "Record " + ", ".join(value["remaining"]) + " in that order. " + text
    return {**value, "next_action": text}


def require_closed_tabs(tabs: list[dict]) -> None:
    blocking = [row for row in tabs if row["blocks_closure"]]
    if blocking:
        code = next((row["failed_read"]["code"] for row in blocking if "failed_read" in row), "worker_tabs_open")
        raise PodError(code, wording(blocking), {"worker_tabs": tabs, "next_action": wording(blocking),
                       "steps": "Record the proof without closing, settle the worker tabs, then close."})
