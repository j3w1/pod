"""A repository with a target branch, and stand-ins for Orca and GitHub, for core tests."""

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from tests.support import PodCase

HERE = Path(__file__).resolve().parent
ROUTES = "schema: pod/v2\nroutes:\n  claude/opus/high: enabled\n  codex/sol/high: enabled\n  codex/sol/low: disabled\npreferred: claude/opus/high\npinned: null\n"
SLUG = "acme/widgets"


class CoreCase(PodCase):
    def setUp(self):
        super().setUp()
        for name in ("orca", "gh"):
            self.script(name, f"#!/bin/sh\nexec {sys.executable} -B {HERE / ('fake_' + name + '.py')} \"$@\"\n")
        self.orca_file, self.gh_file = self.tmp / "orca.json", self.tmp / "gh.json"
        os.environ.update(FAKE_ORCA_STATE=str(self.orca_file), FAKE_GH_STATE=str(self.gh_file), FAKE_ORCA_CALLER="term_one",
                          GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
                          GIT_COMMITTER_EMAIL="t@example.invalid")
        self.gh_file.write_text(json.dumps({"issues": {}, "prs": {}, "checks": []}))
        from pod import gate
        wait = patch.object(gate, "SETTLE_SECONDS", 0)           # tests never wait for time to pass
        wait.start()
        self.addCleanup(wait.stop)
        self.write_routes(ROUTES)
        self.bare, self.repo = self.tmp / "origin.git", self.tmp / "work"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.bare))
        self.git(self.tmp, "init", "-q", "-b", "main", str(self.repo))
        url = f"https://github.com/{SLUG}.git"
        self.git(self.repo, "config", f"url.{self.bare}.insteadOf", url)
        self.git(self.repo, "remote", "add", "origin", url)
        self.commit({"README.md": "one\n"}, "start")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "switch", "-q", "-c", "work")

    def git(self, cwd, *args) -> str:
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, files: dict, message: str = "change") -> str:
        for name, text in files.items():
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if text is None:
                target.unlink()
            else:
                target.write_text(text)
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", message)
        return self.git(self.repo, "rev-parse", "HEAD")

    # --- GitHub stand-in
    def gh(self) -> dict:
        return json.loads(self.gh_file.read_text())

    def set_gh(self, **changes) -> None:
        self.gh_file.write_text(json.dumps({**self.gh(), **changes}))

    def issue(self, number: int, title: str, body: str) -> None:
        state = self.gh()
        state["issues"][str(number)] = {"title": title, "body": body}
        self.gh_file.write_text(json.dumps(state))

    def pull_request(self, number: int = 7, *, head: str | None = None, state: str = "OPEN", base: str = "main",
                     checks: str | None = "pass") -> None:
        """Push the branch and describe its pull request, with one required check in the given state."""
        head = head or self.git(self.repo, "rev-parse", "HEAD")
        self.git(self.repo, "push", "-q", "-f", "origin", "HEAD:refs/heads/work")
        current = self.gh()
        current["prs"][str(number)] = {"number": number, "state": state, "isDraft": False, "headRefOid": head,
                                       "headRefName": "work", "baseRefName": base, "mergeCommit": None, "mergedAt": None, "url": "",
                                       "mergeStateStatus": "CLEAN"}
        current["checks"] = [] if checks is None else [{"name": "linux", "bucket": checks, "state": checks.upper()}]
        self.gh_file.write_text(json.dumps(current))

    # --- Orca stand-in
    def orca(self) -> dict:
        return json.loads(self.orca_file.read_text())

    def set_orca(self, **changes) -> None:
        current = self.orca() if self.orca_file.exists() else {"runs": {}, "tasks": {}, "workers": {}, "requests": {},
                                                                 "bound": {}, "starts": 0, "fail": None}
        self.orca_file.write_text(json.dumps({**current, **changes}))

    def finish(self, task: str, result: dict | None, *, outcome: str = "succeeded", text: str = "Done.") -> None:
        """The worker reports through Orca: its message body ends with the result object."""
        state = self.orca()
        body = text + ("\n" + json.dumps(result) if result is not None else "")
        state["tasks"][task].update(status="completed" if outcome == "succeeded" else "failed", result=json.dumps(
            {"outcome": outcome, "subject": "report", "body": body}))
        for worker in state["workers"].values():
            if worker["taskId"] == task:
                worker.update(workerState=outcome, dispatchStatus="completed", terminalState="reclaimable")
        self.orca_file.write_text(json.dumps(state))

    def entries(self, objective: str) -> list[dict]:
        from pod import log
        return log.read(log.path("acme--widgets", objective))

    # --- short flows
    def project(self, text: str) -> None:
        """Put project rules on the target branch, before any work exists."""
        self.git(self.repo, "switch", "-q", "main")
        self.commit({".pod/config.yaml": text}, "rules")
        self.git(self.repo, "push", "-q", "origin", "main")
        self.git(self.repo, "switch", "-q", "work")
        self.git(self.repo, "reset", "-q", "--hard", "main")

    def opened(self, *criteria: str, **options) -> str:
        from pod import commands
        commands.open_(self.repo, text="Make the widget", objective="widget", criteria=list(criteria) or ["it works"], **options)
        return "widget"

    def implement(self, files: dict, *, paths: list[str] | None = None, integrate: bool = True, **result) -> tuple[str, dict]:
        """One implement assignment from dispatch to accept. Returns its commit and the accept output."""
        from pod import commands
        started = commands.dispatch(self.repo, None, role="implement", title="write", spec="Do it.", paths_=paths)
        commit = self.commit(files)
        self.finish(started["task"], {"outcome": "succeeded", "commit": commit, "summary": "done", **result})
        return commit, commands.accept(self.repo, None, started["assignment"], integrated=commit if integrate else None,
                                       discarded=not integrate)

    def review(self, verdict: str = "pass", findings: list | None = None, **options) -> dict:
        from pod import commands
        started = commands.dispatch(self.repo, None, role="review", title="review", spec="Review it.", route="codex/sol/high",
                                    **options)
        self.finish(started["task"], {"outcome": "succeeded", "verdict": verdict, "findings": findings or [], "summary": "read"})
        return commands.accept(self.repo, None, started["assignment"])

    def refused(self, code: str, call, *args, **kwargs):
        from pod.errors import PodError
        with self.assertRaises(PodError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertTrue(caught.exception.fix, "a refusal states what to change")
        return caught.exception
