"""R208: one objective's exact-candidate facts, with no repository record scan and no fleet discovery."""
from contextlib import ExitStack, contextmanager, redirect_stdout
from io import StringIO
import argparse
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from pod import admission, cli, history
from pod.errors import PodError
from pod.ledger import objective_root, read, state_root
from pod.operations import OrcaPort
from tests.common import logical_json
from tests.kernel_support import KernelCase, assurance_review, assurance_satisfied, git


def facts_digest(answer):
    """R208's digest, recomputed by a caller from the answer it received."""
    from pod.util import digest
    return digest({key: value for key, value in answer.items()
                   if key not in ("digest", "status", "conflicts", "next_action")})


class SelectedCase(KernelCase):
    review, satisfied = assurance_review, assurance_satisfied
    production_native = False  # True: keep the real OrcaPort.read_native and double only the worker-show boundary

    def setUp(self):
        super().setUp()
        self.native_reads = []
        port = self.port
        if self.production_native:
            def show_worker(_port, dispatch):
                self.native_reads.append(dispatch)  # each actual exact native read
                return port.show_worker(dispatch)
            native = patch.object(OrcaPort, "show_worker", show_worker)
        else:
            def read_native(_port, owner, **kwargs):
                self.native_reads.extend(row["native_binding"]["dispatchId"] for row in kwargs.get("assignments", ()))
                return port.read_native(owner, **kwargs)
            native = patch.object(OrcaPort, "read_native", autospec=True, side_effect=read_native)
        native.start(); self.addCleanup(native.stop)

    def ready(self):
        """A recorded review that satisfies assurance A, and criterion O1 proved, at the fixture candidate."""
        self.satisfied()
        rows = self.stored()
        criterion = next(row for row in rows if row["id"] == "O1")
        criterion.pop("executor", None)
        criterion.update(state="satisfied", evidence=[self.proof("O1")])
        self.write(rows)

    def selected(self, **kwargs):
        return admission.selected(self.project, objective=kwargs.pop("objective", "objective"),
                                  candidate=kwargs.pop("candidate", self.base), **kwargs)

    def raw(self):
        path = objective_root(self.project, "objective") / "context.json"
        return path, logical_json(path)

    def edit(self, change):
        path, raw = self.raw()
        change(raw)
        path.write_text(json.dumps(raw))

    def single_query(self):
        """`pod admission --objective O --candidate C` with its fleet and terminal inventories answered."""
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": []}), \
             patch("pod.admission._terminals", return_value={"runtime": "runtime", "complete": True, "terminals": []}), \
             patch("pod.admission._owner_observation", return_value={"status": "unknown", "code": "fixture"}):
            return admission.query(self.project, objective="objective", candidate=self.base)

    def tree(self, commit=None):
        return git(self.project, "rev-parse", (commit or self.base) + "^{tree}")

    def files(self):
        root = state_root(self.project)
        out = {}
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            out[str(path)] = (info.st_mode, info.st_size, info.st_mtime_ns,
                              path.read_bytes() if path.is_file() and not path.is_symlink() else None)
        return out

    @contextmanager
    def no_discovery(self):
        """Every repository-wide or fleet read fails the test; only the selected record may be read."""
        selected = objective_root(self.project, "objective")
        root = state_root(self.project)
        forbidden = AssertionError("repository or fleet discovery")
        real_bytes, real_load, real_scandir = history._context_bytes, history.ChunkStore.load, os.scandir

        def context_bytes(path, limit):
            self.assertEqual(Path(path).parent, selected, "another record was read")
            return real_bytes(path, limit)

        def chunk(store, address):
            self.assertEqual(store.root.parent.parent, selected, "another record's history was read")
            return real_load(store, address)

        def scandir(path=".", *args):
            self.assertNotEqual(Path(os.fsdecode(path)).resolve(), root.resolve(), "the record cohort was listed")
            return real_scandir(path, *args)
        real_popen = subprocess.Popen

        class Guarded(real_popen):
            def __init__(popen, args, *rest, **kwargs):
                first = args[0] if isinstance(args, (list, tuple)) else str(args).split()[0]
                if Path(os.fsdecode(first)).name.startswith("orca"):
                    raise AssertionError("Orca CLI invocation")
                super().__init__(args, *rest, **kwargs)
        with ExitStack() as stack:
            # The subprocess boundary: any Orca executable launched from anywhere, the production port included.
            stack.enter_context(patch("subprocess.Popen", Guarded))
            stack.enter_context(patch("pod.orca.executable", return_value=Path("/forbidden/orca")))
            for name in ("_cohort", "_after_cohort", "_fleet", "_fleet_sequential", "_terminals", "_places",
                         "_run_coordinators", "_owner_observation", "_native_claim", "complete_list"):
                stack.enter_context(patch("pod.admission." + name, side_effect=forbidden))
            stack.enter_context(patch("pod.orca.complete_list", side_effect=forbidden))
            stack.enter_context(patch("pod.orca._read", side_effect=forbidden))  # every Orca CLI read
            stack.enter_context(patch("pod.history._context_bytes", side_effect=context_bytes))
            stack.enter_context(patch.object(history.ChunkStore, "load", chunk))
            stack.enter_context(patch("os.scandir", side_effect=scandir))
            yield


class NoDiscoveryTests(SelectedCase):
    production_native = True

    def test_selected_read_reads_only_its_record_and_exact_bound_workers(self):
        self.ready()
        self.write([self.criterion("satisfied", evidence=[self.proof("O1")])], objective="other",
                   governance={"base_ref": "target"})
        root = state_root(self.project)
        (root / "broken").mkdir(); (root / "broken" / "context.json").write_text("")
        before = self.files()
        with self.no_discovery():
            result = self.selected()
        self.assertEqual((result["status"], result["authorization"], result["unknowns"]), ("ready", False, []))
        bound = sorted(row["native_binding"]["dispatchId"] for row in read(self.project, "objective")["admissions"].values()
                       if row["state"] == "bound")
        self.assertEqual(sorted(self.native_reads), bound)  # exact worker reads by id, nothing else native
        self.assertEqual(self.files(), before)  # no write, compaction, migration or collection

    def test_a_discovery_read_inside_the_production_native_port_fails_the_control(self):
        """Mutation check: the controls catch a forbidden read made from inside OrcaPort.read_native itself."""
        from pod import operations, orca
        self.ready()
        real = OrcaPort.read_native
        injected = {
            "orca_cli": lambda: operations.read_command(["orchestration", "worker-list", "--include-remote", "--json",
                                                         "--limit", "100"]),
            "terminal_inventory": lambda: orca.complete_list("terminal"),
            "fleet": lambda: admission._fleet(),
        }
        for name, call in injected.items():
            def mutated(port, owner, call=call, **kwargs):
                call()
                return real(port, owner, **kwargs)
            with self.subTest(name), patch.object(OrcaPort, "read_native", mutated), self.no_discovery():
                with self.assertRaises(AssertionError):
                    self.selected()

    def test_binding_names_repository_record_candidate_and_tree(self):
        self.ready()
        result = self.selected(tree=self.tree())
        binding = result["binding"]
        self.assertEqual((binding["objective"], binding["candidate"], binding["tree"], binding["path"]),
                         ("objective", self.base, self.tree(), str(self.project)))
        self.assertEqual(binding["record"], objective_root(self.project, "objective").name)
        self.assertEqual(result["schema"], "pod-admission-selected/v1")


class ProofAgreementTests(SelectedCase):
    def agree(self):
        expected = json.dumps(self.single_query()["proof"], sort_keys=False).encode()
        self.assertEqual(json.dumps(self.selected()["proof"], sort_keys=False).encode(), expected)
        return self.selected()

    def test_ready_proof_is_byte_identical(self):
        self.ready()
        self.assertEqual(self.agree()["status"], "ready")

    def test_partial_invalidated_proof_is_byte_identical(self):
        from pod.records import source_identity
        self.satisfied(sources=[source_identity(self.project, "README.md")])
        (self.project / "README.md").write_text("changed after review\n")
        result = self.agree()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["assurances"][0]["gap"], "invalidated")
        self.assertIs(result["assurances"][0]["evidence_current"], False)

    def test_missing_unbound_proof_is_byte_identical(self):
        self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                     "candidate": self.base, "existing_evidence": "tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}})
        result = self.agree()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual((result["assurances"][0]["gap"], result["assurances"][0]["evidence_current"]),
                         ("unbound", False))

    def test_changed_candidate_proof_is_byte_identical(self):
        self.ready()
        (self.project / "next").write_text("next\n")
        git(self.project, "add", "."); git(self.project, "commit", "-qm", "next")
        later = git(self.project, "rev-parse", "HEAD")
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True, "workers": []}), \
             patch("pod.admission._terminals", return_value={"runtime": "runtime", "complete": True, "terminals": []}), \
             patch("pod.admission._owner_observation", return_value={"status": "unknown", "code": "fixture"}):
            single = admission.query(self.project, objective="objective", candidate=later)
        result = self.selected(candidate=later)
        self.assertEqual(json.dumps(result["proof"]), json.dumps(single["proof"]))
        self.assertEqual((result["status"], result["proof"]["reason"]), ("blocked", "candidate_mismatch"))
        self.assertEqual((result["assurances"][0]["evidence_current"], result["assurances"][0]["gap"]),
                         (False, "candidate_mismatch"))

    def test_verification_gaps_and_outstanding_are_byte_identical(self):
        self.ready()
        self.edit(lambda raw: raw["checkpoint"].update(verification_gaps=["hosted CI not run"]))
        result = self.agree()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("hosted CI not run", result["proof"]["gaps"])


class AssuranceTests(SelectedCase):
    def test_recorded_scope_currency_and_accepted_attempts(self):
        self.ready()
        result = self.selected()
        (row,) = result["assurances"]
        review = next(key for key, value in read(self.project, "objective")["admissions"].items()
                      if value["role"] == "review")
        self.assertEqual((row["id"], row["state"], row["scope"]["paths"], row["evidence_current"], row["gap"]),
                         ("A", "satisfied", ["src"], True, None))
        self.assertEqual([attempt["admission"] for attempt in row["accepted_attempts"]
                          if attempt["source"] == "evidence"], [review])

    def test_scope_is_the_recorded_scope_never_inferred(self):
        self.ready()
        self.edit(lambda raw: next(ob for ob in raw["checkpoint"]["obligations"] if ob["id"] == "A")["scope"]
                  .update(paths=["src", "src/old.py"]))
        self.assertEqual(self.selected()["assurances"][0]["scope"]["paths"], ["src", "src/old.py"])

    def test_withdrawn_assurance_has_no_currency(self):
        self.ready()
        rows = admission._selected_assurances({"checkpoint": {"obligations": [
            {"id": "W", "kind": "assurance", "state": "withdrawn", "scope": {"paths": ["x"]}}]}},
            self.selected()["proof"])
        self.assertEqual((rows[0]["evidence_current"], rows[0]["gap"], rows[0]["scope"]["paths"]), (None, None, ["x"]))


class AttemptTests(SelectedCase):
    def review_row(self, result):
        return next(row for row in result["attempts"] if row["role"] == "review")

    def test_effective_facts_come_from_the_record_not_a_reused_terminal(self):
        self.ready()
        recorded = self.review_row(self.selected())["effective"]
        for worker in self.port.workers.values():  # a later reuse of the terminal at another effort
            worker.setdefault("launch", {})["effective"] = {"agent": "codex", "model": "other", "effort": "low"}
        with patch.object(OrcaPort, "show_worker", side_effect=AssertionError("live terminal read")):
            later = self.review_row(self.selected())
        self.assertEqual(later["effective"], recorded)
        self.assertEqual(later["effective"]["source"], "receipt")
        self.assertEqual(later["unknowns"], [])

    def test_missing_effective_effort_is_an_attempt_gap_that_keeps_ready(self):
        self.ready()
        def unknown_effort(raw):
            row = next(value for value in raw["admissions"].values() if value["role"] == "review")
            row["effective_evidence"]["effective"]["effort"] = "unknown"
        self.edit(unknown_effort)
        before = self.selected()
        self.assertEqual(before["status"], "ready")
        self.assertEqual(before["unknowns"], [])
        self.assertEqual(self.review_row(before)["unknowns"],
                         [{"code": "effective_unknown", "fact": "effective_route", "fields": ["effort"]}])
        def restore(raw):
            row = next(value for value in raw["admissions"].values() if value["role"] == "review")
            row["effective_evidence"]["effective"]["effort"] = "high"
        self.edit(restore)
        self.assertNotEqual(self.selected()["digest"], before["digest"])  # the gap is in the digest

    def test_attempt_rows_carry_recorded_native_references(self):
        self.ready()
        state = read(self.project, "objective")
        row = self.review_row(self.selected())
        binding = state["admissions"][row["admission"]]["native_binding"]
        self.assertEqual(row["native"]["dispatch"], binding["dispatchId"])
        self.assertEqual(row["native"]["run"], binding["runId"])
        self.assertEqual(row["requested"]["agent"], state["admissions"][row["admission"]]["request"]["agent"])


class ImplementationLinkageTests(KernelCase):
    def attempt(self, **fields):
        return {"role": "implement", "state": "bound", "admitted_seq": 1, "native_binding": None,
                "effective_evidence": {"effective": {"agent": "claude", "model": "m", "effort": "high",
                                                     "context": "native_default"}}, **fields}

    def test_implementation_linkage_names_whether_the_result_is_in_the_candidate(self):
        (self.project / "later").write_text("later\n")
        git(self.project, "add", "."); git(self.project, "commit", "-qm", "later")
        later = git(self.project, "rev-parse", "HEAD")
        state = {"admissions": {
            "in": self.attempt(disposition={"kind": "integrated_into", "candidate": self.base, "seq": 2,
                                            "validated": "ancestry"}),
            "out": self.attempt(result={"head": later}),
            "none": self.attempt(),
            "bad": self.attempt(result={"head": "f" * 40}),
            "discarded": self.attempt(disposition={"kind": "discarded", "reason": "obsolete", "seq": 3},
                                      result={"head": self.base}),
        }}
        rows = {row["admission"]: row for row in admission._selected_attempts(self.project, state, self.base)}
        self.assertEqual({key: row["in_candidate"] for key, row in rows.items()},
                         {"in": True, "out": False, "none": None, "bad": None, "discarded": True})
        self.assertEqual(rows["in"]["disposition"]["integrated_into"], self.base)
        self.assertEqual([gap["code"] for gap in rows["bad"]["unknowns"]], ["implementation_ancestry_unknown"])
        self.assertEqual(rows["none"]["unknowns"], [])

    def test_unrecorded_effective_facts_are_explicit(self):
        rows = admission._selected_attempts(self.project, {"admissions": {"x": self.attempt(effective_evidence={})}},
                                            self.base)
        self.assertEqual((rows[0]["effective"]["source"], rows[0]["effective"]["effort"]), ("not_recorded", None))
        self.assertEqual(rows[0]["unknowns"], [{"code": "effective_not_recorded", "fact": "effective_route"}])


class RefusalTests(SelectedCase):
    def refused(self, code, call):
        with self.assertRaises(PodError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_wrong_commit_tree_and_repository_refuse_before_any_record_read(self):
        self.ready()
        other = self.root / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        (other / "f").write_text("f\n"); git(other, "add", "."); git(other, "-c", "user.name=x", "-c",
                                                                        "user.email=x@example.invalid", "commit", "-qm", "x")
        with patch("pod.history._context_bytes", side_effect=AssertionError("record read")):
            self.refused("admission_candidate", lambda: self.selected(candidate="a" * 40))
            self.refused("admission_candidate", lambda: self.selected(candidate="abc"))
            self.refused("selected_tree_mismatch", lambda: self.selected(tree="b" * 40))
            self.refused("admission_selected_request", lambda: self.selected(tree="bad"))
            self.refused("admission_candidate", lambda: admission.selected(other, objective="objective",
                                                                           candidate=self.base))
        other_head = git(other, "rev-parse", "HEAD")
        self.refused("objective_unknown", lambda: admission.selected(other, objective="objective",
                                                                     candidate=other_head))

    def test_a_record_naming_another_repository_refuses(self):
        self.ready()
        self.edit(lambda raw: raw["checkpoint"]["worktree"].update(repo_key="0" * 64)
                  if isinstance(raw["checkpoint"].get("worktree"), dict)
                  else raw["checkpoint"].update(worktree={"repo_key": "0" * 64, "path": "/elsewhere",
                                                          "branch": "main", "repository": None}))
        self.refused("selected_repository_mismatch", self.selected)

    def run_cli(self, *argv):
        out = StringIO()
        with redirect_stdout(out):
            code = cli.main(["admission", *argv])
        return code, out.getvalue()

    def test_cli_refuses_discovery_flags_and_requires_json(self):
        self.ready()
        base = ["--selected", "--repository", str(self.project), "--objective", "objective", "--candidate", self.base]
        for extra in (["--worktree", str(self.project)], ["--branch", "main"], ["--target", "main"],
                      ["--effect", "x"], ["--known-repository", str(self.project)], ["--batch"]):
            code, output = self.run_cli(*base, *extra, "--json")
            self.assertEqual(code, 1, extra)
            self.assertIn("admission_selected_request", output)
        code, output = self.run_cli(*base)
        self.assertEqual(code, 1)
        self.assertIn("admission_selected_request", output)
        code, output = self.run_cli("--repository", str(self.project), "--tree", self.tree(), "--json")
        self.assertEqual(code, 1)
        self.assertIn("admission_selected_request", output)

    def test_cli_answers_factually_with_exit_zero(self):
        self.ready()
        code, output = self.run_cli("--selected", "--repository", str(self.project), "--objective", "objective",
                                    "--candidate", self.base, "--tree", self.tree(), "--json")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["status"], "ready")


class DigestTests(SelectedCase):
    def test_digest_is_stable_and_expect_reports_a_change_as_a_conflict(self):
        self.ready()
        first = self.selected()
        again = self.selected(expect=first["digest"])
        self.assertEqual((again["digest"], again["conflicts"], again["status"]), (first["digest"], [], "ready"))
        canonical = {key: value for key, value in first.items()
                     if key not in ("digest", "status", "conflicts", "next_action")}
        from pod.util import digest
        self.assertEqual(first["digest"], digest(canonical))
        self.edit(lambda raw: raw["checkpoint"].update(plan_revision="changed"))
        changed = self.selected(expect=first["digest"])
        self.assertNotEqual(changed["digest"], first["digest"])
        self.assertEqual(changed["conflicts"], [{"kind": "digest_changed", "expected": first["digest"]}])
        self.assertEqual(changed["status"], "blocked")  # never ready, and never a refusal

    def test_the_returned_digest_is_the_digest_of_the_returned_facts_even_after_an_expect_conflict(self):
        self.ready()
        fresh = self.selected()
        stale = self.selected(expect="0" * 64)
        self.assertEqual((stale["status"], stale["conflicts"][0]["kind"]), ("blocked", "digest_changed"))
        self.assertEqual(stale["digest"], fresh["digest"])  # the expect conflict changes no fact
        for answer in (fresh, stale):
            self.assertEqual(facts_digest(answer), answer["digest"])
        self.assertEqual(self.selected(expect=stale["digest"])["status"], "ready")
        with self.assertRaises(PodError) as caught:
            self.selected(expect="not-a-digest")
        self.assertEqual(caught.exception.code, "admission_selected_request")

    def test_a_source_or_policy_change_changes_the_digest(self):
        self.ready()
        first = self.selected()["digest"]
        self.edit(lambda raw: raw["checkpoint"].update(policy_revision="0" * 64))
        self.assertNotEqual(self.selected()["digest"], first)

    def test_cli_expect_mismatch_exits_zero_with_the_conflict(self):
        self.ready()
        out = StringIO()
        with redirect_stdout(out):
            code = cli.main(["admission", "--selected", "--repository", str(self.project), "--objective",
                             "objective", "--candidate", self.base, "--expect", "0" * 64, "--json"])
        result = json.loads(out.getvalue())
        self.assertEqual((code, result["status"], result["conflicts"][0]["kind"]), (0, "blocked", "digest_changed"))


class UncertaintyTests(SelectedCase):
    def test_exhausted_deadline_is_unknown(self):
        self.ready()
        with patch("pod.admission._proof_view", side_effect=PodError("discovery_budget", "spent")):
            result = self.selected()
        self.assertEqual((result["status"], [row["code"] for row in result["unknowns"]]),
                         ("unknown", ["discovery_budget"]))

    def test_a_record_changed_during_the_read_is_unknown(self):
        self.ready()
        real = admission._proof_view

        def changing(*args):
            answer = real(*args)
            self.edit(lambda raw: raw["checkpoint"].update(plan_revision="moved"))
            return answer
        with patch("pod.admission._proof_view", side_effect=changing):
            result = self.selected()
        self.assertEqual((result["status"], [row["code"] for row in result["unknowns"]]),
                         ("unknown", ["record_changed"]))

    def test_a_failed_exact_worker_read_is_unknown(self):
        self.ready()
        with patch.object(OrcaPort, "read_native", side_effect=PodError("orca_unavailable", "unavailable")):
            result = self.selected()
        self.assertEqual(result["proof"]["status"], "unknown")
        self.assertEqual((result["status"], [row["code"] for row in result["unknowns"]]),
                         ("unknown", ["proof_unverified"]))

    def spent_after(self, calls):
        """A budget that runs out right after the given number of selected-record identity reads finish."""
        from pod.window import Budget
        budgets = []

        class Spending(Budget):
            spent = False

            def check(self):
                if self.spent:
                    raise PodError("discovery_budget", "spent")
                return super().check()

            def limit(self, seconds):
                self.check()
                return super().limit(seconds)

        def make(timeout):
            budgets.append(Spending(timeout))
            return budgets[-1]
        real, seen = admission._record_identity, []

        def identity(directory):
            value = real(directory)
            seen.append(directory)
            if len(seen) == calls:
                budgets[-1].spent = True
            return value
        return patch("pod.admission.Budget", side_effect=make), patch("pod.admission._record_identity", side_effect=identity)

    def test_a_deadline_spent_by_the_last_read_is_unknown(self):
        self.ready()
        budget, identity = self.spent_after(2)
        with budget, identity:
            result = self.selected()
        self.assertEqual((result["status"], [row["code"] for row in result["unknowns"]]),
                         ("unknown", ["discovery_budget"]))

    def test_a_deadline_spent_before_the_record_is_read_is_unknown_not_a_refusal(self):
        self.ready()
        with patch("pod.gitio.resolve_commit", side_effect=PodError("discovery_budget", "spent")):
            result = self.selected()
        self.assertEqual((result["status"], [row["code"] for row in result["unknowns"]]),
                         ("unknown", ["discovery_budget"]))
        self.assertEqual((result["binding"]["candidate"], result["binding"]["tree"]), (self.base, None))
        self.assertEqual(facts_digest(result), result["digest"])

    def test_a_deadline_spent_while_the_answer_is_published_is_unknown(self):
        """The reviewer's probe: the real budget expires right after the answer is finished."""
        from pod.window import Budget
        self.ready()
        budgets, real_finish = [], admission._selected_finish

        def make(timeout):
            budgets.append(Budget(timeout))
            return budgets[-1]

        def finish(result, expect):
            answer = real_finish(result, expect)
            budgets[-1].deadline = budgets[-1]._clock() - 1  # expired once the answer, digest and form exist
            return answer
        with patch("pod.admission.Budget", side_effect=make), \
             patch("pod.admission._selected_finish", side_effect=finish):
            result = self.selected()
        self.assertEqual((result["status"], [row["code"] for row in result["unknowns"]]),
                         ("unknown", ["discovery_budget"]))
        self.assertEqual(facts_digest(result), result["digest"])

    def test_the_final_serialized_answer_is_bounded(self):
        self.ready()
        answer = self.selected()
        size = len(json.dumps(answer, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        with patch("pod.orca.MAX_OUTPUT", size - 1):  # only the complete answer is over the bound
            result = self.selected()
        self.assertEqual((result["status"], result["digest"]), ("unknown", None))
        self.assertEqual(result["unknowns"][0]["code"], "selected_projection_limit")
        self.assertLessEqual(len(json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8")), size - 1)
        with patch("pod.orca.MAX_OUTPUT", size):
            self.assertEqual(self.selected()["status"], "ready")

    def test_an_answer_over_the_output_bound_is_withheld(self):
        self.ready()
        with patch("pod.orca.MAX_OUTPUT", 64):
            result = self.selected()
        self.assertEqual((result["status"], result["digest"], result["attempts"], result["assurances"]),
                         ("unknown", None, [], []))
        self.assertEqual(result["unknowns"][0]["code"], "selected_projection_limit")
