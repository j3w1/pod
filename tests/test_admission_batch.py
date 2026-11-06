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

LIST = ["orchestration", "worker-list", "--include-remote", "--json", "--full"]
LIVE = {"connected": True, "writable": True, "orphaned": False}
RUNS = {"runtime": "runtime", "result": {"runs": [], "nextCursor": None}}


def without_census(result):
    """R213 clears only its new witness on an unstable batch fence; all preceding fields still agree."""
    return {key: value for key, value in result.items() if key != "record_census"}


def terminal_page(rows, runtime="runtime", **whole):
    return {"runtime": runtime, "result": {"terminals": rows, "totalCount": len(rows), "truncated": False,
            "hostScope": {"hostIds": ["local"], "omittedHostIds": []}, **whole}}


def terminal(handle, path, **fields):
    return {"handle": handle, "incarnationId": "inc-" + handle, "worktreePath": str(path), "worktreeId": "id:" + handle,
            "executionHostId": "local", "agentIdentity": "codex", **LIVE, "preview": "PRIVATE", **fields}


class NativeDouble:
    """Orca reads the batch makes past the patched inventories: terminal lists, served in order, an empty complete
    Run list, and the fence's fleet reads, answered by a FleetDouble."""

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
            elif argv[:2] == ["orchestration", "run-list"]:
                page, fleet = RUNS, None  # rule S's Run-coordinator list, read before the end fence
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
        self.assertEqual(without_census(answer["responses"][0]["result"]), without_census(single))
        self.assertFalse(answer["responses"][0]["result"]["record_census"]["complete"])
        self.assertTrue(single["record_census"]["complete"])
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
        # Discovery's terminal list and one Run list, then the fence's terminal list; the discovery fleet is a double
        # with no walk, so the fence has no walk page to read again (its fleet side is incomplete).
        self.assertEqual(answer["discovery"]["orca_reads"], 3)
        self.assertEqual(answer["native_fence"]["fleet_end"]["code"], "native_fence_unavailable")
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


class ClaimDemandTests(BatchCase):
    """A1018: an abandoned Dispatch's current claim is read only when some query's answer can use it, and every
    result equals both its single query and the result of reading every abandoned row's claim."""

    def rows(self):
        here = base.ReviewCorrectionTests.writer(self, dispatch="here")
        away = base.ReviewCorrectionTests.writer(self, path=self.root / "elsewhere", dispatch="away")
        plain = base.ReviewCorrectionTests.writer(self, path=self.root / "elsewhere", dispatch="plain")
        here["workerState"] = away["workerState"] = "abandoned"
        return [here, away, plain]

    def answer(self, queries, demanded=None):
        rows, read = self.rows(), []
        claim = {"process": "live", "effects": []}
        def reader(source, runtime):
            read.append(source["dispatchId"])
            return claim
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": rows}), \
             patch("pod.admission._native_claim", side_effect=reader), \
             patch("pod.admission._claim_demanded", side_effect=demanded or admission._claim_demanded):
            answer = self.batch(*queries)
            singles = [self.query(**query) for query in queries]
        return answer, singles, set(read)

    def test_claims_are_read_only_where_an_answer_can_use_them_with_unchanged_results(self):
        self.intake(worktree=self.place)
        everything = lambda slot, source, places: (source.get("workerState") == "abandoned"
                                                   or source.get("dispatchId") in slot.abandoned)
        shapes = {"worktree only": [{"worktree": str(self.project)}], "branch": [{"branch": "main"}],
                  "effects": [{"effects": ["deploy:target"]}],
                  "mixed": [{"worktree": str(self.project)}, {"branch": "main"}, {"effects": ["deploy:target"]}]}
        for label, queries in shapes.items():
            with self.subTest(label):
                answer, singles, read = self.answer(queries)
                previous, _, all_read = self.answer(queries, everything)
                self.assertEqual(all_read, {"here", "away"})  # what every call read before
                self.assertNotIn("plain", read)
                if label == "worktree only":
                    self.assertEqual(read, {"here"})  # the other worktree's abandoned row cannot be relevant
                else:
                    self.assertIn("away", read)  # a branch nothing records for it may be the requested one
                for row, before, single in zip(answer["responses"], previous["responses"], singles):
                    self.assertEqual(row["result"], before["result"])
                    self.assertEqual(without_census(row["result"]), without_census(single))

    def test_a_record_accounted_abandoned_row_is_judged_like_a_native_one(self):
        self.intake(worktree=self.place)
        rows = self.rows()
        accounted = []
        real = admission._claim_demanded
        def spy(slot, source, places):
            value = real(slot, source, places)
            accounted.append((source["dispatchId"], source["dispatchId"] in slot.abandoned, value))
            return value
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": rows}), \
             patch("pod.admission._native_claim", return_value={"process": "live", "effects": []}), \
             patch("pod.admission._claim_demanded", side_effect=spy):
            self.batch({"worktree": str(self.project)})
        self.assertIn(("here", False, True), accounted)
        self.assertIn(("away", False, False), accounted)
        slot = admission._HostExitSlot(self.project.resolve(), {"worktree": None, "branch": "main", "target": None,
                                                                 "effects": []}, set(), set(), set(), {"plain"},
                                       scope={"repo_key": "key"}, owed={}, owners=None, unreadable=None)
        self.assertTrue(admission._claim_demanded(slot, rows[2], {}))  # accounted abandoned by this query's records
        self.assertFalse(admission._claim_demanded(slot, {**rows[2], "projection": None}, {}))  # refused as malformed


class WorkerFactTests(BatchCase):
    """A1020: each fleet row's worker facts are derived once per call, each query receives its own copy, and every
    result still equals its single query."""

    def test_worker_facts_are_derived_once_per_call(self):
        self.intake(worktree=self.place)
        rows = [base.ReviewCorrectionTests.writer(self, path=self.root / f"w{index}", dispatch=f"d{index}", verdict="live")
                for index in range(5)]
        rows.append(base.ReviewCorrectionTests.writer(self, dispatch="mine", verdict="live"))
        shapes = [{"worktree": str(self.project)}, {"branch": "main"}, {}, {"effects": ["deploy:target"]}]
        tables = []
        real = admission._fact_table
        def counted(rows):
            tables.append(len(rows))
            return real(rows)
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": rows}):
            singles = [self.query(**shape) for shape in shapes]
            with patch("pod.admission._fact_table", side_effect=counted):
                answer = self.batch(*shapes)
                again = self.batch(*shapes)
        self.assertEqual(tables, [6, 6])  # once per call, never across calls
        for row, single, repeated in zip(answer["responses"], singles, again["responses"]):
            self.assertEqual(without_census(row["result"]), without_census(single))
            self.assertEqual(row["result"], repeated["result"])
        fleet = {"runtime": "runtime", "complete": True, "workers": rows}
        discovery = admission._Discovery()
        token = admission._SHARED.set(discovery)
        try:
            discovery.memo(("fleet",), lambda: fleet, stage="first")
            first, second = admission._shown(rows[0]), admission._shown(rows[0])
            first["liveness"], first["resource"]["id"] = "exited", "changed"
            self.assertEqual((second["liveness"], second["resource"]["id"]), ("live", None))
            self.assertEqual(admission._shown(rows[0]), admission._worker_fact(rows[0]))
            outside = dict(rows[0])
            self.assertEqual(admission._shown(outside), admission._worker_fact(outside))  # not this call's row
            with self.assertRaises(PodError):
                admission._shown({**rows[1], "projection": None})
        finally:
            admission._SHARED.reset(token)
            discovery.close()


class EarlyStartTests(BatchCase):
    """R207 (A861): in a batch the Run list starts once the terminal inventory is known, and each Run owner read once
    a record pass names it, while discovery's walk still runs."""

    def test_the_run_list_and_the_run_owners_start_while_the_walk_runs(self):
        self.intake(worktree=self.place)
        events = {"run-list": threading.Event(), "owner": threading.Event()}
        native = self.native
        def read(argv):
            if argv[:2] == ["orchestration", "run-list"]:
                events["run-list"].set()
            return native(argv)
        def owner(run, terminals, runtime):
            events["owner"].set()
            return {"run": run, "runtime": runtime, "terminal": None, "consumer_generation": 1, "worktree": None,
                    "live": False, "source": "native_run_and_terminal"}
        seen = {}
        def fleet():
            for name, event in events.items():
                seen[name] = event.wait(5)  # Set by reads that must not wait for this walk; the bound only ends a failure.
            return {"runtime": "runtime", "complete": True, "workers": []}
        with patch("pod.admission._read", side_effect=read), \
             patch("pod.admission._owner_observation", side_effect=owner), \
             patch("pod.admission._fleet", side_effect=fleet):
            answer = self.batch({"worktree": str(self.project), "branch": "main"})
        self.assertEqual(seen, {"run-list": True, "owner": True})
        self.assertIn("native_fence", answer)
        self.assertEqual([thread for thread in threading.enumerate() if thread.name.startswith("pod-admission")], [])


    def test_owners_named_before_the_terminal_inventory_start_when_it_completes(self):
        """The reversed order: the first query names its Run while the terminal list is pending; that owner read
        starts when the list completes, before the second query's record pass, with no read after the fence."""
        self.intake(worktree=self.place)
        release, owner_started, seen = threading.Event(), threading.Event(), {}
        real_terminals, real_query = admission._terminals, admission._query
        def terminals():
            release.wait(5)  # Held until the first query has named its Run; the bound only ends a failure.
            return real_terminals()
        def owner(run, inventory, runtime):
            owner_started.set()
            return {"run": run, "runtime": runtime, "terminal": None, "consumer_generation": 1, "worktree": None,
                    "live": False, "source": "native_run_and_terminal"}
        queries = []
        def query(*args, **kwargs):
            index = len(queries); queries.append(index)
            generator = real_query(*args, **kwargs)
            value = next(generator)
            while True:
                yield value
                if index == 1 and not release.is_set():  # the second query's record pass is about to run
                    seen["named_before_terminals"] = bool(admission._SHARED.get().owner_runs)
                    release.set()
                    seen["owner_before_second_pass"] = owner_started.wait(5)
                try:
                    value = next(generator)
                except StopIteration as done:
                    return done.value
        with patch("pod.admission._terminals", side_effect=terminals), \
             patch("pod.admission._owner_observation", side_effect=owner), \
             patch("pod.admission._query", side_effect=query):
            answer = self.batch({"worktree": str(self.project), "branch": "main"},
                                {"worktree": str(self.project), "branch": "main"})
        self.assertEqual(seen, {"named_before_terminals": True, "owner_before_second_pass": True})
        for row in answer["responses"]:
            self.assertNotIn("native_read_after_fence", [unknown["code"] for unknown in row["result"]["unknowns"]])
        self.assertEqual([thread for thread in threading.enumerate() if thread.name.startswith("pod-admission")], [])


class CallSharingTests(BatchCase):
    """R207 (A862): the local facts every query repeats are derived once per call, and each result still equals its
    single query."""

    def test_repeated_local_facts_are_derived_once_per_call(self):
        self.scenario()
        shapes = [{"worktree": str(self.project)}, {"branch": "main"}, {"worktree": str(self.project), "branch": "main"},
                  {}, {"effects": ["deploy:target"]}, {"expect": "0" * 64}]
        known = [str(self.project)]
        singles = [self.query(**shape, known_repositories=known) for shape in shapes]
        calls = {}
        def counted(name):
            real = getattr(admission, name)
            def wrapper(*args, **kwargs):
                calls.setdefault(name, []).append(tuple(str(arg) for arg in args))
                return real(*args, **kwargs)
            return patch(f"pod.admission.{name}", side_effect=wrapper)
        with counted("state_root"), counted("_resolve"), counted("_after_context_read"), \
             counted("_after_journal_read"), counted("_terminal_validated"):
            answer = self.batch(*shapes, known=known)
        for shape, single, row in zip(shapes, singles, answer["responses"]):
            with self.subTest(shape=shape):
                self.assertEqual(row["result"], single)
        self.assertEqual(len(calls["state_root"]), 1)  # one repository, six queries
        for name in ("_resolve", "_after_context_read", "_after_journal_read"):
            with self.subTest(name):
                self.assertTrue(calls.get(name))
                self.assertEqual(len(calls[name]), len(set(calls[name])), "derived twice in one call")
        self.assertEqual(len(calls.get("_terminal_validated", [])), len(set(calls.get("_terminal_validated", []))))


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
        self.assertEqual(set(answer["discovery"]),
                         {"elapsed_s", "native_workers", "records", "fleet_read", "orca_reads", "host_exit"})
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
            admission._SHARED.get().fleet_start = {"page": start([*LIST, "--limit", "100"]), "rows": list(self.rows),
                                                   "pages": walk_pages(self.rows)}
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

    def fenced(self, start=None, end=None, fleet_end=None, claims=None, query=None):
        terminals = [terminal("a", self.root / "other")]
        self.native.pages = [start or terminal_page(terminals), end or terminal_page(terminals)]
        self.native.fleet = fleet_end or fleet_model(self.rows)
        self.claim_values = claims or {"t2": [LIVE_CLAIM]}
        return self.batch(query or {"branch": "main"})

    def assert_unstable(self, answer, stable):
        self.assertFalse(answer["native_fence"]["stable"])
        self.assertEqual((answer["status"], answer["complete"]), ("unknown", False))
        preceding = lambda response: [{**row, "result": without_census(row["result"])}
                                      for row in response["responses"]]
        self.assertEqual(preceding(answer), preceding(stable))
        self.assertTrue(all(row["result"]["record_census"] == admission._no_census()
                            for row in answer["responses"]))


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
        released = sorted((identity(row) for row in self.rows if row["terminalState"] == "released"),
                          key=lambda row: row["dispatchId"])
        newest = sorted((identity(row) for row in self.rows[:100]), key=lambda row: row["dispatchId"])
        self.assertEqual(fence["fleet_start"], {
            "runtime": "runtime", "complete": True, "total": 123, "counts": {"active": 1, "retained": 2, "released": 120},
            "unreleased": 3, "unreleased_sha256": canonical("runtime", unreleased),
            "released": 120, "released_sha256": canonical("runtime", released),
            "newest": 100, "newest_sha256": canonical("runtime", newest),
            "pages": 2, "pages_sha256": canonical("runtime", [[page["ids"], page["more"], None, False]
                                                             for page in walk_pages(self.rows)]),
            "claims_read": 1, "claims_sha256": canonical("runtime", [{"dispatch": "t2", **LIVE_CLAIM}]),
            "host_exit": 0, "host_exit_sha256": canonical("runtime", [])})  # R212: nothing counted exited
        self.assertEqual(fence["fleet_end"], fence["fleet_start"])
        self.assertTrue(answer["complete"])
        reads = self.native.calls
        self.assertIn([*LIST, "--limit", "100"], reads)
        # Every page of the walk is read again from its own cursor; no partition is read (pod#267).
        self.assertIn([*LIST, "--limit", "100", "--cursor", "c1"], reads)
        self.assertEqual([call for call in reads if "--terminal-state" in call], [])

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

    def test_end_reads_start_only_after_every_native_read_a_result_uses(self):
        events, lock = [], threading.Lock()
        def logged(name, fn):
            def call(*args):
                with lock:
                    events.append(("start", name, admission._FENCING.get()))
                try:
                    return fn(*args)
                finally:
                    with lock:
                        events.append(("end", name, admission._FENCING.get()))
            return call
        claim = admission._native_claim  # FencedCase's claim double
        owner = lambda run, terminals, runtime: {"status": "unknown", "code": "fixture_unobserved"}
        with patch("pod.admission._read", side_effect=logged("orca", self.native)), \
             patch("pod.admission._native_claim", side_effect=logged("claim", claim)), \
             patch("pod.admission._owner_observation", side_effect=logged("owner", owner)):
            answer = self.fenced(query={"worktree": str(self.project), "branch": "main"})
        self.assertTrue(answer["native_fence"]["stable"])
        first_fence = next(index for index, event in enumerate(events) if event[2])
        used = [(index, event) for index, event in enumerate(events) if not event[2]]
        self.assertEqual({event[1] for _, event in used}, {"orca", "claim", "owner"})  # terminals, claim, Run owner
        self.assertTrue(all(index < first_fence for index, _ in used), events)
        self.assertEqual({event[1] for event in events[first_fence:]}, {"orca", "claim"})  # end reads only

    def test_a_released_row_outside_the_newest_page_that_changes_makes_the_fence_unstable(self):
        stable = self.fenced()
        position = next(index for index, row in enumerate(self.rows) if row["dispatchId"] == "r110")
        self.assertGreaterEqual(position, 100)  # outside the newest page, released and not abandoned at the start
        cases = {"becomes abandoned with a pending effect": (dict(self.rows[position], workerState="abandoned"),
                     {"t2": [LIVE_CLAIM], "r110": [{"process": "exited", "effects": [{"kind": "push", "id": "main",
                                                                                      "state": "pending"}]}]}),
                 "other identity change": (dict(self.rows[position], dispatchStatus="failed"), None)}
        for label, (row, claims) in cases.items():
            with self.subTest(label):
                changed = [*self.rows[:position], row, *self.rows[position + 1:]]
                answer = self.fenced(fleet_end=fleet_model(changed), claims=claims)
                fence = answer["native_fence"]
                self.assertNotEqual(fence["fleet_start"]["released_sha256"], fence["fleet_end"]["released_sha256"])
                self.assert_unstable(answer, stable)
        self.assertTrue(self.fenced()["native_fence"]["stable"])  # unchanged stays stable

    def test_a_released_circuit_broken_correction_outside_the_newest_page_makes_the_fence_unstable(self):
        position = next(index for index, row in enumerate(self.rows) if row["dispatchId"] == "r110")
        self.assertGreaterEqual(position, 100)
        self.rows[position] = dict(self.rows[position], workerState="failed", dispatchStatus="circuit_broken")
        stable = self.fenced()
        self.assertTrue(stable["native_fence"]["stable"])
        corrected = [*self.rows[:position], dict(self.rows[position], workerState="succeeded", dispatchStatus="completed"),
                     *self.rows[position + 1:]]
        self.assert_unstable(self.fenced(fleet_end=fleet_model(corrected)), stable)
        # Why released rows stay compared (pod#213): Orca's late-report correction can turn failed/circuit_broken into
        # succeeded/completed, and Pod settles only the second pair, in rule S as in candidate proof.
        self.assertIn(("succeeded", "completed"), admission._SETTLED_WORKERS)
        self.assertNotIn(("failed", "circuit_broken"), admission._SETTLED_WORKERS)

    def test_an_unproved_end_or_a_bound_caller_is_incomplete(self):
        stable = self.fenced()
        page = [*LIST, "--limit", "100"]
        bound = {"runtime": "runtime", "result": {"scope": {"source": "bound", "run": "run"}}}
        cases = {"bound end": (FleetDouble({}, overrides={tuple(page): bound}), "native_inventory_scope"),
                 "failed end": (FleetDouble({}, overrides={tuple(page): PodError("orca_read_failed", "no")}), "orca_read_failed"),
                 "failed walk page": (fleet_model(self.rows, overrides={tuple([*page, "--cursor", "c1"]):
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


class WalkReReadTests(FencedCase):
    """R207: the end fence reads discovery's own walk again, page by page in parallel, and compares everything (pod#267)."""

    def test_a997_every_walk_page_is_read_again_in_parallel_from_its_own_cursor(self):
        first, second = [*LIST, "--limit", "100"], [*LIST, "--limit", "100", "--cursor", "c1"]
        both = threading.Event()
        model = fleet_model(self.rows)
        def page(argv):
            if argv == second:
                both.set()
            elif argv == first and admission._FENCING.get():
                self.assertTrue(both.wait(2.0), "the end read its pages one after another")
            return model(argv)
        class Parallel(FleetDouble):
            def __call__(inner, argv):
                with inner.lock:
                    inner.calls.append(list(argv))
                return page(argv)
        answer = self.fenced(fleet_end=Parallel({}))
        fence = answer["native_fence"]
        self.assertTrue(fence["stable"], fence)
        self.assertIsNone(fence["reason"])
        self.assertEqual((fence["fleet_end"]["pages"], fence["fleet_end"]["released"]), (2, 120))
        self.assertEqual(sorted(self.native.fleet.calls), sorted([first, second]))  # the walk's two pages, nothing else

    def test_a998_a_change_only_the_page_lists_show_makes_the_fence_unstable(self):
        stable = self.fenced()
        swapped = [*self.rows[:101], self.rows[102], self.rows[101], *self.rows[103:]]  # same rows, same identities
        more = fleet_model(self.rows, overrides={tuple([*LIST, "--limit", "100", "--cursor", "c1"]): {
            **fleet_model(self.rows)([*LIST, "--limit", "100", "--cursor", "c1"]), "result": {
                **fleet_model(self.rows)([*LIST, "--limit", "100", "--cursor", "c1"])["result"],
                "page": {"limit": 100, "total": 123, "hasMore": True, "nextCursor": "c2"}}}})
        for label, fleet in {"two old rows change places": fleet_model(swapped),
                             "the last page reads more": more}.items():
            with self.subTest(label):
                answer = self.fenced(fleet_end=fleet)
                fence = answer["native_fence"]
                self.assertEqual({key: fence["fleet_end"][key] for key in ("unreleased_sha256", "released_sha256", "newest_sha256")},
                                 {key: fence["fleet_start"][key] for key in ("unreleased_sha256", "released_sha256", "newest_sha256")})
                self.assertNotEqual(fence["fleet_end"]["pages_sha256"], fence["fleet_start"]["pages_sha256"])
                self.assertEqual(fence["reason"], "fleet_changed")
                self.assert_unstable(answer, stable)

    def test_a999_a_spent_budget_is_the_reason_whatever_else_failed(self):
        first, second = [*LIST, "--limit", "100"], [*LIST, "--limit", "100", "--cursor", "c1"]
        spent, failed = PodError(DEADLINE_SPENT, "spent"), PodError("orca_read_failed", "no")
        for label, overrides in {"budget on the first page": {tuple(first): spent, tuple(second): failed},
                                 "budget on the last page": {tuple(first): failed, tuple(second): spent}}.items():
            with self.subTest(label):
                fence = self.fenced(fleet_end=fleet_model(self.rows, overrides=overrides))["native_fence"]
                self.assertEqual((fence["fleet_end"]["code"], fence["reason"]), (DEADLINE_SPENT, "discovery_budget"))
        fence = self.fenced(fleet_end=fleet_model(self.rows, overrides={tuple(second): failed}))["native_fence"]
        self.assertEqual((fence["fleet_end"]["code"], fence["reason"]), ("orca_read_failed", "incomplete"))
        start = {"page": fleet_model(self.rows)(first), "rows": self.rows, "pages": walk_pages(self.rows)}
        for claims in ({"a": failed, "b": spent}, {"a": spent, "b": failed}):
            self.assertEqual(admission._fleet_side(start, claims)["code"], DEADLINE_SPENT)
        invalid = {**start, "page": {**start["page"], "result": {**start["page"]["result"], "scope": {"source": "bound"}}}}
        for label, observed in {"a failed walk page": failed, "an invalid walk page": invalid}.items():
            with self.subTest(label):  # beside a spent claim read, in the side and through the whole batch
                self.assertEqual(admission._fleet_side(observed, {"a": spent})["code"], DEADLINE_SPENT)
        def claim(source, runtime):
            if admission._FENCING.get():
                raise spent
            return LIVE_CLAIM
        with patch("pod.admission._native_claim", side_effect=claim):
            fence = self.fenced(fleet_end=fleet_model(self.rows, overrides={tuple(first): failed}))["native_fence"]
        self.assertEqual((fence["fleet_end"]["code"], fence["reason"]), (DEADLINE_SPENT, "discovery_budget"))
        cases = {"a changed row": (fleet_model([dict(self.rows[0], dispatchStatus="failed"), *self.rows[1:]]), None,
                                   "fleet_changed"),
                 "a runtime change": (fleet_model(self.rows, runtime="other-runtime"), None, "runtime_changed"),
                 "a new terminal": (None, terminal_page([terminal("a", self.root / "other"), terminal("b", self.root / "other")]),
                                    "terminals_changed")}
        for label, (fleet, end, reason) in cases.items():
            with self.subTest(label):
                fence = self.fenced(fleet_end=fleet, end=end)["native_fence"]
                self.assertFalse(fence["stable"])
                self.assertEqual(fence["reason"], reason)

    def test_a1000_a_malformed_run_identity_answers_as_the_single_query_and_never_crashes(self):
        for label, value in {"list": ["malformed"], "object": {"id": "malformed"}}.items():
            with self.subTest(label):
                self.rows[110] = dict(self.rows[110], runId=value,
                                      projection={**self.rows[110]["projection"], "runId": value})
                answer = self.fenced()
                self.assertTrue(answer["native_fence"]["stable"], answer["native_fence"])
                self.claim_values = {"t2": [LIVE_CLAIM]}
                self.assertEqual(without_census(answer["responses"][0]["result"]),
                                 without_census(self.query(branch="main")))


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
    """A structurally valid worker row (projection passes `_worker_fact`)."""
    row = {"dispatchId": dispatch, "runId": "run", "taskId": "task", "terminalState": state, "workerState": "ready",
           "projection": {"id": "w-" + dispatch, "dispatchId": dispatch, "runId": "run", "taskId": "task"}}
    row.update(fields)
    return row


def walk_pages(rows):
    """The pages of discovery's newest-first walk over `rows`, as `_fleet_sequential` records them."""
    chunks = [rows[index:index + 100] for index in range(0, max(len(rows), 1), 100)]
    return [{"cursor": f"c{index}" if index else None, "ids": [row["dispatchId"] for row in chunk],
             "more": index < len(chunks) - 1} for index, chunk in enumerate(chunks)]


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


class FleetReadTests(base.AdmissionQueryCase):
    """Discovery's sequential walk, and the end fence's parallel reads of it."""

    def read(self, double, fn=None):
        discovery = admission._Discovery()
        token = admission._SHARED.set(discovery)
        with read_deadline(Budget(5.0)), patch("pod.admission._read", side_effect=double):
            try:
                value = (fn or self._real_fleet)(*([discovery] if fn else []))
            except (PodError, OSError) as exc:
                value = exc
            finally:
                admission._SHARED.reset(token)
                discovery.close()  # Waits for every read the walk started, inside the double.
        return value, discovery

    def fleet(self):
        return {"active": [worker("a1", "active")], "retained": [worker(f"t{i}", "retained") for i in range(130)],
                "released": [worker(f"r{i}", "released") for i in range(250)]}

    def test_discovery_reads_the_partitions_in_a_bracket_and_starts_the_fence_from_them(self):
        """A1019: the opening newest page, the six partitions in parallel, then the closing newest page; the union is
        the fleet, in partition order, and every page read is the fence's start."""
        rows = self.fleet()
        order = [*rows["released"][:50], *rows["retained"], *rows["active"], *rows["released"][50:]]
        double = FleetDouble(rows, sequential=order)
        fleet, discovery = self.read(double)
        self.assertEqual(discovery.fleet_read, "partitioned")
        by_state = [row["dispatchId"] for state in admission.TERMINAL_STATES for row in rows.get(state, [])]
        self.assertEqual([row["dispatchId"] for row in fleet["workers"]], by_state)
        unfiltered = [call for call in double.calls if "--terminal-state" not in call]
        self.assertEqual(unfiltered, [[*LIST, "--limit", "100"]] * 2)  # the bracket: opening and closing pages
        partitions = sorted(call[call.index("--terminal-state") + 1:] for call in double.calls if "--terminal-state" in call)
        self.assertEqual(partitions, sorted([["active"], ["reclaimable"], ["release_pending"], ["release_unknown"],
                                             ["retained"], ["retained", "--cursor", "c1"], ["released"],
                                             ["released", "--cursor", "c1"], ["released", "--cursor", "c2"]]))
        self.assertEqual(discovery.fleet_start["page"], double([*LIST, "--limit", "100"]))  # the opening page
        pages = discovery.fleet_start["pages"]
        self.assertEqual(pages[0], {"cursor": None, "state": None, "bracket": True, "ids": [row["dispatchId"] for row in order[:100]],
                                    "more": True})
        self.assertEqual([(page["state"], page["cursor"], len(page["ids"]), page["more"]) for page in pages[1:]],
                         [("active", None, 1, False), ("reclaimable", None, 0, False), ("retained", None, 100, True),
                          ("retained", "c1", 30, False), ("release_pending", None, 0, False),
                          ("release_unknown", None, 0, False), ("released", None, 100, True), ("released", "c1", 100, True),
                          ("released", "c2", 50, False)])
        side = admission._fleet_side(discovery.fleet_start, {})
        self.assertEqual((side["total"], side["counts"], side["unreleased"], side["released"], side["newest"]),
                         (381, {"active": 1, "retained": 130, "released": 250}, 131, 250, 100))

    def test_an_unproved_partition_read_falls_back_to_the_sequential_walk(self):
        """A1019: anything the bracket cannot prove whole reads the newest-first walk instead, which lists the fleet."""
        rows = self.fleet()
        order = [*rows["retained"], *rows["active"], *rows["released"]]
        moved = dict(rows["retained"][5], terminalState="released")
        page = FleetDouble(rows).page(rows["retained"], 1, 130)
        retained2 = tuple([*LIST, "--limit", "100", "--terminal-state", "retained", "--cursor", "c1"])

        class Closing(FleetDouble):
            """The closing newest page shows one more Dispatch than the opening page did."""
            def __call__(inner, argv):
                answer = FleetDouble.__call__(inner, argv)
                if "--terminal-state" not in argv and "--cursor" not in argv:
                    with inner.lock:
                        inner.unfiltered = getattr(inner, "unfiltered", 0) + 1
                        if inner.unfiltered == 2:
                            answer = {**answer, "result": {**answer["result"], "page": {**answer["result"]["page"],
                                                                                         "total": 382}}}
                return answer
        cases = {
            "a Dispatch in two partitions": FleetDouble({**rows, "released": [*rows["released"], moved]},
                                                        counts={"active": 1, "retained": 130, "released": 251},
                                                        sequential=order),
            "a count differs from its partition": FleetDouble(rows, counts={"active": 1, "retained": 129, "released": 251},
                                                              sequential=order),
            "a total differs from the union": FleetDouble(rows, total=382, sequential=order),
            "a row outside its partition": FleetDouble(rows, sequential=order, overrides={retained2: {
                **page, "result": {**page["result"], "workers": [moved, *page["result"]["workers"][1:]]}}}),
            "a partition total changes between pages": FleetDouble(rows, sequential=order, overrides={retained2: {
                **page, "result": {**page["result"], "page": {**page["result"]["page"], "total": 131}}}}),
            "a malformed row": FleetDouble({**rows, "active": [worker("a1", "active", projection=None)]},
                                           sequential=[*rows["retained"], worker("a1", "active", projection=None),
                                                       *rows["released"]]),
            "a newest row differs from its partition's": FleetDouble(rows, sequential=[
                dict(order[0], dispatchStatus="running"), *order[1:]]),
            "the closing page differs": Closing(rows, sequential=order),
        }
        for label, double in cases.items():
            with self.subTest(label):
                fleet, discovery = self.read(double)
                self.assertEqual(discovery.fleet_read, "sequential")
                walk = [call for call in double.calls if "--cursor" in call and "--terminal-state" not in call]
                self.assertTrue(walk, "the sequential walk is read")
                self.assertEqual([row["dispatchId"] for row in fleet["workers"]],
                                 [row["dispatchId"] for row in double.sequential])
                self.assertIsNone(discovery.fleet_start["pages"][0].get("state"))
                self.assertNotIn("bracket", discovery.fleet_start["pages"][0])

    def test_a_failed_partition_read_raises_as_the_walk_would(self):
        rows = self.fleet()
        failed = tuple([*LIST, "--limit", "100", "--terminal-state", "released", "--cursor", "c1"])
        for label, fault in {"read failed": PodError("orca_read_failed", "no"), "unreadable": OSError("failed")}.items():
            with self.subTest(label):
                value, discovery = self.read(FleetDouble(rows, overrides={failed: fault}))
                self.assertIs(value, fault)
                self.assertIsNone(discovery.fleet_start)

    def test_a_bound_first_page_answers_none_with_one_read(self):
        bound = {"runtime": "runtime", "result": {"scope": {"source": "bound", "run": "run"}}}
        double = FleetDouble(self.fleet(), overrides={tuple([*LIST, "--limit", "100"]): bound})
        fleet, discovery = self.read(double)
        self.assertIsNone(fleet)
        self.assertEqual(discovery.fleet_read, "bound")
        self.assertEqual(len(double.calls), 1)
        self.assertEqual(admission._fleet_side(discovery.fleet_start, {})["code"], "native_inventory_scope")

    def test_a_demanded_abandoned_row_starts_its_claim_read_while_discovery_lists_it(self):
        """A1018: once every record pass is done, a listed abandoned row starts its claim read when a query can use
        it; an abandoned row no query can use starts none."""
        here, elsewhere = str(self.project.resolve()), str(self.root / "elsewhere")
        def abandoned(dispatch, path):
            return worker(dispatch, "retained", workerState="abandoned", resource={"worktreeId": "wt::" + path},
                          projection={"id": "w" + dispatch, "dispatchId": dispatch, "runId": "run", "taskId": "task",
                                      "workspace": {"id": "wt::" + path}})
        rows = {"retained": [abandoned("a1", here), abandoned("a2", elsewhere)]}
        def show(dispatch):
            return {"runtime": "runtime", "result": {"dispatch": {"id": dispatch, "runId": "run", "taskId": "task"},
                    "worker": {}, "projection": {"id": "w" + dispatch}}}
        double = FleetDouble(rows, overrides={
            tuple(["orchestration", "worker-show", "--dispatch", key, "--json", "--full"]): show(key) for key in ("a1", "a2")})
        slot = admission._HostExitSlot(self.project.resolve(), {"worktree": here, "branch": None, "target": None,
                                                                 "effects": []}, set(), set(), set(), set(),
                                       scope={"repo_key": "key"}, owed={}, owners=None, unreadable=None)
        def ready(discovery):
            discovery.host_exit_slots.append(slot)
            discovery.memo(("places", str(self.project.resolve())), lambda: {}, stage="first")
            discovery.judge_claims()  # every record pass is done before the fleet is listed
            return self._real_fleet()
        with patch("pod.abandoned.is_abandoned", return_value=True), \
             patch("pod.abandoned.current_claim", return_value={"process": "exited", "effects": []}):
            fleet, discovery = self.read(double, ready)
        self.assertEqual(discovery.fleet_read, "partitioned")
        shows = [call[3] for call in double.calls if call[:2] == ["orchestration", "worker-show"]]
        self.assertEqual(shows, ["a1"])
        self.assertEqual(discovery.done(("claim", "a1", "runtime")), {"process": "exited", "effects": []})

    def test_a_listed_workspace_starts_its_identity_read_during_the_walk(self):
        """A863: a current row's workspace identity read starts from its inventory row, in the Git pool; a released
        row that is not abandoned starts none."""
        here = str(self.project.resolve())
        rows = {"active": [worker("a1", "active", resource={"worktreeId": "wt::" + here})],
                "released": [worker("r1", "released", resource={"worktreeId": "wt::" + str(self.root / "gone")})]}
        fleet, discovery = self.read(FleetDouble(rows))
        self.assertEqual(len(fleet["workers"]), 2)
        self.assertEqual(discovery._placed, {here})
        identity = discovery._memo[("first", "context", here)]
        self.assertTrue(identity.done())
        self.assertEqual(identity.result()["worktree"], here)

    def test_the_end_fence_reads_the_walk_again_from_its_own_cursors_and_refuses_any_race(self):
        rows = self.fleet()
        _, start = self.read(FleetDouble(rows), admission._fleet_sequential)
        pages = start.fleet_start["pages"]
        self.assertEqual([(page["cursor"], len(page["ids"]), page["more"]) for page in pages],
                         [(None, 100, True), ("c1", 100, True), ("c2", 100, True), ("c3", 81, False)])
        double = FleetDouble(rows)
        observed, _ = self.read(double, lambda discovery: admission._fleet_end(discovery, pages))
        self.assertEqual(len(observed["rows"]), 381)
        self.assertEqual(observed["pages"], [{"ids": page["ids"], "more": page["more"], "state": None, "bracket": False}
                                             for page in pages])
        walk = [*LIST, "--limit", "100"]
        self.assertEqual(sorted(double.calls), sorted([walk, *([*walk, "--cursor", f"c{index}"] for index in (1, 2, 3))]))
        everything = [row for state in rows.values() for row in state]
        page = FleetDouble(rows).page(everything, 2, 381)
        bound = {"runtime": "runtime", "result": {"scope": {"source": "bound", "run": "run"}}}
        third = tuple([*walk, "--cursor", "c2"])
        cases = {
            "bound": FleetDouble(rows, overrides={third: bound}),
            "one Run's scope": FleetDouble(rows, overrides={third: {**page, "result": {**page["result"],
                                                                    "scope": {"source": "flag", "run": "run"}}}}),
            "runtime changed": FleetDouble(rows, overrides={third: {**page, "runtime": "other"}}),
            "truncated": FleetDouble(rows, overrides={third: {**page, "result": {**page["result"], "truncated": True}}}),
            "duplicate within a page": FleetDouble(rows, overrides={third: {**page, "result": {
                **page["result"], "workers": [page["result"]["workers"][0]] * 2}}}),
            "read failed": FleetDouble(rows, overrides={third: PodError("orca_read_failed", "no")}),
            "read unreadable": FleetDouble(rows, overrides={third: OSError("failed")}),
        }
        for label, double in cases.items():
            with self.subTest(label):
                observed, _ = self.read(double, lambda discovery: admission._fleet_end(discovery, pages))
                self.assertIsInstance(observed, (PodError, OSError))
                self.assertFalse(admission._fleet_side(observed, {})["complete"])
        observed, _ = self.read(FleetDouble(rows), lambda discovery: admission._fleet_end(discovery, None))
        self.assertEqual(observed.code, "native_fence_unavailable")

    def test_the_end_fence_reads_each_partition_page_again_and_a_move_changes_a_page(self):
        """A1019: the end reads every page of a partitioned discovery again from its own partition and cursor; the
        bracket page is the end's newest page and adds no rows; a Dispatch that moved between partitions changes a
        page's Dispatches even with every total and count kept."""
        rows = self.fleet()
        _, start = self.read(FleetDouble(rows))
        self.assertEqual(start.fleet_read, "partitioned")
        pages = start.fleet_start["pages"]
        double = FleetDouble(rows)
        observed, _ = self.read(double, lambda discovery: admission._fleet_end(discovery, pages))
        self.assertEqual(len(observed["rows"]), 381)
        self.assertEqual(observed["pages"], [{"ids": page["ids"], "more": page["more"], "state": page["state"],
                                              "bracket": bool(page.get("bracket"))} for page in pages])
        expected = [[*LIST, "--limit", "100", *(["--terminal-state", page["state"]] if page["state"] else []),
                     *(["--cursor", page["cursor"]] if page["cursor"] else [])] for page in pages]
        self.assertEqual(sorted(double.calls), sorted(expected))
        same = admission._fleet_side(start.fleet_start, {})
        self.assertEqual(admission._fleet_side(observed, {}), same)
        # A balanced move: retained t5 settles into released while released r0 is deleted.
        settled = dict(rows["retained"][5], terminalState="released")
        moved = {"active": rows["active"], "retained": [row for row in rows["retained"] if row["dispatchId"] != "t5"],
                 "released": [settled, *rows["released"][1:]]}
        observed, _ = self.read(FleetDouble(moved, counts={"active": 1, "retained": 129, "released": 250}),
                                lambda discovery: admission._fleet_end(discovery, pages))
        side = admission._fleet_side(observed, {})
        self.assertNotEqual(side["pages_sha256"], same["pages_sha256"])
        self.assertNotEqual(side["unreleased_sha256"], same["unreleased_sha256"])

    def test_the_allowlist_accepts_exactly_the_walk_and_partition_reads(self):
        from pod.orca import _read_allowed
        self.assertTrue(_read_allowed([*LIST, "--limit", "100"]))
        self.assertTrue(_read_allowed([*LIST, "--limit", "100", "--cursor", "c"]))
        self.assertTrue(_read_allowed([*LIST, "--limit", "100", "--terminal-state", "released"]))
        self.assertTrue(_read_allowed([*LIST, "--limit", "100", "--terminal-state", "released", "--cursor", "c"]))
        self.assertFalse(_read_allowed([*LIST, "--limit", "100", "--terminal-state", "paused"]))
        self.assertFalse(_read_allowed([*LIST, "--limit", "100", "--terminal-state"]))
        self.assertFalse(_read_allowed([*LIST, "--limit", "1"]))
        self.assertFalse(_read_allowed([*LIST, "--limit", "2"]))


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
        self.assertEqual(without_census(answer["responses"][0]["result"]), without_census(single))
        self.assertFalse(answer["responses"][0]["result"]["record_census"]["complete"])
        for result in (single, answer["responses"][1]["result"]):
            codes = [row["code"] for row in result["unknowns"]] + list(result["unrelated_unknowns"]["by_code"])
            self.assertIn("native_location_unavailable", codes)
            self.assertIn("native_runtime_changed", codes)


class CorrectionRoundTests(BatchCase):
    """Review corrections: the newest page binds the union, malformed rows keep the sequential order,
    read faults stay scoped unknowns, and a late Run-owner read is never made."""

    def real_inventories(self):
        fleet = patch("pod.admission._fleet", side_effect=self._real_fleet)
        terminals = patch("pod.admission._terminals", side_effect=self._real_terminals)
        fleet.start(); self.addCleanup(fleet.stop)
        terminals.start(); self.addCleanup(terminals.stop)

    def replaced(self, before, after):
        """Discovery's walk lists `before`; the end fence's fresh reads see `after`."""
        start, end = fleet_model([before]), fleet_model([after])
        self.native.fleet = lambda argv: (end if admission._FENCING.get() else start)(argv)

    def row(self, dispatch, path, state, worker_state, verdict):
        row = base.ReviewCorrectionTests.writer(self, path=path, verdict=verdict, dispatch=dispatch)
        row.update(terminalState=state, workerState=worker_state, dispatchStatus="completed")
        row["projection"]["resource"] = {"state": state}
        return row

    def test_a_balanced_replacement_during_the_call_is_never_a_complete_stable_answer_without_it(self):
        self.intake(worktree=self.place)
        self.real_inventories()
        gone = self.row("B", self.root / "foreign", "released", "ready", "exited")
        new = self.row("C", self.project, "released", "abandoned", "exited")
        pending = {"process": "exited", "effects": [{"kind": "push", "id": "main", "state": "pending"}]}
        for label, (before, after) in {"C created after discovery": (gone, new),
                                       "C already listed": (new, new)}.items():
            with self.subTest(label), patch("pod.admission._native_claim", return_value=pending):
                self.replaced(before, after)
                answer = self.batch({"branch": "main"})
                result = answer["responses"][0]["result"]
                claimed = "C" in [claim.get("dispatch") for claim in result["claims"]]
                self.assertEqual(answer["discovery"]["fleet_read"], "partitioned")
                self.assertTrue(claimed or not answer["native_fence"]["stable"])
                self.assertEqual(claimed, label == "C already listed")

    def test_an_active_replacement_makes_the_fence_unstable(self):
        self.intake(worktree=self.place)
        self.real_inventories()
        self.replaced(self.row("B", self.root / "foreign", "active", "ready", "live"),
                      self.row("C", self.project, "active", "ready", "live"))
        answer = self.batch({"branch": "main"})
        self.assertFalse(answer["native_fence"]["stable"])
        self.assertEqual((answer["status"], answer["complete"]), ("unknown", False))

    def test_a_malformed_row_keeps_the_sequential_order_and_its_claims(self):
        self.intake(worktree=self.place)
        self.real_inventories()
        valid = base.ReviewCorrectionTests.writer(self, verdict="live", dispatch="valid")
        valid.update(terminalState="retained", workerState="ready")
        self.native.fleet = fleet_model([valid, worker("bad", "active", projection=None)])
        head = self.query(branch="main")  # main's newest-first walk: the writer comes first, then the refusal
        self.assertEqual([claim["dispatch"] for claim in head["claims"] if "dispatch" in claim], ["valid"])
        self.assertIn(["branch"], [row["dimensions"] for row in head["conflicts"]
                                   if row.get("claim", {}).get("dispatch") == "valid"])
        self.assertIn("orca_contract", [row["code"] for row in head["unknowns"]])
        answer = self.batch({"branch": "main"})
        self.assertEqual(answer["responses"][0]["result"], head)

    def test_read_faults_stay_scoped_unknowns_in_both_forms(self):
        self.intake(worktree=self.place)
        with patch("pod.admission._fleet", side_effect=OSError("failed")):
            single = self.query()
            answer = self.batch({}, {"branch": "main"})
        self.assertIn("inventory_unreadable", [row["code"] for row in single["unknowns"]])
        self.assertEqual(answer["responses"][0]["result"], single)
        self.assertEqual(answer["native_fence"]["fleet_start"]["complete"], False)
        real = history.load_admission_facts
        record = objective_root(self.project, "objective").name
        def unreadable(path, *, include_admissions=True):
            if path.parent.name == record:
                raise OSError("failed")
            return real(path, include_admissions=include_admissions)
        with patch("pod.history.load_admission_facts", side_effect=unreadable):
            single = self.query()
            answer = self.batch({}, {"branch": "main"})
        self.assertIn({"record": record, "code": "unreadable"}, single["unavailable_records"])
        self.assertEqual(answer["responses"][0]["result"], single)
        with patch("pod.admission._native_fence", side_effect=OSError("failed")), self.assertRaises(OSError):
            self.batch({})  # A fault outside every read point stays fatal; nothing partial is returned.
        self.assertEqual([thread for thread in threading.enumerate() if thread.name.startswith("pod-admission")], [])

    def test_a_late_run_owner_read_is_never_made_and_marks_the_result_incomplete(self):
        self.intake(worktree=self.place)
        real = admission._prefetch_native
        def skip_owners(discovery, *args, **kwargs):
            discovery.owner_runs.clear()  # force the projection to ask for the owner after the fence starts
            real(discovery, *args, **kwargs)
        with patch("pod.admission._prefetch_native", side_effect=skip_owners), \
             patch.object(admission._Discovery, "start_owners", lambda discovery: None), \
             patch("pod.admission._owner_observation") as owner:
            answer = self.batch({"worktree": str(self.project), "branch": "main"})
        owner.assert_not_called()
        result = answer["responses"][0]["result"]
        late = [row for row in result["unknowns"] if row["code"] == "native_read_after_fence"]
        self.assertEqual([row["fact"] for row in late], ["current_run_owner"])
        self.assertFalse(result["complete"])
        ownership = next(claim for claim in result["claims"] if claim["kind"] == "ownership")
        self.assertEqual(ownership["owner_observations"][0]["code"], "native_read_after_fence")
        self.assertFalse(ownership["owner_identity_matches"])


class RestoredRecordTests(BatchCase):
    """A record restored after its claims read and before the change recheck (A/B/A, the pod#222 review)."""

    def test_a_restored_writer_is_record_changed_in_every_query(self):
        self.intake(worktree=self.place)
        path, intermediate = self.reserved_writer()
        with self.claims_read_meets(path, intermediate):
            answer = self.batch({"worktree": str(self.project)}, {"branch": "main"})
        self.assertFalse(answer["complete"])
        for row in answer["responses"]:
            self.assertFalse(row["result"]["complete"])
            self.assertIn("record_changed", [unknown["code"] for unknown in row["result"]["unknowns"]])


class RunCoordinatorFenceTests(BatchCase):
    """R206 rule S in a batch (A825): the Run-coordinator list is read before the end fence, through the same
    gated reads the fence guards, so an ended Dispatch whose resources are gone settles as in the single query."""

    writer = base.ReviewCorrectionTests.writer
    worker = base.TerminalHistoryTests.worker
    ended = base.TerminalHistoryTests.ended
    settled = base.TerminalHistoryTests.settled

    def setUp(self):
        super().setUp()
        self.intake(worktree=self.place)
        native, self.run_reads = self.native, []
        def read(argv):
            if argv[:2] == ["orchestration", "run-list"]:
                self.run_reads.append(list(argv))
                return {"runtime": "runtime", "result": {
                    "runs": [{"id": "ended-run", "coordinator_handle": None}], "nextCursor": None}}
            return native(argv)
        reads = patch("pod.admission._read", side_effect=read)
        reads.start(); self.addCleanup(reads.stop)
        real, self.refused = admission._Discovery.ordered, []
        def ordered(discovery):
            try:
                return real(discovery)
            except PodError as exc:
                self.refused.append(exc.code)
                raise
        guard = patch.object(admission._Discovery, "ordered", ordered)
        guard.start(); self.addCleanup(guard.stop)
        inventories = {"runtime": "runtime", "complete": True}
        for name, value in (("_fleet", {**inventories, "workers": [self.ended()]}),
                            ("_terminals", {**inventories, "terminals": []})):
            fixed = patch(f"pod.admission.{name}", return_value=value)
            fixed.start(); self.addCleanup(fixed.stop)

    def test_an_ended_dispatch_with_gone_resources_settles_as_in_the_single_query(self):
        shape = {"worktree": str(self.project), "branch": "main"}
        single = self.query(**shape)
        self.settled(single)
        self.run_reads.clear()
        answer = self.batch(shape, {"branch": "main"})
        result = answer["responses"][0]["result"]
        self.settled(result)
        self.assertEqual(without_census(result), without_census(single))
        self.assertFalse(result["record_census"]["complete"])
        self.assertEqual(result["unknowns"], single["unknowns"])  # the same blocking unknowns
        self.assertEqual(len(self.run_reads), 1)  # one walk per call, shared by every query
        self.assertEqual(self.refused, [])  # no native read was refused after the fence

    def test_a_rule_s_verdict_is_judged_once_per_call(self):
        """A862: every query that judges the ended Dispatch reads the same verdict."""
        with patch("pod.admission._settled_gone", wraps=admission._settled_gone) as judged:
            answer = self.batch({"worktree": str(self.project), "branch": "main"}, {"branch": "main"},
                                {"worktree": str(self.project)})
        self.assertEqual(judged.call_count, 1)
        self.settled(answer["responses"][0]["result"])

    def test_a_normal_batch_requests_no_native_read_after_the_fence(self):
        answer = self.batch({}, {"worktree": str(self.project)}, {"effects": ["deploy:target"]})
        self.assertEqual(self.refused, [])
        self.assertIn("native_fence", answer)
        for row in answer["responses"]:
            self.assertNotIn("native_read_after_fence", [unknown["code"] for unknown in row["result"]["unknowns"]])


class DiscoveryRaceTests(BatchCase):
    """A change between discovery's walk and the end fence gives the claim or an unstable fence."""

    def walked(self, start, end):
        first, last = fleet_model(start), fleet_model(end)
        self.native.fleet = lambda argv: (last if admission._FENCING.get() else first)(argv)

    def row(self, dispatch, path, state, abandoned=False):
        row = base.ReviewCorrectionTests.writer(self, path=path, verdict="exited", dispatch=dispatch)
        row.update(terminalState=state, workerState="abandoned" if abandoned else "ready",
                   dispatchStatus="failed" if abandoned else "completed")
        row["projection"]["resource"] = {"state": state}
        return row

    def setUp(self):
        super().setUp()
        self.intake(worktree=self.place)
        fleet = patch("pod.admission._fleet", side_effect=self._real_fleet)
        terminals = patch("pod.admission._terminals", side_effect=self._real_terminals)
        fleet.start(); self.addCleanup(fleet.stop)
        terminals.start(); self.addCleanup(terminals.stop)
        self.newer = [self.row(f"N{index}", self.root / "foreign", "released") for index in range(100)]

    def outcome(self, query=None):
        answer = self.batch(query or {})
        self.assertEqual(answer["discovery"]["fleet_read"], "partitioned")
        return answer, answer["responses"][0]["result"]

    def test_an_old_abandoned_dispatch_settling_beside_a_deletion_and_a_creation(self):
        # The review's sequence: 100 newer released rows, released B and an old retained abandoned D with a
        # pending push; then D settles, B is deleted and retained C is created, keeping every total and count.
        foreign = self.root / "foreign"
        b, c = self.row("B", foreign, "released"), self.row("C", foreign, "retained")
        self.walked([*self.newer, b, self.row("D", self.project, "retained", True)],
                    [c, *self.newer, self.row("D", self.project, "released", True)])
        pending = {"process": "exited", "effects": [{"kind": "push", "id": "main", "state": "pending"}]}
        with patch("pod.admission._native_claim", return_value=pending):
            answer, result = self.outcome({"branch": "main"})
        self.assertIn("D", [claim.get("dispatch") for claim in result["claims"]])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(answer["native_fence"]["stable"])

    def test_a_creation_or_a_deletion_makes_the_fence_unstable(self):
        foreign = self.root / "foreign"
        cases = {"creation": (self.newer, [self.row("C", foreign, "retained"), *self.newer]),
                 "deletion": ([*self.newer, self.row("B", foreign, "released")], self.newer)}
        for label, (start, end) in cases.items():
            with self.subTest(label):
                self.walked(start, end)
                answer, _ = self.outcome()
                self.assertFalse(answer["native_fence"]["stable"])

    def test_a_balanced_state_swap_of_two_old_rows_makes_the_fence_unstable(self):
        foreign = self.root / "foreign"
        x, y = self.row("X", foreign, "retained"), self.row("Y", foreign, "active")
        swapped = [dict(x, terminalState="active"), dict(y, terminalState="retained")]
        for row in swapped:
            row["projection"] = {**row["projection"], "resource": {"state": row["terminalState"]}}
        self.walked([*self.newer, x, y], [*self.newer, *swapped])  # same total, counts and newest page
        answer, _ = self.outcome()
        fence = answer["native_fence"]
        self.assertEqual((fence["fleet_start"]["counts"], fence["fleet_start"]["newest_sha256"]),
                         (fence["fleet_end"]["counts"], fence["fleet_end"]["newest_sha256"]))
        self.assertFalse(fence["stable"])  # the unreleased rows differ

    def test_an_unchanged_fleet_is_stable(self):
        rows = [*self.newer, self.row("B", self.root / "foreign", "retained")]
        self.walked(rows, rows)
        answer, _ = self.outcome()
        self.assertTrue(answer["native_fence"]["stable"])
