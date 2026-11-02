"""R212 fixtures: a real verifier process answering `process_exit` from a control file, and stuck Dispatch rows.

Every id, handle and host is synthetic, in the shape Orca reports; no live state is read."""

from datetime import datetime, timezone
import json
import sys

import yaml

from pod.config import personal_path
from tests import test_admission_query as base

VERIFIER = '''import json, os, signal, sys
from datetime import datetime, timezone
from pathlib import Path
control = json.loads(Path(sys.argv[1]).read_text())
request = json.load(sys.stdin)
with Path(sys.argv[2]).open("a") as log:
    log.write(json.dumps(request) + "\\n")
references = request["references"]
mode = control.get("faults", {}).get(references[0], control.get("mode"))
if mode == "fail":
    print("verifier-private-sentinel", file=sys.stderr)
    sys.exit(3)
if mode == "timeout":
    signal.pause()
if mode == "oversized":
    os.write(1, b"x" * 32768)
    signal.pause()
if mode == "invalid_json":
    print("verifier-private-sentinel")
    sys.exit()
entries, handles = control.get("entries", {}), control.get("handles", {})
def entry(reference):
    value = entries.get(reference, {})
    if value.get("known") is False:
        return {"reference": reference, "known": False, "state": None, "terminal_handle": None, "carriers": None,
                "checked": None}
    state = value.get("state", "absent")
    return {"reference": reference, "known": True, "state": state,
            "terminal_handle": value.get("terminal_handle", handles.get(reference, reference + "-terminal")),
            "carriers": value.get("carriers", {"absent": 0, "present": 1, "partial": None}[state]),
            "checked": value.get("checked", ["terminal_handle", "pane"])}
verdict = {"schema": request["schema"], "action": request["action"], "verified": True,
           "host": control.get("host", "local"), "runtime": control.get("runtime", "runtime"),
           "method": control.get("method", "fixture whole process-table scan"), "processes": control.get("processes", 42),
           "observed_at": control.get("observed_at") or datetime.now(timezone.utc).isoformat(),
           "dispatches": [entry(reference) for reference in references]}
verdict.update(control.get("override", {}))
if mode == "overflow":
    verdict["observed_at"] = control.get("overflow_at", "0001-01-01T00:00:00+01:00")
if mode == "unverified":
    verdict.update(verified=False, host=None, runtime=None, method=None, processes=None, observed_at=None,
                   dispatches=None)
elif mode == "missing":
    verdict["dispatches"].pop()
elif mode == "extra":
    verdict["dispatches"].append(dict(verdict["dispatches"][0], reference="extra-dispatch"))
elif mode == "repeated":
    verdict["dispatches"][-1] = dict(verdict["dispatches"][0])
elif mode == "reordered":
    verdict["dispatches"].reverse()
text = json.dumps(verdict, indent=control.get("indent"))
if mode == "duplicate":
    text = text.rstrip()[:-1] + ', "verified": true}'
print(text)
'''


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class VerifierMixin:
    """A real `authorization_verifier` process in the personal config, answering from `self.control`."""

    def setup_verifier(self, configure=True):
        self.control = self.root / "process-exit-control.json"
        self.log = self.root / "process-exit-requests.jsonl"
        script = self.root / "process_exit_verifier.py"
        script.write_text(VERIFIER)
        self.command = [sys.executable, str(script), str(self.control), str(self.log)]
        self.answer()
        if configure:
            self.configure(self.command)

    def configure(self, command):
        path = personal_path(self.project)
        value = yaml.safe_load(path.read_text())
        if command is None:
            value.pop("authorization_verifier", None)
        else:
            value["authorization_verifier"] = command
        path.write_text(yaml.safe_dump(value))

    def answer(self, **control):
        """The verifier's next answers: `entries` by Dispatch, `mode`, per-request `faults` keyed by the first id, and
        `override` fields. The scan time defaults to this test's own start, so repeated calls answer identically."""
        self.control.write_text(json.dumps({"observed_at": self.scan_time, **control}))

    @property
    def scan_time(self):
        if not hasattr(self, "_scan_time"):
            self._scan_time = now()
        return self._scan_time

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def asked(self):
        return [reference for request in self.requests() for reference in request["references"]]


def stuck(case, dispatch="stuck", *, path=None, run="lead-run", handle=None, host=("local", "local"),
          state=("succeeded", "completed"), verdict="unverifiable", terminal_state="retained"):
    """A settled Dispatch Orca cannot observe: liveness `unverifiable`/`missing_status`, no observation times."""
    row = base.ReviewCorrectionTests.writer(case, path=path or case.project, verdict=verdict, dispatch=dispatch)
    row.update(runId=run, workerState=state[0], dispatchStatus=state[1], terminalState=terminal_state,
               agentTerminalHandle=handle or dispatch + "-terminal")
    row["resource"].update(id="wtr-" + dispatch, terminalHandle=row["agentTerminalHandle"],
                           endpointId="runtime", endpointIncarnation="pty-" + dispatch + ":incarnation-1")
    row["projection"].update(runId=run, host=None if host is None else {"kind": host[0], "id": host[1]},
                             evidence={"durable": True, "liveStatus": "missing", "lastObservedAt": None})
    row["projection"]["liveness"]["reason"] = "missing_status"
    return row


def held(result, dispatch):
    """Whether the Dispatch still holds this result: a claim or an unknown names it."""
    return (any(row.get("dispatch") == dispatch for row in result["claims"])
            or any(row.get("dispatch") == dispatch for row in result["unknowns"]))


def exit_codes(result):
    """Each held candidate's R212 code, from its unknown."""
    return {row["dispatch"]: row["host_exit"]["code"] for row in result["unknowns"] if "host_exit" in row}
