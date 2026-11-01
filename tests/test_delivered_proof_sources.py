"""After delivery, finished proof is judged against the delivered candidate's tree (R204).

The shape is synthetic: a delivered objective whose satisfied row binds a source file, a target that later moves
with changed governance, and the shared worktree reused for later, unrelated work that edits that file.
"""

import hashlib
import os
import subprocess
import unittest
from unittest.mock import patch

from pod import config, governance
from pod.governance import delivered_source_reader
from pod.ledger import checkpoint, closed_snapshot_context, objective_root, read
from pod.obligations import validate_closed_snapshot
from pod.records import source_identity
from pod.succession import _closure_recorded
from tests import test_delivery as delivery
from tests.decision_policy_support import PERSON
from tests.common import fixture
from tests.kernel_support import KernelCase, git

EXTERNAL = {"party": "third_party", "need": "acceptance", "unblocks_when": "accepted"}


class DeliveredProofSourceTests(KernelCase):
    prepare_result = delivery.VerifiedDeliveryTests.prepare_result
    move_target = delivery.VerifiedDeliveryTests.move_target

    def intake(self, *rows, **fields):
        return self.write([self.criterion(), self.sub("R", state="blocked_external", external=dict(EXTERNAL))],
                          governance={"base_ref": "target"})

    def setUp(self):
        super().setUp()
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)

    def deliver(self, *, move=True, dirty=None, later=True):
        """O1 satisfied with proof binding README.md (as the candidate changed it) and src/old.py, delivered; the
        target then moves with changed governance and the worktree takes later work that edits README.md."""
        self.prepare_result()
        if dirty is not None:
            (self.project / dirty).write_text("uncommitted bytes the proof was bound to\n")
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1", sources=[
            source_identity(self.project, "README.md"), source_identity(self.project, "src/old.py")])])
        self.write(rows)
        self.write(self.stored(), delivery={"record": "merge-1"})
        if dirty is not None:
            git(self.project, "checkout", "-q", "--", dirty)
        self.bound = read(self.project, "objective")["checkpoint"]["governance"]
        if move:
            self.target = self.move_target("AGENTS.md")
        if later:
            self.later_work()

    def later_work(self):
        git(self.project, "checkout", "-qb", "later-work")
        (self.project / "README.md").write_text("later, unrelated work\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "later work")
        self.later = git(self.project, "rev-parse", "HEAD")

    def record(self):
        return (objective_root(self.project, "objective") / "context.json").read_bytes()

    def update(self, changes, **fields):
        seq = read(self.project, "objective")["checkpoint"]["seq"] + 1
        return checkpoint(self.project, "objective", owner="owner",
                          value=self.core(update=changes, seq=seq, **fields),
                          native={"runtime": "runtime", "delegation": "available"})

    def external_evidence(self):
        return {"R": {"evidence": [self.proof("R", status="FAILED")]}}

    def refused_unchanged(self, code, detail, call):
        before = self.record()
        error = self.refused(code, detail, call)
        self.assertEqual(self.record(), before)
        return error

    def test_bookkeeping_keeps_proof_the_delivery_relied_on(self):
        self.deliver()
        kept = self.update(self.external_evidence())
        self.assertFalse(kept["governance_adoption"]["adopted"])
        stored = kept["checkpoint"]
        self.assertEqual(stored["governance"], self.bound)
        self.assertEqual(stored["obligations"][0], read(self.project, "objective")["checkpoint"]["obligations"][0])
        self.assertEqual(stored["obligations"][1]["state"], "blocked_external")
        # Full rows behave the same as an update.
        rows = self.stored()
        rows[1].update(evidence=[self.proof("R", status="FAILED", check="again")])
        self.assertFalse(self.write(rows)["governance_adoption"]["adopted"])

    def test_a_changed_candidate_or_new_satisfaction_keeps_todays_refusal(self):
        self.deliver()
        with self.subTest("changed candidate"):
            self.refused_unchanged("governance_changed", "governance_changed",
                                   lambda: self.update(self.external_evidence(), candidate=self.later))
        readme = {"path": "README.md", "state": "present",
                  "sha256": read(self.project, "objective")["checkpoint"]["obligations"][0]["evidence"][0]
                  ["sources"][0]["sha256"]}
        satisfied = {"state": "satisfied", "evidence": [self.proof("R", sources=[readme])]}
        with self.subTest("new satisfaction bound to the delivered bytes"):
            error = self.refused_unchanged("obligation_unaccounted", "evidence_invalidated",
                                           lambda: self.update({"R": satisfied}))
            self.assertIn("R: binding change", str(error))
            self.assertNotIn("O1", str(error))
        with self.subTest("new satisfaction bound to the worktree"):
            live = {"state": "satisfied",
                    "evidence": [self.proof("R", sources=[source_identity(self.project, "README.md")])]}
            self.assertEqual(self.update({"R": live})["checkpoint"]["obligations"][1]["state"], "satisfied")

    def test_a_bound_source_that_differs_from_the_delivered_tree_refuses(self):
        self.deliver(dirty="src/old.py")
        error = self.refused_unchanged("obligation_unaccounted", "evidence_invalidated",
                                       lambda: self.update(self.external_evidence()))
        self.assertIn("src/old.py", str(error))
        self.assertIn("delivered candidate " + self.candidate[:12], str(error))
        self.assertNotIn("README.md", str(error))
        self.assertEqual(error.detail["referent"]["invalidations"][0]["paths"], ["src/old.py"])

    def test_a_compared_row_the_kernel_changes_refuses_after_the_kernel(self):
        self.deliver()
        import pod.obligations as kernel
        original = kernel.accept_write

        def changing(prior, proposed, ctx):
            stored = original(prior, proposed, ctx)
            self.assertIn("O1", ctx["delivered_sources"]["rows"])
            stored["obligations"][0] = {**stored["obligations"][0], "check": "changed by the kernel"}
            return stored
        with patch.object(kernel, "accept_write", changing):
            # On the completion route; on R202's route its own post-kernel check refuses first.
            error = self.refused_unchanged("obligation_unaccounted", "evidence_invalidated",
                                           lambda: self.write(self.closing_rows(), close=True))
        self.assertIn("compared with the delivered candidate changed", str(error))

    def test_writes_outside_the_bookkeeping_route_judge_the_worktree(self):
        self.deliver(move=False)  # the target still names the delivery: currency holds, no route is taken
        error = self.refused_unchanged("obligation_unaccounted", "evidence_invalidated",
                                       lambda: self.update(self.external_evidence()))
        self.assertIn("O1: binding change", str(error))
        self.assertNotIn("delivered candidate", str(error))

    def closing_rows(self):
        rows = self.stored()
        rows[1].pop("external")
        rows[1].update(state="satisfied", evidence=[self.proof("R")])
        return rows

    def test_a_row_re_satisfied_in_the_closing_write_is_judged_against_the_worktree(self):
        self.deliver()
        rows = self.closing_rows()
        rows[0] = {**rows[0], "evidence": [self.proof("O1", check="again", sources=[
            read(self.project, "objective")["checkpoint"]["obligations"][0]["evidence"][0]["sources"][0]])]}
        error = self.refused_unchanged("obligation_unaccounted", "evidence_invalidated",
                                       lambda: self.write(rows, close=True))
        self.assertIn("O1: binding change", str(error))
        self.assertNotIn("delivered candidate", str(error))

    def test_the_closing_write_and_its_replay_use_the_delivered_tree(self):
        self.deliver()
        closed = self.write(self.closing_rows(), close=True)
        self.assertTrue(closed["checkpoint"]["closure"])
        self.assertFalse(closed["governance_adoption"]["adopted"])
        state = read(self.project, "objective")
        self.assertEqual(validate_closed_snapshot(state["checkpoint"],
                                                  closed_snapshot_context(self.project, "objective", state)), ())
        self.assertTrue(_closure_recorded(self.project, "objective", state))

    def test_a_recorded_closure_replays_against_the_delivered_tree_after_the_worktree_moves(self):
        self.deliver(later=False)
        self.write(self.closing_rows(), close=True)  # the worktree still holds the delivered bytes
        self.later_work()
        state = read(self.project, "objective")
        self.assertEqual(validate_closed_snapshot(state["checkpoint"],
                                                  closed_snapshot_context(self.project, "objective", state)), ())
        self.assertTrue(_closure_recorded(self.project, "objective", state))


class DeliveredSourceReaderTests(unittest.TestCase):
    """R204's read of the delivered tree: Git objects only, regular blobs only, nothing followed."""

    @classmethod
    def setUpClass(cls):
        cls.temp = fixture()
        cls.root = cls.temp.__enter__()
        cls.project = cls.root / "project"
        cls.project.mkdir()
        run = lambda *argv: subprocess.run(["git", "-C", str(cls.project), *argv], check=True,
                                           capture_output=True, text=True).stdout.strip()
        run("init", "-q", "-b", "main")
        run("config", "user.name", "Fixture"); run("config", "user.email", "fixture@example.invalid")
        (cls.project / "src").mkdir()
        (cls.project / "src" / "file.py").write_text("bound\n")
        (cls.project / "tool.sh").write_text("#!/bin/sh\n")
        os.chmod(cls.project / "tool.sh", 0o755)
        os.symlink("src/file.py", cls.project / "link.py")
        os.symlink("src", cls.project / "linkdir")
        (cls.project / "big.txt").write_text("x" * 64)
        run("add", ".")
        run("update-index", "--add", "--cacheinfo", "160000," + "1" * 40 + ",module")
        run("commit", "-qm", "tree")
        cls.commit = run("rev-parse", "HEAD")
        (cls.project / "src" / "file.py").write_text("worktree edit\n")  # the worktree is never read

    @classmethod
    def tearDownClass(cls):
        cls.temp.__exit__(None, None, None)

    def test_regular_blobs_are_read_from_the_commit_and_absence_is_absent(self):
        read = delivered_source_reader(self.project, self.commit)
        self.assertEqual(read("src/file.py"), {"path": "src/file.py", "state": "present",
                                               "sha256": hashlib.sha256(b"bound\n").hexdigest()})
        self.assertEqual(read("tool.sh")["state"], "present")
        self.assertEqual(read("missing.py"), {"path": "missing.py", "state": "absent"})
        self.assertEqual(read("nowhere/missing.py"), {"path": "nowhere/missing.py", "state": "absent"})

    def test_links_gitlinks_trees_unsafe_and_oversized_paths_are_unreadable(self):
        read = delivered_source_reader(self.project, self.commit)
        for path in ("link.py", "linkdir/file.py", "module", "module/inner.py", "src", "src/file.py/x",
                     "../src/file.py", "./src/file.py", "src//file.py", ".env", "/src/file.py"):
            with self.subTest(path=path):
                self.assertIsNone(read(path))
        with patch.object(governance, "MAX_SOURCE_BYTES", 63):
            self.assertIsNone(delivered_source_reader(self.project, self.commit)("big.txt"))
        self.assertIsNone(delivered_source_reader(self.project, "0" * 40)("src/file.py"))
        self.assertIsNone(delivered_source_reader(self.project, "not-a-commit")("src/file.py"))


if __name__ == "__main__":
    unittest.main()
