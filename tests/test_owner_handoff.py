"""B1 table and invariants through production boundaries; Orca alone is replaced."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tarfile
import unittest
from unittest.mock import patch

from pod import cli
from pod.errors import PodError
from pod.internal import run
from pod.ledger import objective_root, read
from pod.operations import OrcaPort
from tests.issue41_support import ProductionCase

SENTINEL = "PRESENTATION-MUST-NOT-LEAK"
BASELINE = "86d8b90db7c1d0f6a38c3c9167b950548ff79fad"


class HandoffCase(ProductionCase):
    def setUp(self):
        super().setUp()
        self.terminals = {"owner": "live", "next": "live", "third": "live"}
        self.native_calls, self.mutations, self.phase1 = [], [], []
        self.mode = "success"
        self.binding_failure = False
        self.worker_list_failure = False
        self.extra_workers = []
        mutation = patch("pod.orca.mutate_command", side_effect=self.mutate)
        mutation.start(); self.addCleanup(mutation.stop)

    def native_read(self, argv, **kwargs):
        self.native_calls.append(list(argv))
        if argv[:2] == ["terminal", "show"]:
            handle = argv[3]
            state = self.terminals.get(handle, "gone")
            if state in ("stale", "gone", "unsupported", "unreadable"):
                code = {"stale": "terminal_handle_stale", "gone": "terminal_not_found",
                        "unsupported": "terminal_unsupported_for_agent_session", "unreadable": "read_unavailable"}[state]
                raise PodError("orca_read_failed", SENTINEL, {"native_code": code})
            return {"runtime": self.port.runtime, "result": {"terminal": {
                "handle": handle, "connected": True, "writable": True, "orphaned": False,
                **{key: SENTINEL for key in ("preview", "title", "worktreePath", "branch", "ptyId", "tabId", "leafId")}}}}
        if argv[:2] == ["orchestration", "run-current"] and self.binding_failure:
            raise PodError("orca_unavailable", SENTINEL)
        if argv[:2] == ["orchestration", "worker-list"]:
            if self.worker_list_failure:
                raise PodError("orca_unavailable", SENTINEL)
            response = super().native_read(argv, **kwargs)
            response["result"]["scope"]["run"] = argv[argv.index("--run") + 1]
            response["result"]["workers"].extend(self.extra_workers)
            return response
        return super().native_read(argv, **kwargs)

    def mutate(self, argv, **kwargs):
        self.mutations.append(list(argv))
        state = read(self.project, "objective")
        self.phase1.append(deepcopy(state))
        entry = state["checkpoint"]["continuity"]["history"][-1]
        self.assertEqual(entry["state"], "pending")
        self.assertEqual(state["owner"], "owner")
        self.assertEqual(argv, ["orchestration", "run-use", "--id", "run", "--json"])
        if self.mode not in ("refused", "error_unchanged", "unreadable"):
            self.current.update(coordinator_handle=os.environ["ORCA_TERMINAL_HANDLE"], consumer_generation=2)
        if self.mode == "unreadable":
            self.binding_failure = True
        if self.mode in ("lost", "error_unchanged", "unreadable"):
            raise PodError("native_effect_uncertain", SENTINEL)
        return {"runtime": self.port.runtime, "exit": 1 if self.mode == "refused" else 0,
                "error": {"code": "refused"} if self.mode == "refused" else None, "result": {}}

    def establish(self, *, admissions=True):
        self.prepare()  # Real checkpoint and Governor journal, never hand-written records.
        self.write([self.criterion(), self.sub("S1", boundary={"paths": ["area1"]}),
                    self.sub("S2", boundary={"paths": ["area2"]})])
        self.frozen = self.packet(["S1"], boundary={"paths": ["area1"]})
        self.admission = self.start("task-1", self.frozen)["admission"] if admissions else None

    def lose(self, caller="next"):
        self.terminals["owner"] = "stale"
        self.port.runtime = "runtime-new"
        self.port.placement["runtime"] = self.port.runtime
        os.environ["ORCA_TERMINAL_HANDLE"] = caller
        self.native_calls.clear()

    def snapshot(self):
        return {str(p.relative_to(self.root / "state")): p.read_bytes()
                for p in (self.root / "state").rglob("*.json")}

    def state(self):
        return read(self.project, "objective")

    def status(self, *, text=False):
        out, err = StringIO(), StringIO()
        args = ["status", "--objective", "objective"] + ([] if text else ["--json"])
        old = Path.cwd()
        try:
            os.chdir(self.project)
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(args)
        finally:
            os.chdir(old)
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn(SENTINEL, out.getvalue() + err.getvalue())
        return out.getvalue() if text else json.loads(out.getvalue())

    def decision(self):
        return {"provenance": "user_direct", "instruction": "Make this terminal the objective coordinator",
                **self.status()["owner_handoff"]["facts"]}

    def handoff(self, decision=None, *, owner=None):
        return self.op("owner-handoff", owner=owner or os.environ["ORCA_TERMINAL_HANDLE"],
                       decision=self.decision() if decision is None else decision)

    def entry(self):
        return self.state()["checkpoint"]["continuity"]["history"][-1]

    def refused(self, call, *, code=None):
        with self.assertRaises(PodError) as caught:
            call()
        if code:
            self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertNotIn(SENTINEL, str(caught.exception) + json.dumps(caught.exception.detail))
        return caught.exception

    def boundary_calls(self, supplied):
        frozen = self.packet(["S2"], boundary={"paths": ["area2"]})
        return {
            "checkpoint": lambda: self.op("checkpoint", owner=supplied, value=self.core()),
            "governor": lambda: self.op("governor-prepare", owner=supplied, unit="next"),
            "report": lambda: self.report(self.admission, self.frozen),
            "admission": lambda: self.start("task-2", frozen, owner=supplied),
        }

    def assert_only_handoff_changes(self, before, after, *, completed):
        # One invariant covers every phase and failure sibling, independent of code structure.
        for state in (before, after):
            state.pop("revision")
            state["checkpoint"].pop("continuity")
            if completed:
                state["owner"] = "*"
                for ref in state["checkpoint"]["native_refs"]:
                    ref["runtime"] = "*"
                for row in state["admissions"].values():
                    row["runtime"] = row["owner"] = "*"
        self.assertEqual(after, before)
        self.assertNotIn(SENTINEL, json.dumps(self.state()))


class DetectionTableTests(HandoffCase):
    def test_detection_table_through_status_and_every_authority_boundary(self):
        self.establish()
        original_worker = deepcopy(self.port.workers)
        original_current = deepcopy(self.current)
        rows = [
            ("fresh", lambda: None, "handoff_available", "fresh"),
            ("owner live", lambda: self.terminals.update(owner="live"), "handoff_unavailable", "owner_not_lost"),
            ("owner unsupported", lambda: self.terminals.update(owner="unsupported"), "handoff_unavailable", "owner_not_lost"),
            ("owner unreadable", lambda: self.terminals.update(owner="unreadable"), "handoff_unavailable", "owner_not_lost"),
            ("caller lost", lambda: self.terminals.update(next="stale"), "handoff_unavailable", "caller_not_live"),
            ("caller unbound", lambda: setattr(self, "current", None), "handoff_unavailable", "caller_not_linked"),
            ("binding unreadable", lambda: setattr(self, "binding_failure", True), "handoff_unavailable", "binding_unreadable"),
            ("different Run", lambda: self.current.update(id="other"), "handoff_unavailable", "different_run"),
            ("manual caller rebind", lambda: self.current.update(coordinator_handle="next", consumer_generation=2), "handoff_unavailable", "lineage_unproven"),
            ("manual foreign rebind", lambda: self.current.update(coordinator_handle="third", consumer_generation=2), "handoff_unavailable", "lineage_unproven"),
            ("generation", lambda: self.current.update(consumer_generation=8), "handoff_unavailable", "generation_changed"),
            ("worker-only", lambda: self.extra_workers.append({"terminalHandle": "next"}), "handoff_unavailable", "caller_is_worker"),
            ("native worker handle", lambda: self.extra_workers.append({"agentTerminalHandle": "next"}), "handoff_unavailable", "caller_is_worker"),
            ("worker list unreadable", lambda: setattr(self, "worker_list_failure", True), "handoff_unavailable", "workers_unreadable"),
            ("work identity", lambda: next(iter(self.port.workers.values())).update(worktree="different"), "handoff_unavailable", "identity_differs"),
        ]
        self.lose()
        for name, vary, outcome, condition in rows:
            with self.subTest(row=name):
                self.current = deepcopy(original_current); self.port.workers = deepcopy(original_worker)
                self.terminals.update(owner="stale", next="live")
                self.binding_failure = self.worker_list_failure = False; self.extra_workers = []
                vary()
                before = self.snapshot()
                observed = self.status()["owner_handoff"]
                self.assertEqual((observed["status"], observed["condition"]), (outcome, condition))
                self.assertIn("run-use by hand", self.status(text=True))
                for supplied in ("owner", "next"):
                    for boundary, call in self.boundary_calls(supplied).items():
                        with self.subTest(boundary=boundary, supplied=supplied):
                            error = self.refused(call)
                            expected = "coordinator_conflict" if boundary in ("checkpoint", "admission") and supplied != "owner" else "native_authority_unverified"
                            self.assertEqual(error.code, expected)
                            self.assertEqual(error.detail["owner_handoff"]["condition"], condition)
                            self.assertNotIn("internal runtime-continuity", str(error) + json.dumps(error.detail))
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.mutations, [])
                self.assertEqual(len(self.port.starts), 1)
        self.current = original_current; self.port.workers = original_worker
        self.terminals.update(owner="stale", next="live"); self.binding_failure = self.worker_list_failure = False
        question = self.status()["owner_handoff"]["question"]
        self.assertIn("objective", question); self.assertIn("handle stale or gone", question)
        self.assertNotIn("next", question); self.assertNotIn("run", question)

    def test_shared_scope_and_every_bound_identity_disagreement(self):
        self.establish()
        # A second real checkpoint records the same Run; detection names the objective.
        run("checkpoint", {"project": str(self.project), "objective": "other-objective", "owner": "owner",
                           "value": self.core(obligations=[self.criterion()], governance={"base_ref":"refs/remotes/origin/target"})})
        self.lose(); before = self.snapshot()
        observed = self.status()["owner_handoff"]
        self.assertEqual(observed["condition"], "run_shared")
        self.assertEqual(observed["other_objectives"], ["other-objective"])
        self.refused(lambda: self.handoff())
        self.assertEqual(before, self.snapshot())

    def test_exact_identity_table_and_unreadable_ambiguity(self):
        self.establish(); self.lose()
        dispatch = self.admission["native_binding"]["dispatchId"]
        worker = deepcopy(self.port.workers[dispatch])
        for key in ("run", "task", "worktree"):
            with self.subTest(identity=key):
                self.port.workers[dispatch] = {**worker, key: "different"}
                self.assertEqual(self.status()["owner_handoff"]["condition"], "identity_differs")
        self.port.workers[dispatch] = worker
        original = self.port.show_worker
        for part in ("dispatch", "projection"):
            def disagree(key, part=part):
                value = original(key); value["result"][part]["id"] = "different"; return value
            with self.subTest(identity=part), patch.object(OrcaPort, "show_worker", side_effect=disagree):
                self.assertEqual(self.status()["owner_handoff"]["condition"], "identity_differs")
        # List stays readable, while exact worker-show is unavailable: confirmation alone covers it.
        with patch.object(OrcaPort, "show_worker", side_effect=PodError("orca_unavailable", SENTINEL)):
            detected = self.status()["owner_handoff"]
            self.assertEqual(detected["status"], "handoff_available")
            self.assertIn("worker read unavailable", detected["facts"]["ambiguity"][0])
            decision = self.decision()
            self.refused(lambda: self.handoff({**decision, "ambiguity": []}), code="handoff_decision_mismatch")
            self.handoff(decision)
        self.assertEqual(self.state()["admissions"][self.admission["admission_id"]]["state"], "bound")


class TransitionTableTests(HandoffCase):
    def test_transition_outcome_table_preserves_every_other_field_and_journal(self):
        # A fresh disposable objective for each sibling checks the same state invariant.
        for mode, expected, mutations in (("success", "done", 1), ("refused", "aborted", 1),
                                          ("error_unchanged", "aborted", 1), ("lost", "pending", 1),
                                          ("unreadable", "pending", 1)):
            case = HandoffCase(); case.setUp()
            try:
                with self.subTest(outcome=mode):
                    case.establish(); before = case.state(); journal = (objective_root(case.project, "objective") / "governor.json").read_bytes()
                    case.lose(); case.mode = mode; decision = case.decision()
                    if mode == "success": case.handoff(decision)
                    else: case.refused(lambda: case.handoff(decision))
                    self.assertEqual(case.entry()["state"], expected)
                    self.assertEqual(len(case.mutations), mutations)
                    case.assert_only_handoff_changes(deepcopy(before), deepcopy(case.phase1[0]), completed=False)
                    case.assert_only_handoff_changes(deepcopy(before), case.state(), completed=expected == "done")
                    self.assertEqual((objective_root(case.project, "objective") / "governor.json").read_bytes(), journal)
                    self.assertEqual(len(case.port.starts), 1)
                    if mode == "lost":
                        case.mode = "success"; case.handoff(decision)
                        self.assertEqual(case.entry()["state"], "done")
                        self.assertEqual(len(case.mutations), 1)
                    if mode == "unreadable":
                        case.binding_failure = False; case.current.update(coordinator_handle="next", consumer_generation=2)
                        case.handoff(decision)
                        self.assertEqual(len(case.mutations), 1)
            finally:
                case.doCleanups()

    def test_confirmation_table_refuses_before_pending_or_native_call(self):
        self.establish(); self.lose(); decision = self.decision(); before = self.snapshot()
        variants = [{**decision, "provenance": "project_policy"}, {**decision, "scope": "all"}]
        variants += [{**decision, key: [] if key == "ambiguity" else "mismatched"}
                     for key in decision if key not in ("provenance", "instruction", "ambiguity")]
        variants.append({**decision, "ambiguity": ["unobserved ambiguity"]})
        for value in variants:
            with self.subTest(confirmation=value):
                self.refused(lambda: self.handoff(value))
                self.assertEqual(self.snapshot(), before); self.assertEqual(self.mutations, [])
        self.refused(lambda: self.handoff({}))
        self.assertEqual(self.snapshot(), before)
        self.terminals["next"] = "stale"
        self.refused(lambda: self.handoff(decision)); self.assertEqual(self.snapshot(), before)

    def test_pending_is_caller_scoped_and_lost_holder_can_be_replaced(self):
        self.establish(); self.lose(); self.mode = "unreadable"; decision = self.decision()
        self.refused(lambda: self.handoff(decision)); self.binding_failure = False
        os.environ["ORCA_TERMINAL_HANDLE"] = "third"
        self.assertEqual(self.status()["owner_handoff"]["condition"], "pending_other_caller")
        before = self.snapshot(); self.refused(lambda: self.handoff()); self.assertEqual(before, self.snapshot())
        self.terminals["next"] = "stale"
        self.mode = "success"; self.handoff()
        self.assertEqual(self.state()["owner"], "third")
        self.assertEqual(len(self.state()["checkpoint"]["continuity"]["history"]), 1)
        self.assertEqual(self.entry()["abort_reason"], "pending caller handle stale or gone")

    def test_pending_definitive_loss_aborts_and_unreadable_facts_do_not(self):
        self.establish(); self.lose(); self.mode = "unreadable"
        self.refused(lambda: self.handoff()); self.binding_failure = False
        before = self.snapshot()
        self.binding_failure = True
        self.refused(lambda: self.handoff(self.entry()["decision"])); self.assertEqual(before, self.snapshot())
        self.binding_failure = False
        self.current.update(id="other")
        self.refused(lambda: self.handoff())
        self.assertEqual(self.entry()["state"], "aborted")
        self.assertEqual(self.entry()["abort_reason"], "different_run")

    def test_pending_eligibility_loss_table_aborts_only_its_history_entry(self):
        for condition in ("identity_differs", "run_shared", "caller_not_live"):
            case = HandoffCase(); case.setUp()
            try:
                with self.subTest(condition=condition):
                    case.establish(); case.lose(); case.mode = "lost"
                    case.refused(lambda: case.handoff())
                    if condition == "identity_differs":
                        next(iter(case.port.workers.values())).update(worktree="different")
                    elif condition == "run_shared":
                        run("checkpoint", {"project":str(case.project), "objective":"shared-objective", "owner":"next",
                            "value":case.core(obligations=[case.criterion()], governance={"base_ref":"refs/remotes/origin/target"})})
                    else:
                        case.terminals["next"] = "stale"
                    detected = case.status()["owner_handoff"]
                    self.assertEqual(detected["condition"], condition)
                    before = case.state(); snapshots = case.snapshot(); mutation_count = len(case.mutations)
                    case.refused(lambda: case.handoff())
                    self.assertEqual(case.entry()["state"], "aborted")
                    self.assertEqual(case.entry()["abort_reason"], condition)
                    case.assert_only_handoff_changes(before, case.state(), completed=False)
                    after = case.snapshot()
                    context_path = str((objective_root(case.project,"objective") / "context.json").relative_to(case.root / "state"))
                    self.assertEqual({key:value for key,value in snapshots.items() if key != context_path},
                                     {key:value for key,value in after.items() if key != context_path})
                    self.assertEqual(len(case.mutations), mutation_count)
            finally: case.doCleanups()

    def fill_history(self, n):
        for index in range(n):
            self.port.runtime = f"runtime-{index}"; self.port.placement["runtime"] = self.port.runtime
            self.write(self.stored())

    def test_history_invariant_full_resume_replacement_and_no_retry_entry(self):
        self.establish(); self.fill_history(7); self.lose(); self.mode = "refused"
        self.refused(lambda: self.handoff()); self.assertEqual(len(self.state()["checkpoint"]["continuity"]["history"]), 8)
        self.assertEqual(self.entry()["state"], "aborted")
        self.mode = "lost"; decision = self.decision(); self.refused(lambda: self.handoff(decision))
        self.assertEqual(len(self.state()["checkpoint"]["continuity"]["history"]), 8)
        self.assertEqual(self.status()["owner_handoff"]["condition"], "resume")
        before_calls = len(self.mutations); self.handoff(decision)
        self.assertEqual(len(self.mutations), before_calls)
        self.assertEqual(len(self.state()["checkpoint"]["continuity"]["history"]), 8)

    def test_full_history_without_reusable_record_and_closed_objective_refuse(self):
        self.establish(admissions=False); self.fill_history(8); self.lose(); before = self.snapshot()
        self.refused(lambda: self.handoff(), code="runtime_continuity_full")
        self.assertEqual(self.snapshot(), before); self.assertEqual(self.mutations, [])

    def test_no_work_effects_old_handle_refuses_and_lost_reviewer_settles_normally(self):
        self.establish(); self.lose(); self.handoff()
        state = self.state(); self.assertEqual(state["owner"], "next")
        self.assertEqual(state["checkpoint"]["continuity"]["binding"], {"run":"run", "coordinator":"next", "generation":2})
        self.assertEqual(self.entry()["verified"]["coordinator"], "owner")
        self.assertEqual(self.entry()["completion"]["coordinator"], "next")
        self.assertEqual(len(self.port.starts), 1)
        self.assertTrue(all(call[:2] != ["orchestration", "request-show"] for call in self.native_calls))
        with patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "owner"}):
            for call in self.boundary_calls("owner").values(): self.refused(call)
        worker = next(iter(self.port.workers.values())); worker.update(state="failed", outcome="failed")
        status = self.status()
        self.assertEqual(len(status["assignments"]["settled"]), 1)


    def test_closed_objective_is_detected_and_never_written(self):
        self.establish(admissions=False)
        rows = [self.criterion("satisfied", evidence=[self.proof("O1")]),
                {**self.sub("S1", "withdrawn"), "withdrawal":{"by":"coordinator", "reason":"folded into O1"}},
                {**self.sub("S2", "withdrawn"), "withdrawal":{"by":"coordinator", "reason":"folded into O1"}}]
        self.write(rows, close=True)
        self.lose(); before = self.snapshot()
        self.assertEqual(self.status()["owner_handoff"]["condition"], "objective_closed")
        self.refused(lambda: self.handoff(), code="objective_closed")
        self.assertEqual(self.snapshot(), before); self.assertEqual(self.mutations, [])

    def test_reserved_and_unresolved_ambiguity_table_stays_outstanding(self):
        self.establish(admissions=False)
        with patch.object(OrcaPort, "start_worker", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.start("reserved", self.frozen)
        second = self.packet(["S2"], boundary={"paths":["area2"]})
        with patch.object(OrcaPort, "start_worker", side_effect=PodError("native_effect_uncertain", "transport unavailable")):
            with self.assertRaises(PodError): self.start("unresolved", second)
        self.lose()
        before = self.state()
        self.assertEqual({row["state"] for row in before["admissions"].values()}, {"reserved", "unresolved"})
        variants = [(None, "unresolved attempt"), ([], "unresolved attempt"),
                    ([{"id":"synthetic-dispatch"}], "Dispatch found without a recorded native binding"),
                    ("inconclusive", "exact Run/Task lookup inconclusive"),
                    (PodError("orca_unavailable", "lookup unavailable"), "exact Run/Task lookup unavailable")]
        for returned, phrase in variants:
            with self.subTest(lookup=phrase):
                kwargs = {"side_effect": returned} if isinstance(returned, PodError) else {"return_value":returned}
                with patch.object(OrcaPort, "find_worker", **kwargs):
                    observed = self.status()["owner_handoff"]
                    self.assertEqual(observed["status"], "handoff_available")
                    self.assertIn(phrase, "; ".join(observed["facts"]["ambiguity"]))
        self.handoff()
        after = self.state()
        self.assertEqual({row["state"] for row in after["admissions"].values()}, {"reserved", "unresolved"})
        self.assertTrue(all(row["native_binding"] is None for row in after["admissions"].values()))
        self.assertEqual(self.status()["pending_admissions"], 2)
        self.assert_only_handoff_changes(before, after, completed=True)

    def test_lost_reviewer_remains_failed_and_reserved_work_is_not_replayed(self):
        self.establish(admissions=False)
        assurance = {"id":"A", "kind":"assurance", "provenance":"coordinator", "parent":"O1",
            "check":"review", "scope":{"paths":["src"]}, "question":"correct?", "candidate":self.candidate,
            "existing_evidence":"tests", "insufficiency":"no review", "state":"waiting",
            "wait":{"class":"sequenced", "referent":"O1"}}
        self.write([*self.stored(), assurance])
        frozen = self.packet(["A"], role="review")
        review = self.start("review", frozen)["admission"]
        self.lose(); self.handoff()
        self.port.workers[review["native_binding"]["dispatchId"]].update(state="failed", outcome="failed")
        observed = self.status()
        self.assertEqual(observed["assignments"]["settled"][0]["role"], "review")
        self.assertEqual(len(self.port.starts), 1)



class CompatibilityTests(HandoffCase):
    def test_genuine_080_reader_blocks_objective_and_skips_run_without_conversion(self):
        self.establish(); self.lose(); self.handoff(); before = self.snapshot()
        archive = self.root / "old.tar"
        root = Path(__file__).resolve().parents[1]
        with archive.open("wb") as stream:
            subprocess.run(["git", "-C", str(root), "archive", BASELINE, "skills/pod", "VERSION"], stdout=stream, check=True)
        old = self.root / "old"; old.mkdir()
        with tarfile.open(archive) as bundle: bundle.extractall(old, filter="data")
        script = '''import json,os
from pathlib import Path
from pod import __version__
from pod.status import status
from pod import orca
assert __version__ == '0.8.0'
# The genuine reader and authority functions are unchanged; replace only Orca reads.
orca.current_run=lambda: {'runtime':'runtime-new','run':{'id':'run','coordinator_handle':'next','consumer_generation':2}}
orca.worker_rows=lambda run: {'runtime':'runtime-new','workers':[],'complete':True,'scope':{'run':run}}
print(json.dumps({'objective':status(Path.cwd(),None,objective='objective'), 'run':status(Path.cwd(),'run')}))
'''
        env = {**os.environ, "PYTHONPATH": str(old / "skills")}
        result = subprocess.run([os.sys.executable, "-c", script], cwd=self.project, env=env, capture_output=True, text=True, check=True)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["objective"]["blocker"], "state_unsupported")
        self.assertNotEqual(observed["run"].get("objective"), "objective")
        self.assertEqual(self.snapshot(), before)
        import hashlib
        self.old_reader_proof = {"reader_commit": BASELINE, "reader_version": "0.8.0",
            "candidate": subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
            "objective_blocker": observed["objective"]["blocker"],
            "run_selected_objective": observed["run"].get("objective"),
            "before": {key: hashlib.sha256(value).hexdigest() for key, value in before.items()},
            "after": {key: hashlib.sha256(value).hexdigest() for key, value in self.snapshot().items()}}

    def test_genuine_066_writer_missing_generation_requires_named_confirmation(self):
        import shutil
        predecessor = "f4101feb8e517382cb4cc626a1afc702c6429f37"
        root = Path(__file__).resolve().parents[1]
        old = self.root / "writer-066"; old.mkdir()
        archive = self.root / "writer.tar"
        with archive.open("wb") as stream:
            subprocess.run(["git", "-C", str(root), "archive", predecessor, "skills/pod", "VERSION"], stdout=stream, check=True)
        with tarfile.open(archive) as bundle: bundle.extractall(old, filter="data")
        output = self.root / "writer-context.json"
        script = """from pathlib import Path
import shutil,sys
from pod import __version__
from pod.ledger import objective_root
from tests.issue41_support import ProductionCase
assert __version__ == '0.6.6'
case=ProductionCase();case.setUp()
try:
    case.write([case.criterion()],governance={'base_ref':'refs/remotes/origin/target'})
    shutil.copyfile(objective_root(case.project,'objective')/'context.json',sys.argv[1])
finally: case.doCleanups()
"""
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(old / "skills"), str(root)))}
        subprocess.run([os.sys.executable, "-c", script, str(output)], env=env, cwd=self.project, check=True, capture_output=True)
        destination = objective_root(self.project, "objective") / "context.json"
        destination.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(output, destination)
        self.assertNotIn("continuity", self.state()["checkpoint"])
        self.lose(); verdict = self.status()["owner_handoff"]
        self.assertEqual(verdict["status"], "handoff_available")
        self.assertIsNone(verdict["facts"]["recorded_generation"])
        self.assertIn("recorded consumer generation (none recorded)", verdict["facts"]["ambiguity"])
        decision = self.decision(); before = self.snapshot()
        self.refused(lambda: self.handoff({**decision, "ambiguity": []}), code="handoff_decision_mismatch")
        self.assertEqual(self.snapshot(), before)
        self.handoff(decision)
        self.assertEqual(self.entry()["state"], "done")

    def test_exact_native_allowlists(self):
        from pod.orca import _read_allowed, _mutate_allowed
        self.assertTrue(_read_allowed(["terminal", "show", "--terminal", "next", "--json"]))
        self.assertTrue(_mutate_allowed(["orchestration", "run-use", "--id", "run", "--json"]))
        for argv in (["terminal", "show", "--json"], ["terminal", "show", "--terminal", "-bad", "--json"],
                     ["terminal", "show", "--terminal", "next", "--preview", "--json"],
                     ["orchestration", "run-use", "--id", "run"], ["orchestration", "run-use", "--run", "run", "--json"],
                     ["orchestration", "run-use", "--id", "run", "--json", "--from", "next"],
                     ["orchestration", "run-create", "--json"]):
            with self.subTest(argv=argv):
                self.assertFalse(_read_allowed(argv)); self.assertFalse(_mutate_allowed(argv))


if __name__ == "__main__": unittest.main()
