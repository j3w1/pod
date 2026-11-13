from pod import commands, gate, proof
from pod.state import fold
from tests.core.support import CoreCase

REQUIRED = "schema: pod-project/v1\nreview: required\n"


class ProofTests(CoreCase):
    def setUp(self):
        super().setUp()
        self.project(REQUIRED)
        self.opened()

    def uncovered(self, **rules):
        state = fold(self.entries("widget"))
        head = self.git(self.repo, "rev-parse", "HEAD")
        base = self.git(self.repo, "merge-base", "HEAD", "origin/main")
        return proof.coverage(self.repo, state, head, base, **rules)["uncovered"]

    def test_a_candidate_is_reviewed_when_every_changed_path_has_an_applicable_passing_review(self):
        self.implement({"src/a.py": "a\n", "src/b.py": "b\n"})
        self.assertEqual(self.uncovered(), ["src/a.py", "src/b.py"])
        self.review("pass", paths_=["src/a.py"])
        self.assertEqual(self.uncovered(), ["src/b.py"])
        self.review("fail", paths_=["src/b.py"])
        self.assertEqual(self.uncovered(), ["src/b.py"])
        self.review("pass", paths_=["src/b.py"])
        self.assertEqual(self.uncovered(), [])

    def test_a_delta_review_composes_with_an_earlier_full_review_without_any_declaration(self):
        self.implement({"src/a.py": "a\n", "src/b.py": "b\n", "docs/c.md": "c\n"})
        self.review("pass")
        self.implement({"src/b.py": "b corrected\n"})
        self.assertEqual(self.uncovered(), ["src/b.py"])                   # only the path whose content changed
        self.review("pass", paths_=["src/b.py"])
        self.assertEqual(self.uncovered(), [])
        self.assertEqual({entry["kind"] for entry in self.entries("widget")},
                         {"objective", "criterion", "assignment", "fact"})

    def test_evidence_never_qualifies_different_content(self):
        self.implement({"src/a.py": "a\n"})
        self.review("pass")
        self.commit({"src/a.py": "changed after the review\n"})
        self.assertEqual(self.uncovered(), ["src/a.py"])
        self.commit({"src/a.py": "a\n"})                                    # the reviewed content again
        self.assertEqual(self.uncovered(), [])

    def test_merging_the_target_reopens_only_paths_whose_content_changed_in_the_candidate(self):
        self.implement({"src/a.py": "a\n", "src/b.py": "b\n"})
        self.review("pass")
        self.git(self.repo, "switch", "-q", "main")
        self.commit({"README.md": "the target moved\n", "other.txt": "new\n"})
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "switch", "-q", "work")
        self.git(self.repo, "merge", "-q", "--no-edit", "main")
        self.assertEqual(self.uncovered(), [])
        self.assertEqual(self.uncovered(final_tree=True), ["src/a.py", "src/b.py"])   # a project may ask for the final tree
        self.review("pass")
        self.assertEqual(self.uncovered(final_tree=True), [])

    def test_a_review_lapses_when_a_context_path_it_named_changes(self):
        self.implement({"src/a.py": "a\n", "schema.json": "{}\n"})
        self.review("pass", paths_=["src/a.py"], context=["schema.json"])
        self.assertEqual(self.uncovered(), ["schema.json"])
        self.commit({"schema.json": '{"changed": true}\n'})
        self.assertEqual(self.uncovered(), ["schema.json", "src/a.py"])

    def test_a_deleted_path_is_a_changed_path_and_can_be_reviewed(self):
        self.implement({"README.md": None})
        self.assertEqual(self.uncovered(), ["README.md"])
        self.review("pass")
        self.assertEqual(self.uncovered(), [])

    def test_a_reviewer_who_wrote_and_a_review_of_a_commit_that_is_gone_cover_nothing(self):
        self.implement({"src/a.py": "a\n"})
        started = commands.dispatch(self.repo, None, role="review", title="r", spec="Review.")
        edited = self.commit({"src/b.py": "added by the reviewer\n"})      # the reviewed file itself is untouched
        self.finish(started["task"], {"outcome": "succeeded", "verdict": "pass", "commit": edited})
        commands.accept(self.repo, None, "a2")
        self.assertEqual(self.uncovered(), ["src/a.py", "src/b.py"])
        commands.record(self.repo, None, carried="an earlier record", commit="HEAD", paths=["src/a.py", "src/b.py"],
                        verdict="pass")
        self.assertEqual(self.uncovered(), [])
        state = fold(self.entries("widget"))
        state.of("review")[-1]["commit"] = "0" * 40
        head, base = self.git(self.repo, "rev-parse", "HEAD"), self.git(self.repo, "merge-base", "HEAD", "origin/main")
        self.assertEqual(proof.coverage(self.repo, state, head, base)["uncovered"], ["src/a.py", "src/b.py"])

    def test_a_required_finding_stays_open_until_a_later_review_of_its_path_passes_or_it_is_waived(self):
        self.implement({"src/a.py": "a\n", "src/b.py": "b\n"})
        self.review("fail", [{"path": "src/a.py", "required": True, "text": "wrong"},
                             {"path": "src/b.py", "required": True, "text": "also wrong"},
                             {"path": "src/b.py", "required": False, "text": "style"}])
        opened = lambda: [row["id"] for row in proof.open_required(self.repo, fold(self.entries("widget")), self.git(self.repo, "rev-parse", "HEAD"))]
        self.assertEqual(opened(), ["a2.1", "a2.2"])
        self.implement({"src/a.py": "a fixed\n"})
        self.assertEqual(opened(), ["a2.1", "a2.2"])                        # a correction alone closes nothing
        self.review("pass", paths_=["src/a.py"])
        self.assertEqual(opened(), ["a2.2"])
        self.refused("person_decides", commands.record, self.repo, None, waive="a2.2", why="accepted risk")
        self.refused("waiver_invalid", commands.record, self.repo, None, waive="a9.9", why="x", words="ok")
        commands.record(self.repo, None, waive="a2.2", why="accepted for this release", words="waive it")
        self.assertEqual(opened(), [])

    def test_a_check_applies_only_to_the_same_tree(self):
        commands.record(self.repo, None, check="true")
        tree = self.git(self.repo, "rev-parse", "HEAD^{tree}")
        state = fold(self.entries("widget"))
        self.assertEqual([fact["exit"] for fact in proof.checks(state, tree)], [0])
        self.commit({"x": "x\n"})
        self.assertEqual(proof.checks(state, self.git(self.repo, "rev-parse", "HEAD^{tree}")), [])

    def test_existing_line_and_column_findings_close_without_rewriting_evidence(self):
        self.implement({"f.mjs": "bad\n", "colon:name.mjs": "bad\n"})
        self.review("fail", [{"path": p, "required": True, "text": "wrong"}
                             for p in ("f.mjs:111", "f.mjs:111:2", "colon:name.mjs:2:3")])
        self.implement({"f.mjs": "fixed\n", "colon:name.mjs": "fixed\n"})
        self.review("pass", paths_=["f.mjs", "colon:name.mjs"])
        before = self.entries("widget")
        self.assertEqual(proof.open_required(self.repo, fold(before), self.git(self.repo, "rev-parse", "HEAD")), [])
        self.assertEqual(self.entries("widget"), before)

    def test_recorded_locations_resolve_only_after_reviewing_current_corrected_content(self):
        self.implement({"src/a.py": "bad\n", "src/b.py": "b\n"})
        locations = ["src/a.py", "src/a.py:111", "src/a.py:111:2"]
        self.review("fail", [{"path": p, "required": True, "text": "wrong"} for p in locations])
        recorded = self.entries("widget")
        rows = lambda: proof.findings(self.repo, fold(self.entries("widget")), self.git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(self.uncovered(), ["src/a.py"])
        self.review("pass", paths_=["src/a.py"])
        self.assertTrue(all(row["open"] for row in rows()))  # a second opinion is no correction
        self.implement({"src/a.py": "fixed\n"})
        self.review("pass", paths_=["src/b.py"])
        self.assertTrue(all(row["open"] for row in rows()))
        self.review("fail", [{"path": "src/a.py:112:3", "required": True, "text": "still wrong"}])
        self.assertTrue(all(row["open"] for row in rows()))
        self.assertEqual(self.uncovered(), ["src/a.py"])
        self.implement({"src/a.py": "really fixed\n"})
        self.review("pass", paths_=["src/a.py"])
        self.assertTrue(all(not row["open"] for row in rows()))
        self.assertEqual([row["path"] for row in rows()][:3], locations)
        self.assertEqual(self.entries("widget")[:len(recorded)], recorded)  # old facts stay untouched
        self.assertFalse(any("finding " in need for need in gate.evaluate(
            self.repo, fold(self.entries("widget")), remote=False)["needs"]))
        self.commit({"src/a.py": "bad\n"})
        self.assertTrue(all(row["open"] for row in rows()))

    def test_unknown_malformed_and_ambiguous_locations_do_not_resolve(self):
        self.implement({"src/a.py": "bad\n", "src/a.py:111": "literal\n"})
        locations = ["missing:1", "src/a.py:0", "src/a.py:-1", "src/a.py:1:x",
                     "src/a.py:1:2:3", "src/a.py:111:2"]  # last could name either tracked file
        self.review("fail", [{"path": p, "required": True, "text": "wrong"} for p in locations])
        self.implement({"src/a.py": "fixed\n", "src/a.py:111": "literal fixed\n"})
        self.review("pass")
        opened = proof.open_required(self.repo, fold(self.entries("widget")), self.git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual([row["path"] for row in opened], locations)

    def test_a_tracked_colon_filename_keeps_its_identity(self):
        self.implement({"src/a.py": "bad\n", "src/a.py:111": "literal bad\n"})
        self.review("fail", [{"path": "src/a.py:111", "required": True, "text": "wrong literal"}])
        self.assertEqual(self.uncovered(), ["src/a.py:111"])
        self.implement({"src/a.py": "fixed\n"})
        self.review("pass", paths_=["src/a.py"])
        state = lambda: fold(self.entries("widget"))
        head = lambda: self.git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(len(proof.open_required(self.repo, state(), head())), 1)
        self.implement({"src/a.py:111": "literal fixed\n"})
        self.review("pass", paths_=["src/a.py:111"])
        self.assertEqual(proof.open_required(self.repo, state(), head()), [])
        self.commit({"src/a.py:111": None})
        self.review("pass", paths_=["src/a.py"])
        self.assertEqual(len(proof.open_required(self.repo, state(), head())), 1)

    def test_a_plain_path_finding_can_still_be_corrected_by_deleting_the_file(self):
        self.implement({"README.md": "bad\n"})
        self.review("fail", [{"path": "README.md", "required": True, "text": "remove this"}])
        self.implement({"README.md": None})
        self.review("pass", paths_=["README.md"])
        self.assertEqual(proof.open_required(self.repo, fold(self.entries("widget")),
                                             self.git(self.repo, "rev-parse", "HEAD")), [])

    def test_a_check_run_on_uncommitted_changes_binds_to_no_commit_and_a_failure_is_recorded_as_one(self):
        (self.repo / "scratch.txt").write_text("not committed\n")
        output = commands.record(self.repo, None, check="exit 3")
        self.assertIn("binds to no commit", output["recorded"][0])
        fact = self.entries("widget")[-1]
        self.assertEqual((fact["exit"], fact["source"]), (3, "run by pod"))
        self.assertNotIn("tree", fact)

    def test_a_criterion_can_rest_on_the_check_the_same_call_ran_and_never_on_a_failed_one(self):
        output = commands.record(self.repo, None, check="true", met=["c1"])
        check, criterion = self.entries("widget")[-2:]
        self.assertEqual((check["fact"], check["exit"], criterion["state"], criterion["facts"]), ("check", 0, "met", [check["n"]]))
        self.assertEqual(output["recorded"], ["check exit 0", "c1 met"])
        commands.record(self.repo, None, add_criterion=["second"])
        self.refused("check_failed", commands.record, self.repo, None, check="exit 4", met=["c2"])
        failed = self.entries("widget")[-1]
        self.assertEqual((failed["fact"], failed["exit"]), ("check", 4))
        self.assertEqual(fold(self.entries("widget")).criteria["c2"]["state"], "open")
        (self.repo / "stray.txt").write_text("not committed\n")
        self.refused("check_unbound", commands.record, self.repo, None, check="true", met=["c2"])
        self.assertEqual(fold(self.entries("widget")).criteria["c2"]["state"], "open")

    def test_files_a_check_leaves_behind_do_not_unbind_it_and_a_changed_tracked_file_does(self):
        commands.record(self.repo, None, check="mkdir -p cache && echo x > cache/left")
        self.assertEqual(self.entries("widget")[-1]["commit"], self.git(self.repo, "rev-parse", "HEAD"))
        self.git(self.repo, "clean", "-qfd")
        commands.record(self.repo, None, check="echo changed > README.md")
        self.assertNotIn("commit", self.entries("widget")[-1])

    def test_the_gate_names_uncovered_paths_and_open_findings(self):
        self.implement({"src/a.py": "a\n"})
        self.review("fail", [{"path": "src/a.py", "required": True, "text": "wrong"}])
        needs = gate.evaluate(self.repo, fold(self.entries("widget")), remote=False)["needs"]
        self.assertTrue(any("independent passing review of 1 changed paths: src/a.py" in need for need in needs), needs)
        self.assertTrue(any("finding a2.1" in need for need in needs), needs)
