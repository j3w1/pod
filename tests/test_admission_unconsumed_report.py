"""R185 and R206: a bound admission whose report is still owed keeps its placement held by its record once the native
side settles its Dispatch (Orca exited, released and exited, rule S, R212), in open, continued and closed records; a
`retired` record holds nothing. The R108 list counts such admissions per objective (A870-A880). The hold stands
whether or not the Dispatch is counted exited, and a native writer stands in for it only inside a query's own result
(A902-A904)."""
import json
from unittest.mock import patch

from pod import admission, listing, process_exit
from pod.governor import report_owed
from pod.github import repository_context
from pod.ledger import objective_root
from tests import test_admission_batch as batch
from tests.common import logical_json
from tests.kernel_support import git
from tests.process_exit_support import FakeVerifier, VerifierMixin, stuck
from tests.test_admission_host_exit import HostExitCase

BRANCH = "pod/worker"


def admission_row(path, repo_key, *, state="bound", role="implement", report=None, disposition=None,
                  terminal="terminal", **fields):
    """One stored admission placed in its own linked worktree `path` on BRANCH, bound to Dispatch d1; its accepted
    placement lives in its recovery facts, as a stored admission's does."""
    binding = {"runId": "run", "taskId": "task", "workerId": "worker", "dispatchId": "d1",
               "worktreeId": str(path), "terminalHandle": terminal}
    return {"schema": "pod-admission/v4", "admission_id": "a1", "state": state, "role": role,
            "runtime": "runtime", "native_binding": binding, "boundary": None, "report": report,
            "disposition": disposition, "worktree": str(path),
            "recovery": {"placement_binding": {"path": str(path), "branch": BRANCH, "repo_key": repo_key}},
            "result": {"worktree": str(path), "base": None}, **fields}


class UnconsumedReportCase(batch.FencedCase):
    """The open objective `objective` (FencedCase intake) admitted one worker in its own linked worktree. The
    single query and a one-query batch read the same native inventory, the batch with a real end fence."""

    def setUp(self):
        super().setUp()
        self.worker_path = self.root / "worker"
        git(self.project, "worktree", "add", "-q", "-b", BRANCH, str(self.worker_path))
        self.query_scope = {"worktree": str(self.worker_path), "branch": BRANCH}

    def context_path(self, objective="objective"):
        return objective_root(self.project, objective) / "context.json"

    def admit(self, objective="objective", **fields):
        path = self.context_path(objective)
        raw = logical_json(path)
        raw["admissions"]["a1"] = admission_row(self.worker_path, self.place["repo_key"], **fields)
        path.write_text(json.dumps(raw))
        return path

    def phase(self, phase):
        """Write the objective `old` in `phase` with a terminal entry, and admit its worker there."""
        if phase == "closed":
            self.write([self.criterion("satisfied", evidence=[self.proof("O1")])], objective="old",
                       governance={"base_ref": "target"}, close=True, worktree=self.place)
        else:
            self.write([self.criterion()], objective="old", governance={"base_ref": "target"}, worktree=self.place)
        path = self.admit("old")
        if phase in ("continued", "retired"):
            raw = logical_json(path)
            raw["continued_by" if phase == "continued" else "historical_retirement"] = {"fixture": phase}
            path.write_text(json.dumps(raw))
        return path

    def native_worker(self, *, verdict="exited", released=False, terminal_state="retained",
                      worker_state="succeeded", dispatch_status="completed"):
        row = batch.base.ReviewCorrectionTests.writer(self, path=self.worker_path, verdict=verdict, dispatch="d1")
        row.update(terminalState=terminal_state, workerState=worker_state, dispatchStatus=dispatch_status)
        if released:
            row["projection"]["resource"] = {"state": "released"}
            row["resource"].update(releaseState="released", ownershipState="released")
        return row

    def serve(self, worker):
        self.rows = [worker, *(row for row in self.rows if row["dispatchId"] != "d1")]
        self.native.pages = [batch.terminal_page([batch.terminal("a", self.root / "other")])]
        self.native.fleet = batch.fleet_model(self.rows)
        self.claim_values = {"t2": [batch.LIVE_CLAIM], "d1": [{"process": "exited", "effects": []}]}

    def answers(self, worker):
        """The single query and the one-query batch over the same native inventory."""
        self.serve(worker)
        single = admission.query(self.project, **self.query_scope)
        return single, self.batch(dict(self.query_scope))

    HELD_KEYS = {"objective", "record", "kind", "admission", "worktree", "branch", "locations_complete", "liveness",
                 "boundary"}  # exactly the record-side claim an unfinished unbound admission makes

    @staticmethod
    def holds(result):
        return [row for row in result["claims"] if row.get("admission") == "a1"]

    def assert_held(self, worker, objective="objective"):
        """Complete, with one record-side claim naming the admission's objective, Dispatch and placement, single
        and batch alike, and a stable fence."""
        single, answer = self.answers(worker)
        self.assertTrue(answer["native_fence"]["stable"])
        self.assertEqual(answer["responses"][0]["result"], single)
        held = self.holds(single)
        self.assertTrue(single["complete"], single["unknowns"])
        self.assertEqual((single["status"], answer["status"]), ("blocked", "blocked"))
        self.assertEqual(len(held), 1, single["claims"])
        self.assertEqual(set(held[0]), self.HELD_KEYS)
        self.assertEqual({key: held[0][key] for key in self.HELD_KEYS - {"record"}},
                         {"objective": objective, "kind": "writer", "admission": "a1", "worktree": str(self.worker_path),
                          "branch": BRANCH, "locations_complete": True, "liveness": "exited", "boundary": None})
        self.assertNotIn("d1", [row.get("dispatch") for row in single["claims"]])  # no native claim beside it
        self.assertEqual([sorted(row["dimensions"]) for row in single["conflicts"] if row.get("claim") == held[0]],
                         [["branch", "worktree"]])
        return single

    def assert_free(self, worker):
        single, answer = self.answers(worker)
        self.assertEqual(answer["responses"][0]["result"], single)
        self.assertTrue(single["complete"], single["unknowns"])
        self.assertEqual((single["status"], single["claims"]), ("observed", []))
        return single


class HoldTests(UnconsumedReportCase):
    """A870 and A872."""

    def test_an_orca_exited_worker_keeps_the_open_objectives_placement_held(self):
        self.admit()
        result = self.assert_held(self.native_worker(verdict="exited"))
        self.assertEqual(result["unknowns"], [])

    def test_an_exited_and_released_worker_keeps_the_placement_held(self):
        self.admit()
        self.assert_held(self.native_worker(verdict="exited", released=True, terminal_state="released"))

    def test_a_released_worker_with_unverifiable_liveness_stays_a_native_writer(self):
        """The native claim and its liveness unknown hold it; no second, record-side claim is added."""
        self.admit()
        single, answer = self.answers(self.native_worker(verdict="unverifiable", released=True,
                                                         terminal_state="released"))
        self.assertEqual(answer["responses"][0]["result"], single)
        self.assertEqual(self.holds(single), [])
        self.assertIn("d1", [row.get("dispatch") for row in single["claims"]])
        self.assertIn("native_liveness_unverified", [row["code"] for row in single["unknowns"]])

    def test_a_dispatch_absent_from_the_inventory_stays_unknown(self):
        self.admit()
        self.serve(self.native_worker())
        self.rows = [row for row in self.rows if row["dispatchId"] != "d1"]
        self.native.fleet = batch.fleet_model(self.rows)
        result = admission.query(self.project, **self.query_scope)
        self.assertEqual(self.holds(result), [])
        self.assertIn(("native_identity_unverified", "a1"),
                      [(row["code"], row.get("admission")) for row in result["unknowns"]])

    def test_a_closed_record_holds(self):
        self.phase("closed")
        self.assert_held(self.native_worker(verdict="exited"), objective="old")

    def test_a_continued_record_holds(self):
        self.phase("continued")
        self.assert_held(self.native_worker(verdict="exited"), objective="old")

    def test_a_retired_record_holds_nothing(self):
        self.phase("retired")
        self.assert_free(self.native_worker(verdict="exited"))

    def test_rule_s_keeps_the_branch_of_a_removed_worktree_held(self):
        """Rule S settles the ended Dispatch (worktree removed, Run without a coordinator); its branch remains."""
        self.admit()
        git(self.project, "worktree", "remove", "--force", str(self.worker_path))
        self.query_scope = {"branch": BRANCH}
        with patch("pod.admission._run_coordinators", side_effect=lambda runtime, check=None: {"run": None}):
            single, answer = self.answers(self.native_worker(verdict="unverifiable"))
        self.assertEqual(answer["responses"][0]["result"], single)
        self.assertIn("native_liveness_unverified", single["unrelated_unknowns"]["by_code"])  # rule S applied
        self.assertTrue(single["complete"], single["unknowns"])
        self.assertEqual([(row["admission"], row["branch"]) for row in self.holds(single)], [("a1", BRANCH)])


class FinishedTests(UnconsumedReportCase):
    """A873: only an admission that owes its report holds; finalization's one rule decides."""

    def test_a_finished_admission_settles_with_its_dispatch(self):
        cases = {"implement dispositioned": {"report": {"outcome": "succeeded"}, "disposition": {"kind": "accepted"}},
                 "review consumed": {"role": "review", "report": {"outcome": "succeeded"}},
                 "abandoned accounting": {"abandoned_accounting": {"classification": "x", "execution": "y"}}}
        for name, fields in cases.items():
            with self.subTest(name):
                self.admit(**fields)
                self.assert_free(self.native_worker(verdict="exited"))

    def test_an_implementation_without_disposition_still_holds(self):
        self.admit(report={"outcome": "succeeded"})
        self.assert_held(self.native_worker(verdict="exited"))

    def test_finalization_and_the_hold_share_one_rule(self):
        stored = admission_row(self.worker_path, self.place["repo_key"])
        projected = {**{key: value for key, value in stored.items() if key not in ("report", "disposition")},
                     "report_consumed": False, "disposition_recorded": False}
        self.assertEqual((report_owed(stored), report_owed(projected)), ("bound", "bound"))
        projected.update(report_consumed=True)
        stored.update(report={"outcome": "succeeded"})
        self.assertEqual((report_owed(stored), report_owed(projected)), ("undispositioned", "undispositioned"))
        projected.update(disposition_recorded=True)
        stored.update(disposition={"kind": "accepted"})
        self.assertEqual((report_owed(stored), report_owed(projected)), (None, None))
        self.assertIsNone(report_owed({**stored, "state": "deferred", "disposition": None}))

    def test_the_facts_reader_projects_presence_never_bodies(self):
        path = self.admit(report={"outcome": "succeeded", "observation": {"body": "PRIVATE"}})
        from pod import history
        row = history.load_admission_facts(path)["admissions"]["a1"]
        self.assertEqual((row["report_consumed"], row["disposition_recorded"]), (True, False))
        self.assertNotIn("report", row)
        self.assertNotIn("PRIVATE", json.dumps(row))


class RecheckTests(UnconsumedReportCase):
    """A874: a report seen consumed only by the claims read is a record change, never a settled hold."""

    def test_a_report_seen_consumed_only_by_the_claims_read_is_record_changed(self):
        path = self.admit()
        raw = logical_json(path)
        raw["admissions"]["a1"].update(report={"outcome": "succeeded"}, disposition={"kind": "accepted"})
        self.serve(self.native_worker(verdict="exited"))
        with self.claims_read_meets(path, json.dumps(raw).encode()):
            result = admission.query(self.project, **self.query_scope)
        self.assertFalse(result["complete"])
        self.assertIn("record_changed", [row["code"] for row in result["unknowns"]])


class ForeignObjectiveTests(UnconsumedReportCase):
    """A877: another repository's objective whose worker was placed in this repository holds that placement the
    same way; only the records owning the settled Dispatch's Run are read."""

    def foreign(self, phase="open", name="foreign", placement=None, **fields):
        """Record the objective `name` of its own second repository, owning Run `run`, with admission a1 placed in
        this repository's worker worktree (or the given accepted `placement`)."""
        other = self.root / (name + "-repository")
        other.mkdir(exist_ok=True)
        git(other, "init", "-q", "-b", "main")
        raw = logical_json(self.context_path())
        raw["checkpoint"]["objective"] = name
        raw["checkpoint"]["worktree"] = {**self.place, "path": str(other), "repo_key": repository_context(other)["repo_key"]}
        raw["checkpoint"]["native_refs"] = [{"runId": "run", "runtime": "runtime"}]
        raw["admissions"] = {"a1": admission_row(self.worker_path, self.place["repo_key"], **fields)}
        if placement is not None:
            raw["admissions"]["a1"]["recovery"]["placement_binding"] = placement
        if phase == "retired":
            raw["historical_retirement"] = {"fixture": phase}
        destination = objective_root(other, name) / "context.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(raw))
        return destination

    def test_a_foreign_objectives_exited_worker_keeps_its_placement_here_held(self):
        self.foreign()
        self.assert_held(self.native_worker(verdict="exited"), objective="foreign")

    def test_a_foreign_objectives_released_worker_keeps_its_placement_here_held(self):
        self.foreign()
        self.assert_held(self.native_worker(verdict="exited", released=True, terminal_state="released"),
                         objective="foreign")

    def test_a_foreign_finished_admission_holds_nothing(self):
        self.foreign(report={"outcome": "succeeded"}, disposition={"kind": "accepted"})
        self.assert_free(self.native_worker(verdict="exited"))

    def test_a_retired_foreign_objective_holds_nothing(self):
        self.foreign(phase="retired")
        self.assert_free(self.native_worker(verdict="exited"))

    # The review of 91dd204 and the systematic foreign-path cases (A879).

    def removed(self):
        git(self.project, "worktree", "remove", "--force", str(self.worker_path))
        self.query_scope = {"branch": BRANCH}

    def branch_held(self, worker, records=("foreign",)):
        single, answer = self.answers(worker)
        self.assertTrue(answer["native_fence"]["stable"])
        self.assertEqual(answer["responses"][0]["result"], single)
        self.assertTrue(single["complete"], single["unknowns"])
        held = self.holds(single)
        self.assertEqual(sorted((row["objective"], row["branch"]) for row in held),
                         sorted((record, BRANCH) for record in records))
        for row in held:
            self.assertEqual(set(row), self.HELD_KEYS)
        return single

    def test_a_foreign_objectives_removed_worktree_keeps_its_branch_held(self):
        self.foreign()
        self.removed()
        self.branch_held(self.native_worker(verdict="exited"))

    def test_rule_s_keeps_a_foreign_objectives_removed_worktree_branch_held(self):
        self.foreign()
        self.removed()
        with patch("pod.admission._run_coordinators", side_effect=lambda runtime, check=None: {"run": None}):
            single = self.branch_held(self.native_worker(verdict="unverifiable"))
        self.assertIn("native_liveness_unverified", single["unrelated_unknowns"]["by_code"])  # rule S applied

    def test_a_foreign_worker_without_a_native_location_is_read_from_its_owner(self):
        self.foreign()
        worker = self.native_worker(verdict="exited")
        worker["resource"]["worktreeId"] = None
        worker["projection"]["workspace"] = {}
        self.branch_held(worker)

    def test_a_foreign_branch_only_placement_keeps_its_branch_held(self):
        self.foreign(placement={"branch": BRANCH, "repo_key": self.place["repo_key"]})
        self.removed()
        self.branch_held(self.native_worker(verdict="exited"))

    def test_a_consumed_local_binding_never_discharges_an_owing_foreign_admission(self):
        self.foreign()
        self.admit(report={"outcome": "succeeded"}, disposition={"kind": "accepted"})
        self.assert_held(self.native_worker(verdict="exited"), objective="foreign")

    def test_every_owing_admission_of_one_dispatch_holds_across_records(self):
        self.foreign()
        self.foreign(name="second")
        self.admit()
        self.removed()
        self.branch_held(self.native_worker(verdict="exited"), records=("foreign", "objective", "second"))

    def test_only_the_non_retired_owner_of_a_shared_dispatch_holds(self):
        self.foreign()
        self.foreign(phase="retired", name="second")
        self.removed()
        self.branch_held(self.native_worker(verdict="exited"))

    def test_a_placement_attributed_to_another_repository_holds_nothing_here(self):
        self.foreign(placement={"path": str(self.worker_path), "branch": BRANCH, "repo_key": "f" * 64})
        self.removed()
        self.assert_free(self.native_worker(verdict="exited"))

    def test_an_unreadable_owning_admission_keeps_its_dispatch_unknown(self):
        self.foreign(placement={"path": str(self.worker_path), "branch": 5})
        single, answer = self.answers(self.native_worker(verdict="exited"))
        self.assertEqual(answer["responses"][0]["result"], single)
        self.assertFalse(single["complete"])
        self.assertIn(("worker_placement_unknown", "d1"),
                      [(row["code"], row.get("dispatch")) for row in single["unknowns"]])

    def committed_repository(self, name):
        """A second repository with one commit, so it can register linked worktrees."""
        other = self.root / name
        other.mkdir(exist_ok=True)
        git(other, "init", "-q", "-b", "main")
        git(other, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q",
            "--allow-empty", "-m", "base")
        return other

    def test_a_placement_recorded_in_another_repositorys_worktree_holds_nothing(self):
        """Its recorded repository key decides, even with no key: a location proved another repository's
        registered worktree is not this repository's."""
        other = self.committed_repository("theirs-repository")
        theirs = self.root / "theirs"
        git(other, "worktree", "add", "-q", "-b", BRANCH, str(theirs))
        for key in (repository_context(other)["repo_key"], None):
            with self.subTest(repo_key=key):
                placement = {"path": str(theirs), "branch": BRANCH}
                if key is not None:
                    placement["repo_key"] = key
                path = self.foreign(placement=placement)
                raw = logical_json(path)
                raw["admissions"]["a1"]["native_binding"]["worktreeId"] = raw["admissions"]["a1"]["worktree"] = str(theirs)
                path.write_text(json.dumps(raw))
                worker = self.native_worker(verdict="exited")
                worker["resource"]["worktreeId"] = worker["projection"]["workspace"]["id"] = str(theirs)
                self.assert_free(worker)

    def test_a_reused_path_never_frees_the_owed_branch_recorded_here(self):
        """Another repository registered a worktree where the worker's was; the record still names this
        repository's branch, so it stays held (review of 4885817, P1)."""
        self.foreign()
        other = self.committed_repository("reuser-repository")
        git(self.project, "worktree", "remove", "--force", str(self.worker_path))
        git(other, "worktree", "add", "-q", "-b", "theirs", str(self.worker_path))
        git(self.project, "show-ref", "--verify", "refs/heads/" + BRANCH)  # the owed branch still exists here
        self.query_scope = {"branch": BRANCH}
        self.branch_held(self.native_worker(verdict="exited"))

    def test_a_nested_foreign_worktree_never_holds_this_repositorys_equal_branch(self):
        """A foreign linked worktree nested under this repository's root, on a branch with this repository's
        branch's name: its recorded repository key wins over the path (review of 4885817, P2)."""
        other = self.committed_repository("nested-owner-repository")
        nested = self.project / "nested-foreign"
        git(other, "worktree", "add", "-q", "-b", BRANCH, str(nested))
        path = self.foreign(placement={"path": str(nested), "branch": BRANCH,
                                       "repo_key": repository_context(other)["repo_key"]})
        raw = logical_json(path)
        raw["admissions"]["a1"]["native_binding"]["worktreeId"] = raw["admissions"]["a1"]["worktree"] = str(nested)
        path.write_text(json.dumps(raw))
        worker = self.native_worker(verdict="exited")
        worker["resource"]["worktreeId"] = worker["projection"]["workspace"]["id"] = str(nested)
        self.query_scope = {"branch": BRANCH}
        self.assert_free(worker)

    def test_an_earlier_admission_without_a_role_holds_as_an_assignment(self):
        """Earlier records may carry an admission with no role; its hold is an assignment, never a fault."""
        row = admission_row(self.worker_path, self.place["repo_key"])
        del row["role"]
        held, repo_key = admission._owed_hold("foreign", "record", "a1", row)
        self.assertEqual((held["kind"], set(held), repo_key), ("assignment", self.HELD_KEYS, self.place["repo_key"]))

    def test_an_owner_judgment_fault_is_that_dispatchs_unknown_never_a_failed_query(self):
        self.foreign()
        with patch("pod.admission._owed_hold", side_effect=KeyError("role")):
            single, answer = self.answers(self.native_worker(verdict="exited"))
        self.assertEqual(answer["responses"][0]["result"], single)
        self.assertFalse(single["complete"])
        self.assertIn(("worker_placement_unknown", "d1"),
                      [(row["code"], row.get("dispatch")) for row in single["unknowns"]])

    def test_this_repositorys_admission_placed_in_another_repository_holds_nothing_here(self):
        self.admit(recovery={"placement_binding": {"path": str(self.worker_path), "branch": BRANCH,
                                                   "repo_key": "f" * 64}})
        self.assert_free(self.native_worker(verdict="exited"))


class PlacementRecheckTests(UnconsumedReportCase):
    """A878: an accepted placement changed between the claims read and the change recheck is a record change."""

    def changed_before_recheck(self, change):
        path = self.admit()
        raw = logical_json(path)
        change(raw["admissions"]["a1"]["recovery"]["placement_binding"])
        later, real, written = json.dumps(raw).encode(), admission._after_context_read, []

        def recheck(target):
            if target == path and not written:
                path.write_bytes(later)  # the recheck meets the changed placement
                written.append(True)
            return real(target)
        self.serve(self.native_worker(verdict="exited"))
        with patch("pod.admission._after_context_read", side_effect=recheck):
            single = admission.query(self.project, **self.query_scope)
        restore = self.admit()
        written.clear()
        with patch("pod.admission._after_context_read", side_effect=recheck):
            answer = self.batch(dict(self.query_scope))
        self.assertEqual(restore, path)
        for result in (single, answer["responses"][0]["result"]):
            self.assertFalse(result["complete"])
            self.assertIn("record_changed", [row["code"] for row in result["unknowns"]])

    def test_a_changed_placement_branch_is_record_changed(self):
        self.query_scope = {"branch": "new-branch"}
        self.changed_before_recheck(lambda placement: placement.update(branch="new-branch"))

    def test_a_placement_branch_becoming_unrecorded_is_record_changed(self):
        self.query_scope = {"branch": "other-branch"}
        self.changed_before_recheck(lambda placement: placement.pop("branch"))

    def test_a_changed_placement_worktree_is_record_changed(self):
        self.changed_before_recheck(lambda placement: placement.update(path=str(self.root / "elsewhere")))


class HostExitHoldTests(HostExitCase):
    """A871: a Dispatch R212 counts exited on the host's verdict leaves its owed report's placement held."""

    def test_a_host_counted_exit_keeps_the_placement_held(self):
        worker = self.root / "worker"
        git(self.project, "worktree", "add", "-q", "-b", BRANCH, str(worker))
        self.intake(worktree=self.place)
        path = objective_root(self.project, "objective") / "context.json"
        raw = logical_json(path)
        raw["admissions"]["a1"] = admission_row(worker, self.place["repo_key"], terminal="d1-terminal")
        path.write_text(json.dumps(raw))
        self.coordinators = {"run": "lead-terminal"}
        lead = {"handle": "lead-terminal", "worktreePath": str(self.root), "connected": True, "writable": True,
                "orphaned": False}
        row = stuck(self, "d1", path=worker, run="run")
        with patch.object(process_exit, "_output", side_effect=FakeVerifier()):
            result = self.read([row], [lead], worktree=str(worker), branch=BRANCH)
        self.assertEqual([entry["counted"] for entry in result["host_exits"]], [True], result["host_exits"])
        self.assertTrue(result["complete"], result["unknowns"])
        held = [claim for claim in result["claims"] if claim.get("admission") == "a1"]
        self.assertEqual([(claim["kind"], claim["worktree"], claim["branch"], claim["liveness"]) for claim in held],
                         [("writer", str(worker), BRANCH, "exited")])
        self.assertEqual(set(held[0]), UnconsumedReportCase.HELD_KEYS)


class CountingCase(VerifierMixin, UnconsumedReportCase):
    """pod#237: an owed report holds its recorded placement whether or not its Dispatch is counted exited. Stored
    records are read through the production facts reader; only the native inventories and the verifier's transport
    are doubled. Each read runs with no verifier configured, with a verifier that does not count the settled,
    unobservable Dispatch (it answers `verified: false`, as a host verifier sometimes does) and with one that counts
    it exited."""

    MODES = ("no verifier", "uncounted", "counted")
    foreign = ForeignObjectiveTests.foreign

    def setUp(self):
        super().setUp()
        self.setup_verifier()
        self.fake = FakeVerifier()
        # No Run coordinator is known, so rule S never settles the Dispatch: only R212 can count it exited.
        for target, replacement in (("pod.admission._run_coordinators", lambda runtime, check=None: {}),
                                    ("pod.process_exit._output", self.fake)):
            double = patch(target, side_effect=replacement)
            double.start(); self.addCleanup(double.stop)

    def on_its_branch(self):
        """This repository's admission a1 owes its report; its worktree is still on the recorded branch."""
        self.admit(terminal="d1-terminal")

    def detached(self):
        """Shape 1: the same, with the worktree detached at the same commit after admission."""
        self.admit(terminal="d1-terminal")
        git(self.worker_path, "checkout", "--detach", "-q")

    def on_another_branch(self):
        """The same, with the worktree switched to another branch after admission."""
        self.admit(terminal="d1-terminal")
        git(self.worker_path, "checkout", "-q", "-b", "moved")

    def foreign_in_place(self):
        """Another repository's objective owns Run `run` and owes the report of its worker placed in this
        repository's worktree; this repository's records do not name `run`."""
        self.foreign(terminal="d1-terminal")
        path, raw = self.raw()
        raw["checkpoint"]["native_refs"] = []
        self.save(path, raw)

    def foreign_removed(self):
        """Shape 2: the same, with that worktree removed."""
        self.foreign_in_place()
        git(self.project, "worktree", "remove", "--force", str(self.worker_path))

    def read(self, mode, queries, *, verdict="unverifiable", state=("succeeded", "completed"), singles=True):
        """Every query under `mode` in one batch with a real end fence and, with `singles`, alone too; the batch
        answers as the single queries do. The settled Dispatch d1's agent terminal is gone."""
        self.configure(None if mode == "no verifier" else self.command)
        self.fake.mode = "unverified" if mode == "uncounted" else None
        self.serve(stuck(self, "d1", path=self.worker_path, run="run", handle="d1-terminal", verdict=verdict,
                         state=state))
        answer = self.batch(*queries)
        self.assertTrue(answer["native_fence"]["stable"])
        results = [row["result"] for row in answer["responses"]]
        if singles:
            self.assertEqual(results, [admission.query(self.project, **query) for query in queries])
        return results

    @staticmethod
    def native_claims(result):
        return [row for row in result["claims"] if row.get("dispatch") == "d1"]

    @staticmethod
    def facts(result):
        """What a result holds, whatever the host disclosed about its candidates."""
        return {key: result[key] for key in ("status", "complete", "claims", "conflicts", "unknowns")}


class CountingTests(CountingCase):
    """A902 and A903: each shape the pod#227 review found reads `blocked` on the recorded branch, with the same owed
    hold, with no verifier, an uncounted verifier and a counted one; counted, it holds as before (A871). One batch
    also asks the unfiltered query, whose own result decides how the Dispatch is represented."""

    HOST_EXITS = {"no verifier": None, "uncounted": [("d1", False, process_exit.UNVERIFIED)],
                  "counted": [("d1", True, None)]}

    def assert_held_in_every_mode(self, objective):
        branch_results, unfiltered = {}, {}
        for mode in self.MODES:
            with self.subTest(mode):
                branch_results[mode], unfiltered[mode] = self.read(mode, [{"branch": BRANCH}, {}])
                result = branch_results[mode]
                self.assertEqual((result["status"], result["complete"], result["unknowns"]), ("blocked", True, []))
                held = self.holds(result)
                self.assertEqual(len(held), 1, result["claims"])
                self.assertEqual(set(held[0]), self.HELD_KEYS)
                self.assertEqual({key: held[0][key] for key in self.HELD_KEYS - {"record"}},
                                 {"objective": objective, "kind": "writer", "admission": "a1",
                                  "worktree": str(self.worker_path), "branch": BRANCH, "locations_complete": True,
                                  "liveness": "exited", "boundary": None})
                self.assertEqual((self.native_claims(result), [row["dimensions"] for row in result["conflicts"]]),
                                 ([], [["branch"]]))
                host_exits = result.get("host_exits")
                self.assertEqual(host_exits and [(row["dispatch"], row["counted"], row["code"]) for row in host_exits],
                                 self.HOST_EXITS[mode])
                # The unfiltered query shows its native writer or the hold, never both.
                self.assertEqual(len(self.native_claims(unfiltered[mode])) + len(self.holds(unfiltered[mode])), 1)
        self.assertEqual(self.facts(branch_results["no verifier"]), self.facts(branch_results["counted"]))
        self.assertEqual(self.facts(branch_results["uncounted"]), self.facts(branch_results["counted"]))
        return unfiltered

    def test_a_worktree_detached_after_admission_holds_its_recorded_branch_in_every_mode(self):
        """A902. The native writer has a complete location without a branch, so the branch query's relevance leaves it
        out, and with it its process-liveness unknown; it no longer hides the hold. The unfiltered result keeps that
        native writer while nothing counts it, and shows the hold instead once it is counted exited."""
        self.detached()
        unfiltered = self.assert_held_in_every_mode("objective")
        self.assertEqual([(len(self.native_claims(unfiltered[mode])), len(self.holds(unfiltered[mode])))
                          for mode in self.MODES], [(1, 0), (1, 0), (0, 1)])

    def test_a_foreign_owners_removed_worktree_holds_its_recorded_branch_in_every_mode(self):
        """A903. The row is outside this repository, on a Run its records do not name, so it gets no claim; its
        owners are still read, as when it is counted exited, and every query holds the owed placement."""
        self.foreign_removed()
        unfiltered = self.assert_held_in_every_mode("foreign")
        self.assertEqual([(len(self.native_claims(unfiltered[mode])), len(self.holds(unfiltered[mode])))
                          for mode in self.MODES], [(0, 1)] * 3)

    def test_a_live_row_outside_the_repository_reads_as_before(self):
        """A903. A row Orca shows live is a writer where Orca shows it, outside this repository and on another
        repository's Run; it is no host-exit candidate and holds nothing here, as before."""
        self.foreign_removed()
        for mode in self.MODES:
            with self.subTest(mode):
                result, = self.read(mode, [{"branch": BRANCH}], verdict="live", state=("running", "running"),
                                    singles=False)
                self.assertEqual((result["status"], result["complete"], result["claims"], result["unknowns"],
                                  result.get("host_exits")), ("observed", True, [], [], None))


class RepresentationTests(CountingCase):
    """A904: one representation per query. Over each placement shape, with every verifier mode, a native writer claim
    in a query's own result stands for the Dispatch and the owed hold is not repeated; one that relevance leaves out,
    or a row with no claim, leaves the hold to stand. The unfiltered query shows exactly one of the two, a query
    naming the recorded worktree or branch is never read free, and a query of another branch counts the Dispatch
    once among its unrelated claims, as the unfiltered query shows it once."""

    def represented(self, shape):
        shape()
        for mode in self.MODES:
            with self.subTest(mode):
                queries = [{}, {"branch": BRANCH}, {"worktree": str(self.worker_path)}, {"branch": "other"}]
                unfiltered, *named, other = self.read(mode, queries, singles=False)
                for result in (unfiltered, *named, other):
                    self.assertFalse(self.native_claims(result) and self.holds(result), result["claims"])
                self.assertEqual(len(self.native_claims(unfiltered)) + len(self.holds(unfiltered)), 1,
                                 unfiltered["claims"])
                for result in named:
                    self.assertNotEqual(result["status"], "observed", result["scope"]["selection"])
                # Another branch's query leaves every claim out, the Dispatch's once, as the unfiltered query shows it.
                self.assertEqual((other["claims"], other["unrelated_claims"]), ([], len(unfiltered["claims"])))

    def test_a_worktree_on_its_recorded_branch(self):
        self.represented(self.on_its_branch)

    def test_a_worktree_detached_after_admission(self):
        self.represented(self.detached)

    def test_a_worktree_moved_to_another_branch(self):
        self.represented(self.on_another_branch)

    def test_a_foreign_owners_worktree_in_place(self):
        self.represented(self.foreign_in_place)

    def test_a_foreign_owners_removed_worktree(self):
        self.represented(self.foreign_removed)


class ListTests(UnconsumedReportCase):
    """A875: `pod status --list` counts the admissions that still owe their report, from the record alone."""

    def listed(self):
        with patch("pod.orca.read_command", side_effect=AssertionError("the list reads no Orca state")), \
                patch.dict("os.environ"):
            import os
            os.environ.pop("ORCA_TERMINAL_HANDLE", None)
            return {row["objective"]: row for row in listing.objective_list(self.project)["objectives"]}

    def test_an_open_objective_counts_owed_reports(self):
        self.assertEqual(self.listed()["objective"]["unfinished_admissions"], 0)
        self.admit()
        self.assertEqual(self.listed()["objective"]["unfinished_admissions"], 1)
        self.admit(report={"outcome": "succeeded"}, disposition={"kind": "accepted"})
        self.assertEqual(self.listed()["objective"]["unfinished_admissions"], 0)

    def terminal_row(self, phase):
        self.phase(phase)
        with patch("pod.succession.links", return_value={}), \
                patch("pod.retirement.status_block", return_value={}), \
                patch("pod.readiness.label", return_value="fixture"):
            row = self.listed()["old"]
        self.assertEqual((row["phase"], row["unfinished_admissions"]), (phase, 1))

    def test_a_closed_objective_counts_owed_reports(self):
        self.terminal_row("closed")

    def test_a_continued_objective_counts_owed_reports(self):
        self.terminal_row("continued")

    def test_a_retired_objective_counts_owed_reports(self):
        self.terminal_row("retired")

    def test_an_uncountable_record_is_marked_never_zero(self):
        path = self.admit()
        raw = logical_json(path)
        raw["admissions"]["broken"] = "not an admission"
        path.write_text(json.dumps(raw))
        row = self.listed()["objective"]
        self.assertNotIn("unfinished_admissions", row)
        self.assertEqual(row["unfinished_admissions_unavailable"], "admission_unreadable")
