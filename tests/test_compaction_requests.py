"""Declared lossless maintenance requests and immutable mixed-version receipts."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from unittest.mock import patch

from pod.errors import PodError
from pod.governor import _empty_journal
from pod.history import (CONTEXT_LIMIT, MAINTENANCE_LIMIT, _maintenance_valid, _sha,
                         capacity, compact, is_reference, publish_context)
from pod.internal import run
from pod.ledger import _lock, _read, _write, objective_root
from pod.util import digest, encoded
from tests.history_support import synthetic_state
from tests.kernel_support import KernelCase


class CompactionRequestTests(KernelCase):
    @classmethod
    def setUpClass(cls):
        cls.large = synthetic_state()

    def setUp(self):
        super().setUp()
        self.state = deepcopy(self.large)
        self.path = objective_root(self.project, "synthetic-capacity") / "context.json"
        self.path.parent.mkdir(parents=True)
        (self.path.parent / ".lock").touch()
        self.save()
        (self.path.parent / "governor.json").write_bytes(encoded(_empty_journal(), compact=True))
        self.other = self.path.parent.parent / "unrelated.json"
        self.other.write_bytes(b"unrelated")

    def save(self):
        self.path.write_bytes(encoded(self.state, compact=True))

    def operate(self, request=None, **extra):
        return run("record-compaction", {"project": str(self.project), "objective": "synthetic-capacity",
                                        **({"request": request} if request is not None else {}), **extra})

    def requested(self, by="coordinator", *, detected=None):
        facts = (detected or self.operate())["facts"]
        return {"schema": "pod-compaction-request/v1", "requested_by": by,
                "reason": "Compact settled history without changing logical work",
                "repository": facts["repository"], "objective": facts["objective"],
                "facts": {key: facts[key] for key in ("record", "revision", "input_sha256", "logical_sha256")},
                **({"owner": facts["owner"]} if by == "coordinator" else
                   {"relay": {"reference": "project-lead-maintenance-request"}})}

    def files(self):
        return {str(path.relative_to(self.path.parent.parent)): path.read_bytes()
                for path in self.path.parent.parent.rglob("*") if path.is_file()}

    def refused(self, request, code="compaction_decision_mismatch", **extra):
        before = self.files()
        with self.assertRaises(PodError) as caught:
            self.operate(request, **extra)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.files(), before)

    def receipts(self):
        return capacity(self.path, _read(self.path))["maintenance"]

    def add_history(self):
        state = _read(self.path)
        key = f"later-{len(state['admissions'])}"
        row = deepcopy(state["admissions"]["attempt-048"])
        row["admission_id"] = key
        state["admissions"][key] = row
        physical = json.loads(self.path.read_bytes())
        physical["admissions"][key] = row
        self.path.write_bytes(encoded(physical, compact=True))
        return state

    def assert_compacted(self, request):
        before = self.path.stat().st_size
        self.assertGreater(before, CONTEXT_LIMIT)
        self.assertLessEqual(before, MAINTENANCE_LIMIT)
        unchanged = deepcopy(request)
        with patch("pod.orca.read_command", side_effect=AssertionError("native call")), \
                patch("pod.ledger.require_authority", side_effect=AssertionError("native authority")), \
                patch("pod.github.GhPort", side_effect=AssertionError("provider call")):
            result = self.operate(request)
        self.assertEqual((result["status"], result["written"]), ("compacted", True))
        self.assertEqual(request, unchanged)
        self.assertEqual(_read(self.path), self.state)
        self.assertLess(self.path.stat().st_size, CONTEXT_LIMIT)
        self.assertEqual(self.other.read_bytes(), b"unrelated")
        receipt = self.receipts()[0]
        self.assertEqual(set(receipt), {"authority", "decision", "input_sha256", "logical_sha256"})
        self.assertEqual(receipt["authority"], request)
        self.assertEqual(receipt["decision"], digest(request))
        self.assertEqual(receipt["input_sha256"], request["facts"]["input_sha256"])
        self.assertEqual(receipt["logical_sha256"], digest(self.state))

    def test_coordinator_compacts_over_limit_without_a_decision(self):
        self.assert_compacted(self.requested())

    def test_project_lead_compacts_when_the_coordinator_is_live(self):
        self.assert_compacted(self.requested("project_lead"))

    def test_project_lead_compacts_without_a_live_coordinator(self):
        os.environ.pop("ORCA_TERMINAL_HANDLE", None)
        self.assert_compacted(self.requested("project_lead"))

    def test_detection_offers_a_coordinator_request_and_names_the_lead(self):
        before = self.files()
        with patch("pod.ledger._lock", side_effect=AssertionError("detection lock")):
            detected = self.operate()
        self.assertEqual(detected["status"], "compaction_available")
        self.assertIn("project lead or coordinator", detected["question"])
        self.assertNotIn("owner", detected["question"])
        action = detected["next_action"]
        self.assertEqual(action["operation"], "record-compaction")
        self.assertNotIn("decision", action["request"])
        request = action["request"]["request"]
        self.assertEqual(request["requested_by"], "coordinator")
        self.assertEqual(request["owner"], self.state["owner"])
        self.assertEqual(request["facts"]["input_sha256"], _sha(self.path.read_bytes()))
        self.assertEqual(self.files(), before)
        self.assertEqual(run(action["operation"], action["request"])["status"], "compacted")

    def test_wrong_coordinator_and_missing_lead_relay_refuse(self):
        request = self.requested()
        self.refused({**request, "owner": "another-coordinator"})
        request = self.requested("project_lead")
        del request["relay"]
        self.refused(request)

    def test_request_kind_and_authority_fields_are_discriminated(self):
        request = self.requested()
        for changes in ({"schema": "pod-compaction-request/v99"}, {"requested_by": "worker"},
                        {"relay": {"reference": "lead"}}, {"provenance": "user_direct"}):
            with self.subTest(fields=sorted(changes)):
                self.refused({**request, **changes})
        self.refused({**self.requested("project_lead"), "owner": self.state["owner"]})

    def test_reason_and_coordinator_identity_bounds_refuse(self):
        request = self.requested()
        for field, value in (("reason", ""), ("reason", "x" * 1025), ("reason", "\ud800"),
                             ("owner", "x" * 257)):
            with self.subTest(field=field):
                self.refused({**request, field: value})

    def test_lead_relay_is_exact_bounded_utf8_text(self):
        request = self.requested("project_lead")
        for relay in ({}, {"reference": ""}, {"reference": "x" * 513}, {"reference": "\ud800"},
                      {"reference": "lead", "owner_source": "unrelated"}):
            with self.subTest(fields=sorted(relay)):
                self.refused({**request, "relay": relay})

    def test_wrong_repository_or_objective_refuses(self):
        request = self.requested()
        self.refused({**request, "repository": "0" * 64})
        self.refused({**request, "objective": "different-objective"})

    def test_stale_record_and_revision_refuse(self):
        request = self.requested()
        for field, value in (("record", "0" * 64), ("revision", request["facts"]["revision"] + 1)):
            self.refused({**request, "facts": {**request["facts"], field: value}})

    def test_stale_physical_and_logical_digests_refuse(self):
        request = self.requested("project_lead")
        for field in ("input_sha256", "logical_sha256"):
            self.refused({**request, "facts": {**request["facts"], field: "0" * 64}})

    def test_malformed_facts_and_mixed_input_forms_refuse(self):
        request = self.requested()
        for facts in ({**request["facts"], "revision": True}, {**request["facts"], "extra": "unsupported"}, {}):
            self.refused({**request, "facts": facts})
        self.refused(request, decision={"provenance": "user_direct"})

    def test_coordinator_and_lead_replay_are_idempotent(self):
        request = self.requested()
        self.operate(request)
        before = self.files()
        self.assertEqual(self.operate(request)["status"], "replayed")
        self.assertEqual(self.files(), before)
        self.add_history()
        request = self.requested("project_lead")
        self.operate(request)
        before = self.files()
        self.assertEqual(self.operate(request)["status"], "replayed")
        self.assertEqual(self.files(), before)

    def test_replay_does_not_ignore_request_changes_or_later_logical_writes(self):
        request = self.requested("project_lead")
        self.operate(request)
        self.refused({**request, "reason": "a different reason"})
        self.refused({**request, "relay": {"reference": "another-relay"}})
        _write(self.path, _read(self.path))
        self.refused(request)

    def test_mixed_legacy_and_new_receipts_survive_later_writes(self):
        request = self.requested()
        decision = {key: request[key] for key in ("repository", "objective", "facts")}
        decision.update(provenance="user_direct", channel="session", instruction="Earlier scoped maintenance")
        self.assertEqual(self.operate(decision=decision)["status"], "compacted")
        previous_files, previous_receipts = self.files(), self.receipts()
        self.state = self.add_history()
        request = self.requested("project_lead")
        self.assertEqual(self.operate(request)["status"], "compacted")
        self.assertEqual(_read(self.path), self.state)
        receipts = self.receipts()
        self.assertEqual(receipts[:1], previous_receipts)
        self.assertEqual(receipts[1]["authority"], request)
        state = _read(self.path)
        _write(self.path, state)
        self.assertEqual(self.receipts(), receipts)
        for name, content in previous_files.items():
            if "/history/" in name:
                self.assertEqual(self.files()[name], content)

    def test_malformed_new_authority_or_receipt_binding_refuses_hydration(self):
        request = self.requested()
        self.operate(request)
        receipt = self.receipts()[0]
        for changes in ({"decision": "0" * 64}, {"input_sha256": "0" * 64},
                        {"logical_sha256": "0" * 64}, {"authority": {**request, "extra": True}}):
            with self.subTest(fields=sorted(changes)), self.assertRaises(PodError) as caught:
                _maintenance_valid([{**receipt, **changes}])
            self.assertEqual(caught.exception.code, "history_invalid")

    def test_detection_allowance_covers_maximum_escaped_lead_authority(self):
        detected = self.operate()
        request = self.requested("project_lead", detected=detected)
        request.update(reason="\x01" * 1024, relay={"reference": "\x01" * 512})
        result = self.operate(request)
        self.assertEqual(result["proposed_bytes"], detected["proposed_bytes"])
        self.assertEqual(self.receipts()[0]["authority"], request)
        self.assertEqual(_read(self.path), self.state)

    def test_detection_allowance_covers_maximum_escaped_coordinator_identity(self):
        self.state["owner"] = "\x01" * 256
        self.save()
        detected = self.operate()
        request = self.requested(detected=detected)
        request["reason"] = "\x01" * 1024
        result = self.operate(request)
        self.assertLessEqual(result["proposed_bytes"], detected["proposed_bytes"])
        self.assertEqual(_read(self.path), self.state)

    def test_receipt_allowance_includes_a_new_fragment_reference(self):
        request = self.requested("project_lead")
        request.update(reason="\x01" * 1024, relay={"reference": "\x01" * 512})
        receipts = []
        for index in range(4):
            authority = {**request, "reason": "\x01" * 1023 + str(index)}
            receipts.append({"authority": authority, "decision": digest(authority),
                             "input_sha256": authority["facts"]["input_sha256"], "logical_sha256": digest(self.state)})
        record, chunks = compact(self.state, force=True, maintenance=receipts)
        publish_context(self.path, record, chunks)
        self.state = self.add_history()
        detected = self.operate()
        request = self.requested("project_lead", detected=detected)
        request.update(reason="\x01" * 1024, relay={"reference": "\x01" * 512})
        result = self.operate(request)
        self.assertEqual(result["proposed_bytes"], detected["proposed_bytes"])
        physical = json.loads(self.path.read_bytes())
        self.assertEqual(len(physical["history"]["maintenance"]["$pod_history"]["parts"]), 2)
        self.assertEqual(len(self.receipts()), 5)
        self.assertEqual(_read(self.path), self.state)

    def test_request_rereads_physical_facts_under_the_objective_lock(self):
        request = self.requested()
        @contextmanager
        def change_before_lock(path):
            with _lock(path):
                path.write_bytes(path.read_bytes() + b" ")
                yield
        with patch("pod.ledger._lock", side_effect=change_before_lock):
            with self.assertRaises(PodError) as caught:
                self.operate(request)
        self.assertEqual(caught.exception.code, "compaction_decision_mismatch")
        self.assertFalse((self.path.parent / "history").exists())
        self.assertEqual(self.path.read_bytes(), encoded(self.state, compact=True) + b" ")

    def test_request_replans_when_governor_protections_change(self):
        request = self.requested()
        # An unreadable journal conservatively protects every admission and receipt.
        (self.path.parent / "governor.json").write_bytes(b"{")
        self.refused(request, "record_too_large")
        self.assertFalse((self.path.parent / "history").exists())

    def test_protected_admissions_and_selected_receipts_stay_inline(self):
        row = self.state["checkpoint"]["obligations"][0]
        row["executor"] = "attempt-048"
        row["evidence"] = [deepcopy(row["receipts"][0]["evidence"])]
        self.save()
        self.operate(self.requested())
        physical = json.loads(self.path.read_bytes())
        self.assertFalse(is_reference(physical["admissions"]["attempt-048"]))
        self.assertFalse(is_reference(physical["checkpoint"]["obligations"][0]["receipts"][0]))
        self.assertEqual(_read(self.path), self.state)

    def test_no_savings_adds_no_new_receipt_or_chunk(self):
        self.operate(self.requested())
        detected = self.operate()
        self.assertEqual(detected["status"], "unchanged")
        self.assertNotIn("question", detected)
        before = self.files()
        request = self.requested("project_lead", detected=detected)
        self.assertEqual(self.operate(request)["status"], "unchanged")
        self.assertEqual(self.files(), before)
        self.assertEqual(len(self.receipts()), 1)

    def test_impossible_fit_with_a_request_publishes_nothing(self):
        request = self.requested()
        self.state["source_rejections"] = {"unsettled": "x" * CONTEXT_LIMIT}
        self.save()
        request["facts"].update(input_sha256=_sha(self.path.read_bytes()), logical_sha256=digest(self.state))
        self.refused(request, "record_too_large")
        self.assertFalse((self.path.parent / "history").exists())

    def test_evidence_closed_record_refuses_before_lock_or_publication(self):
        request = self.requested("project_lead")
        (self.path.parent / "closure-evidence.json").write_bytes(b"{}\n")
        with patch("pod.ledger._lock", side_effect=AssertionError("closed record lock")):
            self.refused(request, "objective_closed")

    def test_continued_record_refuses_both_request_roles(self):
        requests = [self.requested(), self.requested("project_lead")]
        self.state["continued_by"] = {"objective": "successor", "record": "1" * 64, "owner": "next",
                                      "runs": ["synthetic-run"], "at": "2026-10-01T00:00:00Z",
                                      "instruction": "continue", "unfinished": [], "unreported": [],
                                      "ambiguity": [], "orca_claim": "unavailable"}
        self.save()
        for request in requests:
            self.refused(request, "objective_continued")

    def test_historically_retired_record_refuses_both_request_roles(self):
        requests = [self.requested(), self.requested("project_lead")]
        self.state["historical_retirement"] = {
            "at": "2026-10-01T00:00:00Z", "decision": "1" * 64, "instruction": "Retain delivered history",
            "reason": "Synthetic retirement", "repository": "synthetic", "objectives": ["synthetic-capacity"],
            "owner": self.state["owner"], "runs": [{"run": "synthetic-run", "generation": 1}],
            "released": ["/synthetic/project"], "preserved": [], "unfinished": [], "delivery": ["synthetic merge"],
            "native": {"runtime": "synthetic-runtime", "owner": self.state["owner"], "runs": 1,
                       "dispatches": 0, "exited_terminals": 1}}
        self.save()
        for request in requests:
            self.refused(request, "objective_retired")

    def test_ordinary_closed_record_still_allows_only_physical_maintenance(self):
        self.state["checkpoint"]["closure"] = {"closed_at": "synthetic-time"}
        self.save()
        self.assert_compacted(self.requested())
