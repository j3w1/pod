"""Obligation-kernel refusals and records, exercised through the pure boundary functions.

Every function here is the one the checkpoint, admission, report and acceptance boundaries
call; the ledger integration is covered in test_kernel_boundaries.
"""

from copy import deepcopy
import unittest

from pod.errors import PodError
from pod.obligations import (accept_write, admission_refusal, admit, disposition, evaluate_trace,
                             label_qualification, overlap, report_projection, serialization_flags,
                             status_projection, triage)
from tests.common import VERIFICATION, proof

EMPTY = {"paths": [], "surfaces": []}


def ctx(**extra) -> dict:
    base = {"admissions": {}, "outstanding": [], "ceiling": 2, "delegation": "available",
            "constraints": [], "criteria": ["PoD#1"], "candidate": "c1",
            "governance": {"status": "no_repository"}, "policy_revision": "p",
            "verification": dict(VERIFICATION), "source_current": lambda entry: True}
    base.update(extra)
    return base


def ob(id, state="waiting", *, kind="subgoal", provenance="coordinator", **fields):
    row = {"id": id, "kind": kind, "provenance": provenance, "state": state}
    if kind in ("subgoal", "correction") or (kind == "assurance" and provenance == "coordinator"):
        row.setdefault("parent", "O1")
    if "resolves" not in fields:
        row["check"] = f"{id} check"
    if state == "waiting" and "wait" not in fields:
        row["wait"] = {"class": "sequenced", "referent": "O1"}
    if state == "active" and "executor" not in fields:
        row["executor"] = "coordinator"
    row.update(fields)
    return row


def criterion(state="active", **fields):
    return ob("O1", state, kind="criterion", provenance="objective", source={"ref": "PoD#1"}, **fields)


def intake(*rows, governance=None, **extra):
    return {"governance": governance or {"base_ref": None}, "obligations": [criterion(), *rows], **extra}


def write(prior, rows, **extra):
    return {"obligations": rows, **extra}


def refused(test, code, detail, call, *args, **kwargs):
    with test.assertRaises(PodError) as caught:
        call(*args, **kwargs)
    test.assertEqual(caught.exception.code, code, str(caught.exception))
    test.assertEqual(caught.exception.detail["detail"], detail, str(caught.exception))
    test.assertIn("Next:", str(caught.exception))
    return caught.exception


def rows_of(state):
    return deepcopy(state["obligations"])


def by_id(state, key):
    return next(row for row in state["obligations"] if row["id"] == key)


def settled_row(serves, role="implement", boundary=None, **extra):
    row = {"serves": serves, "role": role, "boundary": boundary or dict(EMPTY), "state": "bound",
           "disposition": None, "changed_paths": None, "boundary_exceeded": [], "candidate": "c1",
           "report": None, "result": None,
           "binding": {"policy_revision": "p", "sources": [], **deepcopy(VERIFICATION)}}
    row.update(extra)
    return row


def packet(serves, role="implement", boundary=None, revision=1, **extra):
    return {"serves": serves, "role": role, "boundary": boundary or dict(EMPTY), "map_revision": revision,
            **extra}


class ProvenanceTests(unittest.TestCase):
    def test_trivial_direct_work_has_no_map_and_first_delegation_needs_one(self):
        self.assertEqual(accept_write(None, {}, ctx()), {})
        refused(self, "unbound_assignment", "no_map", admission_refusal, None, packet(["O1"]), ctx(),
                admission_id="a")

    def test_kind_and_provenance_authority_matrix(self):
        first = accept_write(None, intake(), ctx())
        self.assertEqual((first["seq"], first["revision"], first["coordinator_slot"]), (1, 1, "O1"))
        cases = [
            (ob("C", "unassigned", kind="criterion", provenance="coordinator"), "provenance_unauthorized"),
            (ob("D", "unassigned", kind="delivery", provenance="coordinator"), "provenance_unauthorized"),
            (ob("S", "unassigned", kind="steer", provenance="coordinator"), "provenance_unauthorized"),
            (ob("L", "unassigned", kind="criterion", provenance="objective", source={"ref": "late"}),
             "provenance_unauthorized"),
            (ob("U", "unassigned", kind="criterion", provenance="user_direct",
                source={"instruction": "also do this"}), "provenance_unauthorized"),
            ({**ob("N", "unassigned"), "parent": "missing"}, "no_parent"),
            ({k: v for k, v in ob("P", "unassigned").items() if k != "parent"}, "no_parent"),
            ({k: v for k, v in ob("Q", "unassigned").items() if k != "check"}, "no_check"),
            (ob("R", "unassigned", resolves="which API"), "no_check"),
            (ob("X", "unassigned", kind="subgoal", provenance="coordinator", source={"ref": "PoD#9"}),
             "provenance_unauthorized"),
        ]
        for row, detail in cases:
            with self.subTest(row=row["id"]):
                refused(self, "obligation_invalid", detail, accept_write, first,
                        write(first, [*rows_of(first), row]), ctx())
        for kind in ("subgoal", "assurance", "correction"):
            with self.subTest(allowed=kind):
                extra = ({"scope": {"paths": ["a"]}, "question": "q", "candidate": "c1",
                          "existing_evidence": "none", "insufficiency": "no review"}
                         if kind == "assurance" else {})
                parent_rows = rows_of(first)
                if kind == "correction":
                    parent_rows.append(ob("A", "unassigned", kind="assurance", scope={"paths": ["a"]},
                                          question="q", candidate="c1", existing_evidence="none",
                                          insufficiency="no review"))
                    extra = {"parent": "A"}
                accepted = accept_write(first, write(first, [*parent_rows, ob("K", "unassigned", kind=kind, **extra)]),
                                        ctx())
                self.assertEqual(by_id(accepted, "K")["introduced_seq"], 2)
        user = {"provenance": "user_direct", "instruction": "please also add the ranking criterion"}
        revised = accept_write(first, write(first, [*rows_of(first), ob(
            "C", "unassigned", kind="criterion", provenance="user_direct",
            source={"instruction": "add ranking"})], revision_authority=user), ctx())
        self.assertEqual(revised["revision"], 2)
        refused(self, "obligation_invalid", "provenance_unauthorized", accept_write, first,
                write(first, rows_of(first), revision_authority={"provenance": "coordinator",
                                                                 "instruction": "x"}), ctx())

    def test_the_brief_map_covers_every_original_criterion_with_a_check_or_dependency(self):
        from pod.internal import run
        request = {"criteria": ["PoD#1", "PoD#2"],
                   "coverage": [{"criterion": "PoD#1", "check": "unit"},
                                {"criterion": "PoD#2", "dependency": "owner confirms the live trial"}]}
        rows = [criterion(), ob("O2", "blocked_external", kind="delivery", provenance="objective",
                                source={"ref": "PoD#2"}, external={"party": "owner", "need": "a live trial",
                                                                   "unblocks_when": "the owner runs it"})]
        mapped = {"governance": {"base_ref": None}, "obligations": rows}
        brief = run("brief", {**request, "map": mapped})
        self.assertEqual(brief["map"]["criteria_covered"], ["PoD#1", "PoD#2"])
        self.assertEqual((brief["map"]["draft"], brief["map"]["persisted"], brief["map"]["seq"]), (True, False, 1))
        self.assertEqual(brief["map"]["coordinator_slot"], "O1")
        refused(self, "obligation_unaccounted", "map_missing", run, "brief", request)
        refused(self, "obligation_unaccounted", "map_missing", run, "brief", {**request, "map": {}})
        refused(self, "obligation_unaccounted", "criterion_uncovered", run, "brief",
                {**request, "map": {**mapped, "obligations": rows[:1]}})
        refused(self, "obligation_invalid", "provenance_unauthorized", run, "brief",
                {**request, "map": {**mapped, "obligations": [*rows, ob("X", kind="criterion",
                                                                       provenance="coordinator")]}})
        refused(self, "obligation_unaccounted", "property_P1", run, "brief",
                {**request, "map": {**mapped, "obligations": [criterion("unassigned"), rows[1]]}})
        refused(self, "governance_unavailable", "governance_unavailable", run, "brief",
                {**request, "map": {**mapped, "governance": {"base_ref": "main"}}})

    def test_definitions_and_identity_are_stable_without_a_user_revision(self):
        first = accept_write(None, intake(ob("S")), ctx())
        rows = rows_of(first)
        rows[1]["check"] = "a weaker check"
        refused(self, "obligation_invalid", "redefinition_unauthorized", accept_write, first, write(first, rows), ctx())
        refused(self, "obligation_unaccounted", "missing_state", accept_write, first,
                write(first, rows_of(first)[:1]), ctx())
        refused(self, "map_stale", "map_stale", accept_write, first, write(first, rows_of(first), seq=5), ctx())
        refused(self, "obligation_unaccounted", "missing_state", accept_write, first, {"obligations": None}, ctx())
        carried = accept_write(first, {}, ctx())
        self.assertEqual((carried["seq"], [row["id"] for row in carried["obligations"]]), (2, ["O1", "S"]))
        refused(self, "obligation_unaccounted", "unassigned_expired", accept_write,
                accept_write(first, write(first, [*rows_of(first), ob("N", "unassigned")]), ctx()), {}, ctx())
        rows = rows_of(first)
        rows[1]["check"] = "the user's revised check"
        accepted = accept_write(first, write(first, rows, revision_authority={
            "provenance": "user_direct", "instruction": "change the check"}), ctx())
        self.assertEqual(by_id(accepted, "S")["check"], "the user's revised check")

    def test_proposals_change_nothing_until_adopted_by_user_or_governance(self):
        first = accept_write(None, intake(proposals=[
            {"id": "P1", "source": "worker_report", "origin_ref": "adm-1", "summary": "add Windows paths",
             "status": "open"}]), ctx())
        refused(self, "obligation_invalid", "provenance_unauthorized", accept_write, first,
                write(first, [*rows_of(first), ob("W", "unassigned", adopts="P1")],
                      proposals=first["proposals"]), ctx())
        refused(self, "obligation_invalid", "malformed", accept_write, first,
                write(first, rows_of(first), proposals=[]), ctx())
        refused(self, "unbound_assignment", "proposed", admission_refusal, first, packet(["P1"]), ctx(),
                admission_id="a")
        report = report_projection(first, ctx())
        self.assertEqual([row["id"] for row in report["proposals_out_of_scope"]], ["P1"])
        adopted = accept_write(first, write(first, [*rows_of(first), ob(
            "W", "unassigned", kind="steer", provenance="user_direct", source={"instruction": "adopt P1"},
            adopts="P1")], proposals=first["proposals"],
            revision_authority={"provenance": "user_direct", "instruction": "adopt P1"}), ctx())
        self.assertEqual(adopted["proposals"][0]["status"], "adopted")
        self.assertEqual(report_projection(adopted, ctx())["proposals_out_of_scope"], [])


class AccountingTests(unittest.TestCase):
    def test_state_accounting_refusals(self):
        first = accept_write(None, intake(ob("S")), ctx())
        cases = [
            ({**ob("S"), "state": None}, "missing_state"),
            ({k: v for k, v in ob("S").items() if k != "state"}, "missing_state"),
            ({**ob("S"), "state": ["waiting", "active"]}, "multiple_states"),
            ({**ob("S", "active"), "wait": {"class": "sequenced", "referent": "O1"}}, "multiple_states"),
            ({**ob("S", "unassigned")}, "unassigned_expired"),
            ({**ob("S", "satisfied")}, "satisfied_without_evidence"),
            ({**ob("S", "satisfied"), "evidence": [proof("S", status="FAILED")]}, "evidence_invalidated"),
            ({**ob("S", "active", executor="ghost")}, "executor_unknown"),
        ]
        for row, detail in cases:
            with self.subTest(detail=detail):
                refused(self, "obligation_unaccounted", detail, accept_write, first,
                        write(first, [rows_of(first)[0], row]), ctx())

    def test_evidence_is_the_detailed_record_joined_by_obligation_id_and_its_r41_binding(self):
        # Different human check text is fine; the structural join is the obligation id.
        first = accept_write(None, intake(ob("S", "satisfied", check="parser round-trips",
                                             evidence=[proof("S", check="tests.test_parser.round_trip",
                                                             sources=[{"path": "src/p.py", "state": "present",
                                                                       "sha256": "a" * 64}])])), ctx())
        self.assertEqual(by_id(first, "S")["evidence"][0]["command"], "python -m unittest")
        refused(self, "obligation_invalid", "evidence_unbound", accept_write, None,
                intake(ob("S", "satisfied", evidence=[proof("O1")])), ctx())
        summary_only = {"check": "unit", "status": "PASS", "candidate": "c1", "reference": "log"}
        refused(self, "obligation_invalid", "malformed", accept_write, None,
                intake(ob("S", "satisfied", evidence=[summary_only])), ctx())
        changed = [
            ctx(verification={**VERIFICATION, "environment": "another runner"}),
            ctx(verification={**VERIFICATION, "dependencies": ["pyyaml==7.0"]}),
            ctx(policy_revision="changed-governor-policy"),
            ctx(source_current=lambda entry: False),
            ctx(verification=None),
        ]
        for context in changed:
            with self.subTest(context={k: context[k] for k in ("policy_revision", "verification")}):
                refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, first,
                        write(first, rows_of(first)), context)
        widened = ctx(verification={**VERIFICATION, "dependencies": ["pyyaml==6.0.3", "pyte==0.8.2"]})
        self.assertEqual(by_id(accept_write(first, write(first, rows_of(first)), widened), "S")["state"],
                         "satisfied")
        rows = rows_of(first)
        rows[1] = ob("S", check="parser round-trips", evidence=rows[1]["evidence"])
        restated = accept_write(first, write(first, rows), ctx(verification={**VERIFICATION, "environment": "x"}))
        self.assertEqual(by_id(restated, "S")["state"], "waiting")

    def test_reuse_keeps_every_binding_except_the_candidate(self):
        first = accept_write(None, intake(ob("S", "satisfied", boundary={"paths": ["docs"]},
                                             evidence=[proof("S")])), ctx())
        rows = rows_of(first)
        rows[1]["reuse"] = {"from": "c1"}
        moved = {"candidate": "c2", "git_delta": lambda source, target: ["src/x.py"]}
        self.assertEqual(by_id(accept_write(first, write(first, rows), ctx(**moved)), "S")["reuse"]["to"], "c2")
        refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, first, write(first, rows),
                ctx(**moved, verification={**VERIFICATION, "environment": "another runner"}))

    def test_new_unassigned_work_needs_something_active_in_its_checkpoint(self):
        refused(self, "obligation_unaccounted", "property_P1", accept_write, None,
                {"governance": {"base_ref": None},
                 "obligations": [criterion("unassigned")]}, ctx())
        self.assertEqual(accept_write(None, intake(ob("S", "unassigned")), ctx())["seq"], 1)

    def test_satisfied_reverts_when_its_evidence_is_invalidated(self):
        evidence = [proof("S")]
        first = accept_write(None, intake(ob("S", "satisfied", evidence=evidence)), ctx())
        self.assertEqual(by_id(first, "S")["state"], "satisfied")
        refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, first,
                write(first, rows_of(first)), ctx(candidate="c2"))
        rows = rows_of(first)
        rows[1] = ob("S", evidence=evidence)
        self.assertEqual(by_id(accept_write(first, write(first, rows), ctx(candidate="c2")), "S")["state"], "waiting")

    def test_one_coordinator_slot_and_its_boundary(self):
        refused(self, "obligation_unaccounted", "coordinator_slot_exceeded", accept_write, None,
                intake(ob("S", "active")), ctx())
        running = ctx(admissions={"adm-1": settled_row(["S"], boundary={"paths": ["src/api"], "surfaces": []},
                                                       state="reserved")}, outstanding=["adm-1"])
        second = accept_write(None, intake(ob("S", boundary={"paths": ["src/api"]}),
                                           ob("T", boundary={"paths": ["src"]})), ctx())
        admitted = admit(second, packet(["S"], boundary={"paths": ["src/api"]}), ctx(), admission_id="adm-1")
        rows = rows_of(admitted)
        rows[0]["state"] = "waiting"
        rows[0].pop("executor")
        rows[0]["wait"] = {"class": "dependency", "referent": "T"}
        rows[2] = {**ob("T", "active"), "boundary": {"paths": ["src"], "surfaces": []}}
        refused(self, "obligation_unaccounted", "coordinator_boundary_overlap", accept_write, admitted,
                write(admitted, rows), running)
        held = accept_write(None, {"governance": {"base_ref": None}, "obligations": [
            criterion(boundary={"paths": ["src/api"]}), ob("S")]}, ctx())
        conflict = refused(self, "ownership_conflict", "ownership_conflict", admission_refusal, held,
                           packet(["S"], boundary={"paths": ["src"]}), ctx(), admission_id="x")
        self.assertEqual(conflict.detail["referent"]["obligation"], "O1")
        rows = rows_of(held)
        rows[1]["wait"] = {"class": "ownership", "referent": "O1"}
        rows[1]["boundary"] = {"paths": ["src"], "surfaces": []}
        self.assertEqual(by_id(accept_write(held, write(held, rows), ctx()), "S")["wait"]["class"], "ownership")


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.first = accept_write(None, intake(ob("S"), ob("T"), ob("U", kind="subgoal")), ctx())

    def restate(self, key, wait, context=None, state=None, **extra):
        base = state or self.first
        rows = rows_of(base)
        row = next(row for row in rows if row["id"] == key)
        row.update({"state": "waiting", "wait": wait, **extra})
        row.pop("executor", None)
        return accept_write(base, write(base, rows), context or ctx())

    def test_each_wait_class_validates_its_referent(self):
        invalid = [
            ({"class": "another_worker_active", "referent": "adm"}, "unclassified"),
            ({"referent": "O1"}, "unclassified"),
            ({"class": "dependency", "referent": "ghost"}, "unknown_referent"),
            ({"class": "dependency", "referent": "S"}, "unknown_referent"),
            ({"class": "contract_unsettled", "referent": "O1"}, "unknown_referent"),
            ({"class": "sequenced", "referent": "T"}, "unknown_referent"),
            ({"class": "ownership", "referent": "ghost"}, "unknown_referent"),
            ({"class": "capacity", "referent": []}, "capacity_below_ceiling"),
            ({"class": "integration_pending", "referent": "ghost"}, "unknown_referent"),
            ({"class": "input_unavailable", "referent": "inputs/data.csv"}, "resolved_referent"),
            ({"class": "authority", "referent": {"kind": "vibes"}}, "unknown_referent"),
            ({"class": "user_hold", "referent": "no-such-constraint"}, "unknown_referent"),
        ]
        for wait, detail in invalid:
            with self.subTest(detail=detail, wait=wait):
                refused(self, "wait_invalid", detail, self.restate, "S", wait,
                        ctx(source_state=lambda path: "present"))
        valid = [
            {"class": "dependency", "referent": "T"},
            {"class": "contract_unsettled", "referent": "U"},
            {"class": "sequenced", "referent": "O1"},
            {"class": "input_unavailable", "referent": "inputs/data.csv"},
            {"class": "authority", "referent": {"kind": "authorization", "scope": "merge", "candidate": "c1"}},
            {"class": "authority", "referent": {"kind": "native_authority", "need": "coordinator Run binding"}},
            {"class": "user_hold", "referent": "hold-1"},
        ]
        constraints = [{"id": "hold-1", "kind": "max_workers", "provenance": "user_direct", "value": 1}]
        for wait in valid:
            with self.subTest(valid=wait["class"]):
                state = self.restate("S", wait, ctx(source_state=lambda path: "unavailable", constraints=constraints))
                self.assertEqual(by_id(state, "S")["wait"], wait)

    def test_a_resolved_referent_invalidates_the_wait_at_the_next_write(self):
        state = self.restate("S", {"class": "dependency", "referent": "T"})
        rows = rows_of(state)
        by = {row["id"]: row for row in rows}
        by["T"].update({"state": "satisfied", "evidence": [proof("T")]})
        by["T"].pop("wait")
        refused(self, "wait_invalid", "resolved_referent", accept_write, state, write(state, rows), ctx())
        granted = ctx(authorization_granted=lambda scope, candidate: True)
        refused(self, "wait_invalid", "resolved_referent", self.restate, "S",
                {"class": "authority", "referent": {"kind": "authorization", "scope": "merge", "candidate": "c1"}},
                granted)
        hold = [{"id": "hold-1", "kind": "max_workers", "provenance": "user_direct", "value": 1, "active": False}]
        refused(self, "wait_invalid", "resolved_referent", self.restate, "S",
                {"class": "user_hold", "referent": "hold-1"}, ctx(constraints=hold))

    def test_dependency_cycles_and_a_parked_coordinator_are_refused(self):
        state = self.restate("S", {"class": "dependency", "referent": "T"})
        rows = rows_of(state)
        next(row for row in rows if row["id"] == "T")["wait"] = {"class": "dependency", "referent": "S"}
        refused(self, "wait_invalid", "cycle", accept_write, state, write(state, rows), ctx())
        rows = rows_of(self.first)
        rows[0] = criterion("waiting", wait={"class": "authority", "referent": {"kind": "permission",
                                                                                "need": "owner approval"}})
        refused(self, "wait_invalid", "sequenced_without_active", accept_write, self.first,
                write(self.first, rows), ctx())

    def test_capacity_and_ownership_waits_need_their_facts(self):
        full = ctx(outstanding=["a1", "a2"], admissions={
            "a1": settled_row(["T"], state="reserved", boundary={"paths": ["src"], "surfaces": []}),
            "a2": settled_row(["U"], state="reserved")})
        rows = rows_of(self.first)
        for row in rows:
            if row["id"] in ("T", "U"):
                row.update(state="active", executor="a1" if row["id"] == "T" else "a2")
                row.pop("wait")
        base = accept_write(self.first, write(self.first, rows), full)
        state = self.restate("S", {"class": "capacity", "referent": ["a1", "a2"]}, full, base)
        self.assertEqual(by_id(state, "S")["wait"]["class"], "capacity")
        refused(self, "wait_invalid", "unknown_referent", self.restate, "S",
                {"class": "capacity", "referent": ["a1"]}, full, base)
        refused(self, "wait_invalid", "capacity_below_ceiling", self.restate, "S",
                {"class": "capacity", "referent": ["a1", "a2"]}, {**full, "ceiling": 3}, base)
        refused(self, "wait_invalid", "ownership_without_overlap", self.restate, "S",
                {"class": "ownership", "referent": "a1"}, full, base, boundary={"paths": ["docs"]})
        state = self.restate("S", {"class": "ownership", "referent": "a1"}, full, base,
                             boundary={"paths": ["src/x.py"]})
        self.assertEqual(by_id(state, "S")["wait"]["referent"], "a1")

    def test_quiescence_requires_chains_that_end_outside_the_objective(self):
        rows = [criterion("blocked_external", external={"party": "user", "need": "a non-Owner test identity",
                                                         "unblocks_when": "the user supplies the identity"}),
                ob("S", wait={"class": "dependency", "referent": "O1"})]
        state = accept_write(None, {"governance": {"base_ref": None}, "obligations": rows}, ctx())
        self.assertEqual(state["quiescence"]["state"], "quiescent")
        self.assertIn("user: a non-Owner test identity", state["quiescence"]["interim_report"])
        refused(self, "obligation_unaccounted", "external_invalid", accept_write, None,
                {"governance": {"base_ref": None}, "obligations": [
                    criterion("blocked_external", external={"party": "user", "need": "S", "unblocks_when": "x"}),
                    ob("S", wait={"class": "dependency", "referent": "O1"})]}, ctx())
        refused(self, "obligation_unaccounted", "external_invalid", accept_write, None,
                {"governance": {"base_ref": None}, "obligations": [
                    criterion("blocked_external", external={"party": "the worker", "need": "n",
                                                            "unblocks_when": "x"})]}, ctx())


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.state = accept_write(None, intake(
            ob("S", boundary={"paths": ["src/a"]}), ob("T", boundary={"paths": ["src/b"]}),
            ob("A", kind="assurance", scope={"paths": ["src"]}, question="is the API safe?", candidate="c1",
               existing_evidence="unit tests", insufficiency="no independent review"),
            ob("I", resolves="which parser", stop_condition="one parser named")), ctx())

    def test_each_unbound_assignment_detail(self):
        busy = ctx(outstanding=["a1"], admissions={"a1": settled_row(["S"], state="reserved")})
        cases = [
            (packet(["ghost"]), ctx(), "missing"),
            (packet(["S"], revision=9), ctx(), "out_of_revision"),
            (packet(["S"]), busy, "obligation_busy"),
            (packet(["S"], role="review"), ctx(), "role_kind_mismatch"),
            (packet(["A"], role="implement"), ctx(), "role_kind_mismatch"),
            (packet(["A", "S"], role="review"), ctx(), "role_kind_mismatch"),
            (packet(["I"], role="investigate"), ctx(), "missing_resolves"),
            (packet([], role="implement"), ctx(), "missing"),
        ]
        for body, context, detail in cases:
            with self.subTest(detail=detail):
                refused(self, "unbound_assignment", detail, admission_refusal, self.state, body, context,
                        admission_id="new")
        admitted = admit(self.state, packet(["I"], role="investigate", resolves="which parser",
                                            stop_condition="one parser named"), ctx(), admission_id="inv")
        self.assertEqual(by_id(admitted, "I")["executor"], "inv")

    def test_ownership_and_consolidated_corrections(self):
        running = ctx(outstanding=["a1"], admissions={
            "a1": settled_row(["S"], state="reserved", boundary={"paths": ["src/a"], "surfaces": ["api"]})})
        conflict = refused(self, "ownership_conflict", "ownership_conflict", admission_refusal, self.state,
                           packet(["T"], boundary={"paths": ["src"]}), running, admission_id="new")
        self.assertEqual(conflict.detail["referent"]["admission"], "a1")
        refused(self, "ownership_conflict", "ownership_conflict", admission_refusal, self.state,
                packet(["T"], boundary={"paths": ["lib"], "surfaces": ["api"]}), running, admission_id="new")
        admission_refusal(self.state, packet(["T"], boundary={"paths": ["src/b"]}), running, admission_id="new")
        rows = rows_of(self.state)
        rows += [ob("K1", "unassigned", kind="correction", parent="A"),
                 ob("K2", "unassigned", kind="correction", parent="A")]
        corrected = accept_write(self.state, write(self.state, rows), ctx())
        both = admit(corrected, packet(["K1", "K2"]), ctx(), admission_id="fix")
        self.assertEqual({by_id(both, key)["executor"] for key in ("K1", "K2")}, {"fix"})

    def test_admission_sets_served_obligations_active_in_the_same_write(self):
        admitted = admit(self.state, packet(["S"]), ctx(), admission_id="a1")
        self.assertEqual((admitted["seq"], by_id(admitted, "S")["state"], by_id(admitted, "S")["executor"]),
                         (2, "active", "a1"))
        refused(self, "obligation_unaccounted", "outstanding_unaccounted", accept_write, admitted,
                write(admitted, rows_of(self.state)),
                ctx(outstanding=["a1"], admissions={"a1": settled_row(["S"], state="reserved")}))


class ResultTests(unittest.TestCase):
    def setUp(self):
        base = accept_write(None, intake(ob("S", boundary={"paths": ["src"]}),
                                         ob("T", boundary={"paths": ["src/x"]}),
                                         ob("V", boundary={"paths": ["docs"]})), ctx())
        self.admitted = admit(base, packet(["S"], boundary={"paths": ["src"]}), ctx(), admission_id="A")
        self.row = settled_row(["S"], boundary={"paths": ["src"], "surfaces": []},
                               changed_paths=["src/a.py", "tools/extra.py"], boundary_exceeded=["tools/extra.py"],
                               result={"base": "b" * 40, "head": "d" * 40, "worktree": "/w"})
        self.settled = ctx(admissions={"A": self.row})

    def restated(self, **fields):
        rows = rows_of(self.admitted)
        row = next(row for row in rows if row["id"] == "S")
        row.pop("executor")
        row.update({"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}, **fields})
        return rows

    def test_an_undispositioned_result_blocks_only_overlapping_work_and_its_owner(self):
        refused(self, "obligation_unaccounted", "active_admission_settled", accept_write, self.admitted,
                write(self.admitted, rows_of(self.admitted)), self.settled)
        state = accept_write(self.admitted, write(self.admitted, self.restated()), self.settled)
        refused(self, "integration_pending", "integration_pending", admission_refusal, state,
                packet(["T"], boundary={"paths": ["src/x"]}), self.settled, admission_id="B")
        admission_refusal(state, packet(["V"], boundary={"paths": ["docs"]}), self.settled, admission_id="B")
        rows = self.restated(state="satisfied", evidence=[proof("S")])
        rows[1].pop("wait")
        refused(self, "obligation_unaccounted", "undispositioned_result", accept_write, state,
                write(state, rows), self.settled)
        rows = rows_of(state)
        next(row for row in rows if row["id"] == "T").update(
            wait={"class": "integration_pending", "referent": "A"})
        self.assertEqual(by_id(accept_write(state, write(state, rows), self.settled), "T")["wait"]["class"],
                         "integration_pending")

    def test_dispositions_validate_ancestry_attestation_and_boundary_reasons(self):
        ok = {**self.settled, "is_ancestor": lambda head, candidate: candidate == "good"}
        refused(self, "obligation_unaccounted", "disposition_invalid", disposition, self.row,
                {"admission": "A", "integrated_into": "good"}, ok, seq=3)
        refused(self, "obligation_unaccounted", "disposition_invalid", disposition, self.row,
                {"admission": "A", "integrated_into": "bad", "reason": "tooling change agreed"}, ok, seq=3)
        record = disposition(self.row, {"admission": "A", "integrated_into": "good",
                                        "reason": "tooling change agreed"}, ok, seq=3)
        self.assertEqual((record["kind"], record["validated"]), ("integrated_into", "ancestry"))
        uncommitted = {**self.row, "boundary_exceeded": [], "result": {"base": "b" * 40, "head": "b" * 40}}
        refused(self, "obligation_unaccounted", "disposition_invalid", disposition, uncommitted,
                {"admission": "A", "integrated_into": "good"}, ok, seq=3)
        self.assertEqual(disposition(uncommitted, {"admission": "A", "integrated_into": "good",
                                                   "attestation": "applied the patch by hand"}, ok,
                                     seq=3)["validated"], "attestation")
        refused(self, "obligation_unaccounted", "disposition_invalid", disposition, self.row,
                {"admission": "A", "discarded": True}, ok, seq=3)
        refused(self, "obligation_unaccounted", "malformed", disposition, self.row,
                {"admission": "A", "superseded_by": "B", "reason": "replaced"}, ok, seq=3)
        refused(self, "obligation_unaccounted", "disposition_invalid", disposition,
                {**self.row, "disposition": {"kind": "discarded"}},
                {"admission": "A", "discarded": True, "reason": "again"}, ok, seq=3)
        refused(self, "obligation_unaccounted", "disposition_invalid", disposition, self.row,
                {"admission": "A", "discarded": True, "reason": "still running"},
                {**ok, "outstanding": ["A"]}, seq=3)
        pending = {**self.row, "changed_paths": None}
        refused(self, "obligation_unaccounted", "disposition_invalid", disposition, pending,
                {"admission": "A", "integrated_into": "good", "reason": "x", "attestation": "trust me"}, ok, seq=3)
        outside_git = {**pending, "result": {"base": None, "head": None, "worktree": "/w"}}
        refused(self, "obligation_unaccounted", "disposition_invalid", disposition, outside_git,
                {"admission": "A", "integrated_into": "good"}, ok, seq=3)
        attested = disposition(outside_git, {"admission": "A", "integrated_into": "good",
                                             "attestation": "copied into the candidate by hand"}, ok, seq=3)
        self.assertEqual((attested["validated"], attested["boundary_check"]), ("attestation", "unavailable"))

    def test_rejected_result_is_discarded_and_its_replacement_admits(self):
        state = accept_write(self.admitted, write(self.admitted, self.restated()), self.settled)
        refused(self, "integration_pending", "integration_pending", admission_refusal, state,
                packet(["S"], boundary={"paths": ["src"]}), self.settled, admission_id="B")
        self.row["disposition"] = disposition(self.row, {"admission": "A", "discarded": True,
                                                         "reason": "review rejected the approach"},
                                              self.settled, seq=state["seq"] + 1)
        accepted = accept_write(state, write(state, rows_of(state)), self.settled)
        replaced = admit(accepted, packet(["S"], boundary={"paths": ["src"]}), self.settled, admission_id="B")
        self.assertEqual(by_id(replaced, "S")["executor"], "B")
        self.assertEqual(self.row["disposition"]["kind"], "discarded")
        self.assertEqual(self.row["changed_paths"], ["src/a.py", "tools/extra.py"])
        report = report_projection(replaced, self.settled)
        self.assertEqual(report["boundary_exceeded"][0]["disposition"]["kind"], "discarded")


def assurance_state():
    rows = [ob("S", boundary={"paths": ["src"]}),
            ob("A", kind="assurance", scope={"paths": ["src"]}, question="is the parser correct?",
               candidate="c1", existing_evidence="unit tests", insufficiency="no independent review")]
    return accept_write(None, intake(*rows), ctx())


class AssuranceTests(unittest.TestCase):
    def reviewed(self, state=None):
        state = state or assurance_state()
        review = admit(state, packet(["A"], role="review"), ctx(), admission_id="R1")
        row = settled_row(["A"], role="review", report={"outcome": "succeeded", "status": "validated_observation"})
        return review, {"R1": row}

    def settle_review(self, findings=(), extra_rows=()):
        review, admissions = self.reviewed()
        rows = rows_of(review)
        by = {row["id"]: row for row in rows}
        by["A"].update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        by["A"].pop("executor")
        rows += list(extra_rows)
        value, triaged = triage(review, write(review, rows), list(findings), [], ctx(admissions=admissions),
                                admission_id="R1")
        return accept_write(review, value, ctx(admissions=admissions), triaged=triaged), admissions

    def test_review_packets_serve_one_assurance_and_repeat_review_is_refused(self):
        state = assurance_state()
        refused(self, "unbound_assignment", "role_kind_mismatch", admission_refusal, state,
                packet(["S"], role="review"), ctx(), admission_id="r")
        rows = rows_of(state) + [ob("A2", "unassigned", kind="assurance", scope={"paths": ["src/x"]},
                                   question="again?", candidate="c1", existing_evidence="e",
                                   insufficiency="i")]
        refused(self, "obligation_invalid", "missing_uncovered_risk", accept_write, state, write(state, rows), ctx())
        rows[-1]["uncovered_risk"] = "concurrency in the parser cache"
        self.assertEqual(by_id(accept_write(state, write(state, rows), ctx()), "A2")["uncovered_risk"],
                         "concurrency in the parser cache")
        settled, admissions = self.settle_review()
        rows = rows_of(settled)
        by = {row["id"]: row for row in rows}
        by["A"].update(state="satisfied", evidence=[{"attempt": "R1"}])
        by["A"].pop("wait")
        satisfied = accept_write(settled, write(settled, rows), ctx(admissions=admissions))
        self.assertEqual(by_id(satisfied, "A")["evidence"][0]["candidate"], "c1")
        refused(self, "unbound_assignment", "satisfied", admission_refusal, satisfied,
                packet(["A"], role="review"), ctx(admissions=admissions), admission_id="R2")
        self.assertEqual(label_qualification(satisfied, ctx(admissions=admissions), "c1")["label"], "QUALIFIED")

    def test_only_a_completed_validated_review_binds_assurance(self):
        for report in ({"outcome": "failed", "status": "validated_observation"},
                       {"outcome": "partial", "status": "validated_observation"},
                       {"outcome": "blocked", "status": "validated_observation"},
                       {"outcome": "uncertain", "status": "validated_observation"},
                       {"outcome": "succeeded", "status": "reconciliation_required"},
                       {"outcome": "succeeded"}, None):
            with self.subTest(report=report):
                state, admissions = self.settle_review()
                admissions["R1"]["report"] = report
                rows = rows_of(state)
                by = {row["id"]: row for row in rows}
                by["A"].update(state="satisfied", evidence=[{"attempt": "R1"}])
                by["A"].pop("wait")
                refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, state,
                        write(state, rows), ctx(admissions=admissions))
                self.assertEqual(label_qualification(state, ctx(admissions=admissions), "c1")["label"], "WITHHELD")
        state, admissions = self.settle_review()
        admissions["R1"]["binding"] = {**admissions["R1"]["binding"], "environment": "another runner"}
        rows = rows_of(state)
        by = {row["id"]: row for row in rows}
        by["A"].update(state="satisfied", evidence=[{"attempt": "R1"}])
        by["A"].pop("wait")
        refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, state,
                write(state, rows), ctx(admissions=admissions))

    def test_triage_creates_corrections_or_proposals_and_lists_downgrades(self):
        correction = ob("K1", "unassigned", kind="correction", parent="A", boundary={"paths": ["src/p.py"]})
        findings = [
            {"finding": "f1", "severity": "blocker", "triage": "required_correction", "summary": "off by one",
             "correction": "K1"},
            {"finding": "f2", "severity": "major", "triage": "advisory", "summary": "rename helper",
             "reason": "cosmetic; the reviewer marked style issues major"},
            {"finding": "f3", "severity": "minor", "triage": "advisory", "summary": "docstring"},
        ]
        refused(self, "obligation_invalid", "downgrade_unreasoned", self.settle_review,
                [{k: v for k, v in findings[1].items() if k != "reason"}])
        refused(self, "obligation_invalid", "no_parent", self.settle_review, [findings[0]])
        state, admissions = self.settle_review(findings, [correction])
        self.assertEqual(by_id(state, "K1")["finding"]["severity"], "blocker")
        self.assertEqual(len(state["proposals"]), 2)
        report = report_projection(state, ctx(admissions=admissions))
        self.assertEqual([row["finding"] for row in report["triage_downgrades"]], ["f2"])
        rows = rows_of(state)
        next(row for row in rows if row["id"] == "A")["findings"] = []
        next(row for row in rows if row["id"] == "K1").update(
            state="waiting", wait={"class": "sequenced", "referent": "O1"})
        kept = accept_write(state, write(state, rows, proposals=state["proposals"]), ctx(admissions=admissions))
        self.assertEqual(len(by_id(kept, "A")["findings"]), 3)
        forged = ob("K9", "unassigned", kind="correction", parent="A",
                    finding={"attempt": "R1", "finding": "f9", "severity": "blocker"})
        refused(self, "obligation_invalid", "malformed", accept_write, state,
                write(state, [*rows_of(state), forged], proposals=state["proposals"]), ctx(admissions=admissions))
        self.assertEqual(len(report["proposals_out_of_scope"]), 2)
        rows = rows_of(state)
        by = {row["id"]: row for row in rows}
        by["A"].update(state="satisfied", evidence=[{"attempt": "R1"}])
        by["A"].pop("wait")
        refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, state, write(state, rows),
                ctx(admissions=admissions))
        by["K1"].update(state="withdrawn", withdrawal={"by": "coordinator", "reason": "not needed"})
        refused(self, "obligation_invalid", "withdrawal_unauthorized", accept_write, state, write(state, rows),
                ctx(admissions=admissions))

    def test_correction_changing_reviewed_scope_reopens_for_delta_review_and_reuses_unaffected(self):
        state, admissions = self.settle_review()
        rows = rows_of(state)
        evidence = [proof("D", check="doc")]
        rows.append(ob("D", "satisfied", boundary={"paths": ["docs"]}, evidence=evidence))
        by = {row["id"]: row for row in rows}
        by["A"].update(state="satisfied", evidence=[{"attempt": "R1"}])
        by["A"].pop("wait")
        satisfied = accept_write(state, write(state, rows), ctx(admissions=admissions))
        moved = ctx(admissions=admissions, candidate="c2",
                    git_delta=lambda source, target: ["src/parser.py"])
        refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, satisfied,
                write(satisfied, rows_of(satisfied)), moved)
        rows = rows_of(satisfied)
        by = {row["id"]: row for row in rows}
        by["A"].update(state="waiting", wait={"class": "sequenced", "referent": "O1"}, candidate="c2")
        by["D"]["reuse"] = {"from": "c1"}
        reopened = accept_write(satisfied, write(satisfied, rows), moved)
        self.assertEqual(by_id(reopened, "A")["state"], "waiting")
        self.assertEqual(by_id(reopened, "D")["reuse"]["delta"], ["src/parser.py"])
        label = label_qualification(reopened, moved, "c2")
        self.assertEqual(label["assurance_unbound"], [{"obligation": "A", "gap": "unbound"}])
        touching = ctx(admissions=admissions, candidate="c2", git_delta=lambda source, target: ["docs/x.md"])
        refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, satisfied,
                write(satisfied, rows), touching)
        unprovable = ctx(admissions=admissions, candidate="c2", git_delta=lambda source, target: None)
        refused(self, "obligation_unaccounted", "evidence_invalidated", accept_write, satisfied,
                write(satisfied, rows), unprovable)
        delta = admit(reopened, packet(["A"], role="review", delta_from="c1"), moved, admission_id="R2")
        self.assertEqual(by_id(delta, "A")["executor"], "R2")

    def test_label_needs_bound_non_withdrawn_assurance(self):
        none = accept_write(None, intake(), ctx())
        self.assertEqual(label_qualification(none, ctx(), "c1")["assurance_unbound"],
                         [{"obligation": None, "gap": "none_recorded"}])
        unbound = assurance_state()
        self.assertEqual(label_qualification(unbound, ctx(), "c1")["assurance_unbound"],
                         [{"obligation": "A", "gap": "unbound"}])
        rows = rows_of(unbound)
        rows[2].update(state="withdrawn", withdrawal={"by": "coordinator", "reason": "risk judged low"})
        rows[2].pop("wait")
        withdrawn = accept_write(unbound, write(unbound, rows), ctx())
        label = label_qualification(withdrawn, ctx(), "c1")
        self.assertEqual((label["label"], label["assurance_unbound"][0]["gap"]), ("WITHHELD", "none_recorded"))
        self.assertEqual(label["withdrawn"][0]["reason"], "risk judged low")
        self.assertEqual(label_qualification(None, ctx(), "c1")["label"], "WITHHELD")


class LifecycleTests(unittest.TestCase):
    def test_closure_admission_refusal_and_user_reopen(self):
        evidence = [proof("O1")]
        state = accept_write(None, intake(ob("S")), ctx())
        refused(self, "obligation_unaccounted", "closure_unfinished", accept_write, state,
                write(state, rows_of(state), close=True), ctx())
        rows = [criterion("satisfied", evidence=evidence),
                ob("S", "withdrawn", withdrawal={"by": "coordinator", "reason": "folded into O1"})]
        for row in rows:
            row.pop("executor", None)
            row.pop("wait", None)
        refused(self, "obligation_unaccounted", "closure_unfinished", accept_write, state,
                write(state, rows, close=True), ctx(governor_pending=True))
        closed = accept_write(state, write(state, rows, close=True), ctx())
        self.assertEqual(closed["closure"]["report"]["status"], "open")
        self.assertEqual(closed["closure"]["report"]["withdrawn"][0]["reason"], "folded into O1")
        self.assertEqual(closed["closure"]["report"]["label"]["label"], "WITHHELD")
        refused(self, "objective_closed", "objective_closed", admission_refusal, closed, packet(["S"]), ctx(),
                admission_id="a")
        refused(self, "objective_closed", "objective_closed", accept_write, closed, write(closed, rows), ctx())
        refused(self, "objective_closed", "objective_closed", accept_write, closed,
                write(closed, rows, governance_refresh=True), ctx())
        refused(self, "objective_closed", "objective_closed", accept_write, closed,
                write(closed, rows, revision_authority={"provenance": "user_direct", "instruction": "x"}), ctx())
        reopened = accept_write(closed, write(closed, [*rows, ob(
            "N", "active", kind="criterion", provenance="user_direct",
            source={"instruction": "also support CSV"})], reopen=True,
            revision_authority={"provenance": "user_direct", "instruction": "also support CSV"}), ctx())
        self.assertIsNone(reopened["closure"])
        self.assertEqual(reopened["reopened"][0]["closed_revision"], closed["revision"])

    def test_withdrawal_authority(self):
        state = accept_write(None, intake(ob("S")), ctx())
        cases = [
            (0, {"by": "coordinator", "reason": "too hard"}),
            (0, {"by": "project_policy", "reason": "policy says so"}),
            (0, {"by": "objective", "reason": "x"}),
            (0, {"by": "user_direct", "reason": "no longer needed"}),
        ]
        for index, withdrawal in cases:
            rows = rows_of(state)
            rows[index].pop("executor", None)
            rows[index].update(state="withdrawn", withdrawal=withdrawal)
            rows[1]["state"] = "active"
            rows[1]["executor"] = "coordinator"
            rows[1].pop("wait")
            with self.subTest(by=withdrawal["by"]):
                refused(self, "obligation_invalid", "withdrawal_unauthorized", accept_write, state,
                        write(state, rows), ctx())
        rows = rows_of(state)
        rows[0].pop("executor")
        rows[0].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "dropped by the user"})
        rows[1].update(state="active", executor="coordinator")
        rows[1].pop("wait")
        user = {"provenance": "user_direct", "instruction": "drop PoD#1"}
        withdrawn = accept_write(state, write(state, rows, revision_authority=user), ctx())
        self.assertEqual(by_id(withdrawn, "O1")["withdrawal"]["instruction"], "drop PoD#1")
        rows = rows_of(withdrawn)
        rows[1].update(state="withdrawn", withdrawal={"by": "coordinator", "reason": "parent dropped"})
        rows[1].pop("executor")
        done = accept_write(withdrawn, write(withdrawn, rows), ctx())
        self.assertEqual(done["quiescence"], {"state": "closable"})
        rows = rows_of(done)
        rows[0].pop("withdrawal")
        rows[0].update(state="active", executor="coordinator")
        refused(self, "obligation_invalid", "withdrawal_unauthorized", accept_write, done, write(done, rows), ctx())


class ObservationTests(unittest.TestCase):
    def two_writes(self, context, wait=None):
        first = accept_write(None, intake(ob("S", boundary={"paths": ["src"]}, wait=wait or {
            "class": "sequenced", "referent": "O1"}), ob("T")), context)
        rows = rows_of(first)
        second = accept_write(first, write(first, rows), context)
        return first, second

    def test_serialization_flag_needs_free_capacity_and_positive_delegation(self):
        _, second = self.two_writes(ctx())
        self.assertEqual([row["obligation"] for row in second["observations"]["serialization"]], ["S", "T"])
        for context in (ctx(delegation="unavailable"), ctx(delegation="unknown"), ctx(ceiling=0)):
            with self.subTest(context=context["delegation"], ceiling=context["ceiling"]):
                self.assertEqual(self.two_writes(context)[1]["observations"]["serialization"], [])
        first = accept_write(None, intake(ob("S", boundary={"paths": ["src"]})), ctx())
        rows = rows_of(first)
        rows[1]["wait"] = {"class": "sequenced", "referent": "O1", "rationale": "S is smaller than a handoff",
                           "revisit_when": "O1 lands", "inputs": first["observations"]["inputs"]}
        explained = accept_write(first, write(first, rows), ctx())
        self.assertEqual(explained["observations"]["serialization"], [])
        stale = accept_write(explained, write(explained, rows_of(explained)), ctx(candidate="c2"))
        self.assertEqual([row["obligation"] for row in stale["observations"]["serialization"]], ["S"])
        overlapping = accept_write(None, {"governance": {"base_ref": None}, "obligations": [
            criterion(boundary={"paths": ["src"]}), ob("S", boundary={"paths": ["src/a"]})]}, ctx())
        again = accept_write(overlapping, write(overlapping, rows_of(overlapping)), ctx())
        self.assertEqual(again["observations"]["serialization"], [])
        self.assertEqual(serialization_flags(None, again), [])

    def test_churn_is_an_observation_and_status_lists_every_referent(self):
        admissions = {f"a{index}": settled_row(["S"], role="investigate") for index in range(3)}
        state = accept_write(None, intake(ob("S", resolves="why", stop_condition="named")), ctx(admissions=admissions))
        self.assertEqual(state["observations"]["churn"], ["S"])
        lines = status_projection(state, ctx(admissions=admissions))["lines"]
        self.assertTrue(any("sequenced → O1" in line for line in lines))
        self.assertTrue(any(line.startswith("Assurance") for line in lines))
        self.assertEqual(status_projection(None, ctx())["map"], None)

    def test_boundary_overlap_is_component_wise(self):
        self.assertEqual(overlap({"paths": ["src/ab"], "surfaces": []}, {"paths": ["src/a"], "surfaces": []})["paths"], [])
        self.assertEqual(overlap({"paths": ["src"], "surfaces": ["x"]}, {"paths": ["src/a"], "surfaces": ["x"]}),
                         {"paths": ["src", "src/a"], "surfaces": ["x"]})
        self.assertTrue(overlap({"paths": ["."], "surfaces": []}, {"paths": ["any/path"], "surfaces": []})["paths"])


class TraceTests(unittest.TestCase):
    def test_trace_checks_fail_refused_behaviour_and_flag_observations(self):
        first = accept_write(None, intake(ob("S", boundary={"paths": ["src"]})), ctx())
        second = accept_write(first, write(first, rows_of(first)), ctx())
        trace = {"schema": "pod-trace/v1", "label": "synthetic", "records": [
            {"kind": "write", "map": first}, {"kind": "write", "map": second},
            {"kind": "admission", "serves": ["P1"], "accepted": True},
            {"kind": "settlement", "serves": ["S"]}, {"kind": "settlement", "serves": ["S"]},
            {"kind": "settlement", "serves": ["S"]}],
            "observations": {"wall_time_s": 42}}
        result = evaluate_trace(trace)
        self.assertTrue(result["failed"])
        self.assertEqual(result["checks"]["slot_filling"], "FAILED")
        self.assertEqual(result["checks"]["serialization"], "FLAGGED")
        self.assertEqual(result["checks"]["churn"], "FLAGGED")
        self.assertEqual(result["observations"], {"wall_time_s": 42, "admissions": 1})
        native = evaluate_trace({"source": "recorded", "sanitized": True, "observations": [
            {"assignment": "assignment-001", "start_observed": True, "settlement_observed": True,
             "completed_at": "2026-09-24T15:15:11Z"}]})
        self.assertFalse(native["evaluable"])
        self.assertEqual(set(native["checks"].values()), {"NOT_EVALUABLE"})
        self.assertEqual(native["observations"]["admissions"], "unknown")


if __name__ == "__main__":
    unittest.main()
