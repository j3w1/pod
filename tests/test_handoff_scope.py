"""Records outside the documented layouts hold handoff scope only when they could name the Run (R206)."""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from pod.errors import PodError
from pod.handoff import MAX_CHECKOUTS, _unproven_reason, peer_label, run_scope, shared_detail
from pod.internal import run
from pod.github import repository_context
from pod.ledger import objective_root, read
from tests.common import GroupedTable, assert_grouped_table
from tests.kernel_support import git
from tests.test_owner_handoff import HandoffCase, SENTINEL, logical_fixture
from tests.test_window_continuation import WindowCase

REPOSITORY = "acme/example"
CONSUMER = "acme/consumer"


def older_record(**checkpoint):
    """The hand-written shape of an earlier writer's record, as the cutover tests use it."""
    value = {"schema": "pod-checkpoint/v2", "objective": "earlier",
             "native_refs": [{"runId": "elsewhere", "runtime": "rt"}],
             "next_safe_action": "continue the earlier work"}
    value.update(checkpoint)
    return {"schema": "pod-context/v4", "revision": 1, "owner": "former", "checkpoint": value,
            "admissions": {}, "interventions": {}, "source_rejections": {}, "constraints": []}


CASES = {
    # kind: (record, expected condition; None means the handoff is available)
    "older-foreign": (older_record(), None),
    "older-mentions-run": (older_record(next_safe_action="select run again"), "scope_unreadable"),
    "older-referenced": (older_record(native_refs=[{"runId": "run", "runtime": "rt"}]), "run_shared"),
    # R206: a record that cannot prove non-membership holds the Run only when it mentions it.
    "older-reference-unreadable": (older_record(native_refs=[{"runtime": "rt"}]), None),
    "future-foreign": (older_record(schema="pod-checkpoint/v9"), None),
    "unknown-family": (older_record(schema="other-checkpoint/v1"), None),
    "future-mentions-run": (older_record(schema="pod-checkpoint/v9", next_safe_action="select run again"),
                            "scope_unreadable"),
    "empty-record": ("", "scope_unreadable"),  # Undecodable: membership unknown, so it holds.
    "escaped-mention": (json.dumps(older_record(schema="pod-checkpoint/v9", next_safe_action="select run again"))
                        .replace("select run", "select r\\u0075n"), "scope_unreadable"),
    "decodable-foreign": ('["elsewhere"]', None),
    "broken-mentions-run": ('{"checkpoint": {"native_refs": [{"runId": "run"', "scope_unreadable"),
}


class OlderSchemaScopeTests(GroupedTable):
    """Each record shape, one named group method per few rows so suite lanes can spread them."""
    GROUPS = {"test_older_records": ["older-foreign", "older-mentions-run", "older-referenced"],
              "test_unproved_layouts": ["older-reference-unreadable", "future-foreign", "unknown-family"],
              "test_records_that_could_name_the_run": ["future-mentions-run", "empty-record", "broken-mentions-run"],
              "test_decoded_membership": ["escaped-mention", "decodable-foreign"]}

    def row(self, kind):
        record, expected = CASES[kind]
        case = HandoffCase(); case.setUp()
        try:
            with self.subTest(kind=kind):
                case.establish()
                peer = objective_root(case.project, "earlier") / "context.json"
                peer.parent.mkdir(parents=True)
                peer.write_text(record if isinstance(record, str) else json.dumps(record))
                case.lose()
                before = case.snapshot()
                verdict = case.status()["owner_handoff"]
                if expected is None:
                    self.assertEqual(verdict["status"], "handoff_available", verdict)
                    case.handoff()
                    self.assertEqual(case.entry()["state"], "done")
                else:
                    self.assertEqual(verdict["condition"], expected)
                    case.refused(lambda: case.handoff())
                    self.assertEqual(before, case.snapshot())
                    self.assertEqual(case.mutations, [])
                self.assertEqual(peer.read_bytes(), before[str(peer.relative_to(case.root / "state"))])
        finally:
            case.doCleanups()

    def test_older_records(self):
        self.rows()

    def test_unproved_layouts(self):
        self.rows()

    def test_records_that_could_name_the_run(self):
        self.rows()

    def test_decoded_membership(self):
        self.rows()


class OlderSchemaScopeShapeTests(unittest.TestCase):
    def test_the_scope_table_runs_every_row_once(self):
        assert_grouped_table(self, OlderSchemaScopeTests, list(CASES), limit=3)


class UnprovenClosureScopeTests(WindowCase):
    """A closed peer whose closure proof fails still blocks, named apart from open peers with a fixed reason (pod#250)."""

    PREFIX = "Another open or unverified-closed objective records this Run: "

    def peers(self, *, closed=True, opened=False):
        """Real peer records written through the checkpoint boundary, before the coordinator is lost."""
        self.establish(admissions=False)
        if closed:
            self.closed_review_peer()
        if opened:
            run("checkpoint", {"project": str(self.project), "objective": "open-peer", "owner": "owner",
                               "value": self.core(obligations=[self.criterion()],
                                                  governance={"base_ref": "refs/remotes/origin/target"})})

    def rewrite(self, change, objective="closure-peer"):
        path = objective_root(self.project, objective) / "context.json"
        raw = logical_fixture(path)
        change(raw)
        path.write_text(json.dumps(raw))
        return path

    def scope(self, objectives, opened, unproven):
        scope = run_scope(self.project, "objective", "run")
        self.assertEqual(scope["objectives"], objectives)  # The blocking union is exactly as before.
        self.assertEqual((scope["open"], scope["closure_unproven"]), (opened, unproven))
        self.assertNotIn(SENTINEL, json.dumps(scope))
        return scope

    def refusal(self, detail, *, objectives, opened, unproven):
        self.lose()
        verdict = self.status()["owner_handoff"]
        self.assertEqual((verdict["condition"], verdict["definitive"], verdict["other_objectives"]),
                         ("run_shared", True, objectives))
        self.assertEqual(verdict["next_action"], "Resolve the shared objective scope before handoff; "
                                                 "do not run orca orchestration run-use by hand.")
        self.assertEqual(verdict["message"], self.PREFIX + detail + ".")
        self.assertEqual((verdict["open_objectives"], verdict["unproven_closures"]), (opened, unproven))
        self.assertNotIn(SENTINEL, json.dumps(verdict))
        return verdict

    def gone(self, raw):
        """The peer's recorded worktree was removed by completion cleanup."""
        raw["checkpoint"]["worktree"]["path"] = str(self.root / SENTINEL)

    def unproven(self, reason, objective="closure-peer", repository=REPOSITORY):
        return [{"objective": objective, "repository": repository, "reason": reason}]

    def test_a_closed_peer_whose_worktree_was_removed_is_proved_from_its_records(self):
        self.peers()
        self.rewrite(self.gone)
        self.scope([], [], [])
        self.lose()
        self.assertNotEqual(self.status()["owner_handoff"].get("condition"), "run_shared")

    def test_a_removed_worktree_peer_with_a_damaged_closure_still_refuses(self):
        self.peers()
        self.rewrite(lambda raw: (self.gone(raw), raw["checkpoint"]["closure"].update(seq=SENTINEL)))
        unproven = self.unproven("recorded worktree missing; closure proof refused (obligation_invalid)")
        self.scope(["closure-peer"], [], unproven)
        self.refusal("closed with unproven closure: closure-peer in acme/example (recorded worktree missing; "
                     "closure proof refused (obligation_invalid))", objectives=["closure-peer"], opened=[],
                     unproven=unproven)

    def integrity(self, change):
        """Review F1: with the worktree removed, a damaged closure refuses exactly as with the worktree present."""
        self.peers()
        self.rewrite(lambda raw: (self.gone(raw), change(raw["checkpoint"])))
        scope = run_scope(self.project, "objective", "run")
        self.assertEqual(scope["objectives"], ["closure-peer"])
        self.assertRegex(scope["closure_unproven"][0]["reason"],
                         r"^recorded worktree missing; closure proof refused \([a-z][a-z0-9_]*\)$")
        self.lose()
        calls = len(self.mutations)
        self.assertEqual(self.status()["window_continuation"]["condition"], "run_shared")
        self.assertEqual(len(self.mutations), calls)

    def test_a_removed_worktree_peer_with_an_empty_closure_report_refuses(self):
        self.integrity(lambda checkpoint: checkpoint["closure"].update(report={}))

    def test_a_removed_worktree_peer_with_an_active_row_refuses(self):
        self.integrity(lambda checkpoint: checkpoint["obligations"][0].update(state="active", executor="coordinator"))

    def test_a_removed_worktree_peer_with_a_damaged_settlement_projection_refuses(self):
        self.integrity(lambda checkpoint: checkpoint["closure"]["settlement_admissions"].update(
            {key: "0" * 16 for key in checkpoint["closure"]["settlement_admissions"]}))

    def test_a_removed_worktree_keeps_the_retained_history_hold(self):
        """Review F2: a closed peer whose retained REUSE of a changed definition cannot be verified still holds
        peer_history_unverifiable once its worktree is gone, named with its repository."""
        self.peers(closed=False)

        def checkpoint(rows=None, **extra):
            value = self.core(governance={"base_ref": "refs/remotes/origin/target"}, **extra)
            if rows is not None:
                value["obligations"] = rows
            return run("checkpoint", {"project": str(self.project), "objective": "hold-peer", "owner": "owner",
                                      "value": value})
        checkpoint([self.criterion("satisfied", proof_scope=["src"], evidence=[
            {"check": "criterion", "command": "unit", "result": "passed", "reference": "original-proof"}])],
            close=True)
        original = self.candidate
        (self.project / "docs").mkdir()
        (self.project / "docs/extra.txt").write_text("outside original verified scope\n")
        git(self.project, "add", ".")
        git(self.project, "commit", "-qm", "unaffected original proof")
        self.candidate = git(self.project, "rev-parse", "HEAD")
        authority = {"provenance": "user_direct", "instruction": "Revise the disposable definition"}
        rows = json.loads(json.dumps(read(self.project, "hold-peer")["checkpoint"]["obligations"]))
        rows[0]["reuse"] = {"from": original}
        checkpoint(rows, reopen=True, close=True, revision_authority=authority)
        rows = json.loads(json.dumps(read(self.project, "hold-peer")["checkpoint"]["obligations"]))
        rows[0].pop("evidence"); rows[0].pop("reuse")
        rows[0].update(state="withdrawn", proof_scope=["elsewhere"],
                       withdrawal={"by": "user_direct", "reason": "authorized definition revision"})
        checkpoint(rows, reopen=True, close=True, revision_authority=authority)
        self.rewrite(self.gone, objective="hold-peer")
        scope = run_scope(self.project, "objective", "run")
        self.assertEqual((scope["objectives"], scope["unverifiable"], scope["unverifiable_rows"]),
                         ([], ["hold-peer"], [{"objective": "hold-peer", "repository": REPOSITORY}]))
        self.lose()
        calls = len(self.mutations)
        verdict = self.status()["owner_handoff"]
        self.assertEqual(verdict["condition"], "peer_history_unverifiable")
        self.assertIn("for peer hold-peer in acme/example.", verdict["message"])
        window = self.status()["window_continuation"]
        self.assertEqual(window["condition"], "peer_history_unverifiable")
        self.assertIn("hold-peer in acme/example", window["message"])
        self.assertEqual(len(self.mutations), calls)

    def test_a_removed_worktree_peer_with_no_closure_record_is_open(self):
        self.peers()
        self.rewrite(lambda raw: (self.gone(raw), raw["checkpoint"].update(closure=None)))
        opened = [{"objective": "closure-peer", "repository": REPOSITORY}]
        self.scope(["closure-peer"], opened, [])
        self.refusal("open: closure-peer in acme/example", objectives=["closure-peer"], opened=opened, unproven=[])

    def test_a_removed_worktree_peer_outside_its_bound_directory_is_unproven(self):
        self.peers()
        path = self.rewrite(self.gone)
        shutil.move(str(path.parent), str(path.parent.with_name("moved-peer")))
        self.scope(["closure-peer"], [], self.unproven("record not where its recorded repository and objective place it"))

    def test_a_removed_worktree_peer_whose_settled_work_changed_is_unproven(self):
        self.peers()

        def change(raw):
            self.gone(raw)
            self.assertIn("settlement_digest", raw["checkpoint"]["closure"])
            next(iter(raw["admissions"].values()))["plan_revision"] = "another-plan"
        self.rewrite(change)
        self.scope(["closure-peer"], [], self.unproven(
            "recorded worktree missing; closure proof refused (obligation_unaccounted)"))

    def test_a_removed_worktree_peer_whose_governor_journal_is_unreadable_is_unproven(self):
        self.peers()
        path = self.rewrite(self.gone)
        (path.parent / "governor.json").write_text(SENTINEL)
        scope = run_scope(self.project, "objective", "run")
        self.assertEqual(scope["objectives"], ["closure-peer"])
        self.assertRegex(scope["closure_unproven"][0]["reason"], r"^recorded worktree missing; closure proof refused")
        self.assertNotIn(SENTINEL, json.dumps(scope))

    def test_a_peer_of_another_repository_with_no_checkout_is_unproven(self):
        """The live pod#251 case without any recorded checkout of the consumer repository."""
        self.peers()
        from pod.ledger import record_name
        key = "f" * 64
        path = self.rewrite(lambda raw: (self.gone(raw), raw["checkpoint"].update(objective="objective"),
                                         raw["checkpoint"]["worktree"].update(repo_key=key, repository=CONSUMER)))
        moved = path.parent.with_name(record_name(self.project, {"repo_key": key}, "objective"))
        shutil.move(str(path.parent), str(moved))
        unproven = self.unproven("recorded worktree missing; no other checkout of its recorded repository found",
                                 "objective", CONSUMER)
        self.scope(["objective"], [], unproven)
        self.refusal("closed with unproven closure: objective in acme/consumer (recorded worktree missing; no other "
                     "checkout of its recorded repository found)", objectives=["objective"], opened=[],
                     unproven=unproven)

    def consumer_peer(self):
        """The live pod#251 shape: a closed peer named like this objective in a consumer repository, written through
        the checkpoint boundary in a linked worktree that completion cleanup then removes, while another record of
        that repository binds its main checkout and names no Run."""
        consumer, removed = self.root / "consumer", self.root / "consumer-peer"
        subprocess.run(["git", "clone", "-q", "--no-local", str(self.project), str(consumer)], check=True)
        git(consumer, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/target")  # As in this project.
        git(consumer, "worktree", "add", "-q", "-b", "peer", str(removed))
        original, placement = self.project, dict(self.port.placement)
        try:
            self.project = removed
            context = repository_context(removed)
            self.port.placement = {**placement, "repository": context["repository"], "repo_key": context["repo_key"],
                                   "path": context["worktree"],
                                   "branch": context["branch"]}
            self.closed_review_peer(objective="objective")
            self.project = consumer
            run("checkpoint", {"project": str(consumer), "objective": "consumer-work", "owner": "owner",
                               "value": self.core(obligations=[self.criterion()],
                                                  governance={"base_ref": "refs/remotes/origin/target"})})
        finally:
            self.project, self.port.placement = original, placement
        other = objective_root(consumer, "consumer-work") / "context.json"
        raw = logical_fixture(other)
        raw["checkpoint"]["native_refs"] = []  # It records no Run, so only its binding counts.
        other.write_text(json.dumps(raw))
        shutil.rmtree(removed)
        return consumer, other

    def test_a_peer_of_another_repository_is_proved_in_a_recorded_checkout_of_it(self):
        self.peers(closed=False)
        self.consumer_peer()
        self.scope([], [], [])
        self.lose()
        self.assertNotEqual(self.status()["owner_handoff"].get("condition"), "run_shared")

    def test_removed_checkouts_sorting_first_never_exhaust_the_candidates(self):
        """The live pod#251 case on 0.21.51: most recorded checkouts of the consumer repository were cleaned up and
        sort before the one that still exists; removed paths are dropped before the candidate cap."""
        self.peers(closed=False)
        _, other = self.consumer_peer()
        raw = logical_fixture(other)
        for number in range(MAX_CHECKOUTS + 1):
            raw["checkpoint"]["worktree"]["path"] = str(self.root / f"a-removed-{number}")  # Sorts first; gone.
            removed = other.parent.with_name(f"removed-record-{number}")
            removed.mkdir()
            (removed / "context.json").write_text(json.dumps(raw))
        self.scope([], [], [])

    def test_a_recorded_checkout_of_another_repository_is_never_used(self):
        self.peers(closed=False)
        _, other = self.consumer_peer()
        stranger = self.root / "stranger"
        subprocess.run(["git", "clone", "-q", "--no-local", str(self.project), str(stranger)], check=True)
        raw = logical_fixture(other)
        raw["checkpoint"]["worktree"]["path"] = str(stranger)  # Its key still names the consumer repository.
        other.write_text(json.dumps(raw))
        reason = "recorded worktree missing; no other checkout of its recorded repository found"
        repository = logical_fixture(other)["checkpoint"]["worktree"]["repository"]
        self.scope(["objective"], [], self.unproven(reason, "objective", repository))

    def changed_between_reads(self, binding):
        """Delta review F3: the checkout chosen for the first read's repository key is never used once the reread
        binding names another; the peer holds as a record changing while read, with no native mutation."""
        from pod import history
        path = self.rewrite(self.gone)
        real, reads = history.load_context, []

        def second_pass(target, *args, **kwargs):
            raw = real(target, *args, **kwargs)
            frame = sys._getframe(1)
            while frame is not None and frame.f_code.co_name != "classify":
                frame = frame.f_back
            if Path(target) == path and frame is not None and frame.f_locals.get("substitute") is not None:
                reads.append(1)  # The scan's second read of this peer now names another repository.
                raw = json.loads(json.dumps(raw))
                raw["checkpoint"]["worktree"].update(binding)
            return raw
        with mock.patch("pod.history.load_context", side_effect=second_pass):
            scope = run_scope(self.project, "objective", "run")
            self.assertEqual((scope["objectives"], scope["closure_unproven"], scope.get("changing")), ([], [], 1))
            self.lose()
            calls = len(self.mutations)
            self.assert_lead_refusal("record_changed_during_read")
            self.assertEqual(len(self.mutations), calls)
        self.assertTrue(reads)

    def test_a_peer_whose_repository_changes_between_passes_holds_as_changing(self):
        self.peers()
        self.changed_between_reads({"repo_key": "f" * 64, "repository": CONSUMER})

    def test_a_peer_rebound_to_an_existing_checkout_between_passes_holds_as_changing(self):
        """The second read names another repository's existing checkout, so its path no longer reads missing."""
        self.peers()
        other = self.root / "other"
        subprocess.run(["git", "clone", "-q", "--no-local", str(self.project), str(other)], check=True)
        git(other, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/target")
        context = repository_context(other)
        self.changed_between_reads({"repo_key": context["repo_key"], "repository": context["repository"],
                                    "path": context["worktree"], "branch": context["branch"]})

    def test_a_closed_peer_whose_proof_refuses_names_only_the_code(self):
        self.peers()
        self.rewrite(lambda raw: raw["checkpoint"]["closure"]["report"].update(status=SENTINEL))
        scope = self.scope(["closure-peer"], [], self.unproven("closure proof refused (obligation_invalid)"))
        self.assertRegex(scope["closure_unproven"][0]["reason"], r"^closure proof refused \([a-z][a-z0-9_]*\)$")

    def test_a_worktree_path_that_is_no_directory_is_unreadable(self):
        self.peers()
        self.rewrite(lambda raw: raw["checkpoint"]["worktree"].update(path=str(self.project / "README.md")))
        self.scope(["closure-peer"], [], self.unproven("recorded worktree unreadable"))

    def test_a_closed_peer_with_no_binding_outside_its_own_directory_is_unavailable(self):
        self.peers()
        path = self.rewrite(lambda raw: raw["checkpoint"].pop("worktree"))
        shutil.move(str(path.parent), str(path.parent.with_name("moved-peer")))
        self.scope(["closure-peer"], [], self.unproven("recorded repository binding unavailable", repository=None))

    def test_a_closed_record_in_an_unsupported_format_says_so(self):
        self.peers()
        self.rewrite(lambda raw: raw.update(schema="unknown-future-context"))
        self.scope(["closure-peer"], [], self.unproven("record format unsupported"))

    def test_an_open_peer_is_named_open(self):
        self.peers(closed=False, opened=True)
        opened = [{"objective": "open-peer", "repository": REPOSITORY}]
        self.scope(["open-peer"], opened, [])
        self.refusal("open: open-peer in acme/example", objectives=["open-peer"], opened=opened, unproven=[])

    def test_open_and_unproven_peers_are_named_apart(self):
        self.peers(opened=True)
        self.rewrite(lambda raw: raw["checkpoint"]["closure"]["report"].update(status=SENTINEL))
        unproven = self.unproven("closure proof refused (obligation_invalid)")
        opened = [{"objective": "open-peer", "repository": REPOSITORY}]
        self.scope(["closure-peer", "open-peer"], opened, unproven)
        detail = ("open: open-peer in acme/example; closed with unproven closure: closure-peer in acme/example "
                  "(closure proof refused (obligation_invalid))")
        self.refusal(detail, objectives=["closure-peer", "open-peer"], opened=opened, unproven=unproven)
        self.assertIn("Owner handoff: run_shared; " + self.PREFIX + detail + ".", self.status(text=True))


class PeerLabelTests(unittest.TestCase):
    def test_peers_carry_their_repository(self):
        self.assertEqual(peer_label({"objective": "a", "repository": "acme/x"}), "a in acme/x")
        self.assertEqual(peer_label({"objective": "a", "repository": None}), "a in an unrecorded repository")
        self.assertEqual(peer_label("a"), "a")  # A result from an earlier shape names the objective alone.
        self.assertEqual(shared_detail({"objectives": ["a"], "open": ["a"]}), "open: a")


class UnprovenReasonTests(unittest.TestCase):
    def test_reasons_never_carry_exception_text(self):
        for error, reason in ((PodError("history_missing", SENTINEL), "closure proof refused (history_missing)"),
                              (PodError(SENTINEL, SENTINEL), "closure proof refused (unknown)"),
                              (PodError("x" * 65, SENTINEL), "closure proof refused (unknown)"),
                              (OSError(SENTINEL), "recorded worktree unreadable"),
                              (KeyError(SENTINEL), "closed record malformed"),
                              (TypeError(SENTINEL), "closed record malformed"),
                              (ValueError(SENTINEL), "closed record malformed")):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(_unproven_reason(error), reason)


if __name__ == "__main__":
    unittest.main()
