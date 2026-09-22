"""The waste governor kernel: deterministic decisions over durable objective records.

Every test here runs without network, Orca, gh or a model. A fake GitHub port stands in
for the remote where execution is exercised; the port's own allowlist has its own tests.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from pod.config import effective
from pod.errors import PodError
from pod.governor import (classify_failure, decide, discover_triggers, enforcement, execute,
                          observe_candidate, prepare_candidate, reconcile, record_correction,
                          record_outcome, record_preflight, status)
from pod.ledger import checkpoint, reserve
from tests.common import establishment, fixture

NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)
COMMIT, TREE = "a" * 40, "c" * 40
COMMIT2, TREE2 = "b" * 40, "d" * 40
COMMIT3, TREE3 = "e" * 40, "f" * 40
BASE = "1" * 40
WORKFLOW = "2" * 64
BRANCH = {"remote": "origin", "branch": "agent/release", "base": "main"}
TARGET = "origin/agent/release"
# ci.yml is what a push triggers under PROJECT_CONFIG; release.yml is only ever dispatched.
RELEASE = "release.yml"
PROJECT_CONFIG = """schema: pod/v1
waste_governor:
  preflight: [unit, workflow-lint]
  triggers:
    push: ["workflow:ci.yml"]
    pr_update: ["workflow:ci.yml"]
"""


def authorization(candidate=COMMIT, tree=TREE, scope=("merge", "release")):
    return {"schema": "pod-release-authorization/v1", "candidate": candidate, "tree": tree,
            "scope": list(scope), "authorized_by": "owner", "utc": "2026-09-21T00:00:00Z",
            "reference": "tasks/pod/authorization.md"}


def action(kind="push", candidate=COMMIT, target=TARGET, unit="release", **extra):
    return {"kind": kind, "candidate": candidate, "target": target, "unit": unit,
            "reason": "converged candidate", **extra}


def dispatch(candidate=COMMIT, target="ci.yml", **extra):
    return action(kind="workflow_dispatch", candidate=candidate, target=target, **extra)


def diagnostic(candidate=COMMIT, check="permission probe", **extra):
    return action(kind="remote_diagnostic", candidate=candidate, target="probe.yml",
                  diagnostic={"question": "can the workflow actor read the ruleset field?",
                              "local_limitation": "local credentials do not represent that actor",
                              "check": check, "stopping_condition": "record the result; no unchanged repeat"},
                  **extra)


def body(candidate=COMMIT, gaps=()):
    return {"schema": "pod-checkpoint/v1", "criteria": ["works"], "plan_revision": "p",
            "candidate": candidate, "policy_revision": "r", "native_refs": [], "assignments": [],
            "questions": [], "verification_gaps": list(gaps), "next_safe_action": "inspect"}


def observation(commit=COMMIT, tree=TREE, *, dirty=0, workflows=None, verification=("unit",),
                policy="r", base=BASE, environment=None):
    return {"schema": "pod-candidate-observation/v1", "commit": commit, "tree": tree, "dirty_paths": dirty,
            "base": {"ref": "origin/main", "commit": base, "merge_base": base} if base else None,
            "workflows": {".github/workflows/ci.yml": WORKFLOW} if workflows is None else workflows,
            "verification": list(verification), "toolchain": {"python": "3.13.0"},
            "environment": environment or {}, "policy_revision": policy}


def correction(key, criterion="works"):
    return {"criterion_id": criterion, "failure_id": key, "obligation": "make the release boundary pass",
            "failing_example": "release-boundary test fails on the runner", "hypothesis": "an off-by-one",
            "last_meaningful_evidence": "the failing assertion",
            "next_discriminating_check": "run the single test", "correction_key": key}


class FakePort:
    """A remote that records what it was asked and can lose a response after acting."""

    def __init__(self, *, lost=(), rejected=(), delay=0.0, auto_ci=(), broken=(), blind=False):
        self.calls = []
        self.auto_ci = tuple(auto_ci)
        self.broken = set(broken)
        # A blind port starts runs but its readback sees none of them yet.
        self.blind = blind
        self.heads = {}
        self.prs = {}
        self.runs_by = {}
        self.run_status = {}
        self.next_run = 100
        self.lost = set(lost)
        self.rejected = set(rejected)
        self.delay = delay

    def branch_head(self, *, remote, branch):
        return self.heads.get(f"{remote}/{branch}")

    def push(self, *, remote, branch, commit, expected):
        self.calls.append(("push", remote, branch, commit, expected))
        key = f"{remote}/{branch}"
        if "push" in self.rejected or (expected is not None and self.heads.get(key) not in (expected, None)):
            return {"status": "rejected", "remote_head": self.heads.get(key)}
        self.heads[key] = commit
        for workflow in self.auto_ci:
            self._start(workflow, commit, event="push")
        if "push" in self.lost:
            raise PodError("remote_effect_uncertain", "lost")
        return {"status": "pushed", "remote_head": commit}

    def _start(self, workflow, commit, *, event):
        run = {"id": str(self.next_run), "status": "in_progress", "conclusion": None,
               "created_at": "2026-09-21T00:00:01+00:00", "url": "https://example.invalid/run",
               "event": event}
        self.next_run += 1
        self.runs_by.setdefault((workflow, commit), []).append(run)
        self.run_status[run["id"]] = run
        return run

    def pull_request(self, *, head, base):
        self.calls.append(("pr_list", head, base))
        if "pull_request" in self.broken:
            return "not a mapping"
        return self.prs.get((head, base))

    def open_pull_request(self, *, head, base, title, body):
        self.calls.append(("pr_create", head, base, title))
        record = {"number": 7, "url": "https://example.invalid/pull/7", "head_sha": None, "draft": False}
        self.prs[(head, base)] = record
        return record

    def dispatch(self, *, workflow, ref, inputs):
        self.calls.append(("dispatch", workflow, ref, dict(inputs)))
        if self.delay:
            time.sleep(self.delay)
        self._start(workflow, self.heads.get(f"origin/{ref}"), event="workflow_dispatch")
        if "dispatch" in self.lost:
            raise PodError("remote_effect_uncertain", "lost")
        return {"status": "dispatched"}

    def runs(self, *, workflow, commit):
        self.calls.append(("runs", workflow, commit))
        if self.blind:
            return []
        return [dict(run) for run in self.runs_by.get((workflow, commit), [])]

    def run(self, *, run_id):
        return dict(self.run_status[run_id])

    def rerun(self, *, run_id, failed_only):
        self.calls.append(("rerun", run_id, failed_only))
        self.run_status[run_id].update({"status": "in_progress", "conclusion": None})
        return {"status": "rerun_requested"}

    def cancel(self, *, run_id):
        self.calls.append(("cancel", run_id))
        if "cancel" in self.broken:
            raise PodError("gh_unavailable", "gh is not on PATH")
        self.run_status[run_id].update({"status": "completed", "conclusion": "cancelled"})
        return {"status": "cancel_requested"}

    def complete(self, run_id, conclusion):
        self.run_status[run_id].update({"status": "completed", "conclusion": conclusion})


class GovernorCase(unittest.TestCase):
    """Shared preparation: a checkpointed objective with one prepared delivery unit."""

    def setUp(self):
        self.stack = fixture()
        self.root = self.stack.__enter__()
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
                                           "XDG_CONFIG_HOME": str(self.root / "config")})
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        self.addCleanup(self.stack.__exit__, None, None, None)
        self.project = self.root / "project"
        self.project.mkdir(exist_ok=True)

    def configure(self, project_yaml=PROJECT_CONFIG, personal_yaml=None):
        if project_yaml is not None:
            (self.project / ".pod").mkdir(exist_ok=True)
            (self.project / ".pod" / "config.yaml").write_text(project_yaml)
        if personal_yaml is not None:
            personal = self.root / "config" / "pod" / "config.yaml"
            personal.parent.mkdir(parents=True, exist_ok=True)
            personal.write_text(personal_yaml)

    def prepared(self, *, project_yaml=PROJECT_CONFIG, personal_yaml=None, gaps=(), unit="release",
                 tasks=None, obs=None, branch=BRANCH):
        self.configure(project_yaml, personal_yaml)
        checkpoint(self.project, "objective", owner="owner", value=body(COMMIT, gaps),
                   native={"runtime": "runtime"})
        return prepare_candidate(self.project, "objective", owner="owner", unit=unit,
                                 observation=obs or observation(), branch=branch, tasks=tasks, now=NOW)

    def preflight(self, candidate, *, unit="release", checks=("unit", "workflow-lint"), status="PASS"):
        for check in checks:
            record_preflight(self.project, "objective", owner="owner", unit=unit, candidate=candidate,
                             check=check, status=status, now=NOW)

    def decide(self, request, **extra):
        return decide(self.project, "objective", owner="owner", action=request, now=NOW, **extra)

    def codes(self, result):
        return [reason["code"] for reason in result["reasons"]]


class CandidateTests(GovernorCase):
    def test_generations_open_only_at_explicit_boundaries(self):
        first = self.prepared()
        self.assertEqual((first["status"], first["candidate"]["generation"]), ("prepared", 1))
        again = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                  observation=observation(), now=NOW)
        self.assertEqual((again["status"], again["candidate"]["id"]), ("unchanged", first["candidate"]["id"]))
        changed = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                    observation=observation(workflows={".github/workflows/ci.yml": "3" * 64}),
                                    now=NOW)
        self.assertEqual((changed["status"], changed["candidate"]["generation"]), ("prepared", 2))
        self.assertEqual(changed["candidate"]["supersedes"], first["candidate"]["id"])
        self.assertEqual(changed["candidate"]["commit"], COMMIT)
        self.assertNotEqual(changed["candidate"]["id"], first["candidate"]["id"])

    def test_context_changes_invalidate_evidence_even_on_the_same_commit(self):
        first = self.prepared()["candidate"]
        self.preflight(first["id"])
        record_outcome(self.project, "objective", owner="owner", record_id=self.decide(action())["record_id"],
                       outcome="PASS")
        record_outcome(self.project, "objective", owner="owner",
                       record_id=self.decide(dispatch(candidate=first["id"], target=RELEASE))["record_id"],
                       outcome="PASS")
        self.assertEqual(self.decide(dispatch(candidate=first["id"], target=RELEASE))["decision"], "REUSE")
        for changed in (observation(workflows={".github/workflows/ci.yml": "3" * 64}),
                        observation(base="4" * 40), observation(policy="r2"),
                        observation(environment={"actor": "release-bot"}),
                        observation(commit=COMMIT2, tree=TREE2)):
            with self.subTest(changed=json.dumps(changed, sort_keys=True)[:60]):
                binding = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                            observation=changed, now=NOW)["candidate"]
                self.preflight(binding["id"])
                pushed = self.decide(action(candidate=binding["id"]))
                record_outcome(self.project, "objective", owner="owner", record_id=pushed["record_id"],
                               outcome="PASS")
                result = self.decide(dispatch(candidate=binding["id"], target=RELEASE))
                self.assertEqual(result["decision"], "ALLOW", result["explanation"])
                self.assertIsNone(result["reuse"])
                record_outcome(self.project, "objective", owner="owner", record_id=result["record_id"],
                               outcome="PASS")
                stale = self.decide(dispatch(candidate=first["id"], target=RELEASE))
                self.assertEqual((stale["decision"], self.codes(stale)), ("DEFER", ["superseded_candidate"]))

    def test_an_unfrozen_tree_cannot_cross_the_remote_boundary(self):
        binding = self.prepared(obs=observation(dirty=2))["candidate"]
        self.preflight(binding["id"])
        for request in (action(candidate=binding["id"]), dispatch(candidate=binding["id"])):
            result = self.decide(request)
            self.assertEqual(result["decision"], "DEFER")
            self.assertIn("candidate_unfrozen", self.codes(result))
            self.assertIn("commit or discard", result["next_action"])
        cleaned = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                    observation=observation(), now=NOW)
        self.assertEqual((cleaned["status"], cleaned["candidate"]["id"], cleaned["candidate"]["dirty_paths"]),
                         ("refreshed", binding["id"], 0))
        self.assertEqual(self.decide(action(candidate=binding["id"]))["decision"], "ALLOW")

    def test_observation_reads_git_and_never_the_caller(self):
        repo = self.root / "repo"
        repo.mkdir()
        def git(*argv):
            return subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, text=True, check=True,
                                  env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
        git("init", "-q", "-b", "main")
        (repo / ".github" / "workflows").mkdir(parents=True)
        (repo / ".github" / "workflows" / "ci.yml").write_text("on: [push]\n")
        (repo / "a.txt").write_text("one")
        git("add", ".")
        git("commit", "-q", "-m", "one")
        observed = observe_candidate(repo, base_ref="main", workflows=[".github/workflows/ci.yml"],
                                     verification=["unit"])
        self.assertEqual(observed["commit"], git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual(observed["tree"], git("rev-parse", "HEAD^{tree}").stdout.strip())
        self.assertEqual(observed["dirty_paths"], 0)
        self.assertEqual(observed["base"]["commit"], observed["commit"])
        self.assertEqual(len(observed["workflows"][".github/workflows/ci.yml"]), 64)
        self.assertIn("python", observed["toolchain"])
        (repo / "a.txt").write_text("two")
        self.assertEqual(observe_candidate(repo, base_ref="main")["dirty_paths"], 1)
        with self.assertRaises(PodError) as missing:
            observe_candidate(repo, base_ref="main", workflows=[".github/workflows/absent.yml"])
        self.assertEqual(missing.exception.code, "workflow_missing")
        with self.assertRaises(PodError) as forged:
            prepare_candidate(self.project, "objective", owner="owner", unit="release",
                              observation={**observation(), "commit": "not-a-commit"}, now=NOW)
        self.assertEqual(forged.exception.code, "invalid_observation")


class DecisionTests(GovernorCase):
    def test_effects_are_judged_not_the_verb(self):
        binding = self.prepared(project_yaml="schema: pod/v1\n")["candidate"]
        unknown = self.decide(action(candidate=binding["id"]))
        self.assertEqual((unknown["decision"], self.codes(unknown)), ("DEFER", ["effects_unknown"]))
        nothing = self.decide(action(candidate=binding["id"], effects=[]))
        self.assertEqual((nothing["decision"], nothing["purpose"]), ("ALLOW", "checkpoint"))
        self.prepared()
        mapped = self.decide(action(candidate=binding["id"], target=TARGET))
        self.assertEqual((mapped["decision"], mapped["purpose"], mapped["effect_source"]),
                         ("DEFER", "validation", "mapped"))
        self.assertIn("local_preflight_missing", self.codes(mapped))
        with self.assertRaises(PodError):
            self.decide(action(candidate=binding["id"], purpose="checkpoint"))

    def test_local_preflight_gates_remote_validation(self):
        binding = self.prepared()["candidate"]
        missing = self.decide(action(candidate=binding["id"]))
        self.assertEqual(missing["decision"], "DEFER")
        self.assertIn("missing: unit, workflow-lint", missing["reasons"][0]["detail"])
        self.assertTrue(missing["explanation"].startswith("DEFER: local_preflight_missing"))
        self.preflight(binding["id"], checks=("unit",))
        self.assertIn("workflow-lint", self.decide(action(candidate=binding["id"]))["reasons"][0]["detail"])
        self.preflight(binding["id"], checks=("workflow-lint",))
        self.assertEqual(self.decide(action(candidate=binding["id"]))["decision"], "ALLOW")
        self.preflight(binding["id"], checks=("unit",), status="FAILED")
        blocked = self.decide(action(candidate=binding["id"]))
        self.assertEqual(self.codes(blocked), ["blocking_findings"])
        self.assertEqual(blocked["reasons"][0]["class"], "correctness")
        with self.assertRaises(PodError) as stale:
            record_preflight(self.project, "objective", owner="owner", unit="release", candidate="x" * 40,
                             check="unit", status="PASS")
        self.assertEqual(stale.exception.code, "superseded_candidate")

    def test_reuse_attaches_to_a_running_action_and_returns_evidence(self):
        binding = self.prepared()["candidate"]
        self.preflight(binding["id"])
        first = self.decide(dispatch(candidate=binding["id"], target=RELEASE))
        self.assertEqual(self.codes(first), ["candidate_unpublished"])
        pushed = self.decide(action(candidate=binding["id"]))
        record_outcome(self.project, "objective", owner="owner", record_id=pushed["record_id"], outcome="PASS")
        first = self.decide(dispatch(candidate=binding["id"], target=RELEASE))
        self.assertEqual(first["decision"], "ALLOW")
        attached = self.decide(dispatch(candidate=binding["id"], target=RELEASE))
        self.assertEqual((attached["decision"], attached["reuse"]["kind"], attached["record_id"]),
                         ("REUSE", "attach", first["record_id"]))
        self.assertFalse(attached["recorded"])
        record_outcome(self.project, "objective", owner="owner", record_id=first["record_id"], outcome="PASS",
                       provider={"run_id": "100"}, now=NOW + timedelta(minutes=9))
        evidence = self.decide(dispatch(candidate=binding["id"], target=RELEASE))
        self.assertEqual((evidence["decision"], evidence["reuse"]["kind"]), ("REUSE", "evidence"))
        repeat = self.decide(action(candidate=binding["id"]))
        self.assertEqual((repeat["decision"], repeat["reuse"]["kind"]), ("REUSE", "already_done"))
        projection = status(self.project, "objective")
        self.assertEqual(projection["counters"]["attachments"], 1)
        self.assertEqual(projection["counters"]["evidence_reused"], 2)
        self.assertEqual(projection["units"]["release"]["published"][TARGET], COMMIT)
        row = [row for row in projection["actions"] if row["record_id"] == first["record_id"]][0]
        self.assertEqual((row["outcome"], row["provider"], row["commit"]), ("PASS", {"run_id": "100"}, COMMIT))
        # A reuse is not a row: nothing in the journal says a second run happened.
        self.assertEqual([r["kind"] for r in projection["actions"]], ["push", "workflow_dispatch", "workflow_dispatch"])
        self.assertEqual(projection["actions"][1]["derived_from"], pushed["record_id"])

    def test_a_publication_journals_the_validation_it_starts(self):
        binding = self.prepared()["candidate"]
        self.preflight(binding["id"])
        pushed = self.decide(action(candidate=binding["id"]))
        held = self.decide(dispatch(candidate=binding["id"]))
        self.assertEqual(self.codes(held), ["publication_unsettled"])
        record_outcome(self.project, "objective", owner="owner", record_id=pushed["record_id"], outcome="PASS")
        derived = status(self.project, "objective")["units"]["release"]["active_validation"]
        self.assertEqual([(row["kind"], row["target"], row["provider"]) for row in derived],
                         [("workflow_dispatch", "ci.yml", None)])
        attached = self.decide(dispatch(candidate=binding["id"]))
        self.assertEqual((attached["decision"], attached["reuse"]["kind"], attached["record_id"]),
                         ("REUSE", "attach", derived[0]["record_id"]))
        port = FakePort()
        port.heads[TARGET] = COMMIT
        run = port._start("ci.yml", COMMIT, event="push")
        found = reconcile(self.project, "objective", owner="owner", record_id=derived[0]["record_id"], port=port,
                          now=NOW)
        self.assertEqual((found["status"], found["receipt"]["provider"]["run_id"]), ("pending", run["id"]))
        port.complete(run["id"], "success")
        self.assertEqual(reconcile(self.project, "objective", owner="owner", record_id=derived[0]["record_id"],
                                   port=port, now=NOW)["status"], "PASS")
        self.assertEqual(self.decide(dispatch(candidate=binding["id"]))["reuse"]["kind"], "evidence")
        self.assertEqual(status(self.project, "objective")["phase"], "candidate")

    def test_a_dispatch_needs_the_candidate_on_the_branch_first(self):
        binding = self.prepared()["candidate"]
        self.preflight(binding["id"])
        early = self.decide(dispatch(candidate=binding["id"]))
        self.assertEqual(self.codes(early), ["candidate_unpublished"])
        pushed = self.decide(action(candidate=binding["id"]))
        record_outcome(self.project, "objective", owner="owner", record_id=pushed["record_id"], outcome="PASS")
        self.assertEqual(self.decide(dispatch(candidate=binding["id"]))["decision"], "REUSE")
        self.assertEqual(self.decide(dispatch(candidate=binding["id"], target=RELEASE))["decision"], "ALLOW")

    def test_authorization_binds_the_candidate_commit_and_tree(self):
        binding = self.prepared()["candidate"]
        self.preflight(binding["id"])
        for grant in (None, authorization(tree=TREE2), authorization(candidate=COMMIT2),
                      authorization(scope=("deploy",))):
            with self.subTest(grant=grant and (grant["candidate"][:2], grant["tree"][:2], grant["scope"])):
                held = self.decide(action(kind="merge", target="main", candidate=binding["id"],
                                          authorization=grant))
                self.assertEqual((held["decision"], self.codes(held)), ("DEFER", ["authorization_missing"]))
        clear = self.decide(action(kind="merge", target="main", candidate=binding["id"],
                                   authorization=authorization()))
        self.assertEqual((clear["decision"], clear["phase"]), ("ALLOW", "candidate"))
        gapped = self.prepared(gaps=("live matrix",), unit="other")["candidate"]
        self.preflight(gapped["id"], unit="other")
        release = self.decide(action(kind="release", target="v0.2.0", unit="other", candidate=gapped["id"],
                                     authorization=authorization()))
        self.assertEqual(self.codes(release), ["verification_gaps"])
        pushed = self.decide(action(unit="other", candidate=gapped["id"]))
        self.assertEqual(pushed["decision"], "ALLOW")
        self.assertIn("verification_gaps", [warning["code"] for warning in pushed["warnings"]])

    def test_a_deploying_push_needs_deploy_authorization(self):
        binding = self.prepared()["candidate"]
        self.preflight(binding["id"])
        held = self.decide(action(candidate=binding["id"], effects=["deploy:preview"]))
        self.assertEqual((held["purpose"], self.codes(held)), ("release", ["authorization_missing"]))
        allowed = self.decide(action(candidate=binding["id"], effects=["deploy:preview"],
                                     authorization=authorization(scope=("deploy",))))
        self.assertEqual(allowed["decision"], "ALLOW")

    def test_unrelated_work_does_not_block_an_independent_unit(self):
        self.prepared(tasks=["t1"])
        other = prepare_candidate(self.project, "objective", owner="owner", unit="urgent",
                                  observation=observation(commit=COMMIT2, tree=TREE2), tasks=["t2"],
                                  branch={"remote": "origin", "branch": "agent/urgent", "base": "main"},
                                  now=NOW)["candidate"]
        route = {"agent": "codex", "model": "m", "account": "a", "bucket": None, "effort": "high"}
        reserve(self.project, "objective", owner="owner", operation_id="op", requested=route,
                route_decision={"status": "usable", "selected": route,
                                "policy_revision": effective(self.project)["revision"]},
                establishment=establishment(route, runtime="runtime"),
                native_reader=lambda: {"runtime": "runtime", "authoritative": True, "owner": "owner",
                                       "scope": "all", "complete": True, "workers": [], "cross_host": False},
                capacity=2, run_id="run", plan_revision="p")
        from pod.ledger import _lock, _path, _read, _write
        path = _path(self.project, "objective")
        with _lock(path):
            state = _read(path)
            state["effects"]["op"]["state"] = "confirmed"
            state["effects"]["op"]["native_binding"] = {"dispatchId": "d", "workerId": "w", "taskId": "t1",
                                                       "runId": "run", "worktreeId": "wt",
                                                       "terminalHandle": None, "terminalResourceId": None}
            _write(path, state)
        release = self.decide(action(candidate=status(self.project, "objective")["units"]["release"]["candidate"]["id"]))
        self.assertIn("integration_unsettled", self.codes(release))
        self.preflight(other["id"], unit="urgent")
        urgent = self.decide(action(unit="urgent", candidate=other["id"], target="origin/agent/urgent"))
        self.assertEqual(urgent["decision"], "ALLOW", urgent["explanation"])
        self.assertEqual(self.decide(action(unit="nowhere", candidate=other["id"]))["reasons"][0]["code"],
                         "unit_unknown")

    def test_a_remote_only_question_gets_a_bounded_diagnostic_without_readiness(self):
        binding = self.prepared()["candidate"]
        probe = self.decide(diagnostic(candidate=binding["id"]))
        self.assertEqual(probe["decision"], "ALLOW", probe["explanation"])
        self.assertEqual(probe["purpose"], "diagnostic")
        # The lean governor's "one early remote diagnostic is allowed, and annotated".
        self.assertIn("early_diagnostic", [w["code"] for w in probe["warnings"]])
        record_outcome(self.project, "objective", owner="owner", record_id=probe["record_id"], outcome="PASS")
        answered = self.decide(diagnostic(candidate=binding["id"]))
        self.assertEqual((answered["decision"], answered["reuse"]["kind"]), ("REUSE", "evidence"))
        different = self.decide(diagnostic(candidate=binding["id"], check="runner python version"))
        self.assertEqual(different["decision"], "ALLOW")
        stale = self.decide(diagnostic(candidate="x" * 40))
        self.assertEqual(stale["decision"], "ALLOW")
        self.assertIn("superseded_candidate", [warning["code"] for warning in stale["warnings"]])
        self.assertEqual(self.decide(action(kind="release", target="v1", candidate=binding["id"],
                                            authorization=authorization()))["reasons"][0]["code"],
                         "local_preflight_missing")
        with self.assertRaises(PodError):
            self.decide({**diagnostic(candidate=binding["id"]), "diagnostic": {"question": "only"}})
        with self.assertRaises(PodError):
            self.decide({**action(candidate=binding["id"]), "diagnostic": {"question": "q", "local_limitation": "l",
                                                                        "check": "c", "stopping_condition": "s"}})


class FailureTests(GovernorCase):
    def validated(self):
        binding = self.prepared()["candidate"]
        self.preflight(binding["id"])
        pushed = self.decide(action(candidate=binding["id"]))
        record_outcome(self.project, "objective", owner="owner", record_id=pushed["record_id"], outcome="PASS")
        # The push started ci.yml; the derived row is the run, and a dispatch attaches to it.
        run = self.decide(dispatch(candidate=binding["id"]))
        self.assertEqual((run["decision"], run["reuse"]["kind"]), ("REUSE", "attach"))
        record_outcome(self.project, "objective", owner="owner", record_id=run["record_id"], outcome="FAILED",
                       provider={"run_id": "100"})
        return binding, run

    def classify(self, record_id, cls, **extra):
        return classify_failure(self.project, "objective", owner="owner", record_id=record_id,
                                classification={"class": cls, "reason": cls + " observed", **extra}, now=NOW)

    def test_a_changed_input_still_earns_a_rerun(self):
        """The lean governor's rerun rule, kept: anti-thrashing must not become anti-progress.

        An unbound unit has no generation digest to invalidate, so the question falls back
        to whether anything a rerun would consume has changed. It has — the correction is
        recorded in the intervention state the digest covers — so the attempt is allowed
        and annotated rather than deferred as a repeat.

        The other half of the rule, refusing an unchanged repeat, is asserted against a
        bound candidate in test_repeated_code_defects_go_through_the_intervention_rule,
        where the generation digest makes "unchanged" mean something stronger.
        """
        self.prepared()
        first = self.decide(dispatch(unit="default"))
        record_outcome(self.project, "objective", owner="owner",
                       record_id=first["record_id"], outcome="FAILED")
        self.classify(first["record_id"], "code_defect", correction=correction("fix the manifest"))
        changed = self.decide(dispatch(unit="default"))
        self.assertEqual(changed["decision"], "ALLOW")
        self.assertIn("necessary_rerun", [w["code"] for w in changed["warnings"]])

    def test_an_unchanged_probe_of_a_remote_only_failure_is_not_repeated(self):
        """The lean governor's repeat_diagnostic rule: asking the same question the same
        way teaches nothing, so the check must change or the answer must be recorded."""
        self.prepared()
        probe = self.decide(diagnostic(unit="default"))
        record_outcome(self.project, "objective", owner="owner",
                       record_id=probe["record_id"], outcome="FAILED")
        self.classify(probe["record_id"], "remote_only")
        repeated = self.decide(diagnostic(unit="default"))
        self.assertEqual(repeated["decision"], "DEFER")
        self.assertIn("repeat_diagnostic", self.codes(repeated))
        changed = self.decide(diagnostic(unit="default", check="runner python version"))
        self.assertEqual(changed["decision"], "ALLOW")

    def test_an_unclassified_failure_is_not_retried(self):
        binding, run = self.validated()
        held = self.decide(dispatch(candidate=binding["id"]))
        self.assertEqual((held["decision"], self.codes(held)), ("DEFER", ["failure_unclassified"]))
        self.assertEqual(held["reasons"][0]["class"], "correctness")
        with self.assertRaises(PodError) as wrong:
            self.classify(run["record_id"], "code_defect")
        self.assertEqual(wrong.exception.code, "invalid_classification")

    def test_transient_failures_get_a_bounded_retry(self):
        binding, run = self.validated()
        self.classify(run["record_id"], "transient")
        retry = self.decide(dispatch(candidate=binding["id"]))
        self.assertEqual(retry["decision"], "ALLOW")
        self.assertIn("transient_retry", [warning["code"] for warning in retry["warnings"]])
        record_outcome(self.project, "objective", owner="owner", record_id=retry["record_id"], outcome="FAILED")
        self.classify(retry["record_id"], "transient")
        spent = self.decide(dispatch(candidate=binding["id"]))
        self.assertEqual(self.codes(spent), ["transient_budget_exhausted"])
        self.classify(retry["record_id"], "external", evidence=["quota exhausted until reset"])
        blocked = self.decide(dispatch(candidate=binding["id"]))
        self.assertEqual((self.codes(blocked), blocked["reasons"][0]["class"]), (["external_blocker"], "correctness"))
        self.classify(retry["record_id"], "remote_only")
        remote = self.decide(dispatch(candidate=binding["id"]))
        self.assertEqual(self.codes(remote), ["remote_only_needs_diagnostic"])
        probe = self.decide(diagnostic(candidate=binding["id"]))
        self.assertEqual(probe["decision"], "ALLOW")
        rows = status(self.project, "objective")["actions"]
        self.assertEqual([row["attempt"] for row in rows if row["kind"] == "workflow_dispatch"], [1, 2])

    def test_repeated_code_defects_go_through_the_intervention_rule(self):
        binding, run = self.validated()
        self.classify(run["record_id"], "code_defect", correction=correction("first"))
        held = self.decide(dispatch(candidate=binding["id"]))
        self.assertEqual(self.codes(held), ["unchanged_rerun"])
        second = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                   observation=observation(commit=COMMIT2, tree=TREE2), now=NOW)["candidate"]
        self.preflight(second["id"])
        record_outcome(self.project, "objective", owner="owner",
                       record_id=self.decide(action(candidate=second["id"]))["record_id"], outcome="PASS")
        run2 = self.decide(dispatch(candidate=second["id"]))
        self.assertEqual((run2["decision"], run2["reuse"]["kind"]), ("REUSE", "attach"))
        record_outcome(self.project, "objective", owner="owner", record_id=run2["record_id"], outcome="FAILED")
        self.classify(run2["record_id"], "code_defect", correction=correction("second"))
        third = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                  observation=observation(commit=COMMIT3, tree=TREE3), now=NOW)["candidate"]
        self.preflight(third["id"])
        # The push itself starts CI here, so even publication waits for the diagnosis.
        blocked = self.decide(action(candidate=third["id"]))
        self.assertEqual((blocked["decision"], self.codes(blocked)), ("DEFER", ["diagnosis_required"]))
        self.assertEqual(blocked["reasons"][0]["class"], "correctness")
        with self.assertRaises(PodError) as reflex:
            record_correction(self.project, "objective", owner="owner", unit="release",
                              correction=correction("third"))
        self.assertEqual(reflex.exception.code, "diagnosis_required")
        (self.project / "probe.log").write_text("the runner's ruleset read returns 403")
        record_correction(self.project, "objective", owner="owner", unit="release",
                          correction=correction("third"), diagnosis={"diagnosis_evidence": "probe.log"})
        record_outcome(self.project, "objective", owner="owner",
                       record_id=self.decide(action(candidate=third["id"]))["record_id"], outcome="PASS")
        self.assertEqual(self.decide(dispatch(candidate=third["id"]))["decision"], "REUSE")
        with self.assertRaises(PodError) as conflict:
            record_outcome(self.project, "objective", owner="owner", record_id=run2["record_id"], outcome="PASS")
        self.assertEqual(conflict.exception.code, "outcome_conflict")
        projection = status(self.project, "objective")
        self.assertEqual(projection["counters"]["failures_interrupted"], 0)
        self.assertEqual(projection["pending_diagnosis"], [])

    def test_a_third_classification_without_diagnosis_is_interrupted(self):
        binding, run = self.validated()
        self.classify(run["record_id"], "code_defect", correction=correction("first"))
        for index, (commit, tree) in enumerate(((COMMIT2, TREE2), (COMMIT3, TREE3))):
            candidate = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                          observation=observation(commit=commit, tree=tree), now=NOW)["candidate"]
            self.preflight(candidate["id"])
            if index == 1:
                self.assertEqual(self.codes(self.decide(action(candidate=candidate["id"]))), ["diagnosis_required"])
                (self.project / "probe.log").write_text("evidence")
                record_correction(self.project, "objective", owner="owner", unit="release",
                                  correction=correction("third"), diagnosis={"diagnosis_evidence": "probe.log"})
            record_outcome(self.project, "objective", owner="owner",
                           record_id=self.decide(action(candidate=candidate["id"]))["record_id"], outcome="PASS")
            run = self.decide(dispatch(candidate=candidate["id"]))
            self.assertEqual(run["decision"], "REUSE")
            if index == 0:
                record_outcome(self.project, "objective", owner="owner", record_id=run["record_id"], outcome="FAILED")
                self.classify(run["record_id"], "code_defect", correction=correction("second"))
            else:
                record_outcome(self.project, "objective", owner="owner", record_id=run["record_id"], outcome="FAILED")
                with self.assertRaises(PodError) as interrupted:
                    self.classify(run["record_id"], "code_defect", correction=correction("fourth"))
                self.assertEqual(interrupted.exception.code, "diagnosis_required")
        self.assertEqual(status(self.project, "objective")["counters"]["failures_interrupted"], 1)


class PolicyTests(GovernorCase):
    GRANT = """schema: pod/v1
waste_governor:
  exceptions:
    - id: g1
      action: efficiency_exception
      objective: objective
      kinds: [workflow_dispatch]
      reason: the runner alone reproduces the ruleset read
      valid_until: "2027-01-01T00:00:00Z"
"""

    def test_an_exception_rejoins_a_personal_grant_and_lifts_only_efficiency(self):
        binding = self.prepared(personal_yaml=self.GRANT)["candidate"]
        record_outcome(self.project, "objective", owner="owner",
                       record_id=self.decide(action(candidate=binding["id"], effects=[]))["record_id"], outcome="PASS")
        held = self.decide(dispatch(candidate=binding["id"]))
        self.assertEqual(self.codes(held), ["local_preflight_missing"])
        softened = self.decide(dispatch(candidate=binding["id"]),
                               exception={"grant": "g1", "reason": "runner-only", "by": "owner"})
        self.assertEqual((softened["decision"], softened["exception"]["applied"]), ("ALLOW", True))
        self.assertEqual([w["code"] for w in softened["warnings"] if w.get("softened_by") == "exception"],
                         ["local_preflight_missing"])
        for bad in ({"grant": "absent", "reason": "r", "by": "o"},):
            with self.assertRaises(PodError) as unbound:
                self.decide(dispatch(candidate=binding["id"]), exception=bad)
            self.assertEqual(unbound.exception.code, "exception_grant_required")
        with self.assertRaises(PodError) as kind:
            self.decide(action(kind="merge", target="main", candidate=binding["id"]),
                        exception={"grant": "g1", "reason": "ship it", "by": "owner"})
        self.assertEqual(kind.exception.code, "exception_grant_required")
        merge_grant = self.GRANT.replace("[workflow_dispatch]", "[merge, workflow_dispatch]")
        self.configure(personal_yaml=merge_grant)
        held = self.decide(action(kind="merge", target="main", candidate=binding["id"]),
                           exception={"grant": "g1", "reason": "ship it", "by": "owner"})
        self.assertEqual((held["decision"], held["exception"]["ignored_because"]), ("DEFER", "authorization"))
        self.assertEqual(status(self.project, "objective")["counters"]["exceptions_applied"], 1)

    def test_a_project_file_cannot_widen_governor_authority(self):
        self.prepared(personal_yaml="schema: pod/v1\nwaste_governor:\n  cancel_superseded_validation: false\n")
        for widened in ("waste_governor:\n  mode: observe\n",
                        "waste_governor:\n  cancel_superseded_validation: true\n",
                        "waste_governor:\n  consolidate_related_changes: false\n",
                        "waste_governor:\n  transient_retries: 3\n",
                        "waste_governor:\n  host_control: docs/host.md\n",
                        self.GRANT.split("\n", 1)[1]):
            with self.subTest(widened=widened.splitlines()[1]):
                (self.project / ".pod" / "config.yaml").write_text("schema: pod/v1\n" + widened)
                with self.assertRaises(PodError) as refused:
                    effective(self.project)
                self.assertEqual(refused.exception.code, "authority_expansion")
        (self.project / ".pod" / "config.yaml").write_text(
            "schema: pod/v1\nwaste_governor:\n  mode: enforce\n  transient_retries: 0\n  preflight: [unit]\n")
        policy = effective(self.project)["policy"]["waste_governor"]
        self.assertEqual((policy["transient_retries"], policy["preflight"]), (0, ["unit"]))
        self.configure(personal_yaml=self.GRANT)
        (self.project / ".pod" / "config.yaml").write_text("schema: pod/v1\nwaste_governor:\n  exceptions: []\n")
        self.assertEqual([g["id"] for g in effective(self.project)["policy"]["waste_governor"]["exceptions"]], ["g1"])

    def test_observe_mode_records_what_enforcement_would_defer(self):
        binding = self.prepared(personal_yaml="schema: pod/v1\nwaste_governor:\n  mode: observe\n")["candidate"]
        observed = self.decide(action(candidate=binding["id"]))
        self.assertEqual((observed["decision"], observed["mode"], observed["exception"]["observed"]),
                         ("ALLOW", "observe", True))
        self.assertEqual([w["code"] for w in observed["warnings"] if w.get("softened_by") == "observe_mode"],
                         ["local_preflight_missing"])
        held = self.decide(action(kind="merge", target="main", candidate=binding["id"]))
        self.assertEqual(held["decision"], "DEFER")
        projection = status(self.project, "objective")
        self.assertEqual(projection["counters"]["observed_deferrals"], 1)
        self.assertEqual(projection["counters"]["deferrals"], {"local_preflight_missing": 1, "authorization_missing": 1})

    def test_enforcement_is_advisory_unless_the_host_declares_control(self):
        self.configure()
        advisory = enforcement(project=self.project)
        self.assertEqual((advisory["level"], advisory["tier"], advisory["ungoverned_routes"]),
                         ("advisory", "unavailable", "unknown"))
        self.configure(personal_yaml="schema: pod/v1\nwaste_governor:\n  host_control: docs/host-policy.md\n")
        declared = enforcement(project=self.project)
        self.assertEqual((declared["level"], declared["tier"]), ("declared", "owner_route_config"))
        self.assertNotIn("enforceable_control", json.dumps(declared))
        self.configure(personal_yaml="schema: pod/v1\nwaste_governor:\n  host_control: docs/host-policy.md\n  mode: observe\n")
        self.assertEqual(enforcement(project=self.project)["level"], "advisory")

    def test_trigger_discovery_proposes_and_never_applies(self):
        self.prepared(project_yaml="schema: pod/v1\n")
        workflows = self.project / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text(
            "on:\n  push:\n    branches: [main]\n    paths: ['src/**']\n  pull_request:\n  workflow_dispatch:\n"
            "concurrency:\n  group: ci-${{ github.ref }}\n  cancel-in-progress: true\njobs: {}\n")
        (workflows / "queue.yml").write_text("on: [merge_group]\njobs: {}\n")
        (workflows / "broken.yml").write_text(": : :\n")
        proposal = discover_triggers(self.project)
        self.assertTrue(proposal["proposal"])
        self.assertEqual(proposal["triggers"], {"pr_update": ["workflow:ci.yml"], "push": ["workflow:ci.yml"]})
        ci = proposal["workflows"]["ci.yml"]
        self.assertEqual((ci["dispatchable"], ci["cancel_in_progress"], ci["path_filtered"], ci["push_branches"]),
                         (True, True, True, ["main"]))
        self.assertTrue(proposal["workflows"]["queue.yml"]["merge_queue"])
        self.assertEqual(proposal["workflows"]["broken.yml"], {"error": "unreadable"})
        projection = status(self.project, "objective")
        self.assertEqual(projection["configured"]["triggers"], {})
        self.assertEqual(projection["trigger_proposal"]["triggers"], proposal["triggers"])
        held = self.decide(action(candidate=projection["units"]["release"]["candidate"]["id"]))
        self.assertEqual(self.codes(held), ["effects_unknown"])


class ExecutorTests(GovernorCase):
    def published(self, port=None, **prepared):
        binding = self.prepared(**prepared)["candidate"]
        self.preflight(binding["id"], unit=prepared.get("unit", "release"))
        remote = port or FakePort()
        return binding, remote

    def execute(self, request, port, **extra):
        return execute(self.project, "objective", owner="owner", action=request, port=port, now=NOW, **extra)

    def test_execution_targets_the_bound_commit_and_reuses_the_pull_request(self):
        binding, port = self.published()
        opened = self.execute(action(kind="pr_update", candidate=binding["id"]), port,
                              pull_request={"title": "Repair the release boundary", "body": "unit A"})
        self.assertEqual((opened["decision"], opened["outcome"]), ("ALLOW", "PASS"))
        self.assertEqual(port.calls[0], ("push", "origin", "agent/release", COMMIT, None))
        self.assertEqual(opened["receipt"]["provider"]["triggered"], {})
        self.assertEqual((opened["receipt"]["provider"]["pr"], opened["receipt"]["provider"]["pr_reused"]), (7, False))
        second = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                   observation=observation(commit=COMMIT2, tree=TREE2), now=NOW)["candidate"]
        self.preflight(second["id"])
        updated = self.execute(action(kind="pr_update", candidate=second["id"]), port)
        self.assertEqual(updated["outcome"], "PASS")
        self.assertEqual([call for call in port.calls if call[0] == "push"][-1],
                         ("push", "origin", "agent/release", COMMIT2, COMMIT))
        self.assertTrue(updated["receipt"]["provider"]["pr_reused"])
        self.assertEqual(status(self.project, "objective")["units"]["release"]["published"][TARGET], COMMIT2)
        with self.assertRaises(PodError) as wrong:
            self.execute(action(candidate=second["id"], target="origin/other"), port)
        self.assertEqual(wrong.exception.code, "target_mismatch")
        with self.assertRaises(PodError) as governance:
            self.execute(action(kind="merge", target="main", candidate=second["id"], authorization=authorization()), port)
        self.assertEqual(governance.exception.code, "invalid_action")
        moved = FakePort(rejected=("push",))
        third = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                  observation=observation(commit=COMMIT3, tree=TREE3), now=NOW)["candidate"]
        self.preflight(third["id"])
        rejected = self.execute(action(candidate=third["id"]), moved)
        self.assertEqual(rejected["outcome"], "FAILED")
        self.assertIn("branch_moved", rejected["receipt"]["detail"])

    def test_two_callers_admit_one_execution(self):
        binding, port = self.published(port=FakePort(delay=0.3))
        self.execute(action(candidate=binding["id"]), port)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: self.execute(dispatch(candidate=binding["id"], target=RELEASE), port),
                                     range(2)))
        self.assertEqual(sorted(result["decision"] for result in outcomes), ["ALLOW", "REUSE"])
        self.assertEqual([call for call in port.calls if call[0] == "dispatch"],
                         [("dispatch", RELEASE, "agent/release", {})])
        admitted = [result for result in outcomes if result["decision"] == "ALLOW"][0]
        self.assertEqual((admitted["outcome"], admitted["receipt"]["provider"]["run_id"]), ("pending", "100"))
        attached = [result for result in outcomes if result["decision"] == "REUSE"][0]
        self.assertEqual((attached["reuse"]["record_id"], attached["record_id"]),
                         (admitted["record_id"], admitted["record_id"]))

    def test_a_lost_submission_is_reconciled_before_another_one(self):
        binding, port = self.published(port=FakePort(lost=("dispatch",)))
        self.execute(action(candidate=binding["id"]), port)
        lost = self.execute(dispatch(candidate=binding["id"], target=RELEASE), port)
        self.assertEqual((lost["decision"], lost["outcome"]), ("ALLOW", "UNKNOWN"))
        held = self.decide(dispatch(candidate=binding["id"], target=RELEASE))
        self.assertEqual((held["decision"], self.codes(held)), ("DEFER", ["effect_unresolved"]))
        self.assertIn("governor-reconcile", held["next_action"])
        found = reconcile(self.project, "objective", owner="owner", record_id=lost["record_id"], port=port, now=NOW)
        self.assertEqual((found["status"], found["receipt"]["provider"]["run_id"]), ("pending", "100"))
        self.assertEqual(self.decide(dispatch(candidate=binding["id"], target=RELEASE))["reuse"]["kind"], "attach")
        port.complete("100", "success")
        passed = reconcile(self.project, "objective", owner="owner", record_id=lost["record_id"], port=port, now=NOW)
        self.assertEqual(passed["status"], "PASS")
        self.assertEqual(self.decide(dispatch(candidate=binding["id"], target=RELEASE))["reuse"]["kind"], "evidence")
        self.assertEqual(len([call for call in port.calls if call[0] == "dispatch"]), 1)
        unknown_push = FakePort(lost=("push",))
        second = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                   observation=observation(commit=COMMIT2, tree=TREE2), now=NOW)["candidate"]
        self.preflight(second["id"])
        pushed = self.execute(action(candidate=second["id"]), unknown_push)
        self.assertEqual(pushed["outcome"], "UNKNOWN")
        settled = reconcile(self.project, "objective", owner="owner", record_id=pushed["record_id"],
                            port=unknown_push, now=NOW)
        self.assertEqual(settled["status"], "PASS")
        self.assertEqual(status(self.project, "objective")["units"]["release"]["published"][TARGET], COMMIT2)

    def test_skipped_and_failed_runs_never_become_proof(self):
        binding, port = self.published()
        self.execute(action(candidate=binding["id"]), port)
        run = self.execute(dispatch(candidate=binding["id"], target=RELEASE), port)
        port.complete("100", "skipped")
        skipped = reconcile(self.project, "objective", owner="owner", record_id=run["record_id"], port=port, now=NOW)
        self.assertEqual(skipped["status"], "FAILED")
        self.assertIn("skipped", skipped["receipt"]["detail"])
        classify_failure(self.project, "objective", owner="owner", record_id=run["record_id"],
                         classification={"class": "transient", "reason": "runner never picked the job"}, now=NOW)
        rerun = self.execute(action(kind="validation_rerun", target=RELEASE, candidate=binding["id"]), port)
        self.assertEqual((rerun["outcome"], rerun["receipt"]["provider"]["run_id"]), ("pending", "100"))
        self.assertEqual(port.calls[-1], ("rerun", "100", True))

    def test_supersedence_cancels_only_safe_validation(self):
        binding, port = self.published()
        pushed = self.execute(action(candidate=binding["id"]), port)
        running = self.execute(dispatch(candidate=binding["id"], target=RELEASE), port)
        deploying = self.decide(dispatch(candidate=binding["id"], target="deploy.yml", effects=["deploy:preview"],
                                         authorization=authorization(scope=("deploy",))))
        self.assertEqual(deploying["decision"], "ALLOW")
        second = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                   observation=observation(commit=COMMIT2, tree=TREE2), now=NOW)
        derived = [row["record_id"] for row in status(self.project, "objective")["actions"]
                   if row["derived_from"] == pushed["record_id"]]
        self.assertEqual(second["superseded_validation"], derived + [running["record_id"]])
        self.preflight(second["candidate"]["id"])
        pushed = self.execute(action(candidate=second["candidate"]["id"]), port)
        self.assertEqual([call for call in port.calls if call[0] == "cancel"], [("cancel", "100")])
        self.assertEqual([(row["target"], row["decision"], row["outcome"]) for row in pushed["cancellations"]],
                         [(derived[0], "DEFER", None), (running["record_id"], "ALLOW", "PASS")])
        projection = status(self.project, "objective")
        by_id = {row["record_id"]: row for row in projection["actions"]}
        self.assertEqual(by_id[running["record_id"]]["outcome"], "CANCELED")
        self.assertEqual(by_id[deploying["record_id"]]["outcome"], "pending")
        self.assertEqual(projection["counters"]["cancellations"], 1)
        record_outcome(self.project, "objective", owner="owner", record_id=running["record_id"], outcome="CANCELED")
        fresh = self.execute(dispatch(candidate=second["candidate"]["id"], target=RELEASE), port)
        self.assertEqual((fresh["decision"], fresh["reuse"]), ("ALLOW", None))
        self.configure(personal_yaml="schema: pod/v1\nwaste_governor:\n  cancel_superseded_validation: false\n")
        third = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                  observation=observation(commit=COMMIT3, tree=TREE3), now=NOW)
        self.preflight(third["candidate"]["id"])
        kept = self.execute(action(candidate=third["candidate"]["id"]), port)
        self.assertEqual(kept["cancellations"], [])
        self.assertIn("superseded_validation_pending", [w["code"] for w in kept["warnings"]])

    def test_execution_needs_a_prepared_unit(self):
        self.configure()
        checkpoint(self.project, "objective", owner="owner", value=body(), native={"runtime": "runtime"})
        held = self.execute(action(unit="default", effects=[]), FakePort())
        self.assertEqual((held["decision"], self.codes(held)), ("DEFER", ["unit_unbound"]))
        cancel = self.execute(action(kind="cancel_validation", unit="default", target="x"), FakePort())
        self.assertEqual(self.codes(cancel), ["unit_unbound"])

    def test_a_failed_cancellation_does_not_strand_the_primary_action(self):
        binding, port = self.published(port=FakePort(broken=("cancel",)))
        self.execute(action(candidate=binding["id"]), port)
        running = self.execute(dispatch(candidate=binding["id"], target=RELEASE), port)
        second = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                   observation=observation(commit=COMMIT2, tree=TREE2), now=NOW)["candidate"]
        self.preflight(second["id"])
        pushed = self.execute(action(candidate=second["id"]), port)
        self.assertEqual((pushed["decision"], pushed["outcome"]), ("ALLOW", "PASS"))
        failed = [row for row in pushed["cancellations"] if row["target"] == running["record_id"]]
        self.assertEqual((failed[0]["outcome"], failed[0]["detail"]), ("FAILED", "gh_unavailable"))
        by_id = {row["record_id"]: row for row in status(self.project, "objective")["actions"]}
        self.assertEqual(by_id[running["record_id"]]["outcome"], "pending")
        self.assertEqual(by_id[pushed["record_id"]]["outcome"], "PASS")
        self.assertEqual([row["outcome"] for row in by_id.values() if row["kind"] == "cancel_validation"], ["FAILED"])

    def test_an_unexpected_port_answer_leaves_the_row_unknown(self):
        binding, port = self.published(port=FakePort(broken=("pull_request",)))
        with self.assertRaises(AttributeError):
            self.execute(action(kind="pr_update", candidate=binding["id"]), port)
        row = status(self.project, "objective")["actions"][-1]
        self.assertEqual((row["kind"], row["outcome"]), ("pr_update", "UNKNOWN"))
        self.assertEqual(self.codes(self.decide(action(kind="pr_update", candidate=binding["id"]))),
                         ["effect_unresolved"])

    def test_a_superseded_row_is_read_back_by_its_own_commit(self):
        binding, port = self.published(port=FakePort(lost=("push",), blind=True))
        pushed = self.execute(action(candidate=binding["id"]), port)
        self.assertEqual(pushed["outcome"], "UNKNOWN")
        second = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                   observation=observation(commit=COMMIT2, tree=TREE2), now=NOW)["candidate"]
        # The lost push belongs to a superseded generation; readback still settles it by its
        # own commit, and the branch is recorded as carrying that commit.
        settled = reconcile(self.project, "objective", owner="owner", record_id=pushed["record_id"], port=port, now=NOW)
        self.assertEqual(settled["status"], "PASS")
        self.assertEqual(status(self.project, "objective")["units"]["release"]["published"][TARGET], COMMIT)
        self.preflight(second["id"])
        port.lost.clear()
        self.execute(action(candidate=second["id"]), port)
        newer = self.execute(dispatch(candidate=second["id"], target=RELEASE), port)
        self.assertEqual((newer["outcome"], newer["receipt"]["provider"]), ("pending", {"ref_commit": COMMIT2}))
        third = prepare_candidate(self.project, "objective", owner="owner", unit="release",
                                  observation=observation(commit=COMMIT3, tree=TREE3), now=NOW)["candidate"]
        self.preflight(third["id"])
        self.execute(action(candidate=third["id"]), port)
        latest = self.execute(dispatch(candidate=third["id"], target=RELEASE), port)
        port.blind = False
        older = reconcile(self.project, "objective", owner="owner", record_id=newer["record_id"], port=port, now=NOW)
        self.assertEqual((older["status"], older["receipt"]["provider"]["run_id"],
                          older["receipt"]["provider"]["ref_commit"]), ("pending", "100", COMMIT2))
        current = reconcile(self.project, "objective", owner="owner", record_id=latest["record_id"], port=port, now=NOW)
        self.assertEqual(current["receipt"]["provider"]["run_id"], "101")
        by_id = {row["record_id"]: row for row in status(self.project, "objective")["actions"]}
        self.assertEqual((by_id[pushed["record_id"]]["commit"], by_id[newer["record_id"]]["commit"]),
                         (COMMIT, COMMIT2))

    def test_run_readback_tolerates_second_precision_timestamps(self):
        from pod.governor import _pick_run
        runs = [{"id": "1", "created_at": "2026-09-21T00:00:00Z"}]
        self.assertEqual(_pick_run(runs, since="2026-09-21T00:00:00.600000+00:00"), runs[0])
        self.assertIsNone(_pick_run(runs, since="2026-09-21T00:00:01+00:00"))


class RecoveryTests(GovernorCase):
    def legacy_journal(self, kind, outcome, target=TARGET):
        """Replace the journal with a pod-governor/v1 one holding a row for this action.

        The row is spelled so the upgrade produces the *same* logical key the request
        below computes — same kind, unit, candidate, target and effects. That collision is
        the whole risk: a v1 row that could not be matched could never be misused.
        """
        from pod.governor import _record_path
        from pod.util import atomic_json
        atomic_json(_record_path(self.project, "objective"), {
            "schema": "pod-governor/v1", "revision": 1, "actions": [
                {"record_id": "carried-forward", "action": {"kind": kind, "candidate": COMMIT,
                                                            "target": target, "reason": "v1"},
                 "decision": "ALLOW", "phase": "candidate", "at": "2026-09-20T00:00:00+00:00",
                 "outcome": outcome, "inputs": "i", "override": None, "reasons": []}]})

    def settled_objective(self):
        """A converged unit with nothing outstanding, so only the v1 row is under test."""
        binding = self.prepared()["candidate"]
        self.preflight(binding["id"])
        pushed = self.decide(action(candidate=binding["id"]))
        record_outcome(self.project, "objective", owner="owner",
                       record_id=pushed["record_id"], outcome="PASS")

    def test_a_v1_pass_is_not_promoted_to_v2_proof(self):
        """The upgrade nulls commit, workflow, base and environment, because v1 froze none
        of them. A row that cannot show it covers the same work is not evidence that the
        work need not be repeated, however exactly the logical key happens to match."""
        self.settled_objective()
        self.legacy_journal("workflow_dispatch", "PASS", target="ci.yml")
        decided = self.decide(dispatch(unit="default"))
        self.assertEqual(decided["decision"], "ALLOW")
        self.assertIsNone(decided["reuse"])
        self.assertIn("legacy_evidence_ignored", [w["code"] for w in decided["warnings"]])

    def test_a_v1_completed_publication_is_not_reused_either(self):
        self.settled_objective()
        self.legacy_journal("push", "PASS")
        decided = self.decide(action(unit="default"))
        self.assertEqual((decided["decision"], decided["reuse"]), ("ALLOW", None))
        self.assertIn("legacy_evidence_ignored", [w["code"] for w in decided["warnings"]])

    def test_a_v1_row_left_running_is_unresolved_rather_than_attached(self):
        """v1 recorded no provider or run identity, so there is nothing to attach to and
        nothing reconciliation could read back. Deferring says so instead of pretending."""
        for outcome in ("pending", "UNKNOWN"):
            with self.subTest(outcome):
                self.setUp()
                self.settled_objective()
                self.legacy_journal("workflow_dispatch", outcome, target="ci.yml")
                decided = self.decide(dispatch(unit="default"))
                self.assertEqual(decided["decision"], "DEFER")
                self.assertIn("effect_unresolved", [r["code"] for r in decided["reasons"]])
                self.assertIsNone(decided["reuse"])

    def test_state_survives_a_restart_and_a_legacy_journal(self):
        binding = self.prepared()["candidate"]
        self.preflight(binding["id"])
        pushed = self.decide(action(candidate=binding["id"]))
        record_outcome(self.project, "objective", owner="owner", record_id=pushed["record_id"], outcome="PASS")
        run = self.decide(dispatch(candidate=binding["id"]))
        script = ("import json,sys; from pathlib import Path; from pod.governor import status; "
                  "print(json.dumps(status(Path(sys.argv[1]), 'objective')))")
        resumed = json.loads(subprocess.run([sys.executable, "-c", script, str(self.project)],
                                            capture_output=True, text=True, check=True,
                                            env=os.environ.copy()).stdout)
        unit = resumed["units"]["release"]
        self.assertEqual((unit["generation"], unit["candidate"]["commit"], unit["preflight"]),
                         (1, COMMIT, {"unit": "PASS", "workflow-lint": "PASS"}))
        self.assertEqual([row["record_id"] for row in unit["active_validation"]], [run["record_id"]])
        self.assertEqual((run["decision"], unit["last_decision"]["decision"]), ("REUSE", "REUSE"))
        self.assertEqual(resumed["enforcement"]["level"], "advisory")
        from pod.governor import _record_path
        from pod.util import atomic_json
        legacy = {"schema": "pod-governor/v1", "revision": 3, "actions": [
            {"record_id": "old", "action": {"kind": "push", "candidate": COMMIT, "target": TARGET,
                                            "reason": "legacy"},
             "decision": "WARN", "phase": "candidate", "at": "2026-09-20T00:00:00+00:00",
             "outcome": "PASS", "inputs": "i", "override": None, "reasons": []}]}
        atomic_json(_record_path(self.project, "objective"), legacy)
        upgraded = status(self.project, "objective")
        self.assertEqual(upgraded["actions"][0]["decision"], "ALLOW")
        self.assertEqual(upgraded["units"], {})
        # effects=[] differs from the null the upgrade writes, so this request does not
        # collide with the carried-forward row at all; the colliding case is covered above.
        self.assertEqual(self.decide(action(unit="default", effects=[]))["decision"], "ALLOW")
        self.assertEqual(status(self.project, "objective")["actions"][0]["record_id"], "old")
        crowded = {**legacy, "actions": [{**legacy["actions"][0], "record_id": f"old{index}",
                                          "outcome": "pending" if index == 0 else "PASS"} for index in range(230)]}
        atomic_json(_record_path(self.project, "objective"), crowded)
        self.assertEqual(self.decide(action(unit="default", effects=[]))["decision"], "ALLOW")
        after = status(self.project, "objective")
        self.assertEqual(after["counters"]["legacy_rows_dropped"], 150)
        from pod.governor import _read_journal
        journal = _read_journal(_record_path(self.project, "objective"))
        self.assertEqual(len(journal["actions"]), 81)
        self.assertEqual(journal["actions"][0]["record_id"], "old0")
        self.assertEqual([row["outcome"] for row in journal["actions"][:2]], ["pending", "PASS"])
        self.assertEqual(self.decide(action(unit="default", effects=[], target="origin/other"))["decision"], "ALLOW")

    def test_a_malformed_journal_asks_for_migration(self):
        self.prepared()
        from pod.governor import _record_path
        from pod.util import atomic_json
        for broken in ({"schema": "pod-governor/v2", "revision": 1, "units": {}, "actions": [{}], "counters": {}},
                       {"schema": "pod-governor/v2", "revision": 1, "units": {"u": {}}, "actions": [], "counters": {}},
                       {"schema": "pod-governor/v9", "revision": 1, "units": {}, "actions": [], "counters": {}},
                       {"schema": "pod-governor/v1", "revision": 1, "actions": "not a list"}):
            with self.subTest(broken=str(broken)[:48]):
                atomic_json(_record_path(self.project, "objective"), broken)
                with self.assertRaises(PodError) as caught:
                    self.decide(action(effects=[]))
                self.assertEqual(caught.exception.code, "state_migration_required")

    def test_malformed_actions_and_foreign_coordinators_are_refused(self):
        self.prepared()
        for broken in ({**action(), "kind": "publish"}, {**action(), "extra": True},
                       {**action(), "effects": ["shell:rm"]}, {**action(), "inputs": {"k": "v"}},
                       {**action(), "unit": "../x"}):
            with self.subTest(broken=sorted(broken)):
                with self.assertRaises(PodError):
                    self.decide(broken)
        with self.assertRaises(PodError) as foreign:
            decide(self.project, "objective", owner="other", action=action(), now=NOW)
        self.assertEqual(foreign.exception.code, "coordinator_conflict")

    def test_the_governor_and_the_release_gate_agree_on_an_authorization(self):
        from pod.release import validate_authorization as gate_validator
        binding = self.prepared()["candidate"]
        self.preflight(binding["id"])
        for broken in ({**authorization(), "candidate": "short"},
                       {**authorization(), "utc": "2026-09-21T00:00:00+00:00"},
                       {**authorization(), "scope": ["publish"]}):
            with self.subTest(broken=str(broken["scope"])):
                with self.assertRaises(PodError):
                    gate_validator(broken, candidate=COMMIT, tree=TREE)
                with self.assertRaises(PodError):
                    self.decide(action(kind="merge", target="main", candidate=binding["id"], authorization=broken))
