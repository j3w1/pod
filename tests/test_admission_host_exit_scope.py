"""R212 (pod#227): each query's host-exit candidates are the tightest sound superset of the rows its projection holds.

A worktree-only query decides a located row by its path, a row in this repository's registered worktree is judged
exactly, and a row outside the repository that is not on one of the query's own Runs is never a candidate, because
the projection makes no claim for it (A859, A898). A fake verifier replaces Pod's transport, so no process
starts and no read reaches live Orca."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pod import admission, process_exit
from pod.window import Budget
from tests import test_admission_query as base
from tests.process_exit_support import FakeVerifier, VerifierMixin, exit_codes, stuck

OTHER_KEY = "f" * 64  # another repository's key, recorded in a placement


def git(*argv):
    subprocess.run(["git", *argv], check=True, capture_output=True)


class ScopeCase(VerifierMixin, base.AdmissionQueryCase):
    """Three records: this repository's open objective naming `own-run` with recorded placements, a closed record
    whose deferred admission binds the gap Dispatch on `old-run`, and another repository's record naming
    `other-run`."""

    GONE = base.TerminalHistoryTests.GONE
    terminal = base.TerminalHistoryTests.terminal
    bound = base.TerminalHistoryTests.bound

    def setUp(self):
        super().setUp()
        self.setup_verifier()
        guard = patch("pod.admission._read", side_effect=AssertionError("fixture: no live native read"))
        guard.start(); self.addCleanup(guard.stop)
        runs = patch("pod.admission._run_coordinators", side_effect=lambda runtime, check=None: {})
        runs.start(); self.addCleanup(runs.stop)
        claims = patch("pod.admission._native_claim", return_value={"process": "unverifiable", "effects": []})
        claims.start(); self.addCleanup(claims.stop)
        self.fake = FakeVerifier()
        transport = patch.object(process_exit, "_output", side_effect=self.fake)
        transport.start(); self.addCleanup(transport.stop)
        self.key = self.place["repo_key"]
        self.placements = {}  # Dispatch -> (path, branch, repository key) this repository's record recorded
        self.intake(worktree=self.place)
        self.write([self.criterion()], objective="foreign", governance={"base_ref": "target"}, worktree=self.place)
        self.foreign = base.objective_root(self.project, "foreign")
        self.real_facts, self.shape = base.history.load_admission_facts, None
        facts = patch("pod.history.load_admission_facts", create=True, side_effect=self.facts_for)
        facts.start(); self.addCleanup(facts.stop)
        real_membership = admission._membership
        def membership(project, context, record, raw, known=()):
            if record == self.foreign.name:
                return None  # another repository's record
            return real_membership(project, context, record, raw, known)
        foreign = patch("pod.admission._membership", side_effect=membership)
        foreign.start(); self.addCleanup(foreign.stop)

    def placed(self, dispatch, path, branch, key):
        """A bound admission of this repository's record whose accepted placement is `path` on `branch`."""
        self.placements[dispatch] = (str(path), branch, key)
        return stuck(self, dispatch, path=path, run="own-run")

    def admission_rows(self):
        return {f"a-{dispatch}": {
            "schema": "pod-admission/v4", "admission_id": f"a-{dispatch}", "objective": "objective", "owner": "owner",
            "state": "bound", "role": "implement", "runtime": "runtime", "run_id": "own-run", "task_id": "task",
            "worktree": None, "placement_binding": {"path": path, "branch": branch, "repo_key": key},
            "report_consumed": True, "disposition_recorded": True,
            "native_binding": {"runId": "own-run", "taskId": "task", "dispatchId": dispatch, "workerId": "worker",
                               "worktreeId": path, "terminalHandle": dispatch + "-terminal"}}
            for dispatch, (path, branch, key) in self.placements.items()}

    def facts_for(self, path, *, include_admissions):
        facts = self.real_facts(path, include_admissions=include_admissions)
        checkpoint = facts["context"]["checkpoint"]
        name = checkpoint["objective"]
        if name == "objective":
            checkpoint["native_refs"] = [{"runId": "own-run", "runtime": "runtime"}]
            if include_admissions:
                facts["admissions"] = self.admission_rows()
        elif name == "foreign":
            checkpoint["native_refs"] = [{"runId": "other-run", "runtime": "runtime"}]
        elif name == "old" and self.shape is not None:
            rows, placed = self.shape
            checkpoint["native_refs"] = [{"runId": "old-run", "runtime": "runtime"}]
            checkpoint["worktree"] = None
            if include_admissions:
                facts.update(admissions=rows, complete=placed, errors=[{"code": "objective_placement_unknown"}])
        return facts

    def read(self, workers, **request):
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": workers}), \
             patch("pod.admission._terminals", return_value={"runtime": "runtime", "complete": True, "terminals": []}):
            return admission.query(self.project, **request)

    def batch(self, workers, queries):
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": workers}), \
             patch("pod.admission._terminals", return_value={"runtime": "runtime", "complete": True, "terminals": []}):
            return admission._call(self.project, queries, Budget(10.0))


def candidates(result):
    return {row["dispatch"] for row in result.get("host_exits", [])}


def held(result):
    """Every Dispatch a claim or an unknown of this result names."""
    return ({row["dispatch"] for row in result["claims"] if row.get("dispatch")}
            | {row["dispatch"] for row in result["unknowns"] if row.get("dispatch")})


class ProjectionTests(ScopeCase):
    """A859: over every row location and request shape, every row the projection holds is a candidate, and the
    candidates are exactly the documented superset."""

    def setUp(self):
        super().setUp()
        root, project = self.root, self.project
        git("-C", str(project), "worktree", "add", "-q", "-b", "feature", str(root / "feature"))
        git("-C", str(project), "worktree", "add", "-q", "--detach", str(root / "detached"))
        git("init", "-q", "-b", "main", str(root / "other"))  # another repository's worktree
        (root / "other" / "w").mkdir()
        self.terminal("closed")  # its deferred admission binds `old-dispatch` on `old-run`: the gap Dispatch
        unlocated = [stuck(self, "unlocated", run="own-run"), stuck(self, "unlocated-other", run="other-run")]
        for row in unlocated:
            row["resource"]["worktreeId"], row["projection"]["workspace"] = None, {}
        self.workers = [*unlocated,
            stuck(self, "main", path=project, run="own-run"),
            stuck(self, "main-nested", path=project / "wa", run="own-run"),
            stuck(self, "feature", path=root / "feature", run="own-run"),
            stuck(self, "detached", path=root / "detached", run="own-run"),
            stuck(self, "removed", path=root / "removed", run="own-run"),
            self.placed("placed-main", root / "placed-main", "main", self.key),
            self.placed("placed-feature", root / "placed-feature", "feature", self.key),
            self.placed("placed-other-key", root / "placed-other-key", "main", OTHER_KEY),
            stuck(self, "other-repo", path=root / "other", run="own-run"),
            stuck(self, "other-repo-other-run", path=root / "other" / "w", run="other-run"),
            stuck(self, "removed-other-run", path=root / "removed-2", run="other-run"),
            stuck(self, "old-dispatch", path=self.GONE, run="old-run", handle="old-terminal")]
        self.fake.mode = "fail"  # nothing counts, so every candidate and every held row stays visible

    ALWAYS = {"unlocated", "unlocated-other", "old-dispatch"}
    LOOSE = {"removed", "placed-main", "placed-feature", "placed-other-key", "other-repo"}  # branch only a record knows

    def shapes(self):
        project, root = str(self.project), self.root
        every = {row["dispatchId"] for row in self.workers}
        return {
            "empty": ({}, every),
            "worktree only, main": ({"worktree": project}, {"main", "main-nested"} | self.ALWAYS),
            "worktree only, removed": ({"worktree": str(root / "removed")}, {"removed"} | self.ALWAYS),
            "worktree only, other repository": ({"worktree": str(root / "other")}, {"other-repo"} | self.ALWAYS),
            "worktree only, feature": ({"worktree": str(root / "feature")}, {"feature"} | self.ALWAYS),
            "branch only, main": ({"branch": "main"}, {"main", "main-nested"} | self.LOOSE | self.ALWAYS),
            "branch only, feature": ({"branch": "feature"}, {"feature"} | self.LOOSE | self.ALWAYS),
            "both": ({"worktree": project, "branch": "feature"},
                     {"main", "main-nested", "feature"} | self.LOOSE | self.ALWAYS),
            "target only": ({"target": "main"}, self.LOOSE | self.ALWAYS),
            "effects only": ({"effects": ["deploy:target"]}, self.LOOSE | self.ALWAYS)}

    def check(self, *labels):
        """Each shape's result: every row it holds is a candidate, and its candidates are exactly the superset."""
        results = {}
        for label in labels:
            request, expected = self.shapes()[label]
            results[label] = result = self.read(self.workers, **request)
            with self.subTest(label):
                self.assertLessEqual(held(result), candidates(result), result["unknowns"])
                self.assertEqual(candidates(result), expected)
        return results

    def test_the_empty_and_worktree_only_requests(self):
        results = self.check("empty", "worktree only, main", "worktree only, removed",
                             "worktree only, other repository", "worktree only, feature")
        # Each located row holds only the worktree-only query that overlaps it, whatever its branch or owner.
        self.assertEqual(held(results["worktree only, removed"]) - self.ALWAYS, {"removed"})
        self.assertEqual(held(results["worktree only, other repository"]) - self.ALWAYS, {"other-repo"})

    def test_the_branch_and_both_requests(self):
        results = self.check("branch only, main", "branch only, feature", "both")
        # The projection knows recorded placements: a recorded other branch or another repository's key frees the
        # query, and an unknown branch holds it.
        main = held(results["branch only, main"])
        self.assertTrue({"placed-main", "removed", "other-repo"} <= main, main)
        self.assertFalse({"placed-feature", "placed-other-key", "feature", "detached"} & main, main)

    def test_the_target_and_effects_requests(self):
        self.check("target only", "effects only")


class FactTests(unittest.TestCase):
    """A859: for every fact a record may give a row, `_relevant` keeping its writer claim or process-liveness unknown
    implies the row is a candidate. Pure: the registry is given, and no record, Git or native read is made."""

    key = "a" * 64

    def test_the_superset_contains_relevant_for_every_fact_a_record_may_give(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.root = Path(temporary).resolve()
            self.project = self.root / "project"
            self.project.mkdir()
            self.enumerate()

    def enumerate(self):
        root, project = self.root, str(self.project)
        feature, detached = str(root / "feature"), str(root / "detached")
        places = {project: "main", feature: "feature", detached: None}
        locations = {"unlocated": (None, "own-run"), "unlocated, other run": (None, "other-run"),
                     "registered": (project, "own-run"), "nested": (project + "/wa", "own-run"),
                     "feature": (feature, "own-run"), "detached": (detached, "own-run"),
                     "removed": (str(root / "removed"), "own-run"),
                     "removed, other run": (str(root / "removed"), "other-run"),
                     "gap": (str(root / "gap"), "own-run")}
        requests = [{}, {"worktree": project}, {"worktree": project + "/wa"}, {"worktree": feature},
                    {"worktree": str(root / "removed")}, {"worktree": str(root)}, {"branch": "main"},
                    {"branch": "feature"}, {"branch": "other"}, {"worktree": project, "branch": "main"},
                    {"worktree": project, "branch": "feature"}, {"worktree": str(root / "removed"), "branch": "main"},
                    {"target": "main"}, {"effects": ["deploy:target"]}, {"target": "main", "effects": ["deploy:x"]}]
        checked = 0
        with patch("pod.admission._SHARED") as shared:
            shared.get.return_value = None
            for request in requests:
                full = {"worktree": None, "branch": None, "target": None, "effects": [], **request}
                slot = admission._HostExitSlot(self.project, full, {"own-run", "other-run", "old-run"}, {"own-run"},
                                               {"gap"}, set())
                for label, (path, run) in locations.items():
                    dispatch = label
                    registered, inside = admission._workspace(self.project, path, places) if path else (None, False)
                    if not (path is None or inside or run in slot.own_runs):
                        continue  # the projection makes no claim and no unknown for it
                    if registered is not None:
                        variants = [(self.key, places[registered], True)]  # a complete location, no record read
                    else:  # without a matching record, or with one giving any key and branch
                        variants = [(None, None, False), *((key, branch, True) for key in (None, self.key, OTHER_KEY)
                                                           for branch in (None, "main", "feature"))]
                    for key, branch, known in variants:
                        scope = {"repo_key": key, "worktree": path, "branch": branch,
                                 "locations_complete": bool(path) and known}
                        writer = {**scope, "kind": "writer", "dispatch": dispatch, "native_effects": []}
                        kept = (admission._relevant(scope, full, self.key, admission._overlap, "process_liveness")
                                or admission._relevant(writer, full, self.key, admission._overlap))
                        if kept:
                            checked += 1
                            with self.subTest(request=request, row=label, key=key, branch=branch, known=known):
                                self.assertTrue(admission._host_exit_relevant(
                                    slot, {"dispatch": dispatch, "run": run, "worktree": path}, places))
                # A gap Dispatch is a candidate on any Run, for rule T, whether or not the projection claims it.
                self.assertTrue(admission._host_exit_relevant(
                    slot, {"dispatch": "gap", "run": "old-run", "worktree": str(root / "gap")}, places))
        self.assertGreater(checked, 200)


class OrphanTests(ScopeCase):
    """A898: settled Dispatches of removed worktrees and of other repositories no longer fill every query's set."""

    def setUp(self):
        super().setUp()
        for name, value in (("K", 1), ("R", 4)):  # at most four ids per call
            bound = patch.object(process_exit, name, value)
            bound.start(); self.addCleanup(bound.stop)
        git("init", "-q", "-b", "main", str(self.root / "other"))
        self.removed = [self.root / f"removed-{index}" for index in range(4)]
        self.workers = [*(stuck(self, f"gone-{index}", path=path, run="own-run")
                          for index, path in enumerate(self.removed)),
                        *(stuck(self, f"theirs-{index}", path=self.root / "other", run="other-run")
                          for index in range(2))]

    def test_each_removed_worktree_query_asks_only_its_own_row_and_counts_it(self):
        queries = [{"worktree": str(path)} for path in self.removed]
        singles = [self.read(self.workers, **query) for query in queries]
        answer = self.batch(self.workers, queries)
        self.assertEqual(answer["results"], singles)
        for index, result in enumerate(singles):
            self.assertEqual([(row["dispatch"], row["counted"]) for row in result["host_exits"]],
                             [(f"gone-{index}", True)])
        self.assertEqual(answer["discovery"]["host_exit"], {"requests": 4, "asked": 4, "counted": 4})

    def test_a_branch_query_never_takes_another_repositorys_rows(self):
        result = self.read(self.workers, branch="main")
        self.assertEqual(candidates(result), {f"gone-{index}" for index in range(4)})
        self.assertEqual(exit_codes(result), {})  # four ids fit the bound: every one counted
        self.assertTrue(all(row["counted"] for row in result["host_exits"]))
