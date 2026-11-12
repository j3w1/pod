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
        opened = lambda: [row["id"] for row in proof.open_required(self.repo, fold(self.entries("issue-5")), self.git(self.repo, "rev-parse", "HEAD"))]
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

    def test_a_refused_open_leaves_no_objective_and_unknown_objectives_are_refused_cleanly(self):
        started = commands.dispatch(self.repo, None, role="investigate", title="look", spec="Look.")
        self.refused("run_busy", commands.open_, self.repo, text="Another", objective="another", run="run_1")
        self.refused("objective_unknown", commands.record, self.repo, "another", note="x")
        self.assertEqual([row["objective"] for row in commands.listing()["objectives"]], ["issue-5"])
        self.refused("objective_unknown", gate.deliver, self.repo, "nowhere", check=True)
        self.refused("evidence_required", commands.record, self.repo, None, met=["c1"], facts=["a2.1"])
        self.assertTrue(started["dispatch"])


class FinalReviewTests(GateCase):
    """Edges the final independent review found."""

    def opened(self):
        return [row["id"] for row in proof.open_required(self.repo, fold(self.entries("issue-5")), self.git(self.repo, "rev-parse", "HEAD"))]

    def uncovered(self):
        head, base = self.git(self.repo, "rev-parse", "HEAD"), self.git(self.repo, "merge-base", "HEAD", "origin/main")
        return proof.coverage(self.repo, fold(self.entries("issue-5")), head, base)["uncovered"]

    def test_a_directory_named_as_context_lapses_the_review_when_anything_under_it_changes(self):
        self.implement({"src/client.py": "c\n", "schema/api.json": "{}\n"})
        self.review("pass", paths_=["src/client.py"], context=["schema"])
        self.assertEqual(self.uncovered(), ["schema/api.json"])
        self.commit({"schema/new.json": "{}\n"})                           # a file added under the directory
        self.assertEqual(self.uncovered(), ["schema/api.json", "schema/new.json", "src/client.py"])

    def test_the_whole_tree_can_be_named_as_context(self):
        self.implement({"src/client.py": "c\n"})
        self.review("pass", context=["."])
        self.assertEqual(self.uncovered(), [])
        self.commit({"elsewhere.txt": "x\n"})
        self.assertEqual(self.uncovered(), ["elsewhere.txt", "src/client.py"])

    def test_a_review_orca_did_not_report_as_completed_covers_nothing_and_keeps_its_findings(self):
        self.implement({"src/a.py": "a\n"})
        started = commands.dispatch(self.repo, None, role="review", title="r", spec="Review.")
        self.finish(started["task"], {"outcome": "succeeded", "verdict": "pass", "findings": []}, outcome="failed")
        self.assertIn("covers nothing", commands.accept(self.repo, None, "a2")["note"])
        self.assertEqual(self.uncovered(), ["src/a.py"])
        started = commands.dispatch(self.repo, None, role="review", title="r", spec="Review.")
        self.finish(started["task"], {"outcome": "failed", "verdict": "fail",
                                      "findings": [{"path": "src/a.py", "required": True, "text": "wrong"}]}, outcome="failed")
        commands.accept(self.repo, None, "a3")
        self.assertEqual(self.opened(), ["a3.1"])

    def test_bringing_the_offending_content_back_reopens_its_finding(self):
        self.implement({"src/a.py": "A\n"})
        self.review("pass")
        self.review("fail", [{"path": "src/a.py", "required": True, "text": "A is wrong"}])
        self.implement({"src/a.py": "B\n"})
        self.review("pass", paths_=["src/a.py"])
        self.assertEqual(self.opened(), [])
        self.commit({"src/a.py": "A\n"})                                   # the correction is reverted
        self.assertEqual(self.uncovered(), [])                              # the first pass on A applies again
        self.assertEqual(self.opened(), ["a3.1"])                           # and so does the finding on A

    def test_a_failing_full_review_covers_the_files_it_found_nothing_required_on(self):
        self.implement({"src/a.py": "a\n", "src/b.py": "b\n", "docs/c.md": "c\n"})
        self.review("fail", [{"path": "src/a.py", "required": True, "text": "wrong"},
                             {"path": "docs/c.md", "required": False, "text": "typo"}])
        self.assertEqual(self.uncovered(), ["src/a.py"])
        self.implement({"src/a.py": "a corrected\n"})
        self.review("pass", paths_=["src/a.py"])                            # only the corrected file is read again
        self.assertEqual((self.uncovered(), self.opened()), ([], []))

    def test_a_finding_without_a_path_or_an_unexplained_failure_covers_nothing(self):
        self.implement({"src/a.py": "a\n", "src/b.py": "b\n"})
        self.review("fail", [{"path": None, "required": True, "text": "the approach is wrong"}])
        self.review("fail", [{"path": "src/a.py", "required": False, "text": "style"}])
        self.assertEqual(self.uncovered(), ["src/a.py", "src/b.py"])

    def test_a_review_that_raises_a_required_finding_on_the_corrected_file_does_not_close_the_earlier_one(self):
        self.implement({"src/a.py": "a\n"})
        self.review("fail", [{"path": "src/a.py", "required": True, "text": "wrong"}])
        self.implement({"src/a.py": "a, still wrong\n"})
        self.review("fail", [{"path": "src/a.py", "required": True, "text": "still wrong"}])
        self.assertEqual(self.opened(), ["a2.1", "a4.1"])

    def test_a_short_write_is_never_taken_for_a_whole_entry(self):
        import os
        from unittest.mock import patch
        target = log.path("acme--widgets", "issue-5")
        real, calls = os.pwrite, []

        def short(handle, data, offset):
            calls.append(len(data))
            return real(handle, data[:max(1, len(data) // 2)], offset)

        with patch.object(os, "pwrite", short), log.writer(target) as writer:
            writer.append({"kind": "fact", "fact": "note", "text": "x" * 200})
        self.assertGreater(len(calls), 1)
        self.assertEqual(log.read(target)[-1]["text"], "x" * 200)

    def test_a_refused_open_cannot_take_another_openers_log_with_it(self):
        target = log.path("acme--widgets", "raced")
        with self.assertRaises(RuntimeError), log.writer(target, create=True):
            raise RuntimeError("refused before anything was written")
        self.assertTrue(target.exists())                                    # the file identity other openers may hold stays
        with log.writer(target, create=True) as writer:
            writer.append({"kind": "objective", "id": "raced", "source": {"kind": "text", "text": "x"}, "target": "main"})
        self.assertEqual(len(log.read(target)), 1)

    def test_a_lost_start_is_settled_from_orcas_receipt_before_anything_is_voided(self):
        self.set_orca(lose_reply=True)
        self.refused("orca_unknown", commands.dispatch, self.repo, None, role="implement", title="w", spec="Do.", paths_=["src"])
        state = self.orca()
        state["tasks"]["task_1"]["task_title"] = "renamed by a person"       # the title can no longer identify it
        self.orca_file.write_text(json.dumps(state))
        settled = commands.dispatch(self.repo, None, retry="a1")
        self.assertEqual((settled.get("task"), settled.get("dispatch"), settled.get("void")), ("task_1", "ctx_1", None))
        self.refused("paths_overlap", commands.dispatch, self.repo, None, role="implement", title="w", spec="Do.", paths_=["src"])

    def test_the_tick_and_the_comment_are_owed_separately(self):
        self.ready()
        real = self.bin / "gh.real"
        real.write_text((self.bin / "gh").read_text())
        real.chmod(0o755)
        self.script("gh", f'#!/bin/sh\nif [ "$1 $2" = "issue edit" ]; then exit 1; fi\nexec {real} "$@"\n')
        first = gate.deliver(self.repo, None)
        self.assertEqual((first["issue"], first["issue_owed"]), ("checklist not ticked, evidence commented", ["the checklist edit"]))
        self.assertNotIn("left as it is", self.gh()["comments"][0])          # nothing false is said about the issue text
        self.script("gh", f'#!/bin/sh\nif [ "$1 $2" = "issue comment" ]; then exit 1; fi\nexec {real} "$@"\n')
        second = gate.deliver(self.repo, None)
        self.assertEqual(second["issue"], "checklist ticked")
        self.assertEqual((self.gh()["issues"]["5"]["body"], len(self.gh()["comments"])), (BODY.replace("[ ]", "[x]"), 1))
        self.assertNotIn("issue", gate.deliver(self.repo, None))             # nothing is owed any more

    def test_check_only_reports_authorization_apart_and_status_never_calls_an_unauthorized_candidate_ready(self):
        self.ready(authorize=False)
        verdict = gate.deliver(self.repo, None, check=True)
        self.assertEqual((verdict["pass"], verdict["authorized"]), (True, False))
        self.assertIn("still needs a merge authorization", commands.status(self.repo, None)["gate"])
        commands.record(self.repo, None, authorize=True, words="go")
        self.assertEqual(commands.status(self.repo, None)["gate"], "ready to deliver")

    def test_a_commit_is_only_ever_judged_never_merged_by_name(self):
        self.ready()
        self.refused("commit_needs_check", gate.deliver, self.repo, None, commit="HEAD~1")
        self.assertEqual(self.gh().get("merge_attempts", 0), 0)

    def test_requests_of_the_same_objective_name_in_two_repositories_differ(self):
        from pod import orca
        self.assertNotEqual(orca.request_id("acme--widgets", "issue-5", "a1", 1), orca.request_id("acme--gears", "issue-5", "a1", 1))
