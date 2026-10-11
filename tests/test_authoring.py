"""Copyable authoring bodies remain ordinary Markdown, separate from intake."""

from pathlib import Path
import re
import unittest

from pod.bundle import bundle_root
from pod.errors import PodError
from pod.github import issue_intake
from tests.common import fixture
from tests.test_eel_intake import TitledPort
from tests.test_github import repository


BUNDLE = bundle_root()
SECTIONS = {
    "seal": ("Context", "Scope", "Requirements", "Limits", "Acceptance", "Completion"),
    "pes": ("Objective", "Context", "Requirements", "Proof of Done", "Validation", "Completion"),
    "eel": ("Context and boundaries", "Sources", "Findings", "Evidence threshold", "Disposition"),
}
MARKERS = {"seal": "SEAL v1", "pes": "Pod Execution Spec v1", "eel": "Evidence Evaluation Ledger v1"}


def body(kind):
    text = (BUNDLE / f"references/{kind}-template.md").read_text()
    return text, re.search(r"```markdown\n(.*?)\n```", text, re.S).group(1)


def filled(kind, description, *, parent=False):
    """Small authored controls derive from the shipped fenced body, not another template."""
    _, draft = body(kind)
    draft = re.sub(r"<!--.*?-->\n", "", draft, flags=re.S)
    draft = re.sub(r"\[(?! \])[^\[\]]+\]", description, draft)
    draft = draft.replace("owner/repository", "acme/widgets")
    if kind == "pes":
        replacement = "https://github.com/acme/requirements/issues/11 R1 and Limits" if parent else None
        draft = re.sub(r"(?m)^\*\*Requirements source:\*\*.*\n",
                       f"**Requirements source:** {replacement}\n" if replacement else "", draft)
    # Omit optional sections in these controls. A detailed procedure is never added.
    draft = re.sub(r"\n## (?:Delivery outline|Open questions|Non-goals)\n.*?(?=\n## |\Z)", "", draft, flags=re.S)
    return draft


class AuthoringTests(unittest.TestCase):
    def test_templates_and_filled_examples_have_distinct_responsibilities(self):
        examples = [("seal", "Export notes in the single acme/widgets system.", False),
                    ("seal", "Exchange text exports between acme/api and acme/ui.", False),
                    ("pes", "Exported files preserve Unicode and use LF line endings.", False),
                    ("pes", "Implement parent R1: UI accepts API text exports; design remains agent-owned.", True),
                    ("eel", "Repeated sends were not observed; no change is justified by this evidence.", False)]
        with fixture() as root:
            _, worktree = repository(root)
            for kind, description, parent in examples:
                with self.subTest(kind=kind, description=description):
                    text, draft = body(kind)
                    self.assertIn(f"GitHub title: `{kind.upper()}:", text[:text.index("```markdown")])
                    self.assertTrue(draft.startswith(f"**Format:** {MARKERS[kind]}<br>\n"))
                    self.assertNotRegex(draft, r"(?m)^# ")
                    positions = [draft.index("## " + name) for name in SECTIONS[kind]]
                    self.assertEqual(positions, sorted(positions))
                    authored = filled(kind, description, parent=parent)
                    self.assertNotIn("<!--", authored)
                    self.assertNotRegex(authored, r"\[(?! \])[^\[\]]+\]")
                    self.assertIn(description, authored)
                    port = TitledPort(title=f"{kind.upper()}: Text exports", body=authored)
                    if kind == "pes":
                        self.assertEqual(issue_intake(worktree, "https://github.com/acme/widgets/issues/7", port=port)["status"], "ready")
                        self.assertEqual("**Requirements source:**" in authored, parent)
                        self.assertIn("PoD#1", authored)
                        self.assertIn("**Left to the implementer:**", authored)
                    else:
                        with self.assertRaises(PodError) as caught:
                            issue_intake(worktree, "https://github.com/acme/widgets/issues/7", port=port)
                        self.assertEqual(caught.exception.code, "issue_is_requirements_spec" if kind == "seal" else "issue_is_evidence_ledger")
                    if kind == "eel":
                        for label in ("Observation", "Evidence", "Assessment", "Impact", "Uncertainty and counter-evidence"):
                            self.assertIn(f"**{label}:**", authored)


if __name__ == "__main__":
    unittest.main()
