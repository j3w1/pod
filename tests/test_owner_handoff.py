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
    def observation_peer(self, kind, *, objective="observation-peer"):
        def checkpoint(rows=None, **extra):
            value = self.core(governance={"base_ref":"refs/remotes/origin/target"}, **extra)
            if rows is not None: value["obligations"] = rows
            if kind.startswith("noverif"): value.pop("verification")
            return run("checkpoint", {"project":str(self.project), "objective":objective,
                "owner":os.environ["ORCA_TERMINAL_HANDLE"], "value":value})
        review = {"id":"A", "kind":"assurance", "provenance":"coordinator", "parent":"O1", "check":"review",
            "scope":{"paths":["src"]}, "question":"correct?", "candidate":self.candidate,
            "existing_evidence":"tests", "insufficiency":"no review", "state":"waiting",
            "wait":{"class":"sequenced", "referent":"O1"}}
        rows = [self.criterion(), review]
        if kind == "implementation": rows.append(self.sub("S1", boundary={"paths":["area1"]}))
        if kind == "other-assurance": rows.append({**review, "id":"B", "scope":{"paths":["docs"]}})
        checkpoint(rows)
        served = "S1" if kind == "implementation" else "B" if kind == "other-assurance" else "A"
        from pod.records import packet
        body = deepcopy(self.packet([served], role="implement" if kind == "implementation" else "review",
            boundary={"paths":["area1"]} if kind == "implementation" else None)["body"])
        body.update(objective=objective, map_revision=read(self.project, objective)["checkpoint"]["revision"])
        frozen = packet(body)
        admission = run("admission", {"project":str(self.project), "objective":objective,
            "owner":os.environ["ORCA_TERMINAL_HANDLE"], "run":"run", "task":"observation-attempt", "plan_revision":"plan", "packet":frozen})["admission"]
        rows = deepcopy(read(self.project, objective)["checkpoint"]["obligations"])
        if kind in ("implementation", "other-assurance", "noverif-failed"):
            rows[1]["evidence"] = [{"attempt":admission["admission_id"]}]
            checkpoint(rows)
        if kind.startswith("noverif"):
            self.settle(admission)
            rows[1].pop("executor"); rows[1].update(state="waiting", wait={"class":"sequenced", "referent":"O1"})
            report = {"schema":"pod-report/v1", "assignment":frozen["packet_id"], "attempt":admission["native_binding"]["dispatchId"],
                "candidate":self.candidate, "outcome":"failed" if kind.endswith("failed") else "succeeded", "scope":frozen["body"]["scope"],
                "files":[], "checks":["observed"], "failures":[], "evidence":[], "uncertainty":[], "questions":[]}
            run("report", {"project":str(self.project), "objective":objective, "admission_id":admission["admission_id"],
                "packet":frozen, "report":report, "map":{"obligations":rows}})
        return checkpoint, admission

    def reused_review_peer(self, objective="history-peer"):
        self.closed_review_peer(objective=objective)
        original = self.candidate
        self.move("README.md")
        def checkpoint(rows):
            return run("checkpoint", {"project":str(self.project), "objective":objective,
                "owner":os.environ["ORCA_TERMINAL_HANDLE"], "value":self.core(obligations=rows,
                reopen=True, close=True, revision_authority={"provenance":"user_direct", "instruction":"Reopen the disposable peer"})})
        rows = deepcopy(read(self.project, objective)["checkpoint"]["obligations"])
        rows[0]["evidence"] = [{"check":"criterion", "command":"unit", "result":"passed", "reference":"fresh-peer-proof"}]
        rows[1].update(candidate=self.candidate, reuse={"from":original})
        checkpoint(rows)
        rows = deepcopy(read(self.project, objective)["checkpoint"]["obligations"])
        rows[1].pop("evidence"); rows[1].pop("reuse")
        rows[1].update(state="withdrawn", withdrawal={"by":"coordinator", "reason":"review no longer required"})
        checkpoint(rows)

    def assert_peer_scope(self, peer, *, eligible, phase, unverifiable=False):
        before = self.snapshot(); own = deepcopy(self.state()); calls = len(self.mutations)
        starts, workers = deepcopy(self.port.starts), deepcopy(self.port.workers)
        condition = ("fresh" if phase == "fresh" else "resume") if eligible else "peer_history_unverifiable" if unverifiable else "run_shared"
        verdict = self.status()["owner_handoff"]
        self.assertEqual(verdict["condition"], condition)
        self.assertIn(condition if not eligible else "handoff", self.status(text=True))
        for supplied in ("owner", "next"):
            for name, call in self.boundary_calls(supplied).items():
                with self.subTest(boundary=name, supplied=supplied):
                    error = self.refused(call)
                    self.assertEqual(error.detail["owner_handoff"]["condition"], condition)
        if eligible:
            self.handoff(); self.assertEqual(self.entry()["state"], "done")
            self.assertEqual(len(self.mutations) - calls, phase == "fresh")
            self.assert_only_handoff_changes(own, self.state(), completed=True)
        else:
            self.refused(lambda:self.handoff()); self.assertEqual(len(self.mutations), calls)
            if phase == "pending" and not unverifiable:
                self.assertEqual(self.entry()["state"], "aborted")
                self.assert_only_handoff_changes(own, self.state(), completed=False)
            else: self.assertEqual(own, self.state())
        ownkey = str((objective_root(self.project, "objective") / "context.json").relative_to(self.root / "state"))
        self.assertEqual({k:v for k,v in before.items() if k != ownkey}, {k:v for k,v in self.snapshot().items() if k != ownkey})
        self.assertEqual(peer.read_bytes(), before[str(peer.relative_to(self.root / "state"))])
        self.assertEqual(self.port.starts, starts); self.assertEqual(self.port.workers, workers)

    def closed_review_peer(self, *, withdrawn=False, objective="closure-peer", unknown=False, packet_scope=None):
        def checkpoint(rows, **extra):
            return run("checkpoint", {"project":str(self.project), "objective":objective,
                "owner":os.environ["ORCA_TERMINAL_HANDLE"], "value":self.core(obligations=rows,
                governance={"base_ref":"refs/remotes/origin/target"}, **extra)})
        review = {"id":"A", "kind":"assurance", "provenance":"coordinator", "parent":"O1", "check":"review",
            "scope":{"paths":["src"]}, "question":"correct?", "candidate":self.candidate,
            "existing_evidence":"tests", "insufficiency":"no review", "state":"waiting",
            "wait":{"class":"sequenced", "referent":"O1"}}
        checkpoint([self.criterion(), review])
        from pod.records import packet
        body = deepcopy(self.packet(["A"], role="review")["body"])
        if packet_scope is not None: body["scope"] = list(packet_scope)
        body.update(objective=objective, map_revision=read(self.project, objective)["checkpoint"]["revision"])
        frozen = packet(body)
        admission = run("admission", {"project":str(self.project), "objective":objective,
            "owner":os.environ["ORCA_TERMINAL_HANDLE"], "run":"run", "task":"peer-review", "plan_revision":"plan", "packet":frozen})["admission"]
        if unknown:
            rows = deepcopy(read(self.project, objective)["checkpoint"]["obligations"])
            rows[1]["evidence"] = [{"attempt":"unobserved-review"}, {"attempt":admission["admission_id"]}]
            checkpoint(rows)
        self.settle(admission)
        rows = deepcopy(read(self.project, objective)["checkpoint"]["obligations"])
        for row in rows: row.pop("executor")
        rows[0].update(state="satisfied", evidence=[{"check":"criterion", "command":"unit", "result":"passed", "reference":"peer-proof"}])
        rows[1].update(state="satisfied", evidence=[{"attempt":admission["admission_id"]}])
        report = {"schema":"pod-report/v1", "assignment":frozen["packet_id"], "attempt":admission["native_binding"]["dispatchId"],
            "candidate":self.candidate, "outcome":"succeeded", "scope":frozen["body"]["scope"], "files":[], "checks":["unit"],
            "failures":[], "evidence":[], "uncertainty":[], "questions":[]}
        run("report", {"project":str(self.project), "objective":objective, "admission_id":admission["admission_id"],
            "packet":frozen, "report":report, "map":{"obligations":rows}})
        rows = deepcopy(read(self.project, objective)["checkpoint"]["obligations"])
        if withdrawn:
            rows[1].pop("evidence"); rows[1].update(state="withdrawn", withdrawal={"by":"coordinator", "reason":"review no longer required"})
        checkpoint(rows, close=True)

    def released_peer(self, commit, version, objective="closure-peer"):
        """Emit untouched peer bytes with a complete immutable released bundle."""
        root = Path(__file__).resolve().parents[1]
        old = self.root / ("writer-" + version); old.mkdir(); archive = old / "bundle.tar"
        with archive.open("wb") as stream:
            subprocess.run(["git", "-C", str(root), "archive", commit, "skills/pod", "VERSION"], stdout=stream, check=True)
        with tarfile.open(archive) as bundle: bundle.extractall(old, filter="data")
        for source in (old / "skills/pod").rglob("*"):
            if source.is_file() and source.name != "VERSION":
                self.assertEqual(source.read_bytes(), subprocess.check_output(["git", "-C", str(root), "show", commit + ":" + str(source.relative_to(old))]))
        script = '''import json,os,sys
from pathlib import Path
import pod
from pod import orca
from pod.internal import run
from pod.ledger import objective_root
data=json.load(sys.stdin)
assert pod.__version__ == data['version']
def read(argv,**kwargs):
 if argv[:2]==['orchestration','run-current']:return {'runtime':data['runtime'],'result':{'run':data['current']}}
 if argv[:2]==['orchestration','worker-list']:return {'runtime':data['runtime'],'result':{'workers':[],'scope':{'run':data['current']['id'],'source':'flag'},'page':{'hasMore':False}}}
 raise AssertionError('unexpected released-writer native read '+str(argv))
orca.read_command=read
orca.contract=lambda:{'status':'observed','runtime':data['runtime'],'capabilities':{}}
if data['version']=='0.5.0':
 value={'schema':'pod-checkpoint/v1','criteria':['shared work'],'plan_revision':'plan',
 'candidate':'candidate','policy_revision':'r','native_refs':[{'runId':'run','runtime':'runtime'}],
 'assignments':[],'questions':[],'verification_gaps':['shared work'],'next_safe_action':'continue shared work'}
 run('checkpoint',{'project':str(Path.cwd()),'objective':data['objective'],'owner':'owner','value':value})
else:
 value=data['core']
 value['obligations']=[{'id':'O1','kind':'criterion','provenance':'objective','source':{'ref':'PoD#1'},
 'check':'PoD#1 passes','state':'satisfied','evidence':[{'check':'unit','command':'unit command','result':'passed','reference':'released-history'}]}]
 value['governance']={'base_ref':'refs/remotes/origin/target'}
 run('checkpoint',{'project':str(Path.cwd()),'objective':data['objective'],'owner':os.environ['ORCA_TERMINAL_HANDLE'],'value':value})
 value['obligations'][0]['evidence'][0]['reference']='released-selected';value['close']=True
 run('checkpoint',{'project':str(Path.cwd()),'objective':data['objective'],'owner':os.environ['ORCA_TERMINAL_HANDLE'],'value':value})
print(json.dumps({'path':str(objective_root(Path.cwd(),data['objective'])/'context.json')}))
'''
        process = subprocess.run([os.sys.executable, "-c", script], cwd=self.project,
            input=json.dumps({"version":version, "objective":objective, "runtime":self.port.runtime,
                              "current":self.current, "core":self.core()}),
            env={**os.environ, "PYTHONPATH":str(old / "skills")}, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        return Path(json.loads(process.stdout)["path"])

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

    def status(self, *, text=False, objective="objective"):
        out, err = StringIO(), StringIO()
        args = ["status", "--objective", objective] + ([] if text else ["--json"])
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
    def test_owner_hold_for_unverifiable_original_reuse_scope(self):
        for phase in ("fresh", "pending"):
            for kind in ("review-scope", "review-scope-and-question", "ordinary-proof-scope"):
                case = HandoffCase(); case.setUp()
                try:
                    with self.subTest(phase=phase, kind=kind):
                        case.establish()
                        if phase == "pending":
                            case.lose(); case.mode = "lost"; case.refused(lambda:case.handoff()); case.mode = "success"
                        def checkpoint(rows=None, **extra):
                            value = case.core(governance={"base_ref":"refs/remotes/origin/target"}, **extra)
                            if rows is not None: value["obligations"] = rows
                            return run("checkpoint", {"project":str(case.project), "objective":"hold-peer",
                                "owner":os.environ["ORCA_TERMINAL_HANDLE"], "value":value})
                        if kind.startswith("review-"):
                            case.closed_review_peer(objective="hold-peer", packet_scope=("src", "docs"))
                        else:
                            checkpoint([case.criterion("satisfied", proof_scope=["src"], evidence=[
                                {"check":"criterion", "command":"unit", "result":"passed", "reference":"original-proof"}])], close=True)
                        original = case.candidate
                        from tests.kernel_support import git
                        (case.project / "docs").mkdir(); (case.project / "docs/extra.txt").write_text("outside original verified scope\n")
                        git(case.project, "add", "."); git(case.project, "commit", "-qm", "unaffected original proof")
                        case.candidate = git(case.project, "rev-parse", "HEAD")
                        authority = {"provenance":"user_direct", "instruction":"Revise the disposable definition and preserve its history"}
                        rows = deepcopy(read(case.project, "hold-peer")["checkpoint"]["obligations"])
                        if kind.startswith("review-"):
                            rows[0]["evidence"] = [{"check":"criterion", "command":"unit", "result":"passed", "reference":"new-proof"}]
                            rows[1].update(candidate=case.candidate, reuse={"from":original})
                        else: rows[0]["reuse"] = {"from":original}
                        checkpoint(rows, reopen=True, close=True, revision_authority=authority)
                        rows = deepcopy(read(case.project, "hold-peer")["checkpoint"]["obligations"])
                        row = rows[1] if kind.startswith("review-") else rows[0]
                        row.pop("evidence"); row.pop("reuse")
                        row.update(state="withdrawn", withdrawal={"by":"coordinator" if kind.startswith("review-") else "user_direct", "reason":"authorized definition revision"})
                        if kind.startswith("review-"): row["scope"] = {"paths":["elsewhere"]}
                        else: row["proof_scope"] = ["elsewhere"]
                        if kind.endswith("question"): row["question"] = "authorized changed question"
                        checkpoint(rows, reopen=True, close=True, revision_authority=authority)
                        checkpoint(reopen=True, close=True, revision_authority=authority)
                        stored = read(case.project, "hold-peer")
                        ob = stored["checkpoint"]["obligations"][1 if kind.startswith("review-") else 0]
                        receipt = ob["receipts"][0]
                        self.assertNotEqual(receipt["evidence"]["definition"], ob["definition"])
                        self.assertEqual(receipt["reuse"]["delta"], ["docs/extra.txt"])
                        if kind.startswith("review-"):
                            self.assertEqual(next(iter(stored["admissions"].values()))["report"]["observation"]["scope"], ["src", "docs"])
                        peer = objective_root(case.project, "hold-peer") / "context.json"
                        if phase == "fresh": case.lose()
                        before = case.snapshot(); calls = len(case.mutations)
                        verdict = case.status()["owner_handoff"]
                        self.assertEqual(verdict["unverifiable_peers"], ["hold-peer"])
                        self.assertIn("hold-peer", case.status(text=True))
                        self.assertNotIn("run-use --id", case.status(text=True))
                        decision = case.decision()
                        for confirmation in (decision, {**decision, "scope":"all"}, {**decision, "ambiguity":["override peer hold"]}):
                            case.refused(lambda:case.handoff(confirmation))
                        self.assertEqual(case.snapshot(), before); self.assertEqual(len(case.mutations), calls)
                        case.assert_peer_scope(peer, eligible=False, phase=phase, unverifiable=True)
                finally: case.doCleanups()

    def test_faithful_observations_remain_readable_and_never_qualify(self):
        for phase in ("fresh", "pending"):
            for kind in ("noverif-succeeded", "noverif-failed", "implementation", "other-assurance"):
                for path in ("carry", "restated", "coordinator", "user"):
                    case = HandoffCase(); case.setUp()
                    try:
                        with self.subTest(phase=phase, kind=kind, path=path):
                            case.establish()
                            if phase == "pending":
                                case.lose(); case.mode = "lost"; case.refused(lambda:case.handoff()); case.mode = "success"
                            checkpoint, admission = case.observation_peer(kind)
                            peer = objective_root(case.project, "observation-peer") / "context.json"
                            original = deepcopy(read(case.project, "observation-peer")["checkpoint"]["obligations"][1]["receipts"])
                            rows = deepcopy(read(case.project, "observation-peer")["checkpoint"]["obligations"])
                            if path in ("coordinator", "user"):
                                for field in ("wait", "executor", "evidence"): rows[1].pop(field, None)
                                rows[1].update(state="withdrawn", withdrawal={"by":"coordinator" if path == "coordinator" else "user_direct", "reason":"observation retained"})
                            authority = {"provenance":"user_direct", "instruction":"Withdraw the disposable assurance"}
                            checkpoint(None if path == "carry" else rows, **({"revision_authority":authority} if path == "user" else {}))
                            state = read(case.project, "observation-peer")
                            self.assertEqual(state["checkpoint"]["obligations"][1]["receipts"], original)
                            # These are real records; an observation supplies no passing proof.
                            from pod.ledger import kernel_context
                            from pod.assurance import evidence_valid
                            from pod.obligations import governance_digest
                            evidence = original[0]["evidence"]
                            ob = state["checkpoint"]["obligations"][1]
                            ctx = kernel_context(case.project, "observation-peer", state, None)
                            self.assertFalse(evidence_valid({**ob, "evidence":[evidence]}, ctx, governance_digest(state["checkpoint"]["governance_sources"])))
                            if kind.startswith("noverif"):
                                rows = deepcopy(state["checkpoint"]["obligations"])
                                for row in rows:
                                    if row["state"] == "withdrawn": continue
                                    for field in ("wait", "executor", "evidence", "withdrawal"): row.pop(field, None)
                                    row.update(state="withdrawn", withdrawal={"by":"user_direct", "reason":"close observation-only peer"})
                                checkpoint(rows, close=True, revision_authority=authority)
                            if phase == "fresh": case.lose()
                            case.assert_peer_scope(peer, eligible=kind.startswith("noverif"), phase=phase)
                    finally: case.doCleanups()

    def test_retained_review_history_keeps_assignment_and_git_facts(self):
        marker_cases = [(markers, kind) for markers in ((), ("accepted_seq",), ("reported_seq",), ("accepted_seq", "reported_seq"))
                        for kind in ("genuine", "null-governance", "role", "serves")]
        cases = marker_cases + [((), kind) for kind in ("unknown-genuine", "duplicate", "reported-before-admitted", "reuse-genuine",
                 "reuse-unknown-target", "reuse-false-delta", "reuse-source", "reuse-definition", "reuse-duplicate", "reuse-unreadable", "reuse-touched-accurate")]
        for phase in ("fresh", "pending"):
            for markers, kind in cases:
                case = HandoffCase(); case.setUp()
                try:
                    with self.subTest(phase=phase, omitted=markers, kind=kind):
                        case.establish()
                        if phase == "pending":
                            case.lose(); case.mode = "lost"; case.refused(lambda:case.handoff()); case.mode = "success"
                        if kind.startswith("reuse-"): case.reused_review_peer()
                        else: case.closed_review_peer(withdrawn=True, objective="history-peer", unknown=kind == "unknown-genuine")
                        peer = objective_root(case.project, "history-peer") / "context.json"
                        raw = json.loads(peer.read_text()); cp = raw["checkpoint"]
                        ob = cp["obligations"][1]; receipt = ob["receipts"][0]; evidence = receipt["evidence"]
                        admission = next(iter(raw["admissions"].values()))
                        for marker in markers: receipt.pop(marker)
                        if kind == "null-governance": evidence["governance"] = None
                        elif kind == "role": admission["role"] = "investigate"
                        elif kind == "serves": admission["serves"] = ["O1"]
                        elif kind == "duplicate": ob["receipts"].append(deepcopy(receipt))
                        elif kind == "reported-before-admitted": receipt["reported_seq"] = admission["admitted_seq"] - 1
                        elif kind == "reuse-unknown-target": receipt["reuse"]["to"] = "nonexistent-target"
                        elif kind == "reuse-false-delta": receipt["reuse"]["delta"] = []
                        elif kind == "reuse-source": receipt["reuse"]["from"] = "unknown-source"
                        elif kind == "reuse-definition": receipt["reuse"]["definition"] = "0" * 64
                        elif kind == "reuse-duplicate": receipt["reuse"]["delta"] *= 2
                        elif kind == "reuse-unreadable":
                            receipt["reuse"]["to"] = "0" * 40
                        elif kind == "reuse-touched-accurate":
                            from tests.kernel_support import git
                            (case.project / "src/old.py").write_text("changed original reviewed scope\n")
                            git(case.project, "add", "."); git(case.project, "commit", "-qm", "touch reviewed scope")
                            target = git(case.project, "rev-parse", "HEAD")
                            receipt["reuse"].update(to=target, delta=sorted(git(case.project, "diff", "--name-only", receipt["reuse"]["from"], target).split()))
                        if markers or kind not in ("genuine", "reuse-genuine", "unknown-genuine"):
                            peer.write_text(json.dumps(raw))  # Defensive corrupt-record control, never writer/live proof.
                        if phase == "fresh": case.lose()
                        case.assert_peer_scope(peer, eligible=kind in ("genuine", "reuse-genuine", "unknown-genuine"), phase=phase)
                finally: case.doCleanups()

    def test_continuity_runtime_shapes_refuse_before_fingerprint_collection(self):
        values = ([], {}, None, "", " ", "runtime\x00bad", "x" * 4097, 1, True)
        cases = [(field, value) for field in ("from_runtime", "to_runtime") for value in values]
        cases += [(field, "missing") for field in ("from_runtime", "to_runtime")]
        cases += [("row", value) for value in (None, [], "bad", {}, {"provenance":"owner_handoff"})]
        for phase in ("fresh", "pending"):
            for field, value in cases:
                case = HandoffCase(); case.setUp()
                try:
                    with self.subTest(phase=phase, field=field, shape=type(value).__name__):
                        case.establish()
                        if phase == "pending":
                            case.lose(); case.mode = "lost"; case.refused(lambda:case.handoff()); case.mode = "success"
                        case.closed_review_peer(withdrawn=True, objective="runtime-peer")
                        peer = objective_root(case.project, "runtime-peer") / "context.json"
                        raw = json.loads(peer.read_text()); entry = {"from_runtime":"runtime", "to_runtime":"runtime",
                            "at":"offline control", "provenance":"automatic", "verified":{}}
                        if field == "row": entry = value
                        elif value == "missing": entry.pop(field)
                        else: entry[field] = value
                        raw["checkpoint"]["continuity"]["history"].append(entry)
                        peer.write_text(json.dumps(raw))  # Defensive shape control, never writer/live proof.
                        before = case.snapshot()
                        error = case.refused(lambda:run("checkpoint", {"project":str(case.project), "objective":"runtime-peer",
                            "owner":os.environ["ORCA_TERMINAL_HANDLE"], "value":case.core(reopen=True,
                            revision_authority={"provenance":"user_direct", "instruction":"Reopen disposable peer"}, close=True)}))
                        self.assertTrue(error.code.startswith(("invalid_", "state_")))
                        self.assertIsInstance(case.status(objective="runtime-peer"), dict)
                        case.status(text=True, objective="runtime-peer")
                        self.assertEqual(before, case.snapshot())
                        if phase == "fresh": case.lose()
                        case.assert_peer_scope(peer, eligible=False, phase=phase)
                finally: case.doCleanups()

    def test_complete_persisted_closure_contract_for_fresh_and_pending(self):
        # One scope invariant at real boundaries. Corruption controls begin with
        # real accepted records; none claims genuine-writer or live-loss proof.
        fields = ("schema", "criterion", "candidate", "sources", "policy_revision", "dependencies",
                  "environment", "check", "command", "result", "timestamp", "status", "reference", "definition", "governance")
        cases = (["selected-missing-" + field for field in fields]
                 + ["retained-missing-" + field for field in fields]
                 + ["selected-null", "selected-string", "selected-empty", "selected-extra", "selected-criterion",
                    "selected-sources", "selected-dependencies", "selected-reference", "retained-null",
                    "missing-receipts", "null-receipts", "null-receipt", "duplicate-receipt", "contradictory-receipt",
                    "receipt-negative", "receipt-zero", "receipt-boolean", "receipt-future", "receipt-unaccepted",
                    "receipt-reuse", "retained-negative", "history-overflow", "withdrawal-coordinator",
                    "withdrawal-nobody", "withdrawal-null", "withdrawal-no-by", "withdrawal-no-reason",
                    "withdrawal-no-instruction", "withdrawal-empty-instruction", "withdrawal-extra",
                    "criteria-missing", "criteria-extra", "criteria-null", "obligations-empty", "duplicate-id",
                    "introduced-negative", "definition-invalid", "boundary-missing", "source-null", "governance-null",
                    "checkpoint-missing-candidate", "checkpoint-invalid-verification", "checkpoint-unknown-field",
                    "outer-nonobject", "outer-unsupported", "outer-unsupported-foreign",
                    "outstanding-bound", "outstanding-reserved", "outstanding-unresolved",
                    "genuine-closed", "genuine-history", "genuine-user-withdrawal", "genuine-coordinator-withdrawal",
                    "genuine-steering", "genuine-assurance-withdrawal", "genuine-foreign", "genuine-071", "genuine-080", "released-050-shared",
                    "governance-header-null", "proposals-null", "proposal-null", "observations-null", "observation-row-null",
                    "reopened-null", "reopen-row-null", "quiescence-invalid", "slot-contradiction",
                    "assurance-findings-null", "assurance-finding-null",
                    "worktree-list", "worktree-string", "worktree-integer", "worktree-boolean", "worktree-null",
                    "checkpoint-null-policy", "retained-reuse-conflict", "retained-reuse-definition",
                    "governance-header-base", "governance-header-selection", "governance-header-exclusion",
                    "genuine-policy", "policy-line-reversed", "policy-line-outside", "policy-quote",
                    "policy-revision", "policy-base", "policy-undeclared", "policy-forged-gone"])
        cases += ["genuine-reviewed-closed", "genuine-reviewed-withdrawn", "consumed-report-null", "consumed-report-digest",
                  "consumed-report-attempt", "consumed-report-observation", "consumed-report-result", "consumed-report-runtime",
                  "accepted-review-null-governance", "accepted-review-null-binding", "accepted-review-null-definition",
                  "accepted-review-null-candidate",
                  *["consumed-report-missing-" + field for field in ("schema", "assignment", "attempt", "candidate", "outcome",
                    "scope", "files", "checks", "failures", "evidence", "uncertainty", "questions")]]
        for phase in ("fresh", "pending"):
            for kind in cases:
                case = HandoffCase(); case.setUp()
                try:
                    with self.subTest(phase=phase, kind=kind):
                        if kind.startswith("policy-") or kind == "genuine-policy":
                            from tests.kernel_support import git
                            (case.project / "AGENTS.md").write_text("# Rules\n\nKeep project facts intact.\n")
                            git(case.project, "add", "AGENTS.md"); git(case.project, "commit", "-qm", "policy")
                            case.base = case.candidate = git(case.project, "rev-parse", "HEAD")
                            git(case.project, "update-ref", "refs/remotes/origin/target", case.base)
                        case.establish()
                        if phase == "pending":
                            case.lose(); case.mode = "lost"; case.refused(lambda:case.handoff()); case.mode = "success"
                        current = deepcopy(case.current)
                        if kind in ("genuine-foreign", "outer-unsupported-foreign"):
                            case.current.update(id="foreign-run")
                        def checkpoint(rows, **extra):
                            return run("checkpoint", {"project":str(case.project), "objective":"closure-peer",
                                "owner":os.environ["ORCA_TERMINAL_HANDLE"],
                                "value":case.core(obligations=rows, governance={"base_ref":"refs/remotes/origin/target"}, **extra)})
                        # Two genuine writes establish immutable selected/retained receipts.
                        released = kind in ("genuine-071", "genuine-080", "released-050-shared")
                        if released:
                            commit, version = {"genuine-071":("2f02be99f27e6a860c3115a9c62539b0812bc038", "0.7.1"),
                                               "genuine-080":(BASELINE, "0.8.0"),
                                               "released-050-shared":("9c8639a5d7406b708b67b96a5ab26677efae1581", "0.5.0")}[kind]
                            case.released_peer(commit, version)
                        else:
                            checkpoint([case.criterion("active" if kind == "genuine-history" else "satisfied",
                                evidence=[case.proof("O1", reference="history-proof", status="FAILED" if kind == "genuine-history" else "PASS")])])
                        rows = [case.criterion("satisfied", evidence=[case.proof("O1", reference="selected-proof")])]
                        extra = {"close":True}
                        if kind.startswith("policy-") or kind == "genuine-policy":
                            rows.append({"id":"P", "kind":"criterion", "provenance":"project_policy",
                                "source":{"path":"AGENTS.md", "lines":"3"}, "check":"policy satisfied",
                                "state":"satisfied", "evidence":[{"check":"policy satisfied", "command":"policy check",
                                    "result":"passed", "reference":"policy-proof"}]})
                        if kind.startswith("withdrawal-") or kind == "genuine-user-withdrawal":
                            rows = [case.criterion("withdrawn", withdrawal={"by":"user_direct", "reason":"Owner withdrew this criterion"})]
                            extra["revision_authority"] = {"provenance":"user_direct", "instruction":"Withdraw the peer criterion"}
                        elif kind == "genuine-coordinator-withdrawal":
                            rows.append(case.sub("S", "withdrawn", withdrawal={"by":"coordinator", "reason":"Subgoal no longer needed"}))
                        elif kind == "genuine-steering":
                            rows.append({"id":"U", "kind":"steer", "provenance":"user_direct", "source":{"instruction":"Record steering", "ref":"owner-note"},
                                "check":"steering recorded", "state":"withdrawn", "withdrawal":{"by":"user_direct", "reason":"Owner withdrew steering"}})
                            extra["revision_authority"] = {"provenance":"user_direct", "instruction":"Record then withdraw steering"}
                        elif kind in ("genuine-assurance-withdrawal", "assurance-findings-null", "assurance-finding-null"):
                            rows.append({"id":"A", "kind":"assurance", "provenance":"coordinator", "parent":"O1",
                                "check":"independent review", "scope":{"paths":["src"]}, "question":"correct?", "candidate":case.candidate,
                                "existing_evidence":"tests", "insufficiency":"no review", "state":"withdrawn",
                                "withdrawal":{"by":"user_direct", "reason":"Owner withdrew peer review"}})
                            extra["revision_authority"] = {"provenance":"user_direct", "instruction":"Withdraw peer review"}
                        reviewed = kind.startswith(("consumed-report-", "accepted-review-")) or kind in ("genuine-reviewed-closed", "genuine-reviewed-withdrawn")
                        if reviewed: case.closed_review_peer(withdrawn=kind == "genuine-reviewed-withdrawn" or kind.startswith("accepted-review-"))
                        elif not released: checkpoint(rows, **extra)
                        case.current = current
                        peer = objective_root(case.project, "closure-peer") / "context.json"
                        raw = json.loads(peer.read_text()); cp = raw["checkpoint"]; ob = (cp.get("obligations") or [{}])[0]
                        selected = ob.get("evidence", [{}])[0] if ob.get("evidence") else {}
                        retained = (ob.get("receipts") or [{"evidence":{}}])[0]["evidence"]
                        if kind.startswith("selected-missing-"): selected.pop(kind.removeprefix("selected-missing-"))
                        elif kind.startswith("retained-missing-"): retained.pop(kind.removeprefix("retained-missing-"))
                        elif kind == "selected-null": ob["evidence"] = [None]
                        elif kind == "selected-string": ob["evidence"] = ["not evidence"]
                        elif kind == "selected-empty": ob["evidence"] = []
                        elif kind == "selected-extra": selected["unknown"] = True
                        elif kind == "selected-criterion": selected["criterion"] = "other"
                        elif kind == "selected-sources": selected["sources"] = [None]
                        elif kind == "selected-dependencies": selected["dependencies"] = None
                        elif kind == "selected-reference": selected["reference"] = "unrecorded"
                        elif kind == "retained-null": ob["receipts"][0]["evidence"] = None
                        elif kind == "missing-receipts": ob.pop("receipts")
                        elif kind == "null-receipts": ob["receipts"] = None
                        elif kind == "null-receipt": ob["receipts"] = [None]
                        elif kind == "duplicate-receipt": ob["receipts"].append(deepcopy(ob["receipts"][0]))
                        elif kind == "contradictory-receipt": ob["receipts"][-1]["evidence"]["candidate"] = "0" * 40
                        elif kind in ("receipt-negative", "receipt-zero", "receipt-boolean", "receipt-future"):
                            ob["receipts"][-1]["accepted_seq"] = {"receipt-negative":-1, "receipt-zero":0, "receipt-boolean":True, "receipt-future":cp["seq"]+1}[kind]
                        elif kind == "receipt-unaccepted": ob["receipts"][-1].pop("accepted_seq")
                        elif kind == "receipt-reuse": ob["receipts"][-1]["reuse"] = {"from":"unrecorded"}
                        elif kind == "retained-negative": ob["receipts"][0]["accepted_seq"] = -1
                        elif kind == "history-overflow": ob["receipts"] *= 9
                        elif kind == "withdrawal-coordinator": ob["withdrawal"] = {"by":"coordinator", "reason":"discard objective criterion"}
                        elif kind == "withdrawal-nobody": ob["withdrawal"] = {"by":"nobody", "reason":""}
                        elif kind == "withdrawal-null": ob["withdrawal"] = None
                        elif kind.startswith("withdrawal-no-"): ob["withdrawal"].pop(kind.removeprefix("withdrawal-no-"))
                        elif kind == "withdrawal-empty-instruction": ob["withdrawal"]["instruction"] = ""
                        elif kind == "withdrawal-extra": ob["withdrawal"]["extra"] = True
                        elif kind == "criteria-missing": cp["criteria"] = []
                        elif kind == "criteria-extra": cp["criteria"].append("uncovered")
                        elif kind == "criteria-null": cp["criteria"] = None
                        elif kind == "obligations-empty": cp["obligations"] = []
                        elif kind == "duplicate-id": cp["obligations"].append(deepcopy(ob))
                        elif kind == "introduced-negative": ob["introduced_seq"] = -1
                        elif kind == "definition-invalid": ob["definition"] = "0" * 64
                        elif kind == "boundary-missing": ob.pop("boundary")
                        elif kind == "source-null": ob["source"] = None
                        elif kind == "governance-null": cp["governance_sources"] = [None]
                        elif kind == "checkpoint-missing-candidate": cp.pop("candidate")
                        elif kind == "checkpoint-invalid-verification": cp["verification"] = None
                        elif kind == "checkpoint-unknown-field": cp["unknown"] = True
                        elif kind.startswith("accepted-review-null-"):
                            cp["obligations"][1]["receipts"][0]["evidence"][kind.removeprefix("accepted-review-null-")] = None
                        elif kind.startswith("consumed-report-"):
                            admission = next(iter(raw["admissions"].values())); report = admission["report"]
                            if kind.startswith("consumed-report-missing-"):
                                report["observation"].pop(kind.removeprefix("consumed-report-missing-"))
                            elif kind == "consumed-report-null": admission["report"] = None
                            elif kind == "consumed-report-digest": report["observation_digest"] = "0" * 64
                            elif kind == "consumed-report-attempt": report["attempt"] = "unobserved"
                            elif kind == "consumed-report-observation": report["observation"]["checks"] = ["changed observation"]
                            elif kind == "consumed-report-result": report["outcome"] = "failed"
                            elif kind == "consumed-report-runtime": admission["runtime"] = "unknown-runtime"
                        elif kind.startswith("worktree-"):
                            cp["worktree"] = {"worktree-list":[None], "worktree-string":"bad", "worktree-integer":1,
                                              "worktree-boolean":True, "worktree-null":None}[kind]
                        elif kind == "checkpoint-null-policy": cp["policy_revision"] = None
                        elif kind in ("retained-reuse-conflict", "retained-reuse-definition"):
                            ob["receipts"][0]["reuse"] = {"from":"unobserved" if kind.endswith("conflict") else retained["candidate"],
                                "to":case.candidate, "delta":[], "definition":"0" * 64 if kind.endswith("definition") else retained["definition"]}
                        elif kind == "governance-header-base": cp["governance"]["base"] = "0" * 40
                        elif kind == "governance-header-selection": cp["governance"]["selection"] = "unsupported-selection"
                        elif kind == "governance-header-exclusion": cp["governance"]["exclude"] = ["AGENTS.md"]
                        elif kind.startswith("policy-"):
                            policy = cp["obligations"][1]; source = policy["source"]
                            changes = {"policy-line-reversed":("lines", "4-3"), "policy-line-outside":("lines", "9999"),
                                "policy-quote":("quote_sha256", "0" * 64), "policy-revision":("revision", "sha256:" + "0" * 64),
                                "policy-base":("base", "0" * 40), "policy-undeclared":("path", ".pod/config.yaml")}
                            if kind in changes:
                                field, value = changes[kind]; source[field] = value
                            else:
                                source["gone"] = True
                                policy.update(state="withdrawn", withdrawal={"by":"project_policy", "reason":"policy supposedly gone"})
                                cp["closure"]["report"]["satisfied"] = ["O1"]
                                cp["closure"]["report"]["withdrawn"] = [{"obligation":"P", "provenance":"project_policy", **policy["withdrawal"]}]
                        elif kind == "governance-header-null": cp["governance"] = None
                        elif kind == "proposals-null": cp["proposals"] = None
                        elif kind == "proposal-null": cp["proposals"] = [None]
                        elif kind == "observations-null": cp["observations"] = None
                        elif kind == "observation-row-null": cp["observations"]["outstanding"] = [None]
                        elif kind == "reopened-null": cp["reopened"] = None
                        elif kind == "reopen-row-null": cp["reopened"] = [None]
                        elif kind == "quiescence-invalid": cp["quiescence"] = {"state":"unknown"}
                        elif kind == "slot-contradiction": cp["coordinator_slot"] = "O1"
                        elif kind == "assurance-findings-null": cp["obligations"][1]["findings"] = None
                        elif kind == "assurance-finding-null": cp["obligations"][1]["findings"] = [None]
                        elif kind.startswith("outer-unsupported"): raw["schema"] = "unsupported-future-context"
                        elif kind.startswith("outstanding-"):
                            admission = deepcopy(case.admission); admission.update(objective="closure-peer", serves=["O1"], state=kind.removeprefix("outstanding-"))
                            raw["admissions"][admission["admission_id"]] = admission
                        if kind.startswith("withdrawal-") and isinstance(ob["withdrawal"], dict):
                            cp["closure"]["report"]["withdrawn"] = [{"obligation":ob["id"], "provenance":ob["provenance"], **ob["withdrawal"]}]
                        if not kind.startswith("genuine-") and not released:
                            peer.write_text(json.dumps([] if kind == "outer-nonobject" else raw))
                        if phase == "fresh": case.lose()
                        before = case.snapshot(); own = deepcopy(case.state()); calls = len(case.mutations)
                        starts, workers = deepcopy(case.port.starts), deepcopy(case.port.workers)
                        verdict = case.status()["owner_handoff"]; text = case.status(text=True)
                        if kind.startswith("genuine-"):
                            self.assertEqual(verdict["status"], "handoff_available")
                            case.handoff(); self.assertEqual(case.entry()["state"], "done")
                            self.assertEqual(len(case.mutations), 1)
                            case.assert_only_handoff_changes(own, case.state(), completed=True)
                        else:
                            expected = "scope_unreadable" if kind in ("outer-nonobject", "outer-unsupported-foreign") else "run_shared"
                            self.assertEqual(verdict["condition"], expected); self.assertIn(expected, text)
                            for supplied in ("owner", "next"):
                                for boundary, call in case.boundary_calls(supplied).items():
                                    error = case.refused(call)
                                    self.assertEqual(error.detail["owner_handoff"]["condition"], expected)
                            case.refused(lambda:case.handoff())
                            self.assertEqual(len(case.mutations), calls)
                            if phase == "pending" and expected == "run_shared":
                                self.assertEqual(case.entry()["state"], "aborted")
                                case.assert_only_handoff_changes(own, case.state(), completed=False)
                            else: self.assertEqual(case.state(), own)
                        own_key = str((objective_root(case.project, "objective") / "context.json").relative_to(case.root / "state"))
                        self.assertEqual({key:value for key,value in case.snapshot().items() if key != own_key},
                                         {key:value for key,value in before.items() if key != own_key})
                        self.assertEqual(case.port.starts, starts); self.assertEqual(case.port.workers, workers)
                finally: case.doCleanups()

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

    def test_successful_nonlive_caller_shapes_keep_authority_codes_and_no_effects(self):
        self.establish(); self.lose(); original = self.native_read
        rows = [("connected", False), ("writable", False), ("orphaned", True),
                ("handle", "different"), ("terminal", None), ("terminal", {})]
        for field, value in rows:
            def read_port(argv, **kwargs):
                result = original(argv, **kwargs)
                if argv[:2] == ["terminal", "show"] and argv[3] == "next":
                    if field == "terminal": result["result"]["terminal"] = value
                    else: result["result"]["terminal"][field] = value
                return result
            with self.subTest(field=field, value=value), patch("pod.orca.read_command", side_effect=read_port):
                before = self.snapshot()
                from pod.orca import terminal_identity
                self.assertIsNone(terminal_identity("next")["code"])
                self.assertEqual(self.status()["owner_handoff"]["condition"], "caller_not_live")
                self.assertIn("did not prove liveness", self.status(text=True))
                for supplied in ("owner", "next"):
                    for boundary, call in self.boundary_calls(supplied).items():
                        with self.subTest(boundary=boundary, supplied=supplied):
                            error = self.refused(call)
                            expected = "coordinator_conflict" if boundary in ("checkpoint", "admission") and supplied != "owner" else "native_authority_unverified"
                            self.assertEqual(error.code, expected)
                            self.assertEqual(error.detail["owner_handoff"]["condition"], "caller_not_live")
                self.assertEqual(before, self.snapshot()); self.assertEqual(self.mutations, [])

    def test_absent_caller_stays_absent_at_every_boundary(self):
        self.establish(); self.lose(); os.environ.pop("ORCA_TERMINAL_HANDLE")
        before = self.snapshot()
        self.assertEqual(self.status()["owner_handoff"]["condition"], "caller_not_live")
        self.assertIn("not in a live Orca terminal", self.status(text=True))
        for call in self.boundary_calls("owner").values():
            error = self.refused(call, code="native_authority_unverified")
            self.assertEqual(error.detail["owner_handoff"]["condition"], "caller_not_live")
        self.assertEqual(before, self.snapshot()); self.assertEqual(self.mutations, [])

    def test_scope_barrier_accounts_for_record_shape_and_closure(self):
        # Corrupt/opaque inputs are explicitly adversarial controls, not older-writer evidence.
        cases = ("supported-open", "supported-closed", "corrupt-revision", "opaque-schema", "invalid-json", "foreign-run", "corrupt-closure", "unsupported-closed")
        for kind in cases:
            case = HandoffCase(); case.setUp()
            try:
                with self.subTest(kind=kind):
                    case.establish()
                    obligations = [case.criterion()]
                    extra = {}
                    if kind in ("supported-closed", "corrupt-closure", "unsupported-closed"):
                        obligations = [case.criterion("satisfied", evidence=[case.proof("O1")])]; extra["close"] = True
                    saved = deepcopy(case.current)
                    if kind == "foreign-run": case.current.update(id="foreign-run")
                    run("checkpoint", {"project":str(case.project), "objective":"scope-objective", "owner":"owner",
                        "value":case.core(obligations=obligations, governance={"base_ref":"refs/remotes/origin/target"}, **extra)})
                    case.current = saved
                    other = objective_root(case.project, "scope-objective") / "context.json"
                    if kind in ("corrupt-revision", "opaque-schema", "corrupt-closure", "unsupported-closed"):
                        raw = json.loads(other.read_text())
                        if kind == "corrupt-revision": raw["revision"] = "corrupted"
                        elif kind == "corrupt-closure": raw["checkpoint"]["closure"]["report"]["status"] = "unverified"
                        else: raw["schema"] = "unknown-future-context"
                        other.write_text(json.dumps(raw))
                    elif kind == "invalid-json": other.write_text("truncated{")
                    case.lose(); before = case.snapshot(); detected = case.status()["owner_handoff"]
                    if kind in ("supported-closed", "foreign-run"):
                        self.assertEqual(detected["status"], "handoff_available")
                        case.handoff()
                    else:
                        expected = "scope_unreadable" if kind == "invalid-json" else "run_shared"
                        self.assertEqual(detected["condition"], expected)
                        text = case.status(text=True)
                        self.assertIn(expected, text)
                        if expected == "run_shared": self.assertIn("scope-objective", text)
                        case.refused(lambda:case.handoff())
                        self.assertEqual(before, case.snapshot()); self.assertEqual(case.mutations, [])
                    self.assertEqual(other.read_bytes(), before[str(other.relative_to(case.root / "state"))])
            finally: case.doCleanups()

    def test_scope_frontier_and_closure_invariant_for_fresh_and_pending(self):
        # Scope must be completely readable and closure validated/noncontradictory.
        cases = ("directory-denied", "root-denied", "empty-directory-denied", "active-obligation",
                 "missing-obligations", "empty-report", "stale-sequence", "stale-revision",
                 "boolean-sequence", "genuine-closed", "genuine-foreign")
        for phase in ("fresh", "pending"):
            for kind in cases:
                case = HandoffCase(); case.setUp()
                changed_permission = None
                try:
                    with self.subTest(phase=phase, kind=kind):
                        case.establish()
                        if phase == "pending":
                            case.lose(); case.mode = "lost"; case.refused(lambda:case.handoff()); case.mode = "success"
                        closed = kind not in ("directory-denied", "root-denied", "genuine-foreign")
                        rows = [case.criterion("satisfied", evidence=[case.proof("O1")])] if closed else [case.criterion()]
                        current = deepcopy(case.current)
                        if kind == "genuine-foreign": case.current.update(id="foreign-run")
                        run("checkpoint", {"project":str(case.project), "objective":"frontier-peer",
                            "owner":os.environ["ORCA_TERMINAL_HANDLE"],
                            "value":case.core(obligations=rows, governance={"base_ref":"refs/remotes/origin/target"}, **({"close":True} if closed else {}))})
                        case.current = current
                        other = objective_root(case.project, "frontier-peer") / "context.json"
                        raw = json.loads(other.read_text())
                        if kind == "active-obligation":
                            ob = raw["checkpoint"]["obligations"][0]; ob.update(state="active", executor="coordinator")
                            ob.pop("evidence", None); ob.pop("receipts", None)
                        elif kind == "missing-obligations": raw["checkpoint"].pop("obligations")
                        elif kind == "empty-report": raw["checkpoint"]["closure"]["report"] = {"status":"closed"}
                        elif kind == "stale-sequence": raw["checkpoint"]["closure"]["seq"] -= 1
                        elif kind == "stale-revision": raw["checkpoint"]["closure"]["revision"] -= 1
                        elif kind == "boolean-sequence": raw["checkpoint"]["closure"]["seq"] = True
                        if kind in ("active-obligation", "missing-obligations", "empty-report", "stale-sequence", "stale-revision", "boolean-sequence"):
                            # These alone are adversarial corruption controls, never genuine writer proof.
                            other.write_text(json.dumps(raw))
                        peer_bytes = other.read_bytes()
                        if phase == "fresh": case.lose()
                        own_before = deepcopy(case.state()); calls = len(case.mutations)
                        prepared_calls = case.boundary_calls("owner") if kind == "root-denied" else None
                        prepared_decision = case.decision() if kind == "root-denied" else None
                        if kind in ("directory-denied", "root-denied", "empty-directory-denied"):
                            directory = case.root / "state/pod" if kind == "root-denied" else other.parent
                            if kind == "empty-directory-denied":
                                directory = case.root / "state/pod/unreadable-frontier"; directory.mkdir()
                            changed_permission = (directory, directory.stat().st_mode & 0o777)
                            directory.chmod(0)
                        if kind == "root-denied":
                            observed = case.status()
                            self.assertEqual(observed["status"], "blocked")
                            for call in prepared_calls.values(): case.refused(call)
                            # The denied root also makes the objective lock unavailable;
                            # either boundary refusal must precede any native mutation.
                            with self.assertRaises((PodError, OSError)):
                                case.handoff(prepared_decision)
                            directory, mode = changed_permission; directory.chmod(mode); changed_permission = None
                            self.assertEqual(case.state(), own_before)
                            self.assertEqual(len(case.mutations), calls)
                            self.assertEqual(other.read_bytes(), peer_bytes)
                            continue
                        detected = case.status()["owner_handoff"]
                        if kind in ("genuine-closed", "genuine-foreign"):
                            self.assertEqual(detected["status"], "handoff_available")
                            case.handoff(); self.assertEqual(case.entry()["state"], "done")
                            self.assertEqual(len(case.mutations), 1)
                        else:
                            self.assertEqual(detected["status"], "handoff_unavailable")
                            self.assertEqual(detected["condition"], "scope_unreadable" if "denied" in kind else "run_shared")
                            for supplied in ("owner", "next"):
                                for boundary, call in case.boundary_calls(supplied).items():
                                    error = case.refused(call)
                                    self.assertEqual(error.detail["owner_handoff"]["condition"], detected["condition"])
                            case.refused(lambda:case.handoff())
                            if phase == "pending" and "denied" not in kind:
                                self.assertEqual(case.entry()["state"], "aborted")
                                case.assert_only_handoff_changes(own_before, case.state(), completed=False)
                            else:
                                self.assertEqual(case.state(), own_before)
                            self.assertEqual(len(case.mutations), calls)
                        if changed_permission:
                            directory, mode = changed_permission; directory.chmod(mode); changed_permission = None
                        self.assertEqual(other.read_bytes(), peer_bytes)
                finally:
                    if changed_permission: changed_permission[0].chmod(changed_permission[1])
                    case.doCleanups()

    def test_text_status_names_binding_cause_and_available_ambiguity(self):
        self.establish(); self.lose(); self.current = None
        self.assertIn("no stable current Run binding", self.status(text=True))
        self.current = {"id":"run", "coordinator_handle":"owner", "consumer_generation":1}
        with patch.object(OrcaPort, "show_worker", side_effect=PodError("orca_unavailable", "read unavailable")):
            text = self.status(text=True)
            self.assertIn("Ambiguity:", text)
            self.assertIn("worker read unavailable", text)
            self.assertIn(self.admission["admission_id"], text)



class TransitionTableTests(HandoffCase):
    def test_consumed_native_observation_survives_handoff_without_rewrite(self):
        self.establish(); self.settle(self.admission)
        rows = self.stored()
        for row in rows:
            row.pop("executor", None); row.pop("wait", None)
            if row["id"] == "S1": row.update(state="active", executor="coordinator")
            elif row["id"] == "O1": row.update(state="waiting", wait={"class":"dependency", "referent":"S1"})
            else: row.update(state="blocked_external", external={"party":"user", "need":"input", "unblocks_when":"input received"})
        self.report(self.admission, self.frozen, result_commit=self.candidate, map={"obligations":rows})
        before = self.state(); observation = deepcopy(before["admissions"][self.admission["admission_id"]]["report"])
        self.lose(); self.handoff()
        after = self.state()
        self.assertEqual(after["admissions"][self.admission["admission_id"]]["report"], observation)
        self.assert_only_handoff_changes(before, after, completed=True)
        self.assertEqual(len(self.mutations), 1)

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

    def test_pending_unreadable_scope_and_nonlive_success_do_not_abort(self):
        self.establish(); self.lose(); self.mode = "lost"
        self.refused(lambda:self.handoff()); self.mode = "success"
        unknown = objective_root(self.project, "unknown-record") / "context.json"
        unknown.parent.mkdir(parents=True); unknown.write_text("incomplete{")
        before = self.snapshot()
        self.assertEqual(self.status()["owner_handoff"]["condition"], "scope_unreadable")
        self.refused(lambda:self.handoff())
        self.assertEqual(self.entry()["state"], "pending")
        self.assertEqual(self.snapshot(), before); self.assertEqual(len(self.mutations), 1)
        original = self.native_read
        def nonlive(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[:2] == ["terminal", "show"] and argv[3] == "next":
                result["result"]["terminal"]["connected"] = False
            return result
        with patch("pod.orca.read_command", side_effect=nonlive):
            self.refused(lambda:self.handoff())
        self.assertEqual(self.entry()["state"], "pending")
        self.assertEqual(self.snapshot(), before); self.assertEqual(len(self.mutations), 1)

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

    def test_genuine_050_shared_objective_blocks_without_conversion(self):
        self.establish()
        predecessor = "9c8639a5d7406b708b67b96a5ab26677efae1581"
        root = Path(__file__).resolve().parents[1]
        old = self.root / "writer-050"; old.mkdir(); archive = self.root / "writer-050.tar"
        with archive.open("wb") as stream:
            subprocess.run(["git", "-C", str(root), "archive", predecessor, "skills/pod", "VERSION"], stdout=stream, check=True)
        with tarfile.open(archive) as bundle: bundle.extractall(old, filter="data")
        original_source = subprocess.check_output(["git", "-C", str(root), "show", predecessor+":skills/pod/ledger.py"])
        self.assertEqual((old / "skills/pod/ledger.py").read_bytes(), original_source)
        script = """import json
from pathlib import Path
import pod
from pod.internal import run
from pod.ledger import objective_root
from pod import orca
assert pod.__version__ == '0.5.0'
orca.contract=lambda:{'status':'observed','runtime':'runtime','capabilities':{}}
value={'schema':'pod-checkpoint/v1','criteria':['shared work'],'plan_revision':'plan',
'candidate':'candidate','policy_revision':'r','native_refs':[{'runId':'run','runtime':'runtime'}],
'assignments':[],'questions':[],'verification_gaps':['shared work'],'next_safe_action':'continue shared work'}
run('checkpoint',{'project':str(Path.cwd()),'objective':'genuine-050-shared','owner':'owner','value':value})
print(json.dumps({'path':str(objective_root(Path.cwd(),'genuine-050-shared')/'context.json')}))
"""
        process = subprocess.run([os.sys.executable, "-c", script], cwd=self.project,
            env={**os.environ, "PYTHONPATH":str(old / "skills")}, capture_output=True, text=True, check=True)
        other = Path(json.loads(process.stdout)["path"]); original = other.read_bytes()
        self.assertNotEqual(json.loads(original)["schema"], self.state()["schema"])
        self.lose(); before = self.snapshot()
        self.assertEqual(self.status()["owner_handoff"]["condition"], "run_shared")
        self.assertIn("genuine-050-shared", self.status(text=True))
        self.refused(lambda:self.handoff())
        self.assertEqual(self.snapshot(), before); self.assertEqual(other.read_bytes(), original)
        self.assertEqual(self.mutations, [])

    def test_unseen_run_is_an_explicit_synthetic_defensive_control(self):
        # Ordinary supported writers cannot emit this multi-Run state. This sole
        # Owner-approved proof exception is defensive, never genuine-writer/live evidence.
        self.establish()
        path = objective_root(self.project, "objective") / "context.json"
        restored = json.loads(path.read_text())
        restored["checkpoint"]["native_refs"].append({"runId":"unseen", "runtime":"runtime"})
        path.write_text(json.dumps(restored))
        self.lose(); before = self.snapshot(); detected = self.status()["owner_handoff"]
        self.assertEqual(detected["status"], "handoff_available")
        self.assertIn("run unseen: not shown by run-current", detected["facts"]["ambiguity"])
        decision = self.decision()
        self.refused(lambda:self.handoff({**decision, "ambiguity":[]}), code="handoff_decision_mismatch")
        self.assertEqual(before, self.snapshot()); self.assertEqual(self.mutations, [])
        self.handoff(decision)
        self.assertEqual(self.entry()["state"], "done"); self.assertEqual(len(self.mutations), 1)

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
