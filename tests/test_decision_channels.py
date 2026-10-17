"""R120: every accepted owner decision records its honest delivery channel."""

from copy import deepcopy
import unittest

from pod.errors import PodError
from pod.governor import validate_authorization
from pod.ledger import read
from pod.util import owner_decision, decision_channel, map_decisions, context_decisions
from tests.kernel_support import KernelCase, USER, authorization, git, governance_commit
from tests import test_succession as succession
from tests import test_historical_retirement as retirement
from tests import test_owner_handoff as handoff
from tests import test_runtime_continuity as continuity


RELAY = {"reference": "CE operation op-test; mailbox message msg-test",
         "owner_source": "verified-owner:test-owner"}


class DecisionChannelTests(KernelCase):
    commit = governance_commit

    def test_new_governance_decision_records_default_session_channel(self):
        state = self.intake(revision_authority=USER)["checkpoint"]
        self.assertEqual(state["governance_history"]["decisions"][0]["channel"], "session")

    def test_relayed_merge_authorization_refuses_with_stable_code(self):
        with self.assertRaises(PodError) as caught:
            validate_authorization({**authorization(), "channel": "relayed", "relay": RELAY})
        self.assertEqual(caught.exception.code, "relayed_authorization_refused")

    def test_relayed_snapshot_adoption_records_channel_and_verified_source(self):
        self.intake()
        self.candidate = self.commit("AGENTS.md", "changed policy\n", "policy")
        git(self.project, "update-ref", "refs/remotes/origin/target", self.candidate)
        authority = {**USER, "channel": "relayed", "relay": RELAY}
        result = self.write(self.stored(), governance_refresh=True,
                            governance={"base_ref": "refs/remotes/origin/target", "base": self.candidate},
                            revision_authority=authority)
        decision = result["checkpoint"]["governance_history"]["decisions"][-1]
        self.assertEqual((decision["channel"], decision["relay"]), ("relayed", RELAY))
        self.assertEqual(result["report"]["governance_decisions"][-1], decision)
        self.assertEqual(read(self.project, "objective")["decision_channels"], 1)

    def test_relayed_withdrawal_is_stored_and_reported_with_its_channel(self):
        self.intake()
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="withdrawn", withdrawal={"by": "user_direct", "reason": "owner canceled criterion"})
        result = self.write(rows, close=True, revision_authority={**USER, "channel": "relayed", "relay": RELAY})
        withdrawn = result["checkpoint"]["obligations"][0]["withdrawal"]
        self.assertEqual((withdrawn["channel"], withdrawn["relay"]), ("relayed", RELAY))
        report = result["checkpoint"]["closure"]["report"]
        self.assertEqual(report["withdrawn"][0]["channel"], "relayed")
        self.assertTrue(all(row["decision"]["channel"] == "relayed" for row in report["owner_decisions"]))

    def test_relayed_general_revision_requires_previously_recorded_question(self):
        self.intake(questions=[{"id": "owner-question", "text": "Add criterion?"}])
        authority = {**USER, "channel": "relayed", "relay": RELAY}
        new = {"id": "U", "kind": "criterion", "provenance": "user_direct", "check": "new decision",
               "source": {"instruction": "Add criterion"}, "state": "waiting",
               "wait": {"class": "sequenced", "referent": "O1"}}
        self.refused("relayed_decision_refused", None, self.write, [*self.stored(), new], revision_authority=authority)
        self.refused("decision_question_unrecorded", None, self.write, [*self.stored(), new],
                     revision_authority={**authority, "question": "missing"})
        result = self.write([*self.stored(), new], revision_authority={**authority, "question": "owner-question"})
        decision = result["checkpoint"]["revision_decisions"][-1]
        self.assertEqual((decision["channel"], decision["question"]), ("relayed", "owner-question"))
        self.assertEqual(result["checkpoint"]["obligations"][-1]["source"]["relay"], RELAY)

    def test_session_revision_obligation_and_reopen_records(self):
        self.intake()
        user = {"id": "U", "kind": "criterion", "provenance": "user_direct", "check": "new decision",
                "source": {"instruction": "Add criterion"}, "state": "withdrawn",
                "withdrawal": {"by": "user_direct", "reason": "scoped withdrawal"}}
        rows = [self.criterion("satisfied", evidence=[self.proof("O1")]), user]
        closed = self.write(rows, close=True, revision_authority=USER)["checkpoint"]
        self.assertTrue(all(row["decision"]["channel"] == "session" for row in map_decisions(closed)))
        reopened = self.write(self.stored(), reopen=True, revision_authority=USER)["checkpoint"]
        self.assertEqual(reopened["reopened"][-1]["channel"], "session")

    def test_constraints_and_governor_exceptions_require_session(self):
        from pod.ledger import constraints_update
        from pod.governor import _validate_exception
        self.intake()
        constraint = {"id": "C", "kind": "max_workers", "provenance": "user_direct", "value": 1}
        result = constraints_update(self.project, "objective", owner="owner", action="add", value=constraint)
        self.assertEqual(result["constraints"][0]["channel"], "session")
        with self.assertRaises(PodError) as caught:
            constraints_update(self.project, "objective", owner="owner", action="add",
                               value={**constraint, "id": "relay", "channel": "relayed", "relay": RELAY})
        self.assertEqual(caught.exception.code, "relayed_decision_refused")
        exception = {"grant": "efficiency-1", "reason": "scoped owner exception", "by": "owner"}
        self.assertEqual(_validate_exception(exception)["channel"], "session")
        with self.assertRaises(PodError) as caught:
            _validate_exception({**exception, "channel": "relayed", "relay": RELAY})
        self.assertEqual(caught.exception.code, "relayed_decision_refused")

    def test_user_route_failure_clear_records_channel(self):
        from pod.ledger import route_failure
        self.intake(self.sub("S"))
        admission = self.start("route-test", self.packet(["S"]))["admission"]
        self.settle(admission)
        fields = {"owner": "owner", "admission_id": admission["admission_id"], "kind": "auth_failed", "source": "fixture",
                  "native_reader": lambda row: self.port.read_native("owner", authority_runs=(row["run_id"],), assignments=(row,))}
        route_failure(self.project, "objective", **fields)
        with self.assertRaises(PodError) as caught:
            route_failure(self.project, "objective", **fields, clear=True, cleared_by="user",
                          decision={**USER, "channel": "relayed", "relay": RELAY})
        self.assertEqual(caught.exception.code, "relayed_decision_refused")
        with self.assertRaises(PodError):
            route_failure(self.project, "objective", **fields, clear=True, cleared_by="user", decision=[])
        route_failure(self.project, "objective", **fields, clear=True, cleared_by="user")
        failure = read(self.project, "objective")["admissions"][admission["admission_id"]]["failures"][0]
        self.assertEqual(failure["clear_decision"]["channel"], "session")


class SharedDecisionTests(unittest.TestCase):
    def test_complete_relays_are_accepted_only_for_the_exhaustive_kind_list(self):
        for kind in ("question", "governance", "withdrawal", "claim", "retirement"):
            with self.subTest(kind=kind):
                self.assertEqual(owner_decision({**USER, "channel": "relayed", "relay": RELAY}, kind=kind)["relay"], RELAY)
        for kind in ("handoff", "continuity", "continuation", "constraint", "reopen", "revision", "compaction", "governor_exception"):
            with self.subTest(kind=kind):
                with self.assertRaises(PodError) as caught:
                    owner_decision({**USER, "channel": "relayed", "relay": RELAY}, kind=kind)
                self.assertEqual(caught.exception.code, "relayed_decision_refused")

    def test_incomplete_or_misclassified_relays_refuse(self):
        for value in ({**USER, "channel": "relayed"}, {**USER, "channel": "session", "relay": RELAY},
                      {**USER, "channel": "relayed", "relay": {}},
                      {**USER, "channel": "relayed", "relay": {"reference": "message"}},
                      {**USER, "channel": "relayed", "relay": {"reference": "message", "owner_source": ""}},
                      {**USER, "channel": "relayed", "relay": {"reference": "a" * 513, "owner_source": "verified"}},
                      {**USER, "channel": "mailbox"}):
            with self.subTest(value=value), self.assertRaises(PodError):
                owner_decision(value, kind="governance")

    def test_unverified_authorization_never_grants_authority(self):
        from pod.governor import _check_authority
        for scope in ("publish", "merge", "release", "deploy"):
            with self.subTest(scope=scope):
                self.assertEqual(validate_authorization(authorization(scope=(scope,)))["channel"], "session")
                with self.assertRaises(PodError) as caught:
                    validate_authorization({**authorization(scope=(scope,)), "channel": "relayed", "relay": RELAY})
                self.assertEqual(caught.exception.code, "relayed_authorization_refused")
                supplied = {**authorization(scope=(scope,)), "channel": "relayed", "relay": RELAY}
                action = {"kind": "push" if scope == "publish" else scope,
                          "candidate": supplied["candidate"], "authorization": supplied}
                binding = {key: supplied[key] for key in ("tree",)} | {"commit": supplied["candidate"]}
                with self.assertRaises(PodError) as boundary:
                    _check_authority(action, binding, False, False, [])
                self.assertEqual(boundary.exception.code, "relayed_authorization_refused")

    def test_earlier_session_consent_cannot_become_fresh_by_default_channel(self):
        from pod.governor import _check_carried_consent
        # Synthetic earlier-authorization control: the envelope predates channel recording.
        earlier = authorization(scope=("publish",))
        consent = validate_authorization(earlier)
        journal = {"actions": [{"action": {"authorization": earlier},
                                "carried_from": {"objective": "predecessor"}}]}
        reasons = []
        _check_carried_consent({"authorization": consent}, journal, reasons)
        self.assertEqual([row["code"] for row in reasons], ["authorization_missing"])
        self.assertNotIn("channel", earlier)

    def test_legacy_projection_never_rewrites_the_record(self):
        stored = {**USER}
        before = deepcopy(stored)
        self.assertEqual(owner_decision(stored, kind="governance", stored=True), before)
        self.assertEqual(decision_channel(stored)["channel"], "session")
        self.assertFalse(decision_channel(stored)["channel_recorded"])
        self.assertEqual(stored, before)

    def test_eighty_owner_authorized_writes_have_no_revision_count_cap(self):
        from pod.obligations import accept_write, validate_map_record
        from tests.test_obligations import ctx, intake, rows_of, write
        state = accept_write(None, intake(revision_authority=USER), ctx())
        for index in range(1, 80):
            state = accept_write(state, write(state, rows_of(state),
                                 revision_authority={**USER, "instruction": f"Owner revision {index}"}), ctx())
            validate_map_record(state)
        self.assertEqual(len(state["revision_decisions"]), 80)
        self.assertEqual([row["seq"] for row in state["revision_decisions"]], list(range(1, 81)))


class PublicAuthorizationChannelTests(unittest.TestCase):
    """Use all three public operations; refused input must never reserve an effect."""

    def setUp(self):
        from tests.test_finalization import ResumeTests
        self.case = ResumeTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def check_authorizations(self, operation, *, merge=False):
        from pod.governor import _record_path
        from tests.kernel_support import action
        case = self.case
        path = _record_path(case.project, "objective")
        before = path.read_bytes()
        calls = deepcopy(case.remote.calls)
        shapes = (
            ({"channel": "relayed", "relay": RELAY}, "relay_verifier_unconfigured"),
            ({"channel": "relayed"}, "invalid_decision_relay"),
            ({"channel": "relayed", "relay": {}}, "invalid_decision_relay"),
            ({"channel": "relayed", "relay": {"reference": "message"}}, "relay_verifier_unconfigured"),
            ({"channel": "relayed", "relay": {"reference": "message", "owner_source": ""}}, "invalid_decision_relay"),
            ({"channel": "relayed", "relay": {"reference": "r" * 513, "owner_source": "verified"}}, "invalid_decision_relay"),
            ({"channel": "session", "relay": RELAY}, "invalid_decision_relay"),
            ({"channel": "relay"}, "invalid_decision_channel"),
        )
        for fields, code in shapes:
            with self.subTest(fields=fields):
                request = (case.merge() if merge else action(candidate=case.observed["commit"],
                    target="origin/candidate", unit="delivery", effects=["workflow:ci.yml"],
                    authorization=case.consent("publish")))
                request["authorization"].update(fields)
                request["authorization"]["target"] = "origin/target"
                if merge:
                    request["authorization"]["pr"] = 23
                with self.assertRaises(PodError) as caught:
                    case.op(operation, action=request)
                self.assertEqual(caught.exception.code, code)
                self.assertIn("session", caught.exception.detail["next_action"])
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(case.remote.calls, calls)

    def test_internal_governor_merge_refuses_relay_without_journal_write(self):
        self.check_authorizations("governor", merge=True)

    def test_internal_governor_publish_refuses_relay_without_journal_write(self):
        self.check_authorizations("governor")

    def test_governor_execute_publish_refuses_relay_without_journal_write(self):
        self.check_authorizations("governor-execute")


class ClaimChannelTests(succession.SuccessionCase):
    def test_relayed_claim_records_source_and_status_channel(self):
        self.predecessor()
        self.successor()
        result = self.claim(self.decide(channel="relayed", relay=RELAY))
        self.assertEqual(result["continued_by"]["relay"], RELAY)
        self.assertEqual(context_decisions(self.state())[0]["decision"]["channel"], "relayed")
        status = self.status()
        self.assertEqual([row for row in status["owner_decisions"] if row["kind"] == "claim"][0]["decision"]["channel"], "relayed")
        self.assertIn("Decision claim: relayed", self.status(text=True))


class RetirementChannelTests(retirement.RetirementCase):
    def test_relayed_retirement_records_source_and_status_channel(self):
        self.delivered()
        result = self.retire(self.decide(channel="relayed", relay=RELAY))
        self.assertEqual(result["objectives"][0]["retirement"]["channel"], "relayed")
        self.assertEqual(self.state()["historical_retirement"]["relay"], RELAY)
        self.assertIn("Decision retirement: relayed", self.text_status())

    def test_earlier_retirement_replays_explicit_default_session_only(self):
        """Synthetic earlier entry: the same digest rule before channels existed."""
        from pod.ledger import objective_root
        from pod.util import digest
        import json
        self.delivered()
        decision = self.decide()
        self.assertEqual(self.retire(decision)["status"], "retired")
        path = objective_root(self.project, "objective") / "context.json"
        state = json.loads(path.read_bytes())
        state["historical_retirement"].pop("channel")
        state["historical_retirement"]["decision"] = digest(decision)
        state.pop("decision_channels", None)
        path.write_text(json.dumps(state))
        before = self.snapshot()
        self.assertEqual(self.retire({**decision, "channel": "session"})["objectives"][0]["status"], "replayed")
        for changes in ({"instruction": "Different decision"}, {"channel": "relayed", "relay": RELAY}):
            refused = self.retire({**decision, **changes})["objectives"][0]
            self.assertEqual((refused["status"], refused["condition"]), ("refused", "objective_retired"))
        self.assertEqual(self.snapshot(), before)


class HandoffChannelTests(handoff.HandoffCase):
    def test_owner_handoff_requires_session_and_records_it(self):
        self.establish(admissions=False)
        self.lose()
        decision = self.decision()
        with self.assertRaises(PodError) as caught:
            self.handoff({**decision, "channel": "relayed", "relay": RELAY})
        self.assertEqual(caught.exception.code, "relayed_decision_refused")
        self.handoff(decision)
        self.assertEqual(self.entry()["decision"]["channel"], "session")


class ContinuityChannelTests(continuity.ContinuityCase):
    def test_runtime_continuity_requires_session_and_records_it(self):
        self.port.reports_binding = False
        self.started()
        self.port.reports_binding = True
        self.change_runtime()
        error = self.refused(continuity.AMBIGUOUS, self.checkpoint_op)
        with self.assertRaises(PodError) as caught:
            self.owner_op(self.decision(error, channel="relayed", relay=RELAY))
        self.assertEqual(caught.exception.code, "relayed_decision_refused")
        result = self.owner_op(self.decision(error))
        self.assertEqual(result["runtime_continuity"]["decision"]["channel"], "session")
