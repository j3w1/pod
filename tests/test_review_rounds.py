"""R150: real admission/checkpoint/report boundaries, replacing only external ports."""
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
        question = self.case.write()["report"]["review_allowance"]["question"]
        authority = {"provenance": "user_direct", "channel": "session", "instruction": f"Choose {choice}",
                     "question": question, "allowance": {"choice": choice, **({"rounds": rounds} if rounds is not None else {})},
                     **fields}
        return self.case.write(rows, revision_authority=authority)

    def questions(self):
        return [row for row in self.facts()["checkpoint"]["questions"]
                if isinstance(row, dict) and row.get("kind") == "review_allowance"]

    def test_linked_required_group_and_reuse_statement_are_retained(self):
        self.restore("one")
        result = self.finish(finding=self.finding(findings=[{"finding": "F", "severity": "major"},
                                                          {"finding": "F2", "severity": "minor"}]))
        finding = result["report"]["review_findings"][0]
        self.assertEqual((finding["blocks"], finding["findings"], finding["correction"]), ("R1", ["F", "F2"], "K"))
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

    def test_third_finished_round_blocks_fourth_and_checkpoints_one_owner_question(self):
        self.assertEqual([row["id"] for row in self.questions()], ["review-allowance-3"])
        error = self.unchanged_refusal("review_allowance_reached", self.begin, "due")
        self.assertEqual(error.detail["referent"]["question"], self.questions()[0])
        for questions in ([], self.questions() * 2, []):
            self.case.write(questions=questions)
            self.assertEqual(len(self.questions()), 1)
        self.assertTrue(self.questions()[0]["owner_only"])
        self.assertNotIn("choices", self.questions()[0])

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
        self.assertEqual(self.facts(), before)
        self.case.write(questions=[{**self.questions()[0], "resolved": True, "status": "answered", "answer": "continue"}])
        self.assertNotIn("answer", self.questions()[0])
        self.unchanged_refusal("review_allowance_reached", self.begin, "due")

    def test_clean_third_round_finishes_without_question(self):
        self.restore("third")
        result = self.finish(clean=True)
        self.assertEqual(result["report"]["unfinished"], [])
        self.assertEqual(result["report"]["review_allowance"]["state"], "clear")
        self.case.write()
        self.assertEqual(self.questions(), [])

    def test_continue_expires_and_asks_with_the_new_granted_number(self):
        result = self.answer("continue", rounds=2)
        self.assertEqual(result["report"]["review_allowance"]["granted"], 5)
        for _ in range(2):
            self.begin()
            self.finish()
        self.case.write(questions=[])
        self.assertEqual([row["id"] for row in self.questions()], ["review-allowance-3", "review-allowance-5"])
        self.assertEqual(self.questions()[0]["answer"], {"choice": "continue", "rounds": 2})
        self.assertEqual((self.questions()[0]["resolved"], self.questions()[0]["status"]), (True, "answered"))
        self.unchanged_refusal("review_allowance_reached", self.begin, "due")

    def test_simplify_requires_a_named_change_and_grants_exactly_one_round(self):
        self.unchanged_refusal("obligation_invalid", lambda: self.case.write(revision_authority={
            "provenance": "user_direct", "instruction": "simplify", "question": "review-allowance-3",
            "allowance": {"choice": "simplify"}}), "simplification_required")
        rows = self.case.stored()
        correction = next(row for row in rows if row["id"] == "K")
        correction.pop("wait")
        correction.update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "Remove this requirement"})
        self.answer("simplify", rows=rows)
        self.begin()
        self.finish()
        self.case.write()
        self.assertEqual(self.questions()[-1]["id"], "review-allowance-4")
        self.unchanged_refusal("review_allowance_reached", self.begin)

    def test_simplified_verification_can_finish_cleanly(self):
        rows = self.case.stored()
        next(row for row in rows if row["id"] == "K")["check"] = "repair the simplified result"
        self.answer("simplify", rows=rows)
        self.repair()
        self.begin()
        self.finish(clean=True)
        self.assertEqual(self.case.write()["report"]["review_allowance"]["state"], "clear")
        self.assertTrue(all(row.get("resolved") for row in self.questions()))

    def test_stop_reports_every_unfinished_obligation_and_prevents_review(self):
        result = self.answer("stop")["report"]
        self.assertEqual(result["review_allowance"]["state"], "stopped")
        self.assertEqual(set(result["unfinished"]), {"O1", "A", "K"})
        self.assertIn("incomplete", result["interim_report"])
        self.assertEqual(result["satisfied"], [])
        self.unchanged_refusal("review_allowance_reached", self.begin, "stopped")

    def test_relayed_allowance_answers_refuse_without_verifier_or_effect(self):
        with patch("pod.relay.verify", side_effect=AssertionError("allowance called relay verifier")):
            self.unchanged_refusal("relayed_decision_refused", lambda: self.case.write(revision_authority={
                "provenance": "user_direct", "instruction": "continue", "channel": "relayed",
                "relay": {"reference": "verified-relay", "owner_source": "authenticated fixture"},
                "question": "review-allowance-3", "allowance": {"choice": "continue", "rounds": 1}}))

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
