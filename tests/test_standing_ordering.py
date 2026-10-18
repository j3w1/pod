"""A430/A434-A436: one event-time oracle over permutations of relay arrivals."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import permutations
import hashlib
import json
import unittest
from unittest.mock import patch

from pod import standing
from pod.errors import PodError
from pod.governor import _record_path
from pod.ledger import _path, read, state_root
from tests.common import GroupedTable, assert_grouped_table
from tests.test_standing_authorization import DECISION, StandingCase


# (kind, decision time, deploy widening). Pod/session events arrive at their
# decision time. The four relays arrive at 10, 11, 12, 13 in every order.
RELAYS = (("activate", 1, False), ("revoke", 2, False),
          ("activate", 3, True), ("revoke", 4, False))
ON = (("activate", 0, False),)
SCENARIOS = {
    "four_relays": ((), RELAYS),
    "stop_while_none": ((("revoke", 5, False),), RELAYS),
    "stop_between_arrivals": ((("revoke", 11.5, False),), RELAYS),
    "stop_while_ended": (ON + (("close", 2, False), ("reopen", 4, False), ("revoke", 6, False)),
                          (("activate", 1, False), ("revoke", 3, False),
                           ("activate", 5, True), ("revoke", 5.5, False))),
    "scope_before_arrivals": (ON + (("scope", 5, False),), RELAYS),
    "scope_between_arrivals": (ON + (("scope", 11.5, False),), RELAYS),
    "lifecycle_before_arrivals": (ON + (("close", 5, False), ("reopen", 6, False)), RELAYS),
    "lifecycle_between_arrivals": (ON + (("close", 10.5, False), ("reopen", 10.75, False)), RELAYS),
    "newer_after_scope": (ON + (("scope", 5, False),),
                          (("activate", 4, False), ("revoke", 6, False),
                           ("activate", 9, True), ("revoke", 8, False))),
    "newer_after_lifecycle": (ON + (("close", 5, False), ("reopen", 6, False)),
                              (("activate", 4, False), ("revoke", 7, False),
                               ("activate", 9, True), ("revoke", 8, False))),
    "session_activation_between": ((("revoke", 5, False), ("activate", 11.5, False)), RELAYS),
    "stop_tie": ((), (("activate", 1, False), ("revoke", 2, False),
                       ("activate", 4, True), ("revoke", 4, False))),
    "scope_tie": (ON + (("scope", 5, False),),
                  (("activate", 1, False), ("revoke", 2, False),
                   ("activate", 5, True), ("revoke", 4, False))),
    "reopen_tie": (ON + (("close", 5, False), ("reopen", 6, False)),
                   (("activate", 1, False), ("revoke", 2, False),
                    ("activate", 6, True), ("revoke", 4, False))),
    "future_verdicts": ((), (("activate", 1, False), ("revoke", 20, False),
                              ("activate", 20, True), ("revoke", 4, False))),
    "future_after_stop": (ON + (("revoke", 5, False),),
                          (("activate", 1, False), ("revoke", 2, False),
                           ("activate", 20, True), ("revoke", 4, False))),
    "future_stop_session": (ON + (("activate", 11.5, False),),
                            (("activate", 1, False), ("revoke", 20, False),
                             ("activate", 3, True), ("revoke", 4, False))),
    "duplicate_activation": ((), (("activate", 1, True), ("revoke", 2, False),
                                  ("activate", 1, True), ("revoke", 4, False))),
    "duplicate_revocation": ((), (("activate", 1, False), ("revoke", 2, False),
                                  ("revoke", 2, False), ("activate", 3, True))),
    "scope_never_on": ((("scope", 5, False),),
                         (("activate", 1, False), ("revoke", 2, False), ("activate", 3, True), ("activate", 4, False))),
    "stop_while_none_then_scope": ((("revoke", 0.5, False), ("scope", 5, False)),
                                    (("activate", 1, False), ("revoke", 2, False), ("activate", 3, True), ("activate", 4, False))),
    "scope_twice_relays_between": (ON + (("scope", 2, False), ("scope3", 4, False)),
                                    (("activate", 3, True), ("revoke", 3.2, False), ("activate", 3.4, False), ("revoke", 1, False))),
    "scope_twice_relays_after": (ON + (("scope", 2, False), ("scope3", 4, False)),
                                 (("activate", 5, True), ("revoke", 6, False), ("activate", 7, False), ("revoke", 1, False))),
    "future_retry_after_stop": (ON + (("revoke", 11.5, False),),
                                (("activate", 12, True), ("revoke", 1, False), ("activate", 12, True), ("revoke", 2, False))),
    "unverifiable_stop_while_on": (ON + (("failed_stop", 5, False),),
                                   (("activate", 1, False), ("activate", 3, True), ("activate", 4, True), ("revoke", -1, False))),

    "scope_restore_holder_old": (ON + (("scope", 2, False), ("restore", 4, False)),
                                   (("activate", 1, False), ("revoke", -1, False), ("activate", 1.5, True), ("revoke", -2, False))),
    "scope_restore_holder_new": (ON + (("scope", 2, False), ("restore", 4, False)),
                                   (("activate", 1, False), ("revoke", -1, False), ("activate", 5, True), ("activate", 7, False))),
    "scope_restore_never_on": ((("scope", 2, False), ("restore", 4, False)),
                                 (("activate", 1, False), ("revoke", -1, False), ("activate", 1.5, True), ("revoke", -2, False))),
    "holder_BCB": (ON + (("scope", 2, False), ("scope3", 4, False), ("restore2", 5, False)),
                     (("activate", 3, False), ("revoke", -1, False), ("activate", 3.5, True), ("revoke", -2, False))),
    "holder_revoked_BCB": (ON + (("revoke", 1, False), ("scope", 2, False), ("scope3", 4, False), ("restore2", 5, False)),
                             (("activate", 3, False), ("revoke", -1, False), ("activate", 3.5, True), ("revoke", -2, False))),
    "stop_none_ABA": ((("revoke", 0.5, False), ("scope", 2, False), ("restore", 4, False)),
                        (("activate", 1, False), ("revoke", -1, False), ("activate", 1.5, True), ("revoke", -2, False))),
    "control_reconfirmed_BCB": (ON + (("scope", 2, False), ("activate", 2.5, False), ("scope3", 4, False), ("restore2", 5, False)),
                                 (("activate", 3, False), ("revoke", -1, False), ("activate", 3.5, True), ("revoke", -2, False))),
}
ORDERS = tuple(permutations(range(4)))
CASES = tuple((name, order) for name in SCENARIOS for order in ORDERS)


class StandingArrivalOracleTests(StandingCase, GroupedTable):
    MOMENT = datetime(2030, 1, 1, tzinfo=timezone.utc)
    GROUPS = {f"test_{name}_{group + 1}": tuple((name, order) for order in ORDERS[group * 8:(group + 1) * 8])
              for name in SCENARIOS for group in range(3)}

    def setUp(self):
        super().setUp()
        from pod.github import _read_repository_context
        identity = _read_repository_context(self.project.resolve())
        # No event in this table changes Git identity. Resolve that expensive
        # fixture once; authority and event-time reads still run on every event.
        discovery = patch("pod.github._read_repository_context", return_value=identity)
        discovery.start(); self.addCleanup(discovery.stop)
        self.initial = self.saved()

    def reset(self):
        # Each order starts from the same disposable objective and empty journal.
        _path(self.project, "objective").write_bytes(self.initial[0])
        _record_path(self.project, "objective").write_bytes(self.initial[1])
        for path in (state_root(self.project) / ".relay-authorizations").glob("*.json"):
            path.unlink()

    def instant(self, seconds):
        return self.MOMENT + timedelta(seconds=seconds)

    def owner_event(self, event, arrival, key, relayed, decision_scope):
        kind, decided, deploy = event
        failed_stop = kind == "failed_stop"
        kind = "revoke" if failed_stop else kind
        decision = {**DECISION, **({"widening": ["deploy"]} if deploy else {})}
        if relayed:
            decision.update(channel="relayed", relay={"reference": key})

        def output(command, request):
            # The existing verifier-process tests own authentication and exact
            # binding. Here its injected result leaves all ordering, receipts,
            # validation, context writes and Governor checks in production code.
            if failed_stop:
                raise PodError("relay_verifier_failed", "injected verifier failure")
            verdict = {**deepcopy(request), "verified": True, "owner_source": "authenticated-owner:fixture",
                       "decided_at": self.instant(decided).isoformat(), "single_use": key}
            if kind == "activate":
                verdict["standing"]["scope_digest"] = decision_scope
            return json.dumps(verdict).encode()

        before, receipts = self.saved(), self.receipts()
        with patch("pod.relay._output", side_effect=output):
            try:
                standing.operate({"project": str(self.project), "objective": "objective", "owner": "owner",
                                  "action": kind, "decision": decision}, now=self.instant(arrival))
            except PodError as exc:
                self.assertIn(exc.code, ("standing_authorization_stale", "standing_authorization_future", "relay_authorization_reused",
                                         "relay_verifier_mismatch", "relay_verifier_failed"))
                if failed_stop:
                    old = json.loads(before[0]).get("standing_authorization", {})
                    after = read(self.project, "objective")["standing_authorization"]
                    self.assertEqual(after["decisions"], old.get("decisions", []))
                    self.assertEqual(after["not_before"], self.instant(arrival).strftime("%Y-%m-%dT%H:%M:%SZ"))
                    self.assertEqual(self.saved()[1], before[1])
                else:
                    self.assertEqual(self.saved(), before, "refused event wrote context or journal")
                if exc.code == "standing_authorization_future":
                    added = {name: value for name, value in self.receipts().items() if name not in receipts}
                    self.assertEqual(len(added), 1)
                    self.assertEqual(json.loads(next(iter(added.values())))["refused"], "standing_authorization_future")
                    self.assertEqual({name: value for name, value in self.receipts().items() if name in receipts}, receipts)
                else:
                    self.assertEqual(self.receipts(), receipts, "refused event consumed its key")

    def pod_event(self, kind):
        if kind == "close":
            self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        elif kind == "reopen":
            self.case.write([self.case.criterion()], reopen=True, revision_authority=DECISION)
        elif kind in ("restore", "restore2"):
            rows = self.case.stored()
            rows[-1].pop("wait", None)
            rows[-1].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "restore the original scope"})
            self.case.write(rows, criteria=["PoD#1"] if kind == "restore" else ["PoD#1", "PoD#2"], revision_authority=DECISION)
        else:
            number = 3 if kind == "scope3" else 2
            added = {"id": f"O{number}", "kind": "criterion", "provenance": "user_direct",
                     "source": {"instruction": f"Add PoD#{number}", "ref": f"PoD#{number}"}, "check": f"PoD#{number} passes",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
            self.case.write([*self.case.stored(), added], criteria=[f"PoD#{index}" for index in range(1, number + 1)], revision_authority=DECISION)

    def row(self, case):
        name, order = case
        with self.subTest(scenario=name, arrivals=order):
            self.reset()
            local, relays = SCENARIOS[name]
            arrivals = [(event[1], event, f"session-{index}", False) for index, event in enumerate(local)]
            aliases = {2: 0} if name in ("duplicate_activation", "future_retry_after_stop") else {2: 1} if name == "duplicate_revocation" else {}
            arrivals += [(10 + index, relays[item], f"relay-{aliases.get(item, item)}", True) for index, item in enumerate(order)]
            events, refused_future_keys = [], set()
            record_exists = False

            def scope_at(moment, *, decision=False):
                # Declarative direct-objective scope, independent of the kernel's
                # digest/observation helpers and of which inputs it accepts.
                # A scope change wins a decision-time tie, so the tied grant
                # still names the scope preceding that change.
                count = 1
                for kind, time, _ in sorted(local, key=lambda item: item[1]):
                    if time < moment if decision else time <= moment:
                        if kind.startswith("scope"):
                            count = 3 if kind == "scope3" else 2
                        elif kind == "restore":
                            count = 1
                        elif kind == "restore2":
                            count = 2
                criteria = [f"PoD#{index}" for index in range(1, count + 1)]
                return hashlib.sha256(json.dumps({"criteria": criteria}, sort_keys=True,
                                                separators=(",", ":")).encode()).hexdigest()

            for arrival, event, key, relayed in sorted(arrivals):
                with patch("pod.standing._clock", return_value=self.instant(arrival)):
                    if event[0] in ("activate", "revoke", "failed_stop"):
                        self.owner_event(event, arrival, key, relayed or event[0] == "failed_stop", scope_at(event[1], decision=True))
                    else:
                        self.pod_event(event[0])
                if relayed and event[0] == "activate":
                    if event[1] > arrival and scope_at(event[1], decision=True) == scope_at(arrival):
                        refused_future_keys.add(key)
                    # An activation counts only for the scope current when the
                    # owner decided, and a refused future key never counts later.
                    failed_stops = [item[1] for item in local if item[0] == "failed_stop" and item[1] <= arrival]
                    if (key in refused_future_keys or scope_at(event[1], decision=True) != scope_at(arrival)
                            or any(event[1] <= time for time in failed_stops)):
                        continue
                # Only a genuinely never-record objective lacks the history of
                # intermediate revisions. A stop creates a record without a grant.
                if event[0] in ("scope", "scope3", "restore", "restore2") and not record_exists:
                    continue
                if event[0] in ("activate", "revoke", "failed_stop"):
                    record_exists = True
                if event[0] != "failed_stop":
                    events.append((event[0], min(event[1], arrival) if relayed else event[1], event[2]))
            # Independent declarative oracle: the latest decision/event wins;
            # every non-activation beats an activation at the same instant.
            latest = max(events, key=lambda item: (item[1], item[0] != "activate"))
            active = latest[0] == "activate"
            context = read(self.project, "objective")
            self.assertEqual(standing.state(context, now=self.instant(14)) == "active", active, latest)
            for kind in ("push", "deploy"):
                result = self.case.op("governor", action=self.proposed(kind))
                self.assertEqual(result["decision"], "ALLOW" if active and (kind == "push" or latest[2]) else "DEFER",
                                 (latest, kind, result))


    def test_four_relays_1(self):
        self.rows()

    def test_four_relays_2(self):
        self.rows()

    def test_four_relays_3(self):
        self.rows()

    def test_stop_while_none_1(self):
        self.rows()

    def test_stop_while_none_2(self):
        self.rows()

    def test_stop_while_none_3(self):
        self.rows()

    def test_stop_between_arrivals_1(self):
        self.rows()

    def test_stop_between_arrivals_2(self):
        self.rows()

    def test_stop_between_arrivals_3(self):
        self.rows()

    def test_stop_while_ended_1(self):
        self.rows()

    def test_stop_while_ended_2(self):
        self.rows()

    def test_stop_while_ended_3(self):
        self.rows()

    def test_scope_before_arrivals_1(self):
        self.rows()

    def test_scope_before_arrivals_2(self):
        self.rows()

    def test_scope_before_arrivals_3(self):
        self.rows()

    def test_scope_between_arrivals_1(self):
        self.rows()

    def test_scope_between_arrivals_2(self):
        self.rows()

    def test_scope_between_arrivals_3(self):
        self.rows()

    def test_lifecycle_before_arrivals_1(self):
        self.rows()

    def test_lifecycle_before_arrivals_2(self):
        self.rows()

    def test_lifecycle_before_arrivals_3(self):
        self.rows()

    def test_lifecycle_between_arrivals_1(self):
        self.rows()

    def test_lifecycle_between_arrivals_2(self):
        self.rows()

    def test_lifecycle_between_arrivals_3(self):
        self.rows()

    def test_newer_after_scope_1(self):
        self.rows()

    def test_newer_after_scope_2(self):
        self.rows()

    def test_newer_after_scope_3(self):
        self.rows()

    def test_newer_after_lifecycle_1(self):
        self.rows()

    def test_newer_after_lifecycle_2(self):
        self.rows()

    def test_newer_after_lifecycle_3(self):
        self.rows()

    def test_session_activation_between_1(self):
        self.rows()

    def test_session_activation_between_2(self):
        self.rows()

    def test_session_activation_between_3(self):
        self.rows()

    def test_stop_tie_1(self):
        self.rows()

    def test_stop_tie_2(self):
        self.rows()

    def test_stop_tie_3(self):
        self.rows()

    def test_scope_tie_1(self):
        self.rows()

    def test_scope_tie_2(self):
        self.rows()

    def test_scope_tie_3(self):
        self.rows()

    def test_reopen_tie_1(self):
        self.rows()

    def test_reopen_tie_2(self):
        self.rows()

    def test_reopen_tie_3(self):
        self.rows()

    def test_future_verdicts_1(self):
        self.rows()

    def test_future_verdicts_2(self):
        self.rows()

    def test_future_verdicts_3(self):
        self.rows()


    def test_future_after_stop_1(self):
        self.rows()

    def test_future_after_stop_2(self):
        self.rows()

    def test_future_after_stop_3(self):
        self.rows()

    def test_future_stop_session_1(self):
        self.rows()

    def test_future_stop_session_2(self):
        self.rows()

    def test_future_stop_session_3(self):
        self.rows()

    def test_duplicate_activation_1(self):
        self.rows()

    def test_duplicate_activation_2(self):
        self.rows()

    def test_duplicate_activation_3(self):
        self.rows()


    def test_duplicate_revocation_1(self):
        self.rows()

    def test_duplicate_revocation_2(self):
        self.rows()

    def test_duplicate_revocation_3(self):
        self.rows()

    def test_scope_never_on_1(self):
        self.rows()

    def test_scope_never_on_2(self):
        self.rows()

    def test_scope_never_on_3(self):
        self.rows()

    def test_stop_while_none_then_scope_1(self):
        self.rows()

    def test_stop_while_none_then_scope_2(self):
        self.rows()

    def test_stop_while_none_then_scope_3(self):
        self.rows()

    def test_scope_twice_relays_between_1(self):
        self.rows()

    def test_scope_twice_relays_between_2(self):
        self.rows()

    def test_scope_twice_relays_between_3(self):
        self.rows()

    def test_scope_twice_relays_after_1(self):
        self.rows()

    def test_scope_twice_relays_after_2(self):
        self.rows()

    def test_scope_twice_relays_after_3(self):
        self.rows()

    def test_future_retry_after_stop_1(self):
        self.rows()

    def test_future_retry_after_stop_2(self):
        self.rows()

    def test_future_retry_after_stop_3(self):
        self.rows()

    def test_unverifiable_stop_while_on_1(self):
        self.rows()

    def test_unverifiable_stop_while_on_2(self):
        self.rows()

    def test_unverifiable_stop_while_on_3(self):
        self.rows()

    def test_scope_restore_holder_old_1(self):
        self.rows()

    def test_scope_restore_holder_old_2(self):
        self.rows()

    def test_scope_restore_holder_old_3(self):
        self.rows()

    def test_scope_restore_holder_new_1(self):
        self.rows()

    def test_scope_restore_holder_new_2(self):
        self.rows()

    def test_scope_restore_holder_new_3(self):
        self.rows()

    def test_scope_restore_never_on_1(self):
        self.rows()

    def test_scope_restore_never_on_2(self):
        self.rows()

    def test_scope_restore_never_on_3(self):
        self.rows()

    def test_holder_BCB_1(self):
        self.rows()

    def test_holder_BCB_2(self):
        self.rows()

    def test_holder_BCB_3(self):
        self.rows()

    def test_holder_revoked_BCB_1(self):
        self.rows()

    def test_holder_revoked_BCB_2(self):
        self.rows()

    def test_holder_revoked_BCB_3(self):
        self.rows()

    def test_stop_none_ABA_1(self):
        self.rows()

    def test_stop_none_ABA_2(self):
        self.rows()

    def test_stop_none_ABA_3(self):
        self.rows()

    def test_control_reconfirmed_BCB_1(self):
        self.rows()

    def test_control_reconfirmed_BCB_2(self):
        self.rows()

    def test_control_reconfirmed_BCB_3(self):
        self.rows()


class StandingOracleInventoryTests(unittest.TestCase):
    def test_every_arrival_order_runs_once_in_bounded_named_groups(self):
        assert_grouped_table(self, StandingArrivalOracleTests, CASES, limit=8)
        self.assertEqual(len(ORDERS), 24)
        self.assertEqual(len(CASES), 768)
