"""Exact, complete role identities take precedence over an intake target mismatch."""

import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.github import EEL_FORMAT, SEAL_FORMAT, issue_intake, issue_recheck
from pod.internal import run
from tests.common import fixture
from tests.test_github import git, repository


LOCATOR = "https://github.com/acme/requirements/issues/7"
NEXT_ACTION = ("use read-only SEAL delivery planning, a bounded Pod Execution Spec citing selected "
               "SEAL requirements, or an explicitly scoped analysis request")


class RecordingPort:
    def __init__(self, *, error=None, **fields):
        self.calls = []
        self.error = error
        self.fields = fields

    def issue(self, *, repository, number):
        self.calls.append((repository, number))
        if self.error is not None:
            raise self.error
        return {"number": number, "url": f"https://github.com/{repository}/issues/{number}",
                "title": "PES: implement export", "body": "Complete executable scope",
                "state": "OPEN", **self.fields}


class SealIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = fixture()
        self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.main, self.worktree = repository(self.root)
        self.directory = self.root / "plain"
        self.directory.mkdir()

    def refuse(self, project, port, code, *, locator=LOCATOR):
        with self.assertRaises(PodError) as caught:
            issue_intake(project, locator, port=port)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(port.calls, [("acme/requirements", 7)])
        if code == "repository_mismatch":
            self.assertEqual(str(caught.exception), "Issue target differs from the actual checkout repository")
            self.assertIsNone(caught.exception.detail)
        return caught.exception

    def role_forms(self):
        for prefix, marker, code in (
                ("SEAL:", SEAL_FORMAT, "issue_is_requirements_spec"),
                ("EEL:", EEL_FORMAT, "issue_is_evidence_ledger")):
            yield {"title": prefix + " export"}, code, "title"
            for newline in ("\n", "\r\n"):
                for suffix in ("", "<br>"):
                    yield {"body": newline + "  " + newline + marker + suffix + newline}, code, "body"

    def role_refusals(self, project):
        for fields, code, marker in self.role_forms():
            with self.subTest(fields=fields):
                error = self.refuse(project, RecordingPort(**fields), code)
                self.assertEqual(error.detail["marker"], marker)
                self.assertEqual(error.detail["locator"], LOCATOR)
                if code == "issue_is_requirements_spec":
                    self.assertEqual(error.detail["next_action"], NEXT_ACTION)

    def test_other_repository_refuses_each_role_form_after_one_read(self):
        self.role_refusals(self.worktree)

    def test_non_git_directory_refuses_each_role_form_after_one_read(self):
        self.role_refusals(self.directory)

    def test_checkout_without_origin_refuses_each_role_form_after_one_read(self):
        git(self.main, "remote", "remove", "origin")
        self.role_refusals(self.worktree)

    def test_non_github_origin_refuses_each_role_form_after_one_read(self):
        git(self.main, "remote", "set-url", "origin", "https://example.invalid/acme/widgets.git")
        self.role_refusals(self.worktree)

    def test_internal_issue_intake_uses_the_same_role_refusals(self):
        for project in (self.worktree, self.directory):
            for fields, code, marker in self.role_forms():
                with self.subTest(project=project.name, fields=fields):
                    port = RecordingPort(**fields)
                    with patch("pod.github.GhPort", return_value=port), self.assertRaises(PodError) as caught:
                        run("issue-intake", {"project": str(project), "locator": LOCATOR})
                    self.assertEqual(caught.exception.code, code)
                    self.assertEqual(caught.exception.detail["marker"], marker)
                    self.assertEqual(port.calls, [("acme/requirements", 7)])

    def test_eel_precedence_and_casefolded_identity_are_preserved(self):
        for title, body in (("SEAL: export", EEL_FORMAT), ("EEL: export", SEAL_FORMAT)):
            with self.subTest(title=title):
                self.refuse(self.worktree, RecordingPort(title=title, body=body,
                    url="https://github.com/ACME/Requirements/issues/7"), "issue_is_evidence_ledger")

    def test_readable_pes_keeps_repository_mismatch(self):
        for project in (self.worktree, self.directory):
            with self.subTest(project=project.name):
                self.refuse(project, RecordingPort(), "repository_mismatch")

    def test_incomplete_title_keeps_repository_mismatch(self):
        for title in (None, 7, "", "  ", "SEAL:" + "x" * 508):
            with self.subTest(title=title):
                self.refuse(self.worktree, RecordingPort(title=title, body=SEAL_FORMAT),
                            "repository_mismatch")

    def test_incomplete_body_keeps_repository_mismatch(self):
        for body in (None, 7, "", " \n", "é" * (256 * 1024) + "x"):
            with self.subTest(size=len(body) if isinstance(body, str) else None):
                self.refuse(self.worktree, RecordingPort(title="SEAL: export", body=body),
                            "repository_mismatch")

    def test_complete_source_at_title_and_body_bounds_still_refuses_role(self):
        self.refuse(self.worktree, RecordingPort(title="SEAL:" + "x" * 507,
                    body="é" * (256 * 1024)), "issue_is_requirements_spec")

    def test_read_errors_keep_repository_mismatch_without_content(self):
        for code in ("issue_access_unavailable", "gh_unavailable", "gh_contract",
                     "unsupported_gh_operation", "issue_identity_mismatch"):
            with self.subTest(code=code):
                self.refuse(self.worktree, RecordingPort(error=PodError(code, "Issue content sentinel")),
                            "repository_mismatch")

    def test_returned_identity_mismatch_keeps_repository_mismatch(self):
        for fields in ({"number": 8}, {"number": "7"},
                       {"url": "https://github.com/acme/elsewhere/issues/7"},
                       {"url": "https://github.com/acme/requirements/issues/8"},
                       {"url": "invalid"}, {"url": None}):
            with self.subTest(fields=fields):
                self.refuse(self.worktree, RecordingPort(title="SEAL: export", **fields),
                            "repository_mismatch")

    def test_non_identity_markers_keep_repository_mismatch(self):
        for body in ("> " + SEAL_FORMAT, "```\n" + SEAL_FORMAT + "\n```",
                     "Context\n" + SEAL_FORMAT, "  " + SEAL_FORMAT, SEAL_FORMAT + " "):
            with self.subTest(body=body):
                self.refuse(self.worktree, RecordingPort(body=body), "repository_mismatch")

    def test_recheck_mismatch_never_reads(self):
        binding = {"schema": "pod-issue-source/v1", "repository": "acme/requirements",
                   "number": 7, "locator": LOCATOR, "body_sha256": "a" * 64, "amendments": []}
        for project in (self.worktree, self.directory):
            with self.subTest(project=project.name):
                port = RecordingPort(error=AssertionError("Recheck must not read a mismatched issue"))
                with self.assertRaises(PodError) as caught:
                    issue_recheck(project, binding, port=port)
                self.assertEqual(caught.exception.code, "repository_mismatch")
                self.assertEqual(port.calls, [])

    def test_same_repository_intake_and_recheck_keep_existing_results(self):
        git(self.main, "remote", "set-url", "origin", "https://github.com/ACME/Requirements.git")
        self.role_refusals(self.worktree)
        port = RecordingPort()
        result = issue_intake(self.worktree, LOCATOR, port=port)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(issue_recheck(self.worktree, result["source"], port=port)["status"], "current")
        port.fields["title"] = "SEAL: export"
        with self.assertRaises(PodError) as caught:
            issue_recheck(self.worktree, result["source"], port=port)
        self.assertEqual(caught.exception.code, "issue_is_requirements_spec")
        self.assertEqual(caught.exception.detail["next_action"], NEXT_ACTION)
        for fields, code in (({"body": ""}, "incomplete_issue_source"),
                             ({"body": "", "title": "SEAL: export"}, "incomplete_issue_source")):
            with self.subTest(fields=fields):
                self.refuse(self.worktree, RecordingPort(**fields), code)
        self.refuse(self.worktree, RecordingPort(error=PodError("issue_access_unavailable", "Unavailable")),
                    "issue_access_unavailable")
        self.assertEqual(issue_intake(self.worktree, LOCATOR,
                         port=RecordingPort(state="CLOSED"))["status"], "reconciliation_required")

    def test_git_identity_failure_with_git_ancestor_still_prevents_issue_read(self):
        for code in ("repository_identity_unavailable", "git_unavailable"):
            with self.subTest(code=code):
                port = RecordingPort(error=AssertionError("Git identity must be established first"))
                with patch("pod.github._local_git", side_effect=PodError(code, "Unavailable")), \
                        self.assertRaises(PodError) as caught:
                    issue_intake(self.worktree, LOCATOR, port=port)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(port.calls, [])


if __name__ == "__main__":
    unittest.main()
