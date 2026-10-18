"""A430/A434-A436: one event-time oracle over permutations of relay arrivals."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import permutations
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

    def owner_event(self, event, arrival, key, relayed):
        kind, decided, deploy = event
        decision = {**DECISION, **({"widening": ["deploy"]} if deploy else {})}
        if relayed:
            decision.update(channel="relayed", relay={"reference": key})

        def verify(command, request):
            # The existing verifier-process tests own authentication and exact
            # binding. Here its injected result leaves all ordering, receipts,
            # validation, context writes and Governor checks in production code.
            return {**deepcopy(request), "verified": True, "owner_source": "authenticated-owner:fixture",
                    "decided_at": self.instant(decided).isoformat(), "single_use": key}

        before, receipts = self.saved(), self.receipts()
        with patch("pod.relay.verify", side_effect=verify):
            try:
                standing.operate({"project": str(self.project), "objective": "objective", "owner": "owner",
                                  "action": kind, "decision": decision}, now=self.instant(arrival))
            except PodError as exc:
                self.assertIn(exc.code, ("standing_authorization_stale", "standing_authorization_future", "relay_authorization_reused"))
                self.assertEqual(self.saved(), before, "refused event wrote context or journal")
                self.assertEqual(self.receipts(), receipts, "refused event consumed its key")

    def pod_event(self, kind):
        if kind == "close":
            self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        elif kind == "reopen":
            self.case.write([self.case.criterion()], reopen=True, revision_authority=DECISION)
        else:
            added = {"id": "O2", "kind": "criterion", "provenance": "user_direct",
                     "source": {"instruction": "Add PoD#2", "ref": "PoD#2"}, "check": "PoD#2 passes",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
            self.case.write([*self.case.stored(), added], criteria=["PoD#1", "PoD#2"], revision_authority=DECISION)

    def row(self, case):
        name, order = case
        with self.subTest(scenario=name, arrivals=order):
            self.reset()
            local, relays = SCENARIOS[name]
            arrivals = [(event[1], event, f"session-{index}", False) for index, event in enumerate(local)]
            aliases = {2: 0} if name == "duplicate_activation" else {2: 1} if name == "duplicate_revocation" else {}
            arrivals += [(10 + index, relays[item], f"relay-{aliases.get(item, item)}", True) for index, item in enumerate(order)]
            events = []
            for arrival, event, key, relayed in sorted(arrivals):
                with patch("pod.standing._clock", return_value=self.instant(arrival)):
                    if event[0] in ("activate", "revoke"):
                        self.owner_event(event, arrival, key, relayed)
                    else:
                        self.pod_event(event[0])
                if not (relayed and event[0] == "activate" and event[1] > arrival):
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


class StandingOracleInventoryTests(unittest.TestCase):
    def test_every_arrival_order_runs_once_in_bounded_named_groups(self):
        assert_grouped_table(self, StandingArrivalOracleTests, CASES, limit=8)
        self.assertEqual(len(ORDERS), 24)
        self.assertEqual(len(CASES), 456)
