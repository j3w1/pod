"""R207: the batch admission query shares one discovery, one deadline and one change recheck."""
from contextlib import redirect_stdout
import hashlib
from io import BytesIO, StringIO, TextIOWrapper
import json
import shutil
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


class NativeDouble:
    """Orca reads the batch makes past the patched inventories: terminal lists, served in order, and the
    fence's fleet reads, answered by a FleetDouble."""

    def __init__(self, *pages, fleet=None):
        self.pages, self.fleet, self.calls, self.lock = list(pages), fleet, [], threading.Lock()

    def __call__(self, argv):
        with self.lock:
            self.calls.append(list(argv))
            if argv[:len(LIST)] == LIST and self.fleet is not None:
                fleet = self.fleet
            elif argv[:2] == ["terminal", "list"]:
                page = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
                fleet = None
            else:
                raise AssertionError(f"unexpected native read {argv}")
        if fleet is not None:
            return fleet(argv)
        if isinstance(page, BaseException):
            raise page
        return page


class BatchCase(base.AdmissionQueryCase):
    def setUp(self):
        super().setUp()
        self.native = NativeDouble(terminal_page([]), fleet=FleetDouble({}))
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
        # Discovery's terminal list, then the fence's terminal list, newest page and five unreleased
        # partitions; the discovery fleet is a double.
        self.assertEqual(answer["discovery"]["orca_reads"], 8)
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

    def test_a_record_removed_during_discovery_is_an_inventory_change(self):
        self.intake(worktree=self.place)
        self.write([self.criterion()], objective="second", governance={"base_ref": "target"},
                   worktree={**self.place, "branch": "second-branch"})
        directory = objective_root(self.project, "second")
        real = admission._after_cohort
        def remove(root, budget):
            shutil.rmtree(directory)
            return real(root, budget)
        with patch("pod.admission._after_cohort", side_effect=remove):
            answer = self.batch({}, {"branch": "main"})
        for row in answer["responses"]:
            self.assertIn("inventory_changed", [unknown["code"] for unknown in row["result"]["unknowns"]])
            self.assertFalse(row["result"]["complete"])

    def test_an_effect_policy_change_during_the_call_is_reported(self):
        self.intake(worktree=self.place)
        base.ReviewCorrectionTests.journal(self, kind="push", target="main")  # mapped through the effect policy
        def effective(project):
            stage = admission._SHARED.get().stage
            return {"revision": 1 if stage == "first" else 2, "policy": {"waste_governor": {"triggers": {}}}}
        with patch("pod.config.effective", side_effect=effective) as reads:
            answer = self.batch({"effects": ["push:main"]}, {"target": "main"})
        self.assertEqual(reads.call_count, 2)  # once in discovery, once in the shared recheck
        changed = [row["result"] for row in answer["responses"]
                   if "effect_policy_changed" in [unknown["code"] for unknown in row["result"]["unknowns"]]]
        self.assertTrue(changed)
        self.assertTrue(all(result["complete"] is False for result in changed))

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
        # One current-claim read shared by every query that needs it, and the fence's fresh end read.
        self.assertEqual(reader.call_count, 2)
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

    def test_a_fault_inside_one_projection_prints_no_partial_batch(self):
        self.intake(worktree=self.place)
        request = json.dumps(self.request(queries=[{"id": "one", "repository": str(self.project)},
                                                   {"id": "two", "repository": str(self.project), "branch": "main"}])).encode()
        real = admission._claim_identity
        def fail(raw, rows):
            raise RuntimeError("projection fault")
        with patch("pod.admission._claim_identity", side_effect=fail), self.assertRaises(RuntimeError):
            self.run_cli(["--batch", "--json"], request)
        self.assertEqual(self.last_output.getvalue(), "")  # nothing, not a partial answer, reached stdout
        self.assertEqual([thread for thread in threading.enumerate() if thread.name.startswith("pod-admission")], [])
        self.assertIs(admission._claim_identity, real)

    def test_the_whole_response_limit_refuses_the_batch(self):
        self.intake(worktree=self.place)
        with patch("pod.admission.MAX_RESPONSE_BYTES", 1000), self.assertRaises(PodError) as caught:
            self.batch({}, {"branch": "main"})
        self.assertEqual(caught.exception.code, "admission_batch_projection_limit")

    def run_cli(self, argv, data=b""):
        stdin = TextIOWrapper(BytesIO(data))
        out = self.last_output = StringIO()
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


LIVE_CLAIM = {"process": "live", "effects": []}


class FencedCase(BatchCase):
    """Discovery reads a start fleet; the fence's end reads come from a separate fleet double."""

    def setUp(self):
        super().setUp()
        real = patch("pod.admission._terminals", side_effect=self._real_terminals)
        real.start(); self.addCleanup(real.stop)
        self.rows = [self.row("a1", "active"), self.row("t1", "retained"), self.row("t2", "retained", workerState="abandoned"),
                     *(self.row(f"r{index}", "released") for index in range(120))]
        self.claim_values = {}
        def fleet():
            start = fleet_model(self.rows)
            admission._SHARED.get().fleet_start = {"page": start([*LIST, "--limit", "100"]), "rows": list(self.rows)}
            return {"runtime": "runtime", "complete": True, "workers": list(self.rows)}
        native = patch("pod.admission._fleet", side_effect=fleet)
        native.start(); self.addCleanup(native.stop)
        def claim(source, runtime):
            values = self.claim_values[source["dispatchId"]]
            return values.pop(0) if len(values) > 1 else values[0]
        claims = patch("pod.admission._native_claim", side_effect=claim)
        claims.start(); self.addCleanup(claims.stop)
        self.intake(worktree=self.place)

    def row(self, dispatch, state, **fields):
        """A worker of an unrecorded Run outside the repository: it holds no claim of these queries."""
        row = base.ReviewCorrectionTests.writer(self, path=self.root / "elsewhere", verdict="exited", dispatch=dispatch)
        row.update(runId="other-run", terminalState=state, workerState="ready", dispatchStatus="completed")
        row["projection"]["runId"] = "other-run"
        row.update(fields)
        return row

    def fenced(self, start=None, end=None, fleet_end=None, claims=None):
        terminals = [terminal("a", self.root / "other")]
        self.native.pages = [start or terminal_page(terminals), end or terminal_page(terminals)]
        self.native.fleet = fleet_end or fleet_model(self.rows)
        self.claim_values = claims or {"t2": [LIVE_CLAIM]}
        return self.batch({"branch": "main"})

    def assert_unstable(self, answer, stable):
        self.assertFalse(answer["native_fence"]["stable"])
        self.assertEqual((answer["status"], answer["complete"]), ("unknown", False))
        self.assertEqual(answer["responses"], stable["responses"])  # the results themselves are unchanged


def identity(row):
    """The documented row identity, computed independently of Pod."""
    def part(value, *names):
        return {name: (value or {}).get(name) for name in names}
    projection = row.get("projection") or {}
    return {**part(row, "dispatchId", "runId", "taskId", "terminalState", "workerState", "dispatchStatus",
                   "agentTerminalHandle"),
            "resource": part(row.get("resource"), "id", "ownerDispatchId", "ownershipState", "releaseState",
                             "terminalHandle", "worktreeId"),
            "projection": {"id": projection.get("id"), "liveness": part(projection.get("liveness"), "verdict"),
                           "workspace": part(projection.get("workspace"), "id"),
                           "resource": part(projection.get("resource"), "state")}}


def canonical(runtime, rows):
    return hashlib.sha256(json.dumps({"runtime": runtime, "rows": rows}, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class FleetFenceTests(FencedCase):
    def test_an_unchanged_fleet_fence_is_stable_and_digests_the_documented_rows(self):
        answer = self.fenced()
        fence = answer["native_fence"]
        self.assertTrue(fence["stable"], fence)
        unreleased = sorted((identity(row) for row in self.rows if row["terminalState"] != "released"),
                            key=lambda row: row["dispatchId"])
        newest = sorted((identity(row) for row in self.rows[:100]), key=lambda row: row["dispatchId"])
        self.assertEqual(fence["fleet_start"], {
            "runtime": "runtime", "complete": True, "total": 123, "counts": {"active": 1, "retained": 2, "released": 120},
            "unreleased": 3, "unreleased_sha256": canonical("runtime", unreleased),
            "newest": 100, "newest_sha256": canonical("runtime", newest),
            "claims_read": 1, "claims_sha256": canonical("runtime", [{"dispatch": "t2", **LIVE_CLAIM}])})
        self.assertEqual(fence["fleet_end"], fence["fleet_start"])
        self.assertTrue(answer["complete"])
        reads = self.native.calls
        self.assertIn([*LIST, "--limit", "100"], reads)
        self.assertNotIn([*LIST, "--limit", "100", "--terminal-state", "released"], reads)  # released is never re-read

    def test_each_fleet_change_between_discovery_and_the_end_is_unstable(self):
        stable = self.fenced()
        created = self.row("c1", "active")
        settled = [dict(row, terminalState="released") if row["dispatchId"] == "t1" else row for row in self.rows]
        changed = [dict(row, dispatchStatus="running") if row["dispatchId"] == "t1" else row for row in self.rows]
        # A balanced sequence: create C (retained), settle A (t1), delete B (an old released row).
        balanced = [self.row("c1", "retained"), *(row for row in settled if row["dispatchId"] != "r119")]
        cases = {"created": (fleet_model([created, *self.rows]), None),
                 "settled into released": (fleet_model(settled), None),
                 "deleted": (fleet_model(self.rows[:-1]), None),
                 "changed identity field": (fleet_model(changed), None),
                 "changed abandoned claim": (None, {"t2": [LIVE_CLAIM, {"process": "exited", "effects": ["push:main"]}]}),
                 "balanced create, settle and delete": (fleet_model(balanced), None),
                 "runtime change": (fleet_model(self.rows, runtime="other-runtime"), None)}
        for label, (fleet, claims) in cases.items():
            with self.subTest(label):
                answer = self.fenced(fleet_end=fleet, claims=claims)
                fence = answer["native_fence"]
                self.assert_unstable(answer, stable)
                if label == "balanced create, settle and delete":
                    self.assertEqual((fence["fleet_end"]["total"], fence["fleet_end"]["counts"]),
                                     (fence["fleet_start"]["total"], fence["fleet_start"]["counts"]))
                    self.assertNotEqual(fence["fleet_end"]["unreleased_sha256"], fence["fleet_start"]["unreleased_sha256"])

    def test_a_released_row_changing_outside_the_newest_page_is_the_documented_residual(self):
        changed = [dict(row, workerState="unsupervised") if row["dispatchId"] == "r110" else row for row in self.rows]
        self.assertGreaterEqual(changed.index(next(row for row in changed if row["dispatchId"] == "r110")), 100)
        answer = self.fenced(fleet_end=fleet_model(changed))
        self.assertTrue(answer["native_fence"]["stable"])  # Not covered by design: see R207's residual.

    def test_an_unproved_end_or_a_bound_caller_is_incomplete(self):
        stable = self.fenced()
        page = [*LIST, "--limit", "100"]
        bound = {"runtime": "runtime", "result": {"scope": {"source": "bound", "run": "run"}}}
        cases = {"bound end": (FleetDouble({}, overrides={tuple(page): bound}), "native_inventory_scope"),
                 "failed end": (FleetDouble({}, overrides={tuple(page): PodError("orca_read_failed", "no")}), "orca_read_failed"),
                 "failed partition": (fleet_model(self.rows, overrides={tuple([*page, "--terminal-state", "retained"]):
                                                                        PodError("orca_read_failed", "no")}), "orca_read_failed")}
        for label, (fleet, code) in cases.items():
            with self.subTest(label):
                answer = self.fenced(fleet_end=fleet)
                self.assertEqual(answer["native_fence"]["fleet_end"]["code"], code)
                self.assertFalse(answer["native_fence"]["fleet_end"]["complete"])
                self.assert_unstable(answer, stable)
        with patch("pod.admission._fleet", side_effect=lambda: setattr(admission._SHARED.get(), "fleet_start",
                   PodError("native_inventory_scope", "bound"))):
            answer = self.fenced()
        self.assertEqual(answer["native_fence"]["fleet_start"],
                         {"runtime": None, "complete": False, "code": "native_inventory_scope"})
        self.assertFalse(answer["native_fence"]["stable"])


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
        self.assertNotIn("PRIVATE", json.dumps(answer))
        terminal_reads = [call for call in self.native.calls if call[:2] == ["terminal", "list"]]
        self.assertEqual(terminal_reads, [["terminal", "list", "--json"]] * 2)
        self.assertEqual(self.native.calls[0], ["terminal", "list", "--json"])  # the end reads come last
        self.assertTrue(answer["complete"])
        self.assertEqual(answer["status"], answer["responses"][0]["result"]["status"])

    def test_each_terminal_change_or_an_incomplete_end_is_unstable(self):
        rows = [terminal("a", self.root / "other")]
        stable = self.fenced(terminal_page(rows), terminal_page(rows))
        ends = {"connected flip": terminal_page([terminal("a", self.root / "other", connected=False)]),
                "new terminal": terminal_page([*rows, terminal("c", self.root / "other")]),
                "terminal gone": terminal_page([]),
                "incarnation change": terminal_page([terminal("a", self.root / "other", incarnationId="inc-2")]),
                "location change": terminal_page([terminal("a", self.root / "moved")]),
                "runtime change": terminal_page(rows, runtime="other-runtime"),
                "incomplete": terminal_page(rows, totalCount=2),
                "failed": PodError("orca_read_failed", "Orca read did not succeed")}
        for label, end in ends.items():
            with self.subTest(label):
                answer = self.fenced(terminal_page(rows), end)
                fence = answer["native_fence"]
                self.assert_unstable(answer, stable)
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


def fleet_model(rows, **fields):
    """A consistent fleet whose newest-first order is `rows`."""
    partitions = {}
    for row in rows:
        partitions.setdefault(row["terminalState"], []).append(row)
    return FleetDouble(partitions, sequential=list(rows), **fields)


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
        if "--terminal-state" in tail:
            state = tail[tail.index("--terminal-state") + 1]
            rows = self.partitions.get(state, [])
            return self.page(rows, index, len(rows))
        # Unfiltered pages: Orca's newest-first walk, carrying the fleet's total and counts.
        rows = self.sequential if self.sequential is not None else [row for rows in self.partitions.values() for row in rows]
        return self.page(rows, index, self.total)


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
        self.assertNotIn([*LIST, "--limit", "100", "--cursor", "c1"], double.calls)  # no sequential walk
        self.assertEqual(double.calls[-1], [*LIST, "--limit", "100"])  # the newest page comes after every partition
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
                self.assertIn([*LIST, "--limit", "100", "--cursor", "c1"], double.calls)  # the sequential walk
                self.assertEqual(len(fleet["workers"]), 381)

    def test_a_bound_first_page_answers_none_without_a_fallback(self):
        bound = {"runtime": "runtime", "result": {"scope": {"source": "bound", "run": "run"}}}
        double = FleetDouble(self.fleet(), overrides={tuple([*LIST, "--limit", "100", "--terminal-state", "retained"]): bound})
        fleet, discovery = self.read(double)
        self.assertIsNone(fleet)
        self.assertEqual(discovery.fleet_read, "bound")
        self.assertNotIn([*LIST, "--limit", "100"], double.calls)

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
        self.assertTrue(_read_allowed([*LIST, "--limit", "100"]))
        self.assertTrue(_read_allowed([*LIST, "--limit", "100", "--terminal-state", "released"]))
        self.assertTrue(_read_allowed([*LIST, "--limit", "100", "--terminal-state", "released", "--cursor", "c"]))
        self.assertFalse(_read_allowed([*LIST, "--limit", "100", "--terminal-state", "paused"]))
        self.assertFalse(_read_allowed([*LIST, "--limit", "100", "--terminal-state"]))
        self.assertFalse(_read_allowed([*LIST, "--limit", "2"]))
        self.assertFalse(_read_allowed([*LIST, "--limit", "1", "--cursor", "c"]))

    def test_the_fleet_start_is_the_read_that_closed_discovery(self):
        double = FleetDouble({"retained": [worker("t1", "retained")], "released": [worker("r1", "released")]})
        _, discovery = self.read(double)
        side = admission._fleet_side(discovery.fleet_start, {})
        self.assertEqual((side["total"], side["counts"], side["unreleased"], side["newest"], side["claims_read"]),
                         (2, {"retained": 1, "released": 1}, 1, 2, 0))
        self.assertEqual(discovery.fleet_start["page"], double([*LIST, "--limit", "100"]))  # the newest page closed it
        double = FleetDouble({"retained": [worker("t1", "retained")]}, total=2)  # partitions refused
        _, discovery = self.read(double)
        self.assertEqual(discovery.fleet_read, "sequential")
        self.assertEqual(admission._fleet_side(discovery.fleet_start, {})["total"], 2)  # the sequential first page


class PreexistingFaultTests(BatchCase):
    def test_an_unlocated_foreign_worker_and_a_stale_runtime_answer_normally(self):
        """Before 0.21.31 the unlocated worker rebound the owners map to a set and the stale runtime
        check then raised AttributeError, taking down the query (and in a batch, every query)."""
        self.intake(worktree=self.place)
        path, raw = self.raw()
        raw["checkpoint"]["native_refs"][0]["runtime"] = "previous-runtime"
        self.save(path, raw)
        unlocated = base.ReviewCorrectionTests.writer(self, verdict="live", dispatch="unlocated")
        unlocated.update(runId="other-run", resource={"worktreeId": None})
        unlocated["projection"].update(runId="other-run", workspace={"id": None})
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": [unlocated]}):
            single = self.query()
            answer = self.batch({}, {"worktree": str(self.project), "branch": "main"})
        self.assertEqual(answer["responses"][0]["result"], single)
        for result in (single, answer["responses"][1]["result"]):
            codes = [row["code"] for row in result["unknowns"]] + list(result["unrelated_unknowns"]["by_code"])
            self.assertIn("native_location_unavailable", codes)
            self.assertIn("native_runtime_changed", codes)
