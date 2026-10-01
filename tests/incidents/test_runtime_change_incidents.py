"""Sanitized Orca runtime-change incidents from the #28 evidence review (R15, R17).

Only the shape is kept: counts of finished Dispatches and consumed reports, and which
current-Run identities changed. No runtime ids, terminal handles or private content.
"""

from __future__ import annotations

import unittest

from pod.errors import PodError
from pod.ledger import read
from pod.status import status as pod_status
from tests import test_runtime_continuity as continuity
from tests import test_status_unverified as unverified


class R15StatusIncident(unverified.UnverifiedStatusCase):
    """R15: 5 finished Dispatches, 4 with consumed reports, then the Orca runtime changed."""

    def test_finished_rows_after_a_runtime_change_are_unverified_not_active(self):
        admissions = self.finished(5, 4)
        state = read(self.project, "objective")
        self.assertEqual(sum(row.get("report") is not None for row in state["admissions"].values()), 4)
        self.port.runtime = "runtime-after-update"
        result, rendered = self.observe(runtime="runtime-after-update")
        self.assertEqual(len(result["assignments"]["active"]), 0)
        self.assertEqual(len(result["assignments"]["unverified"]), 5)
        self.assertEqual({row["admission"] for row in result["assignments"]["unverified"]},
                         {row["admission_id"] for row in admissions})
        self.assertIn("0 active, 0 settled, 5 unverified", rendered)
        self.assertNotRegex(rendered, r"waiting for .*delivery")


class R17ProvenIncident(continuity.ContinuityCase):
    """R17 as a 0.6.7 objective: after an Orca update only the runtime id changed.

    The post-update checkpoint recorded the generation, so the same Run, coordinator and
    generation, with every bound worker readable, prove continuity.
    """

    def test_same_run_coordinator_and_generation_rebind_as_proven(self):
        admissions = self.started(2)
        self.change_runtime("runtime-after-update")
        result = self.checkpoint_op()
        for admission in admissions:  # they finish after the rebind; settlement reads work again
            self.settle(admission)
        [entry] = result["runtime_continuity"]
        self.assertEqual(entry["provenance"], "automatic")
        self.assertEqual(sorted(entry["verified"]["admissions"]),
                         sorted(row["admission_id"] for row in admissions))
        observed = pod_status(self.project, None, objective="objective",
                              current_run_fn=lambda: {"run": {"id": "run"}, "runtime": "runtime-after-update"},
                              worker_rows_fn=lambda _run: {"runtime": "runtime-after-update", "workers": [],
                                                           "scope": None, "complete": True})
        self.assertEqual(observed["native_settlement"], "observed")
        self.assertEqual(len(observed["assignments"]["settled"]), 2)
        self.assertEqual(observed["runtime_continuity"]["history"], [entry])


class R15DisprovenIncident(continuity.ContinuityCase):
    """R15: the runtime changed and so did the coordinator terminal; that disproves continuity."""

    def test_changed_coordinator_fails_closed_without_a_rebind(self):
        self.started()
        self.change_runtime("runtime-after-change")
        self.port.run_binding = {**self.port.run_binding, "coordinator_handle": "successor-terminal"}
        before = self.snapshot()
        with self.assertRaises(PodError) as caught:
            self.checkpoint_op()
        self.assertEqual(caught.exception.code, "native_assignment_unverified")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.history(), [])


if __name__ == "__main__":
    unittest.main()
