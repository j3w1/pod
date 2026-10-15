"""R120/R5: genuine released writers and earlier readers retain decision meaning."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import patch

from pod.history import load_context, compact, publish_context
from pod.ledger import _validate_context, closed_snapshot_context, map_of
from pod.obligations import validate_closed_snapshot, report_projection, status_projection
from pod.util import context_decisions, map_decisions
from tests.kernel_support import KernelCase, USER, git, governance_commit


BASELINE = "60247346a4ee7306fbbbe47510adc5f11c4b4aa2"
REPOSITORY = Path(__file__).resolve().parents[1]


class DecisionCompatibilityTests(KernelCase):
    commit = governance_commit

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        parent = Path(os.environ["POD_CACHE_HOME"]) if "POD_CACHE_HOME" in os.environ else None
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        cls.bundle_home = tempfile.TemporaryDirectory(prefix="decision-compat-", dir=parent)
        cls.addClassCleanup(cls.bundle_home.cleanup)
        cls.bundle = Path(cls.bundle_home.name)
        archive = cls.bundle / "released.tar"
        subprocess.run(["git", "-C", str(REPOSITORY), "archive", "-o", str(archive), BASELINE, "skills", "VERSION"], check=True)
        with tarfile.open(archive) as source:
            source.extractall(cls.bundle, filter="data")

    def earlier(self, code, *args):
        result = subprocess.run([sys.executable, "-c", code, *map(str, args)], cwd=REPOSITORY,
                                env={**os.environ, "PYTHONPATH": os.pathsep.join((str(self.bundle / "skills"), str(REPOSITORY)))},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def released(self, kind):
        destination = self.root / kind
        destination.mkdir()
        self.earlier("from tests.decision_released_writer import main; main()", destination, kind)
        self.assertEqual(json.loads((destination / "provenance.json").read_text())["writer"], BASELINE)
        state = json.loads((destination / "objective.json").read_bytes())
        before = deepcopy(state)
        self.assertEqual(_validate_context(state), before)
        self.assertEqual(load_context(destination / "objective.json"), before)
        self.assertEqual(state, before)
        decisions = context_decisions(state) + map_decisions(state["checkpoint"])
        if kind != "admission":
            self.assertTrue(decisions)
        self.assertTrue(all(row["decision"]["channel"] == "session" and row["decision"]["channel_recorded"] is False for row in decisions))
        return destination, state

    def test_genuine_earlier_closed_record_validates_without_rewrite(self):
        destination, state = self.released("closed")
        project = destination / "project"
        before = deepcopy(state)
        ctx = closed_snapshot_context(project, "objective", state)
        self.assertEqual(validate_closed_snapshot(map_of(state), ctx), ())
        report = report_projection(map_of(state), ctx)
        waiver = report["label"]["withdrawn"][0]
        self.assertEqual((waiver["channel"], waiver["channel_recorded"]), ("session", False))
        rows = {row["id"]: row for row in status_projection(map_of(state), ctx)["map"]["obligations"]}
        self.assertEqual((rows["A"]["referent"]["channel"], rows["A"]["referent"]["channel_recorded"]), ("session", False))
        acceptance = rows["O1"]["acceptances"][0]
        self.assertEqual(acceptance, state["checkpoint"]["obligations"][0]["acceptances"][0])
        self.assertNotIn("channel", acceptance)
        self.assertEqual(state, before)

    def test_genuine_earlier_claim_reads_without_rewrite(self):
        destination, state = self.released("claim")
        self.assertNotIn("channel", state["continued_by"])
        successor = json.loads((destination / "successor.json").read_bytes())
        self.assertEqual(_validate_context(successor), successor)

    def test_genuine_earlier_retirement_reads_without_rewrite(self):
        _, state = self.released("retirement")
        self.assertNotIn("channel", state["historical_retirement"])

    def assert_earlier_refuses(self, state):
        path = self.root / "candidate.json"
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        result = self.earlier('''import json, sys
from pathlib import Path
from pod.ledger import _validate_context, _read, _write
from pod.errors import PodError
path = Path(sys.argv[1])
outcomes = []
for operation in (lambda: _validate_context(json.loads(path.read_bytes())), lambda: _write(path, json.loads(path.read_bytes()))):
    try:
        operation()
    except PodError as exc:
        outcomes.append(exc.code)
    else:
        outcomes.append("ACCEPTED")
print(json.dumps(outcomes))
''', path)
        self.assertNotIn("ACCEPTED", json.loads(result))
        self.assertEqual(path.read_bytes(), before)

    def test_earlier_reader_and_writer_refuse_derived_adoption(self):
        self.intake()
        target = self.commit("README.md", "sibling\n", "sibling")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        state = self.write(self.stored())
        from pod.ledger import read
        self.assert_earlier_refuses(read(self.project, "objective"))

    def test_earlier_reader_and_writer_refuse_relay_instead_of_misreading_session(self):
        from tests.test_decision_channels import RELAY
        self.intake(questions=["owner-question"])
        self.write(self.stored(), revision_authority={**USER, "question": "owner-question", "channel": "relayed", "relay": RELAY})
        from pod.ledger import read
        self.assert_earlier_refuses(read(self.project, "objective"))

    def test_earlier_reader_and_writer_refuse_new_session_governance_fields(self):
        self.intake(revision_authority=USER)
        from pod.ledger import read
        self.assert_earlier_refuses(read(self.project, "objective"))

    def test_genuine_earlier_reader_refuses_session_carried_consent(self):
        from pod.governor import _check_carried_consent, _read_journal, _record_path, validate_authorization
        from tests.test_carried_claim import CarriedCase
        patch.stopall()
        case = CarriedCase()
        case.setUp()
        try:
            case.lost_with(case.merge_decision)
            case.carry()
            path = _record_path(case.project, "successor")
            before = path.read_bytes()
            result = self.earlier('''import json, sys
from pathlib import Path
from pod import __version__
from pod.errors import PodError
from pod.governor import _read_journal, _check_carried_consent, validate_authorization
assert __version__ == "0.13.3"
try:
    journal = _read_journal(Path(sys.argv[1]))
except PodError as exc:
    print(json.dumps({"refused": exc.code}))
else:
    reasons = []
    _check_carried_consent({"authorization": validate_authorization(json.loads(sys.argv[2]))}, journal, reasons)
    print(json.dumps({"misread": [row["code"] for row in reasons]}))
''', path, json.dumps(case.consent))
            self.assertEqual(json.loads(result), {"refused": "invalid_governor"}, result)
            reasons = []
            _check_carried_consent({"authorization": validate_authorization(case.consent)}, _read_journal(path), reasons)
            self.assertEqual([row["code"] for row in reasons], ["authorization_missing"])
            self.assertEqual(path.read_bytes(), before)
            self.assert_earlier_refuses(case.state())
        finally:
            case.doCleanups()

    def test_earlier_reader_and_writer_refuse_new_claim_relay(self):
        from tests.test_succession import SuccessionCase
        from tests.test_decision_channels import RELAY
        patch.stopall()
        case = SuccessionCase()
        case.setUp()
        try:
            case.predecessor()
            case.successor()
            case.claim(case.decide(channel="relayed", relay=RELAY))
            self.assert_earlier_refuses(case.state())
        finally:
            case.doCleanups()

    def test_earlier_reader_and_writer_refuse_new_retirement_relay(self):
        from tests.test_historical_retirement import RetirementCase
        from tests.test_decision_channels import RELAY
        patch.stopall()
        case = RetirementCase()
        case.setUp()
        try:
            case.delivered()
            case.retire(case.decide(channel="relayed", relay=RELAY))
            self.assert_earlier_refuses(case.state())
        finally:
            case.doCleanups()

    def test_genuine_earlier_admission_loads_complete_without_rewrite(self):
        destination, state = self.released("admission")
        before = (destination / "objective.json").read_bytes()
        self.assertTrue(state["admissions"])
        self.assertEqual(load_context(destination / "objective.json"), state)
        self.assertEqual((destination / "objective.json").read_bytes(), before)

    def old_storage_refuses(self, state):
        record, chunks = compact(state, force=True, archive_retirement=True)
        self.assertTrue(chunks)
        path = self.root / "history-context.json"
        publish_context(path, record, chunks)
        before = path.read_bytes()
        result = self.earlier('''import json, sys
from pathlib import Path
from pod import __version__
from pod.ledger import _read, _write
from pod.errors import PodError
assert __version__ == "0.13.3"
path = Path(sys.argv[1])
outcomes = []
for operation in (lambda: _read(path), lambda: _write(path, _read(path))):
    try:
        operation()
    except PodError as exc:
        outcomes.append(exc.code)
    else:
        outcomes.append("ACCEPTED")
print(json.dumps(outcomes))
''', path)
        self.assertNotIn("ACCEPTED", json.loads(result))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(load_context(path), state)

    def test_earlier_reader_and_writer_refuse_admission_history(self):
        from tests.history_support import synthetic_state
        state = synthetic_state(count=1)
        state["admissions"]["attempt-000"]["role"] = "investigate"
        state["checkpoint"].pop("governance_history")
        state["checkpoint"]["obligations"] = []
        self.old_storage_refuses(state)

    def test_earlier_reader_and_writer_refuse_receipt_history(self):
        from pod.ledger import _empty
        from tests.test_obligations import proof
        state = _empty()
        state["checkpoint"] = {"schema": "pod-checkpoint/v3", "candidate": "c1", "obligations": [{"id": "O1", "kind": "criterion", "evidence": [],
            "receipts": [{"evidence": proof("O1", "c0"), "accepted_seq": 1}]}]}
        self.old_storage_refuses(state)

    def test_earlier_reader_and_writer_refuse_governance_history(self):
        from tests.history_support import synthetic_state
        state = synthetic_state(count=0)
        self.old_storage_refuses(state)

    def test_earlier_reader_and_writer_refuse_retirement_entry_history(self):
        _, state = self.released("retirement")
        self.old_storage_refuses(state)

    def test_combined_channel_revision_and_derived_decisions_survive_history(self):
        from tests.test_decision_channels import RELAY
        from tests.history_support import synthetic_state
        from pod.ledger import read
        from pod.util import encoded
        self.intake(questions=["owner-question"])
        self.write(self.stored(), revision_authority={**USER, "question": "owner-question", "channel": "relayed", "relay": RELAY})
        target = self.commit("README.md", "sibling\n", "sibling")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        self.write(self.stored())
        state = read(self.project, "objective")
        row = synthetic_state(count=1)["admissions"]["attempt-000"]
        row["role"] = "investigate"
        state["admissions"]["attempt-000"] = row
        state["checkpoint"]["native_refs"].append({"runId": row["run_id"], "runtime": row["runtime"]})
        record, chunks = compact(state, force=True)
        path = self.root / "combined.json"
        publish_context(path, record, chunks)
        restored = load_context(path)
        self.assertEqual(encoded(restored, compact=True), encoded(state, compact=True))
        self.assertEqual(context_decisions(restored) + map_decisions(restored["checkpoint"]),
                         context_decisions(state) + map_decisions(state["checkpoint"]))
        self.assert_earlier_refuses(record)
