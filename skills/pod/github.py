"""Thin GitHub port: an exact command allowlist over `git` and `gh`, nothing else.

Every remote operation the waste governor performs goes through one of the argv shapes
below, against the exact candidate commit the executor was admitted for. The port never
rewrites a workflow, changes branch protection, or infers what a repository requires.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess

from .errors import PodError
from .gitio import OBJECT_ID as _GIT_ID, worktree_inventory, worktree_rows

MAX_OUTPUT = 2_000_000
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}\Z")
_WORKFLOW = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.ya?ml\Z")
# Command authorization remains local even though the object-id syntax is shared.
_RUN_ID = re.compile(r"[0-9]{1,20}\Z")
_ATTEMPT = re.compile(r"[1-9][0-9]{0,3}\Z")
_INPUT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}=[^\n\r\x00]{0,512}\Z")
RUN_FIELDS = "databaseId,status,conclusion,createdAt,updatedAt,headSha,url,event,workflowName,attempt,startedAt"
# A run list asks for one more run than it reads, so a full list says whether it was cut short.
RUN_LIST_LIMIT = 20
# A mutation that has not answered within this many seconds has ended; a read is bounded by half.
MUTATION_TIMEOUT_S = 120
PR_FIELDS = "number,url,headRefOid,state,isDraft"
# A pull request read back for a carried effect (R118): every state, with its merge commit.
PR_READ_FIELDS = "number,url,headRefOid,state,mergeCommit,mergedAt"
PR_LIST_LIMIT = 10
ISSUE_FIELDS = "number,title,body,state,stateReason,url,updatedAt"
# What a delivery proof reads (R145): an issue's closing reason, and one pull request by its number.
ISSUE_STATE_FIELDS = "number,url,state,stateReason,body"
PR_VIEW_FIELDS = "number,url,state,mergedAt,mergeCommit"
PR_COMMITS_FIELDS = "number,headRefOid,commits"
# Fixed, read-only connection. The seventeenth node and pageInfo detect truncation.
CLOSING_PRS_QUERY = ("query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
    "nameWithOwner issue(number:$number){number url closedByPullRequestsReferences(first:17,includeClosedPrs:true){"
    "nodes{number url state mergedAt mergeCommit{oid} baseRefName baseRepository{nameWithOwner}} "
    "pageInfo{hasNextPage}}}}}")
# Only the latest reopen matters: earlier events cannot change its ordering against Pod's closure.
ISSUE_REOPEN_QUERY = ("query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
    "nameWithOwner issue(number:$number){number url timelineItems(last:1,itemTypes:[REOPENED_EVENT]){"
    "nodes{__typename ... on ReopenedEvent{createdAt}}}}}}")
AMENDMENT_JQ = "{id: .id, body: .body, url: .html_url, updatedAt: .updated_at}"
_ISSUE_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)/?\Z")
_COMMENT_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)#issuecomment-([1-9][0-9]*)\Z")
POD_REF = re.compile(r"PoD#([0-9]{1,9})(?![0-9])")
_POD_LINE = re.compile(r"- \[([ xX])\] \*\*PoD#([0-9]{1,9})(?![0-9])")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)\Z")


def pod_ref_number(ref: object, *, seal: bool = False) -> int | None:
    """The bounded PoD number at the start of a criterion reference, if present."""
    match = POD_REF.match(ref) if isinstance(ref, str) else None
    if match is None and seal and isinstance(ref, str):
        match = re.fullmatch(r"AC([0-9]{1,9})", ref)
    return int(match[1]) if match else None


def pod_lines(body: str, *, seal: bool = False) -> dict[int, list[dict]]:
    """Recognize only column-zero PoD lines outside CommonMark fenced code blocks."""
    found: dict[int, list[dict]] = {}
    fence_char, fence_length = "", 0
    offset = 0
    # Only LF and CRLF delimit this bounded line rule.
    for raw in body.split("\n"):
        line = raw.removesuffix("\r")
        fence = _FENCE.fullmatch(line)
        if fence_char:
            if (fence and fence[1][0] == fence_char and len(fence[1]) >= fence_length
                    and not fence[2].strip(" \t")):
                fence_char, fence_length = "", 0
        elif fence and (fence[1][0] == "~" or "`" not in fence[2]):
            fence_char, fence_length = fence[1][0], len(fence[1])
        else:
            match = _POD_LINE.match(line)
            if match is None and seal:
                match = re.match(r"- \[([ xX])\] \*\*AC([0-9]{1,9})(?![0-9A-Za-z_])", line)
            if match:
                found.setdefault(int(match[2]), []).append(
                    {"offset": offset + 3, "ticked": match[1] != " "})
        offset += len(raw) + 1
    return found


def flip_pod_checkbox(body: str, offset: int, ticked: bool, *, seal: bool = False) -> str:
    """Change exactly one recognized checkbox character, preserving every other byte."""
    target = "x" if ticked else " "
    if (type(offset) is not int or type(ticked) is not bool
            or not any(row["offset"] == offset for rows in pod_lines(body, seal=seal).values() for row in rows)
            or body[offset] == target):
        raise PodError("internal_error", "PoD edit must change exactly one recognized checkbox character")
    return body[:offset] + target + body[offset + 1:]


def pod_blocks(body: str, *, seal: bool = False) -> dict[int, list[dict]]:
    """Complete physical list-item scope for recognized PoDs, without their checkbox.

    Indented continuations/nested lists, internal blank lines and lazy paragraph
    continuations belong to the item. A sibling block or an unindented paragraph
    after a blank line ends it. Only the final line terminator is excluded.
    """
    physical = body.split("\n")
    positions, offset = {}, 0
    for index, raw in enumerate(physical):
        positions[offset + 3] = index
        offset += len(raw) + 1
    boundary = re.compile(r"(?:#{1,6}(?:[ \t]|$)|(?:[-+*]|[0-9]+[.)])[ \t]+|>|(?:[-*_][ \t]*){3,}$)")
    found = {}
    for number, occurrences in pod_lines(body, seal=seal).items():
        for occurrence in occurrences:
            first = positions[occurrence["offset"]]
            last, blank = first + 1, False
            for index in range(first + 1, len(physical)):
                line = physical[index].removesuffix("\r")
                if not line.strip(" \t"):
                    blank = True
                    continue
                if not line.startswith((" ", "\t")) and (blank or boundary.match(line) or _FENCE.fullmatch(line)):
                    break
                last, blank = index + 1, False
            text = "\n".join(physical[first:last])[6:].removesuffix("\r")
            found.setdefault(number, []).append({**occurrence, "text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    return found


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


def gh_allowed(argv: list[str], *, bound_issue: tuple[str, int] | None = None,
               stdin: str | None = None) -> bool:
    """The exact `gh` shapes the port issues. Anything else is refused before a process starts."""
    if argv[:3] == ["api", "--method", "PATCH"]:
        if (not isinstance(bound_issue, tuple) or len(bound_issue) != 2
                or not isinstance(bound_issue[0], str) or type(bound_issue[1]) is not int
                or bound_issue[1] < 1
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", bound_issue[0])
                or argv != ["api", "--method", "PATCH", "--include",
                            f"repos/{bound_issue[0]}/issues/{bound_issue[1]}", "--input", "-"]
                or not isinstance(stdin, str)):
            return False
        # Exact spelling deliberately binds the path, including repository case.
        try:
            payload = json.loads(stdin)
            if payload == {"state": "closed", "state_reason": "completed"}:
                return stdin == json.dumps({"state": "closed", "state_reason": "completed"})
            return (isinstance(payload, dict) and set(payload) == {"body"}
                    and isinstance(payload["body"], str)
                    and len(payload["body"].encode()) <= 512 * 1024
                    and stdin == json.dumps({"body": payload["body"]}))
        except (ValueError, UnicodeEncodeError):
            return False
    if stdin is not None:
        return False
    if (len(argv) == 10 and argv[:3] == ["api", "graphql", "-f"]
            and argv[3] in ("query=" + CLOSING_PRS_QUERY, "query=" + ISSUE_REOPEN_QUERY)
            and argv[4] == "-f" and argv[5].startswith("owner=") and _name(argv[5][6:])
            and argv[6] == "-f" and argv[7].startswith("name=") and _name(argv[7][5:])
            and argv[8] == "-F" and re.fullmatch(r"number=[1-9][0-9]{0,8}", argv[9])):
        return "/" not in argv[5][6:] and "/" not in argv[7][5:]
    if (len(argv) == 12 and argv[:2] == ["pr", "list"] and argv[2] == "--head" and _name(argv[3])
            and argv[4] == "--base" and _name(argv[5]) and argv[6:] == [
                "--state", "open", "--json", PR_FIELDS, "--limit", "5"]):
        return True
    if (len(argv) == 12 and argv[:2] == ["pr", "list"] and argv[2] == "--head" and _name(argv[3])
            and argv[4] == "--base" and _name(argv[5]) and argv[6:] == [
                "--state", "all", "--json", PR_READ_FIELDS, "--limit", str(PR_LIST_LIMIT + 1)]):
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
            and argv[6:] == ["--json", RUN_FIELDS, "--limit", str(RUN_LIST_LIMIT + 1)]):
        return True
    if (len(argv) == 5 and argv[:2] == ["run", "view"] and _RUN_ID.fullmatch(argv[2])
            and argv[3:] == ["--json", RUN_FIELDS]):
        return True
    if (len(argv) == 7 and argv[:2] == ["run", "view"] and _RUN_ID.fullmatch(argv[2])
            and argv[3] == "--attempt" and _ATTEMPT.fullmatch(argv[4]) and argv[5:] == ["--json", RUN_FIELDS]):
        return True
    if len(argv) == 3 and argv[:2] == ["run", "cancel"] and _RUN_ID.fullmatch(argv[2]):
        return True
    if argv[:2] == ["run", "rerun"] and len(argv) in (3, 4) and _RUN_ID.fullmatch(argv[2]) \
            and argv[3:] in ([], ["--failed"]):
        return True
    if (len(argv) == 7 and argv[:2] == ["issue", "view"] and _RUN_ID.fullmatch(argv[2])
            and argv[3] == "--repo" and _name(argv[4]) and argv[5:] == ["--json", ISSUE_FIELDS]):
        return True
    if (len(argv) == 7 and argv[:2] == ["issue", "view"] and _RUN_ID.fullmatch(argv[2])
            and argv[3] == "--repo" and _name(argv[4]) and argv[5:] == ["--json", ISSUE_STATE_FIELDS]):
        return True
    if (len(argv) == 7 and argv[:2] == ["pr", "view"] and _RUN_ID.fullmatch(argv[2])
            and argv[3] == "--repo" and _name(argv[4]) and argv[5:] in (["--json", PR_VIEW_FIELDS],
                                                                        ["--json", PR_COMMITS_FIELDS])):
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
    # Discovery reads this host's Git only, so its failures are local, never a remote contract (`_run`).
    try:
        completed = subprocess.run([str(git_executable()), *argv], capture_output=True, text=True, timeout=timeout,
                                   check=False, cwd=str(project))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PodError("git_unavailable", "Git did not answer the repository discovery read") from exc
    except UnicodeDecodeError:
        raise PodError("git_contract", "Git discovery output is not valid UTF-8") from None
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


# One helper call or one-shot command reads each repository's identity once. A native effect
# forgets what was read, so identity read after an effect is fresh; unscoped calls always read Git.
_IDENTITY: ContextVar[dict | None] = ContextVar("pod_repository_identity", default=None)


@contextmanager
def repository_reads():
    """Share repository identity reads within one helper call or one-shot command."""
    if _IDENTITY.get() is not None:
        yield
        return
    token = _IDENTITY.set({})
    try:
        yield
    finally:
        _IDENTITY.reset(token)


def forget_repository_reads() -> None:
    """After a native effect, read repository identity again."""
    reads = _IDENTITY.get()
    if reads is not None:
        reads.clear()


def repository_context(project: Path, *, fresh: bool = False) -> dict:
    """Resolve stable repository identity and this linked worktree through Git reads only.

    `fresh` always reads Git, for a check that binds identity right before an effect, and the
    call's later reads then agree with what that check saw.
    """
    root = project.resolve()
    reads = _IDENTITY.get()
    if reads is not None and not fresh and str(root) in reads:
        return deepcopy(reads[str(root)])
    context, complete = _read_repository_context(root)
    if reads is not None:
        if complete:
            reads[str(root)] = deepcopy(context)
        else:
            reads.pop(str(root), None)  # A transient remote failure is never kept for the call.
    return context


def _read_repository_context(root: Path) -> tuple[dict, bool]:
    """The identity, and whether every read answered (a failed read raises or is incomplete)."""
    try:
        lines = _local_git(root, ["rev-parse", "--path-format=absolute", "--show-toplevel",
                                  "--git-common-dir"]).splitlines()
    except PodError:
        if any((candidate / ".git").exists() for candidate in (root, *root.parents)):
            raise
        return {"repository": None, "repo_key": None, "worktree": str(root),
                "main_worktree": str(root), "branch": None, "dirty": None,
                "linked_worktrees": [str(root)]}, True
    if len(lines) != 2:
        raise PodError("git_contract", "Git repository identity is incomplete")
    worktree = Path(lines[0]).resolve()
    common = Path(lines[1]).resolve()
    worktrees: list[dict] = []
    with worktree_inventory(worktree, executable=str(git_executable())) as inventory:
        for fields in worktree_rows(inventory):
            candidate = fields.get("worktree")
            if isinstance(candidate, str):
                worktrees.append({"path": str(Path(candidate).resolve()),
                                  "branch": (fields.get("branch", "").removeprefix("refs/heads/") or None),
                                  "detached": "detached" in fields})
    if not worktrees or not any(row["path"] == str(worktree) for row in worktrees):
        raise PodError("git_contract", "Current worktree is absent from Git's worktree inventory")
    complete = True
    try:
        remote = _local_git(worktree, ["remote", "get-url", "origin"]).strip()
    except PodError as exc:
        remote = ""
        complete = exc.code == "repository_identity_unavailable"  # No origin answers; a timeout does not.
    repository = _github_repository(remote)
    current = next(row for row in worktrees if row["path"] == str(worktree))
    return {"repository": repository, "repo_key": hashlib.sha256(str(common).encode()).hexdigest(),
            "worktree": str(worktree), "main_worktree": worktrees[0]["path"],
            "branch": current["branch"], "dirty": None,
            "linked_worktrees": [row["path"] for row in worktrees]}, complete


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
                           "amendments", "role"}, {"schema", "repository", "number", "locator",
                                           "body_sha256", "amendments"}, name="issue_source")
    if source["schema"] != "pod-issue-source/v1":
        raise PodError("invalid_issue_source", "Issue source schema is unsupported")
    if "role" in source and source["role"] != "SEAL":
        raise PodError("invalid_issue_source", "Only a SEAL completion binding may carry a role")
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


EEL_TITLE = "EEL:"
EEL_FORMAT = "**Format:** Evidence Evaluation Ledger v1"
SEAL_TITLE = "SEAL:"
SEAL_FORMAT = "**Format:** SEAL v1"


def _document_marker(title: object, body: object, prefix: str, declaration: str) -> str | None:
    """The exact title prefix, or the first non-empty unindented body line; nothing else is read."""
    if isinstance(title, str) and title.startswith(prefix):
        return "title"
    for line in body.split("\n") if isinstance(body, str) else ():
        line = line[:-1] if line.endswith("\r") else line
        if line.strip():
            return "body" if line in (declaration, declaration + "<br>") else None
    return None


def _refuse_eel(row: dict, locator: str) -> None:
    marker = _document_marker(row.get("title"), row.get("body"), EEL_TITLE, EEL_FORMAT)
    if marker is not None:
        raise PodError("issue_is_evidence_ledger",
                       "Issue is an Evidence Evaluation Ledger (EEL): evidence only, never an objective "
                       "source. Implementation needs a separate Pod Execution Spec that cites it",
                       {"marker": marker, "locator": locator,
                        "next_action": "write or use a separate Pod Execution Spec that cites this EEL"})


def _refuse_requirements(row: dict, locator: str, *, seal: bool = False) -> None:
    # Either restrictive identity wins over a conflicting executable declaration.
    # Preserve the EEL diagnostic, including when the other marker says SEAL.
    _refuse_eel(row, locator)
    marker = _document_marker(row.get("title"), row.get("body"), SEAL_TITLE, SEAL_FORMAT)
    if seal:
        if marker is None:
            raise PodError("issue_role_changed", "The bound SEAL changed document role",
                           {"next_action": "the project lead reconciles the source role before further work"})
        return
    if marker is not None:
        raise PodError("issue_is_requirements_spec",
                       "Issue is a SEAL (Scope, Expectations, Acceptance & Limits): requirements input, "
                       "not a primary executable issue for the whole initiative",
                       {"marker": marker, "locator": locator,
                        "next_action": "use read-only SEAL delivery planning, a bounded Pod Execution Spec "
                                       "citing selected SEAL requirements, or an explicitly scoped analysis request"})


def _complete_issue_source(row: dict) -> bool:
    title, body = row.get("title"), row.get("body")
    return (isinstance(title, str) and bool(title.strip()) and len(title) <= 512
            and isinstance(body, str) and bool(body.strip()) and len(body.encode()) <= 512 * 1024)


def issue_intake(project: Path, locator: str, *, port: "GhPort | None" = None,
                 amendments: list[str] | None = None, completion: bool = False) -> dict:
    if type(completion) is not bool:
        raise PodError("invalid_request", "completion must be boolean")
    parsed = parse_issue_locator(locator)
    context = repository_context(project, fresh=True)  # A100 binds the target now
    reader = port or GhPort(project)
    if context["repository"] is None or context["repository"].casefold() != parsed["repository"].casefold():
        # A complete, exact role identity can explain the refusal from any checkout.
        # All other observations, including failed reads, retain the target refusal.
        try:
            row = reader.issue(repository=parsed["repository"], number=parsed["number"])
            returned = parse_issue_locator(row.get("url")) if isinstance(row, dict) else None
        except PodError:
            returned = None
        if (returned is not None and row.get("number") == parsed["number"]
                and returned["number"] == parsed["number"]
                and returned["repository"].casefold() == parsed["repository"].casefold()
                and _complete_issue_source(row)):
            _refuse_requirements(row, parsed["locator"])
        raise PodError("repository_mismatch", "Issue target differs from the actual checkout repository")
    row = reader.issue(repository=parsed["repository"], number=parsed["number"])
    body = row.get("body")
    title = row.get("title")
    if not _complete_issue_source(row):
        raise PodError("incomplete_issue_source", "Issue body is empty or exceeds the bounded source size")
    seal = completion and _document_marker(title, body, SEAL_TITLE, SEAL_FORMAT) is not None
    _refuse_requirements(row, parsed["locator"], seal=seal)
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
    if seal:
        binding["role"] = "SEAL"
    validate_issue_binding(binding)
    status = "reconciliation_required" if row["state"] == "CLOSED" else "ready"
    return {"schema": "pod-issue-intake/v1", "status": status,
            "reason": "closed_issue_requires_intent_reconciliation" if status != "ready" else None,
            "source": binding, "title": title, "body": body,
            "updated_at": row.get("updatedAt"),
            "worktree": {"repository": context["repository"], "repo_key": context["repo_key"],
                         "path": context["worktree"], "branch": context["branch"]}}


def issue_recheck(project: Path, binding: dict, *, port: "GhPort | None" = None) -> dict:
    return issue_recheck_live(project, binding, port=port)[0]


def issue_recheck_live(project: Path, binding: dict, *,
                       port: "GhPort | None" = None) -> tuple[dict, dict | None]:
    """The unchanged recheck result together with the issue row read for that check."""
    source = validate_issue_binding(binding)
    context = repository_context(project, fresh=True)  # A100 binds the target now
    if context["repository"] is None or context["repository"].casefold() != source["repository"].casefold():
        raise PodError("repository_mismatch", "Bound issue target differs from the actual checkout")
    row = (port or GhPort(project)).issue(repository=source["repository"], number=source["number"])
    # A restrictive document identity stops execution even when only its title changed.
    _refuse_requirements(row, source["locator"], seal=source.get("role") == "SEAL")
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
            "state": row["state"], "state_reason": row.get("stateReason"), "updated_at": row.get("updatedAt"),
            "reason": ("issue_body_changed" if changed else
                       "issue_amendment_changed" if amendment_changed else
                       "closed_issue_requires_intent_reconciliation" if closed else None)}, row


def _run(command: list[str], *, cwd: Path, timeout: int, mutation: bool,
         input: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False,
                              cwd=str(cwd), **({"input": input} if input is not None else {}))
    except (OSError, subprocess.TimeoutExpired) as exc:
        code = "remote_effect_uncertain" if mutation else "remote_read_failed"
        raise PodError(code, "The remote command gave no response") from exc
    except UnicodeDecodeError:
        code = "remote_effect_uncertain" if mutation else "remote_read_failed"
        raise PodError(code, "The command's output is not valid UTF-8") from None
    finally:
        if mutation:
            forget_repository_reads()


def _normalise_run(row: dict) -> dict:
    attempt = row.get("attempt")
    return {"id": str(row.get("databaseId")), "status": row.get("status"), "conclusion": row.get("conclusion"),
            "created_at": row.get("createdAt"), "updated_at": row.get("updatedAt"),
            "head_sha": row.get("headSha"), "url": row.get("url"), "event": row.get("event"),
            "workflow": row.get("workflowName"), "started_at": row.get("startedAt"),
            "attempt": attempt if type(attempt) is int and attempt >= 1 else None}


def _http_response(output: str) -> tuple[int | None, str]:
    """Read complete --include header blocks, skipping interim 1xx responses only."""
    remaining = output.replace("\r\n", "\n")
    while True:
        headers, separator, payload = remaining.partition("\n\n")
        lines = headers.split("\n")
        match = re.fullmatch(r"HTTP/[0-9]+(?:\.[0-9]+)? ([1-5][0-9]{2})(?: [^\r\n]*)?", lines[0])
        if (not separator or match is None
                or any(re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+:[^\r\n]*", line) is None
                       for line in lines[1:])):
            return None, ""
        status = int(match[1])
        if status >= 200:
            return status, payload
        remaining = payload


class GhPort:
    """The installed `gh` and `git` executables, driven only through the allowlist."""

    def __init__(self, project: Path):
        self.project = project

    def _git(self, argv: list[str], *, mutation: bool) -> subprocess.CompletedProcess:
        if not git_allowed(argv):
            raise PodError("unsupported_git_operation", "The GitHub port issues only an exact push or head read")
        return _run([str(git_executable()), *argv], cwd=self.project,
                    timeout=MUTATION_TIMEOUT_S if mutation else 30, mutation=mutation)

    def _gh(self, argv: list[str], *, mutation: bool,
            bound_issue: tuple[str, int] | None = None, stdin: str | None = None) -> subprocess.CompletedProcess:
        if not gh_allowed(argv, bound_issue=bound_issue, stdin=stdin):
            raise PodError("unsupported_gh_operation", "The GitHub port accepts only known gh operations")
        completed = _run([str(gh_executable()), *argv], cwd=self.project,
                         timeout=MUTATION_TIMEOUT_S if mutation else 60, mutation=mutation,
                         **({"input": stdin} if stdin is not None else {}))
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

    def issue_state(self, *, repository: str, number: int) -> dict:
        """One issue's state, closing reason and body, read only: what a delivery proof checks (R145)."""
        rows = self._gh_json(["issue", "view", str(number), "--repo", repository, "--json", ISSUE_STATE_FIELDS])
        try:
            returned = parse_issue_locator(str(rows.get("url", ""))) if isinstance(rows, dict) else None
        except PodError:
            returned = None
        if (returned is None or rows.get("number") != number or returned["number"] != number
                or returned["repository"].casefold() != repository.casefold()
                or rows.get("state") not in ("OPEN", "CLOSED") or not isinstance(rows.get("body"), str)
                or not isinstance(rows.get("stateReason"), (str, type(None)))):
            raise PodError("issue_identity_mismatch", "GitHub returned a different or incomplete issue")
        return {"number": number, "url": rows["url"], "state": rows["state"], "state_reason": rows.get("stateReason"),
                "body": rows["body"]}

    def issue_reopened_at(self, *, repository: str, number: int) -> str | None:
        """One bounded, read-only query for this exact issue's latest reopen event."""
        from .util import normalize_timestamp
        owner, name = repository.split("/")
        response = self._gh_json(["api", "graphql", "-f", "query=" + ISSUE_REOPEN_QUERY,
                                  "-f", "owner=" + owner, "-f", "name=" + name, "-F", f"number={number}"])
        try:
            if response.get("errors"):
                raise ValueError
            repo = response["data"]["repository"]
            issue = repo["issue"]
            identity = parse_issue_locator(issue["url"])
            nodes = issue["timelineItems"]["nodes"]
            if (repo["nameWithOwner"].casefold() != repository.casefold()
                    or type(issue["number"]) is not int or issue["number"] != number
                    or identity["number"] != number or identity["repository"].casefold() != repository.casefold()
                    or not isinstance(nodes, list) or len(nodes) > 1):
                raise ValueError
            if not nodes:
                return None
            event = nodes[0]
            if event["__typename"] != "ReopenedEvent":
                raise ValueError
            return normalize_timestamp(event["createdAt"], field="Issue reopen time", code="gh_contract")
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise PodError("gh_contract", "GitHub returned an inconsistent issue reopen timeline") from exc

    def pr_view(self, *, repository: str, number: int) -> dict:
        """One pull request by its number, read only: whether a delivery pull request merged (R145)."""
        row = self._gh_json(["pr", "view", str(number), "--repo", repository, "--json", PR_VIEW_FIELDS])
        if (not isinstance(row, dict) or row.get("number") != number
                or row.get("state") not in ("OPEN", "CLOSED", "MERGED")):
            raise PodError("gh_contract", "gh pr view returned an unsupported shape")
        merged = row.get("mergeCommit") if isinstance(row.get("mergeCommit"), dict) else {}
        oid = merged.get("oid")
        return {"number": number, "url": row.get("url") if isinstance(row.get("url"), str) else None,
                "state": row["state"], "merged_at": row.get("mergedAt") if isinstance(row.get("mergedAt"), str) else None,
                "merge_commit": oid if isinstance(oid, str) and _GIT_ID.fullmatch(oid) else None}

    def closing_prs(self, *, repository: str, number: int) -> list[dict]:
        """Complete bounded delivery association, with exact issue, PR and base identities."""
        owner, name = repository.split("/")
        response = self._gh_json(["api", "graphql", "-f", "query=" + CLOSING_PRS_QUERY,
                                  "-f", "owner=" + owner, "-f", "name=" + name, "-F", f"number={number}"])
        try:
            if response.get("errors"):
                raise ValueError
            repo = response["data"]["repository"]
            issue = repo["issue"]
            identity = parse_issue_locator(issue["url"])
            connection = issue["closedByPullRequestsReferences"]
            nodes, page = connection["nodes"], connection["pageInfo"]
            if (repo["nameWithOwner"].casefold() != repository.casefold()
                    or issue["number"] != number or identity["number"] != number
                    or identity["repository"].casefold() != repository.casefold()
                    or not isinstance(nodes, list) or type(page["hasNextPage"]) is not bool):
                raise ValueError
            if len(nodes) > 16 or page["hasNextPage"]:
                raise PodError("scope_too_large", "Linked delivery exceeds 16 pull requests or is truncated")
            result, seen = [], set()
            for row in nodes:
                pr = row["number"]
                oid = (row.get("mergeCommit") or {}).get("oid")
                base_repository = row["baseRepository"]["nameWithOwner"]
                identity = (base_repository.casefold(), pr)
                url = f"https://github.com/{base_repository}/pull/{pr}"
                if (type(pr) is not int or pr < 1 or identity in seen or row["url"].casefold() != url.casefold()
                        or row["state"] not in ("OPEN", "CLOSED", "MERGED")
                        or not isinstance(base_repository, str) or not _name(base_repository)
                        or not isinstance(row["baseRefName"], str) or not _name(row["baseRefName"])
                        or row["state"] == "MERGED" and (not isinstance(oid, str) or not _GIT_ID.fullmatch(oid)
                                                                          or not isinstance(row["mergedAt"], str))):
                    raise ValueError
                seen.add(identity)
                if base_repository.casefold() != repository.casefold():
                    continue
                result.append({"number": pr, "url": row["url"], "state": row["state"],
                               "merged_at": row.get("mergedAt"), "merge_commit": oid,
                               "base_repository": base_repository, "base_branch": row["baseRefName"]})
            return result
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise PodError("gh_contract", "GitHub returned inconsistent linked delivery identities") from exc

    def pr_commits(self, *, repository: str, number: int) -> dict:
        """One pull request's head and commits, read only (C11); a list at GitHub's bound may be truncated."""
        from .evidence_closure import MAX_PR_COMMITS
        row = self._gh_json(["pr", "view", str(number), "--repo", repository, "--json", PR_COMMITS_FIELDS])
        commits = row.get("commits") if isinstance(row, dict) else None
        head = row.get("headRefOid") if isinstance(row, dict) else None
        if (not isinstance(row, dict) or row.get("number") != number or not isinstance(commits, list)
                or not isinstance(head, str) or not _GIT_ID.fullmatch(head)):
            raise PodError("gh_contract", "gh pr view returned an unsupported commit list")
        oids = [item.get("oid") for item in commits if isinstance(item, dict)]
        if len(oids) != len(commits) or not all(isinstance(oid, str) and _GIT_ID.fullmatch(oid) for oid in oids):
            raise PodError("gh_contract", "gh pr view returned an unsupported commit list")
        return {"number": number, "head": head, "commits": oids, "truncated": len(oids) >= MAX_PR_COMMITS}

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

    def issue_body_write(self, *, source: dict, body: str) -> dict:
        """Send one bound issue PATCH; distinguish a held refusal from an uncertain effect."""
        return self._issue_write(source, {"body": body})

    def issue_close(self, *, source: dict) -> dict:
        """The only source-issue lifecycle effect: close this bound issue as completed."""
        return self._issue_write(source, {"state": "closed", "state_reason": "completed"})

    def _issue_write(self, source: dict, payload: dict) -> dict:
        binding = validate_issue_binding(source)
        argv = ["api", "--method", "PATCH", "--include",
                f"repos/{binding['repository']}/issues/{binding['number']}", "--input", "-"]
        try:
            completed = self._gh(argv, mutation=True,
                                 bound_issue=(binding["repository"], binding["number"]),
                                 stdin=json.dumps(payload))
        except PodError as exc:
            if exc.code == "gh_contract":
                raise PodError("remote_effect_uncertain", "Issue write response exceeded its bound") from exc
            raise
        status, payload = _http_response(completed.stdout)
        if status is not None and 200 <= status < 300 and completed.returncode == 0:
            return {"result": "accepted", "status": status}
        if status is not None and 400 <= status < 500 and completed.returncode > 0:
            try:
                row = json.loads(payload)
            except ValueError:
                row = None
            message = row.get("message") if isinstance(row, dict) else None
            return {"result": "refused", "status": status,
                    "message": message[:512] if isinstance(message, str) else ""}
        raise PodError("remote_effect_uncertain", "Issue write gave no unambiguous success or refusal")

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

    def pull_requests(self, *, head: str, base: str) -> list[dict]:
        """Pull requests of this head into this base in every state, newest first: at most PR_LIST_LIMIT + 1."""
        rows = self._gh_json(["pr", "list", "--head", head, "--base", base, "--state", "all",
                              "--json", PR_READ_FIELDS, "--limit", str(PR_LIST_LIMIT + 1)])
        if not isinstance(rows, list):
            raise PodError("gh_contract", "gh pr list returned an unsupported shape")
        found = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            merged = row.get("mergeCommit") if isinstance(row.get("mergeCommit"), dict) else {}
            oid = merged.get("oid")
            found.append({"number": row.get("number"), "url": row.get("url"), "head_sha": row.get("headRefOid"),
                          "state": row.get("state"), "merged_at": row.get("mergedAt"),
                          "merge_commit": oid if isinstance(oid, str) and _GIT_ID.fullmatch(oid) else None})
        return found

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
        """This workflow's runs on this commit, newest first: at most RUN_LIST_LIMIT + 1 of them.

        `--workflow` and `--commit` filter on the server. One run past the limit says the newest
        RUN_LIST_LIMIT were not all of them; GitHub lists only the runs it still retains.
        """
        rows = self._gh_json(["run", "list", "--workflow", workflow, "--commit", commit,
                              "--json", RUN_FIELDS, "--limit", str(RUN_LIST_LIMIT + 1)])
        if not isinstance(rows, list):
            raise PodError("gh_contract", "gh run list returned an unsupported shape")
        return [_normalise_run(row) for row in rows if isinstance(row, dict)]

    def run(self, *, run_id: str, attempt: int | None = None) -> dict:
        """One run, as its latest attempt or exactly the attempt named."""
        argv = ["run", "view", run_id]
        if attempt is not None:
            argv += ["--attempt", str(attempt)]
        row = self._gh_json([*argv, "--json", RUN_FIELDS])
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
