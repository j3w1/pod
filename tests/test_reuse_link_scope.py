"""R173: REUSE resolves its scope through committed links in both candidates' trees (pod#161).

One synthetic repository per class: a base commit holding every link, and one child commit per delta.
"""

from pathlib import Path
import subprocess
import tempfile
import unittest

from pod.assurance import _bind_reuse, _reuse_valid, evidence_valid, reuse_eligibility, validate_receipts
from pod.errors import PodError
from pod.governance import scope_resolver
from pod.ledger import git_delta, kernel_context
from pod.obligations import bound_governance_digest
from tests import test_obligations as base
from tests.common import proof
from tests.kernel_support import git

ob, write, rows_of, by_id, refused = base.ob, base.write, base.rows_of, base.by_id, base.refused

LINKS = {
    "rules-link": "docs/rules.md",       # a file link
    "cfg": "docs",                       # a parent-directory link
    "loop-a": "loop-b", "loop-b": "loop-a",
    **{f"hop{index}": f"hop{index + 1}" for index in range(9)}, "hop9": "docs/rules.md",  # ten hops
    "code-link": "src/old.py",           # an unrelated link
    "ver-link": "VERSION",
    "out": "../outside",
    "chain": "middle", "middle": "docs/rules.md",   # a chain through an intermediate link
    "outer": "inner", "inner": "docs",              # a parent-link prefix through an intermediate link
}
# Child commits that re-point one committed link of c1; the files they name are unchanged.
RELINKS = {"middle -> ./docs/rules.md": ("middle", "./docs/rules.md"),   # an equivalent spelling
           "middle -> other.txt": ("middle", "other.txt"),               # another unchanged file
           "inner -> ./docs": ("inner", "./docs")}


class Repository:
    """c1 carries every link and a gitlink; each delta is one child commit of c1."""

    def __init__(self, root: Path):
        self.root = root
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
        git(root, "config", "user.name", "Fixture")
        git(root, "config", "user.email", "fixture@example.invalid")
        for name, text in {"docs/rules.md": "rules\n", "src/old.py": "x = 1\n", "VERSION": "1.0.0\n",
                           "other.txt": "other\n"}.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        for name, target in LINKS.items():
            (root / name).symlink_to(target)
        git(root, "add", "-A")
        git(root, "commit", "-qm", "base")
        seed = git(root, "rev-parse", "HEAD")
        # A committed submodule entry (mode 160000); its subtree is another repository's and is never read.
        git(root, "update-index", "--add", "--cacheinfo", f"160000,{seed},module")
        git(root, "commit", "-qm", "gitlink")
        self.c1 = git(root, "rev-parse", "HEAD")
        self.after = {name: self.child(name, text) for name, text in
                      {"docs/rules.md": "changed\n", "VERSION": "1.0.1\n", "other.txt": "changed\n"}.items()}
        self.after.update({key: self.relink(*pair) for key, pair in RELINKS.items()})

    def child(self, name: str, text: str) -> str:
        git(self.root, "checkout", "-q", "--detach", self.c1)
        (self.root / name).write_text(text)
        git(self.root, "add", "--", name)
        git(self.root, "commit", "-qm", "edit " + name)
        return git(self.root, "rev-parse", "HEAD")

    def relink(self, name: str, target: str) -> str:
        git(self.root, "checkout", "-q", "--detach", self.c1)
        (self.root / name).unlink()
        (self.root / name).symlink_to(target)
        git(self.root, "add", "--", name)
        git(self.root, "commit", "-qm", "relink " + name)
        return git(self.root, "rev-parse", "HEAD")

    def ctx(self, candidate: str, *, resolve: bool = True, **extra) -> dict:
        found = {"candidate": candidate, "git_delta": lambda source, target: git_delta(self.root, source, target), **extra}
        if resolve:
            # As ledger.kernel_context supplies it: traversed links join the compared names.
            found["reuse_resolver"] = lambda source, target: scope_resolver(self.root, [source, target], links=True)
        return base.ctx(**found)


class LinkFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = Repository(Path(cls._tmp.name) / "repo")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def proved(self, scope):
        """S satisfied at c1 for one declared scope path, or a list of them."""
        paths = [scope] if isinstance(scope, str) else scope
        definition = ob("S", boundary={"paths": paths})
        row = ob("S", "satisfied", boundary={"paths": paths},
                 evidence=[proof("S", self.repo.c1, definition=definition)])
        return base.accept_write(None, base.intake(row), self.repo.ctx(self.repo.c1))

    def reuse(self, scope, changed, **extra):
        """Carry S's proof to the child commit that changes `changed`; the state, or the refusal."""
        state = self.proved(scope)
        rows = rows_of(state)
        by = next(row for row in rows if row["id"] == "S")
        by["reuse"] = {"from": self.repo.c1}
        moved = self.repo.ctx(self.repo.after[changed], **extra)
        try:
            return base.accept_write(state, write(state, rows), moved), None
        except PodError as exc:
            return None, exc

    def touched(self, scope, changed):
        _, error = self.reuse(scope, changed)
        self.assertIsNotNone(error, scope)
        self.assertEqual((error.code, error.detail["detail"]), ("obligation_unaccounted", "evidence_invalidated"))
        entry = error.detail["referent"]["invalidations"][0]
        self.assertEqual(entry["eligibility"], "touched")
        self.assertEqual(entry["reuse_failure"], {"predicate": "reuse_scope_untouched"})
        return entry

    def unresolved(self, scope, changed="other.txt"):
        _, error = self.reuse(scope, changed)
        self.assertIsNotNone(error, scope)
        entry = error.detail["referent"]["invalidations"][0]
        self.assertEqual(entry["eligibility"], "unresolved scope path " + scope)
        self.assertEqual(entry["paths"], [scope])
        self.assertEqual(entry["reuse_failure"], {"predicate": "reuse_scope_untouched"})
        self.assertTrue(entry["correction"].startswith("record fresh evidence for S"))
        self.assertNotIn("add reuse", entry["correction"])
        return entry


class ResolvedScopeTests(LinkFixture):
    def test_a_file_link_whose_target_the_delta_edits_is_touched(self):
        entry = self.touched("rules-link", "docs/rules.md")
        self.assertIn("docs/rules.md", entry["paths"])

    def test_a_parent_directory_link_is_touched(self):
        entry = self.touched("cfg/rules.md", "docs/rules.md")
        self.assertIn("docs/rules.md", entry["paths"])

    def test_the_direct_path_control_is_touched(self):
        self.touched("docs/rules.md", "docs/rules.md")

    def test_an_unrelated_link_whose_target_is_untouched_still_reuses(self):
        state, error = self.reuse("code-link", "docs/rules.md")
        self.assertIsNone(error)
        row = by_id(state, "S")
        self.assertEqual((row["state"], row["reuse"]["from"], row["reuse"]["delta"]),
                         ("satisfied", self.repo.c1, ["docs/rules.md"]))

    def test_a_link_to_version_under_a_version_only_delta_is_touched(self):
        self.touched("ver-link", "VERSION")

    def test_a_scope_that_merely_contains_the_root_keeps_a_version_only_delta(self):
        state, error = self.reuse(".", "VERSION")
        self.assertIsNone(error)
        self.assertEqual(by_id(state, "S")["reuse"]["delta"], ["VERSION"])


class LinkChainTests(LinkFixture):
    """Every committed link traversed while resolving a scope path joins the compared names (F1)."""

    def bind(self, scope, changed):
        row = by_id(self.proved(scope), "S")
        row["reuse"] = {"from": self.repo.c1}
        with self.assertRaises(PodError) as caught:
            _bind_reuse(row, None, self.repo.ctx(self.repo.after[changed]))
        error = caught.exception
        self.assertIn("REUSE is unproven: touched", str(error))
        return error.detail["referent"]

    def test_re_pointing_an_intermediate_link_to_another_unchanged_file_touches_the_chain(self):
        referent = self.bind("chain", "middle -> other.txt")
        self.assertEqual((referent["predicate"], referent["paths"]), ("reuse_scope_untouched", "middle"))
        self.assertEqual(self.touched("chain", "middle -> other.txt")["paths"], ["middle"])

    def test_an_equivalent_spelling_of_an_intermediate_link_touches_the_chain(self):
        self.assertEqual(self.bind("chain", "middle -> ./docs/rules.md")["paths"], "middle")
        self.assertEqual(self.touched("chain", "middle -> ./docs/rules.md")["paths"], ["middle"])

    def test_an_intermediate_link_under_a_parent_link_prefix_touches_the_scope(self):
        self.assertEqual(self.bind("outer/rules.md", "inner -> ./docs")["paths"], "inner")

    def test_an_unchanged_chain_still_reuses(self):
        state, error = self.reuse("chain", "other.txt")
        self.assertIsNone(error)
        self.assertEqual((by_id(state, "S")["state"], by_id(state, "S")["reuse"]["delta"]), ("satisfied", ["other.txt"]))

    def test_a_stored_reuse_across_a_re_pointed_intermediate_link_is_not_current_proof(self):
        for changed in ("middle -> ./docs/rules.md", "middle -> other.txt"):
            with self.subTest(changed=changed):
                target = self.repo.after[changed]
                state = self.proved("chain")
                rows = rows_of(state)
                next(row for row in rows if row["id"] == "S")["reuse"] = {"from": self.repo.c1}
                # As the lexical rule bound it: the delta names only middle, not chain.
                stored = base.accept_write(state, write(state, rows), self.repo.ctx(target, resolve=False))
                row, gov = by_id(stored, "S"), bound_governance_digest(stored)
                self.assertTrue(evidence_valid(row, self.repo.ctx(target, resolve=False), gov))
                self.assertFalse(_reuse_valid(row, self.repo.ctx(target), target))
                self.assertFalse(evidence_valid(row, self.repo.ctx(target), gov))

    def test_r203_resolution_without_links_is_unchanged(self):
        before, after = self.repo.c1, self.repo.after["middle -> other.txt"]
        self.assertEqual(scope_resolver(self.repo.root, [before, after])("chain"), ["docs/rules.md", "other.txt"])
        self.assertEqual(scope_resolver(self.repo.root, [before])("outer/rules.md"), ["docs/rules.md"])
        self.assertEqual(scope_resolver(self.repo.root, [before, after], links=True)("chain"),
                         ["docs/rules.md", "chain", "middle", "other.txt"])
        self.assertIsNone(scope_resolver(self.repo.root, [before], links=True)("loop-a"))


class UnresolvedScopeTests(LinkFixture):
    def test_a_link_loop_refuses_naming_the_path(self):
        self.unresolved("loop-a")

    def test_an_over_long_chain_refuses_naming_the_path(self):
        self.unresolved("hop0")
        # Within the bound the same chain resolves: hop2 reaches docs/rules.md in eight hops.
        self.assertEqual(scope_resolver(self.repo.root, [self.repo.c1])("hop2"), ["docs/rules.md"])

    def test_a_gitlink_scope_refuses_naming_the_path(self):
        self.unresolved("module")

    def test_an_escaping_link_refuses_naming_the_path(self):
        self.unresolved("out")

    def test_bind_reuse_words_the_refusal_as_unresolved_and_directs_fresh_evidence(self):
        state = self.proved("loop-a")
        row = by_id(state, "S")
        row["reuse"] = {"from": self.repo.c1}
        with self.assertRaises(PodError) as caught:
            _bind_reuse(row, None, self.repo.ctx(self.repo.after["other.txt"]))
        error = caught.exception
        self.assertIn("REUSE is unproven: unresolved scope path loop-a", str(error))
        self.assertEqual((error.detail["referent"]["predicate"], error.detail["referent"]["paths"]),
                         ("reuse_scope_untouched", "loop-a"))
        action = error.detail["next_action"]
        self.assertTrue(action.startswith("record fresh evidence for S"), action)
        self.assertIn("loop-a is unresolved", action)
        self.assertNotIn("set reuse", action)

    def test_a_gitlink_beside_a_touched_path_is_still_named_unresolved(self):
        label, paths, _ = reuse_eligibility(ob("S", boundary={"paths": ["module", "docs"]}), self.repo.c1,
                                            self.repo.ctx(self.repo.after["docs/rules.md"]))
        self.assertEqual((label, paths), ("unresolved scope path module", ["module", "docs", "docs/rules.md"]))

    def test_a_loop_beside_a_touched_path_binds_a_refusal_naming_both(self):
        row = by_id(self.proved(["loop-a", "docs"]), "S")
        row["reuse"] = {"from": self.repo.c1}
        with self.assertRaises(PodError) as caught:
            _bind_reuse(row, None, self.repo.ctx(self.repo.after["docs/rules.md"]))
        error = caught.exception
        self.assertIn("REUSE is unproven: unresolved scope path loop-a", str(error))
        self.assertEqual((error.detail["referent"]["predicate"], error.detail["referent"]["paths"]),
                         ("reuse_scope_untouched", "loop-a,docs,docs/rules.md"))
        action = error.detail["next_action"]
        self.assertIn("loop-a is unresolved", action)
        self.assertIn("also touches its scope (docs, docs/rules.md)", action)
        self.assertNotIn("set reuse", action)

    def test_a_loop_beside_a_touched_path_names_both_in_the_aggregate_refusal(self):
        _, error = self.reuse(["loop-a", "docs"], "docs/rules.md")
        self.assertEqual((error.code, error.detail["detail"]), ("obligation_unaccounted", "evidence_invalidated"))
        entry = error.detail["referent"]["invalidations"][0]
        self.assertEqual((entry["eligibility"], entry["paths"], entry["reuse_failure"]),
                         ("unresolved scope path loop-a", ["loop-a", "docs", "docs/rules.md"],
                          {"predicate": "reuse_scope_untouched"}))
        self.assertIn("loop-a is unresolved", entry["correction"])
        self.assertIn("also touches its scope (docs, docs/rules.md)", entry["correction"])
        self.assertNotIn("add reuse", entry["correction"])


class ProposalTests(LinkFixture):
    """The refusal proposal (candidate-only classification) uses the resolved comparison."""

    def proposal(self, scope, changed):
        state = self.proved(scope)
        error = refused(self, "obligation_unaccounted", "evidence_invalidated", base.accept_write, state,
                        write(state, rows_of(state)), self.repo.ctx(self.repo.after[changed]))
        entry = error.detail["referent"]["invalidations"][0]
        self.assertEqual(entry["classification"], "candidate-only")
        return entry

    def test_a_link_touched_scope_proposes_fresh_evidence(self):
        entry = self.proposal("rules-link", "docs/rules.md")
        self.assertEqual(entry["eligibility"], "touched")
        self.assertTrue(entry["correction"].startswith("record fresh evidence for S"))
        self.assertIn("touches its scope", entry["correction"])

    def test_an_unresolved_scope_proposes_fresh_evidence_naming_the_path(self):
        entry = self.proposal("hop0", "other.txt")
        self.assertEqual(entry["eligibility"], "unresolved scope path hop0")
        self.assertIn("hop0 is unresolved", entry["correction"])
        self.assertNotIn("add reuse", entry["correction"])

    def test_an_unrelated_link_proposes_the_reuse(self):
        entry = self.proposal("code-link", "docs/rules.md")
        self.assertEqual(entry["eligibility"], "eligible")
        self.assertIn(f"add reuse {{from: {self.repo.c1}}}", entry["correction"])


class RetainedHistoryTests(LinkFixture):
    """A REUSE stored under the lexical comparison stays readable and writable, but is not current proof."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.target = cls.repo.after["docs/rules.md"]

    def stored(self):
        state = self.proved("rules-link")
        rows = rows_of(state)
        next(row for row in rows if row["id"] == "S")["reuse"] = {"from": self.repo.c1}
        # As the earlier lexical rule bound it: the delta does not name rules-link.
        return base.accept_write(state, write(state, rows), self.repo.ctx(self.target, resolve=False))

    def test_a_stored_now_touched_reuse_is_not_current_proof(self):
        row = by_id(self.stored(), "S")
        self.assertTrue(_reuse_valid(row, self.repo.ctx(self.target, resolve=False), self.target))
        self.assertFalse(_reuse_valid(row, self.repo.ctx(self.target), self.target))

    def test_its_receipt_history_stays_valid(self):
        row = by_id(self.stored(), "S")
        validate_receipts(row, self.repo.ctx(self.target), seq=row["receipts"][-1]["accepted_seq"])

    def test_the_next_write_gets_an_ordinary_refusal_with_a_fresh_evidence_route(self):
        stored = self.stored()
        error = refused(self, "obligation_unaccounted", "evidence_invalidated", base.accept_write, stored,
                        write(stored, rows_of(stored)), self.repo.ctx(self.target))
        entry = error.detail["referent"]["invalidations"][0]
        self.assertEqual((entry["eligibility"], entry["reuse_failure"]), ("touched", {"predicate": "reuse_scope_untouched"}))
        self.assertTrue(entry["correction"].startswith("record fresh evidence for S"))

    def test_fresh_evidence_at_the_current_candidate_is_accepted(self):
        stored = self.stored()
        rows = rows_of(stored)
        row = next(item for item in rows if item["id"] == "S")
        row.pop("reuse")
        row["evidence"] = [proof("S", self.target, definition=ob("S", boundary={"paths": ["rules-link"]}))]
        accepted = base.accept_write(stored, write(stored, rows), self.repo.ctx(self.target))
        self.assertEqual(by_id(accepted, "S")["state"], "satisfied")
        self.assertNotIn("reuse", by_id(accepted, "S"))


class LedgerContextTests(LinkFixture):
    def test_the_kernel_context_supplies_the_resolver_cached_per_pair(self):
        target = self.repo.after["docs/rules.md"]
        ctx = kernel_context(self.repo.root, "objective", {"admissions": {}}, None, candidate=target)
        resolver = ctx["reuse_resolver"](self.repo.c1, target)
        self.assertIs(ctx["reuse_resolver"](self.repo.c1, target), resolver)
        self.assertEqual((resolver("rules-link"), resolver("cfg/rules.md"), resolver("loop-a")),
                         (["docs/rules.md", "rules-link"], ["docs/rules.md", "cfg"], None))
        self.assertEqual(ctx["git_delta"](self.repo.c1, target), ["docs/rules.md"])
        self.assertEqual(reuse_eligibility(ob("S", boundary={"paths": ["rules-link"]}), self.repo.c1, ctx)[0], "touched")

    def test_the_kernel_context_compares_a_re_pointed_intermediate_link(self):
        target = self.repo.after["middle -> ./docs/rules.md"]
        ctx = kernel_context(self.repo.root, "objective", {"admissions": {}}, None, candidate=target)
        self.assertEqual(reuse_eligibility(ob("S", boundary={"paths": ["chain"]}), self.repo.c1, ctx)[:2],
                         ("touched", ["middle"]))


if __name__ == "__main__":
    unittest.main()
