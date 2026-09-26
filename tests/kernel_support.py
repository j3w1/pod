"""Shared deterministic kernel fixtures, independent of test modules."""

from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch

from pod.config import effective, write_defaults
from pod.errors import PodError
from pod.github import repository_context
from pod.internal import run as internal_run
from pod.ledger import checkpoint, objective_root, read, state_root
from pod.operations import OrcaPort, guarded_start, _assignment_evidence
from pod.records import packet
from tests.common import VERIFICATION, fake_authority, fixture, proof


NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)
COMMIT, TREE = "a" * 40, "c" * 40
COMMIT2, TREE2 = "b" * 40, "d" * 40
COMMIT3, TREE3 = "e" * 40, "f" * 40
BASE = "1" * 40
WORKFLOW = "2" * 64
BRANCH = {"remote": "origin", "branch": "agent/release", "base": "main"}
TARGET = "origin/agent/release"
# ci.yml is what a push triggers under PROJECT_CONFIG; release.yml is only ever dispatched.
RELEASE = "release.yml"
PROJECT_CONFIG = """schema: pod/v1
waste_governor:
  preflight: [unit, workflow-lint]
  triggers:
    push: ["workflow:ci.yml"]
    pr_update: ["workflow:ci.yml"]
"""


def authorization(candidate=COMMIT, tree=TREE, scope=("merge", "release"), target="origin/main"):
    # Merge consent names the target it was given for; BRANCH merges into origin/main.
    return {"schema": "pod-authorization/v1", "candidate": candidate, "tree": tree,
            "scope": list(scope), "authorized_by": "owner", "utc": "2026-09-21T00:00:00Z",
            "reference": "tasks/pod/authorization.md", **({"target": target} if "merge" in scope else {})}


def action(kind="push", candidate=COMMIT, target=TARGET, unit="release", **extra):
    return {"kind": kind, "candidate": candidate, "target": target, "unit": unit,
            "reason": "converged candidate", **extra}


def dispatch(candidate=COMMIT, target="ci.yml", **extra):
    return action(kind="workflow_dispatch", candidate=candidate, target=target, **extra)


def diagnostic(candidate=COMMIT, check="permission probe", **extra):
    return action(kind="remote_diagnostic", candidate=candidate, target="probe.yml",
                  diagnostic={"question": "can the workflow actor read the ruleset field?",
                              "local_limitation": "local credentials do not represent that actor",
                              "check": check, "stopping_condition": "record the result; no unchanged repeat"},
                  **extra)


def body(candidate=COMMIT, gaps=()):
    return {"schema": "pod-checkpoint/v3", "criteria": ["works"], "plan_revision": "p",
            "candidate": candidate, "policy_revision": "r", "native_refs": [], "assignments": [],
            "questions": [], "verification_gaps": list(gaps), "next_safe_action": "inspect"}


def observation(commit=COMMIT, tree=TREE, *, dirty=0, workflows=None, verification=("unit",),
                policy="r", base=BASE, environment=None):
    return {"schema": "pod-candidate-observation/v1", "commit": commit, "tree": tree, "dirty_paths": dirty,
            "base": {"ref": "origin/main", "commit": base, "merge_base": base} if base else None,
            "workflows": {".github/workflows/ci.yml": WORKFLOW} if workflows is None else workflows,
            "verification": list(verification), "toolchain": {"python": "3.13.0"},
            "environment": environment or {}, "policy_revision": policy}


def correction(key, criterion="works"):
    return {"criterion_id": criterion, "failure_id": key, "obligation": "make the release boundary pass",
            "failing_example": "release-boundary test fails on the runner", "hypothesis": "an off-by-one",
            "last_meaningful_evidence": "the failing assertion",
            "next_discriminating_check": "run the single test", "correction_key": key}


class GovernorFakePort:
    """A remote that records what it was asked and can lose a response after acting."""

    def __init__(self, *, lost=(), rejected=(), delay=0.0, auto_ci=(), broken=(), blind=False):
        self.calls = []
        self.auto_ci = tuple(auto_ci)
        self.broken = set(broken)
        # A blind port starts runs but its readback sees none of them yet.
        self.blind = blind
        self.heads = {}
        self.prs = {}
        self.runs_by = {}
        self.run_status = {}
        self.next_run = 100
        self.lost = set(lost)
        self.rejected = set(rejected)
        self.delay = delay

    def branch_head(self, *, remote, branch):
        return self.heads.get(f"{remote}/{branch}")

    def push(self, *, remote, branch, commit, expected):
        self.calls.append(("push", remote, branch, commit, expected))
        key = f"{remote}/{branch}"
        if "push" in self.rejected or (expected is not None and self.heads.get(key) not in (expected, None)):
            return {"status": "rejected", "remote_head": self.heads.get(key)}
        self.heads[key] = commit
        for workflow in self.auto_ci:
            self._start(workflow, commit, event="push")
        if "push" in self.lost:
            raise PodError("remote_effect_uncertain", "lost")
        return {"status": "pushed", "remote_head": commit}

    def _start(self, workflow, commit, *, event):
        run = {"id": str(self.next_run), "status": "in_progress", "conclusion": None,
               "created_at": "2026-09-21T00:00:01+00:00", "url": "https://example.invalid/run",
               "event": event}
        self.next_run += 1
        self.runs_by.setdefault((workflow, commit), []).append(run)
        self.run_status[run["id"]] = run
        return run

    def pull_request(self, *, head, base):
        self.calls.append(("pr_list", head, base))
        if "pull_request" in self.broken:
            return "not a mapping"
        return self.prs.get((head, base))

    def open_pull_request(self, *, head, base, title, body):
        self.calls.append(("pr_create", head, base, title))
        record = {"number": 7, "url": "https://example.invalid/pull/7", "head_sha": None, "draft": False}
        self.prs[(head, base)] = record
        return record

    def dispatch(self, *, workflow, ref, inputs):
        self.calls.append(("dispatch", workflow, ref, dict(inputs)))
        if self.delay:
            time.sleep(self.delay)
        self._start(workflow, self.heads.get(f"origin/{ref}"), event="workflow_dispatch")
        if "dispatch" in self.lost:
            raise PodError("remote_effect_uncertain", "lost")
        return {"status": "dispatched"}

    def runs(self, *, workflow, commit):
        self.calls.append(("runs", workflow, commit))
        if self.blind:
            return []
        return [dict(run) for run in self.runs_by.get((workflow, commit), [])]

    def run(self, *, run_id):
        return dict(self.run_status[run_id])

    def rerun(self, *, run_id, failed_only):
        self.calls.append(("rerun", run_id, failed_only))
        self.run_status[run_id].update({"status": "in_progress", "conclusion": None})
        return {"status": "rerun_requested"}

    def cancel(self, *, run_id):
        self.calls.append(("cancel", run_id))
        if "cancel" in self.broken:
            raise PodError("gh_unavailable", "gh is not on PATH")
        self.run_status[run_id].update({"status": "completed", "conclusion": "cancelled"})
        return {"status": "cancel_requested"}

    def complete(self, run_id, conclusion):
        self.run_status[run_id].update({"status": "completed", "conclusion": conclusion})




REQUEST='11111111-1111-4111-8111-111111111111'
OTHER='22222222-2222-4222-8222-222222222222'
ROUTE={'agent':'codex','model':'gpt-6-sol','effort':'medium','context':'native_default',
       'reason':'bounded implementation with tests'}


class FakePort:
    def __init__(self):
        self.runtime='runtime'; self.starts=[]; self.workers={}; self.receipt=None
        self.state='completed'; self.request_id=REQUEST; self.request_receipt=None
        self.find_rows=None; self.show_failure=None; self.effective=None
        self.before_native=None; self.placement=None; self.capable=True
        self.headless=False

    def capability(self):
        if not self.capable:
            raise PodError('launch_preferences_unavailable','no launch preference support')
        return {'status':'observed','runtime':self.runtime,
                'capabilities':{'launch_preferences_v1':True}}

    def resolve_worktree(self, selector):
        if self.placement is None:
            raise PodError('worktree_resolution_unavailable','no placement fixture')
        return dict(self.placement)

    def read_native(self, owner, *, authority_runs=(), assignments=()):
        if self.before_native:
            callback=self.before_native; self.before_native=None; callback()
        evidence=[]
        for row in assignments:
            binding=row['native_binding']
            if binding['dispatchId'] in self.workers:
                evidence.append(_assignment_evidence(self.show_worker(binding['dispatchId']),row))
        return {'runtime':self.runtime,'owner':owner if authority_runs else None,
                'authoritative':bool(authority_runs),'scope':'objective_assignments',
                'complete':True,'assignments':evidence,'physical_capacity':'unavailable'}

    def start_worker(self, *, run, task, owner, route, worktree='current', retry_request=None, terminal=None):
        self.starts.append({'run':run,'task':task,'route':route.copy(),'worktree':worktree,
                            'retry_request':retry_request,'terminal':terminal})
        if self.receipt is not None:
            return dict(self.receipt)
        dispatch='dispatch-'+str(len(self.workers)+1)
        self.workers[dispatch]={'run':run,'task':task,'route':route.copy(),'worktree':worktree,
                                'state':'ready','outcome':'in_progress',
                                'terminal':None if self.headless else terminal or 'term-'+dispatch}
        return {'runtime':self.runtime,'request_uuid':retry_request or REQUEST,
                'runId':run,'taskId':task,'dispatchId':dispatch,'state':'ready',
                'launch':{'requested':self._launch(route),'effective':self._launch(route)}}

    @staticmethod
    def _launch(route):
        data={key:route[key] for key in ('agent','model')}
        if route['effort']!='native_default': data['effort']=route['effort']
        return data

    def show_worker(self, dispatch):
        if self.show_failure: raise self.show_failure
        item=self.workers[dispatch]
        effective=self.effective if self.effective is not None else self._launch(item['route'])
        status=('completed' if item['outcome']=='succeeded' else
                'failed' if item['outcome'] in ('failed','stopped') else 'running')
        return {'runtime':self.runtime,'result':{
            'dispatch':{'id':dispatch,'runId':item['run'],'taskId':item['task'],'status':status},
            'projection':{'id':'worker-'+dispatch,'dispatchId':dispatch,'runId':item['run'],
                          'taskId':item['task'],'outcome':item['outcome'],
                          'stage':{'dispatch':status,
                                   'detail':'settled' if item['outcome']!='in_progress' else 'input_accepted'}},
            'worker':{'dispatchId':dispatch,'worktreeId':item['worktree'],'state':item['state'],
                      'agentTerminalHandle':item['terminal'],
                      'startOptions':{'launch':{'requested':self._launch(item['route']),
                                                'effective':effective}}}}}

    def request_show(self, request_uuid):
        receipt=self.request_receipt
        if receipt is None and self.workers:
            dispatch,item=next(iter(self.workers.items()))
            receipt={'runId':item['run'],'taskId':item['task'],'dispatchId':dispatch,
                     'launch':{'requested':self._launch(item['route']),
                               'effective':self._launch(item['route'])}}
        result={'requestId':self.request_id,'state':self.state,'receipt':receipt}
        if self.state!='absent': result['method']='orchestration.workerStart'
        return {'runtime':self.runtime,'result':result}

    def find_worker(self, *, run, task):
        if self.find_rows is not None: return list(self.find_rows)
        return [{'dispatchId':dispatch} for dispatch,item in self.workers.items()
                if item['run']==run and item['task']==task]




AGENTS = ("# Project rules\n\nEvery change to src/ receives a fresh candidate-bound review.\n"
          "Reviews bind to a frozen candidate.\nDocs are optional.\n")


def git(project: Path, *argv: str) -> str:
    return subprocess.run(["git", "-C", str(project), *argv], capture_output=True, text=True,
                          check=True).stdout.strip()


class KernelCase(unittest.TestCase):
    def setUp(self):
        self.temp = fixture(); self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
                                           "XDG_CONFIG_HOME": str(self.root / "config")})
        self.env.__enter__(); self.addCleanup(self.env.__exit__, None, None, None)
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.project)], check=True)
        git(self.project, "config", "user.name", "Fixture")
        git(self.project, "config", "user.email", "fixture@example.invalid")
        (self.project / "AGENTS.md").write_text(AGENTS)
        (self.project / "src").mkdir()
        (self.project / "src" / "old.py").write_text("old\n")
        (self.project / "README.md").write_text("readme\n")
        git(self.project, "add", ".")
        git(self.project, "commit", "-q", "-m", "base")
        git(self.project, "branch", "target")
        self.base = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", self.base)
        git(self.project, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/target")
        write_defaults(self.root / "config" / "pod" / "config.yaml")
        self.port = FakePort()
        context = repository_context(self.project)
        self.port.placement = {"repository": None, "repo_key": context["repo_key"],
                               "path": context["worktree"], "branch": "main", "runtime": "runtime"}
        authority = patch("pod.ledger.require_authority",
                          side_effect=lambda *args, **kwargs: fake_authority(self.port)(*args, **kwargs))
        authority.start(); self.addCleanup(authority.stop)
        self.candidate = self.base

    def core(self, **extra) -> dict:
        return {"schema": "pod-checkpoint/v3", "criteria": ["PoD#1"], "plan_revision": "plan",
                "candidate": self.candidate, "policy_revision": effective(self.project)["revision"],
                "native_refs": [], "assignments": [], "questions": [], "verification_gaps": [],
                "next_safe_action": "continue", "verification": dict(VERIFICATION), **extra}

    def proof(self, obligation, **extra):
        state = read(self.project, "objective")
        definition = (next(row for row in self.stored() if row["id"] == obligation)
                      if state else self.criterion())
        return proof(obligation, self.candidate, definition=definition,
                     policy_revision=effective(self.project)["revision"], **extra)

    def write(self, obligations, objective="objective", **fields) -> dict:
        return checkpoint(self.project, objective, owner="owner",
                          value=self.core(obligations=obligations, **fields),
                          native={"runtime": "runtime", "delegation": "available"})

    def intake(self, *rows, **fields) -> dict:
        return self.write([self.criterion(), *rows], governance={"base_ref": "target"}, **fields)

    @staticmethod
    def criterion(state="active", **fields):
        row = {"id": "O1", "kind": "criterion", "provenance": "objective", "source": {"ref": "PoD#1"},
               "check": "PoD#1 passes", "state": state}
        if state == "active":
            row["executor"] = "coordinator"
        row.update(fields)
        return row

    @staticmethod
    def sub(key, state="waiting", **fields):
        row = {"id": key, "kind": "subgoal", "provenance": "coordinator", "parent": "O1",
               "check": f"{key} is delivered", "state": state}
        if state == "waiting":
            row["wait"] = {"class": "sequenced", "referent": "O1"}
        row.update(fields)
        return row

    def stored(self, objective="objective"):
        return [dict(row) for row in read(self.project, objective)["checkpoint"]["obligations"]]

    def packet(self, serves, *, role="implement", boundary=None, revision=None, **extra):
        from pod.config import load
        snapshot = load(self.project)
        state = read(self.project, "objective")
        return packet({"schema": "pod-packet/v3", "objective": "objective", "criteria": ["PoD#1"],
                       "responsibility": "writer", "scope": ["src"], "actions": ["edit"],
                       "candidate": self.candidate, "context": [], "dependencies": [],
                       "route": {**ROUTE, "preference_revision": snapshot["revision"]},
                       "policy_revision": snapshot["policy_revision"], "plan_revision": "plan",
                       "report_contract": "checks", "sources": [], "serves": serves, "role": role,
                       "boundary": boundary or {"paths": [], "surfaces": []},
                       "map_revision": revision or state["checkpoint"]["revision"], **extra})

    def start(self, task, frozen, accompanying=None):
        return guarded_start(self.project, "objective", owner="owner", run="run", task=task,
                             plan_revision="plan", frozen_packet=frozen, worktree="current",
                             port=self.port, accompanying=accompanying)

    def refused(self, code, detail, call, *args, **kwargs):
        with self.assertRaises(PodError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        if detail is not None:
            self.assertEqual(caught.exception.detail["detail"], detail, str(caught.exception))
        return caught.exception

    def settle(self, admission):
        self.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = "succeeded"

    def report(self, admission, frozen, *, outcome="succeeded", scope=("src",), **extra):
        body = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                "attempt": admission["native_binding"]["dispatchId"], "candidate": self.candidate,
                "outcome": outcome, "scope": list(scope), "files": ["src/new.py"], "checks": ["unit"],
                "failures": [] if outcome == "succeeded" else ["review not completed"], "evidence": [],
                "uncertainty": [], "questions": []}
        with patch.object(OrcaPort, "show_worker", autospec=True,
                          side_effect=lambda _port, dispatch: self.port.show_worker(dispatch)):
            return internal_run("report", {"project": str(self.project), "objective": "objective",
                                           "admission_id": admission["admission_id"], "packet": frozen,
                                           "report": body, **extra})




def assurance_review(self, task="review", **packet_fields):
    frozen = self.packet(["A"], role="review", **packet_fields)
    admission = self.start(task, frozen)["admission"]
    self.settle(admission)
    rows = self.stored()
    a = next(row for row in rows if row["id"] == "A")
    a.pop("executor")
    a.update(state="satisfied", evidence=[{"attempt": admission["admission_id"]}])
    self.report(admission, frozen, map={"obligations": rows})
    return admission

def assurance_satisfied(self, **packet_fields):
    self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                 "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                 "candidate": self.base, "existing_evidence": "tests", "insufficiency": "no review",
                 "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}})
    return self.review(**packet_fields)

def assurance_label(self):
    with patch.object(OrcaPort, "read_native", autospec=True,
                      side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
        return internal_run("acceptance", {
            "project": str(self.project), "objective": "objective", "criteria": ["PoD#1"],
            "evidence_rows": [], "candidate": self.candidate, "policy_revision": "p", "sources": [],
            "dependencies": [], "environment": "fixture", "review_required": False,
            "hosted_required": False})["independently_reviewed"]

def assurance_waiting(self):
    rows = deepcopy(self.stored())
    a = next(row for row in rows if row["id"] == "A")
    a.pop("evidence", None); a.pop("reuse", None)
    a.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
    return rows



def governance_policy(self, key="PA", lines="3", **fields):
    row = {"id": key, "kind": "assurance", "provenance": "project_policy",
           "source": {"path": "AGENTS.md", "lines": lines}, "check": "fresh review",
           "scope": {"paths": ["src"]}, "question": "is src correct?", "candidate": self.base,
           "existing_evidence": "unit tests", "insufficiency": "no independent review",
           "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
    row.update(fields)
    return row


USER = {"provenance": "user_direct", "instruction": "Select this target for this objective"}


def governance_commit(self, path, text, message):
    (self.project / path).write_text(text)
    git(self.project, "add", path)
    git(self.project, "commit", "-qm", message)
    return git(self.project, "rev-parse", "HEAD")

def governance_guarded_refresh(self, rows=None, **fields):
    before = read(self.project, "objective")
    self.refused("governance_unavailable", "governance_unavailable", self.write,
                 self.stored() if rows is None else rows, governance_refresh=True, **fields)
    self.assertEqual(read(self.project, "objective"), before)
