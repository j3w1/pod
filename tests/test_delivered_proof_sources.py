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
from pod.gitio import blob, is_ancestor, read as read_git, tree_entry
from pod.governance import delivered_source_reader, scope_resolver
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

    def deliver(self, *, move=True, dirty=None, later=True, untracked=None):
        """O1 satisfied with proof binding README.md (as the candidate changed it) and src/old.py, delivered; the
        target then moves with changed governance and the worktree takes later work that edits README.md.

        `dirty` is a tracked file whose uncommitted bytes the proof binds; `untracked` is a file only the worktree
        holds while the proof is accepted and the delivery recorded."""
        self.prepare_result()
        if dirty is not None:
            (self.project / dirty).write_text("uncommitted bytes the proof was bound to\n")
        bound = ["README.md", "src/old.py"]
        if untracked is not None:
            (self.project / untracked).write_text("proof bytes\n")
            bound.append(untracked)
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1", sources=[
            source_identity(self.project, path) for path in bound])])
        self.write(rows)
        self.write(self.stored(), delivery={"record": "merge-1"})
        if dirty is not None:
            git(self.project, "checkout", "-q", "--", dirty)
        if untracked is not None:
            (self.project / untracked).unlink()
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

    def test_replacement_refs_cannot_redirect_the_delivered_candidate(self):
        self.deliver(dirty="src/old.py")
        proof = hashlib.sha256(b"uncommitted bytes the proof was bound to\n").hexdigest()
        forged = subprocess.run(["git", "-C", str(self.project), "hash-object", "-w", "--stdin"], check=True,
                                capture_output=True, text=True,
                                input="uncommitted bytes the proof was bound to\n").stdout.strip()
        original = git(self.project, "rev-parse", self.candidate + ":src/old.py")
        index = self.root / "replacement-index"
        environment = {**os.environ, "GIT_INDEX_FILE": str(index)}
        run = lambda *argv: subprocess.run(["git", "-C", str(self.project), *argv], env=environment, check=True,
                                           capture_output=True, text=True).stdout.strip()
        run("read-tree", self.candidate)
        run("update-index", "--cacheinfo", "100644," + forged + ",src/old.py")
        parents = git(self.project, "rev-list", "--parents", "-n", "1", self.candidate).split()[1:]
        replacement = run("commit-tree", run("write-tree"), *sum((["-p", parent] for parent in parents), []),
                          "-m", "replacement")
        for name, (old, new) in (("commit", (self.candidate, replacement)), ("blob", (original, forged))):
            with self.subTest(name):
                git(self.project, "replace", old, new)
                try:
                    shown = subprocess.run(["git", "-C", str(self.project), "show", self.candidate + ":src/old.py"],
                                           check=True, capture_output=True).stdout
                    self.assertEqual(hashlib.sha256(shown).hexdigest(), proof)  # ordinary Git follows the replacement
                    error = self.refused_unchanged("obligation_unaccounted", "evidence_invalidated",
                                                   lambda: self.update(self.external_evidence()))
                    self.assertIn("src/old.py", str(error))
                finally:
                    git(self.project, "replace", "-d", old)

    def test_a_carriage_return_name_does_not_stand_in_for_a_newline_name(self):
        lf, cr = "line\nname.py", "line\rname.py"
        (self.project / cr).write_text("proof bytes\n")
        git(self.project, "add", cr)
        git(self.project, "commit", "-qm", "carriage-return name")
        self.deliver(untracked=lf)  # only the worktree held the newline name; the candidate holds the other
        error = self.refused_unchanged("obligation_unaccounted", "evidence_invalidated",
                                       lambda: self.update(self.external_evidence()))
        # Refusal text drops control characters, so the newline name reads as "linename.py".
        self.assertEqual(error.detail["referent"]["invalidations"][0]["paths"], ["linename.py"])

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


class GitObjectTrustTests(unittest.TestCase):
    """Pod's Git reads see the repository's own objects and history: no replacement refs, grafts or shallow
    boundaries, and distinct names stay distinct."""

    def setUp(self):
        self.temp = fixture()
        self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.project = self.root / "project"
        self.project.mkdir()
        self.run_git("init", "-q", "-b", "main")
        self.run_git("config", "user.name", "Fixture")
        self.run_git("config", "user.email", "fixture@example.invalid")
        self.run_git("config", "advice.graftFileDeprecated", "false")

    def run_git(self, *argv, check=True):
        return subprocess.run(["git", "-C", str(self.project), *argv], check=check, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, files, links=(), message="tree", parents=None):
        for name, text in files.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        for name, target in links:
            os.symlink(target, self.project / name)
        self.run_git("add", "-A")
        self.run_git("commit", "-qm", message)
        return self.run_git("rev-parse", "HEAD")

    def test_replacement_refs_redirect_neither_trees_nor_blobs(self):
        delivered = self.commit({"AGENTS.md": "rules\n", "src/file.py": "bound\n"}, [("rules-link", "AGENTS.md")])
        bound = hashlib.sha256(b"bound\n").hexdigest()
        os.unlink(self.project / "rules-link")
        other = self.commit({"src/file.py": "other\n", "rules-link": "plain file\n"}, message="other")
        self.run_git("replace", delivered, other)
        self.assertEqual(self.run_git("show", delivered + ":src/file.py"), "other")  # ordinary Git follows it
        self.assertEqual(delivered_source_reader(self.project, delivered)("src/file.py")["sha256"], bound)
        self.assertEqual(scope_resolver(self.project, [delivered])("rules-link"), ["AGENTS.md"])
        self.run_git("replace", "-d", delivered)
        self.run_git("replace", self.run_git("rev-parse", delivered + ":src/file.py"),
                     self.run_git("rev-parse", other + ":src/file.py"))
        self.assertEqual(self.run_git("show", delivered + ":src/file.py"), "other")
        self.assertEqual(delivered_source_reader(self.project, delivered)("src/file.py")["sha256"], bound)

    def test_grafts_and_shallow_boundaries_leave_recorded_parents(self):
        root = self.commit({"a.txt": "a\n"})
        self.run_git("checkout", "-q", "--orphan", "other")
        orphan = self.commit({"b.txt": "b\n"}, message="orphan")
        (self.project / ".git" / "info" / "grafts").write_text(f"{orphan} {root}\n")
        self.assertEqual(subprocess.run(["git", "-C", str(self.project), "merge-base", "--is-ancestor", root, orphan],
                                        capture_output=True).returncode, 0)  # ordinary Git follows the graft
        self.assertFalse(is_ancestor(self.project, root, orphan))
        self.assertEqual(delivered_source_reader(self.project, orphan)("b.txt")["state"], "present")
        (self.project / ".git" / "info" / "grafts").unlink()
        self.run_git("checkout", "-q", "main")
        middle = self.commit({"a.txt": "a2\n"})
        head = self.commit({"a.txt": "a3\n"})
        (self.project / ".git" / "shallow").write_text(middle + "\n")
        self.assertEqual(subprocess.run(["git", "-C", str(self.project), "merge-base", "--is-ancestor", root, head],
                                        capture_output=True).returncode, 1)  # ordinary Git stops at the boundary
        self.assertTrue(is_ancestor(self.project, root, head))

    def test_carriage_return_and_newline_names_stay_distinct(self):
        commit = self.commit({"AGENTS.md": "rules\n", "line\rname.py": "cr\n", "crlf\r\nname.py": "crlf\n"},
                             [("a\rb", "AGENTS.md")])
        read = delivered_source_reader(self.project, commit)
        self.assertEqual(read("line\rname.py")["sha256"], hashlib.sha256(b"cr\n").hexdigest())
        self.assertEqual(read("line\nname.py"), {"path": "line\nname.py", "state": "absent"})
        self.assertEqual(read("crlf\r\nname.py")["state"], "present")
        self.assertEqual(read("crlf\nname.py")["state"], "absent")
        resolve = scope_resolver(self.project, [commit])
        self.assertEqual(resolve("a\rb"), ["AGENTS.md"])
        self.assertEqual(resolve("a\nb"), ["a\nb"])
        self.assertEqual(tree_entry(self.project, commit, "line\nname.py"), (True, None))

    def test_blob_bytes_must_match_their_object_id(self):
        commit = self.commit({"file.txt": "real\n"})
        obj = self.run_git("rev-parse", commit + ":file.txt")
        self.assertEqual(blob(self.project, obj, limit=64), b"real\n")

        def corrupted(root, argv, binary=False):
            answer = read_git(root, argv, binary=binary)
            if argv[:2] == ["cat-file", "blob"]:
                answer.stdout = b"fake\n"
            return answer
        self.assertIsNone(blob(self.project, obj, limit=64, reader=corrupted))
        self.assertIsNone(blob(self.project, obj, limit=4))


if __name__ == "__main__":
    unittest.main()
