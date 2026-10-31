"""Retained Governor evidence grows through the same immutable revision store."""
from copy import deepcopy
import errno
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.governor import (_compact, _read_journal, _record_path, _write_journal,
                          load_admission_effects, prepare_candidate)
from pod.history import (audit_context, migrate_context)
from tests import test_governor as support
from tests.kernel_support import NOW, action, observation


class JournalScaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = cls.case = support.GovernorCase(); case.setUp()
        cls.addClassCleanup(case.doCleanups)
        case.prepared()
        cls.path = _record_path(case.project, "objective")
        original = _read_journal(cls.path)
        migrate_context(cls.path, kind="governor", revision=original["revision"],
                        sha256=hashlib.sha256(cls.path.read_bytes()).hexdigest())
        # Synthetic immutable settled evidence. It has no current-candidate key
        # and cannot qualify the real next production decision below.
        journal = _read_journal(cls.path)
        journal["actions"] = [{"record_id": f"past-{n}", "logical_key": f"past-key-{n}", "attempt": 1,
            "action": {"kind": "workflow_dispatch", "candidate": f"past-candidate-{n}", "target": "ci.yml", "unit": "release"},
            "candidate_id": f"past-candidate-{n}", "decision": "ALLOW", "outcome": "PASS",
            "receipt": {"provider": {"observations": f"synthetic-{n}:" + "x" * 4096}},
            "warnings": [], "reasons": [], "classifications": []} for n in range(128)]
        _write_journal(cls.path, journal)
        cls.initial_actions = [row["record_id"] for row in _read_journal(cls.path)["actions"]]
        for n in range(20):
            binding = prepare_candidate(case.project, "objective", owner="owner", unit="release",
                observation=observation(workflows={".github/workflows/ci.yml": f"{n + 1:064x}"}), now=NOW)["candidate"]
        cls.unit_bindings = {}
        for n in range(35):
            cls.unit_bindings[f"unit-{n}"] = prepare_candidate(case.project, "objective", owner="owner",
                unit=f"unit-{n}", observation=observation(), now=NOW)["candidate"]
        case.preflight(binding["id"])
        cls.decision = case.decide(action(candidate=binding["id"], effects=[]))
        if cls.decision["decision"] != "ALLOW": raise AssertionError(cls.decision)
        cls.pending_facts = load_admission_effects(cls.path)
        cls.outcome = case.performed(cls.decision["record_id"])
        cls.audit = audit_context(cls.path, kind="governor")
        cls.saved = {path.relative_to(cls.path.parent): path.read_bytes() for path in cls.path.parent.rglob("*")
                     if path.is_file() and path.name != ".lock"}

    def cloned(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / self.path.parent.name / "governor.json"
        for relative, raw in self.saved.items():
            destination = path.parent / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        return path

    def test_next_governed_operation_completes_beyond_action_unit_and_candidate_limits(self):
        self.assertEqual(self.decision["decision"], "ALLOW")
        state = self.audit["state"]
        self.assertGreater(len(state["actions"]), 128)
        self.assertGreater(len(state["units"]), 32)
        self.assertGreater(len(state["units"]["release"]["history"]), 16)
        self.assertTrue(all(state["units"][name]["candidate"] == binding for name, binding in self.unit_bindings.items()))
        self.assertEqual([row["record_id"] for row in state["actions"][:128]], self.initial_actions)
        selected = next(row for row in state["actions"] if row["record_id"] == self.decision["record_id"])
        self.assertEqual(selected["outcome"], "PASS")
        self.assertTrue(self.audit["complete"])
        self.assertGreater(self.audit["logical_bytes"], 256 * 1024)

    def test_effect_facts_retain_current_pending_claims_without_settled_bodies(self):
        self.assertEqual(self.pending_facts["schema"], "pod-admission-effects/v1")
        self.assertTrue(self.pending_facts["complete"])
        self.assertEqual(len(self.pending_facts["units"]), 36)
        self.assertIn(self.decision["record_id"], [row["record_id"] for row in self.pending_facts["actions"]])
        self.assertTrue(all("receipt" not in row for row in self.pending_facts["actions"]))

    def test_missing_old_receipt_does_not_hide_current_effects_but_fails_full_audit(self):
        path = self.cloned()
        state = _read_journal(path)
        reference = state["actions"][0]["receipt"].reference
        (path.parent / "history/sha256" / (reference["sha256"] + ".json")).unlink()
        self.assertTrue(load_admission_effects(path)["complete"])
        with self.assertRaises(PodError) as caught: audit_context(path, kind="governor")
        self.assertEqual(caught.exception.code, "history_missing")

    def test_stale_journal_writer_cannot_replace_a_new_revision(self):
        path = self.cloned()
        first, stale = _read_journal(path), _read_journal(path)
        _write_journal(path, first)
        accepted = path.read_bytes()
        with self.assertRaises(PodError) as caught: _write_journal(path, stale)
        self.assertEqual(caught.exception.code, "map_stale")
        self.assertEqual(path.read_bytes(), accepted)

    def test_journal_disk_full_preserves_prior_authority(self):
        path = self.cloned()
        prior = path.read_bytes()
        revision = _read_journal(path)["revision"]
        with patch("pod.history.atomic_json", side_effect=OSError(errno.ENOSPC, "injected full disk")):
            with self.assertRaises(PodError) as caught: _write_journal(path, _read_journal(path))
        self.assertEqual(caught.exception.code, "storage_full")
        self.assertEqual(path.read_bytes(), prior)
        _write_journal(path, _read_journal(path))
        self.assertEqual(_read_journal(path)["revision"], revision + 1)
        self.assertTrue(load_admission_effects(path)["complete"])

    def test_current_in_flight_budget_still_refuses_without_evicting_history(self):
        state = deepcopy(self.audit["state"])
        state["actions"] = [dict(state["actions"][0], record_id=f"current-{n}", outcome="UNKNOWN") for n in range(96)]
        before = deepcopy(state)
        with self.assertRaises(PodError) as caught: _compact(state)
        self.assertEqual(caught.exception.code, "governor_full")
        self.assertEqual(state, before)

    def test_missing_declared_effects_do_not_become_an_explicit_empty_declaration(self):
        path = self.cloned()
        state = _read_journal(path)
        row = state["actions"][-1]
        row["outcome"] = "UNKNOWN"
        row["action"].pop("effects", None)
        row.pop("effects", None)
        _write_journal(path, state)
        projected = next(item for item in load_admission_effects(path)["actions"] if item["record_id"] == row["record_id"])
        self.assertIsNone(projected["action"]["effects"])
        self.assertIsNone(projected["effects"])

    def test_declared_and_admitted_effect_identities_survive_projection(self):
        path = self.cloned()
        state = _read_journal(path)
        row = state["actions"][-1]
        row["outcome"] = "UNKNOWN"
        row["action"]["effects"] = ["declared:shared-target"]
        row["effects"] = ["declared:shared-target", "mapped:release-target"]
        _write_journal(path, state)
        projected = next(item for item in load_admission_effects(path)["actions"] if item["record_id"] == row["record_id"])
        self.assertEqual(projected["action"]["effects"], row["action"]["effects"])
        self.assertEqual(projected["effects"], row["effects"])

    def test_absent_journal_has_an_explicit_empty_generation(self):
        path = self.cloned().with_name("absent.json")
        facts = load_admission_effects(path)
        self.assertEqual(facts["generation"], {"revision": 0, "sha256": hashlib.sha256(b"").hexdigest()})
        self.assertTrue(facts["complete"])
        self.assertEqual(facts["actions"], [])
