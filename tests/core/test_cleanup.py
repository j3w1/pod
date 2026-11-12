"""Cleanup after an objective ends: only what it created, only when Git and Orca show it is safe."""

import json
import os
from pathlib import Path

from pod import cli, commands, gate, proof
from pod.state import fold
from tests.core.test_gate import GateCase
from tests.support import ROOT


class CleanupCase(GateCase):
    def worker(self, files: dict, *, paths: list[str], integrate: bool = True) -> tuple[Path, str]:
        """An implementer in a worktree Orca made for it. Returns the worktree and its assignment."""
        started = commands.dispatch(self.repo, None, role="implement", title="write", spec="Do.", paths_=paths,
                                    worktree="new-child")
        place = Path(self.orca()["workers"][started["dispatch"]]["resource"]["worktreeId"].partition("::")[2])
        for name, text in files.items():
            (place / name).parent.mkdir(parents=True, exist_ok=True)
            (place / name).write_text(text)
        self.git(place, "add", "-A")
        self.git(place, "commit", "-q", "-m", "worker change")
        commit = self.git(place, "rev-parse", "HEAD")
        self.finish(started["task"], {"outcome": "succeeded", "commit": commit})
        if integrate:
            self.git(self.repo, "merge", "-q", "--no-edit", commit)
            commands.accept(self.repo, None, started["assignment"], integrated=self.git(self.repo, "rev-parse", "HEAD"))
        return place, started["assignment"]

    def finished(self):
        """Review, meet the criteria, push and authorize: everything but the delivery itself."""
        self.review("pass")
        commands.record(self.repo, None, met=["c1", "c2"], why="seen", authorize=True, words="merge it")
        self.pull_request()

    def cleanups(self):
        return [entry for entry in self.entries("issue-5") if entry.get("effect") == "cleanup"]


class CleanupTests(CleanupCase):
    def test_a_completed_objective_releases_its_workers_and_removes_their_worktrees_with_no_further_command(self):
        one, _ = self.worker({"src/a.py": "a\n"}, paths=["src"])
        two, _ = self.worker({"docs/b.md": "b\n"}, paths=["docs"])
        self.finished()
        calls_before = len(self.gh()["calls"])
        output = gate.deliver(self.repo, None)
        self.assertEqual(output["state"], "closed")
        self.assertEqual(sorted(output["cleanup"]["removed"]), sorted([str(one), str(two)]))
        self.assertEqual(len(output["cleanup"]["released"]), 3)               # two implementers and the reviewer
        self.assertNotIn("kept", output["cleanup"])
        self.assertFalse(one.exists() or two.exists())
        self.assertEqual(self.git(self.repo, "branch", "--list", "worker/*"), "")
        self.assertTrue(self.repo.exists() and self.git(self.repo, "status", "--porcelain") == "")   # its own worktree stays
        self.assertTrue(all(row["terminalState"] == "released" for row in self.orca()["workers"].values()))
        intent, outcome = self.cleanups()
        self.assertEqual((sorted(intent["remove"]), sorted(outcome["removed"])), (sorted([str(one), str(two)]),) * 2)
        self.assertLess(intent["n"], outcome["n"])
        self.assertNotIn("pr checks", [" ".join(call[:2]) for call in self.gh()["calls"][calls_before:]][-3:])

    def test_repeating_it_is_safe_and_does_nothing_twice(self):
        self.worker({"src/a.py": "a\n"}, paths=["src"])
        self.finished()
        gate.deliver(self.repo, None)
        counts = (self.orca()["releases"], self.orca()["removal_attempts"], len(self.cleanups()))
        again = gate.deliver(self.repo, None)
        self.assertNotIn("cleanup", again)
        self.assertEqual((self.orca()["releases"], self.orca()["removal_attempts"], len(self.cleanups())), counts)

    def test_running_kept_open_changed_and_unmerged_work_is_left_exactly_as_it_is(self):
        dirty, _ = self.worker({"src/a.py": "a\n"}, paths=["src"])
        (dirty / "notes.txt").write_text("not committed\n")
        ahead, _ = self.worker({"docs/b.md": "b\n"}, paths=["docs"])
        (ahead / "docs/later.md").write_text("after the hand-in\n")
        self.git(ahead, "add", "-A")
        self.git(ahead, "commit", "-q", "-m", "more, never integrated")
        visited, _ = self.worker({"lib/c.py": "c\n"}, paths=["lib"])
        running = commands.dispatch(self.repo, None, role="investigate", title="still looking", spec="Look.")
        self.review("pass")
        state = self.orca()
        reviewer = max(state["workers"], key=lambda key: int(key.split("_")[1]))       # it worked in the objective's own worktree
        state["workers"][reviewer].update(terminalState="retained")
        state["workers"][reviewer]["resource"]["retainedReason"] = "user_requested"
        state["open_terminals"] = {str(visited): [{"handle": "term_person"}]}
        self.orca_file.write_text(json.dumps(state))
        commands.record(self.repo, None, met=["c1", "c2"], why="seen", authorize=True, words="merge it")
        self.pull_request()
        output = gate.deliver(self.repo, None)
        self.assertEqual(output["state"], "closed")
        kept = "\n".join(output["cleanup"]["kept"])
        for reason in ("still running", "(review): its terminal is retained (user_requested)",
                       f"worktree {dirty}: it has uncommitted or untracked files",
                       f"worktree {ahead}: it holds commits that were not delivered",
                       f"worktree {visited}: a terminal other than its worker's is open in it"):
            self.assertIn(reason, kept)
        self.assertTrue(dirty.exists() and ahead.exists() and visited.exists())
        self.assertEqual((dirty / "notes.txt").read_text(), "not committed\n")
        self.assertEqual(len(self.git(self.repo, "branch", "--list", "worker/*").splitlines()), 3)
        workers = self.orca()["workers"]
        self.assertEqual([workers[key]["terminalState"] for key in ("ctx_1", "ctx_2", "ctx_3", reviewer, running["dispatch"])],
                         ["retained", "retained", "retained", "retained", "active"])     # nothing kept was released
        self.assertNotIn("removed", output["cleanup"])

    def test_a_worktree_another_worker_is_still_using_stays(self):
        place, _ = self.worker({"src/a.py": "a\n"}, paths=["src"])
        commands.dispatch(self.repo, None, role="investigate", title="reading there", spec="Look.", worktree=f"path:{place}")
        self.finished()
        output = gate.deliver(self.repo, None)
        self.assertIn(f"worktree {place}: another worker is using it", output["cleanup"]["kept"])
        self.assertTrue(place.exists())

    def test_a_worker_orca_does_not_show_as_finished_keeps_its_terminal_and_worktree(self):
        place, _ = self.worker({"src/a.py": "a\n"}, paths=["src"])
        state = self.orca()
        state["workers"]["ctx_1"].update(dispatchStatus="failed", workerState="abandoned", terminalState="active")
        self.orca_file.write_text(json.dumps(state))
        self.finished()
        output = gate.deliver(self.repo, None)
        self.assertIn("a1 (write): Orca does not show that it finished (failed, abandoned)", output["cleanup"]["kept"])
        self.assertTrue(place.exists())
        self.assertEqual(self.orca()["workers"]["ctx_1"]["terminalState"], "active")

    def test_a_worktree_is_removed_only_after_orca_confirms_its_terminal_was_released(self):
        place, _ = self.worker({"src/a.py": "a\n"}, paths=["src"])
        self.finished()
        self.set_orca(release_stays="retained")
        output = gate.deliver(self.repo, None)
        kept = "\n".join(output["cleanup"]["kept"])
        self.assertIn(f"worktree {place}: its worker's terminal was not released: Orca reports it retained", kept)
        self.assertIn("Orca reports it retained after the release was asked", kept)
        self.assertEqual((place.exists(), "released" in output["cleanup"], self.orca().get("removal_attempts", 0)), (True, False, 0))
        self.assertIn("pod deliver", output["next"])
        self.set_orca(release_stays=None)
        self.assertEqual(gate.deliver(self.repo, None)["cleanup"]["removed"], [str(place)])

    def test_terminals_that_cannot_be_read_or_a_terminal_orca_does_not_own_keep_the_worktree(self):
        place, _ = self.worker({"src/a.py": "a\n"}, paths=["src"])
        self.finished()
        real = self.bin / "orca.real"
        real.write_text((self.bin / "orca").read_text())
        real.chmod(0o755)
        self.script("orca", f'#!/bin/sh\nif [ "$1 $2" = "terminal list" ]; then echo \'{{"ok": true, "result": {{}}}}\'; exit 0; fi\n'
                            f'exec {real} "$@"\n')
        output = gate.deliver(self.repo, None)
        self.assertIn(f"worktree {place}: the terminals open in it could not be read", output["cleanup"]["kept"])
        self.script("orca", real.read_text())
        state = self.orca()
        state["workers"]["ctx_1"]["resource"]["ownershipState"] = "user_owned"
        self.orca_file.write_text(json.dumps(state))
        again = gate.deliver(self.repo, None)
        self.assertIn(f"worktree {place}: its worker's terminal is not Orca's to release", again["cleanup"]["kept"])
        self.assertTrue(place.exists())
        self.assertEqual(self.orca()["workers"]["ctx_1"]["terminalState"], "retained")

    def test_a_worktree_another_objective_now_works_in_stays(self):
        place, _ = self.worker({"src/a.py": "a\n"}, paths=["src"])
        commands.open_(place, text="Follow-up in that worktree", objective="follow-up", delivery=False)
        self.finished()
        output = gate.deliver(self.repo, "issue-5")
        self.assertIn(f"worktree {place}: objective follow-up works in it", output["cleanup"]["kept"])
        self.assertTrue(place.exists())

    def test_cleanup_after_an_objective_that_record_closed_can_be_tried_again(self):
        commands.open_(self.repo, text="Find out why", objective="why", delivery=False)
        started = commands.dispatch(self.repo, "why", role="investigate", title="look", spec="Look.", worktree="new-child")
        place = Path(self.orca()["workers"][started["dispatch"]]["resource"]["worktreeId"].partition("::")[2])
        self.finish(started["task"], {"outcome": "succeeded"})
        commands.accept(self.repo, "why", "a1")
        self.set_orca(removal_fails=True)
        first = commands.record(self.repo, "why", met=["c1"], why="found")
        self.assertIn("pod deliver", first["next"])
        self.assertTrue(place.exists())
        self.set_orca(removal_fails=False)
        again = gate.deliver(self.repo, "why")
        self.assertEqual((again["state"], again["cleanup"]["removed"]), ("closed", [str(place)]))
        self.assertNotIn("cleanup", commands.record(self.repo, "why", note="done"))

    def test_a_failed_removal_says_what_remains_and_never_reopens_the_delivery(self):
        place, _ = self.worker({"src/a.py": "a\n"}, paths=["src"])
        self.finished()
        self.set_orca(removal_fails=True)
        output = gate.deliver(self.repo, None)
        self.assertEqual((output["state"], self.gh()["merges"]), ("closed", 1))
        self.assertIn(f"worktree {place}: Orca did not remove it (worktree_remove_failed", output["cleanup"]["kept"][0])
        self.assertIn("run `pod deliver` again to finish cleaning up; the delivery itself is unaffected", output["next"])
        self.assertTrue(place.exists())
        self.assertEqual(commands.listing()["objectives"], [])               # still closed
        self.set_orca(removal_fails=False)
        again = gate.deliver(self.repo, None)
        self.assertEqual((again["cleanup"]["removed"], again["state"], self.gh()["merges"]), ([str(place)], "closed", 1))
        self.assertFalse(place.exists())

    def test_orca_being_unreadable_keeps_everything_and_the_delivery_stands(self):
        place, _ = self.worker({"src/a.py": "a\n"}, paths=["src"])
        self.finished()
        real = self.bin / "orca.real"
        real.write_text((self.bin / "orca").read_text())
        real.chmod(0o755)
        marker = self.tmp / "merged"
        self.script("gh", (self.bin / "gh").read_text().replace('exec ', f'if [ "$1 $2" = "pr merge" ]; then touch {marker}; fi\nexec '))
        self.script("orca", f'#!/bin/sh\nif [ -e {marker} ]; then echo "not json"; exit 1; fi\nexec {real} "$@"\n')
        output = gate.deliver(self.repo, None)
        self.assertEqual(output["state"], "closed")
        self.assertIn("Orca could not be read", output["cleanup"]["kept"][0])
        self.assertTrue(place.exists())

    def test_a_dropped_objective_releases_settled_workers_and_keeps_unfinished_work(self):
        unmerged, _ = self.worker({"src/a.py": "a\n"}, paths=["src"], integrate=False)
        idle = commands.dispatch(self.repo, None, role="investigate", title="look", spec="Look.", worktree="new-child")
        empty = Path(self.orca()["workers"][idle["dispatch"]]["resource"]["worktreeId"].partition("::")[2])
        self.finish(idle["task"], {"outcome": "succeeded"})
        output = commands.drop(self.repo, None, why="no longer wanted")
        self.assertEqual(output["cleanup"]["removed"], [str(empty)])          # it never held anything of its own
        self.assertIn(f"worktree {unmerged}: it holds commits that were not delivered", output["cleanup"]["kept"])
        self.assertTrue(unmerged.exists())
        states = [row["terminalState"] for row in self.orca()["workers"].values()]
        self.assertEqual((self.orca()["releases"], states), (1, ["retained", "released"]))   # the unfinished work keeps its terminal

    def test_an_objective_without_a_delivery_is_tidied_when_its_last_criterion_is_met(self):
        commands.open_(self.repo, text="Find out why", objective="why", delivery=False)
        started = commands.dispatch(self.repo, "why", role="investigate", title="look", spec="Look.")
        self.finish(started["task"], {"outcome": "succeeded"})
        commands.accept(self.repo, "why", "a1")
        output = commands.record(self.repo, "why", met=["c1"], why="found")
        self.assertEqual(output["cleanup"]["released"], [started["dispatch"]])

    def test_it_never_touches_another_objectives_workers(self):
        elsewhere = self.tmp / "elsewhere"
        self.git(self.repo, "worktree", "add", "-q", "-b", "other", str(elsewhere), "main")
        commands.open_(elsewhere, text="Another", objective="another", delivery=False)
        other = commands.dispatch(elsewhere, "another", role="investigate", title="other", spec="Look.")
        self.finish(other["task"], {"outcome": "succeeded"})
        os.environ["FAKE_ORCA_CALLER"] = "term_two"                           # each objective has its own Run
        self.worker({"src/a.py": "a\n"}, paths=["src"])
        self.finished()
        gate.deliver(self.repo, "issue-5")
        self.assertEqual(self.orca()["workers"][other["dispatch"]]["terminalState"], "reclaimable")


class CleanupSmallnessTests(CleanupCase):
    def test_an_unrelated_release_on_the_target_reopens_no_review_of_cleanup_code(self):
        self.implement({"skills/pod/cleanup.py": "def tidy(): ...\n", "tests/test_cleanup.py": "pass\n"})
        self.review("pass")
        head = lambda: self.git(self.repo, "rev-parse", "HEAD")
        base = lambda: self.git(self.repo, "merge-base", "HEAD", "origin/main")
        for release in ("0.22.1", "0.22.2"):                                 # two unrelated releases move the target
            self.git(self.repo, "switch", "-q", "main")
            self.commit({"VERSION": release + "\n", "CHANGES.md": release + "\n"}, "release " + release)
            self.git(self.repo, "push", "-q", "origin", "main")
            self.git(self.repo, "switch", "-q", "work")
            self.git(self.repo, "merge", "-q", "--no-edit", "main")
            self.assertEqual(proof.coverage(self.repo, fold(self.entries("issue-5")), head(), base())["uncovered"], [])
        self.assertEqual(len(fold(self.entries("issue-5")).of("review")), 1)

    def test_using_cleanup_needs_no_review_authorization_or_command_of_its_own(self):
        self.worker({"src/a.py": "a\n"}, paths=["src"])
        self.finished()
        before = {kind: len(fold(self.entries("issue-5")).of(kind)) for kind in ("review", "authorization", "waiver")}
        self.assertIn("cleanup", gate.deliver(self.repo, None))
        state = fold(self.entries("issue-5"))
        self.assertEqual({kind: len(state.of(kind)) for kind in before}, before)
        self.assertNotIn("cleanup", cli.parser()._subparsers._group_actions[0].choices)

    def test_it_is_one_small_module_over_orca_and_git_with_no_host_dependency_and_no_state_of_its_own(self):
        text = (ROOT / "skills/pod/cleanup.py").read_text()
        self.assertLess(len(text.splitlines()), 140)
        self.assertNotRegex(text, r"ce-devbox|ce_|subprocess|open\(|json|class |--force\b(?!:)")
        self.assertEqual(sorted(path.name for path in (ROOT / "skills/pod").glob("*cleanup*")), ["cleanup.py"])
        self.worker({"src/a.py": "a\n"}, paths=["src"])
        self.finished()
        gate.deliver(self.repo, None)
        self.assertEqual({entry["kind"] for entry in self.entries("issue-5")},
                         {"objective", "criterion", "assignment", "fact", "effect"})
        from pod import log
        self.assertEqual([path.name for path in log.path("acme--widgets", "issue-5").parent.iterdir()], ["issue-5.jsonl"])
