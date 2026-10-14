"""R113: historical retirement of recorded worktrees proven absent, and what cleanup holds afterwards."""
from copy import deepcopy
import json
import os
import unittest

from pod.errors import PodError
from pod.ledger import objective_root, read
from pod.operations import guarded_start
from pod.retirement import status_block, validate_entry
from tests.common import GroupedTable, assert_grouped_table
from tests.kernel_support import git
from tests import test_historical_retirement as earlier  # Its test classes would be discovered twice if imported.
from tests.test_historical_retirement import RetirementCase
from tests.test_owner_handoff import SENTINEL
from tests.test_succession import _raw


class AbsentCase(RetirementCase):
    """Delivered history whose two workers ran in their own task worktrees: every location is recorded."""

    def placed(self):
        self.establish(admissions=False)
        self.gone, self.kept = str(self.root / "task-gone"), str(self.root / "task-kept")
        git(self.project, "worktree", "add", "-q", "-b", "task-gone", self.gone)
        git(self.project, "worktree", "add", "-q", "-b", "task-kept", self.kept)
        self.admissions = []
        for task, serves, area, path in (("task-1", "S1", "area1", self.gone), ("task-2", "S2", "area2", self.kept)):
            frozen = self.packet([serves], boundary={"paths": [area]})
            admission = guarded_start(self.project, "objective", owner="owner", run="run", task=task,
                                      plan_revision="plan", frozen_packet=frozen, worktree="path:" + path,
                                      port=self.port)["admission"]
            self.admissions.append(admission)
        self.op("checkpoint", value=self.core(obligations=self.stored(), worktree=self.binding()))
        for admission in self.admissions:
            self.settle(admission)
        self.lose()
        git(self.project, "worktree", "remove", self.gone)  # Removed through Git: the directory and registration go.

    def request(self, *paths):
        return [{"objective": "objective", "release": list(paths)}]

    def located(self, found):
        return {row["path"]: row for row in found["objectives"][0]["locations"]}


class DetectionTests(AbsentCase):
    def test_a_mixed_release_names_each_disposition_and_writes_nothing(self):
        self.placed()
        self.instances[self.kept] = "wt2:local:kept"
        self.inventory["terminals"] = [{"handle": "old", "connected": False, "writable": False, "orphaned": False,
                                        "exitCause": {"kind": "exited"}, "worktreePath": self.gone + "/sub"}]
        before, mutations, starts = self.snapshot(), list(self.mutations), deepcopy(self.port.starts)
        found = self.retire(objectives=self.request(self.gone, self.kept))
        verdict = found["objectives"][0]
        self.assertEqual((found["status"], verdict["status"]), ("retirement_available", "eligible"), verdict.get("message"))
        facts = verdict["facts"]
        self.assertEqual(facts["locations"], [{"path": self.gone, "disposition": "absent"},
                                              {"path": self.kept, "disposition": "existing", "worktree": "wt2:local:kept"}])
        self.assertEqual((facts["retained"], facts["unrecorded_locations"]), ([self.where()], "none"))
        evidence = self.located(found)[self.gone]["evidence"]
        self.assertEqual(evidence, {"filesystem": "absent", "git": "unregistered", "orca": "unlisted",
                                    "hosts": ["local"], "exited_terminals": 1})
        self.assertEqual(verdict["native"]["exited_terminals"], 1)
        question = found["question"]
        for words in (self.gone + " (absent)", self.kept + " (existing worktree)",
                      "keeps holding its recorded worktree(s) it does not release: " + self.where(),
                      "its missing directory does not prove that work stopped"):
            self.assertIn(words, question)
        self.assertNotIn("every task worktree of this repository", question)
        self.assertIn("locations, retained, unrecorded_locations", found["next_action"])
        self.assertEqual((before, self.mutations, self.port.starts), (self.snapshot(), mutations, starts))
        self.assertNotIn(SENTINEL, json.dumps(found))

    def test_an_incomplete_record_names_the_unrecorded_hold_retirement_ends(self):
        self.delivered()  # Its worker ran in "current": the record never names that location.
        found = self.retire()
        facts = found["objectives"][0]["facts"]
        self.assertEqual((facts["unrecorded_locations"], facts["retained"]), ("hold_ends", []))
        self.assertIn("record does not list every location its work used, so while it is open cleanup holds every "
                      "task worktree of this repository for it; retirement ends that hold", found["question"])
        decision = self.decide()
        self.assertEqual(decision["objectives"][0]["unrecorded_locations"], "hold_ends")
        self.assertEqual(self.retire(decision)["status"], "retired")
        block = self.status()["retirement"]
        self.assertEqual((block["unrecorded_locations"], block["retained"]), ("hold_ends", []))

    def test_the_inventory_reads_are_exact(self):
        from pod.orca import _read_allowed
        for argv in (["worktree", "list", "--json"], ["terminal", "list", "--json"]):
            self.assertTrue(_read_allowed(argv))
        for argv in (["worktree", "list"], ["worktree", "list", "--json", "--limit", "1"],
                     ["worktree", "list", "--repo", "path:/x", "--json"], ["terminal", "list"],
                     ["terminal", "list", "--json", "--include-visual-layouts"], ["worktree", "rm", "--json"]):
            with self.subTest(argv=argv):
                self.assertFalse(_read_allowed(argv))


def _stale_registration(case):
    git(case.project, "worktree", "add", "-q", "-b", "again", case.gone)
    os.rename(case.gone, case.gone + "-moved")  # Its directory gone, Git keeps a prunable registration.


def _orca_lists(case):
    case.inventory["worktrees"] = [{"path": case.gone, "git": {"path": case.gone}}]


def _orca_lists_beneath(case):
    case.inventory["worktrees"] = [{"path": "/elsewhere", "git": {"path": case.gone + "/nested"}}]


def _live_terminal_beneath(case):
    case.inventory["terminals"] = [{"handle": "user", "connected": True, "writable": True, "orphaned": False,
                                    "worktreePath": case.gone + "/nested"}]


def _shape(kind, shape):
    return lambda case: case.inventory_shape.update({kind: shape})


def _linked(case):
    git(case.project, "worktree", "remove", case.kept)
    (case.root / "elsewhere").mkdir()
    os.symlink(case.root / "elsewhere", case.kept)  # The recorded path is now a link: where it leads is unproven.


def _plain_directory(case):
    os.mkdir(case.gone)  # Something else now stands where the worktree was.


def _another_repository(case):
    os.mkdir(case.gone)
    git(case.root, "init", "-q", case.gone)


def _recorded_entry(case, entry):
    """The record names the kept worktree as Orca's `entry::path`, as a real start records it."""
    path, raw = _raw(case, "objective")
    for row in raw["admissions"].values():
        binding = row.get("native_binding") or {}
        if binding.get("worktreeId") == "path:" + case.kept:
            binding["worktreeId"] = entry + "::" + case.kept
    path.write_text(json.dumps(raw))
    case.port.workers[case.admissions[1]["native_binding"]["dispatchId"]]["worktree"] = entry + "::" + case.kept


def _reused_entry(case):
    _recorded_entry(case, "repo-recorded")  # Orca now registers the path under its entry "repo".


# kind: (arrange after the owner decided, released path, expected condition, expected disposition)
PATH_REFUSALS = {
    "stale-registration": (_stale_registration, "gone", "path_unverified", "unknown"),
    "orca-lists-it": (_orca_lists, "gone", "path_unverified", "unknown"),
    "orca-lists-beneath": (_orca_lists_beneath, "gone", "path_unverified", "unknown"),
    "live-terminal-beneath": (_live_terminal_beneath, "gone", "path_in_use", "absent"),
    "worktrees-truncated": (_shape("worktrees", {"truncated": True}), "gone", "path_unverified", "unknown"),
    "terminals-host-omitted": (_shape("terminals", {"hostScope": {"hostIds": ["local"], "omittedHostIds": ["ssh"]}}),
                               "gone", "path_unverified", "unknown"),
    "terminals-miscounted": (_shape("terminals", {"totalCount": 7}), "gone", "path_unverified", "unknown"),
    "worktrees-host-unnamed": (_shape("worktrees", {"hostScope": {"hostIds": ["ssh"], "omittedHostIds": []}}),
                               "gone", "path_unverified", "unknown"),
    "worktrees-unreadable": (_shape("worktrees", "unreadable"), "gone", "path_unverified", "unknown"),
    "linked-path": (_linked, "kept", "path_unverified", "unknown"),
    "plain-directory": (_plain_directory, "gone", "path_foreign", "foreign"),
    "another-repository": (_another_repository, "gone", "path_foreign", "foreign"),
    "reused-entry": (_reused_entry, "kept", "path_foreign", "foreign"),
}


class PathRefusalTableTests(GroupedTable):
    """A released path whose absence or identity is unproven refuses detection and the earlier decision."""
    # One row per method: each builds its own delivered history, so a method stays well under the fast limit.
    GROUPS = {
        "test_a_stale_git_registration_refuses": ["stale-registration"],
        "test_an_orca_listed_path_refuses": ["orca-lists-it"],
        "test_an_orca_listed_descendant_refuses": ["orca-lists-beneath"],
        "test_a_live_terminal_beneath_refuses": ["live-terminal-beneath"],
        "test_a_truncated_worktree_inventory_refuses": ["worktrees-truncated"],
        "test_a_worktree_inventory_without_this_host_refuses": ["worktrees-host-unnamed"],
        "test_an_unreadable_worktree_inventory_refuses": ["worktrees-unreadable"],
        "test_a_terminal_inventory_omitting_a_host_refuses": ["terminals-host-omitted"],
        "test_a_miscounted_terminal_inventory_refuses": ["terminals-miscounted"],
        "test_a_linked_path_refuses": ["linked-path"],
        "test_a_plain_directory_refuses": ["plain-directory"],
        "test_another_repository_refuses": ["another-repository"],
        "test_a_reused_path_refuses": ["reused-entry"]}

    def row(self, kind):
        arrange, which, condition, disposition = PATH_REFUSALS[kind]
        case = AbsentCase(); case.setUp()
        try:
            with self.subTest(kind=kind):
                case.placed()
                case.instances[case.kept] = "wt2:local:kept"
                release = case.request(case.gone, case.kept)
                decision = case.decide(objectives=release)
                arrange(case)
                path = getattr(case, which)
                before, starts, mutations = case.snapshot(), len(case.port.starts), len(case.mutations)
                found = case.retire(objectives=release)
                verdict = found["objectives"][0]
                self.assertEqual((found["status"], verdict["status"], verdict["condition"]),
                                 ("retirement_unavailable", "refused", condition), verdict.get("message"))
                row = case.located(found)[path]
                self.assertEqual((row["disposition"], row["condition"]), (disposition, condition), row)
                self.assertIn(path, verdict["message"])
                applied = case.retire(decision, objectives=release)
                self.assertEqual((applied["status"], applied["objectives"][0]["condition"]), ("refused", condition))
                self.assertEqual(before, case.snapshot())
                self.assertEqual((len(case.mutations), len(case.port.starts)), (mutations, starts))
                self.assertNotIn(SENTINEL, json.dumps(found))
        finally:
            case.doCleanups()

    def test_a_stale_git_registration_refuses(self):
        self.rows()

    def test_an_orca_listed_path_refuses(self):
        self.rows()

    def test_an_orca_listed_descendant_refuses(self):
        self.rows()

    def test_a_live_terminal_beneath_refuses(self):
        self.rows()

    def test_a_truncated_worktree_inventory_refuses(self):
        self.rows()

    def test_a_worktree_inventory_without_this_host_refuses(self):
        self.rows()

    def test_an_unreadable_worktree_inventory_refuses(self):
        self.rows()

    def test_a_terminal_inventory_omitting_a_host_refuses(self):
        self.rows()

    def test_a_miscounted_terminal_inventory_refuses(self):
        self.rows()

    def test_a_linked_path_refuses(self):
        self.rows()

    def test_a_plain_directory_refuses(self):
        self.rows()

    def test_another_repository_refuses(self):
        self.rows()

    def test_a_reused_path_refuses(self):
        self.rows()


class PathRefusalTableShapeTests(unittest.TestCase):
    def test_the_path_refusal_table_runs_every_row_once(self):
        assert_grouped_table(self, PathRefusalTableTests, list(PATH_REFUSALS), limit=1)


class UnprovenFilesystemTests(AbsentCase):
    def coordinator_at(self, location):
        """The coordinator's recorded location, as an earlier checkpoint named it."""
        path, raw = _raw(self, "objective")
        raw["checkpoint"]["worktree"]["path"] = location
        path.write_text(json.dumps(raw))

    def test_an_unreadable_parent_or_a_linked_ancestor_keeps_absence_unproven(self):
        self.placed()
        locked, target = self.root / "locked", self.root / "target"
        locked.mkdir(); target.mkdir()
        os.symlink(target, self.root / "link")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o700)
        for location, shown in ((str(locked / "task"), "inaccessible"), (str(self.root / "link" / "task"), "linked")):
            with self.subTest(shown=shown):
                self.coordinator_at(location)
                found = self.retire(objectives=self.request(self.gone, location))
                row = self.located(found)[location]
                self.assertEqual((row["disposition"], row["condition"], row["evidence"]),
                                 ("unknown", "path_unverified", {"filesystem": shown}))
                self.assertEqual(self.located(found)[self.gone]["disposition"], "absent")
                self.assertNotIn("question", found)

    def test_a_matching_recorded_repository_entry_passes(self):
        self.placed()
        _recorded_entry(self, "repo")
        verdict = self.retire(objectives=self.request(self.kept))["objectives"][0]
        self.assertEqual((verdict["status"], verdict["facts"]["locations"][0]["disposition"]),
                         ("eligible", "existing"), verdict.get("message"))


class DecisionTests(AbsentCase):
    def decided(self):
        self.placed()
        self.instances[self.kept] = "wt2:local:kept"
        self.release = self.request(self.gone, self.kept)
        return self.decide(objectives=self.release)

    def refused_for(self, decision, changed):
        before = self.snapshot()
        verdict = self.retire(decision, objectives=self.release)["objectives"][0]
        self.assertEqual((verdict["status"], verdict["condition"], verdict["changed"]),
                         ("refused", "retirement_decision_mismatch", changed))
        self.assertEqual(before, self.snapshot())

    def test_a_reappearing_or_recreated_path_refuses_and_stable_facts_retire(self):
        decision = self.decided()
        git(self.project, "worktree", "add", "-q", "-b", "reappeared", self.gone)
        self.instances[self.gone] = "wt2:local:new"
        self.refused_for(decision, ["locations"])  # Absent when the owner decided, a worktree again now.
        git(self.project, "worktree", "remove", self.gone)
        self.instances[self.kept] = "wt2:local:recreated"
        self.refused_for(decision, ["locations"])  # Another worktree instance now stands at the kept path.
        self.instances[self.kept] = "wt2:local:kept"
        result = self.retire(decision, objectives=self.release)
        self.assertEqual(result["status"], "retired")
        entry = read(self.project, "objective")["historical_retirement"]
        self.assertEqual(entry["locations"], decision["objectives"][0]["locations"])
        replayed = self.retire(decision, objectives=self.release)
        self.assertEqual(replayed["objectives"][0]["status"], "replayed")

    def test_a_decision_without_dispositions_or_claiming_more_refuses(self):
        decision = self.decided()
        block = decision["objectives"][0]
        earlier = {key: block[key] for key in ("objective", "owner", "runs", "release", "records", "delivery")}
        self.refused_for({**decision, "objectives": [earlier]}, ["malformed"])  # A Pod 0.11.0 decision.
        for changes, changed in (
                ({"locations": [{"path": self.gone, "disposition": "existing", "worktree": "wt2:local:x"},
                                block["locations"][1]]}, ["locations"]),
                ({"retained": []}, ["retained"]),
                ({"unrecorded_locations": "hold_ends"}, ["unrecorded_locations"])):
            with self.subTest(changes=changes):
                self.refused_for({**decision, "objectives": [{**block, **changes}]}, changed)
        for changes in ({"locations": [{"path": self.gone, "disposition": "unknown"}, block["locations"][1]]},
                        {"retained": [self.where(), self.gone]}, {"unrecorded_locations": "ended"}):
            with self.subTest(malformed=changes):
                self.refused_for({**decision, "objectives": [{**block, **changes}]}, ["malformed"])
        with self.assertRaises(PodError) as caught:  # Naming another path broadens the release: the whole request.
            self.retire({**decision, "objectives": [{**block, "release": [self.gone, self.kept, self.where()]}]},
                        objectives=self.release)
        self.assertEqual(caught.exception.code, "retirement_decision_mismatch")

    def test_a_record_another_writer_widened_refuses(self):
        decision = self.decided()
        path, raw = _raw(self, "objective")
        raw["checkpoint"].pop("worktree")  # Now the record no longer lists every location: a broader hold.
        path.write_text(json.dumps(raw))
        self.refused_for(decision, ["retained", "unrecorded_locations"])

    def test_a_mixed_batch_reports_each_objective_and_later_retires_the_other(self):
        decision = self.decided()
        self.claim_only()
        both = self.release + [{"objective": "reconcile", "release": [self.where()]}]
        decision = self.decide(objectives=both)
        os.mkdir(self.gone)  # The first objective's path is no longer proven absent.
        result = self.retire(decision, objectives=both)
        rows = {row["objective"]: row for row in result["objectives"]}
        self.assertEqual((result["status"], rows["objective"]["condition"], rows["reconcile"]["status"]),
                         ("partially_retired", "path_foreign", "retired"))
        self.assertNotIn("historical_retirement", read(self.project, "objective"))
        os.rmdir(self.gone)
        later = {row["objective"]: row["status"] for row in self.retire(decision, objectives=both)["objectives"]}
        self.assertEqual(later, {"objective": "retired", "reconcile": "replayed"})


class PreservationTests(AbsentCase):
    def test_retiring_an_absent_path_keeps_history_and_reads_back_its_reservations(self):
        self.placed()
        self.instances[self.kept] = "wt2:local:kept"
        release = self.request(self.gone)
        decision = self.decide(objectives=release)
        state, journal, before = deepcopy(self.state()), self.journal(), self.snapshot()
        own = str((objective_root(self.project, "objective") / "context.json").relative_to(self.root / "state"))
        self.assertEqual(self.retire(decision, objectives=release)["status"], "retired")
        after = self.state()
        entry = after.pop("historical_retirement")
        state["revision"] += 1
        self.assertEqual(after, state)
        self.assertEqual(self.journal(), journal)
        self.assertEqual({key for key, value in self.snapshot().items() if before.get(key) != value} - {own}, set())
        self.assertEqual((entry["released"], entry["locations"]), ([self.gone], [{"path": self.gone, "disposition": "absent"}]))
        shown = self.status()
        block = shown["retirement"]
        self.assertEqual((shown["phase"], block["completed"], block["released"], block["retained"],
                          block["unrecorded_locations"]),
                         ("retired", False, [self.gone], sorted([self.where(), self.kept]), "none"))
        self.assertEqual(self.listed()["objective"]["retirement"], block)
        text = self.text_status()
        self.assertIn(f"Released {self.gone}: absent from this host, Git and Orca when the owner decided", text)
        self.assertIn(f"Still held: {self.kept}, a recorded worktree this retirement did not release", text)
        self.assertNotIn("retirement ended cleanup's hold", text)
        self.returning()
        self.refused(lambda: self.op("checkpoint", value=self.core()), code="objective_retired")

    def test_an_earlier_entry_without_dispositions_still_reads(self):
        self.delivered()
        self.retire(self.decide())
        path, raw = _raw(self, "objective")
        raw["historical_retirement"].pop("locations")  # As Pod 0.11.0 recorded it.
        path.write_text(json.dumps(raw))
        block = self.status()["retirement"]
        self.assertEqual((block["locations"], block["released"], block["retained"], block["unrecorded_locations"]),
                         (None, [self.where()], [], "hold_ends"))
        self.assertEqual(self.listed()["objective"]["retirement"], block)
        self.assertEqual(self.retire()["objectives"][0]["facts"]["locations"], None)


class LocatedEntryShapeTests(unittest.TestCase):
    ENTRY = {**earlier.RecordShapeTests.ENTRY, "released": ["/w/one", "/w/two"],
             "locations": [{"path": "/w/one", "disposition": "absent"},
                           {"path": "/w/two", "disposition": "existing", "worktree": "wt2:local:k"}]}

    def test_dispositions_are_bound_to_each_released_path(self):
        validate_entry(self.ENTRY)
        validate_entry({key: value for key, value in self.ENTRY.items() if key != "locations"})
        for locations in ([], [{"path": "/w/one", "disposition": "absent"}],
                          [{"path": "/w/two", "disposition": "absent"}, {"path": "/w/one", "disposition": "absent"}],
                          [{"path": "/w/one", "disposition": "unknown"}, self.ENTRY["locations"][1]],
                          [{"path": "/w/one", "disposition": "existing"}, self.ENTRY["locations"][1]],
                          [{"path": "/w/one", "disposition": "absent", "worktree": "k"}, self.ENTRY["locations"][1]]):
            with self.subTest(locations=locations), self.assertRaises(PodError) as caught:
                validate_entry({**self.ENTRY, "locations": locations})
            self.assertEqual(caught.exception.code, "state_unsupported")

    def test_the_block_derives_reservations_from_the_record(self):
        state = {"historical_retirement": self.ENTRY,
                 "checkpoint": {"worktree": {"path": "/w/coordinator"}},
                 "admissions": {"a": {"native_binding": {"worktreeId": "repo::/w/one"}},
                                "b": {"worktree": "path:/w/two"}}}
        block = status_block(state)
        self.assertEqual((block["retained"], block["unrecorded_locations"], block["locations"]),
                         (["/w/coordinator"], "none", self.ENTRY["locations"]))
        state["admissions"]["c"] = {"worktree": "current"}
        self.assertEqual(status_block(state)["unrecorded_locations"], "hold_ends")


if __name__ == "__main__":
    unittest.main()
