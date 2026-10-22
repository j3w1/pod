"""Pure names, ownership, status and proposed argv over supplied display facts.

No native calls, writes, or lifecycle decisions belong in this module.
"""
from __future__ import annotations

import re
from pathlib import Path

from .errors import PodError
from .orca import DISPLAY_WORKTREE_LIMIT, _passable, display_worktree_argv
from .readiness import STATES

TITLE_LIMIT = 96
WORDS = {"implementer": "Implementer", "fixer": "Fixer", "investigator": "Investigator",
         "reviewer": "Reviewer", "follow_up_reviewer": "Follow-up reviewer"}
NOUNS = {"implementer": "Implementation", "fixer": "Correction", "investigator": "Investigation",
         "reviewer": "Review", "follow_up_reviewer": "Follow-up review"}
LIVE_STATES = ("reserved", "unresolved", "bound", "closed")
CODEX_REASON = ("Codex thread naming is interactive; the row relies on Orca's custom tab title "
                "(stablyai/orca#30037)")
WORKER_REASON = ("Pod sends no input to a worker tab: Orca marks a tab that receives input user-owned, which stops "
                 "its release or reuse; the row relies on the Task display name and Orca's custom tab title "
                 "(stablyai/orca#30037)")
HOST_SENDER = ("the host or another terminal sends it at the coordinator's idle prompt; "
               "the coordinator never types into its own tab")
LIMITATION = ("Orca shows CLI titles in the desktop row only from the release containing stablyai/orca#30037; "
              "until then a row can show the agent's own title (stablyai/orca#29975)")
_OMIT = frozenset("""status progress outcome pending running working ready completed complete done
    succeeded success failed failure blocked stopped active retained released approved authorized
    finished verified reviewed passing pass fail stale unverifiable disconnected
    authority authorization permission owner user system developer coordinator admin root model effort codex claude gpt
    opus fable sonnet haiku astra sol luna gemini openai anthropic low medium high xhigh max ultra minimal native default secret secrets
    password token credential credentials bearer api key terminal handle run task dispatch admission
    packet id uuid sha sha256 term ctx wtr req sk pk""".split()) | frozenset(STATES)


def _words(value: str) -> str:
    # A title admits ordinary words only, never identifier/path/credential-shaped
    # tokens. Do not split an unsafe token into seemingly safe pieces.
    words = []
    skip_value = False
    for token in value.split():
        token = token.strip(",.;:!?()[]{}\"'")
        if skip_value:
            skip_value = False
            continue
        if token.casefold() in {"model", "effort", "authority", "password", "token", "bearer", "secret",
                                "credential", "key", "id", "terminal", "handle", "run", "task", "dispatch",
                                "admission", "packet"}:
            skip_value = True
            continue
        if (re.fullmatch(r"[A-Za-z]{1,24}(?:-[A-Za-z]{1,24})?", token)
                and not any(re.fullmatch(r"[a-fA-F]{7,}", part) for part in token.split("-"))
                and all(part.casefold() not in _OMIT for part in token.split("-"))):
            words.append(token)
    return " ".join(words)


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit + 1].rsplit(" ", 1)[0] if " " in text[:limit + 1] else text[:limit]


def task_rejections(value: str) -> list[str]:
    """Tokens a task name refuses: anything but plain words (digits, paths, ids, hex runs)."""
    return [token for token in value.split()
            if not re.fullmatch(r"[A-Za-z]+(?:['-][A-Za-z]+)*", token)
            or any(re.fullmatch(r"[a-fA-F]{7,}", part) for part in re.split(r"['-]", token))]


def validate_record(value: object, admissions: dict | None = None) -> dict:
    task = value.get("task") if isinstance(value, dict) else None
    rejected = task_rejections(task) if isinstance(task, str) else []
    if rejected:
        raise PodError("invalid_display_name", "Display task takes plain words only; refused: "
                       + ", ".join(token[:24] for token in rejected[:4]))
    if (not isinstance(value, dict) or set(value) != {"task", "continued", "host_tab"}
            or not isinstance(value["task"], str) or not 1 <= len(value["task"]) <= 48
            or not value["task"].split() or value["task"] != " ".join(value["task"].split())
            or not isinstance(value["continued"], list) or len(value["continued"]) > 32
            or any(not isinstance(key, str) or not key for key in value["continued"])
            or len(set(value["continued"])) != len(value["continued"])
            or type(value["host_tab"]) is not bool
            or admissions is not None and any(key != "coordinator" and key not in admissions
                                              for key in value["continued"])):
        raise PodError("invalid_display_name", "Display needs plain task words (at most 48 characters), existing continued admissions and host_tab")
    return value


def validate_facts(value: object) -> None:
    if (not isinstance(value, dict) or set(value) != {"kind", "job", "round", "own_worktree", "worktree_name"}
            or not isinstance(value["kind"], str) or value["kind"] not in WORDS
            or type(value["own_worktree"]) is not bool
            or not isinstance(value["job"], str) or not 1 <= len(value["job"]) <= 40
            or _words(value["job"]) != value["job"]
            or value["round"] is not None and (type(value["round"]) is not int or value["round"] < 1)
            or value["worktree_name"] is not None and
            (not isinstance(value["worktree_name"], str) or not 1 <= len(value["worktree_name"]) <= 44
             or not re.fullmatch(r"[A-Za-z -]+(?: [2-9][0-9]*| 1[0-9]+)?", value["worktree_name"]))):
        raise PodError("state_unsupported", "Admission display facts are malformed")


def ordered(state: dict) -> list[dict]:
    # Stable insertion order is the fallback for genuine earlier rows without a sequence.
    return sorted((row for row in state.get("admissions", {}).values() if row.get("state") in LIVE_STATES),
                  key=lambda row: row.get("admitted_seq", 0))


def placement(row: dict) -> str | None:
    return ((row.get("recovery") or {}).get("placement_binding") or {}).get("path")


def task_path(state: dict) -> str | None:
    return (((state.get("checkpoint") or {}).get("worktree")) or {}).get("path")


def kind(body: dict, state: dict) -> str:
    if body.get("role") == "implement":
        corrections = {ob["id"] for ob in (state.get("checkpoint") or {}).get("obligations", [])
                       if ob.get("kind") == "correction"}
        return "fixer" if corrections.intersection(body.get("serves", [])) else "implementer"
    if body.get("role") == "review":
        return "follow_up_reviewer" if body.get("delta_from") else "reviewer"
    return "investigator"


def job(body: dict, worker_kind: str, task: str = "") -> str:
    value = _words(body.get("purpose", ""))
    if task:
        phrase = r"[\s-]+".join(re.escape(word) for word in re.split(r"[\s-]+", task) if word)
        value = re.sub(r"(?<![\w-])" + phrase + r"(?![\w-])", " ", value, flags=re.I)
        value = " ".join(word.strip("-") for word in value.split() if word.strip("-"))
    value = value or NOUNS[worker_kind]
    return _cut(value[0].upper() + value[1:], 40)


def admission_facts(state: dict, body: dict, path: str | None, naming: dict | None = None) -> dict:
    worker_kind = kind(body, state)
    rows = ordered(state)
    reviews = [row for row in rows if row.get("role") == "review"]
    round_number = (len(reviews) + 1 if worker_kind in ("reviewer", "follow_up_reviewer")
                    else max(1, len(reviews)) if worker_kind == "fixer" else None)
    plain_task = ((state.get("checkpoint") or {}).get("display") or {}).get("task", "")
    worker_job = job(body, worker_kind, plain_task)
    own = bool(path and task_path(state) and path != task_path(state))
    name = None
    if own:
        prior = next((row["display"]["worktree_name"] for row in rows
                      if placement(row) == path and (row.get("display") or {}).get("worktree_name")), None)
        used = {row["display"]["worktree_name"].casefold() for row in rows if placement(row) != path
                and (row.get("display") or {}).get("worktree_name")}
        # Pod names only a worktree Orca reports unnamed and not main; anything else is the host's.
        unnamed = bool(naming and naming.get("mode") == "automatic" and naming.get("main") is False)
        name = prior or (worker_job if unnamed else None)
        ordinal = 2
        while not prior and name and name.casefold() in used:
            suffix = f" {ordinal}"
            name = _cut(worker_job, 44 - len(suffix)) + suffix
            ordinal += 1
    return {"kind": worker_kind, "job": worker_job, "round": round_number,
            "own_worktree": own, "worktree_name": name}


def facts(state: dict, row: dict) -> dict:
    if "display" in row:
        return row["display"]
    return {"kind": {"implement": "implementer", "review": "reviewer", "investigate": "investigator"}[row["role"]],
            "job": "", "round": None, "own_worktree": bool(placement(row) and task_path(state) and placement(row) != task_path(state)),
            "worktree_name": None}


def round_of(state: dict, row: dict) -> int | None:
    """Per-objective review ordinal in admission order; a fixer takes the latest earlier review's round."""
    kind = facts(state, row)["kind"]
    if kind not in ("reviewer", "follow_up_reviewer", "fixer"):
        return None
    seq = row.get("admitted_seq", 0)
    earlier = sum(1 for other in ordered(state) if other.get("role") == "review"
                  and (other.get("admitted_seq", 0), other["admission_id"]) < (seq, row["admission_id"]))
    return earlier + 1 if kind != "fixer" else max(1, earlier)


def _title(state: dict, row: dict, review_count: int | None = None) -> str:
    display = facts(state, row)
    value = WORDS[display["kind"]]
    if review_count is None:
        review_count = sum(r.get("role") == "review" for r in ordered(state))
    number = round_of(state, row)
    if number is not None and review_count > 1:
        value += f" · round {number}"
    if not display["own_worktree"] and display["job"]:
        value += " · " + display["job"]
    return value


def worker_titles(state: dict) -> dict[str, str]:
    titles, used = {}, {}
    rows = ordered(state)
    latest = {(row.get("native_binding") or {}).get("terminalHandle"): row["admission_id"] for row in rows
              if (row.get("native_binding") or {}).get("terminalHandle")}
    continued = ((state.get("checkpoint") or {}).get("display") or {}).get("continued", [])
    review_count = sum(row.get("role") == "review" for row in rows)
    for row in rows:
        key = row["admission_id"]
        suffix = " (continued)" if row.get("reuse_of") or key in continued else ""
        value = _cut(_title(state, row, review_count), TITLE_LIMIT - len(suffix))
        path = placement(row) or task_path(state)
        taken = used.setdefault(path, set())
        handle = (row.get("native_binding") or {}).get("terminalHandle")
        if value + suffix in taken:
            worker_job = facts(state, row)["job"]
            if worker_job and not value.endswith(" · " + worker_job):
                value = _cut(value, TITLE_LIMIT - len(suffix) - len(worker_job) - 3) + " · " + worker_job
        base, ordinal = value, 2
        while value + suffix in taken:
            number = f" {ordinal}"
            value = _cut(base, TITLE_LIMIT - len(suffix) - len(number)) + number
            ordinal += 1
        titles[key] = value + suffix
        if not handle or latest.get(handle) == key:
            taken.add(titles[key])
    return titles


def worker_title(state: dict, row: dict) -> str:
    # Deferred starts still have useful words, but consume neither rounds nor sibling names.
    titles = worker_titles(state)
    return titles[row["admission_id"]] if row["admission_id"] in titles else _title(state, row)


def rename(value: str, handle: str | None) -> list[str] | None:
    return ["orca", "terminal", "rename", "--terminal", handle, "--title", value, "--json"] if handle else None


def session_rename(value: str, handle: str | None, agent: str | None, *, coordinator=False) -> dict:
    """Only a Claude coordinator tab gets a provider rename, sent by its host; worker tabs get none."""
    if not coordinator:
        return {"session_rename": None, "session_reason": WORKER_REASON}
    if not handle:
        return {"session_rename": None, "session_reason": "the coordinator tab is not readable"}
    if agent != "claude":
        return {"session_rename": None, "session_reason": CODEX_REASON}
    text = "/rename " + value
    return {"session_rename": {"text": text, "sender": "host",
            "wait": ["orca", "terminal", "wait", "--terminal", handle, "--for", "tui-idle", "--timeout-ms", "60000", "--json"],
            "command": ["orca", "terminal", "send", "--terminal", handle, "--text", text, "--enter", "--json"]},
            "session_reason": HOST_SENDER}


def issue(state: dict) -> tuple[int | None, str | None]:
    checkpoint = state.get("checkpoint") or {}
    source = checkpoint.get("objective_source") or {}
    repository = (checkpoint.get("worktree") or {}).get("repository")
    number = source.get("number")
    if type(number) is not int or number < 1:
        return None, None
    source_repo = source.get("repository")
    return number, source_repo if source_repo and source_repo.casefold() != (repository or "").casefold() else None


def pr(state: dict) -> int | None:
    provider = ((state.get("checkpoint") or {}).get("delivery") or {}).get("provider") or {}
    value = provider.get("pr")
    return value if type(value) is int and value > 0 else None


def task_name(state: dict, observed: dict | None) -> str | None:
    if observed and observed.get("displayNameMode") == "fixed":
        return observed.get("displayName")
    value = ((state.get("checkpoint") or {}).get("display") or {}).get("task")
    if not value:
        return None
    number, other = issue(state)
    return value + (f" ({other}#{number})" if other else f" (#{number})" if number else "")


def coordinator_title(state: dict, terminal: dict | None, worktree: dict | None) -> str | None:
    if terminal and terminal.get("worktreePath") == task_path(state):
        value = "Coordinator"
    else:
        name = task_name(state, worktree)
        if not name:
            return None
        value = "Coordinator · " + name
    if state.get("continues") or "coordinator" in (((state.get("checkpoint") or {}).get("display") or {}).get("continued", [])):
        value += " (continued)"
    return value


def worktree_status(state: dict, path: str) -> tuple[str, str]:
    from .worker_tabs import retention_reason
    rows = [row for row in ordered(state) if placement(row) == path]
    working = next((row for row in rows if facts(state, row)["kind"] in ("implementer", "fixer", "investigator")
                    and row.get("report") is None), None)
    if working:
        return "in-progress", worker_title(state, working) + " working"
    reviewing = next((row for row in rows if facts(state, row)["kind"] in ("reviewer", "follow_up_reviewer")
                      and row.get("report") is None), None)
    if reviewing:
        return "in-review", worker_title(state, reviewing) + " working"
    assurance = any(ob.get("kind") == "assurance" and ob.get("state") not in ("satisfied", "withdrawn")
                    for ob in (state.get("checkpoint") or {}).get("obligations", []))
    if any(row.get("report") is None or retention_reason(state, row, None) in ("corrections", "delta review")
           or facts(state, row)["kind"] in ("implementer", "fixer") and assurance for row in rows):
        return "in-review", "Waiting on review"
    return "completed", "Report recorded"


def worktree_command(state: dict, row: dict, *, first=False) -> dict:
    display, path = facts(state, row), placement(row)
    if not display["worktree_name"] or not path:
        return {"worktree_status": None, "worktree_command": None}
    status, comment = worktree_status(state, path)
    command = ["orca", "worktree", "set", "--worktree", "path:" + path]
    if first:
        command += ["--display-name", display["worktree_name"], "--parent-worktree", "path:" + task_path(state)]
        number, other = issue(state)
        if number and not other:
            command += ["--issue", str(number)]
        if pr(state):
            command += ["--pr", str(pr(state))]
    command += ["--workspace-status", status, "--comment", comment, "--json"]
    return {"worktree_status": status, "worktree_command": command}


def admission_output(state: dict, row: dict) -> dict:
    from .worker_tabs import rename as naming
    started = row.get("state") == "bound"
    handle = (row.get("native_binding") or {}).get("terminalHandle") if started else None
    title = worker_title(state, row)
    prior = [r for r in ordered(state) if placement(r) == placement(row) and (r.get("display") or {}).get("worktree_name")]
    output = {**naming(title, handle, started=started),
              **session_rename(title, handle, (row.get("request") or {}).get("agent")),
              **worktree_command(state, row, first=bool(prior and prior[0]["admission_id"] == row["admission_id"]))}
    if not started:
        output["worktree_command"] = None
    if row.get("state") in ("reserved", "unresolved"):
        output["rename_reason"] = "worker start is not verified"
    return output


def packet_title(state: dict | None, body: dict) -> str:
    if state is None:
        return "Follow-up reviewer" if body.get("role") == "review" and body.get("delta_from") else {
            "implement": "Implementer", "review": "Reviewer", "investigate": "Investigator"}[body["role"]]
    row = {"admission_id": "display-preview", "role": body["role"], "state": "reserved",
           "admitted_seq": max((r.get("admitted_seq", 0) for r in ordered(state)), default=0) + 1,
           "recovery": {"placement_binding": body.get("placement") or body.get("worktree")},
           "display": admission_facts(state, body, (body.get("placement") or body.get("worktree") or {}).get("path"))}
    preview = {**state, "admissions": {**state.get("admissions", {}), row["admission_id"]: row}}
    return worker_title(preview, row)


def _inventory(readback: dict | None, key: str) -> list[dict] | None:
    result = readback.get("result") if isinstance(readback, dict) else None
    page = result.get("page") if isinstance(result, dict) else None
    if (not isinstance(result, dict) or not isinstance(result.get(key), list)
            or result.get("truncated") is True
            or page is not None and (not isinstance(page, dict) or page.get("hasMore") is not False)
            or any(not isinstance(row, dict) for row in result[key])):
        return None
    if key == "worktrees":
        rows, scope = result[key], result.get("hostScope")
        if (len(rows) > DISPLAY_WORKTREE_LIMIT or type(result.get("totalCount")) is not int
                or result["totalCount"] != len(rows) or result.get("truncated") is not False
                or not isinstance(scope, dict) or not isinstance(scope.get("hostIds"), list)
                or not scope["hostIds"] or not all(_passable(host) and host for host in scope["hostIds"])
                or len(set(scope["hostIds"])) != len(scope["hostIds"])
                or "local" not in scope["hostIds"] or scope.get("omittedHostIds") != []):
            return None
        paths, identities = set(), set()
        for row in rows:
            path, identity = row.get("path"), row.get("id")
            if (not _passable(path) or not path or not Path(path).is_absolute()
                    or path.startswith("//") or str(Path(path)) != path or ".." in Path(path).parts
                    or not _passable(identity) or not identity or path in paths or identity in identities):
                return None
            git = row.get("git")
            if git is not None and (not isinstance(git, dict) or git.get("path") != path):
                return None
            paths.add(path)
            identities.add(identity)
    return result[key]


def projection(state: dict, terminals_read: dict | None, worktrees_read: dict | None,
               version_read: dict | None) -> dict:
    """Compare only complete supplied backend readbacks; desktop truth needs a screenshot."""
    terminals = _inventory(terminals_read, "terminals")
    worktrees = _inventory(worktrees_read, "worktrees")
    by_path = {row.get("path"): row for row in worktrees or []}
    by_id = {row.get("id"): row.get("path") for row in worktrees or []}
    by_handle = {row.get("handle"): row for row in terminals or []}
    repeat_terminal = ["orca", "terminal", "list", "--json"]
    repeat_worktree = ["orca", *display_worktree_argv()]
    rows = []

    def add(layer, owner, target, expected, observed, command, repeat, *, reason=None, session=None):
        readable = observed is not None and all(key in observed for key in expected)
        # A null link means Pod has no known link to set, rather than permission to clear one.
        compared = {key: value for key, value in expected.items() if value is not None}
        verdict = ("host" if owner == "host" else "unreadable" if not readable or reason else
                   "matches" if all(observed[key] == value for key, value in compared.items()) else "differs")
        row = {"layer": layer, "owner": owner, "target": target, "expected": expected,
               "observed": observed, "verdict": verdict,
               "command": command if verdict == "differs" else None}
        if verdict == "unreadable":
            row.update(reason=reason or "repeat the native read", read_command=repeat)
        elif owner == "host":
            row["reason"] = "host names win"
        if layer.endswith("tab"):
            row.update(session or {"session_rename": None})
        rows.append(row)

    def observed_worktree(row):
        if row is None:
            return None
        value = {}
        for source, dest in (("displayName", "name"), ("workspaceStatus", "status"), ("comment", "comment")):
            if source in row:
                value[dest] = row[source]
        if "parentWorktreeId" in row:
            parent = row["parentWorktreeId"]
            if parent is None or parent in by_id:
                value["parent"] = by_id.get(parent)
        for source, dest in (("linkedIssue", "issue"), ("linkedPR", "pr")):
            if source in row:
                linked = row[source]
                if linked is None or type(linked) is int:
                    value[dest] = linked
                elif isinstance(linked, dict) and type(linked.get("number")) is int:
                    value[dest] = linked["number"]
        return value

    path = task_path(state)
    task = by_path.get(path)
    mode = (task or {}).get("displayNameMode")
    filled = task_name(state, None)
    # A name Orca reports fixed is the host's unless it is exactly the name Pod fills in.
    owner = ("host" if (task or {}).get("isMainWorktree") is True
             or mode == "fixed" and (not filled or (task or {}).get("displayName") != filled) else "pod")
    name = task_name(state, task) if owner == "host" else filled
    number, other = issue(state)
    expected = {"name": name}
    command = ["orca", "worktree", "set", "--worktree", "path:" + path] if path else None
    if command and name:
        command += ["--display-name", name]
        if number and not other:
            command += ["--issue", str(number)]
            expected["issue"] = number
        command += ["--json"]
    else:
        command = None
    reason = ("repeat the native read" if mode not in ("automatic", "fixed") else
              "record display.task" if not name else None)
    add("task_worktree", owner, "path:" + path if path else None, expected,
        observed_worktree(task), command, repeat_worktree, reason=reason)

    coordinator = by_handle.get(state.get("owner"))
    title = coordinator_title(state, coordinator, task)
    host_tab = ((state.get("checkpoint") or {}).get("display") or {}).get("host_tab", False)
    add("coordinator_tab", "host" if host_tab else "pod", state.get("owner"), {"title": title},
        {"title": coordinator["title"]} if coordinator and "title" in coordinator
        and coordinator.get("connected") is True and isinstance(coordinator.get("worktreePath"), str) else None,
        rename(title, state.get("owner")) if title else None, repeat_terminal,
        reason="record display.task" if not title else None,
        session=session_rename(title, state.get("owner") if coordinator else None,
                               (coordinator or {}).get("agentIdentity"), coordinator=True) if title else
        {"session_rename": None, "session_reason": "record display.task"})

    named, hosted = {}, {}
    for row in ordered(state):
        if facts(state, row)["worktree_name"]:
            named.setdefault(placement(row), row)
        elif facts(state, row)["own_worktree"] and placement(row):
            hosted.setdefault(placement(row), row)
    for worker_path in hosted:
        if worker_path in named or worktrees is not None and worker_path not in by_path:
            continue
        add("worker_worktree", "host", "path:" + worker_path, {}, observed_worktree(by_path.get(worker_path)),
            None, repeat_worktree)
    for worker_path, row in named.items():
        # A complete inventory proves a removed worktree absent; it is not drift.
        if worktrees is not None and worker_path not in by_path:
            continue
        status, comment = worktree_status(state, worker_path)
        expected = {"name": facts(state, row)["worktree_name"], "parent": path,
                    "issue": number if not other else None, "pr": pr(state), "status": status, "comment": comment}
        add("worker_worktree", "pod", "path:" + worker_path, expected,
            observed_worktree(by_path.get(worker_path)),
            worktree_command(state, row, first=True)["worktree_command"], repeat_worktree)

    latest = {}
    for row in ordered(state):
        handle = (row.get("native_binding") or {}).get("terminalHandle")
        if handle and row.get("state") in ("bound", "closed") and handle != state.get("owner"):
            latest[handle] = row
    for handle, row in latest.items():
        terminal = by_handle.get(handle)
        # A complete inventory can prove absence; a failed inventory cannot.
        if terminals is not None and (terminal is None or terminal.get("connected") is False):
            continue
        title = worker_title(state, row)
        add("worker_tab", "pod", handle, {"title": title},
            {"title": terminal["title"]} if terminal and terminal.get("connected") is True and "title" in terminal else None,
            rename(title, handle), repeat_terminal,
            session=session_rename(title, handle, (row.get("request") or {}).get("agent")))

    version = (version_read or {}).get("version")
    parsed = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version or "")
    limitation = LIMITATION if parsed and tuple(map(int, parsed.groups())) <= (1, 4, 229) else None
    if parsed is None:
        limitation = "Orca version unreadable; repeat orca --version"
    drift = any(row["owner"] == "pod" and row["verdict"] in ("differs", "unreadable") for row in rows)
    verdict = "drift" if drift else "orca_limitation" if limitation else "backend_matches"
    actions = []
    for row in rows:
        if row["command"]:
            actions.append({"command": row["command"]})
        elif row["verdict"] == "unreadable":
            actions.append({"command": row["read_command"], "reason": row["reason"]})
        # Only the coordinator row can carry a provider rename; its host sends it at an idle prompt.
        if (row.get("session_rename") and row["layer"] == "coordinator_tab" and row["owner"] == "pod"
                and row["verdict"] == "differs"):
            actions.append({"session_rename": row["session_rename"],
                            "guard": "the host sends it once, only after the exact terminal is tui-idle"})
    if parsed is None:
        actions.append({"command": ["orca", "--version"]})
    return {"rows": rows, "orca": {"version": version, "limitation": limitation},
            "desktop": "NOT_VERIFIED", "verdict": verdict, "next_action": actions}
