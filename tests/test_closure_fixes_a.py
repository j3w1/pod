"""Closure audit regressions through production paths and isolated native/provider ports."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.evidence_closure import close_by_evidence, close_source_issues, prove, sweep
from pod.github import GhPort, issue_intake
from pod.governor import _read_journal, _record_path
from pod.installer import STATE_SCHEMAS
from pod.internal import acknowledge, run
from pod.ledger import objective_root, read
from tests.issue_port_support import patch_issue_port
from tests.pod_tick_support import TickCase
from tests import test_completed_closure as completed
from tests.test_evidence_closure import ClosureCase, MERGED, TICKED, _older, _questions


class SourceIntentTests(TickCase):
    def setUp(self):
        super().setUp()
        self.issue = completed.SourcePort()
        replacement = patch_issue_port(self.issue)
        replacement.start(); self.addCleanup(replacement.stop)

    def ready(self):
        self.bind(); self.prove(); self.tick()

    def copy_closed(self, name):
        root = objective_root(self.project, "objective")
        raw = json.loads((root / "context.json").read_text())
        raw["checkpoint"]["objective"] = name
        other = objective_root(self.project, name)
        other.mkdir(parents=True)
        (other / "context.json").write_text(json.dumps(raw))

    def test_source_reopen_is_preserved_by_effect_and_repeated_sweeps(self):
        self.ready()
        self.assertEqual(self.write(close=True)["source_issues"][0]["outcome"], "closed")
        before = self.saved()
        self.issue.state, self.issue.state_reason = "OPEN", "REOPENED"
        effect = close_source_issues(self.project, "objective")["source_issues"][0]
        self.assertEqual((effect["outcome"], effect["reason"]), ("reopened", "source_reopened"))
        self.assertIn("project lead", effect["next_action"])
        self.assertIn("reopen", effect["next_action"])
        for _ in range(2):
            result = sweep(self.project, objectives=["objective"])
            row = result["repositories"][0]["objectives"][0]
            self.assertEqual(row["party"], "lead")
            self.assertEqual(row["source_issues"][0]["reason"], "source_reopened")
            self.assertEqual(_questions(result), [])
            self.assertIn("project lead", result["next_safe_action"])
        self.assertEqual((self.issue.state, self.issue.state_reason), ("OPEN", "REOPENED"))
        self.assertEqual(len(self.issue.closes), 1)
        self.assertEqual(self.saved(), before)

    def test_reopened_source_never_gets_its_first_automatic_close_either(self):
        self.ready()
        self.issue.state_reason = "REOPENED"
        result = self.write(close=True)
        self.assertTrue(result["checkpoint"]["closure"])
        self.assertEqual(result["source_issues"][0]["outcome"], "reopened")
        self.assertEqual(self.issue.closes, [])

    def test_shared_reopened_source_has_one_lead_task_and_one_read_per_sweep(self):
        self.ready(); self.write(close=True); self.copy_closed("another")
        self.issue.state, self.issue.state_reason = "OPEN", "REOPENED"
        before = len(self.issue.calls)
        result = sweep(self.project)
        rows = result["repositories"][0]["objectives"]
        self.assertEqual(sum(row["party"] == "lead" for row in rows), 1)
        self.assertEqual([row["source_issues"][0]["outcome"] for row in rows], ["reopened", "reopened"])
        self.assertEqual(rows[1]["source_issues"][0]["task_with"], "another")
        self.assertEqual(len(self.issue.calls) - before, 1)
        self.assertEqual(len(self.issue.closes), 1)

    def test_older_ordinary_closure_observes_completed_source_without_conversion(self):
        self.ready(); self.write(close=True)
        path = objective_root(self.project, "objective") / "context.json"
        raw = json.loads(path.read_text()); raw["schema"] = _older(STATE_SCHEMAS["context"])
        path.write_text(json.dumps(raw))
        before = self.saved()
        result = sweep(self.project)
        row = result["repositories"][0]["objectives"][0]
        self.assertEqual((row["outcome"], row["party"]), ("already_closed", None))
        self.assertEqual(row["source_issues"][0]["outcome"], "already_completed")
        self.assertIn("nothing else needs the sweep", result["next_safe_action"])
        self.assertEqual(self.saved(), before)
        self.assertEqual(len(self.issue.closes), 1)

    def test_older_ordinary_closure_cannot_authorize_a_new_source_write(self):
        self.ready(); self.issue.close_result = "refused"; self.write(close=True)
        path = objective_root(self.project, "objective") / "context.json"
        raw = json.loads(path.read_text()); raw["schema"] = _older(STATE_SCHEMAS["context"])
        path.write_text(json.dumps(raw))
        self.issue.close_result = "accepted"
        before = self.saved()
        result = sweep(self.project)
        self.assertEqual(result["repositories"][0]["objectives"][0]["source_issues"][0]["reason"], "closure_unverified")
        self.assertEqual(len(self.issue.closes), 1)
        self.assertEqual(self.saved(), before)

    def test_sweep_memoizes_shared_sources_and_parents_and_reports_each_parent_once(self):
        self.bind("- [ ] **PoD#1\n\nSEAL: https://github.com/acme/example/issues/8")
        self.prove(); self.tick()
        original = self.issue.issue
        def issue(**arguments):
            if arguments["number"] == 8:
                return {"number": 8, "title": "SEAL: parent", "body": "Unproved parent", "state": "OPEN"}
            return original(**arguments)
        with patch.object(self.issue, "issue", side_effect=issue):
            self.write(close=True)
            self.copy_closed("another")
            # The two ordinary closed records bind the same completed source and parent.
            seen = []
            def observed(**arguments):
                seen.append(arguments["number"])
                return issue(**arguments)
            with patch.object(self.issue, "issue", side_effect=observed):
                for _ in range(2):
                    seen.clear()
                    result = sweep(self.project)
                    rows = result["repositories"][0]["objectives"]
                    self.assertEqual(sorted(seen), [7, 8])
                    parents = [item for row in rows for item in row["parents_open"]]
                    self.assertEqual(len(parents), 1)
                    self.assertEqual(parents[0]["also_referenced_by"], ["objective"])
                    self.assertEqual(sum(row["party"] == "lead" for row in rows), 1)
        self.assertEqual(len(self.issue.closes), 1)

    def test_bound_seal_source_refresh_keeps_identity_and_refuses_another_issue(self):
        self.issue.title, self.issue.body = "SEAL: acceptance", "- [ ] **AC1"
        self.write([self.criterion(source={"ref": "AC1"})], criteria=["AC1"], governance={"base_ref": "target"})
        source = issue_intake(self.project, self.locator, port=self.issue, completion=True)["source"]
        self.write(criteria=["AC1"], objective_source=source)
        self.issue.body += "\nUpdated acceptance context"
        refreshed = issue_intake(self.project, self.locator, port=self.issue, completion=True)["source"]
        self.write(criteria=["AC1"], objective_source=refreshed)
        self.assertEqual(read(self.project, "objective")["checkpoint"]["objective_source"], refreshed)
        other = issue_intake(self.project, "https://github.com/acme/example/issues/8", port=self.issue,
                             completion=True)["source"]
        before = self.saved()
        self.failure("seal_completion_binding_refused", self.write, criteria=["AC1"], objective_source=other)
        self.assertEqual(self.saved(), before)


class ClaimedSourceTests(ClosureCase):
    def ready_successor(self):
        self.shipped(unknown_start=False)
        rows = self.claimed_direct()
        carried = next(row for row in _read_journal(_record_path(self.project, "successor"))["actions"]
                       if row.get("carried_from"))
        self.remote.pulls[7] = {**MERGED, "head": "f" * 40, "commits": [carried["commit"], "f" * 40]}
        return {"project": str(self.project), "objective": "successor", "owner": "next",
                "value": self.core(obligations=rows, close=True)}

    def test_ordinary_successor_closure_closes_and_acknowledges_predecessor_source(self):
        request = self.ready_successor()
        with patch("pod.governor_effects.GhPort", return_value=self.remote):
            result = acknowledge("checkpoint", request, run("checkpoint", request))
        self.assertTrue(read(self.project, "successor")["checkpoint"]["closure"])
        self.assertIsNone(read(self.project, "successor")["checkpoint"].get("objective_source"))
        self.assertEqual([(row["objective"], row["outcome"]) for row in result["source_issues"]],
                         [("objective", "closed")])
        self.assertEqual(self.remote.issues[("acme/example", 82)]["state"], "CLOSED")
        self.assertEqual(_questions(result), [])
        before = (self.files(), self.files("successor"))
        again = sweep(self.project, objectives=["successor"], remote=self.remote)
        rows = {row["objective"]: row for row in again["repositories"][0]["objectives"]}
        self.assertEqual(rows["objective"]["source_issues"][0]["outcome"], "already_completed")
        self.assertEqual(len([call for call in self.remote.calls if call[0] == "issue_close"]), 1)
        self.assertEqual((self.files(), self.files("successor")), before)

    def test_sweep_retries_predecessor_source_after_ordinary_successor_close(self):
        request = self.ready_successor()
        with patch("pod.governor_effects.GhPort", return_value=self.remote), \
                patch.object(self.remote, "issue_close", return_value={"result": "refused", "status": 403}):
            result = run("checkpoint", request)
        self.assertEqual(result["source_issues"][0]["outcome"], "refused")
        before = (self.files(), self.files("successor"))
        result = sweep(self.project, remote=self.remote)
        rows = {row["objective"]: row for row in result["repositories"][0]["objectives"]}
        self.assertEqual(rows["objective"]["source_issues"][0]["outcome"], "closed")
        self.assertEqual(rows["successor"]["outcome"], "already_closed")
        self.assertEqual((self.files(), self.files("successor")), before)

    def test_tail_record_change_during_predecessor_read_fences_the_write(self):
        request = self.ready_successor()
        with patch("pod.governor_effects.GhPort", return_value=self.remote), \
                patch.object(self.remote, "issue_close", return_value={"result": "refused", "status": 403}):
            run("checkpoint", request)
        original = self.remote.issue
        def concurrent(**arguments):
            result = original(**arguments)
            path = objective_root(self.project, "successor") / "context.json"
            raw = json.loads(path.read_text()); raw["checkpoint"]["closure"] = None
            path.write_text(json.dumps(raw))
            return result
        with patch.object(self.remote, "issue", side_effect=concurrent):
            result = close_source_issues(self.project, "successor", remote=self.remote)
        self.assertEqual(result["source_issues"][0]["reason"], "closure_changed")
        self.assertEqual([call for call in self.remote.calls if call[0] == "issue_close"], [])

    def test_duplicate_fully_ticked_lines_still_prove_evidence_delivery(self):
        self.shipped(unknown_start=False)
        self.remote.issues[("acme/example", 82)]["body"] = TICKED + "\n" + TICKED
        result = prove(self.project, "objective", remote=self.remote)
        self.assertEqual(result["status"], "proved")
        self.assertEqual(result["facts"]["source"]["proof"], "pod_ticks")
        closed = close_by_evidence(self.project, "objective", caller="lead", remote=self.remote)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["source_issues"][0]["reason"], "source_changed")
        self.assertEqual([call for call in self.remote.calls if call[0] == "issue_close"], [])

    def test_evidence_closed_source_reopen_stays_open_in_sweep(self):
        self.shipped(unknown_start=False)
        issue = self.remote.issues[("acme/example", 82)]
        issue.update(state="CLOSED", state_reason="COMPLETED")
        self.assertEqual(close_by_evidence(self.project, "objective", caller="lead", remote=self.remote)["status"], "closed")
        before = self.files()
        issue.update(state="OPEN", state_reason="REOPENED")
        result = sweep(self.project, remote=self.remote)
        row = result["repositories"][0]["objectives"][0]
        self.assertEqual((row["party"], row["source_issues"][0]["reason"]), ("lead", "source_reopened"))
        self.assertEqual(issue["state"], "OPEN")
        self.assertEqual([call for call in self.remote.calls if call[0] == "issue_close"], [])
        self.assertEqual(self.files(), before)


class ForeignLinkedPRTests(unittest.TestCase):
    def test_valid_foreign_closer_is_skipped_even_with_same_pr_number(self):
        response = completed.ClosingPortTests().response()
        nodes = response["data"]["repository"]["issue"]["closedByPullRequestsReferences"]["nodes"]
        foreign = deepcopy(nodes[0])
        foreign["url"] = "https://github.com/consumer/app/pull/9"
        foreign["baseRepository"]["nameWithOwner"] = "consumer/app"
        nodes.insert(0, foreign)
        with patch.object(GhPort, "_gh_json", return_value=response):
            result = GhPort(None).closing_prs(repository="acme/widgets", number=7)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["base_repository"], "acme/widgets")

    def test_foreign_url_with_target_repository_identity_still_refuses(self):
        response = completed.ClosingPortTests().response()
        response["data"]["repository"]["issue"]["closedByPullRequestsReferences"]["nodes"][0]["url"] = \
            "https://github.com/consumer/app/pull/9"
        with patch.object(GhPort, "_gh_json", return_value=response), self.assertRaises(PodError) as caught:
            GhPort(None).closing_prs(repository="acme/widgets", number=7)
        self.assertEqual(caught.exception.code, "gh_contract")


class DeliveryOnlyTests(unittest.TestCase):
    def test_dispositions_do_not_receive_the_delivery_only_exemption(self):
        case, decision = completed.GovernanceCompletionTests.merged(self)
        completed.GovernanceCompletionTests.advance(self, case)
        path = objective_root(case.project, "objective") / "context.json"
        before = path.read_bytes()
        error = case.refusal("checkpoint", value=case.core(obligations=case.stored(),
            delivery={"record": decision["record_id"]},
            dispositions=[{"admission": "unbound-result", "kind": "discard", "reason": "not integrated"}]))
        self.assertEqual(error.code, "governance_changed")
        self.assertEqual(path.read_bytes(), before)
