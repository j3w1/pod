"""A429-A441: standing consent at production boundaries, with disposable ports only."""

from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import StringIO
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from pod import relay, standing
from pod.errors import PodError
from pod.governor import (_read_journal, _record_path, _write_journal, observe_candidate,
                          prepare_candidate, validate_authorization)
from pod.internal import run
from pod.ledger import _authorization_granted, _path, _read, _write, read, state_root
from pod.util import context_decisions, digest, encoded
from tests.kernel_support import action, git
from tests.test_relayed_authorization import RelayCase, VERIFIER


BASELINE = "3ba7c64f34da8471b289e92445a929369020f581"
REPOSITORY = Path(__file__).resolve().parents[1]
DECISION = {"provenance": "user_direct", "instruction": "use yolo from now on"}
ANSWER = {"question": "Q1", "text": "Use the existing endpoint", "relay": {"reference": "lead-answer"}}
RESPONDER_VERIFIER = VERIFIER.replace(
    '"owner_source": "authenticated-owner:fixture"',
    '("responder_source" if request["action"] == "answer" else "owner_source"): '
    '("authenticated-responder:fixture" if request["action"] == "answer" else "authenticated-owner:fixture")').replace(
    '"decided_at": "2026-09-21T00:00:00Z"',
    '"decided_at": controls.get("standing_decided_at", "2026-09-21T00:00:00Z")').replace(
    '    verdict.pop(key)',
    '    target = verdict\n    parts = key.split(".")\n    for part in parts[:-1]:\n        target = target[part]\n    target.pop(parts[-1], None)')


class StandingCase(RelayCase):
    def setUp(self):
        super().setUp()
        self.script.write_text(RESPONDER_VERIFIER)
        self.verifier_times = {}
        for target in ("pod.orca.executable", "pod.github._run"):
            guard = patch(target, side_effect=AssertionError("standing test escaped its disposable ports"))
            guard.start(); self.addCleanup(guard.stop)

    def operation(self, operation="activate", *, objective="objective", **fields):
        if operation in ("activate", "revoke"):
            fields.setdefault("decision", dict(DECISION))
            if fields["decision"].get("channel") == "relayed":
                controls = json.loads(self.controls.read_text())
                key = controls.get("changes", {}).get("single_use", "decision-one")
                # Deterministic for an interrupted receipt, fresh for a new decision.
                controls["standing_decided_at"] = self.verifier_times.setdefault(
                    key, datetime.now(timezone.utc).isoformat())
                self.controls.write_text(json.dumps(controls))
        return run("standing-authorization", {"project": str(self.project), "objective": objective,
                    "owner": "owner", "action": operation, **fields})

    def activate(self, **fields):
        return self.operation(decision={**DECISION, **fields})

    def proposed(self, kind="push", **fields):
        targets = {"push": "origin/candidate", "pr_update": "origin/candidate", "merge": "origin/target",
                   "workflow_dispatch": "ci.yml", "validation_rerun": "ci.yml", "remote_diagnostic": "probe.yml",
                   "cancel_validation": "old-record", "release": "v-next", "deploy": "production"}
        if kind == "remote_diagnostic":
            fields.setdefault("diagnostic", {key: "bounded probe" for key in
                              ("question", "local_limitation", "check", "stopping_condition")})
        fields.setdefault("effects", [])
        return action(kind=kind, candidate=self.case.observed["commit"], target=targets[kind],
                      unit="delivery", **fields)

    def saved(self):
        return _path(self.project, "objective").read_bytes(), _record_path(self.project, "objective").read_bytes()

    def receipts(self):
        return {path.name: path.read_bytes() for path in (state_root(self.project) / ".relay-authorizations").glob("*.json")}

    def refusal(self, code, callback, *args, **kwargs):
        before, calls = self.saved(), deepcopy(self.case.remote.calls)
        with self.assertRaises(PodError) as caught:
            callback(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.case.remote.calls, calls)
        return caught.exception

    def public_status(self, *, by_run=False):
        from pod.status import status
        from pod.operations import OrcaPort
        with patch.object(OrcaPort, "read_native", side_effect=self.case.port.read_native):
            return status(self.project, "run", objective=None if by_run else "objective",
                          worker_rows_fn=lambda _: {"workers": [], "scope": {"run_id": "run"}, "complete": True})


class StandingOrderingTests(StandingCase):
    MOMENT = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def at(self, seconds, operation="activate", *, relayed=False, key="delayed", **fields):
        decision = {**DECISION, **fields}
        if relayed:
            decision.update(channel="relayed", relay={"reference": key})
        return standing.operate({"project": str(self.project), "objective": "objective", "owner": "owner",
                                 "action": operation, "decision": decision}, now=self.MOMENT + timedelta(seconds=seconds))

    def relay_time(self, seconds, key="delayed", *, scope=None):
        self.control(changes={"single_use": key, "decided_at":
                              (self.MOMENT + timedelta(seconds=seconds)).isoformat(),
                              **({"standing.scope_digest": scope} if scope is not None else {})})

    def assert_stale(self, callback, *args, stopped=True, **kwargs):
        receipts = self.receipts()
        exc = self.refusal("standing_authorization_stale", callback, *args, **kwargs)
        self.assertTrue(exc.detail["next_action"])
        self.assertEqual(self.receipts(), receipts)
        if stopped:
            for kind in ("push", "deploy"):
                held = self.case.op("governor", action=self.proposed(kind))
                self.assertEqual(held["decision"], "DEFER")
                self.assertIn("authorization_missing", [row["code"] for row in held["reasons"]])
        return exc

    def test_n1_delayed_activation_before_session_stop_cannot_restore_push_or_deploy(self):
        self.at(0)
        self.at(2, "revoke")
        self.relay_time(1)
        self.assert_stale(self.at, 3, relayed=True, widening=["deploy"])

    def test_n1_interrupted_receipt_before_stop_cannot_complete_after_stop(self):
        self.at(0)
        self.relay_time(1)
        with patch("pod.ledger._write", side_effect=OSError("interrupted replacement")):
            with self.assertRaises(OSError):
                self.at(1, relayed=True, widening=["deploy"])
        self.assertEqual(len(self.receipts()), 1)
        self.at(2, "revoke")
        self.assert_stale(self.at, 3, relayed=True, widening=["deploy"])

    def test_n1_activation_orders_by_verified_stop_time_instead_of_recording_time(self):
        self.at(0)
        self.relay_time(2, "stop")
        self.at(10, "revoke", relayed=True, key="stop")
        self.relay_time(2)
        self.assert_stale(self.at, 11, relayed=True)
        self.relay_time(3, "fresh")
        accepted = self.at(12, relayed=True, key="fresh", widening=["deploy"])
        self.assertEqual(accepted["standing_authorization"]["state"], "active")
        self.assertEqual(self.case.op("governor", action=self.proposed("deploy"))["decision"], "ALLOW")

    def test_n1_activation_before_newer_activation_is_stale(self):
        self.at(2)
        self.relay_time(1)
        self.assert_stale(self.at, 3, relayed=True, stopped=False)

    def test_n1_newer_relay_follows_activation_recorded_late(self):
        # On an existing record (here created by a stop), owner time orders decisions:
        # a late-recorded older relay never makes a genuinely newer decision stale.
        self.at(0, "revoke")
        self.relay_time(1, "first")
        self.at(10, relayed=True, key="first")
        self.relay_time(3, "newer")
        result = self.at(11, relayed=True, key="newer")
        self.assertEqual(result["standing_authorization"]["state"], "active")
        self.assertEqual(result["decision"]["id"], "Y2")

    def test_r5_2_first_record_creating_relay_sets_one_time_receipt_cutoff(self):
        self.relay_time(1, "first")
        created = self.at(10, relayed=True, key="first")
        self.assertEqual(created["decision"]["relay_verification"]["decided_at"], "2030-01-01T00:00:01Z")
        self.assertEqual(read(self.project, "objective")["standing_authorization"]["not_before"], "2030-01-01T00:00:10Z")
        # Another decision made before the record existed needs a fresh owner decision.
        self.relay_time(3, "in-flight-before-record")
        self.assert_stale(self.at, 11, relayed=True, key="in-flight-before-record", stopped=False)
        self.relay_time(12, "after-record")
        result = self.at(13, relayed=True, key="after-record", widening=["deploy"])
        self.assertEqual(result["decision"]["id"], "Y2")
        self.assertEqual(self.case.op("governor", action=self.proposed("deploy"))["decision"], "ALLOW")

    def test_r5_2_creation_cutoff_never_loses_a_newer_stop(self):
        self.relay_time(1, "first")
        self.at(10, relayed=True, key="first")
        # The stop was decided after the activation but before its delayed receipt.
        self.relay_time(5, "stop")
        result = self.at(11, "revoke", relayed=True, key="stop")
        self.assertEqual(result["standing_authorization"]["state"], "revoked")
        for kind in ("push", "deploy"):
            self.assertEqual(self.case.op("governor", action=self.proposed(kind))["decision"], "DEFER")

    def test_n1_old_revoke_refuses_after_session_activation_but_tie_keeps_stop(self):
        self.at(2)
        self.relay_time(1)
        self.assert_stale(self.at, 3, "revoke", relayed=True, stopped=False)
        self.relay_time(2, "tie")
        result = self.at(3, "revoke", relayed=True, key="tie")
        self.assertEqual(result["standing_authorization"]["state"], "revoked")

    def test_n1_closure_and_reopen_times_survive_and_reject_older_activation(self):
        self.at(0)
        with patch("pod.standing._clock", return_value=self.MOMENT + timedelta(seconds=2)):
            self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        with patch("pod.standing._clock", return_value=self.MOMENT + timedelta(seconds=4)):
            self.case.write([self.case.criterion()], reopen=True, revision_authority=DECISION)
        for seconds in (1, 2, 3, 4):
            self.relay_time(seconds, f"old-{seconds}")
            self.assert_stale(self.at, 5, relayed=True, key=f"old-{seconds}")
        record = read(self.project, "objective")["standing_authorization"]
        self.assertEqual(record["closed_at"], "2030-01-01T00:00:02Z")
        self.assertEqual(record["reopened_at"], "2030-01-01T00:00:04Z")
        self.relay_time(5, "fresh")
        self.assertEqual(self.at(6, relayed=True, key="fresh")["standing_authorization"]["state"], "active")

    def test_n1_first_relay_after_normal_closure_obeys_reopen_time(self):
        with patch("pod.standing._clock", return_value=self.MOMENT + timedelta(seconds=2)):
            closed = self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        self.assertNotIn("standing_authorization", closed["checkpoint"]["closure"]["report"])
        with patch("pod.standing._clock", return_value=self.MOMENT + timedelta(seconds=4)):
            self.case.write([self.case.criterion()], reopen=True, revision_authority=DECISION)
        self.assertNotIn("standing_authorization", read(self.project, "objective"))
        self.relay_time(3)
        self.assert_stale(self.at, 5, relayed=True)
        # No prior reopen time exists, so even a newer first relay fails safe.
        self.relay_time(6, "newer-first")
        exc = self.assert_stale(self.at, 7, relayed=True, key="newer-first")
        self.assertIn("coordinator's session", exc.detail["next_action"])
        self.assertEqual(exc.detail["reason"], "reopen_time_unavailable")
        closed = self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        self.assertNotIn("standing_authorization", closed["checkpoint"]["closure"]["report"])

    def test_e3_session_activation_after_legacy_reopen_allows_a_newer_relay(self):
        self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        self.case.write([self.case.criterion()], reopen=True, revision_authority=DECISION)
        self.at(2)
        self.relay_time(3, "post-session")
        self.assertEqual(self.at(4, relayed=True, key="post-session")["standing_authorization"]["state"], "active")

    def test_e1_stop_while_off_blocks_older_activation_and_interrupted_receipt(self):
        self.relay_time(1, "interrupted-off")
        with patch("pod.ledger._write", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.at(1, relayed=True, key="interrupted-off", widening=["deploy"])
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual(self.at(2, "revoke"), {"state": "none", "changed": False})
        record = read(self.project, "objective")["standing_authorization"]
        self.assertEqual(record["decisions"], [])
        self.assertEqual(record["not_before"], "2030-01-01T00:00:02Z")
        self.assert_stale(self.at, 3, relayed=True, key="interrupted-off", widening=["deploy"])

    def test_e1_verified_noop_stop_uses_decision_time_without_consuming_key(self):
        self.relay_time(2, "off-stop")
        self.assertEqual(self.at(10, "revoke", relayed=True, key="off-stop"), {"state": "none", "changed": False})
        record = read(self.project, "objective")["standing_authorization"]
        self.assertEqual(record["not_before"], "2030-01-01T00:00:02Z")
        self.assertEqual(record["decisions"], [])
        self.assertEqual(self.receipts(), {})
        self.relay_time(1, "older-off")
        self.assert_stale(self.at, 11, relayed=True, key="older-off")
        self.relay_time(3, "newer-off")
        self.assertEqual(self.at(12, relayed=True, key="newer-off")["standing_authorization"]["state"], "active")

    def test_e1_failed_noop_verifier_uses_receipt_and_blocks_older_activation(self):
        self.control(mode="fail")
        self.assertEqual(self.at(2, "revoke", relayed=True, key="unavailable-stop"), {"state": "none", "changed": False})
        self.assertEqual(read(self.project, "objective")["standing_authorization"]["not_before"], "2030-01-01T00:00:02Z")
        self.assertEqual(self.receipts(), {})
        self.relay_time(1, "before-unavailable-stop")
        self.assert_stale(self.at, 3, relayed=True, key="before-unavailable-stop")

    def test_e1_session_decision_time_is_recorded_after_lock_authority_read(self):
        from pod.ledger import require_authority
        self.at(0)
        self.at(2, "revoke")
        clock = [self.MOMENT + timedelta(seconds=1)]

        def authority(*args, **kwargs):
            result = require_authority(*args, **kwargs)
            clock[0] = self.MOMENT + timedelta(seconds=3)
            return result

        request = {"project": str(self.project), "objective": "objective", "owner": "owner", "action": "activate",
                   "decision": DECISION}
        with patch("pod.standing._clock", side_effect=lambda now: now if now is not None else clock[0]), \
                patch("pod.ledger.require_authority", side_effect=authority):
            row = standing.operate(request)["decision"]
        self.assertEqual(row["utc"], "2030-01-01T00:00:03Z")
        self.relay_time(2.5, "before-session-recording")
        self.assert_stale(self.at, 4, relayed=True, key="before-session-recording", stopped=False)

    def test_e5_future_activation_refuses_and_future_stop_uses_receipt_barrier(self):
        self.at(0)
        self.at(1, "revoke")
        self.relay_time(3600, "future-on")
        receipts = self.receipts()
        self.refusal("standing_authorization_future", self.at, 2, relayed=True, key="future-on", widening=["deploy"])
        self.assertEqual(len(self.receipts()), len(receipts) + 1)
        self.assertEqual(self.operation("show")["standing_authorization"]["state"], "revoked")
        self.relay_time(3, "fresh-on")
        self.assertEqual(self.at(3, relayed=True, key="fresh-on")["standing_authorization"]["state"], "active")
        self.relay_time(3600, "future-off")
        row = self.at(4, "revoke", relayed=True, key="future-off")["decision"]
        self.assertEqual(row["relay_verification"]["decided_at"], "2030-01-01T01:00:00Z")
        self.assertEqual(standing.decision_time(row), "2030-01-01T00:00:04Z")
        self.assertEqual(self.at(5)["standing_authorization"]["state"], "active")

    def test_e2_scope_digest_and_sticky_barrier_cover_distinct_changes(self):
        self.at(0)
        original = standing.scope_digest(read(self.project, "objective"))
        added = {"id": "O2", "kind": "criterion", "provenance": "user_direct",
                 "source": {"instruction": "Add PoD#2", "ref": "PoD#2"}, "check": "PoD#2 passes",
                 "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        with patch("pod.standing._clock", return_value=self.MOMENT + timedelta(seconds=2)):
            self.case.write([*self.case.stored(), added], criteria=["PoD#1", "PoD#2"], revision_authority=DECISION)
        record = read(self.project, "objective")["standing_authorization"]
        self.assertEqual(record["not_before"], "2030-01-01T00:00:02Z")
        self.assertEqual(record["scope_changed_for"], "Y1")
        self.relay_time(1, "before-scope", scope=original)
        exc = self.refusal("relay_verifier_mismatch", self.at, 5, relayed=True, key="before-scope", widening=["deploy"])
        self.assertEqual(exc.detail["fields"], ["standing.scope_digest"])
        self.relay_time(3, "after-scope")
        self.assertEqual(self.at(6, relayed=True, key="after-scope")["standing_authorization"]["state"], "active")

    def test_a1_noop_session_stop_uses_time_after_waiting_for_lock(self):
        from pod.ledger import _lock
        clock = [self.MOMENT]

        @contextmanager
        def waited(path):
            with _lock(path):
                # Deterministically model acquisition after an earlier writer.
                clock[0] = self.MOMENT + timedelta(seconds=2)
                yield

        with patch("pod.ledger._lock", side_effect=waited), \
                patch("pod.standing._clock", side_effect=lambda now: now if now is not None else clock[0]):
            result = standing.operate({"project": str(self.project), "objective": "objective", "owner": "owner",
                                       "action": "revoke", "decision": DECISION})
        self.assertEqual(result, {"state": "none", "changed": False})
        self.assertEqual(read(self.project, "objective")["standing_authorization"]["not_before"], "2030-01-01T00:00:02Z")
        self.relay_time(1, "during-lock-wait")
        self.assert_stale(self.at, 3, relayed=True, key="during-lock-wait")

    def test_a2_unverifiable_stop_while_on_sets_receipt_barrier_without_revoking(self):
        self.at(0)
        before, journal = read(self.project, "objective"), self.saved()[1]
        receipts = self.receipts()
        self.control(mode="fail")
        with self.assertRaises(PodError) as caught:
            self.at(6, "revoke", relayed=True, key="unverifiable-stop")
        self.assertEqual(caught.exception.code, "relay_verifier_failed")
        after = read(self.project, "objective")
        self.assertEqual(after["standing_authorization"]["decisions"], before["standing_authorization"]["decisions"])
        self.assertEqual(after["standing_authorization"]["not_before"], "2030-01-01T00:00:06Z")
        self.assertEqual(standing.state(after, now=self.MOMENT), "active")
        self.assertEqual(self.saved()[1], journal)
        self.assertEqual(self.receipts(), receipts)
        self.relay_time(5, "before-failed-stop")
        self.assert_stale(self.at, 10, relayed=True, key="before-failed-stop", widening=["deploy"], stopped=False)
        self.assertEqual(self.case.op("governor", action=self.proposed("deploy"))["decision"], "DEFER")
        self.relay_time(7, "after-failed-stop")
        self.assertEqual(self.at(11, relayed=True, key="after-failed-stop")["standing_authorization"]["state"], "active")

    def test_f2_future_activation_key_stays_refused_after_stop_and_clock_catches_up(self):
        self.relay_time(3600, "future-old")
        self.refusal("standing_authorization_future", self.at, 10, relayed=True, key="future-old", widening=["deploy"])
        receipt = self.receipts()
        self.assertEqual(len(receipt), 1)
        self.assertEqual(json.loads(next(iter(receipt.values())))["refused"], "standing_authorization_future")
        self.assertNotIn("standing_authorization", read(self.project, "objective"))
        self.at(20, "revoke")
        self.refusal("relay_authorization_reused", self.at, 3700, relayed=True, key="future-old", widening=["deploy"])
        self.assertEqual(self.receipts(), receipt)
        for kind in ("push", "deploy"):
            self.assertEqual(self.case.op("governor", action=self.proposed(kind))["decision"], "DEFER")
        self.relay_time(3701, "new-owner-grant")
        self.assertEqual(self.at(3702, relayed=True, key="new-owner-grant")["standing_authorization"]["state"], "active")


class StandingScopeDigestTests(StandingCase):
    MOMENT = StandingOrderingTests.MOMENT
    at = StandingOrderingTests.at
    relay_time = StandingOrderingTests.relay_time

    def change_scope(self, number):
        added = {"id": f"O{number}", "kind": "criterion", "provenance": "user_direct",
                 "source": {"instruction": f"Add PoD#{number}", "ref": f"PoD#{number}"},
                 "check": f"PoD#{number} passes", "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        self.case.write([*self.case.stored(), added], criteria=[f"PoD#{i}" for i in range(1, number + 1)],
                        revision_authority=DECISION)

    def old_digest_refuses(self, *, on=False, stopped=False, twice=False):
        if on:
            self.at(0)
        if twice:
            self.change_scope(2)
        digest_seen = self.public_status()["standing_authorization"]["scope_digest"]
        if stopped:
            self.at(2.5 if twice else 0.5, "revoke")
        self.change_scope(3 if twice else 2)
        current = self.public_status()["standing_authorization"]["scope_digest"]
        self.assertNotEqual(current, digest_seen)
        self.relay_time(3 if twice else 1, "old-owner-decision", scope=digest_seen)
        receipts = self.receipts()
        with patch("pod.relay.check_use", side_effect=AssertionError("scope mismatch reached key use")):
            exc = self.refusal("relay_verifier_mismatch", self.at, 10, relayed=True, key="old-owner-decision", widening=["deploy"])
        self.assertEqual(exc.detail["fields"], ["standing.scope_digest"])
        self.assertIn("fresh owner decision", exc.detail["next_action"])
        self.assertEqual(self.receipts(), receipts)
        for kind in ("push", "deploy"):
            self.assertEqual(self.case.op("governor", action=self.proposed(kind))["decision"], "DEFER")
        if not on and not stopped:
            context = read(self.project, "objective")
            self.assertNotIn("standing_authorization", context)
            self.assertEqual(context.get("decision_channels", 1), 1)
        self.relay_time(11, "current-owner-decision", scope=current)
        accepted = self.at(12, relayed=True, key="current-owner-decision", widening=["deploy"])
        self.assertEqual(accepted["decision"]["binding"]["scope_digest"], current)
        self.assertEqual(self.case.op("governor", action=self.proposed("deploy"))["decision"], "ALLOW")

    def test_f1_s1a_scope_before_any_activation_refuses_old_digest(self):
        self.old_digest_refuses()

    def test_f1_s1b_stop_while_none_then_scope_refuses_old_digest(self):
        self.old_digest_refuses(stopped=True)

    def test_f1_s2a_second_scope_change_refuses_digest_from_between_changes(self):
        self.old_digest_refuses(on=True, twice=True)

    def test_f1_s2c_second_change_after_stop_refuses_digest_from_between_changes(self):
        self.old_digest_refuses(on=True, stopped=True, twice=True)

    def test_f1_missing_or_unknown_digest_refuses_before_key_use(self):
        for controls in ({"omit": ["standing.scope_digest"]}, {"omit": ["standing"]},
                         {"changes": {"standing.scope_digest": "0" * 64}},
                         {"changes": {"standing.scope_digest": None}}):
            with self.subTest(controls=controls):
                self.control(**controls)
                with patch("pod.relay.check_use", side_effect=AssertionError("invalid binding used a key")):
                    exc = self.refusal("relay_verifier_mismatch", self.activate,
                                       channel="relayed", relay={"reference": "unknown-scope"})
                self.assertEqual(exc.detail["fields"], ["standing.scope_digest"])
                self.assertIn("fresh owner decision", exc.detail["next_action"])
                self.assertEqual(self.receipts(), {})

    def test_f2_inauthentic_future_verdicts_never_poison_a_key(self):
        changes = ({"verified": False}, {"objective": "other"}, {"standing.scope_digest": "0" * 64},
                   {"decided_at": "not-a-time"})
        for change in changes:
            with self.subTest(change=change):
                self.control(changes={"single_use": "future-untrusted", "decided_at": "2099-01-01T00:00:00Z", **change})
                before = self.saved()
                with self.assertRaises(PodError):
                    self.activate(channel="relayed", relay={"reference": "future-untrusted"})
                self.assertEqual(self.saved(), before)
                self.assertEqual(self.receipts(), {})

    def test_f2_same_key_concurrent_future_refusals_leave_one_durable_marker(self):
        from concurrent.futures import ThreadPoolExecutor
        request = {"project": str(self.project), "objective": "objective", "owner": "owner", "action": "activate",
                   "decision": {**DECISION, "channel": "relayed", "relay": {"reference": "future-concurrent"}}}

        def verify(command, value):
            return {**deepcopy(value), "verified": True, "owner_source": "authenticated-owner:fixture",
                    "decided_at": (self.MOMENT + timedelta(seconds=3600)).isoformat(), "single_use": "future-concurrent"}

        def attempt(_):
            try:
                standing.operate(request, now=self.MOMENT + timedelta(seconds=10))
            except PodError as exc:
                return exc.code
            self.fail("future activation was accepted")

        before = self.saved()
        with patch("pod.relay.verify", side_effect=verify), ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(attempt, range(2)))
        self.assertCountEqual(codes, ["standing_authorization_future", "relay_authorization_reused"])
        self.assertEqual(self.saved(), before)
        receipt = self.receipts()
        self.assertEqual(len(receipt), 1)
        # The durable marker survives losing the first refusal's response.
        with patch("pod.relay.verify", side_effect=verify):
            self.refusal("relay_authorization_reused", standing.operate, request,
                         now=self.MOMENT + timedelta(seconds=3700))
        self.assertEqual(self.receipts(), receipt)

    def test_f2_refusal_does_not_overwrite_an_interrupted_or_accepted_receipt(self):
        self.relay_time(1, "accepted-key")
        with patch("pod.ledger._write", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.at(1, relayed=True, key="accepted-key")
        receipt = self.receipts()
        self.refusal("relay_authorization_reused", self.at, 0, relayed=True, key="accepted-key")
        self.assertEqual(self.receipts(), receipt)
        self.at(2, relayed=True, key="accepted-key")
        self.refusal("relay_authorization_reused", self.at, 0, relayed=True, key="accepted-key")
        self.assertEqual(self.receipts(), receipt)


class StandingScopeHistoryTests(StandingCase):
    MOMENT = StandingOrderingTests.MOMENT
    at = StandingOrderingTests.at
    relay_time = StandingOrderingTests.relay_time
    assert_stale = StandingOrderingTests.assert_stale
    change_scope = StandingScopeDigestTests.change_scope

    def change_at(self, seconds, number):
        with patch("pod.standing._clock", return_value=self.MOMENT + timedelta(seconds=seconds)):
            self.change_scope(number)

    def restore_at(self, seconds, count):
        rows = self.case.stored()
        rows[-1].pop("wait", None)
        rows[-1].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "restore the earlier scope"})
        with patch("pod.standing._clock", return_value=self.MOMENT + timedelta(seconds=seconds)):
            self.case.write(rows, criteria=[f"PoD#{i}" for i in range(1, count + 1)], revision_authority=DECISION)

    def holder_bcb(self, *, revoked=False):
        self.at(0)
        if revoked:
            self.at(1, "revoke")
        self.change_at(2, 2)
        # Save the digest with the simulated owner decision on B. The verifier
        # returns this saved value after C and restoration, never a fresh echo.
        confirmed = self.public_status()["standing_authorization"]["scope_digest"]
        self.relay_time(3, "owner-on-B", scope=confirmed)
        self.change_at(4, 3)
        self.restore_at(5, 2)
        self.assert_stale(self.at, 10, relayed=True, key="owner-on-B", widening=["deploy"])
        record = read(self.project, "objective")["standing_authorization"]
        self.assertEqual(record["not_before"], "2030-01-01T00:00:05Z")
        self.assertEqual(record["scope_changed_for"], "Y1")
        self.relay_time(6, "new-owner-on-B", scope=confirmed)
        self.assertEqual(self.at(11, relayed=True, key="new-owner-on-B")["standing_authorization"]["state"], "active")

    def test_r4_1_holder_bcb_refuses_grant_before_the_intermediate_change(self):
        self.holder_bcb()

    def test_r4_1_revoked_holder_bcb_keeps_each_scope_barrier(self):
        self.holder_bcb(revoked=True)

    def test_r4_1_stop_created_record_remembers_aba_without_any_activation(self):
        self.at(0.5, "revoke")
        confirmed = self.public_status()["standing_authorization"]["scope_digest"]
        self.relay_time(1, "owner-on-A", scope=confirmed)
        self.change_at(2, 2)
        self.restore_at(4, 1)
        self.assert_stale(self.at, 10, relayed=True, key="owner-on-A", widening=["deploy"])
        record = read(self.project, "objective")["standing_authorization"]
        self.assertEqual(record["decisions"], [])
        self.assertEqual(record["not_before"], "2030-01-01T00:00:04Z")
        self.assertEqual(self.at(11)["standing_authorization"]["state"], "active")

    def test_r4_1_identical_observations_keep_the_latest_change_time(self):
        self.at(0)
        self.change_at(2, 2)
        self.change_at(4, 3)
        for seconds in (6, 8):
            with patch("pod.standing._clock", return_value=self.MOMENT + timedelta(seconds=seconds)):
                self.case.write(self.case.stored(), criteria=["PoD#1", "PoD#2", "PoD#3"])
            self.assertEqual(read(self.project, "objective")["standing_authorization"]["not_before"],
                             "2030-01-01T00:00:04Z")

    def test_r4_1_missing_or_malformed_history_cannot_use_the_never_record_exception(self):
        self.at(0.5, "revoke")
        self.relay_time(1, "owner-on-A", scope=self.public_status()["standing_authorization"]["scope_digest"])
        context = read(self.project, "objective")
        for mutation in ("lost_record", "lost_digest", "invalid_digest"):
            with self.subTest(mutation=mutation):
                altered = deepcopy(context)
                if mutation == "lost_record":
                    altered.pop("standing_authorization")
                elif mutation == "lost_digest":
                    altered["standing_authorization"].pop("observed_scope_digest")
                else:
                    altered["standing_authorization"]["observed_scope_digest"] = "invalid"
                _path(self.project, "objective").write_bytes(encoded(altered, compact=True))
                receipts = self.receipts()
                exc = self.refusal("state_unsupported", self.at, 10, relayed=True, key="owner-on-A")
                self.assertIn("never-record", exc.detail["next_action"])
                self.assertEqual(self.receipts(), receipts)


class StandingRecordTests(StandingCase):
    def test_a3_record_without_its_required_barrier_refuses_without_derivation(self):
        self.activate()
        context = read(self.project, "objective")
        context["standing_authorization"].pop("not_before")
        path = _path(self.project, "objective")
        path.write_bytes(encoded(context, compact=True))
        before = self.saved()
        with self.assertRaises(PodError) as caught:
            _read(path)
        self.assertEqual(caught.exception.code, "state_unsupported")
        self.assertEqual(self.saved(), before)

    def test_n3_noop_relay_revoke_reports_state_when_verifier_is_unavailable(self):
        decision = {**DECISION, "channel": "relayed", "relay": {"reference": "stop"}}
        self.configure(None)
        receipts = self.receipts()
        self.assertEqual(self.operation("revoke", decision=decision), {"state": "none", "changed": False})
        record = read(self.project, "objective")["standing_authorization"]
        self.assertEqual(record["decisions"], [])
        self.assertIn("not_before", record)
        self.assertEqual(self.receipts(), receipts)
        self.activate()
        self.operation("revoke")
        barrier = read(self.project, "objective")["standing_authorization"]["not_before"]
        with patch("pod.standing._verify", side_effect=PodError("relay_verifier_failed", "unavailable")) as verify:
            self.assertEqual(self.operation("revoke", decision=decision), {"state": "revoked", "changed": False})
        verify.assert_called_once()
        self.assertGreater(read(self.project, "objective")["standing_authorization"]["not_before"], barrier)
        self.assertEqual(self.receipts(), receipts)
        self.assertFalse(self.requests.exists())

    def test_encoded_row_caps_refuse_before_any_receipt_is_consumed(self):
        # A fresh objective makes the byte limit discriminate independently of counts.
        self.case.write([self.case.criterion()], objective="row-size", governance={"base_ref": "target"})
        self.control(changes={"single_use": "oversized-decision"})
        receipts = self.receipts()
        before = _path(self.project, "row-size").read_bytes()
        with self.assertRaises(PodError) as caught:
            self.operation(objective="row-size", decision={**DECISION, "instruction": "\x01" * 1024,
                           "channel": "relayed", "relay": {"reference": "large-decision"}})
        self.assertEqual(caught.exception.code, "invalid_standing_authorization")
        self.assertEqual(caught.exception.detail["limit_bytes"], standing.MAX_DECISION_BYTES)
        self.assertEqual(_path(self.project, "row-size").read_bytes(), before)
        self.assertEqual(self.receipts(), receipts)

    def test_relay_activation_replay_after_session_revoke_cannot_restore_yolo(self):
        fields = {"channel": "relayed", "relay": {"reference": "standing-one"}}
        self.activate(**fields)
        self.operation("revoke")
        receipts = self.receipts()
        self.refusal("relay_authorization_reused", self.activate, **fields)
        self.assertEqual(self.receipts(), receipts)
        held = self.case.op("governor", action=self.proposed())
        self.assertEqual(held["decision"], "DEFER")
        self.assertIn("authorization_missing", [row["code"] for row in held["reasons"]])

    def test_relay_revoke_replay_cannot_end_a_new_session_activation(self):
        self.activate()
        decision = {**DECISION, "channel": "relayed", "relay": {"reference": "stop-one"}}
        self.operation("revoke", decision=decision)
        receipts = self.receipts()
        # No-op verification observes the original time; active key reuse still refuses.
        self.assertEqual(self.operation("revoke", decision=decision), {"state": "revoked", "changed": False})
        self.activate()
        self.refusal("relay_authorization_reused", self.operation, "revoke", decision=decision)
        self.assertEqual(self.receipts(), receipts)
        self.assertEqual(self.operation("show")["standing_authorization"]["state"], "active")

    def test_interrupted_relay_activation_receipt_can_complete_exactly_one_row(self):
        fields = {"channel": "relayed", "relay": {"reference": "standing-one"}}
        before = self.saved()
        with patch("pod.ledger._write", side_effect=OSError("interrupted context replacement")):
            with self.assertRaises(OSError):
                self.activate(**fields)
        self.assertEqual(self.saved(), before)
        receipts = self.receipts()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(self.activate(**fields)["decision"]["id"], "Y1")
        self.refusal("relay_authorization_reused", self.activate, **fields)
        self.assertEqual(self.receipts(), receipts)

    def test_interrupted_relay_revoke_receipt_can_complete_exactly_one_row(self):
        self.activate()
        decision = {**DECISION, "channel": "relayed", "relay": {"reference": "stop-one"}}
        before = self.saved()
        with patch("pod.ledger._write", side_effect=OSError("interrupted revocation replacement")):
            with self.assertRaises(OSError):
                self.operation("revoke", decision=decision)
        self.assertEqual(self.saved(), before)
        receipts = self.receipts()
        result = self.operation("revoke", decision=decision)
        self.assertEqual((result["decision"]["id"], result["standing_authorization"]["state"]), ("Y2", "revoked"))
        self.assertEqual(self.operation("revoke", decision=decision), {"state": "revoked", "changed": False})
        self.assertEqual(self.receipts(), receipts)

    def test_reopen_keeps_yolo_ended_until_a_new_activation_after_reopen(self):
        self.activate()
        self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        reopened = self.case.write([self.case.criterion()], reopen=True, revision_authority=DECISION)
        self.assertEqual(self.operation("show")["standing_authorization"]["state"], "ended")
        held = self.case.op("governor", action=self.proposed())
        self.assertEqual(held["decision"], "DEFER")
        self.assertIn("authorization_missing", [row["code"] for row in held["reasons"]])
        activated = self.activate()
        self.assertEqual(activated["decision"]["seq"], reopened["checkpoint"]["seq"])
        self.assertEqual(activated["standing_authorization"]["state"], "active")
        self.assertEqual(self.case.op("governor", action=self.proposed())["decision"], "ALLOW")

    def test_checkpoint_with_unreadable_governor_journal_acknowledges_committed_write(self):
        before = read(self.project, "objective")["checkpoint"]["seq"]
        _record_path(self.project, "objective").write_text(json.dumps({"schema": "pod-governor/v0", "actions": []}))
        result = self.case.write(self.case.stored(), next_safe_action="continue after checkpoint")
        after = read(self.project, "objective")["checkpoint"]
        self.assertEqual(after["seq"], before + 1)
        self.assertEqual(after["next_safe_action"], "continue after checkpoint")
        self.assertEqual(result["standing_authorization"]["state"], "none")

    def test_active_standing_checkpoint_with_unreadable_journal_keeps_mode_and_acknowledgement(self):
        self.activate()
        _record_path(self.project, "objective").write_text(json.dumps({"schema": "pod-governor/v0", "actions": []}))
        result = self.case.write(self.case.stored())
        self.assertEqual(result["standing_authorization"]["state"], "active")
        self.assertEqual(self.operation("show")["standing_authorization"], result["standing_authorization"])

    def test_verified_relay_deploy_widening_covers_exact_candidate_then_revoke_holds(self):
        fields = {"channel": "relayed", "relay": {"reference": "standing-deploy"}, "widening": ["deploy"]}
        activated = self.activate(**fields)
        verdict = activated["decision"]["relay_verification"]
        self.assertEqual(verdict["standing"]["widening"], ["deploy"])
        self.assertIn("deploy", verdict["standing"]["scope"])
        allowed = self.case.op("governor", action=self.proposed("deploy"))
        self.assertEqual(allowed["decision"], "ALLOW")
        consent = self.journal()["actions"][-1]["action"]["authorization"]
        self.assertEqual((consent["candidate"], consent["tree"], consent["target"]),
                         (self.case.observed["commit"], self.case.observed["tree"], "origin/target"))
        self.operation("revoke")
        receipts = self.receipts()
        self.refusal("relay_authorization_reused", self.activate, **fields)
        self.assertEqual(self.receipts(), receipts)
        held = self.case.op("governor", action=self.proposed("deploy"))
        self.assertEqual(held["decision"], "DEFER")
        self.assertIn("authorization_missing", [row["code"] for row in held["reasons"]])

    def test_relay_deploy_widening_missing_or_mismatched_verifier_never_grants(self):
        fields = {"channel": "relayed", "relay": {"reference": "standing-deploy"}, "widening": ["deploy"]}
        self.configure(None)
        self.refusal("relay_verifier_unconfigured", self.activate, **fields)
        self.configure(self.command)
        self.control(changes={"standing.widening": []})
        self.refusal("relay_verifier_mismatch", self.activate, **fields)
        self.control(changes={"verified": False})
        self.refusal("relay_verifier_denied", self.activate, **fields)
        self.assertEqual(self.receipts(), {})
        held = self.case.op("governor", action=self.proposed("deploy"))
        self.assertEqual(held["decision"], "DEFER")

    def test_expiry_and_criteria_change_suspend_until_owner_reconfirmation(self):
        self.activate(expires_at="2099-01-01T00:00:00Z")
        with patch("pod.standing._clock", return_value=datetime(2100, 1, 1, tzinfo=timezone.utc)):
            self.assertEqual(self.operation("show")["standing_authorization"]["state"], "expired")
            self.assertEqual(self.operation("revoke")["standing_authorization"]["state"], "revoked")
        self.activate()
        new = {"id": "O2", "kind": "criterion", "provenance": "user_direct",
               "source": {"instruction": "Add PoD#2", "ref": "PoD#2"},
               "check": "PoD#2 passes", "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        self.case.write([*self.case.stored(), new], criteria=["PoD#1", "PoD#2"], revision_authority=DECISION)
        self.assertEqual(self.operation("show")["standing_authorization"]["state"], "suspended")
        self.assertEqual(self.activate()["standing_authorization"]["state"], "active")

    def test_activation_at_intake_and_mid_objective_satisfies_next_check(self):
        self.case.write([self.case.criterion()], objective="at-start", governance={"base_ref": "target"})
        initial = read(self.project, "at-start")
        started = self.operation(objective="at-start", decision={**DECISION, "instruction": "/pod objective yolo"})
        self.assertEqual(started["decision"]["seq"], initial["checkpoint"]["seq"])
        self.assertEqual(read(self.project, "at-start")["admissions"], {})
        before = read(self.project, "objective")
        deferred = self.case.op("governor", action=self.proposed())
        self.assertIn("authorization_missing", [row["code"] for row in deferred["reasons"]])
        result = self.activate()
        row = result["decision"]
        self.assertEqual(row["scope"], list(standing.DEFAULT_SCOPE))
        self.assertEqual(row["exclusions"], list(standing.STANDING_EXCLUSIONS))
        self.assertEqual((row["id"], row["operation"], row["channel"], row["expires_at"]), ("Y1", "activate", "session", None))
        after = read(self.project, "objective")
        self.assertEqual(after["decision_channels"], 3)
        self.assertEqual(after["checkpoint"], before["checkpoint"])
        self.assertEqual(after["admissions"], before["admissions"])
        self.assertEqual(row["binding"]["scope_digest"], standing.scope_digest(after))
        allowed = self.case.op("governor", action=self.proposed())
        self.assertEqual(allowed["decision"], "ALLOW", allowed)

    def test_relay_activation_confirms_scope_operation_expiry_and_consumes_key(self):
        result = self.activate(channel="relayed", relay={"reference": "standing-one", "owner_source": "caller assertion"},
                               expires_at="2099-01-01T03:00:00+03:00")
        row = result["decision"]
        sent = json.loads(self.requests.read_text().splitlines()[0])
        self.assertEqual(sent, {"schema": relay.SCHEMA, "reference": "standing-one", "action": "standing",
            "objective": "objective", "candidate": None, "pr": None,
            "target": {"repository": "acme/widgets", "remote": "origin", "base": "target"},
            "standing": {"operation": "activate", "scope": list(standing.DEFAULT_SCOPE), "widening": [], "expires_at": "2099-01-01T00:00:00Z",
                         "scope_digest": row["binding"]["scope_digest"]}})
        self.assertEqual(row["relay"]["owner_source"], "authenticated-owner:fixture")
        self.assertEqual(row["relay_verification"]["standing"], sent["standing"])
        self.assertTrue(row["relay_verification"]["verified"])
        from pod.ledger import state_root
        self.assertEqual(len(list((state_root(self.project) / ".relay-authorizations").glob("*.json"))), 1)

    def test_malformed_bounded_decisions_refuse_without_writes(self):
        invalid = ({"scope": []}, {"scope": ["publish", "publish"]}, {"scope": ["unknown"]},
                   {"scope": ["deploy"]}, {"widening": ["secrets", "secrets"]}, {"widening": ["unknown"]},
                   {"expires_at": "2000-01-01T00:00:00Z"}, {"expires_at": "2099-01-01T00:00:00"},
                   {"instruction": "x" * 1025}, {"provenance": "worker"}, {"utc": "2099-01-01T00:00:00Z"})
        for fields in invalid:
            with self.subTest(fields=fields):
                exc = self.refusal("invalid_standing_authorization", self.activate, **fields)
                self.assertIn(exc.detail["record"], ("standing authorization", "standing decision"))
                self.assertTrue(exc.detail["bounded_fields"])
        self.assertFalse(self.requests.exists())

    def test_revoke_is_immediate_and_revoke_when_off_is_a_noop(self):
        self.assertEqual(self.operation("revoke"), {"state": "none", "changed": False})
        self.assertEqual(read(self.project, "objective")["standing_authorization"]["decisions"], [])
        self.activate()
        revoked = self.operation("revoke", decision={**DECISION, "instruction": "stop yolo"})
        self.assertEqual(revoked["standing_authorization"]["state"], "revoked")
        before = read(self.project, "objective")["standing_authorization"]
        self.assertEqual(self.operation("revoke"), {"state": "revoked", "changed": False})
        after = read(self.project, "objective")["standing_authorization"]
        self.assertEqual(before["decisions"], after["decisions"])
        self.assertGreater(after["not_before"], before["not_before"])
        held = self.case.op("governor", action=self.proposed())
        self.assertEqual(held["decision"], "DEFER")
        self.assertIn("authorization_missing", [row["code"] for row in held["reasons"]])

    def test_verified_relay_revoke_and_replacement_preserve_history(self):
        self.activate()
        self.activate(scope=["merge"])
        self.assertEqual(self.operation("show")["standing_authorization"]["decision"]["scope"], ["merge"])
        revoked = self.operation("revoke", decision={**DECISION, "channel": "relayed", "relay": {"reference": "stop-one"}})
        self.assertEqual(revoked["decision"]["relay_verification"]["standing"],
                         {"operation": "revoke", "scope": None, "widening": None, "expires_at": None})
        self.assertEqual(revoked["standing_authorization"]["history"]["total"], 3)

    def test_private_contract_show_and_native_ownership(self):
        contract = run("contract", {"record": "standing authorization"})["records"]["standing authorization"]
        self.assertEqual(contract["fields"]["action"]["allowed"], ["activate", "revoke", "answer", "show"])
        for name, expected in (("standing decision", standing.MAX_DECISION_BYTES), ("responder answer", standing.MAX_ANSWER_BYTES)):
            self.assertEqual(run("contract", {"record": name})["records"][name]["stored_max_bytes"], expected)
        before = self.saved()
        self.assertEqual(self.operation("show")["standing_authorization"]["state"], "none")
        self.assertEqual(before, self.saved())
        self.refusal("coordinator_conflict", run, "standing-authorization",
                     {"project": str(self.project), "objective": "objective", "owner": "foreign", "action": "activate", "decision": DECISION})
        with patch("pod.ledger.require_authority", side_effect=PodError("native_authority_unverified", "fixture refusal")):
            self.refusal("native_authority_unverified", self.activate)


class StandingGovernorTests(StandingCase):
    def test_all_remote_action_kinds_receive_exact_standing_evidence(self):
        self.activate()
        self.case.op("governor-execute", action=self.proposed())
        for kind in ("push", "pr_update", "workflow_dispatch", "validation_rerun", "remote_diagnostic", "cancel_validation", "merge", "release"):
            with self.subTest(kind=kind):
                result = self.case.op("governor", action=self.proposed(kind, **({"pr": 23} if kind == "merge" else {})))
                self.assertIn(result["decision"], ("ALLOW", "REUSE"), result)
                consent = self.journal()["actions"][-1]["action"]["authorization"]
                self.assertEqual(consent["scope"], ["merge" if kind == "merge" else "release" if kind == "release" else "publish"])
                self.assertEqual((consent["candidate"], consent["tree"], consent["target"]),
                                 (self.case.observed["commit"], self.case.observed["tree"], "origin/target"))
                self.assertEqual(consent["standing"], {"id": "Y1", "reference": "standing_authorization/decisions/0", "channel": "session"})
                self.assertEqual(consent.get("pr"), 23 if kind == "merge" else None)
                self.assertTrue({"provenance", "instruction", "channel", "relay"}.isdisjoint(consent))
        self.assertEqual(self.journal()["decision_channels"], 3)
        inventory = context_decisions(read(self.project, "objective"), self.journal())
        self.assertEqual([row["kind"] for row in inventory], ["standing_authorization"])
        self.assertEqual([call[0] for call in self.case.remote.calls], ["push"])

    def test_managed_push_and_pr_execute_and_delivery_counts_recorded(self):
        self.activate()
        for kind in ("push", "pr_update"):
            result = self.case.op("governor-execute", action=self.proposed(kind))
            self.assertEqual((result["decision"], result["outcome"]), ("ALLOW", "PASS"))
        self.assertEqual(sum(call[0] == "push" for call in self.case.remote.calls), 2)
        self.assertEqual(self.case.op("governor-status")["units"]["delivery"]["authorization"]["publish"], "RECORDED")
        self.assertFalse(self.requests.exists())

    def test_managed_dispatch_and_diagnostic_perform_without_consent_wait(self):
        self.activate()
        self.case.remote.stamp = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.case.op("governor-execute", action=self.proposed())
        for kind in ("workflow_dispatch", "remote_diagnostic"):
            result = self.case.op("governor-execute", action=self.proposed(kind))
            self.assertEqual((result["decision"], result["outcome"]), ("ALLOW", "pending"), result)
        self.assertEqual(sum(call[0] == "dispatch" for call in self.case.remote.calls), 2)

    def test_managed_failed_job_rerun_keeps_classification_and_attempt_binding(self):
        self.activate()
        self.case.remote.stamp = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.case.op("governor-execute", action=self.proposed())
        dispatched = self.case.op("governor-execute", action=self.proposed("workflow_dispatch"))
        run_id = dispatched["receipt"]["provider"]["run_id"]
        self.case.remote.complete(run_id, "failure")
        self.case.op("governor-reconcile", record_id=dispatched["record_id"])
        self.case.op("governor-classify", record_id=dispatched["record_id"],
                     classification={"class": "transient", "reason": "runner disconnected"})
        rerun = self.case.op("governor-execute", action=self.proposed("validation_rerun"))
        self.assertEqual((rerun["decision"], rerun["outcome"]), ("ALLOW", "pending"), rerun)
        self.assertIn(("rerun", run_id, True), self.case.remote.calls)
        self.assertTrue(rerun["receipt"]["provider"]["accepted"])

    def test_managed_cancellation_only_reaches_older_validation_in_its_unit(self):
        self.activate()
        self.case.remote.stamp = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.case.op("governor-execute", action=self.proposed())
        dispatched = self.case.op("governor-execute", action=self.proposed("workflow_dispatch"))
        (self.project / "README.md").write_text("next candidate\n")
        git(self.project, "add", "README.md"); git(self.project, "commit", "-qm", "next candidate")
        self.case.candidate = git(self.project, "rev-parse", "HEAD")
        self.case.write(self.case.stored())
        self.case.observed = observe_candidate(self.project, base_ref="refs/remotes/origin/target")
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=self.case.observed)
        cancel = self.proposed("cancel_validation")
        cancel["target"] = dispatched["record_id"]
        result = self.case.op("governor-execute", action=cancel)
        self.assertEqual((result["decision"], result["outcome"]), ("ALLOW", "PASS"), result)
        self.assertIn(("cancel", dispatched["receipt"]["provider"]["run_id"]), self.case.remote.calls)

    def test_explicit_exact_consent_takes_precedence_and_bad_consent_never_falls_back(self):
        self.activate()
        result = self.case.op("governor", action=self.proposed(authorization=self.case.consent("publish")))
        self.assertEqual(result["decision"], "ALLOW")
        self.assertNotIn("standing", self.journal()["actions"][-1]["action"]["authorization"])
        wrong = {**self.case.consent("merge"), "candidate": "f" * 40}
        held = self.case.op("governor", action=self.proposed("merge", pr=23, authorization=wrong))
        self.assertEqual(held["decision"], "DEFER")
        self.assertIn("authorization_missing", [row["code"] for row in held["reasons"]])

    def test_excluded_scopes_merge_without_pr_and_unprepared_targets_still_ask(self):
        self.activate(scope=["publish", "merge", "cleanup"])
        for kind in ("deploy", "release", "merge"):
            with self.subTest(kind=kind):
                held = self.case.op("governor", action=self.proposed(kind))
                self.assertEqual(held["decision"], "DEFER")
                reasons = [row for row in held["reasons"] if row["code"] == "authorization_missing"]
                self.assertTrue(reasons)
                self.assertIn("yolo", reasons[0]["detail"])
        for binding, unit in ((None, None), (self.case.observed, {})):
            self.assertIsNone(standing.synthesize(read(self.project, "objective"), binding, unit, {"publish"})[0])

    def test_explicit_deploy_widening_covers_all_needed_effect_scopes(self):
        result = self.activate(widening=["deploy"])
        decision = result["standing_authorization"]["decision"]
        self.assertIn("deploy", decision["scope"])
        self.assertNotIn("deploy", decision["exclusions"])
        self.assertEqual(decision["widening"], ["deploy"])
        allowed = self.case.op("governor", action=self.proposed("deploy"))
        self.assertEqual(allowed["decision"], "ALLOW", allowed)
        combined = self.case.op("governor", action=self.proposed(effects=["deploy:production", "release:next"]))
        self.assertEqual(combined["decision"], "ALLOW", combined)
        self.assertEqual(self.journal()["actions"][-1]["action"]["authorization"]["scope"], ["deploy", "publish", "release"])
        self.activate(scope=["publish"])
        held = self.case.op("governor", action=self.proposed(effects=["release:another"]))
        self.assertEqual(held["decision"], "DEFER")

    def test_revocation_between_admission_and_effect_fails_row_without_effect(self):
        self.activate()
        def revoked(_project):
            self.operation("revoke")
            return self.case.remote
        with patch("pod.governor_effects._port", side_effect=revoked), self.assertRaises(PodError) as caught:
            self.case.op("governor-execute", action=self.proposed())
        self.assertEqual(caught.exception.code, "standing_authorization_inactive")
        row = self.journal()["actions"][-1]
        self.assertEqual((row["outcome"], row["receipt"]["detail"]), ("FAILED", "standing_authorization_inactive"))
        self.assertEqual(self.case.remote.calls, [])

    def test_candidate_movement_between_admission_and_effect_fails_row_without_effect(self):
        self.activate()
        def moved(_project):
            (self.project / "README.md").write_text("changed at standing boundary\n")
            git(self.project, "add", "README.md")
            git(self.project, "commit", "-qm", "boundary change")
            observed = observe_candidate(self.project, base_ref="refs/remotes/origin/target")
            prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=observed)
            return self.case.remote
        with patch("pod.governor_effects._port", side_effect=moved), self.assertRaises(PodError) as caught:
            self.case.op("governor-execute", action=self.proposed())
        self.assertEqual(caught.exception.code, "standing_authorization_inactive")
        self.assertEqual(self.journal()["actions"][-1]["outcome"], "FAILED")
        self.assertEqual(self.case.remote.calls, [])

    def test_target_candidate_and_missing_audit_gates_stay(self):
        self.activate()
        moved = self.proposed()
        moved["candidate"] = "f" * 40
        held = self.case.op("governor", action=moved)
        self.assertIn("superseded_candidate", [row["code"] for row in held["reasons"]])
        wrong_target = self.proposed("merge", pr=23)
        wrong_target["target"] = "origin/foreign"
        held = self.case.op("governor", action=wrong_target)
        self.assertIn("merge_target_mismatch", [row["code"] for row in held["reasons"]])
        from tests.kernel_support import governance_policy
        audit = governance_policy(self.case, "audit", unit="delivery", scope={"paths": ["src"]})
        self.case.write(self.case.stored() + [audit])
        held = self.case.op("governor", action=self.proposed("merge", pr=23))
        self.assertIn("assurance_unbound", [row["code"] for row in held["reasons"]])

    def test_red_hosted_ci_stays_a_failure_and_classification_gate(self):
        self.activate()
        self.case.remote.stamp = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.case.op("governor-execute", action=self.proposed())
        dispatched = self.case.op("governor-execute", action=self.proposed("workflow_dispatch"))
        self.assertEqual(dispatched["decision"], "ALLOW", dispatched)
        self.case.remote.complete(dispatched["receipt"]["provider"]["run_id"], "failure")
        failed = self.case.op("governor-reconcile", record_id=dispatched["record_id"])
        self.assertEqual(failed["status"], "FAILED")
        again = self.case.op("governor", action=self.proposed("workflow_dispatch"))
        self.assertEqual(again["decision"], "DEFER")
        self.assertIn("failure_unclassified", [row["code"] for row in again["reasons"]])

    def test_governance_drift_still_refuses_before_a_journal_write(self):
        self.activate()
        git(self.project, "checkout", "-q", "target")
        (self.project / "AGENTS.md").write_text("# Changed policy\nDifferent review rules\n")
        git(self.project, "add", "AGENTS.md")
        git(self.project, "commit", "-qm", "policy changed")
        git(self.project, "update-ref", "refs/remotes/origin/target", git(self.project, "rev-parse", "HEAD"))
        git(self.project, "checkout", "-q", "candidate")
        self.refusal("governance_changed", self.case.op, "governor", action=self.proposed())

    def test_replacement_fails_mutation_recheck(self):
        self.activate()
        def replaced(_project):
            self.activate()
            return self.case.remote
        with patch("pod.governor_effects._port", side_effect=replaced), self.assertRaises(PodError) as caught:
            self.case.op("governor-execute", action=self.proposed())
        self.assertEqual(caught.exception.code, "standing_authorization_inactive")
        self.assertEqual(self.case.remote.calls, [])

    def test_dirty_prepared_binding_fails_mutation_recheck_without_remote_effect(self):
        self.activate()
        def dirtied(_project):
            (self.project / "README.md").write_text("dirty at effect boundary\n")
            observed = observe_candidate(self.project, base_ref="refs/remotes/origin/target")
            self.assertTrue(observed["dirty_paths"])
            prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=observed)
            binding = self.journal()["units"]["delivery"]["candidate"]
            self.assertEqual(binding["commit"], self.case.observed["commit"])
            self.assertEqual(binding["tree"], self.case.observed["tree"])
            self.assertTrue(binding["dirty_paths"])
            return self.case.remote
        with patch("pod.governor_effects._port", side_effect=dirtied), self.assertRaises(PodError) as caught:
            self.case.op("governor-execute", action=self.proposed())
        self.assertEqual(caught.exception.code, "standing_authorization_inactive")
        row = self.journal()["actions"][-1]
        self.assertEqual((row["outcome"], row["receipt"]["detail"]), ("FAILED", "standing_authorization_inactive"))
        self.assertEqual(self.case.remote.calls, [])


class StandingApplicableConsentTests(StandingCase):
    def test_n5_scope_suspension_and_authority_wait_share_one_checkpoint(self):
        self.reserved()
        added = {"id": "O2", "kind": "criterion", "provenance": "user_direct",
                 "source": {"instruction": "Add PoD#2", "ref": "PoD#2"}, "check": "PoD#2 passes",
                 "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        wait = {**self.case.sub("S1"), "wait": {"class": "authority", "referent": {
            "kind": "authorization", "scope": "merge", "candidate": self.case.observed["commit"]}}}
        result = self.case.write([*self.case.stored(), added, wait], criteria=["PoD#1", "PoD#2"],
                                 revision_authority=DECISION)
        self.assertEqual(result["standing_authorization"]["state"], "suspended")
        self.assertEqual(result["checkpoint"]["obligations"][-1]["wait"], wait["wait"])

    def reserved(self):
        self.activate(expires_at="2099-01-01T00:00:00Z")
        self.case.op("governor", action=self.proposed())
        self.case.op("governor", action=self.proposed("merge", pr=23))

    def assert_consent(self, recorded):
        expected = "RECORDED" if recorded else "MISSING"
        scopes = self.case.op("governor-status")["units"]["delivery"]["authorization"]
        self.assertEqual(scopes, {"publish": expected, "merge": expected})
        from pod.governor import objective_delivery_reporting
        self.assertEqual(objective_delivery_reporting(self.project, "objective")["units"]["delivery"], scopes)
        for scope in ("publish", "merge"):
            self.assertEqual(_authorization_granted(self.project, "objective")(scope, self.case.observed["commit"]), recorded)
        rows = [*self.case.stored(), *[
            {**self.case.sub(f"S{index}", state="waiting"), "wait": {"class": "authority", "referent": {
                "kind": "authorization", "scope": scope, "candidate": self.case.observed["commit"]}}}
            for index, scope in enumerate(("publish", "merge"), 1)]]
        criteria = read(self.project, "objective")["checkpoint"]["criteria"]
        if recorded:
            self.refusal("wait_invalid", self.case.write, rows, criteria=criteria)
        else:
            self.case.write(rows, criteria=criteria)

    def test_still_active_standing_rows_remain_recorded_and_resolve_authority_wait(self):
        self.reserved()
        self.assert_consent(True)

    def test_revoked_standing_rows_are_missing_and_accept_authority_wait(self):
        self.reserved()
        self.operation("revoke")
        self.assert_consent(False)

    def test_suspended_standing_rows_are_missing_and_accept_authority_wait(self):
        self.reserved()
        added = {"id": "O2", "kind": "criterion", "provenance": "user_direct",
                 "source": {"instruction": "Add PoD#2", "ref": "PoD#2"}, "check": "PoD#2 passes",
                 "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        self.case.write([*self.case.stored(), added], criteria=["PoD#1", "PoD#2"], revision_authority=DECISION)
        self.assertEqual(self.operation("show")["standing_authorization"]["state"], "suspended")
        self.assert_consent(False)

    def test_expired_standing_rows_are_missing_and_accept_authority_wait(self):
        self.reserved()
        with patch("pod.standing._clock", return_value=datetime(2100, 1, 1, tzinfo=timezone.utc)):
            self.assert_consent(False)

    def test_replaced_or_narrowed_decision_cannot_reuse_earlier_standing_rows(self):
        self.reserved()
        self.activate(scope=["publish"])
        self.assert_consent(False)

    def test_same_decision_must_still_cover_the_recorded_scopes(self):
        from pod.governor import applicable_authorization
        self.reserved()
        context, journal = read(self.project, "objective"), self.journal()
        context["standing_authorization"]["decisions"][-1]["scope"] = ["publish"]
        unit = journal["units"]["delivery"]
        for row in journal["actions"]:
            consent = row["action"].get("authorization")
            if not consent:
                continue
            scope = consent["scope"][0]
            self.assertEqual(applicable_authorization(unit, consent, candidate=consent["candidate"],
                             tree=consent["tree"], scope=scope, context=context), scope == "publish")


class StandingResponderTests(StandingCase):
    def test_e6_unrecorded_question_precedes_preflight_and_target_checks(self):
        self.activate()
        for text in ("answer", "x" * 4096):
            with self.subTest(text_length=len(text)), patch("pod.standing._target", side_effect=AssertionError("target checked")), \
                    patch("pod.relay.verify", side_effect=AssertionError("verifier called")):
                receipts = self.receipts()
                self.refusal("decision_question_unrecorded", self.operation, "answer", answer={**ANSWER, "text": text})
                self.assertEqual(self.receipts(), receipts)

    def test_n3_inactive_answer_with_prior_history_refuses_before_verifier(self):
        self.activate()
        self.question()
        self.operation("answer", answer=ANSWER)
        self.operation("revoke")
        calls, receipts = self.requests.read_bytes(), self.receipts()
        self.configure(None)
        self.refusal("responder_answer_requires_yolo", self.operation, "answer", answer=ANSWER)
        self.configure(self.command)
        with patch("pod.relay.verify", side_effect=AssertionError("inactive answer called verifier")):
            self.refusal("responder_answer_requires_yolo", self.operation, "answer", answer=ANSWER)
        self.assertEqual(self.requests.read_bytes(), calls)
        self.assertEqual(self.receipts(), receipts)

    def test_n6_impossible_answer_refuses_before_verification(self):
        self.activate()
        self.question()
        receipts = self.receipts()
        for text in ("x" * 4096, "\x01" * 1000, "\U0001f600" * 1100):
            with self.subTest(text_bytes=len(text.encode())):
                self.refusal("invalid_standing_authorization", self.operation, "answer", answer={**ANSWER, "text": text})
        self.assertFalse(self.requests.exists())
        self.assertEqual(self.receipts(), receipts)
        self.operation("answer", answer={**ANSWER, "text": "x" * 3500})
        self.assertEqual(len(self.requests.read_text().splitlines()), 1)

    def test_encoded_answer_cap_refuses_before_any_receipt_is_consumed(self):
        self.case.write([self.case.criterion()], objective="answer-size", governance={"base_ref": "target"}, questions=["Q1"])
        self.operation(objective="answer-size")
        self.control(changes={"single_use": "oversized-answer"})
        receipts = self.receipts()
        before = _path(self.project, "answer-size").read_bytes()
        with self.assertRaises(PodError) as caught:
            self.operation("answer", objective="answer-size", answer={**ANSWER, "text": "\x01" * 2048})
        self.assertEqual(caught.exception.code, "invalid_standing_authorization")
        self.assertEqual(caught.exception.detail["limit_bytes"], standing.MAX_ANSWER_BYTES)
        self.assertEqual(_path(self.project, "answer-size").read_bytes(), before)
        self.assertEqual(self.receipts(), receipts)

    def test_answer_replay_records_nothing_and_preserves_receipts(self):
        self.activate()
        self.question()
        self.operation("answer", answer=ANSWER)
        receipts = self.receipts()
        self.refusal("relay_authorization_reused", self.operation, "answer", answer=ANSWER)
        self.assertEqual(self.receipts(), receipts)
        self.operation("revoke")
        self.refusal("responder_answer_requires_yolo", self.operation, "answer", answer=ANSWER)
        self.activate()
        self.refusal("relay_authorization_reused", self.operation, "answer", answer=ANSWER)
        self.assertEqual(self.receipts(), receipts)

    def test_interrupted_relay_answer_receipt_can_complete_exactly_one_row(self):
        self.activate()
        self.question()
        before = self.saved()
        with patch("pod.ledger._write", side_effect=OSError("interrupted answer replacement")):
            with self.assertRaises(OSError):
                self.operation("answer", answer=ANSWER)
        self.assertEqual(self.saved(), before)
        receipts = self.receipts()
        self.assertEqual(self.operation("answer", answer=ANSWER)["answer"]["id"], "A1")
        self.refusal("relay_authorization_reused", self.operation, "answer", answer=ANSWER)
        self.assertEqual(self.receipts(), receipts)

    def question(self):
        self.case.write(self.case.stored(), questions=[{"id": "Q1", "reference": "question-one", "text": "Which endpoint?"}])

    def test_verified_active_answer_is_responder_observation_and_never_authority(self):
        self.activate(scope=["publish"])
        self.question()
        self.control(changes={"single_use": "answer-one"})
        answered = self.operation("answer", answer=ANSWER)
        row = answered["answer"]
        self.assertEqual((row["id"], row["decided_by"], row["under"]), ("A1", "responder", "Y1"))
        self.assertEqual(row["responder_source"], "authenticated-responder:fixture")
        self.assertEqual(row["relay_verification"]["answer"], {"question": "Q1", "text": ANSWER["text"]})
        self.assertNotIn("owner_source", row["relay_verification"])
        owners = context_decisions(read(self.project, "objective"), self.journal())
        self.assertEqual([item["kind"] for item in owners], ["standing_authorization"])
        held = self.case.op("governor", action=self.proposed("merge", pr=23))
        self.assertEqual(held["decision"], "DEFER")
        self.assertEqual(read(self.project, "objective")["checkpoint"]["questions"][0]["text"], "Which endpoint?")

    def test_without_active_yolo_answers_keep_owner_route_and_unrecorded_questions_refuse(self):
        self.question()
        self.refusal("responder_answer_requires_yolo", self.operation, "answer", answer=ANSWER)
        self.activate()
        self.refusal("decision_question_unrecorded", self.operation, "answer", answer={**ANSWER, "question": "absent"})
        self.operation("revoke")
        self.refusal("responder_answer_requires_yolo", self.operation, "answer", answer=ANSWER)
        self.assertFalse(self.requests.exists())

    def test_answer_cannot_carry_owner_decisions_or_verifier_proof(self):
        self.activate()
        self.question()
        for extra in ({"scope": ["deploy"]}, {"provenance": "user_direct"}, {"operation": "activate"},
                      {"relay_verification": {"verified": True}}):
            with self.subTest(extra=extra):
                self.refusal("invalid_standing_authorization", self.operation, "answer", answer={**ANSWER, **extra})


class StandingRefusalTests(StandingCase):
    def test_repository_verifier_mismatch_names_the_exact_target_field(self):
        self.control(changes={"target.repository": "other/widgets"})
        exc = self.refusal("relay_verifier_mismatch", self.activate, channel="relayed", relay={"reference": "standing-one"})
        self.assertEqual(exc.detail["fields"], ["target.repository"])
        self.control()
        git(self.project, "remote", "set-url", "origin", "not-a-github-remote")
        exc = self.refusal("relay_verifier_mismatch", self.activate, channel="relayed", relay={"reference": "standing-one"})
        self.assertEqual(exc.detail["fields"], ["target.repository"])

    def test_unconfigured_denied_and_mismatched_relay_activation_write_nothing(self):
        self.configure(None)
        fields = {"channel": "relayed", "relay": {"reference": "standing-one"}}
        self.refusal("relay_verifier_unconfigured", self.activate, **fields)
        self.configure(self.command)
        self.control(changes={"verified": False})
        self.refusal("relay_verifier_denied", self.activate, **fields)
        for changed in ({"objective": "other"}, {"standing.operation": "revoke", "standing.scope": None,
                       "standing.widening": None, "standing.expires_at": None}, {"standing.scope": ["merge"]},
                       {"standing.widening": ["deploy"]}, {"standing.expires_at": "2099-01-01T00:00:00Z"}):
            with self.subTest(changed=changed):
                self.control(changes=changed)
                self.refusal("relay_verifier_mismatch", self.activate, **fields)

    def test_reused_key_and_mismatched_question_or_answer_write_nothing(self):
        self.activate(channel="relayed", relay={"reference": "standing-one"})
        self.refusal("relay_authorization_reused", self.operation, "revoke",
                     decision={**DECISION, "channel": "relayed", "relay": {"reference": "stop-one"}})
        self.case.write(self.case.stored(), questions=["Q1"])
        for changed in ({"answer.question": "Q2"}, {"answer.text": "A different answer"}):
            self.control(changes={"single_use": "answer-one", **changed})
            self.refusal("relay_verifier_mismatch", self.operation, "answer", answer=ANSWER)

    def test_caller_cannot_supply_standing_or_verification_and_constraints_still_refuse(self):
        self.activate()
        for extra in ({"standing": {"id": "Y1"}}, {"relay_verification": {"verified": True}}):
            with self.subTest(extra=extra):
                self.refusal("invalid_authorization", self.case.op, "governor",
                             action=self.proposed(authorization={**self.case.consent("publish"), **extra}))
                self.refusal("invalid_standing_authorization", self.activate, **extra)
        self.refusal("relayed_decision_refused", self.case.op, "constraint", action="add", value={
            "id": "route", "kind": "only_agents", "value": ["codex"],
            "provenance": "user_direct", "instruction": "change route", "channel": "relayed",
            "relay": {"reference": "relay-one", "owner_source": "owner"}})


class StandingVisibilityTests(StandingCase):
    def test_e4_no_decision_closure_status_has_stable_or_absent_at_closure(self):
        self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        self.case.write([self.case.criterion()], reopen=True, revision_authority=DECISION)
        self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        before = self.saved()
        stamps = []
        for hour in (1, 2):
            with patch("pod.standing._clock", return_value=datetime(2030, 1, 1, hour, tzinfo=timezone.utc)):
                stamps.append(self.public_status()["report"]["standing_authorization"].get("at_closure"))
        self.assertEqual(stamps[0], stamps[1])
        self.assertEqual(self.saved(), before)

    def test_run_selected_status_includes_governor_owner_authorization_rows(self):
        self.case.op("governor", action=self.proposed(authorization=self.case.consent("publish")))
        objective, selected = self.public_status(), self.public_status(by_run=True)
        self.assertEqual(selected["owner_decisions"], objective["owner_decisions"])
        rows = [row for row in selected["owner_decisions"] if row["kind"] == "authorization"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["reference"].startswith("governor/"))

    def test_delivery_checkpoint_acknowledgement_contains_the_same_mode_and_effects(self):
        self.activate()
        decided = self.case.op("governor", action=self.proposed("merge", pr=23))
        git(self.project, "checkout", "-q", "target")
        git(self.project, "merge", "--no-ff", "-qm", "merge candidate", "candidate")
        merged = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", merged)
        self.case.op("governor-outcome", record_id=decided["record_id"], outcome="PASS", provider={"merge_commit": merged})
        acknowledgement = self.case.checkpoint(delivery={"record": decided["record_id"]})
        self.assertEqual(acknowledgement["standing_authorization"], self.operation("show")["standing_authorization"])
        self.assertEqual(acknowledgement["standing_authorization"]["effects"]["items"][0]["outcome"], "PASS")

    def test_status_text_run_selection_and_closure_share_projection_without_verifier(self):
        self.activate(channel="relayed", relay={"reference": "standing-one"}, widening=["deploy"])
        self.case.write(self.case.stored(), questions=["Q1"])
        self.control(changes={"single_use": "answer-one"})
        self.operation("answer", answer=ANSWER)
        self.case.op("governor-execute", action=self.proposed())
        requests = self.requests.read_bytes()
        self.script.unlink()
        before = self.saved()
        objective, selected = self.public_status(), self.public_status(by_run=True)
        expected = self.operation("show")["standing_authorization"]
        self.assertEqual(objective["standing_authorization"], expected)
        self.assertEqual(selected["standing_authorization"], expected)
        self.assertEqual((expected["effects"]["total"], expected["responder_decisions"]["total"]), (1, 1))
        self.assertEqual(expected["decision"]["owner_source"], "authenticated-owner:fixture")
        from pod.status import render
        output = StringIO()
        with redirect_stdout(output):
            render(objective)
        self.assertIn("Mode: yolo (publish, merge, release, install, cleanup, deploy; relayed since", output.getvalue())
        self.assertEqual(before, self.saved())
        closed = self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        projection = closed["checkpoint"]["closure"]["report"]["standing_authorization"]
        self.assertEqual((projection["mode"], projection["state"]), ("normal", "ended"))
        self.assertEqual({key: projection["at_closure"][key] for key in ("mode", "decision", "active_until_closure")},
                         {"mode": "yolo", "decision": "Y1", "active_until_closure": True})
        self.assertEqual(projection["effects"], expected["effects"])
        closed_answer = projection["responder_decisions"]["items"][0]
        self.assertEqual({key: closed_answer[key] for key in expected["responder_decisions"]["items"][0]},
                         expected["responder_decisions"]["items"][0])
        self.assertEqual(closed_answer["reference"], "standing_authorization/answers/0")
        self.assertFalse(closed_answer["text_truncated"])
        self.assertEqual(self.public_status()["standing_authorization"], projection)
        self.assertEqual(self.requests.read_bytes(), requests)
        self.refusal("objective_closed", self.activate)
        from pod.ledger import closed_snapshot_context
        from pod.obligations import validate_closed_snapshot
        stored = read(self.project, "objective")
        validate_closed_snapshot(stored["checkpoint"], closed_snapshot_context(self.project, "objective", stored))

    def test_closure_keeps_yolo_history_after_the_decisions_expiry_time(self):
        self.activate(expires_at="2099-01-01T00:00:00Z")
        closed = self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        expected = closed["checkpoint"]["closure"]["report"]["standing_authorization"]
        self.assertTrue(expected["at_closure"]["active_until_closure"])
        from pod.ledger import closed_snapshot_context
        from pod.obligations import validate_closed_snapshot
        with patch("pod.standing._clock", side_effect=lambda now: now or datetime(2100, 1, 1, tzinfo=timezone.utc)):
            self.assertEqual(self.operation("show")["standing_authorization"], expected)
            stored = read(self.project, "objective")
            validate_closed_snapshot(stored["checkpoint"], closed_snapshot_context(self.project, "objective", stored))

    def test_closure_names_prior_yolo_even_when_revoked_before_closure(self):
        self.activate()
        self.operation("revoke")
        result = self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        closed = result["checkpoint"]["closure"]["report"]["standing_authorization"]["at_closure"]
        self.assertEqual({key: closed[key] for key in ("ran_in_yolo", "decision", "active_until_closure", "mode")},
                         {"ran_in_yolo": True, "decision": "Y1", "active_until_closure": False, "mode": "normal"})

    def test_projection_bounds_newest_effects_and_preserves_history(self):
        self.activate()
        self.case.op("governor", action=self.proposed())
        context, journal = read(self.project, "objective"), self.journal()
        sample = journal["actions"][0]
        journal["actions"] = [{**deepcopy(sample), "record_id": str(index)} for index in range(70)]
        before = deepcopy((context, journal))
        result = standing.projection(context, journal)
        self.assertEqual((result["effects"]["total"], result["effects"]["truncated"]), (70, True))
        self.assertEqual([row["record_id"] for row in result["effects"]["items"]], list(map(str, range(6, 70))))
        self.assertEqual((context, journal), before)
        self.assertEqual(result["history"]["total"], 1)

    def test_end_states_and_other_objectives_never_inherit_consent(self):
        self.activate()
        context = read(self.project, "objective")
        for transition in ({"continued_by": {"objective": "successor"}}, {"historical_retirement": {"at": "now"}}):
            self.assertEqual(standing.state({**context, **transition}), "ended")
        self.case.write([self.case.criterion()], objective="another", governance={"base_ref": "target"})
        self.assertEqual(self.operation("show", objective="another")["standing_authorization"]["state"], "none")
        copied = self.journal()
        copied["actions"] = []
        _write_journal(_record_path(self.project, "another"), copied)
        held = run("governor", {"project": str(self.project), "objective": "another", "owner": "owner", "action": self.proposed()})
        self.assertEqual(held["decision"], "DEFER")


from tests.pod_tick_support import TickCase


class StandingSourceTests(TickCase):
    def setUp(self):
        super().setUp()
        guard = patch("pod.orca.executable", side_effect=AssertionError("standing source test escaped native double"))
        guard.start(); self.addCleanup(guard.stop)

    def activate(self):
        return self.op("standing-authorization", action="activate", decision=DECISION)

    def summary(self):
        return self.op("standing-authorization", action="show")["standing_authorization"]

    def test_a1_p453_relay_uses_scope_after_its_own_live_observation(self):
        from pod.config import personal_path
        import yaml
        path = personal_path(self.project)
        config = yaml.safe_load(path.read_text())
        config["authorization_verifier"] = ["/bin/false"]
        path.write_text(yaml.safe_dump(config))
        self.bind(); self.activate()
        original = read(self.project, "objective")["standing_authorization"]["decisions"][-1]
        self.issue.body += "\nA new requirement."
        with patch("pod.pod_ticks.refresh"):
            self.rebind()
        context = read(self.project, "objective")
        source = context["checkpoint"]["objective_source"]
        binding = {"criteria": context["checkpoint"]["criteria"],
                   **{key: source[key] for key in ("repository", "number", "locator", "amendments")},
                   "body_scope_sha256": hashlib.sha256(self.issue.body.encode()).hexdigest()}
        expected = digest(binding)
        decided = (datetime.fromisoformat(original["utc"].replace("Z", "+00:00")) + timedelta(microseconds=1)).isoformat()

        def verify(command, request):
            self.assertEqual(request["standing"]["scope_digest"], expected)
            # Inject the saved old decision, independently of the request. The
            # ordinary verifier-process tests exercise the same mismatch there.
            return {**deepcopy(request), "verified": True, "owner_source": "authenticated-owner:fixture",
                    "decided_at": decided, "single_use": "before-body-edit",
                    "standing": {**request["standing"], "scope_digest": original["binding"]["scope_digest"]}}

        before = self.saved()
        with patch("pod.relay.verify", side_effect=verify), \
                patch("pod.standing._target", return_value={"repository": "acme/example", "remote": "origin", "base": "target"}):
            with self.assertRaises(PodError) as caught:
                self.op("standing-authorization", action="activate", decision={**DECISION, "channel": "relayed",
                                                                              "relay": {"reference": "before-body-edit"}})
        self.assertEqual(caught.exception.code, "relay_verifier_mismatch")
        self.assertEqual(caught.exception.detail["fields"], ["standing.scope_digest"])
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.summary()["state"], "suspended")

    def test_f1_current_digest_after_unobserved_rebind_is_accepted_by_own_live_read(self):
        from pod.config import personal_path
        import yaml
        self.bind(); self.activate()
        self.issue.body += "\nA new requirement."
        with patch("pod.pod_ticks.refresh"):
            self.rebind()
        confirmed = self.summary()["scope_digest"]
        path = personal_path(self.project)
        config = yaml.safe_load(path.read_text()); config["authorization_verifier"] = ["/bin/false"]
        path.write_text(yaml.safe_dump(config))
        decided = datetime.now(timezone.utc).isoformat()

        def verify(command, request):
            self.assertEqual(request["standing"]["scope_digest"], confirmed)
            return {**deepcopy(request), "verified": True, "owner_source": "authenticated-owner:fixture",
                    "decided_at": decided, "single_use": "current-unobserved-scope",
                    "standing": {**request["standing"], "scope_digest": confirmed}}

        with patch("pod.relay.verify", side_effect=verify), \
                patch("pod.standing._target", return_value={"repository": "acme/example", "remote": "origin", "base": "target"}):
            result = self.op("standing-authorization", action="activate", decision={**DECISION, "channel": "relayed",
                                                                                 "relay": {"reference": "current-unobserved-scope"}})
        self.assertEqual(result["standing_authorization"]["state"], "active")
        self.assertEqual(result["decision"]["binding"]["scope_digest"], confirmed)

    def test_r4_1_own_live_read_preserves_an_earlier_observed_scope_barrier(self):
        from pod.config import personal_path
        import yaml
        moment = StandingOrderingTests.MOMENT
        self.bind()
        with patch("pod.standing._clock", return_value=moment):
            self.activate()
        self.issue.body += "\nRequirement B."
        with patch("pod.standing._clock", return_value=moment + timedelta(seconds=2)):
            self.rebind()
        self.issue.body += "\nRequirement C."
        with patch("pod.pod_ticks.refresh"), patch("pod.standing._clock", return_value=moment + timedelta(seconds=4)):
            self.rebind()
        confirmed = self.summary()["scope_digest"]
        path = personal_path(self.project)
        config = yaml.safe_load(path.read_text()); config["authorization_verifier"] = ["/bin/false"]
        path.write_text(yaml.safe_dump(config))

        def output(command, request):
            return json.dumps({**deepcopy(request), "verified": True, "owner_source": "authenticated-owner:fixture",
                               "decided_at": (moment + timedelta(seconds=1)).isoformat(), "single_use": "before-observed-B",
                               "standing": {**request["standing"], "scope_digest": confirmed}}).encode()

        before = self.saved()
        with patch("pod.relay._output", side_effect=output), \
                patch("pod.standing._clock", return_value=moment + timedelta(seconds=10)), \
                patch("pod.standing._target", return_value={"repository": "acme/example", "remote": "origin", "base": "target"}):
            with self.assertRaises(PodError) as caught:
                self.op("standing-authorization", action="activate", decision={**DECISION, "channel": "relayed",
                        "relay": {"reference": "before-observed-B"}})
        self.assertEqual(caught.exception.code, "standing_authorization_stale")
        self.assertEqual(self.saved(), before)
        # The unread rebind at t4 is itself a transition (R5-1). Round 4 asserted t2 here,
        # which encoded the gap; that expectation is superseded.
        self.assertEqual(read(self.project, "objective")["standing_authorization"]["not_before"], "2030-01-01T00:00:04Z")

    def test_f1_restored_scope_keeps_holder_suspended_until_fresh_confirmation(self):
        from pod.config import personal_path
        import yaml
        self.bind(); self.activate()
        original_body = self.issue.body
        original = read(self.project, "objective")["standing_authorization"]["decisions"][-1]
        self.issue.body += "\nA new requirement."
        self.rebind()
        changed = read(self.project, "objective")["standing_authorization"]
        self.issue.body = original_body
        self.rebind()
        self.assertEqual(self.summary()["state"], "suspended")
        restored = read(self.project, "objective")["standing_authorization"]
        self.assertGreater(restored["not_before"], changed["not_before"])
        self.assertEqual(restored["scope_changed_for"], original["id"])
        self.assertEqual(self.summary()["scope_digest"], original["binding"]["scope_digest"])
        path = personal_path(self.project)
        config = yaml.safe_load(path.read_text()); config["authorization_verifier"] = ["/bin/false"]
        path.write_text(yaml.safe_dump(config))
        decided = original["utc"]

        def verify(command, request):
            return {**deepcopy(request), "verified": True, "owner_source": "authenticated-owner:fixture",
                    "decided_at": decided, "single_use": "restored-scope-decision"}

        request = {**DECISION, "channel": "relayed", "relay": {"reference": "restored-scope-decision"}}
        before = self.saved()
        with patch("pod.relay.verify", side_effect=verify), \
                patch("pod.standing._target", return_value={"repository": "acme/example", "remote": "origin", "base": "target"}):
            with self.assertRaises(PodError) as caught:
                self.op("standing-authorization", action="activate", decision=request)
            self.assertEqual(caught.exception.code, "standing_authorization_stale")
            self.assertEqual(self.saved(), before)
            decided = datetime.now(timezone.utc).isoformat()
            self.assertEqual(self.op("standing-authorization", action="activate", decision=request)["standing_authorization"]["state"], "active")
        self.issue.body += "\nAnother material change."
        self.rebind(); self.issue.body = original_body; self.rebind()
        self.assertEqual(self.summary()["state"], "suspended")
        self.assertEqual(self.activate()["standing_authorization"]["state"], "active")

    def test_f1_never_yolo_issue_status_exposes_normalized_digest_without_writes(self):
        from pod.status import status
        self.bind("- [x] **PoD#1\n- [X] Extra check")
        before = self.saved()
        context = read(self.project, "objective")
        source = context["checkpoint"]["objective_source"]
        expected = digest({"criteria": context["checkpoint"]["criteria"],
                           **{key: source[key] for key in ("repository", "number", "locator", "amendments")},
                           "body_scope_sha256": hashlib.sha256(self.issue.body.replace("[x]", "[ ]").replace("[X]", "[ ]").encode()).hexdigest()})
        with patch("pod.github.GhPort", side_effect=AssertionError("public status fetched the issue")):
            public = status(self.project, None, objective="objective")["standing_authorization"]
        self.assertIsNone(public["scope_digest"])
        self.assertEqual(public["scope_digest_reason"], "no_record")
        # An arbitrary reader may explicitly request this read without acquiring
        # coordinator write authority, and it never records the observation.
        explicit = self.op("standing-authorization", owner="read-only-observer", action="show")["standing_authorization"]
        self.assertEqual(explicit["scope_digest"], expected)
        self.assertEqual(explicit["scope_digest_source"], "live_issue")
        self.assertEqual(self.saved(), before)
        self.assertNotIn("standing_authorization", read(self.project, "objective"))
        self.assertEqual(self.activate()["decision"]["binding"]["scope_digest"], expected)
        with patch("pod.github.GhPort", side_effect=AssertionError("a stored digest fetched the issue")):
            observed = self.summary()
            public = status(self.project, None, objective="objective")["standing_authorization"]
        self.assertEqual(public, observed)
        self.assertEqual(public["scope_digest"], expected)
        self.assertEqual(public["scope_digest_source"], "recorded")

    def issue_scope_restoration(self, *, stop_created=False, revoked=False):
        from pod.config import personal_path
        import yaml
        moment = StandingOrderingTests.MOMENT
        self.bind()
        self.prepare()
        with patch("pod.standing._clock", return_value=moment):
            self.op("standing-authorization", action="revoke" if stop_created else "activate", decision=DECISION)
        if revoked:
            with patch("pod.standing._clock", return_value=moment + timedelta(seconds=1)):
                self.op("standing-authorization", action="revoke", decision=DECISION)
        if not stop_created:
            self.issue.body += "\nRequirement B."
            with patch("pod.standing._clock", return_value=moment + timedelta(seconds=2)):
                self.rebind()
        confirmed = self.summary()["scope_digest"]
        decided = (moment + timedelta(seconds=3)).isoformat()
        reference = "owner-on-B"
        body_b = self.issue.body
        self.issue.body += "\nRequirement C."
        with patch("pod.standing._clock", return_value=moment + timedelta(seconds=4)):
            self.rebind()
        self.issue.body = body_b
        with patch("pod.standing._clock", return_value=moment + timedelta(seconds=5)):
            self.rebind()
        path = personal_path(self.project)
        config = yaml.safe_load(path.read_text()); config["authorization_verifier"] = ["/bin/false"]
        path.write_text(yaml.safe_dump(config))

        def output(command, request):
            return json.dumps({**deepcopy(request), "verified": True, "owner_source": "authenticated-owner:fixture",
                               "decided_at": decided, "single_use": reference,
                               "standing": {**request["standing"], "scope_digest": confirmed}}).encode()

        record = read(self.project, "objective")["standing_authorization"]
        self.assertEqual(self.summary()["scope_digest"], confirmed)
        before = self.saved()
        from pod.ledger import state_root
        receipts = {p.name: p.read_bytes() for p in (state_root(self.project) / ".relay-authorizations").glob("*.json")}
        with patch("pod.relay._output", side_effect=output), \
                patch("pod.standing._clock", return_value=moment + timedelta(seconds=10)), \
                patch("pod.standing._target", return_value={"repository": "acme/example", "remote": "origin", "base": "target"}):
            with self.assertRaises(PodError) as caught:
                self.op("standing-authorization", action="activate", decision={**DECISION, "channel": "relayed",
                        "relay": {"reference": reference}, "widening": ["deploy"]})
        self.assertEqual(caught.exception.code, "standing_authorization_stale")
        self.assertTrue(caught.exception.detail["next_action"])
        self.assertEqual(self.saved(), before)
        self.assertEqual({p.name: p.read_bytes() for p in (state_root(self.project) / ".relay-authorizations").glob("*.json")}, receipts)
        self.assertEqual(record["not_before"], "2030-01-01T00:00:05Z")
        for kind in ("push", "deploy"):
            result = self.op("governor", action=self.action(kind, authorization=None))
            self.assertEqual(result["decision"], "DEFER")
            self.assertIn("authorization_missing", [row["code"] for row in result["reasons"]])
        self.assertEqual(self.summary()["state"], "none" if stop_created else "revoked" if revoked else "suspended")
        decided = (moment + timedelta(seconds=6)).isoformat()
        reference = "new-owner-on-B"
        with patch("pod.relay._output", side_effect=output), \
                patch("pod.standing._clock", return_value=moment + timedelta(seconds=11)), \
                patch("pod.standing._target", return_value={"repository": "acme/example", "remote": "origin", "base": "target"}):
            result = self.op("standing-authorization", action="activate", decision={**DECISION, "channel": "relayed",
                            "relay": {"reference": reference}, "widening": ["deploy"]})
        self.assertEqual(result["standing_authorization"]["state"], "active")

    def test_r4_1_issue_body_bcb_refuses_the_saved_grant_without_writes_or_effects(self):
        self.issue_scope_restoration()

    def test_r4_1_revoked_issue_holder_remembers_every_body_change(self):
        self.issue_scope_restoration(revoked=True)

    def test_r4_1_stop_created_issue_record_remembers_aba_and_accepts_a_newer_decision(self):
        self.issue_scope_restoration(stop_created=True)

    def test_r4_1_repeated_issue_observations_do_not_move_the_barrier(self):
        moment = StandingOrderingTests.MOMENT
        self.bind()
        with patch("pod.standing._clock", return_value=moment):
            self.activate()
        self.issue.body += "\nRequirement B."
        with patch("pod.standing._clock", return_value=moment + timedelta(seconds=2)):
            self.rebind()
        for seconds in (4, 6):
            with patch("pod.standing._clock", return_value=moment + timedelta(seconds=seconds)):
                self.rebind()
            self.assertEqual(read(self.project, "objective")["standing_authorization"]["not_before"],
                             "2030-01-01T00:00:02Z")

    def test_r4_1_never_record_issue_aba_accepts_only_the_saved_restored_content(self):
        from pod.config import personal_path
        import yaml
        moment = StandingOrderingTests.MOMENT
        self.bind()
        confirmed = self.summary()["scope_digest"]
        body_a = self.issue.body
        self.issue.body += "\nRequirement B."
        self.rebind()
        self.issue.body = body_a
        self.rebind()
        context = read(self.project, "objective")
        self.assertNotIn("standing_authorization", context)
        self.assertEqual(context.get("decision_channels", 1), 1)
        path = personal_path(self.project)
        config = yaml.safe_load(path.read_text()); config["authorization_verifier"] = ["/bin/false"]
        path.write_text(yaml.safe_dump(config))

        def output(command, request):
            return json.dumps({**deepcopy(request), "verified": True, "owner_source": "authenticated-owner:fixture",
                               "decided_at": (moment + timedelta(seconds=1)).isoformat(), "single_use": "first-owner-on-A",
                               "standing": {**request["standing"], "scope_digest": confirmed}}).encode()

        with patch("pod.relay._output", side_effect=output), \
                patch("pod.standing._clock", return_value=moment + timedelta(seconds=10)), \
                patch("pod.standing._target", return_value={"repository": "acme/example", "remote": "origin", "base": "target"}):
            result = self.op("standing-authorization", action="activate", decision={**DECISION, "channel": "relayed",
                            "relay": {"reference": "first-owner-on-A"}})
        self.assertEqual(result["standing_authorization"]["state"], "active")
        self.assertEqual(result["decision"]["binding"]["scope_digest"], confirmed)

    def test_r4_2_show_releases_the_objective_lock_before_its_live_issue_read(self):
        import threading
        from pod.github import issue_recheck_live
        from pod.ledger import _lock
        self.bind()
        path = _path(self.project, "objective")
        revision = read(self.project, "objective")["revision"]
        done, errors, completed_during_read, after_write = threading.Event(), [], [], []

        def writer():
            try:
                with _lock(path):
                    _write(path, _read(path))
                after_write.append(self.saved())
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=writer)

        def live_read(project, source):
            thread.start()
            completed_during_read.append(done.wait(1))
            return issue_recheck_live(project, source)

        try:
            with patch("pod.github.issue_recheck_live", side_effect=live_read):
                shown = self.op("standing-authorization", owner="any-reader", action="show")["standing_authorization"]
        finally:
            if thread.ident is not None:
                thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(completed_during_read, [True])
        self.assertEqual(read(self.project, "objective")["revision"], revision + 1)
        self.assertIsNone(shown["scope_digest"])
        self.assertEqual(shown["scope_digest_reason"], "context_changed")
        self.assertIn("retry", shown["scope_digest_next_action"])
        self.assertEqual(self.saved(), after_write[0])
        self.assertNotIn("standing_authorization", read(self.project, "objective"))

    def test_f1_unobserved_status_and_failed_explicit_reads_name_the_missing_digest(self):
        from pod.status import status
        self.bind(); self.activate()
        self.issue.body += "\nA new requirement."
        with patch("pod.pod_ticks.refresh"):
            self.rebind()
        before = self.saved()
        with patch("pod.github.GhPort", side_effect=AssertionError("public status fetched the issue")):
            public = status(self.project, None, objective="objective")["standing_authorization"]
        self.assertIsNone(public["scope_digest"])
        self.assertEqual(public["scope_digest_reason"], "scope_unobserved")
        self.issue.unreadable = True
        self.assertEqual(self.summary()["scope_digest_reason"], "issue_access_unavailable")
        self.issue.body += "\nUnreconciled edit."
        blocked = self.summary()
        self.assertIsNone(blocked["scope_digest"])
        self.assertTrue(blocked["scope_digest_reason"])
        self.assertEqual(self.saved(), before)

    def test_activation_reads_current_issue_and_refuses_unreconciled_body(self):
        self.bind()
        self.assertNotIn("standing_authorization", read(self.project, "objective"))
        before = self.saved()
        self.issue.body += "\nNew requirement"
        self.failure("issue_reconciliation_required", self.activate)
        self.assertEqual(self.saved(), before)
        self.rebind()
        activated = self.activate()
        context = read(self.project, "objective")
        self.assertEqual(activated["standing_authorization"]["state"], "active")
        self.assertEqual(context["standing_authorization"]["observed"], {
            "source": digest(context["checkpoint"]["objective_source"]),
            "body_scope_sha256": hashlib.sha256(self.issue.body.encode()).hexdigest()})

    def test_pod_tick_rebind_keeps_scope_active(self):
        self.bind(); self.activate(); self.prove()
        binding = read(self.project, "objective")["standing_authorization"]["decisions"][-1]["binding"]
        result = self.tick()
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(self.summary()["state"], "active")

        context = read(self.project, "objective")
        self.assertEqual(context["standing_authorization"]["decisions"][-1]["binding"], binding)
        self.assertEqual(context["standing_authorization"]["observed"]["source"], digest(result["objective_source"]))

    def test_checkbox_reconciliation_and_metadata_only_edits_keep_active(self):
        self.bind("- [ ] **PoD#1\r\n  * [X] Extra check\r\n1. [x] Another check\r\n- [X]\r\n")
        self.activate()
        self.issue.updated = "2099-01-01T00:00:00Z"
        self.issue.title = "PES: Renamed widgets"
        self.assertEqual(self.summary()["state"], "active")
        self.issue.body = self.issue.body.replace("[ ]", "[x]").replace("[X]", "[ ]")
        self.rebind()
        self.assertEqual(self.summary()["state"], "active")

    def test_reconciled_requirement_text_suspends_and_owner_reconfirms(self):
        self.bind(); self.activate()
        activated = read(self.project, "objective")["standing_authorization"]["not_before"]
        self.issue.body += "\nA new requirement."
        self.rebind()
        self.assertEqual((self.summary()["state"], self.summary()["reason"]), ("suspended", "scope_changed"))
        self.assertGreater(read(self.project, "objective")["standing_authorization"]["not_before"], activated)
        self.assertEqual(self.activate()["standing_authorization"]["state"], "active")

    def test_amendment_change_suspends(self):
        from pod.github import issue_intake
        self.issue.amendments[99] = "Original decision"
        amendments = [self.locator + "#issuecomment-99"]
        self.bind(amendments=amendments); self.activate()
        activated = read(self.project, "objective")["standing_authorization"]["not_before"]
        self.issue.amendments[99] = "Changed decision"
        source = issue_intake(self.project, self.locator, port=self.issue, amendments=amendments)["source"]
        self.write(objective_source=source)
        self.assertEqual(self.summary()["state"], "suspended")
        self.assertGreater(read(self.project, "objective")["standing_authorization"]["not_before"], activated)

    def test_unobserved_rebind_fails_closed_until_matching_observation(self):
        self.bind(); self.activate()
        self.issue.body = self.issue.body.replace("[ ]", "[x]")
        with patch("pod.pod_ticks.refresh"):
            self.rebind()
        self.assertEqual((self.summary()["state"], self.summary()["reason"]), ("suspended", "scope_unobserved"))
        context = read(self.project, "objective")
        from pod.pod_ticks import refresh
        refresh(context, self.project, context["checkpoint"]["objective_source"])
        _write(_path(self.project, "objective"), context)
        self.assertEqual(self.summary()["state"], "active")

    # R5-1: one observer predicate joins each observation to the actual bound source.
    # An unread changed source is its own identity (a fail-closed transition); learning
    # the body of that same bound source, or Pod's own tick, is not a transition.
    def _at(self, seconds):
        return patch("pod.standing._clock", return_value=StandingOrderingTests.MOMENT + timedelta(seconds=seconds))

    def _rebind(self, seconds, *, read=True):
        with self._at(seconds):
            if read:
                self.rebind()
            else:
                with patch("pod.pod_ticks.refresh"):
                    self.rebind()

    def _saved_relay(self, *, decided, arrival, confirmed, reference):
        from pod.config import personal_path
        import yaml
        moment = StandingOrderingTests.MOMENT
        path = personal_path(self.project)
        config = yaml.safe_load(path.read_text()); config["authorization_verifier"] = ["/bin/false"]
        path.write_text(yaml.safe_dump(config))

        def output(command, request):
            # The host verifier returns the digest it saved with the owner's decision.
            return json.dumps({**deepcopy(request), "verified": True, "owner_source": "authenticated-owner:fixture",
                               "decided_at": (moment + timedelta(seconds=decided)).isoformat(), "single_use": reference,
                               "standing": {**request["standing"], "scope_digest": confirmed}}).encode()

        with patch("pod.relay._output", side_effect=output), self._at(arrival), \
                patch("pod.standing._target", return_value={"repository": "acme/example", "remote": "origin", "base": "target"}):
            return self.op("standing-authorization", action="activate", decision={**DECISION, "channel": "relayed",
                           "relay": {"reference": reference}, "widening": ["deploy"]})

    def _assert_saved_relay_stale(self, **relay):
        from pod.ledger import state_root
        before = self.saved()
        receipts = {item.name: item.read_bytes() for item in (state_root(self.project) / ".relay-authorizations").glob("*.json")}
        with self.assertRaises(PodError) as caught:
            self._saved_relay(**relay)
        self.assertEqual(caught.exception.code, "standing_authorization_stale")
        self.assertEqual(self.saved(), before)
        self.assertEqual({item.name: item.read_bytes()
                          for item in (state_root(self.project) / ".relay-authorizations").glob("*.json")}, receipts)
        for kind in ("push", "deploy"):
            result = self.op("governor", action=self.action(kind, authorization=None))
            self.assertEqual(result["decision"], "DEFER")

    def _not_before(self):
        return read(self.project, "objective")["standing_authorization"]["not_before"]

    def _holder_bcb(self, *, restore_read):
        self.bind(); self.prepare()
        with self._at(0):
            self.activate()
        self.issue.body += "\nRequirement B."
        self._rebind(2)
        confirmed = self.summary()["scope_digest"]
        body_b = self.issue.body
        self.issue.body += "\nRequirement C."
        self._rebind(4, read=False)
        self.assertEqual(self._not_before(), "2030-01-01T00:00:04Z")
        self.issue.body = body_b
        self._rebind(5, read=restore_read)
        self.assertEqual(self._not_before(), "2030-01-01T00:00:05Z")
        self._assert_saved_relay_stale(decided=3, arrival=10, confirmed=confirmed, reference="owner-on-B")

    def test_r5_1_unread_intermediate_rebind_fences_holder_bcb(self):
        self._holder_bcb(restore_read=True)

    def test_r5_1_unread_restore_also_fences_holder_bcb(self):
        self._holder_bcb(restore_read=False)

    def test_r5_1_decision_on_the_live_scope_after_an_unread_rebind_is_accepted(self):
        self.bind(); self.prepare()
        with self._at(0):
            self.activate()
        self.issue.body += "\nRequirement C."
        self._rebind(4, read=False)
        confirmed = self.summary()["scope_digest"]
        result = self._saved_relay(decided=6, arrival=10, confirmed=confirmed, reference="owner-on-C")
        self.assertEqual(result["standing_authorization"]["state"], "active")
        # Learning the body of the same bound source during the activation is no transition.
        self.assertEqual(self._not_before(), "2030-01-01T00:00:06Z")

    def test_r5_1_stop_created_record_first_read_by_pod_tick_moves_no_barrier(self):
        self.bind(); self.prepare()
        with self._at(0):
            self.op("standing-authorization", action="revoke", decision=DECISION)
        confirmed = self.summary()["scope_digest"]
        self.prove()
        with self._at(2):
            self.assertEqual(self.tick()["status"], "confirmed")
        self.assertEqual(self._not_before(), "2030-01-01T00:00:00Z")
        result = self._saved_relay(decided=1, arrival=10, confirmed=confirmed, reference="owner-on-A")
        self.assertEqual(result["standing_authorization"]["state"], "active")

    def test_r5_1_stop_created_record_first_read_by_a_checkpoint_moves_no_barrier(self):
        self.bind(); self.prepare()
        with self._at(0):
            self.op("standing-authorization", action="revoke", decision=DECISION)
        confirmed = self.summary()["scope_digest"]
        # A reconciling checkpoint's own production refresh reads the unchanged bound body.
        context = read(self.project, "objective")
        from pod.pod_ticks import refresh
        refresh(context, self.project, context["checkpoint"]["objective_source"])
        with self._at(2):
            _write(_path(self.project, "objective"), context)
        self.assertIn("observed", read(self.project, "objective")["standing_authorization"])
        self.assertEqual(self._not_before(), "2030-01-01T00:00:00Z")
        result = self._saved_relay(decided=1, arrival=10, confirmed=confirmed, reference="owner-on-A")
        self.assertEqual(result["standing_authorization"]["state"], "active")

    def test_r5_1_stop_created_unread_change_then_restore_refuses_the_older_grant(self):
        self.bind(); self.prepare()
        with self._at(0):
            self.op("standing-authorization", action="revoke", decision=DECISION)
        confirmed = self.summary()["scope_digest"]
        body_a = self.issue.body
        self.issue.body += "\nRequirement B."
        self._rebind(2, read=False)
        self.issue.body = body_a
        self._rebind(4)
        self.assertEqual(self._not_before(), "2030-01-01T00:00:04Z")
        self._assert_saved_relay_stale(decided=1, arrival=10, confirmed=confirmed, reference="owner-on-A")

    def test_r5_1_unread_checkbox_edit_fails_closed_then_learning_moves_no_barrier(self):
        self.bind(); self.prepare()
        with self._at(0):
            self.activate()
        self.issue.body = self.issue.body.replace("[ ]", "[x]")
        self._rebind(2, read=False)
        self.assertEqual(self._not_before(), "2030-01-01T00:00:02Z")
        context = read(self.project, "objective")
        from pod.pod_ticks import refresh
        refresh(context, self.project, context["checkpoint"]["objective_source"])
        with self._at(4):
            _write(_path(self.project, "objective"), context)
        self.assertEqual(self._not_before(), "2030-01-01T00:00:02Z")
        self.assertEqual(self.summary()["state"], "active")


class StandingCleanupTests(unittest.TestCase):
    def setUp(self):
        from tests.test_cleanup_plan import CleanupPlanTests
        self.case = CleanupPlanTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.project = self.case.project
        guard = patch("pod.orca.executable", side_effect=AssertionError("cleanup escaped recording ports"))
        guard.start(); self.addCleanup(guard.stop)

    def activate(self, **fields):
        return run("standing-authorization", {"project": str(self.project), "objective": "objective", "owner": "owner",
                                              "action": "activate", "decision": {**DECISION, **fields}})

    def test_cleanup_projection_covers_integrated_asks_unique_and_never_covers_protected(self):
        self.assertEqual(self.case.observed()["standing_authorization"]["covers"], [])
        self.activate()
        integrated = self.case.observed()
        self.assertTrue(integrated["standing_authorization"]["covers"])
        self.assertTrue(all(row["id"] in integrated["standing_authorization"]["covers"] for row in integrated["resources"]
                            if row["class"] == "integrated"))
        valuable = self.case.child / "valuable.txt"
        valuable.write_text("unique material\n")
        unique = self.case.observed()
        worktree = self.case.resource(unique, "worktree")
        self.assertIn(worktree["id"], unique["standing_authorization"]["asks"])
        self.assertNotIn(worktree["id"], unique["standing_authorization"]["covers"])
        self.activate(widening=["unique_deletion"])
        widened = self.case.observed()
        self.assertIn(worktree["id"], widened["standing_authorization"]["covers"])
        self.case.terminals = [{"handle": "foreign", "worktreePath": str(self.case.child)}]
        protected = self.case.observed()
        self.assertEqual(self.case.resource(protected, "worktree")["class"], "protected")
        self.assertNotIn(worktree["id"], protected["standing_authorization"]["covers"])
        self.assertEqual(valuable.read_text(), "unique material\n")


class StandingGuidanceTests(unittest.TestCase):
    def test_n4_delivery_projection_names_current_consent_and_historical_effects(self):
        text = self.text("docs/spec/interface.md").split("### R139", 1)[1].split("## Acceptance", 1)[0]
        self.assertIn("`delivery_authorization` is current applicable consent", text)
        self.assertIn("effects completed under yolo are listed under `standing_authorization.effects`", text)

    def test_n7_each_relay_widening_is_explicit_and_exactly_bound(self):
        text = self.text("docs/spec/delivery.md")
        for requirement, following in (("### R120", "### R136"), ("### R136", "### R137")):
            section = text.split(requirement, 1)[1].split(following, 1)[0]
            self.assertIn("A verified relayed widening may name any default exclusion", section)
            self.assertIn("`deploy`, `paid_resources`, `secrets`, `unique_deletion` and `scope_growth`", section)
            self.assertIn("each explicitly, under the same exact binding", section)

    def text(self, path):
        return " ".join((REPOSITORY / path).read_text().split())

    def test_responder_answers_never_resolve_excluded_or_owner_decisions(self):
        for path in ("docs/spec/delivery.md", "skills/pod/references/governor.md"):
            text = self.text(path)
            self.assertIn("A responder answer never resolves an R4-excluded question or any owner-decision kind", text)
            for exclusion in ("production deployment", "new paid or cloud resources", "secrets",
                              "deleting unique or unmerged material", "work beyond the objective's scope"):
                self.assertIn(exclusion, text)
            self.assertIn("Those questions still go to the owner", text)

    def test_cleanup_uses_the_plans_exact_scope_standing_instruction(self):
        text = self.text("skills/pod/references/cleanup.md")
        for clause in ("standing_authorization", "covers", "asks", "applicable exact-scope instruction",
                       "Covered classes need no further owner confirmation", "asks still require it"):
            self.assertIn(clause, text)

    def test_standing_authorities_have_distinct_names_and_lifetimes(self):
        self.assertIn("worker-tab standing close authorization", self.text("skills/pod/SKILL.md"))
        self.assertIn("independent of the objective's yolo record", self.text("skills/pod/references/worker-tabs.md"))
        self.assertIn("separate from both the yolo record and the worker-tab standing close authorization",
                      self.text("docs/spec/interface.md"))

    def test_synthesized_consent_requires_a_prepared_target_in_spec(self):
        text = self.text("docs/spec/delivery.md").split("### R137", 1)[1].split("### R138", 1)[0]
        self.assertIn("the required prepared target `<remote>/<base>`", text)
        self.assertNotIn("when the unit has one", text)

    def test_run_selected_status_spec_names_governor_owner_decisions(self):
        self.assertIn("Run-selected status includes Governor authorization rows in `owner_decisions`",
                      self.text("docs/spec/interface.md"))

    def test_delayed_caller_effect_requires_a_fresh_governor_or_show_check(self):
        text = self.text("skills/pod/references/governor.md")
        self.assertIn("Immediately before a caller-executed merge or reservation", text)
        self.assertIn("when time has passed or a stop may have arrived", text)
        self.assertIn("run the Governor again (or `show` to check standing state)", text)


class StandingCapacityTests(StandingCase):
    MOMENT = datetime(2030, 1, 1, tzinfo=timezone.utc)
    QUESTION = "q" * 512

    @staticmethod
    def padding(size):
        # Control characters take six encoded bytes each, unlike ASCII or UTF-8.
        return "\x01" * (size // 6) + "x" * (size % 6)

    @classmethod
    def maximum_decision(cls, fixture, operation="activate"):
        context = read(fixture.project, "objective")
        index = len(context.get("standing_authorization", {}).get("decisions", [])) + 1
        key = f"decision-{index}".ljust(512, "k")
        decision = {**DECISION, "instruction": "x", "channel": "relayed", "relay": {"reference": "r" * 512}}
        parsed = standing._decision(decision, operation, cls.MOMENT)
        verdict = {"schema": relay.SCHEMA, "reference": decision["relay"]["reference"], "action": "standing",
                   "objective": "objective", "candidate": None, "target": standing._target(fixture.project, context),
                   "pr": None, "standing": standing._request_fields(operation, parsed, scope_digest=standing.scope_digest(context)), "verified": True,
                   "owner_source": "s" * 512, "single_use": key, "decided_at": f"2030-01-01T00:00:{index:02d}Z"}
        row = {**parsed, "id": f"Y{index}", "operation": operation, "utc": "2030-01-01T00:00:00Z",
               "seq": context["checkpoint"]["seq"], "relay": {"reference": verdict["reference"], "owner_source": verdict["owner_source"]},
               "relay_verification": verdict}
        if operation == "activate":
            row.update(binding={"scope_digest": standing.scope_digest(context)}, exclusions=list(standing.STANDING_EXCLUSIONS))
        decision["instruction"] = cls.padding(standing.MAX_DECISION_BYTES - len(encoded(row, compact=True)) + 1)
        fixture.control(changes={"single_use": key, "owner_source": verdict["owner_source"], "decided_at": verdict["decided_at"]})
        with patch("pod.standing._clock", return_value=cls.MOMENT + timedelta(seconds=index)):
            result = fixture.operation(operation, decision=decision)
        fixture.assertEqual(len(encoded(result["decision"], compact=True)), standing.MAX_DECISION_BYTES)
        return result

    @classmethod
    def maximum_answer(cls, fixture, index):
        context = read(fixture.project, "objective")
        answer = {"question": cls.QUESTION, "text": "x", "relay": {"reference": "r" * 512}}
        verdict = {"schema": relay.SCHEMA, "reference": answer["relay"]["reference"], "action": "answer",
                   "objective": "objective", "candidate": None, "target": standing._target(fixture.project, context),
                   "pr": None, "answer": {key: answer[key] for key in ("question", "text")}, "verified": True,
                   "responder_source": "s" * 512, "single_use": f"answer-{index}".ljust(512, "k"),
                   "decided_at": "2026-09-21T00:00:00Z"}
        row = {**answer, "id": f"A{index}", "decided_by": "responder", "responder_source": verdict["responder_source"],
               "relay_verification": verdict, "utc": "2030-01-01T00:00:00Z", "under": context["standing_authorization"]["decisions"][-1]["id"]}
        remaining = standing.MAX_ANSWER_BYTES - len(encoded(row, compact=True)) + 2
        if remaining % 2:
            verdict["single_use"] = verdict["single_use"][:-1]
            remaining += 1
        answer["text"] = cls.padding(remaining // 2)
        fixture.control(changes={"single_use": verdict["single_use"], "responder_source": verdict["responder_source"]})
        with patch("pod.standing._clock", return_value=cls.MOMENT):
            result = fixture.operation("answer", answer=answer)
        fixture.assertEqual(len(encoded(result["answer"], compact=True)), standing.MAX_ANSWER_BYTES)

    @classmethod
    def setUpClass(cls):
        # Full encoded-size rows use the actual write and verifier boundaries once.
        cls.prepared = StandingCase()
        cls.prepared.setUp()
        cls.addClassCleanup(cls.prepared.doCleanups)
        for _ in range(31):
            cls.maximum_decision(cls.prepared)
        cls.prepared.case.write(cls.prepared.case.stored(), questions=[cls.QUESTION])
        for index in range(1, standing.MAX_ANSWERS + 1):
            cls.maximum_answer(cls.prepared, index)
        cls.context_bytes = _path(cls.prepared.project, "objective").read_bytes()

    def setUp(self):
        # Reuse the real disposable context; no synthetic history bypasses activation.
        self.__dict__.update({key: value for key, value in self.prepared.__dict__.items()
                              if key not in ("_cleanups", "_outcome", "_testMethodName", "_testMethodDoc")})
        _path(self.project, "objective").write_bytes(self.context_bytes)

    def test_activation_reserves_last_slot_so_revoke_always_fits(self):
        self.assertEqual(len(read(self.project, "objective")["standing_authorization"]["decisions"]), 31)
        self.refusal("standing_authorization_full", self.activate)
        revoked = self.maximum_decision(self, "revoke")
        self.assertEqual((revoked["decision"]["id"], revoked["standing_authorization"]["state"]), ("Y32", "revoked"))
        self.refusal("standing_authorization_full", self.activate)

    def test_full_maximum_size_verified_answers_fit_and_next_refuses_without_write(self):
        self.assertEqual(self.operation("show")["standing_authorization"]["responder_decisions"]["total"], standing.MAX_ANSWERS)
        self.control(changes={"single_use": "one-past-answer-cap"})
        self.refusal("responder_answers_full", self.operation, "answer", answer={**ANSWER, "question": self.QUESTION})
        context = read(self.project, "objective")
        self.assertEqual([row["id"] for row in context["standing_authorization"]["answers"]],
                         [f"A{index}" for index in range(1, standing.MAX_ANSWERS + 1)])

    def test_worst_case_record_leaves_headroom_for_checkpoint_and_closure_report(self):
        from pod.history import CONTEXT_LIMIT
        self.maximum_decision(self, "revoke")
        context = read(self.project, "objective")
        record = context["standing_authorization"]
        maximum_rows = 32 * standing.MAX_DECISION_BYTES + standing.MAX_ANSWERS * standing.MAX_ANSWER_BYTES
        self.assertEqual(sum(len(encoded(row, compact=True)) for row in [*record["decisions"], *record["answers"]]), maximum_rows)
        self.assertLessEqual(len(encoded(record, compact=True)), maximum_rows + 256)
        self.assertLessEqual(maximum_rows, CONTEXT_LIMIT // 2)
        self.assertGreater(CONTEXT_LIMIT - len(encoded(context, compact=True)), 240 * 1024)
        self.case.write(self.case.stored(), next_safe_action="checkpoint after full standing history")
        result = self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        self.assertEqual(result["checkpoint"]["closure"]["report"]["standing_authorization"]["state"], "ended")
        self.assertLess(_path(self.project, "objective").stat().st_size, CONTEXT_LIMIT)

    def test_e7_capacity_warning_names_bounded_standing_section(self):
        from pod.history import capacity
        self.maximum_decision(self, "revoke")
        path = _path(self.project, "objective")
        before = self.saved()
        result = capacity(path, read(self.project, "objective"))
        self.assertTrue(result["warning"])
        self.assertEqual(result["largest_sections"][0]["section"], "standing_authorization")
        self.assertIn("standing_authorization section is bounded and needs no maintenance", result["next_action"])
        self.assertEqual(self.saved(), before)

    def test_e5_stale_and_future_relays_refuse_before_full_history_cap(self):
        for code, seconds in (("standing_authorization_stale", 10), ("standing_authorization_future", 61)):
            with self.subTest(code=code):
                self.control(changes={"single_use": code, "decided_at": (self.MOMENT + timedelta(seconds=seconds)).isoformat()})
                request = {"project": str(self.project), "objective": "objective", "owner": "owner", "action": "activate",
                           "decision": {**DECISION, "channel": "relayed", "relay": {"reference": code}}}
                receipts = self.receipts()
                self.refusal(code, standing.operate, request, now=self.MOMENT + timedelta(seconds=60))
                if code == "standing_authorization_future":
                    self.assertEqual(len(self.receipts()), len(receipts) + 1)
                else:
                    self.assertEqual(self.receipts(), receipts)

    def test_n2_maximum_history_and_64_effects_close_with_ordinary_objective_headroom(self):
        from pod.history import CONTEXT_LIMIT
        self.maximum_decision(self, "revoke")
        # A completed production Governor row supplies the real effect shape.
        journal = self.journal()
        source = self.context_bytes
        context = read(self.project, "objective")
        _path(self.project, "objective").write_bytes(source)
        self.case.op("governor-execute", action=self.proposed())
        sample = deepcopy(self.journal()["actions"][-1])
        _write(_path(self.project, "objective"), context)
        journal["actions"] = [{**deepcopy(sample), "record_id": f"effect-{index:02d}"} for index in range(64)]
        _write_journal(_record_path(self.project, "objective"), journal)
        # 64 KB of current objective context is independent of the standing history.
        questions = [{"id": f"Q{index}", "text": f"Question {index}: " + "q" * 1010} for index in range(64)]
        self.case.write(self.case.stored(), questions=questions)
        closed = self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])],
                                 questions=questions, close=True)
        report = closed["checkpoint"]["closure"]["report"]
        self.assertEqual(report["standing_authorization"]["effects"]["total"], 64)
        owners = [row for row in report["owner_decisions"] if row["kind"] == "standing_authorization"]
        self.assertEqual(len(owners), 32)
        self.assertTrue(all(set(row["decision"]) == {"id", "operation", "channel", "decided_at"} for row in owners))
        answers = report["standing_authorization"]["responder_decisions"]["items"]
        self.assertTrue(all(len(encoded(row["text"], compact=True)) <= 259 and row["text_truncated"] for row in answers))
        self.assertGreater(CONTEXT_LIMIT - _path(self.project, "objective").stat().st_size, 100 * 1024)
        # Full answer text and verdicts remain at the referenced record positions.
        stored = read(self.project, "objective")
        self.assertEqual(stored["standing_authorization"]["answers"], context["standing_authorization"]["answers"])
        from pod.ledger import closed_snapshot_context
        from pod.obligations import validate_closed_snapshot
        validate_closed_snapshot(stored["checkpoint"], closed_snapshot_context(self.project, "objective", stored))

    def test_full_standing_history_keeps_admission_and_report_writes_available(self):
        self.case.write([self.case.criterion(), self.case.sub("S1")])
        from pod.github import repository_context
        placement = repository_context(self.project)
        self.case.port.placement.update(repository=placement["repository"], branch=placement["branch"])
        frozen = self.case.packet(["S1"])
        admission = self.case.start("bounded-work", frozen)["admission"]
        self.case.settle(admission)
        rows = self.case.stored()
        rows[1].pop("executor")
        rows[1].update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.case.report(admission, frozen, map={"obligations": rows})
        context = read(self.project, "objective")
        self.assertEqual(context["admissions"][admission["admission_id"]]["report"]["outcome"], "succeeded")

    def test_recorded_keys_refuse_replay_before_full_history_checks(self):
        record = read(self.project, "objective")["standing_authorization"]
        decision = record["decisions"][0]
        self.control(changes={"single_use": decision["relay_verification"]["single_use"],
                              "owner_source": decision["relay_verification"]["owner_source"]})
        fields = {key: decision[key] for key in ("provenance", "instruction", "channel", "relay", "scope", "widening", "expires_at")}
        self.refusal("relay_authorization_reused", self.operation, decision=fields)
        answer = record["answers"][-1]
        self.control(changes={"single_use": answer["relay_verification"]["single_use"],
                              "responder_source": answer["responder_source"]})
        self.refusal("relay_authorization_reused", self.operation, "answer",
                     answer={key: answer[key] for key in ("question", "text", "relay")})


class StandingSuccessionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_carried_claim import CarriedCase
        self.case = CarriedCase()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        guard = patch("pod.orca.executable", side_effect=AssertionError("succession escaped native double"))
        guard.start(); self.addCleanup(guard.stop)

    def test_real_successor_claim_carries_effect_but_never_standing_authority(self):
        def reserve():
            self.case.op("standing-authorization", action="activate", decision=DECISION)
            return self.case.op("governor", action=self.case.action(authorization=None))
        reserved = self.case.lost_with(reserve)
        self.assertEqual(reserved["decision"], "ALLOW")
        self.case.carry()
        predecessor = read(self.case.project, "objective")
        successor = read(self.case.project, "successor")
        self.assertEqual(standing.state(predecessor), "ended")
        self.assertEqual(standing.state(successor), "none")
        self.assertNotIn("standing_authorization", successor)
        carried = self.case.row(reserved["record_id"])
        self.assertEqual(carried["action"]["authorization"]["standing"]["id"], "Y1")
        self.assertIn("carried_from", carried)
        self.assertEqual(context_decisions(successor, self.case.journal("successor")), [])
        proposed = self.case.action(authorization=None)
        held = self.case.mine("governor", action=proposed)
        self.assertEqual(held["decision"], "DEFER")
        self.assertIn("authorization_missing", [reason["code"] for reason in held["reasons"]])

    def test_successor_claim_between_admission_and_effect_preserves_pending_predecessor(self):
        case = self.case
        case.establish()
        case.op("checkpoint", value=case.core(obligations=case.stored(), worktree=case.binding()))
        case.settle(case.admission)
        rows = case.stored()
        rows[1].pop("executor")
        rows[1].update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        case.op("checkpoint", value=case.core(obligations=rows))
        case.op("standing-authorization", action="activate", decision=DECISION)
        admitted_bytes = []
        def claimed(_project):
            admitted_bytes.append(_record_path(case.project, "objective").read_bytes())
            case.lose()
            case.carry()
            return case.remote
        with patch("pod.governor_effects._port", side_effect=claimed), self.assertRaises(PodError) as caught:
            case.op("governor-execute", action=case.action(authorization=None))
        self.assertEqual(caught.exception.code, "standing_authorization_inactive")
        self.assertEqual(_record_path(case.project, "objective").read_bytes(), admitted_bytes[0])
        self.assertEqual(case.journal()["actions"][-1]["outcome"], "pending")
        self.assertEqual(case.journal("successor")["actions"][-1]["outcome"], "pending")
        self.assertEqual(case.writes(), [])
        held = case.mine("governor", action=case.action(authorization=None))
        self.assertEqual(held["decision"], "DEFER")
        self.assertIn("authorization_missing", [reason["code"] for reason in held["reasons"]])


class StandingCompatibilityTests(StandingCase):
    @classmethod
    def setUpClass(cls):
        parent = Path(os.environ["POD_CACHE_HOME"]) if "POD_CACHE_HOME" in os.environ else None
        if parent:
            parent.mkdir(parents=True, exist_ok=True)
        cls.previous_home = tempfile.TemporaryDirectory(prefix="standing-0180-", dir=parent)
        cls.addClassCleanup(cls.previous_home.cleanup)
        cls.bundle = Path(cls.previous_home.name)
        archive = cls.bundle / "source.tar"
        subprocess.run(["git", "-C", str(REPOSITORY), "archive", "-o", str(archive), BASELINE, "skills", "VERSION"], check=True)
        with tarfile.open(archive) as source:
            source.extractall(cls.bundle, filter="data")

    def earlier(self, code, *args):
        result = subprocess.run([sys.executable, "-c", code, *map(str, args)], cwd=REPOSITORY,
            env={**os.environ, "PYTHONPATH": os.pathsep.join((str(self.bundle / "skills"), str(REPOSITORY)))},
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_ordinary_payload_vocabulary_does_not_select_new_discriminator(self):
        from pod.util import protect_decision_record
        for payload in ({"standing": "value", "channel": "session"},
                        {"standing_authorization": "workflow input", "channel": "session"}):
            protect_decision_record(payload)
            self.assertEqual(payload["decision_channels"], 1)

    def test_normal_closure_keeps_genuine_0180_shape_and_readers_add_normal_mode(self):
        closed = self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        report = closed["checkpoint"]["closure"]["report"]
        self.assertNotIn("standing_authorization", report)
        self.assertEqual(closed["decision_channels"], 1)
        before = self.saved()
        earlier = self.earlier('''import json, sys
from pathlib import Path
from pod import __version__
from pod.ledger import _read, closed_snapshot_context
from pod.obligations import report_projection
assert __version__ == "0.18.0"
project, path = map(Path, sys.argv[1:])
state = _read(path)
print(json.dumps(report_projection(state["checkpoint"], closed_snapshot_context(project, "objective", state))))
''', self.project, _path(self.project, "objective"))
        self.assertEqual(report, earlier)
        self.assertEqual(self.public_status()["report"]["standing_authorization"]["state"], "none")
        self.assertEqual(closed["standing_authorization"]["mode"], "normal")
        self.assertEqual(self.saved(), before)

    def test_e3_never_yolo_close_reopen_close_keeps_0180_readability_and_defer_text(self):
        before_reason = self.case.op("governor", action=self.proposed())["reasons"]
        for event in ("close", "reopen", "close"):
            if event == "close":
                self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
            else:
                self.case.write([self.case.criterion()], reopen=True, revision_authority=DECISION)
                self.assertEqual(self.case.op("governor", action=self.proposed())["reasons"], before_reason)
            context = read(self.project, "objective")
            self.assertNotIn("standing_authorization", context)
            self.assertEqual(context["decision_channels"], 1)
            before = self.saved()
            self.assertEqual(self.earlier('''import json, sys
from pathlib import Path
from pod import __version__
from pod.ledger import _read
assert __version__ == "0.18.0"
state = _read(Path(sys.argv[1]))
print(json.dumps({"read": "ok", "standing": "standing_authorization" in state}))
''', _path(self.project, "objective")), {"read": "ok", "standing": False})
            self.assertEqual(self.saved(), before)

    def test_genuine_0180_refuses_standing_context_and_journal_preserving_bytes(self):
        self.activate()
        self.case.op("governor", action=self.proposed())
        context, journal = _path(self.project, "objective"), _record_path(self.project, "objective")
        before = self.saved()
        results = self.earlier('''import json, sys
from pathlib import Path
from pod import __version__
from pod.errors import PodError
from pod.governor import _read_journal
from pod.ledger import _read
assert __version__ == "0.18.0"
outcomes = []
for callback, path in ((_read, sys.argv[1]), (_read_journal, sys.argv[2])):
    try:
        callback(Path(path))
    except PodError as exc:
        outcomes.append(exc.code)
    else:
        outcomes.append("incorrectly accepted")
print(json.dumps(outcomes))
''', context, journal)
        self.assertTrue(all(result in ("state_unsupported", "invalid_context") for result in results), results)
        self.assertEqual(self.saved(), before)
        _read(context); self.journal()

    def test_genuine_0180_session_and_verified_relay_records_read_unchanged(self):
        destination = self.case.root / "previous"
        destination.mkdir()
        result = self.earlier('''import json, sys
from pathlib import Path
from pod import __version__
from pod.governor import _record_path
from pod.ledger import objective_root
from tests.test_relayed_authorization import RelayCase
assert __version__ == "0.18.0"
case = RelayCase(); case.setUp()
try:
    case.verdict()
    target = Path(sys.argv[1])
    target.joinpath("context.json").write_bytes(objective_root(case.project, "objective").joinpath("context.json").read_bytes())
    target.joinpath("governor.json").write_bytes(_record_path(case.project, "objective").read_bytes())
    print(json.dumps({"version": __version__, "writer": "3ba7c64f34da8471b289e92445a929369020f581"}))
finally:
    case.doCleanups()
''', destination)
        self.assertEqual(result, {"version": "0.18.0", "writer": BASELINE})
        from pod.ledger import _validate_context
        for name, reader in (("context.json", lambda p: _validate_context(json.loads(p.read_bytes()))),
                             ("governor.json", _read_journal)):
            path = destination / name
            before = path.read_bytes()
            self.assertEqual(reader(path), json.loads(before))
            self.assertEqual(path.read_bytes(), before)

    def test_publish_and_merge_verifier_requests_are_byte_identical_to_0180(self):
        # Run both genuine writer and candidate with one literal request and the same frozen fixture.
        payload = self.case.root / "request.json"
        for scope in ("publish", "merge"):
            request = self.request(scope)
            payload.write_text(json.dumps(request))
            earlier = self.earlier('''import json, sys
from pathlib import Path
from pod import __version__
from tests.test_relayed_authorization import RelayCase
assert __version__ == "0.18.0"
case = RelayCase(); case.setUp()
try:
    request = json.loads(Path(sys.argv[1]).read_text())
    request["candidate"] = case.case.observed["commit"]
    request["authorization"].update(candidate=case.case.observed["commit"], tree=case.case.observed["tree"])
    case.case.op("governor", action=request)
    print(json.dumps(case.requests.read_text()))
finally:
    case.doCleanups()
''', payload)
            # Candidate identity is deliberately replaced with the same ids for this byte comparison;
            # all other request facts and serialization are untouched.
            previous = json.loads(earlier)
            request["authorization"].update(candidate=previous["candidate"]["commit"], tree=previous["candidate"]["tree"])
            from pod.governor import _verify_relay
            previous_binding = {"commit": previous["candidate"]["commit"], "tree": previous["candidate"]["tree"]}
            self.requests.unlink(missing_ok=True)
            self.control(changes={"single_use": "comparison-" + scope})
            _verify_relay(self.project, "objective", request["authorization"], previous_binding,
                          self.journal()["units"]["delivery"], self.command, pr=request.get("pr"))
            self.assertEqual(self.requests.read_text(), earlier)
