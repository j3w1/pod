from pathlib import Path
import shutil
import unittest
import zipfile

from pod.bundle import BUNDLE_FILES, bundle_root, canonical
from pod.errors import PodError
from pod.skill_validation import validate_installed, validate_skill, validate_wheel
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

    def test_metadata_version_must_match_the_package(self):
        with fixture() as root:
            skill = copied(root)
            text = (skill / "SKILL.md").read_text(encoding="utf-8")
            (skill / "SKILL.md").write_text(text.replace('version: "', 'version: "9.'),
                                            encoding="utf-8")
            with self.assertRaises(PodError):
                validate_skill(skill)

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

    def test_wheel_parity_compares_exact_bytes(self):
        with fixture() as root:
            wheel = root / "j3w1_pod-0.1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                for name, data in canonical().items():
                    archive.writestr(f"pod/{name}", data)
                archive.writestr("j3w1_pod-0.1.0.dist-info/METADATA", "Name: j3w1-pod\n")
                archive.writestr("pod/__pycache__/cli.cpython-313.pyc", b"ignored")
            self.assertEqual(validate_wheel(wheel)["status"], "valid")
            altered = root / "altered.whl"
            with zipfile.ZipFile(altered, "w") as archive:
                for name, data in canonical().items():
                    archive.writestr(f"pod/{name}", data + b"\n" if name == "cli.py" else data)
            with self.assertRaises(PodError) as caught:
                validate_wheel(altered)
            self.assertEqual(caught.exception.code, "bundle_parity")

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
