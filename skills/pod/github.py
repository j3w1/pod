"""Thin GitHub port: an exact command allowlist over `git` and `gh`, nothing else.

Every remote operation the waste governor performs goes through one of the argv shapes
below, against the exact candidate commit the executor was admitted for. The port never
rewrites a workflow, changes branch protection, or infers what a repository requires.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .errors import PodError

MAX_OUTPUT = 2_000_000
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
_WORKFLOW = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.ya?ml\Z")
_GIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_RUN_ID = re.compile(r"[0-9]{1,20}\Z")
_INPUT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}=[^\n\r\x00]{0,512}\Z")
RUN_FIELDS = "databaseId,status,conclusion,createdAt,updatedAt,headSha,url,event,workflowName"
PR_FIELDS = "number,url,headRefOid,state,isDraft"


def _name(value: object) -> bool:
    return isinstance(value, str) and _NAME.fullmatch(value) is not None and ".." not in value


def gh_executable() -> Path:
    selected = os.environ.get("POD_GH_COMMAND") or "gh"
    found = shutil.which(selected)
    if not found:
        raise PodError("gh_unavailable", "The GitHub CLI is not on PATH; set POD_GH_COMMAND or install gh")
    return Path(found)


def git_executable() -> Path:
    found = shutil.which("git")
    if not found:
        raise PodError("git_unavailable", "Git is not on PATH")
    return Path(found)


def git_allowed(argv: list[str]) -> bool:
    """`git push` of one exact commit to one branch, or a read of that branch's head."""
    if len(argv) == 4 and argv[:2] == ["ls-remote", "--heads"] and _name(argv[2]):
        return argv[3].startswith("refs/heads/") and _name(argv[3][len("refs/heads/"):])
    if argv[:2] != ["push", "--porcelain"]:
        return False
    rest = argv[2:]
    if rest and rest[0].startswith("--force-with-lease=refs/heads/"):
        lease = rest[0][len("--force-with-lease=refs/heads/"):]
        branch, _, expected = lease.partition(":")
        if not _name(branch) or not _GIT_ID.fullmatch(expected):
            return False
        rest = rest[1:]
    if len(rest) != 2 or not _name(rest[0]):
        return False
    commit, _, target = rest[1].partition(":")
    return (_GIT_ID.fullmatch(commit) is not None and target.startswith("refs/heads/")
            and _name(target[len("refs/heads/"):]))


def gh_allowed(argv: list[str]) -> bool:
    """The exact `gh` shapes the port issues. Anything else is refused before a process starts."""
    if (len(argv) == 12 and argv[:2] == ["pr", "list"] and argv[2] == "--head" and _name(argv[3])
            and argv[4] == "--base" and _name(argv[5]) and argv[6:] == [
                "--state", "open", "--json", PR_FIELDS, "--limit", "5"]):
        return True
    if (len(argv) == 10 and argv[:2] == ["pr", "create"] and argv[2] == "--head" and _name(argv[3])
            and argv[4] == "--base" and _name(argv[5]) and argv[6] == "--title"
            and isinstance(argv[7], str) and argv[7].strip() and len(argv[7]) <= 256
            and argv[8] == "--body" and isinstance(argv[9], str) and len(argv[9]) <= 16 * 1024):
        return True
    if (len(argv) >= 5 and argv[:2] == ["workflow", "run"] and _WORKFLOW.fullmatch(argv[2])
            and argv[3] == "--ref" and _name(argv[4])):
        tail = argv[5:]
        if len(tail) % 2 or len(tail) > 16:
            return False
        return all(tail[index] == "-f" and _INPUT.fullmatch(tail[index + 1]) is not None
                   for index in range(0, len(tail), 2))
    if (len(argv) == 10 and argv[:2] == ["run", "list"] and argv[2] == "--workflow"
            and _WORKFLOW.fullmatch(argv[3]) and argv[4] == "--commit" and _GIT_ID.fullmatch(argv[5])
            and argv[6:] == ["--json", RUN_FIELDS, "--limit", "20"]):
        return True
    if (len(argv) == 5 and argv[:2] == ["run", "view"] and _RUN_ID.fullmatch(argv[2])
            and argv[3:] == ["--json", RUN_FIELDS]):
        return True
    if len(argv) == 3 and argv[:2] == ["run", "cancel"] and _RUN_ID.fullmatch(argv[2]):
        return True
    if argv[:2] == ["run", "rerun"] and len(argv) in (3, 4) and _RUN_ID.fullmatch(argv[2]) \
            and argv[3:] in ([], ["--failed"]):
        return True
    return False


def _run(command: list[str], *, cwd: Path, timeout: int, mutation: bool) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False,
                              cwd=str(cwd))
    except (OSError, subprocess.TimeoutExpired) as exc:
        code = "remote_effect_uncertain" if mutation else "remote_read_failed"
        raise PodError(code, "The remote command gave no response") from exc


def _normalise_run(row: dict) -> dict:
    return {"id": str(row.get("databaseId")), "status": row.get("status"), "conclusion": row.get("conclusion"),
            "created_at": row.get("createdAt"), "updated_at": row.get("updatedAt"),
            "head_sha": row.get("headSha"), "url": row.get("url"), "event": row.get("event"),
            "workflow": row.get("workflowName")}


class GhPort:
    """The installed `gh` and `git` executables, driven only through the allowlist."""

    def __init__(self, project: Path):
        self.project = project

    def _git(self, argv: list[str], *, mutation: bool) -> subprocess.CompletedProcess:
        if not git_allowed(argv):
            raise PodError("unsupported_git_operation", "The GitHub port issues only an exact push or head read")
        return _run([str(git_executable()), *argv], cwd=self.project, timeout=120 if mutation else 30,
                    mutation=mutation)

    def _gh(self, argv: list[str], *, mutation: bool) -> subprocess.CompletedProcess:
        if not gh_allowed(argv):
            raise PodError("unsupported_gh_operation", "The GitHub port accepts only known gh operations")
        completed = _run([str(gh_executable()), *argv], cwd=self.project, timeout=120 if mutation else 60,
                         mutation=mutation)
        if len(completed.stdout) > MAX_OUTPUT:
            raise PodError("gh_contract", "gh output exceeds bounded size")
        return completed

    def _gh_json(self, argv: list[str]) -> object:
        completed = self._gh(argv, mutation=False)
        if completed.returncode:
            raise PodError("remote_read_failed", "gh read did not succeed")
        try:
            return json.loads(completed.stdout or "null")
        except ValueError as exc:
            raise PodError("gh_contract", "gh returned invalid JSON") from exc

    def branch_head(self, *, remote: str, branch: str) -> str | None:
        completed = self._git(["ls-remote", "--heads", remote, "refs/heads/" + branch], mutation=False)
        if completed.returncode:
            raise PodError("remote_read_failed", "The remote branch head could not be read")
        for line in completed.stdout.splitlines():
            sha, _, ref = line.strip().partition("\t")
            if ref == "refs/heads/" + branch and _GIT_ID.fullmatch(sha):
                return sha
        return None

    def push(self, *, remote: str, branch: str, commit: str, expected: str | None) -> dict:
        argv = ["push", "--porcelain"]
        if expected is not None:
            argv.append(f"--force-with-lease=refs/heads/{branch}:{expected}")
        argv += [remote, f"{commit}:refs/heads/{branch}"]
        completed = self._git(argv, mutation=True)
        rejected = None
        pushed = False
        for line in completed.stdout.splitlines():
            if "\t" not in line:
                continue
            flag = line[0]
            if flag == "!":
                rejected = line.strip()
            elif flag in " +*=":
                pushed = True
        if rejected is not None:
            return {"status": "rejected", "detail": rejected[:256], "remote_head": None}
        if completed.returncode or not pushed:
            raise PodError("remote_effect_uncertain", "The push reported neither success nor rejection")
        return {"status": "pushed", "remote_head": commit}

    def pull_request(self, *, head: str, base: str) -> dict | None:
        rows = self._gh_json(["pr", "list", "--head", head, "--base", base, "--state", "open",
                              "--json", PR_FIELDS, "--limit", "5"])
        if not isinstance(rows, list):
            raise PodError("gh_contract", "gh pr list returned an unsupported shape")
        open_rows = [row for row in rows if isinstance(row, dict) and row.get("state") == "OPEN"]
        if not open_rows:
            return None
        row = open_rows[0]
        return {"number": row.get("number"), "url": row.get("url"), "head_sha": row.get("headRefOid"),
                "draft": bool(row.get("isDraft"))}

    def open_pull_request(self, *, head: str, base: str, title: str, body: str) -> dict:
        completed = self._gh(["pr", "create", "--head", head, "--base", base, "--title", title, "--body", body],
                             mutation=True)
        if completed.returncode:
            raise PodError("remote_effect_uncertain", "gh pr create did not report a pull request")
        url = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else ""
        match = re.search(r"/pull/(\d+)\Z", url)
        if match is None:
            raise PodError("remote_effect_uncertain", "gh pr create did not name the pull request")
        return {"number": int(match.group(1)), "url": url, "head_sha": None, "draft": False}

    def dispatch(self, *, workflow: str, ref: str, inputs: dict) -> dict:
        argv = ["workflow", "run", workflow, "--ref", ref]
        for key, value in sorted(inputs.items()):
            argv += ["-f", f"{key}={value}"]
        completed = self._gh(argv, mutation=True)
        if completed.returncode:
            raise PodError("remote_effect_uncertain", "gh workflow run did not confirm the dispatch")
        return {"status": "dispatched"}

    def runs(self, *, workflow: str, commit: str) -> list[dict]:
        rows = self._gh_json(["run", "list", "--workflow", workflow, "--commit", commit,
                              "--json", RUN_FIELDS, "--limit", "20"])
        if not isinstance(rows, list):
            raise PodError("gh_contract", "gh run list returned an unsupported shape")
        return [_normalise_run(row) for row in rows if isinstance(row, dict)]

    def run(self, *, run_id: str) -> dict:
        row = self._gh_json(["run", "view", run_id, "--json", RUN_FIELDS])
        if not isinstance(row, dict):
            raise PodError("gh_contract", "gh run view returned an unsupported shape")
        return _normalise_run(row)

    def rerun(self, *, run_id: str, failed_only: bool) -> dict:
        argv = ["run", "rerun", run_id] + (["--failed"] if failed_only else [])
        completed = self._gh(argv, mutation=True)
        if completed.returncode:
            raise PodError("remote_effect_uncertain", "gh run rerun did not confirm")
        return {"status": "rerun_requested"}

    def cancel(self, *, run_id: str) -> dict:
        completed = self._gh(["run", "cancel", run_id], mutation=True)
        if completed.returncode:
            raise PodError("remote_effect_uncertain", "gh run cancel did not confirm")
        return {"status": "cancel_requested"}
