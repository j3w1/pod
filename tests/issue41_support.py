"""Production-entry fixtures: disposable Git and only Orca/GitHub ports replaced."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from pod.config import write_defaults
from pod.errors import PodError
from pod.github import repository_context
from pod.internal import run
from pod.operations import OrcaPort
from tests.common import fixture
from tests.kernel_support import KernelCase, FakePort, GovernorFakePort, git


class ProductionCase(KernelCase):
    def setUp(self):
        self.temp = fixture(); self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"), "ORCA_TERMINAL_HANDLE": "owner",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"})
        self.env.start(); self.addCleanup(self.env.stop)
        self.project = self.root / "project"; self.project.mkdir()
        git(self.project, "init", "-q", "-b", "main")
        git(self.project, "config", "user.name", "Fixture")
        git(self.project, "config", "user.email", "fixture@example.invalid")
        (self.project / "src").mkdir()
        (self.project / "src/old.py").write_text("old\n")
        (self.project / "README.md").write_text("readme\n")
        git(self.project, "add", "."); git(self.project, "commit", "-qm", "base")
        self.base = self.candidate = git(self.project, "rev-parse", "HEAD")
        git(self.project, "branch", "target")
        git(self.project, "remote", "add", "origin", "https://github.com/acme/example.git")
        git(self.project, "update-ref", "refs/remotes/origin/target", self.base)
        git(self.project, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/target")
        write_defaults(self.root / "config/pod/config.yaml")
        self.port = FakePort(); self.remote = GovernorFakePort()
        context = repository_context(self.project)
        self.port.placement = {"repository": context["repository"], "repo_key": context["repo_key"],
                               "path": context["worktree"], "branch": "main", "runtime": "runtime"}
        self.current = {"id": "run", "coordinator_handle": "owner", "consumer_generation": 1}
        self.native_error = None
        for method in ("capability", "resolve_worktree", "show_worker", "start_worker", "request_show", "find_worker"):
            replacement = patch.object(OrcaPort, method, side_effect=getattr(self.port, method))
            replacement.start(); self.addCleanup(replacement.stop)
        native = patch("pod.orca.read_command", side_effect=self.native_read)
        native.start(); self.addCleanup(native.stop)
        capability = patch("pod.orca.contract", side_effect=self.port.capability)
        capability.start(); self.addCleanup(capability.stop)
        remote = patch("pod.governor_effects.GhPort", return_value=self.remote)
        remote.start(); self.addCleanup(remote.stop)

    def native_read(self, argv, **kwargs):
        if self.native_error:
            raise self.native_error
        if argv == ["--version"]:
            return {"version": "synthetic-control", "executable": "/synthetic/orca"}
        if argv == ["status", "--json"]:
            return {"runtime": self.port.runtime, "result": {"runtime": {"capabilities": [], "state": "running"}}}
        if argv[:2] == ["orchestration", "run-current"]:
            return {"runtime": self.port.runtime, "result": {"run": deepcopy(self.current)}}
        if argv[:2] == ["orchestration", "worker-list"]:
            return {"runtime": self.port.runtime, "result": {"workers": [
                {**self.port.show_worker(key)["result"]["worker"],
                 "projection": self.port.show_worker(key)["result"]["projection"]}
                for key in self.port.workers], "scope": {"run": "run", "source": "flag"}, "page": {"hasMore": False}}}
        raise AssertionError(f"unexpected Orca port read: {argv}")

    def op(self, operation, **fields):
        owned = {} if operation in ("report", "map") else {"owner": "owner"}
        return run(operation, {"project": str(self.project), "objective": "objective", **owned, **fields})

    def write(self, obligations=None, objective="objective", **fields):
        value = self.core(**fields)
        if obligations is not None:
            value["obligations"] = obligations
        return self.op("checkpoint", value=value)

    def start(self, task, frozen, accompanying=None, **fields):
        return self.op("admission", run="run", task=task, plan_revision="plan", packet=frozen,
                       **({"map": accompanying} if accompanying is not None else {}), **fields)

    def report(self, admission, frozen, *, outcome="succeeded", **fields):
        body = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                "attempt": admission["native_binding"]["dispatchId"], "candidate": frozen["body"]["candidate"],
                "outcome": outcome, "scope": frozen["body"]["scope"], "files": [], "checks": ["unit"],
                "failures": [] if outcome == "succeeded" else ["incomplete"], "evidence": [], "uncertainty": [], "questions": []}
        return self.op("report", admission_id=admission["admission_id"], packet=frozen, report=body, **fields)

    def move(self, path="README.md"):
        (self.project / path).write_text((self.project / path).read_text() + "changed\n")
        git(self.project, "add", path); git(self.project, "commit", "-qm", "change")
        self.candidate = git(self.project, "rev-parse", "HEAD")
        return self.candidate

    def prepare(self):
        self.write([self.criterion()], governance={"base_ref":"refs/remotes/origin/target"})
        prepared = self.op("governor-prepare", unit="default",
            branch={"remote": "origin", "branch": "delivery", "base": "target"})
        candidate = prepared["candidate"]
        self.authorization = {
            "schema": "pod-authorization/v1", "candidate": candidate["commit"], "tree": candidate["tree"],
            "scope": ["publish"], "authorized_by": "owner", "utc": "2026-01-01T00:00:00Z", "reference": "fixture-authorization"}
        return candidate

    def action(self, kind="push", **fields):
        target = "ci.yml" if kind in ("workflow_dispatch", "validation_rerun", "remote_diagnostic") else "origin/delivery"
        base = {"kind": kind, "candidate": self.candidate, "target": target, "reason": "publish candidate", "effects": [], "authorization": self.authorization}
        if kind == "remote_diagnostic":
            base["diagnostic"] = {"question": "probe", "local_limitation": "remote actor", "check": "probe", "stopping_condition": "answer"}
        return {**base, **fields}
