"""R212: admission asks the host verifier, at read time and before the end fence, about settled local Dispatches Orca
cannot observe, and counts one exited only on that call's own verdict (A847-A859; the rounds and the stop after a fault,
A865-A867 and A899-A901, are in test_admission_host_exit_stop)."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import patch

from pod import admission, process_exit
from pod.errors import PodError
from pod.ledger import state_root
from pod.window import Budget
from tests import test_admission_batch as batch
from tests import test_admission_query as base
from tests.process_exit_support import VerifierMixin, exit_codes, held, stuck


def files(root: Path) -> dict:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


class Clock:
    """A call clock the test moves by hand; no test sleeps."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class HostExitCase(VerifierMixin, base.AdmissionQueryCase):
    """A single query over patched inventories, with a real verifier process. No read reaches live Orca."""

    def setUp(self):
        super().setUp()
        self.setup_verifier()
        guard = patch("pod.admission._read", side_effect=AssertionError("fixture: no live native read"))
        guard.start(); self.addCleanup(guard.stop)
        self.coordinators = {}
        runs = patch("pod.admission._run_coordinators", side_effect=lambda runtime, check=None: self.coordinators)
        runs.start(); self.addCleanup(runs.stop)
        claims = patch("pod.admission._native_claim", return_value={"process": "unverifiable", "effects": []})
        claims.start(); self.addCleanup(claims.stop)
        for name in ("wa", "wb"):
            (self.project / name).mkdir(exist_ok=True)

    def read(self, workers=(), terminals=(), **query):
        query = query or {"worktree": str(self.project), "branch": "main"}
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": list(workers)}), \
             patch("pod.admission._terminals", return_value={"runtime": "runtime", "complete": True,
                                                             "terminals": list(terminals)}):
            return admission.query(self.project, **query)

    def without_r212(self, workers=(), terminals=(), **query):
        with patch("pod.admission._host_exit_prefetch"):
            return self.read(workers, terminals, **query)

    def counted(self, result, dispatch):
        rows = {row["dispatch"]: row for row in result.get("host_exits", [])}
        return not held(result, dispatch) and rows.get(dispatch, {}).get("counted") is True


class CountTests(HostExitCase):
    def test_a_verified_absent_dispatch_settles_in_a_single_query(self):
        """A847: liveness unverifiable, terminal absent, workspace present, Run coordinator live."""
        self.coordinators = {"lead-run": "lead-terminal"}
        lead = {"handle": "lead-terminal", "worktreePath": str(self.root), "connected": True, "writable": True,
                "orphaned": False}
        row = stuck(self)
        before = held(self.without_r212([row], [lead]), "stuck")
        state, config = files(state_root(self.project)), files(self.root / "config")
        result = self.read([row], [lead])
        self.assertTrue(before)
        self.assertTrue(self.counted(result, "stuck"), result["unknowns"])
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertEqual(self.requests(), [{"schema": "pod-relay-verification/v1", "action": "process_exit",
                                            "references": ["stuck"]}])
        disclosed = result["host_exits"][0]
        self.assertEqual({key: disclosed[key] for key in ("dispatch", "run", "counted", "code", "host", "runtime", "state")},
                         {"dispatch": "stuck", "run": "lead-run", "counted": True, "code": None, "host": "local",
                          "runtime": "runtime", "state": "absent"})
        self.assertEqual((files(state_root(self.project)), files(self.root / "config")), (state, config))

    def test_only_a_dispatch_the_host_found_no_carrier_for_settles(self):
        """A848: present, partial, unknown and mismatched entries keep their holds while the absent one settles."""
        rows = [stuck(self, name) for name in ("a", "b", "c", "d", "e")]
        self.answer(entries={"b": {"state": "present"}, "c": {"state": "partial"}, "d": {"known": False},
                             "e": {"terminal_handle": "other-terminal"}})
        result = self.read(rows)
        self.assertTrue(self.counted(result, "a"))
        self.assertEqual(exit_codes(result), {"b": "host_exit_present", "c": "host_exit_partial",
                                              "d": "host_exit_host_unknown", "e": "host_exit_identity_mismatch"})
        self.assertEqual(len(self.requests()), 1)


class ConfigTests(HostExitCase):
    def test_an_unconfigured_host_starts_nothing_and_answers_as_before(self):
        """A849."""
        self.configure(None)
        rows = [stuck(self)]
        self.assertEqual(self.read(rows), self.without_r212(rows))
        self.assertEqual(self.requests(), [])

    def test_an_unreadable_config_holds_every_candidate(self):
        with patch("pod.config.effective", side_effect=PodError("invalid_config", "fixture")):
            result = self.read([stuck(self)])
        self.assertEqual(exit_codes(result), {"stuck": "host_exit_verifier_unavailable"})
        self.assertEqual(self.requests(), [])

    def test_an_invalid_or_unreadable_personal_file_holds_through_the_real_reader(self):
        """`config.load` returns an invalid snapshot rather than raising; it is never read as unconfigured."""
        from pod import config
        personal = config.personal_path(self.project)
        real = config._read_bytes
        def unreadable(path, *args, **kwargs):
            if Path(path) == personal:
                raise PodError("config_unreadable", "fixture: the personal file cannot be read")
            return real(path, *args, **kwargs)
        with self.subTest("unreadable"), patch("pod.config._read_bytes", side_effect=unreadable):
            self.assertEqual(exit_codes(self.read([stuck(self)])), {"stuck": "host_exit_verifier_unavailable"})
        personal.write_text("schema: [\n")
        with self.subTest("malformed"):
            self.assertEqual(exit_codes(self.read([stuck(self)])), {"stuck": "host_exit_verifier_unavailable"})
        self.assertEqual(self.requests(), [])

    def test_a_missing_personal_file_is_unconfigured_and_answers_as_before(self):
        from pod import config
        config.personal_path(self.project).unlink()
        rows = [stuck(self)]
        self.assertEqual(self.read(rows), self.without_r212(rows))
        self.assertEqual(self.requests(), [])


class FaultTests(HostExitCase):
    """A850: a transport or contract fault holds its request and starts no further one, so the query's other ids hold
    with the same code without being asked; an answer under the contract holds its request and the query's other ids
    with `host_exit_query_uncovered` (A865 adds the call-level stop)."""

    def faulted(self, mode, **control):
        self.answer(faults={"a": mode}, **control)
        with patch.object(process_exit, "K", 1), patch.object(process_exit, "TIMEOUT_S", 0.3):
            return self.read([stuck(self, "a"), stuck(self, "b")])

    def test_transport_faults_hold_their_request(self):
        for mode, code in {"fail": "host_exit_failed", "timeout": "host_exit_timeout",
                           "oversized": "host_exit_malformed", "invalid_json": "host_exit_malformed",
                           "duplicate": "host_exit_malformed"}.items():
            with self.subTest(mode):
                self.log.unlink(missing_ok=True)
                result = self.faulted(mode)
                self.assertEqual(exit_codes(result), {"a": code, "b": code})
                self.assertEqual(self.asked(), ["a"])
                self.assertNotIn("sentinel", json.dumps(result))

    def test_a_calendar_boundary_scan_time_holds_only_its_request(self):
        for observed in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
            with self.subTest(observed):
                result = self.faulted("overflow", overflow_at=observed)
                self.assertEqual(exit_codes(result), {"a": "host_exit_malformed", "b": "host_exit_malformed"})

    def test_shape_faults_hold_their_request(self):
        for mode in ("missing", "extra"):
            with self.subTest(mode):
                self.log.unlink(missing_ok=True)
                self.assertEqual(exit_codes(self.faulted(mode)), {"a": "host_exit_malformed", "b": "host_exit_malformed"})
                self.assertEqual(self.asked(), ["a"])
        with self.subTest("unverified"):  # an answer under the contract: the next request is still asked
            self.log.unlink(missing_ok=True)
            self.assertEqual(exit_codes(self.faulted("unverified")),
                             {"a": "host_exit_unverified", "b": "host_exit_query_uncovered"})
            self.assertEqual(self.asked(), ["a", "b"])
        for mode in ("repeated", "reordered"):  # two ids in one request: every id of it holds
            with self.subTest(mode):
                self.answer(mode=mode)
                result = self.read([stuck(self, "a"), stuck(self, "b")])
                self.assertEqual(exit_codes(result), {"a": "host_exit_malformed", "b": "host_exit_malformed"})
        self.answer(override={"schema": "pod-relay-verification/v2"})
        self.assertEqual(set(exit_codes(self.read([stuck(self, "a")])).values()), {"host_exit_mismatch"})


class BindingTests(HostExitCase):
    def test_a_terminal_mismatch_holds_only_its_own_id(self):
        """A851."""
        self.answer(entries={"a": {"terminal_handle": "a-terminal-elsewhere"}})
        result = self.read([stuck(self, "a"), stuck(self, "b")])
        self.assertEqual(exit_codes(result), {"a": "host_exit_identity_mismatch"})
        self.assertTrue(self.counted(result, "b"))

    def test_another_host_runtime_or_scan_time_holds_the_whole_request(self):
        stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        for control, code in (({"host": "builder-2"}, "host_exit_foreign_host"),
                              ({"runtime": "older-runtime"}, "host_exit_foreign_runtime"),
                              ({"observed_at": stale}, "host_exit_stale")):
            with self.subTest(code):
                self.answer(**control)
                self.assertEqual(exit_codes(self.read([stuck(self, "a"), stuck(self, "b")])), {"a": code, "b": code})


class EligibilityTests(HostExitCase):
    """A852: rows R212 never asks about keep exactly their earlier result."""

    def assert_never_asked(self, cases, terminals=()):
        for label, row in cases.items():
            with self.subTest(label):
                self.log.unlink(missing_ok=True)
                self.assertEqual(self.read([row], terminals), self.without_r212([row], terminals))
                self.assertEqual(self.asked(), [])

    def test_unsettled_live_exited_and_abandoned_rows_are_never_asked(self):
        self.assert_never_asked({"running": stuck(self, state=("running", "running")),
                                 "live": stuck(self, verdict="live"), "exited": stuck(self, verdict="exited"),
                                 "abandoned": stuck(self, state=("abandoned", "failed"))})

    def test_a_present_terminal_rule_s_and_an_out_of_scope_row_are_never_asked(self):
        present = {"handle": "stuck-terminal", "worktreePath": str(self.project), "connected": False,
                   "writable": False, "orphaned": True}
        self.assert_never_asked({"terminal present": stuck(self)}, terminals=[present])
        self.coordinators = {"gone-run": None}
        self.assert_never_asked({"rule S settles": stuck(self, path=self.project / "gone", run="gone-run"),
                                 "outside the repository": stuck(self, path=self.root / "elsewhere", run="foreign")})

    def test_remote_missing_and_malformed_hosts_are_never_asked(self):
        """F4: a remote row whose runtime and terminal strings otherwise match is never a local absence."""
        self.assert_never_asked({"remote": stuck(self, host=("ssh", "builder-2")),
                                 "remote named local": stuck(self, host=("remote", "local")),
                                 "missing host": stuck(self, host=None),
                                 "malformed host id": stuck(self, host=("local", "local host"))})

    def test_identities_outside_the_alphabet_are_never_asked(self):
        self.assert_never_asked({"dispatch": stuck(self, "stuck one"),
                                 "handle": stuck(self, handle="term/" + "x" * 70)})


class RelevanceTests(HostExitCase):
    """A859: the candidate rule is the conservative superset S(r, q) of `_relevant`, including the empty request."""

    def test_the_unfiltered_query_asks_for_a_located_candidate(self):
        result = self.read([stuck(self)], repository=str(self.project))
        self.assertTrue(self.counted(result, "stuck"))

    def test_each_request_shape_asks_its_superset(self):
        rows = [stuck(self, "in-a", path=self.project / "wa"), stuck(self, "in-b", path=self.project / "wb")]
        shapes = {"worktree only": ({"worktree": str(self.project / "wa")}, ["in-a"]),
                  "branch only": ({"branch": "main"}, ["in-a", "in-b"]),
                  "another branch": ({"branch": "other"}, []),
                  "target only": ({"target": "main"}, []),
                  "effects only": ({"effects": ["deploy:target"]}, [])}
        for label, (query, asked) in shapes.items():
            with self.subTest(label):
                self.log.unlink(missing_ok=True)
                self.read(rows, **query)
                self.assertEqual(sorted(self.asked()), asked)

    def test_the_superset_contains_relevant_and_admits_its_documented_extras(self):
        """Every row `_relevant` keeps is a candidate; a known non-overlapping path with an unknown branch and a gap
        Dispatch outside the selection are the permitted extras."""
        project = str(self.project)
        places = {project: "main"}
        loose = str(self.root / "loose")  # outside every registered worktree: its branch is unknown
        rows = {"in-a": (str(self.project / "wa"), "main"), "in-b": (str(self.project / "wb"), "main"),
                "unlocated": (None, None), "unknown-branch": (loose, None)}
        requests = [{}, {"worktree": str(self.project / "wa")}, {"branch": "main"}, {"branch": "other"},
                    {"target": "main"}, {"effects": ["deploy:target"]},
                    {"worktree": str(self.project / "wa"), "branch": "other"}]
        with patch("pod.admission._SHARED") as shared:
            shared.get.return_value = None
            for request in requests:
                full = {"worktree": None, "branch": None, "target": None, "effects": [], **request}
                slot = admission._HostExitSlot(self.project, full, {"lead-run"}, {"gap"}, set())
                for dispatch, (path, branch) in rows.items():
                    shown = {"dispatch": dispatch, "run": "lead-run", "worktree": path}
                    facts = {"repo_key": "key", "worktree": path, "branch": branch,
                             "locations_complete": bool(path) and branch is not None}
                    relevant = admission._relevant(facts, full, "key", admission._overlap, "process_liveness")
                    with self.subTest(request=request, row=dispatch):
                        if relevant:
                            self.assertTrue(admission._host_exit_relevant(slot, shown, places))
                self.assertTrue(admission._host_exit_relevant(slot, {"dispatch": "gap", "run": "x",
                                                                     "worktree": str(self.project / "wb")}, places))
                if request.get("worktree") and not request.get("branch"):
                    shown = {"dispatch": "unknown-branch", "run": "lead-run", "worktree": loose}
                    self.assertTrue(admission._host_exit_relevant(slot, shown, places))  # an extra, never unsafe


class DeadlineTests(HostExitCase):
    """A854 with a call clock: discovery returns at 3.9 s and rule S's Run list at 4.2 s, so the reserve is
    max(4.0, 1.25 * 3.9) = 4.875 s and a request needs 4.2 + 3 + 4.875 = 12.075 s of deadline."""

    def call(self, seconds, *, discovery=3.9):
        clock = Clock()
        budget = Budget(seconds)
        budget._clock, budget.deadline = clock, clock.now + seconds
        def fleet():
            clock.now += discovery
            return {"runtime": "runtime", "complete": True, "workers": [stuck(self)]}
        def runs(runtime, check=None):
            clock.now += 0.3
            return {}
        with patch("pod.admission._fleet", side_effect=fleet), \
             patch("pod.admission._terminals", return_value={"runtime": "runtime", "complete": True, "terminals": []}), \
             patch("pod.admission._run_coordinators", side_effect=runs):
            return admission._call(self.project, [{"worktree": str(self.project), "branch": "main"}], budget)["results"][0]

    def test_below_the_threshold_nothing_is_asked_and_the_dispatch_holds(self):
        result = self.call(12.0)
        self.assertEqual(exit_codes(result), {"stuck": "host_exit_deadline"})
        self.assertEqual(self.requests(), [])

    def test_above_the_threshold_the_host_is_asked(self):
        for seconds in (12.1, 18.0):
            with self.subTest(seconds):
                self.log.unlink(missing_ok=True)
                self.assertTrue(self.counted(self.call(seconds), "stuck"))
                self.assertEqual(self.asked(), ["stuck"])

    def test_the_reserve_follows_a_slower_discovery_read(self):
        # 6.0 s of discovery: the reserve is 7.5 s and a request needs 6.3 + 3 + 7.5 = 16.8 s.
        self.assertEqual(exit_codes(self.call(16.7, discovery=6.0)), {"stuck": "host_exit_deadline"})
        self.assertTrue(self.counted(self.call(16.9, discovery=6.0), "stuck"))


class Rows:
    """Stuck rows added to a FencedCase fleet of 123 unrelated rows."""

    def stuck_rows(self, *names, path=None, terminal_state="released"):
        rows = [stuck(self, name, path=path, terminal_state=terminal_state) for name in names]
        self.rows.extend(rows)  # appended last: outside the newest page of 100
        return rows

    def calls(self, *queries, terminals=()):
        self.native.pages = [batch.terminal_page(list(terminals))]
        self.native.fleet = batch.fleet_model(self.rows)
        self.claim_values = {"t2": [batch.LIVE_CLAIM]}
        singles = [admission.query(self.project, **query) for query in queries]
        return singles, self.batch(*queries)


class BatchEquivalenceTests(VerifierMixin, Rows, batch.FencedCase):
    """A848: candidates, counting and disclosure are per query; the shared requests only supply verdicts."""

    def setUp(self):
        super().setUp()
        self.setup_verifier()
        for name in ("wa", "wb"):
            (self.project / name).mkdir(exist_ok=True)
        self.a, self.b = {"worktree": str(self.project / "wa")}, {"worktree": str(self.project / "wb")}

    def assert_equal_to_singles(self, singles, answer):
        for single, row in zip(singles, answer["responses"]):
            self.assertEqual(row["result"], single)

    def test_thirty_two_and_one_candidates_in_disjoint_worktrees_equal_their_single_queries(self):
        self.stuck_rows(*(f"a{index:02d}" for index in range(32)), path=self.project / "wa")
        self.stuck_rows("b00", path=self.project / "wb")
        singles, answer = self.calls(self.a, self.b)
        self.assert_equal_to_singles(singles, answer)
        self.assertEqual(sum(row["counted"] for row in answer["responses"][0]["result"]["host_exits"]), 32)
        self.assertEqual(answer["discovery"]["host_exit"], {"requests": 5, "asked": 33, "counted": 33})  # 8 ids each
        self.assertTrue(answer["native_fence"]["stable"])
        self.assertEqual(answer["native_fence"]["host_exit_dispatches"], sorted(row["dispatchId"] for row in self.rows[123:]))

    def test_reversed_and_duplicated_queries_answer_the_same(self):
        self.stuck_rows("a00", path=self.project / "wa")
        self.stuck_rows("b00", path=self.project / "wb")
        singles, answer = self.calls(self.b, self.a, self.a)
        self.assert_equal_to_singles(singles, answer)
        self.assertEqual(len(self.requests()), 1 + 3)  # one shared request in the batch, one per single query

    def test_a_calendar_boundary_scan_time_stops_the_requests_but_not_the_call(self):
        """One request's unusable scan time holds its query and, unasked, the next request's query; the call still
        answers, and a query without candidates answers exactly as its single query. This verifier faults for one id
        only, so the second query's single query counts: a fault that depends on the ids is outside the equality."""
        (self.project / "wc").mkdir(exist_ok=True)
        self.stuck_rows("a00", path=self.project / "wa")
        self.stuck_rows("b00", path=self.project / "wb")
        self.answer(faults={"a00": "overflow"}, overflow_at="9999-12-31T23:59:59-01:00")
        with patch.object(process_exit, "K", 1):
            singles, answer = self.calls(self.a, self.b, {"worktree": str(self.project / "wc")})
        results = [row["result"] for row in answer["responses"]]
        self.assertEqual(results[0], singles[0])
        self.assertEqual(results[2], singles[2])
        self.assertEqual(exit_codes(results[0]), {"a00": "host_exit_malformed"})
        self.assertEqual(exit_codes(results[1]), {"b00": "host_exit_malformed"})
        self.assertEqual([row["counted"] for row in singles[1]["host_exits"]], [True])
        self.assertNotIn("host_exits", results[2])
        self.assertEqual(answer["discovery"]["host_exit"], {"requests": 1, "asked": 1, "counted": 0,
                                                            "not_asked_after": "host_exit_malformed", "not_asked": 1})

    def test_a_verdict_relevant_to_one_query_leaves_the_other_unchanged(self):
        self.stuck_rows("a00", path=self.project / "wa")
        singles, answer = self.calls(self.a, self.b)
        self.assert_equal_to_singles(singles, answer)
        other = answer["responses"][1]["result"]
        self.assertNotIn("host_exits", other)
        self.assertIn("a00", other["unrelated_unknowns"]["references"]["dispatches"])


class BoundTests(VerifierMixin, Rows, batch.FencedCase):
    """A853 with K = 2 and R = 2: at most four ids per call."""

    def setUp(self):
        super().setUp()
        self.setup_verifier()
        for name in ("wa", "wb"):
            (self.project / name).mkdir(exist_ok=True)
        for name, value in (("K", 2), ("R", 2)):
            bound = patch.object(process_exit, name, value)
            bound.start(); self.addCleanup(bound.stop)

    def test_over_the_bound_whole_query_sets_are_chosen_by_size_then_ids_whatever_the_order(self):
        self.stuck_rows("a0", path=self.project / "wa")
        self.stuck_rows("b0", "b1", "b2", "b3", path=self.project / "wb")
        a, b = {"worktree": str(self.project / "wa")}, {"worktree": str(self.project / "wb")}
        for order in ((a, b), (b, a)):
            with self.subTest(order=[query["worktree"][-2:] for query in order]):
                self.log.unlink(missing_ok=True)
                singles, answer = self.calls(*order)
                results = {query["worktree"]: row["result"] for query, row in zip(order, answer["responses"])}
                single = {query["worktree"]: result for query, result in zip(order, singles)}
                self.assertEqual(results[a["worktree"]], single[a["worktree"]])
                self.assertEqual(set(exit_codes(results[b["worktree"]]).values()), {"host_exit_call_limit"})
                self.assertTrue(all(row["counted"] for row in single[b["worktree"]]["host_exits"]))
                self.assertLessEqual(answer["discovery"]["host_exit"]["requests"], 2)

    def test_a_single_query_over_the_bound_asks_nothing(self):
        self.stuck_rows("b0", "b1", "b2", "b3", "b4", path=self.project / "wb")
        singles, _ = self.calls({"worktree": str(self.project / "wb")})
        self.assertEqual(set(exit_codes(singles[0]).values()), {"host_exit_call_limit"})


class FenceTests(VerifierMixin, Rows, batch.FencedCase):
    """A855: the fence compares every native field a counted Dispatch's exit depends on."""

    def setUp(self):
        super().setUp()
        self.setup_verifier()
        self.stuck_rows("stuck")

    def fenced_with(self, changed=None, terminals=()):
        end = [changed if changed is not None and row["dispatchId"] == changed["dispatchId"] else row for row in self.rows]
        self.native.pages = [batch.terminal_page([]), batch.terminal_page(list(terminals))]
        self.native.fleet = batch.fleet_model(end)
        self.claim_values = {"t2": [batch.LIVE_CLAIM]}
        return self.batch({"branch": "main"})

    def test_a_counted_dispatch_unchanged_is_stable(self):
        answer = self.fenced_with()
        self.assertTrue(answer["native_fence"]["stable"])
        self.assertEqual(answer["native_fence"]["host_exit_dispatches"], ["stuck"])
        self.assertEqual(answer["native_fence"]["fleet_start"]["host_exit"], 1)

    def test_each_depended_on_field_changing_before_the_end_is_unstable(self):
        def changed(edit):
            row = deepcopy(self.rows[-1])
            edit(row)
            return row
        cases = {"lastObservedAt": lambda row: row["projection"]["evidence"].update(lastObservedAt=1793401656234),
                 "liveness observedAt": lambda row: row["projection"]["liveness"].update(observedAt=1793401656234),
                 "liveness reason": lambda row: row["projection"]["liveness"].update(reason="stale_status"),
                 "liveness verdict": lambda row: row["projection"]["liveness"].update(verdict="live"),
                 "endpoint incarnation": lambda row: row["resource"].update(endpointIncarnation="pty-stuck:incarnation-2"),
                 "execution host": lambda row: row["projection"].update(host={"kind": "ssh", "id": "builder-2"}),
                 "worker state": lambda row: row.update(workerState="failed", dispatchStatus="failed")}
        for label, edit in cases.items():
            with self.subTest(label):
                answer = self.fenced_with(changed(edit))
                self.assertFalse(answer["native_fence"]["stable"])
                self.assertFalse(answer["complete"])
        appeared = batch.terminal("stuck-terminal", self.project)
        self.assertFalse(self.fenced_with(terminals=[appeared])["native_fence"]["stable"])

    def test_an_uncounted_live_rows_watermark_moving_keeps_the_fence_stable(self):
        live = stuck(self, "heartbeat", path=self.root / "elsewhere", verdict="live")
        live["projection"]["evidence"]["lastObservedAt"] = 1
        self.rows.insert(0, live)
        moved = deepcopy(live)
        moved["projection"]["evidence"]["lastObservedAt"] = 2
        self.assertTrue(self.fenced_with(moved)["native_fence"]["stable"])


class OrderTests(VerifierMixin, Rows, batch.FencedCase):
    """A856: the scan follows the discovery reads and precedes every end read; nothing survives the call."""

    def setUp(self):
        super().setUp()
        self.setup_verifier()
        self.stuck_rows("stuck")

    def test_the_verifier_runs_after_discovery_and_before_the_end_reads(self):
        real = process_exit.ask
        def ask(command, ids, **kwargs):
            self.native.calls.append(["verifier"])
            return real(command, ids, **kwargs)
        with patch.object(process_exit, "ask", side_effect=ask):
            self.calls({"branch": "main"})
        calls = [call[:2] for call in self.native.calls]
        single, rest = calls.index(["verifier"]), calls[calls.index(["verifier"]) + 1:]
        fenced = rest.index(["verifier"]) + single + 1
        self.assertIn(["terminal", "list"], calls[single + 1:fenced])  # the batch's discovery read
        self.assertIn(["orchestration", "worker-list"], calls[fenced + 1:])  # its end reads come after the scan
        self.assertNotIn(["orchestration", "worker-list"], calls[single + 1:fenced])

    def test_a_later_call_asks_again_and_nothing_persists(self):
        state = files(state_root(self.project))
        first = self.calls({"branch": "main"})[1]["responses"][0]["result"]
        self.answer(entries={"stuck": {"state": "present"}})  # it returned under unchanged identifiers
        second = self.calls({"branch": "main"})[1]["responses"][0]["result"]
        self.assertTrue(first["host_exits"][0]["counted"])
        self.assertEqual(exit_codes(second), {"stuck": "host_exit_present"})
        self.assertEqual(files(state_root(self.project)), state)

    def test_a_host_clock_skewed_within_the_window_decides_nothing(self):
        skewed = (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat()
        self.answer(observed_at=skewed)
        self.assertTrue(self.calls({"branch": "main"})[1]["responses"][0]["result"]["host_exits"][0]["counted"])


class RuleTTests(VerifierMixin, base.AdmissionQueryCase):
    """A857: a closed record whose only bound admission is the counted Dispatch moves its gap in that call."""

    GONE = base.TerminalHistoryTests.GONE
    terminal = base.TerminalHistoryTests.terminal
    bound = base.TerminalHistoryTests.bound
    facts_for = base.TerminalHistoryTests.facts_for
    runs = base.TerminalHistoryTests.runs
    outside = base.TerminalHistoryTests.outside
    raw = base.AdmissionQueryCase.raw
    save = base.AdmissionQueryCase.save
    LINK, ENTRY = base.TerminalHistoryTests.LINK, base.TerminalHistoryTests.ENTRY

    def setUp(self):
        super().setUp()
        self.setup_verifier()
        self.intake(worktree=self.place)
        self.old = base.objective_root(self.project, "old")
        self.real_facts, self.shape = base.history.load_admission_facts, None
        facts = patch("pod.history.load_admission_facts", create=True, side_effect=self.facts_for)
        facts.start(); self.addCleanup(facts.stop)
        self.run_pages = {}
        def gated(argv):
            if argv[:2] == ["orchestration", "run-list"]:
                return deepcopy(self.run_pages[None])
            raise AssertionError(f"fixture: no live native read {argv}")
        pages = patch("pod.admission._gated_read", side_effect=gated)
        pages.start(); self.addCleanup(pages.stop)

    def read_gap(self):
        row = stuck(self, "old-dispatch", path=self.GONE, run="old-run", handle="old-terminal")
        coordinator = self.outside("coordinator")  # live, so rule S cannot settle it
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": [row]}), \
             patch("pod.admission._terminals", return_value={"runtime": "runtime", "complete": True,
                                                             "terminals": [coordinator]}):
            return admission.query(self.project, worktree=str(self.project), branch="main")

    def test_the_counted_dispatch_moves_a_terminal_records_gap(self):
        self.terminal("closed")
        self.runs({"old-run": "coordinator"})
        self.answer(handles={"old-dispatch": "old-terminal"})
        result = self.read_gap()
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertIn("admission_facts_incomplete", result["unrelated_unknowns"]["by_code"])
        self.configure(None)
        self.assertIn("admission_facts_incomplete", [row["code"] for row in self.read_gap()["unknowns"]])
