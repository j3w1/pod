"""Unreported kept tabs when a real record or route no longer accepts reports."""
import os

from pod.errors import PodError
from pod.ledger import read
from tests.test_historical_retirement import RetirementCase
from tests.test_succession import SuccessionCase
from tests.test_worker_tabs import TabCase


class EndedTabAssertions:
    def assert_tab_wording(self, case, *, ended=None):
        before = self.saved()
        view = self.status()
        row = view["worker_tabs"][0]
        ready = view["readiness"]
        self.assertEqual((row["row"], row["state"]),
                         (case, "releasable" if case == "owned" else "kept"))
        if case == "owned":
            self.assertEqual(row["command"], ["orca", *self.release_argv])
            self.assertIn("Release still owed", row["wording"])
            self.assertNotIn("before closure", row["wording"])
        else:
            self.assertIsNone(row["command"])
            self.assertIn("Do not reuse", row["wording"])
            self.assertIn("The owner's standing close authorization does not cover this tab because its report "
                          "was not recorded; name it in the final report for the owner's decision.", row["wording"])
            self.assertNotIn("once its report is recorded", row["wording"])
        self.assertIn(row["wording"], ready["summary"])
        # Ended readiness has no finalization; the map still exposes its own
        # finalization projection, which must carry the same tab guidance.
        if ended:
            self.assertEqual(ready["state"], ended)
            self.assertIsNone(ready["finalization"])
        else:
            self.assertIn(row["wording"], ready["finalization"]["next_action"])
        self.assertIn(row["wording"], self.op("map")["finalization"]["next_action"])
        self.assertEqual(before, self.saved())

    def assert_report_refused_without_write(self, code):
        before = self.saved()
        os.environ["ORCA_TERMINAL_HANDLE"] = "owner"
        with self.assertRaises(PodError) as caught:
            self.report(self.admission, self.frozen)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(before, self.saved())

    def discard(self):
        self.op("checkpoint", value=self.core(obligations=self.waiting_rows(), worktree=self.binding(),
                dispositions=[{"admission": self.admission["admission_id"], "discarded": True,
                               "reason": "superseded"}]))


class ContinuedTabTests(EndedTabAssertions, TabCase, SuccessionCase):
    def continued(self, case, *, discarded=False):
        self.establish()
        self.op("checkpoint", value=self.core(obligations=self.stored(), worktree=self.binding()))
        self.settle(self.admission)
        self.tab_case = case
        if discarded:
            self.discard()
        # Use the real succession path without a runtime change, as in the
        # audit probe: the predecessor's native assignment remains readable.
        self.terminals["owner"] = "exited"
        self.successor()
        verdict = self.claim()
        self.assertEqual(verdict["status"], "claim_available")
        self.assertIn(self.admission["admission_id"], verdict["unreported"])
        self.claim(self.decide())
        state = read(self.project, "objective")
        self.assertEqual(state["continued_by"]["objective"], "successor")
        self.assertIsNone(state["checkpoint"].get("closure"))
        self.assertIsNone(state["admissions"][self.admission["admission_id"]]["report"])
        self.assert_tab_wording(case, ended="continued")
        self.assert_report_refused_without_write("objective_continued")

    def test_unreported_user_owned_predecessor_names_owner_decision(self):
        self.continued("user_owned")

    def test_discarded_user_owned_predecessor_names_owner_decision(self):
        self.continued("user_owned", discarded=True)

    def test_unreported_external_predecessor_names_owner_decision(self):
        self.continued("external")

    def test_discarded_external_predecessor_names_owner_decision(self):
        self.continued("external", discarded=True)

    def test_owned_predecessor_release_is_still_owed_without_closure_timing(self):
        self.continued("owned")


class RetiredTabTests(EndedTabAssertions, TabCase, RetirementCase):
    def retired(self, case, *, discarded=False):
        runtime = self.port.runtime
        self.delivered(unknown_start=False)
        # Lose the coordinator without changing the native assignment runtime.
        self.port.runtime = self.port.placement["runtime"] = runtime
        os.environ["ORCA_TERMINAL_HANDLE"] = "owner"
        self.tab_case = case
        if discarded:
            self.discard()
        self.terminals["owner"] = "exited"
        os.environ["ORCA_TERMINAL_HANDLE"] = "next"
        self.assertEqual(self.retire(self.decide())["status"], "retired")
        state = read(self.project, "objective")
        self.assertIsNotNone(state["historical_retirement"])
        self.assertIsNone(state["checkpoint"].get("closure"))
        self.assertIsNone(state["admissions"][self.admission["admission_id"]]["report"])
        self.assert_tab_wording(case, ended="retired")
        self.assert_report_refused_without_write("objective_retired")

    def test_unreported_user_owned_retired_record_names_owner_decision(self):
        self.retired("user_owned")

    def test_discarded_user_owned_retired_record_names_owner_decision(self):
        self.retired("user_owned", discarded=True)

    def test_unreported_external_retired_record_names_owner_decision(self):
        self.retired("external")

    def test_discarded_external_retired_record_names_owner_decision(self):
        self.retired("external", discarded=True)

    def test_owned_retired_record_release_is_still_owed_without_closure_timing(self):
        self.retired("owned")


class RouteMismatchTabTests(EndedTabAssertions, TabCase):
    def route_mismatch(self, case):
        self.intake(self.sub("S"))
        self.frozen = self.packet(["S"], purpose="Repair parser", boundary={"paths": ["src"]})
        self.admission = self.start("implement", self.frozen)["admission"]
        self.settle(self.admission)
        self.tab_case = case
        self.port.effective = {"agent": "codex", "model": "gpt-6-luna", "effort": "medium"}
        with self.assertRaises(PodError) as caught:
            self.report(self.admission, self.frozen)
        self.assertEqual(caught.exception.code, "route_mismatch")
        admission = read(self.project, "objective")["admissions"][self.admission["admission_id"]]
        self.assertTrue(admission["route_decision"]["route_mismatch"])
        self.assertIsNone(admission["report"])
        self.assert_tab_wording(case)
        self.write(self.waiting_rows(), dispositions=[{"admission": self.admission["admission_id"],
                   "discarded": True, "reason": "route mismatch"}])
        self.assertIsNone(read(self.project, "objective")["checkpoint"].get("closure"))
        self.assert_tab_wording(case)
        before = read(self.project, "objective")
        with self.assertRaises(PodError) as caught:
            self.report(self.admission, self.frozen)
        self.assertEqual(caught.exception.code, "route_mismatch")
        after = read(self.project, "objective")
        # This existing refusal refreshes the recorded route decision, as in
        # 0.15.0; it still records no report and preserves the disposition/map.
        self.assertEqual(after["checkpoint"], before["checkpoint"])
        admission = after["admissions"][self.admission["admission_id"]]
        self.assertTrue(admission["route_decision"]["route_mismatch"])
        self.assertIsNone(admission["report"])
        self.assertEqual(admission["disposition"],
                         before["admissions"][self.admission["admission_id"]]["disposition"])
        self.write(self.finished_rows(), close=True)
        self.assert_tab_wording(case)

    def test_open_user_owned_route_mismatch_names_owner_decision_before_and_after_discard(self):
        self.route_mismatch("user_owned")

    def test_open_external_route_mismatch_names_owner_decision_before_and_after_discard(self):
        self.route_mismatch("external")
