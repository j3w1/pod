"""Public decision compaction and retained correction resolution in disposable fixtures."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from pod import config, history, relay
from pod.errors import PodError
from pod.internal import run
from pod.ledger import _read, objective_root, read, state_root
from pod.lineage import Lineage
from pod.util import digest, encoded
from tests import issue41_support as production
from tests import test_records_boundaries as renewal
from tests.decision_policy_support import LOCAL, PERSON, RELAY, VERIFIER, MachineCase, save_vouch
from tests.history_support import synthetic_state
from tests.kernel_support import KernelCase


PRECEDING = "438dde67737a68c6f1f175f11ed9c2d00c857e71"
WAITING = {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}


class CompactionCase(KernelCase):
    """Shared builders only; importing this base discovers no unrelated tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.large = synthetic_state(count=12)

    def setUp(self):
        super().setUp()
        self.state = deepcopy(self.large)
        self.path = objective_root(self.project, "synthetic-capacity") / "context.json"
        self.path.parent.mkdir(parents=True)
        (self.path.parent / ".lock").touch()
        self.path.write_bytes(encoded(self.state, compact=True))
        self.controls = self.root / "compaction-controls.json"
        self.controls.write_text("{}")
        self.verifier = self.root / "compaction-verifier.py"
        self.verifier.write_text(VERIFIER)

    configure_verifier = MachineCase.configure_verifier

    def operate(self, *, decision=None, request=None):
        value = {"project": str(self.project), "objective": "synthetic-capacity"}
        if decision is not None:
            value["decision"] = decision
        if request is not None:
            value["request"] = request
        return run("record-compaction", value)

    def decision(self, source=RELAY):
        facts = self.operate()["facts"]
        return {**deepcopy(source), "repository": facts["repository"], "objective": facts["objective"],
                "facts": {key: facts[key] for key in ("record", "revision", "input_sha256", "logical_sha256")}}

    def request(self, role="coordinator"):
        value = deepcopy(self.operate()["next_action"]["request"]["request"])
        value["reason"] = "Keep the exact logical work while relocating settled history"
        if role == "project_lead":
            value.pop("owner")
            value.update(requested_by=role, relay={"reference": "fixture-lead-request"})
        return value

    def files(self):
        return {str(path.relative_to(state_root())): path.read_bytes()
                for path in state_root().rglob("*") if path.is_file()}

    def uses(self):
        return {path.name: path.read_bytes() for path in (state_root() / ".relay-authorizations").glob("*.json")}

    def receipts(self):
        return history.capacity(self.path, _read(self.path))["maintenance"]

    def prepared(self):
        self.configure_verifier()
        decision = self.decision()
        before = self.files()
        vouch = save_vouch(self, lambda: self.operate(decision=decision))
        self.assertEqual(self.files(), before)
        return decision, vouch

    def refused(self, decision, code):
        before = self.files()
        with self.assertRaises(PodError) as caught:
            self.operate(decision=decision)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.files(), before)


class VerifiedCompactionTests(CompactionCase):
    def test_saved_agent_vouch_compacts_without_a_recorded_question_losslessly(self):
        decision, _ = self.prepared()
        self.assertFalse(self.state["checkpoint"].get("questions"))
        result = self.operate(decision=decision)
        self.assertEqual((result["status"], result["written"]), ("compacted", True))
        self.assertEqual(_read(self.path), self.state)
        physical = json.loads(self.path.read_bytes())
        self.assertTrue(any(history.is_reference(row) for row in physical["admissions"].values()))
        self.assertEqual(physical["history"]["decision_channels"], 5)
        authority = self.receipts()[0]["authority"]
        self.assertEqual((authority["provenance"], authority["channel"]), ("agent_decision", "relayed"))
        self.assertEqual(authority["actor"]["kind"], "agent")
        self.assertEqual(authority["decision_binding"]["kind"], "compaction")
        self.assertEqual(len(self.uses()), 1)

    def test_local_cooperative_compaction_keeps_its_truthful_actor(self):
        config.set_decision_policy(self.project, "delegate", decision=PERSON)
        result = self.operate(decision=self.decision(LOCAL))
        self.assertEqual((result["status"], result["written"]), ("compacted", True))
        authority = self.receipts()[0]["authority"]
        self.assertEqual(authority["actor"], {"kind": "agent", "channel": "agent_session"})
        self.assertNotIn("decision_verification", authority)
        self.assertEqual(_read(self.path), self.state)
        self.assertEqual(json.loads(self.path.read_bytes())["history"]["decision_channels"], 5)

    def test_routine_remains_any_agent_under_all_presets(self):
        from pod.policy import preset
        for name in ("careful", "delegate", "hands_off"):
            with self.subTest(preset=name):
                self.assertEqual(preset(name)["routine"], "any_agent")
        custom = preset("careful")
        self.assertEqual(preset("custom", custom)["routine"], "any_agent")

    def bad_vouch(self, path, value):
        decision, vouch = self.prepared()
        target = vouch
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
        self.controls.write_text(json.dumps({"saved_verdict": vouch}))
        self.refused(decision, "relay_verifier_mismatch")

    def test_stale_payload_refuses_without_receipt_or_state_effect(self):
        self.bad_vouch("decision.payload_sha256", "f" * 64)

    def test_stale_state_refuses_without_receipt_or_state_effect(self):
        self.bad_vouch("decision.state_sha256", "f" * 64)

    def test_scope_for_another_record_refuses_without_effect(self):
        self.bad_vouch("principal.scope.resources", {"record": "another-record", "revision": "1"})

    def test_vouch_for_another_kind_refuses_without_effect(self):
        self.bad_vouch("principal.scope.kinds", ["withdrawal"])

    def test_absent_verifier_never_turns_relay_into_session_authority(self):
        self.refused(self.decision(), "relay_verifier_unconfigured")

    def test_public_input_refuses_forged_stored_metadata(self):
        decision = self.decision(LOCAL)
        for field, value in (("actor", {"kind": "person"}), ("decision_verification", {"verified": True}),
                             ("decided_at", "2026-10-01T00:00:00Z")):
            with self.subTest(field=field):
                self.refused({**decision, field: value}, "compaction_decision_mismatch")

    def test_actual_record_preflight_failure_precedes_receipt_and_publication(self):
        decision, _ = self.prepared()
        before = self.files()
        original, records = history._publish_compaction_decision, []
        def bounded(path, state, raw, record, chunks, capability, receipts):
            records.append(record)
            # Exercise the real size check on the complete prepared record.
            with patch.object(history, "CONTEXT_LIMIT", len(encoded(record, compact=True)) - 1):
                return original(path, state, raw, record, chunks, capability, receipts)
        with patch.object(history, "_publish_compaction_decision", side_effect=bounded), self.assertRaises(PodError) as caught:
            self.operate(decision=decision)
        self.assertEqual(caught.exception.code, "record_too_large")
        self.assertEqual(len(records), 1)
        self.assertEqual(self.files(), before)

    def test_receipt_before_publication_recovers_only_identical_transition(self):
        decision, _ = self.prepared()
        before = self.path.read_bytes()
        with patch.object(history, "publish_context", side_effect=OSError("fixture before publication")):
            with self.assertRaises(OSError):
                self.operate(decision=decision)
        self.assertEqual(self.path.read_bytes(), before)
        uses = self.uses()
        self.assertEqual(len(uses), 1)
        self.refused({**decision, "instruction": "A different transition"}, "relay_verifier_mismatch")
        result = self.operate(decision=decision)
        self.assertEqual((result["status"], result["written"]), ("compacted", True))
        self.assertEqual(self.uses(), uses)
        self.assertEqual(_read(self.path), self.state)
        files = self.files()
        replay = self.operate(decision=decision)
        self.assertEqual((replay["status"], replay["written"]), ("replayed", False))
        self.assertEqual(self.files(), files)

    def test_response_loss_after_atomic_replace_recovers_committed_readback(self):
        decision, _ = self.prepared()
        publish = history.publish_context
        def committed(*args, **kwargs):
            publish(*args, **kwargs)
            raise OSError("fixture response lost after atomic replacement")
        with patch.object(history, "publish_context", side_effect=committed), self.assertRaises(OSError):
            self.operate(decision=decision)
        self.assertEqual(_read(self.path), self.state)
        files, uses = self.files(), self.uses()
        replay = self.operate(decision=decision)
        self.assertEqual((replay["status"], replay["written"]), ("replayed", False))
        self.assertEqual(len(uses), 1)
        self.assertEqual(self.files(), files)
        self.assertEqual(self.uses(), uses)


class MaintenanceReaderTests(CompactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.later = synthetic_state(count=49)["admissions"]["attempt-048"]
        temporary = tempfile.TemporaryDirectory(prefix="compaction-preceding-")
        cls.addClassCleanup(temporary.cleanup)
        cls.previous = Path(temporary.name)
        archive = cls.previous / "source.tar"
        repository = Path(__file__).resolve().parents[1]
        subprocess.run(["git", "-C", str(repository), "archive", "-o", str(archive), PRECEDING, "skills", "VERSION"], check=True)
        with tarfile.open(archive) as source:
            source.extractall(cls.previous, filter="data")
        assert (cls.previous / "VERSION").read_text().strip() == "0.21.16"

    def preceding(self, script, value=None):
        return subprocess.run([sys.executable, "-c", script, str(self.path)], input=json.dumps(value),
            cwd=self.previous, env={**os.environ, "PYTHONPATH": str(self.previous / "skills"),
                                   "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=10)

    def assert_preceding_readable(self):
        before = self.files()
        result = self.preceding("from pathlib import Path; import sys; from pod.ledger import _read; from pod.util import digest; print(digest(_read(Path(sys.argv[1]))))")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), digest(self.state))
        self.assertEqual(self.files(), before)

    def ordinary(self, role):
        result = self.operate(request=self.request(role))
        self.assertEqual((result["status"], result["written"]), ("compacted", True))
        self.assertTrue(any(history.is_reference(row) for row in json.loads(self.path.read_bytes())["admissions"].values()))
        self.assertNotIn("decision_channels", json.loads(self.path.read_bytes())["history"])
        self.assertEqual(_read(self.path), self.state)
        self.assert_preceding_readable()

    def test_coordinator_request_does_not_upgrade_an_older_record(self):
        self.ordinary("coordinator")

    def test_project_lead_request_does_not_upgrade_an_older_record(self):
        self.ordinary("project_lead")

    def test_session_compaction_keeps_exact_three_field_receipt_and_reader_floor(self):
        result = self.operate(decision=self.decision(PERSON))
        self.assertEqual((result["status"], result["written"]), ("compacted", True))
        self.assertEqual(set(self.receipts()[0]["authority"]), {"provenance", "instruction", "channel"})
        self.assertNotIn("decision_channels", json.loads(self.path.read_bytes())["history"])
        self.assert_preceding_readable()

    def test_genuine_preceding_writer_receipt_reads_without_conversion(self):
        value = {"project": str(self.project), "objective": "synthetic-capacity", "decision": self.decision(PERSON)}
        result = self.preceding("import json,sys; from pod.internal import run; print(json.dumps(run('record-compaction', json.load(sys.stdin))))", value)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "compacted")
        before = self.files()
        receipts = self.receipts()
        self.assertEqual(set(receipts[0]["authority"]), {"provenance", "instruction", "channel"})
        self.assertEqual(history._maintenance_valid(receipts), receipts)
        self.assertEqual(_read(self.path), self.state)
        self.assertEqual(self.files(), before)

    def test_ordinary_request_never_erases_an_existing_new_evidence_marker(self):
        decision, _ = self.prepared()
        self.operate(decision=decision)
        first = deepcopy(self.receipts()[0])
        physical = json.loads(self.path.read_bytes())
        for index in range(3):
            later = deepcopy(self.later)
            key = f"later-settled-{index}"
            later["admission_id"] = key
            physical["admissions"][key] = later
            self.state["admissions"][key] = later
        self.path.write_bytes(encoded(physical, compact=True))
        result = self.operate(request=self.request("project_lead"))
        self.assertEqual((result["status"], result["written"]), ("compacted", True))
        physical = json.loads(self.path.read_bytes())
        self.assertEqual(physical["history"]["decision_channels"], 5)
        self.assertTrue(all(history.is_reference(physical["admissions"][f"later-settled-{index}"]) for index in range(3)))
        self.assertEqual(self.receipts()[0], first)
        self.assertEqual(_read(self.path), self.state)
        before = self.files()
        result = self.preceding("from pathlib import Path; import sys; from pod.ledger import _read; _read(Path(sys.argv[1]))")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("history_invalid", result.stderr)
        self.assertEqual(self.files(), before)

    def test_partial_or_forged_old_receipt_is_not_upgraded_into_authority(self):
        authority = {"provenance": "user_direct", "instruction": "Compact the exact synthetic record", "channel": "session"}
        receipt = {"authority": authority, "decision": digest(authority), "input_sha256": "a" * 64, "logical_sha256": "b" * 64}
        for changed in ({key: value for key, value in authority.items() if key != "channel"},
                        {**authority, "actor": {"kind": "person"}}, {**authority, "verified": True}):
            with self.subTest(fields=sorted(changed)), self.assertRaises(PodError) as caught:
                history._maintenance_valid([{**receipt, "authority": changed}])
            self.assertEqual(caught.exception.code, "history_invalid")

    def test_other_kind_stored_evidence_is_not_a_compaction_receipt(self):
        decision, _ = self.prepared()
        self.operate(decision=decision)
        receipt = deepcopy(self.receipts()[0])
        authority = receipt["authority"]
        authority["decision_binding"]["kind"] = "revision"
        authority["decision_binding"]["kinds"] = ["revision"]
        authority["decision_verification"]["decision"]["kind"] = "revision"
        authority["decision_verification"]["decision"]["kinds"] = ["revision"]
        authority["decision_verification"]["principal"]["scope"]["kinds"] = ["revision"]
        authority["actor"]["scope"]["kinds"] = ["revision"]
        relay.validate_decision_record(authority, kind="revision")
        with self.assertRaises(PodError) as caught:
            history._maintenance_valid([receipt])
        self.assertEqual(caught.exception.code, "history_invalid")


def review_fixture():
    """Reuse the real renewal builder without inheriting its discovered tests."""
    case = production.ProductionCase()
    case.setUp()
    try:
        renewal.RenewalAssuranceTests.policy(case)
        packet = case.packet(["A"], role="review")
        admission = case.start("review", packet)["admission"]
        case.settle(admission)
        seq = read(case.project, "objective")["checkpoint"]["seq"]
        case.report(admission, packet, outcome="failed", map={"seq": seq + 1, "update": {
            "A": WAITING, "K": {"kind": "correction", "provenance": "coordinator", "parent": "A",
                "check": "repair the defect", "proof_scope": ["src"], **WAITING}}},
            triage=[{"finding": "F1", "severity": "major", "triage": "required_correction",
                     "correction": "K", "summary": "repair required"}])
        case.write(renew=[{"id": "A", "as": "A2"}])
        case.controls = case.root / "withdrawal-controls.json"
        case.controls.write_text("{}")
        case.verifier = case.root / "withdrawal-verifier.py"
        case.verifier.write_text(VERIFIER)
        MachineCase.configure_verifier(case)
        return case, admission["admission_id"]
    except BaseException:
        case.doCleanups()
        raise


def withdraw(case):
    seq = read(case.project, "objective")["checkpoint"]["seq"] + 1
    return case.write(seq=seq, update={"K": {"state": "withdrawn", "withdrawal": {
        "by": "agent_decision", "reason": "Exact scoped correction withdrawal"}}}, revision_authority=RELAY)


class AgentWithdrawalHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        case, cls.key = review_fixture()
        cls.addClassCleanup(case.doCleanups)
        config.set_decision_policy(case.project, "delegate", decision=PERSON)
        save_vouch(case, lambda: withdraw(case))
        cls.accepted = withdraw(case)
        cls.accepted_seq = cls.accepted["checkpoint"]["seq"]
        case.move()
        case.write(seq=cls.accepted_seq + 1)
        cls.state = read(case.project, "objective")

    def correction(self, state):
        return next(row for row in state["checkpoint"]["obligations"] if row["id"] == "K")

    def test_public_agent_withdrawal_retains_its_actual_epoch_and_actor(self):
        withdrawal = self.correction(self.state)["withdrawal"]
        self.assertEqual(withdrawal["by"], "agent_decision")
        self.assertEqual(withdrawal["actor"]["kind"], "agent")
        self.assertEqual(withdrawal["accepted_seq"], self.accepted_seq)
        self.assertEqual(withdrawal["decision_binding"]["resources"]["map_seq"], str(self.accepted_seq))
        rows = self.state["checkpoint"]["obligations"]
        frozen = next(row for row in rows if row["id"] == "A")
        self.assertEqual(frozen["state"], "renewed")
        self.assertNotIn("resolved_seq", frozen["findings"][0])
        self.assertEqual(Lineage(rows).findings("A")[0]["resolved_seq"], self.accepted_seq)

    def test_resolved_superseded_review_relocates_and_hydrates_losslessly(self):
        row = self.state["admissions"][self.key]
        self.assertNotEqual(row["candidate"], self.state["checkpoint"]["candidate"])
        self.assertTrue(history.admission_movable(self.state, self.key, row))
        record, chunks = history.compact(self.state, trigger_bytes=0)
        self.assertEqual((self.state["decision_channels"], record["decision_channels"]), (5, 5))
        self.assertTrue(history.is_reference(record["admissions"][self.key]))
        self.assertEqual(history.hydrate(record, chunks.__getitem__), self.state)

    def test_forged_resolution_sequence_is_rejected_before_history(self):
        state = deepcopy(self.state)
        self.correction(state)["withdrawal"]["accepted_seq"] += 1
        with self.assertRaises(PodError) as caught:
            Lineage(state["checkpoint"]["obligations"]).findings("A")
        self.assertEqual(caught.exception.code, "invalid_decision")

    def test_person_actor_cannot_be_relabelled_as_agent_withdrawal(self):
        state = deepcopy(self.state)
        authority = self.correction(state)["withdrawal"]
        authority["actor"]["kind"] = "person"
        authority["provenance"] = "user_direct"
        authority["decision_verification"]["principal"]["kind"] = "person"
        relay.validate_decision_record(authority)
        with self.assertRaises(PodError):
            Lineage(state["checkpoint"]["obligations"]).findings("A")

    def test_wrong_kind_cannot_resolve_a_retained_correction(self):
        state = deepcopy(self.state)
        authority = self.correction(state)["withdrawal"]
        authority["decision_binding"]["kind"] = "compaction"
        authority["decision_binding"]["kinds"] = ["compaction"]
        authority["decision_verification"]["decision"]["kind"] = "compaction"
        authority["decision_verification"]["decision"]["kinds"] = ["compaction"]
        authority["decision_verification"]["principal"]["scope"]["kinds"] = ["compaction"]
        authority["actor"]["scope"]["kinds"] = ["compaction"]
        relay.validate_decision_record(authority, kind="compaction")
        with self.assertRaises(PodError) as caught:
            Lineage(state["checkpoint"]["obligations"]).findings("A")
        self.assertEqual(caught.exception.code, "invalid_decision")

    def test_careful_denial_never_reaches_lineage_or_history(self):
        case, key = review_fixture()
        self.addCleanup(case.doCleanups)
        save_vouch(case, lambda: withdraw(case))
        before = {path: path.read_bytes() for path in state_root().rglob("*") if path.is_file()}
        with patch.object(history, "publish_context", side_effect=AssertionError("denied publication")):
            with self.assertRaises(PodError) as caught:
                withdraw(case)
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual({path: path.read_bytes() for path in state_root().rglob("*") if path.is_file()}, before)
        state = read(case.project, "objective")
        self.assertNotIn("withdrawal", self.correction(state))
        self.assertNotIn("resolved_seq", Lineage(state["checkpoint"]["obligations"]).findings("A")[0])
        self.assertFalse(history.admission_movable(state, key, state["admissions"][key]))
