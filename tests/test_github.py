"""The GitHub port: exact argv shapes, and what each response is read as."""

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.github import GhPort, PR_FIELDS, RUN_FIELDS, gh_allowed, git_allowed

COMMIT = "a" * 40
OLD = "b" * 40


def completed(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


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
