from tests.common import logical_json
"""Completed bookkeeping uses proof; provider effects are isolated, exact and retryable."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.evidence_closure import close_by_evidence, close_source_issues, prove, sweep
from pod.github import CLOSING_PRS_QUERY, GhPort, gh_allowed, issue_intake, pod_lines, pod_ref_number
from pod.internal import acknowledge
from pod.ledger import objective_root, read
from tests.issue_port_support import RecordingIssuePort, SimulatedInterruption, patch_issue_port
from tests.pod_tick_support import TickCase
from tests.test_evidence_closure import ClosureCase, MERGED
from tests import test_evidence_closure as evidence, test_pod_ticks_closure as tick_closure
from tests.test_github import completed, issue_binding, issue_patch
from tests.kernel_support import git


class ClosingPortTests(unittest.TestCase):
    def query(self):
        return ["api", "graphql", "-f", "query=" + CLOSING_PRS_QUERY, "-f", "owner=acme",
                "-f", "name=widgets", "-F", "number=7"]

    def response(self):
        return {"data": {"repository": {"nameWithOwner": "acme/widgets", "issue": {
            "number": 7, "url": "https://github.com/acme/widgets/issues/7",
            "closedByPullRequestsReferences": {"nodes": [{"number": 9,
                "url": "https://github.com/acme/widgets/pull/9", "state": "MERGED",
                "mergedAt": "2026-10-23T00:00:00Z", "mergeCommit": {"oid": "a" * 40},
                "baseRefName": "main", "baseRepository": {"nameWithOwner": "acme/widgets"}}],
                "pageInfo": {"hasNextPage": False}}}}}}

    def test_only_the_fixed_query_and_exact_bound_completion_patch_are_allowed(self):
        query = self.query()
        self.assertTrue(gh_allowed(query))
        for changed in ([*query, "--paginate"], [*query[:3], query[3] + " mutation{}", *query[4:]],
                        [*query[:5], "owner=other/name", *query[6:]]):
            self.assertFalse(gh_allowed(changed))
        payload = json.dumps({"state": "closed", "state_reason": "completed"})
        self.assertTrue(gh_allowed(issue_patch(), bound_issue=("acme/widgets", 7), stdin=payload))
        for data in ({"state": "open"}, {"state": "closed", "state_reason": "not_planned"},
                     {"state": "closed", "state_reason": "completed", "body": "changed"}):
            self.assertFalse(gh_allowed(issue_patch(), bound_issue=("acme/widgets", 7), stdin=json.dumps(data)))
        self.assertFalse(gh_allowed(issue_patch(number=8), bound_issue=("acme/widgets", 7), stdin=payload))
        with patch.object(GhPort, "_gh", return_value=completed("HTTP/2.0 200 OK\r\n\r\n{}")) as command:
            self.assertEqual(GhPort(None).issue_close(source=issue_binding())["result"], "accepted")
        self.assertEqual(json.loads(command.call_args.kwargs["stdin"]), {"state": "closed", "state_reason": "completed"})

    def test_linked_pr_identity_and_pagination_fail_closed(self):
        with patch.object(GhPort, "_gh_json", return_value=self.response()):
            self.assertEqual(GhPort(None).closing_prs(repository="acme/widgets", number=7)[0]["merge_commit"], "a" * 40)
        for field in ("repository", "issue", "url", "oid", "pagination", "count"):
            with self.subTest(field=field):
                data = self.response(); repo = data["data"]["repository"]; issue = repo["issue"]
                connection = issue["closedByPullRequestsReferences"]; pr = connection["nodes"][0]
                if field == "repository": repo["nameWithOwner"] = "other/widgets"
                elif field == "issue": issue["number"] = 8
                elif field == "url": pr["url"] = "https://github.com/other/widgets/pull/9"
                elif field == "oid": pr["mergeCommit"]["oid"] = "unknown"
                elif field == "pagination": connection["pageInfo"]["hasNextPage"] = True
                else: connection["nodes"] *= 17
                with patch.object(GhPort, "_gh_json", return_value=data), self.assertRaises(PodError) as error:
                    GhPort(None).closing_prs(repository="acme/widgets", number=7)
                self.assertEqual(error.exception.code, "scope_too_large" if field in ("pagination", "count") else "gh_contract")


class LinkedClosureTests(ClosureCase):
    def linked(self):
        self.shipped(pr=False, unknown_start=False)
        self.remote.linked[("acme/example", 82)] = [{"number": 9, "url": "https://github.com/acme/example/pull/9",
            "base_repository": "acme/example", "base_branch": "target", **MERGED}]

    def test_github_only_proof_closes_without_inventing_a_delivery_record(self):
        self.linked()
        before = self.files()
        result = close_by_evidence(self.project, "objective", caller="fixture")
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.closure()["delivery"][0]["proof"], "github_link")
        self.assertIsNone(read(self.project, "objective")["checkpoint"].get("delivery"))
        self.assertEqual(self.status()["readiness"]["state"], "closed")

    def test_wrong_linked_target_is_a_lead_task(self):
        self.linked()
        self.remote.linked[("acme/example", 82)][0]["base_branch"] = "elsewhere"
        result = prove(self.project, "objective")
        self.assertEqual(result["status"], "not_proved")
        self.assertIn("delivery_unrecorded", [row["code"] for row in result["conditions"]])
        self.assertIn("project lead", self.status()["next_action"])

    def test_old_record_without_target_identity_is_unavailable(self):
        self.linked()
        self.rewrite(change=lambda raw: raw["checkpoint"]["governance"].pop("base_ref"))
        # A genuine documented-field older record cannot borrow today's default branch.
        from tests.test_evidence_closure import _older
        from pod.installer import STATE_SCHEMAS
        self.rewrite(schema=_older(STATE_SCHEMAS["context"]))
        result = prove(self.project, "objective")
        self.assertIn("target_identity_unavailable", [row["code"] for row in result["conditions"]])

    def test_recorded_unmerged_pr_keeps_its_check_despite_a_merged_link(self):
        self.shipped(merged=False, unknown_start=False)
        self.remote.linked[("acme/example", 82)] = [{"number": 9, "base_repository": "acme/example",
                                                   "base_branch": "target", **MERGED}]
        result = prove(self.project, "objective")
        self.assertIn("pr_not_merged", [row["code"] for row in result["conditions"]])
        self.assertFalse(any(row[0] == "closing_prs" for row in self.remote.calls))

    def test_chain_proves_every_source_including_the_predecessor(self):
        self.linked()
        self.remote.issues[("acme/example", 83)] = {"body": evidence.TICKED, "state": "OPEN"}
        self.source = issue_intake(self.project, "https://github.com/acme/example/issues/83", port=self.remote)["source"]
        self.remote.linked[("acme/example", 83)] = deepcopy(self.remote.linked[("acme/example", 82)])
        self.successor_bound(); self.terminals["next"] = "stale"
        self.remote.issues[("acme/example", 82)]["body"] = evidence.UNTICKED
        result = prove(self.project, "successor")
        conditions = [row for row in result["conditions"] if row["code"] == "source_unproved"]
        self.assertTrue(any("issues/82" in row["message"] for row in conditions), result)
        self.assertIsNone(self.closure("successor"))

    def test_interrupted_chain_source_effects_retry_only_the_remaining_issue(self):
        self.linked()
        self.remote.issues[("acme/example", 83)] = {"body": evidence.TICKED, "state": "OPEN"}
        self.source = issue_intake(self.project, "https://github.com/acme/example/issues/83", port=self.remote)["source"]
        self.remote.linked[("acme/example", 83)] = deepcopy(self.remote.linked[("acme/example", 82)])
        self.successor_bound(); self.terminals["next"] = "stale"
        writes, interrupted = [], [True]
        def close(*, source):
            writes.append(source["number"])
            if source["number"] == 82 and interrupted[0]:
                interrupted[0] = False
                raise SimulatedInterruption()
            self.remote.issues[(source["repository"], source["number"])].update(state="CLOSED", state_reason="COMPLETED")
            return {"result": "accepted", "status": 200}
        self.remote.issue_close = close
        with self.assertRaises(SimulatedInterruption):
            close_by_evidence(self.project, "successor", caller="fixture")
        self.assertIsNotNone(self.closure()); self.assertIsNotNone(self.closure("successor"))
        before = self.everything()
        result = close_by_evidence(self.project, "successor", caller="fixture")
        self.assertEqual([row["outcome"] for row in result["source_issues"]], ["already_completed", "closed"])
        self.assertEqual(writes, [83, 82, 82])
        self.assertEqual(self.everything(), before)

    def test_unproved_retirement_detection_names_all_conditions_and_optional_abandonment(self):
        self.shipped(pr=False, ticked=False, unknown_start=False)
        found = self.retire()
        row = found["objectives"][0]
        self.assertEqual(row["party"], "lead")
        self.assertEqual({item["code"] for item in row["completion"]["conditions"]},
                         {"source_unproved", "delivery_unrecorded"})
        self.assertTrue(found["next_action"].startswith("The project lead"))
        self.assertTrue(found["question"].startswith("Optional abandonment:"))
        self.assertNotIn("delivered history", found["question"])


class SourcePort(RecordingIssuePort):
    def __init__(self):
        super().__init__()
        self.closes = []
        self.close_result = "accepted"
        self.close_hook = None
        self.reopened_at = "2099-01-01T00:00:00Z"
        self.reopen_reads = []

    def issue_reopened_at(self, *, repository, number):
        self.reopen_reads.append((repository, number))
        return self.reopened_at

    def issue_close(self, *, source):
        self.closes.append(deepcopy(source))
        if self.close_hook:
            self.close_hook()
        if self.close_result == "refused":
            return {"result": "refused", "status": 403, "message": "fixture permission refusal"}
        if self.close_result != "lost_not_landed":
            self.state, self.state_reason = "CLOSED", "COMPLETED"
        if self.close_result == "interrupted":
            raise SimulatedInterruption()
        if self.close_result.startswith("lost"):
            raise PodError("remote_effect_uncertain", "fixture lost response")
        return {"result": "accepted", "status": 200}


class SourceClosingTests(TickCase):
    def setUp(self):
        super().setUp()
        self.issue = SourcePort()
        patched = patch_issue_port(self.issue)
        patched.start(); self.addCleanup(patched.stop)

    def ready(self):
        self.bind(); self.prove(); self.tick()

    def test_checkpoint_closes_source_and_sweep_replays_without_rewriting_pod(self):
        self.ready()
        result = self.write(close=True)
        self.assertEqual(self.ack(result)["source_issues"][0]["outcome"], "closed")
        before = self.saved()
        again = close_source_issues(self.project, "objective")
        self.assertEqual(again["source_issues"][0]["outcome"], "already_completed")
        swept = sweep(self.project, objectives=["objective"])
        self.assertEqual(swept["repositories"][0]["objectives"][0]["source_issues"][0]["outcome"], "already_completed")
        self.assertEqual(self.saved(), before)
        self.assertEqual(len(self.issue.closes), 1)

    def test_refused_close_is_acknowledged_and_sweep_retries_it(self):
        self.ready(); self.issue.close_result = "refused"
        closed = self.ack(self.write(close=True))
        self.assertEqual(closed["source_issues"][0]["outcome"], "refused")
        self.assertTrue(read(self.project, "objective")["checkpoint"]["closure"])
        before = self.saved(); self.issue.close_result = "accepted"
        result = sweep(self.project, objectives=["objective"])
        self.assertEqual(result["repositories"][0]["objectives"][0]["source_issues"][0]["outcome"], "closed")
        self.assertEqual(self.saved(), before)

    def test_lost_close_response_uses_readback_and_never_resends_a_completed_issue(self):
        self.ready(); self.issue.close_result = "lost_landed"
        row = self.write(close=True)["source_issues"][0]
        self.assertEqual((row["outcome"], row["write"]["result"]), ("closed", "unconfirmed"))
        self.assertEqual(close_source_issues(self.project, "objective")["source_issues"][0]["outcome"], "already_completed")
        self.assertEqual(len(self.issue.closes), 1)

    def test_unlanded_close_is_unknown_then_retried_by_sweep(self):
        self.ready(); self.issue.close_result = "lost_not_landed"
        self.assertEqual(self.write(close=True)["source_issues"][0]["outcome"], "unknown")
        self.issue.close_result = "accepted"
        self.assertEqual(sweep(self.project)["repositories"][0]["objectives"][0]["source_issues"][0]["outcome"], "closed")

    def test_reopened_or_unclosed_objective_cannot_send_a_source_effect(self):
        self.ready()
        self.assertEqual(close_source_issues(self.project, "objective")["source_issues"][0]["reason"], "objective_not_closed")
        self.issue.close_result = "refused"; self.write(close=True)
        self.write(reopen=True, revision_authority={"provenance": "user_direct", "instruction": "Reopen the objective"})
        before = len(self.issue.closes)
        self.assertEqual(close_source_issues(self.project, "objective")["source_issues"][0]["reason"], "objective_not_closed")
        self.assertEqual(len(self.issue.closes), before)

    def test_source_changed_during_immediate_read_prevents_the_effect(self):
        self.ready(); self.issue.close_result = "refused"; self.write(close=True)
        original = self.issue.issue
        def concurrent(**arguments):
            result = original(**arguments)
            path = objective_root(self.project, "objective") / "context.json"
            raw = logical_json(path); raw["checkpoint"]["closure"] = None
            path.write_text(json.dumps(raw))
            return result
        before = len(self.issue.closes)
        with patch.object(self.issue, "issue", side_effect=concurrent):
            result = close_source_issues(self.project, "objective")
        self.assertEqual(result["source_issues"][0]["reason"], "closure_changed")
        self.assertEqual(len(self.issue.closes), before)

    def test_source_material_change_and_closed_other_reason_never_write(self):
        self.ready(); self.issue.body += "\nChanged scope"
        result = self.write(close=True)
        self.assertEqual(result["source_issues"][0]["reason"], "source_changed")
        self.issue.state, self.issue.state_reason = "CLOSED", "NOT_PLANNED"
        result = close_source_issues(self.project, "objective")
        self.assertEqual(result["source_issues"][0]["outcome"], "closed_other_reason")
        self.assertEqual(self.issue.closes, [])

    def test_empty_recognized_criteria_never_close_vacuously(self):
        self.bind("No recognized checkbox criteria"); self.prove()
        self.assertEqual(self.write(close=True)["source_issues"][0]["reason"], "source_unproved")
        self.assertEqual(self.issue.closes, [])

    def test_duplicate_ticked_criteria_still_refuse_source_completion(self):
        self.bind("- [x] **PoD#1\n- [x] **PoD#1"); self.prove()
        self.assertEqual(self.write(close=True)["source_issues"][0]["reason"], "source_unproved")
        self.assertEqual(self.issue.closes, [])

    def test_malformed_ordinary_closure_cannot_authorize_an_issue_effect(self):
        self.ready(); self.issue.close_result = "refused"; self.write(close=True)
        path = objective_root(self.project, "objective") / "context.json"
        raw = logical_json(path); raw["checkpoint"]["closure"]["report"] = {}
        path.write_text(json.dumps(raw))
        before = len(self.issue.closes)
        row = close_source_issues(self.project, "objective")["source_issues"][0]
        self.assertEqual(row["outcome"], "unverified")
        self.assertEqual(len(self.issue.closes), before)

    def test_seal_completion_binding_cannot_start_a_new_objective(self):
        self.issue.title, self.issue.body = "SEAL: acceptance", "- [ ] **AC1"
        source = issue_intake(self.project, self.locator, port=self.issue, completion=True)["source"]
        self.failure("seal_completion_binding_refused", self.write,
                     [self.criterion(source={"ref": "AC1"})], criteria=["AC1"], objective_source=source)
        self.assertIsNone(read(self.project, "objective"))

    def test_an_issue_bound_objective_cannot_rebind_to_a_seal(self):
        self.bind(); before = self.saved()
        self.issue.title, self.issue.body = "SEAL: acceptance", "- [ ] **AC1"
        source = issue_intake(self.project, self.locator, port=self.issue, completion=True)["source"]
        self.failure("seal_completion_binding_refused", self.write, objective_source=source)
        self.assertEqual(self.saved(), before)

    def test_referenced_seal_and_eel_stay_named_lead_tasks(self):
        self.bind("- [ ] **PoD#1\n\nSEAL: https://github.com/acme/example/issues/8\nEEL: https://github.com/acme/example/issues/9")
        self.prove(); self.tick()
        original = self.issue.issue
        def read_issue(**arguments):
            if arguments["number"] in (8, 9):
                return {"number": arguments["number"], "title": "SEAL: parent" if arguments["number"] == 8 else "EEL: evidence",
                        "body": "Unproved parent", "state": "OPEN"}
            return original(**arguments)
        with patch.object(self.issue, "issue", side_effect=read_issue):
            result = self.write(close=True)
        self.assertEqual([row["role"] for row in result["parents_open"]], ["SEAL", "EEL"])
        self.assertTrue(all(row["party"] == "lead" for row in result["parents_open"]))
        self.assertEqual(result["parents_open"][0]["completion_rule"], "closes when every requirement's delivering PES is closed")
        self.assertEqual(result["parents_open"][1]["completion_rule"], "closes when its consuming PES work is delivered")
        self.assertTrue(all(row["facts"]["state"] == "OPEN" for row in result["parents_open"]))
        self.assertEqual([row["number"] for row in self.issue.closes], [7])

    def test_report_closure_has_the_same_source_effect_and_acknowledgement(self):
        frozen, admission = tick_closure.ClosureTests.worker(self)
        tick_closure.ClosureTests.report_close(self, frozen, admission, close=False)
        self.tick()
        result = tick_closure.ClosureTests.report_close(self, frozen, admission)
        self.assertEqual(result["source_issues"][0]["outcome"], "closed")
        self.assertEqual(self.ack(result, "report", admission_id=admission["admission_id"])["source_issues"], result["source_issues"])

    proof_rows = tick_closure.ClosureTests.proof_rows


class CompletedTickTests(TickCase):
    def test_pre_send_completed_source_ticks_without_reopen(self):
        self.bind(); self.prove()
        self.issue.state, self.issue.state_reason = "CLOSED", "COMPLETED"
        result = self.tick()
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["receipt"]["source_completed"])
        self.assertEqual(self.line()["status"], "current")
        self.assertEqual(self.issue.state, "CLOSED")

    def test_completed_during_send_keeps_the_confirmed_authorship(self):
        self.bind(); self.prove()
        original = self.issue.issue_body_write
        def write(**arguments):
            answer = original(**arguments)
            self.issue.state, self.issue.state_reason = "CLOSED", "COMPLETED"
            return answer
        with patch.object(self.issue, "issue_body_write", side_effect=write):
            result = self.tick()
        self.assertEqual((result["status"], result["receipt"]["authorship"]), ("confirmed", "own"))

    def test_lost_response_on_completed_source_remains_ambiguous(self):
        self.bind(); self.prove()
        self.issue.state, self.issue.state_reason = "CLOSED", "COMPLETED"
        self.issue.lose_next = "landed"
        result = self.tick()
        self.assertEqual((result["status"], result["receipt"]["authorship"]), ("unconfirmed", "ambiguous"))
        self.assertEqual(self.line()["status"], "ambiguous")

    def test_completed_source_never_bypasses_material_or_role_changes(self):
        self.bind(); self.prove()
        self.issue.state, self.issue.state_reason = "CLOSED", "COMPLETED"
        self.issue.body += "\nChanged requirement"
        self.assertEqual(self.failure("issue_reconciliation_required", self.tick).detail["reason"], "issue_body_changed")
        self.assertEqual(self.issue.writes, [])
        self.issue.title = "EEL: observations"
        self.failure("issue_is_evidence_ledger", self.tick)


class GovernanceCompletionTests(unittest.TestCase):
    def merged(self, *, changed_in_delivery=False, record=False):
        from tests import test_finalization as finalization
        case = finalization.ResumeTests()
        case.setUp(); self.addCleanup(case.doCleanups)
        if changed_in_delivery:
            (case.project / "CLAUDE.md").write_text("# Delivered policy\n")
            git(case.project, "add", "CLAUDE.md"); git(case.project, "commit", "-qm", "delivered policy")
            case.candidate = git(case.project, "rev-parse", "HEAD")
            case.write(case.stored())
            from pod.governor import observe_candidate, prepare_candidate
            case.observed = observe_candidate(case.project, base_ref="refs/remotes/origin/target")
            prepare_candidate(case.project, "objective", owner="owner", unit="delivery", observation=case.observed,
                              branch={"remote": "origin", "base": "target", "branch": "candidate"})
        decision = case.op("governor", action=case.merge())
        git(case.project, "checkout", "-q", "target")
        git(case.project, "merge", "--no-ff", "-qm", "fixture merge", "candidate")
        result = git(case.project, "rev-parse", "HEAD")
        git(case.project, "update-ref", "refs/remotes/origin/target", result)
        case.op("governor-outcome", record_id=decision["record_id"], outcome="PASS", provider={"merge_commit": result})
        if record:
            case.checkpoint(delivery={"record": decision["record_id"]})
        return case, decision

    def advance(self, case):
        (case.project / "CLAUDE.md").write_text("# Later policy\n")
        git(case.project, "add", "CLAUDE.md"); git(case.project, "commit", "-qm", "later policy")
        git(case.project, "update-ref", "refs/remotes/origin/target", git(case.project, "rev-parse", "HEAD"))

    def test_delivery_only_write_after_governance_change_keeps_baseline(self):
        case, decision = self.merged()
        bound = deepcopy(read(case.project, "objective")["checkpoint"]["governance"])
        self.advance(case)
        result = case.checkpoint(delivery={"record": decision["record_id"]})
        self.assertFalse(result["governance_adoption"]["adopted"])
        self.assertEqual(read(case.project, "objective")["checkpoint"]["governance"], bound)
        rows = case.stored(); rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[case.proof("O1")])
        self.assertTrue(case.op("checkpoint", value=case.core(obligations=rows, close=True))["checkpoint"]["closure"])

    def test_exact_merge_result_with_delivered_policy_does_not_adopt_it(self):
        case, decision = self.merged(changed_in_delivery=True)
        bound = deepcopy(read(case.project, "objective")["checkpoint"]["governance"])
        result = case.checkpoint(delivery={"record": decision["record_id"]})
        self.assertFalse(result["governance_adoption"]["adopted"])
        self.assertEqual(read(case.project, "objective")["checkpoint"]["governance"], bound)

    def test_combined_delivery_and_closure_cannot_skip_finalization(self):
        case, decision = self.merged()
        rows = case.stored(); rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[case.proof("O1")])
        result = case.refusal("checkpoint", value=case.core(obligations=rows, delivery={"record": decision["record_id"]}, close=True))
        self.assertEqual(result.detail["detail"], "closure_unfinished")

    def test_default_retarget_and_unknown_ancestry_never_grant_the_exemption(self):
        from pod.governance import closure_keeps_baseline
        case, _ = self.merged(record=True)
        self.advance(case)
        state = read(case.project, "objective")["checkpoint"]
        with patch("pod.governance.git_is_ancestor", return_value=None):
            self.assertIsNone(closure_keeps_baseline(case.project, state))
        state = deepcopy(state); state["governance"]["selection"] = "default"
        with patch("pod.governance._symbolic_ref", return_value="refs/remotes/origin/other"):
            self.assertIsNone(closure_keeps_baseline(case.project, state))

    def test_report_closure_after_governance_change_uses_the_same_proof(self):
        case, _ = self.merged(record=True)
        rows = case.stored()
        rows.append(case.sub("S"))
        case.write(rows)
        case.port.placement["branch"] = "target"
        frozen = case.packet(["S"], role="investigate", resolves="verify delivery", stop_condition="report consumed")
        admission = case.start("final-report", frozen)["admission"]; case.settle(admission)
        self.advance(case)
        rows = case.stored()
        for row in rows:
            row.pop("executor", None); row.pop("wait", None)
            row.update(state="satisfied", evidence=[case.proof(row["id"])])
        result = case.report(admission, frozen, map={"obligations": rows, "close": True})
        self.assertFalse(result["governance_adoption"]["adopted"])
        self.assertTrue(read(case.project, "objective")["checkpoint"]["closure"])

    def test_unproved_closing_currency_names_the_lead_and_preserves_bytes(self):
        case, _ = self.merged(record=True)
        self.advance(case)
        path = objective_root(case.project, "objective") / "context.json"
        before = path.read_bytes()
        with patch("pod.governance.git_is_ancestor", return_value=None):
            error = case.refusal("checkpoint", value=case.core(obligations=case.stored(), close=True))
        self.assertEqual(error.code, "governance_changed")
        self.assertTrue(error.detail["next_action"].startswith("the project lead"))
        self.assertNotIn("owner", str(error))
        self.assertEqual(path.read_bytes(), before)


class SealCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Full 20-AC handoff built once; individual tests stay well inside the fast lane.
        cls.case = case = TickCase()
        case.setUp(); cls.addClassCleanup(case.doCleanups)
        case.issue = SourcePort()
        patched = patch_issue_port(case.issue); patched.start(); cls.addClassCleanup(patched.stop)
        cls.refs = [f"AC{number}" for number in range(1, 21)] + ["handoff", "portable", "delivery"]
        case.issue.title = "SEAL: Widget acceptance"
        case.issue.body = "**Format:** SEAL v1\n\n" + "\n".join(f"- [ ] **AC{number} — widgets pass**" for number in range(1, 21))
        rows = [case.criterion("waiting", id=f"C{number}", source={"ref": ref}, check=f"{ref} is complete",
                               wait={"class": "sequenced", "referent": "C21"})
                for number, ref in enumerate(cls.refs, 1)]
        rows[20].pop("wait"); rows[20].update(state="active", executor="coordinator")
        case.write(rows, criteria=cls.refs, governance={"base_ref": "target"})
        case.prove(criteria=cls.refs)
        cls.handoff = deepcopy(case.stored()[20])
        case.write(criteria=cls.refs, renew=[{"id": "C21", "as": "handoff-next", "state": "active", "executor": "coordinator"}])
        source = issue_intake(case.project, case.locator, port=case.issue, completion=True)["source"]
        case.write(criteria=cls.refs, objective_source=source)
        rows = [row for row in case.stored() if row["state"] != "renewed"]
        for row in rows:
            row.pop("executor", None); row.pop("wait", None)
            row.update(state="satisfied", evidence=[case.proof(row["id"])])
        case.write(rows, criteria=cls.refs)
        for number in range(1, 21):
            case.tick(number)

    def test_extra_unmapped_handoff_still_holds_evidence_closure(self):
        from pod.evidence_closure import _source, _Proof
        state = deepcopy(read(self.case.project, "objective"))
        next(row for row in state["checkpoint"]["obligations"] if row["id"] == "handoff-next")["state"] = "waiting"
        body = self.case.issue.body
        class Remote:
            def get(self): return self
            def issue_state(self, **arguments):
                return {"body": body, "state": "CLOSED", "state_reason": "COMPLETED"}
        proof = _Proof("objective")
        _source(None, {"issue": state["checkpoint"]["objective_source"]}, state, proof, Remote())
        self.assertIn("source_unproved", [row["code"] for row in proof.conditions])

    def test_full_seal_handoff_renews_rebinds_ticks_and_closes_its_own_issue(self):
        case = self.case
        state = read(case.project, "objective")
        self.assertEqual(state["checkpoint"]["objective_source"]["role"], "SEAL")
        self.assertEqual(len(case.issue.writes), 20)
        old = next(row for row in case.stored() if row["id"] == "C21")
        self.assertEqual(old["receipts"], self.handoff["receipts"])
        self.assertEqual(old["renewal"]["by"], "handoff-next")
        closed = case.ack(case.write(criteria=self.refs, close=True))
        self.assertEqual(closed["source_issues"][0]["outcome"], "closed")
        before = case.saved()
        self.assertEqual(close_source_issues(case.project, "objective")["source_issues"][0]["outcome"], "already_completed")
        self.assertEqual(case.saved(), before)
        self.assertEqual(len(case.issue.closes), 1)


class SealRecognitionTests(unittest.TestCase):
    def test_ac_lines_and_exact_refs_are_recognized_only_for_bound_seals(self):
        body = "- [ ] **AC1 — accepted**\n- [x] **PoD#2\n```\n- [x] **AC3\n```"
        self.assertEqual(set(pod_lines(body)), {2})
        self.assertEqual(set(pod_lines(body, seal=True)), {1, 2})
        self.assertIsNone(pod_ref_number("AC1"))
        self.assertEqual(pod_ref_number("AC1", seal=True), 1)
        self.assertIsNone(pod_ref_number("AC1 extra", seal=True))


class CompletionGuidanceTests(unittest.TestCase):
    def test_capacity_and_governance_unavailable_name_the_lead_action(self):
        from pod import governance, history
        with self.assertRaises(PodError) as full:
            history._require_size({"padding": "x" * history.CONTEXT_LIMIT}, current_bytes=0)
        self.assertIn("storage", full.exception.detail["next_action"])
        self.assertIn("inspect", full.exception.detail["next_action"])
        self.assertIn("migrate", full.exception.detail["next_action"])
        self.assertNotIn("renew", full.exception.detail["next_action"])
        self.assertNotIn("with an owner decision", full.exception.detail["next_action"])
        case, _ = evidence.DeliveryClosesTests.merged(self)
        state = read(case.project, "objective")["checkpoint"]
        observed = governance.governance_observation(case.project, state["governance"]["base_ref"])
        with patch("pod.governance.git_is_ancestor", return_value=None), self.assertRaises(PodError) as unavailable:
            governance._independent_governance(case.project, observed, case.candidate, {}, established=state["governance"])
        self.assertEqual(unavailable.exception.code, "governance_unavailable")
        self.assertTrue(unavailable.exception.detail["next_action"].startswith("the project lead"))
        self.assertIn("ordinary work adopting changed governance", unavailable.exception.detail["next_action"])
