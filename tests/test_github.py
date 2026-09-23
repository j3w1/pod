"""The GitHub port: exact argv shapes, and what each response is read as."""

import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pod.config import effective, personal_path
from pod.errors import PodError
from pod.github import (AMENDMENT_JQ, GhPort, ISSUE_FIELDS, PR_FIELDS, RUN_FIELDS,
                        gh_allowed, git_allowed,
                        issue_intake, issue_recheck, repository_context)
from pod.ledger import checkpoint, objective_root, read, state_root
from pod.setup import inspect
from pod.util import digest
from tests.common import fixture

COMMIT = "a" * 40
OLD = "b" * 40


def completed(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def git(project: Path, *argv: str) -> str:
    return subprocess.run(["git", "-C", str(project), *argv], capture_output=True,
                          text=True, check=True).stdout.strip()


def repository(root: Path, remote: str = "https://github.com/acme/widgets.git") -> tuple[Path, Path]:
    main = root / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(main)], check=True, capture_output=True)
    git(main, "config", "user.name", "Fixture")
    git(main, "config", "user.email", "fixture@example.invalid")
    (main / "README.md").write_text("fixture\n")
    git(main, "add", "README.md")
    git(main, "commit", "-m", "fixture")
    git(main, "remote", "add", "origin", remote)
    worktree = root / "objective"
    git(main, "worktree", "add", "-b", "orca/issue-7", str(worktree))
    return main, worktree


class IssuePort:
    def __init__(self, *, body="Complete issue body", state="OPEN",
                 updated="2026-09-23T00:00:00Z", amendment="Decision", amendment_issue=7):
        self.body, self.state, self.updated = body, state, updated
        self.amendment_body, self.amendment_issue = amendment, amendment_issue

    def issue(self, *, repository, number):
        return {"number": number, "title": "Implement widgets", "body": self.body,
                "state": self.state, "url": f"https://github.com/{repository}/issues/{number}",
                "updatedAt": self.updated}

    def amendment(self, *, repository, number, comment):
        return {"id": comment, "body": self.amendment_body,
                "url": f"https://github.com/{repository}/issues/{self.amendment_issue}#issuecomment-{comment}",
                "updatedAt": self.updated}


class AllowlistTests(unittest.TestCase):
    def test_git_shapes(self):
        self.assertTrue(git_allowed(["push", "--porcelain", "origin", f"{COMMIT}:refs/heads/agent/x"]))
        self.assertTrue(git_allowed(["push", "--porcelain", f"--force-with-lease=refs/heads/agent/x:{OLD}",
                                     "origin", f"{COMMIT}:refs/heads/agent/x"]))
        self.assertTrue(git_allowed(["ls-remote", "--heads", "origin", "refs/heads/agent/x"]))
        for refused in (["push", "origin", "HEAD:refs/heads/agent/x"],
                        ["push", "--porcelain", "origin", "HEAD:refs/heads/agent/x"],
                        ["push", "--porcelain", "--force", "origin", f"{COMMIT}:refs/heads/agent/x"],
                        ["push", "--porcelain", "origin", f"{COMMIT}:refs/heads/../x"],
                        ["push", "--porcelain", "--force-with-lease=refs/heads/agent/x", "origin",
                         f"{COMMIT}:refs/heads/agent/x"],
                        ["push", "--porcelain", "-o", "ci.skip", "origin", f"{COMMIT}:refs/heads/agent/x"],
                        ["ls-remote", "--heads", "origin", "agent/x"],
                        ["fetch", "origin"]):
            with self.subTest(refused=refused):
                self.assertFalse(git_allowed(refused))

    def test_gh_shapes(self):
        allowed = (["pr", "list", "--head", "agent/x", "--base", "main", "--state", "open", "--json", PR_FIELDS,
                    "--limit", "5"],
                   ["pr", "create", "--head", "agent/x", "--base", "main", "--title", "t", "--body", ""],
                   ["workflow", "run", "ci.yml", "--ref", "agent/x"],
                   ["workflow", "run", "ci.yml", "--ref", "agent/x", "-f", "probe=ruleset"],
                   ["run", "list", "--workflow", "ci.yml", "--commit", COMMIT, "--json", RUN_FIELDS, "--limit", "20"],
                   ["run", "view", "123", "--json", RUN_FIELDS],
                   ["issue", "view", "7", "--repo", "acme/widgets", "--json", ISSUE_FIELDS],
                   ["api", "repos/acme/widgets/issues/comments/99", "--jq", AMENDMENT_JQ],
                   ["run", "cancel", "123"], ["run", "rerun", "123"], ["run", "rerun", "123", "--failed"])
        for argv in allowed:
            with self.subTest(argv=argv[:3]):
                self.assertTrue(gh_allowed(argv))
        for refused in (["pr", "merge", "7"], ["release", "create", "v1"], ["api", "repos/x/y"],
                        ["workflow", "run", "ci.yml", "--ref", "agent/x", "-f", "bad key=1"],
                        ["workflow", "run", "ci.yml", "--ref", "agent/x", "--json"],
                        ["workflow", "run", "ci", "--ref", "agent/x"],
                        ["run", "view", "abc", "--json", RUN_FIELDS],
                        ["run", "cancel", "123", "--force"],
                        ["issue", "view", "7", "--repo", "acme/widgets"],
                        ["pr", "list", "--head", "agent/x", "--base", "main", "--state", "all", "--json", PR_FIELDS,
                         "--limit", "5"],
                        ["pr", "create", "--head", "agent/x", "--base", "main", "--title", "", "--body", ""]):
            with self.subTest(refused=refused[:4]):
                self.assertFalse(gh_allowed(refused))

    def test_no_process_starts_for_a_refused_shape(self):
        port = GhPort(Path("."))
        with patch("pod.github.subprocess.run", side_effect=AssertionError("process started")), \
                patch("pod.github.gh_executable", return_value=Path("/usr/bin/gh")), \
                patch("pod.github.git_executable", return_value=Path("/usr/bin/git")):
            with self.assertRaises(PodError) as git:
                port._git(["push", "origin", "HEAD"], mutation=True)
            self.assertEqual(git.exception.code, "unsupported_git_operation")
            with self.assertRaises(PodError) as gh:
                port._gh(["pr", "merge", "7"], mutation=True)
            self.assertEqual(gh.exception.code, "unsupported_gh_operation")


class PortReadingTests(unittest.TestCase):
    def setUp(self):
        self.port = GhPort(Path("."))
        self.commands = []

    def run_with(self, responses):
        def runner(command, *, cwd, timeout, mutation):
            self.commands.append(command[1:])
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return patch.multiple("pod.github", _run=runner, gh_executable=lambda: Path("/usr/bin/gh"),
                              git_executable=lambda: Path("/usr/bin/git"))

    def test_push_is_read_from_porcelain_output(self):
        with self.run_with([completed(f" \t{COMMIT}:refs/heads/agent/x\t{OLD[:7]}..{COMMIT[:7]}\nDone\n")]):
            self.assertEqual(self.port.push(remote="origin", branch="agent/x", commit=COMMIT, expected=OLD),
                             {"status": "pushed", "remote_head": COMMIT})
        self.assertEqual(self.commands[-1][2], f"--force-with-lease=refs/heads/agent/x:{OLD}")
        with self.run_with([completed(f"!\t{COMMIT}:refs/heads/agent/x\t[rejected] (stale info)\nDone\n", 1)]):
            self.assertEqual(self.port.push(remote="origin", branch="agent/x", commit=COMMIT, expected=None)["status"],
                             "rejected")
        with self.run_with([completed("", 128)]):
            with self.assertRaises(PodError) as unknown:
                self.port.push(remote="origin", branch="agent/x", commit=COMMIT, expected=None)
            self.assertEqual(unknown.exception.code, "remote_effect_uncertain")
        with self.run_with([PodError("remote_effect_uncertain", "timeout")]):
            with self.assertRaises(PodError):
                self.port.push(remote="origin", branch="agent/x", commit=COMMIT, expected=None)

    def test_branch_head_and_pull_requests(self):
        with self.run_with([completed(f"{COMMIT}\trefs/heads/agent/x\n")]):
            self.assertEqual(self.port.branch_head(remote="origin", branch="agent/x"), COMMIT)
        with self.run_with([completed("")]):
            self.assertIsNone(self.port.branch_head(remote="origin", branch="agent/x"))
        with self.run_with([completed('[{"number": 7, "url": "https://example.invalid/pull/7", "headRefOid": "%s", '
                                      '"state": "OPEN", "isDraft": true}]' % COMMIT)]):
            found = self.port.pull_request(head="agent/x", base="main")
        self.assertEqual((found["number"], found["head_sha"], found["draft"]), (7, COMMIT, True))
        with self.run_with([completed("[]")]):
            self.assertIsNone(self.port.pull_request(head="agent/x", base="main"))
        with self.run_with([completed("https://example.invalid/pull/8\n")]):
            opened = self.port.open_pull_request(head="agent/x", base="main", title="t", body="b")
        self.assertEqual((opened["number"], opened["url"]), (8, "https://example.invalid/pull/8"))
        with self.run_with([completed("not a url\n")]):
            with self.assertRaises(PodError) as unnamed:
                self.port.open_pull_request(head="agent/x", base="main", title="t", body="b")
            self.assertEqual(unnamed.exception.code, "remote_effect_uncertain")

    def test_runs_are_normalised_and_dispatch_inputs_are_ordered(self):
        row = ('[{"databaseId": 123, "status": "completed", "conclusion": "success", "createdAt": "2026-09-21T00:00:01Z",'
               ' "updatedAt": "2026-09-21T00:05:00Z", "headSha": "%s", "url": "u", "event": "workflow_dispatch",'
               ' "workflowName": "ci"}]' % COMMIT)
        with self.run_with([completed(row)]):
            runs = self.port.runs(workflow="ci.yml", commit=COMMIT)
        self.assertEqual(runs[0]["id"], "123")
        self.assertEqual((runs[0]["status"], runs[0]["conclusion"], runs[0]["head_sha"]), ("completed", "success", COMMIT))
        with self.run_with([completed(row[1:-1])]):
            self.assertEqual(self.port.run(run_id="123")["workflow"], "ci")
        with self.run_with([completed("ok\n")]):
            self.port.dispatch(workflow="ci.yml", ref="agent/x", inputs={"zeta": "1", "alpha": "2"})
        self.assertEqual(self.commands[-1], ["workflow", "run", "ci.yml", "--ref", "agent/x", "-f", "alpha=2",
                                             "-f", "zeta=1"])
        with self.run_with([completed("", 1)]):
            with self.assertRaises(PodError) as failed:
                self.port.dispatch(workflow="ci.yml", ref="agent/x", inputs={})
            self.assertEqual(failed.exception.code, "remote_effect_uncertain")
        with self.run_with([completed("{}")]):
            with self.assertRaises(PodError) as shape:
                self.port.runs(workflow="ci.yml", commit=COMMIT)
            self.assertEqual(shape.exception.code, "gh_contract")
        with self.run_with([completed(""), completed("")]):
            self.port.cancel(run_id="123")
            self.port.rerun(run_id="123", failed_only=True)
        self.assertEqual(self.commands[-2:], [["run", "cancel", "123"], ["run", "rerun", "123", "--failed"]])

    def test_complete_issue_read_validates_identity_and_access(self):
        body = json.dumps({"number": 7, "title": "Title", "body": "Complete body",
                           "state": "OPEN", "url": "https://github.com/acme/widgets/issues/7",
                           "updatedAt": "2026-09-23T00:00:00Z"})
        with self.run_with([completed(body)]):
            issue = self.port.issue(repository="acme/widgets", number=7)
        self.assertEqual(issue["body"], "Complete body")
        self.assertEqual(self.commands[-1], ["issue", "view", "7", "--repo", "acme/widgets",
                                             "--json", ISSUE_FIELDS])
        wrong = body.replace("/issues/7", "/issues/8")
        with self.run_with([completed(wrong)]), self.assertRaises(PodError) as mismatch:
            self.port.issue(repository="acme/widgets", number=7)
        self.assertEqual(mismatch.exception.code, "issue_identity_mismatch")
        with self.run_with([completed("", 1)]), self.assertRaises(PodError) as denied:
            self.port.issue(repository="acme/widgets", number=7)
        self.assertEqual(denied.exception.code, "issue_access_unavailable")

    def test_amendment_read_validates_the_exact_issue_identity(self):
        matching = json.dumps({"id": 99, "body": "Decision",
                               "url": "https://github.com/acme/widgets/issues/7#issuecomment-99"})
        with self.run_with([completed(matching)]):
            amendment = self.port.amendment(repository="acme/widgets", number=7, comment=99)
        self.assertEqual(amendment["body"], "Decision")
        wrong_issue = matching.replace("/issues/7", "/issues/8")
        with self.run_with([completed(wrong_issue)]), self.assertRaises(PodError) as mismatch:
            self.port.amendment(repository="acme/widgets", number=7, comment=99)
        self.assertEqual(mismatch.exception.code, "issue_identity_mismatch")


class RepositoryAndIssueTests(unittest.TestCase):
    def test_linked_worktree_preserves_native_home_containment_and_explicit_overrides(self):
        with fixture() as root:
            main, worktree = repository(root)
            inside_config = main / "native-config"
            inside_state = main / "native-state"
            inside_agents = main / "native-agents"
            for path in (inside_config, inside_state, inside_agents):
                path.mkdir()
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(inside_config)}), \
                 self.assertRaises(PodError) as config_error:
                personal_path(worktree)
            self.assertEqual(config_error.exception.code, "project_contained_native_home")
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(inside_state)}), \
                 self.assertRaises(PodError) as state_error:
                state_root(worktree)
            self.assertEqual(state_error.exception.code, "project_contained_native_home")
            with patch.dict(os.environ, {"CODEX_HOME": str(inside_agents),
                                         "CLAUDE_CONFIG_DIR": str(root / "safe-claude")}), \
                 self.assertRaises(PodError) as setup_error:
                inspect(worktree, global_scope=True)
            self.assertEqual(setup_error.exception.code, "project_contained_native_home")

            with patch.dict(os.environ, {"POD_CONFIG_HOME": str(inside_config),
                                         "POD_STATE_HOME": str(inside_state)}):
                self.assertEqual(personal_path(worktree), inside_config / "config.yaml")
                self.assertEqual(state_root(worktree), inside_state)

            outside = root / "outside-native"
            outside.mkdir()
            linked = root / "linked-native"
            linked.symlink_to(outside, target_is_directory=True)
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(linked)}):
                self.assertEqual(personal_path(worktree), linked / "pod" / "config.yaml")

    def test_passive_repository_and_config_reads_do_not_run_fsmonitor(self):
        with fixture() as root:
            main, worktree = repository(root)
            marker = root / "fsmonitor-ran"
            hook = root / "fsmonitor.sh"
            hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf '2\\n'\n")
            hook.chmod(0o700)
            git(main, "config", "core.fsmonitor", str(hook))
            context = repository_context(worktree)
            self.assertIsNone(context["dirty"])
            self.assertEqual(effective(worktree)["schema"], "pod/v1")
            self.assertFalse(marker.exists())

    def test_unavailable_git_identity_never_claims_a_clean_worktree(self):
        with fixture() as root:
            context = repository_context(root)
            self.assertIsNone(context["dirty"])
            nested = root / "repository"
            nested.mkdir()
            main, _ = repository(nested)
            with patch("pod.github._local_git", side_effect=PodError("git_unavailable", "offline")), \
                 self.assertRaises(PodError) as unavailable:
                repository_context(main)
            self.assertEqual(unavailable.exception.code, "git_unavailable")

    def test_local_git_repository_without_github_remote_still_supports_direct_work(self):
        with fixture() as root:
            main, worktree = repository(root)
            git(main, "remote", "remove", "origin")
            context = repository_context(worktree)
            self.assertIsNone(context["repository"])
            self.assertIsNotNone(context["repo_key"])
            self.assertEqual(effective(worktree)["schema"], "pod/v1")

    def test_issue_intake_target_and_content_reconciliation(self):
        with fixture() as root:
            main, worktree = repository(root)
            intake = issue_intake(worktree, "https://github.com/acme/widgets/issues/7",
                                  port=IssuePort(), amendments=[
                                      "https://github.com/acme/widgets/issues/7#issuecomment-99"])
            self.assertEqual(intake["status"], "ready")
            self.assertEqual(intake["body"], "Complete issue body")
            self.assertEqual(intake["worktree"]["branch"], "orca/issue-7")
            metadata = issue_recheck(worktree, intake["source"],
                                     port=IssuePort(updated="2026-09-24T00:00:00Z"))
            self.assertEqual(metadata["status"], "current")
            changed = issue_recheck(worktree, intake["source"], port=IssuePort(body="Changed"))
            self.assertEqual((changed["status"], changed["reason"]),
                             ("reconciliation_required", "issue_body_changed"))
            amendment = issue_recheck(worktree, intake["source"],
                                      port=IssuePort(amendment="Changed decision"))
            self.assertEqual(amendment["reason"], "issue_amendment_changed")
            with self.assertRaises(PodError) as wrong_initial_amendment:
                issue_intake(worktree, "https://github.com/acme/widgets/issues/7",
                             port=IssuePort(amendment_issue=8), amendments=[
                                 "https://github.com/acme/widgets/issues/7#issuecomment-99"])
            self.assertEqual(wrong_initial_amendment.exception.code, "issue_identity_mismatch")
            with self.assertRaises(PodError) as wrong_rechecked_amendment:
                issue_recheck(worktree, intake["source"], port=IssuePort(amendment_issue=8))
            self.assertEqual(wrong_rechecked_amendment.exception.code, "issue_identity_mismatch")
            with self.assertRaises(PodError) as mismatch:
                issue_intake(worktree, "https://github.com/other/repo/issues/7", port=IssuePort())
            self.assertEqual(mismatch.exception.code, "repository_mismatch")
            with self.assertRaises(PodError) as incomplete:
                issue_intake(worktree, "https://github.com/acme/widgets/issues/7",
                             port=IssuePort(body=""))
            self.assertEqual(incomplete.exception.code, "incomplete_issue_source")
            closed = issue_intake(main, "https://github.com/acme/widgets/issues/7",
                                  port=IssuePort(state="CLOSED"))
            self.assertEqual(closed["reason"], "closed_issue_requires_intent_reconciliation")

    def test_linked_worktree_reuses_state_and_restricts_private_main_policy(self):
        with fixture() as root:
            main, worktree = repository(root)
            main_context, worktree_context = repository_context(main), repository_context(worktree)
            self.assertEqual(main_context["repo_key"], worktree_context["repo_key"])
            self.assertNotEqual(main_context["worktree"], worktree_context["worktree"])
            self.assertEqual(worktree_context["branch"], "orca/issue-7")

            (main / ".pod").mkdir()
            (main / ".pod" / "config.yaml").write_text(
                "schema: pod/v1\npolicy: {max_workers: 2}\n")
            (worktree / ".pod").mkdir()
            (worktree / ".pod" / "config.yaml").write_text(
                "schema: pod/v1\npolicy: {max_workers: 1}\n")
            self.assertEqual(effective(worktree)["policy"]["policy"]["max_workers"], 1)

            value = {"schema": "pod-checkpoint/v1", "criteria": ["works"],
                     "plan_revision": "plan", "candidate": "candidate",
                     "policy_revision": "policy", "native_refs": [], "assignments": [],
                     "questions": [], "verification_gaps": ["works"],
                     "next_safe_action": "continue"}
            checkpoint(main, "issue-7", owner="owner", value=value,
                       native={"runtime": "runtime"})
            self.assertEqual(read(worktree, "issue-7")["checkpoint"]["objective"], "issue-7")

            dirty = main / "owner-change.txt"
            dirty.write_text("preserve")
            collision = root / "collision"
            collision.mkdir()
            repository_context(main)
            self.assertEqual(dirty.read_text(), "preserve")
            self.assertTrue(collision.is_dir())
