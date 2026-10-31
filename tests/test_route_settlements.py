"""Route holds settle from fresh kernel evidence without erasing their history."""
from tests.history_support import admission_movable
from copy import deepcopy
import json
import shutil
import unittest
from unittest.mock import patch

from pod.assurance import pending_serving
from pod.errors import PodError

from pod.internal import acknowledge, run
from pod.ledger import objective_root, read, update_admission
from pod.operations import OrcaPort
from pod.util import route_discarded, route_flags, route_hold, route_next_action
from tests.issue41_support import ProductionCase


WAIT = {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}


class SettlementCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = cls.case = ProductionCase(); case.setUp()
        cls.addClassCleanup(case.doCleanups)
        case.intake(case.sub("S"), case.sub("T"))
        prepared = case.op("governor-prepare", unit="default",
                           branch={"remote": "origin", "branch": "delivery", "base": "target"})
        candidate = prepared["candidate"]
        case.authorization = {"schema": "pod-authorization/v1", "candidate": candidate["commit"],
            "tree": candidate["tree"], "scope": ["publish"], "authorized_by": "owner",
            "utc": "2026-01-01T00:00:00Z", "reference": "fixture-authorization"}
        cls.root = objective_root(case.project, "objective")
        cls.baseline = cls.files()
        cls.native = deepcopy(case.port.__dict__)

    @classmethod
    def files(cls):
        return {str(path.relative_to(cls.root)): path.read_bytes() for path in cls.root.rglob("*") if path.is_file()}

    def setUp(self):
        self.c = type(self).case
        shutil.rmtree(self.root)
        for relative, data in self.baseline.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        self.c.port.__dict__.clear(); self.c.port.__dict__.update(deepcopy(self.native))

    def state(self):
        return read(self.c.project, "objective")

    def row(self, admission):
        return self.state()["admissions"][admission["admission_id"]]

    def parked(self, serves=("S",)):
        return {"seq": self.state()["checkpoint"]["seq"] + 1, "update": {key: deepcopy(WAIT) for key in serves}}

    def start(self, task="original", serves=("S",), **extra):
        frozen = self.c.packet(list(serves), **extra)
        admission = self.c.start(task, frozen)["admission"]
        self.c.settle(admission)
        return admission, frozen

    def flag(self, admission, frozen):
        with patch.object(self.c.port, "effective", {"agent": "codex", "model": "gpt-6-luna", "effort": "medium"}):
            with self.assertRaises(PodError) as caught:
                self.c.report(admission, frozen)
        self.assertEqual(caught.exception.code, "route_mismatch")
        self.assertTrue(route_hold(self.row(admission)))
        return caught.exception

    def original(self, serves=("S",), **extra):
        admission, frozen = self.start(serves=serves, **extra)
        self.flag(admission, frozen)
        return admission, frozen

    def lead(self, admission, decision="discard", **extra):
        return {"admission": admission["admission_id"], "basis": "lead_decision", "decision": decision,
                "source": {"reference": "repository-task/lead-decision"},
                "reason": "The project lead delegates this result decision", **extra}

    def dispose(self, admission, kind="discarded", **extra):
        record = {"admission": admission["admission_id"], **extra}
        if kind == "discarded":
            record.update(discarded=True, reason="the attempt is superseded")
        else:
            record.update(integrated_into=self.c.candidate, attestation="result is included in this candidate")
        return record

    def replacement(self, original, frozen, *, serves=None, outcome="succeeded", route=None):
        self.c.write(**self.parked(original["serves"]), dispositions=[self.dispose(original)])
        options = {}
        if route:
            options["route"] = {**frozen["body"]["route"], **route}
        replacement, body = self.start("replacement", serves or original["serves"], **options)
        self.c.report(replacement, body, outcome=outcome, map=self.parked(replacement["serves"]))
        return replacement, body

    def acceptance(self):
        passing = {"schema": "pod-evidence/v1", "criterion": "PoD#1", "candidate": self.c.candidate,
            "sources": [], "policy_revision": "p", "dependencies": [], "environment": "fixture",
            "check": "unit", "command": "unit", "result": "passed", "timestamp": "2026-09-24T00:00:00Z",
            "status": "PASS", "reference": "fixture-log"}
        return run("acceptance", {"project": str(self.c.project), "objective": "objective",
            "criteria": ["PoD#1"], "evidence_rows": [passing], "candidate": self.c.candidate,
            "policy_revision": "p", "sources": [], "dependencies": [], "environment": "fixture",
            "review_required": False, "hosted_required": False,
            "owner_acceptance": {"schema": "pod-acceptance-authorization/v1", "candidate": self.c.candidate,
                "policy_revision": "p", "utc": "2026-09-24T00:00:00Z", "accepted_by": "owner"}})

    def assert_held(self, admission):
        verdict = self.c.op("governor", action=self.c.action())
        self.assertEqual(verdict["decision"], "DEFER")
        self.assertIn("route_mismatch", [row["code"] for row in verdict["reasons"]])
        self.assertEqual(self.acceptance()["route_holds"], [{"admission": admission["admission_id"], "reason": "route_mismatch"}])

    def assert_released(self, admission, basis):
        row = self.row(admission)
        self.assertTrue(route_flags(row)[0])
        self.assertFalse(route_hold(row))
        self.assertEqual(row["route_decision"]["settlements"][-1]["basis"], basis)
        self.assertTrue(self.acceptance()["accepted"])
        self.assertEqual(self.acceptance()["route_holds"], [])
        verdict = self.c.op("governor", action=self.c.action())
        self.assertEqual(verdict["decision"], "ALLOW", verdict)

    def refused_write(self, detail, **value):
        before = self.files()
        with self.assertRaises(PodError) as caught:
            self.c.write(**value)
        self.assertEqual(caught.exception.detail["detail"], detail, str(caught.exception))
        self.assertEqual(self.files(), before)
        return caught.exception


class MatchingReportTests(SettlementCase):
    def test_matching_consumption_releases_governor_and_acceptance_without_erasing_flag(self):
        admission, frozen = self.original()
        self.assert_held(admission)
        old = deepcopy(self.row(admission))
        self.c.report(admission, frozen, map=self.parked())
        self.assert_released(admission, "matching_report")
        row = self.row(admission)
        self.assertEqual(row["effective_evidence"], old["effective_evidence"])
        self.assertEqual(row["route_decision"]["settlements"][0]["report_digest"], row["report"]["observation_digest"])

    def test_legacy_flag_remains_held_until_fresh_report(self):
        admission, frozen = self.original()
        update_admission(self.c.project, "objective", owner="owner", admission_id=admission["admission_id"],
                         update=lambda row: row["route_decision"].pop("mismatch_occurrence"))
        self.assertTrue(route_hold(self.row(admission)))
        self.c.report(admission, frozen, map=self.parked())
        self.assertEqual(self.row(admission)["route_decision"]["settlements"][0]["occurrence"], 1)

    def test_legacy_consumed_report_alone_does_not_derive_a_settlement(self):
        admission, frozen = self.start()
        self.c.report(admission, frozen, map=self.parked())
        self.flag(admission, frozen)
        update_admission(self.c.project, "objective", owner="owner", admission_id=admission["admission_id"],
                         update=lambda row: row["route_decision"].pop("mismatch_occurrence"))
        self.assertTrue(route_hold(self.row(admission)))
        self.assertIsNotNone(self.row(admission)["report"])
        self.c.report(admission, frozen)
        self.assertFalse(route_hold(self.row(admission)))

    def test_record_size_refusal_publishes_no_settlement_or_report(self):
        import pod.history as history
        admission, frozen = self.original()
        before = self.files()
        with patch.object(history, "preflight_landing", side_effect=PodError("storage_full", "injected disk full")), self.assertRaises(PodError) as caught:
            self.c.report(admission, frozen, map=self.parked())
        self.assertEqual(caught.exception.code, "storage_full")
        self.assertEqual(self.files(), before)
        self.assertTrue(route_hold(self.row(admission)))

    def test_identical_replay_persists_settlement_before_no_write_return(self):
        admission, frozen = self.start()
        self.c.report(admission, frozen, map=self.parked())
        self.flag(admission, frozen)
        before = self.state()
        self.c.report(admission, frozen)
        after = self.state()
        self.assertFalse(route_hold(self.row(admission)))
        self.assertEqual(after["checkpoint"], before["checkpoint"])
        self.assertEqual(after["admissions"][admission["admission_id"]]["report"], before["admissions"][admission["admission_id"]]["report"])
        saved = self.files()
        self.c.report(admission, frozen)
        self.assertEqual(self.files(), saved)

    def test_refused_report_map_does_not_settle_the_route(self):
        admission, frozen = self.original()
        before = self.files()
        with self.assertRaises(PodError):
            self.c.report(admission, frozen, map={"seq": 10000})
        self.assertEqual(self.files(), before)
        self.assertTrue(route_hold(self.row(admission)))
        self.assertIsNone(self.row(admission)["report"])

    def test_new_mismatch_after_settlement_starts_another_occurrence(self):
        admission, frozen = self.original()
        self.c.report(admission, frozen, map=self.parked())
        first = deepcopy(self.row(admission)["route_decision"]["settlements"])
        self.flag(admission, frozen); self.flag(admission, frozen)
        self.assertEqual(self.row(admission)["route_decision"]["mismatch_occurrence"], 2)
        self.assertEqual(self.row(admission)["route_decision"]["settlements"], first)
        self.c.report(admission, frozen)
        self.assertEqual([entry["occurrence"] for entry in self.row(admission)["route_decision"]["settlements"]], [1, 2])

    def test_failed_matching_report_settles_route_without_passing_work(self):
        admission, frozen = self.original()
        self.c.report(admission, frozen, outcome="failed", map=self.parked())
        row = self.row(admission)
        self.assertFalse(route_hold(row))
        self.assertEqual(row["report"]["outcome"], "failed")
        self.assertIsNone(row["disposition"])
        self.assertEqual(next(ob for ob in self.c.stored() if ob["id"] == "S")["state"], "waiting")

    def test_matching_a_wrong_recorded_route_never_bypasses_the_admitted_request(self):
        with patch.object(self.c.port, "effective", {"agent": "codex", "model": "gpt-6-luna", "effort": "medium"}):
            admission, frozen = self.start()
            with self.assertRaises(PodError) as caught:
                self.c.report(admission, frozen)
        self.assertEqual(caught.exception.code, "route_mismatch")
        self.assertTrue(route_hold(self.row(admission)))
        self.assertIsNone(self.row(admission)["report"])

    def test_settlement_preserves_history_movement_guards(self):
        admission, frozen = self.original()
        self.c.report(admission, frozen, map=self.parked())
        self.c.write(dispositions=[self.dispose(admission)])
        state = self.state(); row = state["admissions"][admission["admission_id"]]
        state["checkpoint"]["candidate"] = "b" * 40
        self.assertTrue(admission_movable(state, admission["admission_id"], row))
        self.assertFalse(admission_movable(state, admission["admission_id"], row, {admission["admission_id"]}))
        row["report"] = None
        self.assertFalse(admission_movable(state, admission["admission_id"], row))


class ExplicitSettlementTests(SettlementCase):
    def test_redone_releases_only_after_successful_integrated_exact_coverage(self):
        original, frozen = self.original(("S", "T"))
        self.assert_held(original)
        replacement, _ = self.replacement(original, frozen)
        self.c.write(dispositions=[self.dispose(replacement, "integrated_into")], route_settlements=[{
            "admission": original["admission_id"], "basis": "redone", "replacement": replacement["admission_id"]}])
        self.assert_released(original, "redone")
        self.assertIsNone(self.row(original)["report"])

    def test_partial_coverage_cannot_settle_a_whole_attempt(self):
        original, frozen = self.original(("S", "T"))
        replacement, _ = self.replacement(original, frozen, serves=["S"])
        self.refused_write("replacement_coverage", dispositions=[self.dispose(replacement, "integrated_into")],
            route_settlements=[{"admission": original["admission_id"], "basis": "redone", "replacement": replacement["admission_id"]}])

    def test_failed_replacement_cannot_settle(self):
        original, frozen = self.original()
        replacement, _ = self.replacement(original, frozen, outcome="failed")
        self.refused_write("replacement_unqualified", dispositions=[self.dispose(replacement, "integrated_into")],
            route_settlements=[{"admission": original["admission_id"], "basis": "redone", "replacement": replacement["admission_id"]}])

    def test_replacement_on_another_admitted_route_cannot_settle(self):
        original, frozen = self.original()
        replacement, _ = self.replacement(original, frozen, route={"model": "gpt-6-luna"})
        self.refused_write("replacement_unqualified", dispositions=[self.dispose(replacement, "integrated_into")],
            route_settlements=[{"admission": original["admission_id"], "basis": "redone", "replacement": replacement["admission_id"]}])

    def test_lead_accept_needs_integration_and_releases_without_changing_observations(self):
        admission, frozen = self.original()
        self.assert_held(admission)
        self.c.report(admission, frozen, map=self.parked())
        self.flag(admission, frozen)
        self.assertIn("decision accept with ordinary disposition integrated_into", route_next_action(self.row(admission)))
        self.refused_write("disposition_required", route_settlements=[self.lead(admission, "accept")])
        self.c.write(dispositions=[self.dispose(admission, "integrated_into")], route_settlements=[self.lead(admission, "accept")])
        self.assert_released(admission, "lead_decision")

    def test_unreported_git_result_offers_only_discard_or_redone_lead_steps(self):
        admission, frozen = self.original()
        row = self.row(admission)
        self.assertIsNone(row["report"])
        self.assertIsNotNone(row["result"]["base"])
        action = route_next_action(row)
        self.assertIn("decision discard with ordinary disposition discarded", action)
        self.assertIn("route_settlements basis redone", action)
        self.assertNotIn("decision accept", action)
        self.assertNotIn("integrated_into", action)
        self.refused_write("disposition_invalid", **self.parked(),
            dispositions=[self.dispose(admission, "integrated_into")], route_settlements=[self.lead(admission, "accept")])
        self.flag(admission, frozen)
        self.c.write(**self.parked(), dispositions=[self.dispose(admission)], route_settlements=[self.lead(admission)])
        self.assertFalse(route_hold(self.row(admission)))
        self.assertIsNone(self.row(admission)["report"])

    def test_unreported_result_without_git_base_offers_accept_by_attestation(self):
        admission, _ = self.original()
        # Model the admission metadata of an implementation outside Git.
        update_admission(self.c.project, "objective", owner="owner", admission_id=admission["admission_id"],
                         update=lambda row: row["result"].update(base=None))
        self.assertIn("decision accept with ordinary disposition integrated_into", route_next_action(self.row(admission)))
        self.c.write(**self.parked(), dispositions=[self.dispose(admission, "integrated_into")],
                     route_settlements=[self.lead(admission, "accept")])
        row = self.row(admission)
        self.assertEqual(row["disposition"]["validated"], "attestation")
        self.assertIsNone(row["report"])
        self.assertFalse(route_hold(row))

    def test_lead_discard_releases_an_unreported_implementation_only_with_disposition(self):
        admission, _ = self.original()
        self.assert_held(admission)
        self.c.write(**self.parked(), dispositions=[self.dispose(admission)], route_settlements=[self.lead(admission)])
        self.assert_released(admission, "lead_decision")
        self.assertIsNone(self.row(admission)["report"])
        self.assertFalse(route_discarded(self.row(admission)))
        view = self.c.op("map")
        self.assertIn("does not authenticate the lead", view["route_settlement_authority"])

    def test_a_refused_multi_settlement_write_settles_nothing(self):
        original, _ = self.original()
        entry = self.lead(original)
        self.refused_write("route_not_held", **self.parked(), dispositions=[self.dispose(original)],
                           route_settlements=[entry, {**entry, "admission": "0" * 64}])
        self.assertTrue(route_hold(self.row(original)))
        self.assertIsNone(self.row(original)["disposition"])

    def test_map_refusal_precedes_settlement_validation(self):
        admission, _ = self.original()
        self.refused_write("map_stale", seq=10000, route_settlements=[{"invalid": True}])
        self.assertTrue(route_hold(self.row(admission)))

    def test_null_settlements_are_not_a_list(self):
        error = self.refused_write("malformed", route_settlements=None)
        self.assertEqual(error.detail["referent"]["record"], "route settlements")

    def test_redone_resolves_both_served_sets_through_current_renewal_tips(self):
        original, _ = self.original()
        self.c.write(**self.parked(), dispositions=[self.dispose(original)])
        self.c.write(renew=[{"id": "S", "as": "S2", **deepcopy(WAIT)}])
        replacement, frozen = self.start("replacement", ["S2"])
        self.c.report(replacement, frozen, map=self.parked(["S2"]))
        self.c.write(dispositions=[self.dispose(replacement, "integrated_into")], route_settlements=[{
            "admission": original["admission_id"], "basis": "redone", "replacement": replacement["admission_id"]}])
        self.assertFalse(route_hold(self.row(original)))

    def test_redone_rejects_self_and_earlier_references(self):
        earlier, frozen = self.start("earlier")
        self.c.report(earlier, frozen, map=self.parked())
        self.c.write(dispositions=[self.dispose(earlier, "integrated_into")])
        original, frozen = self.original()
        for replacement in (original, earlier):
            with self.subTest(replacement=replacement["admission_id"]):
                self.refused_write("replacement_unqualified", **self.parked(), dispositions=[self.dispose(original)],
                    route_settlements=[{"admission": original["admission_id"], "basis": "redone", "replacement": replacement["admission_id"]}])

    def test_report_accompanying_map_can_settle_a_different_admission(self):
        original, _ = self.original()
        self.c.write(**self.parked(), dispositions=[self.dispose(original)])
        reporter, frozen = self.start("other", ["T"])
        result = self.c.report(reporter, frozen, map={**self.parked(["T"]), "route_settlements": [self.lead(original)]})
        self.assertFalse(route_hold(self.row(original)))
        self.assertEqual(result["route_settlements"][-1]["admission"], original["admission_id"])

    def test_matching_replay_and_redone_can_settle_both_occurrences_in_one_report_write(self):
        original, frozen = self.original()
        replacement, body = self.replacement(original, frozen)
        self.c.write(dispositions=[self.dispose(replacement, "integrated_into")])
        self.flag(replacement, body)
        self.c.report(replacement, body, map={"route_settlements": [{
            "admission": original["admission_id"], "basis": "redone", "replacement": replacement["admission_id"]}]})
        self.assertFalse(route_hold(self.row(original)))
        self.assertFalse(route_hold(self.row(replacement)))

    def test_a_changed_obligation_definition_needs_the_lead_basis(self):
        original, _ = self.original()
        self.c.write(**self.parked(), dispositions=[self.dispose(original)])
        self.c.write(seq=self.state()["checkpoint"]["seq"] + 1, update={"S": {"check": "different required work"}},
                     revision_authority={"provenance": "user_direct", "instruction": "change the required work"})
        replacement, frozen = self.start("replacement")
        self.c.report(replacement, frozen, map=self.parked())
        self.refused_write("replacement_coverage", dispositions=[self.dispose(replacement, "integrated_into")], route_settlements=[{
            "admission": original["admission_id"], "basis": "redone", "replacement": replacement["admission_id"]}])

    def test_changed_assignment_boundary_needs_the_lead_basis(self):
        original, _ = self.original()
        self.c.write(**self.parked(), dispositions=[self.dispose(original)])
        replacement, frozen = self.start("replacement", boundary={"paths": ["src"]})
        self.c.report(replacement, frozen, map=self.parked())
        self.refused_write("replacement_coverage", dispositions=[self.dispose(replacement, "integrated_into")], route_settlements=[{
            "admission": original["admission_id"], "basis": "redone", "replacement": replacement["admission_id"]}])

    def test_unknown_effective_route_keeps_its_independent_hold_after_lead_settlement(self):
        admission, _ = self.original()
        update_admission(self.c.project, "objective", owner="owner", admission_id=admission["admission_id"],
                         update=lambda row: row["route_decision"].update(effective_unknown=True))
        self.c.write(**self.parked(), dispositions=[self.dispose(admission)], route_settlements=[self.lead(admission)])
        self.assertFalse(route_hold(self.row(admission)))
        self.assertEqual(self.acceptance()["route_holds"], [{"admission": admission["admission_id"], "reason": "effective_unknown"}])
        self.assertEqual([reason["code"] for reason in self.c.op("governor", action=self.c.action())["reasons"]], ["effective_unknown"])

    def test_input_bounds_and_exact_basis_shapes_refuse_atomically(self):
        admission, _ = self.original()
        good = self.lead(admission)
        cases = [([good] * 17, "malformed"), ([{**good, "provenance": "user_direct"}], "malformed"),
                 ([{**good, "source": {"reference": "x" * 513}}], "malformed"),
                 ([{**good, "reason": "x" * 1025}], "malformed")]
        for entries, detail in cases:
            with self.subTest(fields=list(entries[0])):
                self.refused_write(detail, **self.parked(), dispositions=[self.dispose(admission)], route_settlements=entries)


class ReviewDiscardTests(SettlementCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        case = cls.case
        case.write([*case.stored(), {"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
            "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
            "candidate": case.candidate, "existing_evidence": "tests", "insufficiency": "no review", **deepcopy(WAIT)}])
        frozen = case.packet(["A"], role="review")
        earlier = case.start("earlier", frozen)["admission"]; case.settle(earlier)
        case.report(earlier, frozen, outcome="failed", map={"update": {"A": deepcopy(WAIT)},
            "seq": read(case.project, "objective")["checkpoint"]["seq"] + 1},
            triage=[{"finding": "earlier", "severity": "minor", "triage": "advisory", "summary": "retained finding"}])
        cls.baseline = cls.files(); cls.native = deepcopy(case.port.__dict__)

    def review(self):
        return self.original(("A",), role="review")

    def discard(self, admission, **fields):
        return self.lead(admission, corrections=[], reason="No required findings are known from the refused attempt", **fields)

    def test_terminal_discard_releases_reservations_and_keeps_review_incomplete(self):
        admission, frozen = self.review()
        self.assert_held(admission)
        before = deepcopy(next(row for row in self.c.stored() if row["id"] == "A")["findings"])
        self.assertIn(admission["admission_id"], pending_serving({"id": "A"}, {"admissions": self.state()["admissions"]}))
        self.c.write(**self.parked(["A"]), route_settlements=[self.discard(admission)])
        row = self.row(admission)
        self.assertTrue(route_discarded(row)); self.assertFalse(route_hold(row))
        self.assertIsNone(row["report"])
        self.assertEqual(pending_serving({"id": "A"}, {"admissions": self.state()["admissions"]}), [])
        self.assertEqual(next(row for row in self.c.stored() if row["id"] == "A")["findings"], before)
        self.assertEqual(self.acceptance()["route_holds"], [])
        self.assertFalse(self.acceptance()["accepted"])
        self.assertEqual(self.c.op("governor", action=self.c.action())["decision"], "ALLOW")
        self.c.start("replacement", frozen)

    def test_terminal_discard_and_renewal_commit_together(self):
        admission, _ = self.review()
        old = deepcopy(next(row for row in self.c.stored() if row["id"] == "A"))
        self.c.write(renew=[{"id": "A", "as": "A2", **deepcopy(WAIT)}], route_settlements=[self.discard(admission)])
        rows = {row["id"]: row for row in self.c.stored()}
        self.assertEqual(rows["A"]["state"], "renewed")
        self.assertEqual(rows["A"]["findings"], old["findings"])
        self.assertEqual(rows["A2"].get("receipts", []), [])
        self.assertFalse(route_hold(self.row(admission)))

    def test_invalid_discard_cannot_release_a_same_write_renewal(self):
        admission, _ = self.review()
        self.refused_write("corrections_required", renew=[{"id": "A", "as": "A2", **deepcopy(WAIT)}],
                           route_settlements=[self.lead(admission, corrections=["missing"])])
        self.assertTrue(route_hold(self.row(admission)))

    def test_known_required_findings_remain_ordinary_corrections_after_discard_and_renewal(self):
        admission, _ = self.review()
        correction = {"id": "C", "kind": "correction", "provenance": "coordinator", "parent": "A",
                      "check": "repair the required finding from the refused attempt", **deepcopy(WAIT)}
        rows = [dict(row, **deepcopy(WAIT)) if row["id"] == "A" else row for row in self.c.stored()]
        next(row for row in rows if row["id"] == "A").pop("executor", None)
        self.c.write([*rows, correction], route_settlements=[self.lead(admission, corrections=["C"])])
        self.c.write(renew=[{"id": "A", "as": "A2", **deepcopy(WAIT)}])
        self.assertEqual(next(row for row in self.c.stored() if row["id"] == "C")["check"], correction["check"])
        self.assertEqual(self.row(admission)["route_decision"]["settlements"][-1]["corrections"], ["C"])

    def discarded_tab_guidance(self, role):
        from pod.worker_tabs import project
        serves = "A" if role == "review" else "S"
        fields = {"resolves": "which correction is required?", "stop_condition": "a supported correction is identified"}
        admission, _ = self.original((serves,), role=role, **(fields if role == "investigate" else {}))
        correction = {"id": "C", "kind": "correction", "provenance": "coordinator", "parent": "A",
                      "check": "repair the required finding from the refused attempt", **deepcopy(WAIT)}
        rows = [dict(row, **deepcopy(WAIT)) if row["id"] == serves else row for row in self.c.stored()]
        next(row for row in rows if row["id"] == serves).pop("executor", None)
        self.c.write([*rows, correction], route_settlements=[self.lead(admission, corrections=["C"])])
        row = self.row(admission)
        binding = row["native_binding"]
        before = self.files()
        for ownership in ("user_owned", "external"):
            facts = {"terminal_ownership": {"ownershipState": ownership, "releaseState": "released",
                     "ownerDispatchId": binding["dispatchId"]},
                     "terminal_readback": {"connected": True, "handle": binding["terminalHandle"]}}
            for ended in (None, "closure", "continued_by", "historical_retirement"):
                with self.subTest(ownership=ownership, ended=ended):
                    state = self.state()
                    if ended == "closure":
                        state["checkpoint"]["closure"] = {"revision": state["checkpoint"]["revision"]}
                    elif ended:
                        state[ended] = {"recorded": True}
                    tab = project(state, row, facts, settled=True)
                    self.assertEqual(tab["row"], ownership)
                    self.assertIsNone(tab["command"])
                    self.assertIn("Project lead:", tab["wording"])
                    self.assertIn(admission["admission_id"], tab["wording"])
                    self.assertIn("no standing close command", tab["wording"])
                    if ended:
                        self.assertIn("successor or repository task", tab["wording"])
                        self.assertNotIn("this objective's map", tab["wording"])
                    else:
                        self.assertIn("its corrections remain in this objective's map", tab["wording"])
                        self.assertNotIn("successor or repository task", tab["wording"])
                    for unavailable in ("owner", "reopen", "internal report", "internal checkpoint"):
                        self.assertNotIn(unavailable, tab["wording"].lower())
                    self.assertEqual(next(ob for ob in state["checkpoint"]["obligations"] if ob["id"] == "C")["check"],
                                     correction["check"])
        self.assertEqual(self.files(), before)

    def test_discarded_review_tab_keeps_corrections_on_open_and_ended_records(self):
        self.discarded_tab_guidance("review")

    def test_discarded_investigation_tab_keeps_corrections_on_open_and_ended_records(self):
        self.discarded_tab_guidance("investigate")

    def test_terminal_discard_refuses_every_later_report_before_native_reads(self):
        admission, frozen = self.review()
        self.c.write(**self.parked(["A"]), route_settlements=[self.discard(admission)])
        before = self.files()
        with patch.object(OrcaPort, "show_worker", side_effect=AssertionError("terminal discard has no report route")):
            for outcome in ("succeeded", "failed"):
                with self.subTest(outcome=outcome), self.assertRaises(PodError) as caught:
                    self.c.report(admission, frozen, outcome=outcome)
                self.assertEqual(caught.exception.code, "route_settled_discarded")
        self.assertEqual(self.files(), before)

    def test_consumed_review_refuses_terminal_discard_even_after_another_mismatch(self):
        admission, frozen = self.review()
        self.c.report(admission, frozen, map=self.parked(["A"]))
        self.flag(admission, frozen)
        self.refused_write("discard_unavailable", route_settlements=[self.discard(admission)])

    def test_identical_triage_replay_settles_before_its_no_write_return(self):
        admission, frozen = self.review()
        finding = {"finding": "replayed", "severity": "minor", "triage": "advisory", "summary": "retained"}
        self.c.report(admission, frozen, map=self.parked(["A"]), triage=[finding])
        self.flag(admission, frozen)
        prior = self.state()["checkpoint"]
        self.c.report(admission, frozen, triage=[finding])
        self.assertEqual(self.state()["checkpoint"], prior)
        self.assertFalse(route_hold(self.row(admission)))

    def test_investigation_discard_is_terminal_without_supplying_a_report(self):
        admission, frozen = self.original(("S",), role="investigate", resolves="bounded question", stop_condition="answer recorded")
        self.c.write(**self.parked(), route_settlements=[self.discard(admission)])
        self.assertTrue(route_discarded(self.row(admission)))
        self.assertIsNone(self.row(admission)["report"])
        with self.assertRaises(PodError) as caught:
            self.c.report(admission, frozen)
        self.assertEqual(caught.exception.code, "route_settled_discarded")

    def test_review_requires_corrections_list_and_cannot_accept_or_discard_live_work(self):
        admission, _ = self.review()
        self.refused_write("malformed", **self.parked(["A"]), route_settlements=[self.lead(admission)])
        self.refused_write("accept_unavailable", **self.parked(["A"]), route_settlements=[self.lead(admission, "accept")])
        self.c.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = "in_progress"
        self.refused_write("discard_unavailable", route_settlements=[self.discard(admission)])

    def test_lead_accepts_a_later_review_mismatch_after_rebind_without_replaying_proof(self):
        admission, frozen = self.review()
        self.c.report(admission, frozen, map=self.parked(["A"]))
        self.flag(admission, frozen)
        self.c.port.runtime = self.c.port.placement["runtime"] = "next-runtime"
        self.c.write()
        with self.assertRaises(PodError) as caught:
            self.c.report(admission, frozen)
        self.assertEqual(caught.exception.code, "report_conflict")
        self.refused_write("discard_unavailable", route_settlements=[self.discard(admission)])
        action = route_next_action(self.row(admission))
        self.assertIn("decision accept", action)
        self.assertIn("while the consumed report stands as recorded", action)
        for unavailable in ("decision discard", "corrections", "basis redone"):
            self.assertNotIn(unavailable, action)
        before = self.state()
        self.c.write(route_settlements=[self.lead(admission, "accept")])
        after = self.state()
        self.assertEqual(self.row(admission)["report"], before["admissions"][admission["admission_id"]]["report"])
        self.assertEqual(after["checkpoint"]["obligations"], before["checkpoint"]["obligations"])
        self.assertFalse(route_hold(self.row(admission)))
        self.assertFalse(route_discarded(self.row(admission)))
        self.assertNotIn("corrections", self.row(admission)["route_decision"]["settlements"][-1])

    def test_lead_accepts_consumed_investigation_route_evidence_without_inventing_success(self):
        admission, frozen = self.original(("S",), role="investigate", resolves="bounded question", stop_condition="answer recorded")
        self.c.report(admission, frozen, outcome="failed", map=self.parked())
        self.flag(admission, frozen)
        action = route_next_action(self.row(admission))
        self.assertIn("decision accept", action)
        self.assertNotIn("decision discard", action)
        self.c.write(route_settlements=[self.lead(admission, "accept")])
        self.assertFalse(route_hold(self.row(admission)))
        self.assertEqual(self.row(admission)["report"]["outcome"], "failed")


class SettlementReadTests(SettlementCase):
    def test_hydration_refuses_arbitrary_or_changed_settlement_records(self):
        admission, frozen = self.original()
        self.c.report(admission, frozen, map=self.parked())
        path = self.root / "context.json"
        from tests.common import logical_json
        original = json.dumps(logical_json(path)).encode()
        for patch_value in ({"basis": "lead_decision"}, {"extra": True}, {"occurrence": 0}, {"report_digest": "0" * 64}):
            with self.subTest(changed=list(patch_value)):
                value = json.loads(original)
                entry = value["admissions"][admission["admission_id"]]["route_decision"]["settlements"][0]
                entry.update(patch_value)
                path.write_text(json.dumps(value))
                with self.assertRaises(PodError) as caught:
                    self.state()
                self.assertEqual(caught.exception.code, "state_unsupported")
                path.write_bytes(original)

    def test_runtime_rebind_keeps_settlement_and_consumed_report_identity(self):
        admission, frozen = self.original()
        self.c.report(admission, frozen, map=self.parked())
        before = self.row(admission)
        self.c.port.runtime = self.c.port.placement["runtime"] = "next-runtime"
        self.c.write()
        after = self.row(admission)
        self.assertEqual(after["runtime"], "next-runtime")
        self.assertEqual(after["report"], before["report"])
        self.assertEqual(after["route_decision"], before["route_decision"])
        self.assertFalse(route_hold(after))

    def test_settlement_lifetime_has_no_sixteen_entry_cap_and_projection_stays_bounded(self):
        from pod.util import flag_route_mismatch, matching_route_settlement, route_settlement_projection
        admission, frozen = self.original()
        self.c.report(admission, frozen, map=self.parked())
        row = self.row(admission)
        for _ in range(65):
            flag_route_mismatch(row); matching_route_settlement(row)
        self.assertEqual(len(row["route_decision"]["settlements"]), 66)
        self.assertFalse(route_hold(row))
        projection = route_settlement_projection([row])
        self.assertEqual(len(projection["route_settlements"]), 64)
        self.assertTrue(projection["route_settlements_truncated"])

    def test_closed_map_refuses_settlement_even_with_reopen_authority(self):
        from pod.obligations import accept_write
        prior = self.state()["checkpoint"]
        prior["closure"] = {"revision": prior["revision"]}
        for extra in ({}, {"reopen": True, "revision_authority": {"provenance": "user_direct", "instruction": "reopen"}}):
            with self.subTest(reopen=bool(extra)), self.assertRaises(PodError) as caught:
                accept_write(prior, {"route_settlements": [], **extra}, {})
            self.assertEqual(caught.exception.code, "objective_closed")
            self.assertEqual(caught.exception.detail["referent"], {"record": "route settlements"})
            self.assertIn("Project lead:", caught.exception.detail["next_action"])
            self.assertIn("accepts no settlement write", caught.exception.detail["next_action"])
            for unavailable in ("owner", "user", "reopen"):
                self.assertNotIn(unavailable, caught.exception.detail["next_action"].lower())

    def test_admission_settlement_refusal_names_the_record(self):
        from pod.obligations import _route_settlements
        with self.assertRaises(PodError) as caught:
            _route_settlements([], {}, {"operation": "admit"})
        self.assertEqual(caught.exception.detail["detail"], "checkpoint_only")
        self.assertEqual(caught.exception.detail["referent"], {"record": "route settlements"})

    def test_internal_contract_exposes_bounded_settlement_wire(self):
        from pod.contracts import describe
        self.assertEqual(describe("map")["fields"]["route_settlements"]["max_items"], 16)
        self.assertEqual(describe("route settlement source")["fields"]["reference"]["max_length"], 512)
        self.assertEqual(describe("route settlement")["fields"]["reason"]["max_length"], 1024)


class RenewalGuidanceTests(SettlementCase):
    def test_route_held_implementation_names_disposition_before_route_settlement(self):
        admission, _ = self.original()
        renewal = [{"id": "S", "as": "S2", **deepcopy(WAIT)}]
        error = self.refused_write("renewal_result_pending", renew=renewal)
        action = error.detail["next_action"]
        required = (f"Project lead: record an ordinary disposition for implementation admission "
                    f"{admission['admission_id']} before retrying renewal.")
        self.assertTrue(action.startswith(required), action)
        self.assertIn("route_settlements basis lead_decision", action)
        self.assertNotIn("owner", action.lower())
        self.c.write(dispositions=[self.dispose(admission)], renew=renewal)
        self.assertEqual(next(row for row in self.c.stored() if row["id"] == "S")["state"], "renewed")
        self.assertTrue(route_hold(self.row(admission)))

    def test_matching_report_still_needs_implementation_disposition_before_renewal(self):
        admission, frozen = self.original()
        self.c.report(admission, frozen, map=self.parked())
        self.assertFalse(route_hold(self.row(admission)))
        renewal = [{"id": "S", "as": "S2", **deepcopy(WAIT)}]
        error = self.refused_write("renewal_result_pending", renew=renewal)
        self.assertIn("record an ordinary disposition", error.detail["next_action"])
        self.c.write(dispositions=[self.dispose(admission)], renew=renewal)
        self.assertEqual(next(row for row in self.c.stored() if row["id"] == "S")["state"], "renewed")


class CapacityGuidanceTests(SettlementCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        case = cls.case
        case.write([*case.stored(), {"id": "B", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
            "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
            "candidate": case.candidate, "existing_evidence": "tests", "insufficiency": "no review", **deepcopy(WAIT)}])
        cls.baseline = cls.files(); cls.native = deepcopy(case.port.__dict__)

    def test_capacity_referent_and_next_action_name_the_same_held_admission(self):
        frozen = self.c.packet(["B"], role="review")
        earlier = self.c.start("earlier", frozen)["admission"]; self.c.settle(earlier)
        held = self.c.start("held", frozen)["admission"]; self.c.settle(held)
        self.flag(held, frozen)
        self.assertFalse(route_hold(self.row(earlier)))
        self.assertIsNone(self.row(earlier)["report"])
        before = self.files()
        with self.assertRaises(PodError) as caught:
            self.c.start("third", frozen)
        error = caught.exception
        self.assertEqual(error.detail["detail"], "finding_limit")
        self.assertEqual(error.detail["referent"]["admission"], held["admission_id"])
        self.assertIn(held["admission_id"], error.detail["next_action"])
        self.assertNotIn(earlier["admission_id"], error.detail["next_action"])
        self.assertIn("Project lead:", error.detail["next_action"])
        self.assertNotIn("owner", error.detail["next_action"].lower())
        self.assertEqual(self.files(), before)
        self.c.report(held, frozen, map=self.parked(["B"]))
        self.assertFalse(route_hold(self.row(held)))
        self.c.start("third", frozen)


class RouteWordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pod.governor import NEXT_ACTIONS
        from pod.ledger import consume_report
        from tests.test_worker_tabs import TabCase
        cls.wordings = []
        case = TabCase(); case.setUp(); cls.addClassCleanup(case.doCleanups)
        case.intake(case.sub("S"))
        frozen = case.packet(["S"])
        start = case.port.start_worker
        def receipt_mismatch(*args, **kwargs):
            value = start(*args, **kwargs)
            value["launch"]["effective"] = {**value["launch"]["effective"], "model": "gpt-6-luna"}
            return value
        with patch.object(OrcaPort, "start_worker", side_effect=receipt_mismatch):
            started = case.start("wording", frozen)
        admission = started["admission"]; key = admission["admission_id"]
        ack = acknowledge("admission", {"project": str(case.project), "objective": "objective"}, started)
        cls.wordings.append(("bind acknowledgement", ack["next_action"], key, "unreported implementation"))
        case.settle(admission); case.tab_case = "external"
        with patch.object(case.port, "effective", {"agent": "codex", "model": "gpt-6-luna", "effort": "medium"}):
            try:
                case.report(admission, frozen)
            except PodError as exc:
                cls.wordings.append(("report refusal", exc.detail["next_action"], key, "unreported implementation"))
        try:
            consume_report(case.project, "objective", owner="owner", admission_id=key, observation=None, route_mismatch=True)
        except PodError as exc:
            cls.wordings.append(("consume refusal", exc.detail["next_action"], key, "unreported implementation"))
        cls.wordings.append(("Governor vocabulary", NEXT_ACTIONS["route_mismatch"], "<holding-admission>", "implementation"))
        prepared = case.op("governor-prepare", unit="default",
                           branch={"remote": "origin", "branch": "delivery", "base": "target"})["candidate"]
        case.authorization = {"schema": "pod-authorization/v1", "candidate": prepared["commit"], "tree": prepared["tree"],
            "scope": ["publish"], "authorized_by": "owner", "utc": "2026-01-01T00:00:00Z", "reference": "fixture-authorization"}
        governor = case.op("governor", action=case.action())
        cls.wordings.append(("Governor action", governor["next_action"], key, "unreported implementation"))
        for reason in governor["reasons"]:
            if reason["code"] == "route_mismatch":
                cls.wordings.append(("Governor reason", reason["detail"], key, "unreported implementation"))
        view = case.status()
        cls.wordings.extend((name, text, key, "unreported implementation") for name, text in (
            ("status safe action", view["next_safe_action"]), ("status action", view["next_action"]),
            ("worker tab", view["worker_tabs"][0]["wording"]),
            ("map finalization", case.op("map")["finalization"]["next_action"])))
        try:
            case.write(renew=[{"id": "S", "as": "S2", **deepcopy(WAIT)}])
        except PodError as exc:
            cls.wordings.append(("implementation renewal refusal", exc.detail["next_action"], key, "unreported implementation"))
        case.write(case.finished_rows(), dispositions=[{"admission": key, "discarded": True, "reason": "superseded"}], close=True)
        ended = case.status()
        cls.wordings.extend((name, text, key, "ended") for name, text in (
            ("ended worker tab", ended["worker_tabs"][0]["wording"]),
            ("ended status safe action", ended["next_safe_action"]), ("ended status action", ended["next_action"])))
        lead = {"admission": key, "basis": "lead_decision", "decision": "discard",
                "source": {"reference": "repository-task/lead-decision"}, "reason": "the lead discards this result"}
        for name, extra in (("closed settlement refusal", {}), ("closed settlement refusal with reopen", {
                "reopen": True, "revision_authority": {"provenance": "user_direct", "instruction": "reopen"}})):
            try:
                case.write(seq=read(case.project, "objective")["checkpoint"]["seq"] + 1,
                           route_settlements=[lead], **extra)
            except PodError as exc:
                cls.wordings.append((name, exc.detail["next_action"], key, "ended"))
        review = TabCase(); review.setUp(); cls.addClassCleanup(review.doCleanups)
        review.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
            "check": "review", "scope": {"paths": ["src"]}, "question": "is src right?", "candidate": review.candidate,
            "existing_evidence": "tests", "insufficiency": "no review", **deepcopy(WAIT)})
        body = review.packet(["A"], role="review")
        earlier = review.start("earlier", body)["admission"]; review.settle(earlier)
        review.report(earlier, body, outcome="failed", map={"update": {"A": deepcopy(WAIT)},
            "seq": read(review.project, "objective")["checkpoint"]["seq"] + 1},
            triage=[{"finding": "earlier", "severity": "minor", "triage": "advisory", "summary": "retained"}])
        admission = review.start("review", body)["admission"]; review.settle(admission)
        with patch.object(review.port, "effective", {"agent": "codex", "model": "gpt-6-luna", "effort": "medium"}):
            try:
                review.report(admission, body)
            except PodError:
                pass
        for name, call in (("renewal refusal", lambda: review.write(renew=[{"id": "A", "as": "A2", **deepcopy(WAIT)}])),):
            try:
                call()
            except PodError as exc:
                cls.wordings.append((name, exc.detail["next_action"], admission["admission_id"], "review"))
        review.tab_case = "external"
        review.write(seq=read(review.project, "objective")["checkpoint"]["seq"] + 1,
            update={"A": deepcopy(WAIT)}, route_settlements=[{"admission": admission["admission_id"],
                "basis": "lead_decision", "decision": "discard", "source": {"reference": "repository-task/lead-decision"},
                "reason": "No required findings are known from the refused attempt", "corrections": []}])
        tab = next(row for row in review.status()["worker_tabs"] if row["admission"] == admission["admission_id"])
        cls.wordings.append(("settled discarded worker tab", tab["wording"], admission["admission_id"], "discarded"))

    def test_every_route_mismatch_surface_names_lead_exact_step_and_no_owner(self):
        self.assertEqual({name for name, *_ in self.wordings}, {"bind acknowledgement", "report refusal", "consume refusal",
            "Governor vocabulary", "Governor action", "Governor reason", "status safe action", "status action", "worker tab",
        "map finalization", "ended worker tab", "ended status safe action", "ended status action",
            "renewal refusal", "implementation renewal refusal", "settled discarded worker tab", "closed settlement refusal",
            "closed settlement refusal with reopen"})
        for name, text, admission, role in self.wordings:
            with self.subTest(surface=name):
                self.assertIn("Project lead:", text)
                self.assertIn(admission, text)
                self.assertNotIn("owner", text.lower())
                self.assertNotIn("user", text.lower())
                self.assertNotIn("reopen", text.lower())
                self.assertNotIn("; The coordinator relays", text)
                if role == "discarded":
                    self.assertIn("terminally discarded", text)
                    self.assertIn("no standing close command", text)
                    self.assertNotIn("route_settlements", text)
                    self.assertNotIn("internal checkpoint", text)
                    self.assertIn("its corrections remain in this objective's map", text)
                    self.assertNotIn("successor or repository task", text)
                elif role == "ended":
                    self.assertIn("successor or repository task", text)
                    self.assertIn("accepts no settlement write", text)
                    self.assertNotIn("internal checkpoint", text)
                    self.assertNotIn("internal report", text)
                    self.assertNotIn("route_settlements", text)
                else:
                    for step in ("internal report", "route_settlements basis lead_decision", "internal checkpoint"):
                        self.assertIn(step, text)
                    self.assertIn("does not authenticate the lead", text)
                    if role == "review":
                        self.assertIn("corrections", text)
                        self.assertIn("decision discard", text)
                        self.assertNotIn("basis redone", text)
                        self.assertNotIn("decision accept", text)
                    else:
                        self.assertIn("route_settlements basis redone", text)
                        if role == "unreported implementation":
                            self.assertNotIn("decision accept", text)
                            self.assertNotIn("integrated_into", text)
                        else:
                            self.assertIn("decision accept with ordinary disposition integrated_into", text)
                        self.assertIn("decision discard with ordinary disposition discarded", text)
