"""The GitHub port: exact argv shapes, and what each response is read as."""

import json
import hashlib
from io import StringIO
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pod.config import effective, personal_path
from pod.errors import PodError
from pod.gitio import worktree_inventory, worktree_rows
from pod.github import (AMENDMENT_JQ, GhPort, ISSUE_FIELDS, ISSUE_REOPEN_QUERY, PR_FIELDS, RUN_FIELDS,
                        MUTATION_TIMEOUT_S, flip_pod_checkbox, gh_allowed, git_allowed,
                        issue_intake, issue_recheck, issue_recheck_live, pod_lines,
                        pod_ref_number, repository_context, repository_reads)
from pod.ledger import checkpoint, objective_root, read, state_root
from pod.placement import inspect
from pod.util import digest
from tests.common import fixture
from tests.issue_port_support import RecordingIssuePort, SimulatedInterruption, patch_issue_port

COMMIT = "a" * 40
OLD = "b" * 40


def issue_binding(body="- [ ] **PoD#1"):
    import hashlib
    return {"schema": "pod-issue-source/v1", "repository": "acme/widgets", "number": 7,
            "locator": "https://github.com/acme/widgets/issues/7",
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "amendments": []}


def issue_patch(repository="acme/widgets", number=7):
    return ["api", "--method", "PATCH", "--include",
            f"repos/{repository}/issues/{number}", "--input", "-"]


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


class IssueReopenTimelineTests(unittest.TestCase):
    def request(self):
        return ["api", "graphql", "-f", "query=" + ISSUE_REOPEN_QUERY, "-f", "owner=acme",
                "-f", "name=widgets", "-F", "number=7"]

    def response(self):
        return {"data": {"repository": {"nameWithOwner": "acme/widgets", "issue": {
            "number": 7, "url": "https://github.com/acme/widgets/issues/7", "timelineItems": {"nodes": [
                {"__typename": "ReopenedEvent", "createdAt": "2026-10-23T09:03:56Z"}]}}}}}

    def test_latest_reopen_is_one_exact_bounded_read_only_query(self):
        with patch.object(GhPort, "_gh", return_value=completed(json.dumps(self.response()))) as command:
            result = GhPort(None).issue_reopened_at(repository="acme/widgets", number=7)
        self.assertEqual(result, "2026-10-23T09:03:56Z")
        command.assert_called_once_with(self.request(), mutation=False)
        self.assertTrue(gh_allowed(self.request()))
        self.assertIn("timelineItems(last:1,itemTypes:[REOPENED_EVENT])", ISSUE_REOPEN_QUERY)
        for request in ([*self.request(), "--paginate"],
                        [*self.request()[:3], "query=" + ISSUE_REOPEN_QUERY.replace("last:1", "last:100"), *self.request()[4:]],
                        [*self.request()[:3], "query=" + ISSUE_REOPEN_QUERY + " mutation{}", *self.request()[4:]],
                        [*self.request()[:5], "owner=acme/other", *self.request()[6:]]):
            self.assertFalse(gh_allowed(request))

    def test_empty_timeline_remains_absent_and_zoned_event_is_normalized(self):
        response = self.response()
        nodes = response["data"]["repository"]["issue"]["timelineItems"]["nodes"]
        nodes[0]["createdAt"] = "2026-10-23T10:03:56+01:00"
        with patch.object(GhPort, "_gh_json", return_value=response):
            self.assertEqual(GhPort(None).issue_reopened_at(repository="acme/widgets", number=7), "2026-10-23T09:03:56Z")
        nodes.clear()
        with patch.object(GhPort, "_gh_json", return_value=response):
            self.assertIsNone(GhPort(None).issue_reopened_at(repository="acme/widgets", number=7))

    def test_inconsistent_identity_partial_errors_and_extra_nodes_refuse(self):
        for field in ("repository", "number", "url", "errors", "count", "event", "missing", "time", "naive"):
            with self.subTest(field=field):
                response = self.response(); repo = response["data"]["repository"]; issue = repo["issue"]
                nodes = issue["timelineItems"]["nodes"]
                if field == "repository": repo["nameWithOwner"] = "other/widgets"
                elif field == "number": issue["number"] = 8
                elif field == "url": issue["url"] = "https://github.com/other/widgets/issues/7"
                elif field == "errors": response["errors"] = [{"message": "partial result"}]
                elif field == "count": nodes *= 2
                elif field == "event": nodes[0]["__typename"] = "ClosedEvent"
                elif field == "missing": nodes[0].pop("createdAt")
                elif field == "time": nodes[0]["createdAt"] = "unknown"
                else: nodes[0]["createdAt"] = "2026-10-23T09:03:56"
                with patch.object(GhPort, "_gh_json", return_value=response), self.assertRaises(PodError) as caught:
                    GhPort(None).issue_reopened_at(repository="acme/widgets", number=7)
                self.assertEqual(caught.exception.code, "gh_contract")

    def test_unreadable_timeline_is_not_reported_as_no_reopen(self):
        with patch.object(GhPort, "_gh", return_value=completed(returncode=1)), self.assertRaises(PodError) as caught:
            GhPort(None).issue_reopened_at(repository="acme/widgets", number=7)
        self.assertEqual(caught.exception.code, "remote_read_failed")


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
    def test_issue_patch_is_bound_to_exact_path_and_json_stdin(self):
        bound = ("acme/widgets", 7)
        stdin = json.dumps({"body": "- [x] **PoD#1\r\n"})
        self.assertTrue(gh_allowed(issue_patch(), bound_issue=bound, stdin=stdin))
        for argv in (issue_patch("other/widgets"), issue_patch(number=8),
                     issue_patch("Acme/widgets"),  # Deliberately case-sensitive.
                     ["api", "--method", "POST", *issue_patch()[3:]],
                     [arg for arg in issue_patch() if arg != "--include"],
                     [*issue_patch()[:-2], "-f", "body=text"],
                     ["issue", "edit", "7", "--repo", "acme/widgets", "--body", "text"],
                     [*issue_patch(), "--silent"]):
            with self.subTest(argv=argv):
                self.assertFalse(gh_allowed(argv, bound_issue=bound, stdin=stdin))
        for payload in (None, "invalid JSON", "null", "[]", '{}',
                        json.dumps({"body": "text", "title": "extra"}),
                        json.dumps({"body": 1}), json.dumps({"body": "é" * (256 * 1024 + 1)}),
                        json.dumps({"body": "\ud800"})):
            with self.subTest(payload_type=type(payload).__name__, size=len(payload or "")):
                self.assertFalse(gh_allowed(issue_patch(), bound_issue=bound, stdin=payload))
        self.assertFalse(gh_allowed(issue_patch(), stdin=stdin))
        self.assertTrue(gh_allowed(issue_patch(), bound_issue=bound,
                                   stdin=json.dumps({"body": "a" * (512 * 1024)})))

    def test_issue_patch_refusals_start_no_process(self):
        port = GhPort(Path("/fixture"))
        with patch("pod.github._run") as runner:
            for bound, stdin in ((None, json.dumps({"body": "text"})),
                                 (("acme/widgets", 8), json.dumps({"body": "text"})),
                                 (("acme/widgets", 7), "invalid")):
                with self.assertRaises(PodError) as refused:
                    port._gh(issue_patch(), mutation=True, bound_issue=bound, stdin=stdin)
                self.assertEqual(refused.exception.code, "unsupported_gh_operation")
            runner.assert_not_called()

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
                   ["run", "list", "--workflow", "ci.yml", "--commit", COMMIT, "--json", RUN_FIELDS, "--limit", "21"],
                   ["run", "view", "123", "--json", RUN_FIELDS],
                   ["run", "view", "123", "--attempt", "1", "--json", RUN_FIELDS],
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
                        ["run", "list", "--workflow", "ci.yml", "--commit", COMMIT, "--json", RUN_FIELDS,
                         "--limit", "20"],
                        ["run", "view", "123", "--attempt", "0", "--json", RUN_FIELDS],
                        ["run", "view", "123", "--attempt", "x", "--json", RUN_FIELDS],
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
               ' "workflowName": "ci", "attempt": 2}]' % COMMIT)
        with self.run_with([completed(row)]):
            runs = self.port.runs(workflow="ci.yml", commit=COMMIT)
        self.assertEqual(self.commands[-1][-2:], ["--limit", "21"])
        self.assertEqual(runs[0]["id"], "123")
        self.assertEqual((runs[0]["status"], runs[0]["conclusion"], runs[0]["head_sha"], runs[0]["attempt"]),
                         ("completed", "success", COMMIT, 2))
        with self.run_with([completed(row[1:-1])]):
            self.assertEqual(self.port.run(run_id="123")["workflow"], "ci")
        self.assertEqual(self.commands[-1], ["run", "view", "123", "--json", RUN_FIELDS])
        with self.run_with([completed(row[1:-1].replace('"attempt": 2', '"attempt": "1"'))]):
            self.assertIsNone(self.port.run(run_id="123", attempt=1)["attempt"])
        self.assertEqual(self.commands[-1], ["run", "view", "123", "--attempt", "1", "--json", RUN_FIELDS])
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


class PodLineTests(unittest.TestCase):
    def test_reference_numbers_are_bounded_and_anchored(self):
        for ref, expected in (("PoD#1", 1), ("PoD#10", 10), ("PoD#1x", 1),
                              ("PoD#1 — title", 1), ("PoD#0001", 1), ("PoD#0", 0),
                              ("PoD#999999999", 999999999), ("PoD#1000000000", None),
                              ("xPoD#1", None), (" PoD#1", None), ("pod#1", None),
                              ("PoD#", None), (None, None), (1, None)):
            with self.subTest(ref=ref):
                self.assertEqual(pod_ref_number(ref), expected)

    def test_checkbox_states_and_line_endings(self):
        for newline in ("\n", "\r\n"):
            for char in (" ", "x", "X"):
                with self.subTest(newline=repr(newline), char=char):
                    prefix = "text é" + newline
                    body = prefix + f"- [{char}] **PoD#1" + newline
                    self.assertEqual(pod_lines(body), {1: [{"offset": len(prefix) + 3,
                                                           "ticked": char != " "}]})

    def test_quotes_indentation_and_non_lf_separators_are_not_lines(self):
        for prefix in ("> ", " ", "  ", "   ", "\t", "text\v", "text\r", "text\u2028"):
            with self.subTest(prefix=prefix):
                self.assertEqual(pod_lines(prefix + "- [ ] **PoD#1\n"), {})

    def test_fenced_lines_are_excluded(self):
        line = "- [ ] **PoD#1"
        for opener, closer in (("```", "```"), ("~~~", "~~~"),
                               ("```python", "````"), ("~~~ info `allowed`", "~~~~"),
                               ("   ```python", "  `````\t"), ("  ~~~yaml", "   ~~~~")):
            for newline in ("\n", "\r\n"):
                with self.subTest(opener=opener, closer=closer, newline=repr(newline)):
                    prefix = newline.join((opener, line, closer, ""))
                    self.assertEqual(pod_lines(prefix + line),
                                     {1: [{"offset": len(prefix) + 3, "ticked": False}]})
        for body in (f"```info\n{line}", f"~~~\n{line}\n```\n{line}",
                     f"````\n{line}\n```\n{line}", f"```\n{line}\n``` trailing\n{line}"):
            with self.subTest(body=body):
                self.assertEqual(pod_lines(body), {})
        # Four-space fences and backtick info containing a backtick do not open a fence.
        for opener in ("    ```", "```bad`info"):
            body = opener + "\n" + line
            self.assertEqual(pod_lines(body), {1: [{"offset": len(opener) + 4, "ticked": False}]})

    def test_numbers_duplicates_suffixes_and_empty_body(self):
        body = "- [ ] **PoD#1\n- [X] **PoD#10\n- [x] **PoD#1x\n- [ ] **PoD#1"
        self.assertEqual(pod_lines(body), {
            1: [{"offset": 3, "ticked": False},
                {"offset": body.index("- [x]") + 3, "ticked": True},
                {"offset": body.rindex("- [ ]") + 3, "ticked": False}],
            10: [{"offset": body.index("- [X]") + 3, "ticked": True}]})
        self.assertEqual(pod_lines(""), {})
        self.assertEqual(pod_lines("- [ ] **PoD#1000000000"), {})

    def test_flip_changes_one_character_and_preserves_crlf(self):
        for char, ticked in ((" ", True), ("x", False), ("X", False), ("X", True)):
            with self.subTest(char=char, ticked=ticked):
                body = "é\r\n" + f"- [{char}] **PoD#1\r\nuntouched\r\n"
                offset = pod_lines(body)[1][0]["offset"]
                result = flip_pod_checkbox(body, offset, ticked)
                self.assertEqual(sum(a != b for a, b in zip(body, result)), 1)
                self.assertEqual(result, body[:offset] + ("x" if ticked else " ") + body[offset + 1:])
                self.assertEqual(len(body.encode()), len(result.encode()))

    def test_flip_refuses_an_invalid_offset_or_no_change(self):
        body = "- [ ] **PoD#1\n```\n- [ ] **PoD#2\n```"
        for offset, ticked in ((-1, True), (0, True), (4, True), (len(body), True),
                               (body.index("- [ ]", 1) + 3, True), (3, False), (True, True),
                               (3.0, True), (3, "yes")):
            with self.subTest(offset=offset, ticked=ticked), self.assertRaises(PodError) as refused:
                flip_pod_checkbox(body, offset, ticked)
            self.assertEqual(refused.exception.code, "internal_error")


class IssueBodyWriteTests(unittest.TestCase):
    def perform(self, response=None, *, error=None):
        runner = patch("pod.github.subprocess.run", return_value=response, side_effect=error)
        return runner

    def test_success_and_held_refusal_status_table(self):
        for status in (200, 201, 403, 404, 410, 422, 429):
            with self.subTest(status=status):
                message = "GitHub message " + "é" * 600
                output = f"HTTP/2.0 {status} Status\r\nContent-Type: application/json\r\n\r\n"
                output += json.dumps({"message": message})
                with self.perform(completed(output, 0 if status < 300 else 1)) as runner, \
                     patch("pod.github.gh_executable", return_value=Path("/fixture/gh")), \
                     patch("pod.github.forget_repository_reads") as forget:
                    body = "- [x] **PoD#1\r\n"
                    result = GhPort(Path("/fixture")).issue_body_write(source=issue_binding(), body=body)
                expected = {"result": "accepted" if status < 300 else "refused", "status": status}
                if status >= 400:
                    expected["message"] = message[:512]
                self.assertEqual(result, expected)
                runner.assert_called_once()
                self.assertEqual(runner.call_args.args[0], ["/fixture/gh", *issue_patch()])
                self.assertEqual(runner.call_args.kwargs["input"], json.dumps({"body": body}))
                self.assertEqual(runner.call_args.kwargs["timeout"], MUTATION_TIMEOUT_S)
                forget.assert_called_once()

    def test_server_errors_and_contradictions_are_uncertain(self):
        for status, exit_code in ((500, 1), (502, 1), (504, 1), (403, 0), (200, 1),
                                  (201, -9), (404, -9), (302, 0), (500, 0)):
            with self.subTest(status=status, exit_code=exit_code):
                output = f'HTTP/1.1 {status} Status\n\n{{"message": "error"}}'
                with self.perform(completed(output, exit_code)) as runner, \
                     patch("pod.github.gh_executable", return_value=Path("/fixture/gh")), \
                     self.assertRaises(PodError) as uncertain:
                    GhPort(Path("/fixture")).issue_body_write(source=issue_binding(), body="new")
                self.assertEqual(uncertain.exception.code, "remote_effect_uncertain")
                runner.assert_called_once()

    def test_missing_or_malformed_status_is_uncertain(self):
        for output in ('{"message": "error"}', "garbage", "", "HTTP/2.0 200 OK",
                       "HTTP/2.0 200 OK\ninvalid header\n\n{}",
                       "garbage\nHTTP/2.0 200 OK\n\n{}", "HTTP/2.0 100 Continue\n\n"):
            with self.subTest(output=output), self.perform(completed(output)) as runner, \
                 patch("pod.github.gh_executable", return_value=Path("/fixture/gh")), \
                 self.assertRaises(PodError) as uncertain:
                GhPort(Path("/fixture")).issue_body_write(source=issue_binding(), body="new")
            self.assertEqual(uncertain.exception.code, "remote_effect_uncertain")
            runner.assert_called_once()

    def test_interim_status_is_skipped_and_body_status_text_is_ignored(self):
        for status, exit_code in ((200, 0), (201, 0), (429, 1), (502, 1)):
            output = ("HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 103 Early Hints\r\n"
                      "Link: </x>\r\n\r\n" + f"HTTP/2.0 {status} Status\r\n\r\n"
                      + json.dumps({"message": "HTTP/2.0 200 OK\n\n"}))
            with self.subTest(status=status), self.perform(completed(output, exit_code)) as runner, \
                 patch("pod.github.gh_executable", return_value=Path("/fixture/gh")):
                if status >= 500:
                    with self.assertRaises(PodError) as uncertain:
                        GhPort(Path("/fixture")).issue_body_write(source=issue_binding(), body="new")
                    self.assertEqual(uncertain.exception.code, "remote_effect_uncertain")
                else:
                    self.assertEqual(GhPort(Path("/fixture")).issue_body_write(
                        source=issue_binding(), body="new")["status"], status)
            runner.assert_called_once()

    def test_timeout_oserror_decode_and_oversized_response_are_uncertain(self):
        for error in (subprocess.TimeoutExpired(["fixture-gh"], 120), OSError("offline"),
                      UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"), None):
            with self.subTest(error=type(error).__name__), \
                 self.perform(completed("a" * 2_000_001), error=error) as runner, \
                 patch("pod.github.gh_executable", return_value=Path("/fixture/gh")), \
                 patch("pod.github.forget_repository_reads") as forget, \
                 self.assertRaises(PodError) as uncertain:
                GhPort(Path("/fixture")).issue_body_write(source=issue_binding(), body="new")
            self.assertEqual(uncertain.exception.code, "remote_effect_uncertain")
            runner.assert_called_once()
            forget.assert_called_once()

    def test_refusal_without_json_message_is_still_a_held_refusal(self):
        for payload in ("garbage", "null", "{}", '{"message": 1}'):
            with self.subTest(payload=payload), \
                 self.perform(completed("HTTP/2.0 403 Forbidden\n\n" + payload, 1)), \
                 patch("pod.github.gh_executable", return_value=Path("/fixture/gh")):
                self.assertEqual(GhPort(Path("/fixture")).issue_body_write(
                    source=issue_binding(), body="new"),
                    {"result": "refused", "status": 403, "message": ""})

    def test_invalid_binding_and_body_are_refused_before_running(self):
        for source, body in (({**issue_binding(), "number": 8}, "new"),
                             (issue_binding(), 1), (issue_binding(), "a" * (512 * 1024 + 1))):
            with self.subTest(body_type=type(body).__name__), patch("pod.github._run") as runner, \
                 self.assertRaises(PodError):
                GhPort(Path("/fixture")).issue_body_write(source=source, body=body)
            runner.assert_not_called()

    def test_issue_write_forgets_scoped_repository_identity(self):
        context = {"repository": "acme/widgets"}
        with repository_reads(), patch("pod.github._read_repository_context", return_value=(context, True)) as read, \
             self.perform(completed("HTTP/2.0 200 OK\n\n{}")), \
             patch("pod.github.gh_executable", return_value=Path("/fixture/gh")):
            repository_context(Path("/fixture"))
            repository_context(Path("/fixture"))
            GhPort(Path("/fixture")).issue_body_write(source=issue_binding(), body="new")
            repository_context(Path("/fixture"))
            self.assertEqual(read.call_count, 2)


class RecordingIssuePortTests(unittest.TestCase):
    def test_mutable_issue_and_amendments_with_call_order_and_exact_writes(self):
        port = RecordingIssuePort(amendments={99: "original"})
        port.title, port.state, port.amendments[99] = "changed", "CLOSED", "amended"
        row = port.issue(repository="acme/widgets", number=7)
        self.assertEqual((row["title"], row["state"]), ("changed", "CLOSED"))
        self.assertEqual(port.amendment(repository="acme/widgets", number=7, comment=99)["body"], "amended")
        body = "- [x] **PoD#1\r\n"
        source = issue_binding()
        self.assertEqual(port.issue_body_write(source=source, body=body), {"result": "accepted", "status": 200})
        self.assertEqual(port.body, body)
        self.assertEqual(port.writes, [body])
        self.assertEqual(port.calls, [("issue", "acme/widgets", 7), ("amendment", "acme/widgets", 7, 99),
                                      ("issue_body_write", source, body)])
        source["number"] = 8
        self.assertEqual(port.calls[-1][1]["number"], 7)

    def test_unreadable_reads_and_refused_write_are_one_shot(self):
        port = RecordingIssuePort()
        for read in (lambda: port.issue(repository="acme/widgets", number=7),
                     lambda: port.amendment(repository="acme/widgets", number=7, comment=99)):
            port.unreadable = True
            with self.assertRaises(PodError) as unavailable:
                read()
            self.assertEqual(unavailable.exception.code, "issue_access_unavailable")
            read()
        port.refuse_next = 429
        before = port.body
        self.assertEqual(port.issue_body_write(source=issue_binding(), body="new")["result"], "refused")
        self.assertEqual(port.body, before)
        self.assertEqual(port.issue_body_write(source=issue_binding(), body="new")["result"], "accepted")
        self.assertEqual(port.writes, ["new", "new"])

    def test_lost_responses_and_interruptions_apply_only_the_specified_write(self):
        for outcome in ("landed", "not_landed", "interrupted"):
            with self.subTest(outcome=outcome):
                port = RecordingIssuePort(body="old")
                if outcome == "interrupted":
                    port.interrupt_next = True
                else:
                    port.lose_next = outcome
                with self.assertRaises(SimulatedInterruption if outcome == "interrupted" else PodError):
                    port.issue_body_write(source=issue_binding(), body="new")
                self.assertEqual(port.body, "old" if outcome == "not_landed" else "new")
                self.assertEqual(port.writes, ["new"])
                self.assertEqual(port.issue_body_write(source=issue_binding(), body="later")["result"], "accepted")

    def test_other_actor_can_write_before_or_instead_of_pod(self):
        for actor, outcome, expected in (("outside", None, "new"),
                                          (("before", "outside"), None, "new"),
                                          (("instead", "outside"), None, "outside"),
                                          ("new", "not_landed", "new"),
                                          ("new", "refused", "new")):
            with self.subTest(actor=actor, outcome=outcome):
                port = RecordingIssuePort(body="old")
                port.other_actor = actor
                if outcome == "refused":
                    port.refuse_next = 403
                elif outcome:
                    port.lose_next = outcome
                try:
                    port.issue_body_write(source=issue_binding(), body="new")
                except PodError as exc:
                    self.assertEqual(exc.code, "remote_effect_uncertain")
                self.assertEqual(port.body, expected)
                self.assertIsNone(port.other_actor)
                self.assertEqual(port.writes, ["new"])

    def test_readback_override_is_one_read_after_a_successful_write(self):
        port = RecordingIssuePort(body="old")
        port.readback_override = "outside"
        self.assertEqual(port.issue(repository="acme/widgets", number=7)["body"], "old")
        port.issue_body_write(source=issue_binding(), body="new")
        self.assertEqual(port.issue(repository="acme/widgets", number=7)["body"], "outside")
        self.assertEqual(port.issue(repository="acme/widgets", number=7)["body"], "new")

    def test_patch_helper_returns_same_instance_without_production_state(self):
        from pod import github
        original = github.GhPort
        port = RecordingIssuePort()
        with patch_issue_port(port) as factory:
            self.assertIs(github.GhPort(Path("/fixture")), port)
            factory.assert_called_once_with(Path("/fixture"))
        self.assertIs(github.GhPort, original)


class IssueRecheckLiveTests(unittest.TestCase):
    def test_live_result_equals_public_recheck_and_returns_the_live_row(self):
        binding = issue_binding("Complete issue body")
        import hashlib
        binding["amendments"] = [{"locator": binding["locator"] + "#issuecomment-99",
                                  "sha256": hashlib.sha256(b"Decision").hexdigest()}]
        for kwargs in ({}, {"updated": "changed"}, {"body": "Changed"},
                       {"amendment": "Changed decision"}, {"state": "CLOSED"},
                       {"state": "CLOSED", "body": "Changed", "amendment": "Changed"}):
            with self.subTest(kwargs=kwargs), patch("pod.github.repository_context", return_value={
                    "repository": "acme/widgets"}) as context:
                port = IssuePort(**kwargs)
                result, row = issue_recheck_live(Path("/fixture"), binding, port=port)
                self.assertEqual(result, issue_recheck(Path("/fixture"), binding, port=port))
                self.assertEqual(row, port.issue(repository="acme/widgets", number=7))
                self.assertTrue(all(call.kwargs == {"fresh": True} for call in context.call_args_list))

    def test_live_and_public_recheck_keep_refusal_order_and_codes(self):
        for repository_name, port, expected, calls in (
                ("other/repo", RecordingIssuePort(title="SEAL: requirements"), "repository_mismatch", []),
                ("acme/widgets", RecordingIssuePort(title="EEL: evidence"), "issue_is_evidence_ledger", ["issue"]),
                ("acme/widgets", RecordingIssuePort(title="SEAL: requirements"), "issue_is_requirements_spec", ["issue"])):
            for recheck in (issue_recheck_live, issue_recheck):
                with self.subTest(recheck=recheck.__name__, expected=expected), \
                     patch("pod.github.repository_context", return_value={"repository": repository_name}), \
                     self.assertRaises(PodError) as refused:
                    port.calls.clear()
                    recheck(Path("/fixture"), issue_binding(), port=port)
                self.assertEqual(refused.exception.code, expected)
                self.assertEqual([call[0] for call in port.calls], calls)
        for recheck in (issue_recheck_live, issue_recheck):
            port = RecordingIssuePort()
            port.unreadable = True
            with patch("pod.github.repository_context", return_value={"repository": "acme/widgets"}), \
                 self.assertRaises(PodError) as refused:
                recheck(Path("/fixture"), issue_binding(), port=port)
            self.assertEqual(refused.exception.code, "issue_access_unavailable")


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
                inspect(worktree)
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
                "schema: pod/v1\nwaste_governor: {transient_retries: 1}\n")
            (worktree / ".pod").mkdir()
            (worktree / ".pod" / "config.yaml").write_text(
                "schema: pod/v1\nwaste_governor: {transient_retries: 0}\n")
            self.assertEqual(effective(worktree)["policy"]["waste_governor"]["transient_retries"], 0)

            value = {"schema": "pod-checkpoint/v3", "criteria": ["works"],
                     "plan_revision": "plan", "candidate": subprocess.run(["git", "-C", str(main), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip(),
                     "policy_revision": effective(main)["revision"], "native_refs": [], "assignments": [],
                     "questions": [], "verification_gaps": ["works"],
                     "next_safe_action": "continue"}
            with patch('pod.ledger.require_authority', return_value={
                    'runtime':'runtime','run_id':'run','references':{'run':'runtime'}}):
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


class StreamingRepositoryTests(unittest.TestCase):
    """Real layout fixtures once per process; failed Git ports never substitute identity facts."""
    @classmethod
    def setUpClass(cls):
        cls.temp = fixture()
        cls.root = cls.temp.__enter__()
        cls.addClassCleanup(cls.temp.__exit__, None, None, None)
        ordinary = cls.root / "ordinary"
        ordinary.mkdir()
        cls.main, cls.linked = repository(ordinary)
        cls.detached = ordinary / "detached"
        git(cls.main, "worktree", "add", "-q", "--detach", str(cls.detached))
        cls.alias = cls.root / "main-alias"
        cls.alias.symlink_to(cls.main, target_is_directory=True)
        cls.bare = cls.root / "bare.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(cls.main), str(cls.bare)], check=True)
        git(cls.bare, "remote", "set-url", "origin", "https://github.com/acme/widgets.git")
        cls.bare_linked = cls.root / "bare-worker"
        git(cls.bare, "worktree", "add", "-qb", "bare-worker", str(cls.bare_linked), "main")
        cls.separate = cls.root / "separate-main"
        cls.metadata = cls.root / "metadata store.git"
        subprocess.run(["git", "init", "-q", "-b", "main", "--separate-git-dir", str(cls.metadata),
                        str(cls.separate)], check=True)
        git(cls.separate, "config", "user.name", "Fixture")
        git(cls.separate, "config", "user.email", "fixture@example.invalid")
        (cls.separate / "README.md").write_text("fixture\n")
        git(cls.separate, "add", "README.md")
        git(cls.separate, "commit", "-qm", "base")
        git(cls.separate, "remote", "add", "origin", "https://github.com/acme/widgets.git")
        cls.separate_linked = cls.root / "separate-worker"
        git(cls.separate, "worktree", "add", "-qb", "separate-worker", str(cls.separate_linked))
        cls.porcelain = git(cls.main, "worktree", "list", "--porcelain", "-z")

    def assert_context(self, path, main, common, branch, others):
        self.assertEqual(repository_context(path), {
            "repository": "acme/widgets", "repo_key": hashlib.sha256(str(common.resolve()).encode()).hexdigest(),
            "worktree": str(path.resolve()), "main_worktree": str(main.resolve()), "branch": branch,
            "dirty": None, "linked_worktrees": [str(main.resolve()), *sorted(map(str, others))]})

    def test_canonical_and_linked_identity_keep_the_common_dir_key_and_full_shape(self):
        others = [self.detached, self.linked]
        self.assert_context(self.main, self.main, self.main / ".git", "main", others)
        self.assert_context(self.linked, self.main, self.main / ".git", "orca/issue-7", others)
        self.assertEqual(repository_context(self.alias), repository_context(self.main))

    def test_detached_identity_preserves_its_path_with_no_branch(self):
        self.assert_context(self.detached, self.main, self.main / ".git", None, [self.detached, self.linked])

    def test_bare_backed_worktree_uses_the_bare_common_dir_and_first_inventory_entry(self):
        self.assert_context(self.bare_linked, self.bare, self.bare, "bare-worker", [self.bare_linked])

    def test_separate_git_dir_keeps_gits_main_entry_and_refuses_an_unlisted_checkout(self):
        self.assertTrue((self.separate / ".git").is_file())
        # Git lists the separate metadata directory as its first entry. Preserve that native fact;
        # neither its parent nor the unlisted main checkout is an invented inventory member.
        self.assert_context(self.separate_linked, self.metadata, self.metadata, "separate-worker",
                            [self.separate_linked])
        with self.assertRaises(PodError) as refused:
            repository_context(self.separate)
        self.assertEqual(refused.exception.code, "git_contract")

    def test_complete_nul_inventory_survives_chunk_boundaries_and_detached_and_bare_rows(self):
        class SmallReads(StringIO):
            def read(self, size):
                return super().read(min(size, 7))
        rows = list(worktree_rows(SmallReads(self.porcelain)))
        self.assertEqual([row["worktree"] for row in rows],
                         [str(self.main), *sorted(map(str, [self.detached, self.linked]))])
        self.assertIn("detached", next(row for row in rows if row["worktree"] == str(self.detached)))
        with worktree_inventory(self.bare_linked) as inventory:
            self.assertEqual(next(worktree_rows(inventory)), {"worktree": str(self.bare), "bare": ""})

    def test_truncated_empty_missing_and_duplicate_inventory_fields_refuse(self):
        head = "a" * 40
        for value in ("", self.porcelain[:-1], self.porcelain.rstrip("\0"),
                      "worktree /fixture\0HEAD " + head + "\0\0",
                      "worktree /fixture\0branch refs/heads/worker\0\0",
                      "worktree /fixture\0worktree /other\0\0",
                      "branch refs/heads/worker\0worktree /fixture\0\0",
                      "worktree relative\0HEAD " + head + "\0detached\0\0"):
            with self.subTest(value=value[:80]), self.assertRaises(PodError) as refused:
                list(worktree_rows(StringIO(value)))
            self.assertEqual(refused.exception.code, "git_contract")

    def failing_inventory(self, value, *, returncode=0):
        native_run = subprocess.run
        def execute(argv, **kwargs):
            if argv[-4:] == ["worktree", "list", "--porcelain", "-z"]:
                kwargs["stdout"].write(value)
                kwargs["stdout"].flush()
                return completed(returncode=returncode)
            return native_run(argv, **kwargs)
        return patch("pod.gitio.subprocess.run", side_effect=execute)

    def test_discovery_and_cleanup_refuse_failed_empty_or_incomplete_native_inventory(self):
        from pod.cleanup import _objective_worktrees
        for value, returncode in (("", 1), ("", 0), (self.porcelain[:-1], 0)):
            with self.subTest(returncode=returncode, length=len(value)), self.failing_inventory(
                    value, returncode=returncode):
                with self.assertRaises(PodError):
                    repository_context(self.main)
                with self.assertRaises(PodError) as refused:
                    _objective_worktrees(self.main, {str(self.linked)}, {"refs/heads/orca/issue-7"})
                self.assertEqual(refused.exception.code, "cleanup_unavailable")

    def test_discovery_refuses_a_complete_inventory_that_omits_the_current_worktree(self):
        without_main = self.porcelain.split("\0\0", 1)[1]
        with self.failing_inventory(without_main), self.assertRaises(PodError) as refused:
            repository_context(self.main)
        self.assertEqual(refused.exception.code, "git_contract")
        self.assertIn("absent", str(refused.exception))

    def test_other_discovery_reads_keep_their_output_bound_and_allowlist(self):
        from pod.github import MAX_OUTPUT, _local_git
        with patch("pod.github.subprocess.run", return_value=completed("x" * (MAX_OUTPUT + 1))), \
                self.assertRaises(PodError) as refused:
            _local_git(self.main, ["remote", "get-url", "origin"])
        self.assertEqual(refused.exception.code, "git_contract")
        with self.assertRaises(PodError) as refused:
            _local_git(self.main, ["status", "--porcelain"])
        self.assertEqual(refused.exception.code, "unsupported_git_operation")
