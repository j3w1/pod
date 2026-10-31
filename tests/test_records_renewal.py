"""R150-R152: immutable row history and mechanical, fresh-proof renewal."""
from copy import deepcopy
import tempfile
import unittest

from pod.errors import PodError
from pod.obligations import (accept_write, admission_refusal, definition_id, report_projection,
                             _persisted_rows, restatable_rows, row_bounds, status_projection)
from pod.lineage import Lineage
from pod.history import compact, hydrate, is_reference, _reference, FORMAT
from pod.ledger import _empty
from tests.test_obligations import ctx, criterion, ob, intake, proof, by_id, rows_of, packet
from tests.test_history_regressions import previous_history_bundle, previous_history_call

BASE = "e443cf4f0bea4bd7bd14f36843cedbcc50f63638"


class RenewalTests(unittest.TestCase):
    def first(self):
        return accept_write(None, intake(ob("S")), ctx())

    def test_objective_and_coordinator_copy_history_and_waits(self):
        first = self.first()
        renewed = accept_write(first, {"renew": [{"id": "O1", "as": "O2"}]}, ctx())
        old, new = by_id(renewed, "O1"), by_id(renewed, "O2")
        self.assertEqual(old["state"], "renewed")
        self.assertEqual(new["renews"], "O1")
        self.assertEqual(new["receipts"], [])
        self.assertEqual(by_id(renewed, "S")["parent"], "O1")
        self.assertEqual(by_id(renewed, "S")["wait"]["referent"], "O2")
        self.assertEqual(renewed["revision"], first["revision"])
        again = accept_write(renewed, {"renew": [{"id": "S", "as": "S2"}]}, ctx())
        self.assertEqual(by_id(again, "S2")["parent"], "O1")
        self.assertEqual(Lineage(again["obligations"]).effective("S"), "S2")
        self.assertEqual(by_id(again, "O1"), old)
        self.assertEqual(report_projection(again, ctx())["renewed"][0]["obligation"], "O1")
        self.assertIn("renewed by O2", "\n".join(status_projection(again, ctx())["lines"]))
        _persisted_rows(again, ctx())

    def test_user_copy_keeps_adoption_and_acceptances_and_allows_later_revision(self):
        first = self.first()
        authority = {"provenance": "user_direct", "instruction": "adopt the proposed check"}
        first = accept_write(first, {"proposals": [{"id": "P", "source": "coordinator_caution", "origin_ref": "fixture", "summary": "check", "status": "open"}],
            "obligations": [*rows_of(first), ob("U", kind="steer", provenance="user_direct", source={"instruction": "check"}, adopts="P")],
            "revision_authority": authority}, ctx())
        renewed = accept_write(first, {"renew": [{"id": "U", "as": "U2"}]}, ctx())
        self.assertEqual(by_id(renewed, "U2")["source"], by_id(first, "U")["source"])
        self.assertEqual(by_id(renewed, "U2")["adopts"], "P")
        accepted = accept_write(first, {"acceptances": [{"obligation": "U", "source": "outside", "verdict": "accepted", "candidate": "c1", "evidence": "observed"}]}, ctx())
        with_acceptance = accept_write(accepted, {"renew": [{"id": "U", "as": "U2"}]}, ctx())
        self.assertEqual(by_id(with_acceptance, "U")["acceptances"], by_id(accepted, "U")["acceptances"])
        self.assertNotIn("acceptances", by_id(with_acceptance, "U2"))
        updated = accept_write(renewed, {"seq": renewed["seq"] + 1, "update": {"U2": {"check": "revised check"}}, "revision_authority": authority}, ctx())
        _persisted_rows(updated, ctx())
        self.assertEqual(by_id(updated, "U"), by_id(renewed, "U"))

    def test_satisfied_row_requires_nonterminal_state_and_copies_no_proof(self):
        first = accept_write(None, {"governance": {"base_ref": None}, "obligations": [criterion("satisfied", evidence=[proof("O1")]), ob("S", "active")]}, ctx())
        with self.assertRaises(PodError) as caught:
            accept_write(first, {"renew": [{"id": "O1", "as": "O2"}]}, ctx())
        self.assertEqual(caught.exception.detail["detail"], "renewal_terminal_state")
        renewed = accept_write(first, {"renew": [{"id": "O1", "as": "O2", "state": "waiting", "wait": {"class": "sequenced", "referent": "S"}}]}, ctx())
        old = by_id(renewed, "O1")
        self.assertEqual(old["receipts"], by_id(first, "O1")["receipts"])
        self.assertEqual(old.get("evidence"), by_id(first, "O1")["evidence"])
        self.assertEqual(by_id(renewed, "O2")["receipts"], [])
        self.assertNotIn("evidence", by_id(renewed, "O2"))
        with self.assertRaises(PodError):
            admission_refusal(renewed, packet(["O1"]), ctx(), admission_id="new")
        self.assertEqual(admission_refusal(renewed, packet(["O2"]), ctx(), admission_id="new")["serves"], ["O2"])

    def test_closure_retains_renewed_rows_and_requires_fresh_tip_proof(self):
        from pod.obligations import validate_closed_snapshot
        first = accept_write(self.first(), {"renew": [{"id": "O1", "as": "O2"}]}, ctx())
        with self.assertRaises(PodError):
            accept_write(first, {"close": True}, ctx())
        rows = restatable_rows(first)
        for row in rows:
            row.pop("executor", None); row.pop("wait", None)
            row.update(state="satisfied", evidence=[proof(row["id"], definition=row)])
        closed = accept_write(first, {"obligations": rows, "close": True}, ctx())
        self.assertEqual(closed["closure"]["report"]["renewed"][0]["obligation"], "O1")
        self.assertEqual(by_id(closed, "O1"), by_id(first, "O1"))
        self.assertEqual(validate_closed_snapshot(closed, ctx()), ())

    def test_definition_override_names_every_field_and_bad_links_refuse(self):
        first = self.first()
        with self.assertRaises(PodError) as caught:
            accept_write(first, {"renew": [{"id": "O1", "as": "O2", "check": "changed", "boundary": {}}]}, ctx())
        self.assertEqual(caught.exception.detail["detail"], "renewal_changed_definition")
        self.assertEqual(caught.exception.detail["referent"]["fields"], ["boundary", "check"])
        from pod.lineage import DEFINITION
        with self.assertRaises(PodError) as all_fields:
            accept_write(first, {"renew": [{"id": "O1", "as": "O2", **{field: "sentinel" for field in DEFINITION}}]}, ctx())
        self.assertEqual(all_fields.exception.detail["referent"]["fields"], sorted(DEFINITION))
        self.assertNotIn("sentinel", str(all_fields.exception))
        good = accept_write(first, {"renew": [{"id": "O1", "as": "O2"}]}, ctx())
        for field, value in (("renews", "missing"), ("introduced_seq", 1)):
            bad = deepcopy(good)
            by_id(bad, "O2")[field] = value
            with self.assertRaises(PodError):
                _persisted_rows(bad, ctx())
        bad = deepcopy(good)
        by_id(bad, "O1")["check"] = "tampered"
        by_id(bad, "O1")["definition"] = definition_id(by_id(bad, "O1"))
        with self.assertRaises(PodError):
            _persisted_rows(bad, ctx())

    def test_renewal_refusals_and_pending_results(self):
        first = self.first()
        requests = ([{"id": "O1", "as": "S"}], [{"id": "O1", "as": "O2"}, {"id": "O2", "as": "O3"}],
                    [{"id": "missing", "as": "O2"}], [{"id": "O1", "as": "O2", "state": "withdrawn"}])
        for renew in requests:
            with self.subTest(renew=renew), self.assertRaises(PodError):
                accept_write(first, {"renew": renew}, ctx())
        for role, outstanding, report, disposition in (("review", [], None, None), ("implement", [], {}, None), ("review", ["a"], {}, None)):
            admissions = {"a": {"serves": ["O1"], "role": role, "state": "bound", "report": report, "disposition": disposition}}
            with self.subTest(role=role, outstanding=outstanding), self.assertRaises(PodError) as caught:
                accept_write(first, {"renew": [{"id": "O1", "as": "O2"}]}, ctx(admissions=admissions, outstanding=outstanding))
            self.assertEqual(caught.exception.detail["detail"], "renewal_result_pending")

    def test_frozen_rows_omitted_or_exact_and_fresh_children_name_tip(self):
        first = accept_write(self.first(), {"renew": [{"id": "O1", "as": "O2"}]}, ctx())
        carried = accept_write(first, {"obligations": restatable_rows(first)}, ctx())
        self.assertEqual(by_id(carried, "O1"), by_id(first, "O1"))
        for patch in ({"state": "active", "executor": "coordinator"}, {"check": "new"}):
            with self.subTest(patch=patch), self.assertRaises(PodError):
                accept_write(first, {"seq": 3, "update": {"O1": patch}}, ctx())
        with self.assertRaises(PodError) as caught:
            accept_write(first, {"seq": 3, "update": {"N": ob("N", parent="O1", wait={"class": "sequenced", "referent": "O2"})}}, ctx())
        self.assertEqual(caught.exception.detail["detail"], "parent_renewed")

    def test_blocked_external_defaults_and_report_renewal_refuses(self):
        block = {"party": "provider", "need": "fixture token", "unblocks_when": "available"}
        first = accept_write(None, intake(ob("S", "blocked_external", external=block)), ctx())
        renewed = accept_write(first, {"renew": [{"id": "S", "as": "S2"}]}, ctx())
        self.assertEqual(by_id(renewed, "S2")["external"], block)
        with self.assertRaises(PodError):
            accept_write(first, {"renew": [{"id": "S", "as": "S2"}]}, ctx(operation="report"))

    def test_renewal_accepts_exact_or_restatable_rows_and_names_state_conflicts(self):
        first = self.first()
        for rows in (first["obligations"], restatable_rows(first)):
            with self.subTest(form="stored" if rows is first["obligations"] else "restatable"):
                renewed = accept_write(first, {"obligations": rows, "renew": [{"id": "O1", "as": "O2"}]}, ctx())
                self.assertEqual(by_id(renewed, "O2")["renews"], "O1")
        for state_fields in ({"state": "waiting", "wait": {"class": "sequenced", "referent": "S"}},
                             {"state": "blocked_external", "external": {"party": "provider", "need": "input", "unblocks_when": "supplied"}}):
            with self.subTest(state=state_fields["state"]), self.assertRaises(PodError) as caught:
                accept_write(first, {"seq": 2, "update": {"O1": state_fields}, "renew": [{"id": "O1", "as": "O2"}]}, ctx())
            self.assertEqual(caught.exception.detail["detail"], "renewal_changed_row")
            self.assertIn("renew entry", caught.exception.detail["next_action"])

    def test_pending_proof_distinguishes_satisfied_and_reopened_sibling_states(self):
        for state in ("satisfied", "active", "waiting", "blocked_external"):
            with self.subTest(state=state):
                first = accept_write(None, intake(ob("S", "satisfied", evidence=[proof("S")])), ctx())
                if state != "satisfied":
                    fields = ({"executor": "coordinator"} if state == "active" else
                              {"wait": {"class": "sequenced", "referent": "O1"}} if state == "waiting" else
                              {"external": {"party": "provider", "need": "input", "unblocks_when": "supplied"}})
                    if state == "active":
                        # O1 already holds the coordinator slot.
                        fields = {"executor": "worker"}
                        context = ctx(admissions={"worker": {"serves": ["S"], "role": "implement"}}, outstanding=["worker"])
                    else:
                        context = ctx()
                    first = accept_write(first, {"seq": 2, "update": {"S": {"state": state, **fields}}}, context)
                renewed = accept_write(first, {"renew": [{"id": "S", "as": "S2", "state": "waiting",
                    "wait": {"class": "sequenced", "referent": "O1"}}]}, ctx())
                self.assertEqual(Lineage(renewed["obligations"]).pending_proof("S2") is not None, state == "satisfied")


class RowAndHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.previous = previous_history_bundle(directory.name, BASE)
        first = accept_write(None, intake(ob("S")), ctx())
        cls.with16 = first
        for index in range(16):
            cls.with16 = accept_write(cls.with16, {"seq": cls.with16["seq"] + 1, "update": {"S": {"evidence": [proof("S", reference=f"proof-{index}")]}}}, ctx())

    def test_genuine_previous_reader_refuses_seventeen_while_current_accepts(self):
        value = {"seq": self.with16["seq"] + 1, "update": {"S": {"evidence": [proof("S", reference="proof-17")]}}}
        original = _empty(); original["checkpoint"] = {"schema": "pod-checkpoint/v3", **self.with16}
        previous = previous_history_call(self.previous, original, operation="map_write", map_value=value,
                                         context={key: item for key, item in ctx().items() if not callable(item)})
        self.assertFalse(previous["accepted"])
        self.assertIsNotNone(previous["detail"], previous)
        self.assertEqual(previous["detail"]["detail"], "receipt_limit")
        self.assertTrue(previous["unchanged"])
        new = accept_write(self.with16, value, ctx())
        self.assertEqual(len(by_id(new, "S")["receipts"]), 17)
        state = _empty(); state["checkpoint"] = {"schema": "pod-checkpoint/v3", **new}
        result = previous_history_call(self.previous, state, context={key: value for key, value in ctx().items() if not callable(value)})
        self.assertEqual(result["version"], "0.21.11")
        self.assertFalse(result["accepted"])
        self.assertTrue(result["unchanged"])

    def test_live_and_total_bounds_are_separate(self):
        row_bounds([{"state": "waiting"}] * 128 + [{"state": "withdrawn"}] * 384)
        for rows, name in (([{"state": "waiting"}] * 129, "live_rows"), ([{"state": "withdrawn"}] * 8193, "hydrated_total")):
            with self.subTest(name=name), self.assertRaises(PodError) as caught:
                row_bounds(rows)
            self.assertEqual(caught.exception.detail["referent"]["bound"], name)

    def test_request_update_and_live_overflows_report_distinct_counts(self):
        full = accept_write(None, intake(*[ob(f"S{i}") for i in range(127)]), ctx())
        with self.assertRaises(PodError) as caught:
            accept_write(full, {"seq": 2, "update": {"EXTRA": ob("EXTRA")}}, ctx())
        detail = caught.exception.detail["referent"]
        self.assertEqual((detail["bound"], detail["request_entries"], detail["hydrated_total"], detail["live"]), ("live_rows", 1, 129, 129))
        with self.assertRaises(PodError) as caught:
            accept_write(full, {"obligations": [{"state": "withdrawn"}] * 513}, ctx())
        self.assertEqual(caught.exception.code, "obligation_invalid")
        self.assertIn("512", str(caught.exception))
        with self.assertRaises(PodError):
            accept_write(full, {"seq": 2, "update": {f"S{i}": {} for i in range(129)}}, ctx())

    def test_receipt_selection_and_criterion_proof_bounds_stay_independent(self):
        from pod.contracts import bound
        from pod.obligations import _structure
        from pod.pod_ticks import empty, validate
        self.assertEqual(bound("obligation", "receipts", "max_items"), 1024)
        self.assertEqual(bound("obligation", "evidence", "max_items"), 16)
        with self.assertRaises(PodError):
            _structure(criterion(evidence=[proof("O1")] * 17))
        ledger = empty()
        scope = {key: "a" * 64 for key in ("definition", "source_digest", "amendments_digest", "block_sha256")}
        proofs = {f"{index:064x}": scope for index in range(64)}
        ledger["criterion_scopes"] = {"O1": {"definition": "a" * 64, "proofs": proofs}}
        validate(ledger)
        proofs[f"{64:064x}"] = scope
        with self.assertRaises(PodError):
            validate(ledger)

    def test_512_hydrated_rows_keep_384_omitted_terminal_rows(self):
        first = accept_write(None, intake(*[ob(f"S{i}") for i in range(127)],
            **{}), ctx())
        rows = rows_of(first)
        rows.extend(ob(f"W{i}", "withdrawn", withdrawal={"by": "coordinator", "reason": "synthetic terminal branch"}) for i in range(384))
        full = accept_write(first, {"obligations": rows}, ctx())
        self.assertEqual(len(full["obligations"]), 512)
        kept = accept_write(full, {"obligations": restatable_rows(full)}, ctx())
        self.assertEqual(kept["obligations"], full["obligations"])
        # A 513th terminal row now fits the retained-history safety bound;
        # it does not spend another live slot or discard omitted history.
        extra = accept_write(full, {"seq": full["seq"] + 1, "update": {
            "W384": ob("W384", "withdrawn", withdrawal={"by": "coordinator", "reason": "terminal"})}}, ctx())
        self.assertEqual(len(extra["obligations"]), 513)
        self.assertEqual(extra["obligations"][:-1], full["obligations"])
        with self.assertRaises(PodError) as caught:
            accept_write(extra, {"seq": extra["seq"] + 1, "update": {"S128": ob("S128")}}, ctx())
        self.assertEqual(caught.exception.detail["referent"]["bound"], "live_rows")

    def test_previous_reader_refuses_renewed_state_and_larger_counts_without_rewrite(self):
        first = accept_write(None, intake(ob("S")), ctx())
        renewed = accept_write(first, {"renew": [{"id": "S", "as": "S2"}]}, ctx())
        large = accept_write(None, intake(*[ob(f"S{i}") for i in range(100)]), ctx())
        for checkpoint in (renewed, large):
            state = _empty(); state["checkpoint"] = {"schema": "pod-checkpoint/v3", **checkpoint}
            result = previous_history_call(self.previous, state, context={key: value for key, value in ctx().items() if not callable(value)})
            self.assertFalse(result["accepted"])
            self.assertTrue(result["unchanged"])

    def test_terminal_history_moves_but_live_unresolved_findings_stay_inline(self):
        first = accept_write(None, intake(ob("S")), ctx())
        renewed = accept_write(first, {"renew": [{"id": "O1", "as": "O2"}]}, ctx())
        logical = _empty(); logical["checkpoint"] = {"schema": "pod-checkpoint/v3", **renewed}
        # Large immutable content gives relocation a useful byte saving.
        by_id(renewed, "O1")["receipts"] = [{"evidence": {"reference": "synthetic", "result": "x" * 2000}}]
        renewed["obligations"].extend([
            ob("A", kind="assurance", scope={"paths": []}, candidate="c1", question="q", existing_evidence="none", insufficiency="review",
               findings=[{"recorded_seq": 2, "triage": "required_correction", "summary": "x" * 512,
                          "attempt": "synthetic", "finding": "F", "severity": "major", "correction": "K"}]),
            ob("K", kind="correction", parent="A")])
        record, chunks = compact(logical, force=True)
        self.assertTrue(is_reference(record["checkpoint"]["obligations"][0]))
        self.assertFalse(is_reference(next(row for row in record["checkpoint"]["obligations"] if row.get("id") == "A")["findings"][0]))
        self.assertEqual(hydrate(record, chunks.__getitem__), logical)
        old = previous_history_call(self.previous, record, chunks=chunks, context={key: value for key, value in ctx().items() if not callable(value)})
        self.assertFalse(old["accepted"])
        self.assertTrue(old["unchanged"])

    def test_nested_references_in_whole_row_chunks_refuse(self):
        chunks = {}
        nested = _reference({"evidence": {"result": "x" * 2000}}, chunks)
        outer = _reference({"id": "old", "state": "renewed", "receipts": [nested]}, chunks)
        with self.assertRaises(PodError) as caught:
            hydrate({"history": FORMAT, "checkpoint": {"obligations": [outer]}}, chunks.__getitem__)
        self.assertEqual(caught.exception.code, "history_integrity")

    def test_every_context_read_validates_the_frozen_renewal_digest(self):
        from pathlib import Path
        from pod.ledger import _read
        from pod.util import encoded
        first = accept_write(None, intake(ob("S")), ctx())
        renewed = accept_write(first, {"renew": [{"id": "S", "as": "S2"}]}, ctx())
        state = _empty(); state["checkpoint"] = {"schema": "pod-checkpoint/v3", **renewed}
        by_id(renewed, "S")["check"] = "corrupt retained check"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_bytes(encoded(state, compact=True))
            before = path.read_bytes()
            with self.assertRaises(PodError) as caught:
                _read(path)
            self.assertEqual(caught.exception.detail["detail"], "renewal_invalid")
            self.assertEqual(path.read_bytes(), before)
