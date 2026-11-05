"""R212: the host's read-time process-exit verdict for settled Dispatches Orca cannot observe.

Admission asks the personal `authorization_verifier` (action `process_exit`) during discovery, before the end fence,
about the settled local Dispatches a query would otherwise keep holding, and uses each verdict only within that one
call. Nothing is stored and a verdict authorizes nothing. Pod never infers exit from Orca facts: the host's own
process-table scan is the only source, and every fault keeps the hold.
"""
from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone

from .errors import PodError
from .util import normalize_timestamp

ACTION = "process_exit"
# Ids per request, from the host verifier's own lookup bound. It resolves a request of n references with n + 1 native
# reads, 4 at a time, inside one window shared by the request, so the trailing sorted references of a larger request
# can be cut and answer `known: false`, always fail-closed. 7 references take exactly two waves of reads, the largest
# size with a whole wave of margin; 8 need a third wave, and live a request of 8 sometimes lost its last reference
# (requests of 16 and 24 lost their last 9 and 13). The largest valid verdict for 7 ids, every string at its bound and
# every count at 2^31-1, is 2,480 bytes compact and 3,821 bytes indented by four; the wire contract's 24 ids (7,393
# and 11,845 bytes) also fit the 16 KiB transport bound.
K = 7
R = 14  # requests per admission call, so at most R * K = 98 ids
# The verifier's concurrent-request allowance: after the first request, which runs alone, at most this many requests
# are in flight at once, in rounds that each start only when every request of the previous round has answered.
# Concurrent requests slow each other's bounded lookups, and the only live sample of three at once answered
# unverified. Raise it to 3 only after an installed observation of three full-size requests at once that all
# verified, and never above 3.
IN_FLIGHT = 2
TIMEOUT_S = 3.0  # one request's whole invocation; the other verifier actions keep relay.TIMEOUT_S
# The work left after the requests (projection, recheck and a batch's end fence) measured 3.6 s for a single query and
# 3.9 s for a batch over 2,939 Dispatches. The call's own discovery read time scales the reserve as the fleet grows.
RESERVE_S = 4.0
RESERVE_SCALE = 1.25
SKEW_S = 5.0  # a scan time outside the request window, widened by this much on each side, is stale

IDENTITY = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")
METHOD = re.compile(r"[\x20\x21\x23-\x5b\x5d-\x7e]{1,128}\Z")  # printable ASCII without '"' or '\': never escaped
STATES = ("absent", "present", "partial")
CHECKED = ("terminal_handle", "pane", "process_incarnation", "worktree")
VERDICT_FIELDS = frozenset({"schema", "action", "verified", "host", "runtime", "method", "processes", "observed_at",
                            "dispatches"})
FACT_FIELDS = ("host", "runtime", "method", "processes", "observed_at")
ENTRY_FIELDS = frozenset({"reference", "known", "state", "terminal_handle", "carriers", "checked"})
MAX_COUNT = 2 ** 31 - 1
MAX_OBSERVED = 40

# Request-level codes: every id of that request holds.
FAILED, TIMEOUT, MALFORMED = "host_exit_failed", "host_exit_timeout", "host_exit_malformed"
MISMATCH, UNVERIFIED = "host_exit_mismatch", "host_exit_unverified"
FOREIGN_HOST, FOREIGN_RUNTIME, STALE = "host_exit_foreign_host", "host_exit_foreign_runtime", "host_exit_stale"
# Transport and contract faults: the verifier did not answer this action at all, whatever ids it was given, so the
# call starts no further round (pod#219, pod#227). The other request-level codes are answers under the contract that
# depend on the group's host or the request's own time, and never stop a call.
STOPPING = frozenset({FAILED, TIMEOUT, MALFORMED, MISMATCH})
# Entry-level codes: only that id holds.
HOST_UNKNOWN, IDENTITY_MISMATCH = "host_exit_host_unknown", "host_exit_identity_mismatch"
PRESENT, PARTIAL = "host_exit_present", "host_exit_partial"
# Call-level codes.
CALL_LIMIT, DEADLINE, UNCOVERED = "host_exit_call_limit", "host_exit_deadline", "host_exit_query_uncovered"
UNAVAILABLE = "host_exit_verifier_unavailable"


def eligible_identity(value: object) -> bool:
    """Whether a native id, handle or host id fits the request alphabet; anything else is never asked."""
    return isinstance(value, str) and IDENTITY.fullmatch(value) is not None


def request(ids: list[str]) -> dict:
    from .relay import SCHEMA
    return {"schema": SCHEMA, "action": ACTION, "references": list(ids)}


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_COUNT


def _entry(value: object, reference: str) -> dict:
    if not isinstance(value, dict) or set(value) != ENTRY_FIELDS or value["reference"] != reference:
        raise ValueError("entry")
    if type(value["known"]) is not bool:
        raise ValueError("known")
    if not value["known"]:
        if any(value[key] is not None for key in ("state", "terminal_handle", "carriers", "checked")):
            raise ValueError("unknown entry facts")
        return dict(value)
    checked = value["checked"]
    if (value["state"] not in STATES or not eligible_identity(value["terminal_handle"])
            or not isinstance(checked, list) or not 1 <= len(checked) <= len(CHECKED)
            or len(set(map(str, checked))) != len(checked) or any(kind not in CHECKED for kind in checked)
            or "terminal_handle" not in checked):
        raise ValueError("entry facts")
    carriers = value["carriers"]
    if value["state"] == "absent" and carriers != 0 or value["state"] == "present" and not (_count(carriers) and carriers >= 1):
        raise ValueError("carriers")
    if value["state"] == "partial" and carriers not in (None, 0):
        raise ValueError("partial carriers")
    if carriers is not None and not _count(carriers):
        raise ValueError("carriers bound")
    return dict(value)


def decode(value: object, ids: list[str]) -> dict:
    """The verdict's exact shape for the ids asked, in order and each exactly once; ValueError for anything else."""
    if not isinstance(value, dict) or set(value) != VERDICT_FIELDS:
        raise ValueError("verdict fields")
    if not isinstance(value["schema"], str) or not isinstance(value["action"], str) or type(value["verified"]) is not bool:
        raise ValueError("verdict header")
    if not value["verified"]:
        if any(value[key] is not None for key in (*FACT_FIELDS, "dispatches")):
            raise ValueError("unverified facts")
        return dict(value)
    if (not eligible_identity(value["host"]) or not eligible_identity(value["runtime"])
            or not isinstance(value["method"], str) or METHOD.fullmatch(value["method"]) is None
            or not _count(value["processes"]) or value["processes"] < 1
            or not isinstance(value["observed_at"], str) or len(value["observed_at"]) > MAX_OBSERVED):
        raise ValueError("verdict facts")
    try:
        observed = normalize_timestamp(value["observed_at"], field="scan time", code=MALFORMED)
    except (PodError, OverflowError, ValueError):
        # A calendar-boundary time such as year 1 at +01:00 parses but cannot be expressed in UTC.
        raise ValueError("observed_at") from None
    entries = value["dispatches"]
    if not isinstance(entries, list) or len(entries) != len(ids):
        raise ValueError("entries")  # a missing or extra id
    return {**value, "observed_at": observed,
            "dispatches": [_entry(entry, reference) for entry, reference in zip(entries, ids)]}


def _output(command: list[str], payload: dict) -> bytes:
    """This action's own bounded transport: relay's wire rules (no shell, `/`, 16 KiB of stdout, stderr discarded,
    the whole process group killed) with this action's TIMEOUT_S. relay._output stays byte-identical for every older
    action's wire compatibility, so it is not reused here; its refusal codes and stdout bound are."""
    from .relay import MAX_VERDICT, refusal
    process = None
    try:
        deadline = time.monotonic() + TIMEOUT_S
        process = subprocess.Popen(command, cwd="/", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True,
                                   env={k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")})
        pending = memoryview(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode() + b"\n")
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
            try:  # Also stop descendants retaining the pipe after the command itself exited.
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdin.close()
            process.stdout.close()


def ask(command: list[str], ids: list[str], *, clock=None) -> dict:
    """One bounded request for `ids`: its decoded verdict, or the request-level code that holds every id in it.

    `sent` and `received` are Pod's wall-clock bracket of the invocation, for the conservative stale check."""
    from . import relay
    now = clock or (lambda: datetime.now(timezone.utc))
    sent = now()
    try:
        raw = _output(command, request(ids))
    except PodError as exc:
        return {"code": {"relay_verifier_timeout": TIMEOUT, "relay_verifier_malformed": MALFORMED}.get(exc.code, FAILED),
                "verdict": None, "sent": sent, "received": now()}
    received = now()
    try:
        verdict = decode(json.loads(raw.decode("utf-8"), object_pairs_hook=relay._unique), ids)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return {"code": MALFORMED, "verdict": None, "sent": sent, "received": received}
    return {"code": None, "verdict": verdict, "sent": sent, "received": received}


def judge(answer: dict, *, host: str, runtime: str) -> str | None:
    """The request-level code for one answered request, or None when its entries may be used."""
    from .relay import SCHEMA
    if answer["code"] is not None:
        return answer["code"]
    verdict = answer["verdict"]
    if verdict["schema"] != SCHEMA or verdict["action"] != ACTION:
        return MISMATCH
    if not verdict["verified"]:
        return UNVERIFIED
    if verdict["host"] != host:
        return FOREIGN_HOST
    if verdict["runtime"] != runtime:
        return FOREIGN_RUNTIME
    observed = datetime.fromisoformat(verdict["observed_at"].replace("Z", "+00:00"))
    skew = timedelta(seconds=SKEW_S)
    if not answer["sent"] - skew <= observed <= answer["received"] + skew:
        return STALE
    return None


def entry_code(entry: dict, handle: str) -> str | None:
    """The entry-level code for one id of a usable verdict, or None when the host found no carrier."""
    if not entry["known"]:
        return HOST_UNKNOWN
    if entry["terminal_handle"] != handle:
        return IDENTITY_MISMATCH
    return {"present": PRESENT, "partial": PARTIAL}.get(entry["state"])
