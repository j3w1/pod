"""R112: the owner's historical retirement of delivered objectives whose coordinator is lost."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
import unittest
from unittest.mock import patch

from pod import cli
from pod.errors import PodError
from pod.github import repository_context
from pod.governor import _record_path
from pod.internal import run
from pod.ledger import objective_root, read
from pod.retirement import counts, validate_entry
from tests.common import GroupedTable, assert_grouped_table
from tests.test_owner_handoff import SENTINEL
from tests.test_succession import SuccessionCase, _closed, _differing_identity, _raw


class RetirementCase(SuccessionCase):
    def setUp(self):
        super().setUp()
        self.path_terminals = {}
        self.terminal_list_failure = False
        self.generations = {}
        self.unknown = None

    def native_read(self, argv, **kwargs):
        if argv[:3] == ["terminal", "list", "--worktree"]:
            self.native_calls.append(list(argv))
            if self.terminal_list_failure:
                raise PodError("orca_read_failed", SENTINEL, {"native_code": "selector_not_found"})
            rows = deepcopy(self.path_terminals.get(argv[3].removeprefix("path:"), []))
            return {"runtime": self.port.runtime, "result": {
                "terminals": rows, "totalCount": len(rows), "truncated": False,
                "hostScope": {"hostIds": ["local"], "omittedHostIds": []}}}
        response = super().native_read(argv, **kwargs)
        if argv[:2] == ["orchestration", "run-show"] and argv[3] in self.generations:
            response["result"]["run"]["consumer_generation"] = self.generations[argv[3]]
        return response

    def where(self):
        return repository_context(self.project)["worktree"]

    def delivered(self, *, settled=True, unknown_start=True):
        """Delivered history: a publication whose derived validation is pending, a start whose outcome
        is unknown, then the coordinator lost."""
        self.establish()
        started = self.start("task-2", self.packet(["S2"], boundary={"paths": ["area2"]}))["admission"] \
            if unknown_start else None
        self.op("checkpoint", value=self.core(obligations=self.stored(), worktree=self.binding()))
        published = self.op("governor-execute", action=self.action(effects=["workflow:ci.yml"]))
        self.publication = published["record_id"]
        if settled:
            self.settle(self.admission)
        if started is not None:
            del self.port.workers[started["native_binding"]["dispatchId"]]  # Orca never shows its Dispatch.
            path, raw = _raw(self, "objective")
            raw["admissions"][started["admission_id"]].update(state="unresolved", native_binding=None)
            path.write_text(json.dumps(raw))
            self.unknown = started["admission_id"]
        self.lose()

    def claim_only(self, name="reconcile"):
        """A successor that recorded the same worktree, never claimed, then lost too."""
        self.successor(name=name, caller="next", run_id="run-s")
        self.run_holders["run-s"] = "next"
        self.terminals["next"] = "stale"
        os.environ["ORCA_TERMINAL_HANDLE"] = "administrator"

    def retire(self, decision=None, *, objectives=None):
        request = {"project": str(self.project),
                   "objectives": [{"objective": "objective", "release": [self.where()]}]
                   if objectives is None else objectives}
        if decision is not None:
            request["decision"] = decision
        return run("historical-retirement", request)

    def decide(self, *, objectives=None, delivery=("PR #99 merged as 0548dc6", "issue #98"), **changes):
        found = self.retire(objectives=objectives)
        blocks = [{**row["facts"], "delivery": list(delivery)} for row in found["objectives"]]
        return {"provenance": "user_direct", "instruction": "Retire the delivered objective and release its worktree",
                "repository": found["repository"], "reason": "delivered; coordinator permanently gone",
                "objectives": blocks, **changes}

    def journal(self, objective="objective"):
        return _record_path(self.project, objective).read_bytes()

    def text_status(self, objective="objective"):
        out = StringIO()
        old = os.getcwd()
        try:
            os.chdir(self.project)
            with redirect_stdout(out):
                cli.main(["status", "--objective", objective])
        finally:
            os.chdir(old)
        return out.getvalue()

    def listed(self, *, text=False):
        out = StringIO()
        old = os.getcwd()
        try:
            os.chdir(self.project)
            with redirect_stdout(out):
                cli.main(["status", "--list"] + ([] if text else ["--json"]))
        finally:
            os.chdir(old)
        return out.getvalue() if text else {row["objective"]: row for row in json.loads(out.getvalue())["objectives"]}


class DetectionTests(RetirementCase):
    def test_eligible_facts_preserve_unknown_and_pending_records_and_write_nothing(self):
        self.delivered()
        before, starts = self.snapshot(), deepcopy(self.port.starts)
        found = self.retire()
        self.assertEqual(found["status"], "retirement_available")
        verdict = found["objectives"][0]
        self.assertEqual((verdict["status"], verdict["condition"]), ("eligible", "retirable"))
        facts = verdict["facts"]
        self.assertEqual((facts["objective"], facts["owner"], facts["runs"], facts["release"]),
                         ("objective", "owner", [{"run": "run", "generation": 1}], [self.where()]))
        governor = [row for row in facts["records"] if row["kind"] == "governor"]
        self.assertEqual([(row["action"], row["outcome"]) for row in governor], [("workflow_dispatch", "pending")])
        admissions = {row["id"]: (row["state"], row["dispatch"]) for row in facts["records"] if row["kind"] == "admission"}
        self.assertEqual(admissions, {self.admission["admission_id"]: ("bound", "dispatch-1"),
                                      self.unknown: ("unresolved", None)})
        self.assertEqual(verdict["unfinished"], ["O1", "S1", "S2"])
        self.assertEqual(verdict["native"]["owner"], "terminal_handle_stale")
        self.assertIn("objective", found["question"])
        self.assertIn(self.where(), found["question"])
        for identity in ("owner", "run", "next"):  # The plain question names objectives and paths only.
            self.assertNotIn(f" {identity} ", f" {found['question']} ")
        self.assertEqual(before, self.snapshot())
        self.assertEqual((self.mutations, self.port.starts), ([], starts))
        self.assertNotIn(SENTINEL, json.dumps(found))

    def test_an_exited_terminal_in_the_path_and_an_exited_coordinator_do_not_hold(self):
        self.delivered()
        self.terminals["owner"] = "exited"
        self.path_terminals[self.where()] = [{"handle": "old", "connected": False, "writable": False,
                                              "orphaned": False, "exitCause": {"kind": "operator_close"}}]
        verdict = self.retire()["objectives"][0]
        self.assertEqual(verdict["status"], "eligible", verdict.get("message"))
        self.assertEqual((verdict["native"]["owner"], verdict["native"]["exited_terminals"]), ("exited", 1))

    def test_requests_and_decisions_are_exact(self):
        self.delivered()
        where = self.where()
        for objectives in ([], [{"objective": "objective", "release": ["relative/path"]}],
                           [{"objective": "objective", "release": [where + "/../x"]}],
                           [{"objective": "objective", "release": [where, where]}],
                           [{"objective": "objective", "release": [where]}] * 2,
                           [{"objective": "objective"}]):
            with self.subTest(objectives=objectives):
                with self.assertRaises(PodError) as caught:
                    self.retire(objectives=objectives)
                self.assertIn(caught.exception.code, ("invalid_request", "invalid_retirement_objective"))
        decision = self.decide()
        before = self.snapshot()
        for changes in ({"provenance": "coordinator"}, {"repository": "another"},
                        {"objectives": [{**decision["objectives"][0], "release": ["/elsewhere"]}]},
                        {"objectives": [{**decision["objectives"][0], "delivery": []}]},
                        {"objectives": []}):
            with self.subTest(changes=changes), self.assertRaises(PodError) as caught:
                self.retire({**decision, **changes})
            self.assertEqual(caught.exception.code, "retirement_decision_mismatch")
        self.assertEqual(before, self.snapshot())

    def test_an_unknown_objective_or_an_unrecorded_path_refuses_alone(self):
        self.delivered()
        before = self.snapshot()
        found = self.retire(objectives=[{"objective": "missing", "release": [self.where()]},
                                        {"objective": "objective", "release": [self.where() + "-elsewhere"]}])
        self.assertEqual([row["condition"] for row in found["objectives"]], ["objective_unknown", "path_not_recorded"])
        self.assertFalse(objective_root(self.project, "missing").exists())
        self.assertEqual((before, self.native_calls), (self.snapshot(), []))  # Refused before any Orca read.


def _continued(case):
    path, raw = _raw(case, "objective")
    raw["continued_by"] = {"objective": "successor", "record": "r", "owner": "next", "runs": ["run"],
                           "at": "2026-10-14T00:00:00+00:00", "instruction": "continue", "unfinished": [],
                           "unreported": [], "ambiguity": [], "orca_claim": "unavailable"}
    path.write_text(json.dumps(raw))


def _journal_unreadable(case):
    _record_path(case.project, "objective").write_text("{")


def _record_unsupported(case):
    path, raw = _raw(case, "objective")
    raw["unexpected"] = True
    path.write_text(json.dumps(raw))


def _incomplete(case):
    from pod import orca
    original = orca.worker_rows
    rows = patch("pod.orca.worker_rows", side_effect=lambda run: {**original(run), "complete": False})
    rows.start(); case.addCleanup(rows.stop)


def _live_terminal(case):
    case.path_terminals[case.where()] = [{"handle": "user", "connected": True, "writable": True, "orphaned": False}]


def _detached_terminal(case):
    case.path_terminals[case.where()] = [{"handle": "user", "connected": False, "writable": False, "orphaned": False}]


def _handoff_pending(case):
    pending = patch("pod.handoff.unfinished", return_value={"state": "pending"})
    pending.start(); case.addCleanup(pending.stop)


def _show_failure(case):
    case.port.show_failure = PodError("orca_unavailable", SENTINEL)


# kind: (arrange after the owner decided, expected condition, delivered(settled=...))
REFUSALS = {
    "closed": (_closed, "objective_closed", True),
    "continued": (_continued, "objective_continued", True),
    "owner-live": (lambda case: case.terminals.update(owner="live"), "owner_not_lost", True),
    "owner-unreadable": (lambda case: case.terminals.update(owner="unreadable"), "owner_not_lost", True),
    "owner-stop-unconfirmed": (lambda case: case.terminals.update(owner="stop-unconfirmed"), "owner_not_lost", True),
    "run-rebound": (lambda case: case.run_holders.update(run="third"), "generation_changed", True),
    "generation-changed": (lambda case: case.generations.update(run=2), "generation_changed", True),
    "handoff-pending": (_handoff_pending, "handoff_pending", True),
    "workers-unreadable": (lambda case: setattr(case, "worker_list_failure", True), "workers_unverified", True),
    "workers-incomplete": (_incomplete, "workers_unverified", True),
    "dispatch-unreadable": (_show_failure, "workers_unverified", True),
    "dispatch-differs": (_differing_identity, "workers_unverified", True),
    "dispatch-live": (None, "work_live", False),
    "terminal-live": (_live_terminal, "path_in_use", True),
    "terminal-detached": (_detached_terminal, "path_in_use", True),
    "terminals-unreadable": (lambda case: setattr(case, "terminal_list_failure", True), "path_unverified", True),
    "journal-unreadable": (_journal_unreadable, "scope_unreadable", True),
    "record-unsupported": (_record_unsupported, "scope_unreadable", True),
}


class RefusalTableTests(GroupedTable):
    """Each unproven fact refuses detection and the owner's earlier decision, and writes nothing."""
    GROUPS = {"test_lifecycle_refuses": ["closed", "continued"],
              "test_unreadable_records_refuse": ["handoff-pending", "record-unsupported", "journal-unreadable"],
              "test_coordinator_refuses": ["owner-live", "owner-unreadable", "owner-stop-unconfirmed"],
              "test_run_binding_refuses": ["run-rebound", "generation-changed"],
              "test_workers_refuse": ["workers-unreadable", "workers-incomplete", "dispatch-unreadable"],
              "test_dispatches_refuse": ["dispatch-differs", "dispatch-live"],
              "test_paths_refuse": ["terminal-live", "terminal-detached", "terminals-unreadable"]}

    def row(self, kind):
        arrange, condition, settled = REFUSALS[kind]
        case = RetirementCase(); case.setUp()
        try:
            with self.subTest(kind=kind):
                case.delivered(settled=settled)
                decision = case.decide() if settled else None
                if arrange is not None:
                    arrange(case)
                before, starts = case.snapshot(), len(case.port.starts)
                found = case.retire()
                verdict = found["objectives"][0]
                self.assertEqual((found["status"], verdict["status"], verdict["condition"]),
                                 ("retirement_unavailable", "refused", condition), verdict.get("message"))
                self.assertNotIn("question", found)
                if decision is not None:
                    applied = case.retire(decision)
                    self.assertEqual((applied["status"], applied["objectives"][0]["condition"]), ("refused", condition))
                self.assertEqual(before, case.snapshot())
                self.assertEqual((case.mutations, len(case.port.starts)), ([], starts))
                self.assertNotIn(SENTINEL, json.dumps(found))
        finally:
            case.doCleanups()

    def test_lifecycle_refuses(self):
        self.rows()

    def test_unreadable_records_refuse(self):
        self.rows()

    def test_coordinator_refuses(self):
        self.rows()

    def test_run_binding_refuses(self):
        self.rows()

    def test_workers_refuse(self):
        self.rows()

    def test_dispatches_refuse(self):
        self.rows()

    def test_paths_refuse(self):
        self.rows()


class RefusalTableShapeTests(unittest.TestCase):
    def test_the_refusal_table_runs_every_row_once(self):
        assert_grouped_table(self, RefusalTableTests, list(REFUSALS), limit=3)


class WriteTests(RetirementCase):
    def test_the_entry_is_appended_and_nothing_else_changes(self):
        self.delivered()
        self.claim_only()
        decision = self.decide()
        before, state, journal = self.snapshot(), deepcopy(self.state()), self.journal()
        starts = deepcopy(self.port.starts)
        own = str((objective_root(self.project, "objective") / "context.json").relative_to(self.root / "state"))
        result = self.retire(decision)
        self.assertEqual(result["status"], "retired")
        after = self.state()
        entry = after.pop("historical_retirement")
        state["revision"] += 1
        self.assertEqual(after, state)  # Owner, admissions, obligations, outcomes and evidence are as recorded.
        self.assertEqual(self.journal(), journal)
        self.assertEqual({key: value for key, value in self.snapshot().items() if key != own},
                         {key: value for key, value in before.items() if key != own})
        self.assertEqual((entry["owner"], entry["released"], entry["objectives"], entry["delivery"]),
                         ("owner", [self.where()], ["objective"], ["PR #99 merged as 0548dc6", "issue #98"]))
        self.assertEqual(entry["preserved"], decision["objectives"][0]["records"])
        self.assertEqual(entry["unfinished"], ["O1", "S1", "S2"])
        self.assertEqual((self.mutations, self.port.starts), ([], starts))  # No session, worker or Orca change.

    def test_replay_is_idempotent_and_changed_facts_refuse(self):
        self.delivered()
        decision = self.decide()
        before = self.snapshot()
        for changes in ({"owner": "someone"}, {"runs": [{"run": "run", "generation": 2}]}, {"records": []}):
            with self.subTest(changes=changes):
                changed = {**decision, "objectives": [{**decision["objectives"][0], **changes}]}
                verdict = self.retire(changed)["objectives"][0]
                self.assertEqual((verdict["status"], verdict["condition"]), ("refused", "retirement_decision_mismatch"))
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.retire(decision)["status"], "retired")
        released = self.snapshot()
        replayed = self.retire(decision)
        self.assertEqual((replayed["status"], replayed["objectives"][0]["status"]), ("retired", "replayed"))
        self.assertEqual(released, self.snapshot())
        other = self.retire({**decision, "reason": "another reason"})["objectives"][0]
        self.assertEqual((other["status"], other["condition"]), ("refused", "objective_retired"))
        self.assertEqual(released, self.snapshot())

    def test_a_competing_writer_that_changes_the_facts_is_detected_under_the_lock(self):
        self.delivered()
        decision = self.decide()
        path = _record_path(self.project, "objective")
        journal = json.loads(path.read_text())
        journal["actions"][-1]["outcome"] = "UNKNOWN"  # Another writer recorded the pending validation.
        path.write_text(json.dumps(journal))
        before = self.snapshot()
        verdict = self.retire(decision)["objectives"][0]
        self.assertEqual(verdict["condition"], "retirement_decision_mismatch")
        self.assertEqual(before, self.snapshot())

    def test_a_multi_objective_decision_has_independent_results(self):
        self.delivered()
        self.claim_only()
        both = [{"objective": "objective", "release": [self.where()]},
                {"objective": "reconcile", "release": [self.where()]}]
        decision = self.decide(objectives=both)
        self.terminals["next"] = "live"  # The successor's coordinator came back: it alone refuses.
        result = self.retire(decision, objectives=both)
        rows = {row["objective"]: row for row in result["objectives"]}
        self.assertEqual(result["status"], "partially_retired")
        self.assertEqual((rows["objective"]["status"], rows["reconcile"]["condition"]), ("retired", "owner_not_lost"))
        self.assertNotIn("historical_retirement", read(self.project, "reconcile"))
        self.terminals["next"] = "stale"
        later = self.decide(objectives=both[1:])
        self.assertEqual(self.retire(later, objectives=both[1:])["status"], "retired")
        replayed = {row["objective"]: row for row in self.retire(decision, objectives=both)["objectives"]}
        self.assertEqual((replayed["objective"]["status"], replayed["reconcile"]["condition"]),
                         ("replayed", "objective_retired"))
        self.assertEqual(read(self.project, "objective")["historical_retirement"]["objectives"],
                         ["objective", "reconcile"])


class AfterRetirementTests(RetirementCase):
    def released(self):
        self.delivered()
        self.retire(self.decide())

    def test_every_write_of_the_returning_coordinator_is_refused_by_name(self):
        self.released()
        self.returning()
        before, starts = self.snapshot(), deepcopy(self.port.starts)
        calls = {
            "checkpoint": lambda: self.op("checkpoint", value=self.core()),
            "governor-prepare": lambda: self.op("governor-prepare", unit="next"),
            "governor-outcome": lambda: self.op("governor-outcome", record_id=self.publication, outcome="PASS"),
            "admission": lambda: self.start("task-3", self.packet(["S2"], boundary={"paths": ["area2"]})),
            "report": lambda: self.report(self.admission, self.frozen),
            "runtime-continuity": lambda: self.op("runtime-continuity", decision={
                "provenance": "user_direct", "instruction": "rebind", "objective": "objective",
                "recorded_runtime": "runtime", "current_runtime": "runtime-new", "ambiguity": []}),
        }
        for name, call in calls.items():
            with self.subTest(path=name):
                self.refused(call, code="objective_retired")
        self.assertEqual(before, self.snapshot())
        self.assertEqual((self.mutations, self.port.starts), ([], starts))

    def test_handoff_and_a_successor_claim_refuse_a_retired_objective(self):
        self.released()
        self.terminals["next"] = "live"
        os.environ["ORCA_TERMINAL_HANDLE"] = "next"
        self.current = {"id": "run", "coordinator_handle": "owner", "consumer_generation": 1}
        handoff = self.status()["owner_handoff"]
        self.assertEqual(handoff["condition"], "objective_retired")
        before = self.snapshot()
        self.refused(lambda: self.op("owner-handoff", owner="next", decision={
            "provenance": "user_direct", "instruction": "Make this terminal the coordinator", **handoff["facts"]}),
            code="objective_retired")
        self.assertEqual((before, self.mutations), (self.snapshot(), []))
        self.successor()
        self.assertEqual(self.claim()["condition"], "predecessor_retired")

    def test_status_and_list_show_retired_never_closed_qualified_or_continued(self):
        self.released()
        shown = self.status()
        self.assertEqual((shown["phase"], shown["blocker"]), ("retired", "objective_retired"))
        block = shown["retirement"]
        self.assertEqual((block["completed"], block["released"]), (False, [self.where()]))
        self.assertEqual(block["counts"], {"pending": 1, "unknown": 1, "failed": 0, "unreported": 1})
        self.assertEqual((block["unfinished"], block["delivery"]),
                         (["O1", "S1", "S2"], ["PR #99 merged as 0548dc6", "issue #98"]))
        self.assertNotIn("succession", shown)
        self.assertIsNone(self.state()["checkpoint"].get("closure"))
        settled = {row["admission"]: row["settlement"] for row in shown["assignments"]["settled"]}
        self.assertEqual(settled, {self.admission["admission_id"]: "retirement"})
        self.assertEqual([(row["admission"], row["outcome"]) for row in shown["assignments"]["preserved"]],
                         [(self.unknown, "unknown")])
        text = self.text_status()
        self.assertIn("not completed or closed", text)
        self.assertIn("Delivery reference: issue #98", text)
        self.assertNotIn("objective closed", text)
        self.assertNotIn("QUALIFIED", text)
        row = self.listed()["objective"]
        self.assertEqual((row["phase"], row["retirement"]), ("retired", block))
        self.assertNotIn("continued_by", row)
        phase = "retired"
        self.assertIn(f"| {phase} |", self.listed(text=True))


class RecordShapeTests(unittest.TestCase):
    ENTRY = {"at": "2026-10-14T00:00:00+00:00", "decision": "d" * 64, "instruction": "retire",
             "reason": "delivered", "repository": "r" * 64, "objectives": ["objective"], "owner": "owner",
             "runs": [{"run": "run", "generation": 1}], "released": ["/w/one"],
             "preserved": [{"kind": "governor", "id": "g", "action": "workflow_dispatch", "candidate": "c",
                            "outcome": "FAILED"},
                           {"kind": "governor", "id": "h", "action": "workflow_dispatch", "candidate": "c",
                            "outcome": "UNKNOWN"},
                           {"kind": "admission", "id": "a", "role": "review", "state": "reserved", "dispatch": None}],
             "unfinished": ["pod-9"], "delivery": ["PR #99"],
             "native": {"runtime": "rt", "owner": "terminal_handle_stale", "runs": 1, "dispatches": 0,
                        "exited_terminals": 0}}

    def test_counts_name_failed_and_unknown_records_apart(self):
        validate_entry(self.ENTRY)
        self.assertEqual(counts(self.ENTRY["preserved"]), {"pending": 0, "unknown": 2, "failed": 1, "unreported": 0})

    def test_a_malformed_entry_makes_the_record_unsupported(self):
        for changes in ({"released": []}, {"released": ["relative"]}, {"delivery": []},
                        {"preserved": [{"kind": "governor", "id": "g", "action": "x", "candidate": "c", "outcome": "PASS"}]},
                        {"native": {}}, {"extra": 1}):
            with self.subTest(changes=changes), self.assertRaises(PodError) as caught:
                validate_entry({**self.ENTRY, **changes})
            self.assertEqual(caught.exception.code, "state_unsupported")
