"""R205: historical retirement of a record on an older version of a current schema family (synthetic records).

The record is the kind the 0.6.0 cutover left behind: a current context carrying a checkpoint and admissions of
the previous versions, in that format's exact layout. Retirement reads the whole record, proves each field it
names, keeps its bytes and stores its entry beside it with the decision's evidence.
"""

from copy import deepcopy
import hashlib
import json
import os
import unittest
from unittest.mock import patch

from pod import config, evidence_closure
from pod.installer import STATE_SCHEMAS
from pod.ledger import objective_root, state_root
from pod.retirement import SIDECAR, SIDECAR_SCHEMA, older_scope
from pod.util import digest
from tests.common import logical_json
from tests.decision_policy_support import PERSON
from tests.test_evidence_closure import ClosureCase
from tests.test_historical_retirement import RetirementCase


def _previous(schema: str) -> str:
    family, number = schema.rsplit("/v", 1)
    return f"{family}/v{int(number) - 1}"


def _following(schema: str) -> str:
    family, number = schema.rsplit("/v", 1)
    return f"{family}/v{int(number) + 1}"


NONE_RECORDED = ["none recorded"]
# The layout an earlier Pod wrote, key by key: context v4 with a checkpoint v2 and admissions v3.
CONTEXT_KEYS = ("schema", "revision", "owner", "admissions", "checkpoint", "constraints", "interventions",
                "source_rejections")
CHECKPOINT_KEYS = ("schema", "objective", "worktree", "native_refs", "assignments", "questions", "criteria",
                   "candidate", "plan_revision", "policy_revision", "bundle_digest", "pod_version", "next_safe_action",
                   "verification_gaps")
ADMISSION_KEYS = ("schema", "admission_id", "objective", "owner", "state", "run_id", "task_id", "request_uuid",
                  "native_binding", "worktree", "runtime", "request", "route_decision", "effective_evidence",
                  "plan_revision", "packet_id", "created_at", "updated_at", "error", "failures", "reuse_of",
                  "recovery")
RECOVERY_KEYS = ("checkpoint_binding", "placement_binding", "failed_stage", "native_start_state", "receipt",
                 "residual_resources")


def _older_layout(raw: dict, checkpoint: str, admission: str) -> dict:
    """A synthetic record rewritten in the earlier format's layout; its identities stay those Orca's doubles know."""
    older = {key: raw[key] for key in CONTEXT_KEYS if key in raw}
    older["checkpoint"] = {key: raw["checkpoint"][key] for key in CHECKPOINT_KEYS if key in raw["checkpoint"]}
    older["checkpoint"]["schema"] = checkpoint
    older["admissions"] = {}
    for key, row in raw["admissions"].items():
        kept = {name: row[name] for name in ADMISSION_KEYS if name in row}
        kept["recovery"] = {name: row["recovery"][name] for name in RECOVERY_KEYS if name in row["recovery"]}
        kept["schema"] = admission
        # Current writers have additional route policy fields; this fixture models only the earlier writer.
        kept["route_decision"] = {name: row["route_decision"][name] for name in
            ("agent", "model", "requested_effort", "requested_context", "reason", "preference_revision",
             "policy_revision", "constraint_refs", "pod_version", "effective", "route_mismatch", "effective_unknown")}
        kept["route_decision"]["mode"] = "fixture-mode"
        older["admissions"][key] = kept
    return older


def _admission(raw: dict) -> dict:
    return next(iter(raw["admissions"].values()))


class SupersededCase(RetirementCase):
    def superseded(self, change=None, *, checkpoint=None, admission=None):
        """A lost coordinator's record, rewritten as an earlier Pod left it: its checkpoint and admissions on the
        previous schema versions, in that format's layout. `change` edits the raw record further."""
        self.delivered(unknown_start=False)
        path = self.record_path()
        raw = _older_layout(logical_json(path), checkpoint or _previous(STATE_SCHEMAS["checkpoint"]),
                            admission or _previous(STATE_SCHEMAS["admission"]))
        if change is not None:
            change(raw)
        path.write_text(json.dumps(raw))
        return path.read_bytes()

    def record_path(self, objective="objective"):
        return objective_root(self.project, objective) / "context.json"

    def sidecar(self, objective="objective"):
        return objective_root(self.project, objective) / SIDECAR

    def refusal(self, *, field=None):
        verdict = self.retire()["objectives"][0]
        self.assertEqual(verdict["status"], "refused", verdict)
        self.assertFalse(self.sidecar().exists())
        if field is not None:
            self.assertEqual((verdict["condition"], verdict["field"]), ("retirement_scope_unproved", field))
        return verdict["condition"]

    def rewrite_entry(self, change):
        """Edit the stored entry document after a retirement, as a damaged or hand-made file would leave it."""
        document = json.loads(self.sidecar().read_text())
        change(document)
        os.chmod(self.sidecar(), 0o600)
        self.sidecar().write_text(json.dumps(document))

    def uncertified(self, code):
        shown = self.status()
        self.assertEqual((shown["status"], shown["blocker"], shown["readiness"]["state"]),
                         ("blocked", code, "unavailable"))
        self.assertNotIn("phase", shown)
        row = self.listed()["objective"]
        self.assertEqual((row["phase"], row["readiness"], "retirement" in row), ("open", "unavailable", False))
        proof = evidence_closure.prove(self.project, "objective", port=self.port, remote=self.remote)
        self.assertEqual((proof["status"], [item["code"] for item in proof["conditions"]]),
                         ("unverified", ["retirement_unverified"]))


class SupersededRetirementTests(SupersededCase):
    def test_detection_reads_the_whole_record_and_retirement_keeps_its_bytes(self):
        before = self.superseded()
        found = self.retire()
        verdict = found["objectives"][0]
        self.assertEqual((found["status"], verdict["status"], verdict["record_class"]),
                         ("retirement_available", "eligible", "older"))
        self.assertEqual((verdict["facts"]["owner"], verdict["facts"]["release"]), ("owner", [self.where()]))
        admissions = [row for row in verdict["facts"]["records"] if row["kind"] == "admission"]
        self.assertEqual([(row["role"], row["state"], row["reported"]) for row in admissions],
                         [(None, "bound", False)])
        self.assertIn("superseded record schema", found["question"])
        self.assertIn("No delivery is recorded for objective", found["question"])
        self.assertFalse(self.sidecar().exists())  # Detection writes nothing.
        decision = self.decide(delivery=NONE_RECORDED)
        result = self.retire(decision)
        self.assertEqual(result["status"], "retired", result)
        self.assertEqual(self.record_path().read_bytes(), before)
        stored = json.loads(self.sidecar().read_text())
        entry = stored["historical_retirement"]
        self.assertEqual((entry["delivery"], entry["provenance"], entry["actor"]["kind"]),
                         (NONE_RECORDED, "user_direct", "person"))
        self.assertTrue((state_root(self.project) / ".relay-authorizations" / stored["receipt"]).is_file())
        replay = self.retire(decision)["objectives"][0]
        self.assertEqual(replay["status"], "replayed")
        other = self.retire({**decision, "instruction": "Another decision"})["objectives"][0]
        self.assertEqual((other["status"], other["condition"]), ("refused", "objective_retired"))
        self.assertEqual(self.record_path().read_bytes(), before)

    def test_status_and_list_read_retired_and_every_write_refuses(self):
        before = self.superseded()
        self.retire(self.decide(delivery=NONE_RECORDED))
        shown = self.status()
        self.assertEqual((shown["status"], shown["phase"], shown["blocker"], shown["schema"], shown["record_class"]),
                         ("observed", "retired", "objective_retired", "superseded", "older"))
        readiness = shown["readiness"]
        self.assertEqual({key: readiness[key] for key in ("contract", "state", "acts_next", "coordinator_needed")},
                         {"contract": "pod-readiness/v1", "state": "retired", "acts_next": "none",
                          "coordinator_needed": False})
        self.assertEqual((shown["retirement"]["completed"], shown["retirement"]["released"],
                          shown["retirement"]["delivery"]), (False, [self.where()], NONE_RECORDED))
        self.assertEqual(shown["documented"]["owner"], "owner")
        self.assertEqual([row["settlement"] for row in shown["assignments"]["settled"]], ["retirement"])
        self.assertIn("Historical retirement by the owner", self.text_status())
        row = self.listed()["objective"]
        self.assertEqual((row["phase"], row["schema"], row["readiness"]), ("retired", "superseded", "retired"))
        self.assertEqual(row["retirement"]["released"], [self.where()])
        # R206: computed from the unchanged record on read; an objective under historical retirement holds nothing.
        for block in (shown["retirement"], row["retirement"]):
            self.assertEqual((block["retained"], block["also_released"], block["unrecorded_locations"]),
                             ([], [], "hold_ends"))
        self.returning()
        for name, call in {"checkpoint": lambda: self.op("checkpoint", value=self.core()),
                           "governor-prepare": lambda: self.op("governor-prepare", unit="next")}.items():
            with self.subTest(path=name):
                self.refused(call, code="objective_retired")
        self.assertEqual(self.record_path().read_bytes(), before)

    def test_an_agent_decision_the_hands_off_policy_accepts_retires(self):
        self.superseded()
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        decision = {key: value for key, value in self.decide(delivery=NONE_RECORDED).items() if key != "provenance"}
        decision.update(provenance="agent_decision", channel="agent_session")
        result = self.retire(decision)
        self.assertEqual(result["status"], "retired", result)
        self.assertEqual(self.status()["retirement"]["channel"], "agent_session")
        self.assertEqual(self.retire(decision)["objectives"][0]["status"], "replayed")
        # Its text never says the owner decided: the sidecar's write fence, status and proof read neutrally.
        agent = "historical retirement (agent decision under the decision policy)"
        shown = self.status()
        self.assertEqual(shown["retirement"]["decided_by"], "agent")
        self.assertTrue(shown["readiness"]["summary"].startswith(f"Ended by a {agent}"))
        conditions = evidence_closure.prove(self.project, "objective")["conditions"]
        self.assertEqual([row["message"] for row in conditions], [f"A {agent} covers it."])
        self.returning()
        refused = self.refused(lambda: self.op("checkpoint", value=self.core()), code="objective_retired")
        self.assertTrue(str(refused).startswith(f"A {agent} of this objective at "), str(refused))

    def test_an_owner_decision_relayed_without_verification_refuses(self):
        self.superseded()
        decision = self.decide(delivery=NONE_RECORDED, channel="relayed",
                               relay={"reference": "message-1", "owner_source": "chat"})
        self.refused(lambda: self.retire(decision), code="retirement_decision_unverifiable")
        self.assertFalse(self.sidecar().exists())


class SupersededRefusalTests(SupersededCase):
    def test_a_live_coordinator_or_live_work_still_refuses(self):
        self.superseded()
        self.terminals["owner"] = "live"
        self.assertEqual(self.refusal(), "owner_not_lost")
        self.terminals["owner"] = "stale"
        self.port.workers["dispatch-1"].update(outcome="in_progress", state="ready")
        self.assertEqual(self.refusal(), "work_live")

    def test_a_present_path_that_is_no_worktree_of_this_repository_refuses(self):
        plain = self.root / "plain-directory"
        plain.mkdir()
        def change(raw):
            raw["checkpoint"]["worktree"].update(path=str(plain))
            _admission(raw)["recovery"]["placement_binding"] = raw["checkpoint"]["worktree"]
        self.superseded(change)
        verdict = self.retire(objectives=[{"objective": "objective", "release": [str(plain)]}])["objectives"][0]
        self.assertEqual((verdict["status"], verdict["condition"]), ("refused", "path_foreign"))
        self.assertFalse(self.sidecar().exists())

    def test_an_unknown_schema_or_a_malformed_older_record_refuses(self):
        self.superseded(checkpoint=_following(STATE_SCHEMAS["checkpoint"]))
        self.assertEqual(self.refusal(), "scope_unreadable")
        path = self.record_path()
        raw = json.loads(path.read_text())
        raw["checkpoint"]["schema"] = _previous(STATE_SCHEMAS["checkpoint"])
        _admission(raw)["state"] = "unheard-of"
        path.write_text(json.dumps(raw))
        self.assertEqual(self.refusal(), "scope_unreadable")


class SupersededScopeTests(SupersededCase):
    """Every field of the older record is read and proven, resource-free, or descriptive; anything else refuses."""

    def per_run(self):
        """Orca's worker list names only that Run's Dispatches."""
        base = self.native_read

        def read(argv, **kwargs):
            answer = base(argv, **kwargs)
            if argv[:2] == ["orchestration", "worker-list"]:
                run_id = argv[argv.index("--run") + 1]
                answer["result"]["workers"] = [row for row in answer["result"]["workers"]
                                               if self.port.workers[row["dispatchId"]]["run"] == run_id]
            return answer
        return patch("pod.orca.read_command", side_effect=read)

    def live_dispatch(self, run_id):
        live = deepcopy(self.port.workers["dispatch-1"])
        live.update(run=run_id, task="other-task", outcome="in_progress", state="ready")
        self.port.workers["other-live-dispatch"] = live

    def test_a_binding_on_another_run_than_its_admission_refuses_before_any_native_read(self):
        self.superseded(lambda raw: _admission(raw)["native_binding"].update(runId="other-run"))
        self.port.workers["dispatch-1"]["run"] = "other-run"
        self.live_dispatch("other-run")
        key = next(iter(json.loads(self.record_path().read_text())["admissions"]))[:12]
        self.native_calls.clear()
        self.assertEqual(self.refusal(field=f"admissions.{key}.native_binding"), "retirement_scope_unproved")
        self.assertEqual(self.native_calls, [])

    def test_every_run_the_record_names_has_its_complete_worker_list_read(self):
        def change(raw):
            _admission(raw).update(run_id="other-run")
            _admission(raw)["native_binding"].update(runId="other-run")
        self.superseded(change)
        self.port.workers["dispatch-1"]["run"] = "other-run"
        self.live_dispatch("other-run")
        with self.per_run():
            self.assertEqual(self.refusal(), "work_live")
            self.port.workers["other-live-dispatch"].update(outcome="succeeded", state="completed")
            verdict = self.retire()["objectives"][0]
        self.assertEqual(verdict["status"], "eligible", verdict)
        self.assertEqual([row["run"] for row in verdict["facts"]["runs"]], ["other-run", "run"])

    def test_a_malformed_binding_refuses_in_every_admission_state(self):
        def change(raw):
            _admission(raw).update(state="reserved", native_binding={"runId": "run", "dispatchId": "dispatch-9"})
        self.superseded(change)
        key = next(iter(json.loads(self.record_path().read_text())["admissions"]))[:12]
        self.refusal(field=f"admissions.{key}.native_binding")

    def test_a_native_reference_in_assignments_refuses(self):
        self.superseded(lambda raw: raw["checkpoint"].update(assignments=[
            {"runId": "other-run", "dispatchId": "other-dispatch", "terminalHandle": "other-terminal"}]))
        self.refusal(field="checkpoint.assignments")

    def test_a_start_request_held_only_in_recovery_refuses(self):
        def change(raw):
            _admission(raw).update(state="unresolved", request_uuid=None, native_binding=None)
            _admission(raw)["recovery"]["receipt"] = {"request_uuid": "pending-request", "runId": "run"}
        self.superseded(change)
        key = next(iter(json.loads(self.record_path().read_text())["admissions"]))[:12]
        self.refusal(field=f"admissions.{key}.recovery.receipt")

    def test_a_first_format_record_with_effects_refuses(self):
        def change(raw):
            raw["schema"] = _previous(_previous(_previous(STATE_SCHEMAS["context"])))
            raw.pop("admissions")
            raw["effects"] = {"pending-operation": {"state": "reserved", "run_id": "other-run",
                                                    "native_binding": None}}
        self.superseded(change)
        self.refusal(field="effects")

    def test_an_unknown_key_refuses(self):
        self.superseded(lambda raw: raw["checkpoint"].update(coordinator_slot={"run": "other-run"}))
        self.refusal(field="checkpoint.coordinator_slot")


class SupersededDescriptiveTests(unittest.TestCase):
    """Sanitized shape controls, derived from the earlier writer and genuine-record shape inspection."""

    @classmethod
    def setUpClass(cls):
        cls.case = SupersededCase()
        cls.case.setUp()
        cls.addClassCleanup(cls.case.doCleanups)
        cls.baseline = json.loads(cls.case.superseded())

    def shape_record(self):
        raw = deepcopy(self.baseline)
        raw["constraints"] = [{"id": "ceiling", "kind": "max_workers", "provenance": "repository", "value": 2,
                               "active": True},
                              {"id": "agents", "kind": "only_agents", "provenance": "repository",
                               "value": ["codex"], "role": "worker", "saved_state": "enabled",
                               "mode": "fixture-mode", "file_stamp": "fixture-stamp"}]
        raw["checkpoint"].update(remaining_gates=["review"], verification_gaps=["native proof"])
        row = _admission(raw)
        row["effective_evidence"] = {"requested": deepcopy(row["request"]),
                                     "effective": deepcopy(row["route_decision"]["effective"])}
        row["recovery"]["checkpoint_binding"]["objective_source"] = {
            "schema": "pod-issue-source/v1", "repository": "acme/example", "number": 1,
            "locator": "https://github.com/acme/example/issues/1", "body_sha256": "a" * 64,
            "amendments": [{"locator": "https://github.com/acme/example/issues/1#issuecomment-2",
                            "sha256": "b" * 64}]}
        return raw

    def admission_path(self, raw):
        return ("admissions", next(iter(raw["admissions"])))

    def reject_fields(self, raw, parent, fields):
        """Each field gets hidden native content; the production boundary must refuse before reading Orca."""
        for field in fields:
            with self.subTest(parent=parent, field=field):
                changed = deepcopy(raw)
                target = changed
                for part in parent:
                    target = target[part]
                previous = target[field] if isinstance(target, list) else target.get(field)
                target[field] = {"runId": "hidden-run", "dispatchId": "hidden-dispatch"}
                self.case.record_path().write_text(json.dumps(changed))
                before, journal, starts = self.case.record_path().read_bytes(), self.case.journal(), deepcopy(self.case.port.starts)
                parts = (*parent, field)
                path = "".join(f"[{part}]" if isinstance(part, int) else ("." if i else "") + part
                               for i, part in enumerate(parts))
                # Admission prefixes follow the existing diagnostic format.
                path = path.replace(parts[1], parts[1][:12], 1) if parts[0] == "admissions" else path
                if isinstance(previous, dict):
                    path += ".runId"
                self.case.native_calls.clear()
                self.case.refusal(field=path)
                self.assertEqual(self.case.native_calls, [])
                self.assertEqual((self.case.record_path().read_bytes(), self.case.journal(), self.case.port.starts),
                                 (before, journal, starts))
                self.assertEqual(self.case.mutations, [])

    def test_context_and_constraint_shapes_refuse_hidden_content(self):
        raw = self.shape_record()
        self.reject_fields(raw, (), ("revision", "constraints"))
        self.reject_fields(raw, ("constraints", 0), ("id", "kind", "provenance", "value", "active", "runId"))
        self.reject_fields(raw, ("constraints", 1), ("role", "saved_state", "mode", "file_stamp"))
        self.reject_fields(raw, ("constraints", 1, "value"), (0,))

    def test_checkpoint_scalar_shapes_refuse_hidden_content(self):
        raw = self.shape_record()
        self.reject_fields(raw, ("checkpoint",), ("candidate", "plan_revision", "policy_revision", "bundle_digest",
                                                 "pod_version", "next_safe_action"))
        self.reject_fields(raw, ("checkpoint", "native_refs", 0), ("runtime",))
        self.reject_fields(raw, ("checkpoint", "worktree"), ("branch", "repo_key", "repository"))

    def test_checkpoint_list_shapes_refuse_hidden_content(self):
        raw = self.shape_record()
        self.reject_fields(raw, ("checkpoint",), ("criteria", "verification_gaps", "remaining_gates"))
        for field in ("criteria", "verification_gaps", "remaining_gates"):
            self.reject_fields(raw, ("checkpoint", field), (0,))

    def test_admission_scalar_shapes_refuse_hidden_content(self):
        raw = self.shape_record()
        self.reject_fields(raw, self.admission_path(raw), ("runtime", "plan_revision", "packet_id",
                                                          "created_at", "updated_at"))

    def test_request_and_effective_evidence_shapes_refuse_hidden_content(self):
        raw = self.shape_record()
        parent = self.admission_path(raw)
        self.reject_fields(raw, parent, ("request", "effective_evidence"))
        self.reject_fields(raw, (*parent, "request"), ("agent", "model", "effort", "context", "reason", "runId"))

    def test_effective_evidence_route_shapes_refuse_hidden_content(self):
        raw = self.shape_record()
        parent = self.admission_path(raw)
        self.reject_fields(raw, (*parent, "effective_evidence", "requested"),
                           ("agent", "model", "effort", "context", "reason", "runId"))

    def test_effective_route_shapes_refuse_hidden_content(self):
        raw = self.shape_record()
        parent = self.admission_path(raw)
        self.reject_fields(raw, (*parent, "effective_evidence", "effective"),
                           ("agent", "model", "effort", "context", "runId"))
        self.reject_fields(raw, (*parent, "effective_evidence"), ("runId",))

    def test_route_decision_shapes_refuse_hidden_content(self):
        raw = self.shape_record()
        parent = self.admission_path(raw)
        self.reject_fields(raw, parent, ("route_decision",))
        parent = (*parent, "route_decision")
        self.reject_fields(raw, parent, ("agent", "model", "requested_effort", "requested_context", "reason",
                                        "preference_revision", "policy_revision", "mode", "pod_version"))

    def test_route_decision_flags_and_containers_refuse_hidden_content(self):
        raw = self.shape_record()
        parent = (*self.admission_path(raw), "route_decision")
        self.reject_fields(raw, parent, ("route_mismatch", "effective_unknown", "constraint_refs", "effective", "runId"))

    def test_route_decision_effective_and_reference_shapes_refuse_hidden_content(self):
        raw = self.shape_record()
        parent = (*self.admission_path(raw), "route_decision")
        _admission(raw)["route_decision"]["constraint_refs"] = ["ceiling"]
        self.reject_fields(raw, (*parent, "constraint_refs"), (0,))
        self.reject_fields(raw, (*parent, "effective"), ("agent", "model", "effort", "context", "runId"))

    def test_checkpoint_binding_shapes_refuse_hidden_content(self):
        raw = self.shape_record()
        parent = (*self.admission_path(raw), "recovery", "checkpoint_binding")
        self.reject_fields(raw, parent, ("candidate", "criteria", "plan_revision", "policy_revision",
                                        "objective_source"))
        self.reject_fields(raw, (*parent, "criteria"), (0,))

    def test_checkpoint_binding_source_shapes_refuse_hidden_content(self):
        raw = self.shape_record()
        parent = (*self.admission_path(raw), "recovery", "checkpoint_binding")
        self.reject_fields(raw, (*parent, "objective_source"), ("schema", "repository", "number", "locator",
                                                             "body_sha256", "amendments", "runId"))
        self.reject_fields(raw, (*parent, "objective_source", "amendments", 0), ("locator", "sha256", "runId"))

    def test_scalar_types_and_list_containers_are_exact(self):
        raw = self.shape_record()
        for path, value in ((('revision',), True), (('checkpoint', 'candidate'), []),
                            (('checkpoint', 'criteria'), 'text'), (('constraints',), {}),
                            (('constraints', 0, 'value'), True), (('constraints', 1, 'value'), 'codex')):
            with self.subTest(path=path):
                changed = deepcopy(raw)
                target = changed
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                self.assertIsNotNone(older_scope(changed, "objective")[1])
        raw = self.shape_record()
        cases = ((('constraints', 0, 'kind'), 7, 'constraints[0].kind'),
                 (('constraints', 0, 'kind'), 'unsupported', 'constraints[0].kind'),
                 (('constraints', 0, 'value'), None, 'constraints[0].value'),
                 (('checkpoint', 'candidate'), None, 'checkpoint.candidate'),
                 (('checkpoint', 'candidate'), 'lone\ud800surrogate', 'checkpoint.candidate'))
        for path, value, expected in cases:
            with self.subTest(path=path, value=repr(value)):
                changed = deepcopy(raw)
                target = changed
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                found = older_scope(changed, "objective")[1]
                self.assertEqual(found, expected)
        changed = deepcopy(raw)
        del changed["constraints"][0]["value"]
        self.assertEqual(older_scope(changed, "objective")[1], "constraints[0].value")

    def test_descriptive_maps_require_their_documented_members(self):
        raw = self.shape_record()
        for field, member in (("request", "reason"), ("route_decision", "route_mismatch"),
                              ("effective_evidence", "effective")):
            with self.subTest(field=field):
                changed = deepcopy(raw)
                del _admission(changed)[field][member]
                key = next(iter(raw["admissions"]))[:12]
                self.assertEqual(older_scope(changed, "objective")[1], f"admissions.{key}.{field}.{member}")

    def test_other_historical_constraint_and_nullable_shapes_remain_supported(self):
        raw = self.shape_record()
        for kind, value in (("only_models", ["fixture-model"]), ("exclude_models", ["fixture-model"]),
                            ("role_model", "fixture-model"), ("allow_disabled", "fixture-model"),
                            ("allow_delegation", True)):
            with self.subTest(kind=kind):
                raw["constraints"] = [{"kind": kind, "value": value, "provenance": "user_direct"}]
                self.assertEqual(older_scope(raw, "objective"), (["run"], None))
        recovery = _admission(raw)["recovery"]
        for place in (raw["checkpoint"]["worktree"], recovery["placement_binding"],
                      recovery["checkpoint_binding"]["worktree"]):
            if place is not None:
                place.update(repo_key=None, repository=None)
        _admission(raw)["effective_evidence"] = {}
        _admission(raw)["route_decision"]["constraint_refs"] = [None, "ceiling"]
        self.assertEqual(older_scope(raw, "objective"), (["run"], None))

    def test_null_branch_remains_end_to_end_eligible(self):
        raw = self.shape_record()
        recovery = _admission(raw)["recovery"]
        raw["checkpoint"]["worktree"]["branch"] = None
        recovery["placement_binding"]["branch"] = None
        recovery["checkpoint_binding"]["worktree"] = deepcopy(raw["checkpoint"]["worktree"])
        recovery["checkpoint_binding"]["worktree"]["branch"] = None
        self.case.record_path().write_text(json.dumps(raw))
        before, journal = self.case.record_path().read_bytes(), self.case.journal()
        verdict = self.case.retire()["objectives"][0]
        self.assertEqual((verdict["status"], verdict["record_class"]), ("eligible", "older"), verdict)
        self.assertEqual((self.case.record_path().read_bytes(), self.case.journal()), (before, journal))
        self.assertFalse(self.case.sidecar().exists())

    def test_sanitized_historical_shapes_remain_eligible(self):
        raw = self.shape_record()
        self.assertEqual(older_scope(raw, "objective"), (["run"], None))
        for source in (None, _admission(raw)["recovery"]["checkpoint_binding"]["objective_source"]):
            with self.subTest(source_is_null=source is None):
                changed = deepcopy(raw)
                _admission(changed)["recovery"]["checkpoint_binding"]["objective_source"] = source
                self.case.record_path().write_text(json.dumps(changed))
                before, journal = self.case.record_path().read_bytes(), self.case.journal()
                verdict = self.case.retire()["objectives"][0]
                self.assertEqual((verdict["status"], verdict["record_class"]), ("eligible", "older"), verdict)
                self.assertEqual((self.case.record_path().read_bytes(), self.case.journal()), (before, journal))
                self.assertFalse(self.case.sidecar().exists())


class SupersededEntryTests(SupersededCase):
    """Only a certified entry reads `retired`; its presence alone fences writes and proves nothing."""

    def retire_older(self):
        self.superseded()
        self.assertEqual(self.retire(self.decide(delivery=NONE_RECORDED))["status"], "retired")

    def test_a_hand_placed_entry_without_a_decision_never_reads_retired(self):
        before = self.superseded()
        found = self.retire()["objectives"][0]
        facts = found["facts"]
        entry = {"at": "2026-10-31T00:00:00+00:00", "decision": "0" * 64, "instruction": "No decision was made",
                 "reason": "hand-placed", "repository": "another-repository", "objectives": ["unrelated"],
                 "owner": facts["owner"], "runs": facts["runs"], "released": facts["release"],
                 "locations": facts["locations"], "preserved": facts["records"], "unfinished": [],
                 "delivery": NONE_RECORDED, "native": found["native"]}
        self.sidecar().write_text(json.dumps({
            "schema": SIDECAR_SCHEMA, "objective": "objective", "repository": "another-repository",
            "record": {"context_sha256": hashlib.sha256(before).hexdigest()}, "historical_retirement": entry}))
        self.terminals["owner"] = "live"
        self.uncertified("state_unsupported")
        self.returning()
        self.refused(lambda: self.op("checkpoint", value=self.core()), code="objective_retired")

    def test_another_repository_another_scope_or_a_missing_receipt_never_reads_retired(self):
        self.retire_older()
        stored = json.loads(self.sidecar().read_text())
        receipt = state_root(self.project) / ".relay-authorizations" / stored["receipt"]
        for name, change, code in (
                ("repository", lambda doc: doc.update(repository="another-repository"), "retirement_unverified"),
                ("objectives", lambda doc: doc["historical_retirement"].update(objectives=["unrelated"]),
                 "retirement_unverified"),
                ("decision", lambda doc: doc["historical_retirement"]["decision_binding"].update(kind="claim"),
                 "invalid_decision")):
            with self.subTest(changed=name):
                self.rewrite_entry(lambda doc: doc.update(deepcopy(stored)))
                self.rewrite_entry(change)
                self.uncertified(code)
        self.rewrite_entry(lambda doc: doc.update(deepcopy(stored)))
        self.assertEqual(self.status()["phase"], "retired")
        receipt.unlink()
        self.uncertified("retirement_unverified")

    def test_changed_result_facts_or_a_missing_publication_never_read_retired(self):
        self.retire_older()
        stored = json.loads(self.sidecar().read_text())
        for name, change in {
                "delivery": lambda doc: doc["historical_retirement"].update(delivery=["PR #1 merged"]),
                "released": lambda doc: doc["historical_retirement"].update(released=["/elsewhere"],
                                                                            locations=[{"path": "/elsewhere",
                                                                                        "disposition": "absent"}]),
                "preserved": lambda doc: doc["historical_retirement"].update(preserved=[])}.items():
            with self.subTest(changed=name):
                self.rewrite_entry(lambda doc: doc.update(deepcopy(stored)))
                self.rewrite_entry(change)
                self.uncertified("retirement_unverified")
        self.rewrite_entry(lambda doc: doc.update(deepcopy(stored)))
        self.assertEqual(self.status()["phase"], "retired")
        published = [path for path in (state_root(self.project) / ".relay-authorizations").iterdir()
                     if path.suffix == ".json" and json.loads(path.read_text()).get("objective") == "objective"]
        self.assertEqual(len(published), 1)
        published[0].unlink()
        self.uncertified("retirement_unverified")

    def test_a_certificate_copied_to_an_objective_the_same_decision_refused_never_reads_retired(self):
        self.superseded()
        self.successor(name="other", caller="other-owner", run_id="other-run")
        self.run_holders["other-run"] = "other-owner"
        self.terminals["other-owner"] = "live"
        other = self.record_path("other")
        other.write_text(json.dumps(_older_layout(logical_json(other), _previous(STATE_SCHEMAS["checkpoint"]),
                                                  _previous(STATE_SCHEMAS["admission"]))))
        objectives = [{"objective": name, "release": [self.where()]} for name in ("objective", "other")]
        landed = self.retire(self.decide(objectives=objectives, delivery=NONE_RECORDED), objectives=objectives)
        self.assertEqual([(row["status"], row["condition"]) for row in landed["objectives"]],
                         [("retired", "retirable"), ("refused", "owner_not_lost")])
        self.assertFalse(self.sidecar("other").exists())
        certificate = json.loads(self.sidecar().read_text())
        certificate["objective"] = "other"
        certificate["record"]["context_sha256"] = hashlib.sha256(other.read_bytes()).hexdigest()
        for row in certificate["decision_order"]:
            row["key"] = row["key"].split(":", 1)[0] + ":" + digest("other")
        self.sidecar("other").write_text(json.dumps(certificate))
        shown = self.status(objective="other")
        self.assertEqual((shown["status"], shown["blocker"], shown["readiness"]["state"]),
                         ("blocked", "retirement_unverified", "unavailable"))
        row = {"root": other.parent, "raw": json.loads(other.read_text()), "objective": "other"}
        self.assertFalse(evidence_closure._settled_record(self.project, row))
        self.assertEqual(self.status()["phase"], "retired")  # The objective that did retire still reads so.

    def test_a_stripped_or_stale_entry_never_reads_retired_or_settles(self):
        self.retire_older()
        self.rewrite_entry(lambda doc: [doc["historical_retirement"].pop(key) for key in ("decision_binding", "actor")])
        self.uncertified("state_unsupported")
        raw = json.loads(self.record_path().read_text())
        row = {"root": self.record_path().parent, "raw": raw, "objective": "objective"}
        self.assertFalse(evidence_closure._settled_record(self.project, row))
        os.chmod(self.sidecar(), 0o600)
        self.sidecar().write_text("{")
        self.assertFalse(evidence_closure._settled_record(self.project, row))
        self.uncertified("state_unsupported")
        self.returning()
        self.refused(lambda: self.op("checkpoint", value=self.core()), code="objective_retired")

    def test_a_malformed_stored_entry_never_reads_retired(self):
        self.retire_older()
        self.rewrite_entry(lambda doc: doc["record"].update(context_sha256="0" * 64))
        self.uncertified("state_unsupported")
        self.returning()
        self.refused(lambda: self.op("checkpoint", value=self.core()), code="objective_retired")


class LiveRecoveryTests(ClosureCase):
    def test_a_junk_or_uncertified_entry_never_ends_a_live_recovery_holding_the_run(self):
        self.shipped(unknown_start=False)
        self.recovery(live=True)
        self.rewrite("recovery", older=lambda raw: raw["checkpoint"].update(
            schema=_previous(STATE_SCHEMAS["checkpoint"])))

        def held():
            proof = evidence_closure.prove(self.project, "objective", port=self.port, remote=self.remote)
            return proof["status"], [row["code"] for row in proof["conditions"]]

        self.assertEqual(held(), ("not_proved", ["run_held"]))
        entry = objective_root(self.project, "recovery") / SIDECAR
        for content in ("{", json.dumps({"schema": SIDECAR_SCHEMA, "objective": "recovery"})):
            with self.subTest(entry=content[:12]):
                entry.write_text(content)
                self.assertEqual(held(), ("not_proved", ["run_held"]))
