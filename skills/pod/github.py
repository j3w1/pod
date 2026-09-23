"""Thin GitHub port: an exact command allowlist over `git` and `gh`, nothing else.

Every remote operation the waste governor performs goes through one of the argv shapes
below, against the exact candidate commit the executor was admitted for. The port never
rewrites a workflow, changes branch protection, or infers what a repository requires.
"""

from __future__ import annotations

import json
import hashlib
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
ISSUE_FIELDS = "number,title,body,state,url,updatedAt"
AMENDMENT_JQ = "{id: .id, body: .body, url: .html_url, updatedAt: .updated_at}"
_ISSUE_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)/?\Z")
_COMMENT_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)#issuecomment-([1-9][0-9]*)\Z")


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
    if (len(argv) == 7 and argv[:2] == ["issue", "view"] and _RUN_ID.fullmatch(argv[2])
            and argv[3] == "--repo" and _name(argv[4]) and argv[5:] == ["--json", ISSUE_FIELDS]):
        return True
    if (len(argv) == 4 and argv[0] == "api"
            and re.fullmatch(r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/comments/[1-9][0-9]*", argv[1])
            and argv[2:] == ["--jq", AMENDMENT_JQ]):
        return True
    return False


def _local_git(project: Path, argv: list[str], *, timeout: int = 20) -> str:
    allowed = (
        argv == ["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"]
        or argv == ["worktree", "list", "--porcelain"]
        or argv == ["remote", "get-url", "origin"]
        or argv == ["symbolic-ref", "--quiet", "--short", "HEAD"]
    )
    if not allowed:
        raise PodError("unsupported_git_operation", "Project discovery accepts only bounded Git reads")
    completed = _run([str(git_executable()), *argv], cwd=project, timeout=timeout, mutation=False)
    if completed.returncode:
        raise PodError("repository_identity_unavailable", "Git repository identity could not be read")
    if len(completed.stdout) > MAX_OUTPUT:
        raise PodError("git_contract", "Git discovery output exceeds its bound")
    return completed.stdout


def _github_repository(remote: str) -> str | None:
    value = remote.strip()
    patterns = (
        r"git@github\.com:([^/]+)/(.+?)(?:\.git)?\Z",
        r"ssh://git@github\.com/([^/]+)/(.+?)(?:\.git)?\Z",
        r"https?://github\.com/([^/]+)/(.+?)(?:\.git)?/?\Z",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, value)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return None


def repository_context(project: Path) -> dict:
    """Resolve stable repository identity and this linked worktree through Git reads only."""
    root = project.resolve()
    try:
        lines = _local_git(root, ["rev-parse", "--path-format=absolute", "--show-toplevel",
                                  "--git-common-dir"]).splitlines()
    except PodError:
        if any((candidate / ".git").exists() for candidate in (root, *root.parents)):
            raise
        return {"repository": None, "repo_key": None, "worktree": str(root),
                "main_worktree": str(root), "branch": None, "dirty": None,
                "linked_worktrees": [str(root)]}
    if len(lines) != 2:
        raise PodError("git_contract", "Git repository identity is incomplete")
    worktree = Path(lines[0]).resolve()
    common = Path(lines[1]).resolve()
    blocks = _local_git(worktree, ["worktree", "list", "--porcelain"]).strip().split("\n\n")
    worktrees: list[dict] = []
    for block in blocks:
        fields: dict[str, str | bool] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value if value else True
        candidate = fields.get("worktree")
        if isinstance(candidate, str):
            worktrees.append({"path": str(Path(candidate).resolve()),
                              "branch": (str(fields.get("branch", "")).removeprefix("refs/heads/")
                                         or None),
                              "detached": bool(fields.get("detached"))})
    if not worktrees or not any(row["path"] == str(worktree) for row in worktrees):
        raise PodError("git_contract", "Current worktree is absent from Git's worktree inventory")
    try:
        remote = _local_git(worktree, ["remote", "get-url", "origin"]).strip()
    except PodError:
        remote = ""
    repository = _github_repository(remote)
    current = next(row for row in worktrees if row["path"] == str(worktree))
    return {"repository": repository, "repo_key": hashlib.sha256(str(common).encode()).hexdigest(),
            "worktree": str(worktree), "main_worktree": worktrees[0]["path"],
            "branch": current["branch"], "dirty": None,
            "linked_worktrees": [row["path"] for row in worktrees]}


def parse_issue_locator(locator: object) -> dict:
    if not isinstance(locator, str) or len(locator) > 2048:
        raise PodError("invalid_issue_locator", "Execution Spec issue must be a bounded GitHub issue URL")
    match = _ISSUE_URL.fullmatch(locator)
    if match is None:
        raise PodError("invalid_issue_locator", "Execution Spec issue must use a canonical GitHub issue URL")
    repository = f"{match.group(1)}/{match.group(2)}"
    number = int(match.group(3))
    return {"repository": repository, "number": number,
            "locator": f"https://github.com/{repository}/issues/{number}"}


def parse_amendment_locator(locator: object) -> dict:
    if not isinstance(locator, str) or len(locator) > 2048:
        raise PodError("invalid_issue_amendment", "Amendment must be a bounded GitHub comment URL")
    match = _COMMENT_URL.fullmatch(locator)
    if match is None:
        raise PodError("invalid_issue_amendment", "Amendment must identify one GitHub issue comment")
    return {"repository": f"{match.group(1)}/{match.group(2)}", "number": int(match.group(3)),
            "comment": int(match.group(4)), "locator": locator}


def _validate_amendment_observation(row: object, *, repository: str,
                                    number: int, comment: int) -> dict:
    try:
        returned = parse_amendment_locator(str(row.get("url", ""))) if isinstance(row, dict) else None
    except PodError as exc:
        raise PodError("issue_identity_mismatch", "GitHub returned an invalid amendment URL") from exc
    if (not isinstance(row, dict) or row.get("id") != comment or returned is None
            or not isinstance(row.get("body"), str)
            or returned["repository"].casefold() != repository.casefold()
            or returned["number"] != number or returned["comment"] != comment):
        raise PodError("issue_identity_mismatch", "GitHub returned another issue amendment")
    return row


def validate_issue_binding(value: object) -> dict:
    from .util import exact
    source = exact(value, {"schema", "repository", "number", "locator", "body_sha256",
                           "amendments"}, {"schema", "repository", "number", "locator",
                                           "body_sha256", "amendments"}, name="issue_source")
    if source["schema"] != "pod-issue-source/v1":
        raise PodError("invalid_issue_source", "Issue source schema is unsupported")
    parsed = parse_issue_locator(source["locator"])
    if (parsed["repository"].casefold() != str(source["repository"]).casefold()
            or parsed["number"] != source["number"]
            or not re.fullmatch(r"[0-9a-f]{64}", str(source["body_sha256"]))):
        raise PodError("invalid_issue_source", "Issue source identity is inconsistent")
    amendments = source["amendments"]
    if not isinstance(amendments, list) or len(amendments) > 32:
        raise PodError("invalid_issue_source", "Issue amendments must be bounded")
    for row in amendments:
        exact(row, {"locator", "sha256"}, {"locator", "sha256"}, name="issue_amendment")
        if (not isinstance(row["locator"], str) or len(row["locator"]) > 2048
                or not re.fullmatch(r"[0-9a-f]{64}", str(row["sha256"]))):
            raise PodError("invalid_issue_source", "Issue amendment binding is invalid")
        amendment = parse_amendment_locator(row["locator"])
        if (amendment["repository"].casefold() != str(source["repository"]).casefold()
                or amendment["number"] != source["number"]):
            raise PodError("invalid_issue_source", "Issue amendment belongs to another source")
    return source


def issue_intake(project: Path, locator: str, *, port: "GhPort | None" = None,
                 amendments: list[str] | None = None) -> dict:
    parsed = parse_issue_locator(locator)
    context = repository_context(project)
    if context["repository"] is None or context["repository"].casefold() != parsed["repository"].casefold():
        raise PodError("repository_mismatch", "Issue target differs from the actual checkout repository")
    reader = port or GhPort(project)
    row = reader.issue(repository=parsed["repository"], number=parsed["number"])
    body = row.get("body")
    title = row.get("title")
    if (not isinstance(title, str) or not title.strip() or len(title) > 512
            or not isinstance(body, str) or not body.strip() or len(body.encode()) > 512 * 1024):
        raise PodError("incomplete_issue_source", "Issue body is empty or exceeds the bounded source size")
    requested_amendments = amendments or []
    if (not isinstance(requested_amendments, list) or len(requested_amendments) > 32
            or any(not isinstance(value, str) for value in requested_amendments)
            or len(set(requested_amendments)) != len(requested_amendments)):
        raise PodError("invalid_issue_amendment", "Relevant amendments must be a bounded unique list")
    amendment_bindings = []
    for locator_value in requested_amendments:
        amendment = parse_amendment_locator(locator_value)
        if (amendment["repository"].casefold() != parsed["repository"].casefold()
                or amendment["number"] != parsed["number"]):
            raise PodError("invalid_issue_amendment", "Amendment belongs to another issue")
        observed = _validate_amendment_observation(
            reader.amendment(repository=parsed["repository"], number=parsed["number"],
                             comment=amendment["comment"]),
            repository=parsed["repository"], number=parsed["number"],
            comment=amendment["comment"])
        if len(observed["body"].encode()) > 128 * 1024:
            raise PodError("incomplete_issue_source", "Relevant issue amendment exceeds its bound")
        amendment_bindings.append({"locator": amendment["locator"],
                                   "sha256": hashlib.sha256(observed["body"].encode()).hexdigest()})
    binding = {"schema": "pod-issue-source/v1", "repository": parsed["repository"],
               "number": parsed["number"], "locator": parsed["locator"],
               "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
               "amendments": amendment_bindings}
    validate_issue_binding(binding)
    status = "reconciliation_required" if row["state"] == "CLOSED" else "ready"
    return {"schema": "pod-issue-intake/v1", "status": status,
            "reason": "closed_issue_requires_intent_reconciliation" if status != "ready" else None,
            "source": binding, "title": title, "body": body,
            "updated_at": row.get("updatedAt"),
            "worktree": {"repository": context["repository"], "repo_key": context["repo_key"],
                         "path": context["worktree"], "branch": context["branch"]}}


def issue_recheck(project: Path, binding: dict, *, port: "GhPort | None" = None) -> dict:
    source = validate_issue_binding(binding)
    context = repository_context(project)
    if context["repository"] is None or context["repository"].casefold() != source["repository"].casefold():
        raise PodError("repository_mismatch", "Bound issue target differs from the actual checkout")
    row = (port or GhPort(project)).issue(repository=source["repository"], number=source["number"])
    digest_now = hashlib.sha256(row["body"].encode()).hexdigest()
    changed = digest_now != source["body_sha256"]
    amendment_changed = False
    reader = port or GhPort(project)
    for bound in source["amendments"]:
        amendment = parse_amendment_locator(bound["locator"])
        observed = _validate_amendment_observation(
            reader.amendment(repository=source["repository"], number=source["number"],
                             comment=amendment["comment"]),
            repository=source["repository"], number=source["number"],
            comment=amendment["comment"])
        if len(observed["body"].encode()) > 128 * 1024:
            raise PodError("incomplete_issue_source", "Relevant issue amendment exceeds its bound")
        amendment_changed = (amendment_changed
                             or hashlib.sha256(observed["body"].encode()).hexdigest() != bound["sha256"])
    closed = row["state"] == "CLOSED"
    return {"schema": "pod-issue-recheck/v1",
            "status": "reconciliation_required" if changed or amendment_changed or closed else "current",
            "body_changed": changed, "amendment_changed": amendment_changed,
            "metadata_changed": None,
            "state": row["state"], "updated_at": row.get("updatedAt"),
            "reason": ("issue_body_changed" if changed else
                       "issue_amendment_changed" if amendment_changed else
                       "closed_issue_requires_intent_reconciliation" if closed else None)}


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

    def issue(self, *, repository: str, number: int) -> dict:
        try:
            rows = self._gh_json(["issue", "view", str(number), "--repo", repository,
                                  "--json", ISSUE_FIELDS])
        except PodError as exc:
            if exc.code == "remote_read_failed":
                raise PodError("issue_access_unavailable",
                               "Issue could not be read through the authenticated GitHub CLI") from exc
            raise
        if not isinstance(rows, dict):
            raise PodError("gh_contract", "gh issue view returned an unsupported shape")
        try:
            returned = parse_issue_locator(str(rows.get("url", "")))
        except PodError as exc:
            raise PodError("issue_identity_mismatch", "GitHub returned an invalid issue URL") from exc
        if (rows.get("number") != number or returned["number"] != number
                or returned["repository"].casefold() != repository.casefold()
                or rows.get("state") not in ("OPEN", "CLOSED")
                or not isinstance(rows.get("title"), str)
                or not isinstance(rows.get("body"), str)):
            raise PodError("issue_identity_mismatch", "GitHub returned a different or incomplete issue")
        return rows

    def amendment(self, *, repository: str, number: int, comment: int) -> dict:
        try:
            row = self._gh_json(["api", f"repos/{repository}/issues/comments/{comment}",
                                 "--jq", AMENDMENT_JQ])
        except PodError as exc:
            if exc.code == "remote_read_failed":
                raise PodError("issue_access_unavailable",
                               "Relevant issue amendment could not be read") from exc
            raise
        return _validate_amendment_observation(row, repository=repository,
                                               number=number, comment=comment)

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
