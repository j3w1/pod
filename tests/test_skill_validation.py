import os
from pathlib import Path
import shutil
import unittest

from pod.bundle import BUNDLE_FILES, bundle_root, canonical
from pod.errors import PodError
from pod.skill_validation import validate_installed, validate_skill
from tests.common import fixture

BUNDLE = bundle_root()


def copied(target: Path) -> Path:
    destination = target / "pod"
    shutil.copytree(BUNDLE, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return destination


def rewrite_frontmatter(skill: Path, replacement: str) -> None:
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    head, _, body = text.partition("---\n")[2].partition("---\n")
    (skill / "SKILL.md").write_text("---\n" + replacement + "---\n" + body, encoding="utf-8")


class SkillValidationTests(unittest.TestCase):
    def test_instruction_word_budgets(self):
        skill_words = len((BUNDLE / "SKILL.md").read_text().split())
        references = sorted((BUNDLE / "references").glob("*.md"))
        counts = {path.name: len(path.read_text().split()) for path in references}
        self.assertLessEqual(skill_words, 750)
        self.assertTrue(all(count <= 700 for count in counts.values()), counts)
        self.assertLessEqual(sum(counts.values()), 2200)
        self.assertIn("execution-spec.md", counts)
    def test_repository_bundle_is_valid(self):
        result = validate_skill(BUNDLE)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(set(result["files"]), set(BUNDLE_FILES))

    def test_frontmatter_allowlist_rejects_coordinator_overrides(self):
        with fixture() as root:
            for extra in ("context: fork\n", "model: opus\n", "effort: max\n",
                          "agent: Explore\n", "allowed-tools: Bash\n"):
                with self.subTest(extra=extra.strip()):
                    skill = copied(root / extra.split(":")[0])
                    text = (skill / "SKILL.md").read_text(encoding="utf-8")
                    front = text.partition("---\n")[2].partition("---\n")[0]
                    rewrite_frontmatter(skill, front + extra)
                    with self.assertRaises(PodError) as caught:
                        validate_skill(skill)
                    self.assertEqual(caught.exception.code, "invalid_skill")

    def test_the_version_lives_only_in_a_well_formed_version_file(self):
        with fixture() as root:
            for name, change in (("skill-meta", lambda s: s.replace("metadata:\n", 'metadata:\n  version: "0.4.0"\n')),):
                skill = copied(root / name)
                text = (skill / "SKILL.md").read_text(encoding="utf-8")
                (skill / "SKILL.md").write_text(change(text), encoding="utf-8")
                with self.assertRaises(PodError) as duplicated:
                    validate_skill(skill)
                self.assertIn("VERSION", str(duplicated.exception))
            for body in ("0.4\n", "v0.4.0\n", "0.4.0", "0.4.0\n0.5.0\n"):
                with self.subTest(body=body):
                    skill = copied(root / ("bad" + str(abs(hash(body)))))
                    (skill / "VERSION").write_text(body, encoding="ascii")
                    with self.assertRaises(PodError):
                        validate_skill(skill)

    def test_only_the_repository_version_link_is_allowed(self):
        with fixture() as root:
            skill = copied(root / "linked")
            (root / "linked" / "VERSION").write_text("0.4.0\n")
            (skill / "VERSION").unlink()
            (skill / "VERSION").symlink_to("../../VERSION")
            with self.assertRaises(PodError):
                validate_skill(skill)
            (skill / "VERSION").unlink()
            (skill / "VERSION").symlink_to("../VERSION")
            with self.assertRaises(PodError):
                validate_skill(skill)
        repository = Path(__file__).resolve().parents[1]
        link = repository / "skills" / "pod" / "VERSION"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "../../VERSION")
        self.assertEqual(validate_skill(repository / "skills" / "pod")["status"], "valid")

    def test_bare_helper_command_forms_are_rejected(self):
        with fixture() as root:
            for bad in ("Read preferences with `pod config`.",
                        "Run python -m pod.internal preview for the route.",
                        "See `pod.internal` for details."):
                with self.subTest(bad=bad):
                    skill = copied(root / str(abs(hash(bad))))
                    text = (skill / "SKILL.md").read_text(encoding="utf-8")
                    (skill / "SKILL.md").write_text(text + "\n" + bad + "\n", encoding="utf-8")
                    with self.assertRaises(PodError) as caught:
                        validate_skill(skill)
                    self.assertEqual(caught.exception.code, "invalid_skill")

    def test_launcher_must_not_import_the_package_before_its_check(self):
        with fixture() as root:
            skill = copied(root)
            launcher = skill / "scripts" / "pod.py"
            launcher.write_text("import yaml\n" + launcher.read_text(encoding="utf-8"),
                                encoding="utf-8")
            with self.assertRaises(PodError) as caught:
                validate_skill(skill)
            self.assertEqual(caught.exception.code, "invalid_skill")

    def test_inventory_extras_and_absences_are_both_reported(self):
        with fixture() as root:
            extra = copied(root / "extra")
            (extra / "notes.md").write_text("# stray\n", encoding="utf-8")
            with self.assertRaises(PodError) as with_extra:
                validate_skill(extra)
            self.assertIn("unexpected", str(with_extra.exception))
            missing = copied(root / "missing")
            (missing / "references" / "routing.md").unlink()
            with self.assertRaises(PodError) as without:
                validate_skill(missing)
            self.assertIn("missing", str(without.exception))

    def test_installed_copy_parity_names_what_differs(self):
        with fixture() as root:
            skill = copied(root)
            self.assertEqual(validate_installed(skill)["status"], "valid")
            (skill / "references" / "planning.md").write_text("# edited\n", encoding="utf-8")
            with self.assertRaises(PodError) as caught:
                validate_installed(skill)
            self.assertEqual(caught.exception.code, "bundle_parity")
            self.assertIn("references/planning.md", str(caught.exception))

    def test_interpreter_caches_are_not_a_difference(self):
        with fixture() as root:
            skill = copied(root)
            cache = skill / "__pycache__"
            cache.mkdir()
            (cache / "cli.cpython-313.pyc").write_bytes(b"cached")
            self.assertEqual(validate_installed(skill)["status"], "valid")
            self.assertEqual(validate_skill(skill)["status"], "valid")
