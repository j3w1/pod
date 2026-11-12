"""The delivery gate. It is deterministic, makes no model call and performs at most one merge.

Five conditions, each read at the moment of use: the exact candidate, the required checks on
that head, review coverage and findings, the criteria, and an authorization. `evaluate` only
reads. `deliver` records its intent before it acts and reads the outcome back from GitHub, so
a repeated call after a lost response settles what happened instead of doing it again.
"""

from pathlib import Path

from . import github, gitio, issue, log, project, proof, routefile, verifier
from .errors import PodError
from .state import State, fold


def evaluate(root: Path, state: State, *, commit: str | None = None, remote: bool = True) -> dict:
    """The verdict for one exact candidate. Unknown is never counted as satisfied."""
    info = state.objective
    slug, target = info.get("repository"), info["target"]
    candidate = gitio.commit(root, commit or "HEAD")
    verdict = {"candidate": candidate, "tree": gitio.tree(root, candidate), "target": target, "needs": []}
    needs = verdict["needs"]
    if commit is None:
        verdict["dirty"] = gitio.dirty(root)
        if verdict["dirty"]:
            needs.append(f"commit or remove {len(verdict['dirty'])} uncommitted paths; the candidate is a clean tree")
    # 1. The candidate is the pull request head.
    request = None
    if not slug:
        needs.append("a GitHub origin; Pod delivers through a pull request")
    elif remote:
        request = github.pull_request(slug, str(info.get("pr") or info.get("branch") or ""))
        if not request:
            needs.append("a pull request for this branch (push, open one, then `pod record --pr N` if it is not found)")
        elif request["state"] != "MERGED" and request["headRefOid"] != candidate:
            needs.append(f"push: pull request {request['number']} is at {request['headRefOid'][:12]}, the candidate is {candidate[:12]}")
        elif request["baseRefName"] != target:
            needs.append(f"pull request {request['number']} targets {request['baseRefName']}, the objective targets {target}")
        elif request["state"] == "CLOSED":
            needs.append(f"pull request {request['number']} is closed")
        elif request.get("isDraft"):
            needs.append(f"pull request {request['number']} is a draft; mark it ready")
    verdict["pr"] = request
    # 2. Required checks are green on that head.
    base = (gitio.fetch(root, "origin", target) if remote else None) or gitio.merge_base(root, candidate, f"origin/{target}")
    rules = project.rules(root, base) if base else {**project.STRICT, "source": "unknown"}
    verdict["rules"] = rules
    if rules.get("problem"):
        needs.append(f"a readable {project.FILE} on {target} ({rules['problem']}); until then review is required")
    if request and request["state"] == "OPEN" and request["headRefOid"] == candidate:
        verdict["checks"] = found = github.checks(slug, request["number"], rules["required_checks"])
        if found["state"] not in ("green", "none"):
            waiting = ", ".join(f"{row['name']} {row['state'] or row['bucket']}" for row in found["rows"]
                                if row["bucket"] not in github.GREEN)
            needs.append(f"required checks on {candidate[:12]}: {waiting or found.get('problem') or found['state']}")
    # 3. Review coverage, and no required finding left open.
    merge_base = gitio.merge_base(root, candidate, base) if base else None
    if not merge_base:
        needs.append(f"the target branch {target}, which could not be read")
    elif rules["review"]:
        cover = proof.coverage(root, state, candidate, merge_base, final_tree=rules["final_tree_review"])
        verdict["uncovered"] = cover["uncovered"]
        if cover["uncovered"]:
            shown = ", ".join(cover["uncovered"][:8]) + (f" and {len(cover['uncovered']) - 8} more" if len(cover["uncovered"]) > 8 else "")
            needs.append(f"an independent passing review of {len(cover['uncovered'])} changed paths: {shown}")
    for finding in proof.open_required(state):
        needs.append(f"finding {finding['id']} resolved by a passing review of {finding['path'] or 'its scope'}, or waived")
    # 4. Every criterion that can be proved before the merge.
    for row in state.open_criteria(before_merge_only=True):
        needs.append(f"criterion {row['id']} met or dropped: {row['text'][:100]}")
    # 5. An authorization that covers this candidate.
    verdict["authorization"] = proof.authorization(state, repository=slug, target=target, candidate=candidate)
    verdict["pass"] = not needs
    verdict["authorized"] = verdict["authorization"] is not None
    return verdict


def _authorize(writer, state: State, verdict: dict) -> dict | None:
    """Find or obtain the authorization. The person's setting decides who may give it."""
    if verdict["authorization"]:
        return verdict["authorization"]
    preferences = routefile.load()
    info, request = state.objective, verdict["pr"]
    fact = {"kind": "fact", "fact": "authorization", "action": "merge", "repository": info["repository"],
            "target": info["target"], "candidate": verdict["candidate"]}
    if preferences.merge_authorization == "coordinator":
        return writer.append({**fact, "by": "coordinator"})[0]
    if preferences.authorization_verifier and request:
        vouched = verifier.ask(preferences.authorization_verifier, objective=state.id, repository=info["repository"],
                               target=info["target"], pr=request["number"], candidate=verdict["candidate"], tree=verdict["tree"])
        if vouched:
            return writer.append({**fact, "by": "verifier", "reference": vouched["reference"]})[0]
    return None


def public(verdict: dict) -> dict:
    """The verdict as a host or a coordinator reads it."""
    request = verdict.get("pr")
    return {"candidate": verdict["candidate"], "tree": verdict["tree"], "target": verdict["target"],
            "pr": request and request["number"], "pass": verdict["pass"], "authorized": verdict["authorized"],
            "needs": verdict["needs"]}


def deliver(cwd: Path, objective: str | None, *, check: bool = False, commit: str | None = None) -> dict:
    from .commands import locate
    target, root, _ = locate(cwd, objective)
    if check:
        return public(evaluate(root, fold(log.read(target)), commit=commit))
    with log.writer(target) as writer:
        state = fold(writer.entries)
        if state.dropped:
            raise PodError("objective_closed", f"Objective {state.id} was dropped.", fix="open a new objective")
        verdict = evaluate(root, state)
        request, candidate, info = verdict["pr"], verdict["candidate"], state.objective
        pending = next((effect for effect in state.merges() if not effect["outcome"]), None)
        done = next((effect for effect in state.merges() if state.merged(effect)
                     and effect["intent"]["head"] == candidate), None)
        # Settle before acting: a merge that already happened is recorded, never repeated.
        landed = request and request["state"] == "MERGED" and request["headRefOid"] == candidate
        on_target = not landed and _on_target(root, state, candidate)
        if not done and (landed or on_target):
            ident = pending["intent"]["id"] if pending and pending["intent"]["head"] == candidate else state.next_id("m", state.effects)
            if not (pending and pending["intent"]["id"] == ident):
                writer.append({"kind": "effect", "effect": "merge", "phase": "intent", "id": ident, "head": candidate,
                               "pr": request and request["number"], "target": info["target"], "performed_by": "another party"})
            writer.append({"kind": "effect", "effect": "merge", "phase": "outcome", "id": ident, "merged": True,
                           "observed": "pull request merged" if landed else f"candidate is on {info['target']}",
                           "merge_commit": ((request or {}).get("mergeCommit") or {}).get("oid") if landed else None})
            done = True
        if done:
            state = fold(writer.entries)
            return {"objective": state.id, "merged": candidate, **_update_issue(writer, state, request),
                    "state": "closed" if fold(writer.entries).closed else "open"}
        if pending and pending["intent"]["head"] != candidate:
            writer.append({"kind": "effect", "effect": "merge", "phase": "outcome", "id": pending["intent"]["id"],
                           "merged": False, "observed": "the candidate moved before the merge happened"})
        if not verdict["pass"]:
            raise PodError("gate_closed", "The candidate cannot be delivered yet.", detail={"needs": verdict["needs"]},
                           fix="satisfy each need, then run `pod deliver` again")
        if not _authorize(writer, state, verdict):
            raise PodError("authorization_required", f"No authorization covers merging {candidate[:12]} into {info['target']}.",
                           fix="ask the person, then `pod record --authorize --words 'what they said'` and deliver again")
        if "checks" in verdict:
            writer.append({"kind": "fact", "fact": "ci", "commit": candidate, "state": verdict["checks"]["state"],
                           "checks": verdict["checks"]["rows"]})
        ident = pending["intent"]["id"] if pending and pending["intent"]["head"] == candidate else None
        if not ident:
            ident = state.next_id("m", state.effects)
            writer.append({"kind": "effect", "effect": "merge", "phase": "intent", "id": ident, "head": candidate,
                           "pr": request["number"], "target": info["target"], "method": verdict["rules"]["merge_method"]})
        accepted, problem = github.merge(info["repository"], request["number"], candidate, verdict["rules"]["merge_method"])
        after = github.pull_request(info["repository"], str(request["number"]))
        if not after or after["state"] != "MERGED" or after["headRefOid"] != candidate:
            if after and after["state"] == "OPEN":
                writer.append({"kind": "effect", "effect": "merge", "phase": "outcome", "id": ident, "merged": False,
                               "observed": f"GitHub did not merge: {problem}"[:300]})
                raise PodError("merge_refused", f"GitHub did not merge pull request {request['number']}: {problem}",
                               fix="resolve what GitHub reported, then run `pod deliver` again")
            raise PodError("merge_unknown", "The merge was requested and its outcome could not be read.",
                           fix="run `pod deliver` again; it reads GitHub first and never merges twice")
        writer.append({"kind": "effect", "effect": "merge", "phase": "outcome", "id": ident, "merged": True,
                       "observed": "pull request merged", "merge_commit": (after.get("mergeCommit") or {}).get("oid")})
        state = fold(writer.entries)
        return {"objective": state.id, "merged": candidate, "merge_commit": (after.get("mergeCommit") or {}).get("oid"),
                **_update_issue(writer, state, after), "state": "closed" if fold(writer.entries).closed else "open"}


def _on_target(root: Path, state: State, candidate: str) -> bool:
    """Whether someone else delivered this exact candidate: it has work of its own and Git shows it on the target."""
    head = gitio.fetch(root, "origin", state.objective["target"])
    return bool(head) and candidate != state.objective.get("base") and gitio.is_ancestor(root, candidate, head)


def _update_issue(writer, state: State, request: dict | None) -> dict:
    """Tick the proved criteria on the source issue once and comment the evidence. Never untick."""
    source = state.objective["source"]
    ticked = {ident for effect in state.effects.values() if effect["intent"]["effect"] == "issue_update" and effect["outcome"]
              and effect["outcome"].get("updated") for ident in effect["intent"]["criteria"]}
    proved = [row for row in state.criteria.values() if row["state"] == "met" and row["id"] not in ticked]
    if source["kind"] != "issue" or not proved:
        return {}
    known = [source["digest"]] + [effect["outcome"]["digest"] for effect in state.effects.values()
                                  if effect["intent"]["effect"] == "issue_update" and effect["outcome"]
                                  and effect["outcome"].get("digest")]
    ident = state.next_id("u", state.effects)
    writer.append({"kind": "effect", "effect": "issue_update", "phase": "intent", "id": ident,
                   "criteria": [row["id"] for row in proved]})
    document = github.issue(source["repository"], source["number"])
    lines = [row["line"] for row in proved if "line" in row]
    edited, digest = False, None
    if lines and issue.digest(document["body"]) == known[-1]:
        body = issue.tick(document["body"], lines)
        edited = body != document["body"] and github.edit_issue(source["repository"], source["number"], body)
        digest = issue.digest(body) if edited else None
    where = f"pull request {request['number']}" if request else "the target branch"
    text = "\n".join([f"Delivered through {where}. Criteria proved:", ""] + [
        f"- {row['text']}" + (f" ({row['why']})" if row.get("why") else "") for row in proved] + (
        [] if edited or not lines else ["", "The issue text changed after the objective opened, so its checklist was left as it is."]))
    commented = github.comment_issue(source["repository"], source["number"], text)
    writer.append({"kind": "effect", "effect": "issue_update", "phase": "outcome", "id": ident,
                   "updated": bool(edited or commented), "edited": bool(edited), "commented": bool(commented), "digest": digest})
    return {"issue": ("checklist ticked" if edited else "checklist left unchanged") + (", evidence commented" if commented else ", comment failed")}
