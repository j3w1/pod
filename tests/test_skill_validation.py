import os
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
import unittest

from pod.bundle import BUNDLE_FILES, bundle_root, canonical
from pod.errors import PodError
from pod.skill_validation import (DELIVERY_FILES, MAX_DELIVERY_WORDS, MAX_SKILL_WORDS,
                                  main, validate_installed, validate_skill)
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
        self.assertLessEqual(skill_words, 400)
        self.assertTrue(all(count <= 450 for count in counts.values()), counts)
        self.assertLessEqual(sum(counts.values()), 2000)
        self.assertLessEqual(sum(len((BUNDLE / name).read_text().split())
                                 for name in DELIVERY_FILES), MAX_DELIVERY_WORDS)
        self.assertIn("execution-spec.md", counts)
    def test_repository_bundle_is_valid(self):
        result = validate_skill(BUNDLE)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(set(result["files"]), set(BUNDLE_FILES))

    def test_validator_reports_all_word_counts(self):
        result = validate_skill(BUNDLE)
        counts = result["word_counts"]
        self.assertEqual(counts["SKILL.md"], len((BUNDLE / "SKILL.md").read_text().split()))
        self.assertEqual(counts["delivery_path"], sum(counts[name] for name in DELIVERY_FILES))
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([str(BUNDLE)]), 0)
        for name, count in counts.items():
            self.assertIn(f"{name}: {count} words", output.getvalue())

    def test_descriptors_and_router_structure_are_enforced(self):
        cases = (
            ("description-length", "description: ", "description: Use when " + "x" * 251 + " # ", "250 characters"),
            ("description-trigger", "Use when", "Invoke if", "Use when"),
            ("compatibility", "compatibility: Linux,", "compatibility: |\n  Linux,", "one plain line"),
            ("sections", "## Roles", "## Other roles", "five router sections"),
            ("duplicate-link", "- PES authoring:", "- Duplicate: [intake](references/execution-spec.md).\n- PES authoring:", "exactly once"),
            ("missing-trigger", "- PES authoring:", "-", "read-when trigger"),
            ("duplicate-trigger", "- PES authoring:", "- Issue intake:", "read-when trigger"),
        )
        with fixture() as root:
            for name, old, new, message in cases:
                with self.subTest(name=name):
                    skill = copied(root / name)
                    path = skill / "SKILL.md"
                    text = path.read_text().replace(old, new)
                    if name == "duplicate-link":
                        # Exercise link uniqueness independently of the full authoring budget.
                        text = text.replace("Read objective, criteria, instructions and assumptions.\n", "")
                    path.write_text(text)
                    with self.assertRaisesRegex(PodError, message):
                        validate_skill(skill)
            for index, target in enumerate(("verification.md", "../references/verification.md",
                                           "https://example.invalid/references/verification.md",
                                           "verification", "../references/verification#proof")):
                skill = copied(root / f"reference-link-{index}")
                path = skill / "references" / "governor.md"
                path.write_text(path.read_text() + f"\nSee [verification]({target}).\n")
                with self.assertRaisesRegex(PodError, "must not link another reference"):
                    validate_skill(skill)
            for index, target in enumerate(("models.md", "../references/models", "<verification.md>")):
                skill = copied(root / f"definition-link-{index}")
                path = skill / "references" / "governor.md"
                path.write_text(path.read_text() + f"\nSee [route].\n\n[route]: {target}\n")
                with self.assertRaisesRegex(PodError, "must not link another reference"):
                    validate_skill(skill)
            skill = copied(root / "interface-length")
            path = skill / "agents" / "openai.yaml"
            path.write_text('interface:\n  display_name: Pod\n  short_description: ' + 'x' * 101)
            with self.assertRaisesRegex(PodError, "short_description is invalid"):
                validate_skill(skill)

    def test_compatibility_remains_optional(self):
        with fixture() as root:
            skill = copied(root)
            path = skill / "SKILL.md"
            path.write_text("\n".join(line for line in path.read_text().splitlines()
                                      if not line.startswith("compatibility:")) + "\n")
            self.assertEqual(validate_skill(skill)["status"], "valid")

    def test_runtime_omits_kernel_details_and_generic_test_reminders(self):
        texts = [(BUNDLE / "SKILL.md").read_text(),
                 *(p.read_text() for p in (BUNDLE / "references").glob("*.md"))]
        text = " ".join(" ".join(texts).lower().split())
        for removed in ("pending replay does not re-read model preferences",
                        "exact native settlement frees the logical slot",
                        "`native_default` effort exists only in older records",
                        "pod-context/v4", "pod-admission/v4", "pod-packet/v3", "pod-checkpoint/v3"):
            self.assertNotIn(removed, text)
        self.assertNotRegex(text, r"\brun (?:project |focused |required )?(?:tests|checks)\b")

    def test_each_word_budget_has_a_refusal(self):
        with fixture() as root:
            for name, limit, message in (("SKILL.md", 400, "SKILL.md exceeds"),
                                         ("references/cleanup.md", 450, "cleanup.md exceeds")):
                skill = copied(root / name.replace("/", "-"))
                path = skill / name
                text = path.read_text()
                path.write_text(text + " filler" * (limit + 1 - len(text.split())))
                with self.assertRaisesRegex(PodError, message):
                    validate_skill(skill)
            for kind in ("combined", "delivery"):
                skill = copied(root / kind)
                counts = validate_skill(skill)["word_counts"]
                total = counts["references_combined" if kind == "combined" else "delivery_path"]
                remaining = 2001 - total
                names = (["references/cleanup.md", "references/pes-template.md"] if kind == "combined"
                         else list(DELIVERY_FILES[1:]))
                for name in names:
                    path = skill / name
                    text = path.read_text()
                    added = min(remaining, 450 - len(text.split()))
                    path.write_text(text + " filler" * added)
                    remaining -= added
                self.assertEqual(remaining, 0)
                # The delivery-only control leaves the combined reference total in budget.
                if kind == "delivery":
                    for name in ("references/cleanup.md", "references/pes-template.md", "references/recovery.md"):
                        (skill / name).write_text("# Optional\n")
                with self.assertRaisesRegex(PodError, f"{'Skill references' if kind == 'combined' else 'Skill delivery path'} exceed"):
                    validate_skill(skill)

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
                text += " filler" * (MAX_SKILL_WORDS - len(text.split()))
                self.assertEqual(len(text.split()), 400)
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

    def test_obsolete_helper_and_install_forms_are_rejected(self):
        with fixture() as root:
            for bad in ("Run python3 ~/.agents/skills/pod/scripts/pod.py doctor.",
                        "Run python -m pod.internal preview for the route.",
                        "See `pod.internal` for details.",
                        "Run npx skills add j3w1/pod --skill pod."):
                with self.subTest(bad=bad):
                    skill = copied(root / str(abs(hash(bad))))
                    text = (skill / "SKILL.md").read_text(encoding="utf-8")
                    (skill / "SKILL.md").write_text(text.replace("Do not run source-checkout helpers or create another configuration source.", bad), encoding="utf-8")
                    with self.assertRaises(PodError) as caught:
                        validate_skill(skill)
                    self.assertEqual(caught.exception.code, "invalid_skill")

    def test_missing_launcher_forms_are_rejected(self):
        with fixture() as root:
            for required in ("pod config --json", "pod internal <op> --input FILE",
                             "~/.local/bin/pod"):
                with self.subTest(required=required):
                    skill = copied(root / str(abs(hash(required))))
                    text = (skill / "SKILL.md").read_text(encoding="utf-8")
                    (skill / "SKILL.md").write_text(text.replace(required, "omitted"), encoding="utf-8")
                    with self.assertRaises(PodError):
                        validate_skill(skill)

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
            (missing / "references" / "models.md").unlink()
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
