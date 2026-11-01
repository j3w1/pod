"""After delivery, finished proof is judged against the delivered candidate's tree (R204).

The shape is synthetic: a delivered objective whose satisfied row binds a source file, a target that later moves
with changed governance, and the shared worktree reused for later, unrelated work that edits that file.
"""

import hashlib
import os
import shutil
import subprocess
import unittest
import zlib
from unittest.mock import patch

from pod import config, governance
from pod.errors import PodError
from pod.gitio import blob, git_bool, is_ancestor, partial_clone, read as read_git, resolve_commit, tree_entry
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


def forged_store(project, commit, directory, name, data, store):
    """A copy of the object store whose `directory` tree, under its genuine id, lists `name` as a correctly hashed
    blob of `data`: malformed storage, not a hash collision. Returns the genuine tree id."""
    run = lambda *argv, **kwargs: subprocess.run(["git", "-C", str(project), *argv], check=True, capture_output=True,
                                                 **kwargs).stdout
    original = run("rev-parse", f"{commit}:{directory}/{name}").decode().strip()
    tree = run("rev-parse", f"{commit}:{directory}").decode().strip()
    replacement = run("hash-object", "-w", "--stdin", input=data).decode().strip()
    raw = run("cat-file", "tree", tree)
    forged = raw.replace(bytes.fromhex(original), bytes.fromhex(replacement))
    assert forged != raw and hashlib.sha1(b"tree %d\0" % len(forged) + forged).hexdigest() != tree
    shutil.copytree(project / ".git" / "objects", store)
    target = store / tree[:2] / tree[2:]
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        target.chmod(0o600)
    target.write_bytes(zlib.compress(b"tree %d\0" % len(forged) + forged))
    return tree


def promisor(project, root):
    """Make `project` a partial clone of a local bare copy whose upload-pack leaves a marker when run."""
    remote, marker, helper = root / "remote.git", root / "upload-pack-called", root / "upload-pack-probe"
    subprocess.run(["git", "clone", "--bare", "-q", str(project), str(remote)], check=True, capture_output=True)
    helper.write_text(f'#!/bin/sh\n: > "{marker}"\nexec git-upload-pack "$@"\n')
    helper.chmod(0o700)
    for argv in (["remote", "add", "probe", str(remote)], ["config", "remote.probe.uploadpack", str(helper)],
                 ["config", "remote.probe.promisor", "true"], ["config", "core.repositoryformatversion", "1"],
                 ["config", "extensions.partialClone", "probe"]):
        subprocess.run(["git", "-C", str(project), *argv], check=True, capture_output=True)
    return marker


def packs(project):
    return sorted((project / ".git" / "objects" / "pack").glob("*.pack"))


class DeliveredProofSourceTests(KernelCase):
    prepare_result = delivery.VerifiedDeliveryTests.prepare_result
    move_target = delivery.VerifiedDeliveryTests.move_target

    assurance = False  # add an unsatisfied assurance with a non-empty editing boundary

    def intake(self, *rows, **fields):
        extra = [{"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1", "check": "review src",
                  "scope": {"paths": ["src"], "surfaces": []},
                  "boundary": {"paths": ["src"], "surfaces": ["ownership"]},
                  "question": "is src correct?", "candidate": self.candidate, "existing_evidence": "unit",
                  "insufficiency": "review needed", "state": "blocked_external",
                  "external": dict(EXTERNAL)}] if self.assurance else []
        return self.write([self.criterion(), self.sub("R", state="blocked_external", external=dict(EXTERNAL)),
                           *extra], governance={"base_ref": "target"})

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

    def later_work(self, path="README.md"):
        git(self.project, "checkout", "-qb", "later-work")
        (self.project / path).write_text("later, unrelated work\n")
        git(self.project, "add", path)
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

    def forged(self):
        self.deliver(dirty="src/old.py")
        self.forged_bytes = b"uncommitted bytes the proof was bound to\n"
        return forged_store(self.project, self.candidate, "src", "old.py", self.forged_bytes, self.root / "forged")

    def refused_while_git_reads_forged(self, environment, *, ordinary=True):
        with patch.dict(os.environ, environment):
            if ordinary:  # ordinary Git reads the forged membership
                shown = subprocess.run(["git", "-C", str(self.project), "show", self.candidate + ":src/old.py"],
                                       check=True, capture_output=True).stdout
                self.assertEqual(shown, self.forged_bytes)
            error = self.refused_unchanged("obligation_unaccounted", "evidence_invalidated",
                                           lambda: self.update(self.external_evidence()))
        self.assertIn("src/old.py", str(error))

    def test_ambient_object_or_repository_redirection_cannot_substitute_the_delivered_tree(self):
        tree = self.forged()
        copy = self.root / "copied-repository"
        shutil.copytree(self.project / ".git", copy)
        (copy / "objects" / tree[:2] / tree[2:]).chmod(0o600)
        shutil.copyfile(self.root / "forged" / tree[:2] / tree[2:], copy / "objects" / tree[:2] / tree[2:])
        for name, environment, ordinary in (
                ("GIT_OBJECT_DIRECTORY", {"GIT_OBJECT_DIRECTORY": str(self.root / "forged")}, True),
                ("GIT_DIR", {"GIT_DIR": str(copy), "GIT_WORK_TREE": str(self.project)}, True),
                ("GIT_ALTERNATE_OBJECT_DIRECTORIES", {"GIT_ALTERNATE_OBJECT_DIRECTORIES": str(self.root / "forged")},
                 False)):
            with self.subTest(name):
                self.refused_while_git_reads_forged(environment, ordinary=ordinary)

    def test_a_forged_tree_in_an_alternate_object_store_cannot_substitute_the_delivered_tree(self):
        tree = self.forged()
        (self.project / ".git" / "objects" / tree[:2] / tree[2:]).rename(self.root / "genuine-tree")
        (self.project / ".git" / "objects" / "info" / "alternates").write_text(str(self.root / "forged") + "\n")
        self.refused_while_git_reads_forged({})

    def test_a_partial_clone_is_refused_and_nothing_is_fetched(self):
        self.deliver()
        marker = promisor(self.project, self.root)
        before = packs(self.project)
        record = self.record()
        with self.assertRaises(PodError):
            self.update(self.external_evidence())
        self.assertEqual(self.record(), record)
        self.assertFalse(marker.exists())
        self.assertEqual(packs(self.project), before)

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
        self.deliver(move=False)  # currency holds; adding a row is not post-delivery bookkeeping
        rows = self.stored() + [self.sub("S", state="blocked_external", external=dict(EXTERNAL))]
        error = self.refused_unchanged("obligation_unaccounted", "evidence_invalidated", lambda: self.write(rows))
        self.assertIn("O1: binding change", str(error))
        self.assertNotIn("delivered candidate", str(error))

    def deliver_reused(self, *, dirty=False):
        """With governance current: O1 is proved at the base, carried by REUSE to the candidate and delivered;
        the worktree then takes later work that edits the bound source, src/old.py."""
        self.prepare_result()
        delivered, self.candidate = self.candidate, self.base
        if dirty:
            (self.project / "src" / "old.py").write_text("uncommitted bytes the proof was bound to\n")
        rows = self.stored()
        rows[0]["proof_scope"] = ["src"]  # REUSE carries proof only for a declared scope
        self.write(rows)
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1", sources=[
            source_identity(self.project, "src/old.py")])])
        self.write(rows)
        self.candidate = delivered
        self.update({"O1": {"reuse": {"from": self.base}}})
        self.write(self.stored(), delivery={"record": "merge-1"})
        if dirty:
            git(self.project, "checkout", "-q", "--", "src/old.py")
        self.assertEqual(self.stored()[0]["reuse"]["from"], self.base)
        self.later_work("src/old.py")

    def test_bookkeeping_with_current_governance_keeps_reused_proof_the_delivery_relied_on(self):
        self.deliver_reused()
        recorded = self.stored()[0]
        stored = self.update(self.external_evidence())["checkpoint"]
        self.assertEqual(stored["obligations"][0], recorded)
        self.assertEqual(stored["obligations"][1]["state"], "blocked_external")
        # Reads judge the same row the same way: at the delivered tree, not the worktree.
        from pod.assurance import evidence_valid
        from pod.ledger import kernel_view
        from pod.obligations import bound_governance_digest
        view = kernel_view(self.project, "objective", delivered=True)
        gov = bound_governance_digest(view["map"])
        self.assertTrue(evidence_valid(recorded, view["ctx"], gov))
        self.assertFalse(evidence_valid(recorded, kernel_view(self.project, "objective")["ctx"], gov))

    def test_bookkeeping_with_current_governance_refuses_proof_the_delivered_tree_lacks(self):
        self.deliver_reused(dirty=True)
        error = self.refused_unchanged("obligation_unaccounted", "evidence_invalidated",
                                       lambda: self.update(self.external_evidence()))
        self.assertIn("delivered candidate " + self.candidate[:12], str(error))
        self.assertEqual(error.detail["referent"]["invalidations"][0]["paths"], ["src/old.py"])

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

    def without_boundary(self):
        rows = self.stored()
        rows[2].pop("boundary")  # full rows that omit the assurance's editing boundary
        return rows

    def test_an_omitted_boundary_is_not_bookkeeping_so_the_worktree_judges_delivered_proof(self):
        self.assurance = True
        self.deliver(move=False)  # currency holds; the worktree's README.md differs from the delivered tree
        error = self.refused_unchanged("obligation_unaccounted", "evidence_invalidated",
                                       lambda: self.write(self.without_boundary()))
        self.assertIn("O1: binding change", str(error))
        self.assertNotIn("delivered candidate", str(error))

    def test_an_omitted_boundary_with_nothing_stale_is_an_ordinary_write(self):
        self.assurance = True
        self.deliver(move=False, later=False)  # the worktree still holds the delivered bytes
        import pod.obligations as kernel
        original, seen = kernel.accept_write, []

        def traced(prior, proposed, ctx):
            seen.append("delivered_sources" in ctx)
            return original(prior, proposed, ctx)
        with patch.object(kernel, "accept_write", traced):
            stored = self.write(self.without_boundary())["checkpoint"]
        # As on main: no R204 route is taken, the worktree check passes and the kernel records the empty boundary.
        self.assertEqual(seen, [False])
        self.assertEqual(stored["obligations"][2]["boundary"], {"paths": [], "surfaces": []})

    def test_a_bookkeeping_proposal_whose_stored_result_is_not_bookkeeping_refuses(self):
        self.deliver(move=False)
        import pod.obligations as kernel
        original = kernel.accept_write

        def changing(prior, proposed, ctx):
            stored = original(prior, proposed, ctx)
            self.assertIn("O1", ctx["delivered_sources"]["rows"])
            stored["obligations"][1] = {**stored["obligations"][1], "check": "changed by the kernel"}
            return stored
        with patch.object(kernel, "accept_write", changing):
            error = self.refused_unchanged("obligation_unaccounted", "evidence_invalidated",
                                           lambda: self.update(self.external_evidence()))
        self.assertIn("changes more than post-delivery bookkeeping", str(error))

    def test_a_closing_write_with_current_governance_uses_the_delivered_tree(self):
        self.deliver(move=False)  # currency holds; the worktree has moved on and edited README.md
        closed = self.write(self.closing_rows(), close=True)
        self.assertTrue(closed["checkpoint"]["closure"])
        state = read(self.project, "objective")
        self.assertEqual(validate_closed_snapshot(state["checkpoint"],
                                                  closed_snapshot_context(self.project, "objective", state)), ())

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

    def test_ambient_repository_and_object_redirection_is_ignored(self):
        head = self.commit({"AGENTS.md": "rules\n", "src/file.py": "bound\n"})
        other = self.root / "other"
        subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
        (other / "x.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(other), "add", "."], check=True)
        subprocess.run(["git", "-C", str(other), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm",
                        "other"], check=True)
        with patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}):
            self.assertEqual(resolve_commit(self.project, "HEAD"), head)
        tree = forged_store(self.project, head, "src", "file.py", b"forged\n", self.root / "forged")
        with patch.dict(os.environ, {"GIT_OBJECT_DIRECTORY": str(self.root / "forged")}):
            self.assertEqual(delivered_source_reader(self.project, head)("src/file.py")["sha256"],
                             hashlib.sha256(b"bound\n").hexdigest())
        (self.project / ".git" / "objects" / tree[:2] / tree[2:]).unlink()
        (self.project / ".git" / "objects" / "info" / "alternates").write_text(str(self.root / "forged") + "\n")
        self.assertIsNone(delivered_source_reader(self.project, head)("src/file.py"))  # forged storage: unreadable
        self.assertEqual(tree_entry(self.project, head, "src/file.py"), (False, None))
        self.assertIsNone(scope_resolver(self.project, [head])("src/file.py"))
        self.assertEqual(delivered_source_reader(self.project, head)("AGENTS.md")["state"], "present")

    def test_a_partial_clone_is_refused_and_a_missing_object_is_never_fetched(self):
        head = self.commit({"source.txt": "delivered bytes\n"})
        obj = self.run_git("rev-parse", head + ":source.txt")
        marker = promisor(self.project, self.root)
        self.assertIsNone(delivered_source_reader(self.project, head)("source.txt"))  # refused while present
        (self.project / ".git" / "objects" / obj[:2] / obj[2:]).unlink()
        self.assertIsNone(delivered_source_reader(self.project, head)("source.txt"))
        self.assertIsNone(blob(self.project, obj, limit=64))  # a direct read does not fetch either
        self.assertFalse(marker.exists())
        self.assertEqual(packs(self.project), [])

    def promisor_value(self, line):
        """Give remote `p` the raw config line `line` (pod#167); returns the delivered reader's view of a file."""
        self.run_git("config", "--remove-section", "remote.p", check=False)
        with open(self.project / ".git" / "config", "a") as config:
            config.write('[remote "p"]\n\t' + line + "\n")
        return delivered_source_reader(self.project, self.run_git("rev-parse", "HEAD"))("source.txt")

    def test_a_valueless_promisor_key_refuses_as_a_promisor_repository(self):
        self.commit({"source.txt": "delivered bytes\n"})
        self.assertEqual(self.promisor_value("url = /nowhere")["state"], "present")  # control: no promisor key
        self.assertIsNone(self.promisor_value("promisor"))
        self.assertIs(partial_clone(self.project), True)
        self.assertEqual(self.run_git("config", "--type=bool", "--get", "remote.p.promisor"), "true")

    def test_promisor_spellings_follow_gits_boolean_rules(self):
        self.commit({"source.txt": "delivered bytes\n"})
        for value, expected in (("TrUe", True), ("YES", True), ("On", True), ("1", True),
                                ("FaLsE", False), ("No", False), ("OFF", False), ("0", False), ("", False)):
            with self.subTest(value=value):
                seen = self.promisor_value("promisor = " + value if value else "promisor =")
                self.assertIs(partial_clone(self.project), expected)
                self.assertIs(git_bool(value), expected)
                self.assertEqual(self.run_git("config", "--type=bool", "--get", "remote.p.promisor"),
                                 "true" if expected else "false")
                self.assertEqual(seen is None, expected)

    def test_an_unrecognized_promisor_value_refuses(self):
        self.commit({"source.txt": "delivered bytes\n"})
        for value in ("maybe", "2", '" true"'):
            with self.subTest(value=value):
                self.assertIsNone(self.promisor_value("promisor = " + value))
                self.assertIsNone(partial_clone(self.project))
        self.assertIsNone(git_bool("maybe"))

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
