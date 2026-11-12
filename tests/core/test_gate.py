import json

from pod import commands, gate
from tests.core.support import CoreCase, ROUTES

REQUIRED = "schema: pod-project/v1\nreview: required\n"
BODY = "## Proof of Done\n- [ ] it works\n- [ ] it is documented\n"


class GateCase(CoreCase):
    rules = REQUIRED

    def setUp(self):
        super().setUp()
        if self.rules:
            self.project(self.rules)
        self.issue(5, "Make the widget", BODY)
        commands.open_(self.repo, source="5")

    def ready(self, *, authorize: bool = True, review: bool = True) -> str:
        """A reviewed, pushed candidate with green checks and every criterion met."""
        commit, _ = self.implement({"src/a.py": "a\n"})
        if review:
            self.review("pass")
        commands.record(self.repo, None, met=["c1", "c2"], why="observed in the review")
        self.pull_request()
        if authorize:
            commands.record(self.repo, None, authorize=True, words="merge it")
        return commit

    def needs(self) -> list[str]:
        problem = self.refused("gate_closed", gate.deliver, self.repo, None)
        self.assertEqual(self.gh().get("merges", 0), 0)
        return problem.detail["needs"]

    def effects(self, kind="merge"):
        return [entry for entry in self.entries("issue-5") if entry.get("effect") == kind]


class GateTests(GateCase):
    def test_the_exact_candidate_merges_once_and_the_objective_closes_without_a_closing_write(self):
        commit = self.ready()
        output = gate.deliver(self.repo, None)
        self.assertEqual((output["merged"], output["state"]), (commit, "closed"))
        intent, outcome = self.effects()
        self.assertEqual((intent["phase"], intent["head"], intent["pr"]), ("intent", commit, 7))
        self.assertEqual((outcome["phase"], outcome["merged"], outcome["observed"]), ("outcome", True, "pull request merged"))
        self.assertLess(intent["n"], outcome["n"])
        self.assertEqual(self.gh()["merges"], 1)
        ci = [entry for entry in self.entries("issue-5") if entry.get("fact") == "ci"][0]
        self.assertEqual((ci["commit"], ci["state"]), (commit, "green"))

    def test_a_moved_head_is_refused(self):
        self.ready()
        self.commit({"src/a.py": "changed after the push\n"})
        self.assertTrue(any(need.startswith("push: pull request 7 is at") for need in self.needs()))

    def test_a_pull_request_head_someone_else_moved_is_refused(self):
        self.ready()
        self.pull_request(head="f" * 40)
        self.assertTrue(any(need.startswith("push: pull request 7 is at ffffffffffff") for need in self.needs()))

    def test_uncommitted_changes_are_refused(self):
        self.ready()
        (self.repo / "stray.txt").write_text("x\n")
        self.assertTrue(any("uncommitted" in need for need in self.needs()))

    def test_a_required_check_that_is_not_green_is_refused(self):
        self.ready()
        for bucket in ("fail", "pending"):
            self.pull_request(checks=bucket)
            self.assertTrue(any(f"required checks on" in need and "linux" in need for need in self.needs()), bucket)
        self.set_gh(down=True)
        self.assertTrue(self.needs())                                       # unknown is never green

    def test_a_changed_path_without_applicable_review_is_refused(self):
        self.ready(review=False)
        self.assertTrue(any("independent passing review" in need and "src/a.py" in need for need in self.needs()))

    def test_an_open_required_finding_is_refused(self):
        self.implement({"src/a.py": "a\n"})
        self.review("fail", [{"path": "src/a.py", "required": True, "text": "wrong"}])
        self.pull_request()
        self.assertTrue(any("finding a2.1" in need for need in self.needs()))

    def test_an_open_criterion_is_refused_unless_it_is_proved_after_the_merge(self):
        self.implement({"src/a.py": "a\n"})
        self.review("pass")
        self.pull_request()
        commands.record(self.repo, None, authorize=True, words="merge it", met=["c1"], why="seen")
        self.assertEqual(self.needs(), ["criterion c2 met or dropped: it is documented"])
        commands.record(self.repo, None, after_merge=["c2"])
        output = gate.deliver(self.repo, None)
        self.assertEqual(output["state"], "open")                           # merged, and c2 is still to prove
        self.assertEqual(self.gh()["issues"]["5"]["body"], BODY.replace("[ ] it works", "[x] it works"))
        commands.record(self.repo, None, met=["c2"], why="seen in production")
        self.assertEqual(gate.deliver(self.repo, None)["state"], "closed")
        self.assertEqual(self.gh()["issues"]["5"]["body"], BODY.replace("[ ]", "[x]"))
        self.assertEqual(self.gh()["merges"], 1)

    def test_no_authorization_no_merge(self):
        self.ready(authorize=False)
        problem = self.refused("authorization_required", gate.deliver, self.repo, None)
        self.assertIn("pod record --authorize", problem.fix)
        self.assertEqual(self.gh().get("merge_attempts", 0), 0)

    def test_an_authorization_covers_only_the_commit_it_was_given_for(self):
        self.ready()
        self.implement({"src/b.py": "b\n"})
        self.review("pass")
        self.pull_request()
        self.refused("authorization_required", gate.deliver, self.repo, None)
        commands.record(self.repo, None, authorize=True, standing=True, words="merge whatever passes")
        gate.deliver(self.repo, None)
        self.assertEqual(self.gh()["merges"], 1)

    def test_a_withdrawn_authorization_no_longer_covers(self):
        self.ready()
        commands.record(self.repo, None, withdraw=True)
        self.refused("authorization_required", gate.deliver, self.repo, None)

    def test_the_coordinator_authorizes_only_when_the_person_set_that(self):
        self.ready(authorize=False)
        self.refused("person_decides", commands.record, self.repo, None, authorize=True)
        self.write_routes(ROUTES + "merge_authorization: coordinator\n")
        gate.deliver(self.repo, None)
        fact = [entry for entry in self.entries("issue-5") if entry.get("fact") == "authorization"][0]
        self.assertEqual((fact["by"], fact["candidate"]), ("coordinator", self.git(self.repo, "rev-parse", "HEAD")))

    def test_the_host_verifier_can_vouch_for_the_exact_candidate_and_nothing_else(self):
        commit = self.ready(authorize=False)
        seen = self.tmp / "request.json"
        answer = self.tmp / "answer.json"
        self.script("verifier", f"#!/bin/sh\ncat > {seen}\ncat {answer}\n")
        self.write_routes(ROUTES + f"authorization_verifier:\n- {self.bin / 'verifier'}\n")
        verdict = {"schema": "pod-authorization/v1", "verified": True, "action": "merge", "repository": "acme/widgets",
                   "target": "main", "pr": 7, "candidate": "0" * 40, "reference": "card-1"}
        for wrong in (verdict, {**verdict, "candidate": commit, "verified": "yes"}, {"verified": True}, "not json"):
            answer.write_text(wrong if isinstance(wrong, str) else json.dumps(wrong))
            self.refused("authorization_required", gate.deliver, self.repo, None)
        self.assertEqual(json.loads(seen.read_text())["candidate"], commit)
        answer.write_text(json.dumps({**verdict, "candidate": commit}))
        gate.deliver(self.repo, None)
        fact = [entry for entry in self.entries("issue-5") if entry.get("fact") == "authorization"][0]
        self.assertEqual((fact["by"], fact["reference"]), ("verifier", "card-1"))

    def test_a_repeated_deliver_after_a_lost_response_reads_github_and_never_merges_twice(self):
        commit = self.ready()
        self.set_gh(lose_merge_reply=True)
        output = gate.deliver(self.repo, None)                              # the reply was lost; the read-back settles it
        self.assertEqual(output["merged"], commit)
        self.assertEqual(gate.deliver(self.repo, None)["merged"], commit)
        self.assertEqual((self.gh()["merges"], self.gh()["merge_attempts"]), (1, 1))
        self.assertEqual(len(self.effects()), 2)

    def test_an_outcome_that_could_not_be_read_stays_unknown_and_is_settled_by_the_next_call(self):
        commit = self.ready()
        (self.bin / "gh.real").write_text((self.bin / "gh").read_text())
        (self.bin / "gh.real").chmod(0o755)
        marker = self.tmp / "merged"
        self.script("gh", f'#!/bin/sh\nif [ -e {marker} ]; then exit 1; fi\n'
                          f'if [ "$1 $2" = "pr merge" ]; then {self.bin / "gh.real"} "$@"; touch {marker}; exit 1; fi\n'
                          f'exec {self.bin / "gh.real"} "$@"\n')
        self.refused("merge_unknown", gate.deliver, self.repo, None)        # merged at GitHub, nothing readable after
        self.assertEqual([entry["phase"] for entry in self.effects()], ["intent"])
        marker.unlink()
        self.script("gh", (self.bin / "gh.real").read_text())
        self.assertEqual(gate.deliver(self.repo, None)["merged"], commit)
        self.assertEqual((self.gh()["merges"], self.gh()["merge_attempts"]), (1, 1))
        self.assertEqual([entry["phase"] for entry in self.effects()], ["intent", "outcome"])

    def test_a_merge_github_refuses_is_recorded_as_not_merged(self):
        self.ready()
        state = self.gh()
        state["prs"]["7"]["state"] = "OPEN"
        self.gh_file.write_text(json.dumps(state))
        real = self.bin / "gh.real"
        real.write_text((self.bin / "gh").read_text())
        real.chmod(0o755)
        self.script("gh", f'#!/bin/sh\nif [ "$1 $2" = "pr merge" ]; then echo "merge conflict" >&2; exit 1; fi\nexec {real} "$@"\n')
        self.refused("merge_refused", gate.deliver, self.repo, None)
        self.assertEqual(self.effects()[-1]["merged"], False)

    def test_check_only_prints_the_same_verdict_and_changes_nothing(self):
        commit = self.ready(authorize=False)
        before = self.entries("issue-5")
        verdict = gate.deliver(self.repo, None, check=True)
        self.assertEqual(verdict, {"candidate": commit, "tree": self.git(self.repo, "rev-parse", "HEAD^{tree}"),
                                   "target": "main", "pr": 7, "pass": True, "authorized": False, "needs": []})
        self.assertEqual(gate.deliver(self.repo, None, check=True, commit=commit)["pass"], True)
        self.pull_request(checks="fail")
        self.assertEqual(gate.deliver(self.repo, None, check=True)["pass"], False)
        self.assertEqual(self.entries("issue-5"), before)
        self.assertEqual(self.gh().get("merge_attempts", 0), 0)

    def test_a_delivery_the_host_performed_is_recorded_once_git_shows_the_candidate_on_the_target(self):
        commit = self.ready(authorize=False)
        self.refused("authorization_required", gate.deliver, self.repo, None)
        self.git(self.repo, "push", "-q", "origin", "HEAD:main")           # the host merged it itself
        output = gate.deliver(self.repo, None)
        self.assertEqual((output["merged"], output["state"]), (commit, "closed"))
        intent, outcome = self.effects()
        self.assertEqual((intent["performed_by"], outcome["observed"]), ("another party", "candidate is on main"))
        self.assertEqual(self.gh().get("merge_attempts", 0), 0)

    def test_a_fresh_objective_on_the_target_commit_is_not_taken_for_delivered(self):
        self.pull_request()
        self.assertTrue(self.needs())
        self.assertEqual(self.effects(), [])


class IssueUpdateTests(GateCase):
    def test_proved_criteria_are_ticked_in_one_edit_with_one_comment(self):
        self.ready()
        self.assertEqual(gate.deliver(self.repo, None)["issue"], "checklist ticked, evidence commented")
        state = self.gh()
        self.assertEqual((state["issues"]["5"]["body"], state["edits"], len(state["comments"])),
                         (BODY.replace("[ ]", "[x]"), 1, 1))
        self.assertIn("pull request 7", state["comments"][0])
        gate.deliver(self.repo, None)
        self.assertEqual((self.gh()["edits"], len(self.gh()["comments"])), (1, 1))

    def test_a_body_that_changed_since_open_is_commented_on_and_not_edited(self):
        self.ready()
        changed = "Rewritten by a person.\n" + BODY
        self.issue(5, "Make the widget", changed)
        self.assertEqual(gate.deliver(self.repo, None)["issue"], "checklist left unchanged, evidence commented")
        state = self.gh()
        self.assertEqual((state["issues"]["5"]["body"], state.get("edits", 0), len(state["comments"])), (changed, 0, 1))
        self.assertIn("left as it is", state["comments"][0])

    def test_only_checklist_lines_of_met_criteria_change_and_nothing_is_ever_unticked(self):
        from pod import issue
        body = "- [x] already\n- [ ] first\ntext [ ] not a box\n- [ ] second\r\n"
        self.assertEqual(issue.tick(body, [1]), "- [x] already\n- [x] first\ntext [ ] not a box\n- [ ] second\r\n")
        self.assertEqual(issue.tick(body, [0, 2, 3, 99]), "- [x] already\n- [ ] first\ntext [ ] not a box\n- [x] second\r\n")
        spaced = "- [ ] hard break after this   \n* [ ] second  \r\n  + [ ] nested [ ] twice\n"
        self.assertEqual(issue.tick(spaced, [0, 1, 2]), spaced.replace("- [ ]", "- [x]").replace("* [ ]", "* [x]")
                         .replace("+ [ ] nested", "+ [x] nested"))

    def test_fenced_code_is_neither_a_heading_nor_a_criterion(self):
        from pod import issue
        body = ("## Proof of Done\n- [ ] a\n```sh\n# run tests\n- [ ] not a criterion\n```\n- [ ] b\n~~~\n## Not a heading\n~~~\n"
                "- [ ] c\n## Notes\n- [ ] outside\n")
        self.assertEqual([(row["text"], row["line"]) for row in issue.criteria(body)], [("a", 1), ("b", 6), ("c", 10)])

    def test_a_document_type_is_recognised_whatever_its_case(self):
        from pod import issue
        self.assertEqual([issue.kind(title) for title in ("EEL: x", "eel: x", " Seal : y", "PES: z", "Peel: no", "An EEL: no")],
                         ["eel", "eel", "seal", "pes", None, None])


class ProjectRulesTests(GateCase):
    rules = None

    def test_without_project_rules_review_is_not_required_and_missing_required_checks_do_not_block(self):
        self.ready(review=False)
        self.pull_request(checks=None)
        self.assertEqual(gate.deliver(self.repo, None)["state"], "closed")

    def test_a_change_cannot_relax_the_gate_it_passes_through(self):
        self.project(REQUIRED)
        self.implement({".pod/config.yaml": "schema: pod-project/v1\nreview: none\n", "src/a.py": "a\n"})
        commands.record(self.repo, None, met=["c1", "c2"], why="seen", authorize=True, words="merge")
        self.pull_request()
        self.assertTrue(any("independent passing review of 2 changed paths" in need for need in self.needs()))

    def test_rules_this_version_cannot_read_require_review_and_say_so(self):
        self.project("schema: pod/v1\nwaste_governor:\n  preflight: [a, b]\n")
        self.ready(review=False)
        needs = self.needs()
        self.assertTrue(any(".pod/config.yaml" in need and "expected pod-project/v1" in need for need in needs), needs)
        self.assertTrue(any("independent passing review" in need for need in needs), needs)

    def test_a_project_can_name_its_own_required_checks_and_ask_for_a_final_tree_review(self):
        self.project("schema: pod-project/v1\nreview: required\nfinal_tree_review: true\nmerge_method: squash\n"
                     "required_checks:\n- linux\n- docs\n")
        self.ready()
        self.assertTrue(any("docs MISSING" in need for need in self.needs()))
        state = self.gh()
        state["checks"].append({"name": "docs", "bucket": "pass", "state": "SUCCESS"})
        self.gh_file.write_text(json.dumps(state))
        gate.deliver(self.repo, None)
        self.assertEqual(self.effects()[0]["method"], "squash")
