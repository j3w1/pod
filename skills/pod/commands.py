"""The coordinator's commands: open, dispatch, accept, record, status, resume and drop.

Each takes a handful of arguments and holds the objective's lock while it runs. Each asks the
coordinator only for what software cannot know: the plan, a decision, a person's words.
Everything else is read from Orca, Git and GitHub at the moment it is needed.
"""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess

from . import cleanup, github, gitio, issue, log, orca, proof, routefile
from .errors import PodError
from .state import ROLES, State, fold

RESULT_FORMAT = ('{"outcome": "succeeded" or "failed", "commit": "<full id of your result commit, or null>", '
                 '"checks": [{"command": "<what you ran>", "exit": 0}], "summary": "<one or two sentences>"}')
REVIEW_FORMAT = ('{"outcome": "succeeded" or "failed", "verdict": "pass" or "fail", "findings": [{"path": "<file>", '
                 '"required": true, "text": "<what is wrong and why>"}], "summary": "<one or two sentences>"}')


# --- finding the objective -------------------------------------------------------------------------

def place(cwd: Path) -> tuple[Path, str, str | None, dict | None]:
    """Where Pod runs: the root, the log namespace, the GitHub repository and the Orca folder workspace.

    Git comes first and behaves as always. Only a directory outside every Git worktree may be a
    registered Orca folder workspace, whose objectives have no checkout and never deliver.
    """
    try:
        root = gitio.root(cwd)
    except PodError as exc:
        if exc.code != "not_a_repository":
            raise
        workspace = orca.folder_workspace(str(Path(cwd).resolve()))
        if not workspace:
            raise PodError("not_a_repository", f"{cwd} is neither inside a Git worktree nor the Orca folder workspace "
                           "of this terminal.", fix="run Pod from the objective's worktree, or from the Orca terminal "
                           "of its folder workspace") from exc
        return Path(workspace["path"]), f"folder-{workspace['project']}", None, workspace
    repository, slug = gitio.repository(root)
    return root, repository, slug, None


def folder(state: State) -> bool:
    """An objective bound to an Orca folder workspace: no Git, no candidate, no delivery."""
    return state.objective.get("workspace_kind") == "folder"


def _refuse_in_folder(state: State, what: str) -> None:
    if folder(state):
        raise PodError("folder_no_delivery", f"Objective {state.id} lives in an Orca folder workspace with no Git checkout, "
                       f"so it cannot {what}.", fix="record criteria, decisions and notes only; it closes when no criterion is open")


def locate(cwd: Path, objective: str | None, *, placed: tuple | None = None) -> tuple[Path, Path, str | None, dict | None]:
    """The log, the worktree root, the GitHub repository and this terminal's folder workspace, if any.

    A folder objective is checked against the workspace with `guard` on the state each command
    actually folds, under its lock, never on an earlier read.
    """
    root, repository, slug, workspace = placed or place(cwd)       # a caller that already decided on it passes its placement
    if objective:
        return log.path(repository, objective), root, slug, workspace
    here, finished = [], []
    for candidate in log.every():
        if candidate.parent.name != repository:
            continue
        try:
            state = fold(log.read(candidate))
        except PodError:
            continue                    # an unreadable log is named by `pod status --all`; it does not stop other work
        if state.entries and (_same_workspace(state.objective, workspace) if workspace
                              else state.objective.get("worktree") == str(root)):
            (finished if state.closed else here).append((state.entries[-1]["at"], candidate))
    if len(here) == 1 or (not here and finished):
        found = max(here or finished)[1]                    # nothing open: the one most recently closed here
        return found, root, slug, workspace
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


def _same_workspace(info: dict, workspace: dict) -> bool:
    """A folder objective is bound to one native workspace and instance; its path is checked as an attribute."""
    return (info.get("workspace_kind") == "folder" and info.get("workspace_id") == workspace["workspace_id"]
            and info.get("workspace_instance_id") == workspace["instance_id"] and info.get("worktree") == workspace["path"])


def guard(state: State, workspace: dict | None) -> State:
    """The folded state, refused when this terminal's folder workspace is not the objective's own."""
    if workspace is not None and state.entries and not _same_workspace(state.objective, workspace):
        raise PodError("folder_binding_mismatch", f"Objective {state.id} is bound to workspace "
                       f"{state.objective.get('workspace_id')} ({state.objective.get('workspace_instance_id')}), not to this "
                       f"terminal's {workspace['workspace_id']} ({workspace['instance_id']}).",
                       fix="run Pod from the Orca terminal of the objective's own folder workspace")
    return state


def _folder_after(state: State, after: str | None) -> None:
    """A failed plan review vouches for no other plan: folder reviews carry no content to compare."""
    if after and folder(state):
        raise PodError("review_exception_invalid", "A folder plan review has no commit, so an earlier failed review cannot "
                       "stand in for this one.", fix="pass the person's own --words, or choose an allowed reviewer")


def _folder_worktree(state: State, worktree: str, commit: str | None) -> str:
    """A folder objective's workers start in its own folder workspace; Pod never makes a Git worktree for them."""
    if commit:
        raise PodError("folder_no_commit", "A folder workspace objective has no Git commit to name.", fix="omit --commit")
    if worktree.startswith("new-"):
        raise PodError("folder_no_worktree", "A folder workspace objective has no Git repository to make a worktree from.",
                       fix="omit --worktree, or name the objective's own folder workspace")
    named = orca.workspace(worktree) if worktree != "current" else None
    if worktree != "current" and not (named and _same_workspace(state.objective, named)):
        raise PodError("folder_binding_mismatch", f"Orca does not resolve {worktree} to the objective's own folder workspace "
                       f"{state.objective['workspace_id']}.", fix="omit --worktree, or name the objective's own folder workspace")
    return "current"


# --- open ------------------------------------------------------------------------------------------

def open_(cwd: Path, *, source: str | None = None, text: str | None = None, objective: str | None = None,
          target: str = "main", branch: str | None = None, pr: int | None = None, run: str | None = None,
          continues: str | None = None, criteria: list[str] | None = None, delivery: bool = True) -> dict:
    root, repository, slug, workspace = place(cwd)
    if workspace and (delivery or branch or pr or target != "main"):
        raise PodError("folder_no_delivery", "An Orca folder workspace has no Git checkout, so its objective has no branch, "
                       "pull request or delivery.", fix="open it with --no-delivery and without --target, --branch or --pr")
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
            state = guard(fold(writer.entries), workspace)          # a sibling workspace never reuses this objective
            if workspace is None and folder(state):
                raise PodError("folder_binding_mismatch", f"Objective {objective} belongs to an Orca folder workspace.",
                               fix="run Pod from the Orca terminal of its folder workspace")
            if state.objective.get("source") != described:
                raise PodError("objective_exists", f"Objective {objective} already exists with a different source.",
                               fix="choose another --objective id, or continue it with `pod resume`")
            return view(root, state, remote=False, gate=False)
        if run:
            busy = [row["dispatch"] for row in orca.workers(run) if orca.live(row)]
            if busy:
                raise PodError("run_busy", f"Run {run} has workers this objective did not start: {', '.join(busy)}.",
                               fix="wait for them or stop them through Orca, then open again")
        if workspace:
            bound = {"repository": repository, "target": None, "worktree": str(root), "branch": None, "pr": None,
                     "workspace_id": workspace["workspace_id"], "workspace_instance_id": workspace["instance_id"],
                     "workspace_kind": "folder", "delivery": "none", "base": None}
        else:
            head = gitio.commit(root)
            bound = {"repository": slug, "target": target, "worktree": str(root), "branch": branch or gitio.branch(root),
                     "pr": pr, **({} if delivery else {"delivery": "none"}),
                     "base": gitio.merge_base(root, head, f"origin/{target}") or head}
        writer.append(
            {"kind": "objective", "id": objective, "source": described, **bound, "run": run, "continues": continues},
            *({"kind": "criterion", "id": f"c{index}", "text": row["text"], "state": "open",
               **({"line": row["line"]} if "line" in row else {})} for index, row in enumerate(rows, 1)))
        return view(root, fold(writer.entries), remote=False, gate=False)


# --- dispatch --------------------------------------------------------------------------------------

def _reviewer_note_lines(state: State, *, commit: str | None = None) -> list[str]:
    lines: list[str] = []
    for item in state.reviewer_notes():
        if commit is not None and item.get("commit") not in (None, commit):
            continue
        who = item.get("assignment") or "review"
        verdict = item.get("verdict") or "partial"
        if item.get("notes"):
            lines.append(f"- {who} ({verdict}): {_line(item['notes'], 300)}")
        for finding in item.get("partial_findings") or []:
            req = "required" if finding.get("required") else "advisory"
            where = finding.get("path") or ""
            lines.append(f"- {who} partial {req} {where}: {_line(str(finding.get('text') or ''), 200)}")
    return lines[-10:]


def _normalize_notes(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:2000]
    if isinstance(value, list):
        joined = "\n".join(str(item).strip() for item in value if isinstance(item, str) and item.strip())
        return joined[:2000] if joined else None
    return None


def packet(state: State, assignment: dict, spec: str) -> str:
    role, paths_ = assignment["role"], assignment.get("paths") or []
    in_folder = folder(state)
    lines = [f"# Assignment {state.id} {assignment['id']} ({role})", "", spec.strip(), "", "## Boundaries", "",
             f"- Folder: {state.objective['worktree']} (an Orca folder workspace with no Git checkout)" if in_folder
             else f"- Base commit: {assignment['base']}"]
    if assignment.get("profile"):
        lines.append(f"- Profile: {assignment['profile']} (mcp: {'on' if assignment.get('mcp') else 'off'})")
    lines.append(f"- Browser scope: {assignment.get('browser_scope') or 'none'}")
    if role == "implement":
        lines += ["- Change only: " + (", ".join(paths_) if paths_ else "what the task needs"),
                  "- Leave your work in this folder and name what you changed in your summary; there is no Git checkout, "
                  "so do not create a repository or a commit, and your result commit is null." if in_folder else
                  "- Commit your work on the branch you are given. Do not push, merge or open a pull request.",
                  "- Run tests in the foreground. Never end a turn while a background run is pending: send",
                  ("  worker_done with what you changed, or, when you cannot finish, with what blocked you." if in_folder else
                   "  worker_done with the commit and the test result, or, when you cannot finish, with what blocked you.")]
    elif role == "review":
        lines += ["- Review the plan in the task text above" + (", and these paths: " + ", ".join(paths_) if paths_ else "")
                  + ". There is no commit." if in_folder else f"- Review commit {assignment['base']}, these paths: " + ", ".join(paths_),
                  "- Report every finding in one pass. Do not change any file; the writer makes corrections.",
                  "- Mark a finding required only when the change must not merge without it.",
                  "- `outcome` says whether you completed the review; `verdict` says whether the change passes.",
                  "- Include brief `notes` (checked invariants or partial progress) in your result JSON so findings persist across handoffs."]
        if assignment.get("context"):
            lines.append("- Read as context: " + ", ".join(assignment["context"]))
    else:
        lines.append("- Read only. Change nothing.")
    if role == "implement":
        note_lines = _reviewer_note_lines(state)
        if note_lines:
            lines += ["", "## Reviewer context", "", *note_lines]
    elif role == "review":
        prior_notes = _reviewer_note_lines(state, commit=assignment["base"])
        if prior_notes:
            lines += ["", "## Prior reviewer notes", "", *prior_notes]
    lines += ["- Report what you observed. If something could not be run or read, say so; never report it as passed.",
              "- Leave this terminal's name as it is.", "",
              "## Result", "",
              "Finish through Orca as its instructions say. The body of your final worker_done message must end with",
              "this JSON object, filled in, on its own line. It is the only part of your report that is read by software;",
              "without it your " + ("verdict is not recorded" if role == "review" else "result commit and checks are not recorded") + ".",
              "", REVIEW_FORMAT if role == "review" else RESULT_FORMAT]
    return "\n".join(lines) + "\n"


def _failed_review(state: State, ident: str, base: str, agents: list[str]) -> bool:
    prior = state.assignments.get(ident)
    if not prior or prior["role"] != "review" or prior["base"] != base:
        return False
    if agents and (prior["route"].partition("/")[0] not in agents
                   or prior.get("effective", prior["route"]).partition("/")[0] not in agents):
        return False
    if any(review["reviewer"] == ident for review in state.of("review")):
        return False
    if prior.get("void"):
        return True
    if not prior.get("dispatch") or not prior.get("task"):
        return False
    run = state.objective["run"]
    task = orca.tasks(run).get(prior["task"])
    if not task:
        return False
    if (state.result(ident) or {}).get("decision") == "discarded" and not task["report"]:
        # Pod settled it as discarded and its worker never reported (a stopped worker, such as a provider
        # content-gate refusal): nothing was reviewed, so it is a void attempt. Any report disqualifies it.
        return True
    if task["status"] != "failed":
        return False
    if parse_result((task["report"] or {}).get("body")).get("verdict") in ("pass", "fail"):
        return False
    # A Task can outlive its Dispatch and later fail under a different worker.
    return any(worker["dispatch"] == prior["dispatch"] and worker["task"] == prior["task"]
               and worker["state"] == "failed" for worker in orca.workers(run))


def _validate_review_exception(state: State, agents: list[str], base: str,
                               *, words: str | None, after: str | None, why: str | None) -> dict:
    if words and words.strip() and after is why is None:
        return {"kind": "words", "words": words}
    if words is None and after and why and why.strip() and _failed_review(state, after, base, agents):
        return {"kind": "after", "after": after, "why": why}
    raise PodError("review_exception_invalid", "The review exception is incomplete or the prior review is ineligible.",
                   fix="pass the person's own --words, or use --after with --why for a failed or void review "
                       "by an allowed agent on the same commit without a review result")


ROLE_WORDS = {"implement": "Builder", "review": "Reviewer", "investigate": "Scout"}
PROFILE_ROLES = {
    "build-fast": "Builder",
    "build-deep": "Builder (deep)",
    "ui-light": "Builder (UI)",
    "ui-design": "Builder (UI)",
    "review-fast": "Reviewer (fast)",
    "review-deep": "Reviewer (deep)",
    "scout": "Scout",
    "scout-fast": "Scout",
    "scout-deep": "Scout (deep)",
    "challenger": "Challenger",
}
SHORT_MODELS = {
    "claude-opus-5-5": "opus",
    "claude-sonnet-5-5": "sonnet",
    "claude-haiku-5-5": "haiku",
    "claude-fable-5-1": "fable",
    "claude-fable-5-2": "fable",
    "gemini-4-neon": "neon",
    "gpt-6.2-sol": "sol",
    "gpt-6.1-sol": "sol",
    "gpt-6.1-terra": "terra",
    "gpt-6.2-astra": "astra",
    "gpt-6-astra": "astra",
    "gpt-6-luna": "luna",
}
_VERSION_TOKEN = re.compile(r"^v?\d+(?:\.\d+)*$", re.IGNORECASE)
_TOPIC_PREFIX = re.compile(r"^[A-Za-z0-9._-]+:a\d+\s+")
_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _clean_topic(title: str) -> str:
    return _TOPIC_PREFIX.sub("", " ".join(title.split())).strip()


def _worktree_display_name(title: str, ident: str) -> str:
    topic = _clean_topic(title)
    if not topic:
        return ident
    return topic if len(topic) <= 60 else topic[:59].rstrip() + "…"


def _worktree_slug(display: str, ident: str) -> str:
    slug = _SLUG_NON_ALNUM.sub("-", display.lower()).strip("-")[:40].rstrip("-")
    return slug or ident


def _tab_role(profile: str | None, role: str, *, in_folder: bool = False) -> str:
    """CE Dev role word for a worker tab, never the raw profile name or kind.

    A `ui-design` worker in a folder workspace objective is the Analyst's Designer; elsewhere it builds UI."""
    name = (profile or "").strip().lower()
    if name == "ui-design" and in_folder:
        return "Designer"
    if name in PROFILE_ROLES:
        return PROFILE_ROLES[name]
    if name == "ui" or name.startswith("ui-"):
        return "Builder (UI)"
    if "analyst" in name:
        return "Analyst"
    if "challenger" in name:
        return "Challenger"
    if name == "review" or name.startswith("review-"):
        return "Reviewer"
    if name == "build" or name.startswith("build-"):
        return "Builder"
    if name == "scout" or name.startswith("scout-"):
        return "Scout"
    return ROLE_WORDS.get(role.strip().lower(), role)


_SHORT_MODEL_MAX = 16


def _short_model(model: str) -> str:
    """Bounded short model label (<= 16 chars) for a worker tab; never ellipsized."""
    key = model.strip().lower()
    if key in SHORT_MODELS:
        return SHORT_MODELS[key]
    chosen = model.strip()
    for part in reversed(chosen.split("-")):
        if part and not _VERSION_TOKEN.fullmatch(part):
            chosen = part
            break
    return chosen[:_SHORT_MODEL_MAX]


_EFFORT_SHORT = {"medium": "med"}
_MARKER_TOKEN = re.compile(r"\s*\[[^\[\]\s]+:a\d+\]\s*")
_TAB_DESCRIPTION_MAX = 34


def _tab_title(profile: str | None, role: str, route: str, title: str, requested: str | None = None, *,
               in_folder: bool = False) -> str:
    """`<Role> │ <model>/<effort> │ <description>` (U+2502 bar, one space each side); the description is cut to 34 characters."""
    segments = route.split("/")
    raw_model = segments[1] if len(segments) > 1 and segments[1] != "None" else ""
    effort = segments[2] if len(segments) > 2 and segments[2] != "None" else ""
    if not effort and requested:
        asked = requested.split("/")
        effort = asked[2] if len(asked) > 2 and asked[2] != "None" else ""
    effort = _EFFORT_SHORT.get(effort, effort)
    short_model = _short_model(raw_model) if raw_model else ""
    model_part = "/".join(([short_model] if short_model else []) + ([effort] if effort else []))
    bare = _MARKER_TOKEN.sub(lambda m: "" if re.match(r"[,.;:!?)\]]", title[m.end():m.end() + 1]) else " ", title)
    short = _clean_topic(bare) or " ".join(bare.split())
    if len(short) > _TAB_DESCRIPTION_MAX:
        short = short[:_TAB_DESCRIPTION_MAX - 1].rstrip() + "…"
    return " │ ".join(part for part in (_tab_role(profile, role, in_folder=in_folder), model_part, short) if part)


def _task_title(profile: str | None, role: str, route: str, objective: str, ident: str, title: str) -> str:
    _, model, effort = routefile.split(route)
    label = profile or role
    return f"[{objective}:{ident}] {label} · {model}/{effort} · {title}"


def _validate_dry_run_review_exception(folded: State | None, root: Path | None, agents: list[str],
                                       *, commit: str | None, words: str | None, after: str | None,
                                       why: str | None) -> tuple[bool, str | None]:
    """Validate review exception flags offline for --dry-run without calling Orca."""
    if words and words.strip() and after is why is None:
        return True, None
    if words is None and after and why and why.strip() and folded is not None:
        prior = folded.assignments.get(after)
        if not prior or prior["role"] != "review":
            return False, None
        if root is not None:
            # `commit` is the one SHA the dry run resolved; an unresolvable candidate cannot establish eligibility
            if not commit or prior["base"] != commit:
                return False, None
        if agents and (prior["route"].partition("/")[0] not in agents
                       or prior.get("effective", prior["route"]).partition("/")[0] not in agents):
            return False, None
        if any(review["reviewer"] == after for review in folded.of("review")):
            return False, None
        if prior.get("void"):
            return True, None
        if not prior.get("dispatch") or not prior.get("task"):
            return False, None
        return True, f"dry-run does not call Orca: prior failure of {after} was not verified against Orca"
    return False, None


def _resolved(root: Path | None, commit: str | None) -> str | None:
    try:
        return gitio.commit(root, commit or "HEAD") if root is not None else None
    except PodError:
        return None


def _dry_run_dispatch(cwd: Path, objective: str | None, *, role: str | None = None, profile: str | None = None,
                      route: str | None = None, reason: str | None = None, exclude_agents: list[str] | None = None,
                      commit: str | None = None, words: str | None = None, after: str | None = None,
                      why: str | None = None, worktree: str = "current") -> dict:
    """Pure read-only route resolution: never writes to the log and never calls Orca."""
    authors: set[str] = set()
    counts: dict[str, int] = {}
    frozen: str | None = None
    notes: list[str] = []
    folded: State | None = None
    root: Path | None = None
    if objective:
        target, root, _, here = locate(cwd, objective)
        entries = log.read(target)
        if not entries:
            raise PodError("objective_unknown", f"There is no objective {target.stem}.", fix="run `pod status --all`")
        folded = guard(fold(entries), here)
        if folder(folded):
            _folder_after(folded, after)
            _folder_worktree(folded, worktree, commit)
        authors = folded.author_families()
        frozen = None if folder(folded) else _resolved(root, commit)
        counts = {} if folder(folded) else folded.author_counts(root, frozen)
        notes = _reviewer_note_lines(folded)
    else:
        try:
            target, root, _, here = locate(cwd, None)
            entries = log.read(target)
            if entries:
                folded = guard(fold(entries), here)
                authors = folded.author_families()
                frozen = None if folder(folded) else _resolved(root, commit)
                counts = {} if folder(folded) else folded.author_counts(root, frozen)
                notes = _reviewer_note_lines(folded)
        except PodError as exc:
            if exc.code.startswith("folder_") or exc.code == "orca_unknown":
                raise                    # the preview needs no objective, but never hides a refused folder workspace
        if folded is not None and folder(folded):      # outside the swallow: a folder preview refuses like the start
            _folder_after(folded, after)
            _folder_worktree(folded, worktree, commit)
    preferences = routefile.load()
    excluded = list(exclude_agents or ())
    has_exception_flags = not (words is after is why is None)
    res = routefile.resolve_dispatch(
        preferences, role=role, profile=profile, route=route, reason=reason, exclude_agents=excluded,
        author_families=authors, author_counts=counts, reviewer_notes=notes, allow_review_exception=False)
    refused_override: dict | None = None
    preview_note: str | None = None
    if has_exception_flags:
        if res.ok and not res.review_fallback_reason:
            refused_override = {
                "code": "review_exception_invalid",
                "message": "This dispatch needs no reviewer exception.",
                "fix": "omit --words, --after and --why",
            }
        else:
            disallowed_seen = (bool(res.review_fallback_reason)
                               or res.error_code in ("reviewer_agent_not_allowed", "reviewer_same_family")
                               or any(step.code in ("reviewer_agent_not_allowed", "author_family") for step in res.trace))
            if disallowed_seen:
                valid_exc, preview_note = _validate_dry_run_review_exception(
                    folded, root, preferences.review_agents, commit=frozen, words=words, after=after, why=why)
                if not valid_exc:
                    refused_override = {
                        "code": "review_exception_invalid",
                        "message": "The review exception is incomplete or the prior review is ineligible.",
                        "fix": ("pass the person's own --words, or use --after with --why for a failed or void review "
                                "by an allowed agent on the same commit without a review result"),
                    }
                else:
                    res = routefile.resolve_dispatch(
                        preferences, role=role, profile=profile, route=route, reason=reason, exclude_agents=excluded,
                        author_families=authors, author_counts=counts, reviewer_notes=notes, allow_review_exception=True)
    ok = res.ok and refused_override is None
    active_paused = routefile.active_holds(preferences.paused_meta, agents=preferences.paused_agents)
    active_drained = routefile.active_holds(preferences.soft_drained_meta, agents=preferences.soft_drained_agents)
    output = {
        "dry_run": True,
        "pass": ok,
        "ok": ok,
        "role": res.role,
        "profile": res.profile,
        "mcp": res.mcp,
        "route": res.route if ok else None,
        "wire": {"agent": res.wire[0], "model": res.wire[1], "effort": res.wire[2]} if (ok and res.wire) else None,
        "author_families": list(res.author_families),
        "review_independence": res.review_independence if ok else None,
        **({"review_fallback_reason": res.review_fallback_reason} if ok and res.review_fallback_reason else {}),
        "paused_agents": list(active_paused),
        "soft_drained_agents": list(active_drained),
        "excluded_agents": sorted({item.strip() for item in excluded if item and item.strip()}),
        **({"reason": res.reason} if ok and res.reason else {}),
        **({"note": preview_note} if ok and preview_note else {}),
        **({"reviewer_notes": list(res.reviewer_notes)} if res.reviewer_notes else {}),
        "trace": [{"route": step.route, "status": step.status, "code": step.code, "reason": step.reason}
                  for step in res.trace],
    }
    if refused_override is not None:
        output["refused"] = refused_override
    elif not res.ok:
        output["refused"] = {"code": res.error_code, "message": res.error_message, "fix": res.error_fix}
    return output


def dispatch(cwd: Path, objective: str | None, *, role: str | None = None, profile: str | None = None,
             title: str | None = None, spec: str | None = None, route: str | None = None,
             reason: str | None = None, exclude_agents: list[str] | None = None, dry_run: bool = False,
             paths_: list[str] | None = None, commit: str | None = None,
             context: list[str] | None = None, worktree: str = "current", retry: str | None = None,
             name: str | None = None, words: str | None = None, after: str | None = None, why: str | None = None) -> dict:
    if dry_run:
        return _dry_run_dispatch(cwd, objective, role=role, profile=profile, route=route, reason=reason,
                                 exclude_agents=exclude_agents, commit=commit, words=words, after=after, why=why,
                                 worktree=worktree)
    target, root, _, here = locate(cwd, objective)
    with log.writer(target) as writer:
        state = guard(fold(writer.entries), here)
        if state.closed:
            raise PodError("objective_closed", f"Objective {state.id} is closed or dropped.", fix="open a new objective")
        if retry:
            return _settle_start(writer, state, retry)
        if (role is None and profile is None) or (role is not None and role not in ROLES) or not (title or "").strip() or not spec:
            raise PodError("dispatch_incomplete", "A dispatch needs a role (or profile), a title and the task text.",
                           fix="pass --role implement|review|investigate (or --profile NAME), --title and --spec or --spec-file")
        preferences = routefile.load()
        authors = state.author_families()
        in_folder = folder(state)
        if in_folder:
            _folder_after(state, after)
            worktree = _folder_worktree(state, worktree, commit)
        base = None if in_folder else gitio.commit(root, commit or "HEAD")
        counts = {} if in_folder else state.author_counts(root, base)
        notes = _reviewer_note_lines(state)
        excluded = list(exclude_agents or ())
        has_exception_flags = not (words is after is why is None)
        res = routefile.resolve_dispatch(
            preferences, role=role, profile=profile, route=route, reason=reason, exclude_agents=excluded,
            author_families=authors, author_counts=counts, reviewer_notes=notes, allow_review_exception=False)
        exception = None
        paths_ = gitio.declared(paths_)
        context = gitio.declared(context, keep_root=True)
        if has_exception_flags:
            if res.ok and not res.review_fallback_reason:
                raise PodError("review_exception_invalid", "This dispatch needs no reviewer exception.",
                               fix="omit --words, --after and --why")
            disallowed_seen = (bool(res.review_fallback_reason)
                               or res.error_code in ("reviewer_agent_not_allowed", "reviewer_same_family")
                               or any(step.code in ("reviewer_agent_not_allowed", "author_family") for step in res.trace))
            if not disallowed_seen:
                raise PodError(res.error_code or "no_eligible_route", res.error_message or "No route is eligible.",
                               fix=res.error_fix)
            res = routefile.resolve_dispatch(
                preferences, role=role, profile=profile, route=route, reason=reason, exclude_agents=excluded,
                author_families=authors, author_counts=counts, reviewer_notes=notes, allow_review_exception=True)
            if not res.ok:
                raise PodError(res.error_code or "no_eligible_route", res.error_message or "No route is eligible.",
                               fix=res.error_fix)
            exception = _validate_review_exception(state, preferences.review_agents, base,
                                                   words=words, after=after, why=why)
        elif not res.ok:
            raise PodError(res.error_code or "no_eligible_route", res.error_message or "No route is eligible.",
                           fix=res.error_fix)
        role = res.role
        route = res.route
        if role == "review" and in_folder:
            pass                    # a plan review reads the task text; there is no commit or change to bind it to
        elif role == "review":
            against = gitio.merge_base(root, base, f"origin/{state.objective['target']}") or gitio.merge_base(
                root, base, state.objective["target"])
            if not against:
                raise PodError("target_unknown", f"The target branch {state.objective['target']} is not in this repository.",
                               fix="fetch it, then dispatch the review again")
            changed = set(gitio.changed(root, against, base))
            for review in state.of("review"):
                locations = proof.finding_paths(root, review)
                wanted = {locations[item["path"]] for item in review.get("findings", [])
                          if item.get("required") and item.get("path")}
                if wanted - {None}:
                    # A correction can have no net target diff, including an added file's removal.
                    changed.update(wanted.intersection(gitio.changed(root, review["commit"], base)))
            paths_ = [item for item in sorted(changed) if not paths_ or gitio.under(item, paths_)]
            if not paths_:
                raise PodError("nothing_to_review", f"Commit {base[:12]} changes none of the named paths against the target.",
                               fix="name the commit and paths that hold the change")
        elif role == "implement":
            live = _live(state)
            target_wt = None if worktree.startswith("new-") else (str(root) if worktree in ("current", f"path:{root}") else worktree)
            for other in state.assignments.values():
                if other["role"] != "implement" or other["id"] not in live or live.get(other["id"]) is False:
                    continue
                if gitio.overlap(paths_, other.get("paths") or []):
                    raise PodError("paths_overlap", f"Assignment {other['id']} is still running and may change the same files.",
                                   fix=f"wait for {other['id']}, stop it through Orca, or declare paths that do not overlap")
                other_raw = other.get("worktree") or "current"
                other_wt = None if other_raw.startswith("new-") else (str(root) if other_raw in ("current", f"path:{root}") else other_raw)
                if target_wt is not None and other_wt == target_wt:
                    raise PodError("worktree_busy", f"Assignment {other['id']} is already implementing in this worktree.",
                                   fix=f"wait for {other['id']}, stop it through Orca, or pass --worktree new-child")
        run = state.objective.get("run")
        if not run:
            run = orca.run_create(f"{state.id}: {state.objective['source'].get('title') or state.objective['source'].get('text', '')}")
            writer.append({"kind": "objective", "run": run})
        ident = state.next_id("a", state.assignments)
        assignment = {"kind": "assignment", "id": ident, "role": role, "title": title, "paths": paths_, "base": base,
                      "route": route, "request": orca.request_id(target.parent.name, state.id, ident, 1), "worktree": worktree,
                      **({"profile": res.profile, "mcp": res.mcp} if res.profile else {}),
                      "browser_scope": routefile.browser_scope(preferences.profiles.get(res.profile) if res.profile else None),
                      **({"reason": res.reason} if res.reason else {}),
                      **({"context": context} if context else {}),
                      **({"review_exception": exception} if exception else {}),
                      **({"requested_review_fallback_reason": res.review_fallback_reason}
                         if res.review_fallback_reason else {})}
        text = packet(state, assignment, spec)
        assignment["packet"] = hashlib.sha256(text.encode()).hexdigest()
        writer.append(assignment)
        selector = (state.objective["workspace_id"] if in_folder else f"path:{root}") if worktree == "current" else worktree
        task_title = _task_title(res.profile, role, route, state.id, ident, title)
        new_from = (str(gitio.main_checkout(root)), state.objective.get("branch") or gitio.branch(root) or base) if worktree.startswith("new-") else None
        display_name = ((name or "").strip() or _worktree_display_name(title, ident)) if new_from else name
        wt_name = _worktree_slug(display_name, ident) if new_from and display_name else None
        try:
            started = orca.start(run=run, spec=text,
                                 title=task_title,
                                 worktree=selector,
                                 route=res.wire, request=assignment["request"],
                                 name=display_name, worktree_name=wt_name,
                                 new_from=new_from)
        except PodError as exc:
            if exc.code == "orca_unknown":
                exc.fix = f"run `pod dispatch --retry {ident}`; it reads what Orca did and never starts a second worker"
            else:
                writer.append({"kind": "assignment", "id": ident, "void": True, "refused": exc.code})
            raise
        if not started.get("dispatch") or started.get("state") == "failed":
            reason = " ".join(orca.failure(started["dispatch"]).split())[:300] if started.get("dispatch") else ""
            writer.append({"kind": "assignment", "id": ident, "void": True, "refused": "start failed",
                           "task": started["task"], "dispatch": started.get("dispatch")})
            raise PodError("start_failed", f"Orca started no worker for {ident}: {reason or 'it gave no reason'}",
                           fix="correct what Orca reported, then dispatch the work again as a new assignment")
        if role == "review" and started.get("dispatch") in state.implement_dispatches():
            writer.append({"kind": "assignment", "id": ident, "void": True, "refused": "same_session_review",
                           "task": started["task"], "dispatch": started["dispatch"]})
            raise PodError("same_session_review",
                           "A review must run in a separate fresh session from the implementer.",
                           fix="dispatch the review in a fresh worker session")
        result = _started(writer, ident, started, route, resolution=res, authors=authors,
                          primary_agent=preferences.review_agents[0] if preferences.review_agents else None)
        try:
            failed = orca.rename(run, started, _tab_title(res.profile, role, routefile.canonical_effective(
                started.get("effective")) or route, title, route, in_folder=in_folder), task_title=task_title, marker=f"[{state.id}:{ident}]",
                workspace=state.objective.get("workspace_id") if in_folder else None)
        except Exception as exc:                 # cosmetic: never fail a started worker
            failed = type(exc).__name__
        if failed:
            result["note"] = f"the worker tab may keep Orca's title (rename: {failed})"
        return result


def _effective_review_proof(
    requested_route: str,
    actual_route: str | None,
    authors: set[str],
    primary_agent: str | None,
    fallback_reason: str | None,
    *,
    observed_effective: bool = True,
) -> tuple[str | None, str | None]:
    if not observed_effective or not actual_route:
        if fallback_reason:
            return None, None
        actual_route = requested_route
    normalized = {routefile.route_family(item) for item in authors if item}
    actual_family = routefile.route_family(actual_route)
    if fallback_reason:
        if actual_family not in normalized:
            return routefile.review_tag(actual_route, normalized, primary_agent, fallback=False), None
        authorized_fallback = (
            actual_route == requested_route and not routefile._is_claude_route(actual_route)
        )
        if authorized_fallback:
            return routefile.review_tag(actual_route, normalized, primary_agent, fallback=True,
                                        mixed=fallback_reason.startswith(routefile.MIXED_PREFIX)), fallback_reason
        return "same_provider", None
    return routefile.review_tag(actual_route, normalized, primary_agent, fallback=False), None


def _started(writer, ident: str, started: dict, route: str, *, resolution: routefile.Resolution | None = None,
             role: str | None = None, authors: set[str] | None = None, primary_agent: str | None = None,
             fallback_reason: str | None = None) -> dict:
    effective = routefile.canonical_effective(started.get("effective"))
    actual_role = resolution.role if resolution else role
    req_fallback = resolution.review_fallback_reason if resolution else fallback_reason
    update = {"kind": "assignment", "id": ident, "task": started["task"], "dispatch": started["dispatch"],
              **({"effective": effective} if effective and effective != route else {}),
              **({"effective_seen": True} if effective else {})}
    result = {"assignment": ident, "task": started["task"], "dispatch": started["dispatch"], "route": route}
    if resolution and resolution.profile:
        result["profile"] = resolution.profile
        result["mcp"] = resolution.mcp
    if resolution and resolution.reason:
        result["reason"] = resolution.reason
    if actual_role == "review":
        observed_effective = bool(effective)
        if not observed_effective and req_fallback:
            update["effective_unknown"] = True
        tag, applied_reason = _effective_review_proof(
            route, effective or route, authors or set(), primary_agent, req_fallback,
            observed_effective=observed_effective,
        )
        if tag is not None:
            update["review_independence"] = tag
            result["review_independence"] = tag
        if applied_reason is not None:
            update["review_fallback_reason"] = applied_reason
            result["review_fallback_reason"] = applied_reason
        if authors:
            result["author_families"] = sorted(authors)
    writer.append(update)
    if resolution and any(step.status == "skipped" for step in resolution.trace):
        result["trace"] = [step.reason for step in resolution.trace]
    if effective and effective != route:
        result["route_mismatch"] = f"asked for {route}, Orca started {effective}"
    if started.get("replayed"):
        result["replayed"] = True
    if started.get("state") not in (None, "ready"):
        result["start_state"] = started["state"]
    result["next"] = f"Orca wakes you when it reports; then `pod accept {ident}`"
    return result


def _settle_start(writer, state: State, ident: str) -> dict:
    """After a lost reply: learn from Orca whether the worker started. Never start it again here.

    Orca's own receipt is read first. Only when Orca holds neither a receipt nor a task for the
    assignment is the start taken as not having happened; anything unsettled keeps its paths.
    """
    row = state.assignments.get(ident)
    if not row:
        raise PodError("assignment_unknown", f"There is no assignment {ident}.", fix="run `pod status` for the ids")
    if row.get("dispatch"):
        return {"assignment": ident, "task": row["task"], "dispatch": row["dispatch"], "already": True}
    run = state.objective["run"]
    answer = orca.call("orchestration", "request-show", "--request", row["request"])
    if answer.get("state") == "pending":
        raise PodError("start_pending", f"Orca is still starting {ident}.", fix=f"run `pod dispatch --retry {ident}` again shortly")
    receipt = answer.get("receipt") or {}
    found = receipt.get("taskId") if answer.get("state") == "completed" else None
    markers = (f"· {state.id}:{ident} ", f"[{state.id}:{ident}]")
    if folder(state) and not found:
        # A folder Run can be shared by sibling workspaces, so a title marker proves nothing, and without Orca's own
        # receipt whether this worker started stays unknown: its paths stay held and nothing is started again.
        raise PodError("start_unknown", f"Orca holds no receipt for {ident}'s request {row['request']}, so whether it "
                       "started is unknown.", fix=f"run `pod dispatch --retry {ident}` once Orca records it; until then it holds its "
                       "paths and this workspace")
    if not folder(state):
        found = found or next((key for key, task in orca.tasks(run).items()
                               if any(marker in (task["title"] or "") for marker in markers)), None)
    if found:
        worker = next((item for item in orca.workers(run) if item["task"] == found), {})
        dispatch_ = receipt.get("dispatchId") or worker.get("dispatch")
        if dispatch_ and receipt.get("state") != "failed":
            launch = receipt.get("launch") if isinstance(receipt.get("launch"), dict) else {}
            prefs = routefile.load()
            primary_agent = prefs.review_agents[0] if prefs.status == "ok" and prefs.review_agents else None
            effective_raw = launch.get("effective")
            started_info = {"task": found, "dispatch": dispatch_,
                            **({"effective": effective_raw} if effective_raw else {})}
            result = _started(
                writer, ident, started_info, row["route"],
                role=row.get("role"),
                authors=state.author_families() if row.get("role") == "review" else None,
                primary_agent=primary_agent,
                fallback_reason=row.get("requested_review_fallback_reason"),
            )
            effective_canon = routefile.canonical_effective(effective_raw)
            if effective_canon and row.get("title"):
                task_title = _task_title(row.get("profile"), row["role"], row["route"], state.id, ident, row["title"])
                try:
                    failed = orca.rename(
                        run, started_info,
                        _tab_title(row.get("profile"), row["role"], effective_canon, row["title"], row["route"],
                                   in_folder=folder(state)),
                        task_title=task_title, marker=f"[{state.id}:{ident}]",
                        workspace=state.objective.get("workspace_id") if folder(state) else None,
                    )
                except Exception as exc:
                    failed = type(exc).__name__
                if failed:
                    result["note"] = f"the worker tab may keep Orca's title (rename: {failed})"
            return result
        if answer.get("state") != "completed" and not dispatch_:
            raise PodError("start_pending", f"Orca has a task for {ident} and no worker yet.",
                           fix=f"run `pod dispatch --retry {ident}` again shortly")
    writer.append({"kind": "assignment", "id": ident, "void": True, "task": found,
                   "refused": "start failed" if found else "start_not_recorded"})
    return {"assignment": ident, "void": True,
            "next": "Orca started no worker for it; dispatch the work again as a new assignment"}


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
    target, root, _, here = locate(cwd, objective)
    with log.writer(target) as writer:
        state = guard(fold(writer.entries), here)
        row = state.assignments.get(ident)
        if not row or row.get("void"):
            raise PodError("assignment_unknown", f"There is no started assignment {ident}.", fix="run `pod status` for the ids")
        if state.result(ident):
            return {"assignment": ident, "already": True, "decision": state.result(ident)["decision"]}
        if not row.get("task"):
            raise PodError("start_unknown", f"Whether {ident} started is not known yet.", fix=f"run `pod dispatch --retry {ident}`")
        run = state.objective["run"]
        task = orca.tasks(run).get(row["task"])
        if not task or task["status"] not in ("completed", "failed"):
            # Orca blocks the Task of a worker it is stopping; only its stopped worker proves the work can no longer
            # change, and then only to be discarded. A hold, a stop in progress or an unproven stop is not that.
            asked = bool(task and discarded and row.get("dispatch"))
            worker = next((item for item in orca.workers(run) if item["dispatch"] == row.get("dispatch")), {}) if asked else {}
            if worker.get("state") != "stopped":
                fix = ("wait for Orca to wake you, or stop the worker through Orca and, once Orca shows it "
                       "stopped, accept with --discarded")
                if worker.get("state") == "abandoned":
                    fix = ("abandonment does not prove process exit; Pod cannot discard this unfinished Task; "
                           f"inspect `orca orchestration worker-show --dispatch {row['dispatch']}` and follow Orca's "
                           "recovery guidance; retain resources whose exit is unproven")
                raise PodError("not_finished", f"{ident} has not reported a result (Orca says {task and task['status']}"
                               + (f", its worker {worker.get('state') or 'not listed'}" if asked else "") + ").",
                               fix=fix)
        report = task["report"] or {}
        stated = parse_result(report.get("body"))
        outcome = report.get("outcome") if report.get("outcome") in ("succeeded", "failed") else "unknown"
        in_folder = folder(state)
        commit = stated.get("commit") if isinstance(stated.get("commit"), str) and not in_folder else None
        try:
            commit = gitio.commit(root, commit) if commit else None
        except PodError:
            commit = None
        writes = row["role"] == "implement"
        if in_folder and integrated:
            raise PodError("folder_no_commit", "A folder workspace objective has no Git commit to integrate into.",
                           fix=f"accept {ident} without --integrated (its work stays in the folder), or with --discarded")
        if writes and not discarded and not integrated and not in_folder:
            raise PodError("decision_required", f"State what you did with {ident}'s result.",
                           fix="pass --integrated COMMIT (where its work now is) or --discarded")
        changed = gitio.changed(root, row["base"], commit) if commit and commit != row["base"] else []
        fact = {"kind": "fact", "fact": "result", "assignment": ident, "role": row["role"], "outcome": outcome,
                "commit": commit, "decision": "discarded" if discarded else "integrated" if writes else "read",
                "summary": str(stated.get("summary") or report.get("body") or report.get("subject") or "")[:600]}
        if why:
            fact["why"] = why
        output = {"assignment": ident, "outcome": outcome, "decision": fact["decision"]}
        facts = [fact]
        if writes:
            fact["paths"] = changed
            outside = [name for name in changed if row.get("paths") and not gitio.under(name, row["paths"])]
            if outside:
                fact["outside"] = output["outside_declared_paths"] = outside
            if in_folder:
                output["note"] = "a folder workspace has no Git: its work stays in the folder and no content is recorded"
            elif not commit:
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
            reviewer_notes = _normalize_notes(stated.get("notes"))
            found = [{"id": f"{ident}.{index}", "path": item.get("path") or None,
                      "required": bool(item.get("required")), "text": str(item.get("text") or "")[:1000]}
                     for index, item in enumerate(stated.get("findings") or [], 1) if isinstance(item, dict)]
            if discarded:
                output["note"] = "discarded: no review is recorded"
            elif verdict:
                impl_dispatches = state.implement_dispatches()
                same_session = bool(row.get("dispatch") and row["dispatch"] in impl_dispatches)
                if not same_session and row.get("dispatch") and impl_dispatches:
                    try:
                        by_dispatch = {item["dispatch"]: item.get("handle") for item in orca.workers(run) if item.get("dispatch")}
                        review_handle = by_dispatch.get(row["dispatch"])
                        impl_handles = {by_dispatch.get(d) for d in impl_dispatches} - {None, ""}
                        same_session = bool(review_handle and review_handle in impl_handles)
                    except PodError:
                        pass
                reviewer_route = row.get("effective") or row["route"]
                prefs = routefile.load()
                primary_agent = prefs.review_agents[0] if prefs.status == "ok" and prefs.review_agents else None
                authors = sorted(state.author_families())
                tag, fallback_reason = _effective_review_proof(
                    row["route"], reviewer_route, set(authors), primary_agent,
                    row.get("requested_review_fallback_reason") or row.get("review_fallback_reason"),
                    observed_effective=not row.get("effective_unknown"),
                )
                facts.append({"kind": "fact", "fact": "review", "commit": row["base"],
                              "tree": None if in_folder else gitio.tree(root, row["base"]),
                              "paths": row["paths"], "context": row.get("context") or [], "verdict": verdict,
                              "reviewer": ident, "wrote": bool(changed), "completed": outcome == "succeeded",
                              **({"review_independence": tag} if tag is not None else {}),
                              **({"review_fallback_reason": fallback_reason} if fallback_reason else {}),
                              **({"author_families": authors} if authors else {}),
                              **({"notes": reviewer_notes} if reviewer_notes else {}),
                              **({"same_session": True} if same_session else {}),
                              "findings": found})
                output.update(verdict=verdict,
                              **({"review_independence": tag} if tag is not None else {}),
                              **({"review_fallback_reason": fallback_reason} if fallback_reason else {}),
                              **({"author_families": authors} if authors else {}),
                              **({"notes": reviewer_notes} if reviewer_notes else {}),
                              findings=[f"{item['id']} {'required' if item['required'] else 'advisory'} "
                                        f"{item['path'] or ''}: {_line(item['text'])}" for item in found])
                if same_session:
                    output["note"] = "the review ran in the same session as an implementer, so this review covers nothing"
                elif changed:
                    output["note"] = "the reviewer changed files, so this review covers nothing"
                elif outcome != "succeeded":
                    output["note"] = (f"Orca reports the review as {outcome}, not completed: its findings are kept, "
                                      "and it covers nothing and closes no finding")
            else:
                if reviewer_notes:
                    fact["reviewer_notes"] = reviewer_notes
                    output["reviewer_notes"] = reviewer_notes
                if found:
                    fact["partial_findings"] = found
                    output["partial_findings"] = [f"{item['id']} {'required' if item['required'] else 'advisory'} "
                                                  f"{item['path'] or ''}: {_line(item['text'])}" for item in found]
                output["note"] = ("the reviewer's message has no result object with a verdict, so no review is recorded; "
                                  "dispatch the review again if it is needed")
        if fact["summary"]:
            output["summary"] = fact["summary"]
        output["facts"] = [entry["n"] for entry in writer.append(*facts)]
        return output


# --- record ----------------------------------------------------------------------------------------

def record(cwd: Path, objective: str | None, **given) -> dict:
    """Add what only the coordinator knows: a criterion met, a decision, a person's words, the note."""
    target, root, slug, here = locate(cwd, objective)
    with log.writer(target) as writer:
        state = guard(fold(writer.entries), here)
        entries, done = [], []
        for flag, what in (("check", "bind a check to a commit"), ("waive", "waive a finding before a delivery"),
                           ("authorize", "authorize a merge"), ("withdraw", "withdraw a merge authorization"),
                           ("carried", "carry a review of a commit"), ("commit", "name a commit"),
                           ("pr", "have a pull request"), ("branch", "have a branch"),
                           ("after_merge", "prove a criterion after a merge")):
            value = given.get(flag)
            if value is not None and value is not False and value != []:   # presence: `--pr 0` and `--branch ''` count
                _refuse_in_folder(state, what)

        def criterion(ident: str) -> dict:
            if ident not in state.criteria:
                raise PodError("criterion_unknown", f"There is no criterion {ident}.",
                               fix="use one of: " + ", ".join(state.criteria))
            return state.criteria[ident]

        observed = None
        if given.get("check"):
            observed = _run_check(root, given["check"])
            if given.get("met") and not given.get("facts") and not given.get("why") and (
                    observed["exit"] != 0 or "tree" not in observed):
                writer.append(observed)
                if observed["exit"] != 0:
                    raise PodError("check_failed", f"The check exited {observed['exit']}, so no criterion was recorded as met.",
                                   fix="correct the work and run the check again; its failed run is recorded")
                raise PodError("check_unbound", "The check ran on uncommitted or untracked files, so it proves nothing about a commit.",
                               fix="commit or remove them (ignore build leftovers in .gitignore) and run the check again")
            entries.append(observed)
            done.append(f"check exit {observed['exit']}" + (
                "" if observed.get("tree") else " (the tree was not one clean commit throughout: binds to no commit)"))
        for ident in given.get("met") or []:
            criterion(ident)
            named = [str(value) for value in given.get("facts") or []]
            if observed and not named and not given.get("why"):
                named = [str(len(writer.entries))]         # the check this same call ran and observed
            numbers = [int(value) for value in named if value.isdigit()]
            missing = [value for value in named if not value.isdigit()] + [
                value for value in numbers if not (observed and value == len(writer.entries))
                and (value >= len(writer.entries) or writer.entries[value]["kind"] != "fact")]
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
        if given.get("waive"):
            known = {row["id"] for row in proof.findings(root, state, gitio.commit(root))}
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
            given["paths"] = gitio.declared(given.get("paths"))
            if not given["paths"] or given.get("verdict") not in ("pass", "fail"):
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
        result = {"recorded": done, "entries": [row["n"] for row in writer.entries[-len(entries):]]}
        after = fold(writer.entries)
        if after.closed:           # its last criterion is met: tidy now, and again on any later record or deliver
            result.update(cleanup.tidy(writer, root, after, target_head=_target_head(root, after),
                                       again=f"pod record --objective {after.id} --note …" if folder(after) else "pod deliver"))
        return result


def _line(text: str, limit: int = 160) -> str:
    """One display line: whitespace collapsed, cut to a length that keeps output small."""
    return " ".join(text.split())[:limit]


def _words(given: dict) -> dict:
    return {"words": given["words"][:1000]} if given.get("words") else {}


def _may_decide(given: dict, what: str) -> None:
    """A person's own words always count; the coordinator's own decision only when the person allowed it."""
    if given.get("words") or routefile.load().merge_authorization == "coordinator":
        return
    raise PodError("person_decides", f"Only the person may {what} here.",
                   fix="ask them, then pass --words with what they said; or they set merge_authorization: coordinator")


def _run_check(root: Path, command: str) -> dict:
    """Run a check and record what was observed.

    It binds to a commit only if the tree was clean before the run and the same commit, with no
    tracked file changed, after it, so a result never attaches to content the check did not see.
    Files the check itself left behind, such as caches, do not unbind it.
    """
    before = None if gitio.dirty(root) else (gitio.commit(root), gitio.tree(root))
    try:
        done = subprocess.run(command, shell=True, cwd=root, capture_output=True, timeout=3600)
        code, tail = done.returncode, (done.stdout + done.stderr).decode(errors="replace")[-400:]
    except subprocess.TimeoutExpired:
        code, tail = None, "did not finish within one hour"
    bound = (before is not None and not gitio.dirty(root, untracked=False)
             and before == (gitio.commit(root), gitio.tree(root)))
    return {"kind": "fact", "fact": "check", "command": command[:300], "exit": code, "tail": tail, "source": "run by pod",
            **({"commit": before[0], "tree": before[1]} if bound else {})}


# --- status, resume, drop --------------------------------------------------------------------------

def _epoch(stamp: str | None) -> int | None:
    try:
        return int(datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def _route_record(route: str | None, tag: str | None = None, reason: str | None = None) -> dict | None:
    if not route:
        return None
    parts = (route.split("/") + [None, None, None])[:3]
    return {"agent": parts[0], "model": parts[1], "effort": None if parts[2] in (None, "None") else parts[2],
            "fallback_tag": tag, **({"fallback_reason": reason} if reason else {})}


def _assignment_records(state: State) -> tuple[list[dict], list[dict]]:
    """Typed per-assignment routes and events from one pass over the log (`pod status --json` only).

    Events keep log order. `dispatched` needs a confirmed launch (a recorded Orca dispatch), and an
    effective route is reported only where Orca's launch was observed; otherwise it is null."""
    rows: dict[str, dict] = {}
    merged = []
    for entry in state.entries:
        kind = entry.get("kind")
        if kind == "assignment":
            row = rows.setdefault(entry["id"], {"events": [], "first": entry.get("at")})
            if entry.get("dispatch") and not any(item["kind"] == "dispatched" for item in row["events"]):
                row["events"].append({"kind": "dispatched", "at": _epoch(entry.get("at")),
                                      "detail": str(state.assignments[entry["id"]].get("title", ""))[:80]})
        elif kind == "fact" and entry.get("fact") == "result" and entry.get("assignment") in rows:
            rows[entry["assignment"]]["events"].append(
                {"kind": "accepted", "at": _epoch(entry.get("at")), "detail": f"{entry.get('decision')}, {entry.get('outcome')}"})
        elif kind == "fact" and entry.get("fact") == "review" and entry.get("reviewer") in rows:
            rows[entry["reviewer"]]["events"].append(
                {"kind": "review_verdict", "at": _epoch(entry.get("at")), "detail": str(entry.get("verdict"))})
        elif kind == "effect" and entry.get("effect") == "merge" and entry.get("phase") == "outcome" and entry.get("merged") is True:
            merged.append({"kind": "merged", "at": _epoch(entry.get("at")), "detail": str(entry.get("observed") or "")[:120]})
    records = []
    for row in state.assignments.values():
        confirmed = bool(row.get("dispatch")) and not row.get("void")
        seen = row.get("effective") or (row.get("route") if row.get("effective_seen") else None)
        effective = None if (not confirmed or row.get("effective_unknown")) else seen
        records.append({"assignment": row["id"], "role": row.get("role"), "profile": row.get("profile"),
                        "title": row.get("title"), "void": bool(row.get("void")),
                        "requested_route": _route_record(row.get("route"), None, row.get("requested_review_fallback_reason")),
                        "effective_route": _route_record(effective, row.get("review_independence"),
                                                         row.get("review_fallback_reason")),
                        "events": rows.get(row["id"], {}).get("events", [])})
    return records, merged


def view(root: Path, state: State, *, remote: bool = True, gate: bool = True) -> dict:
    """The derived picture of one objective. `open` shows it without the gate, which has nothing to say yet."""
    from . import gate as gates
    info = state.objective
    source = info["source"]
    result = {"objective": state.id, "state": "dropped" if state.dropped else "closed" if state.closed else "open",
              "source": (f"{source['repository']}#{source['number']} {source['title']}" if source["kind"] == "issue"
                         else source["text"][:200]),
              "target": info["target"], "branch": info.get("branch"), "run": info.get("run"), "pr": info.get("pr")}
    if folder(state):
        result.update(worktree=info["worktree"], workspace_id=info["workspace_id"],
                      workspace_instance_id=info.get("workspace_instance_id"), workspace_kind="folder", delivery="none")
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
    found = [] if folder(state) else [f"{row['id']} {row['path'] or ''}: {_line(row['text'])}"
                                      for row in proof.open_required(root, state, gitio.commit(root))]
    if found:
        result["open_required_findings"] = found
    notes = _reviewer_note_lines(state)
    if notes:
        result["reviewer_notes"] = notes
    if not gate:
        return result
    if folder(state):
        result["gate"] = "nothing to deliver: a folder workspace objective closes when no criterion is open"
        return result
    verdict = gates.evaluate(root, state, remote=remote)
    result["candidate"] = f"{verdict['candidate'][:12]} tree {verdict['tree'][:12]}" + (
        "" if not verdict.get("dirty") else f", {len(verdict['dirty'])} uncommitted paths")
    if verdict["notes"]:
        result["notes"] = verdict["notes"]
    result["gate"] = verdict["needs"] or ("ready to deliver" if verdict["authorized"] else
                                           "passes; `pod deliver` still needs a merge authorization for this commit")
    return result


def status(cwd: Path, objective: str | None, *, remote: bool = True, typed: bool = False) -> dict:
    target, root, _, here = locate(cwd, objective)
    entries = log.read(target)
    if not entries:
        raise PodError("objective_unknown", f"There is no objective {target.stem}.", fix="run `pod status --all`")
    folded = guard(fold(entries), here)
    result = view(root, folded, remote=remote)
    if typed:                                  # the typed projection is for `--json` readers only
        result["assignment_records"], result["events"] = _assignment_records(folded)
    return result


def listing() -> dict:
    """The one read Pod offers other tools: open objectives and what they name. No provider is asked."""
    rows = []
    for target in log.every():
        try:
            state = fold(log.read(target))
        except PodError as exc:
            rows.append({"objective": target.stem, "repository": target.parent.name, "problem": exc.code})
            continue
        if not state.entries or state.closed:
            continue
        info = state.objective
        rows.append({"objective": state.id, "repository": info.get("repository") or target.parent.name,
                     "worktree": info.get("worktree"), "branch": info.get("branch"), "run": info.get("run"),
                     "pr": info.get("pr"), "target": "none" if folder(state) else info.get("target"), "opened": info.get("opened"),
                     "unaccepted": [{"assignment": row["id"], "task": row.get("task"), "dispatch": row.get("dispatch"),
                                     "browser_scope": row.get("browser_scope") if row.get("browser_scope") in
                                     routefile.BROWSER_SCOPES else "none"}
                                    for row in state.assignments.values() if not row.get("void") and not state.result(row["id"])]})
    return {"schema": "pod-objectives/v1", "objectives": rows}


def resume(cwd: Path, objective: str | None) -> dict:
    """Continue an objective from a new conversation. Orca moves the Run to it and fences the earlier one."""
    target, root, _, here = locate(cwd, objective)
    with log.writer(target) as writer:
        state = guard(fold(writer.entries), here)
        run = state.objective.get("run")
        if run and not state.closed:
            bound = orca.run_use(run)
            writer.append({"kind": "fact", "fact": "takeover", "run": run, "generation": bound.get("consumer_generation")})
            state = fold(writer.entries)
    return view(root, state)


def _target_head(root: Path, state: State) -> str | None:
    if folder(state):
        return None
    target = state.objective["target"]
    return gitio.fetch(root, "origin", target) or gitio.known(root, f"origin/{target}") or gitio.known(root, target)


def _folder_logs(objective: str, *, besides: Path | None = None) -> list[Path]:
    """Every other log whose folded objective has this id and is bound to an Orca folder workspace."""
    found = []
    for target in log.every():
        if target.stem != objective or target == besides:
            continue
        try:
            state = fold(log.read(target))
        except PodError:
            continue
        if state.entries and folder(state):         # an empty file left by a refused open is no objective
            found.append(target)
    return found


def _drop_target(cwd: Path, objective: str | None) -> tuple[Path, Path, dict | None]:
    """The log `pod drop` changes. Its own place's log comes first, as always; a folder objective
    whose workspace may be gone is found by id from anywhere, and an id that could mean either is refused."""
    try:
        target, root, _, here = locate(cwd, objective)
    except PodError as exc:
        if not objective or exc.code != "not_a_repository":
            raise
        target, root, here = None, None, None
    state = fold(log.read(target)) if target is not None and target.exists() else None
    if objective is None or (state and state.entries and (
            _same_workspace(state.objective, here) if here and folder(state)
            else not folder(state) and state.objective.get("worktree") == str(root))):
        return target, root, here                    # the objective of this very place
    others = _folder_logs(objective, besides=target)
    local = bool(state and state.entries)
    if len(others) + local > 1:
        raise PodError("objective_unclear", f"More than one objective is called {objective}: "
                       + ", ".join(str(path.parent.name) for path in ([target] if local else []) + others) + ".",
                       fix="run `pod drop` from the objective's own worktree or folder workspace")
    if local:
        return target, root, here
    if not others:
        raise PodError("objective_unknown", f"There is no objective {objective} here or in any folder project.",
                       fix="run `pod status --all`")
    return others[0], Path(fold(log.read(others[0])).objective.get("worktree") or cwd), None


def _gone_workspace(state: State, here: dict | None) -> dict | None:
    """Proof, read from Orca now, that a folder objective's own workspace no longer exists.

    None when this terminal is in that workspace. A workspace that still exists is refused: the
    objective is dropped from its own terminal."""
    if not folder(state) or (here is not None and _same_workspace(state.objective, here)):
        return None
    bound, instance = state.objective.get("workspace_id"), state.objective.get("workspace_instance_id")
    proof = orca.gone(bound, instance) if bound and instance else None
    if not proof:
        raise PodError("folder_binding_mismatch", f"Objective {state.id} is bound to workspace {bound} ({instance}), "
                       "which Orca still lists or cannot rule out.",
                       fix="run `pod drop` from the Orca terminal of the objective's own folder workspace")
    return {"workspace_id": bound, "instance_id": instance, "orca": proof}


def drop(cwd: Path, objective: str | None, *, why: str) -> dict:
    """Stop an objective. Its settled workers are released; unfinished or unmerged work is left where it is.

    A folder objective whose workspace Orca no longer lists can be dropped by id from any terminal;
    that proof is recorded with the drop."""
    target, root, here = _drop_target(cwd, objective)
    with log.writer(target) as writer:
        state = fold(writer.entries)
        gone = _gone_workspace(state, here)
        if gone is None:
            state = guard(state, here)
        if not state.dropped:
            writer.append({"kind": "objective", "dropped": True, "why": why, **({"workspace_gone": gone} if gone else {})})
        state = fold(writer.entries)
        return {"objective": state.id, "state": "dropped",
                **cleanup.tidy(writer, root, state, target_head=_target_head(root, state),
                               again=f"pod drop --objective {state.id} --why …")}
