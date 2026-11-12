from pod import commands
from tests.core.support import CoreCase

BODY = """Intro with a box that is not a criterion:
- [ ] unrelated box

## Proof of Done
- [ ] first proof
- [x] proved before
- [ ] second proof

## Notes
- [ ] not a proof
"""


class OpenTests(CoreCase):
    def test_an_issue_gives_its_proof_of_done_checklist_as_criteria(self):
        self.issue(5, "PES: Add a greeting", BODY)
        view = commands.open_(self.repo, source="5")
        self.assertEqual(view["objective"], "issue-5")
        self.assertEqual(view["criteria"], ["c1 open: first proof", "c2 open: second proof"])
        first = self.entries("issue-5")[0]
        self.assertEqual((first["repository"], first["target"], first["branch"], first["worktree"]),
                         ("acme/widgets", "main", "work", str(self.repo)))
        self.assertNotIn("body", first["source"])       # the issue text stays on GitHub; Pod keeps its digest

    def test_without_a_proof_section_every_unchecked_box_is_a_criterion_and_without_boxes_the_title(self):
        self.issue(6, "Boxes", "- [ ] one\n- [x] done\n- [ ] two\n")
        self.issue(8, "Just a title", "prose only")
        self.assertEqual(commands.open_(self.repo, source="acme/widgets#6")["criteria"], ["c1 open: one", "c2 open: two"])
        self.git(self.repo, "switch", "-q", "-c", "other")
        self.assertEqual(commands.open_(self.repo, source="https://github.com/acme/widgets/issues/8")["criteria"],
                         ["c1 open: Just a title"])

    def test_an_eel_or_seal_is_refused_by_its_title(self):
        for number, title in ((9, "EEL: evidence about widgets"), (10, "SEAL: widget programme")):
            self.issue(number, title, "- [ ] x\n")
            self.refused("document_not_deliverable", commands.open_, self.repo, source=str(number))
        self.assertEqual(commands.listing()["objectives"], [])

    def test_opening_again_with_the_same_source_changes_nothing_and_another_source_is_refused(self):
        self.opened("a", "b")
        before = self.entries("widget")
        self.assertEqual(self.opened("a", "b"), "widget")
        self.assertEqual(self.entries("widget"), before)
        self.refused("objective_exists", commands.open_, self.repo, text="Something else", objective="widget")

    def test_continuing_existing_work_names_its_branch_pull_request_and_run_and_waits_for_foreign_workers(self):
        self.opened()
        started = commands.dispatch(self.repo, None, role="investigate", title="look", spec="Look.")
        run = self.orca()["bound"]["term_one"]
        problem = self.refused("run_busy", commands.open_, self.repo, text="Continue the widget", objective="widget-2",
                               run=run, pr=7, continues="widget")
        self.assertIn(started["dispatch"], str(problem))
        self.finish(started["task"], {"outcome": "succeeded"})
        commands.drop(self.repo, "widget", why="continued as widget-2")
        view = commands.open_(self.repo, text="Continue the widget", objective="widget-2", run=run, pr=7, continues="widget")
        self.assertEqual((view["run"], view["pr"]), (run, 7))
        self.assertEqual(self.entries("widget-2")[0]["continues"], "widget")

    def test_the_list_read_names_what_open_objectives_hold_and_asks_no_provider(self):
        self.opened()
        started = commands.dispatch(self.repo, None, role="investigate", title="look", spec="Look.")
        calls = len(self.gh().get("calls", []))
        self.orca_file.rename(self.tmp / "gone.json")     # Orca is not asked either
        listed = commands.listing()
        self.assertEqual(listed["schema"], "pod-objectives/v1")
        self.assertEqual(listed["objectives"], [{
            "objective": "widget", "repository": "acme/widgets", "worktree": str(self.repo), "branch": "work",
            "run": "run_1", "pr": None, "target": "main", "opened": self.entries("widget")[0]["at"],
            "unaccepted": [{"assignment": "a1", "task": started["task"], "dispatch": started["dispatch"]}]}])
        self.assertEqual(len(self.gh().get("calls", [])), calls)

    def test_closure_is_derived_and_a_dropped_objective_holds_nothing(self):
        self.opened("only")
        commands.record(self.repo, None, met=["c1"], why="seen")
        self.assertEqual(commands.status(self.repo, None, remote=False)["state"], "open")     # not delivered yet
        commands.drop(self.repo, None, why="no longer wanted")
        self.assertEqual(commands.listing()["objectives"], [])
        self.assertFalse(any(entry["kind"] == "close" for entry in self.entries("widget")))
        commands.open_(self.repo, text="Find out why", objective="why", delivery=False)
        commands.record(self.repo, "why", met=["c1"], why="found")
        self.assertEqual(commands.status(self.repo, "why", remote=False)["state"], "closed")

    def test_a_new_conversation_continues_with_one_command(self):
        self.opened()
        commands.dispatch(self.repo, None, role="investigate", title="look", spec="Look.")
        commands.record(self.repo, None, note="waiting for a1; then write the parser")
        import os
        os.environ["FAKE_ORCA_CALLER"] = "term_two"
        view = commands.resume(self.repo, None)
        self.assertEqual(view["note"], "waiting for a1; then write the parser")
        self.assertEqual(self.orca()["runs"]["run_1"]["coordinator_handle"], "term_two")
        takeover = self.entries("widget")[-1]
        self.assertEqual((takeover["fact"], takeover["generation"]), ("takeover", 2))
        self.assertIn("a1 investigate running", view["assignments"][0])
