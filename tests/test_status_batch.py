"""PES142: complete public full-status cohorts, paging and original read bounds."""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
from unittest.mock import patch

from pod import cli, listing, status as singular, status_batch as batch
from pod.errors import PodError
from pod.ledger import objective_root, read, state_root
from pod.operations import OrcaPort
from tests.kernel_support import KernelCase


BINDING = {"runtime": "runtime", "run": {"id": "run", "coordinator_handle": "owner", "consumer_generation": 1}}


def small_status(_project, _run, *, objective, **_kwargs):
    return {"schema": "pod-cli/v5", "status": "observed", "objective": objective,
            "readiness": {"state": "working"}}


class BatchCase(KernelCase):
    def setUp(self):
        super().setUp()
        self.listed = {"runtime": "runtime", "scope": {"source": "flag", "run": "run"},
                       "workers": [], "complete": True}

    def singular(self, project, run, **kwargs):
        return singular.status(project, run, current_run_fn=lambda: deepcopy(BINDING),
                               worker_rows_fn=lambda _run: deepcopy(self.listed), **kwargs)

    @contextmanager
    def ports(self, reader=None, binding=None, native_reader=None):
        with patch.object(batch, "status", side_effect=reader or self.singular), \
             patch.object(batch, "_binding", side_effect=binding or (lambda: deepcopy(BINDING))), \
             patch("pod.orca.read_command", side_effect=PodError("orca_unavailable", "fixture unavailable")), \
             patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=native_reader or
                          (lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs))):
            yield

    def many(self, count):
        """A genuine fixture writer's format; cheap copies need no native processes."""
        self.intake()
        value = read(self.project, "objective")
        original = objective_root(self.project, "objective") / "context.json"
        original.unlink()
        for index in range(count):
            name = f"item-{index:03}"
            row = deepcopy(value)
            row["checkpoint"]["objective"] = name
            root = objective_root(self.project, name)
            root.mkdir()
            (root / "context.json").write_text(json.dumps(row))

    def raw(self, name, value):
        root = objective_root(self.project, name)
        root.mkdir(exist_ok=True)
        (root / "context.json").write_text(value if isinstance(value, str) else json.dumps(value))
        return root


class BatchEquivalenceTests(BatchCase):
    def test_nonowner_terminal_retains_the_full_singular_window_report(self):
        self.many(2)

        def caller_native(_port, owner, **kwargs):
            return {**self.port.read_native(owner, **kwargs), "caller": os.environ["ORCA_TERMINAL_HANDLE"]}

        with patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "fixture-nonowner"}), \
             self.ports(native_reader=caller_native):
            expected = {name: self.singular(self.project, None, objective=name)
                        for name in ("item-000", "item-001")}
            result = batch.full_status(self.project)
        self.assertTrue(all(row["report"] is not None for row in result["records"]),
                        [row["failure"] for row in result["records"]])
        self.assertEqual({row["objective"]: row["report"] for row in result["records"]}, expected)
        self.assertTrue(all(row["failure"] is None for row in result["records"]))

    def test_every_full_report_equals_singular_including_closed_and_shared_run(self):
        self.intake()
        self.write([self.criterion("satisfied", evidence=[self.proof("O1")])], objective="closed",
                   governance={"base_ref": "target"}, close=True)
        paths = [objective_root(self.project, name) / "context.json" for name in ("objective", "closed")]
        before = [path.read_bytes() for path in paths]
        with self.ports():
            expected = {name: self.singular(self.project, None, objective=name) for name in ("objective", "closed")}
            result = batch.full_status(self.project)
        self.assertEqual(result["status"], "observed")
        self.assertTrue(result["complete"])
        self.assertTrue(result["cohort"]["enumeration_complete"])
        self.assertEqual({row["objective"]: row["report"] for row in result["records"]}, expected)
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_unknown_native_settlement_remains_unverified_and_not_active(self):
        self.intake()
        frozen = self.packet(["O1"])
        admission = self.start("fixture-work", frozen)["admission"]
        self.settle(admission)
        self.port.runtime = "runtime-other"
        with self.ports():
            expected = self.singular(self.project, None, objective="objective")
            result = batch.full_status(self.project)["records"][0]["report"]
        self.assertEqual(result, expected)
        self.assertEqual(result["native_settlement"], "unverified")
        self.assertEqual(result["assignments"]["active"], [])
        self.assertEqual(result["assignments"]["unverified"][0]["admission"], admission["admission_id"])

    def test_common_discovery_is_shared_but_each_objective_is_projected(self):
        self.many(3)
        with self.ports(), patch.object(singular, "load_config", wraps=singular.load_config) as load, \
             patch.object(singular, "running_identity", wraps=singular.running_identity) as identity, \
             patch.object(batch, "status", side_effect=self.singular) as projected:
            result = batch.full_status(self.project)
        self.assertEqual(len(result["records"]), 3)
        self.assertEqual(projected.call_count, 3)
        self.assertEqual((load.call_count, identity.call_count), (2, 2))  # Initial discovery and fresh final fence.

    def test_a_nonzero_full_json_report_is_retained_as_a_typed_failure(self):
        self.intake()
        value = read(self.project, "objective")
        value["checkpoint"]["schema"] = "pod-checkpoint/v9"
        value["checkpoint"]["objective"] = "earlier"
        self.raw("earlier", value)
        with self.ports():
            expected = self.singular(self.project, None, objective="earlier")
            result = batch.full_status(self.project)
        row = next(row for row in result["records"] if row["objective"] == "earlier")
        self.assertEqual(row["exit_code"], 1)
        self.assertEqual(row["outcome"], "failure")
        self.assertEqual(row["failure"]["code"], "state_unsupported")
        self.assertEqual(row["report"], expected)
        self.assertEqual(result["status"], "incomplete")


class BatchInventoryTests(BatchCase):
    def test_unreadable_oversized_and_unattributed_records_are_not_dropped(self):
        from pod.history import CONTEXT_LIMIT
        self.intake()
        broken = self.raw("broken", "truncated{")
        huge = self.raw("huge", " " * (CONTEXT_LIMIT + 1))
        orphan = state_root(self.project) / "orphan"
        orphan.mkdir()
        (orphan / "governor.json").write_text("{}")
        with self.ports(reader=small_status):
            result = batch.full_status(self.project)
        rows = {row["record"]: row for row in result["records"]}
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[broken.name]["failure"]["code"], "invalid_record")
        self.assertEqual(rows[huge.name]["failure"]["code"], "record_too_large")
        self.assertEqual(rows[orphan.name]["failure"]["code"], "unattributed")
        self.assertTrue(result["cohort"]["enumeration_complete"])
        self.assertEqual(result["status"], "incomplete")

    def test_foreign_repository_is_excluded_and_redirected_record_is_explicit(self):
        from pod.github import repository_context
        self.intake()
        value = read(self.project, "objective")
        foreign = state_root(self.project) / ("f" * 64)
        foreign.mkdir()
        (foreign / "context.json").write_text(json.dumps(value))
        redirect = state_root(self.project) / "redirect"
        redirect.symlink_to(objective_root(self.project, "objective"), target_is_directory=True)
        with self.ports(reader=small_status):
            result = batch.full_status(self.project)
        self.assertEqual({row["record"] for row in result["records"]},
                         {objective_root(self.project, "objective").name, "redirect"})
        self.assertEqual(next(row for row in result["records"] if row["record"] == "redirect")["failure"]["code"],
                         "unsafe_state")
        self.assertEqual(result["scope"]["repo_key"], repository_context(self.project)["repo_key"])

    def test_empty_repository_is_complete_and_scan_overflow_is_not(self):
        with self.ports(reader=small_status):
            empty = batch.full_status(self.project)
        self.assertTrue(empty["complete"])
        self.assertEqual(empty["records"], [])
        self.many(2)
        with self.ports(reader=small_status), patch.object(listing, "MAX_SCAN", 1):
            limited = batch.full_status(self.project)
        self.assertFalse(limited["complete"])
        self.assertFalse(limited["cohort"]["enumeration_complete"])
        self.assertFalse(limited["cohort"]["membership_complete"])
        self.assertIsNone(limited["cohort"]["total"])
        self.assertEqual((limited["status"], limited["reason"]), ("incomplete", "scan_limit"))

    def test_unknown_objective_read_is_a_typed_nonzero_report(self):
        self.intake()
        def unknown(project, run, **kwargs):
            return self.singular(project, run, **{**kwargs, "objective": "absent"})
        with self.ports(reader=unknown):
            result = batch.full_status(self.project)
        row = result["records"][0]
        self.assertEqual(row["failure"]["code"], "objective_unknown")
        self.assertEqual(row["report"]["objective"], "absent")
        self.assertEqual(row["exit_code"], 1)


class BatchPagingTests(BatchCase):
    def test_pages_cover_more_than_the_existing_summary_presentation_limit(self):
        self.many(listing.MAX_ROWS + 2)
        with self.ports(reader=small_status):
            first = batch.full_status(self.project)
            second = batch.full_status(self.project, cursor=first["page"]["next_cursor"])
        self.assertEqual((first["status"], second["status"]), ("page", "page"))
        self.assertEqual(first["cohort"], second["cohort"])
        self.assertEqual(second["page"]["offset"], listing.MAX_ROWS)
        rows = first["records"] + second["records"]
        self.assertEqual(len({row["record"] for row in rows}), listing.MAX_ROWS + 2)
        self.assertTrue(second["page"]["end"])
        self.assertIsNone(second["page"]["next_cursor"])
        self.assertFalse(first["complete"])
        self.assertFalse(second["complete"])

    def test_repeated_page_retains_its_offset_and_adds_no_coverage(self):
        self.many(3)
        with self.ports(reader=small_status):
            first = batch.full_status(self.project, page_size=1)
            second = batch.full_status(self.project, cursor=first["page"]["next_cursor"], page_size=1)
            repeated = batch.full_status(self.project, cursor=first["page"]["next_cursor"], page_size=1)
        self.assertEqual(second, repeated)
        self.assertEqual(second["page"]["offset"], 1)
        self.assertFalse(second["complete"])

    def test_changed_record_or_cohort_refuses_old_cursor_without_restart(self):
        self.many(3)
        with self.ports(reader=small_status):
            first = batch.full_status(self.project, page_size=1)
            root = objective_root(self.project, "item-002")
            value = json.loads((root / "context.json").read_text())
            value["revision"] += 1
            (root / "context.json").write_text(json.dumps(value))
            with self.assertRaises(PodError) as caught:
                batch.full_status(self.project, cursor=first["page"]["next_cursor"])
        self.assertEqual(caught.exception.code, "status_cohort_changed")

    def test_missing_page_does_not_claim_whole_cohort_completion(self):
        self.many(4)
        with self.ports(reader=small_status):
            first = batch.full_status(self.project, page_size=1)
            skipped = batch.full_status(self.project, cursor=batch._cursor(first["cohort"]["id"], 3))
        self.assertEqual(skipped["page"]["offset"], 3)
        self.assertFalse(skipped["complete"])
        self.assertEqual(skipped["status"], "page")

    def test_malformed_cursor_and_option_bounds_are_explicit(self):
        self.intake()
        with self.ports(reader=small_status):
            for cursor in ("bad!", "x" * (batch.CURSOR_BYTES + 1), batch._cursor("0" * 64, True)):
                with self.subTest(cursor=cursor[:20]), self.assertRaises(PodError) as caught:
                    batch.full_status(self.project, cursor=cursor)
                self.assertEqual(caught.exception.code, "status_cursor_invalid")
            for kwargs, code in (({"page_size": 0}, "status_page_size"),
                                 ({"page_size": listing.MAX_ROWS + 1}, "status_page_size"),
                                 ({"timeout": float("nan")}, "status_timeout"),
                                 ({"timeout": 31}, "status_timeout")):
                with self.subTest(kwargs=kwargs), self.assertRaises(PodError) as caught:
                    batch.full_status(self.project, **kwargs)
                self.assertEqual(caught.exception.code, code)


class BatchCurrencyTests(BatchCase):
    def test_page_invalidation_keeps_the_record_failure_reason(self):
        self.intake()
        path = objective_root(self.project, "objective") / "context.json"
        value = json.loads(path.read_text())
        value["checkpoint"]["schema"] = "pod-checkpoint/v9"
        path.write_text(json.dumps(value))

        def changed(*args, **kwargs):
            report = self.singular(*args, **kwargs)
            (path.parent / "governor.json").write_text("{}")
            return report

        with self.ports(reader=changed):
            result = batch.full_status(self.project)
        row = result["records"][0]
        self.assertEqual(row["failure"]["code"], "state_unsupported")
        self.assertEqual(row["page_failure"]["code"], "status_cohort_changed")
        self.assertEqual(row["report"]["blocker"], "state_unsupported")
        self.assertFalse(result["page"]["valid"])

    def test_record_changed_during_projection_invalidates_the_page(self):
        self.intake()
        path = objective_root(self.project, "objective") / "context.json"
        def changed(*args, **kwargs):
            value = json.loads(path.read_text())
            value["revision"] += 1
            path.write_text(json.dumps(value))
            return small_status(*args, **kwargs)
        with self.ports(reader=changed):
            result = batch.full_status(self.project)
        self.assertEqual(result["reason"], "status_cohort_changed")
        self.assertFalse(result["complete"])
        self.assertEqual(result["records"][0]["outcome"], "failure")
        self.assertIsNone(result["page"]["next_cursor"])

    def test_runtime_generation_or_caller_binding_change_invalidates_the_page(self):
        self.intake()
        changed = deepcopy(BINDING)
        changed["run"]["consumer_generation"] += 1
        with self.ports(reader=small_status, binding=[deepcopy(BINDING), changed]):
            result = batch.full_status(self.project)
        self.assertEqual(result["reason"], "status_cohort_changed")
        self.assertEqual(result["records"][0]["failure"]["code"], "status_cohort_changed")

    def test_governor_change_without_a_context_revision_invalidates_the_page(self):
        self.intake()
        path = objective_root(self.project, "objective") / "governor.json"
        def changed(*args, **kwargs):
            path.write_text("{}")
            return small_status(*args, **kwargs)
        with self.ports(reader=changed):
            result = batch.full_status(self.project)
        self.assertEqual(result["reason"], "status_cohort_changed")
        self.assertFalse(result["complete"])

    def test_preference_or_bundle_change_invalidates_common_discovery(self):
        self.intake()
        original = singular.StatusInputs.read
        calls = []
        def changed(project):
            value = original(project)
            calls.append(value)
            if len(calls) == 2:
                return singular.StatusInputs(value.project, value.preferences,
                                             {**value.running, "bundle_digest": "b" * 64}, value.routes)
            return value
        with self.ports(reader=small_status), patch.object(singular.StatusInputs, "read", side_effect=changed):
            result = batch.full_status(self.project)
        self.assertEqual(result["reason"], "status_cohort_changed")
        self.assertFalse(result["complete"])

    def test_changed_context_between_pages_is_not_silently_read_as_current(self):
        self.many(2)
        with self.ports(reader=small_status):
            first = batch.full_status(self.project, page_size=1)
        changed = deepcopy(BINDING)
        changed["runtime"] = "other-runtime"
        with self.ports(reader=small_status, binding=lambda: changed), self.assertRaises(PodError) as caught:
            batch.full_status(self.project, cursor=first["page"]["next_cursor"])
        self.assertEqual(caught.exception.code, "status_cohort_changed")


class BatchFailureTests(BatchCase):
    def test_budget_uses_the_existing_reserved_time_protocol(self):
        from pod.orca import DEADLINE_SPENT
        with patch.object(batch.time, "monotonic", return_value=0.0):
            budget = batch.ReadBudget(10)
            budget.check(need=9)
            with self.assertRaises(PodError) as caught:
                budget.check(need=10)
        self.assertEqual(caught.exception.code, DEADLINE_SPENT)

    def test_late_full_json_is_not_a_timely_success(self):
        self.intake()
        clock = [0.0]
        def late(*args, **kwargs):
            clock[0] = 11.0
            return small_status(*args, **kwargs)
        with self.ports(reader=late), patch.object(batch.time, "monotonic", side_effect=lambda: clock[0]):
            result = batch.full_status(self.project, timeout=10)
        self.assertEqual(result["reason"], "status_deadline")
        self.assertEqual(result["records"][0]["exit_code"], 1)
        self.assertFalse(result["complete"])

    def test_existing_outer_deadline_is_not_restarted_by_a_larger_request(self):
        self.intake()
        from pod.orca import _DEADLINE, read_deadline
        clock, limits = [0.0], []
        def late(*args, **kwargs):
            limits.append(_DEADLINE.get().limit(10))
            clock[0] = 2.0
            return small_status(*args, **kwargs)
        with self.ports(reader=late), patch.object(batch.time, "monotonic", side_effect=lambda: clock[0]):
            parent = batch.ReadBudget(1)
            with read_deadline(parent):
                result = batch.full_status(self.project, timeout=30)
        self.assertEqual(limits, [1.0])
        self.assertEqual(result["reason"], "status_deadline")

    def test_full_reports_page_before_output_bound_and_unicode_uses_bytes(self):
        self.many(2)
        def large(*args, **kwargs):
            return {**small_status(*args, **kwargs), "payload": "x" * 2600}
        with self.ports(reader=large), patch.object(batch, "MAX_OUTPUT", 5000):
            first = batch.full_status(self.project)
            second = batch.full_status(self.project, cursor=first["page"]["next_cursor"])
        self.assertEqual(len(first["records"]), 1)
        self.assertEqual(len(second["records"]), 1)
        self.assertEqual(first["records"][0]["report"]["payload"], "x" * 2600)
        self.assertLessEqual(batch._bytes(first), 5000)
        def unicode(*args, **kwargs):
            return {**small_status(*args, **kwargs), "payload": "é" * 3000}
        with self.ports(reader=unicode), patch.object(batch, "MAX_OUTPUT", 5000):
            result = batch.full_status(self.project)
        self.assertTrue(all(row["failure"]["code"] == "status_report_too_large" for row in result["records"]))

    def test_partial_or_exceptional_projection_has_a_typed_outcome(self):
        self.many(2)
        def invalid(_project, _run, *, objective, **_kwargs):
            if objective == "item-000":
                return {"schema": "pod-cli/v5", "status": "observed"}
            raise ValueError("fixture parse error")
        with self.ports(reader=invalid):
            result = batch.full_status(self.project)
        self.assertEqual([row["failure"]["code"] for row in result["records"]],
                         ["status_contract", "status_read_failed"])
        self.assertEqual(result["records"][1]["failure"]["error_type"], "ValueError")
        self.assertEqual(result["status"], "incomplete")

    def test_unencodable_json_or_unexpected_record_error_does_not_erase_siblings(self):
        self.many(3)
        def invalid(*args, **kwargs):
            if kwargs["objective"] == "item-000":
                return {**small_status(*args, **kwargs), "payload": "\ud800"}
            if kwargs["objective"] == "item-001":
                raise KeyError("fixture malformed field")
            return small_status(*args, **kwargs)
        with self.ports(reader=invalid):
            result = batch.full_status(self.project)
        self.assertEqual(len(result["records"]), 3)
        self.assertEqual([row["outcome"] for row in result["records"]], ["failure", "failure", "status"])
        self.assertEqual([row["failure"]["error_type"] for row in result["records"][:2]],
                         ["UnicodeEncodeError", "KeyError"])
        json.dumps(result, ensure_ascii=False).encode("utf-8")


class BatchCliTests(BatchCase):
    def test_public_cli_outputs_complete_json_and_preserves_nonzero_failure(self):
        self.intake()
        old = Path.cwd()
        try:
            os.chdir(self.project)
            for reader, expected in ((small_status, 0), (lambda *_args, **_kwargs: None, 1)):
                out = StringIO()
                with self.ports(reader=reader), redirect_stdout(out):
                    code = cli.main(["status", "--all", "--json"])
                result = json.loads(out.getvalue())
                self.assertEqual(code, expected)
                self.assertEqual(result["schema"], "pod-status-batch/v1")
                self.assertEqual(len(result["records"]), 1)
        finally:
            os.chdir(old)

    def test_mixed_or_incomplete_selectors_refuse_before_a_read(self):
        for args in (("--all",), ("--all", "--list", "--json"),
                     ("--all", "--objective", "x", "--json"),
                     ("--all", "--worktree", ".", "--json"),
                     ("--cursor", "x", "--json"), ("--page-size", "1", "--json")):
            with self.subTest(args=args), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                cli.main(["status", *args])
