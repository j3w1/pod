"""Suite lanes: classification, deterministic plans, exact-once accounting, gates and refresh."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from tools import suite_lanes as sl

ROOT = Path(__file__).resolve().parents[1]


def manifest(**changes) -> dict:
    value = {"schema": sl.SCHEMA,
             "source": {"kind": "estimate"},
             "budgets": {"record_at_seconds": 1.0, "unrecorded_max_seconds": 2.0, "drift_ratio": 2.0,
                         "drift_slack_seconds": 10, "test_max_seconds": 90, "max_parallel_shards": 6},
             "fast_mean_seconds": 0.25,
             "lanes": {"parallel": {"shards": 2, "workers": 2, "target_seconds": 180},
                       "serial": {"shards": 1, "workers": 1, "target_seconds": 180},
                       "installer": {"shards": 1, "workers": 2, "target_seconds": 180}},
             "overrides": {}, "durations": {}}
    value.update(changes)
    return value


class ManifestTests(unittest.TestCase):
    def test_the_valid_shape_has_no_problem_and_each_broken_part_is_named(self):
        self.assertEqual(sl.manifest_problems(manifest()), [])
        broken = {
            "schema": dict(manifest(), schema="other"),
            "budgets": manifest(budgets={"record_at_seconds": 1.0}),
            "ordering": manifest(budgets=dict(manifest()["budgets"], unrecorded_max_seconds=0.5)),
            "serial workers": manifest(lanes=dict(manifest()["lanes"],
                                                  serial={"shards": 1, "workers": 2, "target_seconds": 1})),
            "owner cap": manifest(lanes=dict(manifest()["lanes"],
                                             parallel={"shards": 7, "workers": 4, "target_seconds": 1})),
            "reason": manifest(overrides={"tests.m.C": {"lane": "serial", "reason": " "}}),
            "duration": manifest(durations={"tests.m.C.test_a": 0}),
            "source": manifest(source={"kind": "guess"}),
            "fast mean": manifest(fast_mean_seconds=0),
            "extra": dict(manifest(), tags=[]),
        }
        for name, value in broken.items():
            with self.subTest(name=name):
                self.assertTrue(sl.manifest_problems(value))

    def test_the_checked_in_manifest_is_valid_and_owner_capped(self):
        value = json.loads((ROOT / "tests" / "suite_lanes.json").read_text())
        self.assertEqual(sl.manifest_problems(value), [])
        self.assertLessEqual(value["lanes"]["parallel"]["shards"], value["budgets"]["max_parallel_shards"])
        self.assertEqual(sl.dump_manifest(value), (ROOT / "tests" / "suite_lanes.json").read_text())
        import re
        rows = re.findall(r'\{python: "([\d.]+)", lane: (\w+), shard: (\d+), of: (\d+)\}',
                          (ROOT / ".github/workflows/ci.yml").read_text())
        # Every lane share runs once on each supported interpreter, with identical lanes and shards.
        expected = [(python, lane, str(shard), str(config["shards"]))
                    for python in ("3.13", "3.14")
                    for lane, config in value["lanes"].items() for shard in range(1, config["shards"] + 1)]
        self.assertEqual(sorted(rows), sorted(expected))

    def test_the_hosted_workflow_keeps_strict_forbid_skips_on_every_interpreter_row(self):
        text = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn('python-version: ${{ matrix.python }}', text)
        self.assertIn('--strict --forbid-skips', text)
        self.assertEqual(text.count('--strict --forbid-skips'), 1)



SOURCES = {
    "tests.pty_harness": "", "tests.install_support": "",
    "tests.helper": "from tests.pty_harness import PtyCase\n",
    "tests.test_pty": "from tests.pty_harness import PtyCase\nclass A:\n    def test_a(self): pass\n",
    "tests.test_install": "import unittest\nclass B(unittest.TestCase):\n    def test_b(self):\n"
                          "        from tests.install_support import sandbox\n",
    "tests.test_both": "from tests import install_support, pty_harness\nclass C:\n    def test_c(self): pass\n",
    "tests.test_through_helper": "from tests.helper import thing\nclass D:\n    def test_d(self): pass\n",
    "tests.test_plain": textwrap.dedent("""\
        import time
        class E:
            def helper(self):
                started = time.monotonic()
                return time.monotonic() - started
            def test_bounded(self):
                self.assertLess(self.helper(), 1.5)
            def test_unbounded(self):
                self.assertEqual(1, 1)
        class F:
            def test_inline(self):
                started = time.perf_counter()
                self.assertLessEqual(time.perf_counter() - started, 2)
            def test_value(self):
                self.assertLess(1, 2)
        """),
}
IDS = ["tests.test_pty.A.test_a", "tests.test_install.B.test_b", "tests.test_both.C.test_c",
       "tests.test_through_helper.D.test_d", "tests.test_plain.E.test_bounded", "tests.test_plain.E.test_unbounded",
       "tests.test_plain.F.test_inline", "tests.test_plain.F.test_value"]


class GroupGuardTests(unittest.TestCase):
    def test_a_group_method_may_only_run_its_group(self):
        from tests.common import GroupedTable, assert_grouped_table

        class Honest(GroupedTable):
            GROUPS = {"test_first": ("a",), "test_second": ("b",)}
            def test_first(self): self.rows()
            def test_second(self): self.rows()

        class Hollow(Honest):
            GROUPS = Honest.GROUPS
            def test_first(self): self.rows()
            def test_second(self): pass

        class Elsewhere(Honest):
            GROUPS = Honest.GROUPS
            def test_first(self): self.rows()
            def test_second(self): self.doCleanups()

        assert_grouped_table(self, Honest, ("a", "b"), limit=1)
        for table in (Hollow, Elsewhere):
            with self.subTest(table=table.__name__), self.assertRaises(AssertionError):
                assert_grouped_table(self, table, ("a", "b"), limit=1)



class ClassificationTests(unittest.TestCase):
    def classify(self, **changes):
        with tempfile.TemporaryDirectory() as root:
            return sl.classify(Path(root), IDS, manifest(**changes), sources=SOURCES)

    def test_harness_use_routes_lanes_directly_inside_functions_and_through_helpers(self):
        lanes = self.classify()["lanes"]
        self.assertEqual(lanes["tests.test_pty.A.test_a"], "serial")
        self.assertEqual(lanes["tests.test_install.B.test_b"], "installer")
        self.assertEqual(lanes["tests.test_both.C.test_c"], "serial")
        self.assertEqual(lanes["tests.test_through_helper.D.test_d"], "serial")
        self.assertEqual(lanes["tests.test_plain.F.test_value"], "parallel")

    def test_wall_clock_bounds_must_be_classified_and_overrides_take_precedence(self):
        unclassified = self.classify()["timing_unclassified"]
        self.assertEqual(unclassified, ["tests.test_plain.E.test_bounded", "tests.test_plain.E.test_unbounded"][:1]
                         + ["tests.test_plain.F.test_inline"])
        overrides = {"tests.test_plain.E": {"lane": "serial", "reason": "deadline"},
                     "tests.test_plain.F.test_inline": {"lane": "parallel", "reason": "wide headroom"},
                     "tests.test_pty": {"lane": "parallel", "reason": "module override"}}
        classified = self.classify(overrides=overrides)
        self.assertEqual(classified["timing_unclassified"], [])
        self.assertEqual(classified["lanes"]["tests.test_plain.E.test_unbounded"], "serial")
        self.assertEqual(classified["lanes"]["tests.test_plain.F.test_inline"], "parallel")
        self.assertEqual(classified["lanes"]["tests.test_pty.A.test_a"], "parallel")


class PlanTests(unittest.TestCase):
    def test_packing_is_longest_first_deterministic_and_keeps_discovery_order(self):
        ids = ["tests.m.C.test_e", "tests.m.C.test_d", "tests.m.C.test_c", "tests.m.C.test_b", "tests.m.C.test_a"]
        seconds = {"tests.m.C.test_a": 5, "tests.m.C.test_b": 4, "tests.m.C.test_c": 3,
                   "tests.m.C.test_d": 3, "tests.m.C.test_e": 1}
        bins = sl.pack(ids, 2, seconds.__getitem__)
        # a(5) and b(4) open the bins, c joins b (4 < 5), d joins a (5 < 7), e joins b (7 < 8).
        self.assertEqual(bins, [["tests.m.C.test_d", "tests.m.C.test_a"],
                                ["tests.m.C.test_e", "tests.m.C.test_c", "tests.m.C.test_b"]])
        self.assertEqual(bins, sl.pack(list(ids), 2, seconds.__getitem__))
        self.assertEqual(sorted(sum(bins, [])), sorted(ids))

    def test_every_test_lands_in_exactly_one_worker_and_the_digest_follows_the_plan(self):
        ids = [f"tests.m.C.test_{index:02d}" for index in range(23)]
        lanes = {test_id: ("serial" if test_id.endswith("0") else "parallel") for test_id in ids}
        value = manifest(durations={ids[1]: 30.0, ids[2]: 12.5})
        plan = sl.build_plan(ids, lanes, value)
        placed = [test_id for lane in sl.LANES for group in plan[lane]["bins"] for test_id in group]
        self.assertEqual(sorted(placed), sorted(ids))
        self.assertEqual(len(plan["parallel"]["bins"]), 4)
        self.assertEqual(sl.plan_digest(plan), sl.plan_digest(sl.build_plan(ids, lanes, deepcopy(value))))
        value["durations"][ids[3]] = 40.0
        self.assertNotEqual(sl.plan_digest(plan), sl.plan_digest(sl.build_plan(ids, lanes, value)))
        self.assertEqual([len(group) for group in sl.shard_bins(plan, "parallel", 2)],
                         [len(group) for group in plan["parallel"]["bins"][2:4]])

    def test_check_names_each_drift_with_its_fix(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "tests" / "incidents").mkdir(parents=True)
            (root / "tests" / "incidents" / "test_quiet.py").write_text("")
            ids = ["tests.m.C.test_a", "tests.m.C.test_b"]
            lanes = dict.fromkeys(ids, "parallel")
            value = manifest(durations={"tests.m.C.test_a": 400.0, "tests.gone.C.test_x": 3.0},
                             overrides={"tests.gone": {"lane": "serial", "reason": "removed"}})
            plan = sl.build_plan(ids, lanes, value)
            findings = sl.check(root, value, ids, lanes, ["tests.m.C.test_b"], plan)
        text = "\n".join(findings)
        self.assertIn("STALE override tests.gone", text)
        self.assertIn("STALE duration tests.gone.C.test_x", text)
        self.assertIn("CEILING tests.m.C.test_a", text)
        self.assertIn("GroupedTable", text)
        self.assertIn("TIMING tests.m.C.test_b", text)
        self.assertIn("INCIDENTS tests/incidents/test_quiet.py", text)
        self.assertIn("PROJECTION parallel share 400.0 s > 180 s", text)
        self.assertIn("set lanes.parallel.shards to 3 and add its ci.yml include row", text)
        self.assertIn("PROJECTION serial shards [1] have no tests", text)
        self.assertTrue(all("refresh --prune" in item for item in findings if item.startswith("STALE")))


def events(*items):
    return [dict(item) for item in items]


class AccountingTests(unittest.TestCase):
    def account(self, ids, items, *, forbid_skips=False):
        return sl.account(ids, events(*items), name="parallel-1", exit_note="exited 1", forbid_skips=forbid_skips)

    def test_each_assigned_test_runs_exactly_once(self):
        ids = ["tests.m.C.test_a", "tests.m.C.test_b", "tests.m.C.test_c", "tests.m.C.test_d"]
        result = self.account(ids, [
            {"event": "start", "id": ids[0]}, {"event": "stop", "id": ids[0], "outcome": "success", "seconds": 0.1},
            {"event": "start", "id": ids[1]}, {"event": "stop", "id": ids[1], "outcome": "expected_failure",
                                                "seconds": 0.1},
            {"event": "start", "id": ids[1]}, {"event": "start", "id": "tests.m.C.test_z"},
            {"event": "start", "id": ids[3]}])
        text = "\n".join(result["problems"])
        self.assertIn("DUPLICATE tests.m.C.test_b ran 2 times", text)
        self.assertIn("NOT RUN 1 test(s) in parallel-1 (exited 1): tests.m.C.test_c", text)
        self.assertIn("UNASSIGNED tests.m.C.test_z", text)
        self.assertIn("IN FLIGHT tests.m.C.test_d: worker parallel-1 exited 1 during it", text)
        self.assertEqual(result["results"][ids[0]], {"outcome": "success", "seconds": 0.1})
        self.assertNotIn("test_b ", text.replace("DUPLICATE tests.m.C.test_b", ""))

    def test_a_class_fixture_names_its_cause_once_and_skips_are_policy(self):
        ids = ["tests.m.C.test_a", "tests.m.C.test_b", "tests.m.D.test_c"]
        fixture_error = {"event": "fixture", "id": "setUpClass (tests.m.C)", "outcome": "error",
                         "detail": "Traceback\nRuntimeError: boom"}
        fixture_skip = {"event": "fixture", "id": "setUpClass (tests.m.D)", "outcome": "skip", "detail": "deps"}
        result = self.account(ids, [fixture_error, fixture_skip])
        self.assertEqual(result["problems"], ["NOT RUN 2 test(s): setUpClass (tests.m.C) reported error: "
                                              "RuntimeError: boom"])
        self.assertEqual(result["results"]["tests.m.D.test_c"]["outcome"], "skip")
        strict = self.account(ids, [fixture_error, fixture_skip], forbid_skips=True)
        self.assertTrue(any(item.startswith("SKIPPED 1 test(s)") for item in strict["problems"]))

    def test_a_worker_must_finish_cleanly_after_its_last_test(self):
        ids = ["tests.m.C.test_a"]
        ran = [{"event": "start", "id": ids[0]}, {"event": "stop", "id": ids[0], "outcome": "success", "seconds": 0.1}]
        clean = sl.account(ids, ran + [{"event": "done", "ok": True}], name="w", exit_note="exited 0",
                           forbid_skips=True, exit_code=0)
        self.assertEqual((clean["problems"], clean["ran"]), ([], 1))
        for events_, code in ((ran, 1), (ran, 0), (ran + [{"event": "done", "ok": True}], -11)):
            with self.subTest(code=code, done=len(events_) > 2):
                problems = sl.account(ids, events_, name="w", exit_note=f"exited {code}", forbid_skips=True,
                                      exit_code=code)["problems"]
                self.assertEqual(len(problems), 1)
                self.assertTrue(problems[0].startswith(f"WORKER w exited {code} without a clean finish"))

    def test_failed_outcomes_are_findings_and_passing_ones_are_not(self):
        ids = [f"tests.m.C.test_{outcome}" for outcome in ("failure", "error", "unexpected_success",
                                                             "expected_failure", "skip")]
        items = []
        for test_id in ids:
            items += [{"event": "start", "id": test_id},
                      {"event": "stop", "id": test_id, "outcome": test_id.rsplit("_", 1)[-1]
                       if not test_id.endswith(("unexpected_success", "expected_failure"))
                       else test_id.split("test_", 1)[1], "seconds": 0.0}]
        problems = self.account(ids, items)["problems"]
        self.assertEqual(problems, ["ERROR tests.m.C.test_error", "FAILURE tests.m.C.test_failure",
                                    "UNEXPECTED SUCCESS tests.m.C.test_unexpected_success"])


class GateTests(unittest.TestCase):
    def test_limits_and_messages(self):
        value = manifest(durations={"tests.m.C.test_recorded": 3.0})
        self.assertEqual(sl.violation(value, "tests.m.C.test_new", 2.5), "UNRECORDED SLOW")
        self.assertIsNone(sl.violation(value, "tests.m.C.test_new", 1.9))
        self.assertIsNone(sl.violation(value, "tests.m.C.test_recorded", 12.9))
        self.assertEqual(sl.violation(value, "tests.m.C.test_recorded", 13.1), "DRIFT")
        self.assertEqual(sl.violation(value, "tests.m.C.test_recorded", 95), "CEILING")
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "4242"}):
            message = sl.gate_message("UNRECORDED SLOW", "tests.m.C.test_new", 2.5, 2.4, value)
        self.assertIn("refresh --run 4242", message)
        self.assertIn("took 2.5 s (2.4 s alone)", message)
        self.assertIn("GroupedTable", sl.gate_message("CEILING", "tests.m.C.test_new", 95, 95, value))

    def test_new_slow_tests_are_timed_alone_before_drift(self):
        value = manifest(durations={f"tests.m.C.test_drift_{index}": 10.0 for index in range(6)})
        results = {f"tests.m.C.test_drift_{index}": {"outcome": "success", "seconds": 40.0 + index}
                   for index in range(6)}
        results["tests.m.C.test_new"] = {"outcome": "success", "seconds": 4.0}
        results["tests.m.C.test_huge"] = {"outcome": "success", "seconds": 95.0}
        ranked = sl.ranked_candidates(results, value)
        self.assertEqual([item[:2] for item in ranked[:3]], [("tests.m.C.test_huge", "CEILING"),
                                                             ("tests.m.C.test_new", "UNRECORDED SLOW"),
                                                             ("tests.m.C.test_drift_5", "DRIFT")])

    def test_past_the_cap_new_slow_tests_fail_and_drift_warns(self):
        value = manifest(durations={f"tests.m.C.test_drift_{index}": 10.0 for index in range(3)})
        candidates = ([(f"tests.m.C.test_new_{index}", "UNRECORDED SLOW", 4.0 - index / 10) for index in range(6)]
                      + [(f"tests.m.C.test_drift_{index}", "DRIFT", 40.0) for index in range(3)])
        timed = []

        def alone(test_id, seconds):  # Every test is within limits alone: pure contention.
            timed.append(test_id)
            return 0.5

        problems, warnings = sl.judge(candidates, value, True, alone)
        self.assertEqual(timed, [f"tests.m.C.test_new_{index}" for index in range(sl.SOLO_LIMIT)])
        self.assertEqual(problems, [sl.gate_message("UNRECORDED SLOW", "tests.m.C.test_new_5", 3.5, None, value)
                                    + " [not timed alone]"])
        self.assertEqual(sum("[not timed alone]" in item for item in warnings), 3)
        self.assertEqual(sum("runner contention" in item for item in warnings), sl.SOLO_LIMIT)
        relaxed, _ = sl.judge(candidates, value, False, alone)
        self.assertEqual(relaxed, [])


def timing_body(lane, shard, of, seconds, *, part=1, parts=1, plan="p", fast=(10, 2.0)):
    return {"schema": sl.TIMINGS_SCHEMA, "lane": lane, "shard": shard, "of": of, "plan": plan, "sha": "commit",
            "workers": 1 if lane == "serial" else 2, "part": part, "parts": parts,
            "planned": len(seconds) + fast[0], "ran": len(seconds) + fast[0], "skips": 0, "fixture_errors": 0,
            "wall": 20.0, "fast": {"count": fast[0], "seconds": fast[1]}, "seconds": seconds}


class RefreshTests(unittest.TestCase):
    def test_timing_lines_round_trip_through_a_hosted_log(self):
        value = manifest(durations={"tests.m.C.test_recorded": 3.0})
        results = {f"tests.m.C.test_{index:03d}": {"outcome": "success", "seconds": 0.6} for index in range(230)}
        results["tests.m.C.test_recorded"] = {"outcome": "success", "seconds": 0.1}
        results["tests.m.C.test_fast"] = {"outcome": "success", "seconds": 0.2}
        results["tests.m.C.test_failed_slow"] = {"outcome": "failure", "seconds": 9.0}
        value["durations"]["tests.m.C.test_not_run"] = 4.0
        results["tests.m.C.test_not_run"] = {"outcome": "error", "seconds": 0.0}
        lines = sl.timing_lines(lane="parallel", shard=1, of=2, workers=2, digest="d", results=results,
                                manifest=value, wall=12.0, skips=0, fixture_errors=0, ran=233)
        self.assertEqual(len(lines), 3)
        log = "\n".join(f"tests (parallel 1/2)\tRouted suite share\t2026-10-12T00:00:00.0Z {line}" for line in lines)
        bodies = sl.parse_timings(log + "\nunrelated log noise\n")
        with self.assertRaisesRegex(sl.Finding, "malformed"):
            sl.parse_timings(log + "\nnoise POD_TEST_TIMINGS {broken\n")
        self.assertEqual([body["part"] for body in bodies], [1, 2, 3])
        merged = {key: item for body in bodies for key, item in body["seconds"].items()}
        self.assertEqual(len(merged), 231)
        self.assertNotIn("tests.m.C.test_fast", merged)
        self.assertFalse({"tests.m.C.test_failed_slow", "tests.m.C.test_not_run"} & set(merged))
        self.assertEqual(bodies[0]["fast"], {"count": 1, "seconds": 0.2})
        self.assertEqual((bodies[0]["planned"], bodies[0]["ran"]), (234, 233))

    def test_a_two_interpreter_run_refreshes_from_the_primary_interpreter_only(self):
        legs = [dict(body, python=python) for python in ("3.13", "3.14") for body in measured_bodies()]
        log = "\n".join(sl.MARKER + json.dumps(body) for body in legs)
        kept = sl.parse_timings(log)
        self.assertEqual({body["python"] for body in kept}, {"3.13"})
        self.assertEqual(len(kept), len(legs) // 2)
        sl.validated_legs(kept)  # Both interpreters together would repeat every test id.
        with self.assertRaises(sl.Finding):
            sl.validated_legs(legs)
        earlier = [dict(body) for body in measured_bodies()]
        self.assertEqual(len(sl.parse_timings("\n".join(sl.MARKER + json.dumps(body) for body in earlier))),
                         len(earlier))
        with self.assertRaisesRegex(sl.Finding, "no 3.13"):
            sl.parse_timings("\n".join(sl.MARKER + json.dumps(dict(body, python=python))
                                       for body in measured_bodies() for python in ("3.14", "3.15")))
        lines = sl.timing_lines(lane="serial", shard=1, of=1, workers=1, digest="d", results={},
                                manifest=manifest(), wall=1.0, skips=0, fixture_errors=0)
        self.assertEqual(sl.parse_timings(lines[0])[0]["python"], "%d.%d" % sys.version_info[:2])

    def test_merge_records_slow_tests_drops_fast_ones_and_ignores_noise(self):
        value = manifest(lanes={"parallel": {"shards": 1, "workers": 2, "target_seconds": 180},
                                "serial": {"shards": 1, "workers": 1, "target_seconds": 180},
                                "installer": {"shards": 1, "workers": 2, "target_seconds": 180}},
                         durations={"tests.m.C.test_steady": 10.0, "tests.m.C.test_moved": 10.0,
                                    "tests.m.C.test_now_fast": 4.0, "tests.m.C.test_absent": 5.0})
        bodies = [timing_body("parallel", 1, 1, {"tests.m.C.test_steady": 12.4, "tests.m.C.test_moved": 13.0,
                                                 "tests.m.C.test_now_fast": 0.5, "tests.m.C.test_new": 1.04,
                                                 "tests.m.C.test_nearly": 0.9}),
                  timing_body("serial", 1, 1, {}, fast=(5, 0.5)), timing_body("installer", 1, 1, {}, fast=(5, 0.5))]
        updated, changes = sl.merge_timings(value, bodies)
        self.assertEqual(updated["durations"], {"tests.m.C.test_steady": 10.0, "tests.m.C.test_moved": 13.0,
                                                "tests.m.C.test_new": 1.0, "tests.m.C.test_absent": 5.0})
        self.assertEqual(updated["fast_mean_seconds"], 0.15)
        self.assertEqual(updated["budgets"], value["budgets"])
        self.assertIn("- tests.m.C.test_now_fast 4.0 (now 0.50)", changes)
        self.assertEqual(value["durations"]["tests.m.C.test_now_fast"], 4.0)

    def test_merge_refuses_incomplete_or_mixed_runs(self):
        value = manifest()
        full = [timing_body(lane, shard, config["shards"], {}) for lane, config in value["lanes"].items()
                for shard in range(1, config["shards"] + 1)]
        self.assertEqual(sl.merge_timings(value, full)[0]["durations"], {})
        cases = {"no lines": [], "missing leg": full[1:],
                 "mixed plans": full[:-1] + [dict(full[-1], plan="other")],
                 "missing part": full[:-1] + [dict(full[-1], parts=2)]}
        for name, bodies in cases.items():
            with self.subTest(name=name), self.assertRaises(sl.Finding):
                sl.merge_timings(value, bodies)


def measured_bodies(value=None, *, ratio=2.0):
    value = value or manifest()
    return [dict(timing_body(lane, shard, config["shards"], {}),
                 workers=config["workers"], wall=100 * ratio, planned_worker_seconds=100.0, clean=True)
            for lane, config in value["lanes"].items() for shard in range(1, config["shards"] + 1)]


class HostedProjectionTests(unittest.TestCase):
    def test_same_run_ratio_changes_target_check_without_changing_worker_estimates(self):
        ids = [f"tests.m.C.test_{i}" for i in range(4)]
        value = manifest(durations=dict.fromkeys(ids, 100.0))
        lanes = dict.fromkeys(ids, "parallel")
        plan = sl.build_plan(ids, lanes, value)
        gap, _ = sl.merge_timings(value, measured_bodies(value, ratio=2.1))
        steady, _ = sl.merge_timings(value, measured_bodies(value, ratio=1.0))
        with tempfile.TemporaryDirectory() as root:
            gap_findings = sl.check(Path(root), gap, ids, lanes, [], plan)
            steady_findings = sl.check(Path(root), steady, ids, lanes, [], plan)
        self.assertTrue(any("share 210.0 s > 180 s" in item and "shards to 3" in item
                            for item in gap_findings))
        self.assertFalse(any("share" in item for item in steady_findings))
        row = sl.projection(plan, gap)[0]
        self.assertEqual((row["largest"], row["ratio"], row["projected"]), (100.0, 2.1, 210.0))
        self.assertEqual(sl.plan_digest(plan), sl.plan_digest(sl.build_plan(ids, lanes, gap)))

    def test_ratio_uses_maximum_not_mean_and_never_reduces_a_plan(self):
        bodies = measured_bodies(ratio=0.2)
        bodies[1]["wall"] = 240.0
        value, _ = sl.merge_timings(manifest(), bodies)
        self.assertEqual(sl.wall_ratio(value, "parallel"), 2.4)
        self.assertEqual(sl.wall_ratio(value, "serial"), 1.0)
        self.assertEqual(value["calibration"]["lanes"]["parallel"][1]["planned_worker_seconds"], 100.0)
        # Observed durations and the refreshed fast mean cannot replace the logged denominator.
        value["fast_mean_seconds"] = 40
        self.assertEqual(sl.wall_ratio(value, "parallel"), 2.4)

    def test_cap_warns_only_for_new_calibrated_excess_and_keeps_raw_failure(self):
        value = manifest()
        value["lanes"]["parallel"]["shards"] = 6
        value, _ = sl.merge_timings(value, measured_bodies(value))
        ids = [f"tests.m.C.test_{i}" for i in range(12)]
        lanes = dict.fromkeys(ids, "parallel")
        with tempfile.TemporaryDirectory() as root:
            value["durations"] = dict.fromkeys(ids, 100.0)
            plan = sl.build_plan(ids, lanes, value)
            self.assertFalse(any("share" in item for item in sl.check(Path(root), value, ids, lanes, [], plan)))
            warning = sl.projection_warnings(plan, value)[0]
            self.assertIn("cap 6 reached; residual 20.0 s", warning)
            self.assertIn("owner decision", warning)
            value["durations"][ids[0]] = 200.0
            plan = sl.build_plan(ids, lanes, value)
            findings = sl.check(Path(root), value, ids, lanes, [], plan)
        self.assertTrue(any("share 400.0" in item for item in findings))
        self.assertFalse(any("shards to 7" in item for item in findings))
        self.assertEqual(sl.projection_warnings(plan, value), [])

    def test_earlier_manifest_and_logs_preserve_existing_calibration(self):
        original = manifest()
        self.assertEqual(sl.wall_ratio(original, "parallel"), 1)
        calibrated, _ = sl.merge_timings(original, measured_bodies())
        earlier = [{key: item for key, item in body.items() if key != "planned_worker_seconds"}
                  for body in measured_bodies()]
        updated, _ = sl.merge_timings(calibrated, earlier)
        self.assertEqual(updated["calibration"], calibrated["calibration"])
        self.assertNotIn("calibration", sl.merge_timings(original, earlier)[0])
        # A finished older topology is complete in its own right after increasing current shards.
        original["lanes"]["parallel"]["shards"] = 6
        self.assertIn("calibration", sl.merge_timings(original, measured_bodies())[0])

    def test_logged_denominator_comes_from_the_original_share_plan(self):
        value = manifest(durations={"tests.m.C.test_a": 70.1234567, "tests.m.C.test_b": 30.0})
        ids = list(value["durations"])
        plan = sl.build_plan(ids, dict.fromkeys(ids, "parallel"), value)
        denominator = sl.planned_worker_seconds(plan, value, "parallel", 1)
        results = {ids[0]: {"outcome": "success", "seconds": 0.5}}
        body = sl.parse_timings(sl.timing_lines(lane="parallel", shard=1, of=2, workers=2,
                               digest=sl.plan_digest(plan), results=results, manifest=value,
                               wall=200, skips=0, fixture_errors=0, planned_worker=denominator)[0])[0]
        self.assertEqual(body["planned_worker_seconds"], 70.1234567)
        self.assertNotEqual(body["planned_worker_seconds"], sum(body["seconds"].values()))

    def test_selected_hosted_run_identity_must_match_log_before_write(self):
        args = sl.parser().parse_args(["refresh", "--run", "99"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            synthetic_tree(root, {}, manifest())
            before = sl.manifest_path(root).read_bytes()
            for change in ({"sha": "different"}, {"run_id": "100"}):
                bodies = [dict(body, **change) for body in measured_bodies()]
                log = "\n".join(sl.MARKER + json.dumps(body) for body in bodies)
                metadata = json.dumps({"status": "completed", "headSha": "commit", "createdAt": "2026-10-25",
                                       "event": "push"})
                with self.subTest(change=change), patch.object(sl, "_gh", side_effect=[metadata, log]):
                    with self.assertRaisesRegex(sl.Finding, "selected hosted run"):
                        sl.command_refresh(root, args)
                self.assertEqual(sl.manifest_path(root).read_bytes(), before)

    def test_refresh_writes_calibration_only_and_refusal_preserves_manifest_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = manifest(fast_mean_seconds=0.2)
            synthetic_tree(root, {}, original)
            bodies = measured_bodies()
            log = root / "hosted.log"
            log.write_text("\n".join(sl.MARKER + json.dumps(body) for body in bodies))
            path = sl.manifest_path(root)
            before = path.read_bytes()
            dry = cli(root, "refresh", "--log", str(log), "--dry-run")
            self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
            self.assertEqual(path.read_bytes(), before)
            written = cli(root, "refresh", "--log", str(log))
            self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["durations"], original["durations"])
            self.assertEqual(saved["fast_mean_seconds"], original["fast_mean_seconds"])
            self.assertEqual(sl.wall_ratio(saved, "parallel"), 2.0)
            before = path.read_bytes()
            bodies.pop()
            log.write_text("\n".join(sl.MARKER + json.dumps(body) for body in bodies))
            refused = cli(root, "refresh", "--log", str(log))
            self.assertEqual(refused.returncode, 1)
            self.assertEqual(path.read_bytes(), before)

    def test_manifest_rejects_invalid_or_incomplete_calibration(self):
        good, _ = sl.merge_timings(manifest(), measured_bodies())
        self.assertEqual(sl.manifest_problems(good), [])
        for bad_value in (0, -1, float("nan"), float("inf"), True):
            bad = deepcopy(good)
            bad["calibration"]["lanes"]["parallel"][0]["planned_worker_seconds"] = bad_value
            with self.subTest(value=bad_value):
                self.assertTrue(sl.manifest_problems(bad))
        for transform in (lambda c: c["lanes"].pop("serial"),
                          lambda c: c["lanes"]["parallel"].pop(),
                          lambda c: c["lanes"]["parallel"].append(c["lanes"]["parallel"][0])):
            bad = deepcopy(good)
            transform(bad["calibration"])
            self.assertTrue(sl.manifest_problems(bad))


class HostedRefreshIdentityTests(unittest.TestCase):
    HEAD, CHECKOUT, BASE = "a" * 40, "b" * 40, "c" * 40

    def metadata(self, event="pull_request"):
        return json.dumps({"status": "completed", "headSha": self.HEAD,
                           "createdAt": "2026-10-25T00:00:00Z", "event": event})

    def log(self, bodies):
        return "\n".join(sl.MARKER + json.dumps(body) for body in bodies)

    def commit(self, parents=None):
        return json.dumps(dict(sha=self.CHECKOUT,
                               parents=[{"sha": sha} for sha in (parents if parents is not None
                                                                else [self.BASE, self.HEAD])]))

    def test_pr_selection_keeps_head_and_measured_merge_checkout_distinct(self):
        bodies = [dict(body, sha=self.CHECKOUT, run_id="99") for body in measured_bodies()]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            synthetic_tree(root, {}, manifest())
            responses = [json.dumps({"headRefOid": self.HEAD}),
                         json.dumps([{"databaseId": 99, "status": "completed"}]),
                         self.metadata(), self.log(bodies), self.commit()]
            with patch.object(sl, "_gh", side_effect=responses) as gh:
                self.assertEqual(sl.command_refresh(root, sl.parser().parse_args(["refresh", "--pr", "7"])), 0)
            gh.assert_any_call("run", "view", "99", "--json", "headSha,createdAt,status,event")
            gh.assert_called_with("api", f"repos/{{owner}}/{{repo}}/git/commits/{self.CHECKOUT}")
            saved = sl.load_manifest(root)
        self.assertEqual(saved["source"], {"kind": "run", "run_id": 99, "head_sha": self.HEAD,
                                           "date": "2026-10-25"})
        self.assertEqual(saved["calibration"]["sha"], self.CHECKOUT)
        self.assertEqual(saved["calibration"]["run_id"], "99")

    def test_legacy_pr_logs_verify_merge_parent_and_retain_existing_calibration(self):
        original, _ = sl.merge_timings(manifest(), measured_bodies())
        bodies = [dict(timing_body(lane, 1, 1, {}, fast=(1, 0.3)), sha=self.CHECKOUT) for lane in sl.LANES]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            synthetic_tree(root, {}, original)
            with patch.object(sl, "_gh", side_effect=[self.metadata(), self.log(bodies), self.commit()]):
                self.assertEqual(sl.command_refresh(root, sl.parser().parse_args(["refresh", "--run", "99"])), 0)
            saved = sl.load_manifest(root)
        self.assertEqual(saved["source"]["head_sha"], self.HEAD)
        self.assertEqual(saved["fast_mean_seconds"], 0.3)
        self.assertEqual(saved["calibration"], original["calibration"])

    def test_push_and_explicit_pr_head_checkout_need_no_merge_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for event in ("push", "pull_request"):
                for run_identity in ({}, {"run_id": "99"}):
                    synthetic_tree(root, {}, manifest())
                    bodies = [dict(body, sha=self.HEAD, **run_identity) for body in measured_bodies()]
                    with self.subTest(event=event, identity=run_identity), patch.object(
                            sl, "_gh", side_effect=[self.metadata(event), self.log(bodies)]) as gh:
                        self.assertEqual(sl.command_refresh(root, sl.parser().parse_args(
                            ["refresh", "--run", "99"])), 0)
                        self.assertEqual(gh.call_count, 2)
                    self.assertEqual(sl.load_manifest(root)["calibration"]["sha"], self.HEAD)

    def test_unrelated_pr_checkout_refuses_even_with_selected_run_id(self):
        bodies = [dict(body, sha=self.CHECKOUT, run_id="99") for body in measured_bodies()]
        commits = [self.commit(parents=[]), self.commit(parents=[self.HEAD, self.BASE]),
                   json.dumps({"sha": self.BASE, "parents": [{"sha": self.BASE}, {"sha": self.HEAD}]})]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            synthetic_tree(root, {}, manifest())
            before = sl.manifest_path(root).read_bytes()
            for commit in commits:
                with self.subTest(commit=commit), patch.object(
                        sl, "_gh", side_effect=[self.metadata(), self.log(bodies), commit]):
                    with self.assertRaisesRegex(sl.Finding, "selected hosted run"):
                        sl.command_refresh(root, sl.parser().parse_args(["refresh", "--run", "99"]))
                self.assertEqual(sl.manifest_path(root).read_bytes(), before)

    def test_other_events_and_unrelated_run_ids_cannot_borrow_pr_merge_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            synthetic_tree(root, {}, manifest())
            before = sl.manifest_path(root).read_bytes()
            cases = [(event, "99") for event in ("push", "pull_request_target", "workflow_dispatch")]
            cases.append(("pull_request", "100"))
            for event, run_id in cases:
                bodies = [dict(body, sha=self.CHECKOUT, run_id=run_id) for body in measured_bodies()]
                with self.subTest(event=event, run_id=run_id), patch.object(
                        sl, "_gh", side_effect=[self.metadata(event), self.log(bodies)]) as gh:
                    with self.assertRaisesRegex(sl.Finding, "selected hosted run"):
                        sl.command_refresh(root, sl.parser().parse_args(["refresh", "--run", "99"]))
                    self.assertEqual(gh.call_count, 2)
                self.assertEqual(sl.manifest_path(root).read_bytes(), before)


class RefreshIntegrityTests(unittest.TestCase):
    def refuse(self, bodies, message=None):
        before = manifest()
        saved = deepcopy(before)
        with self.assertRaises(sl.Finding) as raised:
            sl.merge_timings(before, bodies)
        if message:
            self.assertIn(message, str(raised.exception))
        self.assertEqual(before, saved)

    def test_duplicate_parts_replay_only_identical_content(self):
        full = measured_bodies()
        self.assertEqual(sl.merge_timings(manifest(), full)[0], sl.merge_timings(manifest(), full + [full[0]])[0])
        self.refuse(full + [dict(full[0], wall=1)], "conflicting duplicate")

    def test_inconsistent_identity_topology_and_chunks_refuse(self):
        cases = {"sha": {"sha": "other"}, "plan": {"plan": "other"},
                 "of": {"of": 3}, "workers": {"workers": 3},
                 "part": {"parts": 2}, "mixed telemetry": {"planned_worker_seconds": None}, "run": {"run_id": "2"}}
        for name, change in cases.items():
            full = measured_bodies()
            full[0].update(change)
            with self.subTest(name=name): self.refuse(full)
        full = measured_bodies()
        full[0].pop("planned_worker_seconds")
        self.refuse(full, "mixed planned-worker")
        full = measured_bodies()
        full[0]["parts"] = 2
        full.append(dict(full[0], part=2, wall=1))
        self.refuse(full, "inconsistent chunk")

    def test_invalid_numbers_and_unsuccessful_counts_refuse(self):
        for change in ({"wall": float("nan")}, {"wall": float("inf")}, {"wall": -1},
                       {"planned_worker_seconds": 0}, {"planned_worker_seconds": True},
                       {"planned_worker_seconds": 1e-320},
                       {"ran": 9}, {"planned": True}, {"skips": 1}, {"fixture_errors": 1}, {"clean": False},
                       {"fast": {"count": 10, "seconds": float("inf")}},
                       {"fast": {"count": 0, "seconds": 2}}, {"seconds": {"test": -1}},
                       {"seconds": {"test": float("nan")}}, {"seconds": {"test": 1}}):
            full = measured_bodies()
            full[0].update(change)
            with self.subTest(change=change): self.refuse(full)

    def test_finite_fast_timings_that_overflow_refuse_before_any_manifest_write(self):
        bodies = [timing_body(lane, 1, 1, {}, fast=(1, 1e308)) for lane in sl.LANES]
        self.assertEqual(len(sl.validated_legs(bodies)), len(sl.LANES))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            synthetic_tree(root, {}, manifest())
            before = sl.manifest_path(root).read_bytes()
            log = root / "overflow.log"
            log.write_text("\n".join(sl.MARKER + json.dumps(body) for body in bodies))
            for options in ([], ["--dry-run"]):
                with self.subTest(options=options):
                    refused = cli(root, "refresh", "--log", str(log), *options)
                    self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                    self.assertIn("invalid updated manifest", refused.stderr)
                    self.assertIn("fast_mean_seconds", refused.stderr)
                    self.assertEqual(sl.manifest_path(root).read_bytes(), before)
                    self.assertEqual(sl.load_manifest(root), manifest())

    def test_large_finite_aggregate_can_still_write_a_readable_manifest(self):
        bodies = [timing_body(lane, 1, 1, {}, fast=(1, 1e307)) for lane in sl.LANES]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            synthetic_tree(root, {}, manifest())
            log = root / "finite.log"
            log.write_text("\n".join(sl.MARKER + json.dumps(body) for body in bodies))
            written = cli(root, "refresh", "--log", str(log))
            self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
            self.assertEqual(sl.load_manifest(root)["fast_mean_seconds"], 1e307)

    def test_duplicate_test_ids_across_chunks_and_lanes_refuse(self):
        full = measured_bodies()
        for body in full[:2]:
            body.update(seconds={"tests.m.C.test_a": 2}, planned=11, ran=11)
        self.refuse(full, "duplicate test ids")
        full = measured_bodies()
        full[0].update(seconds={"tests.m.C.test_a": 2}, planned=12, ran=12, parts=2)
        full.append(dict(full[0], part=2))
        self.refuse(full, "duplicate test ids")

    def test_bad_marker_schema_and_metadata_are_findings_not_python_errors(self):
        for log in (sl.MARKER + "{broken", sl.MARKER + "[]", sl.MARKER + '{"schema":"other"}',
                    sl.MARKER + '{"schema":"x","schema":"y"}'):
            with self.subTest(log=log), self.assertRaises(sl.Finding): sl.parse_timings(log)
        for change in ({"lane": []}, {"fast": []}, {"seconds": []}, {"part": 0}, {"shard": 3},
                       {"workers": False}, {"parts": "2"}, {"sha": None}):
            full = measured_bodies()
            full[0].update(change)
            with self.subTest(change=change): self.refuse(full)


SYNTHETIC = {
    "tests/__init__.py": "",
    "tests/incidents/__init__.py": "",
    "tests/incidents/test_incident.py": "import unittest\nclass Incident(unittest.TestCase):\n"
                                        "    def test_recorded(self): pass\n",
}


def synthetic_tree(root: Path, files: dict[str, str], value: dict) -> None:
    for name, text in {**SYNTHETIC, **files}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    (root / "tests" / "suite_lanes.json").write_text(sl.dump_manifest(value))


def cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {key: item for key, item in os.environ.items() if not key.startswith(("GITHUB_", "PYTHONPATH"))}
    return subprocess.run([sys.executable, str(ROOT / "tools" / "suite_lanes.py"), "--root", str(root), *args],
                          cwd=root, env=env, capture_output=True, text=True, timeout=120)


ONE_SHARD = manifest(lanes={"parallel": {"shards": 1, "workers": 2, "target_seconds": 60},
                            "serial": {"shards": 1, "workers": 1, "target_seconds": 60},
                            "installer": {"shards": 1, "workers": 1, "target_seconds": 60}},
                     budgets=dict(manifest()["budgets"], record_at_seconds=0.1, unrecorded_max_seconds=0.25))


class WorkerTests(unittest.TestCase):
    def test_a_ci_share_reports_every_outcome_and_prints_its_timings(self):
        demo = """\
            import os, time, unittest
            class Outcomes(unittest.TestCase):
                def test_pass(self): pass
                def test_fail(self): self.fail("no")
                def test_error(self): raise RuntimeError("bad")
                @unittest.skip("not here")
                def test_skip(self): pass
                def test_subtest(self):
                    for value in (1, 2):
                        with self.subTest(value=value):
                            self.assertEqual(value, 1)
                @unittest.expectedFailure
                def test_expected(self): self.fail("known")
                @unittest.expectedFailure
                def test_unexpected(self): pass
                def test_slow(self): time.sleep(0.3)
            class Broken(unittest.TestCase):
                @classmethod
                def setUpClass(cls): raise RuntimeError("fixture down")
                def test_never(self): pass
            """
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            synthetic_tree(root, {"tests/test_demo.py": demo}, ONE_SHARD)
            completed = cli(root, "run", "--lane", "parallel", "--shard", "1", "--of", "1", "--strict",
                            "--forbid-skips")
        out = completed.stdout
        self.assertEqual(completed.returncode, 1, completed.stderr)
        for expected in ("FAILURE tests.test_demo.Outcomes.test_fail", "ERROR tests.test_demo.Outcomes.test_error",
                         "FAILURE tests.test_demo.Outcomes.test_subtest",
                         "UNEXPECTED SUCCESS tests.test_demo.Outcomes.test_unexpected",
                         "NOT RUN 1 test(s): setUpClass (tests.test_demo.Broken) reported error: "
                         "RuntimeError: fixture down",
                         "SKIPPED 1 test(s)", "UNRECORDED SLOW tests.test_demo.Outcomes.test_slow"):
            self.assertIn(expected, out)
        self.assertNotIn("test_expected", out.split("finding(s):")[1])
        body = sl.parse_timings(out)[0]
        self.assertEqual((body["lane"], body["shard"], body["planned"], body["ran"], body["fixture_errors"]),
                         ("parallel", 1, 10, 9, 1))
        self.assertIn("tests.test_demo.Outcomes.test_slow", body["seconds"])

    def test_a_clean_share_passes_and_a_crash_names_the_test_in_flight(self):
        clean = "import unittest\nclass Fine(unittest.TestCase):\n    def test_one(self): pass\n"
        crash = clean + "class Crash(unittest.TestCase):\n    def test_exit(self):\n        import os; os._exit(3)\n"
        teardown = clean + ("class Late(unittest.TestCase):\n    @classmethod\n    def tearDownClass(cls):\n"
                            "        import os; os._exit(1)\n    def test_two(self): pass\n")
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            synthetic_tree(root, {"tests/test_demo.py": clean}, ONE_SHARD)
            passing = cli(root, "run", "--lane", "parallel", "--shard", "1", "--of", "1", "--strict",
                          "--forbid-skips")
            (root / "tests" / "test_demo.py").write_text(crash)
            crashing = cli(root, "run", "--lane", "parallel", "--shard", "1", "--of", "1")
            (root / "tests" / "test_demo.py").write_text(teardown)
            late = cli(root, "run", "--lane", "parallel", "--shard", "1", "--of", "1")
            mismatch = cli(root, "run", "--lane", "parallel", "--shard", "1", "--of", "2")
        self.assertEqual(passing.returncode, 0, passing.stdout + passing.stderr)
        self.assertIn("2 tests, 0 failed", passing.stdout)
        passing_body = sl.parse_timings(passing.stdout)[0]
        self.assertEqual(passing_body["planned_worker_seconds"], ONE_SHARD["fast_mean_seconds"])
        self.assertTrue(passing_body["clean"])
        self.assertFalse(sl.parse_timings(late.stdout)[0]["clean"])
        self.assertEqual(crashing.returncode, 1)
        self.assertIn("IN FLIGHT tests.test_demo.Crash.test_exit: worker parallel-1.", crashing.stdout)
        self.assertEqual(late.returncode, 1, late.stdout)
        self.assertIn("without a clean finish after its last test", late.stdout)
        self.assertEqual(mismatch.returncode, 2)
        self.assertIn("SHARDS --of 2", mismatch.stderr)

    def test_an_interrupted_run_leaves_no_worker_behind(self):
        slow = "import time, unittest\nclass Slow(unittest.TestCase):\n    def test_wait(self): time.sleep(60)\n"
        spawned = []

        def interrupted(root, pending, running, finished, directory, concurrency):
            running.append(sl._spawn(root, ["tests.test_demo.Slow.test_wait"], directory, "parallel-1", 600))
            spawned.extend(running)
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as scratch:
            synthetic_tree(Path(root), {"tests/test_demo.py": slow}, ONE_SHARD)
            with patch.object(sl, "_drive", interrupted), self.assertRaises(KeyboardInterrupt):
                sl.run_pool(Path(root), [], Path(scratch), 1)
        # The worker would sleep for a minute; it is already reaped, so it was killed.
        self.assertIsNotNone(spawned[0]["process"].poll())

    def test_usage_errors_exit_two(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            synthetic_tree(root, {}, ONE_SHARD)
            for args, message in ((("run", "--lane", "parallel"), "needs --shard and --of"),
                                  (("run", "--lane", "parallel", "--shard", "1", "--of", "1", "--ids", "x"),
                                   "never a CI share"),
                                  (("refresh",), "needs --run")):
                with self.subTest(args=args):
                    completed = cli(root, *args)
                    self.assertEqual(completed.returncode, 2)
                    self.assertIn(message, completed.stderr)


if __name__ == "__main__":
    unittest.main()
