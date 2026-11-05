"""R212: the `process_exit` verifier action's wire contract, checks and transport (A850, A851, A858)."""

from datetime import datetime, timedelta, timezone
import json
import os
import signal
from unittest.mock import patch

from pod import process_exit, relay
from tests.kernel_support import KernelCase
from tests.process_exit_support import VerifierMixin

IDS = ["d1", "d2"]


def entry(reference, state="absent", **fields):
    value = {"reference": reference, "known": True, "state": state, "terminal_handle": reference + "-terminal",
             "carriers": {"absent": 0, "present": 1, "partial": None}[state], "checked": ["terminal_handle"]}
    return {**value, **fields}


def verdict(*entries, **fields):
    entries = entries or tuple(entry(reference) for reference in IDS)
    return {"schema": relay.SCHEMA, "action": "process_exit", "verified": True, "host": "local", "runtime": "runtime",
            "method": "whole table", "processes": 7, "observed_at": "2026-11-02T09:00:00Z",
            "dispatches": list(entries), **fields}


class DecodeTests(KernelCase):
    def test_a_well_formed_verdict_decodes_with_its_time_in_utc(self):
        decoded = process_exit.decode(verdict(observed_at="2026-11-02T10:00:00+01:00"), IDS)
        self.assertEqual(decoded["observed_at"], "2026-11-02T09:00:00Z")
        self.assertEqual([row["reference"] for row in decoded["dispatches"]], IDS)

    def test_every_id_is_answered_exactly_once_in_request_order(self):
        cases = {"missing": [entry("d1")], "extra": [entry("d1"), entry("d2"), entry("d3")],
                 "repeated": [entry("d1"), entry("d1")], "reordered": [entry("d2"), entry("d1")]}
        for label, entries in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                process_exit.decode(verdict(*entries), IDS)

    def test_inconsistent_or_unbounded_entries_are_malformed(self):
        cases = {"absent with a carrier": entry("d1", carriers=1),
                 "present without one": entry("d1", "present", carriers=0),
                 "partial with carriers": entry("d1", "partial", carriers=2),
                 "unknown with facts": entry("d1", known=False),
                 "no terminal handle checked": entry("d1", checked=["pane"]),
                 "an unknown check": entry("d1", checked=["terminal_handle", "cgroup"]),
                 "a repeated check": entry("d1", checked=["terminal_handle", "terminal_handle"]),
                 "a handle outside the alphabet": entry("d1", terminal_handle="term 1"),
                 "a count over the bound": entry("d1", "present", carriers=2 ** 31),
                 "an extra field": {**entry("d1"), "pid": 4}}
        for label, row in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                process_exit.decode(verdict(row, entry("d2")), IDS)

    def test_inconsistent_or_unbounded_verdict_facts_are_malformed(self):
        cases = {"no processes": {"processes": 0}, "a quote in the method": {"method": 'say "x"'},
                 "a host outside the alphabet": {"host": "local host"}, "a long runtime": {"runtime": "r" * 65},
                 "a naive time": {"observed_at": "2026-11-02T09:00:00"}, "a long time": {"observed_at": "2" * 41},
                 "verified as text": {"verified": "true"}}
        for label, fields in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                process_exit.decode(verdict(**fields), IDS)
        with self.assertRaises(ValueError):
            process_exit.decode({key: value for key, value in verdict().items() if key != "method"}, IDS)
        with self.assertRaises(ValueError):
            process_exit.decode({**verdict(), "pids": []}, IDS)

    def test_a_scan_time_at_a_calendar_boundary_is_malformed_not_an_error(self):
        """Both bounds parse but cannot be expressed in UTC; the verdict is unusable and nothing is raised."""
        for observed in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
            with self.subTest(observed), self.assertRaises(ValueError):
                process_exit.decode(verdict(observed_at=observed), IDS)

    def test_an_unverified_verdict_carries_no_facts(self):
        empty = {**verdict(), **{key: None for key in (*process_exit.FACT_FIELDS, "dispatches")}, "verified": False}
        self.assertFalse(process_exit.decode(empty, IDS)["verified"])
        with self.assertRaises(ValueError):
            process_exit.decode({**empty, "host": "local"}, IDS)


class JudgeTests(KernelCase):
    SENT = datetime(2026, 11, 2, 9, 0, 0, tzinfo=timezone.utc)

    def judged(self, answer=None, *, host="local", runtime="runtime", **fields):
        decoded = process_exit.decode(verdict(**fields), IDS)
        answer = answer or {"code": None, "verdict": decoded, "sent": self.SENT, "received": self.SENT}
        return process_exit.judge(answer, host=host, runtime=runtime)

    def test_a_usable_verdict_has_no_request_code(self):
        self.assertIsNone(self.judged())

    def test_each_request_level_binding_holds_the_whole_request(self):
        late = (self.SENT + timedelta(seconds=process_exit.SKEW_S + 1)).isoformat()
        early = (self.SENT - timedelta(seconds=process_exit.SKEW_S + 1)).isoformat()
        cases = {"host_exit_mismatch": ({"schema": "pod-relay-verification/v2"}, {}),
                 "host_exit_foreign_host": ({}, {"host": "builder-2"}),
                 "host_exit_foreign_runtime": ({}, {"runtime": "older-runtime"}),
                 "host_exit_stale": ({"observed_at": early}, {})}
        for code, (fields, binding) in cases.items():
            with self.subTest(code):
                self.assertEqual(self.judged(**fields, **binding), code)
        self.assertEqual(self.judged(observed_at=late), "host_exit_stale")
        self.assertEqual(self.judged(action="local_append"), "host_exit_mismatch")
        unverified = {**verdict(), **{key: None for key in (*process_exit.FACT_FIELDS, "dispatches")}, "verified": False}
        answer = {"code": None, "verdict": process_exit.decode(unverified, IDS), "sent": self.SENT, "received": self.SENT}
        self.assertEqual(process_exit.judge(answer, host="local", runtime="runtime"), "host_exit_unverified")

    def test_entry_codes_affect_only_their_own_id(self):
        cases = {None: entry("d1"), "host_exit_host_unknown": {**entry("d1"), "known": False, "state": None,
                 "terminal_handle": None, "carriers": None, "checked": None},
                 "host_exit_identity_mismatch": entry("d1", terminal_handle="other-terminal"),
                 "host_exit_present": entry("d1", "present"), "host_exit_partial": entry("d1", "partial")}
        for code, row in cases.items():
            with self.subTest(code):
                self.assertEqual(process_exit.entry_code(row, "d1-terminal"), code)


class TransportTests(VerifierMixin, KernelCase):
    def setUp(self):
        super().setUp()
        self.setup_verifier(configure=False)

    def ask(self, ids=IDS):
        return process_exit.ask(self.command, ids)

    def test_one_request_carries_only_the_ids_and_answers_each(self):
        answer = self.ask()
        self.assertIsNone(answer["code"])
        self.assertEqual(self.requests(), [{"schema": relay.SCHEMA, "action": "process_exit", "references": IDS}])
        self.assertEqual([row["state"] for row in answer["verdict"]["dispatches"]], ["absent", "absent"])

    def test_transport_faults_hold_and_never_leak_verifier_output(self):
        cases = {"fail": "host_exit_failed", "invalid_json": "host_exit_malformed", "duplicate": "host_exit_malformed",
                 "oversized": "host_exit_malformed", "missing": "host_exit_malformed", "extra": "host_exit_malformed",
                 "repeated": "host_exit_malformed", "reordered": "host_exit_malformed"}
        for mode, code in cases.items():
            with self.subTest(mode):
                self.answer(mode=mode)
                answer = self.ask()
                self.assertEqual((answer["code"], answer["verdict"]), (code, None))
                self.assertNotIn("sentinel", json.dumps(answer, default=str))
        missing = process_exit.ask([str(self.root / "no-such-verifier")], IDS)
        self.assertEqual(missing["code"], "host_exit_failed")

    def test_a_calendar_boundary_scan_time_from_the_process_is_malformed(self):
        for observed in ("0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
            with self.subTest(observed):
                self.answer(mode="overflow", overflow_at=observed)
                answer = self.ask()
                self.assertEqual((answer["code"], answer["verdict"]), ("host_exit_malformed", None))

    def test_a_timeout_kills_the_process_group_and_holds(self):
        self.answer(mode="timeout")
        spawned = []
        real = process_exit.subprocess.Popen
        def popen(*args, **kwargs):
            spawned.append(real(*args, **kwargs))
            return spawned[-1]
        with patch.object(process_exit, "TIMEOUT_S", 0.3), \
             patch.object(process_exit.subprocess, "Popen", side_effect=popen):
            answer = self.ask()
        self.assertEqual(answer["code"], "host_exit_timeout")
        self.assertIsNotNone(spawned[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.killpg(spawned[0].pid, signal.SIGKILL)  # the whole group is already gone

    def test_the_action_has_its_own_time_bound_and_relay_actions_keep_theirs(self):
        """Its own transport: relay's wire function, unchanged for older actions, is never called."""
        with patch.object(relay, "_output", side_effect=AssertionError("relay._output is not this action's path")):
            self.assertIsNone(self.ask()["code"])
        self.assertEqual((process_exit.TIMEOUT_S, relay.TIMEOUT_S), (3.0, 5))


class SizeTests(VerifierMixin, KernelCase):
    """A858: the largest valid verdict for K ids, and for the wire contract's 24, fits the unchanged 16 KiB transport
    bound."""

    SIZES = {8: 4293, 24: 11845}  # ids: the largest verdict's bytes indented by four, with its newline

    def setUp(self):
        super().setUp()
        self.setup_verifier(configure=False)

    def largest(self, count, indent=4):
        """The largest valid answer for `count` ids, every string at its bound and every count at 2^31-1."""
        ids = sorted("d" * 60 + f"{index:04d}" for index in range(count))
        self.answer(entries={reference: {"state": "present", "carriers": 2 ** 31 - 1, "terminal_handle": "h" * 64,
                                         "checked": list(process_exit.CHECKED)} for reference in ids},
                    host="o" * 64, runtime="r" * 64, method="m" * 128, processes=2 ** 31 - 1, indent=indent,
                    observed_at=datetime.now(timezone.utc).isoformat())
        return ids

    def test_the_largest_valid_verdicts_indented_by_four_are_accepted(self):
        self.assertEqual(process_exit.K, 8)
        for count, size in self.SIZES.items():
            with self.subTest(ids=count):
                ids = self.largest(count)
                self.assertIsNone(process_exit.ask(self.command, ids)["code"])
                largest = dict(verdict(*(entry(reference, "present", carriers=2 ** 31 - 1, terminal_handle="h" * 64,
                                               checked=list(process_exit.CHECKED)) for reference in ids),
                                       host="o" * 64, runtime="r" * 64, method="m" * 128, processes=2 ** 31 - 1,
                                       observed_at="2026-11-02T09:00:00.000000+00:00"))
                self.assertEqual(len((json.dumps(largest, indent=4) + "\n").encode()), size)
                self.assertLessEqual(size, relay.MAX_VERDICT)

    def test_a_verdict_over_the_bound_fails_closed(self):
        for count in self.SIZES:
            with self.subTest(ids=count):
                ids = self.largest(count, indent=64)
                self.assertEqual(process_exit.ask(self.command, ids)["code"], "host_exit_malformed")
