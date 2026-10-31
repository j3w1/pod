"""Public decision compaction and retained correction resolution in disposable fixtures."""
from tests.common import logical_json
from tests.history_support import compact, admission_movable

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


class MaintenanceReaderTests(KernelCase):
    """The genuine preceding writer supplies decoder controls, never a current maintenance writer."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.large = synthetic_state(count=12)
        temporary = tempfile.TemporaryDirectory(prefix="compaction-preceding-")
        cls.addClassCleanup(temporary.cleanup)
        cls.previous = Path(temporary.name)
        archive = cls.previous / "source.tar"
        repository = Path(__file__).resolve().parents[1]
        subprocess.run(["git", "-C", str(repository), "archive", "-o", str(archive), PRECEDING, "skills", "VERSION"], check=True)
        with tarfile.open(archive) as source:
            source.extractall(cls.previous, filter="data")

    def setUp(self):
        super().setUp()
        self.path = objective_root(self.project, "synthetic-capacity") / "context.json"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(encoded(self.large, compact=True))
        program = """import json,sys
from pod.internal import run
request={'project':sys.argv[1], 'objective':'synthetic-capacity'}
facts=run('record-compaction',request)['facts']
decision={'provenance':'user_direct','instruction':'Preserve all recorded work', 'channel':'session',
          'repository':facts['repository'],'objective':facts['objective'],
          'facts':{key:facts[key] for key in ('record','revision','input_sha256','logical_sha256')}}
print(json.dumps(run('record-compaction',{**request,'decision':decision})))
"""
        result = subprocess.run([sys.executable, "-c", program, str(self.project)], cwd=self.previous,
            env={**os.environ, "PYTHONPATH": str(self.previous / "skills")}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "compacted")

    def test_genuine_preceding_writer_receipt_reads_without_conversion(self):
        before = self.path.read_bytes()
        audit = history.audit_context(self.path)
        self.assertEqual(audit["state"], self.large)
        self.assertEqual(set(audit["maintenance"][0]["authority"]), {"provenance", "instruction", "channel"})
        self.assertEqual(self.path.read_bytes(), before)

    def test_source_bound_migration_preserves_preceding_authority_and_logical_state(self):
        before = history.audit_context(self.path)
        result = history.migrate_context(self.path, revision=self.large["revision"],
            sha256=history._sha(self.path.read_bytes()), project=self.project)
        after = history.audit_context(self.path)
        self.assertEqual(result["status"], "migrated")
        self.assertEqual(after["state"], before["state"])
        self.assertEqual(after["maintenance"], before["maintenance"])

    def test_partial_old_receipt_cannot_be_upgraded_to_agent_authority(self):
        receipt = history.audit_context(self.path)["maintenance"][0]
        receipt["authority"]["provenance"] = "agent_decision"
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
            triage=[{"finding": "F1", "severity": "major", "triage": "required_correction", "blocks": "outcome",
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
        self.assertTrue(admission_movable(self.state, self.key, row))
        record, chunks = compact(self.state, trigger_bytes=0)
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
        self.assertFalse(admission_movable(state, key, state["admissions"][key]))
