"""R250: `pod status --objective ID --admission ADMISSION --json` projects one admission's packet and
report-receipt facts from Pod's own record, read-only and bounded, and leaks no report, packet or record-derived
content (A1117-A1121)."""

import contextlib
import fcntl
import io
import json
from unittest.mock import patch

from pod import admission_view, cli
from pod.ledger import objective_root, read
from tests.common import logical_json
from tests.kernel_support import KernelCase, git

# Text that appears only in report or packet bodies; the projection must never carry any of it.
PRIVATE = ("src/new.py", "review not completed", "report_contract", "checks", "edit", "writer", "PoD#1 passes")


class AdmissionViewCase(KernelCase):
    def setUp(self):
        super().setUp()
        self.record = objective_root(self.project, "objective") / "context.json"

    def pod(self, *argv):
        """The public command through the real CLI, with Orca, preferences and locks unavailable."""
        out = io.StringIO()
        refuse = AssertionError("the admission view made a forbidden call")
        with (contextlib.chdir(self.project), contextlib.redirect_stdout(out),
              patch("pod.orca.current_run", side_effect=refuse), patch("pod.orca.worker_rows", side_effect=refuse),
              patch("pod.orca.read_command", side_effect=refuse),
              patch("pod.orca.mutate_command", side_effect=refuse), patch("pod.config.load", side_effect=refuse),
              patch("pod.ledger._lock", side_effect=refuse), patch("pod.ledger._write", side_effect=refuse)):
            code = cli.main(["status", *argv])
        return code, out.getvalue()

    def view(self, admission, objective="objective"):
        code, text = self.pod("--objective", objective, "--admission", admission, "--json")
        result = json.loads(text)
        encoded = json.dumps(result["admission_view"]).encode()
        self.assertLessEqual(len(encoded), admission_view.VIEW_BYTES)
        for private in PRIVATE:
            self.assertNotIn(private, encoded.decode(), private)
        return code, result

    def snapshot(self):
        root = self.record.parent
        return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}

    def implementer(self):
        self.intake(self.sub("S", boundary={"paths": ["src"]}))
        frozen = self.packet(["S"], boundary={"paths": ["src"]})
        started = self.start("implement", frozen)["admission"]
        return frozen, started

    def consume(self, started, frozen, **report):
        """Settle the worker, commit its result and consume its report with S restated waiting."""
        (self.project / "src" / "new.py").write_text("new\n")
        git(self.project, "add", "."); git(self.project, "commit", "-qm", "worker result")
        head = git(self.project, "rev-parse", "HEAD")
        self.settle(started)
        rows = self.stored()
        served = next(row for row in rows if row["id"] == "S")
        served.pop("executor", None); served.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        return head, self.report(started, frozen, result_commit=head, map={"obligations": rows}, **report)


class AdmissionViewTests(AdmissionViewCase):
    def test_validated_report_returns_every_documented_field(self):
        frozen, started = self.implementer()
        head, _ = self.consume(started, frozen)
        stored = read(self.project, "objective")["admissions"][started["admission_id"]]
        before = self.snapshot()
        code, result = self.view(started["admission_id"])
        self.assertEqual((code, result["status"]), (0, "observed"))
        item = result["admission_view"]
        binding = stored["native_binding"]
        self.assertEqual({key: item[key] for key in (
            "schema", "objective", "admission", "record", "found", "state", "role", "packet_id", "candidate",
            "assignment_digest", "run_id", "task_id", "dispatch_id", "unknown")}, {
            "schema": "pod-admission-view/v1", "objective": "objective", "admission": started["admission_id"],
            "record": "read", "found": True, "state": stored["state"], "role": "implement",
            "packet_id": frozen["packet_id"], "candidate": self.candidate,
            "assignment_digest": stored["recovery"]["assignment"], "run_id": "run", "task_id": "implement",
            "dispatch_id": binding["dispatchId"], "unknown": []})
        report = item["report"]
        self.assertEqual(report, {
            "consumed": True, "status": "validated_observation", "settled": True, "outcome": "succeeded",
            "assignment": frozen["packet_id"], "attempt": binding["dispatchId"], "candidate": self.candidate,
            "native_binding": {"runtime": stored["runtime"], "runId": binding["runId"], "taskId": binding["taskId"],
                               "dispatchId": binding["dispatchId"], "workerId": binding["workerId"]},
            "observation_digest": stored["report"]["observation_digest"], "result_commit": head,
            "consumed_at": stored["report"]["consumed_at"],
            "ingestion": {"status": "recorded", "base": self.base, "head": head,
                          "changed_path_count": 1, "boundary_exceeded_count": 0}})
        self.assertEqual(self.snapshot(), before)

    def test_reconciliation_required_report_reads_its_status(self):
        frozen, started = self.implementer()
        _, observation = self.consume(started, frozen, scope=["src", "docs"])
        self.assertEqual(observation["status"], "reconciliation_required")
        code, result = self.view(started["admission_id"])
        self.assertEqual((code, result["status"]), (0, "observed"))
        report = result["admission_view"]["report"]
        self.assertEqual((report["consumed"], report["status"], report["settled"], report["assignment"]),
                         (True, "reconciliation_required", True, frozen["packet_id"]))
        self.assertEqual(result["admission_view"]["unknown"], [])

    def test_admission_without_a_report_says_so(self):
        frozen, started = self.implementer()
        code, result = self.view(started["admission_id"])
        self.assertEqual((code, result["status"]), (0, "observed"))
        item = result["admission_view"]
        self.assertEqual(item["report"], {"consumed": False, "reason": admission_view.NO_REPORT})
        self.assertEqual((item["packet_id"], item["dispatch_id"]),
                         (frozen["packet_id"], started["native_binding"]["dispatchId"]))
        code, text = self.pod("--objective", "objective", "--admission", started["admission_id"])
        self.assertEqual(code, 0)
        self.assertIn("report: " + admission_view.NO_REPORT, text)

    def test_unreadable_record_reads_unknown_with_a_reason(self):
        frozen, started = self.implementer()
        self.record.write_bytes(b"{not json")
        code, result = self.view(started["admission_id"])
        self.assertEqual((code, result["status"]), (1, "unavailable"))
        item = result["admission_view"]
        self.assertEqual(item["record"], "unknown")
        for field in ("found", "state", "role", "packet_id", "candidate", "assignment_digest", "run_id",
                      "task_id", "dispatch_id", "report"):
            self.assertEqual(item[field], "unknown", field)
        self.assertEqual([(row["field"], row["reason"]) for row in item["unknown"]], [("record", "invalid_record")])


class AdmissionViewEdgeTests(AdmissionViewCase):
    def test_unknown_admission_and_objective_and_malformed_id(self):
        self.intake()
        code, result = self.view("a" * 64)
        self.assertEqual((code, result["status"], result["admission_view"]["found"]), (0, "absent", False))
        code, result = self.view("a" * 64, objective="missing")
        self.assertEqual((code, result["status"], result["blocker"]), (1, "blocked", "objective_unknown"))
        self.assertEqual(result["admission_view"]["record"], "absent")
        code, text = self.pod("--objective", "objective", "--admission", "not-an-id", "--json")
        self.assertEqual((code, json.loads(text)["error"]["code"]), (1, "invalid_admission"))

    def test_admission_needs_one_objective_and_no_run_or_list(self):
        for argv in (["--admission", "a" * 64], ["--objective", "o", "--run", "r", "--admission", "a" * 64],
                     ["--list", "--objective", "o", "--admission", "a" * 64]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                cli.main(["status", *argv])

    def test_malformed_recorded_fact_reads_unknown_by_field(self):
        """A recorded fact that is not a bounded identifier is named unknown, never copied."""
        projected = admission_view._project({"state": "bound", "role": "review", "packet_id": "x" * 600,
                                             "candidate": None, "recovery": {}, "run_id": "run", "task_id": "t",
                                             "native_binding": {"dispatchId": "d"}, "report": None})
        self.assertEqual((projected["packet_id"], projected["candidate"], projected["assignment_digest"]),
                         ("unknown", "unknown", "unknown"))
        self.assertEqual([row["field"] for row in projected["unknown"]],
                         ["packet_id", "candidate", "assignment_digest"])
        reserved = admission_view._project({"state": "reserved", "role": "review", "native_binding": None,
                                            "report": None})
        self.assertIsNone(reserved["dispatch_id"])


class AdmissionViewCorrectionTests(AdmissionViewCase):
    """The review's corrections: no record-derived text, only the shared read pin, bounded early responses and a
    reason for every unknown count."""

    def consumed(self):
        frozen, started = self.implementer()
        self.consume(started, frozen)
        return started["admission_id"]

    def edit(self, admission, change):
        raw = logical_json(self.record)
        change(raw["admissions"][admission])
        self.record.write_text(json.dumps(raw))

    def test_unreadable_record_never_carries_record_derived_text(self):
        frozen, started = self.implementer()
        sentinel = "PRIVATE_SOURCE_CONTENT_SENTINEL"
        for content in ({"schema": "pod-context/v4", sentinel: True}, {"schema": sentinel},
                        {**logical_json(self.record), sentinel: {"source": sentinel}}):
            self.record.write_text(json.dumps(content))
            code, text = self.pod("--objective", "objective", "--admission", started["admission_id"], "--json")
            self.assertEqual(code, 1)
            self.assertNotIn(sentinel, text)
            unknown = json.loads(text)["admission_view"]["unknown"]
            self.assertEqual(len(unknown), 1)
            self.assertEqual(unknown[0]["detail"], admission_view.RECORD_REASONS.get(
                unknown[0]["reason"], admission_view.RECORD_REASONS["record_unreadable"]))
        self.assertEqual(admission_view._unread("Not A Code; " + sentinel)["unknown"][0]["reason"], "record_unreadable")

    def test_only_the_shared_non_blocking_read_pin_is_taken(self):
        admission = self.consumed()
        calls, real = [], fcntl.flock
        def traced(fd, operation):
            calls.append(operation)
            return real(fd, operation)
        with patch("pod.history.fcntl.flock", side_effect=traced):
            code, result = self.view(admission)
        self.assertEqual((code, result["status"]), (0, "observed"))
        self.assertTrue(calls)
        self.assertLessEqual(set(calls), {fcntl.LOCK_SH | fcntl.LOCK_NB, fcntl.LOCK_UN})

    def test_objective_is_bounded_and_every_response_stays_within_the_limit(self):
        self.intake()
        code, text = self.pod("--objective", "x" * 9000, "--admission", "a" * 64, "--json")
        result = json.loads(text)
        self.assertEqual((code, result["error"]["code"]), (1, "invalid_objective"))
        self.assertNotIn("x" * 300, text)
        wide = "\u20ac" * admission_view.OBJECTIVE_LIMIT
        code, result = self.view("a" * 64, objective=wide)
        self.assertEqual((code, result["status"], result["admission_view"]["objective"]), (1, "blocked", wide))
        for name in ("bad\nname", ""):
            code, text = self.pod("--objective", name, "--admission", "a" * 64, "--json")
            self.assertEqual((code, json.loads(text)["error"]["code"]), (1, "invalid_objective"))
        oversized = {"schema": "pod-cli/v5", "status": "observed",
                     "admission_view": {"admission": "a" * 64, "padding": "y" * admission_view.VIEW_BYTES}}
        bounded = admission_view._bounded(oversized)
        self.assertEqual((bounded["status"], bounded["admission_view"]["unknown"][0]["reason"]),
                         ("unavailable", "view_limit"))
        self.assertLessEqual(len(json.dumps(bounded["admission_view"]).encode()), admission_view.VIEW_BYTES)

    def test_every_unknown_ingestion_count_names_its_reason(self):
        admission = self.consumed()
        self.edit(admission, lambda row: row.update(boundary_exceeded=None))
        code, result = self.view(admission)
        item = result["admission_view"]
        self.assertEqual((code, item["report"]["ingestion"]["boundary_exceeded_count"]), (0, "unknown"))
        self.assertEqual(item["unknown"], [{"field": "report.ingestion.boundary_exceeded_count", "reason": "absent"}])
        self.edit(admission, lambda row: (row.update(boundary_exceeded=[]),
                                          row["result"].update(paths_status="over_limit", path_count="bad")))
        code, result = self.view(admission)
        item = result["admission_view"]
        self.assertEqual((code, item["report"]["ingestion"]["status"], item["report"]["ingestion"]["changed_path_count"]),
                         (0, "over_limit", "unknown"))
        self.assertEqual(item["unknown"], [{"field": "report.ingestion.changed_path_count", "reason": "unreadable"}])
