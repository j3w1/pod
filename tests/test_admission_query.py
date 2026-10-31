"""R185: production admission reads, selective evidence and conservative gaps."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from unittest.mock import patch

from pod import admission, cli, governor, history
from pod.ledger import objective_root, record_name, state_root
from pod.github import repository_context
from tests.kernel_support import KernelCase, git


class AdmissionQueryCase(KernelCase):
    def setUp(self):
        super().setUp()
        self.place = {"repository": None, "repo_key": repository_context(self.project)["repo_key"],
                      "path": str(self.project), "branch": "main"}

        self._real_fleet = admission._fleet
        native = patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": []})
        native.start(); self.addCleanup(native.stop)
        self._real_terminals = admission._terminals
        terminals = patch("pod.admission._terminals", return_value={"runtime": "runtime", "complete": True, "terminals": []})
        terminals.start(); self.addCleanup(terminals.stop)
        self._real_owner_observation = admission._owner_observation
        owner = patch("pod.admission._owner_observation", return_value={"status": "unknown", "code": "fixture_unobserved"})
        owner.start(); self.addCleanup(owner.stop)

    def raw(self, name="objective"):
        path = objective_root(self.project, name) / "context.json"
        return path, json.loads(path.read_text())

    def save(self, path, raw):
        path.write_text(json.dumps(raw))

    def query(self, **kwargs):
        return admission.query(self.project, **kwargs)


class AdmissionQueryTests(AdmissionQueryCase):
    def test_finished_history_does_not_hydrate_unrelated_receipts(self):
        self.intake(worktree=self.place)
        self.write([self.criterion("satisfied", evidence=[self.proof("O1")])], objective="old",
                   governance={"base_ref": "target"}, close=True, worktree=self.place)
        path, raw = self.raw("old")
        # Unrelated retained proof is not an exclusion authority for claims.
        raw["history"] = history.FORMAT
        raw["checkpoint"]["obligations"][0]["receipts"] = [history._reference({"old": "x" * 1000}, {})]
        self.save(path, raw)
        with patch("pod.history.load_context", side_effect=AssertionError("full read")), \
             patch("pod.admission._primary", wraps=admission._primary) as reads, \
             patch("pod.history.ChunkStore.load", side_effect=AssertionError("unrelated history")) as chunks:
            result = self.query()
        self.assertTrue(result["complete"])
        self.assertEqual(reads.call_count, 4)  # primary plus concurrency recheck, each record
        self.assertEqual(chunks.call_count, 0)
        self.assertEqual([row["objective"] for row in result["claims"]], ["objective"])

    def test_closed_record_keeps_hidden_native_writer_conflict(self):
        self.write([self.criterion("satisfied", evidence=[self.proof("O1")])],
                   governance={"base_ref": "target"}, close=True, worktree=self.place)
        path, raw = self.raw()
        binding = {"runId": "run", "taskId": "task", "workerId": "worker", "dispatchId": "dispatch",
                   "worktreeId": "id:repo::" + str(self.project), "terminalHandle": "terminal"}
        raw["admissions"]["a"] = {"schema": "pod-admission/v4", "admission_id": "a", "state": "closed",
            "role": "implement", "native_binding": binding, "runtime": "runtime"}
        self.save(path, raw)
        writer = {"dispatchId": "dispatch", "runId": "run", "taskId": "task", "agentTerminalHandle": "terminal",
                  "resource": {"worktreeId": binding["worktreeId"]},
                  "projection": {"id": "worker", "dispatchId": "dispatch", "runId": "run", "taskId": "task",
                                 "liveness": {"verdict": "live"}, "resource": {"state": "retained"}}}
        hidden = {**writer, "dispatchId": "unrecorded", "projection": {**writer["projection"], "dispatchId": "unrecorded"}}
        native = {"runtime": "runtime", "complete": True, "workers": [writer, hidden]}
        with patch("pod.admission._fleet", return_value=native):
            result = self.query(worktree=str(self.project))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual({row["claim"]["dispatch"] for row in result["conflicts"]}, {"dispatch", "unrecorded"})

    def test_unreadable_and_unattributed_are_scoped_unknowns(self):
        self.intake(worktree=self.place)
        root = state_root(self.project)
        (root / "broken").mkdir(); (root / "broken" / "context.json").write_text("{")
        (root / "journal-only").mkdir(); (root / "journal-only" / "governor.json").write_text("{}")
        result = self.query()
        self.assertFalse(result["complete"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual({row["code"] for row in result["unknowns"]}, {"invalid_record", "unattributed"})
        self.assertTrue(all(row["scope"] == result["scope"] for row in result["unknowns"]))

    def test_incomplete_inventory_never_proves_absence(self):
        self.intake(worktree=self.place)
        with patch("pod.admission.os.scandir", side_effect=OSError("unavailable")):
            result = self.query()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["unknowns"][0]["code"], "inventory_unreadable")

    def test_changed_merge_target_is_detected_on_recheck(self):
        self.intake(worktree=self.place)
        before = self.query(target="target", target_commit=self.base)
        (self.project / "next").write_text("next\n")
        git(self.project, "add", "."); git(self.project, "commit", "-qm", "next")
        git(self.project, "update-ref", "refs/heads/target", git(self.project, "rev-parse", "HEAD"))
        after = self.query(target="target", target_commit=self.base, expect=before["snapshot"])
        self.assertEqual(after["status"], "blocked")
        self.assertEqual({row["kind"] for row in after["conflicts"]}, {"target_changed", "snapshot_changed"})

    def test_proof_reads_only_selected_exact_candidate(self):
        self.intake(worktree=self.place)
        self.write([self.criterion()], objective="other", governance={"base_ref": "target"}, worktree=self.place)
        with patch("pod.admission._proof", wraps=admission._proof) as proof:
            result = self.query(objective="objective", candidate=self.base)
        proof.assert_called_once_with(self.project, "objective", self.base)
        self.assertEqual(result["proof"]["status"], "blocked")
        other = self.query(objective="objective", candidate="e" * 40)
        self.assertEqual(other["proof"]["reason"], "candidate_mismatch")

    def test_unreadable_journal_and_changed_record_are_unknown(self):
        self.intake(worktree=self.place)
        path, raw = self.raw()
        (path.parent / "governor.json").write_text("{")
        result = self.query()
        self.assertEqual(result["status"], "unknown")
        (path.parent / "governor.json").unlink()
        real = admission._primary
        def changed(p):
            value, identity = real(p)
            if changed.called:
                value["checkpoint"]["worktree"]["branch"] = "changed"
                identity = "changed"
            changed.called = True
            return value, identity
        changed.called = False
        with patch("pod.admission._primary", side_effect=changed):
            result = self.query()
        self.assertIn("record_changed", [row["code"] for row in result["unknowns"]])

    def test_cli_json_and_text_are_read_only(self):
        self.intake(worktree=self.place)
        path, _ = self.raw()
        before = path.read_bytes()
        for flags in (["--json"], []):
            out = StringIO()
            with redirect_stdout(out):
                code = cli.main(["admission", "--repository", str(self.project), *flags])
            self.assertEqual(code, 0)
            if flags:
                self.assertEqual(json.loads(out.getvalue())["schema"], "pod-admission-query/v1")
            else:
                self.assertIn("Pod admission facts: observed", out.getvalue())
        self.assertEqual(path.read_bytes(), before)

    def test_unsupported_schema_is_a_scoped_unknown(self):
        self.intake(worktree=self.place)
        path, raw = self.raw()
        raw["schema"] = "pod-context/v99"
        self.save(path, raw)
        result = self.query()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["unknowns"][0]["record"], path.parent.name)
        self.assertEqual(result["unknowns"][0]["code"], "state_unsupported")

    def test_native_scope_pages_and_replay_are_verified(self):
        from pod.errors import PodError
        first = {"runtime": "runtime", "result": {"scope": {"source": "all"},
                 "workers": [{"dispatchId": "one"}], "page": {"hasMore": True, "nextCursor": "next"}}}
        last = {"runtime": "runtime", "result": {"scope": {"source": "all"},
                "workers": [{"dispatchId": "two"}], "page": {"hasMore": False}}}
        real_fleet = self._real_fleet
        with patch("pod.admission._read", side_effect=[first, last]):
            result = real_fleet()
        self.assertEqual([row["dispatchId"] for row in result["workers"]], ["one", "two"])
        for bad in ({**last, "runtime": "changed"},
                    {**last, "result": {**last["result"], "scope": {"source": "bound", "run": "run"}}},
                    {**last, "result": {**last["result"], "workers": [{"dispatchId": "one"}]}}):
            with self.subTest(bad=bad), patch("pod.admission._read", side_effect=[first, bad]), self.assertRaises(PodError):
                real_fleet()

    def test_bound_native_inventory_reports_coverage_without_history_reads(self):
        self.intake(worktree=self.place)
        # Intake records its exact Run; no global or another bound Run can stand in.
        with patch("pod.admission._fleet", return_value=None), \
             patch("pod.orca.worker_rows", side_effect=AssertionError("prior Run hydration")) as read:
            result = self.query()
        self.assertFalse(result["complete"])
        self.assertIn("native_inventory_scope", [row["code"] for row in result["unknowns"]])
        self.assertEqual(read.call_count, 0)
        with patch("pod.admission._read", return_value={"runtime": "runtime", "result": {"scope": {"source": "bound", "run": "run"}}}):
            self.assertIsNone(self._real_fleet())

    def test_incomplete_native_inventory_never_becomes_empty(self):
        self.intake(worktree=self.place)
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": False, "workers": []}):
            result = self.query()
        self.assertEqual(result["status"], "unknown")
        self.assertIn("native_inventory_incomplete", [row["code"] for row in result["unknowns"]])

    def test_new_native_run_without_a_pod_admission_conflicts_by_branch(self):
        self.intake(worktree=self.place)
        writer = {"dispatchId": "hidden", "runId": "unrecorded", "taskId": "task",
                  "agentTerminalHandle": "terminal", "resource": {"worktreeId": str(self.project)},
                  "projection": {"id": "worker", "dispatchId": "hidden", "runId": "unrecorded", "taskId": "task",
                                 "liveness": {"verdict": "live"}, "resource": {"state": "retained"}}}
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": [writer]}):
            result = self.query(branch="main")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("hidden", [row["claim"].get("dispatch") for row in result["conflicts"]])

    def test_archived_admission_binding_is_checked_without_loading_other_history(self):
        self.intake(worktree=self.place)
        path, raw = self.raw()
        binding = {"runId": "run", "taskId": "task", "workerId": "worker", "dispatchId": "dispatch",
                   "worktreeId": str(self.project), "terminalHandle": "terminal"}
        row = {"schema": "pod-admission/v4", "admission_id": "a", "state": "closed", "role": "implement",
               "native_binding": binding, "runtime": "runtime"}
        chunks = {}
        raw["admissions"]["a"] = history._reference(row, chunks)
        raw["history"] = history.FORMAT
        history.ChunkStore(path.parent).put(chunks)
        self.save(path, raw)
        with patch("pod.history.load_context", side_effect=AssertionError("full hydration")):
            result = self.query()
        self.assertIn("native_identity_unverified", [row["code"] for row in result["unknowns"]])
        for chunk in (path.parent / "history" / "sha256").iterdir():
            chunk.unlink()
        result = self.query()
        self.assertIn("history_missing", [row["code"] for row in result["unknowns"]])

    def test_effect_inventory_includes_pending_and_unknown_but_not_deferred(self):
        from pod.governor import _empty_journal, _record_path
        self.intake(worktree=self.place)
        journal = _empty_journal()
        for i, (decision, outcome, kind) in enumerate((("ALLOW", "pending", "merge"),
                ("ALLOW", "UNKNOWN", "deploy"), ("DEFER", "pending", "merge"), ("ALLOW", "PASS", "push"))):
            journal["actions"].append({"record_id": str(i), "logical_key": str(i), "attempt": 1,
                "action": {"kind": kind, "candidate": self.base, "target": "target", "unit": "u"},
                "decision": decision, "outcome": outcome, "receipt": {}, "warnings": [], "reasons": [],
                "classifications": [], "run_event": None, "rejected_run": None})
        _record_path(self.project, "objective").write_text(json.dumps(journal))
        result = self.query(target="target", effects=["deploy:target"])
        effects = [effect for row in result["claims"] for effect in row.get("effects") or []]
        self.assertCountEqual(effects, ["merge:target", "deploy:target"])
        self.assertEqual(result["status"], "blocked")

    def test_concurrent_inventory_addition_and_symlink_are_unknown(self):
        self.intake(worktree=self.place)
        root = state_root(self.project)
        real = admission._fleet
        def add_record():
            (root / "late").mkdir()
            return real()
        with patch("pod.admission._fleet", side_effect=add_record):
            result = self.query()
        self.assertIn("inventory_changed", [row["code"] for row in result["unknowns"]])
        (root / "redirect").symlink_to(root / "late")
        result = self.query()
        self.assertIn("unsafe_state", [row["code"] for row in result["unknowns"]])

    def test_snapshot_recheck_detects_new_ownership_claim(self):
        self.intake(worktree=self.place)
        before = self.query(branch="new-branch")
        self.write([self.criterion()], objective="new-owner", governance={"base_ref": "target"},
                   worktree={**self.place, "branch": "new-branch"})
        after = self.query(branch="new-branch", expect=before["snapshot"])
        self.assertEqual(after["status"], "blocked")
        self.assertIn("snapshot_changed", [row.get("kind") for row in after["conflicts"]])
        self.assertTrue(any("branch" in row.get("dimensions", []) for row in after["conflicts"]))

    def test_query_deadline_cuts_off_record_reads_as_unknown(self):
        from pod.errors import PodError
        from pod.orca import DEADLINE_SPENT
        self.intake(worktree=self.place)
        with patch("pod.admission._primary", side_effect=PodError(DEADLINE_SPENT, "spent")):
            result = self.query(timeout=0.1)
        self.assertEqual(result["status"], "unknown")
        self.assertIn(DEADLINE_SPENT, [row["code"] for row in result["unknowns"]])

    def test_allowlist_accepts_only_the_native_inventory_shapes(self):
        from pod.orca import _read_allowed
        prefix = ["orchestration", "worker-list", "--include-remote", "--json"]
        self.assertTrue(_read_allowed([*prefix, "--limit", "100"]))
        self.assertTrue(_read_allowed([*prefix, "--limit", "100", "--cursor", "next"]))
        self.assertFalse(_read_allowed([*prefix, "--limit", "101"]))
        self.assertFalse(_read_allowed([*prefix, "--cursor", "next", "--run", "other"]))


class CandidateProofTests(KernelCase):
    from tests.kernel_support import assurance_review as review, assurance_satisfied as satisfied

    def test_exact_proof_is_ready_and_changed_candidate_cannot_reuse_it(self):
        from pod.operations import OrcaPort
        self.satisfied()
        rows = self.stored()
        criterion = next(row for row in rows if row["id"] == "O1")
        criterion.pop("executor", None)
        criterion.update(state="satisfied", evidence=[self.proof("O1")])
        self.write(rows)
        with patch.object(OrcaPort, "read_native", autospec=True,
                side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            proof = admission._proof(self.project, "objective", self.base)
        self.assertEqual(proof["status"], "ready", proof)
        changed = admission._proof(self.project, "objective", "b" * 40)
        self.assertEqual(changed["status"], "blocked")
        self.assertEqual(changed["reason"], "candidate_mismatch")

    def test_missing_required_evidence_and_unverified_native_stay_unready(self):
        from pod.operations import OrcaPort
        from pod.errors import PodError
        self.satisfied()
        with patch.object(OrcaPort, "read_native", side_effect=PodError("orca_unavailable", "unavailable")):
            proof = admission._proof(self.project, "objective", self.base)
        self.assertEqual(proof["status"], "unknown")

    def test_premerge_proof_does_not_require_future_objective_closure(self):
        from pod.operations import OrcaPort
        self.satisfied()
        self.write([*self.stored(), self.sub("DELIVER")])
        with patch.object(OrcaPort, "read_native", autospec=True,
                side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            proof = admission._proof(self.project, "objective", self.base)
        self.assertEqual(proof["status"], "ready", proof)
        self.assertEqual(proof["purpose"], "candidate_proof")
        self.assertFalse(proof["objective_complete"])
        self.assertIn("DELIVER", proof["objective_unfinished"])


class AttributionReadTests(AdmissionQueryCase):
    """Attribution excludes only repository-bound primary records."""
    def foreign_record(self, name="foreign"):
        other = self.root / (name + "-repository")
        other.mkdir(); git(other, "init", "-q", "-b", "main")
        path, raw = self.raw()
        raw["checkpoint"]["objective"] = name
        raw["checkpoint"]["worktree"] = {**self.place, "path": str(other),
                                         "repo_key": repository_context(other)["repo_key"]}
        chunks = {}
        raw["history"] = history.FORMAT
        raw["admissions"]["unused"] = history._reference({"unused": "retained" * 100}, chunks)
        destination = objective_root(other, name) / "context.json"
        destination.parent.mkdir()
        destination.write_text(json.dumps(raw))
        # Missing foreign chunks make any accidental hydration observable.
        return destination

    def test_foreign_finished_records_load_zero_chunks_and_one_native_inventory(self):
        self.intake(worktree=self.place)
        self.foreign_record()
        native = {"runtime": "runtime", "result": {"scope": {"source": "all"},
                  "workers": [], "page": {"hasMore": False}}}
        def native_read(argv):
            if argv[:2] == ["terminal", "list"]:
                return {"runtime": "runtime", "result": {"terminals": [], "totalCount": 0, "truncated": False,
                        "hostScope": {"hostIds": ["local"], "omittedHostIds": []}}}
            return native
        with patch("pod.admission._fleet", side_effect=self._real_fleet), \
             patch("pod.admission._terminals", side_effect=self._real_terminals), \
             patch("pod.admission._read", side_effect=native_read) as native_reads, \
             patch("pod.history._context_bytes", wraps=history._context_bytes) as files, \
             patch("pod.history.ChunkStore.load", side_effect=AssertionError("foreign chunk")) as chunks, \
             patch("pod.governor._read_journal", wraps=governor._read_journal) as journals:
            result = self.query()
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertEqual(files.call_count, 3)  # two primaries, one relevant recheck
        self.assertEqual(chunks.call_count, 0)
        self.assertEqual(native_reads.call_count, 2)
        self.assertEqual(journals.call_count, 2)

    def test_wrong_record_key_is_unknown_instead_of_foreign(self):
        self.intake(worktree=self.place)
        path, raw = self.raw()
        raw["checkpoint"]["objective"] = "different-name"
        self.save(path, raw)
        result = self.query()
        self.assertEqual(result["status"], "unknown")
        self.assertIn("record_attribution_unknown", [row["code"] for row in result["unknowns"]])

    def test_unrelated_finished_primary_reencoding_preserves_claim_generation(self):
        self.intake(worktree=self.place)
        self.write([self.criterion("satisfied", evidence=[self.proof("O1")])], objective="old",
                   governance={"base_ref": "target"}, close=True, worktree=self.place)
        path, raw = self.raw("old")
        before = self.query()
        real = admission._fleet
        def rewrite_history():
            path.write_text(json.dumps(raw, indent=2))
            return real()
        with patch("pod.admission._fleet", side_effect=rewrite_history):
            result = self.query()
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertEqual(result["snapshot"], before["snapshot"])

    def test_new_finished_record_without_current_claim_preserves_snapshot(self):
        self.intake(worktree=self.place)
        before = self.query()
        self.write([self.criterion("satisfied", evidence=[self.proof("O1")])], objective="finished",
                   governance={"base_ref": "target"}, close=True, worktree=self.place)
        after = self.query(expect=before["snapshot"])
        self.assertTrue(after["complete"], after["unknowns"])
        self.assertEqual(after["status"], "observed")
        self.assertEqual(after["snapshot"], before["snapshot"])

    def test_unrelated_record_creation_and_history_churn_do_not_restart_query(self):
        self.intake(worktree=self.place)
        real = admission._fleet
        def add_foreign():
            self.foreign_record()
            return real()
        with patch("pod.admission._fleet", side_effect=add_foreign):
            result = self.query()
        self.assertTrue(result["complete"], result["unknowns"])

    def test_more_than_1024_foreign_records_has_no_history_cliff(self):
        self.intake(worktree=self.place)
        other = self.root / "many"
        other.mkdir(); git(other, "init", "-q", "-b", "main")
        context = repository_context(other)
        records = state_root(self.project)
        for index in range(1030):
            name = f"finished-{index}"
            directory = records / record_name(other, context, name)
            directory.mkdir()
            raw = {"schema": "pod-context/v4", "checkpoint": {"schema": "pod-checkpoint/v3",
                   "objective": name, "worktree": {**self.place, "path": str(other), "repo_key": context["repo_key"]}},
                   "admissions": {"old": history._reference({"old": "report"}, {})}}
            self.save(directory / "context.json", raw)
        with patch("pod.history.ChunkStore.load", side_effect=AssertionError("foreign chunks")):
            result = self.query()
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertEqual(result["status"], "observed")


class FactsContractTests(AdmissionQueryCase):
    def facts(self, include_admissions):
        return {"schema": "pod-admission-facts/v1", "generation": {"revision": 1, "sha256": "a" * 64},
                "phase": "open", "attribution_complete": True, "complete": include_admissions,
                "context": {"schema": "pod-context/v4", "revision": 1, "owner": "owner",
                    "checkpoint": {"schema": "pod-checkpoint/v3", "objective": "objective",
                                   "worktree": self.place, "native_refs": [{"runId": "run", "runtime": "runtime"}]}},
                "admissions": {} if include_admissions else None}

    def test_versioned_storage_projection_does_not_load_full_context(self):
        self.intake(worktree=self.place)
        def read(path, *, include_admissions):
            return self.facts(include_admissions)
        with patch("pod.history.load_admission_facts", create=True, side_effect=read) as reader, \
             patch("pod.history.load_context", side_effect=AssertionError("full context")):
            result = self.query()
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertEqual([call.kwargs["include_admissions"] for call in reader.call_args_list], [False, True, False])

    def test_incomplete_or_changed_claim_generation_is_unknown(self):
        self.intake(worktree=self.place)
        def incomplete(path, *, include_admissions):
            facts = self.facts(include_admissions)
            facts["complete"] = False
            return facts
        with patch("pod.history.load_admission_facts", create=True, side_effect=incomplete):
            result = self.query()
        self.assertIn("admission_facts_incomplete", [row["code"] for row in result["unknowns"]])
        def changed(path, *, include_admissions):
            facts = self.facts(include_admissions)
            if include_admissions:
                facts["generation"]["sha256"] = "b" * 64
            return facts
        with patch("pod.history.load_admission_facts", create=True, side_effect=changed):
            result = self.query()
        self.assertIn("record_changed", [row["code"] for row in result["unknowns"]])

    def test_effect_projection_avoids_full_journal_history(self):
        self.intake(worktree=self.place)
        facts = {"schema": "pod-admission-effects/v1", "complete": True,
                 "generation": {"revision": 1, "sha256": "a" * 64}, "units": {},
                 "actions": [{"record_id": "pending", "decision": "ALLOW", "outcome": "UNKNOWN",
                              "action": {"kind": "merge", "candidate": self.base, "target": "target", "unit": "u"}}]}
        with patch("pod.governor.load_admission_effects", create=True, return_value=facts), \
             patch("pod.governor._read_journal", side_effect=AssertionError("retained journal")):
            result = self.query(target="target")
        self.assertEqual(result["status"], "blocked", result["unknowns"])
        self.assertEqual([effect for row in result["claims"] for effect in row.get("effects") or []], ["merge:target"])


class ReviewCorrectionTests(AdmissionQueryCase):
    def journal(self, kind="workflow_dispatch", target="ci.yml", declared=None, recorded=None, directory=None):
        from pod.governor import _empty_journal
        value = _empty_journal()
        row = {"record_id": "effect", "logical_key": "effect", "attempt": 1,
               "action": {"kind": kind, "candidate": self.base, "target": target, "unit": "u"},
               "decision": "ALLOW", "outcome": "UNKNOWN", "receipt": {}, "warnings": [], "reasons": [],
               "classifications": [], "run_event": None, "rejected_run": None}
        if declared is not None:
            row["action"]["effects"] = declared
        if recorded is not None:
            row["effects"] = recorded
        value["actions"].append(row)
        path = directory or objective_root(self.project, "objective")
        (path / "governor.json").write_text(json.dumps(value))

    def writer(self, path=None, verdict="unverifiable", dispatch="writer"):
        return {"dispatchId": dispatch, "taskId": "task", "runId": "run", "agentTerminalHandle": "terminal",
                "resource": {"worktreeId": str(path or self.project)},
                "projection": {"id": "worker", "dispatchId": dispatch, "taskId": "task", "runId": "run",
                               "workspace": {"id": str(path or self.project)},
                               "liveness": {"verdict": verdict}, "resource": {"state": "retained"}}}

    def test_closed_stale_runtime_has_no_permanent_unknown(self):
        self.write([self.criterion("satisfied", evidence=[self.proof("O1")])],
                   governance={"base_ref": "target"}, close=True, worktree=self.place)
        path, raw = self.raw()
        for ref in raw["checkpoint"]["native_refs"]:
            ref["runtime"] = "previous-runtime"
        self.save(path, raw)
        result = self.query(worktree=str(self.project), branch="main")
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertFalse(result["authorization"])
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True,
                                                        "workers": [self.writer(verdict="live")]}):
            result = self.query(worktree=str(self.project), branch="main")
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["conflicts"])

    def test_open_stale_runtime_remains_scoped_uncertainty(self):
        self.intake(worktree=self.place)
        path, raw = self.raw()
        raw["checkpoint"]["native_refs"][0]["runtime"] = "previous-runtime"
        self.save(path, raw)
        result = self.query(worktree=str(self.project), branch="main")
        self.assertIn("native_runtime_changed", [row["code"] for row in result["unknowns"]])

    def test_governor_intrinsic_and_declared_effects_are_both_visible(self):
        self.intake(worktree=self.place)
        self.journal(declared=["deploy:prod"])
        for effect in ("workflow:ci.yml", "deploy:prod"):
            with self.subTest(effect=effect):
                result = self.query(effects=[effect])
                self.assertEqual(result["status"], "blocked", result["unknowns"])
                self.assertEqual(next(row["effects"] for row in result["claims"] if "effects" in row), ["deploy:prod", "workflow:ci.yml"])

    def test_recorded_mapping_survives_current_policy_changes(self):
        self.intake(worktree=self.place)
        self.journal(kind="push", target="origin/topic", recorded=["workflow:earlier.yml"])
        result = self.query(effects=["workflow:earlier.yml"])
        self.assertEqual(result["status"], "blocked", result["unknowns"])

    def test_current_mapped_effects_and_unknown_effects_are_distinct(self):
        self.intake(worktree=self.place)
        self.journal(kind="push", target="origin/topic")
        config = self.project / ".pod" / "config.yaml"
        config.parent.mkdir()
        config.write_text('schema: pod/v1\nwaste_governor:\n  triggers:\n    push: ["workflow:mapped.yml"]\n')
        result = self.query(effects=["workflow:mapped.yml"])
        self.assertEqual(result["status"], "blocked", result["unknowns"])
        config.unlink()
        result = self.query(effects=["deploy:prod"])
        self.assertEqual(result["status"], "unknown")
        self.assertIn("effects_unknown", [row["code"] for row in result["unknowns"]])
        self.assertTrue(result["unknowns"][0]["reconciliation"])

    def test_foreign_source_cannot_hide_a_shared_effect(self):
        self.intake(worktree=self.place)
        other = self.root / "other"
        other.mkdir(); git(other, "init", "-q", "-b", "main")
        path, raw = self.raw()
        raw["checkpoint"]["objective"] = "foreign"
        raw["checkpoint"]["worktree"] = {**self.place, "path": str(other),
                                         "repo_key": repository_context(other)["repo_key"]}
        foreign = objective_root(other, "foreign")
        foreign.mkdir()
        self.save(foreign / "context.json", raw)
        self.journal(declared=["deploy:prod"], directory=foreign)
        with patch("pod.history.ChunkStore.load", side_effect=AssertionError("foreign history")):
            result = self.query(effects=["deploy:prod"])
        self.assertEqual(result["status"], "blocked", result["unknowns"])
        self.assertEqual(next(row["record"] for row in result["claims"] if "effects" in row), foreign.name)

    def test_unrelated_process_uncertainty_is_disclosed_without_veto(self):
        self.intake(worktree=self.place)
        sibling = self.root / "sibling"
        git(self.project, "worktree", "add", "-q", str(sibling), "-b", "other")
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True,
                                                        "workers": [self.writer(sibling)]}):
            result = self.query(worktree=str(self.project), branch="main")
            conflict = self.query(worktree=str(sibling), branch="other")
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertFalse(result["repository_complete"])
        self.assertTrue(result["unrelated_unknowns"])
        self.assertEqual(conflict["status"], "unknown")
        self.assertTrue(conflict["conflicts"])

    def test_malformed_effect_projection_is_a_scoped_error(self):
        self.intake(worktree=self.place)
        facts = {"schema": "pod-admission-effects/v1", "complete": True,
                 "generation": {"revision": 1, "sha256": "a" * 64}, "units": {}, "actions": [{}]}
        with patch("pod.governor.load_admission_effects", create=True, return_value=facts):
            result = self.query(effects=["deploy:prod"])
        self.assertIn("admission_effects_incomplete", [row["code"] for row in result["unknowns"]])

    def test_known_repository_keys_attribute_missing_worktree_without_migration(self):
        self.intake(worktree=self.place)
        other = self.root / "other"
        other.mkdir(); git(other, "init", "-q", "-b", "main")
        _, raw = self.raw()
        raw["checkpoint"].pop("worktree")
        raw["checkpoint"]["objective"] = "foreign"
        foreign = objective_root(other, "foreign")
        foreign.mkdir(); self.save(foreign / "context.json", raw)
        before = (foreign / "context.json").read_bytes()
        result = self.query(known_repositories=[str(other)])
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertEqual((foreign / "context.json").read_bytes(), before)

    def test_manual_native_terminal_is_a_current_writer_without_a_pod_admission(self):
        self.intake(worktree=self.place)
        terminal = {"handle": "manual", "worktreePath": str(self.project), "connected": True,
                    "writable": True, "orphaned": False, "exitCause": None}
        with patch("pod.admission._terminals", return_value={"runtime": "runtime", "complete": True, "terminals": [terminal]}):
            result = self.query(worktree=str(self.project), branch="main")
        self.assertTrue(result["complete"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("manual", [row["claim"].get("native_terminal") for row in result["conflicts"]])

    def test_settled_unsupervised_dispatch_uses_current_terminal_ownership(self):
        self.intake(worktree=self.place)
        source = self.writer()
        source.update(workerState="unsupervised", dispatchStatus="completed", resource=None)
        source["projection"].update(workspace=None, stage={"worker": "unsupervised", "dispatch": "completed"}, outcome="succeeded")
        terminal = {"handle": "terminal", "worktreePath": str(self.project), "connected": True,
                    "writable": True, "orphaned": False}
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": [source]}), \
             patch("pod.admission._terminals", return_value={"runtime": "runtime", "complete": True, "terminals": [terminal]}):
            held = self.query(worktree=str(self.project), branch="main")
            independent = self.query(worktree=str(self.root / "independent"), branch="new")
        self.assertEqual(held["status"], "blocked")
        self.assertFalse(held["unknowns"])
        self.assertEqual(independent["status"], "observed")
        self.assertTrue(independent["complete"])
        self.assertFalse(independent["authorization"])

    def test_terminal_reader_requires_complete_inventory_and_strips_preview(self):
        from pod.errors import PodError
        result = {"runtime": "runtime", "result": {"terminals": [{"handle": "manual", "worktreePath": str(self.project),
                  "connected": True, "writable": True, "orphaned": False, "preview": "PRIVATE_PREVIEW"}],
                  "totalCount": 1, "truncated": False, "hostScope": {"hostIds": ["local"], "omittedHostIds": []}}}
        with patch("pod.admission._read", return_value=result):
            observed = self._real_terminals()
        self.assertNotIn("PRIVATE_PREVIEW", json.dumps(observed))
        result["result"]["truncated"] = True
        with patch("pod.admission._read", return_value=result), self.assertRaises(PodError):
            self._real_terminals()

    def test_positive_owner_identity_does_not_invent_a_different_writer_per_purpose(self):
        self.intake(worktree=self.place)
        self.write([self.criterion()], objective="retained-purpose", governance={"base_ref": "target"}, worktree=self.place)
        for name in ("objective", "retained-purpose"):
            path, raw = self.raw(name)
            raw["checkpoint"]["native_refs"][0]["runtime"] = "earlier-runtime"
            self.save(path, raw)
        owner = {"run": "run", "runtime": "runtime", "terminal": "owner", "consumer_generation": 4,
                 "worktree": str(self.project), "live": True, "source": "native_run_and_terminal"}
        with patch("pod.admission._owner_observation", return_value=owner) as reads:
            result = self.query(worktree=str(self.project), branch="main")
        ownership = [row for row in result["claims"] if row["kind"] == "ownership"]
        self.assertEqual(len(ownership), 2)
        self.assertTrue(all(row["owner_identity_matches"] for row in ownership))
        self.assertTrue(all(row["recorded_owner"] == "owner" for row in ownership))
        self.assertEqual(reads.call_count, 1)
        self.assertNotIn("native_runtime_changed", [row["code"] for row in result["unknowns"]])
        self.assertEqual(len(result["recorded_runtime_changes"]), 2)
        self.assertFalse(result["authorization"])

    def test_owner_observation_joins_run_and_live_terminal(self):
        run = {"runtime": "runtime", "coordinator_handle": "owner", "consumer_generation": 7}
        terminals = {"terminals": [{"handle": "owner", "worktreePath": str(self.project), "connected": True,
                                  "writable": True, "orphaned": False}]}
        with patch("pod.orca.run_show", return_value=run):
            observed = self._real_owner_observation("run", terminals, "runtime")
        self.assertEqual(observed["terminal"], "owner")
        self.assertEqual(observed["consumer_generation"], 7)
        self.assertTrue(observed["live"])

    def test_abandoned_effects_are_interpreted_by_shared_reader_and_stay_material(self):
        self.intake(worktree=self.place)
        source = self.writer(verdict="exited")
        source["workerState"] = "abandoned"
        claim = {"process": "exited", "effects": [{"kind": "external", "id": "prod", "state": "pending"}]}
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": [source]}), \
             patch("pod.admission._native_claim", return_value=claim) as reader:
            result = self.query(worktree=str(self.project), branch="main")
        self.assertEqual(result["status"], "blocked", result["unknowns"])
        self.assertEqual(reader.call_count, 1)
        self.assertTrue(any(row.get("native_effects") for row in result["claims"]))

    def test_missing_shared_reader_does_not_hide_possible_cross_repository_effect(self):
        from pod.errors import PodError
        self.intake(worktree=self.place)
        source = self.writer(verdict="exited")
        source["workerState"] = "abandoned"
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": [source]}), \
             patch("pod.admission._native_claim", side_effect=PodError("native_claim_reader_unavailable", "missing")):
            result = self.query(worktree=str(self.root / "independent"), branch="new", effects=["deploy:prod"])
        self.assertEqual(result["status"], "unknown")
        self.assertIn("native_claim_reader_unavailable", [row["code"] for row in result["unknowns"]])

    def test_known_previous_binding_metadata_does_not_upgrade_candidate_proof(self):
        self.intake(worktree=self.place)
        path, raw = self.raw()
        raw["checkpoint"]["schema"] = "pod-checkpoint/v2"
        self.save(path, raw)
        result = self.query(worktree=str(self.root / "independent"), branch="new")
        self.assertTrue(result["complete"], result["unknowns"])
        selected = self.query(objective="objective", candidate=self.base)
        self.assertEqual(selected["proof"]["status"], "unknown")
        self.assertEqual(selected["proof"]["reason"], "state_unsupported")

    def test_closed_without_admissions_or_effects_keeps_attribution_gap_disclosed(self):
        self.write([self.criterion("satisfied", evidence=[self.proof("O1")])],
                   governance={"base_ref": "target"}, close=True, worktree=self.place)
        path, raw = self.raw()
        raw["checkpoint"].pop("worktree")
        self.save(path, raw)
        detached = path.parent.with_name("unattributed-closed")
        path.parent.rename(detached)
        result = self.query(worktree=str(self.project), branch="main")
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertIn("record_attribution_unknown", result["unrelated_unknowns"]["by_code"])
        self.journal(directory=detached)
        result = self.query(worktree=str(self.project), branch="main", effects=["workflow:ci.yml"])
        self.assertFalse(result["complete"])
        self.assertIn("record_attribution_unknown", [row["code"] for row in result["unknowns"]])

    def test_foreign_placement_scope_requires_exact_current_native_identity(self):
        self.intake(worktree=self.place)
        other = self.root / "other"
        other.mkdir(); git(other, "init", "-q", "-b", "main")
        _, raw = self.raw()
        removed = self.root / "removed-worker"
        raw["checkpoint"]["objective"] = "foreign-owner"
        raw["checkpoint"]["worktree"] = {**self.place, "path": str(other), "repo_key": repository_context(other)["repo_key"]}
        native = self.writer(removed)
        binding = {"runId": "run", "taskId": "task", "dispatchId": "writer", "workerId": "worker",
                   "worktreeId": str(removed), "terminalHandle": "terminal"}
        raw["admissions"] = {"a": {"schema": "pod-admission/v4", "admission_id": "a", "state": "bound",
            "role": "review", "runtime": "runtime", "native_binding": binding,
            "recovery": {"placement_binding": {**self.place, "path": str(removed), "branch": "old-worker"}}}}
        directory = objective_root(other, "foreign-owner")
        directory.mkdir()
        for task, complete in (("task", True), ("different-task", False)):
            with self.subTest(task=task):
                binding["taskId"] = task
                self.save(directory / "context.json", raw)
                with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": [native]}):
                    result = self.query(worktree=str(self.project), branch="main")
                self.assertEqual(result["complete"], complete, result["unknowns"])

    def test_successful_fact_read_exits_zero_for_intersections_and_uncertainty(self):
        self.intake(worktree=self.place)
        for invalid, expected in ((False, "blocked"), (True, "unknown")):
            if invalid:
                path, _ = self.raw()
                path.write_text("{")
            out = StringIO()
            with redirect_stdout(out):
                code = cli.main(["admission", "--repository", str(self.project), "--worktree", str(self.project),
                                 "--branch", "main", "--json"])
            result = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["scope"]["path"], str(self.project))
            self.assertFalse(result["authorization"])
        out = StringIO()
        with redirect_stdout(out):
            code = cli.main(["admission", "--repository", str(self.project), "--objective", "objective", "--json"])
        self.assertEqual(code, 1)

    def test_git_target_aliases_identify_the_same_pending_target_update(self):
        self.intake(worktree=self.place)
        git(self.project, "update-ref", "refs/remotes/origin/main", self.base)
        self.journal(kind="merge", target="origin/main")
        result = self.query(target="refs/remotes/origin/main", target_commit=self.base)
        self.assertEqual(result["status"], "blocked", result["unknowns"])
        self.assertEqual(result["scope"]["selection"]["target"], "refs/remotes/origin/main")
        self.assertEqual(result["target"]["ref"], "refs/remotes/origin/main")
        self.assertTrue(any("target_update" in row.get("dimensions", []) for row in result["conflicts"]))

    def test_unrelated_diagnostics_are_counts_and_unique_resource_references(self):
        self.intake(worktree=self.place)
        sibling = self.root / "sibling"
        git(self.project, "worktree", "add", "-q", str(sibling), "-b", "other")
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": [self.writer(sibling)]}):
            result = self.query(worktree=str(self.project), branch="main")
        summary = result["unrelated_unknowns"]
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["by_code"], {"native_liveness_unverified": 1})
        self.assertEqual(summary["references"]["dispatches"], ["writer"])
        self.assertNotIn("reconciliation", summary)
        self.assertNotIn("scope", summary)

    def test_native_folder_inside_repository_is_not_a_hidden_manual_writer(self):
        self.intake(worktree=self.place)
        terminal = {"handle": "nested", "worktreePath": str(self.project / "src"), "connected": True,
                    "writable": True, "orphaned": False}
        with patch("pod.admission._terminals", return_value={"runtime": "runtime", "complete": True, "terminals": [terminal]}):
            result = self.query(worktree=str(self.project))
        self.assertEqual(result["status"], "blocked", result["unknowns"])
        self.assertTrue(any(row["claim"].get("native_terminal") == "nested" for row in result["conflicts"]))

    def test_malformed_binding_and_placement_are_scoped_unknowns(self):
        self.intake(worktree=self.place)
        path, raw = self.raw()
        base = {"schema": "pod-admission/v4", "admission_id": "a", "state": "reserved", "role": "implement", "runtime": "runtime"}
        for extra, code in (({"native_binding": "invalid"}, "admission_binding_invalid"),
                            ({"recovery": "invalid"}, "admission_placement_invalid")):
            with self.subTest(code=code):
                raw["admissions"] = {"a": {**base, **extra}}
                self.save(path, raw)
                result = self.query()
                self.assertIn(code, [row["code"] for row in result["unknowns"]])

    def test_target_alias_uses_registered_common_repository_when_old_worktree_is_gone(self):
        self.intake(worktree=self.place)
        path, raw = self.raw()
        raw["checkpoint"]["worktree"]["path"] = str(self.root / "removed")
        self.save(path, raw)
        git(self.project, "update-ref", "refs/remotes/origin/main", self.base)
        self.journal(kind="merge", target="origin/main")
        result = self.query(target="refs/remotes/origin/main", target_commit=self.base)
        self.assertEqual(result["status"], "blocked", result["unknowns"])
        self.assertTrue(any("target_update" in row.get("dimensions", []) for row in result["conflicts"]))
