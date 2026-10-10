"""Document identities and scoped parent reconciliation at production boundaries."""

from copy import deepcopy
import hashlib
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.github import SEAL_FORMAT, issue_intake, issue_recheck
from pod.internal import run
from pod.ledger import objective_root, read
from tests.issue41_support import ProductionCase
from tests import test_eel_intake as eel_cases
from tests import test_operations as operation_cases
from tests.test_eel_intake import TitledPort, FORMAT, PES, LOCATOR


class SealMarkerTests(unittest.TestCase):
    setUp = eel_cases.EelMarkerTests.setUp

    def test_title_and_first_line_forms_refuse_before_binding(self):
        forms = [TitledPort(title="SEAL: export", body=PES)]
        for newline in ("\n", "\r\n"):
            for suffix in ("", "<br>"):
                forms.append(TitledPort(body=newline + "  " + newline + SEAL_FORMAT + suffix + newline))
        for port in forms:
            with self.subTest(title=port.title, body=port.body), self.assertRaises(PodError) as caught:
                issue_intake(self.worktree, LOCATOR, port=port)
            self.assertEqual(caught.exception.code, "issue_is_requirements_spec")
            self.assertIn("requirements input", str(caught.exception))
            self.assertIn("bounded Pod Execution Spec", caught.exception.detail["next_action"])
            self.assertIn("analysis", caught.exception.detail["next_action"])

    def test_restrictive_identity_wins_and_eel_diagnostic_is_preserved(self):
        cases = [("PES: export", SEAL_FORMAT, "issue_is_requirements_spec"),
                 ("SEAL: export", PES, "issue_is_requirements_spec"),
                 ("SEAL: export", FORMAT, "issue_is_evidence_ledger"),
                 ("EEL: export", SEAL_FORMAT, "issue_is_evidence_ledger")]
        for title, body, code in cases:
            with self.subTest(title=title), self.assertRaises(PodError) as caught:
                issue_intake(self.worktree, LOCATOR, port=TitledPort(title=title, body=body))
            self.assertEqual(caught.exception.code, code)

    def test_examples_quotes_and_amendments_are_not_document_identity(self):
        bodies = ["> " + SEAL_FORMAT, "```markdown\n" + SEAL_FORMAT + "\n```",
                  "Context\n" + SEAL_FORMAT, "  " + SEAL_FORMAT, SEAL_FORMAT + " ",
                  "**format:** SEAL v1", PES + "\n" + SEAL_FORMAT]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(issue_intake(self.worktree, LOCATOR, port=TitledPort(body=body))["status"], "ready")
        for title in ("seal: export", " SEAL: export", "Research SEAL: export"):
            self.assertEqual(issue_intake(self.worktree, LOCATOR, port=TitledPort(title=title))["status"], "ready")
        port = TitledPort(body=PES, amendment=SEAL_FORMAT)
        bound = issue_intake(self.worktree, LOCATOR, port=port,
                             amendments=[LOCATOR + "#issuecomment-99"])["source"]
        self.assertEqual(issue_recheck(self.worktree, bound, port=port)["status"], "current")
        port.amendment_body = "This scope is now requirements only."
        self.assertEqual(issue_recheck(self.worktree, bound, port=port)["reason"], "issue_amendment_changed")


class DocumentEffectTests(ProductionCase):
    locator = "https://github.com/acme/example/issues/7"

    def bind_issue(self):
        self.prepare()
        port = TitledPort(body=PES)
        bound = issue_intake(self.project, self.locator, port=port)["source"]
        self.write(objective_source=bound)
        return port, bound

    def stored_bytes(self):
        root = objective_root(self.project, "objective")
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
                if path.is_file() and path.name != ".lock"}

    def test_role_changes_stop_admission_acceptance_and_new_governed_effects(self):
        port, bound = self.bind_issue()
        for title, body, code in [("SEAL: export", PES, "issue_is_requirements_spec"),
                                  ("PES: export", SEAL_FORMAT, "issue_is_requirements_spec"),
                                  ("EEL: export", PES, "issue_is_evidence_ledger")]:
            port.title, port.body = title, body
            before = self.stored_bytes()
            calls = deepcopy(self.remote.calls)
            with patch("pod.github.GhPort", return_value=port):
                for operation in ("governor", "governor-execute"):
                    with self.subTest(operation=operation, title=title), self.assertRaises(PodError) as caught:
                        self.op(operation, action=self.action())
                    self.assertEqual(caught.exception.code, code)
                with self.assertRaises(PodError) as caught:
                    self.start("task", self.packet(["O1"], objective_source=bound))
                self.assertEqual(caught.exception.code, code)
                with self.assertRaises(PodError) as caught:
                    run("acceptance", {"project": str(self.project), "objective_source": bound,
                        "criteria": ["works"], "evidence_rows": [], "candidate": self.candidate,
                        "policy_revision": "p", "sources": [], "dependencies": [],
                        "environment": "fixture", "review_required": False, "hosted_required": False})
                self.assertEqual(caught.exception.code, code)
            self.assertEqual(self.remote.calls, calls)
            self.assertEqual(self.port.starts, [])
            self.assertEqual(self.stored_bytes(), before)

    def test_recording_and_readback_remain_available_after_role_change(self):
        port, _ = self.bind_issue()
        with patch("pod.github.GhPort", return_value=port):
            published = self.op("governor-execute", action=self.action())
        port.title = "SEAL: export"
        with patch("pod.github.GhPort", return_value=port):
            observed = self.op("governor-reconcile", record_id=published["record_id"])
            self.assertEqual(observed["status"], "PASS")
            self.op("governor-outcome", record_id=published["record_id"], outcome="PASS",
                    provider=published["receipt"]["provider"])

    def test_changed_amendment_blocks_publication_until_reconciled(self):
        self.prepare()
        port = TitledPort(body=PES, amendment="Executable scope")
        bound = issue_intake(self.project, self.locator, port=port,
                             amendments=[self.locator + "#issuecomment-99"])["source"]
        self.write(objective_source=bound)
        port.amendment_body = "Requirements only; use a separate PES."
        before = self.stored_bytes()
        with patch("pod.github.GhPort", return_value=port), self.assertRaises(PodError) as caught:
            self.op("governor-execute", action=self.action())
        self.assertEqual(caught.exception.code, "issue_reconciliation_required")
        self.assertEqual(self.stored_bytes(), before)
        self.assertEqual(self.remote.calls, [])


class ParentTraceabilityTests(ProductionCase):
    def test_local_and_cross_repository_parents_do_not_replace_child_target(self):
        self.prepare()
        child = issue_intake(self.project, "https://github.com/acme/example/issues/7",
                             port=TitledPort(body=PES))["source"]
        for repository in ("acme/example", "acme/requirements"):
            parent = TitledPort(title="SEAL: export", body=SEAL_FORMAT + "\nR1: export text")
            observation = parent.issue(repository=repository, number=11)
            dependency = observation["url"] + " R1/Limits sha256:" + hashlib.sha256(b"export text").hexdigest()
            self.write(objective_source=child,
                       verification={"dependencies": [dependency], "environment": "fixture"})
            checkpoint = read(self.project, "objective")["checkpoint"]
            self.assertEqual(checkpoint["objective_source"], child)
            self.assertEqual(checkpoint["verification"]["dependencies"], [dependency])
            # A requirements read is permitted; it is never executable intake.
            with self.assertRaises(PodError):
                issue_intake(self.project, observation["url"], port=parent)

    def test_relevant_dependency_change_invalidates_only_affected_proof(self):
        self.prepare()
        self.write([self.criterion(), self.sub("S")],
                   verification={"dependencies": ["parent:R1:old", "parent:R2:same"], "environment": "fixture"})
        rows = deepcopy(self.stored())
        for row in rows:
            row.pop("executor", None); row.pop("wait", None)
            row.update(state="satisfied", evidence=[self.proof(row["id"],
                dependencies=["parent:R1:old" if row["id"] == "O1" else "parent:R2:same"])])
        self.write(rows, verification={"dependencies": ["parent:R1:old", "parent:R2:same"], "environment": "fixture"})
        # A changed full parent observation need not change selected requirement versions.
        unchanged = deepcopy(self.stored())
        self.assertNotEqual(hashlib.sha256(b"R1 old; R2 same; coverage pending").digest(),
                            hashlib.sha256(b"R1 old; R2 same; coverage updated").digest())
        self.write(unchanged, verification={"dependencies": ["parent:R1:old", "parent:R2:same"], "environment": "fixture"})
        self.assertEqual(self.stored(), unchanged)
        rows = deepcopy(self.stored())
        before = read(self.project, "objective")
        with self.assertRaises(PodError) as caught:
            self.write(rows, verification={"dependencies": ["parent:R1:new", "parent:R2:same"], "environment": "fixture"})
        self.assertEqual(caught.exception.code, "obligation_unaccounted")
        self.assertEqual(read(self.project, "objective"), before)
        affected = next(row for row in rows if row["id"] == "O1")
        affected.update(state="active", executor="coordinator")
        affected.pop("evidence", None)
        self.write(rows, verification={"dependencies": ["parent:R1:new", "parent:R2:same"], "environment": "fixture"})
        self.assertEqual(next(row for row in self.stored() if row["id"] == "S")["state"], "satisfied")
        # An inaccessible required parent is an explicit scoped external wait.
        rows = deepcopy(self.stored())
        affected = next(row for row in rows if row["id"] == "O1")
        affected.pop("executor")
        affected.update(state="blocked_external", external={"party": "third_party",
            "need": "read required parent R1", "unblocks_when": "an accessible current R1 observation"})
        self.write(rows, verification={"dependencies": ["parent:R1:new", "parent:R2:same"], "environment": "fixture"})
        self.assertEqual(next(row for row in self.stored() if row["id"] == "S")["state"], "satisfied")


class SealRecoveryTests(unittest.TestCase):
    base = operation_cases.AdmissionTests
    setUp, checkpoint, frozen, restated = base.setUp, base.checkpoint, base.frozen, base.restated
    start, recover = base.start, base.recover

    def test_pending_replay_surfaces_seal_refusal_without_replacement_start(self):
        from pod.ledger import checkpoint, update_admission
        source = {"schema": "pod-issue-source/v1", "repository": "acme/widgets", "number": 7,
                  "locator": LOCATOR, "body_sha256": "a" * 64, "amendments": []}
        current = read(self.project, "objective")["checkpoint"]
        checkpoint(self.project, "objective", owner="owner",
            value={key: value for key, value in {**current, "objective_source": source}.items()
                   if key not in ("pod_version", "bundle_digest", "seq")}, native={"runtime": "runtime"})
        frozen = self.frozen()
        from pod.records import packet
        frozen = packet({**frozen["body"], "objective_source": source})
        with patch("pod.github.issue_recheck", return_value={"status": "current"}):
            first = self.start(frozen=frozen)["admission"]
        update_admission(self.project, "objective", owner="owner", admission_id=first["admission_id"],
                         update=lambda row: row.update(state="unresolved", native_binding=None))
        self.port.starts.clear(); self.port.state = "pending"
        with patch("pod.github.issue_recheck", side_effect=PodError("issue_is_requirements_spec", "SEAL requirements input")), \
                self.assertRaises(PodError) as caught:
            self.recover(first)
        self.assertEqual(caught.exception.code, "issue_is_requirements_spec")
        self.assertEqual(self.port.starts, [])


if __name__ == "__main__":
    unittest.main()
