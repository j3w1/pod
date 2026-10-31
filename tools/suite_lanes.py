#!/usr/bin/env python3
"""Classify every test into a lane, run lanes in parallel workers and keep the suite fast.

Each test lands in exactly one lane. A test that uses the terminal harness, or asserts a wall-clock
bound, runs in `serial`, alone on its runner; a test that uses the installer harness runs in
`installer`; everything else runs in `parallel`. Recorded CI durations in tests/suite_lanes.json
balance every lane across its shards and workers, and the suite-health limits there stop a slow
test from entering unnoticed. See "Test lanes" in docs/validation.md.

    python tools/suite_lanes.py check
    python tools/suite_lanes.py plan [--json]
    python tools/suite_lanes.py run [--lane L --shard K --of N] [--strict] [--forbid-skips] [--jobs J]
    python tools/suite_lanes.py refresh (--run ID | --pr N | --latest | --log FILE) [--prune] [--dry-run]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if sys.path and Path(sys.path[0] or ".").resolve() == Path(__file__).resolve().parent:
    # Test imports must resolve exactly as under `python -m unittest` from the checkout root.
    sys.path[0] = str(ROOT)

SCHEMA = "suite-lanes/v1"
TIMINGS_SCHEMA = "suite-lanes-timings/v1"
MARKER = "POD_TEST_TIMINGS "
PRIMARY_PYTHON = "3.13"  # The interpreter whose durations and calibration `refresh` records.
LANES = ("parallel", "serial", "installer")
HARNESS = {"tests.pty_harness": "serial", "tests.install_support": "installer"}
PRECEDENCE = ("serial", "installer")
BUDGETS = ("record_at_seconds", "unrecorded_max_seconds", "drift_ratio", "drift_slack_seconds",
           "test_max_seconds", "max_parallel_shards")
KEYS = {"schema", "budgets", "lanes", "overrides", "source", "durations", "fast_mean_seconds"}
CLOCKS = frozenset({"monotonic", "perf_counter", "time", "monotonic_ns", "perf_counter_ns", "time_ns"})
BOUNDS = frozenset({"assertLess", "assertLessEqual"})
CHUNK = 100
SOLO_LIMIT = 5
PASSED = frozenset({"success", "expected_failure"})
FAILED = frozenset({"failure", "error", "unexpected_success"})
FIXTURE = re.compile(r"(setUpClass|tearDownClass|setUpModule|tearDownModule) \((.+)\)")


class Finding(Exception):
    """A routing, manifest or environment problem; its message ends with the fix."""

    def __init__(self, message: str, *, usage: bool = False):
        super().__init__(message)
        self.usage = usage


def _number(value) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


# Manifest ---------------------------------------------------------------------------------------

def manifest_path(root: Path) -> Path:
    return root / "tests" / "suite_lanes.json"


def manifest_problems(manifest: object) -> list[str]:
    """Structural problems only; ids are checked against discovery by `check`."""
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        return [f"schema must be {SCHEMA}"]
    problems = []
    if set(manifest) - {"calibration"} != KEYS:
        problems.append("required keys are " + ", ".join(sorted(KEYS)) + "; only calibration is optional")
    if not (_number(manifest.get("fast_mean_seconds")) and manifest["fast_mean_seconds"] > 0):
        problems.append("fast_mean_seconds must be a positive number")
    budgets = manifest.get("budgets")
    if not isinstance(budgets, dict) or set(budgets) != set(BUDGETS):
        problems.append("budgets must name exactly " + ", ".join(BUDGETS))
        budgets = None
    elif not all(_number(budgets[key]) and budgets[key] > 0 for key in BUDGETS):
        problems.append("every budget must be a positive number")
        budgets = None
    elif not (budgets["record_at_seconds"] < budgets["unrecorded_max_seconds"] < budgets["test_max_seconds"]
              and budgets["drift_ratio"] >= 1 and _count(budgets["max_parallel_shards"])):
        problems.append("budgets need record_at < unrecorded_max < test_max, drift_ratio >= 1 and an "
                        "integer max_parallel_shards")
    lanes = manifest.get("lanes")
    if not isinstance(lanes, dict) or set(lanes) != set(LANES):
        problems.append("lanes must be exactly " + ", ".join(LANES))
    else:
        for name, lane in sorted(lanes.items()):
            if (not isinstance(lane, dict) or set(lane) != {"shards", "workers", "target_seconds"}
                    or not _count(lane["shards"]) or not _count(lane["workers"])
                    or not _number(lane["target_seconds"]) or lane["target_seconds"] <= 0):
                problems.append(f"lane {name} needs integer shards and workers >= 1 and a positive "
                                "target_seconds")
        if isinstance(lanes["serial"], dict) and lanes["serial"].get("workers") != 1:
            problems.append("the serial lane runs exactly one worker")
        if (budgets and isinstance(lanes["parallel"], dict) and _count(lanes["parallel"].get("shards"))
                and lanes["parallel"]["shards"] > budgets["max_parallel_shards"]):
            problems.append("lanes.parallel.shards exceeds budgets.max_parallel_shards, an owner decision")
    overrides = manifest.get("overrides")
    if not isinstance(overrides, dict):
        problems.append("overrides must be an object")
    else:
        for key, value in sorted(overrides.items()):
            if (not isinstance(value, dict) or set(value) != {"lane", "reason"} or value["lane"] not in LANES
                    or not isinstance(value["reason"], str) or not value["reason"].strip()):
                problems.append(f"override {key} needs exactly a lane and a reason")
    durations = manifest.get("durations")
    if not isinstance(durations, dict) or not all(_number(value) and value > 0 for value in durations.values()):
        problems.append("durations must map test ids to positive seconds")
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("kind") not in ("estimate", "run"):
        problems.append("source.kind must be estimate or run")
    if "calibration" in manifest:
        try:
            validate_calibration(manifest["calibration"])
        except Finding as exc:
            problems.append(str(exc))
    return problems


def validate_calibration(value: object) -> None:
    """Keep every measured denominator and its same-run identity, never just a naked multiplier."""
    if (not isinstance(value, dict) or set(value) != {"plan", "sha", "run_id", "method", "lanes"}
            or not isinstance(value["plan"], str) or not value["plan"]
            or not isinstance(value["sha"], str)
            or not isinstance(value["run_id"], str)
            or value["run_id"] != "" and not value["run_id"].isascii()
            or value["run_id"] != "" and not value["run_id"].isdecimal()
            or value["method"] not in ("timing_lines", "verified_plan")
            or not isinstance(value["lanes"], dict) or set(value["lanes"]) != set(LANES)):
        raise Finding("calibration needs plan, sha, method and every lane's measured shares")
    for lane, samples in value["lanes"].items():
        if not isinstance(samples, list) or not samples:
            raise Finding(f"calibration {lane} needs measured shares")
        for sample in samples:
            if (not isinstance(sample, dict) or set(sample) != {"shard", "of", "workers", "wall_seconds",
                                                               "planned_worker_seconds"}
                    or not all(_count(sample[key]) for key in ("shard", "of", "workers"))
                    or sample["shard"] > sample["of"]
                    or not _number(sample["wall_seconds"]) or sample["wall_seconds"] < 0
                    or not _number(sample["planned_worker_seconds"]) or sample["planned_worker_seconds"] <= 0
                    or not _number(sample["wall_seconds"] / sample["planned_worker_seconds"])
                    or lane == "serial" and sample["workers"] != 1):
                raise Finding(f"calibration {lane} has an invalid measured share")
        if (len({(sample["of"], sample["workers"]) for sample in samples}) != 1
                or len(samples) != samples[0]["of"]
                or sorted(sample["shard"] for sample in samples) != list(range(1, samples[0]["of"] + 1))):
            raise Finding(f"calibration {lane} needs each share exactly once")


def wall_ratio(manifest: dict, lane: str) -> float:
    samples = manifest.get("calibration", {}).get("lanes", {}).get(lane, [])
    return max([1.0, *(sample["wall_seconds"] / sample["planned_worker_seconds"] for sample in samples)])


def load_manifest(root: Path) -> dict:
    path = manifest_path(root)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Finding(f"MANIFEST tests/suite_lanes.json is unreadable: {exc}", usage=True) from exc
    problems = manifest_problems(manifest)
    if problems:
        raise Finding("MANIFEST " + "; ".join(problems) + ": fix tests/suite_lanes.json", usage=True)
    return manifest


def dump_manifest(manifest: dict) -> str:
    return json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


# Discovery and classification -------------------------------------------------------------------

def _iterate(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iterate(item)
        else:
            yield item


def discover(root: Path) -> list[str]:
    """Every test id in discovery order, as `python -m unittest discover -s tests -t .` finds them."""
    loader = unittest.TestLoader()
    tests = list(_iterate(loader.discover(str(root / "tests"), top_level_dir=str(root))))
    failed = [test.id() for test in tests if type(test).__name__ == "_FailedTest"]
    if loader.errors or failed:
        raise Finding("DISCOVERY could not import every test module; run from the checkout with "
                      "PYTHONPATH=skills:\n" + ("\n".join(loader.errors) or ", ".join(failed)), usage=True)
    ids = [test.id() for test in tests]
    duplicates = sorted({test_id for test_id in ids if ids.count(test_id) > 1}) if len(set(ids)) != len(ids) else []
    if duplicates:
        raise Finding("DISCOVERY found duplicate test ids " + ", ".join(duplicates) + ": rename them")
    return ids


def module_file(root: Path, module: str) -> Path | None:
    path = root.joinpath(*module.split("."))
    for candidate in (path.with_suffix(".py"), path / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imports(tree: ast.AST) -> set[str]:
    """Dotted names imported anywhere in a module, including inside functions."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _calls(node: ast.AST, names: frozenset[str]) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            target = child.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
            if name in names:
                return True
    return False


def module_facts(source: str) -> dict:
    """The tests.* modules a module imports, and its wall-clock-bounded test methods."""
    tree = ast.parse(source)
    timing = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            functions = [item for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
            helper_clock = any(_calls(item, CLOCKS) for item in functions if not item.name.startswith("test"))
            for item in functions:
                if (item.name.startswith("test") and _calls(item, BOUNDS)
                        and (helper_clock or _calls(item, CLOCKS))):
                    timing.add(f"{node.name}.{item.name}")
    return {"imports": _imports(tree), "timing": timing}


def _strongest(lanes) -> str | None:
    for lane in PRECEDENCE:
        if lane in lanes:
            return lane
    return None


class Classifier:
    """Lanes from harness use (direct or through tests.* helpers) and timing-sensitive methods."""

    def __init__(self, root: Path, sources: dict[str, str] | None = None):
        self.root, self.sources, self.facts, self.harness_cache = root, sources or {}, {}, {}

    def facts_for(self, module: str) -> dict:
        if module not in self.facts:
            text = self.sources.get(module)
            if text is None:
                path = module_file(self.root, module)
                text = path.read_text(encoding="utf-8") if path else ""
            self.facts[module] = module_facts(text)
        return self.facts[module]

    def _local(self, name: str) -> str | None:
        """The tests.* module an imported dotted name refers to, if it is one."""
        for candidate in (name, name.rpartition(".")[0]):
            if candidate.startswith("tests.") and (candidate in self.sources
                                                    or module_file(self.root, candidate)):
                return candidate
        return None

    def harness(self, module: str, trail: frozenset = frozenset()) -> str | None:
        if module in self.harness_cache:
            return self.harness_cache[module]
        found = set()
        for name in self.facts_for(module)["imports"]:
            local = self._local(name)
            if local in HARNESS:
                found.add(HARNESS[local])
            elif local and local != module and local not in trail:
                lane = self.harness(local, trail | {module})
                if lane:
                    found.add(lane)
        lane = _strongest(found)
        if not trail:
            self.harness_cache[module] = lane
        return lane


def classify(root: Path, ids: list[str], manifest: dict, *, sources: dict[str, str] | None = None) -> dict:
    """Each test's lane, and timing-sensitive tests that would run outside `serial` unclassified."""
    classifier = Classifier(root, sources)
    overrides = manifest["overrides"]
    lanes, unclassified = {}, []
    for test_id in ids:
        module, cls, method = test_id.rsplit(".", 2)
        override = overrides.get(test_id) or overrides.get(f"{module}.{cls}") or overrides.get(module)
        if override:
            lanes[test_id] = override["lane"]
            continue
        lanes[test_id] = classifier.harness(module) or "parallel"
        if f"{cls}.{method}" in classifier.facts_for(module)["timing"] and lanes[test_id] != "serial":
            unclassified.append(test_id)
    return {"lanes": lanes, "timing_unclassified": unclassified}


# Planning ---------------------------------------------------------------------------------------

def estimate(manifest: dict, test_id: str) -> float:
    return float(manifest["durations"].get(test_id, manifest["fast_mean_seconds"]))


def pack(ids: list[str], bins: int, seconds) -> list[list[str]]:
    """Longest-first packing into `bins` bins, ties by id; each bin keeps discovery order."""
    order = {test_id: index for index, test_id in enumerate(ids)}
    heap = [(0.0, index) for index in range(bins)]
    assigned: list[list[str]] = [[] for _ in range(bins)]
    for test_id in sorted(ids, key=lambda item: (-seconds(item), item)):
        load, index = heapq.heappop(heap)
        assigned[index].append(test_id)
        heapq.heappush(heap, (load + seconds(test_id), index))
    return [sorted(group, key=order.__getitem__) for group in assigned]


def build_plan(ids: list[str], lanes: dict[str, str], manifest: dict) -> dict:
    plan = {}
    for lane in LANES:
        config = manifest["lanes"][lane]
        members = [test_id for test_id in ids if lanes[test_id] == lane]
        plan[lane] = {"shards": config["shards"], "workers": config["workers"],
                      "bins": pack(members, config["shards"] * config["workers"],
                                   lambda item: estimate(manifest, item))}
    return plan


def plan_digest(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def shard_bins(plan: dict, lane: str, shard: int) -> list[list[str]]:
    entry = plan[lane]
    return entry["bins"][(shard - 1) * entry["workers"]:shard * entry["workers"]]


def planned_worker_seconds(plan: dict, manifest: dict, lane: str, shard: int) -> float:
    return max((sum(estimate(manifest, test_id) for test_id in group)
                for group in shard_bins(plan, lane, shard)), default=0.0)


def projection(plan: dict, manifest: dict) -> list[dict]:
    rows = []
    for lane in LANES:
        entry = plan[lane]
        loads = [sum(estimate(manifest, test_id) for test_id in group) for group in entry["bins"]]
        rows.append({"lane": lane, "tests": sum(len(group) for group in entry["bins"]),
                     "shards": entry["shards"], "workers": entry["workers"],
                     "seconds": round(sum(loads), 1), "largest": round(max(loads, default=0.0), 1),
                     "ratio": wall_ratio(manifest, lane),
                     "projected": max(loads, default=0.0) * wall_ratio(manifest, lane),
                     "target": manifest["lanes"][lane]["target_seconds"],
                     "empty": [shard for shard in range(1, entry["shards"] + 1)
                               if not any(shard_bins(plan, lane, shard))]})
    return rows


def _prefixes(ids) -> set[str]:
    return {test_id.rsplit(".", 1)[0] for test_id in ids} | {test_id.rsplit(".", 2)[0] for test_id in ids}


def check(root: Path, manifest: dict, ids: list[str], lanes: dict, unclassified: list[str], plan: dict) -> list[str]:
    """Every problem that would let the suite drift unnoticed; each message names its fix."""
    findings = []
    known, prefixes = set(ids), _prefixes(ids)
    for key in sorted(manifest["overrides"]):
        if key not in known and key not in prefixes:
            findings.append(f"STALE override {key} names no test: python tools/suite_lanes.py refresh --prune")
    for key in sorted(set(manifest["durations"]) - known):
        findings.append(f"STALE duration {key} names no test: python tools/suite_lanes.py refresh --prune")
    limit = manifest["budgets"]["test_max_seconds"]
    for key, value in sorted(manifest["durations"].items()):
        if value > limit:
            findings.append(f"CEILING {key} is recorded at {value} s > {limit} s: split it into named group "
                            "methods (tests.common.GroupedTable)")
    for test_id in unclassified:
        findings.append(f"TIMING {test_id} asserts a wall-clock bound but would run in {lanes[test_id]}: add a "
                        "\"serial\" override with a reason to tests/suite_lanes.json; keeping it out of serial "
                        "needs an owner decision")
    for path in sorted((root / "tests" / "incidents").glob("test_*.py")):
        if not any(test_id.startswith(f"tests.incidents.{path.stem}.") for test_id in ids):
            findings.append(f"INCIDENTS tests/incidents/{path.name} yields no test: give it a TestCase")
    for row in projection(plan, manifest):
        if row["empty"]:
            findings.append(f"PROJECTION {row['lane']} shards {row['empty']} have no tests: lower "
                            f"lanes.{row['lane']}.shards and drop their ci.yml include rows")
        if (row["largest"] > row["target"] or row["projected"] > row["target"]
                and not at_parallel_cap(row, manifest)):
            slowest = sorted((test_id for test_id in ids if lanes[test_id] == row["lane"]),
                             key=lambda item: (-estimate(manifest, item), item))[:3]
            findings.append(projection_message(row) + ": " +
                            ("no further parallel shard increase is authorized; make these faster: "
                             if at_parallel_cap(row, manifest) else
                             f"set lanes.{row['lane']}.shards to {row['shards'] + 1} and add its ci.yml "
                             "include row (parallel at most budgets.max_parallel_shards), or make these faster: ")
                            + ', '.join(slowest))
    return findings


def at_parallel_cap(row: dict, manifest: dict) -> bool:
    return row["lane"] == "parallel" and row["shards"] >= manifest["budgets"]["max_parallel_shards"]


def projection_message(row: dict) -> str:
    return (f"PROJECTION {row['lane']} share {row['projected']:.1f} s > {row['target']} s "
            f"(largest worker {row['largest']} s x ratio {row['ratio']:.3f}, "
            f"{row['shards']}x{row['workers']})")


def projection_warnings(plan: dict, manifest: dict) -> list[str]:
    return [projection_message(row) +
            f": parallel cap {manifest['budgets']['max_parallel_shards']} reached; "
            f"residual {row['projected'] - row['target']:.1f} s; improve runtime; "
            "budget, target or cap changes need an owner decision"
            for row in projection(plan, manifest)
            if at_parallel_cap(row, manifest) and row["largest"] <= row["target"] < row["projected"]]


def prepare(root: Path):
    manifest = load_manifest(root)
    ids = discover(root)
    classified = classify(root, ids, manifest)
    plan = build_plan(ids, classified["lanes"], manifest)
    return manifest, ids, classified["lanes"], classified["timing_unclassified"], plan


# Worker -----------------------------------------------------------------------------------------

def _owner(test):
    """The real test a result belongs to, or None for a class or module fixture placeholder."""
    kind = type(test).__name__
    if kind == "_SubTest":
        return test.test_case
    return None if kind == "_ErrorHolder" else test


class EventResult(unittest.TextTestResult):
    """Verbose unittest output plus one flushed JSON event per test start, stop and fixture."""

    events = None
    RANK = {"success": 0, "expected_failure": 0, "skip": 1, "unexpected_success": 2, "failure": 3, "error": 4}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clock, self.outcome = {}, {}

    def emit(self, **event):
        self.events.write(json.dumps(event) + "\n")
        self.events.flush()

    def startTest(self, test):
        super().startTest(test)
        if _owner(test) is test:
            self.clock[test.id()], self.outcome[test.id()] = time.perf_counter(), "success"
            self.emit(event="start", id=test.id())

    def stopTest(self, test):
        super().stopTest(test)
        if _owner(test) is test and test.id() in self.clock:
            seconds = round(time.perf_counter() - self.clock.pop(test.id()), 3)
            self.emit(event="stop", id=test.id(), outcome=self.outcome.pop(test.id()), seconds=seconds)

    def mark(self, test, outcome: str, detail: str = ""):
        owner = _owner(test)
        if owner is None:
            self.emit(event="fixture", id=str(test), outcome=outcome, detail=detail[-2000:])
        elif self.RANK[outcome] >= self.RANK[self.outcome.get(owner.id(), "success")]:
            self.outcome[owner.id()] = outcome

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.mark(test, "failure", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.mark(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.mark(test, "skip", str(reason))

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.mark(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.mark(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.mark(subtest, "failure" if issubclass(err[0], test.failureException) else "error",
                      self._exc_info_to_string(err, test))


def worker(ids_file: Path, events_file: Path) -> int:
    ids = [line for line in ids_file.read_text(encoding="utf-8").splitlines() if line]
    loader, suite = unittest.TestLoader(), unittest.TestSuite()
    with events_file.open("a", encoding="utf-8") as events:
        EventResult.events = events
        for test_id in ids:
            suite.addTests(loader.loadTestsFromName(test_id))
        runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=EventResult,
                                         warnings=None if sys.warnoptions else "default")
        result = runner.run(suite)
        events.write(json.dumps({"event": "done", "ok": result.wasSuccessful()}) + "\n")
    return 0 if result.wasSuccessful() else 1


# Running ----------------------------------------------------------------------------------------

def _read_events(path: Path) -> list[dict]:
    events = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    return events


def _last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _covers(fixture_id: str, test_id: str) -> bool:
    """Whether a class or module fixture event (or a load failure for one id) covers a test."""
    match = FIXTURE.fullmatch(fixture_id)
    scope = match.group(2) if match else fixture_id
    return test_id == scope or test_id.startswith(scope + ".")


def account(ids: list[str], events: list[dict], *, name: str, exit_note: str, forbid_skips: bool,
            exit_code: int = 0) -> dict:
    """Exact-once accounting of one worker's events, completion and exit against its assigned ids."""
    started, stopped, fixtures = {}, {}, []
    done = next((event for event in events if event.get("event") == "done"), None)
    for event in events:
        if event.get("event") == "start":
            started[event["id"]] = started.get(event["id"], 0) + 1
        elif event.get("event") == "stop":
            stopped[event["id"]] = event
        elif event.get("event") == "fixture":
            fixtures.append(event)
    results, problems, missing = {}, [], {}
    for test_id in ids:
        if started.get(test_id, 0) > 1:
            problems.append(f"DUPLICATE {test_id} ran {started[test_id]} times in {name}")
        if test_id in stopped:
            results[test_id] = {"outcome": stopped[test_id]["outcome"], "seconds": stopped[test_id]["seconds"]}
        elif test_id in started:
            results[test_id] = {"outcome": "error", "seconds": 0.0}
            problems.append(f"IN FLIGHT {test_id}: worker {name} {exit_note} during it")
        else:
            cause = next((event for event in fixtures if _covers(event["id"], test_id)), None)
            missing.setdefault(cause["id"] if cause else None, []).append(test_id)
            skipped = cause is not None and cause["outcome"] == "skip"
            results[test_id] = {"outcome": "skip" if skipped else "error", "seconds": 0.0}
    for cause, tests in missing.items():
        event = next((item for item in fixtures if item["id"] == cause), None)
        if event is None:
            problems.append(f"NOT RUN {len(tests)} test(s) in {name} ({exit_note}): {', '.join(tests[:5])}")
        elif event["outcome"] != "skip":
            problems.append(f"NOT RUN {len(tests)} test(s): {cause} reported {event['outcome']}: "
                            f"{_last_line(event.get('detail'))}")
    for event in fixtures:
        if event["outcome"] in FAILED and event["id"] not in missing:
            problems.append(f"FIXTURE {event['id']} reported {event['outcome']}: {_last_line(event.get('detail'))}")
    for test_id in sorted(set(started) - set(ids)):
        problems.append(f"UNASSIGNED {test_id} ran in {name} without being planned there")
    skips = sorted(test_id for test_id, result in results.items() if result["outcome"] == "skip")
    if forbid_skips and skips:
        problems.append(f"SKIPPED {len(skips)} test(s), which hosted CI forbids (install the lane prerequisites "
                        f"and fetch full history): {', '.join(skips[:5])}")
    for test_id, result in sorted(results.items()):
        if result["outcome"] in FAILED and test_id in stopped:
            problems.append(f"{result['outcome'].upper().replace('_', ' ')} {test_id}")
    if not problems and (done is None or not done.get("ok") or exit_code != 0):
        # Teardown, cleanup or interpreter exit can still fail after the last test stopped.
        problems.append(f"WORKER {name} {exit_note} without a clean finish after its last test: a class or "
                        "module teardown, cleanup or interpreter exit failed; see its log")
    return {"results": results, "problems": problems, "skips": len(skips),
            "ran": sum(1 for test_id in ids if test_id in stopped),
            "fixture_errors": sum(1 for event in fixtures if event["outcome"] in FAILED)}


def _spawn(root: Path, ids: list[str], directory: Path, name: str, timeout: float) -> dict:
    ids_file, events_file, log_file = (directory / f"{name}.ids", directory / f"{name}.events",
                                       directory / f"{name}.log")
    ids_file.write_text("\n".join(ids) + "\n", encoding="utf-8")
    log = log_file.open("wb")
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--root", str(root), "_worker",
                                str(ids_file), str(events_file)], cwd=root, stdout=log,
                               stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, process_group=0)
    return {"name": name, "ids": ids, "process": process, "log": log, "log_file": log_file,
            "events": events_file, "started": time.monotonic(), "timeout": timeout}


def run_pool(root: Path, jobs: list[tuple[str, list[str], float]], directory: Path, concurrency: int) -> list[dict]:
    """Run (name, ids, timeout) workers at most `concurrency` at a time; kill a worker past its timeout."""
    pending, running, finished = list(jobs), [], []
    try:
        _drive(root, pending, running, finished, directory, concurrency)
    finally:
        for item in running:  # Interrupted: no worker outlives this run.
            try:
                os.killpg(item["process"].pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            item["process"].wait()
            item["log"].close()
    return sorted(finished, key=lambda item: item["name"])


def _drive(root: Path, pending: list, running: list, finished: list, directory: Path, concurrency: int) -> None:
    while pending or running:
        while pending and len(running) < concurrency:
            name, ids, timeout = pending.pop(0)
            running.append(_spawn(root, ids, directory, name, timeout))
        time.sleep(0.2)
        for item in list(running):
            code = item["process"].poll()
            if code is None and time.monotonic() - item["started"] > item["timeout"]:
                item["timed_out"] = True
                try:
                    os.killpg(item["process"].pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                code = item["process"].wait()
            if code is not None:
                item["log"].close()
                item["wall"] = time.monotonic() - item["started"]
                item["note"] = (f"timed out after {item['timeout']:.0f} s" if item.get("timed_out")
                                else f"exited {code}")
                item["code"] = code
                running.remove(item)
                finished.append(item)


def gate_candidates(results: dict, manifest: dict) -> list[tuple[str, str, float]]:
    candidates = []
    for test_id, result in sorted(results.items()):
        kind = violation(manifest, test_id, result["seconds"])
        if kind:
            candidates.append((test_id, kind, result["seconds"]))
    return candidates


def ranked_candidates(results: dict, manifest: dict) -> list[tuple[str, str, float]]:
    """Ceiling breaches, then new slow tests, then drift (slowest first): the limits exist for the first two."""
    rank = {"CEILING": 0, "UNRECORDED SLOW": 1, "DRIFT": 2}
    return sorted(gate_candidates(results, manifest), key=lambda item: (rank[item[1]], -item[2], item[0]))


def violation(manifest: dict, test_id: str, seconds: float) -> str | None:
    budgets, recorded = manifest["budgets"], manifest["durations"].get(test_id)
    if seconds > budgets["test_max_seconds"]:
        return "CEILING"
    if recorded is None and seconds > budgets["unrecorded_max_seconds"]:
        return "UNRECORDED SLOW"
    if recorded is not None and seconds > max(recorded * budgets["drift_ratio"],
                                              recorded + budgets["drift_slack_seconds"]):
        return "DRIFT"
    return None


def gate_message(kind: str, test_id: str, seconds: float, alone: float | None, manifest: dict) -> str:
    budgets, recorded = manifest["budgets"], manifest["durations"].get(test_id)
    run = os.environ.get("GITHUB_RUN_ID")
    record = (f"python tools/suite_lanes.py refresh --run {run}" if run
              else "python tools/suite_lanes.py refresh --pr <N> after its CI run")
    timing = f"took {seconds:.1f} s" + ("" if alone is None else f" ({alone:.1f} s alone)")
    if kind == "CEILING":
        return (f"CEILING {test_id} {timing} > {budgets['test_max_seconds']} s: split it into named group "
                "methods (tests.common.GroupedTable)")
    if kind == "UNRECORDED SLOW":
        return (f"UNRECORDED SLOW {test_id} {timing} > {budgets['unrecorded_max_seconds']} s: make it faster, "
                f"or record it with {record} and commit tests/suite_lanes.json")
    return (f"DRIFT {test_id} {timing}, recorded {recorded} s: find what slowed it, or record the new duration "
            f"with {record} and commit tests/suite_lanes.json")


def timing_lines(*, lane: str, shard: int, of: int, workers: int, digest: str, results: dict, manifest: dict,
                 wall: float, skips: int, fixture_errors: int, ran: int | None = None,
                 planned_worker: float | None = None, clean: bool = True) -> list[str]:
    """Chunked log lines that `refresh` reads back: passing durations near or above the record threshold."""
    ran = len(results) if ran is None else ran
    threshold = manifest["budgets"]["record_at_seconds"] / 2
    passed = {test_id: result["seconds"] for test_id, result in results.items() if result["outcome"] in PASSED}
    listed = sorted((test_id, seconds) for test_id, seconds in passed.items()
                    if seconds >= threshold or test_id in manifest["durations"])
    names = {test_id for test_id, _ in listed}
    fast = [seconds for test_id, seconds in passed.items() if test_id not in names]
    parts = max(1, math.ceil(len(listed) / CHUNK))
    lines = []
    for part in range(parts):
        body = {"schema": TIMINGS_SCHEMA, "lane": lane, "shard": shard, "of": of, "workers": workers,
                "cpus": os.cpu_count(), "python": "%d.%d" % sys.version_info[:2],
                "sha": os.environ.get("GITHUB_SHA", ""),
                "run_id": os.environ.get("GITHUB_RUN_ID", ""), "plan": digest,
                "part": part + 1, "parts": parts, "planned": len(results), "ran": ran, "skips": skips,
                "fixture_errors": fixture_errors, "clean": clean, "wall": round(wall, 1),
                "fast": {"count": len(fast), "seconds": round(sum(fast), 3)},
                "seconds": dict(listed[part * CHUNK:(part + 1) * CHUNK])}
        if planned_worker is not None:
            body["planned_worker_seconds"] = planned_worker
        lines.append(MARKER + json.dumps(body, sort_keys=True, separators=(",", ":")))
    return lines


def _group(title: str, text: str) -> str:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return f"::group::{title}\n{text.rstrip()}\n::endgroup::\n"
    return f"== {title}\n{text.rstrip()}\n"


def execute(root: Path, stages: list[list[tuple[str, list[str]]]], manifest: dict, *, strict: bool,
            forbid_skips: bool, concurrency: int, target: float | None = None) -> dict:
    """Run stages of (worker name, ids) one after another, each through the worker pool."""
    started = time.monotonic()
    outcome = {"results": {}, "problems": [], "warnings": [], "skips": 0, "fixture_errors": 0, "ran": 0, "clean": True}
    with tempfile.TemporaryDirectory(prefix="suite-lanes-") as scratch:
        directory = Path(scratch)
        for stage in stages:
            jobs = [(name, ids, max(600.0, 3 * sum(estimate(manifest, test_id) for test_id in ids) + 120))
                    for name, ids in stage if ids]
            for item in run_pool(root, jobs, directory, concurrency):
                sys.stdout.write(_group(f"{item['name']}: {len(item['ids'])} tests in {item['wall']:.1f} s",
                                        item["log_file"].read_text(encoding="utf-8", errors="replace")))
                accounted = account(item["ids"], _read_events(item["events"]), name=item["name"],
                                    exit_note=item["note"], forbid_skips=forbid_skips, exit_code=item["code"])
                outcome["clean"] = outcome["clean"] and not accounted["problems"]
                outcome["results"].update(accounted["results"])
                outcome["problems"] += accounted["problems"]
                outcome["skips"] += accounted["skips"]
                outcome["fixture_errors"] += accounted["fixture_errors"]
                outcome["ran"] += accounted["ran"]
        lane_wall = time.monotonic() - started
        if target is not None and lane_wall > target:
            message = f"LANE share took {lane_wall:.0f} s, over its {target:.0f} s target"
            if lane_wall > 2 * target and strict:
                outcome["problems"].append(message + ": add a shard (lanes.*.shards and a ci.yml include row) "
                                           "or split its slowest tests")
            else:
                outcome["warnings"].append(message)
        def alone(test_id: str, seconds: float) -> float:
            item = run_pool(root, [("solo", [test_id], min(300.0, 3 * seconds + 30))], directory, 1)[0]
            stops = [event for event in _read_events(item["events"]) if event.get("event") == "stop"]
            item["events"].unlink(missing_ok=True)
            return stops[-1]["seconds"] if stops else float("inf")

        problems, warnings = judge(ranked_candidates(outcome["results"], manifest), manifest, strict, alone)
        outcome["problems"] += problems
        outcome["warnings"] += warnings
    outcome["wall"] = time.monotonic() - started
    return outcome


def judge(candidates: list[tuple[str, str, float]], manifest: dict, strict: bool, alone) -> tuple[list, list]:
    """Turn ranked gate hits into findings and warnings, timing at most SOLO_LIMIT of them alone."""
    problems, warnings = [], []
    for test_id, kind, seconds in candidates[SOLO_LIMIT:]:
        # Past the cap, an unconfirmed ceiling breach or new slow test still fails; drift only warns.
        message = gate_message(kind, test_id, seconds, None, manifest) + " [not timed alone]"
        (problems if strict and kind != "DRIFT" else warnings).append(message)
    for test_id, kind, seconds in candidates[:SOLO_LIMIT]:
        solo = alone(test_id, seconds)
        message = gate_message(kind, test_id, seconds, solo, manifest)
        if violation(manifest, test_id, solo) and strict:
            problems.append(message)
        else:
            warnings.append(message + ("" if violation(manifest, test_id, solo)
                                       else " [within limits alone: runner contention]"))
    return problems, warnings


def summary(title: str, outcome: dict) -> str:
    results = outcome["results"]
    failed = sum(1 for result in results.values() if result["outcome"] in FAILED)
    lines = [f"### Suite lanes: {title}", "",
             f"{len(results)} tests in {outcome['wall']:.1f} s; {failed} failed; {outcome['skips']} skipped; "
             f"{len(outcome['problems'])} finding(s); {len(outcome['warnings'])} warning(s)", "",
             "| Seconds | Slowest tests |", "| ---: | --- |"]
    lines += [f"| {result['seconds']:.1f} | `{test_id}` |"
              for test_id, result in sorted(results.items(), key=lambda item: -item[1]["seconds"])[:10]]
    if outcome["problems"] or outcome["warnings"]:
        lines += ["", *[f"- **{item}**" for item in outcome["problems"]], *[f"- {item}" for item in outcome["warnings"]]]
    return "\n".join(lines) + "\n"


def command_run(root: Path, args) -> int:
    manifest, ids, lanes, unclassified, plan = prepare(root)
    digest = plan_digest(plan)
    if args.lane:
        entry = plan[args.lane]
        if args.of != entry["shards"] or not 1 <= args.shard <= args.of:
            raise Finding(f"SHARDS --of {args.of} --shard {args.shard} disagree with lanes.{args.lane}.shards = "
                          f"{entry['shards']}: keep the ci.yml include rows and tests/suite_lanes.json in step",
                          usage=True)
        bins = shard_bins(plan, args.lane, args.shard)
        stages = [[(f"{args.lane}-{args.shard}.{index + 1}", group) for index, group in enumerate(bins)]]
        title = f"{args.lane} {args.shard}/{args.of}"
        concurrency = entry["workers"]
        target = manifest["lanes"][args.lane]["target_seconds"]
    else:
        if args.ids:
            unknown = sorted(set(args.ids) - set(ids))
            if unknown:
                raise Finding("UNKNOWN test ids " + ", ".join(unknown) + ": name discovered ids", usage=True)
        selected = set(args.ids or ids)
        jobs = args.jobs or os.cpu_count() or 2
        members = {lane: [test_id for test_id in ids if lanes[test_id] == lane and test_id in selected]
                   for lane in LANES}
        seconds = lambda item: estimate(manifest, item)  # noqa: E731
        installer = manifest["lanes"]["installer"]["workers"]
        stages = [[(f"parallel-{index + 1}", group)
                   for index, group in enumerate(pack(members["parallel"], jobs, seconds))]
                  + [(f"installer-{index + 1}", group)
                     for index, group in enumerate(pack(members["installer"], installer, seconds))],
                  [("serial-1", members["serial"])]]
        title, concurrency, target = "local run", jobs + installer, None
        for finding in check(root, manifest, ids, lanes, unclassified, plan):
            print("warning: " + finding, file=sys.stderr)
    outcome = execute(root, stages, manifest, strict=args.strict, forbid_skips=args.forbid_skips,
                      concurrency=concurrency, target=target)
    if args.lane:
        for line in timing_lines(lane=args.lane, shard=args.shard, of=args.of, workers=plan[args.lane]["workers"],
                                 digest=digest, results=outcome["results"], manifest=manifest, wall=outcome["wall"],
                                 skips=outcome["skips"], fixture_errors=outcome["fixture_errors"],
                                 ran=outcome["ran"],
                                 planned_worker=planned_worker_seconds(plan, manifest, args.lane, args.shard),
                                 clean=outcome["clean"]):
            print(line)
    report = summary(title, outcome)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(report)
    for warning in outcome["warnings"]:
        print("warning: " + warning)
    if outcome["problems"]:
        print(f"\n{len(outcome['problems'])} finding(s):")
        print("\n".join(outcome["problems"]))
    failed = sum(1 for result in outcome["results"].values() if result["outcome"] in FAILED)
    print(f"plan {digest}\n{len(outcome['results'])} tests, {failed} failed, {outcome['wall']:.1f} s")
    return 1 if outcome["problems"] else 0


# Refresh ----------------------------------------------------------------------------------------

def _unique_keys(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate timing key {key}")
        result[key] = value
    return result


def parse_timings(text: str) -> list[dict]:
    bodies = []
    for raw in text.splitlines():
        position = raw.find(MARKER)
        if position >= 0:
            try:
                body = json.loads(raw[position + len(MARKER):], object_pairs_hook=_unique_keys)
            except ValueError as exc:
                raise Finding("REFRESH malformed POD_TEST_TIMINGS line: use complete unmodified logs") from exc
            if not isinstance(body, dict) or body.get("schema") != TIMINGS_SCHEMA:
                raise Finding("REFRESH unsupported POD_TEST_TIMINGS schema")
            bodies.append(body)
    return _one_interpreter(bodies)


def _one_interpreter(bodies: list[dict]) -> list[dict]:
    """A matrix run repeats every share per interpreter; record the primary's, never a mixture."""
    versions = {body["python"] for body in bodies if isinstance(body.get("python"), str)}
    if any(not isinstance(body.get("python", ""), str) for body in bodies) or len(versions) < 2:
        return bodies
    if PRIMARY_PYTHON not in versions:
        raise Finding(f"REFRESH found several interpreters ({', '.join(sorted(versions))}) and no "
                      f"{PRIMARY_PYTHON}: refresh from a run that includes the primary interpreter")
    return [body for body in bodies if body.get("python") == PRIMARY_PYTHON]


def validated_legs(bodies: list[dict]) -> dict[tuple, dict]:
    """Validate a complete single run before allowing any duration or calibration update."""
    if not bodies:
        raise Finding("REFRESH found no POD_TEST_TIMINGS lines: name a finished suite-lanes CI run")
    legs: dict[tuple, dict] = {}
    identities, calibrated = set(), set()
    for body in bodies:
        if (not isinstance(body, dict) or body.get("schema") != TIMINGS_SCHEMA
                or body.get("lane") not in LANES
                or not all(_count(body.get(key)) for key in ("shard", "of", "workers", "part", "parts"))
                or body["shard"] > body["of"] or body["part"] > body["parts"]
                or not isinstance(body.get("plan"), str) or not body["plan"]
                or not isinstance(body.get("sha"), str)
                or not isinstance(body.get("run_id", ""), str)
                or body.get("run_id", "") != "" and not body["run_id"].isascii()
                or body.get("run_id", "") != "" and not body["run_id"].isdecimal()
                or not all(type(body.get(key)) is int and body[key] >= 0
                           for key in ("planned", "ran", "skips", "fixture_errors"))
                or body["ran"] != body["planned"] or body["skips"] or body["fixture_errors"]
                or "clean" in body and body["clean"] is not True
                or not _number(body.get("wall")) or body["wall"] < 0
                or not isinstance(body.get("fast"), dict) or set(body["fast"]) != {"count", "seconds"}
                or type(body["fast"]["count"]) is not int or body["fast"]["count"] < 0
                or not _number(body["fast"]["seconds"]) or body["fast"]["seconds"] < 0
                or body["fast"]["count"] == 0 and body["fast"]["seconds"] != 0
                or not isinstance(body.get("seconds"), dict)
                or not all(isinstance(key, str) and key and _number(value) and value >= 0
                           for key, value in body["seconds"].items())
                or body["lane"] == "serial" and body["workers"] != 1):
            raise Finding("REFRESH invalid or unsuccessful share timing metadata: use complete passing shares")
        has_planned = "planned_worker_seconds" in body
        if has_planned and (not _number(body["planned_worker_seconds"]) or body["planned_worker_seconds"] <= 0
                            or not _number(body["wall"] / body["planned_worker_seconds"])
                            or body.get("clean") is not True):
            raise Finding("REFRESH planned worker measurement needs a finite positive denominator, finite ratio "
                          "and clean worker completion")
        calibrated.add(has_planned)
        identities.add((body["plan"], body["sha"], body.get("run_id", "")))
        key = (body["lane"], body["shard"], body["of"])
        parts = legs.setdefault(key, {})
        if body["part"] in parts and parts[body["part"]] != body:
            raise Finding("REFRESH conflicting duplicate timing part")
        parts[body["part"]] = body
    if len(identities) != 1:
        raise Finding("REFRESH legs disagree on the plan digest, SHA or run identity: "
                      "refresh from a single run of one commit")
    if len(calibrated) != 1:
        raise Finding("REFRESH mixed planned-worker telemetry: use one complete run")
    seen = set()
    for lane in LANES:
        shares = [key for key in legs if key[0] == lane]
        topology = {(key[2], legs[key][min(legs[key])]["workers"]) for key in shares}
        if len(topology) != 1:
            raise Finding(f"REFRESH missing legs or inconsistent topology in {lane}")
        of, _ = next(iter(topology))
        if len(shares) != of or sorted(key[1] for key in shares) != list(range(1, of + 1)):
            raise Finding(f"REFRESH missing legs in {lane}: use a run in which every test leg finished")
    for (lane, shard, of), parts in sorted(legs.items()):
        first = parts[min(parts)]
        if len(parts) != first["parts"] or set(parts) != set(range(1, first["parts"] + 1)):
            raise Finding(f"REFRESH leg {lane} {shard}/{of} has incomplete timing lines")
        metadata = {key: value for key, value in first.items() if key not in ("part", "seconds")}
        count = 0
        for body in parts.values():
            if {key: value for key, value in body.items() if key not in ("part", "seconds")} != metadata:
                raise Finding(f"REFRESH inconsistent chunk metadata in {lane} {shard}/{of}")
            if seen.intersection(body["seconds"]):
                raise Finding("REFRESH duplicate test ids across timing parts or shares")
            seen.update(body["seconds"])
            count += len(body["seconds"])
        if count + first["fast"]["count"] != first["planned"]:
            raise Finding(f"REFRESH incomplete passing test counts in {lane} {shard}/{of}")
    return legs


def merge_timings(manifest: dict, bodies: list[dict]) -> tuple[dict, list[str]]:
    """Fold one complete CI run into the manifest; earlier logs retain earlier calibration."""
    legs = validated_legs(bodies)
    seconds, fast_count, fast_total = {}, 0, 0.0
    for parts in legs.values():
        for body in parts.values():
            seconds.update(body["seconds"])
        fast_count += parts[1]["fast"]["count"]
        fast_total += parts[1]["fast"]["seconds"]
    budgets, durations, changes = manifest["budgets"], dict(manifest["durations"]), []
    for test_id, value in sorted(seconds.items()):
        old, new = durations.get(test_id), round(value, 1)
        if old is None:
            if value >= budgets["record_at_seconds"]:
                durations[test_id] = new
                changes.append(f"+ {test_id} {new}")
        elif value < 0.8 * budgets["record_at_seconds"]:
            del durations[test_id]
            changes.append(f"- {test_id} {old} (now {value:.2f})")
        elif abs(value - old) > max(0.25 * old, 1.0):
            durations[test_id] = new
            changes.append(f"~ {test_id} {old} -> {new}")
    updated = dict(manifest, durations=durations)
    if fast_count:
        mean = round(fast_total / fast_count, 2)
        if mean > 0 and abs(mean - manifest["fast_mean_seconds"]) >= 0.01:
            changes.append(f"~ fast_mean_seconds {manifest['fast_mean_seconds']} -> {mean}")
            updated["fast_mean_seconds"] = mean
    first = bodies[0]
    if "planned_worker_seconds" in first:
        calibration = {"plan": first["plan"], "sha": first["sha"], "run_id": first.get("run_id", ""),
                       "method": "timing_lines",
                       "lanes": {lane: [] for lane in LANES}}
        for (lane, shard, of), parts in sorted(legs.items()):
            body = parts[1]
            calibration["lanes"][lane].append({"shard": shard, "of": of, "workers": body["workers"],
                                              "wall_seconds": body["wall"],
                                              "planned_worker_seconds": body["planned_worker_seconds"]})
        validate_calibration(calibration)
        if calibration != manifest.get("calibration"):
            updated["calibration"] = calibration
            changes.append("~ calibration (same-run wall / planned worker, lane maximum with floor 1)")
    return updated, changes


def _gh(*args: str) -> str:
    try:
        completed = subprocess.run(["gh", *args], capture_output=True, text=True, check=False, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Finding(f"GH is unavailable ({exc}): install and authenticate gh", usage=True) from exc
    if completed.returncode:
        raise Finding(f"GH {' '.join(args[:2])} failed: {completed.stderr.strip()[-400:]}", usage=True)
    return completed.stdout


def resolve_run(args) -> int:
    if args.run:
        return args.run
    if args.pr:
        head = json.loads(_gh("pr", "view", str(args.pr), "--json", "headRefOid"))["headRefOid"]
        runs = json.loads(_gh("run", "list", "--workflow", "ci.yml", "--commit", head,
                              "--json", "databaseId,status", "--limit", "20"))
    else:
        runs = json.loads(_gh("run", "list", "--workflow", "ci.yml", "--branch", "main", "--event", "push",
                              "--json", "databaseId,status", "--limit", "20"))
    finished = [run["databaseId"] for run in runs if run["status"] == "completed"]
    if not finished:
        raise Finding("REFRESH found no finished ci.yml run: wait for CI to finish", usage=True)
    return max(finished)


def command_refresh(root: Path, args) -> int:
    manifest, changes = load_manifest(root), []
    if args.log:
        text = Path(args.log).read_text(encoding="utf-8", errors="replace")
        manifest, changes = merge_timings(manifest, parse_timings(text))
        manifest["source"] = {"kind": "run", "run_id": args.run or 0, "head_sha": "", "date": ""}
    elif args.run or args.pr or args.latest:
        run = resolve_run(args)
        meta = json.loads(_gh("run", "view", str(run), "--json", "headSha,createdAt,status,event"))
        if meta["status"] != "completed":
            raise Finding(f"REFRESH run {run} is still {meta['status']}: wait for it to finish", usage=True)
        bodies = parse_timings(_gh("run", "view", str(run), "--log"))
        manifest, changes = merge_timings(manifest, bodies)
        if any(body.get("run_id", str(run)) != str(run) for body in bodies):
            raise Finding("REFRESH timing identity disagrees with the selected hosted run")
        checkout = bodies[0]["sha"]  # merge_timings has validated one complete same-checkout run.
        if checkout != meta["headSha"]:
            # pull_request tests the synthetic merge checkout; headSha is the PR branch head.
            # Verify their relationship without rewriting either identity, including for earlier
            # logs without run_id. Other events retain exact head/checkout equality.
            if meta["event"] != "pull_request" or not re.fullmatch(r"[0-9a-f]{40}", checkout):
                raise Finding("REFRESH timing identity disagrees with the selected hosted run")
            commit = json.loads(_gh("api", f"repos/{{owner}}/{{repo}}/git/commits/{checkout}"))
            parents = commit.get("parents", [])
            if (commit.get("sha") != checkout or len(parents) != 2
                    or parents[1].get("sha") != meta["headSha"]):
                raise Finding("REFRESH timing identity disagrees with the selected hosted run")
        manifest["source"] = {"kind": "run", "run_id": run, "head_sha": meta["headSha"],
                              "date": meta["createdAt"][:10]}
    if args.prune:
        ids = discover(root)
        known, prefixes = set(ids), _prefixes(ids)
        for key in sorted(set(manifest["durations"]) - known):
            changes.append(f"- {key} (no such test)")
        manifest["durations"] = {key: value for key, value in manifest["durations"].items() if key in known}
        for key in sorted(manifest["overrides"]):
            if key not in known and key not in prefixes:
                changes.append(f"- override {key} (no such test)")
        manifest["overrides"] = {key: value for key, value in manifest["overrides"].items()
                                 if key in known or key in prefixes}
    problems = manifest_problems(manifest)
    if problems:
        raise Finding("REFRESH invalid updated manifest: " + "; ".join(problems))
    print("\n".join(changes) if changes else "no changes")
    if changes and not args.dry_run:
        manifest_path(root).write_text(dump_manifest(manifest), encoding="utf-8")
        print("wrote tests/suite_lanes.json")
    return 0


# Entry ------------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    main_parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    commands = main_parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="verify the manifest, lanes and projection")
    plan_parser = commands.add_parser("plan", help="print each lane's planned workers")
    plan_parser.add_argument("--json", action="store_true")
    run_parser = commands.add_parser("run", help="run every test locally, or one CI share")
    run_parser.add_argument("--lane", choices=LANES)
    run_parser.add_argument("--shard", type=int)
    run_parser.add_argument("--of", type=int)
    run_parser.add_argument("--strict", action="store_true", help="fail on suite-health findings (hosted CI)")
    run_parser.add_argument("--forbid-skips", action="store_true", help="fail on any skipped test (hosted CI)")
    run_parser.add_argument("--jobs", type=int, help="local parallel workers (default: CPU count)")
    run_parser.add_argument("--ids", nargs="+", help="run only these discovered test ids, locally")
    refresh = commands.add_parser("refresh", help="record durations from a CI run's timing lines")
    source = refresh.add_mutually_exclusive_group()
    source.add_argument("--run", type=int)
    source.add_argument("--pr", type=int)
    source.add_argument("--latest", action="store_true", help="the newest finished push run on main")
    refresh.add_argument("--log", help="read timing lines from a saved log instead of gh")
    refresh.add_argument("--prune", action="store_true", help="drop entries that name no discovered test")
    refresh.add_argument("--dry-run", action="store_true")
    hidden = commands.add_parser("_worker")
    hidden.add_argument("ids_file", type=Path)
    hidden.add_argument("events_file", type=Path)
    return main_parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "_worker":
            if sys.path[0] != str(root):
                sys.path.insert(0, str(root))
            return worker(args.ids_file, args.events_file)
        if args.command == "run":
            if args.lane and (args.shard is None or args.of is None):
                raise Finding("RUN --lane needs --shard and --of", usage=True)
            if args.lane and (args.ids or args.jobs):
                raise Finding("RUN --ids and --jobs are for local runs, never a CI share", usage=True)
            return command_run(root, args)
        if args.command == "refresh":
            if not (args.run or args.pr or args.latest or args.log or args.prune):
                raise Finding("REFRESH needs --run, --pr, --latest, --log or --prune", usage=True)
            return command_refresh(root, args)
        manifest, ids, lanes, unclassified, plan = prepare(root)
        if args.command == "plan" and args.json:
            print(json.dumps({"digest": plan_digest(plan), "lanes": plan, "projection": projection(plan, manifest)},
                             indent=1, sort_keys=True))
            return 0
        for row in projection(plan, manifest):
            print(f"{row['lane']}: {row['tests']} tests, {row['shards']} shard(s) x {row['workers']} worker(s), "
                  f"{row['seconds']} s recorded or estimated, largest worker {row['largest']} s "
                  f"x wall ratio {row['ratio']:.3f} = projected share {row['projected']:.1f} s "
                  f"(target {row['target']} s)")
        for warning in projection_warnings(plan, manifest):
            print("warning: " + warning)
        print(f"plan {plan_digest(plan)}")
        if args.command == "plan":
            return 0
        findings = check(root, manifest, ids, lanes, unclassified, plan)
        print("\n".join(findings + [f"{len(ids)} tests in {len(LANES)} lanes; {len(findings)} finding(s)"]))
        return 1 if findings else 0
    except Finding as finding:
        print(str(finding), file=sys.stderr)
        return 2 if finding.usage else 1


if __name__ == "__main__":
    raise SystemExit(main())
