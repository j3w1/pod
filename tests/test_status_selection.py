"""Objective selection and derived, read-only public status."""

from __future__ import annotations

from unittest.mock import patch

from pod.cli import execute, parser
from pod.ledger import objective_root
from pod.operations import OrcaPort
from tests.kernel_support import KernelCase


class StatusSelectionTests(KernelCase):
    def status(self, *args, workers=None):
        listed = workers or {"runtime": "runtime", "scope": {"source": "flag", "run": "run"},
                             "workers": [], "complete": True}
        with patch("pod.cli.worker_rows", return_value=listed), \
             patch("pod.cli.current_run", return_value={"run": {"id": "run"}, "runtime": "runtime"}), \
             patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            return execute(parser().parse_args(["status", "--json", *args]), self.project)

    def test_shared_run_requires_objective_choice_then_selects_without_writes(self):
        self.intake()
        self.write([self.criterion()], objective="second", governance={"base_ref": "target"})
        paths = [objective_root(self.project, key) / "context.json" for key in ("objective", "second")]
        before = [path.read_bytes() for path in paths]
        ambiguous = self.status("--run", "run")
        self.assertEqual(ambiguous["status"], "selection_required")
        self.assertEqual({choice["objective"] for choice in ambiguous["choices"]}, {"objective", "second"})
        self.assertEqual(ambiguous["next_safe_action"], "rerun with --objective ID")
        selected = self.status("--run", "run", "--objective", "objective")
        self.assertEqual(selected["objective"], "objective")
        self.assertEqual(selected["assignments"], {"active": [], "settled": []})
        self.assertIn("coordinator: O1", selected["progress"][0])
        self.assertEqual(selected["recorded_next_action"], "continue")
        self.assertIn("coordinator: O1", selected["next_action"])
        mismatched = self.status("--run", "other", "--objective", "objective")
        self.assertEqual(mismatched["blocker"], "objective_run_mismatch")
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_gates_and_hosted_progress_are_derived(self):
        self.intake(remaining_gates=[{"kind": "hosted_ci", "workflow": "ci.yml"}])
        status = self.status("--objective", "objective")
        self.assertEqual(len(status["gates"]["remaining"]), 1)
        self.assertTrue(any("waiting for hosted CI (ci.yml on " in line for line in status["progress"]))

    def test_retained_terminal_requires_live_native_readback_of_settled_attempt(self):
        self.intake()
        frozen = self.packet(["O1"])
        admission = self.start("work", frozen)["admission"]
        self.settle(admission)
        binding = admission["native_binding"]
        worker = {"dispatchId": binding["dispatchId"], "agentTerminalHandle": binding["terminalHandle"],
                  "terminalState": "reclaimable", "projection": {"liveness": {"verdict": "live"}}}
        observed = self.status("--objective", "objective", workers={
            "runtime": "runtime", "scope": {"source": "flag", "run": "run"},
            "workers": [worker], "complete": True})
        self.assertEqual(observed["assignments"]["active"], [])
        self.assertEqual(observed["assignments"]["settled"][0]["admission"], admission["admission_id"])
        self.assertEqual(observed["retained_terminals"][0]["terminal"], binding["terminalHandle"])
        worker["projection"]["liveness"]["verdict"] = "unverifiable"
        hidden = self.status("--objective", "objective", workers={
            "runtime": "runtime", "scope": {"source": "flag", "run": "run"},
            "workers": [worker], "complete": True})
        self.assertEqual(hidden["retained_terminals"], [])


if __name__ == "__main__":
    import unittest
    unittest.main()
