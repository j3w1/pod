"""R185: production admission reads, selective evidence and conservative gaps."""
from contextlib import contextmanager, redirect_stdout
import copy
from io import StringIO
import json
import os
from pathlib import Path
import threading
from unittest.mock import patch

from pod import admission, cli, governor, history
from pod.errors import PodError
from pod.ledger import objective_root, record_name, state_root
from pod.github import repository_context
from tests.kernel_support import KernelCase, git
from tests.common import logical_json
from tests.history_support import _reference


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
        return path, logical_json(path)

    def save(self, path, raw):
        path.write_text(json.dumps(raw))

    def query(self, **kwargs):
        return admission.query(self.project, **kwargs)

    def without_admissions(self, row):
        """The open objective's record with admission `row` (A), and the same record without it, at the same revision
        (B): an admissions-only change the attribution read cannot see."""
        path, raw = self.raw()
        raw["admissions"]["a"] = row
        path.write_bytes(json.dumps(raw).encode())
        raw["admissions"] = {}
        return path, json.dumps(raw).encode()

    def reserved_writer(self):
        return self.without_admissions({"schema": "pod-admission/v4", "admission_id": "a", "state": "reserved",
                                        "role": "implement", "native_binding": None, "runtime": "runtime",
                                        "worktree": str(self.project), "boundary": None,
                                        "recovery": {"placement_binding": dict(self.place)}})

    @contextmanager
    def claims_read_meets(self, path, intermediate, *, restore=True):
        """The call's first claims read of `path` meets `intermediate`; with `restore` the original bytes return
        right after it, before the change recheck (A/B/A, the pod#222 review)."""
        original, read = path.read_bytes(), history.load_admission_facts

        def claims(target, *, include_admissions=True):
            changing = target == path and include_admissions and admission._SHARED.get().stage == "first"
            if changing:
                path.write_bytes(intermediate)
            try:
                return read(target, include_admissions=include_admissions)
            finally:
                if changing and restore:
                    path.write_bytes(original)
        with patch("pod.history.load_admission_facts", side_effect=claims):
            yield
        self.assertEqual(path.read_bytes(), original if restore else intermediate)

    @contextmanager
    def current_chunks_only(self):
        """Current index pages are required; unrelated retained payloads remain forbidden."""
        root = objective_root(self.project, "objective")
        original = history.ChunkStore.load
        reads = []
        def load(store, address):
            self.assertEqual(store.root.parent.parent, root, "unrelated history payload")
            reads.append(address)
            return original(store, address)
        with patch.object(history.ChunkStore, "load", load):
            yield reads


class AdmissionQueryTests(AdmissionQueryCase):
    def test_finished_history_does_not_hydrate_unrelated_receipts(self):
        self.intake(worktree=self.place)
        self.write([self.criterion("satisfied", evidence=[self.proof("O1")])], objective="old",
                   governance={"base_ref": "target"}, close=True, worktree=self.place)
        path, raw = self.raw("old")
        # Unrelated retained proof is not an exclusion authority for claims.
        raw["history"] = history.FORMAT
        raw["checkpoint"]["obligations"][0]["receipts"] = [_reference({"old": "x" * 1000}, {})]
        self.save(path, raw)
        with patch("pod.history.load_context", side_effect=AssertionError("full read")), \
             patch("pod.admission._primary", wraps=admission._primary) as reads, \
             self.current_chunks_only() as chunks:
            result = self.query()
        self.assertTrue(result["complete"])
        self.assertEqual(reads.call_count, 2)  # one primary per record; the recheck compares bytes before decoding
        self.assertGreater(len(chunks), 0)
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

    def test_unreadable_and_unattributed_records_hold_only_themselves(self):
        # R206: one unreadable record, plus one healthy objective whose query proceeds.
        self.intake(worktree=self.place)
        root = state_root(self.project)
        (root / "broken").mkdir(); (root / "broken" / "context.json").write_text("")
        (root / "journal-only").mkdir(); (root / "journal-only" / "governor.json").write_text("{}")
        result = self.query()
        self.assertEqual(result["unknowns"], [])
        self.assertTrue(result["complete"])
        self.assertEqual(result["status"], "observed")
        self.assertEqual([row["objective"] for row in result["claims"]], ["objective"])
        self.assertEqual(result["unavailable_records"], [{"record": "broken", "code": "invalid_record"},
                                                         {"record": "journal-only", "code": "unattributed"}])
        self.assertFalse(result["repository_complete"])
        self.assertEqual(result["unrelated_unknowns"]["references"]["records"], ["broken", "journal-only"])

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
            if not changed.called:
                updated = logical_json(p)
                updated["checkpoint"]["worktree"]["branch"] = "changed"
                self.save(p, updated)
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

    def test_an_unsupported_schema_is_named_unavailable_and_holds_only_itself(self):
        self.intake(worktree=self.place)
        path, raw = self.raw()
        raw["schema"] = "pod-context/v99"
        self.save(path, raw)
        result = self.query()
        self.assertEqual((result["status"], result["unknowns"]), ("observed", []))
        self.assertEqual(result["unavailable_records"], [{"record": path.parent.name, "code": "state_unsupported"}])

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

    def test_every_native_page_is_read_however_many_and_a_repeated_cursor_refuses(self):
        # R206: the all-Run inventory has no page count; a repeated cursor still refuses.
        from pod.errors import PodError
        def pages(count, cursor=lambda index: f"c{index}"):
            return [{"runtime": "runtime", "result": {"scope": {"source": "all"},
                     "workers": [{"dispatchId": f"d{index}"}],
                     "page": {"hasMore": index < count - 1, "nextCursor": cursor(index)}}} for index in range(count)]
        with patch("pod.admission._read", side_effect=pages(101)):
            self.assertEqual(len(self._real_fleet()["workers"]), 101)
        with patch("pod.admission._read", side_effect=pages(3, cursor=lambda index: "same")), \
                self.assertRaises(PodError) as caught:
            self._real_fleet()
        self.assertEqual(caught.exception.code, "orca_pagination")

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
        raw["admissions"]["a"] = _reference(row, chunks)
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
        # A redirected record directory is unreadable: it is named and holds only itself (R206).
        self.assertIn({"record": "redirect", "code": "unsafe_state"}, result["unavailable_records"])
        self.assertNotIn("unsafe_state", [row["code"] for row in result["unknowns"]])

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
        prefix = ["orchestration", "worker-list", "--include-remote", "--json", "--full"]
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


class CandidateTreeProofTests(KernelCase):
    """pod#184: the exact-candidate proof does not depend on which checkout `--repository` names (R185). A root
    checked out at the candidate keeps judging bound sources by its working files, as a review binds them; any other
    root judges them against the candidate's stored blobs. The review binds README.md as the worktree holds it when
    the review is admitted; the candidate is the fixture base unless a test commits a new one."""
    from tests.kernel_support import assurance_review as review, assurance_satisfied as satisfied

    def proved(self):
        from pod.records import source_identity
        self.satisfied(sources=[source_identity(self.project, "README.md")])
        rows = self.stored()
        criterion = next(row for row in rows if row["id"] == "O1")
        criterion.pop("executor", None)
        criterion.update(state="satisfied", evidence=[self.proof("O1")])
        self.write(rows)

    def proof_now(self):
        from pod.operations import OrcaPort
        with patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            return admission._proof(self.project, "objective", self.base)

    def commit_readme(self, text):
        (self.project / "README.md").write_text(text)
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "later work")

    def converted(self, kind):
        """A clean checkout whose README.md working bytes differ from its blob (eol or a filter), committed as the
        candidate."""
        readme = self.project / "README.md"
        if kind == "crlf":
            (self.project / ".gitattributes").write_text("README.md text eol=crlf\n")
            readme.write_text("line one\nline two\n")
        else:
            (self.project / ".gitattributes").write_text("README.md filter=fixture\n")
            git(self.project, "config", "filter.fixture.clean", "sed s/WORKTREE/BLOB/g")
            git(self.project, "config", "filter.fixture.smudge", "sed s/BLOB/WORKTREE/g")
            readme.write_text("WORKTREE\n")
        git(self.project, "add", ".gitattributes", "README.md")
        git(self.project, "commit", "-qm", "converted checkout")
        readme.unlink()
        git(self.project, "checkout", "--", "README.md")
        self.assertEqual(git(self.project, "status", "--porcelain"), "")
        blob = git(self.project, "cat-file", "-p", "HEAD:README.md")
        self.assertNotEqual(readme.read_text(newline="").strip(), blob.strip())  # converted on checkout
        self.base = self.candidate = git(self.project, "rev-parse", "HEAD")

    def ready(self, proof):
        self.assertEqual((proof["status"], proof["assurance"]["label"], proof["gaps"]), ("ready", "QUALIFIED", []))

    def withheld(self, proof):
        self.assertEqual((proof["status"], proof["assurance"]["label"]), ("blocked", "WITHHELD"), proof)
        self.assertEqual(proof["assurance"]["assurance_unbound"], [{"obligation": "A", "gap": "invalidated"}])
        self.assertIn("assurance_unbound", proof["gaps"])

    def test_a_root_on_another_branch_reads_the_candidates_own_tree(self):
        self.proved()
        git(self.project, "checkout", "-qb", "other")
        self.commit_readme("another branch's bytes\n")  # the root now holds other bytes at the bound path
        self.ready(self.proof_now())

    def test_proof_bound_to_bytes_the_candidate_tree_lacks_is_invalidated_at_another_root(self):
        self.commit_readme("bytes of a later commit\n")  # the root holds them; the candidate's tree does not
        self.proved()
        self.withheld(self.proof_now())

    def test_a_root_at_the_candidate_keeps_judging_working_files(self):
        (self.project / "README.md").write_text("uncommitted bytes\n")  # unchanged pre-existing behaviour
        self.proved()
        self.ready(self.proof_now())

    def test_a_clean_crlf_checkout_at_the_candidate_stays_ready(self):
        self.converted("crlf")
        self.proved()
        self.ready(self.proof_now())

    def test_a_clean_filtered_checkout_at_the_candidate_stays_ready(self):
        self.converted("filter")
        self.proved()
        self.ready(self.proof_now())

    def converted_elsewhere(self, kind):
        """Converted bytes, proved at the candidate, then read from a root on another commit. Fail-closed and
        documented (R185): the stored blob differs from the bytes the review bound."""
        self.converted(kind)
        self.proved()
        git(self.project, "checkout", "-qb", "other")
        (self.project / "unrelated.txt").write_text("x\n")
        git(self.project, "add", "unrelated.txt")
        git(self.project, "commit", "-qm", "unrelated")
        self.withheld(self.proof_now())

    def test_crlf_bytes_read_from_another_root_are_invalidated(self):
        self.converted_elsewhere("crlf")

    def test_filtered_bytes_read_from_another_root_are_invalidated(self):
        self.converted_elsewhere("filter")


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
        raw["admissions"]["unused"] = _reference({"unused": "retained" * 100}, chunks)
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
             self.current_chunks_only() as chunks, \
             patch("pod.governor._read_journal", wraps=governor._read_journal) as journals:
            result = self.query()
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertEqual(files.call_count, 5)  # two attributions, relevant claims, and both records' census rechecks
        self.assertGreater(len(chunks), 0)
        self.assertEqual(native_reads.call_count, 2)
        self.assertEqual(journals.call_count, 0)  # Current effects use their projection, not a full journal.

    def test_wrong_record_key_is_unknown_instead_of_foreign(self):
        self.intake(worktree=self.place)
        path, raw = self.raw()
        raw["checkpoint"]["objective"] = "different-name"
        self.save(path, raw)
        result = self.query()
        self.assertEqual(result["status"], "unknown")
        self.assertIn("record_attribution_unknown", [row["code"] for row in result["unknowns"]])

    def rewritten_at(self, point, *, changed=False):
        """Rewrite an unrelated finished record from the discovery thread the call's fleet read starts, exactly when
        the record pass reaches `point` for that record (pod#221). The rewrite stores the same content as plain JSON
        in place of the history tree, or, `changed`, adds a Run reference while keeping the revision."""
        self.intake(worktree=self.place)
        self.write([self.criterion("satisfied", evidence=[self.proof("O1")])], objective="old",
                   governance={"base_ref": "target"}, close=True, worktree=self.place)
        path, raw = self.raw("old")
        if changed:
            raw["checkpoint"]["native_refs"] = [*raw["checkpoint"]["native_refs"], {"runId": "run-new", "runtime": "runtime"}]
        before = self.query()
        reached, rewritten, ended = threading.Event(), threading.Event(), threading.Event()

        def rewrite():
            if reached.wait(5) and not ended.is_set():
                path.write_text(json.dumps(raw, indent=2))
                rewritten.set()
        fleet = admission._fleet

        def fleet_read():
            threading.Thread(target=rewrite, daemon=True).start()
            return fleet()

        def reach(name, read):
            if name == point and read == path:
                reached.set()
                self.assertTrue(rewritten.wait(5))
        facts = history.load_admission_facts

        def facts_read(read, *, include_admissions=True):
            reach("claims" if include_admissions else "attribution", read)
            return facts(read, include_admissions=include_admissions)
        recheck = admission._after_context_read

        def recheck_read(read):
            reach("recheck", read)
            value = recheck(read)
            reach("answered", read)
            return value
        with patch("pod.admission._fleet", side_effect=fleet_read), \
             patch("pod.history.load_admission_facts", side_effect=facts_read), \
             patch("pod.admission._after_context_read", side_effect=recheck_read):
            result = self.query()
        ended.set(); reached.set()  # A point the call never reached fails here rather than rewriting late.
        self.assertTrue(rewritten.is_set(), point)
        return before, result

    def reencoded_at(self, point):
        before, result = self.rewritten_at(point)
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertEqual(result["snapshot"], before["snapshot"])

    def changed_at(self, point):
        return [row["code"] for row in self.rewritten_at(point, changed=True)[1]["unknowns"]]

    def test_unrelated_finished_primary_reencoding_preserves_claim_generation(self):
        self.reencoded_at("recheck")  # After the record pass and before the change recheck.

    def test_a_reencoding_before_the_attribution_read_stays_complete(self):
        self.reencoded_at("attribution")

    def test_a_reencoding_between_the_attribution_and_claims_reads_stays_complete(self):
        self.reencoded_at("claims")

    def test_a_reencoding_after_the_recheck_stays_complete(self):
        self.reencoded_at("answered")

    def test_a_change_before_the_attribution_read_is_read_whole(self):
        self.assertEqual(self.changed_at("attribution"), [])

    def test_a_change_between_the_attribution_and_claims_reads_is_record_changed(self):
        self.assertEqual(self.changed_at("claims"), ["record_changed"])

    def test_a_change_before_the_recheck_is_record_changed(self):
        self.assertEqual(self.changed_at("recheck"), ["record_changed"])

    def test_a_change_after_the_recheck_is_after_the_answer(self):
        self.assertEqual(self.changed_at("answered"), [])

    def codes(self, result):
        return [row["code"] for row in result["unknowns"]]

    def test_a_writer_restored_after_the_claims_read_is_record_changed(self):
        self.intake(worktree=self.place)
        path, intermediate = self.reserved_writer()
        self.assertIn("assignment_unbound", self.codes(self.query(worktree=str(self.project))))
        with self.claims_read_meets(path, intermediate):
            result = self.query(worktree=str(self.project))
        self.assertFalse(result["complete"])
        self.assertIn("record_changed", self.codes(result))

    def test_a_bound_admission_restored_after_the_claims_read_is_record_changed(self):
        self.intake(worktree=self.place)
        binding = {"runId": "run", "taskId": "task", "workerId": "worker", "dispatchId": "dispatch",
                   "worktreeId": str(self.project), "terminalHandle": "terminal"}
        path, intermediate = self.without_admissions({"schema": "pod-admission/v4", "admission_id": "a",
                                                      "state": "closed", "role": "implement",
                                                      "native_binding": binding, "runtime": "runtime"})
        self.assertIn("native_identity_unverified", self.codes(self.query()))
        with self.claims_read_meets(path, intermediate):
            result = self.query()
        self.assertFalse(result["complete"])
        self.assertIn("record_changed", self.codes(result))

    def test_claims_kept_from_the_later_bytes_are_those_bytes_claims(self):
        self.intake(worktree=self.place)
        path, intermediate = self.reserved_writer()
        with self.claims_read_meets(path, intermediate, restore=False):
            result = self.query(worktree=str(self.project))
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertNotIn("a", [row.get("admission") for row in result["claims"]])

    def test_the_selected_record_needs_one_set_of_bytes_for_both_reads(self):
        self.intake(worktree=self.place)
        path, raw = self.raw()
        with self.claims_read_meets(path, json.dumps(raw, indent=2).encode(), restore=False):
            result = self.query(objective="objective", candidate=self.candidate)
        self.assertIn("record_changed", self.codes(result))

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
                   "admissions": {"old": _reference({"old": "report"}, {})}}
            self.save(directory / "context.json", raw)
        with self.current_chunks_only():
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
                facts["generation"] = {"revision": 2, "sha256": "b" * 64}
                facts["context"]["revision"] = 2
            return facts
        with patch("pod.history.load_admission_facts", create=True, side_effect=changed):
            result = self.query()
        self.assertIn("record_changed", [row["code"] for row in result["unknowns"]])

    def claims_read_differs(self, change):
        """The claims read meets other bytes than the attribution read; `change` alters what it gives (pod#221)."""
        self.intake(worktree=self.place)
        def read(path, *, include_admissions):
            facts = self.facts(include_admissions)
            facts["attribution"] = {"status": "proved", "repository": None, "repo_key": self.place["repo_key"],
                                    "objective": "objective", "basis": {"record_key": path.parent.name,
                                    "sources_count": 1, "sources_sha256": "c" * 64}}
            if include_admissions:
                facts["generation"]["sha256"] = "b" * 64
                change(facts)
            return facts
        with patch("pod.history.load_admission_facts", create=True, side_effect=read):
            return [row["code"] for row in self.query()["unknowns"]]

    def test_every_attribution_fact_but_the_generation_and_basis_is_compared(self):
        """Driven by the real reader's attribution read, so a fact it gains later is compared without a list here."""
        self.intake(worktree=self.place)
        path, _ = self.raw()
        attributed = history.load_admission_facts(path, include_admissions=False)
        claimed = history.load_admission_facts(path, include_admissions=True)
        view = admission._facts_context(attributed)

        def leaves(value, trail):
            if trail in (("_facts_generation",), ("_facts_attribution", "basis")):
                return
            if isinstance(value, dict) and value:
                for key, item in value.items():
                    yield from leaves(item, (*trail, key))
            else:
                yield trail
        paths = list(leaves(view, ()))
        self.assertTrue({("revision",), ("owner",), ("_facts_phase",), ("checkpoint", "native_refs"),
                         ("_facts_attribution", "repo_key")} <= set(paths), paths)

        def rows(trail=None):
            facts = copy.deepcopy(claimed)
            facts["generation"] = {**facts["generation"], "sha256": "b" * 64}
            if trail == ("_facts_phase",):
                facts["phase"] = "closed" if facts["phase"] != "closed" else "open"
            elif trail is not None:
                holder = facts["attribution"] if trail[0] == "_facts_attribution" else facts["context"]
                for key in trail[1:-1] if trail[0] == "_facts_attribution" else trail[:-1]:
                    holder = holder[key]
                old = holder[trail[-1]]
                holder[trail[-1]] = (old + 1 if type(old) is int else not old if type(old) is bool
                                     else [*old, "changed"] if isinstance(old, list) else str(old) + "-changed")
            discovery = admission._Discovery()
            token = admission._SHARED.set(discovery)
            try:
                with patch("pod.history.load_admission_facts", return_value=facts):
                    return admission._admission_rows(dict(view), path.parent)
            finally:
                admission._SHARED.reset(token)
                discovery.close()
        rows()  # Other bytes with every fact equal keep the claims.
        for trail in paths:
            with self.subTest(trail=trail), self.assertRaises(PodError) as caught:
                rows(trail)
            self.assertEqual(caught.exception.code, "record_changed", trail)

    def test_other_bytes_keep_the_claims_only_while_every_attribution_fact_is_equal(self):
        self.assertNotIn("record_changed", self.claims_read_differs(lambda facts: None))
        def basis(facts):  # How the attribution was proved, which admission never reads.
            facts["attribution"]["basis"] = {"record_key": facts["attribution"]["basis"]["record_key"],
                                             "sources": ["checkpoint.worktree"]}
        self.assertNotIn("record_changed", self.claims_read_differs(basis))
        def owner(facts):
            facts["context"]["owner"] = "other"
        self.assertIn("record_changed", self.claims_read_differs(owner))
        def attribution(facts):
            facts["attribution"]["repository"] = "owner/other"
        self.assertIn("record_changed", self.claims_read_differs(attribution))

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
        with self.current_chunks_only():
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

    def test_terminal_reader_reads_past_the_default_page_once(self):
        from pod.errors import PodError
        live = {"connected": True, "writable": True, "orphaned": False}
        rows = [{"handle": f"t{index}", "worktreePath": str(self.project), **live} for index in range(230)]
        scope = {"hostIds": ["local"], "omittedHostIds": []}
        for whole, complete in (({"truncated": False, "totalCount": 230}, True), ({"truncated": True, "totalCount": 230}, False),
                                ({"truncated": False, "totalCount": 231}, False)):
            calls = []

            def read(argv):
                calls.append(argv)
                shown = rows if "--limit" in argv else rows[:200]
                extra = whole if "--limit" in argv else {"truncated": True, "totalCount": 230}
                return {"runtime": "runtime", "result": {"terminals": shown, "hostScope": scope, **extra}}
            with self.subTest(whole=whole), patch("pod.admission._read", side_effect=read):
                if complete:
                    self.assertEqual(len(self._real_terminals()["terminals"]), 230)
                else:
                    with self.assertRaises(PodError) as caught:
                        self._real_terminals()
                    self.assertEqual(caught.exception.code, "native_resources_incomplete")
                self.assertEqual(calls, [["terminal", "list", "--json"], ["terminal", "list", "--limit", "230", "--json"]])

    def test_an_incomplete_second_terminal_read_leaves_admission_refusing(self):
        from pod.errors import PodError
        from tests.common import bad_retries
        rows = [{"handle": f"t{index}", "worktreePath": str(self.project), "connected": True} for index in range(230)]
        first = {"runtime": "runtime", "result": {"terminals": rows[:200], "truncated": True, "totalCount": 230,
                                                  "hostScope": {"hostIds": ["local"], "omittedHostIds": []}}}
        for label, second in bad_retries("terminals", rows, 230).items():
            with self.subTest(label), patch("pod.admission._read",
                                            side_effect=[first, {"runtime": "runtime", "result": second}]) as read:
                with self.assertRaises(PodError) as caught:
                    self._real_terminals()
                self.assertEqual((caught.exception.code, read.call_count), ("native_resources_incomplete", 2))

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
        # An unreadable record holds only its own resources (R206); the healthy read still exits zero.
        for invalid, expected in ((False, "blocked"), (True, "observed")):
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

    def test_repository_intrinsic_effects_do_not_leak_to_foreign_repositories(self):
        self.intake(worktree=self.place)
        other = self.root / "other"
        other.mkdir(); git(other, "init", "-q", "-b", "main")
        _, raw = self.raw()
        raw["checkpoint"]["objective"] = "foreign"
        raw["checkpoint"]["worktree"] = {**self.place, "path": str(other), "repo_key": repository_context(other)["repo_key"]}
        directory = objective_root(other, "foreign")
        directory.mkdir(); self.save(directory / "context.json", raw)
        for kind, target, identity in (("merge", "origin/main", "merge:origin/main"),
                                       ("workflow_dispatch", "ci.yml", "workflow:ci.yml"),
                                       ("cancel_validation", "old-run", "cancel:old-run")):
            with self.subTest(kind=kind):
                self.journal(kind=kind, target=target, directory=directory)
                foreign = self.query(effects=[identity])
                self.assertEqual(foreign["status"], "observed", foreign["unknowns"])
                self.assertFalse(foreign["conflicts"])
                self.journal(kind=kind, target=target)
                local = self.query(effects=[identity])
                self.assertEqual(local["status"], "blocked", local["unknowns"])
                (objective_root(self.project, "objective") / "governor.json").unlink()

    def test_declared_and_mapped_workflow_effects_remain_global(self):
        self.intake(worktree=self.place)
        other = self.root / "other"
        other.mkdir(); git(other, "init", "-q", "-b", "main")
        _, raw = self.raw()
        raw["checkpoint"]["objective"] = "foreign"
        raw["checkpoint"]["worktree"] = {**self.place, "path": str(other), "repo_key": repository_context(other)["repo_key"]}
        directory = objective_root(other, "foreign")
        directory.mkdir(); self.save(directory / "context.json", raw)
        for kind, declared, recorded in (("workflow_dispatch", ["workflow:ci.yml"], None),
                                         ("push", None, ["workflow:ci.yml"])):
            with self.subTest(kind=kind):
                self.journal(kind=kind, target="ci.yml" if kind=="workflow_dispatch" else "origin/topic",
                             declared=declared, recorded=recorded, directory=directory)
                result = self.query(effects=["workflow:ci.yml"])
                self.assertEqual(result["status"], "blocked", result["unknowns"])
                self.assertTrue(result["conflicts"])

    def test_unusable_repository_argument_is_a_real_cli_failure(self):
        plain = self.root / "plain"
        plain.mkdir()
        for path in (plain, self.root / "missing"):
            with self.subTest(path=path):
                out = StringIO()
                with redirect_stdout(out):
                    code = cli.main(["admission", "--repository", str(path), "--json"])
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out.getvalue())["schema"], "pod-cli/v5")

    def test_target_only_read_does_not_count_unrelated_process_liveness(self):
        self.intake(worktree=self.place)
        native = self.writer()
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": [native]}):
            result = self.query(target="target", target_commit=self.base)
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertEqual(result["status"], "observed")
        self.assertIn("native_liveness_unverified", result["unrelated_unknowns"]["by_code"])

    def test_target_only_read_keeps_shared_reader_pending_native_target(self):
        self.intake(worktree=self.place)
        git(self.project, "update-ref", "refs/remotes/origin/main", self.base)
        source = self.writer(verdict="exited")
        source["workerState"] = "abandoned"
        claim = {"process": "exited", "effects": [{"kind": "push", "id": "origin/main", "state": "pending"}]}
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": [source]}), \
             patch("pod.admission._native_claim", return_value=claim):
            result = self.query(target="origin/main", target_commit=self.base)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("native_effect_target_unknown", [row["code"] for row in result["unknowns"]])


class TerminalHistoryTests(AdmissionQueryCase):
    """R206 rules T and S, with D1 and D2: a terminal record's settled history and an ended Dispatch whose resources
    are gone make no current admission claim, while anything unproved keeps its unknown. Another objective's record,
    `old`, is written for real with real terminal metadata; only its admission inventory and placement are shaped by
    the facts reader patch. The Run list is served page by page to the production parser."""

    GONE = "/nonexistent/pod-terminal-history-workspace"
    writer = ReviewCorrectionTests.writer
    journal = ReviewCorrectionTests.journal
    LINK = {"fixture": "continued_by"}
    ENTRY = {"fixture": "historical_retirement"}

    def setUp(self):
        super().setUp()
        self.intake(worktree=self.place)
        self.old = objective_root(self.project, "old")
        self.real_facts = history.load_admission_facts
        self.shape = None
        facts = patch("pod.history.load_admission_facts", create=True, side_effect=self.facts_for)
        facts.start(); self.addCleanup(facts.stop)
        self.run_pages = {}
        real = admission._gated_read
        def gated(argv):
            if argv[:2] == ["orchestration", "run-list"]:
                cursor = argv[argv.index("--cursor") + 1] if "--cursor" in argv else None
                if cursor not in self.run_pages:
                    raise PodError("orca_unavailable", "fixture: no such Run page")
                from copy import deepcopy
                return deepcopy(self.run_pages[cursor])
            return real(argv)
        pages = patch("pod.admission._gated_read", side_effect=gated)
        pages.start(); self.addCleanup(pages.stop)

    def runs(self, coordinators, **extra):
        """One complete Run list page with an explicit end cursor."""
        self.run_pages = {None: {"runtime": "runtime", "result": {
            "runs": [{"id": run, "coordinator_handle": handle} for run, handle in coordinators.items()],
            "nextCursor": None, **extra}}}

    def facts_for(self, path, *, include_admissions):
        facts = self.real_facts(path, include_admissions=include_admissions)
        if self.shape is None or facts["context"]["checkpoint"]["objective"] != "old":
            return facts
        rows, placed = self.shape
        facts["context"]["checkpoint"]["native_refs"] = [{"runId": "old-run", "runtime": "runtime"}]
        if not placed:
            facts["context"]["checkpoint"]["worktree"] = None
        if include_admissions:
            facts["admissions"] = rows
            facts["complete"] = placed
            if not placed:
                facts["errors"] = [{"code": "objective_placement_unknown"}]
        return facts

    def terminal(self, phase="closed", rows=None, placed=False, unattributed=False):
        """Write `old` for real in `phase`: a genuine closure, or a continuation or retirement entry whose production
        validator this test accepts; then shape its admission inventory. `unattributed` makes its repository
        attribution unprovable."""
        rows = rows if rows is not None else {"a": self.bound("a")}
        if phase == "closed":
            self.write([self.criterion("satisfied", evidence=[self.proof("O1")])], objective="old",
                       governance={"base_ref": "target"}, close=True, worktree=self.place)
        else:
            self.write([self.criterion()], objective="old", governance={"base_ref": "target"}, worktree=self.place)
        if phase in ("continued", "retired"):
            path, raw = self.raw("old")
            field, value, target = (("continued_by", self.LINK, "pod.succession.validate_link") if phase == "continued"
                                    else ("historical_retirement", self.ENTRY, "pod.retirement.validate_entry"))
            raw[field] = value
            self.save(path, raw)
            real = __import__("pod.succession" if phase == "continued" else "pod.retirement",
                              fromlist=["x"]).__dict__[target.rsplit(".", 1)[1]]
            def accept(*args, **kwargs):
                if value in args:
                    return None
                return real(*args, **kwargs)
            accepted = patch(target, side_effect=accept)
            accepted.start(); self.addCleanup(accepted.stop)
        self.shape = (rows, placed)
        if unattributed:
            real_membership = admission._membership
            def membership(project, context, record, raw, known=()):
                if record == self.old.name:
                    raise PodError("record_attribution_unknown", "fixture: attribution unprovable")
                return real_membership(project, context, record, raw, known)
            unknown = patch("pod.admission._membership", side_effect=membership)
            unknown.start(); self.addCleanup(unknown.stop)

    def bound(self, key, dispatch="old-dispatch", settled=True):
        """A bound admission as the facts reader projects it; settled history has consumed its report and recorded
        its disposition, while `settled=False` still owes the report."""
        return {"schema": "pod-admission/v4", "admission_id": key, "objective": "old", "owner": "owner",
                "state": "bound", "role": "implement", "runtime": "runtime", "run_id": "old-run", "task_id": "task",
                "worktree": None, "placement_binding": None,
                "report_consumed": settled, "disposition_recorded": settled,
                "native_binding": {"runId": "old-run", "taskId": "task", "dispatchId": dispatch, "workerId": "worker",
                                   "worktreeId": self.GONE, "terminalHandle": "old-terminal"}}

    def reserved(self, key, worktree=None):
        return {"schema": "pod-admission/v4", "admission_id": key, "objective": "old", "owner": "owner",
                "state": "unresolved", "role": "implement", "runtime": "runtime", "run_id": "old-run",
                "task_id": "task", "worktree": worktree, "placement_binding": None, "native_binding": None}

    def worker(self, path, *, dispatch="old-dispatch", run="old-run", state=None, status=None, handle="old-terminal",
               verdict="unverifiable"):
        row = self.writer(path, verdict=verdict, dispatch=dispatch)
        row["runId"] = row["projection"]["runId"] = run
        row["agentTerminalHandle"] = handle
        if state is not None:
            row["workerState"], row["dispatchStatus"] = state, status
        return row

    def read(self, workers=(), terminals=(), fleet=True, **query):
        native = {"runtime": "runtime", "complete": True, "workers": list(workers)} if fleet else None
        with patch("pod.admission._fleet", return_value=native), \
             patch("pod.admission._terminals", return_value={"runtime": "runtime", "complete": True,
                                                             "terminals": list(terminals)}):
            return self.query(worktree=str(self.project), branch="main", **query)

    def codes(self, result):
        return [row["code"] for row in result["unknowns"]]

    def moved(self, result, code):
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertFalse(result["repository_complete"])
        self.assertIn(code, result["unrelated_unknowns"]["by_code"])

    def live_here(self, dispatch="live-here", run="foreign-run"):
        """A current worker in this repository that no open record accounts for."""
        return self.worker(str(self.project), dispatch=dispatch, run=run, verdict="live", handle="here-terminal")

    # Rule T: each terminal phase moves; each fail-closed case stays.

    def test_a_closed_record_with_settled_placementless_admissions_is_history(self):
        self.terminal("closed")
        self.moved(self.read(), "admission_facts_incomplete")

    def test_a_continued_record_with_settled_admissions_is_history(self):
        self.terminal("continued")
        self.moved(self.read(), "admission_facts_incomplete")

    def test_a_retired_unattributed_record_with_settled_admissions_is_history(self):
        self.terminal("retired", unattributed=True)
        self.moved(self.read(), "record_attribution_unknown")

    # A876: a bound admission still owing its report keeps a closed or continued record's gap; a `retired` record
    # holds nothing (R112, R206).

    def test_a_closed_record_owing_a_report_keeps_the_gap(self):
        self.terminal("closed", rows={"a": self.bound("a", settled=False)})
        self.assertIn("admission_facts_incomplete", self.codes(self.read()))

    def test_a_continued_record_owing_a_report_keeps_the_gap(self):
        self.terminal("continued", rows={"a": self.bound("a", settled=False)})
        self.assertIn("admission_facts_incomplete", self.codes(self.read()))

    def test_a_retired_record_owing_a_report_is_history(self):
        self.terminal("retired", rows={"a": self.bound("a", settled=False)}, unattributed=True)
        self.moved(self.read(), "record_attribution_unknown")

    def test_a_bound_dispatch_shown_current_keeps_the_gap(self):
        self.terminal("closed")
        result = self.read(workers=[self.worker(self.GONE)])
        self.assertIn("admission_facts_incomplete", self.codes(result))

    def test_a_pending_governor_effect_keeps_the_gap(self):
        self.terminal("closed")
        self.journal(directory=self.old)
        self.assertIn("admission_facts_incomplete", self.codes(self.read()))

    def test_a_record_changed_during_the_read_keeps_the_gap(self):
        self.terminal("closed")
        real = admission._after_context
        with patch("pod.admission._after_context", side_effect=lambda path: "changed" if path.parent == self.old
                   else real(path)):
            self.assertIn("admission_facts_incomplete", self.codes(self.read()))

    def test_a_bound_caller_keeps_the_gap(self):
        self.terminal("closed")
        result = self.read(fleet=False)
        self.assertIn("admission_facts_incomplete", self.codes(result))
        self.assertIn("native_inventory_scope", self.codes(result))

    def test_the_selected_objectives_own_record_never_uses_the_rule(self):
        self.terminal("closed")
        result = self.read(objective="old", candidate=self.base)
        self.assertIn("admission_facts_incomplete", self.codes(result))

    def test_an_open_record_keeps_the_gap(self):
        self.terminal("open")
        self.assertIn("admission_facts_incomplete", self.codes(self.read()))

    def test_invalid_terminal_metadata_keeps_the_gap(self):
        # The production validators, unpatched: the facts reader's truthy phase alone is never terminal status.
        self.write([self.criterion()], objective="old", governance={"base_ref": "target"}, worktree=self.place)
        self.shape = ({"a": self.bound("a")}, False)
        for field, value in (("historical_retirement", {"invalid": "certificate"}), ("continued_by", {"invalid": "link"})):
            with self.subTest(field):
                path, raw = self.raw("old")
                raw.pop("historical_retirement", None); raw.pop("continued_by", None)
                raw[field] = value
                self.save(path, raw)
                self.assertIn("admission_facts_incomplete", self.codes(self.read()))

    def test_invalid_terminal_metadata_on_a_genuine_admission_keeps_the_gap(self):
        # Production reader and membership, no shaping: a real admission in a record with no coordinator placement.
        from copy import deepcopy
        from pod import ledger
        self.start("genuine", self.packet(["O1"], worktree=self.place))
        _, raw = self.raw()
        raw["checkpoint"]["objective"] = "old"
        raw["checkpoint"]["worktree"] = None
        for row in raw["admissions"].values():
            row["objective"] = "old"
        ledger._validate_context(deepcopy(raw))
        self.old.mkdir(parents=True, exist_ok=True)
        for field, value in (("historical_retirement", {"invalid": "certificate"}), ("continued_by", {"invalid": "link"})):
            with self.subTest(field):
                candidate = deepcopy(raw)
                candidate[field] = value
                self.save(self.old / "context.json", candidate)
                result = self.read()
                self.assertNotIn("admission_facts_incomplete", result["unrelated_unknowns"]["by_code"], result)
                self.assertFalse(result["complete"])

    # Rule T, unbound reservations: (i) a retirement certificate, or (ii) no unattributed current actor.

    def test_an_unbound_reservation_on_a_closed_record_without_unattributed_actors_is_history(self):
        self.terminal("closed", rows={"r": self.reserved("r")}, placed=True)
        result = self.read()
        self.moved(result, "assignment_unbound")
        self.assertFalse([row for row in result["claims"] if row.get("admission") == "r"])

    def test_an_unbound_reservation_with_an_unattributed_live_worker_and_no_certificate_stays(self):
        self.terminal("retired", rows={"r": self.reserved("r")}, placed=True)
        with patch("pod.admission._retirement_preserved", return_value=None):
            result = self.read(workers=[self.live_here()])
        self.assertIn("assignment_unbound", self.codes(result))

    def test_a_retirement_certificate_settles_an_unbound_reservation_despite_a_live_worker(self):
        self.terminal("retired", rows={"r": self.reserved("r")}, placed=True)
        with patch("pod.admission._retirement_preserved", return_value={"r"}):
            result = self.read(workers=[self.live_here()])
        self.assertNotIn("assignment_unbound", self.codes(result))
        self.assertIn("assignment_unbound", result["unrelated_unknowns"]["by_code"])

    def test_a_closed_record_takes_no_certificate(self):
        self.terminal("closed", rows={"r": self.reserved("r")}, placed=True)
        with patch("pod.admission._retirement_preserved", return_value={"r"}):
            result = self.read(workers=[self.live_here()])
        self.assertIn("assignment_unbound", self.codes(result))

    # Rule T 4b: a Dispatch current only in another repository's registered worktree.

    def other_worktree(self):
        other = self.root / "other-repository"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        git(other, "config", "user.email", "fixture@example.invalid")
        git(other, "config", "user.name", "Fixture")
        (other / "f").write_text("x\n")
        git(other, "add", "f")
        git(other, "commit", "-qm", "other")
        git(other, "worktree", "add", "-q", str(self.root / "other-worktree"), "-b", "side")
        return str((self.root / "other-worktree").resolve())

    def test_a_dispatch_current_in_another_repositorys_registered_worktree_is_history(self):
        self.terminal("closed")
        self.moved(self.read(workers=[self.worker(self.other_worktree())]), "admission_facts_incomplete")

    def test_another_repository_must_be_proven(self):
        elsewhere = self.other_worktree()
        link = self.root / "linked-worktree"
        link.symlink_to(elsewhere)
        unregistered = self.root / "other-repository" / "plain"
        unregistered.mkdir()
        git(self.project, "worktree", "add", "-q", str(self.root / "own-worktree"), "-b", "own")
        self.terminal("closed")
        cases = {"symlinked": str(link), "same repository": str((self.root / "own-worktree").resolve()),
                 "unregistered": str(unregistered.resolve()), "unreadable": self.GONE + "-unreadable"}
        for name, path in cases.items():
            with self.subTest(name):
                self.assertIn("admission_facts_incomplete", self.codes(self.read(workers=[self.worker(path)])))

    def test_a_foreign_worktree_is_proved_from_its_one_identity_read(self):
        """A863: the 4b proof takes the foreign registry from the workspace's identity read, read once per stage."""
        self.terminal("closed")
        path = self.other_worktree()
        workers = [self.worker(path), self.worker(path, dispatch="second-dispatch", run="second-run")]
        with patch("pod.admission._places", wraps=admission._places) as places, \
             patch("pod.admission.repository_context", wraps=admission.repository_context) as identity:
            result = self.read(workers=workers)
        self.moved(result, "admission_facts_incomplete")
        self.assertEqual({Path(call.args[0]).resolve() for call in places.call_args_list}, {self.project.resolve()})
        # Once in discovery's projection and once in the change recheck, whatever the number of rows naming it.
        self.assertEqual(sum(1 for call in identity.call_args_list if str(call.args[0]) == path), 2)

    def test_an_admission_naming_this_repository_keeps_the_gap_elsewhere(self):
        row = self.bound("a")
        row["worktree"] = "path:" + str(self.project)
        self.terminal("closed", rows={"a": row})
        self.assertIn("admission_facts_incomplete", self.codes(self.read(workers=[self.worker(self.other_worktree())])))

    # D1: a readable evidence closure closes the record in admission.

    def test_an_evidence_closed_record_reads_closed_in_admission(self):
        self.write([self.criterion()], objective="old", governance={"base_ref": "target"}, worktree=self.place)
        path, raw = self.raw("old")
        for ref in raw["checkpoint"]["native_refs"]:
            ref["runtime"] = "previous-runtime"
        self.save(path, raw)
        self.assertIn("native_runtime_changed", self.codes(self.read()))
        with patch("pod.evidence_closure.block", side_effect=lambda root: {"status": "recorded"} if root == self.old
                   else None):
            result = self.read()
        self.assertNotIn("native_runtime_changed", self.codes(result))
        self.assertFalse([row for row in result["claims"] if row.get("objective") == "old"])
        with patch("pod.evidence_closure.block", side_effect=lambda root: {"status": "unreadable", "code": "x"}
                   if root == self.old else None):
            self.assertIn("native_runtime_changed", self.codes(self.read()))

    # D2: a worker whose placement cannot be read from its Run's record is its own fact.

    def test_an_unplaceable_worker_is_its_own_fact(self):
        self.terminal("closed")
        result = self.read(workers=[self.worker(str(self.root / "unregistered-place"), verdict="live")])
        rows = [row for row in result["unknowns"] if row["code"] == "worker_placement_unknown"]
        self.assertEqual([(row["dispatch"], row["cause"]) for row in rows], [("old-dispatch", "admission_facts_incomplete")])
        self.assertIn("admission_facts_incomplete", self.codes(result))

    # Rule S: an ended Dispatch whose terminal, workspace and Run coordinator are gone.

    def ended(self, **fields):
        values = {"dispatch": "ended", "run": "ended-run", "state": "succeeded", "status": "completed",
                  "handle": "gone-terminal", **fields}
        return self.worker(str(self.project / "gone-workspace"), **values)

    def outside(self, handle, **fields):
        folder = self.root / "foreign-folder"
        folder.mkdir(exist_ok=True)
        return {"handle": handle, "worktreePath": str(folder), "connected": True, "writable": True, "orphaned": False,
                **fields}

    def settled(self, result):
        self.assertTrue(result["complete"], result["unknowns"])
        self.assertFalse([row for row in result["claims"] if row.get("dispatch") == "ended"])
        self.assertIn("native_liveness_unverified", result["unrelated_unknowns"]["by_code"])
        self.assertFalse(result["repository_complete"])

    def kept(self, result):
        """The ended Dispatch still holds: a claim or an unknown names it."""
        return (any(row.get("dispatch") == "ended" for row in result["claims"])
                or any(row.get("dispatch") == "ended" for row in result["unknowns"])
                or any(row.get("code") == "native_writer_scope_unknown"
                       and "ended" in row.get("references", {}).get("dispatches", []) for row in result["unknowns"]))

    def test_an_ended_dispatch_with_gone_resources_makes_no_claim(self):
        self.runs({"ended-run": None})
        self.settled(self.read(workers=[self.ended()]))

    def test_an_exited_coordinator_is_gone(self):
        self.runs({"ended-run": "coordinator"})
        exited = self.outside("coordinator", connected=False, writable=False, exitCause={"kind": "exited"})
        from pod.orca import EXIT_KINDS
        exited["exitCause"]["kind"] = sorted(EXIT_KINDS)[0]
        self.settled(self.read(workers=[self.ended()], terminals=[exited]))

    def test_an_ended_dispatch_keeps_its_gap_unless_every_resource_is_gone(self):
        live = {"handle": "gone-terminal", "worktreePath": str(self.project), "connected": True, "writable": True,
                "orphaned": False}
        cases = {"live terminal": ({}, [live], {"ended-run": None}),
                 "existing workspace": ("exists", [], {"ended-run": None}),
                 "live coordinator": ({}, [self.outside("coordinator")], {"ended-run": "coordinator"}),
                 "disconnected coordinator": ({}, [self.outside("coordinator", connected=False)], {"ended-run": "coordinator"}),
                 "orphaned coordinator": ({}, [self.outside("coordinator", connected=False, orphaned=True)],
                                          {"ended-run": "coordinator"}),
                 "unsettled status": ({"state": "running", "status": "running"}, [], {"ended-run": None}),
                 "live projection": ({"verdict": "live"}, [], {"ended-run": None})}
        for name, (fields, terminals, coordinators) in cases.items():
            with self.subTest(name):
                self.runs(coordinators)
                if fields == "exists":
                    (self.project / "gone-workspace").mkdir(exist_ok=True)
                    fields = {}
                result = self.read(workers=[self.ended(**fields)], terminals=terminals)
                (self.project / "gone-workspace").exists() and (self.project / "gone-workspace").rmdir()
                self.assertTrue(self.kept(result), name)

    def test_the_run_list_must_be_provably_complete(self):
        page = {"runs": [{"id": "ended-run", "coordinator_handle": None}], "nextCursor": None}
        cases = {"truncated": {None: {**page, "truncated": True}},
                 "partial": {None: {**page, "partial": True}},
                 "incomplete": {None: {**page, "complete": False}},
                 "missing end cursor": {None: {"runs": page["runs"]}},
                 "more without cursor": {None: {**page, "hasMore": True}},
                 "duplicate Run": {None: {"runs": [{"id": "ended-run", "coordinator_handle": "coordinator"}],
                                          "nextCursor": "c1"},
                                   "c1": {"runs": [{"id": "ended-run", "coordinator_handle": None}], "nextCursor": None}},
                 "repeated cursor": {None: {**page, "nextCursor": "c1"}, "c1": {"runs": [{"id": "x", "coordinator_handle": None}],
                                     "nextCursor": "c1"}},
                 "empty Run id": {None: {"runs": [{"id": "", "coordinator_handle": None}, *page["runs"]], "nextCursor": None}},
                 "runtime change": {None: {**page, "nextCursor": "c1"}, "c1": {"runs": [{"id": "y", "coordinator_handle": None}],
                                    "nextCursor": None, "_runtime": "other"}}}
        terminals = [self.outside("coordinator")]
        for name, pages in cases.items():
            with self.subTest(name):
                from copy import deepcopy
                self.run_pages = {cursor: {"runtime": value.pop("_runtime", "runtime"), "result": value}
                                  for cursor, value in deepcopy(pages).items()}
                self.assertIsNone(admission._run_coordinators("runtime"), name)
                self.assertTrue(self.kept(self.read(workers=[self.ended()], terminals=terminals)), name)

    def test_a_complete_paged_run_list_is_read_whole(self):
        self.run_pages = {None: {"runtime": "runtime", "result": {"runs": [{"id": "a", "coordinator_handle": "x"}],
                                                                  "nextCursor": "c1", "hasMore": True}},
                          "c1": {"runtime": "runtime", "result": {"runs": [{"id": "ended-run", "coordinator_handle": None}],
                                                                  "nextCursor": None, "hasMore": False}}}
        self.assertEqual(admission._run_coordinators("runtime"), {"a": "x", "ended-run": None})
        self.settled(self.read(workers=[self.ended()]))

    def test_an_older_release_never_clears_a_newer_holder(self):
        self.runs({"ended-run": None})
        current = self.ended(dispatch="ended", handle="shared")
        older = self.ended(dispatch="older", handle="shared")
        older["resource"].update(terminalHandle="shared", releaseState="released", ownershipState="released",
                                 originDispatchId="older", ownerDispatchId="older")
        result = self.read(workers=[current, older], terminals=[self.outside("shared")])
        self.assertTrue(self.kept(result))

    def test_a_proven_later_release_clears_a_reused_terminal(self):
        self.runs({"ended-run": None})
        source = self.ended(dispatch="ended", handle="shared")
        source["resource"] = None
        later = self.ended(dispatch="later", handle="shared", verdict="exited")
        later["resource"].update(terminalHandle="shared", releaseState="released", ownershipState="released",
                                 originDispatchId="ended", ownerDispatchId="later")
        self.settled(self.read(workers=[source, later], terminals=[self.outside("shared")]))

    def test_a_bound_caller_never_settles_an_ended_dispatch(self):
        self.runs({"ended-run": None})
        result = self.read(workers=[self.ended()], fleet=False)
        self.assertIn("native_inventory_scope", self.codes(result))
        self.assertNotIn("native_liveness_unverified", result["unrelated_unknowns"]["by_code"])
