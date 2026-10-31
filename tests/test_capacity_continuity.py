"""Healthy public successors keep logical work and native identity in disposable projects."""

from copy import deepcopy
import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
import unittest

from pod.capacity import (CHILD, CONTINUED, PENDING, direct_root, maybe, resume,
                          selected, verify)
from pod.errors import PodError
from pod.history import load_context
from pod.internal import run
from pod.ledger import _read, _write, read
from pod.limits import Limits, using_limits
from pod.assurance import review_allowance
from tests.issue41_support import ProductionCase


class HealthyCapacityTests(unittest.TestCase):
    def build_fixture(self):
        self.case = ProductionCase()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.intake(self.case.sub("ARCHIVED"))
        # Ten genuinely consumed/dispositioned synthetic implementation results
        # become old-candidate history after a normal candidate move. Per-row
        # references have enough overhead for a bulk successor to provide real
        # relief; irreducible live padding alone must never make a successor.
        for index in range(10):
            frozen = self.case.packet(["ARCHIVED"])
            admission = self.case.start(f"settled-history-{index}", frozen)["admission"]
            self.case.settle(admission)
            self.case.report(admission, frozen, map={"seq": read(self.case.project, "objective")["checkpoint"]["seq"] + 1,
                "update": {"ARCHIVED": {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}}})
            self.case.write(dispositions=[{"admission": admission["admission_id"],
                "discarded": True, "reason": "synthetic settled result retained"}])
        self.case.move()
        self.case.write(seq=read(self.case.project, "objective")["checkpoint"]["seq"] + 1, update={"ARCHIVED": {"state": "withdrawn", "withdrawal": {"by": "coordinator", "reason": "synthetic history setup complete"}}})
        self.case.write(remaining_gates=["synthetic current required gate " + "x" * 6000])
        self.project = self.case.project
        self.path = direct_root(self.project, "objective") / "context.json"
        self.before = _read(self.path)
        self.initial_starts = len(self.case.port.starts)
        self.limits = Limits(context_bytes=self.path.stat().st_size * 8 // 7)

    @classmethod
    def setUpClass(cls):
        # Build native-fixture history once, outside each fast test's timing.
        cls.prepared = cls()
        cls.prepared.build_fixture()
        cls.addClassCleanup(cls.prepared.doCleanups)
        sample = cls.prepared
        cls.saved_files = {path.relative_to(sample.path.parent.parent): path.read_bytes()
                           for path in sample.path.parent.parent.rglob("*") if path.is_file()}
        cls.saved_port = deepcopy(sample.case.port.__dict__)
        cls.saved_current = deepcopy(sample.case.current)
        cls.saved_remote = deepcopy(sample.case.remote.__dict__)
        cls.saved_candidate = sample.case.candidate

    def setUp(self):
        import shutil
        sample = self.prepared
        self.case, self.project, self.path = sample.case, sample.project, sample.path
        self.before, self.limits = deepcopy(sample.before), sample.limits
        self.initial_starts = sample.initial_starts
        root = self.path.parent.parent
        shutil.rmtree(root)
        for relative, data in self.saved_files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.case.port.__dict__.clear()
        self.case.port.__dict__.update(deepcopy(self.saved_port))
        self.case.current.clear()
        self.case.current.update(deepcopy(self.saved_current))
        self.case.remote.__dict__.clear()
        self.case.remote.__dict__.update(deepcopy(self.saved_remote))
        self.case.candidate = self.saved_candidate

    def continue_once(self):
        with using_limits(self.limits):
            result = maybe(self.project, "objective", "owner")
        self.assertEqual(result["status"], "continued")
        return result["successor"]

    def test_new_public_id_preserves_map_source_owner_run_and_does_not_close_parent(self):
        successor = self.continue_once()
        parent, child = read(self.project, "objective"), read(self.project, successor)
        self.assertNotEqual(successor, "objective")
        self.assertEqual(child["checkpoint"], {**self.before["checkpoint"], "objective": successor})
        self.assertEqual(child["admissions"], self.before["admissions"])
        self.assertEqual(child["constraints"], self.before["constraints"])
        self.assertEqual(child["owner"], self.before["owner"])
        self.assertEqual(parent[CONTINUED]["successor"], successor)
        self.assertFalse(parent["checkpoint"].get("closure"))
        self.assertEqual(verify(self.project, successor, child), "objective")
        self.assertEqual(self.case.current, {"id": "run", "coordinator_handle": "owner", "consumer_generation": 1})
        self.assertEqual(len(self.case.port.starts), self.initial_starts)

    def test_explicit_descendant_runs_normal_checkpoint_with_original_logical_root(self):
        successor = self.continue_once()
        value = self.case.core(next_safe_action="continue genuine unchanged source")
        value["seq"] = self.before["checkpoint"]["seq"] + 1
        result = run("checkpoint", {"project": str(self.project), "objective": successor,
                                    "owner": "owner", "value": value})
        self.assertEqual(result["logical_root"], "objective")
        self.assertEqual(result["public_objective"], successor)
        self.assertEqual(read(self.project, successor)["checkpoint"]["seq"], value["seq"])
        self.assertEqual(read(self.project, "objective")["checkpoint"], self.before["checkpoint"])

    def test_old_public_id_refuses_writes_instead_of_silently_following(self):
        successor = self.continue_once()
        before = self.path.read_bytes()
        with self.assertRaises(PodError) as caught:
            self.case.write()
        self.assertEqual(caught.exception.code, "objective_continued")
        self.assertEqual(caught.exception.detail["successor"], successor)
        self.assertEqual(self.path.read_bytes(), before)

    def test_replay_selects_the_existing_child_and_does_not_repeat_native_work(self):
        successor = self.continue_once()
        directories = set(self.path.parent.parent.iterdir())
        result = resume(self.project, "objective", "owner")
        self.assertEqual(result["successor"], successor)
        self.assertEqual(set(self.path.parent.parent.iterdir()), directories)
        self.assertEqual(len(self.case.port.starts), self.initial_starts)

    def test_wrong_owner_or_native_generation_holds_before_any_reservation(self):
        for wrong_owner in (True, False):
            with self.subTest(wrong_owner=wrong_owner):
                self.case.current["consumer_generation"] = 1 if wrong_owner else 2
                with using_limits(self.limits), self.assertRaises(PodError):
                    maybe(self.project, "objective", "another-owner" if wrong_owner else "owner")
                self.assertNotIn(PENDING, read(self.project, "objective"))
                self.assertNotIn(CONTINUED, read(self.project, "objective"))

    def test_corrupted_retained_parent_holds_descendant_before_mutation(self):
        successor = self.continue_once()
        # Deliberately corrupt only this disposable synthetic store.
        parent = read(self.project, "objective")
        parent["interventions"]["synthetic-forgery"] = 1
        self.path.write_text(json.dumps(parent))
        child_path = direct_root(self.project, successor) / "context.json"
        before = child_path.read_bytes()
        with self.assertRaises(PodError) as caught:
            with selected(self.project, successor):
                self.fail("unverified lineage entered a helper")
        self.assertEqual(caught.exception.code, "capacity_lineage_unverified")
        self.assertEqual(child_path.read_bytes(), before)

    def test_source_failure_remains_a_real_pre_reservation_hold(self):
        with patch("pod.operations._check_objective_source", side_effect=PodError("source_changed", "synthetic changed source")):
            with using_limits(self.limits), self.assertRaises(PodError) as caught:
                maybe(self.project, "objective", "owner")
        self.assertEqual(caught.exception.code, "source_changed")
        self.assertEqual(read(self.project, "objective"), self.before)

    def test_interruption_after_reservation_resumes_without_a_second_child(self):
        import pod.capacity as capacity
        original = capacity._resume_locked
        with patch.object(capacity, "_resume_locked", side_effect=OSError("synthetic interruption")):
            with using_limits(self.limits), self.assertRaises(OSError):
                maybe(self.project, "objective", "owner")
        pending = read(self.project, "objective")[PENDING]
        with using_limits(self.limits):
            result = original(self.project, pending, "owner")
        self.assertEqual(result["successor"], pending["successor"])
        self.assertNotIn(PENDING, read(self.project, "objective"))
        self.assertEqual(read(self.project, pending["successor"])[CHILD]["phase"], "active")

    def test_repeated_metadata_does_not_make_a_chain_of_children(self):
        successor = self.continue_once()
        path = direct_root(self.project, successor) / "context.json"
        with using_limits(Limits(context_bytes=path.stat().st_size * 8 // 7)):
            held = maybe(self.project, successor, "owner")
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["code"], "capacity_no_material_relief")
        self.assertNotIn(CONTINUED, read(self.project, successor))

    def test_material_writes_at_irreducible_pressure_do_not_cascade(self):
        case = ProductionCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.intake()
        case.write(remaining_gates=["synthetic irreducible live dependency " + "x" * 6000])
        path = direct_root(case.project, "objective") / "context.json"
        limits = Limits(context_bytes=path.stat().st_size * 8 // 7)
        directories = set(path.parent.parent.iterdir())
        with using_limits(limits):
            for number in range(3):
                result = case.write(next_safe_action=f"material source continuation {number}",
                                    remaining_gates=["synthetic irreducible live dependency " + "x" * 6000])
                self.assertEqual(result["capacity_continuity"]["status"], "held")
                self.assertEqual(result["capacity_continuity"]["code"], "capacity_no_material_relief")
                state = read(case.project, "objective")
                self.assertNotIn(PENDING, state)
                self.assertNotIn(CONTINUED, state)
                self.assertEqual(set(path.parent.parent.iterdir()), directories)

    def test_preflight_capacity_failure_does_not_publish_a_reservation(self):
        from pod.util import encoded
        # Only irreducible protected live data, no settled references to group.
        state = deepcopy(self.before)
        state["admissions"] = {}
        self.path.write_bytes(encoded(state, compact=True))
        with using_limits(Limits(context_bytes=self.path.stat().st_size)):
            held = maybe(self.project, "objective", "owner")
        self.assertEqual(held["code"], "capacity_no_material_relief")
        self.assertEqual(read(self.project, "objective"), state)

    def test_pending_native_identity_is_not_translated_into_a_new_goal(self):
        with patch("pod.capacity._native_guard", side_effect=PodError("capacity_native_pending", "synthetic unknown start")):
            with using_limits(self.limits), self.assertRaises(PodError):
                maybe(self.project, "objective", "owner")
        self.assertNotIn(PENDING, read(self.project, "objective"))
        self.assertEqual(len(self.case.port.starts), self.initial_starts)

    def test_original_admission_and_packet_report_consume_through_verified_descendant(self):
        frozen = self.case.packet(["O1"])
        admission = self.case.start("synthetic-owned", frozen)["admission"]
        self.before = read(self.project, "objective")
        self.limits = Limits(context_bytes=self.path.stat().st_size * 8 // 7)
        successor = self.continue_once()
        child = read(self.project, successor)
        key = admission["admission_id"]
        self.assertEqual(child["admissions"][key], self.before["admissions"][key])
        self.case.settle(admission)
        report = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                  "attempt": admission["native_binding"]["dispatchId"],
                  "candidate": frozen["body"]["candidate"], "outcome": "succeeded",
                  "scope": frozen["body"]["scope"], "files": [], "checks": ["synthetic unit"],
                  "failures": [], "evidence": [], "uncertainty": [], "questions": []}
        result = run("report", {"project": str(self.project), "objective": successor,
                                "admission_id": key, "packet": frozen, "report": report,
                                "map": {"seq": child["checkpoint"]["seq"] + 1,
                                        "update": {"O1": {"state": "active", "executor": "coordinator"}}}})
        self.assertEqual(result["logical_root"], "objective")
        actual = read(self.project, successor)["admissions"][key]
        self.assertEqual(actual["objective"], "objective")
        self.assertEqual(actual["native_binding"], admission["native_binding"])
        self.assertEqual(actual["report"]["outcome"], "succeeded")
        self.assertEqual(len(self.case.port.starts), self.initial_starts + 1)

    def test_missing_predecessor_is_not_an_adoption_or_ownership_grant(self):
        successor = self.continue_once()
        self.path.rename(self.path.with_suffix(".saved"))
        with self.assertRaises(PodError) as caught:
            with selected(self.project, successor):
                self.fail("missing lineage must hold")
        self.assertEqual(caught.exception.code, "capacity_lineage_unverified")

    def test_shape_cannot_claim_a_healthy_transition_is_a_lost_owner_claim(self):
        successor = self.continue_once()
        child = read(self.project, successor)
        self.assertNotIn("continues", child)
        self.assertNotIn("continued_by", read(self.project, "objective"))
        self.assertEqual(child[CHILD]["plan"]["root"], "objective")

    def test_recorded_source_protection_survives_and_cannot_be_dropped_by_successor(self):
        self.case.write(remaining_gates=self.before["checkpoint"]["remaining_gates"],
                       history_policy={"keep_failed_current_candidate_reviews": True,
                                        "source": "synthetic narrower source"})
        self.before = read(self.project, "objective")
        self.limits = Limits(context_bytes=self.path.stat().st_size * 8 // 7)
        successor = self.continue_once()
        value = self.case.core(history_policy={"keep_failed_current_candidate_reviews": False,
                                               "source": "synthetic attempted relaxation"})
        value["seq"] = self.before["checkpoint"]["seq"] + 1
        with self.assertRaises(PodError) as caught:
            run("checkpoint", {"project": str(self.project), "objective": successor,
                               "owner": "owner", "value": value})
        self.assertEqual(caught.exception.code, "history_policy_locked")

    def test_actual_review_history_and_unused_allowance_survive_public_identity_change(self):
        audit = {"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                 "check": "synthetic independent check", "scope": {"paths": ["src"]},
                 "question": "is the source correct?", "candidate": self.case.base,
                 "existing_evidence": "synthetic unit proof", "insufficiency": "no review",
                 "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        self.case.write([self.case.criterion(), audit])
        for index in range(2):
            frozen = self.case.packet(["A"], role="review")
            admission = self.case.start(f"synthetic-review-{index}", frozen)["admission"]
            self.case.settle(admission)
            current_state = read(self.project, "objective")
            self.case.report(admission, frozen, outcome="failed", map={
                "seq": current_state["checkpoint"]["seq"] + 1,
                "update": {"A": {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}}})
        self.before = read(self.project, "objective")
        self.limits = Limits(context_bytes=self.path.stat().st_size * 8 // 7)
        before = review_allowance(self.before["checkpoint"], {"admissions": self.before["admissions"], "outstanding": []})
        successor = self.continue_once()
        child = read(self.project, successor)
        after = review_allowance(child["checkpoint"], {"admissions": child["admissions"], "outstanding": []})
        self.assertEqual(before, after)
        self.assertEqual((after["rounds"], after["granted"]), (2, 3))
        self.assertEqual(child["admissions"], self.before["admissions"])

    def test_ordinary_checkpoint_automatically_selects_a_public_successor_at_pressure(self):
        value = self.case.core(next_safe_action="synthetic genuine source continuation",
                               remaining_gates=self.before["checkpoint"]["remaining_gates"])
        value["seq"] = self.before["checkpoint"]["seq"] + 1
        with using_limits(self.limits):
            result = run("checkpoint", {"project": str(self.project), "objective": "objective",
                                        "owner": "owner", "value": value})
        self.assertEqual(result["capacity_continuity"]["status"], "continued")
        successor = result["capacity_continuity"]["successor"]
        self.assertEqual(read(self.project, successor)["checkpoint"]["seq"], value["seq"])

    def test_copied_standing_authority_is_suspended_until_a_fresh_real_decision(self):
        from pod.standing import projection
        self.case.op("standing-authorization", action="activate",
                     decision={"provenance": "user_direct", "instruction": "synthetic use yolo from now on"})
        self.before = read(self.project, "objective")
        self.limits = Limits(context_bytes=self.path.stat().st_size * 8 // 7)
        successor = self.continue_once()
        child = read(self.project, successor)
        self.assertEqual(projection(child)["state"], "suspended")
        self.assertEqual(projection(child)["reason"], "capacity_successor_authority_unverified")
        run("standing-authorization", {"project": str(self.project), "objective": successor,
                                      "owner": "owner", "action": "activate",
                                      "decision": {"provenance": "user_direct", "instruction": "synthetic current successor yolo"}})
        self.assertEqual(projection(read(self.project, successor))["mode"], "yolo")

    def test_unsupported_preceding_reader_refuses_without_writing(self):
        from tests.test_history_regressions import previous_history_bundle, previous_history_call
        successor = self.continue_once()
        with tempfile.TemporaryDirectory() as directory:
            bundle = previous_history_bundle(directory, revision="deda03a2")
            result = previous_history_call(bundle, read(self.project, successor))
        self.assertFalse(result["accepted"])
        self.assertTrue(result["unchanged"])

    def test_interruption_after_child_prepare_and_parent_fence_are_recoverable(self):
        import pod.capacity as capacity
        original = capacity._publish
        calls = []
        def interrupted(path, state, **kwargs):
            original(path, state, **kwargs)
            calls.append(path)
            if len(calls) == 3:
                raise OSError("synthetic interruption after durable parent fence")
        with patch.object(capacity, "_publish", side_effect=interrupted):
            with using_limits(self.limits), self.assertRaises(OSError):
                maybe(self.project, "objective", "owner")
        parent = read(self.project, "objective")
        self.assertIn(CONTINUED, parent)
        successor = parent[CONTINUED]["successor"]
        self.assertEqual(read(self.project, successor)[CHILD]["phase"], "preparing")
        with using_limits(self.limits):
            result = resume(self.project, "objective", "owner")
        self.assertEqual(result["successor"], successor)
        self.assertEqual(read(self.project, successor)[CHILD]["phase"], "active")

    def test_status_uses_public_identity_and_keeps_predecessor_duty_visible(self):
        from pod.status import status
        successor = self.continue_once()
        child = status(self.project, None, objective=successor)
        parent = status(self.project, None, objective="objective")
        self.assertEqual(child["objective"], successor)
        self.assertEqual(child["logical_root"], "objective")
        self.assertEqual(parent["readiness"]["state"], "continued")
        self.assertTrue(parent["readiness"]["coordinator_needed"])
        self.assertFalse(read(self.project, "objective")["checkpoint"].get("closure"))

    def test_concurrent_calls_cannot_create_competing_active_successors(self):
        import pod.capacity as capacity
        original = capacity._read
        barrier = threading.Barrier(2)
        local = threading.local()
        outcomes = []
        def synchronized(project, objective):
            state = original(project, objective)
            if objective == "objective" and not getattr(local, "first", False):
                local.first = True
                barrier.wait(timeout=5)
            return state
        def worker():
            try:
                with using_limits(self.limits):
                    outcomes.append(maybe(self.project, "objective", "owner"))
            except PodError as exc:
                outcomes.append({"code": exc.code})
        with patch.object(capacity, "_read", side_effect=synchronized):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        completed = [item for item in outcomes if item.get("status") == "continued"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(read(self.project, "objective")[CONTINUED]["successor"], completed[0]["successor"])
        self.assertEqual(len(self.case.port.starts), self.initial_starts)

    def test_pending_governor_effect_and_counters_are_preserved_without_submission(self):
        from pod.governor import _read_journal
        self.case.prepare()
        decision = self.case.op("governor", action=self.case.action())
        self.assertEqual(decision["decision"], "ALLOW")
        self.case.write(remaining_gates=["synthetic pending delivery " + "x" * 6000])
        self.before = read(self.project, "objective")
        self.limits = Limits(context_bytes=self.path.stat().st_size * 8 // 7)
        journal = _read_journal(self.path.parent / "governor.json")
        calls = deepcopy(self.case.remote.calls)
        successor = self.continue_once()
        copied = _read_journal(direct_root(self.project, successor) / "governor.json")
        self.assertEqual(copied, journal)
        self.assertEqual(copied["actions"][0]["outcome"], "pending")
        self.assertEqual(self.case.remote.calls, calls)

    def test_owner_keep_instruction_keeps_its_original_scope_and_still_applies(self):
        from pod.worker_tabs import _keep
        frozen = self.case.packet(["O1"])
        admission = self.case.start("synthetic-retained", frozen)["admission"]
        state = read(self.project, "objective")
        binding = admission["native_binding"]
        instruction = {"id": "synthetic-keep", "kind": "keep_worker_tab", "provenance": "user_direct",
                       "instruction": "synthetic keep this tab", "value": {
                           "objective": "objective", "dispatch": binding["dispatchId"],
                           "terminal": binding["terminalHandle"], "keep": True}}
        # An isolated projection fixture, not an actual owner/session instruction.
        state["constraints"].append(instruction)
        _write(self.path, state)
        self.before = read(self.project, "objective")
        self.limits = Limits(context_bytes=self.path.stat().st_size * 8 // 7)
        successor = self.continue_once()
        child = read(self.project, successor)
        self.assertEqual(_keep(child, successor, binding["dispatchId"], binding["terminalHandle"]), instruction)
        self.assertEqual(child["constraints"], state["constraints"])
