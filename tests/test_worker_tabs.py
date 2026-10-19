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
        self.release_argv = ["orchestration", "worker-release", "--dispatch", "dispatch-1"]
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
        release = ("retained" if case in ("retained", "owner_request", "user_owned", "external") else
                   case if case in ("release_pending", "release_unknown") else "not_requested")
        resource = {"terminalHandle": handle, "worktreeId": worktree, "ownerDispatchId": dispatch,
                    "originDispatchId": dispatch, "ownershipState": ownership, "releaseState": release,
                    "retainedReason": "user_takeover" if case == "user_owned" else "owner_request" if case == "owner_request"
                                      else "coordinator_request" if case == "retained" else None}
        terminal = {"handle": handle, "tabId": "synthetic-tab", "worktreeId": worktree,
                    "connected": case != "disconnected", "writable": True, "orphaned": False}
        result.update(terminalResource=resource, terminal=terminal)
        projection = result["projection"]
        projection.update(role="worker", resource={"state": ownership, "releaseState": release,
            "ownerDispatchId": dispatch, "terminalState": "reclaimable"},
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
        if case.startswith("missing_"):
            # The shape of Orca's exact worker-show once a finished worker's terminal record is gone (R125, A475).
            state = {"missing_released": "released", "missing_user_owned": "user_owned",
                     "missing_owned": "owned", "missing_unconfirmed": "released",
                     "missing_other_worker": "released", "missing_no_worker": "released",
                     "missing_bare_match": "released", "missing_bare_other": "released",
                     "missing_bare_none": "released", "missing_null_other": "released"}[case]
            settled = "released" if state == "released" else "retained" if state == "user_owned" else "not_requested"
            resource.update(ownershipState=state, releaseState=settled,
                            retainedReason="user_takeover" if state == "user_owned" else None)
            projection.update(resource={"state": state, "releaseState": settled, "ownerDispatchId": dispatch,
                                        "terminalState": "released" if state == "released" else "reclaimable"},
                              liveness={"verdict": "exited", "source": "resource_release"},
                              nextAction={"kind": "none", "argv": []})
            result["terminal"] = None
            if case != "missing_unconfirmed":
                result["observation"] = {"status": "missing", "exactWorker": False}
            if case in ("missing_other_worker", "missing_no_worker"):
                # The worker no longer names the admitted terminal, so its absence is not proved (review F1).
                worker["agentTerminalHandle"] = "term-other" if case == "missing_other_worker" else None
            if case.startswith(("missing_bare_", "missing_null_")):
                # Orca omits the raw resource (or reports it null); only the projection's resource remains (audit F1).
                if case.startswith("missing_bare_"):
                    result.pop("terminalResource")
                else:
                    result["terminalResource"] = None
                if case.endswith("_other"):
                    worker["agentTerminalHandle"] = "term-other"
                elif case.endswith("_none"):
                    worker.pop("agentTerminalHandle")
        if case == "reused_absent":
            # Orca's exact worker-show for an earlier Dispatch after a later Dispatch reused its terminal and that
            # terminal's record is gone: no terminal, no raw resource and an absent projected resource (R125).
            result["terminal"] = None
            result["terminalResource"] = None
            result["observation"] = {"status": "missing", "exactWorker": False}
            projection.update(resource={"state": "absent", "reason": "not_materialized"},
                              liveness={"verdict": "unverifiable", "reason": "missing_status"},
                              nextAction={"kind": "none", "argv": []})
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
        role_title = {"implement": "Implementer", "investigate": "Investigator", "review": "Reviewer"}[role]
        from pod.display import job
        expected_tab = role_title + " · " + job(fields, role_title.lower())
        self.assertEqual(acknowledged["title"], expected_tab)
        self.assertEqual(acknowledged["rename_command"],
            ["orca", "terminal", "rename", "--terminal", handle, "--title", expected_tab, "--json"] if handle else None)
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
        self.assertEqual(result["title"], "Investigator · Trace tokenizer (continued)")
        self.assertEqual(result["rename_command"][4], admission["native_binding"]["terminalHandle"])
        self.assertNotEqual(result["title"], frozen["title"])
        self.tab_cases[admission["native_binding"]["dispatchId"]] = "reused_later"
        self.settle(result["admission"])
        self.report(result["admission"], fresh, map={"obligations": self.waiting_rows()})
        tabs = self.status()["worker_tabs"]
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["dispatch"], result["admission"]["native_binding"]["dispatchId"])
        self.assertFalse(tabs[0]["owner_kept"])

    def test_a_reused_terminal_stays_the_later_dispatchs_after_orca_drops_the_earlier_resource(self):
        # R125: Pod's own bound reuse admission proves the reuse once Orca no longer names the resource's owner.
        frozen, admission = self.title_admission("investigate", "Investigator: writer")
        self.settle(admission)
        self.keep(admission)
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        fresh = self.packet(["S"], role="investigate", resolves="new question", stop_condition="answer", purpose="Trace tokenizer")
        later = self.start("follow-up", fresh, reuse_of=admission["admission_id"])["admission"]
        self.settle(later)
        self.report(later, fresh, map={"obligations": self.waiting_rows()})
        self.tab_cases[admission["native_binding"]["dispatchId"]] = "reused_absent"
        self.tab_cases[later["native_binding"]["dispatchId"]] = "missing_released"
        self.assertEqual(self.status()["worker_tabs"], [])
        raw = self.write(self.finished_rows(), close=True)
        self.assertEqual(self.ack("checkpoint", {}, raw)["worker_tabs"], [])
        self.assertIsNotNone(read(self.project, "objective")["checkpoint"]["closure"])

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
        self.assertEqual(result["title"], "Implementer · Repair parser")
        self.assertIsNone(result["rename_command"])
        self.assertEqual(result["rename_reason"], "worker not started")

    def test_optional_title_fields_obey_their_bounds(self):
        self.intake(self.sub("S"))
        for name, limit in (("role_label", 48), ("purpose", 160)):
            self.packet(["S"], **{name: "a" * limit})
            with self.assertRaises(PodError): self.packet(["S"], **{name: "a" * (limit + 1)})

    def test_unresolved_start_returns_title_and_honest_uncertainty(self):
        self.intake(self.sub("S"))
        self.port.receipt = {"runtime": "runtime", "exit": 1, "request_uuid": REQUEST,
                             "error": {"code": "runtime_error", "message": "uncertain"}}
        result = self.start("worker", self.packet(["S"], purpose="Repair parser"))
        self.assertEqual(result["title"], "Implementer · Repair parser")
        self.assertIsNone(result["rename_command"])
        self.assertEqual(result["rename_reason"], "worker start is not verified")


class TableTests(TabCase):
    def unreported(self, case):
        _, admission = self.worker()
        self.tab_case = case
        self.assertIsNone(read(self.project, "objective")["admissions"][admission["admission_id"]]["report"])
        result = self.write(self.waiting_rows())
        ack = self.ack("checkpoint", {"value": {}}, result)
        for output in (self.status(), ack):
            row = output["worker_tabs"][0]
            self.assertEqual((row["row"], row["state"]), (case, "kept"))
            self.assertIsNone(row["command"])
            self.assertIn("Do not reuse", row["wording"])
            self.assertIn("once its report is recorded", row["wording"])
            self.assertNotIn("terminal close", json.dumps(output))

    def test_unreported_user_owned_status_and_checkpoint_ack_wait_for_report(self):
        self.unreported("user_owned")

    def test_unreported_external_status_and_checkpoint_ack_wait_for_report(self):
        self.unreported("external")

    def test_status_runtime_change_names_continuity_instead_of_an_unreadable_tab(self):
        frozen, admission = self.worker()
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        self.port.runtime = "changed-runtime"
        result = self.status()
        row = result["worker_tabs"][0]
        self.assertEqual((row["row"], row["state"]), ("runtime_changed", "unknown"))
        self.assertEqual(row["failed_read"], {"code": "native_assignment_unverified", "command": None})
        self.assertIsNone(row["command"])
        self.assertIn("Orca's runtime changed", row["wording"])
        self.assertIn("the next Pod write rebinds when continuity is proven", row["wording"])
        self.assertIn("the owner decides when it is ambiguous", row["wording"])
        for text in (row["wording"], result["readiness"]["summary"],
                     result["readiness"]["finalization"]["next_action"]):
            self.assertIn(row["wording"], text)
            self.assertNotIn("Terminal facts unreadable", text)
            self.assertNotIn("worker-show", text)
        self.assertEqual(result["native_settlement"], "unverified")
        self.write(self.waiting_rows())
        self.assertEqual(self.status()["worker_tabs"][0]["state"], "releasable")

    def disposed_unreported(self, case, *, close):
        self.intake(self.sub("S"))
        frozen = self.packet(["S"], purpose="Repair parser", boundary={"paths": ["src"]})
        admission = self.start("implement", frozen)["admission"]; self.settle(admission)
        self.tab_case = case
        result = self.write(self.finished_rows() if close else self.waiting_rows(), **({"close": True} if close else {}),
                            dispositions=[{"admission": admission["admission_id"], "discarded": True,
                                           "reason": "superseded"}])
        ack = self.ack("checkpoint", {}, result)
        for output in (ack, self.status()):
            row = output["worker_tabs"][0]
            self.assertEqual((row["row"], row["state"]), (case, "kept"))
            self.assertIsNone(row["command"])
            self.assertIn("Do not reuse", row["wording"])
            if close:
                self.assertIn("standing close authorization does not cover this tab", row["wording"])
                self.assertIn("because its report was not recorded", row["wording"])
                self.assertIn("name it in the final report for the owner's decision", row["wording"])
                self.assertNotIn("once its report is recorded", json.dumps(output))
            else:
                self.assertIn("The authorized close becomes available once its report is recorded", row["wording"])
                self.assertNotIn("for the owner's decision", row["wording"])
            self.assertNotIn("terminal close", json.dumps(output))
            finalization = output.get("finalization") or output["readiness"]["finalization"]
            self.assertIn(row["wording"], finalization["next_action"])
        if close:
            before = self.saved()
            with self.assertRaises(PodError) as caught: self.report(admission, frozen)
            self.assertEqual(caught.exception.code, "objective_closed")
            self.assertEqual(before, self.saved())
        else:
            result = self.report(admission, frozen)
            ack = self.ack("report", {"admission_id": admission["admission_id"]}, result)
            for row in (result["worker_tab"], ack["worker_tab"], self.status()["worker_tabs"][0]):
                self.assertEqual((row["row"], row["state"]), (case, "kept"))
                self.assertIn("Do not reuse", row["wording"])
                self.assertEqual(row["command"],
                                 ["orca", "terminal", "close", "--terminal", admission["native_binding"]["terminalHandle"]])
            state = read(self.project, "objective")
            self.assertIsNone(state["checkpoint"].get("closure"))
            self.assertIsNotNone(state["admissions"][admission["admission_id"]]["report"])

    def test_unreported_user_owned_closing_ack_and_status_name_owner_decision(self):
        self.disposed_unreported("user_owned", close=True)

    def test_unreported_external_closing_ack_and_status_name_owner_decision(self):
        self.disposed_unreported("external", close=True)

    def test_unreported_discarded_user_owned_ack_and_status_allow_late_report(self):
        self.disposed_unreported("user_owned", close=False)

    def test_unreported_discarded_external_ack_and_status_allow_late_report(self):
        self.disposed_unreported("external", close=False)

    def test_report_ack_waits_for_another_workers_unrecorded_report(self):
        self.intake(self.sub("S"), self.sub("T"))
        packets = [self.packet([key], role="investigate", resolves="question", stop_condition="answer")
                   for key in ("S", "T")]
        # Freeze the second packet after the first admission changes the map.
        first = self.start("first", packets[0])["admission"]
        packets[1] = self.packet(["T"], role="investigate", resolves="question", stop_condition="answer")
        second = self.start("second", packets[1])["admission"]
        self.settle(first); self.settle(second)
        self.tab_cases[first["native_binding"]["dispatchId"]] = "user_owned"
        self.tab_cases[second["native_binding"]["dispatchId"]] = "external"
        result = self.report(first, packets[0], map={"obligations": self.waiting_rows()})
        ack = self.ack("report", {"admission_id": first["admission_id"]}, result)
        rows = {row["admission"]: row for row in ack["worker_tabs"]}
        self.assertEqual(rows[first["admission_id"]]["command"],
                         ["orca", "terminal", "close", "--terminal", first["native_binding"]["terminalHandle"]])
        other = rows[second["admission_id"]]
        self.assertIsNone(other["command"])
        self.assertIn("once its report is recorded", other["wording"])
        self.assertIn(other["wording"], ack["finalization"]["next_action"])

    def test_owned_report_ack_leads_with_exactly_one_and_relays_native_release(self):
        frozen, admission = self.worker()
        result = self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        ack = self.ack("report", {"admission_id": admission["admission_id"]}, result)
        row = ack["worker_tab"]
        self.assertTrue(row["wording"].startswith("After an accepted report choose exactly one:"), row)
        self.assertIn("immediate supported reuse, permitted retention, or release", row["wording"])
        self.assertEqual(row["command"], ["orca", *self.release_argv])
        self.assertIn("orca orchestration worker-release --dispatch dispatch-1", row["wording"])
        self.assertNotIn("Release owed", row["wording"])
        view = self.status()
        self.assertIn("Release owed", view["readiness"]["summary"])
        self.assertIn("Release owed", view["readiness"]["finalization"]["next_action"])
        self.assertIn("Release owed", ack["finalization"]["next_action"])

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
                        self.assertEqual(row["native_next_action"], self.shown(admission["native_binding"]["dispatchId"])["result"]["projection"]["nextAction"])
                        if case in ("owned", "retained", "release_pending", "release_unknown"):
                            self.assertEqual(row["command"], ["orca", *self.release_argv])
                        if case == "no_command":
                            self.assertIsNone(row["command"])
                            self.assertIn("Orca gives no next command", row["wording"])
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

    def test_user_owned_and_external(self):
        self.table((("user_owned", "user_owned", True, False), ("external", "external", True, False)))

    def test_pending_unknown_and_unreadable(self):
        self.table((("release_pending", "release_pending", True, True),
                    ("release_unknown", "release_pending", True, True), ("unreadable", "unreadable", True, True)))

    def test_not_materialized_disconnected_and_reused(self):
        self.table((("not_materialized", "not_materialized", False, False), ("disconnected", "disconnected", False, False),
                    ("reused_later", "reused_later", False, False)))

    def test_a_gone_terminal_record_reads_disconnected_only_for_a_released_or_unowned_resource(self):
        # A475: Orca's exact read states no terminal record and the resource is released or not Orca's.
        self.table((("missing_released", "disconnected", False, False),
                    ("missing_user_owned", "disconnected", False, False),
                    ("missing_owned", "unreadable", True, True),
                    ("missing_unconfirmed", "unreadable", True, True)))

    def test_an_absent_resource_without_a_recorded_reuse_stays_unreadable(self):
        # R125: without Pod's own bound reuse admission, an absent resource proves no later owner.
        self.table((("reused_absent", "unreadable", True, True),))

    def test_a_gone_record_stays_unreadable_while_the_worker_names_another_or_no_terminal(self):
        # Review F1: the admitted terminal is not proved gone when the worker no longer names it.
        frozen, admission = self.worker()
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        for case in ("missing_other_worker", "missing_no_worker", "missing_bare_other", "missing_bare_none",
                     "missing_null_other"):
            with self.subTest(case=case):
                self.tab_case = case
                row = self.status()["worker_tabs"][0]
                self.assertEqual((row["row"], row["state"], row["blocks_closure"]), ("unreadable", "unknown", True))
                self.assertEqual(row["failed_read"]["code"], "worker_terminal_unverified")
        # Without the raw resource, a worker that still names the admitted terminal reads as its gone tab.
        self.tab_case = "missing_bare_match"
        self.assertEqual(self.status()["worker_tabs"], [])

    def test_coordinator_setup_and_other_objective_excluded(self):
        self.table((("coordinator", "excluded", False, False),
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
                    unreadable = case in ("unreadable", "missing_unconfirmed", "missing_other_worker",
                                          "missing_no_worker", "missing_bare_other", "missing_bare_none",
                                          "missing_null_other")
                    self.assertEqual(error.code, "worker_terminal_unverified" if unreadable else "worker_tabs_open")
                    self.assertIn("worker-show" if unreadable else "worker-release", str(error))
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

    def test_unrecorded_owner_request_is_retained_and_still_blocks_both_routes(self):
        frozen, admission = self.worker(); self.tab_case = "owner_request"
        raw = self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        row = raw["worker_tab"]
        self.assertEqual(row["row"], "retained_owned"); self.assertFalse(row["owner_kept"])
        self.assertEqual(row["reason"], "owner_request"); self.assertNotIn("retained_title", row)
        self.assertEqual(self.status()["worker_tabs"][0]["row"], "retained_owned")
        before = self.saved()
        for report_route in (False, True):
            with self.subTest(report_route=report_route), self.assertRaises(PodError) as caught:
                if report_route: self.report(admission, frozen, map={"obligations": self.finished_rows(), "close": True})
                else: self.write(self.finished_rows(), close=True)
            self.assertEqual(caught.exception.code, "worker_tabs_open")
            self.assertEqual(before, self.saved())

    def test_checkpoint_accepts_kept_and_absent(self):
        self.closing(tuple((case, False) for case in ("user_owned", "external", "not_materialized", "disconnected")),
                     report_route=False)

    # A475: neither closing route is held by a worker whose terminal record Orca reports gone. The table is split
    # into named groups to stay under the fast-lane limit.
    GONE_RECORD_CASES = (("missing_released", False), ("missing_user_owned", False), ("missing_unconfirmed", True))

    def test_closing_checkpoint_accepts_a_gone_terminal_record_and_holds_an_unconfirmed_one(self):
        self.closing(self.GONE_RECORD_CASES, report_route=False)

    def test_closing_checkpoint_holds_a_gone_terminal_record_without_the_workers_own_identity(self):
        # A closing report refuses a differing worker terminal earlier (report_attempt_unverified).
        self.closing((("missing_other_worker", True), ("missing_no_worker", True), ("missing_bare_other", True),
                      ("missing_bare_none", True), ("missing_null_other", True)), report_route=False)

    def test_closing_report_accepts_a_gone_terminal_record_and_holds_an_unconfirmed_one(self):
        self.closing(self.GONE_RECORD_CASES, report_route=True)

    def test_checkpoint_ignores_reused_and_unrelated_terminals(self):
        self.closing(tuple((case, False) for case in ("reused_later", "coordinator", "setup", "other_objective")),
                     report_route=False)

    def test_report_accepts_own_kept_and_absent(self):
        self.closing(tuple((case, False) for case in ("user_owned", "external", "not_materialized", "disconnected")),
                     report_route=True)

    def test_report_ignores_reused_and_unrelated_terminals(self):
        self.closing(tuple((case, False) for case in ("reused_later", "coordinator", "setup", "other_objective")),
                     report_route=True)

    def test_effective_refresh_cannot_write_before_tab_refusal(self):
        from pod.ledger import update_admission
        frozen, admission = self.worker()
        def change(row):
            row["route_decision"]["effective_unknown"] = True
            row["route_decision"]["effective"] = {"agent": "codex", "model": "unknown",
                                                  "effort": "unknown", "context": "unknown"}
            row["effective_evidence"]["effective"] = deepcopy(row["route_decision"]["effective"])
        update_admission(self.project, "objective", owner="owner", admission_id=admission["admission_id"], update=change)
        before = self.saved()
        with self.assertRaises(PodError) as caught:
            self.report(admission, frozen, map={"obligations": self.finished_rows(), "close": True})
        self.assertEqual(caught.exception.code, "worker_tabs_open")
        self.assertEqual(before, self.saved())

    def test_closing_route_mismatch_precedes_tab_body_and_map_validation(self):
        frozen, admission = self.worker()
        self.port.effective = {"agent": "codex", "model": "gpt-6-luna", "effort": "medium"}
        before = read(self.project, "objective")
        for outcome, rows in (("bogus", self.finished_rows()), ("succeeded", "invalid-map")):
            with self.subTest(outcome=outcome), self.assertRaises(PodError) as caught:
                self.report(admission, frozen, outcome=outcome, map={"obligations": rows, "close": True})
            self.assertEqual(caught.exception.code, "route_mismatch")
            after = read(self.project, "objective")
            self.assertTrue(after["admissions"][admission["admission_id"]]["route_decision"]["route_mismatch"])
            self.assertIsNone(after["admissions"][admission["admission_id"]]["report"])
            self.assertEqual(before["checkpoint"], after["checkpoint"])

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
                if case in ("owned", "retained"):
                    self.assertIn("Release still owed after recorded closure", ready["summary"])
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
            self.assertEqual(caught.exception.code, "worker_tabs_open")
            self.assertEqual(before, self.saved())

    def rebind_and_close(self, *, report_route, worker=True):
        if worker:
            frozen, admission = self.worker()
            self.tab_case = "not_materialized"
            if not report_route:
                self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        else:
            self.intake()
        closing = self.finished_rows()
        self.port.runtime = "changed-runtime"
        from pod import ledger
        with patch.object(ledger, "_write", wraps=ledger._write) as writes:
            result = (self.report(admission, frozen, map={"obligations": closing, "close": True})
                      if report_route else self.write(closing, close=True))
        self.assertEqual(writes.call_count, 1, "rebind and closure must be one write")
        state = read(self.project, "objective")
        self.assertIsNotNone(state["checkpoint"]["closure"])
        self.assertEqual(len(state["checkpoint"]["continuity"]["history"]), 1)
        self.assertEqual(state["checkpoint"]["native_refs"][0]["runtime"], "changed-runtime")
        self.assertEqual(result["runtime_continuity"][0]["to_runtime"], "changed-runtime")

    def test_runtime_change_closing_checkpoint_rebinds_atomically_without_tabs(self):
        self.rebind_and_close(report_route=False)

    def test_runtime_change_closing_report_rebinds_atomically_without_tabs(self):
        self.rebind_and_close(report_route=True)

    def test_runtime_change_closing_checkpoint_rebinds_without_admissions(self):
        self.rebind_and_close(report_route=False, worker=False)

    def test_continuity_difference_is_not_an_unreadable_tab(self):
        frozen, admission = self.worker()
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        closing = self.finished_rows(); before = self.saved()
        self.port.runtime = "changed-runtime"
        self.current["consumer_generation"] += 1
        for report_route in (False, True):
            with self.subTest(report_route=report_route), self.assertRaises(PodError) as caught:
                if report_route: self.report(admission, frozen, map={"obligations": closing, "close": True})
                else: self.write(closing, close=True)
            self.assertEqual(caught.exception.code, "native_identity_unverified" if report_route else "native_assignment_unverified")
            self.assertNotIn("Terminal facts unreadable", str(caught.exception.detail))
            self.assertNotIn("worker-show", str(caught.exception.detail))
            self.assertEqual(before, self.saved())
        with self.assertRaises(PodError) as malformed:
            self.report(admission, frozen, outcome="bogus", map={"obligations": closing, "close": True})
        self.assertEqual(malformed.exception.code, "native_identity_unverified")
        self.assertEqual(before, self.saved())

    def test_closing_continuity_ambiguity_keeps_its_owner_decision_route(self):
        frozen, admission = self.worker()
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        from pod.ledger import _lock, _path, _read, _write
        path = _path(self.project, "objective")
        with _lock(path):
            prior_state = _read(path)
            prior_state["checkpoint"].pop("continuity")
            _write(path, prior_state)
        closing = self.finished_rows(); before = self.saved()
        self.port.runtime = "changed-runtime"
        for report_route in (False, True):
            with self.subTest(report_route=report_route), self.assertRaises(PodError) as caught:
                if report_route:
                    self.report(admission, frozen, outcome="bogus", map={"obligations": closing, "close": True})
                else:
                    self.write(closing, close=True)
            self.assertEqual(caught.exception.code, "runtime_continuity_ambiguous")
            self.assertIn("internal runtime-continuity", caught.exception.detail["next_action"])
            self.assertNotIn("Terminal facts unreadable", str(caught.exception.detail))
            self.assertNotIn("worker-show", caught.exception.detail["next_action"])
            self.assertEqual(before, self.saved())

    def test_closed_refusals_use_known_rows_or_name_the_status_read(self):
        frozen, admission = self.worker(); self.tab_case = "not_materialized"
        self.report(admission, frozen, map={"obligations": self.finished_rows(), "close": True})
        self.tab_case = "owned"; before = self.saved()
        first = len(self.calls)
        with self.assertRaises(PodError) as early:
            self.op("governor-outcome", record_id="unused", outcome="FAILED")
        self.assertEqual(self.calls[first:], [], "an early refusal added a native read")
        self.assertIn("Read status", early.exception.detail["finalization"]["next_action"])
        with self.assertRaises(PodError) as observed:
            self.report(admission, frozen)
        self.assertIn("Release still owed", observed.exception.detail["finalization"]["next_action"])
        for error in (early.exception, observed.exception):
            self.assertNotIn("safe to retire", json.dumps(error.detail))
            self.assertNotIn("Pod needs nothing more", json.dumps(error.detail))
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
                    self.assertEqual(row["command"], ["orca", *self.release_argv])
                    if report_route:
                        self.assertEqual(ack["worker_tab"]["retained_title"]["title"], "Investigator · Investigation")
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
            before = self.saved()
            for report_route in (False, True):
                with self.subTest(case=case, report_route=report_route), self.assertRaises(PodError) as caught:
                    if report_route: self.report(admission, frozen, map={"obligations": self.finished_rows(), "close": True})
                    else: self.write(self.finished_rows(), close=True)
                self.assertEqual(caught.exception.code, "worker_tabs_open")
                self.assertEqual(before, self.saved())
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

    def test_implementer_retained_for_corrections_and_delta_review_of_its_result(self):
        self.intake(self.sub("S"), {"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
            "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
            "candidate": self.base, "existing_evidence": "tests", "insufficiency": "no review",
            "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}})
        frozen = self.packet(["S"], purpose="Repair parser", boundary={"paths": ["src"]})
        implementer = self.start("implement", frozen)["admission"]; self.settle(implementer)
        integrated = self.move("src/old.py")
        self.report(implementer, frozen, map={"obligations": self.waiting_rows()}, result_commit=self.candidate)
        rows = self.stored()
        next(row for row in rows if row["id"] == "A")["candidate"] = self.candidate
        self.write(rows, dispositions=[{"admission": implementer["admission_id"], "integrated_into": self.candidate}])
        review_packet = self.packet(["A"], role="review")
        reviewer = self.start("review", review_packet)["admission"]; self.settle(reviewer)
        rows = self.stored()
        audit = next(row for row in rows if row["id"] == "A"); audit.pop("executor")
        audit.update(state="waiting", wait={"class": "dependency", "referent": "K"})
        rows.append({"id": "K", "kind": "correction", "provenance": "coordinator", "parent": "A",
            "check": "repair parser", "boundary": {"paths": ["src"]}, "state": "unassigned"})
        self.tab_case = "retained"
        self.report(reviewer, review_packet, map={"obligations": rows}, triage=[{
            "finding": "defect", "severity": "major", "triage": "required_correction",
            "summary": "parser defect", "correction": "K"}])
        for reason in ("corrections", "delta review"):
            with self.subTest(reason=reason):
                result = self.report(implementer, frozen, result_commit=integrated)
                row = result["worker_tab"]
                self.assertEqual(row["retention_reason"], reason)
                self.assertEqual(row["retained_title"]["title"], "Implementer · Repair parser")
                status_row = next(row for row in self.status()["worker_tabs"]
                                  if row["admission"] == implementer["admission_id"])
                self.assertEqual(status_row["retention_reason"], reason)
                self.assertIn(reason, status_row["wording"])
            if reason == "corrections":
                self.move("src/old.py")
                rows = self.stored()
                audit = next(row for row in rows if row["id"] == "A")
                audit["candidate"] = self.candidate
                next(row for row in rows if row["id"] == "K").update(
                    state="waiting", wait={"class": "sequenced", "referent": "O1"})
                self.write(rows, candidate=self.candidate)
                retained = next(row for row in self.status()["worker_tabs"]
                                if row["admission"] == implementer["admission_id"])
                self.assertEqual(retained["retention_reason"], "corrections")
                rows = self.stored()
                correction = next(row for row in rows if row["id"] == "K")
                correction.pop("wait")
                correction.update(state="satisfied", evidence=[self.proof("K")])
                audit = next(row for row in rows if row["id"] == "A")
                audit.update(wait={"class": "sequenced", "referent": "O1"}, candidate=self.candidate)
                self.write(rows, candidate=self.candidate)
        self.assertNotEqual(integrated, self.candidate)
        self.review("delta-review", delta_from=reviewer["admission_id"])
        for row in self.status()["worker_tabs"]:
            self.assertIsNone(row["retention_reason"])
            self.assertIn("Release owed", row["wording"])

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
        self.assertEqual(raw["worker_tab"]["retained_title"]["title"], "Reviewer · Check parser")
        self.assertEqual(self.status()["worker_tabs"][0]["retention_reason"], "corrections")
        before = self.saved()
        for report_route in (False, True):
            with self.subTest(report_route=report_route), self.assertRaises(PodError) as caught:
                if report_route: self.report(admission, frozen, map={"obligations": self.stored(), "close": True})
                else: self.write(self.stored(), close=True)
            self.assertEqual(caught.exception.code, "worker_tabs_open")
            self.assertEqual(before, self.saved())
        rows = self.stored(); rows[2].update(state="satisfied", evidence=[self.proof("K")])
        rows[1]["wait"] = {"class": "sequenced", "referent": "O1"}; self.write(rows)
        raw = self.report(admission, frozen)
        self.assertEqual(raw["worker_tab"]["retained_title"]["title"], "Reviewer · Check parser")
        self.tab_case = "not_materialized"
        self.review("delta-review", delta_from=admission["admission_id"])
        self.tab_cases[admission["native_binding"]["dispatchId"]] = "retained"
        self.assertEqual(self.status()["worker_tabs"][0]["state"], "releasable")
        with self.assertRaises(PodError) as caught: self.write(self.finished_rows(), close=True)
        self.assertEqual(caught.exception.code, "worker_tabs_open")
        self.tab_cases[admission["native_binding"]["dispatchId"]] = "disconnected"
        self.write(self.finished_rows(), close=True)


class ReadinessWordingTests(TabCase):
    def test_open_summaries_keep_work_wait_and_finalization_detail(self):
        frozen, admission = self.worker()
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        self.assertTrue(self.status()["readiness"]["summary"].startswith(
            "Working: the coordinator acts next; not finished. Worker tab"))
        rows = self.waiting_rows()
        rows[0].pop("executor")
        rows[0].update(state="blocked_external", external={"party": "owner", "need": "choose a channel",
                                                         "unblocks_when": "the owner answers"})
        rows[1]["wait"] = {"class": "dependency", "referent": "O1"}
        self.write(rows)
        self.assertTrue(self.status()["readiness"]["summary"].startswith(
            "Waiting on the owner: choose a channel (O1)"))
        self.write([{key: value for key, value in row.items() if key != "external"} for row in self.finished_rows()])
        summary = self.status()["readiness"]["summary"]
        self.assertTrue(summary.startswith("Delivered, not closed: the coordinator records closure now"), summary)
        self.assertIn("not safe to close the coordinator. Worker tab", summary)

    def test_closed_external_summary_keeps_gate_and_party_detail(self):
        frozen, admission = self.worker(); self.tab_case = "not_materialized"
        self.report(admission, frozen, map={"obligations": self.waiting_rows()})
        self.write(self.finished_rows(), close=True, remaining_gates=[{"kind": "hosted_ci", "workflow": "ci.yml"}])
        self.tab_case = "owned"
        summary = self.status()["readiness"]["summary"]
        self.assertTrue(summary.startswith("Closed with external work: 1 remaining gate(s) outside the objective, "
                                           "for the provider; Worker tab"), summary)
        self.assertNotIn("needs nothing more", summary)
        self.assertNotIn("coordinator may close", summary)
