"""R212 (pod#219): within one admission call, a transport or contract fault on the first `process_exit` request starts
no further request; every candidate not asked holds with that fault (A865-A867). A fake verifier replaces Pod's
transport, so no process starts, and the deadline cases run on a call clock the test moves by hand."""

import threading
from unittest.mock import patch

from pod import admission, process_exit
from pod.window import Budget
from tests import test_admission_batch as batch
from tests.process_exit_support import FakeVerifier, VerifierMixin, exit_codes, stuck
from tests.test_admission_host_exit import Clock, HostExitCase, Rows

STOPPING = {"fail": "host_exit_failed", "timeout": "host_exit_timeout", "malformed": "host_exit_malformed",
            "mismatch": "host_exit_mismatch"}
ANSWERED = {"unverified": "host_exit_unverified", "foreign_host": "host_exit_foreign_host",
            "foreign_runtime": "host_exit_foreign_runtime", "stale": "host_exit_stale"}


def first_then_rest(requests):
    """The first request, then the others in sorted order: those run together, so they arrive in any order."""
    return requests[0], sorted(requests[1:])


class FakeCase(HostExitCase):
    """A single query whose three candidates need three requests (K = 1)."""

    def setUp(self):
        super().setUp()
        bound = patch.object(process_exit, "K", 1)
        bound.start(); self.addCleanup(bound.stop)
        self.rows = [stuck(self, name) for name in ("a", "b", "c")]

    def fake(self, verifier):
        transport = patch.object(process_exit, "_output", side_effect=verifier)
        transport.start(); self.addCleanup(transport.stop)
        return verifier


class StopTests(FakeCase):
    """A865: which first answers stop a call's requests, and what every candidate then holds."""

    def test_a_transport_or_contract_fault_starts_no_further_request(self):
        for mode, code in STOPPING.items():
            with self.subTest(mode), patch.object(process_exit, "_output", side_effect=FakeVerifier(mode)) as fake:
                result = self.read(self.rows)
                self.assertEqual(fake.side_effect.requests, [["a"]])
                self.assertEqual(exit_codes(result), {"a": code, "b": code, "c": code})
                self.assertFalse(any(row["counted"] for row in result["host_exits"]))
                self.assertFalse(any("not_asked" in key for row in result["host_exits"] for key in row))

    def test_an_answer_under_the_contract_never_stops_the_call(self):
        for mode, code in ANSWERED.items():
            with self.subTest(mode), \
                    patch.object(process_exit, "_output", side_effect=FakeVerifier(modes={"a": mode})) as fake:
                result = self.read(self.rows)
                self.assertEqual(first_then_rest(fake.side_effect.requests), (["a"], [["b"], ["c"]]))
                self.assertEqual(exit_codes(result), {"a": code, "b": "host_exit_query_uncovered",
                                                      "c": "host_exit_query_uncovered"})

    def test_a_clean_first_answer_never_stops_later_requests(self):
        verifier = self.fake(FakeVerifier())
        result = self.read(self.rows)
        self.assertEqual(first_then_rest(verifier.requests), (["a"], [["b"], ["c"]]))
        self.assertTrue(all(row["counted"] for row in result["host_exits"]))
        self.assertEqual(exit_codes(result), {})

    def test_a_fault_after_a_clean_first_answer_holds_only_its_request_and_the_query(self):
        """Every later request already started together, so none is left to stop; each holds its own outcome."""
        verifier = self.fake(FakeVerifier(modes={"b": "fail"}))
        result = self.read(self.rows)
        self.assertEqual(first_then_rest(verifier.requests), (["a"], [["b"], ["c"]]))
        self.assertEqual(exit_codes(result), {"a": "host_exit_query_uncovered", "b": "host_exit_failed",
                                              "c": "host_exit_query_uncovered"})


class SequenceTests(FakeCase):
    """A867: the first request runs alone; the rest start together after it, under the same deadline test.

    On the call clock discovery returns at 3.9 s and rule S's Run list at 4.2 s, so a request needs a remaining
    deadline of 3 + max(4.0, 1.25 * 3.9) = 7.875 s when it starts."""

    def call(self, seconds, verifier):
        clock = Clock()
        budget = Budget(seconds)
        budget._clock, budget.deadline = clock, clock.now + seconds
        def fleet():
            clock.now += 3.9
            return {"runtime": "runtime", "complete": True, "workers": list(self.rows)}
        def runs(runtime, check=None):
            clock.now += 0.3
            return {}
        verifier.clock = clock
        with patch("pod.admission._fleet", side_effect=fleet), \
             patch("pod.admission._terminals", return_value={"runtime": "runtime", "complete": True, "terminals": []}), \
             patch("pod.admission._run_coordinators", side_effect=runs), \
             patch.object(process_exit, "_output", side_effect=verifier):
            return admission._call(self.project, [{"worktree": str(self.project), "branch": "main"}], budget)

    def test_the_rest_start_together_only_after_the_first_returned(self):
        events, lock = [], threading.Lock()
        together = threading.Barrier(2, timeout=1.0)  # breaks unless "b" and "c" are in flight at once
        def before(ids):
            with lock:
                events.append(("start", ids[0], len(events)))
            if ids[0] != "a":
                together.wait()
        verifier = FakeVerifier(before=before)
        real = process_exit.ask
        def ask(command, ids, **kwargs):
            answer = real(command, ids, **kwargs)
            with lock:
                events.append(("end", ids[0], len(events)))
            return answer
        with patch.object(process_exit, "ask", side_effect=ask):
            answer = self.call(18.0, verifier)
        order = [(kind, name) for kind, name, _ in events]
        self.assertEqual(order[:2], [("start", "a"), ("end", "a")])
        self.assertEqual(sorted(order[2:]), [("end", "b"), ("end", "c"), ("start", "b"), ("start", "c")])
        self.assertTrue(all(row["counted"] for row in answer["results"][0]["host_exits"]))

    def test_the_rest_hold_the_deadline_when_the_first_request_spent_it(self):
        # 14 s: 9.8 s remain when the first request starts. A first answer after 2.5 s leaves 7.3 s, below 7.875 s.
        def slow(ids):
            verifier.clock.now += 2.5
        verifier = FakeVerifier(before=slow)
        result = self.call(14.0, verifier)["results"][0]
        self.assertEqual(verifier.requests, [["a"]])
        self.assertEqual(exit_codes(result), {"a": "host_exit_query_uncovered", "b": "host_exit_deadline",
                                              "c": "host_exit_deadline"})

    def test_a_quick_first_answer_leaves_the_rest_their_own_bound(self):
        def quick(ids):
            verifier.clock.now += 0.5
        verifier = FakeVerifier(before=quick)
        result = self.call(14.0, verifier)["results"][0]
        self.assertEqual(first_then_rest(verifier.requests), (["a"], [["b"], ["c"]]))
        self.assertTrue(all(row["counted"] for row in result["host_exits"]))


class StopBatchTests(VerifierMixin, Rows, batch.FencedCase):
    """A866 with K = 2 and R = 2: a batch whose first request fails equals its single queries, whatever the order."""

    def setUp(self):
        super().setUp()
        self.setup_verifier()
        for name, value in (("K", 2), ("R", 2)):
            bound = patch.object(process_exit, name, value)
            bound.start(); self.addCleanup(bound.stop)
        self.queries = {}
        for name, count in (("wa", 2), ("wb", 1), ("wc", 3), ("wd", 5), ("we", 0)):
            (self.project / name).mkdir(exist_ok=True)
            self.stuck_rows(*(f"{name[1]}{index}" for index in range(count)), path=self.project / name)
            self.queries[name] = {"worktree": str(self.project / name)}

    def run_with(self, verifier, *names):
        with patch.object(process_exit, "_output", side_effect=verifier):
            return self.calls(*(self.queries[name] for name in names))

    def test_a_failing_verifier_answers_each_query_as_its_single_query(self):
        """`wa` fills the first request and `wb` a later one; `wc` fits alone but the bound left it out; `wd` is too
        large to be asked alone; `we` has no candidate."""
        for names in (("wa", "wb", "wc", "wd", "we"), ("we", "wd", "wc", "wb", "wa"), ("wb", "wa", "wb", "wc", "wd")):
            with self.subTest(names=names):
                verifier = FakeVerifier("fail")
                singles, answer = self.run_with(verifier, *names)
                for single, row in zip(singles, answer["responses"]):
                    self.assertEqual(row["result"], single)
                results = {name: row["result"] for name, row in zip(names, answer["responses"])}
                for name in ("wa", "wb", "wc"):
                    self.assertEqual(set(exit_codes(results[name]).values()), {"host_exit_failed"})
                self.assertEqual(set(exit_codes(results["wd"]).values()), {"host_exit_call_limit"})
                self.assertNotIn("host_exits", results.get("we", {}))
                self.assertEqual(answer["discovery"]["host_exit"], {
                    "requests": 1, "asked": 2, "counted": 0, "not_asked_after": "host_exit_failed", "not_asked": 4})
                batch_requests = verifier.requests[-1:]
                self.assertEqual(batch_requests, [["a0", "a1"]])

    def test_a_correct_verifier_keeps_every_request_and_the_summary_shape(self):
        verifier = FakeVerifier()
        singles, answer = self.run_with(verifier, "wa", "wb")
        for single, row in zip(singles, answer["responses"]):
            self.assertEqual(row["result"], single)
        self.assertEqual(answer["discovery"]["host_exit"], {"requests": 2, "asked": 3, "counted": 3})


class CountTests(VerifierMixin, Rows, batch.FencedCase):
    """A867: 96 candidates over four queries, the request bound exactly, on a fake fleet."""

    def setUp(self):
        super().setUp()
        self.setup_verifier()
        self.queries = []
        for name in ("wa", "wb", "wc", "wd"):
            (self.project / name).mkdir(exist_ok=True)
            self.stuck_rows(*(f"{name[1]}{index:02d}" for index in range(24)), path=self.project / name)
            self.queries.append({"worktree": str(self.project / name)})

    def requests(self, mode):
        verifier = FakeVerifier(mode)
        self.native.pages = [batch.terminal_page([])]
        self.native.fleet = batch.fleet_model(self.rows)
        self.claim_values = {"t2": [batch.LIVE_CLAIM]}
        with patch.object(process_exit, "_output", side_effect=verifier):
            answer = self.batch(*self.queries)
        return len(verifier.requests), answer["discovery"]["host_exit"]

    def test_a_failing_verifier_receives_one_request_and_a_correct_one_four(self):
        self.assertEqual(self.requests("fail"), (1, {"requests": 1, "asked": 24, "counted": 0,
                                                     "not_asked_after": "host_exit_failed", "not_asked": 72}))
        self.assertEqual(self.requests(None), (4, {"requests": 4, "asked": 96, "counted": 96}))
