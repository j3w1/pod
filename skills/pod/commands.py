"""The coordinator's commands: open, dispatch, accept, record, status, resume and drop.

Each takes a handful of arguments and holds the objective's lock while it runs. Each asks the
coordinator only for what software cannot know: the plan, a decision, a person's words.
Everything else is read from Orca, Git and GitHub at the moment it is needed.
"""

import hashlib
import json
from pathlib import Path
import re
import subprocess

from . import github, gitio, issue, log, orca, proof, routefile
from .errors import PodError
from .state import ROLES, State, fold

RESULT_FORMAT = ('{"outcome": "succeeded" or "failed", "commit": "<full id of your result commit, or null>", '
                 '"checks": [{"command": "<what you ran>", "exit": 0}], "summary": "<one or two sentences>"}')
REVIEW_FORMAT = ('{"outcome": "succeeded" or "failed", "verdict": "pass" or "fail", "findings": [{"path": "<file>", '
                 '"required": true, "text": "<what is wrong and why>"}], "summary": "<one or two sentences>"}')


# --- finding the objective -------------------------------------------------------------------------

def locate(cwd: Path, objective: str | None) -> tuple[Path, Path, str | None]:
    """The log, the worktree root and the GitHub repository for the objective this command means."""
    root = gitio.root(cwd)
    repository, slug = gitio.repository(root)
    if objective:
        return log.path(repository, objective), root, slug
    here, finished = [], []
    for candidate in log.every():
        if candidate.parent.name != repository:
            continue
        state = fold(log.read(candidate))
        if state.entries and state.objective.get("worktree") == str(root):
            (finished if state.closed else here).append((state.entries[-1]["at"], candidate))
    if len(here) == 1 or (not here and finished):
        return max(here or finished)[1], root, slug         # nothing open: the one most recently closed here
    names = ", ".join(path.stem for _, path in here)
    raise PodError("objective_unclear", "No objective names this worktree." if not here
                   else f"Several open objectives name this worktree: {names}.",
                   fix="pass --objective ID" if here else "run `pod open`, or pass --objective ID")


def _live(state: State) -> dict:
    """Live state per assignment, read from Orca now. Unknown counts as live."""
    run = state.objective.get("run")
    pending = [row for row in state.assignments.values() if not row.get("void") and not state.result(row["id"])]
    if not pending or not run:
        return {}
    rows = {row["dispatch"]: row for row in orca.workers(run)}
    return {row["id"]: (orca.live(rows[row["dispatch"]]) if row.get("dispatch") in rows else not row.get("dispatch") or None)
            for row in pending}


# --- open ------------------------------------------------------------------------------------------

def open_(cwd: Path, *, source: str | None = None, text: str | None = None, objective: str | None = None,
          target: str = "main", branch: str | None = None, pr: int | None = None, run: str | None = None,
          continues: str | None = None, criteria: list[str] | None = None, delivery: bool = True) -> dict:
    root = gitio.root(cwd)
    repository, slug = gitio.repository(root)
    if bool(source) == bool(text):
        raise PodError("source_required", "Name exactly one source.", fix="pass --issue N or --text 'the objective'")
    rows = [{"text": item} for item in criteria or []]
    if source:
        where, number = issue.reference(source, slug)
        document = github.issue(where, number)
        issue.refuse_undeliverable(document["title"])
        described = {"kind": "issue", "repository": where, "number": number, "title": document["title"],
                     "digest": issue.digest(document["body"])}
        rows = rows or issue.criteria(document["body"]) or [{"text": document["title"]}]
        objective = objective or f"issue-{number}"
    else:
        described = {"kind": "text", "text": text}
        rows = rows or [{"text": text.strip().splitlines()[0][:300]}]
        objective = objective or (re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32].strip("-") + "-"
                                  + hashlib.sha256(text.encode()).hexdigest()[:6])
    with log.writer(log.path(repository, objective), create=True) as writer:
        if writer.entries:
            state = fold(writer.entries)
            if state.objective.get("source") != described:
                raise PodError("objective_exists", f"Objective {objective} already exists with a different source.",
                               fix="choose another --objective id, or continue it with `pod resume`")
            return view(root, state, remote=False)
        if run:
            busy = [row["dispatch"] for row in orca.workers(run) if orca.live(row)]
            if busy:
                raise PodError("run_busy", f"Run {run} has workers this objective did not start: {', '.join(busy)}.",
                               fix="wait for them or stop them through Orca, then open again")
        head = gitio.commit(root)
        writer.append(
            {"kind": "objective", "id": objective, "source": described, "repository": slug, "target": target,
             "worktree": str(root), "branch": branch or gitio.branch(root), "run": run, "pr": pr, "continues": continues,
             **({} if delivery else {"delivery": "none"}), "base": gitio.merge_base(root, head, f"origin/{target}") or head},
            *({"kind": "criterion", "id": f"c{index}", "text": row["text"], "state": "open",
               **({"line": row["line"]} if "line" in row else {})} for index, row in enumerate(rows, 1)))
        return view(root, fold(writer.entries), remote=False)


# --- dispatch --------------------------------------------------------------------------------------

def packet(state: State, assignment: dict, spec: str) -> str:
    role, paths_ = assignment["role"], assignment.get("paths") or []
    lines = [f"# Assignment {state.id} {assignment['id']} ({role})", "", spec.strip(), "", "## Boundaries", "",
             f"- Base commit: {assignment['base']}"]
    if role == "implement":
        lines += ["- Change only: " + (", ".join(paths_) if paths_ else "what the task needs"),
                  "- Commit your work on the branch you are given. Do not push, merge or open a pull request."]
    elif role == "review":
        lines += [f"- Review commit {assignment['base']}, these paths: " + ", ".join(paths_),
                  "- Report every finding in one pass. Do not change any file; the writer makes corrections.",
                  "- Mark a finding required only when the change must not merge without it."]
        if assignment.get("context"):
            lines.append("- Read as context: " + ", ".join(assignment["context"]))
    else:
        lines.append("- Read only. Change nothing.")
    lines += ["- Report what you observed. If something could not be run or read, say so; never report it as passed.", "",
              "## Result", "", "Finish through Orca as its instructions say. End your final message body with one JSON object:",
              "", REVIEW_FORMAT if role == "review" else RESULT_FORMAT]
    return "\n".join(lines) + "\n"


def dispatch(cwd: Path, objective: str | None, *, role: str | None = None, title: str | None = None, spec: str | None = None,
             route: str | None = None, paths_: list[str] | None = None, commit: str | None = None,
             context: list[str] | None = None, worktree: str = "current", retry: str | None = None,
             name: str | None = None) -> dict:
    target, root, _ = locate(cwd, objective)
    with log.writer(target) as writer:
        state = fold(writer.entries)
        if state.closed:
            raise PodError("objective_closed", f"Objective {state.id} is closed or dropped.", fix="open a new objective")
        if retry:
            return _settle_start(writer, state, retry)
        if role not in ROLES or not title or not spec:
            raise PodError("dispatch_incomplete", "A dispatch needs a role, a title and the task text.",
                           fix="pass --role implement|review|investigate, --title and --spec or --spec-file")
        preferences = routefile.load()
        route = route or preferences.pinned or preferences.preferred
        if not route:
            raise PodError("route_required", "No Pin or Preferred route is set.",
                           fix="pass --route, one of: " + (", ".join(preferences.enabled()) or "none enabled; run `pod models`"))
        preferences.check(route)
        paths_ = [item.strip().rstrip("/") for item in paths_ or [] if item.strip()]
        base = gitio.commit(root, commit or "HEAD")
        if role == "review":
            against = gitio.merge_base(root, base, f"origin/{state.objective['target']}") or gitio.merge_base(
                root, base, state.objective["target"])
            if not against:
                raise PodError("target_unknown", f"The target branch {state.objective['target']} is not in this repository.",
                               fix="fetch it, then dispatch the review again")
            changed = gitio.changed(root, against, base)
            paths_ = [item for item in changed if not paths_ or gitio.under(item, paths_)]
            if not paths_:
                raise PodError("nothing_to_review", f"Commit {base[:12]} changes none of the named paths against the target.",
                               fix="name the commit and paths that hold the change")
        elif role == "implement":
            live = _live(state)
            for other in state.assignments.values():
                if (other["role"] == "implement" and live.get(other["id"]) is not False and other["id"] in live
                        and gitio.overlap(paths_, other.get("paths") or [])):
                    raise PodError("paths_overlap", f"Assignment {other['id']} is still running and may change the same files.",
                                   fix=f"wait for {other['id']}, stop it through Orca, or declare paths that do not overlap")
        run = state.objective.get("run")
        if not run:
            run = orca.run_create(f"{state.id}: {state.objective['source'].get('title') or state.objective['source'].get('text', '')}")
            writer.append({"kind": "objective", "run": run})
        ident = state.next_id("a", state.assignments)
        assignment = {"kind": "assignment", "id": ident, "role": role, "title": title, "paths": paths_, "base": base,
                      "route": route, "request": orca.request_id(state.id, ident, 1), "worktree": worktree,
                      **({"context": context} if context else {})}
        text = packet(state, assignment, spec)
        assignment["packet"] = hashlib.sha256(text.encode()).hexdigest()
        writer.append(assignment)
        selector = f"path:{root}" if worktree == "current" else worktree
        try:
            started = orca.start(run=run, spec=text, title=f"{title} [{state.id}:{ident}]", worktree=selector,
                                 route=routefile.split(route), request=assignment["request"], name=name)
        except PodError as exc:
            if exc.code == "orca_unknown":
                exc.fix = f"run `pod dispatch --retry {ident}`; it reads what Orca did and never starts a second worker"
            else:
                writer.append({"kind": "assignment", "id": ident, "void": True, "refused": exc.code})
            raise
        return _started(writer, ident, started, route)


def _started(writer, ident: str, started: dict, route: str) -> dict:
    effective = started.get("effective") or {}
    effective = "/".join(str(effective.get(key)) for key in ("agent", "model", "effort")) if effective else None
    writer.append({"kind": "assignment", "id": ident, "task": started["task"], "dispatch": started["dispatch"],
                   **({"effective": effective} if effective and effective != route else {})})
    result = {"assignment": ident, "task": started["task"], "dispatch": started["dispatch"], "route": route}
    if effective and effective != route:
        result["route_mismatch"] = f"asked for {route}, Orca started {effective}"
    if started.get("replayed"):
        result["replayed"] = True
    result["next"] = f"Orca wakes you when it reports; then `pod accept {ident}`"
    return result


def _settle_start(writer, state: State, ident: str) -> dict:
    """After a lost reply: learn from Orca whether the worker started. Never start it again here."""
    row = state.assignments.get(ident)
    if not row:
        raise PodError("assignment_unknown", f"There is no assignment {ident}.", fix="run `pod status` for the ids")
    if row.get("dispatch"):
        return {"assignment": ident, "task": row["task"], "dispatch": row["dispatch"], "already": True}
    marker = f"[{state.id}:{ident}]"
    found = next((key for key, task in orca.tasks(state.objective["run"]).items() if marker in (task["title"] or "")), None)
    if found:
        worker = next((item for item in orca.workers(state.objective["run"]) if item["task"] == found), {})
        return _started(writer, ident, {"task": found, "dispatch": worker.get("dispatch")}, row["route"])
    receipt = orca.call("orchestration", "request-show", "--request", row["request"])
    if receipt.get("state") == "pending":
        raise PodError("start_pending", f"Orca is still starting {ident}.", fix=f"run `pod dispatch --retry {ident}` again shortly")
    writer.append({"kind": "assignment", "id": ident, "void": True, "refused": "start_not_recorded"})
    return {"assignment": ident, "void": True, "next": "Orca has no task for it; dispatch the work again as a new assignment"}


# --- accept ----------------------------------------------------------------------------------------

def parse_result(body: str | None) -> dict:
    """The last JSON object in the worker's message that states an outcome. Anything else is not a result."""
    text = body or ""
    for start in reversed([match.start() for match in re.finditer(r"\{", text)]):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            continue
        if isinstance(value, dict) and "outcome" in value:
            return value
    return {}


def accept(cwd: Path, objective: str | None, ident: str, *, integrated: str | None = None, discarded: bool = False,
           why: str | None = None) -> dict:
    target, root, _ = locate(cwd, objective)
    with log.writer(target) as writer:
        state = fold(writer.entries)
        row = state.assignments.get(ident)
        if not row or row.get("void"):
            raise PodError("assignment_unknown", f"There is no started assignment {ident}.", fix="run `pod status` for the ids")
        if state.result(ident):
            return {"assignment": ident, "already": True, "decision": state.result(ident)["decision"]}
        if not row.get("task"):
            raise PodError("start_unknown", f"Whether {ident} started is not known yet.", fix=f"run `pod dispatch --retry {ident}`")
        task = orca.tasks(state.objective["run"]).get(row["task"])
        if not task or task["status"] not in ("completed", "failed"):
            raise PodError("not_finished", f"{ident} has not reported a result (Orca says {task and task['status']}).",
                           fix="wait for Orca to wake you, or stop the worker through Orca and accept with --discarded")
        report = task["report"] or {}
        stated = parse_result(report.get("body"))
        outcome = report.get("outcome") if report.get("outcome") in ("succeeded", "failed") else "unknown"
        commit = stated.get("commit") if isinstance(stated.get("commit"), str) else None
        try:
            commit = gitio.commit(root, commit) if commit else None
        except PodError:
            commit = None
        writes = row["role"] == "implement"
        if writes and not discarded and not integrated:
            raise PodError("decision_required", f"State what you did with {ident}'s result.",
                           fix="pass --integrated COMMIT (where its work now is) or --discarded")
        changed = gitio.changed(root, row["base"], commit) if commit and commit != row["base"] else []
        fact = {"kind": "fact", "fact": "result", "assignment": ident, "role": row["role"], "outcome": outcome,
                "commit": commit, "decision": "discarded" if discarded else "integrated" if writes else "read",
                "summary": str(stated.get("summary") or report.get("subject") or "")[:600]}
        if why:
            fact["why"] = why
        output = {"assignment": ident, "outcome": outcome, "decision": fact["decision"]}
        facts = [fact]
        if writes:
            fact["paths"] = changed
            outside = [name for name in changed if row.get("paths") and not gitio.under(name, row["paths"])]
            if outside:
                fact["outside"] = output["outside_declared_paths"] = outside
            if not commit:
                output["note"] = "the worker named no result commit; nothing about its content is recorded"
            if integrated:
                at = gitio.commit(root, integrated)
                fact["integrated_at"] = at
                held = bool(commit) and (gitio.is_ancestor(root, commit, at) or gitio.blobs(root, at, changed)
                                         == gitio.blobs(root, commit, changed))
                fact["contained"] = held
                if commit and not held:
                    output["note"] = f"{at[:12]} does not contain the worker's result {commit[:12]} unchanged"
            if commit:
                tree = gitio.tree(root, commit)
                facts += [{"kind": "fact", "fact": "check", "commit": commit, "tree": tree, "command": str(item.get("command"))[:300],
                           "exit": item.get("exit"), "source": f"reported by {ident}"}
                          for item in stated.get("checks") or [] if isinstance(item, dict) and isinstance(item.get("exit"), int)]
        elif row["role"] == "review":
            verdict = stated.get("verdict") if stated.get("verdict") in ("pass", "fail") else None
            if verdict:
                found = [{"id": f"{ident}.{index}", "path": item.get("path") or None,
                          "required": bool(item.get("required")), "text": str(item.get("text") or "")[:1000]}
                         for index, item in enumerate(stated.get("findings") or [], 1) if isinstance(item, dict)]
                facts.append({"kind": "fact", "fact": "review", "commit": row["base"], "tree": gitio.tree(root, row["base"]),
                              "paths": row["paths"], "context": row.get("context") or [], "verdict": verdict,
                              "reviewer": ident, "wrote": bool(changed), "findings": found})
                output.update(verdict=verdict, findings=[f"{item['id']} {'required' if item['required'] else 'advisory'} "
                                                         f"{item['path'] or ''}: {item['text'][:160]}" for item in found])
                if changed:
                    output["note"] = "the reviewer changed files, so this review covers nothing"
            else:
                output["note"] = "the reviewer stated no verdict; no review is recorded"
        if fact["summary"]:
            output["summary"] = fact["summary"]
        output["facts"] = [entry["n"] for entry in writer.append(*facts)]
        return output


# --- record ----------------------------------------------------------------------------------------

def record(cwd: Path, objective: str | None, **given) -> dict:
    """Add what only the coordinator knows: a criterion met, a decision, a person's words, the note."""
    target, root, slug = locate(cwd, objective)
    with log.writer(target) as writer:
        state = fold(writer.entries)
        entries, done = [], []

        def criterion(ident: str) -> dict:
            if ident not in state.criteria:
                raise PodError("criterion_unknown", f"There is no criterion {ident}.",
                               fix="use one of: " + ", ".join(state.criteria))
            return state.criteria[ident]

        for ident in given.get("met") or []:
            criterion(ident)
            numbers = [int(value) for value in given.get("facts") or []]
            missing = [value for value in numbers if value >= len(writer.entries) or writer.entries[value]["kind"] != "fact"]
            if missing or not (numbers or given.get("why")):
                raise PodError("evidence_required", f"Criterion {ident} needs the facts that prove it.",
                               fix="pass --facts with fact numbers from `pod status`, or --why with what was observed")
            entries.append({"kind": "criterion", "id": ident, "state": "met", "facts": numbers,
                            **({"why": given["why"]} if given.get("why") else {})})
            done.append(f"{ident} met")
        for ident in given.get("drop_criterion") or []:
            criterion(ident)
            if not given.get("why"):
                raise PodError("reason_required", "Dropping a criterion needs who decided and why.", fix="pass --why")
            entries.append({"kind": "criterion", "id": ident, "state": "dropped", "why": given["why"]})
            done.append(f"{ident} dropped")
        for ident in given.get("after_merge") or []:
            criterion(ident)
            entries.append({"kind": "criterion", "id": ident, "after_merge": True})
            done.append(f"{ident} is proved after the merge")
        for text in given.get("add_criterion") or []:
            ident = state.next_id("c", list(state.criteria) + [row["id"] for row in entries if row["kind"] == "criterion"])
            entries.append({"kind": "criterion", "id": ident, "text": text, "state": "open"})
            done.append(f"{ident} added")
        if given.get("check"):
            entries.append(_run_check(root, given["check"]))
            done.append(f"check exit {entries[-1]['exit']}" + ("" if entries[-1].get("tree") else " (uncommitted changes: binds to no commit)"))
        if given.get("waive"):
            known = {row["id"] for row in proof.findings(state)}
            if given["waive"] not in known or not given.get("why"):
                raise PodError("waiver_invalid", "A waiver names one finding and says who waived it and why.",
                               fix="pass --waive FINDING --why 'who and why'; findings: " + (", ".join(sorted(known)) or "none"))
            _may_decide(given, "waive a required finding")
            entries.append({"kind": "fact", "fact": "waiver", "finding": given["waive"], "why": given["why"],
                            "by": "person" if given.get("words") else "coordinator", **_words(given)})
            done.append(f"{given['waive']} waived")
        if given.get("authorize") or given.get("withdraw"):
            if not given.get("withdraw"):
                _may_decide(given, "authorize a merge")
            head = gitio.commit(root)
            entries.append({"kind": "fact", "fact": "authorization", "action": "merge", "repository": slug,
                            "target": state.objective["target"], "by": "person" if given.get("words") else "coordinator",
                            **({"withdrawn": True} if given.get("withdraw") else
                               {"standing": True} if given.get("standing") else {"candidate": head}), **_words(given)})
            done.append("authorization withdrawn" if given.get("withdraw") else
                        "merge authorized for this objective" if given.get("standing") else f"merge authorized for {head[:12]}")
        if given.get("carried"):
            commit = gitio.commit(root, given.get("commit") or "HEAD")
            if not given.get("paths") or given.get("verdict") not in ("pass", "fail"):
                raise PodError("carried_review_incomplete", "A carried review names its commit, paths, verdict and source.",
                               fix="pass --carried REFERENCE --commit SHA --paths a,b --verdict pass")
            entries.append({"kind": "fact", "fact": "review", "commit": commit, "tree": gitio.tree(root, commit),
                            "paths": given["paths"], "context": [], "verdict": given["verdict"], "reviewer": "carried",
                            "carried": given["carried"], "wrote": False, "findings": []})
            done.append(f"review carried from {given['carried']}")
        for kind in ("scope", "decision"):
            if given.get(kind):
                entries.append({"kind": "fact", "fact": kind, "text": given[kind], **_words(given)})
                done.append(f"{kind} recorded")
        fields = {key: given[key] for key in ("note", "pr", "branch") if given.get(key) is not None}
        if fields:
            entries.append({"kind": "objective", **fields})
            done.append(", ".join(fields) + " updated")
        if not entries:
            raise PodError("nothing_to_record", "Nothing was named to record.", fix="run `pod record --help`")
        writer.append(*entries)
        return {"recorded": done, "entries": [row["n"] for row in writer.entries[-len(entries):]]}


def _words(given: dict) -> dict:
    return {"words": given["words"][:1000]} if given.get("words") else {}


def _may_decide(given: dict, what: str) -> None:
    """A person's own words always count; the coordinator's own decision only when the person allowed it."""
    if given.get("words") or routefile.load().merge_authorization == "coordinator":
        return
    raise PodError("person_decides", f"Only the person may {what} here.",
                   fix="ask them, then pass --words with what they said; or they set merge_authorization: coordinator")


def _run_check(root: Path, command: str) -> dict:
    """Run a check and record what was observed. It binds to a commit only if the tree was clean throughout."""
    before = gitio.dirty(root)
    try:
        done = subprocess.run(command, shell=True, cwd=root, capture_output=True, timeout=3600)
        code, tail = done.returncode, (done.stdout + done.stderr).decode(errors="replace")[-400:]
    except subprocess.TimeoutExpired:
        code, tail = None, "did not finish within one hour"
    clean = not before and not gitio.dirty(root)
    return {"kind": "fact", "fact": "check", "command": command[:300], "exit": code, "tail": tail, "source": "run by pod",
            **({"commit": gitio.commit(root), "tree": gitio.tree(root)} if clean else {})}


# --- status, resume, drop --------------------------------------------------------------------------

def view(root: Path, state: State, *, remote: bool = True) -> dict:
    """The derived picture of one objective."""
    from . import gate
    info = state.objective
    source = info["source"]
    result = {"objective": state.id, "state": "dropped" if state.dropped else "closed" if state.closed else "open",
              "source": (f"{source['repository']}#{source['number']} {source['title']}" if source["kind"] == "issue"
                         else source["text"][:200]),
              "target": info["target"], "branch": info.get("branch"), "run": info.get("run"), "pr": info.get("pr")}
    if info.get("note"):
        result["note"] = info["note"]
    result["criteria"] = [f"{row['id']} {row['state']}{' (after merge)' if row.get('after_merge') else ''}: {row['text'][:160]}"
                          for row in state.criteria.values()]
    if state.closed:
        return result
    live = _live(state) if remote else {}
    rows = []
    for row in state.assignments.values():
        done = state.result(row["id"])
        status = ("void" if row.get("void") else f"accepted ({done['decision']}, {done['outcome']})" if done
                  else "start unknown" if not row.get("dispatch") else "running" if live.get(row["id"]) else
                  "finished, not accepted" if live.get(row["id"]) is False else "not read")
        rows.append(f"{row['id']} {row['role']} {status}: {row['title'][:80]}")
    if rows:
        result["assignments"] = rows
    found = [f"{row['id']} {row['path'] or ''}: {row['text'][:160]}" for row in proof.open_required(state)]
    if found:
        result["open_required_findings"] = found
    verdict = gate.evaluate(root, state, remote=remote)
    result["candidate"] = f"{verdict['candidate'][:12]} tree {verdict['tree'][:12]}" + (
        "" if not verdict.get("dirty") else f", {len(verdict['dirty'])} uncommitted paths")
    result["gate"] = "ready to deliver" if verdict["pass"] else verdict["needs"]
    return result


def status(cwd: Path, objective: str | None, *, remote: bool = True) -> dict:
    target, root, _ = locate(cwd, objective)
    entries = log.read(target)
    if not entries:
        raise PodError("objective_unknown", f"There is no objective {target.stem}.", fix="run `pod status --all`")
    return view(root, fold(entries), remote=remote)


def listing() -> dict:
    """The one read Pod offers other tools: open objectives and what they name. No provider is asked."""
    rows = []
    for target in log.every():
        state = fold(log.read(target))
        if not state.entries or state.closed:
            continue
        info = state.objective
        rows.append({"objective": state.id, "repository": info.get("repository") or target.parent.name,
                     "worktree": info.get("worktree"), "branch": info.get("branch"), "run": info.get("run"),
                     "pr": info.get("pr"), "target": info.get("target"), "opened": info.get("opened"),
                     "unaccepted": [{"assignment": row["id"], "task": row.get("task"), "dispatch": row.get("dispatch")}
                                    for row in state.assignments.values() if not row.get("void") and not state.result(row["id"])]})
    return {"schema": "pod-objectives/v1", "objectives": rows}


def resume(cwd: Path, objective: str | None) -> dict:
    """Continue an objective from a new conversation. Orca moves the Run to it and fences the earlier one."""
    target, root, _ = locate(cwd, objective)
    with log.writer(target) as writer:
        state = fold(writer.entries)
        run = state.objective.get("run")
        if run and not state.closed:
            bound = orca.run_use(run)
            writer.append({"kind": "fact", "fact": "takeover", "run": run, "generation": bound.get("consumer_generation")})
            state = fold(writer.entries)
    return view(root, state)


def drop(cwd: Path, objective: str | None, *, why: str) -> dict:
    target, _, _ = locate(cwd, objective)
    with log.writer(target) as writer:
        state = fold(writer.entries)
        if not state.dropped:
            writer.append({"kind": "objective", "dropped": True, "why": why})
        live = [key for key, value in (_live(state) if state.objective.get("run") else {}).items() if value is not False]
    return {"objective": state.id, "state": "dropped",
            **({"still_running": live, "next": "stop these workers through Orca; Pod holds nothing for a dropped objective"}
               if live else {})}
