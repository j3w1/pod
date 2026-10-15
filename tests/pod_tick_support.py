"""Disposable production-entry fixtures; only GitHub and native ports are replaced."""

from copy import deepcopy
from unittest.mock import patch

from pod import internal
from pod.errors import PodError
from pod.github import issue_intake
from pod.ledger import objective_root, read
from pod.status import status
from tests.issue41_support import ProductionCase
from tests.issue_port_support import RecordingIssuePort, patch_issue_port


class TickCase(ProductionCase):
    locator = "https://github.com/acme/example/issues/7"

    def setUp(self):
        super().setUp()
        self.issue = RecordingIssuePort()
        replacement = patch_issue_port(self.issue)
        replacement.start(); self.addCleanup(replacement.stop)
        # No test in this family may escape to a real process-backed remote.
        for target in ("pod.github._run", "pod.operations.mutate_command"):
            guard = patch(target, side_effect=AssertionError("test escaped its recording ports"))
            guard.start(); self.addCleanup(guard.stop)

    def bind(self, body="- [ ] **PoD#1", *rows, criteria=None, amendments=None):
        self.issue.body = body
        source = issue_intake(self.project, self.locator, port=self.issue, amendments=amendments)["source"]
        self.issue.calls.clear()
        return self.intake(*rows, objective_source=source, **({"criteria": criteria} if criteria else {}))

    def rebind(self):
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        return self.write(objective_source=source)

    def ticks(self):
        calls, before = deepcopy(self.issue.calls), self.saved()
        with patch("pod.github.GhPort", side_effect=AssertionError("status fetched GitHub")):
            view = status(self.project, None, objective="objective")["pod_ticks"]
        self.assertEqual(self.issue.calls, calls)
        self.assertEqual(self.saved(), before)
        return view

    def line(self, number=1):
        return next(row for row in self.ticks()["lines"] if row["pod"] == number)

    def tick(self, number=1):
        return self.op("pod-tick", pod=number)

    def recover_tick(self, number=1):
        """Advance only the injected intent clock; never wait for time to pass."""
        from pod.github import MUTATION_TIMEOUT_S
        from pod.pod_ticks import INTENT_MARGIN_S
        request = read(self.project, "objective")["pod_ticks"]["requests"][str(number)]
        with patch("pod.pod_ticks.time", return_value=request["intent_at"] + MUTATION_TIMEOUT_S + INTENT_MARGIN_S):
            return self.tick(number)

    def saved(self):
        root = objective_root(self.project, "objective")
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
                if path.is_file() and path.name != ".lock"}

    def prove(self, *, close=False, **fields):
        rows = self.stored()
        for row in rows:
            row.pop("executor", None); row.pop("wait", None)
            row.update(state="satisfied", evidence=[self.proof(row["id"])])
        return self.write(rows, **({"close": True} if close else {}), **fields)

    def lapse(self, *, withdraw=False, remove_mapping=False, close=False):
        rows = self.stored()
        row = rows[0]
        row.pop("evidence", None); row.pop("receipts", None); row.pop("reuse", None)
        fields = {}
        if withdraw:
            row.update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "scope removed"})
            fields["revision_authority"] = {"provenance": "user_direct", "instruction": "drop PoD#1"}
        elif remove_mapping:
            # D2/D7 are forward-compatible table cases. Today's kernel never
            # drops criteria or changes their source. Replace only the mapping
            # lookup, preserving the real decision/protocol/record/closure code.
            replacement = patch("pod.pod_ticks.mapping", return_value={})
            replacement.start(); self.addCleanup(replacement.stop)
            return
        else:
            row.update(state="active", executor="coordinator")
        if close:
            fields["close"] = True
        return self.write(rows, **fields)

    def packet(self, serves, **fields):
        fields.setdefault("objective_source", read(self.project, "objective")["checkpoint"]["objective_source"])
        return super().packet(serves, **fields)

    def ack(self, result, operation="checkpoint", **fields):
        before, calls = self.saved(), deepcopy(self.issue.calls)
        ack = internal.acknowledge(operation, {"project": str(self.project), "objective": "objective", **fields}, result)
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.issue.calls, calls)
        return ack

    def failure(self, code, callback, *args, **kwargs):
        with self.assertRaises(PodError) as caught:
            callback(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception
