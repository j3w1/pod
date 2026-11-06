"""R212 (pod#219, pod#227, pod#275): within one admission call the first `process_exit` request runs alone and the rest
follow in rounds of at most `IN_FLIGHT`; a request answering unverified is asked once more while R leaves room; a
transport or contract fault in any round, or `UNVERIFIED_STOP` unverified answers standing with none verified, starts no
further round, and every candidate not asked holds with that code (A865-A867, A899-A901, A1032, A1033). A fake verifier
replaces Pod's transport, so no process starts, and the deadline cases run on a call clock the test moves by hand."""

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
UNCOVERED, FAILED, TIMEOUT, DEADLINE = (process_exit.UNCOVERED, process_exit.FAILED, process_exit.TIMEOUT,
                                        process_exit.DEADLINE)
FOURTEEN = tuple("abcdefghijklmn")  # with one id per request, eight rounds: a | b c | d e | f g | h i | j k | l m | n


def first_then_rest(requests):
    """The first request, then the others in sorted order: those run together, so they arrive in any order."""
    return requests[0], sorted(requests[1:])


class FakeCase(HostExitCase):
    """A single query whose candidates need one request each (K = 1): three, or `NAMES`."""

    NAMES = ("a", "b", "c")

    def setUp(self):
        super().setUp()
        bound = patch.object(process_exit, "K", 1)
        bound.start(); self.addCleanup(bound.stop)
        self.rows = [stuck(self, name) for name in self.NAMES]

    def fake(self, verifier):
        transport = patch.object(process_exit, "_output", side_effect=verifier)
        transport.start(); self.addCleanup(transport.stop)
        return verifier

    def call(self, seconds, verifier):
        """One call on a clock whose discovery reads return at 3.9 s and rule S's Run list at 4.2 s, so a round needs a
        remaining deadline of 3 + max(4.0, 1.25 * 3.9) = 7.875 s when it starts."""
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
        """An unverified `a` is also asked once more, in the next round beside `b` (pod#275), and answers the same."""
        for mode, code in ANSWERED.items():
            with self.subTest(mode), \
                    patch.object(process_exit, "_output", side_effect=FakeVerifier(modes={"a": mode})) as fake:
                result = self.read(self.rows)
                rest = [["a"], ["b"], ["c"]] if mode == "unverified" else [["b"], ["c"]]
                self.assertEqual(first_then_rest(fake.side_effect.requests), (["a"], rest))
                self.assertEqual(exit_codes(result), {"a": code, "b": "host_exit_query_uncovered",
                                                      "c": "host_exit_query_uncovered"})

    def test_a_clean_first_answer_never_stops_later_requests(self):
        verifier = self.fake(FakeVerifier())
        result = self.read(self.rows)
        self.assertEqual(first_then_rest(verifier.requests), (["a"], [["b"], ["c"]]))
        self.assertTrue(all(row["counted"] for row in result["host_exits"]))
        self.assertEqual(exit_codes(result), {})

    def test_a_fault_after_a_clean_first_answer_holds_only_its_request_and_the_query(self):
        """`b` and `c` share the second round, so `c` was already in flight when `b` failed and keeps its own outcome;
        no later round is left to stop."""
        verifier = self.fake(FakeVerifier(modes={"b": "fail"}))
        result = self.read(self.rows)
        self.assertEqual(first_then_rest(verifier.requests), (["a"], [["b"], ["c"]]))
        self.assertEqual(exit_codes(result), {"a": "host_exit_query_uncovered", "b": "host_exit_failed",
                                              "c": "host_exit_query_uncovered"})


class SequenceTests(FakeCase):
    """A867: the first request runs alone; the second round, `b` and `c`, starts after it under the same deadline
    test (FakeCase.call)."""

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


class InFlight:
    """Each request's start and answer in one order, and the most requests in flight at once. A request waits until its
    whole round, as `rounds` lists each round's first ids, is in flight, so a round run one request at a time breaks
    its barrier instead of passing."""

    def __init__(self, rounds):
        self.lock, self.events, self.flying, self.most, self.barriers = threading.Lock(), [], 0, 0, {}
        for members in rounds:
            self.barriers.update(dict.fromkeys(members, threading.Barrier(len(members), timeout=1.0)))

    def before(self, ids):
        with self.lock:
            self.events.append(("start", ids[0]))
            self.flying += 1
            self.most = max(self.most, self.flying)
        self.barriers[ids[0]].wait()

    def ask(self, real):
        def ask(command, ids, **kwargs):
            answer = real(command, ids, **kwargs)
            with self.lock:
                self.events.append(("end", ids[0]))
                self.flying -= 1
            return answer
        return ask


class FleetCase(VerifierMixin, Rows, batch.FencedCase):
    """Four queries with 24 candidates each on a fake fleet: 96 ids fill the fourteen-request bound, thirteen requests of
    seven ids and one of five."""

    def setUp(self):
        super().setUp()
        self.setup_verifier()
        self.queries = []
        for name in ("wa", "wb", "wc", "wd"):
            (self.project / name).mkdir(exist_ok=True)
            self.stuck_rows(*(f"{name[1]}{index:02d}" for index in range(24)), path=self.project / name)
            self.queries.append({"worktree": str(self.project / name)})

    def batch_with(self, verifier):
        self.native.pages = [batch.terminal_page([])]
        self.native.fleet = batch.fleet_model(self.rows)
        self.claim_values = {"t2": [batch.LIVE_CLAIM]}
        with patch.object(process_exit, "_output", side_effect=verifier):
            return self.batch(*self.queries)

    def assert_equal_to_singles(self, mode, sent):
        """Each query's result equals its single query; the batch sends `sent` requests."""
        verifier = FakeVerifier(mode)
        with patch.object(process_exit, "_output", side_effect=verifier):
            singles, answer = self.calls(*self.queries)
        self.assertEqual([row["result"] for row in answer["responses"]], singles)
        self.assertEqual(answer["discovery"]["host_exit"]["requests"], sent)
        return singles


class CountTests(FleetCase):
    """A867: a failing verifier receives one request of the fourteen the bound allows."""

    def test_a_failing_verifier_receives_one_request_and_a_correct_one_fourteen(self):
        failing, correct = FakeVerifier("fail"), FakeVerifier()
        self.assertEqual(self.batch_with(failing)["discovery"]["host_exit"], {
            "requests": 1, "asked": 7, "counted": 0, "not_asked_after": "host_exit_failed", "not_asked": 89})
        self.assertEqual(failing.requests, [[f"a{index:02d}" for index in range(7)]])
        self.assertEqual(self.batch_with(correct)["discovery"]["host_exit"], {"requests": 14, "asked": 96, "counted": 96})
        self.assertEqual(sorted(map(len, correct.requests)), [5] + [7] * 13)


class RoundTests(FleetCase):
    """A899: fourteen requests run as 1 + 2 x 6 + 1, and each query still equals its single query."""

    ROUNDS = (("a00",), ("a07", "a14"), ("a21", "b04"), ("b11", "b18"), ("c01", "c08"), ("c15", "c22"), ("d05", "d12"),
              ("d19",))

    def test_fourteen_requests_run_one_then_rounds_of_at_most_two(self):
        self.assertEqual((process_exit.K, process_exit.R, process_exit.IN_FLIGHT), (7, 14, 2))
        flight = InFlight(self.ROUNDS)
        with patch.object(process_exit, "ask", side_effect=flight.ask(process_exit.ask)):
            answer = self.batch_with(FakeVerifier(before=flight.before))
        round_of = {first: index for index, members in enumerate(self.ROUNDS) for first in members}
        # Every request of a round starts before any of it answers, and the next round starts after all of it answered.
        expected = [(kind, index) for index, members in enumerate(self.ROUNDS) for kind in ("start", "end")
                    for _ in members]
        self.assertEqual([(kind, round_of[first]) for kind, first in flight.events], expected)
        self.assertEqual(flight.most, 2)
        self.assertEqual(answer["discovery"]["host_exit"], {"requests": 14, "asked": 96, "counted": 96})

    def test_a_correct_verifier_answers_each_query_as_its_single_query(self):
        singles = self.assert_equal_to_singles(None, 14)
        self.assertTrue(all(row["counted"] for single in singles for row in single["host_exits"]))

    def test_a_failing_verifier_answers_each_query_as_its_single_query(self):
        singles = self.assert_equal_to_singles("fail", 1)
        self.assertEqual({code for single in singles for code in exit_codes(single).values()}, {FAILED})

    def test_an_unverified_verifier_answers_each_query_as_its_single_query(self):
        singles = self.assert_equal_to_singles("unverified", 3)
        self.assertEqual({code for single in singles for code in exit_codes(single).values()}, {"host_exit_unverified"})


class Sequenced(FakeVerifier):
    """A fake verifier whose answers for a request, keyed by its first id, follow `sequences` call by call; a request
    past its sequence, or not in it, answers verified and clean."""

    def __init__(self, sequences, **kwargs):
        super().__init__(**kwargs)
        self.sequences = {key: list(value) for key, value in sequences.items()}

    def __call__(self, command, payload):
        key = payload["references"][0]
        with self.lock:
            queue = self.sequences.get(key)
            self.modes[key] = queue.pop(0) if queue else None  # concurrent requests never share a first id
        return super().__call__(command, payload)


class ReaskTests(FakeCase):
    """A1033 with one id per request: a request answering unverified is asked once more, in plan order in a later
    round, only while every unsent request still fits R beside it, under the same deadline test."""

    def test_a_transient_unverified_answer_then_a_verified_one_counts(self):
        verifier = Sequenced({"a": ["unverified", "clean"]})
        answer = self.call(30.0, verifier)
        self.assertEqual(first_then_rest(verifier.requests), (["a"], [["a"], ["b"], ["c"]]))
        self.assertTrue(all(row["counted"] for row in answer["results"][0]["host_exits"]))
        self.assertEqual(answer["discovery"]["host_exit"], {"requests": 4, "asked": 3, "counted": 3, "reasked": 1})

    def test_a_request_is_asked_again_once_and_two_unverified_answers_hold(self):
        verifier = Sequenced({"a": ["unverified", "unverified", "clean"]})
        answer = self.call(30.0, verifier)
        self.assertEqual(sorted(verifier.requests), [["a"], ["a"], ["b"], ["c"]])
        self.assertEqual(exit_codes(answer["results"][0]), {"a": "host_exit_unverified", "b": UNCOVERED,
                                                            "c": UNCOVERED})
        self.assertEqual(answer["discovery"]["host_exit"], {"requests": 4, "asked": 3, "counted": 0, "reasked": 1})

    def test_a_reask_never_displaces_a_planned_request_or_exceeds_r(self):
        """With R 3 there is no room beside the three planned requests; with R 4 `a` takes the one free place, and `b`,
        answering unverified after it, finds none."""
        for bound, sent, a_code in ((3, [["a"], ["b"], ["c"]], "host_exit_unverified"),
                                    (4, [["a"], ["a"], ["b"], ["c"]], UNCOVERED)):
            with self.subTest(R=bound), patch.object(process_exit, "R", bound):
                verifier = Sequenced({"a": ["unverified", "clean"], "b": ["unverified", "clean"]})
                answer = self.call(30.0, verifier)
                self.assertEqual(sorted(verifier.requests), sent)
                self.assertEqual(exit_codes(answer["results"][0]), {"a": a_code, "b": "host_exit_unverified",
                                                                    "c": UNCOVERED})

    def test_a_reask_runs_under_the_deadline_test(self):
        """14 s: 9.8 s remain when `a` starts; it answers unverified after 0.5 s, and its re-ask beside `b` spends 1.5 s,
        leaving 7.8 s, below 7.875 s, so `c` holds the deadline."""
        def spend(ids):
            verifier.clock.now += {"a": 0.5 if len(verifier.requests) == 1 else 0.0, "b": 1.5}.get(ids[0], 0.0)
        verifier = Sequenced({"a": ["unverified", "clean"]}, before=spend)
        result = self.call(14.0, verifier)["results"][0]
        self.assertEqual(sorted(verifier.requests), [["a"], ["a"], ["b"]])
        self.assertEqual(exit_codes(result), {"a": UNCOVERED, "b": UNCOVERED, "c": DEADLINE})

    def test_the_stop_counts_answers_after_their_reask(self):
        """a | a b | b c | c d: three answers stand unverified after their re-asks, so `d`'s re-ask and `e` are never
        sent."""
        self.rows += [stuck(self, name) for name in "de"]
        verifier = FakeVerifier("unverified")
        answer = self.call(30.0, verifier)
        self.assertEqual(sorted(verifier.requests), [["a"], ["a"], ["b"], ["b"], ["c"], ["c"], ["d"]])
        self.assertEqual(exit_codes(answer["results"][0]), dict.fromkeys("abcde", "host_exit_unverified"))
        self.assertEqual(answer["discovery"]["host_exit"], {
            "requests": 7, "asked": 4, "counted": 0, "reasked": 3, "not_asked_after": "host_exit_unverified",
            "not_asked": 1})


class UnverifiedFleetTests(FleetCase):
    """A1032 on the fake fleet: a verifier answering every request unverified receives the first three of fourteen, and
    every query's result equals the result of asking all fourteen."""

    def test_three_unverified_answers_stop_the_batch_and_hold_the_rest_unverified(self):
        verifier = FakeVerifier("unverified")
        stopped = self.batch_with(verifier)
        self.assertEqual(stopped["discovery"]["host_exit"], {
            "requests": 3, "asked": 21, "counted": 0, "not_asked_after": "host_exit_unverified", "not_asked": 75})
        self.assertEqual(verifier.requests[0], [f"a{index:02d}" for index in range(7)])
        with patch.object(process_exit, "UNVERIFIED_STOP", 99):  # no stop: every request is asked and answers unverified
            asked_all = self.batch_with(FakeVerifier("unverified"))
        self.assertEqual(asked_all["discovery"]["host_exit"], {"requests": 14, "asked": 96, "counted": 0})
        self.assertEqual([row["result"] for row in stopped["responses"]],
                         [row["result"] for row in asked_all["responses"]])
        self.assertEqual({code for row in stopped["responses"] for code in exit_codes(row["result"]).values()},
                         {"host_exit_unverified"})


class UnverifiedStopTests(FakeCase):
    """A1032 with one id per request: a | b c | d e | ... Three unverified answers with none verified stop the call; any
    verified answer, or fewer unverified ones, keeps every round."""

    NAMES = FOURTEEN

    def test_three_unverified_answers_stop_after_the_first_round(self):
        self.assertEqual(process_exit.UNVERIFIED_STOP, 3)
        verifier = FakeVerifier("unverified")
        answer = self.call(30.0, verifier)
        self.assertEqual(first_then_rest(verifier.requests), (["a"], [["b"], ["c"]]))
        result = answer["results"][0]
        self.assertEqual(exit_codes(result), dict.fromkeys(FOURTEEN, "host_exit_unverified"))
        self.assertFalse(any(row["counted"] for row in result["host_exits"]))
        self.assertFalse(any("not_asked" in key for row in result["host_exits"] for key in row))
        self.assertEqual(answer["discovery"]["host_exit"], {
            "requests": 3, "asked": 3, "counted": 0, "not_asked_after": "host_exit_unverified", "not_asked": 11})
        with patch.object(process_exit, "UNVERIFIED_STOP", 99):
            asked_all = self.call(30.0, FakeVerifier("unverified"))
        self.assertEqual(asked_all["results"], answer["results"])

    def test_a_verified_answer_keeps_every_later_round(self):
        """A verified first answer, or one verified answer among the first three, usable or not, keeps every round, and
        later unverified answers never stop the call; fewer than three unverified answers never stop it either."""
        cases = {"first clean": {"a": "clean"}, "first round foreign host": {"c": "foreign_host"},
                 "first round stale": {"b": "stale"}}
        for label, modes in cases.items():
            with self.subTest(label):
                verifier = FakeVerifier("unverified", modes=modes)
                answer = self.call(30.0, verifier)
                self.assertEqual(len(verifier.requests), 14)
                self.assertNotIn("not_asked_after", answer["discovery"]["host_exit"])
        with self.subTest("two unverified"):
            verifier = FakeVerifier(modes={"a": "unverified", "b": "unverified"})
            result = self.call(30.0, verifier)["results"][0]
            self.assertEqual(len(verifier.requests), 14)
            self.assertEqual(exit_codes(result), {**dict.fromkeys("ab", "host_exit_unverified"),
                                                  **dict.fromkeys("cdefghijklmn", UNCOVERED)})

    def test_a_fault_in_the_stopping_round_names_the_fault(self):
        verifier = FakeVerifier("unverified", modes={"c": "fail"})
        answer = self.call(30.0, verifier)
        self.assertEqual(len(verifier.requests), 3)
        self.assertEqual(exit_codes(answer["results"][0]), {**dict.fromkeys("ab", "host_exit_unverified"),
                                                            **dict.fromkeys("cdefghijklmn", FAILED)})
        self.assertEqual(answer["discovery"]["host_exit"]["not_asked_after"], FAILED)


class RoundFaultTests(FakeCase):
    """A901: a transport or contract fault in a later round starts no further round; an answer under the contract never
    stops one."""

    NAMES = FOURTEEN

    def test_a_fault_in_a_later_round_starts_no_further_round(self):
        for mode, code in STOPPING.items():
            with self.subTest(mode), \
                    patch.object(process_exit, "_output", side_effect=FakeVerifier(modes={"d": mode})) as fake:
                result = self.read(self.rows)
                self.assertEqual(sorted(fake.side_effect.requests), [[name] for name in "abcde"])
                self.assertEqual(exit_codes(result), {**dict.fromkeys("abce", UNCOVERED),
                                                      **dict.fromkeys("dfghijklmn", code)})

    def test_two_faults_in_one_round_stop_with_the_first_in_plan_order(self):
        """`e` answers before `d`, so the stopping code is the first by plan order, never by arrival."""
        answered = threading.Event()
        def before(ids):
            if ids == ["d"]:
                answered.wait(timeout=1.0)
        real = process_exit.ask
        def ask(command, ids, **kwargs):
            answer = real(command, ids, **kwargs)
            if ids == ["e"]:
                answered.set()
            return answer
        self.fake(FakeVerifier(modes={"d": "timeout", "e": "fail"}, before=before))
        with patch.object(process_exit, "ask", side_effect=ask):
            result = self.read(self.rows)
        self.assertTrue(answered.is_set())
        self.assertEqual(exit_codes(result), {**dict.fromkeys("abc", UNCOVERED), "d": TIMEOUT, "e": FAILED,
                                              **dict.fromkeys("fghijklmn", TIMEOUT)})

    def test_an_answer_under_the_contract_in_a_later_round_never_stops(self):
        for mode, code in ANSWERED.items():
            with self.subTest(mode), \
                    patch.object(process_exit, "_output", side_effect=FakeVerifier(modes={"d": mode})) as fake:
                result = self.read(self.rows)
                self.assertEqual(len(fake.side_effect.requests), 14)
                self.assertEqual(exit_codes(result), {**dict.fromkeys("abcefghijklmn", UNCOVERED), "d": code})


class RoundBatchTests(VerifierMixin, Rows, batch.FencedCase):
    """A901 in a batch with K = 1: two six-candidate queries run as a0 | a1 a2 | a3 a4 | a5 b0 | b1 b2 | b3 b4 | b5."""

    def setUp(self):
        super().setUp()
        self.setup_verifier()
        bound = patch.object(process_exit, "K", 1)
        bound.start(); self.addCleanup(bound.stop)
        self.queries = []
        for name in ("wa", "wb"):
            (self.project / name).mkdir(exist_ok=True)
            self.stuck_rows(*(f"{name[1]}{index}" for index in range(6)), path=self.project / name)
            self.queries.append({"worktree": str(self.project / name)})

    def test_a_fault_in_the_fifth_round_leaves_the_query_answered_before_it_counted(self):
        verifier = FakeVerifier(modes={"b1": "fail"})
        with patch.object(process_exit, "_output", side_effect=verifier):
            singles, answer = self.calls(*self.queries)
        first, second = (row["result"] for row in answer["responses"])
        self.assertTrue(all(row["counted"] for row in first["host_exits"]))
        self.assertEqual(len(first["host_exits"]), 6)
        self.assertEqual(exit_codes(second), {**dict.fromkeys(("b0", "b2"), UNCOVERED),
                                              **dict.fromkeys(("b1", "b3", "b4", "b5"), FAILED)})
        self.assertEqual(answer["discovery"]["host_exit"], {"requests": 9, "asked": 9, "counted": 6,
                                                            "not_asked_after": "host_exit_failed", "not_asked": 3})
        self.assertEqual([row["result"] for row in answer["responses"]], singles)


class RoundDeadlineTests(FakeCase):
    """A900: the deadline test runs before each of the eight rounds (FakeCase.call)."""

    NAMES = FOURTEEN

    def spending(self, seconds):
        """A verifier whose every round spends `seconds` on the call clock, moved by one request of the round."""
        def spend(ids):
            if ids[0] in ("a", "b", "d", "f", "h", "j", "l", "n"):
                verifier.clock.now += seconds
        verifier = FakeVerifier(before=spend)
        return verifier

    def test_a_thirty_second_call_reaches_every_round(self):
        # The last round starts at 4.2 + 7 * seconds, which leaves at least 7.875 s up to about 2.56 s per round.
        for seconds in (0.9, 2.2):  # the verifier's median, and its own deadline
            with self.subTest(seconds=seconds):
                verifier = self.spending(seconds)
                result = self.call(30.0, verifier)["results"][0]
                self.assertEqual(len(verifier.requests), 14)
                self.assertEqual(len(result["host_exits"]), 14)
                self.assertTrue(all(row["counted"] for row in result["host_exits"]))

    def test_a_short_deadline_stops_the_later_rounds(self):
        # 15 s at 1 s per round: rounds start with 10.8, 9.8 and 8.8 s left; then 7.8 s is under 7.875 s.
        verifier = self.spending(1.0)
        result = self.call(15.0, verifier)["results"][0]
        self.assertEqual(sorted(verifier.requests), [[name] for name in "abcde"])
        self.assertEqual(exit_codes(result), {**dict.fromkeys("abcde", UNCOVERED),
                                              **dict.fromkeys("fghijklmn", DEADLINE)})
