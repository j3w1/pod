"""A5: an Evidence Evaluation Ledger is recognized by two exact markers and never executed."""

from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.github import issue_intake, issue_recheck
from pod.internal import run as internal_run
from pod.ledger import checkpoint, read, update_admission
from pod.operations import _check_objective_source
from pod.records import packet
from tests.common import fixture
from tests.test_github import IssuePort, repository
from tests import test_operations as operation_tests

LOCATOR = "https://github.com/acme/widgets/issues/7"
FORMAT = "**Format:** Evidence Evaluation Ledger v1"
PES = ("# Implement widgets\n\n**Target:** `acme/widgets`<br>\n**Format:** Pod Execution Spec v1<br>\n"
       "**Delivery:** PR\n\nEvidence: the EEL at https://github.com/acme/widgets/issues/6\n")
EEL_CODE = "issue_is_evidence_ledger"


class TitledPort(IssuePort):
    def __init__(self, *, title="Implement widgets", **kwargs):
        super().__init__(**kwargs)
        self.title = title

    def issue(self, *, repository, number):
        return {**super().issue(repository=repository, number=number), "title": self.title}


def bodies() -> dict[str, str]:
    """Every accepted body-marker form: with or without <br>, LF or CRLF, after blank lines."""
    forms = {}
    for suffix in ("", "<br>"):
        for newline in ("\n", "\r\n"):
            forms[f"{suffix or 'plain'}-{'crlf' if newline == chr(13) + chr(10) else 'lf'}"] = (
                newline + "  " + newline + FORMAT + suffix + newline + "Findings follow." + newline)
    return forms


class EelMarkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = fixture()
        root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        _, self.worktree = repository(root)

    def refused(self, call, *args, **kwargs) -> PodError:
        with self.assertRaises(PodError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, EEL_CODE)
        message = str(caught.exception)
        for phrase in ("Evidence Evaluation Ledger", "evidence only",
                       "separate Pod Execution Spec that cites it"):
            self.assertIn(phrase, message)
        return caught.exception

    def test_intake_refuses_title_and_each_body_marker_form_without_a_binding(self):
        error = self.refused(issue_intake, self.worktree, LOCATOR,
                             port=TitledPort(title="EEL: CI cost evidence", body=PES))
        self.assertEqual(error.detail["marker"], "title")
        for name, body in bodies().items():
            with self.subTest(form=name):
                error = self.refused(issue_intake, self.worktree, LOCATOR, port=TitledPort(body=body))
                self.assertEqual(error.detail["marker"], "body")

    def test_internal_issue_intake_refuses_with_the_eel_code(self):
        with patch("pod.github.GhPort", return_value=TitledPort(title="EEL: research")):
            self.refused(internal_run, "issue-intake", {"project": str(self.worktree), "locator": LOCATOR})

    def test_marker_gained_after_binding_is_refused_at_every_recheck(self):
        bound = issue_intake(self.worktree, LOCATOR, port=TitledPort(body=PES))["source"]
        self.assertEqual(issue_recheck(self.worktree, bound, port=TitledPort(body=PES))["status"], "current")
        # Only the title changed: metadata, yet the marker stops execution.
        self.refused(issue_recheck, self.worktree, bound, port=TitledPort(title="EEL: widgets", body=PES))
        self.refused(issue_recheck, self.worktree, bound, port=TitledPort(body=FORMAT + "\n" + PES))
        eel = TitledPort(title="EEL: widgets", body=PES)
        self.refused(_check_objective_source, self.worktree, bound, issue_port=eel)
        with patch("pod.github.GhPort", return_value=eel):
            self.refused(internal_run, "issue-recheck", {"project": str(self.worktree), "source": bound})
            self.refused(internal_run, "acceptance", {
                "project": str(self.worktree), "objective_source": bound, "criteria": ["works"],
                "evidence_rows": [], "candidate": "c", "policy_revision": "p", "sources": [],
                "dependencies": [], "environment": "fixture", "review_required": False,
                "hosted_required": False})

    def test_nothing_else_changes(self):
        cited = issue_intake(self.worktree, LOCATOR, port=TitledPort(body=PES))
        self.assertEqual(cited["status"], "ready")
        for body in ("  " + FORMAT + "\n", "\t" + FORMAT + "\n", "Context first\n" + FORMAT + "\n",
                     "```\n" + FORMAT + "\n```\n", "    " + FORMAT + "\n", FORMAT + " \n",
                     FORMAT + "\rmore\n", "**format:** Evidence Evaluation Ledger v1\n"):
            with self.subTest(body=body):
                self.assertEqual(issue_intake(self.worktree, LOCATOR, port=TitledPort(body=body))["status"],
                                 "ready")
        for title in ("eel: lower case", " EEL: indented", "Research EEL: later", "EEL - no colon"):
            with self.subTest(title=title):
                self.assertEqual(issue_intake(self.worktree, LOCATOR,
                                              port=TitledPort(title=title))["status"], "ready")
        # A direct objective has no issue source to recheck.
        self.assertIsNone(_check_objective_source(self.worktree, None,
                                                  issue_port=TitledPort(title="EEL: x")))
        # Amendments are not inspected for markers.
        amended = issue_intake(self.worktree, LOCATOR, port=TitledPort(amendment=FORMAT + "\n"),
                               amendments=[LOCATOR + "#issuecomment-99"])
        self.assertEqual(amended["status"], "ready")

    def test_convention_and_rule_are_documented_once(self):
        bundle = Path(__file__).resolve().parents[1] / "skills" / "pod"
        documents = [bundle / "SKILL.md", *sorted((bundle / "references").glob("*.md"))]
        holders = [path.name for path in documents if FORMAT in path.read_text(encoding="utf-8")]
        self.assertEqual(holders, ["eel-template.md", "issue-intake.md"])
        text = (bundle / "references" / "issue-intake.md").read_text(encoding="utf-8")
        for phrase in ("`EEL:", "evidence only", "Never execute it or restate it as a direct",
                       "separate PES citing it"):
            self.assertIn(phrase, " ".join(text.split()))


class EelCallerTests(unittest.TestCase):
    """Continuation and affected admission surface the EEL refusal itself."""

    # The admission fixture, without re-running its tests here.
    base = operation_tests.AdmissionTests
    setUp, checkpoint, frozen, restated = base.setUp, base.checkpoint, base.frozen, base.restated
    start, recover = base.start, base.recover

    SOURCE = {"schema": "pod-issue-source/v1", "repository": "acme/widgets", "number": 7,
              "locator": LOCATOR, "body_sha256": "a" * 64, "amendments": []}

    def eel(self) -> PodError:
        return PodError(EEL_CODE, "Issue is an Evidence Evaluation Ledger (EEL): evidence only")

    def test_affected_admission_and_pending_continuation_surface_the_eel_code(self):
        current = read(self.project, "objective")["checkpoint"]
        checkpoint(self.project, "objective", owner="owner",
                   value={key: value for key, value in {**current, "objective_source": self.SOURCE}.items()
                          if key not in ("pod_version", "bundle_digest", "seq")},
                   native={"runtime": "runtime"})
        frozen = packet({**self.frozen()["body"], "objective_source": self.SOURCE})
        with patch("pod.github.issue_recheck", side_effect=self.eel()), \
             self.assertRaises(PodError) as admission:
            self.start(frozen=frozen)
        self.assertEqual(admission.exception.code, EEL_CODE)
        self.assertEqual(self.port.starts, [])
        with patch("pod.github.issue_recheck", return_value={"status": "current"}):
            first = self.start(frozen=frozen)["admission"]
        update_admission(self.project, "objective", owner="owner", admission_id=first["admission_id"],
                         update=lambda row: row.update(state="unresolved", native_binding=None))
        self.port.starts.clear()
        self.port.state = "pending"
        with patch("pod.github.issue_recheck", side_effect=self.eel()), \
             self.assertRaises(PodError) as continuation:
            self.recover(first)
        self.assertEqual(continuation.exception.code, EEL_CODE)
        self.assertEqual(self.port.starts, [])


if __name__ == "__main__":
    unittest.main()
