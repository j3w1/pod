"""Owner-scoped over-limit maintenance, exact replay and refusal invariants (R121)."""
from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import patch

from pod.errors import PodError
from pod.history import (CONTEXT_LIMIT, MAINTENANCE_LIMIT, _sha, compact, hydrate, load_context,
                         is_reference, prepare_context, publish_context, capacity, MARKER)
from pod.internal import run
from pod.ledger import objective_root, _read, _write
from pod.util import encoded
from tests.history_support import synthetic_state, size
from tests.kernel_support import KernelCase


class RecordCompactionTests(KernelCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.large = synthetic_state()

    def arrange(self):
        self.path = objective_root(self.project, "synthetic-capacity") / "context.json"
        self.path.parent.mkdir(parents=True)
        (self.path.parent / ".lock").touch()
        self.state = deepcopy(self.large)
        self.path.write_bytes(encoded(self.state, compact=True))
        self.journal = self.path.parent / "governor.json"
        self.other = self.path.parent.parent / "unrelated.json"
        self.other.write_bytes(b"unrelated")
        return self.path.read_bytes()

    def operate(self, decision=None):
        request = {"project": str(self.project), "objective": "synthetic-capacity"}
        if decision is not None:
            request["decision"] = decision
        return run("record-compaction", request)

    def decide(self):
        found = self.operate()
        decision = found["next_action"]["request"]["decision"]
        decision["instruction"] = "Compact settled history of this exact objective"
        return decision

    def unoffered_decision(self, detected):
        # An explicit exact decision remains a no-op even when detection offers none.
        facts = detected["facts"]
        return {"provenance": "user_direct", "instruction": "Compact this exact record if useful",
                "channel": "session", "repository": facts["repository"], "objective": facts["objective"],
                "facts": {key: facts[key] for key in ("record", "revision", "input_sha256", "logical_sha256")}}

    def files(self):
        return {str(p.relative_to(self.path.parent.parent)): p.read_bytes()
                for p in self.path.parent.parent.rglob("*") if p.is_file()}

    def assert_refused(self, code, decision):
        before = self.files()
        with self.assertRaises(PodError) as caught:
            self.operate(decision)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(before, self.files())

    def test_detection_has_exact_facts_plain_question_and_no_writes(self):
        before = self.arrange()
        files = self.files()
        with self.assertRaises(PodError) as caught:
            _read(self.path)
        self.assertEqual(caught.exception.code, "record_too_large")
        result = self.operate()
        self.assertEqual(result["status"], "compaction_available")
        self.assertEqual(result["facts"]["input_sha256"], _sha(before))
        self.assertEqual(result["facts"]["revision"], self.state["revision"])
        self.assertEqual(result["current_bytes"], len(before))
        self.assertLess(result["proposed_bytes"], CONTEXT_LIMIT)
        self.assertNotIn("synthetic-owner", result["question"])
        self.assertNotIn("synthetic-run", result["question"])
        self.assertEqual(files, self.files())

    def test_unchanged_detection_has_guidance_without_an_owner_question_or_template(self):
        self.intake()
        path = objective_root(self.project, "objective") / "context.json"
        before = {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
        detected = run("record-compaction", {"project": str(self.project), "objective": "objective"})
        self.assertEqual((detected["status"], detected["written"]), ("unchanged", False))
        self.assertNotIn("question", detected)
        self.assertIsInstance(detected["next_action"], str)
        self.assertIn("Continue", detected["next_action"])
        self.assertEqual({p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}, before)

    def test_over_limit_unchanged_detection_names_sections_and_settlement_routes(self):
        from pod.ledger import read
        self.intake()
        path = objective_root(self.project, "objective") / "context.json"
        state = read(self.project, "objective")
        state["source_rejections"] = {"synthetic-unsettled": "x" * CONTEXT_LIMIT}
        path.write_bytes(encoded(state, compact=True))
        before = {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
        detected = run("record-compaction", {"project": str(self.project), "objective": "objective"})
        self.assertEqual((detected["status"], detected["written"]), ("unchanged", False))
        self.assertNotIn("question", detected)
        self.assertEqual(detected["largest_sections"][0]["section"], "source_rejections")
        for route in ("reports", "implementation dispositions", "governed effects"):
            self.assertIn(route, detected["next_action"])
        self.assertEqual({p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}, before)

    def test_detection_counts_receipt_overhead_before_asking_the_owner(self):
        from pod.ledger import read
        self.intake()
        path = objective_root(self.project, "objective") / "context.json"
        state = read(self.project, "objective")
        state["checkpoint"]["revision_decisions"] = [
            {"seq": 1, "kind": "revision", "provenance": "user_direct", "instruction": "x" * 180},
            {"seq": 1, "kind": "revision", "provenance": "user_direct", "instruction": "latest"}]
        path.write_bytes(encoded(state, compact=True))
        before = {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
        detected = run("record-compaction", {"project": str(self.project), "objective": "objective"})
        self.assertEqual((detected["status"], detected["written"]), ("unchanged", False))
        self.assertNotIn("question", detected)
        self.assertIsInstance(detected["next_action"], str)
        self.assertGreaterEqual(detected["proposed_bytes"], detected["current_bytes"])
        self.assertEqual({p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}, before)

    def test_detection_receipt_allowance_covers_the_largest_owner_instruction(self):
        self.arrange()
        detected = self.operate()
        decision = detected["next_action"]["request"]["decision"]
        # Each allowed character needs six JSON bytes; this reaches the allowance.
        decision["instruction"] = "\x01" * 1024
        result = self.operate(decision)
        self.assertEqual(result["status"], "compacted")
        self.assertEqual(result["proposed_bytes"], detected["proposed_bytes"])
        self.assertEqual(capacity(self.path, self.state)["maintenance"][0]["authority"]["instruction"],
                         decision["instruction"])
        self.assertEqual(_read(self.path), self.state)

    def test_owner_maintenance_preserves_every_logical_field_and_revision(self):
        self.arrange()
        decision = self.decide()
        result = self.operate(decision)
        self.assertEqual(result["status"], "compacted")
        self.assertEqual(_read(self.path), self.state)
        self.assertEqual(self.other.read_bytes(), b"unrelated")
        self.assertLess(self.path.stat().st_size, CONTEXT_LIMIT)
        block = capacity(self.path, self.state)
        self.assertEqual(block["maintenance"][0]["authority"]["channel"], "session")

    def test_exact_replay_writes_nothing_and_receipt_survives_ordinary_write(self):
        self.arrange()
        decision = self.decide()
        self.operate(decision)
        files = self.files()
        self.assertEqual(self.operate(decision)["status"], "replayed")
        self.assertEqual(files, self.files())
        state = _read(self.path)
        _write(self.path, state)
        self.assertEqual(len(capacity(self.path, state)["maintenance"]), 1)
        self.assert_refused("compaction_decision_mismatch", decision)

    def test_later_maintenance_appends_immutable_receipt_history(self):
        self.arrange()
        self.operate(self.decide())
        first = self.files()
        # Newly consumed settled history makes the second maintenance useful.
        state = _read(self.path)
        row = deepcopy(state["admissions"]["attempt-048"])
        row["admission_id"] = "later-settled"
        state["admissions"]["later-settled"] = row
        physical = json.loads(self.path.read_bytes())
        physical["admissions"]["later-settled"] = row
        self.path.write_bytes(encoded(physical, compact=True))
        second = self.decide()
        second["instruction"] += " again"
        self.operate(second)
        self.assertEqual(len(capacity(self.path, self.state)["maintenance"]), 2)
        for key, value in first.items():
            if "/history/" in key:
                self.assertEqual(self.files()[key], value)

    def test_repeated_maintenance_without_savings_writes_no_receipt_or_chunk(self):
        self.arrange()
        self.operate(self.decide())
        before = self.files()
        detected = self.operate()
        self.assertEqual(detected["status"], "unchanged")
        self.assertNotIn("question", detected)
        self.assertIsInstance(detected["next_action"], str)
        second = self.unoffered_decision(detected)
        second["instruction"] = "Compact settled history of this exact objective again"
        result = self.operate(second)
        self.assertEqual((result["status"], result["written"]), ("unchanged", False))
        self.assertEqual(self.files(), before)
        self.assertEqual(len(capacity(self.path, _read(self.path))["maintenance"]), 1)

    def test_receipt_overhead_cannot_turn_a_saving_into_a_larger_record(self):
        self.check_receipt_overhead()

    def test_receipt_overhead_at_limit_still_returns_unchanged_without_write(self):
        self.check_receipt_overhead(near_limit=True)

    def check_receipt_overhead(self, *, near_limit=False):
        from pod.ledger import read
        from pod.util import encoded
        self.intake()
        path = objective_root(self.project, "objective") / "context.json"
        state = read(self.project, "objective")
        # A synthetic earlier revision is just large enough to save less than a receipt reference.
        state["checkpoint"]["revision_decisions"] = [
            {"seq": 1, "kind": "revision", "provenance": "user_direct", "instruction": "x" * 180},
            {"seq": 1, "kind": "revision", "provenance": "user_direct", "instruction": "latest"}]
        if near_limit:
            state["source_rejections"] = {"synthetic-unsettled": ""}
            state["source_rejections"]["synthetic-unsettled"] = "x" * (CONTEXT_LIMIT - size(state) - 8)
        path.write_bytes(encoded(state, compact=True))
        request = {"project": str(self.project), "objective": "objective"}
        found = run("record-compaction", request)
        self.assertEqual(found["status"], "unchanged")
        self.assertGreaterEqual(found["proposed_bytes"], found["current_bytes"])
        self.assertNotIn("question", found)
        self.assertIsInstance(found["next_action"], str)
        decision = self.unoffered_decision(found)
        before = {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
        result = run("record-compaction", {**request, "decision": decision})
        self.assertEqual((result["status"], result["written"]), ("unchanged", False))
        self.assertEqual({p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}, before)

    def test_over_limit_record_without_savings_returns_unchanged_without_write(self):
        from pod.ledger import read
        self.intake()
        path = objective_root(self.project, "objective") / "context.json"
        state = read(self.project, "objective")
        state["source_rejections"] = {"synthetic-unsettled": "x" * CONTEXT_LIMIT}
        path.write_bytes(encoded(state, compact=True))
        request = {"project": str(self.project), "objective": "objective"}
        before = {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
        found = run("record-compaction", request)
        self.assertEqual(found["status"], "unchanged")
        self.assertNotIn("question", found)
        self.assertIsInstance(found["next_action"], str)
        decision = self.unoffered_decision(found)
        result = run("record-compaction", {**request, "decision": decision})
        self.assertEqual((result["status"], result["written"]), ("unchanged", False))
        self.assertEqual({p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}, before)

    def test_stale_input_digest_revision_objective_repository_refuse(self):
        self.arrange()
        decision = self.decide()
        for field in ("record", "revision", "input_sha256", "logical_sha256"):
            changed = deepcopy(decision)
            changed["facts"][field] = "wrong"
            self.assert_refused("compaction_decision_mismatch", changed)
        for field in ("objective", "repository"):
            changed = {**decision, field: "wrong"}
            self.assert_refused("compaction_decision_mismatch", changed)

    def test_indirect_and_relayed_compaction_refuse_shared_authority_rule(self):
        self.arrange()
        decision = self.decide()
        for changes in ({"provenance": "worker"}, {"channel": "relayed"},
                        {"channel": "relayed", "relay": {"reference": "verified-relay", "owner_source": "owner-message"}}):
            self.assert_refused("compaction_decision_mismatch", {**decision, **changes})

    def test_concurrent_change_needs_new_detection(self):
        self.arrange()
        decision = self.decide()
        self.path.write_bytes(self.path.read_bytes() + b" ")
        self.assert_refused("compaction_decision_mismatch", decision)

    def test_unknown_objective_and_unsafe_record_create_nothing(self):
        before = list(objective_root(self.project, "unknown").parent.glob("*"))
        with self.assertRaises(PodError) as caught:
            run("record-compaction", {"project": str(self.project), "objective": "unknown"})
        self.assertEqual(caught.exception.code, "objective_unknown")
        self.assertEqual(before, list(objective_root(self.project, "unknown").parent.glob("*")))
        self.arrange()
        target = self.path.parent / "real.json"
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(PodError) as caught:
            self.operate()
        self.assertEqual(caught.exception.code, "unsafe_record")
        self.assertEqual(target.read_bytes(), encoded(self.state, compact=True))

    def test_maintenance_bound_and_unsupported_schema_remain_unavailable(self):
        self.arrange()
        self.path.write_bytes(b" " * (MAINTENANCE_LIMIT + 1))
        before = self.files()
        with self.assertRaises(PodError) as caught:
            self.operate()
        self.assertEqual(caught.exception.code, "record_too_large")
        self.assertEqual(caught.exception.detail["limit_bytes"], MAINTENANCE_LIMIT)
        self.assertEqual(before, self.files())
        self.state["schema"] = "pod-context/v999"
        self.path.write_bytes(encoded(self.state))
        before = self.files()
        with self.assertRaises(PodError) as caught:
            self.operate()
        self.assertEqual(caught.exception.code, "state_unsupported")
        self.assertEqual(before, self.files())

    def test_impossible_fit_publishes_no_chunks(self):
        self.arrange()
        self.state["source_rejections"] = {"unsettled": "x" * CONTEXT_LIMIT}
        self.path.write_bytes(encoded(self.state, compact=True))
        before = self.files()
        with self.assertRaises(PodError) as caught:
            self.operate()
        self.assertEqual(caught.exception.code, "record_too_large")
        self.assertEqual(caught.exception.detail["largest_sections"][0]["section"], "source_rejections")
        self.assertEqual(before, self.files())

    def test_crash_after_chunks_before_context_keeps_readable_old_record_and_orphans(self):
        self.arrange()
        before = self.path.read_bytes()
        plan, chunks = prepare_context(self.path, self.state, force=True, maintenance=[])
        with patch("pod.history.atomic_json", side_effect=OSError("crash before context replacement")):
            with self.assertRaises(OSError):
                publish_context(self.path, plan, chunks)
        self.assertEqual(self.path.read_bytes(), before)
        files = {p: p.read_bytes() for p in (self.path.parent / "history").rglob("*.json")}
        self.assertTrue(files)
        self.operate(self.decide())
        self.assertEqual(_read(self.path), self.state)
        self.assertTrue(all(p.read_bytes() == value for p, value in files.items()))

    def test_small_record_noop_preserves_bytes_without_metadata(self):
        from tests.kernel_support import governance_commit
        self.intake()
        for index in range(6):
            self.candidate = governance_commit(self, "README.md", f"Candidate {index}\n", f"work {index}")
            self.write(self.stored())
        path = objective_root(self.project, "objective") / "context.json"
        request = {"project": str(self.project), "objective": "objective"}
        detected = run("record-compaction", request)
        self.assertEqual(detected["status"], "unchanged")
        self.assertNotIn("question", detected)
        self.assertIsInstance(detected["next_action"], str)
        decision = self.unoffered_decision(detected)
        before = path.read_bytes()
        files = {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
        result = run("record-compaction", {**request, "decision": decision})
        self.assertEqual((result["status"], result["written"]), ("unchanged", False))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual({p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}, files)
        self.assertNotIn("history", json.loads(before))

    def test_automatic_and_retirement_compaction_keep_short_candidates_inline(self):
        from pod.ledger import read
        self.intake()
        state = read(self.project, "objective")
        state["checkpoint"]["governance_history"]["candidates"] = [f"{index + 1:040x}" for index in range(7)]
        path = objective_root(self.project, "objective") / "context.json"
        for purpose in ("ordinary", "retirement"):
            with self.subTest(purpose=purpose):
                logical = deepcopy(state)
                # Trigger ordinary compaction without adding movable rows.
                logical["source_rejections"] = {"synthetic-unsettled": "x" * 328000}
                record, chunks = prepare_context(path, logical, purpose=purpose)
                self.assertEqual((record, chunks), (logical, {}))

    def test_metadata_overhead_cannot_make_forced_compaction_larger(self):
        from pod.ledger import read
        self.intake()
        state = read(self.project, "objective")
        # Synthetic short revision: a reference saves fewer bytes than the history discriminator costs.
        state["checkpoint"]["revision_decisions"] = [
            {"seq": 1, "kind": "revision", "provenance": "user_direct", "instruction": "x" * 120},
            {"seq": 1, "kind": "revision", "provenance": "user_direct", "instruction": "latest"}]
        record, chunks = compact(state, force=True)
        self.assertEqual((record, chunks), (state, {}))
