"""R4: precise Run exception and full-record retirement through the production operation."""
from copy import deepcopy
import json
from unittest.mock import patch

from pod.errors import PodError
from pod.history import CONTEXT_LIMIT, is_reference, capacity
from pod.internal import run
from pod.ledger import objective_root, _write
from pod.retirement import validate_entry
from pod.util import digest, encoded
from tests.history_support import size
from tests.test_historical_retirement import RetirementCase


class RetirementGenerationTests(RetirementCase):
    def test_other_coordinator_refuses_at_either_generation(self):
        self.delivered()
        for generation in (1, 2):
            self.run_holders["run"] = "another"
            self.generations["run"] = generation
            before = self.snapshot()
            self.assertEqual(self.retire()["objectives"][0]["condition"], "generation_changed")
            self.assertEqual(before, self.snapshot())

    def test_old_coordinator_and_omitted_coordinator_keep_changed_generation_refusal(self):
        self.delivered()
        self.generations["run"] = 2
        self.assertEqual(self.retire()["objectives"][0]["condition"], "generation_changed")
        original = self.native_read
        def omit(argv, **kwargs):
            response = original(argv, **kwargs)
            if argv[:2] == ["orchestration", "run-show"]:
                response["result"]["run"].pop("coordinator_handle")
            return response
        with patch("pod.orca.read_command", side_effect=omit):
            self.assertEqual(self.retire()["objectives"][0]["condition"], "generation_changed")

    def test_explicit_null_needs_lost_or_confirmed_exited_owner(self):
        self.delivered()
        self.run_holders["run"] = None
        self.generations["run"] = 2
        self.terminals["owner"] = "live"
        self.assertEqual(self.retire()["objectives"][0]["condition"], "owner_not_lost")
        self.terminals["owner"] = "stop-unconfirmed"
        self.assertEqual(self.retire()["objectives"][0]["condition"], "owner_not_lost")
        self.terminals["owner"] = "exited"
        self.assertEqual(self.retire(self.decide())["status"], "retired")

    def test_invalid_generation_or_unreadable_run_never_qualifies(self):
        self.delivered()
        self.run_holders["run"] = None
        for value in (None, "2", -1):
            self.generations["run"] = value
            self.assertEqual(self.retire()["objectives"][0]["condition"], "workers_unverified")


class RetirementCapacityTests(RetirementCase):
    def full(self, *, settled_history=False, remaining=600):
        self.delivered(reported=True)
        self.path = objective_root(self.project, "objective") / "context.json"
        state = self.state()
        if settled_history:
            row = deepcopy(state["admissions"][self.admission["admission_id"]])
            row.update(admission_id="synthetic-history", state="closed", role="investigate", candidate="e" * 40,
                       recovery={"synthetic": "x" * 30000})
            report = row["report"]
            report["observation"]["candidate"] = row["candidate"]
            report["observation_digest"] = digest({"status": report["status"], "observation": report["observation"],
                "native_binding": {"runtime": row["runtime"], **{k: row["native_binding"][k]
                  for k in ("runId", "taskId", "dispatchId", "workerId")}}})
            state["admissions"][row["admission_id"]] = row
        state["source_rejections"]["synthetic-padding"] = ""
        state["source_rejections"]["synthetic-padding"] = "x" * (CONTEXT_LIMIT - remaining - size(state))
        # Deliberately full but supported raw context: compaction has not run yet.
        from pod.ledger import _validate_context
        _validate_context(state)
        self.path.write_bytes(encoded(state, compact=True))
        return state

    def test_full_record_compacts_settled_history_before_entry_pointer(self):
        state = self.full(settled_history=True)
        journal = self.journal()
        result = self.retire(self.decide())
        self.assertEqual(result["status"], "retired")
        physical = json.loads(self.path.read_bytes())
        self.assertTrue(is_reference(physical["admissions"]["synthetic-history"]))
        self.assertFalse(is_reference(physical["historical_retirement"]))
        after = self.state()
        validate_entry(after["historical_retirement"])
        after.pop("historical_retirement")
        state["revision"] += 1
        state["decision_channels"] = 1
        self.assertEqual(after, state)
        self.assertEqual(journal, self.journal())

    def test_full_unsettled_record_uses_entry_pointer_and_preserves_every_guarantee(self):
        state = self.full()
        decision, journal = self.decide(), self.journal()
        self.assertEqual(self.retire(decision)["status"], "retired")
        physical = json.loads(self.path.read_bytes())
        self.assertTrue(is_reference(physical["historical_retirement"]))
        self.assertEqual(physical["admissions"], state["admissions"])
        self.assertLess(self.path.stat().st_size, CONTEXT_LIMIT)
        entry = self.state()["historical_retirement"]
        validate_entry(entry)
        before = self.snapshot()
        self.assertEqual(self.retire(decision)["objectives"][0]["status"], "replayed")
        self.assertEqual(self.retire({**decision, "reason": "different"})["objectives"][0]["condition"], "objective_retired")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(journal, self.journal())
        status = self.status()
        self.assertEqual((status["phase"], status["blocker"]), ("retired", "objective_retired"))
        self.assertFalse(status["retirement"]["completed"])
        self.assertEqual(status["retirement"], self.listed()["objective"]["retirement"])
        self.returning()
        for operation in self.boundary_calls("owner").values():
            with self.assertRaises(PodError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "objective_retired")
        self.assertEqual(before, self.snapshot())

    def test_pointer_impossible_fit_preserves_context_journal_and_sidecars(self):
        self.full(remaining=5)
        decision = self.decide()
        before = self.snapshot()
        with self.assertRaises(PodError) as caught:
            self.retire(decision)
        self.assertEqual(caught.exception.code, "record_too_large")
        self.assertTrue(caught.exception.detail["largest_sections"])
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.path.parent / "history").exists())

    def test_small_record_keeps_inline_entry_and_maintenance_refuses_frozen_record(self):
        self.delivered()
        self.assertEqual(self.retire(self.decide())["status"], "retired")
        path = objective_root(self.project, "objective") / "context.json"
        self.assertNotIn("history", json.loads(path.read_bytes()))
        before = self.snapshot()
        with self.assertRaises(PodError) as caught:
            run("record-compaction", {"project": str(self.project), "objective": "objective"})
        self.assertEqual(caught.exception.code, "objective_retired")
        self.assertEqual(before, self.snapshot())

    def test_over_limit_record_requires_explicit_maintenance_then_retires(self):
        state = self.full(settled_history=True, remaining=-3000)
        refused = self.retire()["objectives"][0]
        self.assertEqual(refused["condition"], "scope_unreadable")
        self.assertIn("record-compaction", refused["next_action"])
        request = {"project": str(self.project), "objective": "objective"}
        detected = run("record-compaction", request)
        decision = detected["next_action"]["request"]["decision"]
        decision["instruction"] = "Compact settled objective history before retirement"
        self.assertEqual(run("record-compaction", {**request, "decision": decision})["status"], "compacted")
        self.assertEqual(self.state(), state)
        self.assertEqual(self.retire(self.decide())["status"], "retired")
