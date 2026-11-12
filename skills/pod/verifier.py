"""The host's verifier hook: a host that relays the person's consent can vouch for one merge.

Pod sends one JSON request on standard input and reads one JSON verdict. Anything other than a
clear, matching "verified" is not an authorization.
"""

import json
import subprocess

SCHEMA = "pod-authorization/v1"


def ask(command: list[str], *, objective: str, repository: str, target: str, pr: int, candidate: str, tree: str) -> dict | None:
    """The host's verdict for this exact candidate, or None when it did not verify it."""
    request = {"schema": SCHEMA, "action": "merge", "objective": objective, "repository": repository,
               "target": target, "pr": pr, "candidate": candidate, "tree": tree}
    try:
        done = subprocess.run(command, input=json.dumps(request).encode(), capture_output=True, timeout=60, cwd="/")
        verdict = json.loads(done.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if (not isinstance(verdict, dict) or verdict.get("schema") != SCHEMA or verdict.get("verified") is not True
            or any(verdict.get(name) != request[name] for name in ("action", "repository", "target", "pr", "candidate"))):
        return None
    return {"reference": str(verdict.get("reference") or "")[:200]}
