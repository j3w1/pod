"""Recovered facts survive transient DATA through real disposable entrypoints."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import patch

from pod.github import issue_intake, pod_blocks, repository_context
from pod.pod_ticks import proof_identity, requirement_scope
from pod.util import digest
from tests.test_issue_revision_currency import ALPHA, AUTHORITY, CHECK, OWNER
from tests.test_issue_revision_scope_recovery import ScopeRecoveryCase


class RetentionCase(ScopeRecoveryCase):
    def add_pod(self, number):
        self.issue.body += f"\n- [ ] **PoD#{number}** added requirement {number}"
        added = {**self.new_criterion(number), "check": pod_blocks(self.issue.body)[number][0]["text"]}
        return {"seq": self.state()["checkpoint"]["seq"] + 1, "obligations": self.stored() + [added],
                "objective_source": self.source(), "criteria": [f"PoD#{n}" for n in range(1, number + 1)],
                "revision_authority": deepcopy(AUTHORITY)}

    def recovered(self, *, running=False):
        self.begin_unreadable()
        if running:
            self.port.state = "running"
            self.start("retained-task", self.packet(["O1"]))
        else:
            self.observe(ALPHA); self.accept(); self.tick()
        original, acceptance = deepcopy(self.row()), deepcopy(self.scope()["acceptance"])
        proposal = self.add_pod(2)
        proposal["issue_revision_data"] = self.historical_data(ALPHA)
        self.revise(proposal)
        return original, acceptance

    def snapshot_effects(self):
        return (self.saved(), self.receipts(), deepcopy(self.issue.writes),
                deepcopy(self.port.workers), deepcopy(self.port.starts))

    def assert_refused_without_effects(self, proposal, reason):
        before = self.snapshot_effects()
        code = "relay_authorization_reused" if reason == "relay_authorization_reused" else "issue_revision_refused"
        error = self.refused_revision(proposal, code)
        if code == "issue_revision_refused":
            self.assertEqual(error.detail["reason"], reason)
        self.assertEqual(self.snapshot_effects(), before)
        return error

    def same_source_retirement(self):
        prior = self.state()["checkpoint"]
        proposal = {"seq": prior["seq"] + 1, "obligations": self.stored(),
                    "objective_source": prior["objective_source"], "criteria": prior["criteria"],
                    "revision_authority": deepcopy(AUTHORITY)}
        return self.remap(proposal)


class ScopeRetentionTests(RetentionCase):
    def retirement(self, *, data):
        original, acceptance = self.recovered()
        proposal = self.same_source_retirement()
        if data:
            proposal["issue_revision_data"] = self.historical_data(ALPHA)
        self.verdict_changes = {"single_use": "retirement-key"}
        self.assert_refused_without_effects(proposal, "unchanged_criterion_retirement")
        self.assertEqual(self.row(), original)
        self.assertEqual(self.scope()["acceptance"], acceptance)
        line = self.line()
        self.assertEqual((line["proven"], line["authorship"], line["due"]),
                         (True, "own", False))

    def test_identical_source_retirement_with_data_refuses_without_effects(self):
        self.retirement(data=True)

    def test_identical_source_retirement_without_data_refuses_without_effects(self):
        self.retirement(data=False)

    def test_later_material_keep_without_data_preserves_proof_acceptance_and_own_tick(self):
        original, acceptance = self.recovered()
        scope = deepcopy(self.scope())
        self.assertIsNotNone(requirement_scope(scope))
        self.assertEqual(set(scope["proofs"]), {proof_identity(original["receipts"][0])})
        self.verdict_changes = {"single_use": "later-keep"}
        self.revise(self.add_pod(3))
        self.assertEqual(self.row(), original)
        self.assertEqual(self.scope(), scope)
        self.assertEqual(self.scope()["acceptance"], acceptance)
        line = self.line()
        self.assertEqual((line["proven"], line["authorship"], line["due"]),
                         (True, "own", False))

    def test_later_keep_preserves_actual_running_first_admission(self):
        original, acceptance = self.recovered(running=True)
        before = self.state()
        workers, starts = deepcopy(self.port.workers), deepcopy(self.port.starts)
        self.verdict_changes = {"single_use": "running-later-keep"}
        self.revise(self.add_pod(3))
        self.assertEqual(self.row(), original)
        self.assertEqual(self.scope()["acceptance"], acceptance)
        self.assertEqual(self.state()["admissions"], before["admissions"])
        self.assertEqual((self.port.workers, self.port.starts, self.port.state), (workers, starts, "running"))

    def test_selected_proofs_keep_their_distinct_actual_source_digests(self):
        self.begin_unreadable()
        self.issue.unreadable = False
        first_body = ALPHA + "\n\nFirst unrelated note."
        self.observe(first_body); self.accept("first-proof"); self.tick()
        first = deepcopy(self.row()["receipts"][0])
        first_source = self.state()["pod_ticks"]["requests"]["1"]["source"]
        proposal = self.add_pod(2)
        proposal["issue_revision_data"] = self.historical_data(ALPHA, first_body)
        self.revise(proposal)
        first_scope = deepcopy(self.scope()["proofs"][proof_identity(first)])
        self.assertEqual(first_scope["source_digest"], digest(first_source))
        self.assertNotEqual(first_scope["source_digest"], self.scope()["source_digest"])
        self.issue.body += "\n\nSecond unrelated note."
        self.observe(self.issue.body)
        rows = self.stored()
        rows[0]["evidence"].append(self.proof("O1", reference="second-proof"))
        self.write(rows)
        original, proofs = deepcopy(self.row()), deepcopy(self.scope()["proofs"])
        self.assertEqual(len(proofs), 2)
        self.assertEqual(len({scope["source_digest"] for scope in proofs.values()}), 2)
        self.assertEqual(len({scope["block_sha256"] for scope in proofs.values()}), 1)
        self.verdict_changes = {"single_use": "two-proof-keep"}
        self.revise(self.add_pod(3))
        self.assertEqual(self.row(), original)
        self.assertEqual(self.scope()["proofs"], proofs)

    def test_reused_reference_does_not_move_recovered_proof_to_later_source(self):
        original, _ = self.recovered()
        cache = deepcopy(self.scope())
        body = self.issue.body
        self.observe(body.replace("alpha", "beta"))
        rows = self.stored(); rows[0]["evidence"] = original["evidence"]
        self.write(rows)
        self.assertEqual(self.row(), original)
        self.assertEqual(self.scope(), cache)
        self.issue.body = body
        self.verdict_changes = {"single_use": "reference-keep"}
        self.revise(self.add_pod(3))
        self.assertEqual(self.scope(), cache)

    def test_new_selected_beta_proof_is_not_certified_by_known_alpha_history(self):
        self.recovered()
        self.issue.unreadable = False
        alpha = self.issue.body
        self.observe(alpha.replace("alpha", "beta"))
        rows = self.stored()
        rows[0]["evidence"].append(self.proof("O1", reference="beta-proof"))
        self.write(rows)
        self.issue.body = alpha
        self.verdict_changes = {"single_use": "conflicting-selection"}
        error = self.assert_refused_without_effects(self.add_pod(3), "prior_criterion_source_unavailable")
        self.assertEqual(error.detail["fact_reason"], "prior_source_scope_ambiguous")

    def test_amendment_change_cannot_reuse_recovered_known_scope(self):
        self.recovered()
        proposal = self.add_pod(3)
        proposal["objective_source"] = issue_intake(self.project, self.locator, port=self.issue,
            amendments=[self.locator + "#issuecomment-19"])["source"]
        self.assert_refused_without_effects(proposal, "changed_criterion_requires_replacement")

    def test_metadata_change_carries_recovered_requirement_without_new_acceptance(self):
        _, acceptance = self.recovered()
        scope = deepcopy(self.scope())
        self.observe(self.issue.body.replace("alpha", "beta"))
        rows = self.stored(); rows[0].pop("evidence")
        rows[0].update(state="active", executor="coordinator", proof_scope=["src"])
        self.write(rows)
        after = self.scope()
        self.assertEqual(after["acceptance"], acceptance)
        self.assertEqual(after["proofs"], scope["proofs"])
        self.assertEqual({key: after[key] for key in ("source_digest", "amendments_digest", "block_sha256")},
                         {key: scope[key] for key in ("source_digest", "amendments_digest", "block_sha256")})
        self.verdict_changes = {"single_use": "metadata-hold"}
        self.assert_refused_without_effects(self.add_pod(3), "changed_criterion_requires_replacement")

    def test_interrupted_keep_recovers_identical_compact_facts_and_replay_refuses(self):
        self.recovered()
        scope, original = deepcopy(self.scope()), deepcopy(self.row())
        proposal = self.add_pod(3)
        self.verdict_changes = {"single_use": "interrupted-keep"}
        before = self.saved()
        with patch("pod.ledger.publish_context", side_effect=OSError("synthetic interruption")):
            with self.assertRaises(OSError):
                self.revise(proposal)
        self.assertEqual(self.saved(), before)
        consumed = self.receipts()
        self.assertEqual(len(consumed), 2)
        altered = deepcopy(proposal); altered["issue_revision_data"] = self.historical_data(ALPHA)
        self.assert_refused_without_effects(altered, "relay_authorization_reused")
        self.revise(proposal)
        self.assertEqual(self.receipts(), consumed)
        self.assertEqual((self.row(), self.scope()), (original, scope))
        self.assert_refused_without_effects(self.add_pod(4), "relay_authorization_reused")


class PreviousAcceptedRetentionTests(RetentionCase):
    """Real accepted E2b records with the defective writer's incomplete cache."""

    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="accepted-scope-previous-", dir=os.environ.get("POD_CACHE_HOME"))
        cls.addClassCleanup(temporary.cleanup)
        cls.bundle = Path(temporary.name)
        archive = cls.bundle / "previous.tar"
        subprocess.run(["git", "archive", "-o", str(archive), "e2b7597ef18dcaa01c9f24a1b5bd3dbfe52677dc",
                        "skills", "VERSION"], check=True)
        with tarfile.open(archive) as source:
            source.extractall(cls.bundle, filter="data")

    def previous_accepted(self, *, running=False):
        destination = self.root / "previous"; destination.mkdir()
        repository = Path(__file__).resolve().parents[1]
        script = """
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from unittest.mock import patch
from tests.test_issue_revision_scope_retention import RetentionCase
destination, running = Path(sys.argv[1]), sys.argv[2] == 'running'
@contextmanager
def persistent_fixture():
    yield destination
with patch('tests.issue41_support.fixture', persistent_fixture):
    case = RetentionCase(); case.setUp()
try:
    case.recovered(running=running)
    state = case.state()
    assert 'block_sha256' not in state['pod_ticks']['criterion_scopes']['O1']
    assert state['checkpoint']['revision_decisions'][-1]['scope_facts']['previous'][0]['status'] == 'known'
    (destination / 'writer.json').write_text(json.dumps(dict(state=state, body=case.issue.body,
        candidate=case.candidate, workers=case.port.workers)))
finally:
    case.doCleanups()
"""
        completed = subprocess.run([sys.executable, "-c", script, str(destination), "running" if running else "proof"],
            cwd=repository, env={**os.environ, "PYTHONPATH": os.pathsep.join((str(self.bundle / "skills"), str(repository)))},
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

    def test_genuine_accepted_history_recovers_proof_and_own_tick_without_data(self):
        self.previous_accepted()
        original, acceptance = deepcopy(self.row()), deepcopy(self.scope()["acceptance"])
        self.verdict_changes = {"single_use": "earlier-keep"}
        self.revise(self.add_pod(3))
        self.assertEqual(self.row(), original)
        self.assertEqual(self.scope()["acceptance"], acceptance)
        self.assertIsNotNone(requirement_scope(self.scope()))
        self.assertEqual(set(self.scope()["proofs"]), {proof_identity(original["receipts"][0])})
        line = self.line()
        self.assertEqual((line["proven"], line["authorship"], line["due"]),
                         (True, "own", False))

    def test_genuine_accepted_history_prevents_identical_source_retirement(self):
        self.previous_accepted()
        self.verdict_changes = {"single_use": "earlier-retire"}
        self.assert_refused_without_effects(self.same_source_retirement(), "unchanged_criterion_retirement")

    def test_genuine_accepted_history_keeps_running_first_admission(self):
        self.previous_accepted(running=True)
        before = self.state()
        self.verdict_changes = {"single_use": "earlier-running-keep"}
        self.revise(self.add_pod(3))
        self.assertEqual(self.row(), before["checkpoint"]["obligations"][0])
        self.assertEqual(self.state()["admissions"], before["admissions"])
        self.assertEqual(self.scope()["acceptance"], before["pod_ticks"]["criterion_scopes"]["O1"]["acceptance"])

    def test_genuine_known_history_cannot_certify_a_different_selected_receipt(self):
        self.previous_accepted()
        beta = self.issue.body.replace("alpha", "beta")
        with patch("pod.pod_ticks.refresh"):
            self.observe(beta)
        rows = self.stored()
        rows[0]["evidence"].append(self.proof("O1", reference="unobserved-beta-proof"))
        self.write(rows)
        self.verdict_changes = {"single_use": "earlier-different-proof"}
        proposal = self.add_pod(3)
        error = self.assert_refused_without_effects(proposal, "prior_criterion_source_unavailable")
        self.assertEqual(error.detail["fact_reason"], "historical_body_unavailable")
        proposal["issue_revision_data"] = self.historical_data(beta)
        error = self.assert_refused_without_effects(proposal, "prior_criterion_source_unavailable")
        self.assertEqual(error.detail["fact_reason"], "prior_source_scope_ambiguous")

    def test_genuine_accepted_requirement_survives_metadata_definition_change(self):
        self.previous_accepted()
        acceptance = deepcopy(self.scope()["acceptance"])
        rows = self.stored(); rows[0].pop("evidence")
        rows[0].update(state="active", executor="coordinator", proof_scope=["src"])
        self.write(rows)
        original = deepcopy(self.row())
        self.verdict_changes = {"single_use": "earlier-metadata-keep"}
        self.revise(self.add_pod(3))
        self.assertEqual(self.row(), original)
        self.assertEqual(self.scope()["acceptance"], acceptance)
        self.assertIsNotNone(requirement_scope(self.scope()))
        self.assertEqual(self.scope()["definition"], original["definition"])

    def test_returning_semantic_epoch_cannot_borrow_genuine_known_history(self):
        self.previous_accepted()
        original = deepcopy(self.row())
        beta = self.issue.body.replace("alpha", "beta")
        with patch("pod.pod_ticks.refresh"):
            self.observe(beta)
        for check in (CHECK.replace("alpha", "beta"), CHECK):
            rows = self.stored(); rows[0].pop("evidence", None)
            rows[0].update(check=check, state="active", executor="coordinator")
            self.write(rows, revision_authority=OWNER)
        before = self.snapshot_effects()
        rows = self.stored(); rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=original["evidence"])
        error = self.failure("obligation_unaccounted", self.write, rows)
        self.assertEqual(error.detail["detail"], "evidence_invalidated")
        self.assertEqual(self.snapshot_effects(), before)
        self.verdict_changes = {"single_use": "earlier-epoch-hold"}
        error = self.assert_refused_without_effects(self.add_pod(3), "prior_criterion_source_unavailable")
        self.assertEqual(error.detail["fact_reason"], "prior_source_association_unavailable")
