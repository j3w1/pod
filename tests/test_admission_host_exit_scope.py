"""R212 (pod#227): each query's host-exit candidates are the tightest sound superset of the rows its projection holds
and of the rows whose counting can expose a record-side hold.

A worktree-only query decides a located row by its path, a row in this repository's registered worktree is judged
exactly, and a row outside the repository that is not on one of the query's own Runs is a candidate only when a record
can hold it: an owed admission binding it, judged by its recorded placement, or a record naming its Run that cannot be
read whole (A859, A898). A fake verifier replaces Pod's transport, so no process starts and no read reaches live
Orca."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pod import admission, process_exit
from pod.errors import PodError
from pod.window import Budget
from tests import test_admission_query as base
from tests import test_admission_unconsumed_report as unconsumed
from tests.common import GroupedTable, assert_grouped_table
from tests.process_exit_support import FakeVerifier, VerifierMixin, exit_codes, stuck

OTHER_KEY = "f" * 64  # another repository's key, recorded in a placement


def git(*argv):
    subprocess.run(["git", *argv], check=True, capture_output=True)


class ScopeCase(VerifierMixin, base.AdmissionQueryCase):
    """Three records: this repository's open objective naming `own-run` with recorded placements, a closed record
    whose deferred admission binds the gap Dispatch on `old-run` and whose admission inventory cannot be read whole,
    and another repository's record naming `other-run`, with its own recorded placements.

    Admission only reads, so the world (temporary root, environment, Git repositories, records and native doubles) is
    built once per class on a holder instance and kept until the class ends; each test adds its own fake verifier."""

    GONE = base.TerminalHistoryTests.GONE
    terminal = base.TerminalHistoryTests.terminal
    bound = base.TerminalHistoryTests.bound

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        world = cls()  # a fixture holder, never run as a test
        cls.addClassCleanup(world.doCleanups)
        base.AdmissionQueryCase.setUp(world)
        world.build()
        cls.world = world

    def setUp(self):
        vars(self).update({name: value for name, value in vars(self.world).items() if not name.startswith("_")})
        self.fake = FakeVerifier()
        transport = patch.object(process_exit, "_output", side_effect=self.fake)
        transport.start(); self.addCleanup(transport.stop)

    def build(self):
        self.setup_verifier()
        guard = patch("pod.admission._read", side_effect=AssertionError("fixture: no live native read"))
        guard.start(); self.addCleanup(guard.stop)
        runs = patch("pod.admission._run_coordinators", side_effect=lambda runtime, check=None: {})
        runs.start(); self.addCleanup(runs.stop)
        claims = patch("pod.admission._native_claim", return_value={"process": "unverifiable", "effects": []})
        claims.start(); self.addCleanup(claims.stop)
        self.key = self.place["repo_key"]
        self.placements = {}  # Dispatch -> (path, branch, repository key, owed) this repository's record recorded
        self.foreign_placements = {}  # the same, as the other repository's record recorded them on `other-run`
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
        self.arrange()

    def arrange(self):
        """Each class's own Git worktrees, records and fleet rows."""

    def placed(self, dispatch, path, branch, key, *, owed=False, foreign=False):
        """A bound admission whose accepted placement is `path` on `branch`: this repository's record's on `own-run`,
        or the other repository's record's on `other-run`; `owed` leaves its report unconsumed."""
        run = "other-run" if foreign else "own-run"
        (self.foreign_placements if foreign else self.placements)[dispatch] = (str(path), branch, key, owed)
        return stuck(self, dispatch, path=path, run=run)

    @staticmethod
    def admission_rows(placements, objective, run):
        return {f"a-{dispatch}": {
            "schema": "pod-admission/v4", "admission_id": f"a-{dispatch}", "objective": objective, "owner": "owner",
            "state": "bound", "role": "implement", "runtime": "runtime", "run_id": run, "task_id": "task",
            "worktree": None, "placement_binding": {"path": path, "branch": branch, "repo_key": key},
            "report_consumed": not owed, "disposition_recorded": not owed,
            "native_binding": {"runId": run, "taskId": "task", "dispatchId": dispatch, "workerId": "worker",
                               "worktreeId": path, "terminalHandle": dispatch + "-terminal"}}
            for dispatch, (path, branch, key, owed) in placements.items()}

    def facts_for(self, path, *, include_admissions):
        facts = self.real_facts(path, include_admissions=include_admissions)
        checkpoint = facts["context"]["checkpoint"]
        name = checkpoint["objective"]
        if name == "objective":
            checkpoint["native_refs"] = [{"runId": "own-run", "runtime": "runtime"}]
            if include_admissions:
                facts["admissions"] = self.admission_rows(self.placements, "objective", "own-run")
        elif name == "foreign":
            checkpoint["native_refs"] = [{"runId": "other-run", "runtime": "runtime"}]
            if include_admissions:
                facts["admissions"] = self.admission_rows(self.foreign_placements, "foreign", "other-run")
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
    """Every Dispatch a claim or an unknown of this result names, aggregated target and effects unknowns included."""
    return ({row["dispatch"] for row in result["claims"] if row.get("dispatch")}
            | {row["dispatch"] for row in result["unknowns"] if row.get("dispatch")}
            | {dispatch for row in result["unknowns"]
               for dispatch in (row.get("references") or {}).get("dispatches", ())})


def facts(result):
    """The result's relevant claims and unknowns, comparable as sets."""
    return ({json.dumps(row, sort_keys=True) for row in result["claims"]},
            {json.dumps(row, sort_keys=True) for row in result["unknowns"]})


SHAPES = ("empty", "worktree only, main", "worktree only, feature", "worktree only, removed",
          "worktree only, other repository", "worktree only, foreign owed", "branch only, main", "branch only, feature",
          "branch only, worker", "both", "target only", "effects only")


class ProjectionTests(ScopeCase, GroupedTable):
    """A859: over every row location and request shape, every row the projection holds is a candidate, counting every
    row in scope adds nothing relevant that counting the candidates does not, and the candidates are exactly the
    documented superset. Three shapes per named group method."""

    GROUPS = {"test_the_empty_and_registered_worktree_requests": ["empty", "worktree only, main",
                                                                  "worktree only, feature"],
              "test_the_unregistered_worktree_requests": ["worktree only, removed", "worktree only, other repository",
                                                          "worktree only, foreign owed"],
              "test_the_branch_requests": ["branch only, main", "branch only, feature", "branch only, worker"],
              "test_the_both_target_and_effects_requests": ["both", "target only", "effects only"]}

    ALWAYS = {"unlocated", "unlocated-other", "old-dispatch"}
    LOOSE = {"removed", "other-repo"}  # outside the registry, a branch only a later record read may know
    # Outside the registry, on the branch this repository's record places them on (pod#243); never `placed-other-key`,
    # which that record places under another repository's key.
    PLACED = {"main": {"placed-main"}, "feature": {"placed-feature"}}
    UNREAD = {"old-run-row"}  # on a Run whose record cannot be read whole: the placement lookup names it

    def arrange(self):
        root, project = self.root, self.project
        git("-C", str(project), "worktree", "add", "-q", "-b", "feature", str(root / "feature"))
        git("-C", str(project), "worktree", "add", "-q", "--detach", str(root / "detached"))
        # Admitted on `worker`, its report still owed, then detached: the registry no longer names its branch.
        git("-C", str(project), "worktree", "add", "-q", "-b", "worker", str(root / "owed-detached"))
        git("-C", str(root / "owed-detached"), "checkout", "-q", "--detach")
        git("init", "-q", "-b", "main", str(root / "other"))  # another repository's worktree
        (root / "other" / "w").mkdir()
        self.terminal("closed")  # its deferred admission binds `old-dispatch` on `old-run`: the gap Dispatch
        unlocated = [stuck(self, "unlocated", run="own-run"), stuck(self, "unlocated-other", run="other-run")]
        for row in unlocated:
            row["resource"]["worktreeId"], row["projection"]["workspace"] = None, {}
        self.workers = [*unlocated,
            stuck(self, "old-run-row", path=root / "removed-3", run="old-run"),  # first, so the lookup names it
            stuck(self, "main", path=project, run="own-run"),
            stuck(self, "main-nested", path=project / "wa", run="own-run"),
            stuck(self, "feature", path=root / "feature", run="own-run"),
            stuck(self, "detached", path=root / "detached", run="own-run"),
            stuck(self, "removed", path=root / "removed", run="own-run"),
            self.placed("placed-main", root / "placed-main", "main", self.key),
            self.placed("placed-feature", root / "placed-feature", "feature", self.key),
            self.placed("placed-other-key", root / "placed-other-key", "main", OTHER_KEY),
            self.placed("owed-detached", root / "owed-detached", "worker", self.key, owed=True),
            self.placed("foreign-owed", root / "foreign-owed", "worker", self.key, owed=True, foreign=True),
            stuck(self, "other-repo", path=root / "other", run="own-run"),
            stuck(self, "other-repo-other-run", path=root / "other" / "w", run="other-run"),
            stuck(self, "removed-other-run", path=root / "removed-2", run="other-run"),
            stuck(self, "old-dispatch", path=self.GONE, run="old-run", handle="old-terminal")]

    def shapes(self):
        """Each shape: its request, its exact candidates, and Dispatches its result must hold and must not hold."""
        project, root = str(self.project), self.root
        every = {row["dispatchId"] for row in self.workers}
        located = every - self.ALWAYS
        wider = self.LOOSE | self.UNREAD | self.ALWAYS  # what every branch, target or effects request takes
        placed = self.PLACED
        # The projection knows recorded placements: a recorded other branch or another repository's key frees a branch
        # query, and an unknown branch holds it; a located row holds only the worktree-only query that overlaps it. A
        # row whose placement this repository's record binds has a complete location: S judges it exactly, so it is
        # asked about only on its recorded branch and never for a target-only or effects-only request.
        return {
            "empty": ({}, every, None),
            "worktree only, main": ({"worktree": project}, {"main", "main-nested"} | self.ALWAYS, None),
            "worktree only, feature": ({"worktree": str(root / "feature")}, {"feature"} | self.ALWAYS, None),
            "worktree only, removed": ({"worktree": str(root / "removed")}, {"removed"} | self.ALWAYS,
                                       ({"removed"}, located - {"removed"})),
            "worktree only, other repository": ({"worktree": str(root / "other")}, {"other-repo"} | self.ALWAYS,
                                                ({"other-repo"}, located - {"other-repo"})),
            # Another repository's owed admission placed it here: counting it would expose that hold.
            "worktree only, foreign owed": ({"worktree": str(root / "foreign-owed")}, {"foreign-owed"} | self.ALWAYS,
                                            None),
            "branch only, main": ({"branch": "main"}, {"main", "main-nested"} | placed["main"] | wider,
                                  ({"placed-main", "removed", "other-repo"},
                                   {"placed-feature", "placed-other-key", "feature", "detached"})),
            "branch only, feature": ({"branch": "feature"}, {"feature"} | placed["feature"] | wider,
                                     ({"placed-feature"}, {"placed-main", "placed-other-key"})),
            # Both owed admissions recorded `worker`: one detached after admission, one another repository's.
            "branch only, worker": ({"branch": "worker"}, {"owed-detached", "foreign-owed"} | wider, None),
            "both": ({"worktree": project, "branch": "feature"},
                     {"main", "main-nested", "feature"} | placed["feature"] | wider, None),
            "target only": ({"target": "main"}, wider, (set(), {"placed-main", "placed-feature", "placed-other-key"})),
            "effects only": ({"effects": ["deploy:target"]}, wider,
                             (set(), {"placed-main", "placed-feature", "placed-other-key"}))}

    def row(self, label):
        request, expected, holds = self.shapes()[label]
        self.fake.mode = "fail"  # nothing counts, so every candidate and every held row stays visible
        seen = self.read(self.workers, **request)
        self.fake.mode = None  # a correct verifier: every candidate counts
        counted = self.read(self.workers, **request)
        with patch("pod.admission._host_exit_relevant", return_value=True):
            everything = self.read(self.workers, **request)  # every row in scope counts
        with self.subTest(label):
            self.assertLessEqual(held(seen), candidates(seen), seen["unknowns"])
            self.assertEqual(candidates(seen), expected)
            exposed, kept = facts(everything), facts(counted)
            self.assertLessEqual(exposed[0], kept[0], "counting a non-candidate exposed a claim")
            self.assertLessEqual(exposed[1], kept[1], "counting a non-candidate exposed an unknown")
            if holds is not None:
                self.assertLessEqual(holds[0], held(seen))
                self.assertFalse(holds[1] & held(seen), held(seen))

    def test_the_empty_and_registered_worktree_requests(self): self.rows()
    def test_the_unregistered_worktree_requests(self): self.rows()
    def test_the_branch_requests(self): self.rows()
    def test_the_both_target_and_effects_requests(self): self.rows()


class ProjectionTableTests(unittest.TestCase):
    def test_the_projection_table_runs_every_shape_once(self):
        assert_grouped_table(self, ProjectionTests, SHAPES, limit=3)


class Memo:
    """A call's shared memo, so the table resolves each path once."""

    def __init__(self):
        self.values = {}

    def memo(self, key, fn):
        if key not in self.values:
            self.values[key] = fn()
        return self.values[key]


class FactTests(unittest.TestCase):
    """A859: S keeps a row whenever the projection could hold the query through it, for every fact a record may give:

    - its native writer claim or process-liveness unknown, when the projection claims it (no path, inside the
      repository, or on one of the query's own Runs), under `_relevant` for every key and branch a record may give an
      unregistered row (a registered row's writer takes the registry's branch, never a record's);
    - the placement lookup's `worker_placement_unknown` for an unregistered row on a Run whose record cannot be read;
    - every hold counting it can expose: an owed admission binding it, in this repository's records or its Run's other
      repository's, judged by the placement it recorded (any location and branch, writer or assignment), and an
      unreadable foreign owner, judged as the projection's unknown for the row.
    Pure: the registry is given, the record readers are doubles, and no record, Git or native read is made."""

    key = "a" * 64

    def test_every_hold_is_kept_for_requests_naming_a_worktree(self):
        self.enumerate(lambda request: bool(request.get("worktree")))

    def test_every_hold_is_kept_for_the_other_requests(self):
        self.enumerate(lambda request: not request.get("worktree"))

    def test_a_recorded_placement_is_judged_exactly(self):
        """pod#243: a row outside the registry whose exact native identity this repository's record binds has the
        complete location that admission's placement gives, so for a branch, target or effects request S equals the
        projection's judgment of it with nothing else recorded; a binding of another identity leaves the row
        unrecovered and kept. A worktree-only request still decides the row by its path alone."""
        with tempfile.TemporaryDirectory() as temporary:
            self.root = Path(temporary).resolve()
            self.project = self.root / "project"
            self.project.mkdir()
            with patch("pod.admission._SHARED") as shared:
                shared.get.return_value = Memo()
                self.placement_table()

    def placement_table(self):
        root, project = self.root, str(self.project)
        places = {project: "main", str(root / "feature"): "feature"}
        scope = {"repository": None, "repo_key": self.key, "path": project, "inventory": "pod_objectives_and_recorded_runs"}
        requests = [{"worktree": str(root / "removed")}, {"worktree": project}, {"branch": "main"},
                    {"branch": "worker"}, {"worktree": project, "branch": "worker"},
                    {"worktree": str(root / "removed"), "branch": "main"}, {"target": "main"},
                    {"effects": ["deploy:target"]}, {"target": "main", "effects": ["deploy:x"]}]
        locations = {"removed": (str(root / "removed"), "own-run"), "nesting": (str(root), "own-run")}
        checked, failures = 0, []
        for request in requests:
            full = {"worktree": None, "branch": None, "target": None, "effects": [], **request}
            relevant = lambda facts: admission._relevant(facts, full, self.key, admission._overlap, "process_liveness")
            for label, (path, run) in locations.items():
                shown = {"dispatch": label, "run": run, "task": "task", "worker": "worker", "terminal": "t",
                         "worktree": path}
                binding = {"runId": run, "taskId": "task", "dispatchId": label, "workerId": "worker",
                           "worktreeId": path, "terminalHandle": "t"}
                for key in (None, self.key, OTHER_KEY):
                    for branch in (None, "main", "worker"):
                        placement = {"path": path, "branch": branch, "repo_key": key}
                        for matching in (True, False):
                            bound = binding if matching else {**binding, "terminalHandle": "another"}
                            slot = admission._HostExitSlot(
                                self.project, full, {"own-run"}, {"own-run"}, set(), set(), scope=scope, owed={},
                                owners=lambda dispatch, run: (), unreadable=lambda run: (False, ()),
                                placements={label: (bound, placement)})
                            native = {"repo_key": key, "worktree": path, "branch": branch, "locations_complete": True}
                            if full["worktree"] and not full["branch"]:
                                expected = admission._overlap(full["worktree"], path)  # by its path, as before
                            elif matching:
                                expected = relevant(native)
                            else:
                                expected = True
                            checked += 1
                            if admission._host_exit_relevant(slot, shown, places) != expected:
                                failures.append((request, label, key, branch, matching))
        self.assertEqual(failures[:20], [])
        self.assertEqual(checked, 324)

    def situations(self, path):
        """What the records may say about a row at `path`: nothing, an owed admission of this repository or of its
        Run's other repository at a recorded location and branch, an unreadable foreign owner, or an unreadable record
        naming its Run."""
        rows = [("nothing", {}, (), False), ("run unreadable", {}, (), True),
                ("foreign owner unreadable", {}, ((None, None, PodError("record_changed", "fixture")),), False)]
        locations = {None, path, str(self.project), str(self.root / "removed"), str(self.root / "recorded")}
        for role in ("implement", "review"):
            for location in sorted(locations, key=str):
                for branch in (None, "main", "feature", "worker"):
                    admission_row = {"role": role, "worktree": None, "boundary": None,
                                     "native_binding": {"worktreeId": location},
                                     "placement_binding": {"path": location, "branch": branch, "repo_key": self.key}}
                    hold = admission._owed_hold("o", "r", "a", admission_row)
                    label = f"{role} owed at {location} on {branch}"
                    rows.append(("this repository's " + label, {"row": [hold[0]]}, (), False))
                    rows.append(("foreign " + label, {}, ((None, None, hold),), False))
        return rows

    def held(self, slot, path, run, places, situation):
        """Whether the projection could hold the query through the row, uncounted or counted."""
        _, owed, owners, unreadable = situation
        request, key = slot.request, self.key
        registered, inside = admission._workspace(self.project, path, places) if path else (None, False)
        relevant = lambda facts, fact=None: admission._relevant(facts, request, key, admission._overlap, fact)
        fault = relevant({**slot.scope, "worktree": path}, "worker_placement")
        if path is None or inside or run in slot.own_runs:  # the native writer and its process-liveness unknown
            if registered is not None:
                variants = [(key, places[registered], True)]
            else:
                variants = [(None, None, False), *((k, b, True) for k in (None, key, OTHER_KEY)
                                                   for b in (None, "main", "feature", "worker"))]
            for k, branch, known in variants:
                native = {"repo_key": k, "worktree": path, "branch": branch, "locations_complete": bool(path) and known}
                if relevant(native, "process_liveness") or relevant({**native, "kind": "writer", "native_effects": []}):
                    return True
        if path and registered is None and unreadable and fault:  # the placement lookup's unknown
            return True
        return (any(relevant(hold) for hold in owed.get("row", ()))  # what counting it exposes
                or any(isinstance(outcome, PodError) and fault or isinstance(outcome, tuple) and relevant(outcome[0])
                       for _, _, outcome in owners))

    def enumerate(self, selected):
        with tempfile.TemporaryDirectory() as temporary:
            self.root = Path(temporary).resolve()
            self.project = self.root / "project"
            self.project.mkdir()
            with patch("pod.admission._SHARED") as shared:
                shared.get.return_value = Memo()
                self.table(selected)

    def table(self, selected):
        root, project = self.root, str(self.project)
        feature, detached = str(root / "feature"), str(root / "detached")
        places = {project: "main", feature: "feature", detached: None}
        locations = {"unlocated": (None, "own-run"), "unlocated, other run": (None, "other-run"),
                     "registered": (project, "own-run"), "nested": (project + "/wa", "own-run"),
                     "registered, other run": (project, "other-run"), "feature": (feature, "own-run"),
                     "detached": (detached, "own-run"), "removed": (str(root / "removed"), "own-run"),
                     "removed, other run": (str(root / "removed"), "other-run"),
                     "another repository, other run": (str(root / "other"), "other-run")}
        requests = [{}, {"worktree": project}, {"worktree": project + "/wa"}, {"worktree": feature},
                    {"worktree": detached}, {"worktree": str(root / "removed")}, {"worktree": str(root / "recorded")},
                    {"worktree": str(root)}, {"branch": "main"}, {"branch": "feature"}, {"branch": "worker"},
                    {"branch": "other"}, {"worktree": project, "branch": "main"},
                    {"worktree": project, "branch": "feature"}, {"worktree": str(root / "removed"), "branch": "worker"},
                    {"target": "main"}, {"effects": ["deploy:target"]}, {"target": "main", "effects": ["deploy:x"]}]
        scope = {"repository": None, "repo_key": self.key, "path": project,
                 "inventory": "pod_objectives_and_recorded_runs"}
        checked, failures = 0, []
        for request in filter(selected, requests):
            full = {"worktree": None, "branch": None, "target": None, "effects": [], **request}
            for label, (path, run) in locations.items():
                for situation in self.situations(path):
                    _, owed, owners, unreadable = situation
                    slot = admission._HostExitSlot(
                        self.project, full, {"own-run", "other-run"}, {"own-run"}, set(), set(), scope=scope,
                        owed={label: owed["row"]} if owed else {}, owners=lambda dispatch, run, owners=owners: owners,
                        unreadable=lambda run, unreadable=unreadable: (unreadable, ()))
                    if self.held(slot, path, run, places, situation):
                        checked += 1
                        if not admission._host_exit_relevant(slot, {"dispatch": label, "run": run, "worktree": path},
                                                             places):
                            failures.append((request, label, situation[0]))
            # A gap Dispatch is a candidate on any Run, for rule T, whether or not the projection claims it.
            slot = admission._HostExitSlot(self.project, full, {"own-run"}, {"own-run"}, {"gap"}, set(), scope=scope,
                                           owed={}, owners=lambda dispatch, run: (), unreadable=lambda run: (False, ()))
            self.assertTrue(admission._host_exit_relevant(
                slot, {"dispatch": "gap", "run": "old-run", "worktree": str(root / "gap")}, places))
        self.assertEqual(failures[:20], [])
        self.assertGreater(checked, 2000)


class OrphanTests(ScopeCase):
    """A898: settled Dispatches of removed worktrees and of other repositories no longer fill every query's set."""

    def arrange(self):
        git("init", "-q", "-b", "main", str(self.root / "other"))
        self.removed = [self.root / f"removed-{index}" for index in range(4)]
        self.workers = [*(stuck(self, f"gone-{index}", path=path, run="own-run")
                          for index, path in enumerate(self.removed)),
                        *(stuck(self, f"theirs-{index}", path=self.root / "other", run="other-run")
                          for index in range(2))]

    def setUp(self):
        super().setUp()
        for name, value in (("K", 1), ("R", 4)):  # at most four ids per call
            bound = patch.object(process_exit, name, value)
            bound.start(); self.addCleanup(bound.stop)

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


class OwedHoldTests(VerifierMixin, unconsumed.UnconsumedReportCase):
    """A859 (the pod#227 review): a settled Dispatch whose counting exposes an owed-report hold stays a candidate.
    Stored records through the production facts reader; only the native inventories and the verifier's transport are
    doubled. With a correct verifier each reads as the rule before pod#227 does: the hold stands."""

    foreign = unconsumed.ForeignObjectiveTests.foreign

    def setUp(self):
        super().setUp()
        self.setup_verifier()
        self.fake = FakeVerifier()
        for target, replacement in (("pod.admission._run_coordinators", lambda runtime, check=None: {}),
                                    ("pod.admission._terminals",
                                     lambda: {"runtime": "runtime", "complete": True, "terminals": []}),
                                    ("pod.process_exit._output", self.fake)):
            double = patch(target, side_effect=replacement)
            double.start(); self.addCleanup(double.stop)

    def read(self):
        worker = stuck(self, "d1", path=self.worker_path, run="run", handle="d1-terminal")
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": [worker]}):
            return admission.query(self.project, branch=unconsumed.BRANCH)

    def foreign_owned_removed(self):
        """Another repository's objective owns Run `run` and owes the report of its worker placed here, whose
        worktree is gone; this repository's records do not name `run`."""
        self.foreign(terminal="d1-terminal")
        path, raw = self.raw()
        raw["checkpoint"]["native_refs"] = []
        self.save(path, raw)
        git("-C", str(self.project), "worktree", "remove", "--force", str(self.worker_path))

    def assert_counted_and_held(self, result, objective):
        self.assertEqual([(row["dispatch"], row["counted"]) for row in result.get("host_exits", [])], [("d1", True)])
        self.assertEqual((result["status"], result["complete"]), ("blocked", True), result["unknowns"])
        self.assertEqual([(row["objective"], row["branch"], row["liveness"]) for row in self.holds(result)],
                         [(objective, unconsumed.BRANCH, "exited")])

    def test_a_foreign_owners_owed_report_in_a_removed_worktree_holds_its_branch(self):
        self.foreign_owned_removed()
        self.assert_counted_and_held(self.read(), "foreign")

    def test_an_unreadable_foreign_owner_keeps_the_dispatch_a_candidate(self):
        self.foreign_owned_removed()
        real = base.history.load_admission_facts
        def incomplete(path, *, include_admissions):
            facts = real(path, include_admissions=include_admissions)
            if include_admissions and facts["context"]["checkpoint"]["objective"] == "foreign":
                facts.update(complete=False, admissions={}, errors=[{"code": "fixture_incomplete"}])
            return facts
        with patch("pod.history.load_admission_facts", create=True, side_effect=incomplete):
            result = self.read()
        self.assertEqual([(row["dispatch"], row["counted"]) for row in result.get("host_exits", [])], [("d1", True)])
        self.assertFalse(result["complete"])
        self.assertEqual([(row["code"], row.get("dispatch"), row.get("cause")) for row in result["unknowns"]],
                         [("worker_placement_unknown", "d1", "admission_facts_incomplete")])

    def test_a_worktree_detached_after_admission_holds_its_recorded_branch(self):
        self.admit(terminal="d1-terminal")
        git("-C", str(self.worker_path), "checkout", "--detach", "-q")
        self.assert_counted_and_held(self.read(), "objective")

    # R3 of the pod#227 review: every record the judgment reads is a recheck dependency of that query (R207, A869).

    def changed_during_the_judgment(self, restore=True, moved=False):
        """Another repository's owed admission placed here on BRANCH; the worker's old path is now another
        repository's worktree, so no placement lookup reads that owner (the foreign-owner pass still does, as for
        every settled row it skips, pod#237). With `moved`, the worktree is instead still registered here, on another
        branch. While the host-exit judgment runs, the owner holds claims-only bytes B placing it on another branch;
        `restore` puts its exact bytes A back afterwards."""
        if moved:
            self.foreign(terminal="d1-terminal")
            context, raw = self.raw()
            raw["checkpoint"]["native_refs"] = []
            self.save(context, raw)
            git("-C", str(self.worker_path), "checkout", "-q", "-b", "moved")
        else:
            self.foreign_owned_removed()
            git("init", "-q", "-b", "reused", str(self.worker_path))
        path = base.objective_root(self.root / "foreign-repository", "foreign") / "context.json"
        original = path.read_bytes()
        changed = json.loads(original)
        changed["admissions"]["a1"]["recovery"]["placement_binding"]["branch"] = "other"
        real = admission._host_exit_prefetch
        def prefetch(*args, **kwargs):
            path.write_text(json.dumps(changed))
            try:
                return real(*args, **kwargs)
            finally:
                if restore:
                    path.write_bytes(original)
        self.addCleanup(path.write_bytes, original)
        return path, patch("pod.admission._host_exit_prefetch", side_effect=prefetch)

    def test_an_owner_changed_and_restored_after_the_judgment_read_it_is_record_changed(self):
        path, schedule = self.changed_during_the_judgment()
        with schedule:
            result = self.read()
        self.assertFalse(result["complete"])
        self.assertIn(("record_changed", path.parent.name),
                      [(row["code"], row["record"]) for row in result["unknowns"]])

    def test_an_owner_left_changed_is_read_as_its_newer_bytes(self):
        """The control: claims-only bytes that stay are the record's current claims (R207), so the query is complete;
        but a claims read of a later generation than its attribution read never qualifies a census (R213, A885)."""
        _, schedule = self.changed_during_the_judgment(restore=False)
        with schedule:
            result = self.read()
        self.assertEqual((result["status"], result["complete"], result["claims"], result["unknowns"]),
                         ("observed", True, [], []))
        self.assertFalse(result["record_census"]["complete"])

    def test_an_unchanged_owner_the_judgment_read_is_bound_in_a_true_census(self):
        """A same-generation, complete owner read that leaves the row out qualifies: the census stays true and binds
        that read as the projection's own consulted reads are bound."""
        self.foreign(terminal="d1-terminal", placement={"path": str(self.worker_path), "branch": "other",
                                                        "repo_key": self.place["repo_key"]})
        path, raw = self.raw()
        raw["checkpoint"]["native_refs"] = []
        self.save(path, raw)
        git("-C", str(self.project), "worktree", "remove", "--force", str(self.worker_path))
        git("init", "-q", "-b", "reused", str(self.worker_path))
        bound, real = [], admission._RecordCensus.witness
        def witness(census, result, budget, journals, consulted, policies):
            bound.append(set(consulted))
            return real(census, result, budget, journals, consulted, policies)
        with patch.object(admission._RecordCensus, "witness", witness):
            result = self.read()
        self.assertEqual((result["status"], result["complete"], result.get("host_exits")), ("observed", True, None))
        self.assertTrue(result["record_census"]["complete"])
        self.assertIn(base.objective_root(self.root / "foreign-repository", "foreign"), bound[0])

    def test_only_a_query_whose_judgment_read_the_owner_depends_on_it(self):
        """In one batch, the branch query's judgment read the owner, and so did its foreign-owner pass, since its
        result leaves out the native writer on the other branch (pod#237); the unfiltered query takes every row
        natively, its result keeps that native writer, and it read nothing. With nothing counted, only the branch
        query rechecks the owner."""
        path, schedule = self.changed_during_the_judgment(moved=True)
        self.fake.mode = "fail"
        worker = stuck(self, "d1", path=self.worker_path, run="run", handle="d1-terminal")
        with schedule, patch("pod.admission._fleet",
                             return_value={"runtime": "runtime", "complete": True, "workers": [worker]}):
            results = admission._call(self.project, [{"branch": unconsumed.BRANCH}, {}], Budget(10.0))["results"]
        changed = [[row["record"] for row in result["unknowns"] if row["code"] == "record_changed"]
                   for result in results]
        self.assertEqual(changed, [[path.parent.name], []])


class RemovedPlacementTests(VerifierMixin, unconsumed.UnconsumedReportCase):
    """pod#243: a settled Dispatch whose worktree was removed, bound by an admission of this repository's records whose
    report was consumed, has the complete location that admission's placement gives. Stored records through the
    production facts reader; only the native inventories and the verifier's transport are doubled, and no Run
    coordinator is known, so rule S never settles a row and only R212 can count one exited."""

    foreign = unconsumed.ForeignObjectiveTests.foreign
    FINISHED = {"report": {"outcome": "succeeded"}, "disposition": {"kind": "accepted"}}

    def setUp(self):
        super().setUp()
        self.setup_verifier()
        self.fake = FakeVerifier()
        for target, replacement in (("pod.admission._run_coordinators", lambda runtime, check=None: {}),
                                    ("pod.admission._terminals",
                                     lambda: {"runtime": "runtime", "complete": True, "terminals": []}),
                                    ("pod.process_exit._output", self.fake)):
            double = patch(target, side_effect=replacement)
            double.start(); self.addCleanup(double.stop)

    def removed(self, **fields):
        """This repository's admission a1 placed d1 on BRANCH and its report was consumed; then the worktree went."""
        self.admit(terminal="d1-terminal", **{**self.FINISHED, **fields})
        git("-C", str(self.project), "worktree", "remove", "--force", str(self.worker_path))

    def workers(self):
        return [stuck(self, "d1", path=self.worker_path, run="run", handle="d1-terminal")]

    def read(self, workers=None, **request):
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True,
                                                         "workers": workers or self.workers()}):
            return admission.query(self.project, **request)

    def unconfigured(self, **request):
        """The same read with no verifier configured: R212 judges nothing and asks nothing."""
        with patch("pod.config.effective", return_value={"preferences": {"status": "missing",
                                                                         "authorization_verifier": None}}):
            return self.read(**request)

    REQUESTS = {"target only": {"target": "main"}, "effects only": {"effects": ["deploy:target"]},
                "another branch": {"branch": "main"}}

    def test_a_target_effects_or_other_branch_query_never_asks_about_it(self):
        self.removed()
        for label, request in self.REQUESTS.items():
            with self.subTest(label):
                self.fake.mode = None  # a correct verifier, which would count it
                result = self.read(**request)
                self.assertEqual(candidates(result), set())
                self.assertNotIn("d1", held(result))
                self.assertEqual(result, self.unconfigured(**request))
        self.assertEqual(self.fake.requests, [])

    def test_its_recorded_branch_still_asks_and_holds_until_counted(self):
        self.removed()
        self.fake.mode = "fail"
        uncounted = self.read(branch=unconsumed.BRANCH)
        self.assertEqual(candidates(uncounted), {"d1"})
        self.assertEqual(exit_codes(uncounted), {"d1": "host_exit_failed"})
        self.fake.mode = None
        counted = self.read(branch=unconsumed.BRANCH)
        self.assertEqual([(row["dispatch"], row["counted"]) for row in counted["host_exits"]], [("d1", True)])
        self.assertNotIn("d1", held(counted))

    def test_an_unrecovered_location_stays_a_candidate_and_holds(self):
        """No recorded branch, or a binding of another terminal: nothing makes the location complete, so a target-only
        query still asks, and without a count the projection holds it relevantly, as before pod#243."""
        unbranched = {"recovery": {"placement_binding": {"path": str(self.worker_path), "repo_key": self.place["repo_key"]}}}
        for label, fields in (("no branch", unbranched), ("another terminal", {"terminal": "another-terminal"})):
            with self.subTest(label):
                self.admit(**{**self.FINISHED, "terminal": "d1-terminal", **fields})
                if self.worker_path.exists():
                    git("-C", str(self.project), "worktree", "remove", "--force", str(self.worker_path))
                self.fake.mode = "fail"
                result = self.read(target="main")
                self.assertEqual(candidates(result), {"d1"})
                self.assertIn("d1", held(result))
                self.assertLessEqual(held(result), candidates(result))

    def test_another_records_binding_read_first_never_replaces_its_placement(self):
        """Another repository's record on the same Run binds d1 to another terminal on another branch. A row before d1,
        in a removed worktree no record places, makes the worker loop read that record first; d1 still takes this
        repository's placement, in either fleet order, so S and the projection agree that a target-only query has no
        relevant hold through it."""
        self.removed()
        own = self.context_path().parent.name
        for index in range(64):  # records are read in name order: the other one must come after, to replace it
            destination = self.foreign(name=f"foreign{index}", terminal="another-terminal", **self.FINISHED,
                                       placement={"path": str(self.worker_path), "branch": "other",
                                                  "repo_key": self.place["repo_key"]})
            if destination.parent.name > own:
                break
            shutil.rmtree(destination.parent)
        self.assertGreater(destination.parent.name, own)
        first = stuck(self, "first", path=self.root / "first-removed", run="run")
        self.fake.mode = "fail"
        results = [self.read([first, *self.workers()], target="main"),
                   self.read([*self.workers(), first], target="main")]
        for result in results:
            self.assertEqual(candidates(result), {"first"})
            self.assertNotIn("d1", held(result))
            self.assertLessEqual(held(result), candidates(result))
        self.assertEqual(results[0], results[1])


class DependencyTests(unittest.TestCase):
    """R3: the record clauses run only for a row the native judgment leaves out, and every record they read, whatever
    they decide, is that query's recheck dependency; a read that never reached a generation is none."""

    def slot(self, request, owners, unreadable):
        scope = {"repository": None, "repo_key": "a" * 64, "path": "/project", "inventory": "x"}
        return admission._HostExitSlot(Path("/project"), {"worktree": None, "branch": None, "target": None,
                                                          "effects": [], **request},
                                       {"own-run", "other-run"}, {"own-run"}, set(), set(), scope=scope, owed={},
                                       owners=owners, unreadable=unreadable)

    def test_every_record_read_is_a_dependency_whatever_it_decides(self):
        a, b, c, d = (Path("/state") / name for name in "abcd")
        hold = admission._owed_hold("o", "r", "k", {"role": "implement", "worktree": None, "boundary": None,
                                                    "native_binding": {"worktreeId": "/elsewhere"},
                                                    "placement_binding": {"path": "/elsewhere", "branch": "other",
                                                                          "repo_key": "a" * 64}})
        owners = ((a, "ga", None), (b, None, PodError("unreadable", "fixture")), (c, "gc", hold))
        slot = self.slot({"worktree": "/project/w"}, lambda dispatch, run: owners, lambda run: (False, ((d, "gd"),)))
        with patch("pod.admission._SHARED") as shared:
            shared.get.return_value = Memo()
            kept = admission._host_exit_relevant(slot, {"dispatch": "x", "run": "other-run", "worktree": "/gone"}, {})
        self.assertFalse(kept)
        self.assertEqual(slot.dependencies, {"a": (a, "ga"), "c": (c, "gc"), "d": (d, "gd")})

    def test_a_row_the_native_judgment_keeps_reads_no_record(self):
        def unread(*args):
            raise AssertionError("no record read for a row the native judgment keeps")
        slot = self.slot({"worktree": "/gone"}, unread, unread)  # overlapping, on one of the query's own Runs
        with patch("pod.admission._SHARED") as shared:
            shared.get.return_value = Memo()
            self.assertTrue(admission._host_exit_relevant(slot, {"dispatch": "x", "run": "own-run",
                                                                 "worktree": "/gone"}, {}))
        self.assertEqual(slot.dependencies, {})
