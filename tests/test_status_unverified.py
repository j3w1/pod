"""A1: status never shows work it cannot verify as active; nothing else moves."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import types
import unittest
from unittest.mock import patch

from pod import status as current_status
from pod.errors import PodError
from pod.ledger import _outstanding_ids, kernel_view, objective_root, read
from pod.operations import OrcaPort
from tests.kernel_support import KernelCase
from tests import test_runtime_continuity as continuity

BASELINE = "f4101feb8e517382cb4cc626a1afc702c6429f37"  # Pod 0.6.6
REPOSITORY = Path(__file__).resolve().parents[1]


def baseline_status() -> types.ModuleType:
    """The 0.6.6 status module from genuine Git objects, joined to the current package."""
    source = subprocess.run(["git", "-C", str(REPOSITORY), "show", f"{BASELINE}:skills/pod/status.py"],
                            capture_output=True, text=True, check=True).stdout
    module = types.ModuleType("pod._status_066")
    module.__package__ = "pod"
    exec(compile(source, "status-0.6.6.py", "exec"), module.__dict__)
    return module


class UnverifiedStatusCase(KernelCase):
    """Finished Dispatches with consumed reports, then a changed Orca runtime."""

    def finished(self, count: int, reported: int) -> list[dict]:
        self.intake(*[self.sub(f"S{index}", boundary={"paths": [f"area{index}"]})
                      for index in range(1, count + 1)])
        admissions = []
        for index in range(1, count + 1):
            frozen = self.packet([f"S{index}"], boundary={"paths": [f"area{index}"]})
            admission = self.start(f"task-{index}", frozen)["admission"]
            if index <= reported:
                self.settle(admission)
                rows = self.stored()
                row = next(item for item in rows if item.get("executor") == admission["admission_id"])
                row.pop("executor")
                row.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
                self.report(admission, frozen, map={"obligations": rows})
            admissions.append(admission)
        for admission in admissions[reported:]:
            self.settle(admission)
        return admissions

    def observe(self, *, runtime="runtime", workers=None, rows_error=None, module=current_status):
        listed = {"runtime": runtime, "scope": {"source": "flag", "run": "run"},
                  "workers": workers or [], "complete": True}

        def rows(_run):
            if rows_error is not None:
                raise rows_error
            return listed
        output = StringIO()
        with patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            result = module.status(self.project, None, objective="objective",
                                   current_run_fn=lambda: {"run": {"id": "run"}, "runtime": runtime},
                                   worker_rows_fn=rows)
            with redirect_stdout(output):
                module.render(result)
        return json.loads(json.dumps(result)), output.getvalue()


class UnverifiedStatusTests(UnverifiedStatusCase):
    def test_unverified_rows_are_not_active_and_no_line_waits_for_delivery(self):
        admissions = self.finished(3, 1)
        self.port.runtime = "runtime-2"
        path = objective_root(self.project, "objective") / "context.json"
        before = path.read_bytes()
        result, rendered = self.observe(runtime="runtime-2")
        self.assertEqual(result["native_settlement"], "unverified")
        self.assertEqual(result["assignments"]["active"], [])
        self.assertEqual({row["admission"] for row in result["assignments"]["unverified"]},
                         {row["admission_id"] for row in admissions})
        self.assertIn("Assignments: 0 active, 0 settled, 3 unverified", rendered)
        for line in [*result["progress"], result["next_action"], rendered]:
            self.assertNotRegex(line, r"waiting for .*delivery")
        self.assertIn("native settlement is unverified", result["next_action"])
        self.assertEqual(path.read_bytes(), before)

    def test_failure_code_runtimes_and_orca_attention_are_named(self):
        admissions = self.finished(2, 0)
        self.port.runtime = "runtime-2"
        dispatch = admissions[0]["native_binding"]["dispatchId"]
        workers = [{"dispatchId": dispatch, "projection": {"attention": {
            "categories": ["input", "\x1bred"], "requiresAction": True}}}]
        result, rendered = self.observe(runtime="runtime-2", workers=workers)
        self.assertEqual(result["native_settlement_failure"],
                         {"code": "native_assignment_unverified", "bound_runtime": "runtime",
                          "current_runtime": "runtime-2"})
        rows = {row["dispatch"]: row for row in result["assignments"]["unverified"]}
        self.assertEqual(rows[dispatch]["orca_attention"],
                         {"categories": ["input", "\x1bred"], "requires_action": True})
        self.assertNotIn("attention", rows[dispatch])
        self.assertEqual(rows[admissions[1]["native_binding"]["dispatchId"]]["orca_attention"], "unknown")
        self.assertIn("Native settlement unverified: native_assignment_unverified; bound runtime runtime, "
                      "current runtime runtime-2", rendered)
        self.assertIn("Orca attention: input, red; Orca requires action", rendered)
        self.assertIn("Orca attention unknown", rendered)
        self.assertTrue(any("settlement unverified for implement" in line and "Orca attention" in line
                            for line in result["progress"]), result["progress"])
        unknown, _ = self.observe(runtime="runtime-2", rows_error=PodError("orca_unavailable", "offline"))
        self.assertEqual(unknown["native_settlement_failure"]["current_runtime"], "unknown")
        self.assertIn("native settlement is unverified", unknown["next_action"])

    def test_verified_read_matches_066_output_and_kernel_outstanding_is_unchanged(self):
        admissions = self.finished(3, 1)
        dispatch = admissions[2]["native_binding"]["dispatchId"]
        self.port.workers[dispatch]["outcome"] = "in_progress"
        workers = [{"dispatchId": dispatch, "projection": {"attention": {
            "categories": ["guidance"], "requiresAction": True}}}]
        baseline = baseline_status()
        expected, expected_render = self.observe(workers=workers, module=baseline)
        observed, observed_render = self.observe(workers=workers)
        self.assertNotIn("unverified", observed["assignments"])
        self.assertNotIn("native_settlement_failure", observed)
        self.assertEqual(observed, expected)
        self.assertEqual(observed_render, expected_render)
        self.assertEqual(len(observed["assignments"]["active"]), 1)
        # A mismatched runtime changes only presentation: the kernel still counts every open row.
        state = read(self.project, "objective")
        self.port.runtime = "runtime-2"
        with patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            view = kernel_view(self.project, "objective", state=state)
        self.assertEqual(view["settlement"], "unverified")
        self.assertEqual(sorted(view["ctx"]["outstanding"]), sorted(_outstanding_ids(state, None)))
        self.assertEqual(len(view["ctx"]["outstanding"]), 3)


class RecordedGenerationStatusTests(continuity.ContinuityCase):
    """A production-shaped checkpoint: the real authority join records the consumer generation."""

    def test_verified_read_with_a_recorded_generation_matches_066_without_a_continuity_field(self):
        self.started()
        self.assertEqual(read(self.project, "objective")["checkpoint"]["continuity"],
                         {"binding": {"run": "run", "coordinator": "owner", "generation": 1}, "history": []})

        def observe(module):
            output = StringIO()
            result = module.status(self.project, None, objective="objective",
                                   current_run_fn=lambda: {"run": {"id": "run"}, "runtime": "runtime"},
                                   worker_rows_fn=lambda _run: {"runtime": "runtime", "workers": [],
                                                                "scope": None, "complete": True})
            with redirect_stdout(output):
                module.render(result)
            return json.loads(json.dumps(result)), output.getvalue()
        observed, observed_render = observe(current_status)
        expected, expected_render = observe(baseline_status())
        self.assertEqual(observed["native_settlement"], "observed")
        self.assertNotIn("runtime_continuity", observed)
        self.assertEqual(observed, expected)
        self.assertEqual(observed_render, expected_render)


if __name__ == "__main__":
    unittest.main()
