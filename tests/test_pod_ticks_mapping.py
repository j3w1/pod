"""R9 / A-C and D1-D4: real introducing checkpoints, tick and status entries."""

from copy import deepcopy

from pod.ledger import read
from tests.pod_tick_support import TickCase


class MappingTests(TickCase):
    def test_introducing_checkpoint_reports_every_mapping_case_and_exact_reference(self):
        body = ("- [ ] **PoD#1\r\n- [x] **PoD#10 — tenth\r\n- [X] **PoD#2\r\n"
                "- [ ] **PoD#2 duplicate\r\n- [ ] **PoD#4 unmapped\r\n"
                "> - [ ] **PoD#5\r\n - [ ] **PoD#6\r\n"
                "```markdown\r\n- [ ] **PoD#7\r\n```\r\n~~~\r\n- [ ] **PoD#8\r\n~~~\r\n")
        extra = self.criterion("waiting", id="O2", source={"ref": "PoD#1 extra"},
                               wait={"class": "sequenced", "referent": "O1"})
        missing = self.criterion("waiting", id="O3", source={"ref": "PoD#3"},
                                 wait={"class": "sequenced", "referent": "O1"})
        tenth = self.criterion("waiting", id="O10", source={"ref": "PoD#10"},
                               wait={"class": "sequenced", "referent": "O1"})
        result = self.bind(body, extra, missing, tenth, criteria=["PoD#1", "PoD#1 extra", "PoD#3", "PoD#10"])
        self.assertEqual(result["pod_tick_intake"], {"status": "read", "unmapped": [2, 4],
            "multiply_mapped": [1], "duplicated": [2], "missing": [{"pod": 3, "criteria": ["O3"]}]})
        self.assertEqual(self.issue.calls, [("issue", "acme/example", 7)])
        self.assertEqual([row["pod"] for row in self.ticks()["lines"]], [1, 2, 3, 4, 10])
        self.assertEqual(self.line()["criteria"][0]["id"], "O1")
        self.assertEqual(self.ack(result)["pod_tick_intake"], result["pod_tick_intake"])

    def test_direct_objective_is_not_applicable_A1(self):
        result = self.intake()
        self.assertEqual(result["pod_ticks"], {"status": "not_applicable", "lines": []})
        self.assertEqual(self.tick(), {"status": "not_applicable"})
        self.assertEqual(self.issue.calls, [])

    def test_unmapped_never_due_even_when_every_mapped_line_proven_D1(self):
        self.bind("- [ ] **PoD#1\n- [ ] **PoD#2")
        self.prove()
        self.failure("unmapped", self.tick, 2)
        self.assertEqual(self.line(2)["blocked"]["reason"], "unmapped")
        self.assertFalse(self.line(2)["due"])
        self.assertEqual(self.issue.writes, [])

    def test_lost_last_mapping_unticked_is_unmapped_D2(self):
        """Synthetic mapping control; R149 revision/observation tests cover actual loss."""
        self.bind()
        self.lapse(remove_mapping=True)
        self.failure("unmapped", self.tick)
        self.assertFalse(self.line()["due"])
        self.assertEqual(self.line()["status"], "blocked")
        self.assertEqual(self.issue.writes, [])

    def test_two_criteria_require_both_current_proofs_D3_D4(self):
        second = self.criterion("waiting", id="O2", source={"ref": "PoD#1 extra"},
                                wait={"class": "sequenced", "referent": "O1"})
        self.bind("- [ ] **PoD#1", second, criteria=["PoD#1", "PoD#1 extra"])
        rows = self.stored(); rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        rows[1].pop("wait")
        rows[1].update(state="active", executor="coordinator")
        self.write(rows)
        self.assertEqual(self.tick()["status"], "not_proven")
        self.assertEqual(self.line()["status"], "not_proven")
        self.prove(); self.tick()
        self.assertEqual(self.issue.writes, ["- [x] **PoD#1"])

    def test_missing_line_never_written_C2(self):
        self.bind("PES without checkboxes")
        self.prove()
        self.failure("line_missing", self.tick)
        self.assertEqual(self.line()["blocked"]["code"], "line_missing")
        self.assertFalse(self.line()["due"])
        self.assertEqual(self.issue.writes, [])

    def test_duplicated_line_never_written_C3(self):
        self.bind("- [ ] **PoD#1\n- [X] **PoD#1 duplicate")
        self.prove()
        self.failure("line_duplicated", self.tick)
        self.assertEqual(self.line()["occurrences"], 2)
        self.assertEqual(self.line()["checkboxes"], [False, True])
        self.assertEqual(self.line()["blocked"]["code"], "line_duplicated")
        self.assertEqual(self.issue.writes, [])

    def test_unreadable_intake_reports_not_read_without_refusing(self):
        self.issue.unreadable = True
        # Intake source was obtained earlier; only the checkpoint read fails.
        from pod.github import issue_intake
        self.issue.unreadable = False
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.issue.unreadable = True
        result = self.intake(objective_source=source)
        self.assertEqual(result["pod_tick_intake"], {"status": "not_read", "reason": "issue_access_unavailable"})
        self.assertIsNone(read(self.project, "objective")["pod_ticks"]["facts"])
        self.prove()
        self.assertTrue(self.line()["due"])

    def test_mismatched_intake_does_not_record_unbound_line_facts(self):
        from pod.github import issue_intake
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.issue.body += " changed"
        result = self.intake(objective_source=source)
        self.assertEqual(result["pod_tick_intake"]["status"], "not_read")
        self.assertIsNone(read(self.project, "objective")["pod_ticks"]["facts"])

    def test_reconciled_checkpoint_refreshes_mapping_and_clears_observed_authorship(self):
        self.bind(); self.prove(); self.tick()
        self.issue.body = "- [ ] **PoD#1\n- [ ] **PoD#2"
        result = self.rebind()
        self.assertEqual(result["pod_tick_intake"]["unmapped"], [2])
        self.assertEqual(self.line()["authorship"], "not_pod")
        self.assertTrue(self.line()["due"])

    def test_stale_line_facts_keep_proven_lines_possibly_due(self):
        self.bind(); self.prove()
        from pod.github import issue_intake
        self.issue.body += "\n- [ ] **PoD#2"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.issue.unreadable = True
        self.write(objective_source=source)
        self.assertFalse(self.line()["facts_current"])
        self.assertTrue(self.line()["due"])
        self.tick()
        self.assertTrue(self.line()["ticked"])

    def test_withdrawn_mapping_is_not_proven(self):
        self.bind(); self.prove(); self.lapse(withdraw=True)
        self.assertFalse(self.line()["proven"])
        self.assertEqual(self.line()["criteria"][0]["state"], "withdrawn")
        self.assertEqual(self.tick()["status"], "not_proven")
        self.assertEqual(self.issue.writes, [])
