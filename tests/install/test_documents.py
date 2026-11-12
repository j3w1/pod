"""The contributor documents say what is true: the principles bind, and no inventory is required."""

import re
import unittest

from tests.support import ROOT


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.agents = (ROOT / "AGENTS.md").read_text()

    def test_agents_binds_the_design_principles(self):
        text = self.agents.split("## Upstream metadata governance", 1)[0]
        principles = (ROOT / "docs" / "design-principles.md").read_text()
        self.assertTrue(principles.startswith("# Pod's Enduring Design Principles\n"))
        # The read-when row and the short form both name the one canonical document.
        self.assertGreaterEqual(text.count("`docs/design-principles.md`"), 2)
        self.assertIn("## Pod's Enduring Design Principles", text)

    def test_completion_needs_tests_and_review_and_no_inventory(self):
        self.assertIn("Tests are the contract for behaviour", self.agents)
        self.assertIn("there is no requirement,\nscenario or coverage inventory to keep", self.agents)
        self.assertNotRegex(self.agents, r"requirement/scenario/coverage|pod-coverage|docs/validation\.md|suite_lanes")

    def test_every_document_and_check_the_instructions_name_exists(self):
        for name in set(re.findall(r"`((?:docs|tests|skills|tools)/[A-Za-z0-9_./-]+\.(?:md|py|json|yaml))`", self.agents)):
            self.assertTrue((ROOT / name).exists(), name)
        for name in set(re.findall(r"`((?:tests|docs)/[A-Za-z0-9_./-]+\.(?:md|py))`",
                                   (ROOT / "docs" / "design-principles.md").read_text())):
            self.assertTrue((ROOT / name).exists(), name)

    def test_the_external_metadata_notice_is_not_a_product_dependency(self):
        notice = self.agents.split("## Upstream metadata governance", 1)[1]
        self.assertEqual(notice.count("https://"), 1)
        self.assertIn("not required to install, use or fork it", notice)

    def test_the_readme_and_the_skill_name_only_commands_that_exist(self):
        from pod import cli
        known = set(cli.parser()._subparsers._group_actions[0].choices)
        for name in ("README.md", "skills/pod/SKILL.md", "docs/architecture.md"):
            used = set(re.findall(r"`pod ([a-z]+)\b", (ROOT / name).read_text()))
            self.assertLessEqual(used, known, f"{name}: {sorted(used - known)}")
