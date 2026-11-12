"""Edges an independent review found. Each test fails without the rule it names."""

import json
import os

from pod import commands, gate, gitio, log, proof
from pod.state import fold
from tests.core.support import ROUTES
from tests.core.test_gate import BODY, GateCase, REQUIRED


class DeliveryByOthersTests(GateCase):
    def test_a_branch_that_merely_sits_on_the_target_is_not_a_delivery(self):
        self.ready()                                                        # real work, unmerged, on open pull request 7
        self.git(self.repo, "switch", "-q", "main")
        self.commit({"unrelated.txt": "someone else's change\n"})
        self.git(self.repo, "push", "-q", "origin", "main")                 # HEAD is now on the target, without the work
        problem = self.refused("gate_closed", gate.deliver, self.repo, "issue-5")
        self.assertTrue(any(need.startswith("push: pull request 7") for need in problem.detail["needs"]))
        self.assertEqual((self.effects(), self.gh().get("edits", 0), self.gh().get("comments", [])), ([], 0, []))

    def test_a_check_recorded_on_the_target_tip_does_not_make_it_a_delivery(self):
        self.ready()
        self.git(self.repo, "switch", "-q", "main")
        self.commit({"unrelated.txt": "x\n"})
        self.git(self.repo, "push", "-q", "origin", "main")
        commands.record(self.repo, "issue-5", check="true")
        self.refused("gate_closed", gate.deliver, self.repo, "issue-5")
        self.assertEqual(self.effects(), [])

    def test_a_pull_request_merged_into_another_branch_is_not_a_delivery(self):
        commit = self.ready()
        self.pull_request(head=commit, state="MERGED", base="develop")
        problem = self.refused("gate_closed", gate.deliver, self.repo, None)
        self.assertIn("pull request 7 targets develop, the objective targets main", problem.detail["needs"])
        self.assertEqual(self.effects(), [])

    def test_new_work_after_a_merged_pull_request_needs_its_own_pull_request_and_checks(self):
        self.ready()
        gate.deliver(self.repo, None)
        self.commit({"src/more.py": "more\n"})
        verdict = gate.deliver(self.repo, None, check=True)
        self.assertFalse(verdict["pass"])
        self.assertTrue(any(need.startswith("a new pull request: 7 already merged at") for need in verdict["needs"]))
        self.refused("gate_closed", gate.deliver, self.repo, None)
        self.assertEqual((len(self.effects()), self.gh()["merges"], self.gh()["merge_attempts"]), (2, 1, 1))


class SettleTests(GateCase):
    def lose_everything_after_the_merge(self):
        real, marker = self.bin / "gh.real", self.tmp / "merged"
        real.write_text((self.bin / "gh").read_text())
        real.chmod(0o755)
        self.script("gh", f'#!/bin/sh\nif [ -e {marker} ]; then exit 1; fi\n'
                          f'if [ "$1 $2" = "pr merge" ]; then {real} "$@"; touch {marker}; exit 1; fi\nexec {real} "$@"\n')
        self.refused("merge_unknown", gate.deliver, self.repo, None)
        marker.unlink()
        self.script("gh", real.read_text())

    def test_a_merge_that_happened_is_recorded_as_merged_even_after_the_branch_moved(self):
        commit = self.ready()
        self.lose_everything_after_the_merge()
        self.commit({"src/later.py": "later\n"})                           # the local branch moved before the next call
        self.refused("gate_closed", gate.deliver, self.repo, None)
        intent, outcome = self.effects()
        self.assertEqual((intent["head"], outcome["merged"], outcome["observed"]), (commit, True, "pull request merged"))
        self.assertEqual(self.gh()["issues"]["5"]["body"], BODY.replace("[ ]", "[x]"))   # its proof still reaches the issue
        self.assertEqual((self.gh()["merges"], self.gh()["merge_attempts"]), (1, 1))

    def test_a_merge_github_refused_does_not_keep_the_objective_open_after_a_later_merge(self):
        self.ready()
        real = self.bin / "gh.real"
        real.write_text((self.bin / "gh").read_text())
        real.chmod(0o755)
        self.script("gh", f'#!/bin/sh\nif [ "$1 $2" = "pr merge" ]; then echo "policy prohibits the merge" >&2; exit 1; fi\nexec {real} "$@"\n')
        self.refused("merge_refused", gate.deliver, self.repo, None)
        self.script("gh", real.read_text())
        self.assertEqual(gate.deliver(self.repo, None)["state"], "closed")
        self.assertEqual([entry.get("merged") for entry in self.effects() if entry["phase"] == "outcome"], [False, True])
        self.assertEqual(commands.listing()["objectives"], [])

    def test_a_pending_merge_whose_pull_request_cannot_be_read_stays_unknown(self):
        self.ready()
        self.lose_everything_after_the_merge()
        self.set_gh(down=True)
        self.refused("merge_unknown", gate.deliver, self.repo, None)
        self.assertEqual([entry["phase"] for entry in self.effects()], ["intent"])


class RulesAndChecksTests(GateCase):
    rules = None

    def test_rules_added_to_the_target_after_the_branch_forked_apply(self):
        self.ready(review=False)
        self.git(self.repo, "switch", "-q", "main")
        self.commit({".pod/config.yaml": REQUIRED}, "rules")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "switch", "-q", "work")
        self.assertTrue(any("independent passing review" in need for need in self.needs()))

    def test_a_target_that_cannot_be_fetched_requires_review_instead_of_using_an_older_copy(self):
        self.ready(review=False)
        self.git(self.repo, "config", f"url.{self.tmp / 'nowhere.git'}.insteadOf", "https://github.com/acme/widgets.git")
        self.git(self.repo, "config", "--unset", f"url.{self.bare}.insteadOf")
        self.assertTrue(any("could not be read; until then review is required" in need for need in self.needs()))

    def test_no_reported_check_passes_only_when_github_itself_allows_the_merge(self):
        self.ready()
        self.pull_request(checks=None)
        state = self.gh()
        for status in ("BLOCKED", "UNKNOWN"):
            state["prs"]["7"]["mergeStateStatus"] = status
            self.gh_file.write_text(json.dumps(state))
            self.assertTrue(any(f"reports the pull request {status}" in need for need in self.needs()), status)
        state["prs"]["7"]["mergeStateStatus"] = "CLEAN"
        self.gh_file.write_text(json.dumps(state))
        self.assertEqual(gate.deliver(self.repo, None)["state"], "closed")

    def test_a_merge_state_github_is_still_computing_is_read_again_before_refusing(self):
        self.ready()
        self.pull_request(checks=None)
        state = self.gh()
        state["prs"]["7"]["mergeStateStatus"] = "UNKNOWN"
        self.gh_file.write_text(json.dumps(state))
        real = self.bin / "gh.real"
        real.write_text((self.bin / "gh").read_text())
        real.chmod(0o755)
        count = self.tmp / "views"
        self.script("gh", f'#!/bin/sh\nif [ "$1 $2" = "pr view" ]; then echo x >> {count}; '
                          f'if [ "$(wc -l < {count})" -ge 3 ]; then {real} "$@" | sed s/UNKNOWN/CLEAN/; exit 0; fi; fi\n'
                          f'exec {real} "$@"\n')
        self.assertEqual(gate.deliver(self.repo, None)["state"], "closed")

    def test_an_empty_list_of_required_checks_falls_back_to_githubs(self):
        self.project("schema: pod-project/v1\nrequired_checks:\n# - linux\n")
        self.ready(review=False)
        self.pull_request(checks="fail")
        self.assertTrue(any("required checks on" in need for need in self.needs()))

    def test_a_local_status_never_says_ready_without_reading_github(self):
        self.ready()
        self.pull_request(checks="fail")
        shown = commands.status(self.repo, None, remote=False)["gate"]
        self.assertNotEqual(shown, "ready to deliver")
        self.assertTrue(any("were not read" in need for need in shown))


class FindingAndAuthorityTests(GateCase):
    def test_a_second_opinion_on_the_same_content_does_not_close_a_finding(self):
        self.implement({"src/a.py": "a\n"})
        self.review("fail", [{"path": "src/a.py", "required": True, "text": "wrong"}])
        self.review("pass")                                                 # another reviewer, same commit
        opened = lambda: [row["id"] for row in proof.open_required(self.repo, fold(self.entries("issue-5")))]
        self.assertEqual(opened(), ["a2.1"])
        self.commit({"docs/unrelated.md": "x\n"})
        self.review("pass")                                                 # a later commit, the file itself unchanged
        self.assertEqual(opened(), ["a2.1"])
        commands.record(self.repo, None, carried="an earlier record", commit="HEAD", paths=["src/a.py"], verdict="pass")
        self.assertEqual(opened(), ["a2.1"])
        self.implement({"src/a.py": "a corrected\n"})
        commands.record(self.repo, None, carried="an earlier record", commit="HEAD", paths=["src/a.py"], verdict="pass")
        self.assertEqual(opened(), ["a2.1"])                                # a carried review never closes a finding
        self.review("pass", paths_=["src/a.py"])
        self.assertEqual(opened(), [])

    def test_a_withdrawal_holds_against_the_coordinator_setting_and_the_verifier(self):
        self.ready()
        commands.record(self.repo, None, withdraw=True, words="hold the merge")
        self.write_routes(ROUTES + "merge_authorization: coordinator\n")
        self.refused("authorization_required", gate.deliver, self.repo, None)
        self.assertEqual(self.gh().get("merge_attempts", 0), 0)
        commands.record(self.repo, None, authorize=True)
        self.assertEqual(gate.deliver(self.repo, None)["state"], "closed")

    def test_a_discarded_review_covers_nothing(self):
        self.implement({"src/a.py": "a\n"})
        started = commands.dispatch(self.repo, None, role="review", title="r", spec="Review.")
        self.finish(started["task"], {"outcome": "succeeded", "verdict": "pass", "findings": []})
        self.assertIn("no review is recorded", commands.accept(self.repo, None, "a2", discarded=True)["note"])
        self.assertEqual(fold(self.entries("issue-5")).of("review"), [])

    def test_a_mode_change_after_the_review_is_a_change(self):
        self.implement({"tool.sh": "#!/bin/sh\n"})
        self.review("pass")
        os.chmod(self.repo / "tool.sh", 0o755)
        self.git(self.repo, "commit", "-q", "-am", "make it executable")
        head, base = self.git(self.repo, "rev-parse", "HEAD"), self.git(self.repo, "merge-base", "HEAD", "origin/main")
        self.assertEqual(proof.coverage(self.repo, fold(self.entries("issue-5")), head, base)["uncovered"], ["tool.sh"])

    def test_a_check_binds_only_to_the_commit_it_ran_on(self):
        (self.repo / "race.sh").write_text("#!/bin/sh\necho changed > src.txt\ngit add -A\ngit commit -q -m during\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "a check that commits")
        commands.record(self.repo, None, check="sh race.sh")
        fact = self.entries("issue-5")[-1]
        self.assertEqual(fact["exit"], 0)
        self.assertNotIn("commit", fact)


class RobustnessTests(GateCase):
    def test_orca_not_knowing_the_outcome_is_unknown_not_a_refusal(self):
        real = self.bin / "orca.real"
        real.write_text((self.bin / "orca").read_text())
        real.chmod(0o755)
        self.script("orca", f'#!/bin/sh\nif [ "$2" = "worker-start" ]; then {real} "$@" >/dev/null; '
                            'echo \'{"ok": false, "error": {"code": "runtime_timeout", "message": "no answer"}}\'; exit 1; fi\n'
                            f'exec {real} "$@"\n')
        self.refused("orca_unknown", commands.dispatch, self.repo, None, role="implement", title="w", spec="Do.", paths_=["src"])
        self.assertFalse(fold(self.entries("issue-5")).assignments["a1"].get("void"))
        self.refused("paths_overlap", commands.dispatch, self.repo, None, role="implement", title="w", spec="Do.", paths_=["src"])
        self.script("orca", real.read_text())
        self.assertEqual(commands.dispatch(self.repo, None, retry="a1")["dispatch"], "ctx_1")
        self.assertEqual(self.orca()["starts"], 1)

    def test_paths_are_compared_in_one_spelling_and_cannot_leave_the_repository(self):
        commands.dispatch(self.repo, None, role="implement", title="w", spec="Do.", paths_=["src/"])
        self.refused("paths_overlap", commands.dispatch, self.repo, None, role="implement", title="w", spec="Do.",
                     paths_=["./src/a.py"])
        self.refused("path_invalid", commands.dispatch, self.repo, None, role="implement", title="w", spec="Do.", paths_=["../x"])
        self.assertEqual(gitio.declared(["./a//b/", " docs ", ""]), ["a/b", "docs"])
        self.assertEqual(gitio.declared(["src", "."]), [])

    def test_one_unreadable_log_does_not_stop_other_objectives(self):
        broken = log.path("acme--widgets", "broken")
        broken.write_text('{"kind":"mystery","n":0,"at":"2026-01-01T00:00:00Z"}\n')
        self.assertEqual(commands.status(self.repo, None, remote=False)["objective"], "issue-5")
        listed = commands.listing()["objectives"]
        self.assertEqual([(row["objective"], row.get("problem")) for row in listed], [("broken", "log_damaged"), ("issue-5", None)])

    def test_a_refused_open_leaves_no_log_and_unknown_objectives_are_refused_cleanly(self):
        started = commands.dispatch(self.repo, None, role="investigate", title="look", spec="Look.")
        self.refused("run_busy", commands.open_, self.repo, text="Another", objective="another", run="run_1")
        self.assertFalse(log.path("acme--widgets", "another").exists())
        self.refused("objective_unknown", commands.record, self.repo, "another", note="x")
        self.refused("objective_unknown", gate.deliver, self.repo, "nowhere", check=True)
        self.refused("evidence_required", commands.record, self.repo, None, met=["c1"], facts=["a2.1"])
        self.assertTrue(started["dispatch"])
