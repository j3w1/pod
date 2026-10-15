"""Honest titles and the finished-tab table at real production entry points.

Only native/GitHub ports are injected. All values are invented synthetic controls;
no captured worker identity or terminal output is included.
"""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import subprocess
import types
from unittest.mock import patch

from pod import internal
from pod.errors import PodError
from pod.ledger import objective_root, read
from pod.operations import OrcaPort
from pod.orca import mutate_command
from pod.status import status, render
from tests.issue41_support import ProductionCase
from tests.kernel_support import REQUEST, assurance_review, assurance_satisfied, assurance_waiting


class TabCase(ProductionCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.tab_case = "owned"
        self.tab_cases = {}
        self.live = "unverifiable"
        self.release_argv = ["orca", "orchestration", "worker-release", "--dispatch", "dispatch-1", "--json"]
        self.original_show = self.port.show_worker
        self.port.show_worker = self.shown
        replacement = patch.object(OrcaPort, "show_worker", side_effect=self.shown)
        replacement.start(); self.addCleanup(replacement.stop)

    def native_read(self, argv, **kwargs):
        self.calls.append(("read", list(argv)))
        return super().native_read(argv, **kwargs)

    def shown(self, dispatch):
        self.calls.append(("show", dispatch))
        value = self.original_show(dispatch)
        result = value["result"]
        case = self.tab_cases.get(dispatch, self.tab_case)
        if case == "not_materialized":
            return value
        worker = result["worker"]
        handle, worktree = worker["agentTerminalHandle"], worker["worktreeId"]
        ownership = "user_owned" if case == "user_owned" else "external" if case == "external" else "owned"
        release = ("retained" if case in ("retained", "user_owned", "external") else
                   case if case in ("release_pending", "release_unknown") else "none")
        resource = {"terminalHandle": handle, "worktreeId": worktree, "ownerDispatchId": dispatch,
                    "originDispatchId": dispatch, "ownershipState": ownership, "releaseState": release,
                    "retainedReason": "user_takeover" if case == "user_owned" else "coordinator_request" if case == "retained" else None}
        terminal = {"handle": handle, "tabId": "synthetic-tab", "worktreeId": worktree,
                    "connected": case != "disconnected", "writable": True, "orphaned": False}
        result.update(terminalResource=resource, terminal=terminal)
        projection = result["projection"]
        projection.update(role="worker", resource={"state": ownership, "releaseState": release,
            "ownerDispatchId": dispatch, "terminalState": release},
            liveness={"verdict": self.live, "reason": "synthetic_observation"},
            nextAction={"kind": "release", "argv": list(self.release_argv)})
        if case == "no_command":
            projection["nextAction"] = {"kind": "none", "argv": []}
        if case == "unreadable":
            terminal.pop("connected")
        if case == "reused_later":
            resource["ownerDispatchId"] = "dispatch-later"
        if case in ("coordinator", "setup"):
            projection["role"] = case
        if case == "other_objective":
            terminal["worktreeId"] = "other-workspace"
        return value

    def packet(self, serves, **fields):
        frozen = super().packet(serves, **fields)
        return internal.run("packet", frozen["body"])

    def ack(self, operation, request, result):
        before, calls = self.saved(), deepcopy(self.calls)
        output = internal.acknowledge(operation, {"project": str(self.project), "objective": "objective", **request}, result)
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.calls, calls, "acknowledgement added a native read")
        self.assertNotIn("took over", json.dumps(output))
        return output

    def saved(self):
        root = objective_root(self.project, "objective")
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
                if path.is_file() and path.name != ".lock"}

    def restore(self, snapshot):
        root = objective_root(self.project, "objective")
        for name, data in snapshot.items():
            (root / name).write_bytes(data)

    def worker(self):
        self.intake(self.sub("S"))
        frozen = self.packet(["S"], role="investigate", resolves="one question", stop_condition="one answer")
        admission = self.start("worker", frozen)["admission"]
        self.settle(admission)
        return frozen, admission

    def waiting_rows(self):
        rows = self.stored()
        for row in rows:
            if row.get("executor") != "coordinator":
                row.pop("executor", None)
                row.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        return rows

    def finished_rows(self):
        rows = self.stored()
        for row in rows:
            row.pop("executor", None); row.pop("wait", None)
            if row["kind"] != "assurance":
                row.update(state="satisfied", evidence=[self.proof(row["id"])])
        return rows

    def keep(self, admission, keep=True, identity="keep", **bindings):
        binding = admission["native_binding"]
        return self.op("constraint", action="add", value={"id": identity, "kind": "keep_worker_tab",
            "provenance": "user_direct", "instruction": "Keep this worker tab" if keep else "Let this worker tab go",
            "value": {"objective": "objective", "dispatch": binding["dispatchId"],
                      "terminal": binding["terminalHandle"], "keep": keep, **bindings}})

    def status(self, *, by_run=False):
        before = self.saved()
        result = status(self.project, "run" if by_run else None, objective=None if by_run else "objective")
        self.assertEqual(before, self.saved())
        return result


class TitleTests(TabCase):
    def title_admission(self, role, expected, **fields):
        if role == "review":
            self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                "candidate": self.base, "existing_evidence": "tests", "insufficiency": "no review",
                "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}})
            serves = ["A"]
        else:
            self.intake(self.sub("S")); serves = ["S"]
        if role == "investigate":
            fields.update(resolves="one question", stop_condition="one answer")
        frozen = self.packet(serves, role=role, **fields)
        self.assertNotIn("title", frozen["body"])
        self.assertEqual(frozen["title"], expected)
        result = self.start("worker", frozen)
        acknowledged = self.ack("admission", {}, result)
        handle = result["admission"]["native_binding"]["terminalHandle"]
        self.assertEqual(acknowledged["title"], frozen["title"])
        self.assertEqual(acknowledged["rename_command"],
            ["orca", "terminal", "rename", "--terminal", handle, "--title", expected, "--json"] if handle else None)
        return frozen, result["admission"]

    def test_implementer_omits_identifier_path_credential_status_and_route_shapes(self):
        self.title_admission("implement", "Implementer: Repair parser", responsibility=
            "Repair parser term_fixture /private/fixture run_fixture task_fixture ctx_fixture admission_fixture "
            "packet_fixture sk-synthetic_credential123 model=gpt-example effort=high completed authorized")

    def test_investigator_and_optional_purpose(self):
        self.title_admission("investigate", "Investigator: Find parser cause", purpose="Find parser cause")

    def test_reviewer_and_finer_role(self):
        self.title_admission("review", "Challenger: Check parser", role_label="Challenger", purpose="Check parser")

    def test_headless_start_and_empty_safe_title_have_reason_without_command(self):
        self.port.headless = True
        self.tab_case = "not_materialized"
        frozen, admission = self.title_admission("implement", "Implementer: Assigned work", responsibility="/private/fixture ctx_fixture")
        self.assertIsNone(admission["native_binding"]["terminalHandle"])
        for role in ("implement", "investigate", "review"):
            body = {**frozen["body"], "role": role, "purpose": "word " * 30}
            if role == "investigate": body.update(resolves="question", stop_condition="answer")
            title = internal.run("packet", body)["title"]
            self.assertTrue(title); self.assertLessEqual(len(title), 96)

    def test_reuse_gets_a_new_title_and_exact_same_handle(self):
        frozen, admission = self.title_admission("investigate", "Investigator: writer")
        self.settle(admission)
        self.keep(admission)
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        fresh = self.packet(["S"], role="investigate", resolves="new question", stop_condition="answer", purpose="Trace tokenizer")
        result = self.start("follow-up", fresh, reuse_of=admission["admission_id"])
        self.assertEqual(result["title"], "Investigator: Trace tokenizer")
        self.assertEqual(result["rename_command"][4], admission["native_binding"]["terminalHandle"])
        self.assertNotEqual(result["title"], frozen["title"])
        self.tab_cases[admission["native_binding"]["dispatchId"]] = "reused_later"
        self.settle(result["admission"])
        self.report(result["admission"], fresh, map={"obligations": self.waiting_rows()})
        tabs = self.status()["worker_tabs"]
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["dispatch"], result["admission"]["native_binding"]["dispatchId"])
        self.assertFalse(tabs[0]["owner_kept"])

    def test_adapter_refuses_terminal_rename(self):
        with self.assertRaises(PodError) as caught:
            mutate_command(["terminal", "rename", "--terminal", "synthetic-terminal", "--title", "Writer: parser", "--json"])
        self.assertEqual(caught.exception.code, "unsupported_orca_mutation")
        self.assertEqual(self.calls, [])

    def test_deferred_start_returns_title_without_rename(self):
        self.intake(self.sub("S"))
        self.port.receipt = {"runtime": "runtime", "exit": 1, "request_uuid": REQUEST,
                             "error": {"code": "task_not_startable", "message": "refused"}}
        result = self.start("worker", self.packet(["S"], purpose="Repair parser"))
        self.assertEqual(result["title"], "Implementer: Repair parser")
        self.assertIsNone(result["rename_command"])
        self.assertEqual(result["rename_reason"], "worker not started")

    def test_optional_title_fields_obey_their_bounds(self):
        self.intake(self.sub("S"))
        for name, limit in (("role_label", 48), ("purpose", 160)):
            self.packet(["S"], **{name: "a" * limit})
            with self.assertRaises(PodError): self.packet(["S"], **{name: "a" * (limit + 1)})


class TableTests(TabCase):
    def table(self, cases):
        frozen, admission = self.worker()
        snapshot = self.saved()
        for case, expected, listed, blocked in cases:
            with self.subTest(case=case):
                self.restore(snapshot); self.tab_case = case
                before = len([c for c in self.calls if c[0] == "show"])
                raw = self.report(admission, frozen, map={"obligations": self.waiting_rows()})
                ack = self.ack("report", {"admission_id": admission["admission_id"]}, raw)
                self.assertEqual(len([c for c in self.calls if c[0] == "show"]) - before, 2)
                self.assertEqual(ack["worker_tab"]["row"], expected)
                self.assertEqual(ack["worker_tab"]["blocks_closure"], blocked)
                views = [self.status(), self.status(by_run=True)]
                for view in views:
                    self.assertEqual(bool(view["worker_tabs"]), listed)
                    if listed:
                        row = view["worker_tabs"][0]
                        self.assertEqual(row["row"], expected)
                        self.assertEqual(row["command"], ack["worker_tab"]["command"])
                        text = view["readiness"]["summary"] + view["readiness"]["finalization"]["next_action"]
                        self.assertIn(row["wording"], text)
                        self.assertNotIn("safe to retire", text); self.assertNotIn("Pod needs nothing more", text)
                if listed:
                    output = StringIO()
                    with redirect_stdout(output): render(views[0])
                    self.assertEqual(len([line for line in output.getvalue().splitlines() if line.startswith("Worker tab:")]), 1)
                self.assertNotIn("took over", json.dumps(views))

    def test_owned_retained_and_missing_release_command(self):
        self.table((("owned", "owned", True, True), ("retained", "retained_owned", True, True),
                    ("no_command", "owned", True, True)))

    def test_user_owned_external_pending_unknown_and_unreadable(self):
        self.table((("user_owned", "user_owned", True, False), ("external", "external", True, False),
                    ("release_pending", "release_pending", True, True),
                    ("release_unknown", "release_pending", True, True), ("unreadable", "unreadable", True, True)))

    def test_excluded_and_absent_rows(self):
        self.table((("not_materialized", "not_materialized", False, False), ("disconnected", "disconnected", False, False),
                    ("reused_later", "reused_later", False, False), ("coordinator", "excluded", False, False),
                    ("setup", "excluded", False, False), ("other_objective", "excluded", False, False)))

    def test_connected_tabs_ignore_stale_liveness_and_preserve_retained_terminals(self):
        frozen, admission = self.worker()
        self.tab_case = "retained"
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        source = subprocess.run(["git", "show", "29a57a5:skills/pod/status.py"], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, check=True).stdout
        earlier = types.ModuleType("pod._status_0150"); earlier.__package__ = "pod"
        exec(compile(source, "status-0.15.0.py", "exec"), earlier.__dict__)
        for verdict in ("live", "stale", "unverifiable"):
            with self.subTest(verdict=verdict):
                self.live = verdict
                first = len(self.calls)
                old = earlier.status(self.project, None, objective="objective")
                prior_calls = self.calls[first:]
                first = len(self.calls)
                current = self.status()
                self.assertEqual(self.calls[first:], prior_calls, "new status added a native read")
                self.assertEqual(json.dumps(current["retained_terminals"]), json.dumps(old["retained_terminals"]))
                self.assertEqual(current["worker_tabs"][0]["state"], "releasable")


class ClosureTests(TabCase):
    def closing(self, cases, *, report_route):
        frozen, admission = self.worker()
        if not report_route:
            self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        snapshot = self.saved()
        for case, blocked in cases:
            with self.subTest(case=case, report_route=report_route):
                self.restore(snapshot); self.tab_case = case
                rows = self.finished_rows(); before = self.saved()
                try:
                    if report_route:
                        raw = self.report(admission, frozen, map={"obligations": rows, "close": True})
                        ack = self.ack("report", {"admission_id": admission["admission_id"]}, raw)
                    else:
                        raw = self.write(rows, close=True)
                        ack = self.ack("checkpoint", {}, raw)
                except PodError as error:
                    self.assertTrue(blocked, str(error))
                    self.assertEqual(error.code, "worker_terminal_unverified" if case == "unreadable" else "worker_tabs_open")
                    self.assertIn("worker-show" if case == "unreadable" else "worker-release", str(error))
                    self.assertEqual(before, self.saved(), "refusal changed context, map or report bytes")
                else:
                    self.assertFalse(blocked)
                    self.assertIsNotNone(read(self.project, "objective")["checkpoint"]["closure"])
                    if ack["worker_tabs"]:
                        self.assertNotIn("safe to retire", json.dumps(ack))
                        self.assertNotIn("Pod needs nothing more", json.dumps(ack))
                        self.assertEqual(ack["worker_tabs"][0]["command"][:3], ["orca", "terminal", "close"])

    def test_checkpoint_refuses_owned_retained_and_pending(self):
        self.closing((("owned", True), ("retained", True), ("release_pending", True),
                      ("release_unknown", True), ("unreadable", True)), report_route=False)

    def test_report_refuses_own_owned_retained_pending_and_unreadable_tab(self):
        self.closing((("owned", True), ("retained", True), ("release_pending", True),
                      ("release_unknown", True), ("unreadable", True)), report_route=True)

    def test_checkpoint_accepts_kept_absent_and_excluded(self):
        self.closing(tuple((case, False) for case in ("user_owned", "external", "not_materialized", "disconnected",
                     "reused_later", "coordinator", "setup", "other_objective")), report_route=False)

    def test_report_accepts_own_kept_absent_and_excluded(self):
        self.closing(tuple((case, False) for case in ("user_owned", "external", "not_materialized", "disconnected",
                     "reused_later", "coordinator", "setup", "other_objective")), report_route=True)

    def test_route_refresh_and_mismatch_cannot_write_before_tab_refusal(self):
        from pod.ledger import update_admission
        frozen, admission = self.worker()
        for mismatch in (False, True):
            def change(row):
                row["route_decision"]["effective_unknown"] = not mismatch
                row["route_decision"]["effective"] = {"agent": "codex", "model": "other" if mismatch else "unknown",
                                                      "effort": "unknown", "context": "unknown"}
                row["effective_evidence"]["effective"] = deepcopy(row["route_decision"]["effective"])
            update_admission(self.project, "objective", owner="owner", admission_id=admission["admission_id"], update=change)
            before = self.saved()
            with self.assertRaises(PodError) as caught:
                self.report(admission, frozen, map={"obligations": self.finished_rows(), "close": True})
            self.assertEqual(caught.exception.code, "worker_tabs_open")
            self.assertEqual(before, self.saved())

    def test_native_read_failure_names_the_read_without_writing(self):
        frozen, admission = self.worker()
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        closing = self.finished_rows()
        before = self.saved()
        self.port.show_failure = PodError("orca_unavailable", "synthetic worker-show failure")
        for report_route in (False, True):
            with self.subTest(report_route=report_route), self.assertRaises(PodError) as caught:
                if report_route: self.report(admission, frozen, map={"obligations": closing, "close": True})
                else: self.write(closing, close=True)
            self.assertEqual(caught.exception.code, "orca_unavailable")
            self.assertIn("worker-show", caught.exception.detail["next_action"])
            self.assertEqual(before, self.saved())
        projected = self.status()
        self.assertEqual(projected["worker_tabs"][0]["state"], "unknown")
        self.assertIn("orca_unavailable", projected["readiness"]["summary"])

    def test_active_report_never_relays_close(self):
        frozen, admission = self.worker()
        self.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = "in_progress"
        self.tab_case = "user_owned"
        before = self.saved()
        with self.assertRaises(PodError) as caught:
            self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        self.assertEqual(caught.exception.code, "report_attempt_unverified")
        self.assertNotIn("terminal close", str(caught.exception))
        self.assertEqual(self.status()["worker_tabs"], [])
        self.assertEqual(before, self.saved())

    def test_existing_closure_keeps_its_bytes_but_names_every_open_tab(self):
        frozen, admission = self.worker()
        self.tab_case = "not_materialized"
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        self.write(self.finished_rows(), close=True)
        before = self.saved()
        report_bytes = json.dumps(read(self.project, "objective")["checkpoint"]["closure"])
        for case in ("owned", "retained", "user_owned", "external", "release_pending", "release_unknown", "unreadable"):
            with self.subTest(case=case):
                self.tab_case = case
                view = self.status(); ready = view["readiness"]
                self.assertEqual(ready["state"], "closed"); self.assertFalse(ready["coordinator_needed"])
                self.assertTrue(ready["finalization"]["finished"])
                self.assertEqual(ready["finalization"]["remaining"], [])
                self.assertEqual(ready["finalization"]["records"], {})
                self.assertIn(view["worker_tabs"][0]["wording"], ready["summary"])
                self.assertNotIn("safe to retire", json.dumps(ready))
                self.assertNotIn("Pod needs nothing more", json.dumps(ready))
                self.assertEqual(json.dumps(read(self.project, "objective")["checkpoint"]["closure"]), report_bytes)
                self.assertEqual(before, self.saved())

    def test_runtime_change_cannot_rebind_before_a_closing_refusal(self):
        frozen, admission = self.worker()
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        closing = self.finished_rows(); before = self.saved()
        self.port.runtime = "changed-runtime"
        for report_route in (False, True):
            with self.subTest(report_route=report_route), self.assertRaises(PodError) as caught:
                if report_route: self.report(admission, frozen, map={"obligations": closing, "close": True})
                else: self.write(closing, close=True)
            self.assertEqual(caught.exception.code, "native_assignment_unverified")
            self.assertEqual(before, self.saved())


class OwnerKeepTests(TabCase):
    def transition(self, *, report_route):
        frozen, admission = self.worker()
        revision = read(self.project, "objective")["checkpoint"]["revision"]
        self.keep(admission)
        self.assertEqual(read(self.project, "objective")["checkpoint"]["revision"], revision)
        if not report_route:
            self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        snapshot = self.saved()
        for case in ("owned", "retained", "user_owned", "external"):
            with self.subTest(case=case):
                self.restore(snapshot); self.tab_case = case
                if report_route:
                    raw = self.report(admission, frozen, map={"obligations": self.finished_rows(), "close": True})
                    ack = self.ack("report", {"admission_id": admission["admission_id"]}, raw)
                else:
                    raw = self.write(self.finished_rows(), close=True)
                    ack = self.ack("checkpoint", {}, raw)
                row = ack["worker_tabs"][0]
                self.assertTrue(row["owner_kept"])
                self.assertIn("Owner-kept", row["wording"])
                self.assertNotIn("Release owed", json.dumps(ack))
                self.assertNotIn("safe to retire", json.dumps(ack)); self.assertNotIn("Pod needs nothing more", json.dumps(ack))
                if case in ("user_owned", "external"):
                    self.assertIsNone(row["command"])
                    self.assertIn("Do not reuse", row["wording"])
                else:
                    self.assertEqual(row["command"], self.release_argv)
                status_row = self.status()["worker_tabs"][0]
                self.assertTrue(status_row["owner_kept"])
                self.assertEqual(row["command"], status_row["command"])

    def test_checkpoint_keep_survives_ownership_change(self):
        self.transition(report_route=False)

    def test_report_keep_survives_ownership_change(self):
        self.transition(report_route=True)

    def test_revocation_disconnection_reuse_and_pending_precedence(self):
        frozen, admission = self.worker(); self.keep(admission)
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        for case in ("release_pending", "release_unknown"):
            self.tab_case = case
            with self.assertRaises(PodError) as caught:
                self.write(self.finished_rows(), close=True)
            self.assertEqual(caught.exception.code, "worker_tabs_open")
        self.tab_case = "user_owned"
        self.assertTrue(self.status()["worker_tabs"][0]["owner_kept"])
        self.keep(admission, False, "revoke")
        self.assertEqual(self.status()["worker_tabs"][0]["command"][:3], ["orca", "terminal", "close"])
        for case in ("disconnected", "reused_later", "coordinator", "setup", "other_objective"):
            self.tab_case = case
            self.assertEqual(self.status()["worker_tabs"], [])

    def test_keep_needs_recorded_instruction_and_exact_objective(self):
        _, admission = self.worker()
        before = self.saved()
        with self.assertRaises(PodError): self.keep(admission, objective="another-objective")
        self.assertEqual(self.saved(), before)

    def test_recorded_revocation_gives_close_on_both_closing_routes(self):
        frozen, admission = self.worker()
        self.keep(admission); self.tab_case = "user_owned"; self.keep(admission, False, "revoke")
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        snapshot = self.saved()
        for report_route in (False, True):
            with self.subTest(report_route=report_route):
                self.restore(snapshot)
                raw = (self.report(admission, frozen, map={"obligations": self.finished_rows(), "close": True})
                       if report_route else self.write(self.finished_rows(), close=True))
                self.assertFalse(raw["worker_tabs"][0]["owner_kept"])
                self.assertEqual(raw["worker_tabs"][0]["command"][:3], ["orca", "terminal", "close"])

    def test_owner_can_revoke_after_closure_without_changing_the_closure_report(self):
        frozen, admission = self.worker(); self.keep(admission)
        self.tab_case = "user_owned"
        self.report(admission, frozen, map={"obligations": self.finished_rows(), "close": True})
        closure = deepcopy(read(self.project, "objective")["checkpoint"])
        self.keep(admission, False, "revoke-after-close")
        self.assertEqual(read(self.project, "objective")["checkpoint"], closure)
        row = self.status()["worker_tabs"][0]
        self.assertFalse(row["owner_kept"])
        self.assertEqual(row["command"][:3], ["orca", "terminal", "close"])


class RetentionTests(TabCase):
    review = assurance_review
    satisfied = assurance_satisfied
    waiting = assurance_waiting

    def test_required_correction_delta_review_then_release(self):
        self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
            "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
            "candidate": self.base, "existing_evidence": "tests", "insufficiency": "no review",
            "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}})
        frozen = self.packet(["A"], role="review", purpose="Check parser")
        admission = self.start("review", frozen)["admission"]; self.settle(admission)
        rows = self.stored(); a = rows[1]; a.pop("executor")
        a.update(state="waiting", wait={"class": "dependency", "referent": "K"})
        rows.append({"id": "K", "kind": "correction", "provenance": "coordinator", "parent": "A",
            "check": "repair parser", "boundary": {"paths": ["src"]}, "state": "unassigned"})
        self.tab_case = "retained"
        raw = self.report(admission, frozen, map={"obligations": rows}, triage=[{
            "finding": "defect", "severity": "major", "triage": "required_correction",
            "summary": "parser defect", "correction": "K"}])
        self.assertEqual(raw["worker_tab"]["retained_title"]["title"], "Reviewer: Check parser (retained: corrections)")
        rows = self.stored(); rows[2].update(state="satisfied", evidence=[self.proof("K")])
        rows[1]["wait"] = {"class": "sequenced", "referent": "O1"}; self.write(rows)
        raw = self.report(admission, frozen)
        self.assertEqual(raw["worker_tab"]["retained_title"]["title"], "Reviewer: Check parser (retained: delta review)")
        self.tab_case = "not_materialized"
        self.review("delta-review", delta_from=admission["admission_id"])
        self.tab_cases[admission["native_binding"]["dispatchId"]] = "retained"
        self.assertEqual(self.status()["worker_tabs"][0]["state"], "releasable")
        with self.assertRaises(PodError) as caught: self.write(self.finished_rows(), close=True)
        self.assertEqual(caught.exception.code, "worker_tabs_open")
        self.tab_cases[admission["native_binding"]["dispatchId"]] = "disconnected"
        self.write(self.finished_rows(), close=True)
