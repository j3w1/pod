"""Acceptance-lineage recovery through disposable production entrypoints."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import patch

from tests.test_issue_revision_currency import ALPHA, BETA, CHECK, OWNER, CurrencyCase


class ScopeRecoveryCase(CurrencyCase):
    def begin_unreadable(self, *, worker=False):
        self.issue.body = ALPHA
        source = self.source()
        self.issue.unreadable = True
        rows = [self.criterion(check=CHECK)] + ([self.sub("S")] if worker else [])
        self.write(rows, objective_source=source, governance={"base_ref": "target"})

    def select_proof(self):
        rows = self.stored()
        for key in ("external", "wait", "executor"):
            rows[0].pop(key, None)
        rows[0].update(state="satisfied", evidence=[self.proof("O1", reference="first-proof")])
        if len(rows) > 1 and rows[1]["id"] == "S":
            rows[1].pop("wait", None)
            rows[1].update(state="active", executor="coordinator")
        return rows

    def drop_proof(self):
        original = deepcopy(self.row())
        rows = self.stored()
        rows[0].pop("evidence")
        rows[0].update(state="active", executor="coordinator")
        if len(rows) > 1 and rows[1]["id"] == "S":
            rows[1].pop("executor", None)
            rows[1].update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.write(rows)
        return original


class IntakeScopeRecoveryTests(ScopeRecoveryCase):
    def test_iu1_exact_intake_data_keeps_unchanged_proof_and_own_tick(self):
        self.begin_unreadable()
        self.observe(ALPHA)
        self.accept()
        self.tick()
        original = deepcopy(self.row())
        ticks = deepcopy(self.state()["pod_ticks"])
        proposal = self.proposal_for(self.issue.body)
        self.assert_unknown(proposal, "prior_source_association_unavailable")
        decoy = {**proposal, "issue_revision_data": self.historical_data(BETA)}
        error = self.refused_revision(decoy, "issue_revision_refused")
        self.assertEqual(error.detail["reason"], "body_data_unreferenced")
        proposal["issue_revision_data"] = self.historical_data(ALPHA)
        self.revise(proposal)
        self.assertEqual(self.row(), original)
        decision = self.state()["checkpoint"]["revision_decisions"][-1]
        self.assertEqual(decision["previous"]["pod_ticks"], ticks)
        self.assertEqual((self.line()["proven"], self.line()["authorship"], self.line()["due"]),
                         (True, "own", False))

    def test_iu2_running_first_admission_keeps_criterion_with_exact_data(self):
        self.begin_unreadable()
        self.port.state = "running"
        frozen = self.packet(["O1"])
        admission = self.start("existing-task", frozen)["admission"]
        before = self.state()
        bound = before["admissions"][admission["admission_id"]]
        self.assertEqual(bound["admitted_seq"], 2)
        self.assertEqual(bound["recovery"]["checkpoint_binding"]["objective_source"],
                         before["checkpoint"]["objective_source"])
        proposal = self.proposal_for(ALPHA)
        # Try retirement before acceptance consumes the revision key, while the
        # real first admission is still running. Never settle it to pass.
        error = self.refused_revision(self.remap(proposal), "obligation_unaccounted")
        self.assertIn("outstanding_unaccounted", str(error))
        proposal["issue_revision_data"] = self.historical_data(ALPHA)
        self.revise(proposal)
        self.assertEqual(self.state()["admissions"], before["admissions"])
        self.assertEqual(self.row(), before["checkpoint"]["obligations"][0])
        self.assertEqual(self.port.state, "running")

    def test_unreadable_intake_metadata_carries_binding_without_late_beta_backfill(self):
        self.begin_unreadable()
        self.observe(BETA)
        rows = self.stored(); rows[0]["proof_scope"] = ["src"]
        self.write(rows)
        proposal = self.proposal_for(BETA)
        self.assert_unknown(proposal, "prior_source_association_unavailable")
        proposal["issue_revision_data"] = self.historical_data(ALPHA)
        error = self.refused_revision(proposal, "issue_revision_refused")
        self.assertEqual(error.detail["reason"], "changed_criterion_requires_replacement")

    def test_unreadable_owner_semantic_acceptance_resolves_only_its_beta_data(self):
        self.begin_alpha()
        with patch("pod.pod_ticks.refresh"):
            self.observe(BETA)
        rows = self.stored(); rows[0]["check"] = CHECK.replace("alpha", "beta")
        self.write(rows, revision_authority=OWNER)
        original = deepcopy(self.row())
        proposal = self.proposal_for(BETA)
        self.assert_unknown(proposal, "prior_source_association_unavailable")
        decoy = {**proposal, "issue_revision_data": self.historical_data(ALPHA)}
        error = self.refused_revision(decoy, "issue_revision_refused")
        self.assertEqual(error.detail["reason"], "body_data_unreferenced")
        proposal["issue_revision_data"] = self.historical_data(BETA)
        self.revise(proposal)
        self.assertEqual(self.row(), original)

    def test_returning_hash_cannot_use_original_intake_data_for_unknown_beta_epoch(self):
        self.begin_alpha(); self.accept(); self.tick()
        original = deepcopy(self.row())
        with patch("pod.pod_ticks.refresh"):
            self.observe(BETA)
        for check in (CHECK.replace("alpha", "beta"), CHECK):
            rows = self.stored(); rows[0].pop("evidence", None)
            rows[0].update(check=check, state="active", executor="coordinator")
            if check == CHECK:
                rows[0]["evidence"] = original["evidence"]
            self.write(rows, revision_authority=OWNER)
        # Even valid old proof DATA cannot establish the new requirement epoch.
        proposal = self.proposal_for(ALPHA)
        proposal["issue_revision_data"] = self.historical_data(ALPHA)
        self.assert_unknown(proposal, "prior_source_association_unavailable")
        before, receipts, writes = self.saved(), self.receipts(), deepcopy(self.issue.writes)
        rows = self.select_proof()
        error = self.failure("obligation_unaccounted", self.write, rows)
        self.assertEqual(error.detail["detail"], "evidence_invalidated")
        self.assertEqual((self.saved(), self.receipts(), self.issue.writes), (before, receipts, writes))
        self.assertEqual(self.row()["receipts"], original["receipts"])


class OrdinaryReselectionTests(ScopeRecoveryCase):
    def prepare(self, *, unknown=False, worker=False):
        if unknown:
            self.begin_unreadable(worker=worker)
            self.observe(ALPHA)
        else:
            self.issue.body = ALPHA
            rows = [self.criterion(check=CHECK)] + ([self.sub("S")] if worker else [])
            self.write(rows, objective_source=self.source(), governance={"base_ref": "target"})
            self.observe(BETA)
        self.write(self.select_proof())
        return self.drop_proof()

    def check_receipt_and_currency(self, original, *, unknown):
        self.assertEqual(self.row()["receipts"], original["receipts"])
        self.assertEqual(self.row()["evidence"], original["evidence"])
        self.assertTrue(self.line()["proven"])
        # Ordinary bookkeeping success must never turn into source authority.
        self.assert_unknown(self.proposal_for(ALPHA),
                            "prior_source_association_unavailable" if unknown else "prior_source_scope_ambiguous")

    def checkpoint_reselection(self, *, unknown):
        original = self.prepare(unknown=unknown)
        self.write(self.select_proof())
        self.check_receipt_and_currency(original, unknown=unknown)

    def admission_reselection(self, *, unknown):
        original = self.prepare(unknown=unknown, worker=True)
        frozen = self.packet(["S"], role="investigate", resolves="one question", stop_condition="one answer")
        self.start("ordinary-proof-task", frozen, accompanying={"obligations": self.select_proof()})
        self.check_receipt_and_currency(original, unknown=unknown)

    def report_reselection(self, *, unknown):
        original = self.prepare(unknown=unknown, worker=True)
        frozen = self.packet(["S"], role="investigate", resolves="one question", stop_condition="one answer")
        admission = self.start("ordinary-proof-task", frozen)["admission"]
        self.settle(admission)
        self.report(admission, frozen, map={"obligations": self.select_proof()})
        self.check_receipt_and_currency(original, unknown=unknown)

    def test_rs2_checkpoint_unknown_scope_reselects(self):
        self.checkpoint_reselection(unknown=True)

    def test_rs3_checkpoint_observed_beta_reselects(self):
        self.checkpoint_reselection(unknown=False)

    def test_rs2_admission_unknown_scope_reselects(self):
        self.admission_reselection(unknown=True)

    def test_rs3_admission_observed_beta_reselects(self):
        self.admission_reselection(unknown=False)

    def test_rs2_report_unknown_scope_reselects(self):
        self.report_reselection(unknown=True)

    def test_rs3_report_observed_beta_reselects(self):
        self.report_reselection(unknown=False)


class AcceptanceBoundaryTests(ScopeRecoveryCase):
    def returning_epoch(self):
        self.issue.body = ALPHA
        self.write([self.criterion(check=CHECK), self.sub("S")],
                   objective_source=self.source(), governance={"base_ref": "target"})
        self.write(self.select_proof())
        original = self.drop_proof()
        self.observe(BETA)
        for check in (CHECK.replace("alpha", "beta"), CHECK):
            rows = self.stored(); rows[0]["check"] = check
            self.write(rows, revision_authority=OWNER)
        return original

    def assert_refusal_unchanged(self, call, *args, **fields):
        before, receipts, writes = self.saved(), self.receipts(), deepcopy(self.issue.writes)
        workers = deepcopy(self.port.workers)
        error = self.failure("obligation_unaccounted", call, *args, **fields)
        self.assertEqual(error.detail["detail"], "evidence_invalidated")
        self.assertEqual((self.saved(), self.receipts(), self.issue.writes, self.port.workers),
                         (before, receipts, writes, workers))

    def test_admission_cannot_reselect_pre_epoch_alpha_proof_under_returned_beta_hash(self):
        original = self.returning_epoch()
        frozen = self.packet(["S"], role="investigate", resolves="one question", stop_condition="one answer")
        self.assert_refusal_unchanged(self.start, "invalid-proof-task", frozen,
                                      accompanying={"obligations": self.select_proof()})
        self.assertEqual(self.row()["receipts"], original["receipts"])

    def test_report_cannot_reselect_pre_epoch_alpha_proof_under_returned_beta_hash(self):
        original = self.returning_epoch()
        frozen = self.packet(["S"], role="investigate", resolves="one question", stop_condition="one answer")
        admission = self.start("invalid-proof-task", frozen)["admission"]
        self.settle(admission)
        self.assert_refusal_unchanged(self.report, admission, frozen, map={"obligations": self.select_proof()})
        self.assertEqual(self.row()["receipts"], original["receipts"])

    def test_owner_metadata_after_first_proof_keeps_ordinary_unknown_lineage(self):
        self.begin_unreadable(); self.accept()
        original = self.drop_proof()
        for add_scope in (True, False):
            rows = self.stored()
            if add_scope:
                rows[0]["proof_scope"] = ["src"]
            else:
                rows[0].pop("proof_scope")
            self.write(rows, revision_authority=OWNER)
        self.write(self.select_proof())
        self.assertEqual(self.row()["receipts"], original["receipts"])
        self.assert_unknown(self.proposal_for(ALPHA), "prior_source_association_unavailable")

    def test_malformed_acceptance_groups_refuse_without_rewriting(self):
        from pod.errors import PodError
        from pod.ledger import _validate_context
        self.begin_unreadable()
        state, before = self.state(), self.saved()
        for damage in ("partial", "boolean_seq", "zero_seq", "bad_hash", "claimed_authority"):
            broken = deepcopy(state)
            accepted = broken["pod_ticks"]["criterion_scopes"]["O1"]["acceptance"]
            if damage == "partial":
                accepted.pop("body_sha256")
            elif damage == "boolean_seq":
                accepted["seq"] = True
            elif damage == "zero_seq":
                accepted["seq"] = 0
            elif damage == "bad_hash":
                accepted["source_digest"] = "not-a-hash"
            else:
                accepted["verified"] = True
            with self.subTest(damage=damage), self.assertRaises(PodError):
                _validate_context(broken)
        self.assertEqual(self.saved(), before)


class PreviousMarkerTests(ScopeRecoveryCase):
    """Actual b54 writes, before acceptance metadata existed; no context surgery."""

    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="issue-scope-previous-", dir=os.environ.get("POD_CACHE_HOME"))
        cls.addClassCleanup(temporary.cleanup)
        cls.bundle = Path(temporary.name)
        archive = cls.bundle / "previous.tar"
        subprocess.run(["git", "archive", "-o", str(archive), "b54c26433ea24863f9154edd2c74549032758989",
                        "skills", "VERSION"], check=True)
        with tarfile.open(archive) as source:
            source.extractall(cls.bundle, filter="data")

    def previous_marker(self, mode):
        from pod.github import repository_context
        destination = self.root / "previous"; destination.mkdir()
        repository = Path(__file__).resolve().parents[1]
        script = """
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from unittest.mock import patch
from tests.test_issue_revision_scope_recovery import ALPHA, BETA, CHECK, OWNER, ScopeRecoveryCase
destination, mode = Path(sys.argv[1]), sys.argv[2]
@contextmanager
def persistent_fixture():
    yield destination
with patch('tests.issue41_support.fixture', persistent_fixture):
    case = ScopeRecoveryCase(); case.setUp()
try:
    case.begin_unreadable()
    if mode == 'running':
        case.port.state = 'running'
        case.start('previous-task', case.packet(['O1']))
    else:
        case.observe(ALPHA); case.accept(); case.tick()
        if mode == 'returning':
            with patch('pod.pod_ticks.refresh'):
                case.observe(BETA)
            for check in (CHECK.replace('alpha', 'beta'), CHECK):
                rows = case.stored(); rows[0].pop('evidence', None)
                rows[0].update(check=check, state='active', executor='coordinator')
                case.write(rows, revision_authority=OWNER)
    state = case.state()
    assert 'acceptance' not in state['pod_ticks']['criterion_scopes']['O1']
    (destination / 'writer.json').write_text(json.dumps(dict(state=state, body=case.issue.body,
        candidate=case.candidate, workers=case.port.workers)))
finally:
    case.doCleanups()
"""
        completed = subprocess.run([sys.executable, "-c", script, str(destination), mode], cwd=repository,
            env={**os.environ, "PYTHONPATH": os.pathsep.join((str(self.bundle / "skills"), str(repository)))},
            text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        produced = json.loads((destination / "writer.json").read_text())
        self.root, self.project, self.candidate = destination, destination / "project", produced["candidate"]
        os.environ.update(XDG_STATE_HOME=str(destination / "state"), XDG_CONFIG_HOME=str(destination / "config"))
        self.issue.body, self.port.workers = produced["body"], produced["workers"]
        context = repository_context(self.project)
        self.port.placement.update(repository=context["repository"], repo_key=context["repo_key"],
                                   path=context["worktree"], branch=context["branch"])
        self.configure(["/synthetic/verifier"])
        self.assertEqual(self.state(), produced["state"])

    def test_genuine_pre_extension_running_first_admission_recovers_with_intake_data(self):
        self.previous_marker("running")
        before = self.state()
        proposal = self.proposal_for(ALPHA)
        proposal["issue_revision_data"] = self.historical_data(ALPHA)
        self.revise(proposal)
        self.assertEqual(self.row(), before["checkpoint"]["obligations"][0])
        self.assertEqual(self.state()["admissions"], before["admissions"])

    def test_genuine_pre_extension_proof_reselects_but_missing_intake_stays_unknown(self):
        self.previous_marker("proof")
        original = self.drop_proof()
        self.write(self.select_proof())
        self.assertEqual(self.row()["receipts"], original["receipts"])
        self.assert_unknown(self.proposal_for(ALPHA), "prior_source_association_unavailable")

    def test_genuine_pre_extension_returning_hash_cannot_borrow_original_intake(self):
        self.previous_marker("returning")
        before = self.saved()
        error = self.failure("obligation_unaccounted", self.write, self.select_proof())
        self.assertEqual(error.detail["detail"], "evidence_invalidated")
        self.assertEqual(self.saved(), before)
        proposal = self.proposal_for(ALPHA)
        self.assert_unknown(proposal, "prior_source_association_unavailable")

    def test_pre_extension_reader_refuses_new_acceptance_metadata_without_rewriting(self):
        self.begin_unreadable()
        before = self.saved()
        script = """
from pathlib import Path
import sys
from pod.errors import PodError
from pod.ledger import read
try:
    read(Path(sys.argv[1]), 'objective')
except PodError as exc:
    assert exc.code == 'invalid_criterion source facts', (exc.code, str(exc))
    assert 'unsupported acceptance' in str(exc)
else:
    raise AssertionError('preceding reader accepted new metadata')
"""
        repository = Path(__file__).resolve().parents[1]
        completed = subprocess.run([sys.executable, "-c", script, str(self.project)], cwd=repository,
            env={**os.environ, "PYTHONPATH": str(self.bundle / "skills")}, text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.saved(), before)
