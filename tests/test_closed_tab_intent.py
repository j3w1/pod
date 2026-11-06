"""R127 / pod#192: a closed objective's worker-tab intents never run runtime continuity.

Only native ports are injected; every value is an invented synthetic control.
"""

import json

from pod import internal
from pod.errors import PodError
from pod.ledger import read
from pod.selection import validate_constraint
from pod.worker_tabs import _keep
from tests.decision_policy_support import LOCAL
from tests.test_worker_tabs import TabCase

# Everything a tab intent may change (decision_channels is the decision-record format marker);
# every other stored record stays byte-identical.
INTENT_FIELDS = {"constraints", "decision_order", "decision_channels", "revision"}


class ClosedTabIntentCase(TabCase):
    def closed(self, keep=True):
        frozen, admission = self.worker()
        if keep:
            self.keep(admission)
        self.tab_case = "user_owned"
        self.report(admission, frozen, map={"obligations": self.finished_rows(), "close": True})
        return admission

    def change_runtime(self, runtime="runtime-2"):
        self.port.runtime = runtime
        self.port.placement = {**self.port.placement, "runtime": runtime}

    def intent(self, admission, keep, identity, *, authority=None, owner="owner"):
        binding = admission["native_binding"]
        decided = authority or {"provenance": "user_direct", "instruction": "Keep this worker tab" if keep
                                else "Let this worker tab go"}
        return internal.run("constraint", {"project": str(self.project), "objective": "objective", "owner": owner,
                                           "action": "add", "value": {"id": identity, "kind": "keep_worker_tab",
                                           **decided, "value": {"objective": "objective",
                                           "dispatch": binding["dispatchId"], "terminal": binding["terminalHandle"],
                                           "keep": keep}}})

    def record(self):
        return read(self.project, "objective")

    def assert_only_intent_changed(self, before):
        after = self.record()
        self.assertEqual(set(after) - INTENT_FIELDS, set(before) - INTENT_FIELDS)
        for key in set(before) - INTENT_FIELDS:
            self.assertEqual(json.dumps(after[key], sort_keys=True), json.dumps(before[key], sort_keys=True), key)

    def refused(self, code, call, *args, **kwargs):
        before = self.saved()
        with self.assertRaises(PodError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(self.saved(), before)
        return caught.exception


class AfterRuntimeChangeTests(ClosedTabIntentCase):
    def test_person_revocation_records_without_continuity_and_settlement_stays_byte_identical(self):
        admission = self.closed()
        self.change_runtime()
        before = self.record()
        self.intent(admission, False, "revoke-after-runtime-change")
        self.assert_only_intent_changed(before)
        state = read(self.project, "objective")
        row = state["constraints"][-1]
        self.assertEqual((row["value"]["keep"], row["observed_runtime"]), (False, "runtime-2"))
        self.assertEqual(state["checkpoint"]["native_refs"], before["checkpoint"]["native_refs"])
        self.assertEqual((state["checkpoint"].get("continuity") or {}).get("history", []), [])
        self.assertEqual({item["runtime"] for item in state["admissions"].values()}, {"runtime"})

    def test_person_keep_and_another_callers_decision_are_accepted(self):
        admission = self.closed(keep=False)
        self.change_runtime()
        before = self.record()
        self.intent(admission, True, "keep-after-runtime-change")
        self.intent(admission, False, "relayed-let-go", owner="another-terminal")
        self.assert_only_intent_changed(before)
        self.assertEqual([row["id"] for row in read(self.project, "objective")["constraints"]],
                         ["keep-after-runtime-change", "relayed-let-go"])

    def test_status_names_the_tab_intent_write_and_never_reopening(self):
        admission = self.closed()
        self.change_runtime()
        [row] = self.status()["worker_tabs"]
        self.assertEqual(row["row"], "runtime_changed")
        self.assertEqual(row["tab_intent"]["command"], ["pod", "internal", "constraint", "--input", "FILE"])
        self.assertEqual(row["tab_intent"]["input"]["value"]["value"],
                         {"objective": "objective", "dispatch": admission["native_binding"]["dispatchId"],
                          "terminal": admission["native_binding"]["terminalHandle"]})
        self.assertIn("needs no reopening", row["wording"])
        self.assertNotIn("rebinds", row["wording"])
        self.assertIn("The recorded intent is keep by person instruction keep.", row["wording"])
        self.intent(admission, False, "let-go")
        [row] = self.status()["worker_tabs"]
        self.assertIn("The recorded intent is let go by person instruction let-go.", row["wording"])

    def test_open_objective_keeps_the_rebind_wording(self):
        frozen, admission = self.worker()
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        self.change_runtime()
        [row] = self.status()["worker_tabs"]
        self.assertEqual(row["row"], "runtime_changed")
        self.assertNotIn("tab_intent", row)
        self.assertIn("the next Pod write rebinds", row["wording"])


class AgentIntentTests(ClosedTabIntentCase):
    def test_agent_keep_on_a_closed_objective_is_kept_by_agent_and_an_agent_may_revoke_it(self):
        admission = self.closed(keep=False)
        before = self.record()
        self.intent(admission, True, "agent-keep", authority=LOCAL, owner="another-agent")
        [row] = self.status()["worker_tabs"]
        self.assertEqual((row["owner_kept"], row["kept_by"], row["command"]), (False, "agent", None))
        self.assertTrue(row["wording"].startswith("Kept by agent instruction agent-keep."), row["wording"])
        self.intent(admission, False, "agent-let-go", authority=LOCAL)
        [row] = self.status()["worker_tabs"]
        self.assertNotIn("kept_by", row)
        self.assertEqual(row["command"][:3], ["orca", "terminal", "close"])
        self.assert_only_intent_changed(before)

    def test_an_agent_cannot_revoke_the_persons_keep(self):
        admission = self.closed()
        self.change_runtime()
        error = self.refused("constraint_authority", self.intent, admission, False, "agent-let-go", authority=LOCAL)
        self.assertIn("needs the person's direct decision", str(error))
        self.intent(admission, False, "person-let-go")

    def test_an_intervening_agent_keep_never_downgrades_the_persons_keep(self):
        admission = self.closed()
        error = self.refused("constraint_authority", self.intent, admission, True, "agent-keep", authority=LOCAL)
        self.assertIn("person's keep already decides", str(error))
        self.refused("constraint_authority", self.intent, admission, False, "agent-let-go", authority=LOCAL)
        [row] = self.status()["worker_tabs"]
        self.assertEqual((row["owner_kept"], row["kept_by"], row["command"]), (True, "person", None))

    def test_a_person_keep_stays_deciding_over_an_earlier_recorded_agent_row(self):
        state = {"checkpoint": {"objective": "objective", "closure": {"revision": 1}}, "constraints": [
            {"id": "person", "kind": "keep_worker_tab", "provenance": "user_direct", "instruction": "Keep it",
             "value": {"objective": "objective", "dispatch": "d", "terminal": "t", "keep": True}},
            {"id": "agent", "kind": "keep_worker_tab", "provenance": "agent_decision", "actor": {"kind": "agent"},
             "value": {"objective": "objective", "dispatch": "d", "terminal": "t", "keep": False}}]}
        self.assertEqual(_keep(state, "objective", "d", "t")["id"], "person")

    def test_an_agent_intent_on_an_open_objective_is_refused(self):
        _, admission = self.worker()
        for keep in (True, False):
            with self.subTest(keep=keep):
                self.refused("constraint_authority", self.intent, admission, keep, f"agent-{keep}", authority=LOCAL)

    def test_the_closure_gate_counts_only_the_persons_keeps(self):
        state = {"checkpoint": {"objective": "objective"}, "constraints": [
            {"id": "agent", "kind": "keep_worker_tab", "provenance": "agent_decision", "actor": {"kind": "agent"},
             "value": {"objective": "objective", "dispatch": "d", "terminal": "t", "keep": True}}]}
        self.assertIsNone(_keep(state, "objective", "d", "t"))
        state["checkpoint"]["closure"] = {"revision": 1}
        self.assertEqual(_keep(state, "objective", "d", "t")["id"], "agent")

    def test_observed_runtime_is_never_caller_input(self):
        admission = self.closed()
        binding = admission["native_binding"]
        self.refused("invalid_constraint", internal.run, "constraint", {
            "project": str(self.project), "objective": "objective", "owner": "owner", "action": "add",
            "value": {"id": "supplied", "kind": "keep_worker_tab", "provenance": "user_direct",
                      "instruction": "Let this worker tab go", "observed_runtime": "runtime-9",
                      "value": {"objective": "objective", "dispatch": binding["dispatchId"],
                                "terminal": binding["terminalHandle"], "keep": False}}})

    def test_observed_runtime_is_refused_on_every_other_constraint_kind(self):
        others = ({"kind": "only_agents", "value": ["codex"]}, {"kind": "max_workers", "value": 1},
                  {"kind": "allow_delegation", "value": True})
        for item in others:
            for provenance in ("repository", "user_direct"):
                value = {"id": "other", "provenance": provenance, "observed_runtime": "runtime-9", **item,
                         **({"instruction": "Use this constraint"} if provenance == "user_direct" else {})}
                for new in (True, False):
                    with self.subTest(kind=item["kind"], provenance=provenance, new=new):
                        with self.assertRaises(PodError) as caught:
                            validate_constraint(value, new=new)
                        self.assertEqual(caught.exception.code, "invalid_constraint")
                        self.assertIn("Only a worker-tab intent", str(caught.exception))
