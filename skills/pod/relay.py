"""Host verification and durable single-use receipts for bounded relayed decisions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .errors import PodError
from .contracts import bound
from .util import atomic_json, bounded_json, bounded_text, digest, exact, normalize_timestamp

SCHEMA = "pod-relay-verification/v1"
TIMEOUT_S = 5
MAX_VERDICT = 16 * 1024
BINDING_FIELDS = {"schema", "reference", "action", "objective", "candidate", "target", "pr"}
VERDICT_FIELDS = BINDING_FIELDS | {"verified", "owner_source", "decided_at", "single_use"}
NEXT = {
    "relay_verifier_unconfigured": "configure authorization_verifier in the personal config, or obtain session consent",
    "relay_verifier_failed": "repair the host verifier and retry the same decision, or obtain session consent",
    "relay_verifier_timeout": "resolve the host verifier timeout and retry the same decision, or obtain session consent",
    "relay_verifier_malformed": "correct the host verifier JSON contract, or obtain session consent",
    "relay_verifier_mismatch": "obtain a verified owner decision for this exact action, objective, candidate and target",
    "relay_verifier_denied": "obtain a positive verified owner decision, or obtain session consent",
    "relay_authorization_reused": "obtain a fresh owner decision with a new single_use key for this authorization",
}


def refusal(code: str, *, fields: list[str] | None = None, condition: str | None = None) -> PodError:
    # Verifier output, stderr, arguments and exception text never become diagnostics; only a validated window
    # condition from a closed list does.
    next_action = ("obtain a fresh owner decision bound to the current standing_authorization.scope_digest; "
                   "the host must save that digest with the decision" if fields == ["standing.scope_digest"] else NEXT[code])
    return PodError(code, f"Relayed authorization refused: {code}. Next: {next_action}",
                    {"next_action": next_action, **({"fields": fields} if fields else {}),
                     **({"condition": condition} if condition else {})})


def _window_fields(verdict: dict) -> None:
    """The orca_window echo and the host's window facts (pod#110); the window module checks their meaning."""
    from .window import HOST_CONDITIONS, KINDS, LOSS_BASIS, SCHEMA as WINDOW
    echo = exact(verdict["orca_window"], {"old_runtime", "current_runtime", "coordinator"},
                 {"old_runtime", "current_runtime", "coordinator"}, name="relay orca_window")
    for key in echo:
        bounded_text(echo[key], name=key, limit=256).encode("utf-8")
    if verdict["verified"]:
        if "condition" in verdict:
            raise ValueError("condition")
        window = exact(verdict["window"], {"schema", "operation", "effect", "kind", "stopped_at", "old_runtime",
                                           "after_runtime", "ended_sessions", "loss_basis"},
                       {"schema", "operation", "effect", "kind", "stopped_at", "old_runtime", "after_runtime",
                        "ended_sessions", "loss_basis"}, name="relay window")
        for key in ("operation", "effect", "old_runtime"):
            bounded_text(window[key], name=key, limit=512).encode("utf-8")
        if window["after_runtime"] is not None:
            bounded_text(window["after_runtime"], name="after_runtime", limit=512).encode("utf-8")
        if (window["schema"] != WINDOW or window["kind"] not in KINDS or window["ended_sessions"] is not True
                or window["loss_basis"] != LOSS_BASIS):
            raise ValueError("window")
        window["stopped_at"] = normalize_timestamp(window["stopped_at"], field="window stop time",
                                                   code="relay_verifier_malformed")
    elif verdict["window"] is not None or verdict.get("condition") not in HOST_CONDITIONS:
        raise ValueError("denied window")


def _output(command: list[str], request: dict) -> bytes:
    """Bound memory and wall time, including a child that never closes stdout."""
    process = None
    try:
        deadline = time.monotonic() + TIMEOUT_S
        process = subprocess.Popen(command, cwd="/", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True,
                                   env={k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")})
        pending = memoryview(json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode() + b"\n")
        os.set_blocking(process.stdin.fileno(), False)
        output = bytearray()
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            ready.register(process.stdin, selectors.EVENT_WRITE)
            while ready.get_map():
                remaining = deadline - time.monotonic()
                events = ready.select(max(0, remaining))
                if remaining <= 0 or not events:
                    raise refusal("relay_verifier_timeout")
                for key, _ in events:
                    if key.fileobj is process.stdin:
                        pending = pending[os.write(process.stdin.fileno(), pending):]
                        if not pending:
                            ready.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        block = os.read(process.stdout.fileno(), min(4096, MAX_VERDICT + 1 - len(output)))
                        if not block:
                            ready.unregister(process.stdout)
                        output.extend(block)
                        if len(output) > MAX_VERDICT:
                            raise refusal("relay_verifier_malformed")
        process.wait(timeout=max(0, deadline - time.monotonic()))
        if process.returncode:
            raise refusal("relay_verifier_failed")
        return bytes(output)
    except subprocess.TimeoutExpired:
        raise refusal("relay_verifier_timeout") from None
    except OSError:
        raise refusal("relay_verifier_failed") from None
    finally:
        if process is not None:
            # Also stop descendants retaining the pipe after the command itself exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdin.close()
            process.stdout.close()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


def validate_verdict(value: object) -> dict:
    """Validate stored evidence too, without invoking the host on read-only paths."""
    try:
        action = value.get("action") if isinstance(value, dict) else None
        extra = {action} if action in ("standing", "answer", "issue_revision") else set()
        if action == "orca_window":
            extra = {"orca_window", "window"}
        verdict_fields = (VERDICT_FIELDS - {"owner_source"} | {"responder_source"}
                          if action == "answer" else VERDICT_FIELDS) | extra
        optional = verdict_fields | ({"condition"} if action == "orca_window" else set())
        verdict = exact(value, optional, verdict_fields, name="relay verdict")
        if verdict["schema"] != SCHEMA or type(verdict["verified"]) is not bool:
            raise ValueError("verdict")
        if verdict["action"] not in ("publish", "merge", "standing", "answer", "issue_revision", "orca_window"):
            raise ValueError("action")
        source = "responder_source" if action == "answer" else "owner_source"
        for key, limit in (("reference", 512), ("objective", 256), (source, 512), ("single_use", 512)):
            bounded_text(verdict[key], name=key, limit=limit).encode("utf-8")
        if action in ("standing", "answer", "issue_revision", "orca_window"):
            if verdict["candidate"] is not None or verdict["pr"] is not None:
                raise ValueError("candidate/pr")
            if action == "orca_window":
                _window_fields(verdict)
            elif action == "issue_revision":
                from .issue_revision import validate_binding
                validate_binding(verdict["issue_revision"], verdict["target"])
            elif action == "standing":
                from .contracts import STANDING_EXCLUSIONS, STANDING_SCOPES
                standing = exact(verdict["standing"], {"operation", "scope", "widening", "expires_at", "scope_digest"},
                                 {"operation", "scope", "widening", "expires_at"}, name="relay standing")
                if standing["operation"] == "revoke":
                    if "scope_digest" in standing or any(standing[key] is not None for key in ("scope", "widening", "expires_at")):
                        raise ValueError("revoke")
                elif standing["operation"] == "activate":
                    if "scope_digest" in standing:
                        if not isinstance(standing["scope_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", standing["scope_digest"]):
                            raise ValueError("scope digest")
                    from .standing import _names
                    _names(standing["scope"], STANDING_SCOPES, nonempty=True)
                    _names(standing["widening"], STANDING_EXCLUSIONS)
                    if standing["expires_at"] is not None:
                        normalize_timestamp(standing["expires_at"], field="standing expiry", code="relay_verifier_malformed")
                else:
                    raise ValueError("operation")
            else:
                answer = exact(verdict["answer"], {"question", "text"}, {"question", "text"}, name="relay answer")
                for key, limit in (("question", 512), ("text", 4096)):
                    bounded_text(answer[key], name=key, limit=limit).encode("utf-8")
        else:
            candidate = exact(verdict["candidate"], {"commit", "tree"}, {"commit", "tree"}, name="relay candidate")
            from .gitio import OBJECT_ID
            if (any(not isinstance(v, str) or not OBJECT_ID.fullmatch(v) for v in candidate.values())
                    or len(candidate["commit"]) != len(candidate["tree"])):
                raise ValueError("candidate")
        target = exact(verdict["target"], {"repository", "remote", "base"}, {"repository", "remote", "base"},
                       name="relay target")
        for field in target:
            bounded_text(target[field], name=field, limit=256).encode("utf-8")
        if ((verdict["action"] == "publish" and verdict["pr"] is not None)
                or (verdict["action"] == "merge" and (type(verdict["pr"]) is not int
                    or not bound("authorization", "pr", "min") <= verdict["pr"] <= bound("authorization", "pr", "max")))):
            raise ValueError("pr")
        decided = normalize_timestamp(verdict["decided_at"], field="relay decision time",
                                      code="relay_verifier_malformed")
        return {**verdict, "decided_at": decided}
    except (PodError, ValueError, TypeError, UnicodeError):
        raise refusal("relay_verifier_malformed") from None


def verify(command: list[str] | None, request: dict) -> dict:
    if not command:
        raise refusal("relay_verifier_unconfigured")
    try:
        value = json.loads(_output(command, request).decode("utf-8"), object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError):
        raise refusal("relay_verifier_malformed") from None
    if (request["action"] == "standing" and request["standing"]["operation"] == "activate"
            and isinstance(value, dict) and value.get("verified") is True
            and (not isinstance(value.get("standing"), dict)
                 or value["standing"].get("scope_digest") != request["standing"]["scope_digest"])):
        raise refusal("relay_verifier_mismatch", fields=["standing.scope_digest"])
    if (request["action"] == "standing" and isinstance(value, dict) and value.get("verified") is True
            and isinstance(value.get("standing"), dict) and value["standing"].get("operation") != request["standing"]["operation"]):
        raise refusal("relay_verifier_mismatch", fields=["standing"])
    verdict = validate_verdict(value)
    if verdict["action"] == "orca_window" != request["action"]:
        raise refusal("relay_verifier_mismatch", fields=["action"])
    if not verdict["verified"]:
        raise refusal("relay_verifier_denied", condition=verdict.get("condition") if request["action"] == "orca_window" else None)
    binding_fields = BINDING_FIELDS | ({request["action"]} if request["action"] in ("standing", "answer", "issue_revision", "orca_window") else set())
    mismatches = sorted(key for key in binding_fields if verdict.get(key) != request[key])
    if request["action"] in ("standing", "answer") and "target" in mismatches:
        mismatches.remove("target")
        mismatches.extend("target." + key for key in request["target"]
                          if verdict["target"][key] != request["target"][key])
        mismatches.sort()
    if mismatches:
        raise refusal("relay_verifier_mismatch", fields=mismatches)
    return verdict


def check_use(project: Path, verdict: dict, *, consume: bool = False, refuse: bool = False,
              transition: str | None = None) -> None:
    """One non-evicting receipt per key across all objectives, independent of journal retention."""
    from .ledger import _lock, state_root
    path = state_root(project) / ".relay-authorizations" / (digest(verdict["single_use"]) + ".json")
    receipt = {"schema": "pod-relay-use/v1", "authorization": digest(verdict)}
    if transition is not None:
        if not isinstance(transition, str) or not re.fullmatch(r"[0-9a-f]{64}", transition):
            raise refusal("relay_verifier_malformed")
        receipt["transition"] = transition
    with _lock(path):
        if path.exists() or path.is_symlink():
            if bounded_json(path, limit=1024) != receipt or refuse:
                raise refusal("relay_authorization_reused")
        elif refuse:
            atomic_json(path, {**receipt, "refused": "standing_authorization_future"}, limit=1024)
        elif consume:
            atomic_json(path, receipt, limit=1024)


# This action has its own exact decoder. Existing actions above keep their wire
# vocabulary, owner-only meaning and byte serialization unchanged.
DECISION_BINDING = {"kind", "payload_sha256", "state_sha256", "decision_policy_revision",
                    "decision_generation", "categories", "resources", "request_id"}
DECISION_STORED = {"actor", "decided_at", "recorded_at", "decision_binding", "decision_verification"}
DECISION_RAW = {"provenance", "instruction", "channel", "relay", "question"}
_DECISION_SEAL = object()
_CURRENT_DECISION = ContextVar("pod_current_decision", default=None)


def decision_input(value: object) -> bool:
    return isinstance(value, dict) and (value.get("provenance") in ("agent_decision", "decision_request")
                                      or value.get("channel") == "agent_session"
                                      or isinstance(value.get("relay"), dict)
                                      and value["relay"].get("verification") == "decision")


def _hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _decision_binding(value: object) -> dict:
    from .policy import _category_list
    row = exact(value, DECISION_BINDING | {"kinds"}, DECISION_BINDING, name="decision binding")
    bounded_text(row["kind"], name="decision_kind", limit=64)
    kinds = row.get("kinds", [row["kind"]])
    if (not isinstance(kinds, list) or not kinds or len(kinds) > 16
            or any(not isinstance(kind, str) or not kind or len(kind) > 64 for kind in kinds)
            or kinds != sorted(set(kinds)) or row["kind"] not in kinds):
        raise ValueError("kinds")
    for key in ("payload_sha256", "state_sha256", "decision_policy_revision", "request_id"):
        if not _hash(row[key]):
            raise ValueError("digest")
    if type(row["decision_generation"]) is not int or row["decision_generation"] < 0:
        raise ValueError("generation")
    if row["categories"] != _category_list(row["categories"]):
        raise ValueError("categories")
    resources = row["resources"]
    if (not isinstance(resources, dict) or len(resources) > 16
            or any(not isinstance(key, str) or not key or len(key) > 64
                   or not isinstance(item, str) or not item or len(item) > 512
                   for key, item in resources.items())):
        raise ValueError("resources")
    return row


def validate_decision_verdict(value: object) -> dict:
    """Decode the additive action, including stored evidence, without host effects."""
    try:
        names = BINDING_FIELDS | {"decision", "verified", "principal", "decided_at", "single_use"}
        row = exact(value, names, names, name="decision verdict")
        if row["schema"] != SCHEMA or row["action"] != "decision" or type(row["verified"]) is not bool:
            raise ValueError("action")
        if row["candidate"] is not None or row["pr"] is not None:
            raise ValueError("candidate")
        for key in ("reference", "single_use"):
            bounded_text(row[key], name=key, limit=512).encode("utf-8")
        if row["objective"] is not None:
            bounded_text(row["objective"], name="objective", limit=256)
        target = {"machine": "personal"} if row["objective"] is None else {"objective": row["objective"]}
        if row["target"] != target:
            raise ValueError("target")
        binding = _decision_binding(row["decision"])
        principal = exact(row["principal"], {"kind", "identity", "scope"},
                          {"kind", "identity", "scope"}, name="decision principal")
        if principal["kind"] not in ("person", "agent"):
            raise ValueError("principal")
        bounded_text(principal["identity"], name="principal_identity", limit=128)
        scope = exact(principal["scope"], {"kinds", "categories", "resources"},
                      {"kinds", "categories", "resources"}, name="decision scope")
        if (not isinstance(scope["kinds"], list) or not scope["kinds"] or len(scope["kinds"]) > 32
                or len(scope["kinds"]) != len(set(scope["kinds"]))):
            raise ValueError("kinds")
        for kind in scope["kinds"]:
            bounded_text(kind, name="scope_kind", limit=64)
        from .policy import _category_list
        _category_list(scope["categories"])
        if not isinstance(scope["resources"], dict):
            raise ValueError("scope resources")
        # Scope membership is authority, separately checked even on a positive verdict.
        if (not set(binding.get("kinds", [binding["kind"]])) <= set(scope["kinds"])
                or not set(binding["categories"]) <= set(scope["categories"])
                or scope["resources"] != binding["resources"]):
            raise refusal("relay_verifier_mismatch", fields=["principal.scope"])
        return {**row, "decided_at": normalize_timestamp(row["decided_at"], field="decision time",
                                                        code="relay_verifier_malformed")}
    except PodError as exc:
        if exc.code == "relay_verifier_mismatch":
            raise
        raise refusal("relay_verifier_malformed") from None
    except (ValueError, TypeError, UnicodeError):
        raise refusal("relay_verifier_malformed") from None


def verify_decision(command: list[str] | None, request: dict) -> dict:
    if not command:
        raise refusal("relay_verifier_unconfigured")
    try:
        value = json.loads(_output(command, request).decode("utf-8"), object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError):
        raise refusal("relay_verifier_malformed") from None
    verdict = validate_decision_verdict(value)
    if not verdict["verified"]:
        raise refusal("relay_verifier_denied")
    mismatches = sorted(key for key in BINDING_FIELDS | {"decision"} if verdict[key] != request[key])
    if mismatches:
        raise refusal("relay_verifier_mismatch", fields=mismatches)
    return verdict


def _raw_decision(value: dict) -> dict:
    """The caller never supplies an actor, kernel time, verdict or authority flag."""
    if not isinstance(value, dict) or DECISION_STORED & set(value):
        raise PodError("invalid_decision", "Stored decision evidence is not public input")
    row = {key: value[key] for key in DECISION_RAW if key in value}
    if "provenance" not in row:
        raise PodError("invalid_decision", "Declare the actual decision provenance explicitly")
    bounded_text(row.get("instruction"), name="instruction", limit=1024).encode("utf-8")
    provenance, channel = row.get("provenance", "user_direct"), row.get("channel", "session")
    if (provenance, channel) == ("decision_request", "relayed"):
        from .contracts import fields, required
        item = exact(row.get("relay"), fields("decision_action_relay"), required("decision_action_relay"), name="decision_action_relay")
        bounded_text(item["reference"], name="reference", limit=512).encode("utf-8")
        if item["verification"] != "decision":
            raise PodError("invalid_decision_relay", "Use the decision verification action")
    elif (provenance, channel) in (("user_direct", "session"), ("agent_decision", "agent_session")):
        if "relay" in row:
            raise PodError("invalid_decision_relay", "Local decisions carry no relay")
    else:
        raise PodError("invalid_decision", "Decision provenance and channel disagree")
    return {**row, "provenance": provenance, "channel": channel}


def _tool_verdict(value: dict) -> dict:
    """Expand a losslessly factored stored scope, then use the full wire decoder."""
    if isinstance(value, dict) and value.get("schema") == "pod-tool-proof/v1":
        principal = exact(value.get("principal"), {"kind", "identity", "scope"},
                          {"kind", "identity", "scope"}, name="factored principal")
        scope = exact(principal["scope"], {"kinds", "categories"}, set(), name="factored scope")
        binding = _decision_binding(value["decision"])
        principal = {**principal, "scope": {
            "kinds": scope.get("kinds", binding.get("kinds", [binding["kind"]])),
            "categories": scope.get("categories", binding["categories"]), "resources": binding["resources"]}}
        value = {**value, "schema": SCHEMA, "principal": principal}
    return validate_decision_verdict(value)


def validate_decision_record(value: dict, *, kind: str | None = None) -> dict:
    """Read truthful kernel evidence; this is never an input authorization port."""
    try:
        if (isinstance(value, dict) and "decision_verification" in value
                and "decision_binding" not in value):
            # Bounded machine questions store the complete verdict once. Its
            # principal, binding and clock are projections, not duplicated bytes.
            compact = {"provenance", "instruction", "channel", "relay", "decision_verification"}
            exact(value, compact | {"recorded_at"}, compact, name="compact tool decision")
            verdict = _tool_verdict(value["decision_verification"])
            value = {**value, "actor": {**verdict["principal"], "channel": "relayed"},
                     "decided_at": verdict["decided_at"], "decision_binding": verdict["decision"],
                     "decision_verification": verdict}
        binding = _decision_binding(value["decision_binding"])
        if kind is not None and binding["kind"] != kind:
            raise ValueError("kind")
        actor = value["actor"]
        channel = value["channel"]
        provenance = "user_direct" if actor["kind"] == "person" else "agent_decision"
        if value.get("provenance", provenance) != provenance:
            raise ValueError("provenance")
        normalize_timestamp(value["decided_at"], field="decision time", code="invalid_decision")
        if "recorded_at" in value:
            normalize_timestamp(value["recorded_at"], field="decision commit time", code="invalid_decision")
        if channel == "relayed":
            verdict = validate_decision_verdict(value["decision_verification"])
            if (not verdict["verified"] or verdict["decision"] != binding
                    or verdict["decided_at"] != value["decided_at"]
                    or value["relay"] != {"reference": verdict["reference"], "verification": "decision"}
                    or actor != {**verdict["principal"], "channel": "relayed"}):
                raise ValueError("evidence")
        elif (set(actor) != {"kind", "channel"} or actor["kind"] not in ("person", "agent")
              or channel != ("session" if actor["kind"] == "person" else "agent_session")
              or actor["channel"] != channel or "relay" in value or "decision_verification" in value):
            raise ValueError("local actor")
        return dict(value)
    except (KeyError, TypeError, ValueError, PodError):
        raise PodError("invalid_decision", "Stored decision evidence is inconsistent") from None


def compact_tool_decision(value: dict) -> dict:
    """Keep full host evidence within the existing question bound, without a new store."""
    value = validate_decision_record(value)
    if value["channel"] != "relayed":
        return dict(value)
    result = {key: value[key] for key in ("provenance", "instruction", "channel", "relay", "decision_verification", "recorded_at") if key in value}
    proof = value["decision_verification"]
    binding, principal = proof["decision"], proof["principal"]
    scope = {key: principal["scope"][key] for key in ("kinds", "categories")
             if principal["scope"][key] != binding.get(key, [binding["kind"]])}
    result["decision_verification"] = {**proof, "schema": "pod-tool-proof/v1", "principal": {**principal, "scope": scope}}
    return result


def validate_agent_withdrawal(value: dict, *, introduced_seq: int = 1, sequence: int | None = None) -> int:
    """One stored predicate for a true, newly accepted agent resolution epoch."""
    row = validate_decision_record(value)
    accepted = row.get("accepted_seq")
    binding = row["decision_binding"]
    if (row.get("by") != "agent_decision" or row["actor"]["kind"] != "agent"
            or binding["kind"] not in ("withdrawal", "revision", "question", "governance", "reopen")
            or "scope_changes" not in binding["categories"]
            or type(accepted) is not int or accepted < introduced_seq
            or sequence is not None and accepted > sequence
            or binding["resources"].get("map_seq") != str(accepted)):
        raise PodError("invalid_decision", "Agent withdrawal must retain its actual accepted resolution epoch and actor")
    return accepted


def same_decision_input(value: dict, recorded: dict) -> bool:
    """Idempotent readback only; this function creates no capability or grant."""
    raw = _raw_decision(value)
    recorded = validate_decision_record(recorded)
    original = {key: recorded[key] for key in DECISION_RAW if key in recorded}
    original["provenance"] = "user_direct" if recorded["actor"]["kind"] == "person" else "agent_decision"
    if recorded["channel"] == "relayed":
        original["provenance"] = "decision_request"
    return raw == original


class Decision:
    """An exact in-memory capability, created only after the boundary derives its effect.

    The caller owns the objective lock. Host lookup happens at construction, before
    commit obtains the short config lock. A local agent is a cooperative origin,
    never an authenticated trusted identity. No JSON value constructs a capability.
    """

    __slots__ = ("project", "objective", "kind", "effect", "state", "snapshot", "binding", "raw",
                 "actor", "verdict", "record", "_seal", "keys", "kinds", "prior", "kernel_at", "_finalized", "reused",
                 "phase_paths", "phase_expected", "phase_started", "completion_only", "order_at", "clock",
                 "personal_change", "pending_use", "execute", "used_at", "notice")

    def __init__(self, project: Path | None, objective: str | None, *, kind: str, effect: dict,
                 state: object, decision: dict, resources: dict, snapshot: dict | None = None,
                 kinds: list[str] | None = None, clock=None):
        from copy import deepcopy
        from datetime import datetime, timezone
        from . import config, policy
        self._seal = _DECISION_SEAL
        self.kernel_at, self._finalized = None, False
        self.reused = False
        self.phase_paths, self.phase_expected, self.phase_started, self.completion_only = {}, {}, False, False
        self.order_at = None
        self.personal_change, self.pending_use, self.execute, self.used_at = None, None, False, None
        self.notice = ""
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.keys, self.kinds, self.prior = [], {kind}, []
        self.project, self.objective, self.kind = project, objective, kind
        self.effect, self.state = deepcopy(effect), digest(state)
        self.raw = _raw_decision(decision)
        self.snapshot = snapshot or config.load(project)
        if self.snapshot["status"] not in ("valid", "missing"):
            raise PodError("decision_config_unavailable", "Repair personal preferences before deciding")
        categories = policy.categories(kind, effect)
        self.binding = {"kind": kind, "kinds": sorted(set(kinds or [kind])),
                        "payload_sha256": digest({"effect": effect, "decision": self.raw}),
                        "state_sha256": self.state,
                        "decision_policy_revision": digest(self.snapshot["decision_policy"]),
                        "decision_generation": self.snapshot["decision_generation"],
                        "categories": categories, "resources": deepcopy(resources)}
        self.binding["request_id"] = digest({"objective": objective, **self.binding})
        _decision_binding(self.binding)
        self.verdict = None
        if self.raw["channel"] == "relayed":
            request = {"schema": SCHEMA, "reference": self.raw["relay"]["reference"], "action": "decision",
                       "objective": objective, "candidate": None, "pr": None,
                       "target": {"machine": "personal"} if objective is None else {"objective": objective},
                       "decision": self.binding}
            self.verdict = verify_decision(self.snapshot["authorization_verifier"], request)
            self.actor = {**self.verdict["principal"], "channel": "relayed"}
            decided_at = self.verdict["decided_at"]
            facts = self._receipt_facts()
            self.kernel_at = facts.get("recorded_at", facts["decided_at"]) if facts else None
        else:
            self.actor = {"kind": "person" if self.raw["channel"] == "session" else "agent",
                          "channel": self.raw["channel"]}
            # The existing receipt retains kernel time after a receipt-before-write crash.
            facts = self._receipt_facts()
            decided_at = facts["decided_at"] if facts else self.clock().isoformat()
            self.kernel_at = facts.get("recorded_at", decided_at) if facts else None
        normalized = {"kind": "person", "channel": self.actor["channel"]} if self.actor["kind"] == "person" else self.actor
        if not policy.permits(self.snapshot["decision_policy"], categories, normalized):
            raise PodError("decision_policy_denied", "The current policy requires a different decider",
                           {"required": policy.required(self.snapshot["decision_policy"], categories), "categories": categories})
        if (datetime.fromisoformat(decided_at.replace("Z", "+00:00")) > self.clock()
                and not (kind == "standing_authorization" and effect.get("operation") == "revoke")):
            raise PodError("decision_future", "A future decision grants no current authority")
        self.record = {**self.raw, "provenance": "user_direct" if self.actor["kind"] == "person" else "agent_decision",
                       "actor": deepcopy(self.actor), "decided_at": decided_at, "decision_binding": deepcopy(self.binding),
                       **({"decision_verification": deepcopy(self.verdict)} if self.verdict else {})}

    def __reduce__(self):
        raise TypeError("Decision capabilities cannot be serialized")

    @classmethod
    def _recorded(cls, project, objective, recorded, *, snapshot=None):
        """Construct only from an adapter-resolved accepted record, never public input."""
        from copy import deepcopy
        from . import config
        row = validate_decision_record(recorded)
        value = cls.__new__(cls)
        value._seal, value.reused, value._finalized = _DECISION_SEAL, True, True
        value.phase_paths, value.phase_expected, value.phase_started, value.completion_only = {}, {}, True, False
        value.order_at = None
        value.personal_change, value.pending_use, value.execute, value.used_at = None, None, False, None
        value.notice = ""
        from datetime import datetime, timezone
        value.clock = lambda: datetime.now(timezone.utc)
        value.project, value.objective = project, objective
        value.record = {key: deepcopy(row[key]) for key in DECISION_RAW | DECISION_STORED if key in row}
        value.record["provenance"] = "user_direct" if row["actor"]["kind"] == "person" else "agent_decision"
        value.binding, value.actor = deepcopy(row["decision_binding"]), deepcopy(row["actor"])
        request_binding = {key: item for key, item in value.binding.items() if key != "request_id"}
        if value.binding["request_id"] != digest({"objective": objective, **request_binding}):
            raise PodError("decision_scope_mismatch", "The recorded decision belongs to another objective")
        value.kind, value.kinds, value.keys, value.prior = value.binding["kind"], set(value.binding.get("kinds", [value.binding["kind"]])), [], []
        value.raw = {key: deepcopy(row[key]) for key in DECISION_RAW if key in row}
        value.raw["provenance"] = value.record["provenance"]
        if row["channel"] == "relayed":
            value.raw["provenance"] = "decision_request"
        value.raw = _raw_decision(value.raw)
        value.snapshot = snapshot or config.load(project)
        value.state, value.effect = value.binding["state_sha256"], None
        value.kernel_at = row.get("recorded_at", row["decided_at"])
        value.verdict = row.get("decision_verification")
        return value

    def revalidate_authorizer(self):
        from . import policy
        actor = {"kind": "person", "channel": self.record["channel"]} if self.actor["kind"] == "person" else self.actor
        if not policy.permits(self.snapshot["decision_policy"], self.binding["categories"], actor):
            raise PodError("decision_policy_denied", "Current policy no longer permits this decision actor")
        if self.verdict is not None:
            request = {key: self.verdict[key] for key in BINDING_FIELDS | {"decision"}}
            if verify_decision(self.snapshot["authorization_verifier"], request) != self.verdict:
                raise refusal("relay_verifier_mismatch")

    @classmethod
    def reuse(cls, project, objective, recorded, *, state, snapshot=None):
        """Revalidate an existing scoped grant, never mint or consume it again."""
        value = cls._recorded(project, objective, recorded, snapshot=snapshot)
        value.revalidate_authorizer()
        value.recheck(state, value.snapshot)
        value.check_receipt()
        return value

    @classmethod
    def resume(cls, project, objective, recorded, *, effect):
        value = cls.complete(project, objective, recorded, effect=effect)
        value.completion_only = False
        value.revalidate_authorizer()
        value.recheck_policy(value.snapshot)
        return value

    @classmethod
    def complete(cls, project, objective, recorded, *, effect):
        """Only an adapter-proved landed transition may finish factual recording."""
        value = cls._recorded(project, objective, recorded)
        if (value.kind not in ("claim", "handoff", "retirement", "tool_permission", "tool_operation")
                or digest({"effect": effect, "decision": value.raw}) != value.binding["payload_sha256"]):
            raise PodError("decision_stale", "The pending transition binds another complete effect")
        value.effect, value.completion_only = effect, True
        value.check_receipt()
        return value

    def phases(self, paths, states):
        from copy import deepcopy
        if (self.kind not in ("claim", "handoff", "retirement") and not
                (self.reused and self.kind in ("tool_permission", "tool_operation"))) or set(paths) != set(states):
            raise PodError("decision_scope_mismatch", "Transition phases must name its exact existing records")
        if not self.reused and digest(states) != self.state:
            raise PodError("decision_stale", "Transition predecessors changed before preparation")
        self.phase_paths, self.phase_expected = dict(paths), deepcopy(states)

    def check_receipt(self):
        path = self._receipt_path()
        if not (path.exists() or path.is_symlink()):
            raise PodError("decision_receipt_unavailable", "The recorded grant has no durable use receipt")
        receipt = bounded_json(path, limit=1024)
        if (receipt.get("authorization") != digest(self.verdict or self.record)
                or receipt.get("decision", {}).get("request_id") != self.binding["request_id"]):
            raise refusal("relay_authorization_reused")

    def invocation_receipt(self):
        from .ledger import state_root
        identity = self.binding["resources"].get("invocation")
        if self.kind != "tool_operation" or not isinstance(identity, str):
            raise PodError("decision_scope_mismatch", "Only an exact operation decision has a use receipt")
        path = state_root(None) / ".relay-authorizations" / (digest("pod-tool-use:" + identity) + ".json")
        use = {"schema": "pod-relay-use/v1", "authorization": digest(self.verdict or self.record),
               "transition": digest({"objective": self.objective, "invocation": identity, "request_id": self.binding["request_id"]})}
        return path, use

    def prior_invocation(self):
        path, use = self.invocation_receipt()
        if not (path.exists() or path.is_symlink()):
            return None
        recorded = bounded_json(path, limit=1024)
        if {key: recorded.get(key) for key in use} != use:
            raise PodError("tool_invocation_reused", "The invocation was consumed with another binding")
        return recorded

    def consume_invocation(self):
        from .ledger import _lock
        from datetime import datetime, timezone
        path, use = self.invocation_receipt()
        with _lock(path):
            recorded = self.prior_invocation()
            if recorded is None:
                recorded = {**use, "used_at": datetime.now(timezone.utc).isoformat()}
                atomic_json(path, recorded, limit=1024)
                self.execute = True
            self.used_at = recorded.get("used_at")

    def recheck_policy(self, snapshot):
        if (snapshot["status"] not in ("valid", "missing")
                or snapshot["decision_generation"] != self.binding["decision_generation"]
                or digest(snapshot["decision_policy"]) != self.binding["decision_policy_revision"]
                or snapshot["tool_permissions"] != self.snapshot["tool_permissions"]
                or self.verdict is not None and snapshot["authorization_verifier"] != self.snapshot["authorization_verifier"]):
            raise PodError("decision_stale", "Decision state, policy or memory changed; ask for a fresh decision")

    def recheck(self, current_state, snapshot):
        if digest(current_state) != self.state:
            raise PodError("decision_stale", "The exact decision state changed")
        self.recheck_policy(snapshot)

    def __copy__(self):
        raise TypeError("Decision capabilities cannot be copied")

    def commit_clock(self):
        from datetime import datetime, timezone
        if self.kernel_at is None:
            self.kernel_at = self.clock().isoformat()
        return self.kernel_at

    def finalize(self, value):
        """Stamp copied rows at the real commit preflight, before encoding/consumption."""
        at = self.commit_clock()
        self.record["recorded_at"] = at
        if self.verdict is None:
            self.record["decided_at"] = at
        def stamp(item):
            if isinstance(item, dict):
                if (item.get("decision_binding") == self.binding
                        or isinstance(item.get("decision_verification"), dict)
                        and item["decision_verification"].get("decision") == self.binding):
                    item["recorded_at"] = at
                    if self.verdict is None:
                        item["decided_at"] = at
                for nested in item.values():
                    stamp(nested)
            elif isinstance(item, list):
                for nested in item:
                    stamp(nested)
        stamp(value)
        self._finalized = True

    def __deepcopy__(self, memo):
        raise TypeError("Decision capabilities cannot be copied")

    @property
    def verified(self):
        return self._seal is _DECISION_SEAL and self.verdict is not None

    def matches(self, value: dict, kind: str) -> bool:
        if self._seal is not _DECISION_SEAL or kind not in self.kinds:
            return False
        if DECISION_STORED & set(value):
            # A requirement source retains its original instruction. The exact
            # approval is the separately copied kernel evidence and actor/channel.
            return (all(value.get(key) == self.record.get(key) for key in DECISION_STORED | {"channel", "relay"})
                    and ("provenance" not in value or value["provenance"] == self.record["provenance"]))
        return _raw_decision(value) == self.raw

    @contextmanager
    def scope(self):
        token = _CURRENT_DECISION.set(self)
        try:
            yield self
        finally:
            _CURRENT_DECISION.reset(token)

    def _receipt_path(self):
        from .ledger import state_root
        key = self.verdict["single_use"] if self.verdict else "pod-decision:" + self.binding["request_id"]
        return state_root(self.project) / ".relay-authorizations" / (digest(key) + ".json")

    def _receipt_facts(self):
        path = self._receipt_path()
        if not (path.exists() or path.is_symlink()):
            return None
        receipt = bounded_json(path, limit=1024)
        facts = receipt.get("decision")
        if not isinstance(facts, dict) or facts.get("request_id") != self.binding["request_id"]:
            raise refusal("relay_authorization_reused")
        normalize_timestamp(facts.get("decided_at"), field="receipt decision time", code="invalid_decision")
        if "recorded_at" in facts:
            normalize_timestamp(facts["recorded_at"], field="receipt commit time", code="invalid_decision")
        return facts

    def consume(self, *, current_state: object, snapshot: dict, result: object, keys: list[str],
                prior: list[dict] = ()) -> None:
        """Called under objective -> config locks after all encoded results fit.

        Receipt consumption and actual state publication must share the config lock.
        The exact transition can recover a missing publication; committed state fails
        the predecessor check and cannot grant the same one-shot decision again.
        """
        from datetime import datetime
        from .ledger import _lock
        if not self._finalized:
            raise PodError("decision_preflight_required", "Finalize complete decision rows before encoded-capacity preflight")
        self.recheck(current_state, snapshot)
        if self.reused:
            self.check_receipt()
            return
        at = datetime.fromisoformat((self.order_at or self.record["decided_at"]).replace("Z", "+00:00"))
        stop = self.kind == "standing_authorization" and self.effect.get("operation") == "revoke"
        for row in prior:
            if not set(keys).intersection(row.get("keys", [])):
                continue
            stamp = row.get("decided_at")
            if stamp is None and (self.actor["kind"], self.record["channel"]) != ("person", "session"):
                raise PodError("decision_order_unknown", "Earlier decision time is unknown; obtain fresh person resolution")
            previous = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp is not None else None
            if previous is not None and (at < previous or at == previous and not stop):
                raise PodError("decision_stale", "A conflicting decision is newer or tied")
        transition = digest({"predecessor": self.state, "binding": self.binding,
                             "result": result, "keys": sorted(keys)})
        receipt = {"schema": "pod-relay-use/v1", "authorization": digest(self.verdict or self.record),
                   "transition": transition, "decision": {"request_id": self.binding["request_id"],
                                                            "decided_at": self.record["decided_at"],
                                                            "recorded_at": self.record["recorded_at"]}}
        path = self._receipt_path()
        with _lock(path):
            if path.exists() or path.is_symlink():
                if bounded_json(path, limit=1024) != receipt:
                    raise refusal("relay_authorization_reused")
            else:
                atomic_json(path, receipt, limit=1024)


def current_decision() -> Decision | None:
    value = _CURRENT_DECISION.get()
    return value if isinstance(value, Decision) and value._seal is _DECISION_SEAL else None


def decision_commit_time() -> str:
    from datetime import datetime, timezone
    capability = current_decision()
    return capability.commit_clock() if capability is not None else datetime.now(timezone.utc).isoformat()


def stored_or_capability(value: dict, *, kind: str, stored: bool) -> dict:
    """A shape check alone can never authorize either new input channel."""
    if stored:
        return validate_decision_record(value)
    capability = current_decision()
    if capability is None or not capability.matches(value, kind):
        raise PodError("decision_capability_required", "This decision needs its exact current mutation capability")
    if DECISION_STORED & set(value):
        return dict(value)
    return {**value, **{key: item for key, item in capability.record.items() if key in DECISION_RAW | DECISION_STORED}}


def decision_boundary(derive):
    """Reuse the real adapter and its factual checks; no alternate mutation route.

    The adapter-owned derive callable builds a complete typed transition from the
    read predecessor and proposed values. Every publication rechecks that exact
    predecessor. Host lookup never holds the personal config lock.
    """
    def decorate(function):
        @wraps(function)
        def call(project, objective, *args, **kwargs):
            from .ledger import read, _empty
            before = read(project, objective) or _empty()
            facts = derive(before, *args, **kwargs)
            if facts is None:
                return function(project, objective, *args, **kwargs)
            if "replay" in facts:
                return {**facts["replay"], "committed": True, "readback": True}
            capability = Decision(project, objective, kind=facts["kind"], effect=facts["effect"],
                                  state=before, decision=facts["decision"], resources={"objective": objective, **facts["resources"]},
                                  kinds=facts.get("effect_kinds"))
            capability.keys = facts["keys"]
            capability.kinds = set(facts.get("kinds", [facts["kind"]]))
            capability.prior = facts.get("prior", [])
            with capability.scope():
                return function(project, objective, *args, **kwargs)
        return call
    return decorate


def validate_order(value: object) -> list:
    if not isinstance(value, list) or len(value) > 512:
        raise PodError("state_unsupported", "Decision chronology is malformed or full")
    seen = set()
    for item in value:
        row = exact(item, {"key", "request_id", "decided_at", "actor"},
                    {"key", "request_id", "decided_at", "actor"}, name="decision chronology")
        bounded_text(row["key"], name="decision_key", limit=256)
        normalize_timestamp(row["decided_at"], field="decision time", code="state_unsupported")
        if row["key"] in seen or not _hash(row["request_id"]) or row["actor"] not in ("person", "agent"):
            raise PodError("state_unsupported", "Decision chronology key is inconsistent")
        seen.add(row["key"])
    return value


def _contains_binding(value, binding):
    if isinstance(value, dict):
        return (value.get("decision_binding") == binding
                or value.get("schema") in (SCHEMA, "pod-tool-proof/v1") and value.get("action") == "decision" and value.get("decision") == binding
                or any(_contains_binding(item, binding) for item in value.values()))
    if isinstance(value, list):
        return any(_contains_binding(item, binding) for item in value)
    return False


def stamp_decision(value: dict) -> None:
    capability = current_decision()
    if capability is None:
        return
    capability.finalize(value)
    if not _contains_binding(value, capability.binding) and not (capability.phase_paths and capability.completion_only):
        raise PodError("decision_capability_unused", "Record the exact decision in its actual mutation before publication")
    index = {row["key"]: row for row in validate_order(value.get("decision_order", []))}
    for key in capability.keys:
        index[key] = {"key": key, "request_id": capability.binding["request_id"],
                      "decided_at": capability.order_at or capability.record["decided_at"], "actor": capability.actor["kind"]}
    value["decision_order"] = validate_order([index[key] for key in sorted(index)])


def publish_decision(path: Path, proposed: dict, publish) -> None:
    """The publication callback executes while config authority is still locked."""
    capability = current_decision()
    if capability is None:
        publish()
        return
    from . import config
    from .ledger import _read
    if capability.phase_paths:
        from copy import deepcopy
        labels = [key for key, target in capability.phase_paths.items() if target == path]
        if len(labels) != 1:
            raise PodError("decision_scope_mismatch", "This write is outside the accepted transition records")
        with config._edit_lock(config.personal_path(capability.project)):
            current = {key: _read(target) for key, target in capability.phase_paths.items()}
            if digest(current) != digest(capability.phase_expected):
                raise PodError("decision_stale", "A phased transition record changed")
            snapshot = config.load(capability.project)
            if not capability.phase_started:
                prior = [{"keys": [row["key"]], "decided_at": row["decided_at"]}
                         for state in current.values() for row in state.get("decision_order", [])]
                capability.consume(current_state=current, snapshot=snapshot, result=proposed, keys=capability.keys,
                                   prior=[*prior, *capability.prior])
            else:
                capability.check_receipt()
                if not capability.completion_only:
                    capability.recheck_policy(snapshot)
            if capability.pending_use is not None:
                capability.consume_invocation()
            publish()
            capability.phase_expected[labels[0]] = deepcopy(proposed)
            capability.phase_started = True
        return
    if capability.kind in ("tool_permission", "tool_operation") and capability.objective is not None:
        with config._edit_lock(config.personal_path(capability.project)):
            before = _read(path)
            snapshot = config.load(capability.project)
            current = {"context": before, "authority": config.decision_state(snapshot)}
            result = {"context": proposed}
            encoded_config = None
            if capability.personal_change is not None:
                document, encoded_config, preserved = config.prepare_decision_document(snapshot, capability.personal_change, capability)
                capability.notice = "" if preserved else "comments not preserved"
                result["authority"] = config.document_decision_state(document)
            prior = [{"keys": [row["key"]], "decided_at": row["decided_at"]} for row in before.get("decision_order", [])]
            capability.consume(current_state=current, snapshot=snapshot, result=result, keys=capability.keys,
                               prior=[*prior, *config._decision_prior(snapshot)])
            if encoded_config is not None:
                config._replace(config.personal_path(capability.project), encoded_config)
            publish()
        return
    before = _read(path)
    prior = [{"keys": [row["key"]], "decided_at": row["decided_at"]}
             for row in validate_order(before.get("decision_order", []))]
    known = {row["key"] for row in before.get("decision_order", [])}
    prior += [row for row in capability.prior if row.get("decided_at") is not None or not set(row["keys"]) <= known]
    with config._edit_lock(config.personal_path(capability.project)):
        snapshot = config.load(capability.project)
        capability.consume(current_state=before, snapshot=snapshot, result=proposed,
                           keys=capability.keys, prior=prior)
        publish()
