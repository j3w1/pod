"""R209: recording a host-verified local append through the real checkpoint entry and a real verifier process."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pod import relay
from pod.errors import PodError
from pod.governor import _record_path
from pod.ledger import objective_root, read
from pod.local_append import merge_tree, validate_record
from tests.kernel_support import git
from tests.local_append_support import LocalAppendCase, OPERATION


class RecordTests(LocalAppendCase):
    def test_fast_forward_records_every_field_from_its_source(self):
        self.ready()
        before = read(self.project, "objective")["checkpoint"]
        journal = self.journal_bytes()
        accepted = self.record()["checkpoint"]
        delivery = accepted["delivery"]
        tree = self.tree(self.candidate)
        self.assertEqual({key: delivery[key] for key in ("kind", "seq", "operation", "base_ref", "base", "candidate",
                                                        "tree", "previous", "previous_tree", "result", "result_tree",
                                                        "method")},
                         {"kind": "local_append", "seq": before["seq"] + 1, "operation": OPERATION,
                          "base_ref": before["governance"]["base_ref"], "base": self.base,
                          "candidate": self.candidate, "tree": tree, "previous": self.base,
                          "previous_tree": self.tree(self.base), "result": self.candidate, "result_tree": tree,
                          "method": "fast-forward"})
        self.assertEqual((delivery["store"], delivery["row"], delivery["certificate"], delivery["appended_at"]),
                         ({"instance": "host-store", "epoch": "3"}, {"sequence": 41, "digest": "sha256:" + "ab" * 32},
                          {"tier": "V2", "deferred": ["combined docs review at ship"]}, "2026-11-02T03:00:00Z"))
        self.assertEqual(delivery["verification"]["reference"], OPERATION)
        self.assertEqual(validate_record(delivery), delivery)
        # The host gets only the operation id: no expectation it could echo.
        self.assertEqual(self.requests(), [{"schema": "pod-relay-verification/v1", "action": "local_append",
                                            "reference": OPERATION}])
        # Governance does not move; no Governor row, publish or merge authorization is created or consumed.
        self.assertEqual((accepted["governance"], accepted["governance_sources"]),
                         (before["governance"], before["governance_sources"]))
        self.assertEqual(self.journal_bytes(), journal)
        self.assertFalse((objective_root(self.project, "objective").parent / ".relay-authorizations").exists())

    def test_clean_merge_records_with_the_candidate_independent_of_the_result(self):
        self.ready()
        result = self.train_merge()
        delivery = self.record()["checkpoint"]["delivery"]
        self.assertEqual((delivery["method"], delivery["previous"], delivery["result"], delivery["candidate"]),
                         ("merge", self.previous, result, self.candidate))
        self.assertNotEqual(delivery["result_tree"], delivery["tree"])


class ExactnessTests(LocalAppendCase):
    def refuse(self, code, **facts):
        before = self.saved()
        self.answer(self.verdict(**facts))
        error = self.failure(code, self.write, delivery=self.request())
        self.assertEqual(self.saved(), before)
        return error

    def commit_tree(self, tree, *parents, message="host"):
        argv = ["commit-tree", tree]
        for parent in parents:
            argv += ["-p", parent]
        return git(self.project, *argv, "-m", message)

    def test_wrong_parents_squash_and_rebase_refuse(self):
        self.ready()
        result = self.train_merge()
        tree = self.tree(result)
        for parents in ((self.candidate, self.previous), (self.previous,), (self.previous, self.candidate, self.base)):
            with self.subTest(parents=len(parents)):
                other = self.commit_tree(tree, *parents)
                error = self.refuse("local_append_not_exact", result=other)
                self.assertIn(error.detail["referent"]["reason"], ("parents",))
        # A rebase of the candidate onto the train is not the candidate: neither result shape holds.
        rebased = self.commit_tree(tree, self.previous)
        self.refuse("local_append_not_exact", result=rebased, method="fast-forward")

    def test_another_tree_a_conflict_and_a_differing_method_refuse(self):
        self.ready()
        self.train_merge()
        other = self.commit_tree(self.tree(self.base), self.previous, self.candidate)
        self.assertEqual(self.refuse("local_append_not_exact", result=other).detail["referent"]["reason"], "merge_tree")
        self.assertEqual(self.refuse("local_append_not_exact", method="fast-forward").detail["referent"]["reason"],
                         "method")
        # The previous tip must be an ancestor for a fast-forward.
        self.fast_forward()
        self.previous = self.train_merge_previous()
        self.assertEqual(self.refuse("local_append_not_exact", previous=self.previous, result=self.candidate,
                                     method="fast-forward").detail["referent"]["reason"], "previous_not_ancestor")

    def train_merge_previous(self):
        git(self.project, "checkout", "-q", "-b", "side", self.base)
        (self.project / "side.md").write_text("side\n")
        git(self.project, "add", "side.md")
        git(self.project, "commit", "-qm", "side")
        commit = git(self.project, "rev-parse", "HEAD")
        git(self.project, "checkout", "-q", "main")
        return commit

    def test_a_conflicting_merge_refuses_even_with_ambient_union_attributes(self):
        # Every ambient attribute layer says union: the global attributes file, the repository's
        # info/attributes and the tree. Pod's merge ignores them all, so the conflict still refuses.
        (self.project / "src" / "old.py").write_text("candidate\n")
        (self.project / ".gitattributes").write_text("* merge=union\n")
        git(self.project, "add", ".")
        git(self.project, "commit", "-qm", "fix with attributes")
        self.candidate = git(self.project, "rev-parse", "HEAD")
        home = Path(os.environ["HOME"])
        (home / ".config" / "git").mkdir(parents=True, exist_ok=True)
        (home / ".config" / "git" / "attributes").write_text("* merge=union\n")
        info = Path(git(self.project, "rev-parse", "--path-format=absolute", "--git-path", "info"))
        info.mkdir(parents=True, exist_ok=True)
        (info / "attributes").write_text("* merge=union\n")
        self.ready()
        git(self.project, "checkout", "-q", "-b", "train", self.base)
        (self.project / "src" / "old.py").write_text("train\n")
        git(self.project, "commit", "-qam", "train edit")
        self.previous = git(self.project, "rev-parse", "HEAD")
        git(self.project, "checkout", "-q", "main")
        # The host claims a union merge it made with its own attributes.
        union = git(self.project, "merge-tree", "--write-tree", self.previous, self.candidate)
        self.result = self.commit_tree(union.splitlines()[0], self.previous, self.candidate)
        self.method = "merge"
        objects = Path(git(self.project, "rev-parse", "--path-format=absolute", "--git-path", "objects"))
        stored = sorted(str(path.relative_to(objects)) for path in objects.rglob("*") if path.is_file())
        self.assertEqual(self.refuse("local_append_not_exact").detail["referent"]["reason"], "merge_conflicts")
        self.assertEqual(sorted(str(path.relative_to(objects)) for path in objects.rglob("*") if path.is_file()),
                         stored)


class StateTests(LocalAppendCase):
    def refuse_states(self, *states):
        self.ready()
        before = self.saved()
        for state in states:
            with self.subTest(state=state):
                if state == "absent":
                    self.answer(self.verdict(verified=False))
                else:
                    self.answer(self.verdict(state=state))
                self.failure("local_append_" + state, self.write, delivery=self.request())
                self.assertEqual(self.saved(), before)
        self.assertIsNone(self.delivery())

    def test_each_unfinished_state_refuses_with_its_own_code(self):
        self.refuse_states("requested", "running")

    def test_a_failed_unknown_or_unverified_operation_refuses_with_its_own_code(self):
        self.refuse_states("failed", "unknown", "absent")

    def test_a_requested_append_records_once_the_host_reports_success(self):
        self.ready()
        self.answer(self.verdict(state="requested"))
        self.failure("local_append_requested", self.write, delivery=self.request())
        self.answer(self.verdict(state="running"))
        self.failure("local_append_running", self.write, delivery=self.request())
        self.answer(self.verdict())
        self.assertEqual(self.write(delivery=self.request())["checkpoint"]["delivery"]["operation"], OPERATION)
        self.assertEqual(len(self.requests()), 3)


class CheckTests(LocalAppendCase):
    def refuse(self, code, verdict, operation=None):
        before = self.saved()
        self.answer(verdict)
        error = self.failure(code, self.write, delivery=self.request(operation))
        self.assertEqual(self.saved(), before)
        return error

    def test_foreign_repository_objective_and_source_name_only_the_field(self):
        self.ready()
        for path, value, field in (("operation.repository", "f" * 64, "repository"),
                                   ("operation.objective", "objective-elsewhere", "objective"),
                                   ("operation.source", {"repository": "acme/example", "number": 7,
                                                         "locator": self.locator}, "source")):
            with self.subTest(field=field):
                error = self.refuse("local_append_foreign", self.verdict({path: value}))
                self.assertEqual(error.detail["referent"]["fields"], [field])
                self.assertNotIn(str(value), str(error))
        # A foreign failed operation is foreign first.
        self.refuse("local_append_foreign", self.verdict({"operation.objective": "objective-elsewhere"}, state="failed"))

    def test_another_candidate_tree_or_row_refuses(self):
        self.ready()
        self.refuse("local_append_candidate_mismatch", self.verdict({"operation.candidate.commit": self.base}))
        self.refuse("local_append_candidate_mismatch", self.verdict({"operation.candidate.tree": self.tree(self.base)}))
        self.refuse("local_append_row_mismatch", self.verdict({"operation.row.operation": "host-op-0002"}))

    def test_echo_and_tip_checks_refuse(self):
        self.ready()
        self.refuse("local_append_verifier_mismatch", self.verdict({"reference": "host-op-0002"}))
        self.assertEqual(self.refuse("local_append_verifier_mismatch", self.verdict({"action": "merge"}))
                         .detail["referent"]["fields"], ["action"])
        missing = "e" * 40
        self.refuse("local_append_git_unavailable",
                    self.verdict({"operation.previous": {"commit": missing, "tree": missing}}))
        self.refuse("local_append_tip_mismatch",
                    self.verdict({"operation.result.tree": self.tree(self.base)}))


class TransportTests(LocalAppendCase):
    def test_no_verifier_refuses_before_any_process(self):
        self.ready()
        self.configure(None)
        before = self.saved()
        self.failure("local_append_verifier_unconfigured", self.write, delivery=self.request())
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.requests(), [])

    def test_failing_timed_out_and_oversized_verifiers_refuse(self):
        self.ready()
        before = self.saved()
        self.answer(mode="fail")
        error = self.failure("local_append_verifier_failed", self.write, delivery=self.request())
        self.assertNotIn("sentinel", str(error) + json.dumps(error.detail))
        with patch.object(relay, "TIMEOUT_S", .15):
            for mode in ("timeout",):
                self.answer(mode=mode)
                self.failure("local_append_verifier_timeout", self.write, delivery=self.request())
            self.answer(mode="oversized")
            self.failure("local_append_verifier_malformed", self.write, delivery=self.request())
        self.configure([str(self.root / "absent-verifier")])
        self.failure("local_append_verifier_failed", self.write, delivery=self.request())
        self.assertEqual(self.saved(), before)

    def malformed(self, verdicts):
        self.ready()
        before = self.saved()
        for name, verdict, mode in verdicts():
            with self.subTest(name=name):
                self.answer(verdict, mode=mode)
                self.failure("local_append_verifier_malformed", self.write, delivery=self.request())
        self.assertEqual(self.saved(), before)

    def test_invalid_and_duplicate_key_output_refuses(self):
        self.malformed(lambda: [("invalid_json", self.verdict(), "invalid_json"),
                                ("duplicate", self.verdict(), "duplicate")])

    def test_wrong_top_level_and_identity_shapes_refuse(self):
        self.malformed(lambda: [(str(change), self.verdict(change), None)
                                for change in ({"verified": 1}, {"state": "landed"}, {"operation.extra": "untrusted"},
                                               {"operation.candidate.commit": "A" * 40})])

    def test_wrong_provenance_shapes_refuse(self):
        self.malformed(lambda: [(str(change), self.verdict(change), None)
                                for change in ({"operation.appended_at": "yesterday"}, {"operation.row.sequence": 0},
                                               {"operation.certificate.deferred": ["a", "a"]},
                                               {"operation.store": None})]
                       + [("provenance before success", self.verdict({"operation.method": "merge"}, state="running"),
                           None)])

    def test_project_config_cannot_supply_the_verifier(self):
        self.ready()
        self.configure(None)
        (self.project / ".pod").mkdir(exist_ok=True)
        (self.project / ".pod" / "config.yaml").write_text(
            "schema: pod/v1\nauthorization_verifier: [" + json.dumps(self.command[0]) + "]\n")
        with self.assertRaises(PodError) as caught:
            self.write(delivery=self.request())
        self.assertEqual(caught.exception.code, "invalid_config")
        self.assertEqual(self.requests(), [])


def merge_row(case, outcome, record="merge-1"):
    """An allowed Governor merge row in the R93 shape, at `outcome`."""
    from tests.kernel_support import authorization
    tree = case.tree(case.candidate)
    return {"record_id": record, "logical_key": record, "attempt": 1,
            "action": {"kind": "merge", "candidate": "candidate-1", "target": "origin/target", "unit": "delivery",
                       "authorization": authorization(candidate=case.candidate, tree=tree, scope=("merge",),
                                                      target="origin/target")},
            "target_binding": {"remote": "origin", "base": "target", "ref": "refs/remotes/origin/target"},
            "candidate_id": "candidate-1", "commit": case.candidate, "decision": "ALLOW", "outcome": outcome,
            "receipt": {"provider": {}, **({"finished_at": "2026-11-01T00:00:00Z"} if outcome != "pending" else {})},
            "warnings": [], "reasons": [], "classifications": []}


def write_merge_rows(case, *rows):
    from pod.governor import _empty_journal, _read_journal, _write_journal
    journal = _empty_journal()
    journal["revision"] = _read_journal(_record_path(case.project, "objective"))["revision"]
    journal["units"]["delivery"] = {"name": "delivery", "generation": 1, "history": ["candidate-1"], "preflight": {},
                                    "published": {}, "tasks": [],
                                    "candidate": {"id": "candidate-1", "commit": case.candidate,
                                                  "tree": case.tree(case.candidate)},
                                    "branch": {"remote": "origin", "base": "target", "branch": "delivery"}}
    journal["actions"].extend(rows)
    _write_journal(_record_path(case.project, "objective"), journal)


class RecoveryTests(LocalAppendCase):
    def test_the_original_request_after_a_lost_acknowledgement_reads_back(self):
        from pod.status import status
        self.ready()
        value = self.core(seq=read(self.project, "objective")["checkpoint"]["seq"] + 1, delivery=self.request())
        self.answer(self.verdict())
        self.op("checkpoint", value=deepcopy(value))  # committed; its acknowledgement is lost
        before, reads = self.saved(), len(self.requests())
        error = self.failure("map_stale", self.op, "checkpoint", value=deepcopy(value))
        self.assertEqual((self.saved(), len(self.requests())), (before, reads))
        self.assertEqual(error.detail["recorded_delivery"], OPERATION)
        self.assertIn("pod status", error.detail["next_action"])
        self.assertIn("nothing is appended or replayed", str(error))
        shown = status(self.project, None, objective="objective")["delivery"]
        self.assertEqual((shown["kind"], shown["operation"]), ("local_append", OPERATION))

    def test_fresh_sequence_and_closing_restatements_keep_the_record_without_a_read(self):
        self.ready()
        self.record()
        recorded, reads = deepcopy(self.delivery()), len(self.requests())
        self.answer(mode="fail")  # any read would now refuse
        self.write(delivery=self.request())
        self.assertEqual(self.delivery(), recorded)
        self.write(delivery=deepcopy(recorded))  # the stored object itself, round-tripped
        closed = self.write(self.stored(), delivery=self.request(), close=True)["checkpoint"]
        self.assertTrue(closed["closure"])
        self.assertEqual((closed["delivery"], len(self.requests())), (recorded, reads))

    def test_another_operation_or_kind_never_replaces_the_record(self):
        self.ready()
        self.record()
        before = self.saved()
        error = self.failure("delivery_recorded", self.write, delivery=self.request("host-op-0002"))
        self.assertEqual((error.detail["reason"], error.detail["record"], error.detail["kind"]),
                         ("operation", OPERATION, "local_append"))
        error = self.failure("delivery_recorded", self.write, delivery={"record": "merge-1"})
        self.assertEqual(error.detail["reason"], "kind")
        self.assertEqual(self.saved(), before)


class DecisionTests(LocalAppendCase):
    def test_a_merge_that_landed_or_may_have_landed_blocks_the_local_kind(self):
        self.ready()
        before = None
        for outcome in ("PASS", "pending", "UNKNOWN"):
            with self.subTest(outcome=outcome):
                write_merge_rows(self, merge_row(self, outcome))
                before = self.saved()
                self.answer(self.verdict())
                error = self.failure("local_append_remote_merge", self.write, delivery=self.request())
                self.assertEqual(error.detail["referent"]["records"], "merge-1")
                self.assertEqual(self.saved(), before)
        self.assertEqual(self.requests(), [])

    def test_a_failed_or_canceled_merge_does_not_block(self):
        self.ready()
        write_merge_rows(self, merge_row(self, "FAILED"), merge_row(self, "CANCELED", "merge-2"))
        self.assertEqual(self.record()["checkpoint"]["delivery"]["kind"], "local_append")

    def test_a_governor_merge_after_a_local_delivery_refuses_before_authority(self):
        from tests.kernel_support import action
        self.ready()
        self.record()
        self.port.runtime = "runtime-2"
        self.port.placement = {**self.port.placement, "runtime": "runtime-2"}
        before = self.saved()
        merge = action(kind="merge", candidate=self.candidate, target="origin/target", unit="delivery", pr=7)
        # Merge is decide-only (governor); the refusal comes before any rebind is persisted or journal row written.
        error = self.failure("delivery_recorded", self.op, "governor", action=deepcopy(merge))
        self.assertEqual(error.detail["reason"], "kind")
        self.assertEqual(self.saved(), before)
        # The Governor's own guard under the lock refuses too, before it joins authority.
        from pod.governor import decide
        with patch("pod.ledger.require_authority", side_effect=AssertionError("authority joined")):
            self.assertEqual(self.failure("delivery_recorded", decide, self.project, "objective", owner="owner",
                                          action=deepcopy(merge)).detail["reason"], "kind")
        self.assertEqual(self.saved(), before)

    def test_finalization_still_owes_the_merge_record_for_a_merge_pass(self):
        from pod.governor import finalization
        self.ready()
        self.record()
        state = read(self.project, "objective")
        view = finalization(state, {"actions": [merge_row(self, "PASS")]})
        self.assertEqual(view["remaining"][0], "delivery_record")


class ProofTests(LocalAppendCase):
    def test_a_blocked_or_unknown_proof_refuses_naming_its_status(self):
        self.fix()
        self.intake()  # no assurance: the label is withheld, so the proof is blocked
        before = self.saved()
        self.answer(self.verdict())
        error = self.failure("local_append_unproved", self.write, delivery=self.request())
        self.assertEqual(error.detail["proof"]["status"], "blocked")
        self.assertIn("assurance_unbound", error.detail["proof"]["gaps"])
        self.assertEqual(self.saved(), before)
        with patch("pod.admission._proof_view", return_value=({"status": "unknown", "gaps": []}, None, None)):
            self.assertEqual(self.failure("local_append_unproved", self.write, delivery=self.request())
                             .detail["proof"]["status"], "unknown")
        self.assertEqual(self.saved(), before)

    def test_a_write_that_also_changes_the_candidate_is_judged_on_the_recorded_one(self):
        self.ready()
        recorded = self.candidate
        self.fix("src/second.py", "second\n")
        self.answer(self.verdict())
        error = self.failure("local_append_unproved", self.write, delivery=self.request())
        self.assertEqual(error.detail["proof"]["reason"], "candidate_mismatch")
        self.assertEqual(read(self.project, "objective")["checkpoint"]["candidate"], recorded)

    def test_a_restatement_needs_no_proof(self):
        self.ready()
        self.record()
        with patch("pod.admission._proof_view", side_effect=AssertionError("restatement judged a proof")):
            self.write(delivery=self.request())

    def test_the_proof_judges_the_authority_joined_snapshot(self):
        from pod import admission
        self.ready()
        self.port.runtime = "runtime-2"
        self.port.placement = {**self.port.placement, "runtime": "runtime-2"}
        seen = []
        original = admission._proof_view

        def spy(project, objective, candidate, **kwargs):
            seen.append(kwargs)
            return original(project, objective, candidate, **kwargs)
        with patch("pod.admission._proof_view", side_effect=spy):
            self.record()
        self.assertEqual(len(seen), 1)
        joined = seen[0]["state"]
        self.assertEqual({row["runtime"] for row in joined["checkpoint"]["native_refs"]}, {"runtime-2"})
        self.assertEqual(seen[0]["native"]["runtime"], "runtime-2")


class RuntimeChangeTests(LocalAppendCase):
    def change_runtime(self):
        self.port.runtime = "runtime-2"
        self.port.placement = {**self.port.placement, "runtime": "runtime-2"}

    def test_every_refusal_leaves_the_record_without_the_rebind(self):
        self.ready()
        self.change_runtime()
        before = self.saved()
        for name, prepare, code in (
                ("unconfigured", lambda: self.configure(None), "local_append_verifier_unconfigured"),
                ("unfinished", lambda: (self.configure(self.command), self.answer(self.verdict(state="running"))),
                 "local_append_running"),
                ("git", lambda: self.answer(self.verdict({"operation.result.tree": self.tree(self.base)})),
                 "local_append_tip_mismatch")):
            with self.subTest(name=name):
                prepare()
                self.failure(code, self.write, delivery=self.request())
                self.assertEqual(self.saved(), before)
                checkpoint = read(self.project, "objective")["checkpoint"]
                self.assertEqual({row["runtime"] for row in checkpoint["native_refs"]}, {"runtime"})

    def test_an_unproved_refusal_also_leaves_the_rebind_unwritten(self):
        self.fix()
        self.intake()
        self.change_runtime()
        before = self.saved()
        self.answer(self.verdict())
        self.failure("local_append_unproved", self.write, delivery=self.request())
        self.assertEqual(self.saved(), before)

    def test_a_successful_recording_persists_the_rebind_with_its_write(self):
        self.ready()
        self.change_runtime()
        result = self.record()
        checkpoint = read(self.project, "objective")["checkpoint"]
        self.assertEqual(checkpoint["native_refs"], [{"runId": "run", "runtime": "runtime-2"}])
        history = checkpoint["continuity"]["history"]
        self.assertEqual((history[-1]["from_runtime"], history[-1]["to_runtime"]), ("runtime", "runtime-2"))
        self.assertEqual(result["runtime_continuity"], history)
        self.assertEqual(checkpoint["delivery"]["operation"], OPERATION)


class InputTests(LocalAppendCase):
    def test_mixed_unsupported_and_differing_inputs_refuse_with_their_codes(self):
        self.ready()
        before = self.saved()
        for value, code in (({"local_append": {"operation": OPERATION}, "record": "merge-1"}, "delivery_kind_mixed"),
                            ({"local_append": {"operation": OPERATION, "result": "f" * 40}}, "invalid_local_append"),
                            ({"local_append": {"operation": ""}}, "invalid_local_append_operation"),
                            ({"kind": "remote_append", "operation": OPERATION}, "delivery_kind_unsupported"),
                            ({"kind": "local_append", "operation": OPERATION}, "delivery_restatement_mismatch")):
            with self.subTest(code=code):
                self.failure(code, self.write, delivery=value)
                self.assertEqual(self.saved(), before)
        self.assertEqual(self.requests(), [])
        self.record()
        stored = deepcopy(self.delivery())
        changed = {**deepcopy(stored), "certificate": {"tier": "V0", "deferred": []}}
        self.failure("delivery_restatement_mismatch", self.write, delivery=changed)
        self.failure("delivery_kind_mixed", self.write, delivery={**deepcopy(stored), "record": "merge-1"})
        self.assertEqual(self.delivery(), stored)


class GitQualificationTests(unittest.TestCase):
    """The disposable merge repository across object formats, worktree layouts, alternates and partial clones."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix="local-append-git-")
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.home.name)
        cache = patch.dict(os.environ, {"POD_CACHE_HOME": str(self.root / "cache")})
        cache.start(); self.addCleanup(cache.stop)

    def repository(self, name, *, object_format="sha1"):
        path = self.root / name
        subprocess.run(["git", "init", "-q", "-b", "main", f"--object-format={object_format}", str(path)], check=True)
        git(path, "config", "user.name", "Fixture")
        git(path, "config", "user.email", "fixture@example.invalid")
        (path / "a.txt").write_text("one\ntwo\nthree\n")
        (path / "b.txt").write_text("keep\n")
        git(path, "add", ".")
        git(path, "commit", "-qm", "base")
        return path

    def branches(self, path, *, rename=False):
        base = git(path, "rev-parse", "HEAD")
        git(path, "checkout", "-qb", "train")
        (path / "b.txt").write_text("keep\ntrain\n")
        git(path, "commit", "-qam", "train")
        previous = git(path, "rev-parse", "HEAD")
        git(path, "checkout", "-q", base)
        git(path, "checkout", "-qb", "fix")
        if rename:
            git(path, "mv", "a.txt", "renamed.txt")
        else:
            (path / "a.txt").write_text("one\nFIX\nthree\n")
        git(path, "commit", "-qam", "fix")
        candidate = git(path, "rev-parse", "HEAD")
        expected = git(path, "merge-tree", "--write-tree", previous, candidate).splitlines()[0]
        return previous, candidate, expected

    def objects(self, path):
        objects = Path(git(path, "rev-parse", "--path-format=absolute", "--git-path", "objects"))
        return sorted(str(item.relative_to(objects)) for item in objects.rglob("*") if item.is_file())

    def test_sha1_and_sha256_repositories_merge_in_their_own_format(self):
        for object_format in ("sha1", "sha256"):
            with self.subTest(object_format=object_format):
                path = self.repository(object_format, object_format=object_format)
                previous, candidate, expected = self.branches(path)
                before = self.objects(path)
                self.assertEqual(merge_tree(path, previous, candidate), ("clean", expected))
                self.assertEqual(len(expected), 40 if object_format == "sha1" else 64)
                self.assertEqual(self.objects(path), before)

    def test_a_linked_worktree_reads_the_common_object_directory(self):
        path = self.repository("main")
        previous, candidate, expected = self.branches(path)
        linked = self.root / "linked"
        git(path, "worktree", "add", "-q", "--detach", str(linked), candidate)
        self.assertEqual(merge_tree(linked, previous, candidate), ("clean", expected))

    def test_a_source_with_alternates_and_a_rename_merge(self):
        origin = self.repository("origin")
        previous, candidate, expected = self.branches(origin, rename=True)
        borrower = self.root / "borrower"
        subprocess.run(["git", "clone", "-q", "--shared", str(origin), str(borrower)], check=True)
        self.assertTrue((borrower / ".git" / "objects" / "info" / "alternates").exists())
        self.assertEqual(merge_tree(borrower, previous, candidate), ("clean", expected))

    def test_a_partial_clone_is_refused_without_a_fetch(self):
        path = self.repository("partial")
        previous, candidate, _ = self.branches(path)
        git(path, "config", "remote.origin.url", str(self.root / "nowhere"))
        git(path, "config", "remote.origin.promisor", "true")
        with patch("subprocess.run", wraps=subprocess.run) as run:
            self.assertEqual(merge_tree(path, previous, candidate), ("unavailable", None))
        self.assertFalse(any("merge-tree" in call.args[0] for call in run.call_args_list if call.args))


if __name__ == "__main__":
    unittest.main()
