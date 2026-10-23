"""Status order for a reserved successor whose coordinator a verified Orca window ended (pod#110, R118, R159).

Completing an incomplete succession claim needs coordinator authority. While the available window step would move
that authority to the caller, status puts the step first; once it has moved, status points to completing the claim,
which finishes with its original decision.
"""
import os
import unittest
from unittest.mock import patch

from pod.internal import run
from pod.ledger import read
from pod.window import primary_action
from tests.test_owner_handoff import HandoffCase
from tests.test_succession import SuccessionCase
from tests.test_window_continuation import WindowCase

CLAIM_STEP = "rerun the same internal succession claim to complete it"


class ReservedSuccessorStatusTests(WindowCase, SuccessionCase):
    def reserved_after_window(self):
        """A reserved claim whose successor coordinator a window ended, read from a new terminal on a later runtime."""
        self.predecessor()
        self.successor()
        decision = self.decide()
        with patch("pod.ledger._write_succession", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.claim(decision)
        self.assertEqual(read(self.project, "successor")["continues"]["state"], "claiming")
        # A verified window ends the successor's coordinator; a new terminal on a later runtime reads status.
        self.terminals["next"] = "stale"
        os.environ["ORCA_TERMINAL_HANDLE"] = "third"
        self.port.runtime = self.port.placement["runtime"] = "runtime-later"
        self.current = {"id": "run-s", "coordinator_handle": "next", "consumer_generation": 1}
        native = self.native_read

        def read_native(argv, **kwargs):
            if argv[:2] == ["orchestration", "run-show"] and argv[3] == "run-s":
                return HandoffCase.native_read(self, argv, **kwargs)
            return native(argv, **kwargs)

        def move(argv, **kwargs):
            self.assertEqual(argv, ["orchestration", "run-use", "--id", "run-s", "--json"])
            self.current.update(coordinator_handle="third", consumer_generation=2)
            return {"runtime": self.port.runtime, "exit": 0, "error": None, "result": {}}

        return decision, read_native, move

    def test_window_step_comes_before_completing_a_reserved_claim(self):
        decision, read_native, move = self.reserved_after_window()
        with patch("pod.orca.read_command", side_effect=read_native), \
                patch("pod.orca.mutate_command", side_effect=move):
            before = self.status(objective="successor")
            window = before["window_continuation"]
            self.assertEqual((window["status"], window["caller"]), ("window_available", {"ready": True}))
            self.assertEqual(before["succession"]["state"], "claim_incomplete")
            self.assertEqual(before["blocker"], "window_available")
            self.assertIn("pod continue", before["next_action"])
            self.assertEqual(before["next_action"], window["next_action"])
            self.assertEqual(before["next_safe_action"], window["next_action"])

            with self.inside():
                result = run("window-continuation", {"project": str(self.project), "objective": "successor",
                                                     "owner": "third"})
            self.assertEqual(result["status"], "continued")

            after = self.status(objective="successor")
            self.assertNotIn("window_continuation", after)
            self.assertEqual(after["succession"]["state"], "claim_incomplete")
            self.assertEqual((after["next_action"], after["next_safe_action"]), (CLAIM_STEP, CLAIM_STEP))

            completed = self.claim(decision, caller="third")
        self.assertEqual(completed["status"], "claimed")
        self.assertNotIn("state", read(self.project, "successor")["continues"])
        link = read(self.project, "objective")["continued_by"]
        self.assertEqual((link["objective"], link["owner"], link["instruction"]),
                         ("successor", decision["successor_owner"], decision["instruction"]))

    def test_an_unavailable_window_keeps_its_lead_task_before_the_claim(self):
        _, read_native, _ = self.reserved_after_window()
        self.host.update(mode="denied", condition="window_not_authorized")
        with patch("pod.orca.read_command", side_effect=read_native):
            result = self.status(objective="successor")
        window = result["window_continuation"]
        self.assertEqual((window["status"], window["condition"]), ("window_unavailable", "window_not_authorized"))
        self.assertEqual(result["succession"]["state"], "claim_incomplete")
        self.assertEqual((result["next_action"], result["next_safe_action"]),
                         (primary_action(window), primary_action(window)))
        self.assertTrue(result["next_action"].startswith(window["next_action"]))
        self.assertNotIn(CLAIM_STEP, result["next_action"])


if __name__ == "__main__":
    unittest.main()
