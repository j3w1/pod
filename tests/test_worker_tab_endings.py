"""Unreported kept tabs on ended records or with a recorded route mismatch."""
import os
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import read
from pod.operations import OrcaPort
from pod.util import route_hold
from tests.test_historical_retirement import RetirementCase
from tests.test_succession import SuccessionCase
from tests.test_worker_tabs import TabCase


class EndedTabAssertions:
    def assert_tab_wording(self, case, *, ended=None, route_mismatch=False):
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
            state = read(self.project, "objective")
            flagged = any(item["route_decision"]["route_mismatch"] for item in state["admissions"].values())
            if route_mismatch or flagged:
                self.assertIn("Project lead:", row["wording"])
                self.assertNotIn("owner", row["wording"].lower())
                if ended or state["checkpoint"].get("closure"):
                    self.assertIn("this ended record accepts no settlement write and supplies no close command", row["wording"])
                    self.assertTrue(view["route_holds"])
                    for action in (view["next_safe_action"], view["next_action"]):
                        self.assertIn("Project lead:", action)
                        self.assertIn("accepts no settlement write", action)
                        for unavailable in ("internal report", "route_settlements", "internal checkpoint", "owner"):
                            self.assertNotIn(unavailable, action.lower())
                else:
                    self.assertIn("route_settlements basis lead_decision", row["wording"])
                    self.assertIn("internal checkpoint", row["wording"])
                    self.assertIn("This unreported tab has no standing close command", row["wording"])
            else:
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
    def under_retirement(self, case, *, discarded=False):
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
        self.under_retirement("user_owned")

    def test_discarded_user_owned_retired_record_names_owner_decision(self):
        self.under_retirement("user_owned", discarded=True)

    def test_unreported_external_retired_record_names_owner_decision(self):
        self.under_retirement("external")

    def test_discarded_external_retired_record_names_owner_decision(self):
        self.under_retirement("external", discarded=True)

    def test_owned_retired_record_release_is_still_owed_without_closure_timing(self):
        self.under_retirement("owned")


class RouteMismatchTabTests(EndedTabAssertions, TabCase):
    def test_closed_route_hold_status_names_the_lead_and_preserves_the_hold(self):
        self.intake(self.sub("S"))
        frozen = self.packet(["S"])
        start = self.port.start_worker
        def receipt_mismatch(*args, **kwargs):
            value = start(*args, **kwargs)
            value["launch"]["effective"] = {**value["launch"]["effective"], "model": "gpt-6-luna"}
            return value
        with patch.object(OrcaPort, "start_worker", side_effect=receipt_mismatch):
            admission = self.start("closed-route-hold", frozen)["admission"]
        key = admission["admission_id"]
        self.settle(admission); self.tab_case = "external"
        self.write(self.finished_rows(), dispositions=[{"admission": key, "discarded": True, "reason": "superseded"}], close=True)
        before = self.saved()
        view = self.status()
        self.assertEqual(view["blocker"], "route_mismatch")
        self.assertEqual(view["route_holds"], [key])
        for action in (view["next_safe_action"], view["next_action"]):
            self.assertIn(f"Project lead: for admission {key}", action)
            self.assertIn("successor or repository task", action)
            self.assertIn("accepts no settlement write", action)
            for unavailable in ("internal report", "route_settlements", "internal checkpoint", "owner"):
                self.assertNotIn(unavailable, action.lower())
        with self.assertRaises(PodError) as report:
            self.report(admission, frozen)
        self.assertEqual(report.exception.code, "objective_closed")
        lead = {"admission": key, "basis": "lead_decision", "decision": "discard",
                "source": {"reference": "repository-task/lead-decision"}, "reason": "the lead discards this result"}
        for extra in ({}, {"reopen": True, "revision_authority": {"provenance": "user_direct", "instruction": "reopen"}}):
            with self.subTest(reopen=bool(extra)), self.assertRaises(PodError) as checkpoint:
                self.write(seq=read(self.project, "objective")["checkpoint"]["seq"] + 1, route_settlements=[lead], **extra)
            error = checkpoint.exception
            self.assertEqual(error.code, "objective_closed")
            self.assertEqual(error.detail["referent"], {"record": "route settlements"})
            self.assertEqual(error.detail["next_action"], view["next_action"])
            for unavailable in ("owner", "user", "reopen", "internal report", "internal checkpoint"):
                self.assertNotIn(unavailable, error.detail["next_action"].lower())
        self.assertEqual(self.saved(), before)
        self.assertTrue(route_hold(read(self.project, "objective")["admissions"][key]))

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
        self.assert_tab_wording(case, route_mismatch=True)
        self.write(self.waiting_rows(), dispositions=[{"admission": self.admission["admission_id"],
                   "discarded": True, "reason": "route mismatch"}])
        self.assertIsNone(read(self.project, "objective")["checkpoint"].get("closure"))
        self.assert_tab_wording(case, route_mismatch=True)
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

    def test_open_user_owned_route_mismatch_names_lead_before_and_after_discard(self):
        self.route_mismatch("user_owned")

    def test_open_external_route_mismatch_names_lead_before_and_after_discard(self):
        self.route_mismatch("external")

    def receipt_only_mismatch(self, *, discarded=False):
        original = self.port.start_worker

        def tampered(*args, **kwargs):
            receipt = original(*args, **kwargs)
            launch = receipt["launch"]
            receipt["launch"] = {"requested": dict(launch["requested"]),
                                 "effective": {**launch["effective"], "model": "gpt-6-luna"}}
            return receipt

        self.intake(self.sub("S"))
        self.frozen = self.packet(["S"], purpose="Repair parser", boundary={"paths": ["src"]})
        with patch.object(OrcaPort, "start_worker", side_effect=tampered):
            self.admission = self.start("implement", self.frozen)["admission"]
        key = self.admission["admission_id"]
        stored = read(self.project, "objective")["admissions"][key]
        self.assertTrue(stored["route_decision"]["route_mismatch"])
        self.assertEqual(stored["error"]["code"], "route_mismatch")
        self.assertEqual(stored["route_decision"]["effective"]["model"], "gpt-6.1-sol")
        self.assertIsNone(stored["report"])
        self.settle(self.admission)
        self.tab_case = "user_owned"
        self.assert_tab_wording("user_owned", route_mismatch=True)
        if discarded:
            self.write(self.waiting_rows(), dispositions=[{"admission": key, "discarded": True,
                       "reason": "route mismatch at start"}])
            self.assert_tab_wording("user_owned", route_mismatch=True)
        result = self.report(self.admission, self.frozen,
                             **({} if discarded else {"map": {"obligations": self.waiting_rows()}}))
        ack = self.ack("report", {"admission_id": key}, result)
        view = self.status()
        close = ["orca", "terminal", "close", "--terminal", self.admission["native_binding"]["terminalHandle"]]
        for row in (result["worker_tab"], ack["worker_tab"], view["worker_tabs"][0]):
            self.assertEqual((row["row"], row["state"]), ("user_owned", "kept"))
            self.assertEqual(row["command"], close)
            self.assertIn("Do not reuse", row["wording"])
            self.assertIn("Under the owner's standing authorization, close this finished worker tab", row["wording"])
        state = read(self.project, "objective")
        self.assertIsNone(state["checkpoint"].get("closure"))
        self.assertIsNotNone(state["admissions"][key]["report"])
        self.assertTrue(state["admissions"][key]["route_decision"]["route_mismatch"])

    def test_start_receipt_only_mismatch_accepts_report_and_supplies_close(self):
        self.receipt_only_mismatch()

    def test_discarded_start_receipt_only_mismatch_accepts_report_and_supplies_close(self):
        self.receipt_only_mismatch(discarded=True)
