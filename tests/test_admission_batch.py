"""R207: the batch admission query shares one discovery, one deadline and one change recheck."""
from contextlib import redirect_stdout
import hashlib
from io import BytesIO, StringIO, TextIOWrapper
import json
import threading
from unittest.mock import patch

from pod import admission, cli, history
from pod.errors import PodError
from pod.ledger import objective_root, state_root
from pod.orca import DEADLINE_SPENT, read_deadline
from pod.window import Budget
from tests import test_admission_query as base

LIST = ["orchestration", "worker-list", "--include-remote", "--json"]
LIVE = {"connected": True, "writable": True, "orphaned": False}


def terminal_page(rows, runtime="runtime", **whole):
    return {"runtime": runtime, "result": {"terminals": rows, "totalCount": len(rows), "truncated": False,
            "hostScope": {"hostIds": ["local"], "omittedHostIds": []}, **whole}}


def terminal(handle, path, **fields):
    return {"handle": handle, "incarnationId": "inc-" + handle, "worktreePath": str(path), "worktreeId": "id:" + handle,
            "executionHostId": "local", "agentIdentity": "codex", **LIVE, "preview": "PRIVATE", **fields}


def fleet_page(total=3, counts=None, runtime="runtime", scope=None):
    return {"runtime": runtime, "result": {"scope": scope or {"source": "all"}, "workers": [],
            "counts": {"retained": 1, "released": 2} if counts is None else counts,
            "page": {"limit": 1, "total": total, "hasMore": total > 1}}}


class NativeDouble:
    """Orca reads the batch makes past the patched inventories, served in order: the terminal list and
    the fleet fence's unfiltered count read."""

    def __init__(self, *pages, fleet=None):
        self.pages, self.fleet, self.calls, self.lock = list(pages), [fleet or fleet_page()], [], threading.Lock()

    def __call__(self, argv):
        with self.lock:
            self.calls.append(list(argv))
            if argv == [*LIST, "--limit", "1"]:
                queue = self.fleet
            elif argv[:2] == ["terminal", "list"]:
                queue = self.pages
            else:
                raise AssertionError(f"unexpected native read {argv}")
            page = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(page, BaseException):
            raise page
        return page


class BatchCase(base.AdmissionQueryCase):
    def setUp(self):
        super().setUp()
        self.native = NativeDouble(terminal_page([]))
        reads = patch("pod.admission._read", side_effect=self.native)
        reads.start(); self.addCleanup(reads.stop)

    def batch(self, *queries, known=None, timeout=10.0):
        request = {"schema": "pod-admission-batch-request/v1",
                   "queries": [{"id": f"q{index}", "repository": str(self.project), **query}
                               for index, query in enumerate(queries)]}
        if known is not None:
            request["known_repositories"] = known
        return admission.batch(self.project, admission.batch_request(json.dumps(request).encode()), timeout=timeout)

    def scenario(self):
        """An open objective, a finished one, a foreign repository's record and an unreadable record."""
        self.intake(worktree=self.place)
        self.write([self.criterion("satisfied", evidence=[self.proof("O1")])], objective="old",
                   governance={"base_ref": "target"}, close=True, worktree=self.place)
        base.AttributionReadTests.foreign_record(self)
        broken = state_root(self.project) / "broken"
        broken.mkdir(); (broken / "context.json").write_text("")
        base.ReviewCorrectionTests.journal(self, kind="deploy", target="target")

    def shapes(self):
        return [{}, {"worktree": str(self.project)}, {"branch": "main"},
                {"worktree": str(self.project), "branch": "main"},
                {"objective": "objective", "candidate": self.base, "target": "target", "target_commit": self.base},
                {"effects": ["deploy:target", "release:other"]}, {"expect": "0" * 64}]

    def assert_equivalent(self, shapes):
        singles = [self.query(**shape) for shape in shapes]
        answer = self.batch(*shapes)
        self.assertEqual([row["id"] for row in answer["responses"]], [f"q{index}" for index in range(len(shapes))])
        for shape, single, row in zip(shapes, singles, answer["responses"]):
            with self.subTest(shape=shape):
                self.assertEqual(row["result"], single)
        return answer


class EquivalenceTests(BatchCase):
    def test_occupancy_shapes_equal_their_single_queries(self):
        self.scenario()
        answer = self.assert_equivalent(self.shapes()[:4])
        results = [row["result"] for row in answer["responses"]]
        self.assertIn({"record": "broken", "code": "invalid_record"}, results[0]["unavailable_records"])
        self.assertFalse(answer["authorization"])
        self.assertTrue(all(result["authorization"] is False for result in results))

    def test_target_effect_and_expect_shapes_equal_their_single_queries(self):
        self.scenario()
        answer = self.assert_equivalent(self.shapes()[4:])
        results = [row["result"] for row in answer["responses"]]
        self.assertEqual(results[0]["proof"]["candidate"], self.base)
        self.assertEqual(results[0]["target"]["commit"], self.base)
        self.assertIn("external_effect", {dimension for row in results[1]["conflicts"]
                                          for dimension in row.get("dimensions", [])})
        self.assertEqual(results[2]["conflicts"][-1], {"kind": "snapshot_changed"})

    def test_a_native_writer_stays_equivalent(self):
        self.scenario()
        writer = base.ReviewCorrectionTests.writer(self, verdict="live")
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": [writer]}):
            answer = self.assert_equivalent(self.shapes()[1:4])
        self.assertTrue(answer["responses"][2]["result"]["conflicts"])

    def test_a_bound_caller_keeps_its_coverage_unknown(self):
        self.scenario()
        with patch("pod.admission._fleet", return_value=None):
            answer = self.assert_equivalent(self.shapes()[:3])
        for row in answer["responses"]:
            self.assertIn("native_inventory_scope", [unknown["code"] for unknown in row["result"]["unknowns"]])
        self.assertEqual(answer["status"], "unknown")

    def test_the_projection_limit_stays_per_result(self):
        self.scenario()
        with patch("pod.orca.MAX_OUTPUT", 1500):
            answer = self.assert_equivalent(self.shapes()[3:5])
        for row in answer["responses"]:
            self.assertIn("admission_projection_limit", [unknown["code"] for unknown in row["result"]["unknowns"]])

    def test_responses_follow_request_order_and_known_repositories_are_shared(self):
        self.intake(worktree=self.place)
        other = base.AttributionReadTests.foreign_record(self).parent
        repository = self.root / "foreign-repository"
        request = {"schema": "pod-admission-batch-request/v1", "known_repositories": [str(repository)],
                   "queries": [{"id": name, "repository": str(self.project), "branch": branch}
                               for name, branch in (("z", "main"), ("a", None), ("m.1:x-y", "other"))]}
        answer = admission.batch(self.project, admission.batch_request(json.dumps(request).encode()))
        self.assertEqual([row["id"] for row in answer["responses"]], ["z", "a", "m.1:x-y"])
        single = self.query(branch="main", known_repositories=[str(repository)])
        self.assertEqual(answer["responses"][0]["result"], single)
        self.assertTrue(other.is_dir())


class SharedReadTests(BatchCase):
    def test_eight_queries_read_each_source_once_and_two_calls_read_afresh(self):
        self.scenario()
        loads, after = [], []
        real_load, real_bytes = history.load_admission_facts, history._context_bytes
        def load(path, *, include_admissions=True):
            loads.append((path.parent.name, include_admissions))
            return real_load(path, include_admissions=include_admissions)
        def raw_bytes(path, limit):
            if admission._SHARED.get().stage == "recheck":
                after.append(path.parent.name + "/" + path.name)
            return real_bytes(path, limit)
        shapes = (self.shapes() + [{"branch": "other"}])[:8]
        with patch("pod.history.load_admission_facts", side_effect=load), \
             patch("pod.admission.history._context_bytes", side_effect=raw_bytes), \
             patch("pod.admission._fleet", wraps=admission._fleet) as fleet, \
             patch("pod.admission._terminals", wraps=admission._terminals) as terminals:
            answer = self.batch(*shapes)
            first_loads, first_after = list(loads), list(after)
            self.batch({})
        self.assertEqual(len(answer["responses"]), 8)
        self.assertEqual((fleet.call_count, terminals.call_count), (2, 2))  # once per call, never shared across calls
        self.assertEqual(sorted(set(first_loads)), sorted(first_loads), "a record was decoded twice in one call")
        self.assertLessEqual({row for row in first_loads if not row[1]}, set(loads[len(first_loads):]))
        # One shared recheck: each rechecked file's bytes are read once for all eight queries.
        self.assertTrue(first_after)
        self.assertEqual(sorted(set(first_after)), sorted(first_after))
        self.assertEqual(answer["discovery"]["records"], 4)
        # Discovery's terminal list, then the fence's terminal list and fleet count read; the fleet is a double.
        self.assertEqual(answer["discovery"]["orca_reads"], 3)
        self.assertEqual([thread for thread in threading.enumerate() if thread.name.startswith("pod-admission")], [])

    def test_a_change_during_the_call_reaches_only_queries_it_is_relevant_to(self):
        self.intake(worktree=self.place)
        self.write([self.criterion()], objective="second", governance={"base_ref": "target"},
                   worktree={**self.place, "branch": "second-branch"})
        path = objective_root(self.project, "second") / "context.json"
        real = admission._after_context
        def change(target):
            if not change.done:
                change.done = True
                raw = base.logical_json(path)
                raw["checkpoint"]["worktree"]["branch"] = "renamed-branch"
                path.write_text(json.dumps(raw))
            return real(target)
        change.done = False
        with patch("pod.admission._after_context", side_effect=change):
            answer = self.batch({"branch": "main"}, {"branch": "second-branch"})
        unrelated, related = (row["result"] for row in answer["responses"])
        self.assertIn("record_changed", [row["code"] for row in related["unknowns"]])
        self.assertNotIn("record_changed", [row["code"] for row in unrelated["unknowns"]])
        self.assertEqual(unrelated["unrelated_unknowns"]["by_code"].get("record_changed"), 1)
        self.assertEqual(answer["status"], "unknown")

    def test_a_record_added_during_discovery_is_an_inventory_change_for_every_query(self):
        self.intake(worktree=self.place)
        root = state_root(self.project)
        def add():
            (root / "late").mkdir()
            return {"runtime": "runtime", "complete": True, "workers": []}
        with patch("pod.admission._fleet", side_effect=add):
            answer = self.batch({}, {"branch": "main"})
        for row in answer["responses"]:
            self.assertIn("inventory_changed", [unknown["code"] for unknown in row["result"]["unknowns"]])

    def test_a_spent_shared_deadline_reaches_every_result(self):
        self.intake(worktree=self.place)
        def spend():
            from pod.orca import _DEADLINE
            budget = _DEADLINE.get()  # The pool thread carries the call's one shared deadline.
            budget.deadline = budget._clock() - 1
            return {"runtime": "runtime", "complete": True, "workers": []}
        with patch("pod.admission._fleet", side_effect=spend):
            answer = self.batch({}, {"branch": "main"}, {"objective": "objective", "candidate": self.base})
        for row in answer["responses"]:
            self.assertEqual(row["result"]["status"], "unknown")
            self.assertIn(DEADLINE_SPENT, [unknown["code"] for unknown in row["result"]["unknowns"]])
        self.assertFalse(answer["complete"])

    def test_abandoned_workers_claims_start_from_their_inventory_rows(self):
        self.intake(worktree=self.place)
        source = base.ReviewCorrectionTests.writer(self)
        source["workerState"] = "abandoned"
        claim = {"process": "live", "effects": []}
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": [source]}), \
             patch("pod.admission._native_claim", return_value=claim) as reader:
            answer = self.batch({"worktree": str(self.project)}, {"branch": "main"}, {})
        reader.assert_called_once()  # One current-claim read, shared by every query that needs it.
        self.assertTrue(answer["responses"][0]["result"]["conflicts"])


class RequestTests(BatchCase):
    def request(self, **fields):
        value = {"schema": "pod-admission-batch-request/v1",
                 "queries": [{"id": "one", "repository": str(self.project)}]}
        value.update(fields)
        return value

    def test_every_request_refusal_is_one_batch_error(self):
        query = {"id": "one", "repository": str(self.project)}
        cases = {
            "empty": b"", "not json": b"{", "not utf-8": b'{"schema": "\xff"}', "array": b"[]",
            "nan": json.dumps(self.request()).replace('"one"', '"one", "x": NaN').encode(),
            "duplicate key": b'{"schema": "pod-admission-batch-request/v1", "schema": "x", "queries": []}',
            "too large": b" " * (admission.MAX_REQUEST_BYTES + 1),
            "schema": self.request(schema="pod-admission-batch-request/v2"),
            "unknown top key": self.request(extra=1),
            "no queries": self.request(queries=[]), "queries not list": self.request(queries={}),
            "too many queries": self.request(queries=[{**query, "id": f"q{i}"} for i in range(513)]),
            "query not object": self.request(queries=["one"]),
            "unknown query key": self.request(queries=[{**query, "run": "x"}]),
            "missing id": self.request(queries=[{"repository": str(self.project)}]),
            "empty id": self.request(queries=[{**query, "id": ""}]),
            "long id": self.request(queries=[{**query, "id": "a" * 129}]),
            "bad id": self.request(queries=[{**query, "id": "a b"}]),
            "duplicate id": self.request(queries=[query, query]),
            "missing repository": self.request(queries=[{"id": "one"}]),
            "relative repository": self.request(queries=[{**query, "repository": "relative"}]),
            "empty branch": self.request(queries=[{**query, "branch": ""}]),
            "long target": self.request(queries=[{**query, "target": "t" * 4097}]),
            "nul expect": self.request(queries=[{**query, "expect": "a\x00b"}]),
            "surrogate worktree": self.request(queries=[{**query, "worktree": "\ud800"}]),
            "number objective": self.request(queries=[{**query, "objective": 1}]),
            "effects not list": self.request(queries=[{**query, "effects": "deploy:x"}]),
            "too many effects": self.request(queries=[{**query, "effects": [f"e{i}" for i in range(65)]}]),
            "empty effect": self.request(queries=[{**query, "effects": [""]}]),
            "known not list": self.request(known_repositories="/a"),
            "too many known": self.request(known_repositories=[f"/r{i}" for i in range(513)]),
            "relative known": self.request(known_repositories=["r"]),
            "dotdot known": self.request(known_repositories=["/a/../b"]),
            "long known": self.request(known_repositories=["/" + "a" * 4096]),
        }
        for label, value in cases.items():
            data = value if isinstance(value, bytes) else json.dumps(value).encode()
            with self.subTest(label), self.assertRaises(PodError) as caught:
                admission.batch_request(data)
            self.assertEqual(caught.exception.code, "admission_batch_request", label)

    def test_bounds_and_nulls_are_accepted(self):
        query = {"id": "A-z0.9:_" + "x" * 120, "repository": str(self.project), "branch": None,
                 "effects": [f"e{i}" for i in range(64)], "target": "t" * 4096}
        accepted = admission.batch_request(json.dumps(self.request(
            queries=[query, *({"id": f"q{i}", "repository": str(self.project)} for i in range(511))],
            known_repositories=[f"/r{i}" for i in range(512)])).encode())
        self.assertEqual(len(accepted["queries"]), 512)

    def test_timeouts_outside_the_bound_are_refused(self):
        request = admission.batch_request(json.dumps(self.request()).encode())
        for timeout in (0, -1, 30.5, float("nan"), float("inf"), True, "10"):
            with self.subTest(timeout=timeout), self.assertRaises(PodError) as caught:
                admission.batch(self.project, request, timeout=timeout)
            self.assertEqual(caught.exception.code, "admission_timeout")

    def test_per_query_input_faults_name_the_query_before_any_discovery(self):
        self.intake(worktree=self.place)
        cases = {"admission_candidate": {"objective": "objective"},
                 "admission_target": {"target_commit": self.base},
                 "admission_scope_unavailable": {"repository": str(self.root / "missing")}}
        for code, fields in cases.items():
            with self.subTest(code), patch("pod.admission._fleet") as fleet, \
                    patch("pod.admission._terminals") as terminals, self.assertRaises(PodError) as caught:
                self.batch({}, fields)
            self.assertEqual(caught.exception.code, code)
            self.assertTrue(str(caught.exception).startswith("Query q1: "), str(caught.exception))
            self.assertEqual((fleet.call_count, terminals.call_count), (0, 0))
        self.assertEqual([thread for thread in threading.enumerate() if thread.name.startswith("pod-admission")], [])

    def test_the_whole_response_limit_refuses_the_batch(self):
        self.intake(worktree=self.place)
        with patch("pod.admission.MAX_RESPONSE_BYTES", 1000), self.assertRaises(PodError) as caught:
            self.batch({}, {"branch": "main"})
        self.assertEqual(caught.exception.code, "admission_batch_projection_limit")

    def run_cli(self, argv, data=b""):
        stdin = TextIOWrapper(BytesIO(data))
        out = StringIO()
        with patch("sys.stdin", stdin), redirect_stdout(out):
            code = cli.main(["admission", *argv])
        return code, out.getvalue()

    def test_cli_answers_exit_zero_and_refusals_print_only_an_error(self):
        self.intake(worktree=self.place)
        request = json.dumps(self.request()).encode()
        code, out = self.run_cli(["--batch", "--json", "--timeout", "20"], request)
        answer = json.loads(out)
        self.assertEqual((code, answer["schema"], [row["id"] for row in answer["responses"]]),
                         (0, "pod-admission-batch/v1", ["one"]))
        self.assertEqual(set(answer["discovery"]), {"elapsed_s", "native_workers", "records", "fleet_read", "orca_reads"})
        refusals = {("--batch", "--json", "--repository", str(self.project)): "admission_batch_request",
                    ("--batch", "--json", "--effect", "deploy:x"): "admission_batch_request",
                    ("--batch",): "admission_batch_request",
                    ("--batch", "--json", "--timeout", "31"): "admission_timeout"}
        for argv, expected in refusals.items():
            with self.subTest(argv=argv):
                code, out = self.run_cli(list(argv), request)
                self.assertEqual(code, 1)
                if "--json" in argv:
                    self.assertEqual(json.loads(out), {"schema": "pod-cli/v5", "status": "blocked",
                                                       "error": {"code": expected, "message": json.loads(out)["error"]["message"]}})
                else:
                    self.assertIn(expected, out)
        with patch("pod.admission.MAX_RESPONSE_BYTES", 1000):
            code, out = self.run_cli(["--batch", "--json"], request)
        self.assertEqual((code, json.loads(out)["error"]["code"]), (1, "admission_batch_projection_limit"))
        self.assertNotIn("responses", out)


class FencedCase(BatchCase):
    def setUp(self):
        super().setUp()
        real = patch("pod.admission._terminals", side_effect=self._real_terminals)
        real.start(); self.addCleanup(real.stop)
        self.fleet_start = fleet_page()
        def fleet():
            admission._SHARED.get().fleet_start = self.fleet_start  # the read that closed discovery
            return {"runtime": "runtime", "complete": True, "workers": []}
        native = patch("pod.admission._fleet", side_effect=fleet)
        native.start(); self.addCleanup(native.stop)
        self.intake(worktree=self.place)

    def fenced(self, start, end, fleet_end=None):
        self.native.pages, self.native.fleet = [start, end], [fleet_end or fleet_page()]
        return self.batch({"branch": "main"})


class FleetFenceTests(FencedCase):
    def test_a_changed_or_unproved_fleet_makes_the_fence_unstable(self):
        rows = [terminal("a", self.root / "other")]
        stable = self.fenced(terminal_page(rows), terminal_page(rows))
        self.assertTrue(stable["native_fence"]["stable"])
        ends = {"total change": fleet_page(total=4, counts={"retained": 1, "released": 3}),
                "counts change, same total": fleet_page(counts={"retained": 2, "released": 1}),
                "runtime change": fleet_page(runtime="other-runtime"),
                "bound fleet": fleet_page(scope={"source": "bound", "run": "run"}),
                "failed": PodError("orca_read_failed", "Orca read did not succeed")}
        for label, end in ends.items():
            with self.subTest(label):
                answer = self.fenced(terminal_page(rows), terminal_page(rows), end)
                fence = answer["native_fence"]
                self.assertTrue(fence["end"]["complete"])
                self.assertFalse(fence["stable"])
                self.assertEqual((answer["status"], answer["complete"]), ("unknown", False))
                self.assertEqual(answer["responses"], stable["responses"])
                if label == "bound fleet":
                    self.assertEqual(fence["fleet_end"], {"runtime": "runtime", "complete": False,
                                                          "code": "native_inventory_scope"})
                if label == "failed":
                    self.assertEqual(fence["fleet_end"]["code"], "orca_read_failed")

    def test_a_bound_caller_has_an_incomplete_fleet_fence(self):
        self.fleet_start = fleet_page(scope={"source": "bound", "run": "run"})
        rows = [terminal("a", self.root / "other")]
        answer = self.fenced(terminal_page(rows), terminal_page(rows), fleet_page(scope={"source": "bound", "run": "run"}))
        fence = answer["native_fence"]
        self.assertEqual(fence["fleet_start"], {"runtime": "runtime", "complete": False, "code": "native_inventory_scope"})
        self.assertFalse(fence["stable"])
        self.assertEqual(answer["status"], "unknown")


class FenceTests(FencedCase):

    def test_a_stable_fence_fingerprints_the_documented_rule(self):
        rows = [terminal("b", self.root / "elsewhere"), terminal("a", self.root / "other", agentIdentity=None)]
        answer = self.fenced(terminal_page(rows), terminal_page(list(reversed(rows))))
        fence = answer["native_fence"]
        self.assertTrue(fence["stable"])
        nine = ("handle", "incarnationId", "worktreePath", "worktreeId", "executionHostId", "agentIdentity",
                "connected", "writable", "orphaned")
        expected_rows = [{field: row.get(field) for field in nine} for row in sorted(rows, key=lambda row: row["handle"])]
        expected = hashlib.sha256(json.dumps({"runtime": "runtime", "terminals": expected_rows}, sort_keys=True,
                                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(fence["start"], {"runtime": "runtime", "complete": True, "count": 2, "sha256": expected})
        self.assertEqual(fence["end"], {**fence["start"], "terminals": expected_rows})
        self.assertEqual(fence["fleet_start"], {"runtime": "runtime", "complete": True, "total": 3,
                                                "counts": {"retained": 1, "released": 2}})
        self.assertEqual(fence["fleet_end"], fence["fleet_start"])
        self.assertNotIn("PRIVATE", json.dumps(answer))
        self.assertCountEqual(self.native.calls, [["terminal", "list", "--json"]] * 2 + [[*LIST, "--limit", "1"]])
        self.assertEqual(self.native.calls[0], ["terminal", "list", "--json"])  # the end reads come last
        self.assertTrue(answer["complete"])
        self.assertEqual(answer["status"], answer["responses"][0]["result"]["status"])

    def test_a_changed_or_incomplete_end_is_unstable_and_leaves_results_unchanged(self):
        rows = [terminal("a", self.root / "other")]
        stable = self.fenced(terminal_page(rows), terminal_page(rows))
        ends = {"connected flip": terminal_page([terminal("a", self.root / "other", connected=False)]),
                "new terminal": terminal_page([*rows, terminal("c", self.root / "other")]),
                "runtime change": terminal_page(rows, runtime="other-runtime"),
                "incomplete": terminal_page(rows, totalCount=2),
                "failed": PodError("orca_read_failed", "Orca read did not succeed")}
        for label, end in ends.items():
            with self.subTest(label):
                answer = self.fenced(terminal_page(rows), end)
                fence = answer["native_fence"]
                self.assertFalse(fence["stable"])
                self.assertEqual((answer["status"], answer["complete"]), ("unknown", False))
                self.assertEqual(answer["responses"], stable["responses"])
                if label in ("incomplete", "failed"):
                    self.assertFalse(fence["end"]["complete"])
                    self.assertNotIn("terminals", fence["end"])
                    self.assertNotIn("sha256", fence["end"])
                    self.assertEqual(fence["end"]["code"],
                                     "native_resources_incomplete" if label == "incomplete" else "orca_read_failed")
                else:
                    self.assertNotEqual(fence["start"]["sha256"], fence["end"]["sha256"])


def worker(dispatch, state, **fields):
    return {"dispatchId": dispatch, "terminalState": state, "workerState": "ready", **fields}


class FleetDouble:
    """Static worker-list answers keyed by state and cursor; every read is recorded."""

    def __init__(self, partitions, *, counts=None, total=None, runtime="runtime", sequential=None, overrides=None):
        self.partitions, self.runtime, self.overrides = partitions, runtime, overrides or {}
        self.counts = counts if counts is not None else {state: len(rows) for state, rows in partitions.items() if rows}
        self.total = sum(self.counts.values()) if total is None else total
        self.sequential = sequential
        self.calls, self.lock = [], threading.Lock()

    def page(self, rows, index, total):
        chunk = rows[index * 100:(index + 1) * 100]
        more = (index + 1) * 100 < len(rows)
        return {"runtime": self.runtime, "result": {"scope": {"source": "all"}, "counts": dict(self.counts),
                "workers": chunk, "page": {"limit": 100, "total": total, "hasMore": more,
                                           "nextCursor": f"c{index + 1}" if more else None}}}

    def __call__(self, argv):
        with self.lock:
            self.calls.append(list(argv))
        key = tuple(argv)
        if key in self.overrides:
            value = self.overrides[key]
            if isinstance(value, BaseException):
                raise value
            return value
        tail = argv[len(LIST):]
        index = int(tail[tail.index("--cursor") + 1][1:]) if "--cursor" in tail else 0
        if tail == ["--limit", "1"]:
            return {"runtime": self.runtime, "result": {"scope": {"source": "all"}, "counts": dict(self.counts),
                    "workers": [], "page": {"limit": 1, "total": self.total, "hasMore": self.total > 1}}}
        if "--terminal-state" in tail:
            state = tail[tail.index("--terminal-state") + 1]
            rows = self.partitions.get(state, [])
            return self.page(rows, index, len(rows))
        rows = self.sequential if self.sequential is not None else [row for rows in self.partitions.values() for row in rows]
        return self.page(rows, index, len(rows))


class PartitionTests(base.AdmissionQueryCase):
    def read(self, double):
        discovery = admission._Discovery()
        token = admission._SHARED.set(discovery)
        with read_deadline(Budget(5.0)), patch("pod.admission._read", side_effect=double):
            try:
                fleet = self._real_fleet()
            finally:
                admission._SHARED.reset(token)
                discovery.close()  # Waits for every read the walk started, inside the double.
        return fleet, discovery

    def fleet(self):
        return {"active": [worker("a1", "active")], "retained": [worker(f"t{i}", "retained") for i in range(130)],
                "released": [worker(f"r{i}", "released") for i in range(250)]}

    def test_a_complete_partition_union_is_accepted_in_a_fixed_order(self):
        double = FleetDouble(self.fleet())
        fleet, discovery = self.read(double)
        self.assertEqual(discovery.fleet_read, "partitioned")
        self.assertEqual([row["dispatchId"] for row in fleet["workers"]],
                         ["a1", *(f"t{i}" for i in range(130)), *(f"r{i}" for i in range(250))])
        self.assertEqual(fleet["runtime"], "runtime")
        self.assertNotIn([*LIST, "--limit", "100"], double.calls)  # no sequential walk
        self.assertEqual(double.calls[-1], [*LIST, "--limit", "1"])  # the count read comes after every partition
        self.assertEqual(len(double.calls), 1 + 1 + 1 + 2 + 3 + 1 + 1)  # six partitions' pages and the count read

    def test_every_partition_rejection_falls_back_to_the_sequential_walk(self):
        rows = self.fleet()
        moved = {**rows, "active": [worker("a1", "active"), worker("r0", "active")]}
        missing = {**rows, "active": []}
        renamed = {**rows, "active": [worker("a1", "released")]}
        state = [*LIST, "--limit", "100", "--terminal-state"]
        page = FleetDouble(rows).page
        cases = {
            "moved row in two partitions": FleetDouble(moved, counts={"active": 2, "retained": 130, "released": 250}, total=381),
            "moved row in no partition": FleetDouble(missing, counts={"active": 1, "retained": 130, "released": 250}, total=381),
            "row outside its partition": FleetDouble(renamed),
            "duplicate within a partition": FleetDouble({**rows, "active": [worker("a1", "active")] * 2}),
            "partition total changed": FleetDouble(rows, overrides={tuple(state + ["released", "--cursor", "c1"]):
                {**page(rows["released"], 1, 251)}}),
            "rows short of the total": FleetDouble(rows, overrides={tuple(state + ["active"]): page(rows["active"], 0, 2)}),
            "count changed": FleetDouble(rows, counts={"active": 1, "retained": 131, "released": 250}),
            "created during the read": FleetDouble(rows, total=382),
            "deleted during the read": FleetDouble(rows, overrides={tuple(state + ["retained"]): {
                **page(rows["retained"], 0, 130), "result": {**page(rows["retained"], 0, 130)["result"],
                    "counts": {"active": 1, "retained": 131, "released": 250}}}}),
            "new terminal state": FleetDouble(rows, counts={"active": 1, "retained": 130, "released": 250, "paused": 0}),
            "runtime changed": FleetDouble(rows, overrides={tuple(state + ["active"]): {**page(rows["active"], 0, 1), "runtime": "other"}}),
            "partition read failed": FleetDouble(rows, overrides={tuple(state + ["reclaimable"]): PodError("orca_read_failed", "no")}),
        }
        for label, double in cases.items():
            sequential = [row for part in rows.values() for row in part]
            double.sequential = sequential
            with self.subTest(label):
                fleet, discovery = self.read(double)
                self.assertEqual(discovery.fleet_read, "sequential")
                self.assertIsNotNone(discovery.fleet_fallback)
                self.assertIn([*LIST, "--limit", "100"], double.calls)
                self.assertEqual(len(fleet["workers"]), 381)

    def test_a_bound_first_page_answers_none_without_a_fallback(self):
        bound = {"runtime": "runtime", "result": {"scope": {"source": "bound", "run": "run"}}}
        double = FleetDouble(self.fleet(), overrides={tuple([*LIST, "--limit", "100", "--terminal-state", "retained"]): bound})
        fleet, discovery = self.read(double)
        self.assertIsNone(fleet)
        self.assertEqual(discovery.fleet_read, "bound")
        self.assertNotIn([*LIST, "--limit", "100"], double.calls)
        self.assertNotIn([*LIST, "--limit", "1"], double.calls)

    def test_abandoned_rows_start_their_claim_reads_during_the_walk(self):
        rows = self.fleet()
        rows["active"] = [worker("a1", "active", workerState="abandoned", runId="run", taskId="task",
                                 projection={"id": "w"})]
        shown = {"runtime": "runtime", "result": {"dispatch": {"id": "a1", "runId": "run", "taskId": "task"},
                 "worker": {}, "projection": {"id": "w"}}}
        double = FleetDouble(rows, overrides={tuple(["orchestration", "worker-show", "--dispatch", "a1", "--json"]): shown})
        with patch("pod.abandoned.is_abandoned", return_value=True), \
             patch("pod.abandoned.current_claim", return_value={"process": "exited", "effects": []}):
            fleet, discovery = self.read(double)
        self.assertEqual(discovery.fleet_read, "partitioned")
        self.assertIn(["orchestration", "worker-show", "--dispatch", "a1", "--json"], double.calls)
        self.assertEqual(discovery.done(("claim", "a1", "runtime")), {"process": "exited", "effects": []})

    def test_the_allowlist_accepts_exactly_the_partition_and_count_reads(self):
        from pod.orca import _read_allowed
        self.assertTrue(_read_allowed([*LIST, "--limit", "1"]))
        self.assertTrue(_read_allowed([*LIST, "--limit", "100", "--terminal-state", "released"]))
        self.assertTrue(_read_allowed([*LIST, "--limit", "100", "--terminal-state", "released", "--cursor", "c"]))
        self.assertFalse(_read_allowed([*LIST, "--limit", "100", "--terminal-state", "paused"]))
        self.assertFalse(_read_allowed([*LIST, "--limit", "100", "--terminal-state"]))
        self.assertFalse(_read_allowed([*LIST, "--limit", "2"]))
        self.assertFalse(_read_allowed([*LIST, "--limit", "1", "--cursor", "c"]))

    def test_the_fleet_start_is_the_read_that_closed_discovery(self):
        double = FleetDouble({"retained": [worker("t1", "retained")]})
        _, discovery = PartitionTests.read(self, double)
        self.assertEqual(admission._fleet_side(discovery.fleet_start),
                         {"runtime": "runtime", "complete": True, "total": 1, "counts": {"retained": 1}})
        double = FleetDouble({"retained": [worker("t1", "retained")]}, total=2)  # partitions refused
        _, discovery = PartitionTests.read(self, double)
        self.assertEqual(discovery.fleet_read, "sequential")
        self.assertEqual(admission._fleet_side(discovery.fleet_start)["total"], 1)  # the sequential first page

