"""The bundle's structure is checked on a small fixture skill; the shipped skill is checked once, as it is."""

from contextlib import redirect_stdout
from io import StringIO
import os
import unittest

from pod import bundle
from pod.errors import PodError
from pod.skill_validation import main, validate_installed, validate_skill
from tests.install.install_support import BUNDLE, FIXTURE_SKILL, ROOT, fixture_skill
from tests.support import PodCase


class SkillValidationTests(PodCase):
    def setUp(self):
        super().setUp()
        self.count = 0

    def skill(self):
        self.count += 1
        return fixture_skill(self.tmp / f"copy-{self.count}")

    def refused(self, change, message):
        skill = self.skill()
        change(skill)
        with self.assertRaisesRegex(PodError, message) as caught:
            validate_skill(skill)
        self.assertEqual(caught.exception.code, "invalid_skill")

    def text(self, old, new):
        return lambda skill: (skill / "SKILL.md").write_text(FIXTURE_SKILL.replace(old, new))

    def test_the_shipped_bundle_is_valid(self):
        result = validate_skill(BUNDLE)
        self.assertEqual((result["status"], result["files"], result["digest"]),
                         ("valid", bundle.files(BUNDLE), bundle.digest(BUNDLE)))

    def test_the_fixture_is_valid_and_grows_without_any_list_to_edit(self):
        skill = self.skill()
        (skill / "models").mkdir(exist_ok=True)
        (skill / "models/added_later.py").write_text("VALUE = 1\n")
        (skill / "SKILL.md").write_text(FIXTURE_SKILL + "\nMore guidance.\n" * 3000)
        (skill / "__pycache__").mkdir()
        (skill / "__pycache__/cli.cpython-314.pyc").write_bytes(b"cached")
        self.assertIn("models/added_later.py", validate_skill(skill)["files"])
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([str(skill)]), 0)
        self.assertIn("Skill is valid!", output.getvalue())

    def test_front_matter_is_held_to_its_few_fields(self):
        for old, new, message in (
                ("name: pod", "name: other", "name must be pod"),
                ("Use when", "Invoke if", "Use when"),
                ("description: Use when", "description: Use when " + "x" * 250, "250 characters"),
                ("compatibility: Linux,", "compatibility: |\n  Linux,", "plain one-line values"),
                ("license: MIT\n", "license: MIT\nmodel: opus\n", "unsupported fields"),
                ("license: MIT\n", "license: MIT\nallowed-tools: Bash\n", "unsupported fields"),
                ("metadata:\n", 'metadata:\n  version: "0.4.0"\n', "only in VERSION"),
                ("https://github.com/j3w1/pod", "https://example.invalid/pod", "name this repository"),
                ("---\nname", "name", "front matter is missing")):
            with self.subTest(new=new):
                self.refused(self.text(old, new), message)
        skill = self.skill()
        (skill / "SKILL.md").write_text(FIXTURE_SKILL.replace("compatibility: Linux, the latest stable CPython and Orca.\n", ""))
        self.assertEqual(validate_skill(skill)["status"], "valid")

    def test_links_must_resolve_and_every_reference_must_be_reachable_and_titled(self):
        self.refused(self.text("references/planning.md", "references/absent.md"), "names references/absent.md")
        self.refused(self.text("[script](scripts/pod.py)", "`scripts/absent.py`"), "names scripts/absent.py")
        self.refused(self.text("- Planning: [planning](references/planning.md).\n", ""), "not named in SKILL.md")
        plain = self.skill()
        (plain / "SKILL.md").write_text(FIXTURE_SKILL.replace("[planning](references/planning.md).", "`references/planning.md`."))
        self.assertEqual(validate_skill(plain)["status"], "valid")
        self.refused(lambda skill: (skill / "references/planning.md").write_text("no title\n"), "missing its title")
        self.refused(lambda skill: (skill / "references/planning.md").write_text("# T\n" + "x" * bundle.MAX_TEXT), "oversized")

    def test_the_tree_carries_only_bundle_files_and_no_links(self):
        self.refused(lambda skill: (skill / "notes.txt").write_text("stray\n"), "not a kind of file")
        self.refused(lambda skill: (skill / "extra.py").symlink_to(skill / "cli.py"), "link or a special file")
        self.refused(lambda skill: os.mkfifo(skill / "pipe.py"), "link or a special file")
        self.refused(lambda skill: (skill / "installer.py").unlink(), "missing installer.py")
        self.refused(lambda skill: (skill / "errors.py").write_text("def broken(:\n"), "errors.py does not parse")
        self.refused(lambda skill: skill.rename(skill.with_name("other")) and None, "named pod")

    def test_the_version_is_one_well_formed_line_and_only_the_repository_may_link_it(self):
        for body in ("0.4\n", "v0.4.0\n", "0.4.0", "0.4.0\n0.5.0\n"):
            with self.subTest(body=body):
                self.refused(lambda skill, body=body: (skill / "VERSION").write_text(body), "MAJOR.MINOR.PATCH")

        def link(target):
            def change(skill):
                (skill.parent / "VERSION").write_text("0.4.0\n")
                (skill / "VERSION").unlink()
                (skill / "VERSION").symlink_to(target)
            return change
        self.refused(link("../VERSION"), "link or a special file")
        self.refused(link("../../VERSION"), "link or a special file")
        self.assertEqual(os.readlink(BUNDLE / "VERSION"), "../../VERSION")
        self.assertTrue((ROOT / "VERSION").is_file())

    def test_the_launcher_imports_nothing_of_the_package_before_its_check(self):
        def change(skill):
            launcher = skill / "scripts/pod.py"
            launcher.write_text("from pod import cli\n" + launcher.read_text())
        self.refused(change, "must not import pod")

    def test_the_agent_interface_file_is_exact(self):
        for body, message in (('interface:\n  display_name: "Pod"\n  short_description: "' + "x" * 101 + '"\n', "short_description"),
                              ('interface:\n  display_name: "Other"\n  short_description: "x"\n', "exactly the Pod interface"),
                              ('interface:\n  display_name: "Pod"\n  short_description: "x"\nextra: 1\n', "exactly the Pod interface"),
                              ("interface: [\n", "not readable")):
            with self.subTest(body=body):
                self.refused(lambda skill, body=body: (skill / "agents/openai.yaml").write_text(body), message)

    def test_a_placed_copy_is_compared_by_bytes_and_caches_are_no_difference(self):
        skill = self.skill()
        placed = fixture_skill(self.tmp / "placed")
        (placed / "__pycache__").mkdir()
        (placed / "__pycache__/cli.cpython-314.pyc").write_bytes(b"cached")
        self.assertEqual(validate_installed(placed, skill)["digest"], bundle.digest(skill))
        (placed / "references/planning.md").write_text("# edited\n")
        (placed / "extra.py").write_text("")
        (placed / "errors.py").unlink()
        with self.assertRaises(PodError) as caught:
            validate_installed(placed, skill)
        self.assertEqual(caught.exception.code, "bundle_parity")
        for part in ("differs references/planning.md", "unexpected extra.py", "missing errors.py"):
            self.assertIn(part, str(caught.exception))
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--installed", str(placed)]), 1)
        self.assertIn("Skill validation failed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
