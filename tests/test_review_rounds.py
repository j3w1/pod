"""R173: real admission/checkpoint/report boundaries, replacing only external ports."""
from copy import deepcopy
import shutil
import unittest
from unittest.mock import patch

from pod.ledger import read
from tests.issue41_support import ProductionCase


class ReviewRoundsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = case = ProductionCase()
        case.setUp()
        cls.addClassCleanup(case.doCleanups)
        from pod.config import set_decision_policy
        set_decision_policy(case.project, "hands_off", decision={"provenance": "user_direct", "instruction": "Fixture policy"})
        case.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "review src", "scope": {"paths": ["src"]}, "question": "does src meet R1?",
                     "candidate": case.candidate, "existing_evidence": "tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}})
        cls.snapshots = {}
        seed = cls()
        seed.begin()
        cls.save("one", seed.pending)
        seed.finish()
        seed.begin()
        seed.finish()
        seed.begin()
        cls.save("third", seed.pending)
        seed.finish(finding=seed.finding())
        case.write()
        cls.save("due", seed.pending)
        seed.repair()
        cls.save("repaired", seed.pending)
        seed.restore("one")
        # Synthetic earlier-admission control: the earlier writer had neither new admission gate.
        with patch("pod.assurance.refuse_premature_review"), patch(
                "pod.obligations.review_allowance", return_value={"state": "open", "rounds": 0, "granted": 99}):
            seed.finish(finding=seed.finding())
            for _ in range(4):
                seed.begin()
                seed.finish()
        case.write()
        cls.save("earlier-five", seed.pending)

    @classmethod
    def save(cls, key, pending):
        cls.snapshots[key] = ({p.relative_to(cls.case.root / "state"): p.read_bytes()
                               for p in (cls.case.root / "state").rglob("*") if p.is_file()},
                              deepcopy(cls.case.port.__dict__), deepcopy(pending))

    def restore(self, key):
        files, port, self.pending = deepcopy(self.snapshots[key])
        state = self.case.root / "state"
        shutil.rmtree(state)
        for relative, data in files.items():
            path = state / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.case.port.__dict__.clear()
        self.case.port.__dict__.update(port)

    def setUp(self):
        self.restore("due")

    def facts(self):
        return read(self.case.project, "objective")

    def begin(self):
        packet = self.case.packet(["A"], role="review")
        admission = self.case.start(f"review-{len(self.case.port.starts) + 1}", packet)["admission"]
        self.pending = admission, packet
        return admission

    @staticmethod
    def finding(**fields):
        row = {"finding": "F", "severity": "major", "triage": "required_correction",
               "blocks": "R1", "summary": "The missing result prevents R1's outcome", "correction": "K", **fields}
        if "findings" in row:
            row.pop("finding")
            row.pop("severity")
        return row

    def finish(self, *, finding=None, clean=False, outcome="succeeded"):
        case = self.case
        admission, packet = self.pending
        case.settle(admission)
        case.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = outcome
        rows = case.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("executor", None)
        assurance.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        if finding and finding["triage"] == "required_correction":
            rows.append({"id": "K", "kind": "correction", "provenance": "coordinator", "parent": "A",
                         "check": "repair the missing result", "state": "waiting",
                         "wait": {"class": "sequenced", "referent": "O1"}})
        if clean:
            for row in rows:
                row.pop("executor", None)
                row.pop("wait", None)
                row.update(state="satisfied", evidence=[{"attempt": admission["admission_id"]}]
                           if row["id"] == "A" else [case.proof(row["id"])])
        report = {"schema": "pod-report/v1", "assignment": packet["packet_id"],
                  "attempt": admission["native_binding"]["dispatchId"], "candidate": case.candidate,
                  "outcome": outcome, "scope": ["src"], "files": [], "checks": ["source audit"],
                  "failures": [] if outcome == "succeeded" else ["review incomplete"],
                  "evidence": ["R1: small glue reuses triage; R2: installed admission with a bounded check"],
                  "uncertainty": [], "questions": []}
        return case.op("report", admission_id=admission["admission_id"], packet=packet, report=report,
                       map={"obligations": rows}, triage=[finding] if finding else [])

    def repair(self):
        rows = self.case.stored()
        correction = next(row for row in rows if row["id"] == "K")
        correction.pop("wait", None)
        correction.update(state="satisfied", evidence=[self.case.proof("K")])
        return self.case.write(rows)

    def unchanged_refusal(self, code, call, detail=None):
        before, starts = self.facts(), deepcopy(self.case.port.starts)
        error = self.case.refused(code, detail, call)
        self.assertEqual(self.facts(), before)
        self.assertEqual(self.case.port.starts, starts)
        return error

    def answer(self, choice, *, rounds=None, rows=None, **fields):
        question = self.questions()[-1]["id"]
        authority = {"provenance": "user_direct", "channel": "session", "instruction": f"Choose {choice}",
                     "question": question, "allowance": {"choice": choice, **({"rounds": rounds} if rounds is not None else {})},
                     **fields}
        return self.case.write(rows, revision_authority=authority)

    def lead_answer(self, choice="continue", *, rounds=1, rows=None, **fields):
        return self.answer(choice, rounds=rounds if choice == "continue" else None, rows=rows,
                           provenance="agent_decision", channel="agent_session", delegation={
                               "basis": "lead_decision", "source": {"reference": "native-mail/lead-answer"},
                               "reason": "The named remaining proof fits this bounded continuation"}, **fields)

    def questions(self):
        from pod.assurance import REVIEW_ORIGIN
        return [{key: value for key, value in row.items() if key != REVIEW_ORIGIN}
                for row in self.facts()["checkpoint"]["questions"]
                if isinstance(row, dict) and row.get("kind") == "review_allowance"]

    def test_linked_required_group_and_reuse_statement_are_retained(self):
        self.restore("one")
        result = self.finish(finding=self.finding(findings=[{"finding": "F", "severity": "major"},
                                                          {"finding": "F2", "severity": "minor"}]))
        finding = result["report"]["review_findings"][0]
        self.assertEqual(finding, {"obligation": "A", "finding": "F", "severity": "major",
                                   "triage": "required_correction", "blocks": "R1", "correction": "K"})
        retained = next(row for row in self.facts()["checkpoint"]["obligations"] if row["id"] == "A")["findings"][0]
        self.assertEqual(retained["findings"], ["F", "F2"])
        self.assertIn("summary", retained)
        admission, packet = self.pending
        stored = self.facts()["admissions"][admission["admission_id"]]["report"]["observation"]
        self.assertIn("R1: small glue", stored["evidence"][0])
        before = self.facts()
        self.case.op("report", admission_id=admission["admission_id"], packet=packet, report=stored,
                     triage=[self.finding(findings=[{"finding": "F", "severity": "major"}, {"finding": "F2", "severity": "minor"}])])
        self.assertEqual(self.facts(), before)

    def test_missing_link_refuses_with_action_then_unlinked_advisory_is_a_proposal(self):
        self.restore("one")
        finding = self.finding()
        finding.pop("blocks")
        error = self.unchanged_refusal("obligation_invalid", lambda: self.finish(finding=finding), "finding_unlinked")
        self.assertIn("ask the reviewer", error.detail["next_action"])
        self.assertIn("policy:safety", error.detail["next_action"])
        finding.pop("correction")
        finding.update(triage="advisory", reason="No requirement or policy is affected")
        result = self.finish(finding=finding)
        self.assertEqual(len(result["report"]["proposals_out_of_scope"]), 1)
        self.assertNotIn("K", result["report"]["unfinished"])

    def test_safety_and_privacy_links_stay_required(self):
        for target in ("policy:safety", "policy:privacy"):
            with self.subTest(target=target):
                self.restore("one")
                finding = self.finding(summary=f"The exposed result violates {target}")
                finding.pop("blocks")
                self.unchanged_refusal("obligation_invalid", lambda: self.finish(finding=finding), "finding_unlinked")
                self.assertEqual(finding["triage"], "required_correction")
                finding["blocks"] = target
                result = self.finish(finding=finding)
                self.assertIn("K", result["report"]["unfinished"])
                self.assertEqual(result["report"]["review_findings"][0]["triage"], "required_correction")

    def test_advisory_link_and_unbounded_link_refuse_atomically(self):
        self.restore("one")
        for finding in (self.finding(blocks="x" * 129), self.finding(triage="advisory", reason="minor concern")):
            self.unchanged_refusal("obligation_invalid", lambda: self.finish(finding=finding), "malformed")

    def test_third_finished_round_blocks_fourth_and_checkpoints_one_lead_question(self):
        self.assertEqual([row["id"] for row in self.questions()], ["review-allowance-3"])
        error = self.unchanged_refusal("review_allowance_reached", self.begin, "due")
        self.assertEqual(error.detail["referent"]["question"], self.questions()[0])
        for questions in ([], self.questions() * 2, []):
            self.case.write(questions=questions)
            self.assertEqual(len(self.questions()), 1)
        self.assertFalse(self.questions()[0]["owner_only"])
        self.assertEqual(self.questions()[0]["recipient"], "repository_lead")
        self.assertIn("Do not raise a maintainer card or owner-question notice", self.questions()[0]["text"])

    def test_repaired_correction_without_reverification_still_asks(self):
        self.restore("repaired")
        self.assertEqual(self.case.write()["report"]["review_allowance"]["state"], "due")
        self.assertEqual(len(self.questions()), 1)
        self.unchanged_refusal("review_allowance_reached", self.begin)

    def test_running_third_round_has_no_question_and_no_fourth_admission(self):
        self.restore("third")
        self.assertEqual(self.case.write()["report"]["review_allowance"]["state"], "running")
        self.assertEqual(self.questions(), [])
        self.unchanged_refusal("review_allowance_reached", self.begin, "running")

    def test_consumed_failed_third_round_is_finished_without_passing_assurance(self):
        self.restore("third")
        self.finish(outcome="failed")
        result = self.case.write()["report"]
        self.assertEqual(result["review_allowance"]["state"], "due")
        self.assertNotIn("A", result["satisfied"])

    def test_public_status_is_read_only_and_caller_cannot_answer_by_editing_a_row(self):
        from pod.status import status
        before = self.facts()
        observed = status(self.case.project, "run", objective="objective")
        self.assertEqual(observed["report"]["review_allowance"]["state"], "due")
        projection = observed["recorded_questions"]
        self.assertEqual(projection["status"], "complete")
        self.assertEqual((projection["items"], projection["total"]), ([], 0))
        self.assertEqual(observed["review_allowance_question"], self.questions()[0])
        self.assertEqual(self.facts(), before)
        self.case.write(questions=[{**self.questions()[0], "resolved": True, "status": "answered", "answer": "continue"}])
        self.assertNotIn("answer", observed["review_allowance_question"])
        self.assertFalse(self.questions()[-1]["resolved"])
        self.unchanged_refusal("review_allowance_reached", self.begin, "due")

    def test_clean_third_round_finishes_without_question(self):
        self.restore("third")
        result = self.finish(clean=True)
        self.assertEqual(result["report"]["unfinished"], [])
        self.assertEqual(result["report"]["review_allowance"]["state"], "clear")
        self.case.write()
        self.assertEqual(self.questions(), [])

    def test_continue_expires_and_asks_with_the_new_granted_number(self):
        result = self.lead_answer(rounds=2)
        self.assertEqual(result["report"]["review_allowance"]["granted"], 5)
        self.repair()
        for _ in range(2):
            self.begin()
            self.finish()
        self.case.write(questions=[])
        self.assertEqual([row["id"] for row in self.questions()], ["review-allowance-3", "review-allowance-5"])
        self.assertEqual(self.questions()[0]["answer"], {"choice": "continue", "rounds": 2})
        self.assertEqual((self.questions()[0]["resolved"], self.questions()[0]["status"]), (True, "answered"))
        self.unchanged_refusal("review_allowance_reached", self.begin, "due")

    def test_continue_after_five_earlier_rounds_grants_two_further_rounds(self):
        self.restore("earlier-five")
        allowance = self.case.write()["report"]["review_allowance"]
        self.assertEqual((allowance["rounds"], allowance["granted"], allowance["question"]),
                         (5, 3, "review-allowance-5"))
        question = self.questions()[0]
        self.assertIn("5 review rounds", question["text"])
        self.assertIn("3 granted", question["text"])
        error = self.unchanged_refusal("review_allowance_reached", self.begin, "due")
        self.assertEqual(error.detail["referent"]["question"], question)
        result = self.lead_answer(rounds=2)["report"]["review_allowance"]
        self.assertEqual((result["rounds"], result["granted"], result["state"]), (5, 7, "open"))
        self.assertEqual(self.questions()[0]["text"], question["text"])
        self.repair()
        for _ in range(2):
            self.begin()
            self.finish()
        self.case.write()
        self.assertEqual([row["id"] for row in self.questions()], ["review-allowance-5", "review-allowance-7"])
        self.unchanged_refusal("review_allowance_reached", self.begin, "due")
        self.lead_answer()
        self.assertEqual(self.case.write()["report"]["review_allowance"]["granted"], 8)

    def test_simplify_after_five_earlier_rounds_grants_one_verification_round(self):
        self.restore("earlier-five")
        rows = self.case.stored()
        next(row for row in rows if row["id"] == "K")["check"] = "repair the simplified result"
        result = self.answer("simplify", rows=rows)["report"]["review_allowance"]
        self.assertEqual((result["rounds"], result["granted"], result["state"]), (5, 6, "open"))
        self.repair()
        self.begin()
        self.finish()
        self.case.write()
        self.assertEqual([row["id"] for row in self.questions()], ["review-allowance-5", "review-allowance-6"])
        self.unchanged_refusal("review_allowance_reached", self.begin, "due")

    def test_simplify_requires_a_named_change_and_grants_exactly_one_round(self):
        self.unchanged_refusal("obligation_invalid", lambda: self.case.write(revision_authority={
            "provenance": "user_direct", "instruction": "simplify", "question": "review-allowance-3",
            "allowance": {"choice": "simplify"}}), "simplification_required")
        rows = self.case.stored()
        correction = next(row for row in rows if row["id"] == "K")
        correction.pop("wait")
        correction.update(state="withdrawn", withdrawal={"by": "agent_decision", "reason": "Remove this requirement"})
        self.lead_answer("simplify", rows=rows)
        self.begin()
        self.finish()
        self.case.write()
        self.assertEqual(self.questions()[-1]["id"], "review-allowance-4")
        self.unchanged_refusal("review_allowance_reached", self.begin)

    def test_simplified_verification_can_finish_cleanly(self):
        rows = self.case.stored()
        next(row for row in rows if row["id"] == "K")["check"] = "repair the simplified result"
        self.lead_answer("simplify", rows=rows)
        self.repair()
        self.begin()
        self.finish(clean=True)
        self.assertEqual(self.case.write()["report"]["review_allowance"]["state"], "clear")
        self.assertTrue(all(row.get("resolved") for row in self.questions()))

    def test_stop_reports_every_unfinished_obligation_and_prevents_review(self):
        result = self.lead_answer("stop")["report"]
        self.assertEqual(result["review_allowance"]["state"], "stopped")
        self.assertEqual(set(result["unfinished"]), {"O1", "A", "K"})
        self.assertIn("incomplete", result["interim_report"])
        self.assertEqual(result["satisfied"], [])
        self.unchanged_refusal("review_allowance_reached", self.begin, "stopped")

    def test_unsourced_relayed_allowance_refuses_without_verifier_or_effect(self):
        with patch("pod.relay.verify", side_effect=AssertionError("allowance called relay verifier")):
            self.unchanged_refusal("obligation_invalid", lambda: self.case.write(revision_authority={
                "provenance": "user_direct", "instruction": "continue", "channel": "relayed",
                "relay": {"reference": "verified-relay", "owner_source": "authenticated fixture"},
                "question": "review-allowance-3", "allowance": {"choice": "continue", "rounds": 1}}), "allowance_source_required")

    def test_responder_cannot_answer_owner_allowance_even_under_yolo(self):
        self.case.op("standing-authorization", action="activate", decision={
            "provenance": "user_direct", "instruction": "use yolo"})
        with patch("pod.relay.verify", side_effect=AssertionError("allowance called responder verifier")):
            self.unchanged_refusal("responder_answer_refused", lambda: self.case.op("standing-authorization",
                action="answer", answer={"question": "review-allowance-3", "text": "continue 1",
                                         "relay": {"reference": "responder-answer"}}))

    def test_continuation_bounds_and_answer_replay_cannot_grant_twice(self):
        for rounds in (0, 9, True):
            self.unchanged_refusal("obligation_invalid", lambda: self.case.write(revision_authority={
                "provenance": "user_direct", "instruction": "continue", "question": "review-allowance-3",
                "allowance": {"choice": "continue", "rounds": rounds}}))
        result = self.answer("continue", rounds=1)
        decision = result["checkpoint"]["revision_decisions"][-1]
        self.assertEqual((decision["kind"], decision["channel"]), ("question", "session"))
        self.unchanged_refusal("obligation_invalid", lambda: self.case.write(revision_authority={
            key: value for key, value in decision.items() if key not in ("seq", "kind")}), "allowance_not_due")

    def test_sourced_lead_answer_retains_delegation_and_private_source(self):
        result = self.lead_answer()
        decision = result["checkpoint"]["revision_decisions"][-1]
        self.assertEqual((decision["kind"], decision["provenance"], decision["channel"]),
                         ("question", "agent_decision", "agent_session"))
        self.assertEqual(decision["delegation"]["basis"], "lead_decision")
        self.assertEqual(decision["delegation"]["source"], {"reference": "native-mail/lead-answer"})
        self.assertIn("bounded continuation", decision["delegation"]["reason"])
        from pod.status import public_status
        self.assertNotIn("delegation", public_status(decision))
        self.assertEqual(self.case.write()["report"]["review_allowance"]["granted"], 4)

    def test_lead_answer_requires_source_reason_and_lead_basis(self):
        for delegation in (None, {}, {"basis": "lead_decision", "source": {"reference": ""}, "reason": "reason"},
                           {"basis": "lead_decision", "source": {"reference": "mail"}, "reason": ""},
                           *({"basis": role, "source": {"reference": "mail"}, "reason": "reason"}
                             for role in ("reviewer_decision", "worker_decision"))):
            with self.subTest(delegation=delegation):
                self.unchanged_refusal("obligation_invalid", lambda: self.answer("continue", rounds=1,
                    provenance="agent_decision", channel="agent_session",
                    **({"delegation": delegation} if delegation is not None else {})))

    def test_worker_cannot_record_lead_answer_for_coordinator(self):
        import os
        from pod.internal import run
        with patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "worker"}):
            self.unchanged_refusal("coordinator_conflict", lambda: run("checkpoint", {
                "project": str(self.case.project), "objective": "objective", "owner": "worker",
                "value": self.case.core(revision_authority={"provenance": "agent_decision",
                    "channel": "agent_session", "instruction": "continue", "question": "review-allowance-3",
                    "allowance": {"choice": "continue", "rounds": 1}})}))

    def test_lead_answer_never_bypasses_current_machine_policy(self):
        from pod.config import personal_path, set_decision_policy
        path = personal_path(self.case.project)
        before = path.read_bytes()
        self.addCleanup(path.write_bytes, before)
        set_decision_policy(self.case.project, "careful", decision={"provenance": "user_direct", "instruction": "Fixture careful policy"})
        self.unchanged_refusal("decision_policy_denied", self.lead_answer)

    def test_changed_lead_answer_is_not_replayed_as_an_earlier_grant(self):
        self.lead_answer()
        # Reuse the same checkpoint content so only the answer differs.
        self.unchanged_refusal("obligation_invalid", lambda: self.answer("continue", rounds=2,
            provenance="agent_decision", channel="agent_session", delegation={
                "basis": "lead_decision", "source": {"reference": "native-mail/lead-answer"},
                "reason": "The named remaining proof fits this bounded continuation"}), "allowance_not_due")

    def test_lead_continue_does_not_authorize_a_requirement_revision(self):
        rows = self.case.stored()
        next(row for row in rows if row["id"] == "K")["check"] = "an unrelated revised check"
        self.unchanged_refusal("obligation_invalid", lambda: self.lead_answer(rows=rows), "allowance_scope_exceeded")

    def test_unsourced_agent_cannot_simplify_by_withdrawing_a_correction(self):
        rows = self.case.stored()
        correction = next(row for row in rows if row["id"] == "K")
        correction.pop("wait")
        correction.update(state="withdrawn", withdrawal={"by": "agent_decision", "reason": "remove K"})
        self.unchanged_refusal("obligation_invalid", lambda: self.answer("simplify", rows=rows,
            provenance="agent_decision", channel="agent_session"), "allowance_source_required")

    def test_verified_person_and_sourced_verified_lead_keep_their_actual_actor(self):
        from datetime import datetime, timezone
        decided_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        def verdict(kind, request):
            binding = request["decision"]
            return {**request, "verified": True, "decided_at": decided_at,
                    "single_use": "fixture:" + binding["request_id"], "principal": {
                        "kind": kind, "identity": "fixture-" + kind, "scope": {
                            "kinds": binding["kinds"], "categories": binding["categories"],
                            "resources": binding["resources"]}}}
        for kind in ("person", "agent"):
            with self.subTest(kind=kind):
                self.restore("due")
                with patch("pod.relay.verify_decision", side_effect=lambda _, request: verdict(kind, request)):
                    extra = {"delegation": {"basis": "lead_decision", "source": {"reference": "native-mail/lead"},
                                             "reason": "Bounded remaining verification"}} if kind == "agent" else {}
                    result = self.answer("continue", rounds=1, provenance="decision_request", channel="relayed",
                                         relay={"reference": "fixture-vouch", "verification": "decision"}, **extra)
                record = result["checkpoint"]["revision_decisions"][-1]
                self.assertEqual(record["actor"]["kind"], kind)
                self.assertEqual(record["provenance"], "user_direct" if kind == "person" else "agent_decision")
                self.assertEqual(self.case.write()["report"]["review_allowance"]["granted"], 4)

    def test_earlier_owner_answer_history_survives_current_lead_question_projection(self):
        self.answer("continue", rounds=1)
        before = deepcopy(self.facts()["checkpoint"]["revision_decisions"])
        self.repair()
        self.begin()
        self.finish()
        self.case.write()
        self.assertEqual(self.facts()["checkpoint"]["revision_decisions"], before)
        self.assertEqual(self.questions()[-1]["recipient"], "repository_lead")
        self.assertEqual(self.questions()[0]["answer"], {"choice": "continue", "rounds": 1})
