"""Production JSON projections and the next requests they must continue to support."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import unittest
from unittest.mock import patch

from pod import internal
from pod.errors import PodError
from pod.ledger import objective_root, read, update_admission
from tests.kernel_support import (BRANCH, COMMIT, TREE, GovernorFakePort, action, authorization,
                                  observation, correction, assurance_review, assurance_satisfied,
                                  assurance_waiting)
from tests.test_governor import GovernorCase
from tests.test_runtime_continuity import ContinuityCase
from tests.test_owner_handoff import HandoffCase
from tests.common import VERIFICATION


class JsonBoundary:
    def stored_bytes(self):
        root = objective_root(self.project, "objective")
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
                if path.is_file() and path.name != ".lock"}

    def effects(self):
        return deepcopy((getattr(getattr(self, "port", None), "starts", []),
                         getattr(getattr(self, "port", None), "calls", []),
                         getattr(getattr(self, "remote", None), "calls", []),
                         getattr(self, "native_calls", []), getattr(self, "mutations", [])))

    def invoke(self, operation, request, *, code=0):
        original = internal.run
        captured = {}

        def observe(op, value):
            try:
                result = original(op, value)
                captured["raw"] = deepcopy(result)
                return result
            finally:
                captured["bytes"] = self.stored_bytes()
                captured["effects"] = self.effects()

        output, errors = StringIO(), StringIO()
        with patch("pod.internal.run", side_effect=observe), \
                patch("pod.internal.bounded_stdin_json", return_value=request), \
                redirect_stdout(output), redirect_stderr(errors):
            actual = internal.main([operation, "--input", "-"])
        self.assertEqual(actual, code, output.getvalue() + errors.getvalue())
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(self.stored_bytes(), captured["bytes"], "projection changed stored bytes")
        self.assertEqual(self.effects(), captured["effects"], "projection changed native/provider effects")
        envelope = json.loads(output.getvalue())
        self.assertEqual(envelope["schema"], "pod-cli/v5")
        return envelope.get("result", envelope.get("error")), captured.get("raw")

    def owned(self, **fields):
        return {"project": str(self.project), "objective": "objective", "owner": "owner", **fields}


class BoundaryCase(JsonBoundary, ContinuityCase):
    review = assurance_review
    satisfied = assurance_satisfied
    waiting = assurance_waiting

    def report(self, admission, frozen, **fields):
        value = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                 "attempt": admission["native_binding"]["dispatchId"], "candidate": self.candidate,
                 "outcome": "succeeded", "scope": frozen["body"]["scope"], "files": [], "checks": ["unit"],
                 "failures": [], "evidence": [], "uncertainty": [], "questions": []}
        return internal.run("report", {"project": str(self.project), "objective": "objective",
                                       "admission_id": admission["admission_id"], "packet": frozen,
                                       "report": value, **fields})


@contextmanager
def boundary():
    case = BoundaryCase()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


class AcknowledgementBoundaryTests(unittest.TestCase):
    def test_checkpoint_and_constraint_acknowledgements_drive_actual_next_writes(self):
        with boundary() as case:
            rows = [case.criterion(), case.sub("S")]
            first, raw = case.invoke("checkpoint", case.owned(value=case.core(
                obligations=rows, governance={"base_ref": "target"})))
            case.assertEqual(first["written"]["obligations"], ["O1", "S"])
            case.assertNotIn("obligations", first["checkpoint"])
            case.assertLess(len(json.dumps(first)), len(json.dumps(raw)))
            # The caller already owns the rows it supplied; it needs the new seq,
            # normalized candidate bindings and next action, not another full record.
            request = case.owned(value=case.core(obligations=rows, seq=first["next_seq"]))
            original = case.owned(value=case.core(obligations=rows, seq=raw["checkpoint"]["seq"] + 1))
            case.assertEqual(request, original)
            second, _ = case.invoke("checkpoint", request)
            case.assertEqual(second["seq"], first["seq"] + 1)
            case.assertEqual(second["revision"], first["revision"])
            case.assertEqual(second["next_safe_action"], "continue")
            added, full = case.invoke("constraint", case.owned(action="add", value={
                "id": "limit", "kind": "max_workers", "provenance": "user_direct", "value": 1}))
            revoke = case.owned(action="revoke", id=added["constraint"]["id"])
            case.assertEqual(revoke, case.owned(action="revoke", id=full["constraints"][-1]["id"]))
            revoked, _ = case.invoke("constraint", revoke)
            case.assertEqual(revoked["constraint"], {"id": "limit", "active": False})
            listed, full = case.invoke("constraint", case.owned(action="list"))
            case.assertEqual(listed, full)

    def test_checkpoint_bindings_support_reload_without_echoing_future_stored_fields(self):
        with boundary() as case:
            rows = [case.criterion(), case.sub("S")]
            request = case.owned(value=case.core(obligations=rows, governance={"base_ref": "target"}))
            lean, raw = case.invoke("checkpoint", request)
            future = deepcopy(raw)
            future["checkpoint"]["future_stored_field"] = "stored only"
            case.assertNotIn("future_stored_field", internal.acknowledge("checkpoint", request, future)["checkpoint"])
            frozen = case.packet(["S"])
            admission = case.owned(run="run", task="task", plan_revision="plan", packet=frozen)
            before = case.stored_bytes(), case.effects()
            with patch("pod.__version__", "next-version"):
                blocked, _ = case.invoke("admission", admission, code=1)
                case.assertEqual(blocked["code"], "installed_version_changed")
                case.assertEqual(case.stored_bytes(), before[0])
                value = {key: lean["checkpoint"][key] for key in
                         ("schema", "criteria", "candidate", "plan_revision", "policy_revision", "native_refs", "verification")}
                value.update(seq=lean["next_seq"], obligations=rows)
                refreshed, _ = case.invoke("checkpoint", case.owned(value=value))
                case.assertEqual(refreshed["checkpoint"]["pod_version"], "next-version")
                case.assertEqual(refreshed["checkpoint"]["bundle_digest"], lean["checkpoint"]["bundle_digest"])
                started, _ = case.invoke("admission", admission)
                case.assertEqual(started["status"], "bound")
                case.assertEqual(len(case.port.starts), 1)

    def test_checkpoint_delivery_and_closure_acknowledgements_support_real_followup(self):
        from tests.test_delivery import VerifiedDeliveryTests
        with boundary() as case:
            # The existing delivery fixture supplies a local Git result and exact
            # Governor merge receipt; no provider is contacted.
            candidate, tree, result = VerifiedDeliveryTests.prepare_result(case)
            delivered, raw = case.invoke("checkpoint", case.owned(value=case.core(
                obligations=[case.criterion()], seq=2, delivery={"record": "merge-1"})))
            delivery = delivered["checkpoint"]["delivery"]
            case.assertEqual(delivery, raw["checkpoint"]["delivery"])
            case.assertEqual((delivery["candidate"], delivery["tree"], delivery["result"]), (candidate, tree, result))
            observed, _ = case.invoke("integration-observe", {"project": str(case.project),
                "objective": delivered["objective"], "candidate": delivery["candidate"]})
            case.assertTrue(observed["ancestor_of_base"])
            value = {key: delivered["checkpoint"][key] for key in
                     ("schema", "criteria", "candidate", "plan_revision", "policy_revision", "native_refs", "verification")}
            value.update(seq=delivered["next_seq"], close=True, obligations=[case.criterion(state="satisfied", evidence=[{
                "check": "unit", "command": "unit", "result": "passed", "reference": "fixture-proof"}])])
            closed, raw = case.invoke("checkpoint", case.owned(value=value))
            case.assertEqual(closed["report"], raw["report"])
            case.assertEqual(closed["report"]["status"], "closed")
            case.assertEqual(closed["checkpoint"]["closure"], {"seq": closed["seq"], "revision": closed["revision"]})
            case.assertEqual(closed["checkpoint"]["delivery"], delivery)

    def test_admission_sibling_states_keep_the_same_actual_recovery_request(self):
        cases = ("successful", "refused", "pending", "UNKNOWN", "absent", "mismatch", "effective_unknown", "headless")
        for scenario in cases:
            with self.subTest(scenario=scenario), boundary() as case:
                case.intake(case.sub("S", boundary={"paths": ["src"]}))
                frozen = case.packet(["S"], boundary={"paths": ["src"]})
                request = case.owned(run="run", task="task", plan_revision="plan", packet=frozen, worktree="current")
                if scenario == "refused":
                    case.port.receipt = {"runtime": "runtime", "exit": 1, "request_uuid": "11111111-1111-4111-8111-111111111111",
                                         "error": {"code": "task_not_startable", "message": "not ready"}}
                if scenario == "UNKNOWN":
                    case.port.receipt = {"runtime": "runtime", "exit": 1, "request_uuid": "11111111-1111-4111-8111-111111111111",
                                         "error": {"code": "runtime_error", "message": "outcome unknown"}}
                if scenario == "mismatch": case.port.effective = {"agent": "codex", "model": "gpt-6-luna", "effort": "high"}
                if scenario == "effective_unknown": case.port.effective = {}
                if scenario == "headless": case.port.headless = True
                lean, raw = case.invoke("admission", request)
                admission = lean["admission"]
                original = raw["admission"]
                case.assertLess(len(json.dumps(lean)), len(json.dumps(raw)))
                case.assertEqual(lean["decision"], raw["decision"])
                # Build the actual re-entry, including the frozen packet identity,
                # from returned facts; the original packet is retained by its caller.
                def reentry(row):
                    case.assertEqual(row["packet_id"], frozen["packet_id"])
                    return case.owned(run=row["run_id"], task=row["task_id"], plan_revision=row["plan_revision"],
                                      packet=frozen, worktree=row["worktree"])
                case.assertEqual(reentry(admission), reentry(original))
                case.assertEqual(admission["recovery"], original["recovery"])
                case.assertEqual(admission["error"], original["error"])
                if scenario in ("pending", "absent"):
                    update_admission(case.project, "objective", owner="owner", admission_id=admission["admission_id"],
                                     update=lambda row: row.update(state="unresolved", native_binding=None))
                    case.port.state = scenario
                if scenario == "UNKNOWN":
                    # The lost start is not retried: a completed read reports its
                    # authoritative effect-free refusal for the same admitted UUID.
                    case.port.request_receipt = {"exit": 1, "error": {"code": "task_not_startable", "message": "not ready"}}
                # An unrelated map transition advances seq after reservation. Replay
                # must return that current seq, never the immutable admitted_seq.
                sibling_rows = case.stored()
                if scenario == "refused":
                    sibling_rows = [case.criterion(), case.sub("S", boundary={"paths": ["src"]})]
                sibling, _ = case.invoke("checkpoint", case.owned(value=case.core(
                    obligations=sibling_rows, seq=lean["next_seq"])))
                before = len(case.port.starts)
                recovered, full_recovery = case.invoke("admission", reentry(admission))
                case.assertEqual(recovered["status"], full_recovery["status"])
                case.assertEqual(recovered["action"], full_recovery["action"])
                case.assertEqual(recovered["admission"]["native_binding"], full_recovery["admission"]["native_binding"])
                case.assertEqual(recovered["admission"]["request_uuid"], admission["request_uuid"])
                case.assertEqual(recovered["seq"], sibling["seq"])
                case.assertGreater(recovered["seq"], recovered["admission"]["admitted_seq"])
                if scenario == "pending":
                    case.assertEqual(case.port.starts[-1]["retry_request"], admission["request_uuid"])
                    case.assertEqual(case.port.starts[-1]["route"]["model"], lean["decision"]["model"])
                    case.assertEqual(len(case.port.starts), before + 1)
                else:
                    case.assertEqual(len(case.port.starts), before)
                if scenario == "mismatch": case.assertTrue(recovered["decision"]["route_mismatch"])
                if scenario == "effective_unknown": case.assertTrue(recovered["decision"]["effective_unknown"])
                if scenario == "headless": case.assertIsNone(recovered["admission"]["native_binding"]["terminalHandle"])

    def test_route_failure_acknowledgement_drives_exact_clear_and_readback_failure_is_successful(self):
        with boundary() as case:
            case.intake(case.sub("S"))
            frozen = case.packet(["S"])
            started, raw = case.invoke("admission", case.owned(run="run", task="task", plan_revision="plan", packet=frozen))
            case.settle(started["admission"])
            failed, full = case.invoke("route-failure", case.owned(admission_id=started["admission"]["admission_id"],
                kind="unavailable", source="native observation"))
            failure = failed["admission"]["failures"][-1]
            case.assertEqual(failure, full["failures"][-1])
            request = case.owned(admission_id=failed["admission"]["admission_id"], kind=failure["kind"],
                source=failure["source"], clear=True, cleared_by="user")
            cleared, _ = case.invoke("route-failure", request)
            case.assertEqual(cleared["admission"]["failures"][-1]["cleared_by"], "user")
            before = case.stored_bytes(), case.effects()
            for unavailable in (None, PodError("unsafe_state", "unreadable"), RuntimeError("unreadable")):
                with case.subTest(unavailable=type(unavailable).__name__), patch("pod.ledger.read",
                        side_effect=unavailable if isinstance(unavailable, Exception) else None, return_value=None):
                    ack = internal.acknowledge("admission", case.owned(), raw)
                case.assertEqual(ack["status"], "bound")
                case.assertIn("internal map", ack["acknowledgement_warning"])
                case.assertEqual(ack["admission"]["request_uuid"], raw["admission"]["request_uuid"])
                case.assertEqual((case.stored_bytes(), case.effects()), before)

    def test_report_ingestion_drives_actual_disposition_and_preserves_exceptions(self):
        states = (("succeeded", ["src"]), ("failed", ["src"]), ("succeeded", ["elsewhere"]))
        optionals = ({}, {"triage": None, "proposals": None}, {"triage": [], "proposals": []},
                     {"triage": False, "proposals": ""})
        for outcome, scope, optional in ((outcome, scope, optional) for outcome, scope in states for optional in optionals):
            with self.subTest(outcome=outcome, scope=scope, optional=optional), boundary() as case:
                case.intake(case.sub("S", boundary={"paths": ["src"]}))
                frozen = case.packet(["S"], boundary={"paths": ["src"]})
                started, _ = case.invoke("admission", case.owned(run="run", task="task", plan_revision="plan", packet=frozen))
                admission = started["admission"]
                case.settle(admission)
                report = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                          "attempt": admission["native_binding"]["dispatchId"], "candidate": case.candidate,
                          "outcome": outcome, "scope": scope, "files": [], "checks": ["unit"],
                          "failures": [] if outcome == "succeeded" else ["failed check"],
                          "evidence": [], "uncertainty": ["fixture only"], "questions": []}
                request = {"project": str(case.project), "objective": "objective", "packet": frozen,
                           "admission_id": admission["admission_id"], "report": report, **optional}
                rows = [case.criterion(), case.sub("S", boundary={"paths": ["src"]})]
                request["map"] = {"seq": started["next_seq"], "obligations": rows}
                lean, raw = case.invoke("report", request)
                case.assertEqual(lean["ingestion"], raw["ingestion"])
                case.assertEqual(lean["report"], raw["report"])
                case.assertEqual(lean["uncertainty"], report["uncertainty"])
                case.assertEqual(lean.get("deviations"), raw.get("deviations"))
                disposition = {"admission": lean["written"]["admission_id"], "discarded": True,
                               "reason": "fixture result not integrated"}
                original = {**disposition, "admission": admission["admission_id"]}
                case.assertEqual(disposition, original)
                following = case.owned(value=case.core(obligations=rows, seq=lean["next_seq"], dispositions=[disposition]))
                case.assertEqual(following["value"]["seq"], raw["map"]["seq"] + 1)
                case.invoke("checkpoint", following)
                request.pop("map")
                case.invoke("report", request)  # Idempotent consumption keeps its facts.
                for op, request in (("packet", frozen["body"]),
                                    ("source", {"project": str(case.project), "path": "README.md"}),
                                    ("map", {"project": str(case.project), "objective": "objective"})):
                    projected, original = case.invoke(op, request)
                    case.assertEqual(projected, original)

    def test_real_invalidations_and_refusals_keep_their_details_and_bytes(self):
        with boundary() as case:
            case.intake(case.sub("S"))
            before = case.stored_bytes()
            refused, _ = case.invoke("checkpoint", case.owned(value=case.core(obligations=[], seq=2)), code=1)
            case.assertEqual(refused["code"], "obligation_unaccounted")
            case.assertEqual(case.stored_bytes(), before)
            case.assertEqual(refused["detail"]["referent"]["obligation"], "O1")
        with boundary() as case:
            case.satisfied()
            before = case.stored_bytes()
            refused, _ = case.invoke("checkpoint", case.owned(value=case.core(
                obligations=case.stored(), seq=read(case.project, "objective")["checkpoint"]["seq"] + 1,
                verification={**VERIFICATION, "environment": "changed runner"})), code=1)
            case.assertEqual(refused["detail"]["detail"], "evidence_invalidated")
            case.assertTrue(refused["detail"]["referent"]["invalidations"])
            case.assertEqual(case.stored_bytes(), before)

    def test_triage_and_generated_proposal_ids_drive_real_adoption_and_replay(self):
        for grouped in (False, True):
            for required in (False, True):
                with self.subTest(grouped=grouped, required=required), boundary() as case:
                    rows = [case.criterion(), {"id": "A", "kind": "assurance", "provenance": "coordinator",
                        "parent": "O1", "check": "independent review", "scope": {"paths": ["src"]},
                        "question": "is src correct?", "candidate": case.candidate,
                        "existing_evidence": "unit", "insufficiency": "no independent review",
                        "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}]
                    case.invoke("checkpoint", case.owned(value=case.core(obligations=rows, governance={"base_ref": "target"})))
                    frozen = case.packet(["A"], role="review")
                    started, _ = case.invoke("admission", case.owned(run="run", task="review", plan_revision="plan", packet=frozen))
                    admitted = started["admission"]
                    case.settle(admitted)
                    ids = ["F1", "F2"] if grouped else ["F1"]
                    triage = {"triage": "required_correction" if required else "advisory", "summary": "review concern"}
                    if grouped:
                        triage.update(findings=[{"finding": identity, "severity": "major"} for identity in ids],
                                      root_cause="shared cause")
                    else:
                        triage.update(finding="F1", severity="major")
                    if required:
                        triage["correction"] = "K"
                        rows.append({"id": "K", "kind": "correction", "provenance": "coordinator",
                                     "parent": "A", "check": "repair review concern", "state": "waiting",
                                     "wait": {"class": "sequenced", "referent": "O1"}})
                    else:
                        triage["reason"] = "outside the reviewed contract; record as proposed work"
                    report = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                        "attempt": admitted["native_binding"]["dispatchId"], "candidate": case.candidate,
                        "outcome": "succeeded", "scope": frozen["body"]["scope"], "files": [], "checks": ["unit"],
                        "failures": [], "evidence": [], "uncertainty": [], "questions": []}
                    request = {"project": str(case.project), "objective": "objective", "packet": frozen,
                        "admission_id": admitted["admission_id"], "report": report,
                        "map": {"seq": started["next_seq"], "obligations": rows}, "triage": [triage],
                        "proposals": [{"summary": "optional report proposal", "source": "worker_report"}]}
                    lean, raw = case.invoke("report", request)
                    case.assertEqual(lean["written"]["findings"], ids)
                    case.assertEqual(lean["written"]["corrections"], ["K"] if required else [])
                    case.assertEqual(lean["written"]["proposals"], raw["written"]["proposals"])
                    proposal = next(row["id"] for row in lean["report"]["proposals_out_of_scope"]
                                    if row["summary"] == "optional report proposal")
                    case.assertIn(proposal, lean["written"]["proposals"])
                    rows.append(case.sub("N", provenance="user_direct", adopts=proposal,
                                         source={"instruction": "Adopt the report proposal"}))
                    following = case.owned(value=case.core(obligations=rows, seq=lean["next_seq"],
                        revision_authority={"provenance": "user_direct", "instruction": "Adopt the report proposal"}))
                    case.assertEqual(following["value"]["seq"], raw["map"]["seq"] + 1)
                    adopted, _ = case.invoke("checkpoint", following)
                    case.assertNotIn(proposal, [row["id"] for row in adopted["report"]["proposals_out_of_scope"]])
                    request["map"] = {"seq": adopted["next_seq"], "obligations": rows}
                    replayed, _ = case.invoke("report", request)
                    case.assertEqual(replayed["written"], lean["written"])
                    request.pop("map")
                    request.pop("triage")
                    request.pop("proposals")
                    stable, _ = case.invoke("report", request)
                    case.assertEqual(stable["written"], lean["written"])
                    case.assertEqual(stable["seq"], replayed["seq"])

    def test_missing_verification_stays_visible_and_cannot_qualify_after_context_change(self):
        with boundary() as case:
            rows = [case.criterion(), {"id": "A", "kind": "assurance", "provenance": "coordinator",
                    "parent": "O1", "check": "independent review", "scope": {"paths": ["src"]},
                    "question": "is src correct?", "candidate": case.candidate,
                    "existing_evidence": "unit", "insufficiency": "no independent review",
                    "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}]
            value = case.core(obligations=rows, governance={"base_ref": "target"})
            value.pop("verification")
            case.invoke("checkpoint", case.owned(value=value))
            frozen = case.packet(["A"], role="review")
            lean, raw = case.invoke("admission", case.owned(run="run", task="review", plan_revision="plan", packet=frozen))
            admitted = lean["admission"]
            case.assertIsNone(admitted["binding"]["environment"])
            case.assertEqual(admitted["binding"], raw["admission"]["binding"])
            case.settle(admitted)
            observation = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                "attempt": admitted["native_binding"]["dispatchId"], "candidate": case.candidate,
                "outcome": "succeeded", "scope": frozen["body"]["scope"], "files": [], "checks": ["unit"],
                "failures": [], "evidence": [], "uncertainty": [], "questions": []}
            consumed, _ = case.invoke("report", {"project": str(case.project), "objective": "objective",
                "packet": frozen, "admission_id": admitted["admission_id"], "report": observation,
                "map": {"seq": lean["next_seq"], "obligations": rows}})
            changed, _ = case.invoke("checkpoint", case.owned(value=case.core(obligations=rows, seq=consumed["next_seq"])))
            rows = deepcopy(rows)
            rows[1].pop("wait")
            rows[1].update(state="satisfied", evidence=[{"attempt": admitted["admission_id"]}])
            before = case.stored_bytes()
            refused, _ = case.invoke("checkpoint", case.owned(value=case.core(obligations=rows, seq=changed["next_seq"])), code=1)
            case.assertIn(refused["code"], ("obligation_invalid", "obligation_unaccounted"))
            case.assertEqual(case.stored_bytes(), before)
            case.assertIsNone(read(case.project, "objective")["admissions"][admitted["admission_id"]]["binding"]["environment"])

    def test_manual_continuity_acknowledgement_supports_existing_admission_recovery(self):
        with boundary() as case:
            first, = case.started()
            frozen = case.frozen_packets[first["admission_id"]]
            case.change_runtime()
            case.port.unavailable = {first["native_binding"]["dispatchId"]}
            error = case.refused("runtime_continuity_ambiguous", case.checkpoint_op)
            lean, raw = case.invoke("runtime-continuity", case.owned(decision=case.decision(error)))
            case.assertEqual(lean["runtime_continuity"], {key: value for key, value in raw["runtime_continuity"].items()
                                                        if key != "decision"})
            case.port.unavailable.clear()
            recovered, _ = case.invoke("admission", case.owned(run=first["run_id"], task=first["task_id"],
                plan_revision=first["plan_revision"], packet=frozen, worktree=first["worktree"]))
            case.assertEqual(recovered["admission"]["runtime"], lean["runtime_continuity"]["to_runtime"])
            case.assertEqual(len(case.port.starts), 1)

    def test_owner_handoff_acknowledgement_drives_the_next_owned_checkpoint(self):
        class HandoffBoundary(JsonBoundary, HandoffCase):
            pass
        case = HandoffBoundary()
        case.setUp()
        try:
            case.establish()
            case.lose()
            lean, raw = case.invoke("owner-handoff", {**case.owned(), "owner": "next", "decision": case.decision()})
            case.assertEqual(lean["owner_handoff"]["completion"], raw["owner_handoff"]["completion"])
            case.assertEqual(len(case.mutations), 1)
            following = {**case.owned(), "owner": lean["owner_handoff"]["owners"]["to_owner"],
                         "value": case.core(obligations=case.stored(), seq=lean["next_seq"])}
            case.invoke("checkpoint", following)
            case.assertEqual(len(case.mutations), 1)
            case.assertEqual(len(case.port.starts), 1)
        finally:
            case.doCleanups()


class GovernorAcknowledgementTests(JsonBoundary, GovernorCase):
    def invoke(self, operation, request, **kwargs):
        with patch("pod.internal._governor_projection", return_value=None), \
                patch("pod.governance.canonical_bound_ref", return_value="origin/main"), \
                patch("pod.governor.observe_candidate", return_value=observation()), \
                patch("pod.governor_effects.GhPort", return_value=getattr(self, "remote", None)):
            return super().invoke(operation, request, **kwargs)

    def test_prepare_preflight_and_decision_keep_candidate_bindings(self):
        self.prepared()
        lean, raw = self.invoke("governor-prepare", self.owned(unit="release", branch=BRANCH, tasks=["task"]))
        self.assertLess(len(json.dumps(lean)), len(json.dumps(raw)))
        binding = lean["candidate"]
        self.assertEqual(binding, {key: value for key, value in raw["candidate"].items()
                                  if key not in {"schema", "prepared_at"}})
        for check in ("unit", "workflow-lint"):
            request = self.owned(unit=lean["unit"], candidate=binding["id"], check=check, status="PASS")
            self.assertEqual(request, self.owned(unit=raw["unit"], candidate=raw["candidate"]["id"], check=check, status="PASS"))
            acknowledged, original = self.invoke("governor-preflight", request)
            self.assertEqual(acknowledged, original)  # Already bounded projection.
        branch = lean["branch"]
        request = action(candidate=binding["id"], target=branch["remote"] + "/" + branch["branch"],
            authorization=authorization(candidate=binding["commit"], tree=binding["tree"],
                                        target=binding["base"]["ref"], scope=("publish",)))
        decision, raw = self.invoke("governor", self.owned(action=request))
        self.assertEqual(decision, raw)
        self.assertEqual(decision["decision"], "ALLOW")
        outcome, full = self.invoke("governor-outcome", self.owned(record_id=decision["record_id"],
            outcome="PASS", provider={"merge_commit": COMMIT}, evidence=["test receipt"]))
        self.assertEqual(outcome["receipt"]["provider"], full["receipt"]["provider"])
        self.assertEqual(outcome["receipt"]["provider"]["merge_commit"], COMMIT)
        repeated, full = self.invoke("governor-outcome", self.owned(record_id=outcome["record_id"], outcome=outcome["outcome"]))
        self.assertEqual(repeated["outcome"], full["outcome"])

    def test_execution_and_reconciliation_exceptions_are_exact_in_sibling_states(self):
        self.prepared()
        from pod.governor import prepare_candidate
        for index, scenario in enumerate(("successful", "refused", "UNKNOWN", "pending", "recovered")):
            with self.subTest(scenario=scenario):
                # A fresh candidate avoids accidental reuse masking provider effects.
                candidate = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                    observation=observation(commit=str(index+3)*40, tree=str(index+3)*40), branch=BRANCH)["candidate"]
                self.preflight(candidate["id"])
                self.remote = GovernorFakePort(lost=("push",) if scenario in ("UNKNOWN", "recovered") else (),
                    rejected=("push",) if scenario == "refused" else (),
                    auto_ci=("ci.yml",) if scenario == "pending" else ())
                request = self.owned(action=action(candidate=candidate["id"], authorization=authorization(
                    candidate=candidate["commit"], tree=candidate["tree"], scope=("publish",))))
                executed, raw = self.invoke("governor-execute", request)
                self.assertEqual(executed, raw)
                count = len(self.remote.calls)
                recovered, raw = self.invoke("governor-reconcile", self.owned(record_id=executed["record_id"]))
                self.assertEqual(recovered, raw)
                self.assertEqual(sum(call[0] == "push" for call in self.remote.calls), 1)
                if scenario in ("UNKNOWN", "recovered"):
                    self.assertEqual(executed["outcome"], "UNKNOWN")
                    self.assertEqual(recovered["status"], "PASS")
                if scenario == "refused": self.assertEqual(executed["outcome"], "FAILED")
                self.assertGreaterEqual(len(self.remote.calls), count)
                if scenario == "pending":
                    status, original = self.invoke("governor-status", self.owned())
                    self.assertEqual(status, original)
                    self.assertTrue(status["units"]["release"]["active_validation"])

    def test_classification_drives_correction_without_echoing_the_stored_row(self):
        candidate = self.prepared()["candidate"]
        self.preflight(candidate["id"])
        admitted, _ = self.invoke("governor", self.owned(action=self.with_publish_authorization(action(candidate=candidate["id"]))))
        self.invoke("governor-outcome", self.owned(record_id=admitted["record_id"], outcome="FAILED"))
        lean, raw = self.invoke("governor-classify", self.owned(record_id=admitted["record_id"], classification={
            "class": "code_defect", "reason": "fixture defect", "correction": correction("first")}))
        self.assertEqual(lean["classification"]["correction_identity"], raw["classification"]["correction_identity"])
        self.assertLess(len(json.dumps(lean)), len(json.dumps(raw)))
        corrected, original = self.invoke("governor-correct", self.owned(unit="release", correction=correction("second")))
        self.assertEqual(corrected, original)


if __name__ == "__main__":
    unittest.main()
