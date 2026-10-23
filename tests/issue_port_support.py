"""In-memory issue port for tick protocol tests; no production state or remote calls."""

from copy import deepcopy
from unittest.mock import patch

from pod.errors import PodError


class SimulatedInterruption(BaseException):
    """The process died after sending the write and before recording its response."""


class RecordingIssuePort:
    """Record reads and exact write bodies, with one-shot remote outcome switches.

    `other_actor=body` edits immediately before the next write. A tuple
    `("instead", body)` replaces Pod's body after that send; `("before", body)`
    is the explicit equivalent of the string form. Refused and unlanded writes
    leave the other actor's edit in place. `writes` includes every attempted write.
    """

    def __init__(self, *, title="PES: Implement widgets", body="- [ ] **PoD#1",
                 state="OPEN", amendments=None, updated="2026-10-01T00:00:00Z"):
        self.title, self.body, self.state, self.updated = title, body, state, updated
        self.state_reason = None
        self.amendments = deepcopy(amendments) if amendments is not None else {}
        self.calls = []
        self.writes = []
        self.unreadable = False
        self.refuse_next = None
        self.lose_next = None
        self.interrupt_next = False
        self.other_actor = None
        self.readback_override = None
        self._readback_pending = False

    def _read(self):
        if self.unreadable:
            self.unreadable = False
            raise PodError("issue_access_unavailable", "Simulated inaccessible issue")

    def issue(self, *, repository, number):
        self.calls.append(("issue", repository, number))
        self._read()
        body = self.body
        if self._readback_pending:
            self._readback_pending = False
            if self.readback_override is not None:
                body, self.readback_override = self.readback_override, None
        return {"number": number, "title": self.title, "body": body, "state": self.state, "stateReason": self.state_reason,
                "url": f"https://github.com/{repository}/issues/{number}", "updatedAt": self.updated}

    def amendment(self, *, repository, number, comment):
        self.calls.append(("amendment", repository, number, comment))
        self._read()
        value = self.amendments.get(comment, "Decision")
        if isinstance(value, dict):
            return deepcopy(value)
        return {"id": comment, "body": value,
                "url": f"https://github.com/{repository}/issues/{number}#issuecomment-{comment}",
                "updatedAt": self.updated}

    def issue_body_write(self, *, source, body):
        self.calls.append(("issue_body_write", deepcopy(source), body))
        self.writes.append(body)
        refusal, self.refuse_next = self.refuse_next, None
        lost, self.lose_next = self.lose_next, None
        interrupted, self.interrupt_next = self.interrupt_next, False
        actor, self.other_actor = self.other_actor, None
        if isinstance(actor, str):
            actor = ("before", actor)
        if actor is not None:
            self.body = actor[1]
        if refusal is not None:
            return {"result": "refused", "status": refusal, "message": "Simulated GitHub refusal"}
        if lost != "not_landed":
            self.body = body
        if actor is not None and actor[0] == "instead":
            self.body = actor[1]
        if interrupted:
            raise SimulatedInterruption()
        if lost is not None:
            raise PodError("remote_effect_uncertain", "Simulated lost issue write response")
        self._readback_pending = True
        return {"result": "accepted", "status": 200}


def patch_issue_port(port: RecordingIssuePort):
    """Patch every dynamically constructed production issue port with this instance."""
    return patch("pod.github.GhPort", return_value=port)
