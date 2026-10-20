"""R120 question authority through R149 retirement, using recording ports only."""

from copy import deepcopy
from unittest.mock import patch

from pod.errors import PodError
from pod.github import issue_intake
from pod.ledger import kernel_context, read
from pod.obligations import definition_id, validate_closed_snapshot, validate_map_record
from pod.pod_mapping import superseded_originals
from pod.pod_ticks import requirement_epoch, requirement_scope
from pod.status import status
from tests.test_issue_mapping_supersession import SupersessionCase


QUESTIONS = ["owner-question"]
RELAY = {"reference": "fixture-question-answer", "owner_source": "fixture-owner"}


class QuestionCase(SupersessionCase):
    def prepare_question(self, kind="repaired-verified", *, old_proof=False):
        if kind in ("canonical", "verified-canonical"):
            self.criteria = ["PoD#1"]
            self.begin()
            if kind == "verified-canonical":
                self.revise(self.proposal())
                self.criteria = ["PoD#1", "PoD#2"]
            self.index = 0
        elif kind == "repaired":
            self.bind_original()
            self.repair()
            self.index = 0
        else:
            self.supersede(old_proof=old_proof)
            self.index = 1
        self.write(questions=QUESTIONS)
        return deepcopy(read(self.project, "objective"))

    @staticmethod
    def answer_authority(channel="relayed", *, question="owner-question"):
        authority = {"provenance": "user_direct", "instruction": "Answer the recorded acceptance question",
                     "question": question}
        if channel is not None:
            authority["channel"] = channel
        if channel == "relayed":
            authority["relay"] = deepcopy(RELAY)
        return authority

    def answer(self, channel="relayed"):
        rows = self.stored()
        rows[self.index]["check"] += " Owner requires additional diagnostics."
        self.write(rows, questions=QUESTIONS, revision_authority=self.answer_authority(channel))
        return deepcopy(read(self.project, "objective"))

    def assert_no_effects(self, before, calls, receipts):
        self.assertEqual(self.saved(), before)
        self.assertEqual(len(self.requests), calls)
        self.assertEqual(self.receipts(), receipts)
        self.assertEqual((self.port.starts, self.issue.writes, self.remote.calls), ([], [], []))

    def refuse_answer(self, code, authority, *, questions=QUESTIONS):
        rows = self.stored()
        rows[self.index]["check"] += " Unaccepted check change."
        before, calls, receipts = self.saved(), len(self.requests), self.receipts()
        error = self.failure(code, self.write, rows, questions=questions, revision_authority=authority)
        self.assert_no_effects(before, calls, receipts)
        return error


class QuestionSiblingTests(QuestionCase):
    def semantic_sibling(self, kind):
        initial = self.prepare_question(kind)
        calls, receipts = len(self.requests), self.receipts()
        for channel in (None, "session", "relayed"):
            accepted = self.answer(channel)
            row = accepted["checkpoint"]["obligations"][self.index]
            decision = accepted["checkpoint"]["revision_decisions"][-1]
            self.assertEqual((decision["kind"], decision["question"]), ("question", "owner-question"))
            self.assertEqual(decision["channel"], channel or "session")
            if channel == "relayed":
                self.assertEqual(decision["relay"], RELAY)
            else:
                self.assertNotIn("relay", decision)
            self.assertEqual(row["definition"], definition_id(row))
            self.assertEqual(row["receipts"], [])
            self.assertEqual(requirement_epoch(accepted["pod_ticks"]["criterion_scopes"][row["id"]],
                row, accepted["checkpoint"]), accepted["checkpoint"]["seq"])
        self.write()
        after = read(self.project, "objective")
        self.assertEqual(after["checkpoint"]["revision_decisions"], accepted["checkpoint"]["revision_decisions"])
        self.assertEqual(after["checkpoint"]["obligations"], accepted["checkpoint"]["obligations"])
        if kind == "repaired-verified":
            self.assertEqual(after["checkpoint"]["obligations"][0], initial["checkpoint"]["obligations"][0])
        self.assertFalse(any(line["proven"] for line in self.ticks()["lines"]))
        self.assertEqual(len(self.requests), calls)
        self.assertEqual(self.receipts(), receipts)
        self.assertEqual((self.port.starts, self.issue.writes, self.remote.calls), ([], [], []))

    def test_canonical_question_channels_and_default_session(self):
        self.semantic_sibling("canonical")

    def test_repaired_question_channels_and_default_session(self):
        self.semantic_sibling("repaired")

    def test_verified_canonical_question_channels_and_default_session(self):
        self.semantic_sibling("verified-canonical")

    def test_repaired_verified_question_channels_and_default_session(self):
        self.semantic_sibling("repaired-verified")

    def test_recorded_object_question_accepts_its_reference_without_relabeling(self):
        self.prepare_question()
        question = {"id": "question-id", "reference": "question-reference", "text": "Which diagnostics?"}
        self.write(questions=[question])
        rows = self.stored()
        rows[self.index]["check"] += " Owner selected the diagnostic result."
        self.write(rows, questions=[question], revision_authority=self.answer_authority(question=question["reference"]))
        decision = read(self.project, "objective")["checkpoint"]["revision_decisions"][-1]
        self.assertEqual((decision["question"], decision["channel"], decision["relay"]),
                         (question["reference"], "relayed", RELAY))
        self.write()

    def test_question_history_survives_metadata_admission_report_and_checkpoint(self):
        self.prepare_question()
        accepted = self.answer()
        calls, receipts = len(self.requests), self.receipts()
        rows = self.stored()
        rows[1]["proof_scope"] = ["src"]
        self.write(rows)
        self.issue.body += "\n\n## Delivery\nRecord ordinary delivery bookkeeping.\n"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.write(objective_source=source)
        frozen = self.packet(["R1"], criteria=self.criteria)
        admission = self.start("question-revised", frozen,
            accompanying={"seq": read(self.project, "objective")["checkpoint"]["seq"] + 1})["admission"]
        self.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = "succeeded"
        rows = self.stored()
        rows[1]["executor"] = "coordinator"
        self.report(admission, frozen, map={"seq": read(self.project, "objective")["checkpoint"]["seq"] + 1,
                                           "obligations": rows})
        self.write()
        after = read(self.project, "objective")
        self.assertEqual(after["checkpoint"]["obligations"][0], accepted["checkpoint"]["obligations"][0])
        self.assertEqual(after["checkpoint"]["obligations"][1]["check"], accepted["checkpoint"]["obligations"][1]["check"])
        self.assertEqual(after["checkpoint"]["revision_decisions"], accepted["checkpoint"]["revision_decisions"])
        self.assertEqual(after["pod_ticks"]["criterion_scopes"]["R1"]["acceptance"],
                         accepted["pod_ticks"]["criterion_scopes"]["R1"]["acceptance"])
        self.assertEqual(len(self.port.starts), 1)
        self.assertEqual(len(self.requests), calls)
        self.assertEqual(self.receipts(), receipts)
        self.assertEqual((self.issue.writes, self.remote.calls), ([], []))

    def fresh_closure(self, kind, channel):
        self.prepare_question(kind, old_proof=True)
        accepted = self.answer(channel)
        calls, receipts = len(self.requests), self.receipts()
        self.prove_live()
        self.tick(1)
        if kind != "canonical":
            self.tick(2)
        self.write(close=True)
        state, before = read(self.project, "objective"), self.saved()
        self.assertEqual(validate_closed_snapshot(state["checkpoint"],
            kernel_context(self.project, "objective", state, None)), ())
        view = status(self.project, None, objective="objective")
        self.assertEqual(view["owner_decisions"][-1]["decision"]["channel"], channel)
        decisions = state["checkpoint"]["closure"]["report"]["owner_decisions"]
        question = next(row["decision"] for row in decisions if row["decision"].get("question") == "owner-question")
        self.assertEqual(question["channel"], channel)
        if channel == "relayed":
            self.assertEqual(question["relay"], RELAY)
        if kind != "canonical":
            self.assertEqual(state["checkpoint"]["obligations"][0], accepted["checkpoint"]["obligations"][0])
        self.assertEqual(state["checkpoint"]["revision_decisions"], accepted["checkpoint"]["revision_decisions"])
        self.assertTrue(all(line["proven"] for line in self.ticks()["lines"]))
        self.assertEqual(self.saved(), before)
        self.assertEqual(len(self.requests), calls)
        self.assertEqual(self.receipts(), receipts)
        self.assertEqual(self.remote.calls, [])

    def test_fresh_proof_closure_and_readers_preserve_question_channel_and_retired_history(self):
        self.fresh_closure("repaired-verified", "relayed")

    def test_canonical_relayed_question_closes_with_fresh_proof(self):
        self.fresh_closure("canonical", "relayed")

    def test_canonical_session_question_closes_with_fresh_proof(self):
        self.fresh_closure("canonical", "session")

    def test_superseded_session_question_closes_with_fresh_proof(self):
        self.fresh_closure("repaired-verified", "session")

    def test_question_revision_then_relayed_withdrawal_keeps_readable_retirement(self):
        self.prepare_question()
        accepted = self.answer()
        rows = self.stored()
        rows[2].pop("wait")
        rows[2].update(state="blocked_external", external={"party": "user", "need": "provide the second result",
                                                          "unblocks_when": "result is available"})
        self.write(rows)
        rows = self.stored()
        rows[1].pop("executor")
        rows[1].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "Owner removed the revised result"})
        self.write(rows, revision_authority={key: value for key, value in self.answer_authority().items()
                                             if key != "question"})
        withdrawn = deepcopy(read(self.project, "objective"))
        rows = self.stored()
        rows[2].pop("external")
        rows[2].update(state="active", executor="coordinator")
        self.write(rows)
        self.write()
        self.prove_live()
        self.tick(2)
        self.write(close=True)
        state = read(self.project, "objective")
        self.assertEqual(validate_closed_snapshot(state["checkpoint"],
            kernel_context(self.project, "objective", state, None)), ())
        self.assertEqual(state["checkpoint"]["obligations"][:2], withdrawn["checkpoint"]["obligations"][:2])
        self.assertEqual(state["checkpoint"]["obligations"][0], accepted["checkpoint"]["obligations"][0])
        self.assertEqual(state["checkpoint"]["obligations"][1]["withdrawal"]["relay"], RELAY)
        self.assertEqual(state["checkpoint"]["obligations"][1]["withdrawal"]["channel"], "relayed")


class QuestionEpochTests(QuestionCase):
    def returning_hash(self, kind, channel):
        self.prepare_question(kind)
        self.prove_live()
        original = deepcopy(self.stored()[self.index])
        self.issue.body = self.issue.body.replace("old requirement", "changed physical requirement").replace(
            "Check unchanged.", "Check changed physical scope.")
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.write(objective_source=source, questions=QUESTIONS)
        rows = self.stored()
        rows[self.index].pop("evidence")
        rows[self.index].update(check="Owner requires revised diagnostics", state="active", executor="coordinator")
        authority = self.answer_authority(channel)
        self.write(rows, questions=QUESTIONS, revision_authority=authority)
        accepted = read(self.project, "objective")
        row = accepted["checkpoint"]["obligations"][self.index]
        self.assertEqual(requirement_epoch(accepted["pod_ticks"]["criterion_scopes"][row["id"]],
            row, accepted["checkpoint"]), accepted["checkpoint"]["seq"])
        rows = self.stored()
        rows[self.index].pop("executor")
        rows[self.index].update(check=original["check"], state="satisfied", evidence=[
            {key: value for key, value in item.items() if key != "governance"} for item in original["evidence"]])
        before, calls, receipts = self.saved(), len(self.requests), self.receipts()
        error = self.failure("obligation_unaccounted", self.write, rows, questions=QUESTIONS, revision_authority=authority)
        self.assertEqual(error.detail["detail"], "evidence_invalidated")
        self.assert_no_effects(before, calls, receipts)
        rows[self.index]["evidence"][0]["reference"] = "fresh-revised-scope-proof"
        self.write(rows, revision_authority=authority)
        after = self.stored()[self.index]
        self.assertEqual(after["definition"], original["definition"])
        self.assertEqual(after["receipts"][0], original["receipts"][0])

    def test_canonical_relayed_question_epoch_refuses_stale_returning_hash_proof(self):
        self.returning_hash("canonical", "relayed")

    def test_canonical_session_question_epoch_refuses_stale_returning_hash_proof(self):
        self.returning_hash("canonical", "session")

    def test_superseded_relayed_question_epoch_refuses_stale_returning_hash_proof(self):
        self.returning_hash("repaired-verified", "relayed")

    def test_superseded_session_question_epoch_refuses_stale_returning_hash_proof(self):
        self.returning_hash("repaired-verified", "session")

    def unknown_scope(self, kind, channel):
        self.prepare_question(kind)
        self.issue.body += "\n\n## Delivery\nUnobserved source metadata.\n"
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        with patch("pod.pod_ticks.refresh"):
            self.write(objective_source=source, questions=QUESTIONS)
        accepted = self.answer(channel)
        key = accepted["checkpoint"]["obligations"][self.index]["id"]
        entry = accepted["pod_ticks"]["criterion_scopes"][key]
        self.assertIsNone(requirement_scope(entry))
        self.assertEqual(entry["acceptance"]["seq"], accepted["checkpoint"]["seq"])
        self.write()
        self.assertEqual(read(self.project, "objective")["pod_ticks"]["criterion_scopes"][key], entry)
        self.assertFalse(self.line()["proven"])

    def test_unobserved_question_scope_remains_unknown_with_actual_acceptance(self):
        self.unknown_scope("repaired-verified", "relayed")

    def test_canonical_relayed_question_scope_remains_unknown(self):
        self.unknown_scope("canonical", "relayed")

    def test_canonical_session_question_scope_remains_unknown(self):
        self.unknown_scope("canonical", "session")

    def test_superseded_session_question_scope_remains_unknown(self):
        self.unknown_scope("repaired-verified", "session")

    def metadata_scope(self, kind, channel):
        self.prepare_question(kind)
        accepted = self.answer(channel)
        key = accepted["checkpoint"]["obligations"][self.index]["id"]
        entry = accepted["pod_ticks"]["criterion_scopes"][key]
        self.issue.body = self.issue.body.replace("Check unchanged.", "Changed physical scope.").replace(
            "old requirement", "changed physical requirement")
        source = issue_intake(self.project, self.locator, port=self.issue)["source"]
        self.write(objective_source=source, questions=QUESTIONS)
        rows = self.stored()
        rows[self.index]["proof_scope"] = ["src"]
        self.write(rows, revision_authority=self.answer_authority(channel))
        after = read(self.project, "objective")["pod_ticks"]["criterion_scopes"][key]
        self.assertEqual(after["acceptance"], entry["acceptance"])
        self.assertEqual({key: value for key, value in after.items() if key != "definition"},
                         {key: value for key, value in entry.items() if key != "definition"})

    def test_question_authority_on_metadata_does_not_accept_new_physical_scope(self):
        self.metadata_scope("repaired-verified", "relayed")

    def test_canonical_relayed_question_metadata_carries_prior_scope(self):
        self.metadata_scope("canonical", "relayed")

    def test_canonical_session_question_metadata_carries_prior_scope(self):
        self.metadata_scope("canonical", "session")

    def test_superseded_session_question_metadata_carries_prior_scope(self):
        self.metadata_scope("repaired-verified", "session")

    def fallback_epoch(self, kind, channel):
        initial = self.prepare_question(kind)
        accepted = self.answer(channel)
        row = accepted["checkpoint"]["obligations"][self.index]
        before = self.saved()
        # Synthetic earlier representation: no per-criterion acceptance or scope.
        entry = {"definition": row["definition"]}
        self.assertEqual(requirement_epoch(entry, row, accepted["checkpoint"]), accepted["checkpoint"]["seq"])
        self.assertIsNone(requirement_scope(entry))
        self.assertEqual(entry, {"definition": row["definition"]})
        original = initial["checkpoint"]["obligations"][self.index]
        original_entry = initial["pod_ticks"]["criterion_scopes"][original["id"]]
        # Actual explicit acceptance wins over a conservative history fallback.
        self.assertEqual(requirement_epoch(original_entry, original, accepted["checkpoint"]),
                         original_entry["acceptance"]["seq"])
        self.assertEqual(self.saved(), before)

    def test_pre_extension_epoch_uses_accepted_question_without_inventing_scope(self):
        self.fallback_epoch("repaired-verified", "relayed")

    def test_canonical_relayed_question_epoch_fallback_keeps_unknown_scope(self):
        self.fallback_epoch("canonical", "relayed")

    def test_canonical_session_question_epoch_fallback_keeps_unknown_scope(self):
        self.fallback_epoch("canonical", "session")

    def test_superseded_session_question_epoch_fallback_keeps_unknown_scope(self):
        self.fallback_epoch("repaired-verified", "session")


class QuestionRefusalTests(QuestionCase):
    def test_absent_and_same_write_questions_cannot_authorize_a_revision(self):
        self.prepare_question()
        for reference, questions in (("missing", QUESTIONS), ("same-write-question", ["same-write-question"])):
            with self.subTest(reference=reference):
                self.refuse_answer("decision_question_unrecorded", self.answer_authority(question=reference), questions=questions)

    def test_incomplete_and_disguised_question_relays_refuse_atomically(self):
        self.prepare_question()
        for changes, code in (({"relay": None}, "invalid_decision_relay"),
                              ({"channel": "session"}, "invalid_decision_relay"),
                              ({"relay": {"reference": "partial"}}, "invalid_decision_relay"),
                              ({"relay": {**RELAY, "owner_source": ""}}, "invalid_owner_source"),
                              ({"channel": "mailbox"}, "invalid_decision_channel")):
            with self.subTest(changes=changes):
                authority = {**self.answer_authority(), **changes}
                if authority["relay"] is None:
                    authority.pop("relay")
                self.refuse_answer(code, authority)

    def test_generic_relay_and_historical_question_cannot_authorize_a_new_check(self):
        self.prepare_question()
        self.answer()
        authority = self.answer_authority()
        authority.pop("question")
        self.refuse_answer("relayed_decision_refused", authority)
        rows = self.stored()
        rows[1]["check"] += " Further unauthorized check."
        before, calls, receipts = self.saved(), len(self.requests), self.receipts()
        error = self.failure("obligation_invalid", self.write, rows)
        self.assertEqual(error.detail["detail"], "redefinition_unauthorized")
        self.assert_no_effects(before, calls, receipts)

    def test_historical_question_requires_accepted_sequence_rows_source_and_mapping(self):
        self.prepare_question()
        accepted = self.answer()
        prior = accepted["checkpoint"]
        rows = deepcopy({row["id"]: row for row in prior["obligations"]})
        ctx = kernel_context(self.project, "objective", accepted, None)
        self.assertEqual(superseded_originals(prior, rows, ctx), {"O1"})
        before = self.saved()
        for kind in ("missing-question", "old-question", "future-question", "current-check", "source",
                     "context-source", "criteria", "retirement"):
            with self.subTest(kind=kind):
                p, r, c = deepcopy((prior, rows, ctx))
                if kind == "missing-question":
                    p["revision_decisions"].pop()
                elif kind == "old-question":
                    p["revision_decisions"][-1]["seq"] = p["revision_decisions"][0]["seq"]
                elif kind == "future-question":
                    p["revision_decisions"][-1]["seq"] = p["seq"] + 1
                elif kind == "current-check":
                    r["R1"]["check"] += " Unaccepted check."
                    r["R1"]["definition"] = definition_id(r["R1"])
                elif kind == "source":
                    r["R1"]["source"] = {"ref": "PoD#99"}
                    r["R1"]["definition"] = definition_id(r["R1"])
                elif kind == "context-source":
                    c["pod_mapping_source"]["repository"] = "acme/foreign"
                elif kind == "criteria":
                    c["criteria"] = list(reversed(c["criteria"]))
                else:
                    r["O1"]["withdrawal"]["decision"] += 1
                self.assertEqual(superseded_originals(p, r, c), set())
        self.assertEqual(self.saved(), before)

    def test_malformed_stored_question_relay_keeps_shared_validation(self):
        self.prepare_question()
        accepted = self.answer()
        before = self.saved()
        for changes, code in (({"channel": "session"}, "invalid_decision_relay"),
                              ({"relay": {"reference": "partial"}}, "invalid_decision_relay"),
                              ({"relay": {**RELAY, "owner_source": ""}}, "invalid_owner_source")):
            with self.subTest(changes=changes):
                prior = deepcopy(accepted["checkpoint"])
                prior["revision_decisions"][-1].update(changes)
                rows = {row["id"]: row for row in prior["obligations"]}
                ctx = kernel_context(self.project, "objective", accepted, None)
                for callback in (lambda: validate_map_record(prior), lambda: superseded_originals(prior, rows, ctx)):
                    with self.assertRaises(PodError) as caught:
                        callback()
                    self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.saved(), before)
