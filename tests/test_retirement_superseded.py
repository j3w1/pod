"""R205: historical retirement of a record on an older version of a current schema family (synthetic records).

The record is the kind the 0.6.0 cutover left behind: a current context carrying a checkpoint and admissions of
the previous versions. Retirement reads it through R146's documented fields, keeps its bytes and stores its entry
beside it.
"""

import json
import os

from pod import config
from pod.installer import STATE_SCHEMAS
from pod.ledger import objective_root
from pod.retirement import SIDECAR
from tests.common import logical_json
from tests.decision_policy_support import PERSON
from tests.test_historical_retirement import RetirementCase


def _previous(schema: str) -> str:
    family, number = schema.rsplit("/v", 1)
    return f"{family}/v{int(number) - 1}"


def _following(schema: str) -> str:
    family, number = schema.rsplit("/v", 1)
    return f"{family}/v{int(number) + 1}"


NONE_RECORDED = ["none recorded"]


class SupersededCase(RetirementCase):
    def superseded(self, change=None, *, checkpoint=None, admission=None):
        """A lost coordinator's record, rewritten as an earlier Pod left it: its checkpoint and admissions on the
        previous schema versions. `change` edits the raw record further."""
        self.delivered(unknown_start=False)
        path = self.record_path()
        raw = logical_json(path)
        raw["checkpoint"]["schema"] = checkpoint or _previous(STATE_SCHEMAS["checkpoint"])
        for row in raw["admissions"].values():
            row["schema"] = admission or _previous(STATE_SCHEMAS["admission"])
            row.pop("role", None)  # An earlier record documents no role.
        if change is not None:
            change(raw)
        path.write_text(json.dumps(raw))
        return path.read_bytes()

    def record_path(self, objective="objective"):
        return objective_root(self.project, objective) / "context.json"

    def sidecar(self, objective="objective"):
        return objective_root(self.project, objective) / SIDECAR

    def refusal(self):
        verdict = self.retire()["objectives"][0]
        self.assertEqual(verdict["status"], "refused")
        self.assertFalse(self.sidecar().exists())
        return verdict["condition"]


class SupersededRetirementTests(SupersededCase):
    def test_detection_reads_documented_fields_and_retirement_keeps_the_record_bytes(self):
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
        self.assertEqual(stored["historical_retirement"]["delivery"], NONE_RECORDED)
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
        self.superseded(lambda raw: raw["checkpoint"]["worktree"].update(path=str(plain)))
        verdict = self.retire(objectives=[{"objective": "objective", "release": [str(plain)]}])["objectives"][0]
        self.assertEqual((verdict["status"], verdict["condition"]), ("refused", "path_foreign"))
        self.assertFalse(self.sidecar().exists())

    def test_an_unknown_schema_or_a_malformed_older_record_refuses(self):
        self.superseded(checkpoint=_following(STATE_SCHEMAS["checkpoint"]))
        self.assertEqual(self.refusal(), "scope_unreadable")
        path = self.record_path()
        raw = json.loads(path.read_text())
        raw["checkpoint"]["schema"] = _previous(STATE_SCHEMAS["checkpoint"])
        next(iter(raw["admissions"].values()))["state"] = "unheard-of"
        path.write_text(json.dumps(raw))
        self.assertEqual(self.refusal(), "scope_unreadable")

    def test_a_malformed_stored_entry_never_reads_retired(self):
        self.superseded()
        self.retire(self.decide(delivery=NONE_RECORDED))
        stored = json.loads(self.sidecar().read_text())
        stored["record"]["context_sha256"] = "0" * 64
        os.chmod(self.sidecar(), 0o600)
        self.sidecar().write_text(json.dumps(stored))
        shown = self.status()
        self.assertEqual((shown["status"], shown["blocker"]), ("blocked", "state_unsupported"))
        self.assertEqual(shown["readiness"]["state"], "unavailable")
        row = self.listed()["objective"]
        self.assertEqual((row["readiness"], "retirement" in row), ("unavailable", False))
        self.returning()
        self.refused(lambda: self.op("checkpoint", value=self.core()), code="objective_retired")
