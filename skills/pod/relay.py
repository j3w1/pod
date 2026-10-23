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
