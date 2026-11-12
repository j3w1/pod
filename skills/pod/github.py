"""Reads from GitHub through `gh`, the one merge and the one issue update Pod performs.

Every outcome reported here was read from GitHub. A command that fails or cannot be parsed
yields None or a refusal, never an assumed result.
"""

import json
import subprocess

from .errors import PodError

GREEN = ("pass", "skipping")


def gh(*args: str, stdin: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["gh", *args], capture_output=True, input=None if stdin is None else stdin.encode(),
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PodError("github_unavailable", f"gh {args[0]} {args[1] if len(args) > 1 else ''} did not complete: {exc}",
                       fix="check `gh auth status` and the network, then retry") from exc


def _json(*args: str):
    done = gh(*args)
    if done.returncode != 0:
        return None, done.stderr.decode(errors="replace").strip()[:300]
    try:
        return json.loads(done.stdout), None
    except ValueError:
        return None, "gh returned text that is not JSON"


def issue(repository: str, number: int) -> dict:
    value, problem = _json("issue", "view", str(number), "--repo", repository, "--json", "number,title,body,state,url")
    if value is None:
        raise PodError("issue_unreadable", f"Issue {repository}#{number} could not be read: {problem}",
                       fix="check the issue number and `gh auth status`")
    return value


def pull_request(repository: str, selector: str) -> dict | None:
    """The pull request by number or head branch, or None when there is none or it cannot be read."""
    value, _ = _json("pr", "view", str(selector), "--repo", repository, "--json",
                     "number,state,isDraft,headRefOid,headRefName,baseRefName,mergeCommit,mergedAt,mergeStateStatus,url")
    return value


def checks(repository: str, number: int, required: list[str] | None = None) -> dict:
    """Check results on the pull request's current head.

    `required` names the project's own list when it keeps one; otherwise GitHub's required
    checks decide. Returns {"state": green | pending | failing | none | unknown, "rows": [...]}.
    """
    args = ["pr", "checks", str(number), "--repo", repository, "--json", "name,bucket,state"]
    if required is None:
        args.append("--required")
    done = gh(*args)
    text = done.stdout.decode(errors="replace").strip()
    if not text:
        message = done.stderr.decode(errors="replace").lower()
        if "no required checks" in message or "no checks reported" in message:
            return {"state": "none" if required is None else "pending", "rows": []}
        return {"state": "unknown", "rows": [], "problem": message.strip()[:200]}
    try:
        rows = json.loads(text)
    except ValueError:
        return {"state": "unknown", "rows": [], "problem": "gh returned text that is not JSON"}
    if required is not None:
        present = {row["name"] for row in rows}
        rows = [row for row in rows if row["name"] in required] + [
            {"name": name, "bucket": "pending", "state": "MISSING"} for name in required if name not in present]
    rows = [{"name": row["name"], "bucket": row.get("bucket"), "state": row.get("state")} for row in rows]
    if not rows:
        return {"state": "none", "rows": []}
    if all(row["bucket"] in GREEN for row in rows):
        return {"state": "green", "rows": rows}
    if any(row["bucket"] in ("fail", "cancel") for row in rows):
        return {"state": "failing", "rows": rows}
    return {"state": "pending", "rows": rows}


def merge(repository: str, number: int, head: str, method: str) -> tuple[bool, str]:
    """Ask GitHub to merge only if the head is still exactly `head`. The caller reads the outcome back."""
    done = gh("pr", "merge", str(number), "--repo", repository, f"--{method}", "--match-head-commit", head, timeout=120)
    return done.returncode == 0, done.stderr.decode(errors="replace").strip()[:300]


def edit_issue(repository: str, number: int, body: str) -> bool:
    return gh("issue", "edit", str(number), "--repo", repository, "--body-file", "-", stdin=body).returncode == 0


def comment_issue(repository: str, number: int, text: str) -> bool:
    return gh("issue", "comment", str(number), "--repo", repository, "--body-file", "-", stdin=text).returncode == 0
