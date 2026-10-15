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
        self.assertEqual(state, before)
        decisions = context_decisions(state) + map_decisions(state["checkpoint"])
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
        self.assertEqual((acceptance["channel"], acceptance["channel_recorded"]), ("session", False))
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
from pod.ledger import _validate_context, _write
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
