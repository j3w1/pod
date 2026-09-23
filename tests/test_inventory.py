import json
from pathlib import Path
import re
import subprocess
import unittest

from pod.errors import PodError
from pod.skill_validation import validate_skill
from tests.common import fixture

EXAMPLE_SPEC = """> **Outcome:** Search results include archived documents.\n\n\
**Target:** `acme/search`  \n\
**Format:** Pod Execution Spec v1  \n\
**Delivery:** Verified pull request.\n\n\
## Objective\nInclude archived documents behind an explicit filter.\n\n\
## Requirements\nPreserve the default active-only result set.\n\n\
## Proof of Done\n- [ ] **PoD#1 — Filtered results.** A focused test proves both modes.\n\n\
## Validation\nRun the search unit suite.\n\n\
## Completion\n+Report the commit, checks, review and open delivery gates.\n"""


def execution_spec_source(root: Path) -> Path:
    """Return the sole tracked or ordinary untracked authoring source."""
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard", "--", "*execution-spec*"],
        capture_output=True, check=True)
    matches = sorted(root / Path(value.decode()) for value in completed.stdout.split(b"\0") if value)
    if len(matches) != 1:
        raise ValueError("Execution Spec must have exactly one authoring source")
    return matches[0]


class InventoryIntegrityTests(unittest.TestCase):
    def test_repository_owned_validator_checks_the_actual_canonical_skill(self):
        root = Path(__file__).resolve().parents[1]
        result = validate_skill(root / "skills" / "pod")
        self.assertEqual(result["status"], "valid")
        self.assertIn("SKILL.md", result["files"])
        with fixture() as temporary:
            bundle = root / "skills" / "pod"
            skill = temporary / "pod"
            skill.mkdir(parents=True)
            for source in bundle.rglob("*"):
                if source.is_file() and "__pycache__" not in source.parts:
                    target = skill / source.relative_to(bundle)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
            (skill / "SKILL.md").write_text((skill / "SKILL.md").read_text().replace("name: pod", "name: wrong"))
            with self.assertRaises(PodError):
                validate_skill(skill)
    def test_spec_and_coverage_ids_are_complete(self):
        root = Path(__file__).resolve().parents[1]
        spec = (root / "docs" / "pod-spec.md").read_text()
        coverage = json.loads((root / "docs" / "pod-coverage.json").read_text())
        requirements = re.findall(r"^\| (R\d{2}) \|", spec, flags=re.M)
        scenarios = re.findall(r"^\| (A\d{2,3}) \|", spec, flags=re.M)
        self.assertEqual(requirements, [f"R{i:02}" for i in range(1, len(requirements) + 1)])
        self.assertEqual(scenarios, [f"A{i:02}" for i in range(1, len(scenarios) + 1)])
        self.assertEqual([row["id"] for row in coverage["scenarios"]], scenarios)
        for row in coverage["scenarios"]:
            with self.subTest(row=row["id"]):
                self.assertTrue(row["required_evidence"])
                self.assertIn(row["status"], ("NOT_RUN", "PASS"))

    def test_requirement_scenario_references_resolve(self):
        root = Path(__file__).resolve().parents[1]
        spec = (root / "docs" / "pod-spec.md").read_text()
        scenarios = set(re.findall(r"^\| (A\d{2,3}) \|", spec, flags=re.M))
        for requirement, refs in re.findall(r"^\| (R\d{2}) \| [^|]* \| .* \| ([^|]*) \|$", spec, flags=re.M):
            with self.subTest(requirement=requirement):
                for ref in (item.strip() for item in refs.split(",") if item.strip()):
                    if "–" in ref:
                        first, last = ref.split("–")
                        self.assertIn(first, scenarios)
                        self.assertIn(last, scenarios)
                    else:
                        self.assertIn(ref, scenarios)

    def test_evidence_kinds_are_exact(self):
        root = Path(__file__).resolve().parents[1]
        coverage = json.loads((root / "docs" / "pod-coverage.json").read_text())
        kinds = {kind for row in coverage["scenarios"] for kind in row["required_evidence"]}
        self.assertEqual(kinds - {"offline_behavior", "live_native", "external_gate",
                                  "hosted_ci_linux"}, set())

    def test_referenced_fixture_cases_exist(self):
        root = Path(__file__).resolve().parents[1]
        coverage = json.loads((root / "docs" / "pod-coverage.json").read_text())
        for row in coverage["scenarios"]:
            path = row["offline_test"]
            if path:
                module, cls, method = path.rsplit(".", 2)
                file = root.joinpath(*module.split(".")).with_suffix(".py")
                self.assertTrue(file.is_file(), path)
                text = file.read_text()
                self.assertIn(f"class {cls}", text)
                self.assertIn(f"def {method}", text)


class ExecutionSpecDocumentationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]

    def test_one_canonical_execution_spec_reference(self):
        canonical = self.root / "skills" / "pod" / "references" / "execution-spec.md"
        self.assertTrue(canonical.is_file())
        self.assertEqual(execution_spec_source(self.root), canonical)
        self.assertIn("references/execution-spec.md",
                      (self.root / "skills" / "pod" / "bundle.py").read_text())

    def test_execution_spec_inventory_ignores_build_output_but_rejects_an_extra_source(self):
        with fixture() as root:
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            canonical = root / "skills" / "pod" / "references" / "execution-spec.md"
            canonical.parent.mkdir(parents=True)
            canonical.write_text("canonical\n")
            (root / ".gitignore").write_text("build/\n")
            subprocess.run(["git", "-C", str(root), "add", ".gitignore",
                            "skills/pod/references/execution-spec.md"], check=True)
            generated = root / "build" / "lib" / "pod" / "references" / "execution-spec.md"
            generated.parent.mkdir(parents=True)
            generated.write_text("generated\n")
            self.assertEqual(execution_spec_source(root), canonical)
            duplicate = root / "docs" / "execution-spec.md"
            duplicate.parent.mkdir()
            duplicate.write_text("second authoring source\n")
            with self.assertRaisesRegex(ValueError, "exactly one authoring source"):
                execution_spec_source(root)

    def test_reference_has_readable_skeleton_and_numbered_proof(self):
        text = (self.root / "skills" / "pod" / "references" / "execution-spec.md").read_text()
        for heading in ("## Objective", "## Context", "## Requirements", "## Non-goals",
                        "## Design decisions", "## Proof of Done", "## Validation",
                        "## Completion"):
            self.assertIn(heading, text)
        self.assertIn("PoD#1", text)
        self.assertIn("**Delivery:**", text)
        self.assertIn("not a parser or DSL", text)
        self.assertIn("**Outcome:**", EXAMPLE_SPEC)
        self.assertIn("PoD#1", EXAMPLE_SPEC)
        self.assertNotIn("N/A", EXAMPLE_SPEC)

    def test_skill_preserves_direct_plan_and_continuation_paths(self):
        text = (self.root / "skills" / "pod" / "SKILL.md").read_text()
        for phrase in ("a direct objective uses the same flow", "Plan-only",
                       "reconcile changes", "Before implementation", "visible agent tabs"):
            self.assertIn(phrase, text)

    def test_external_metadata_notice_is_short_and_not_a_product_dependency(self):
        text = (self.root / "AGENTS.md").read_text()
        notice = text.split("## Upstream metadata governance", 1)[1]
        self.assertLessEqual(len(notice.split()), 90)
        self.assertIn("not part of Pod", notice)
        self.assertIn("not required to install, use or fork", notice)
        self.assertEqual(notice.count("https://"), 1)
        for forbidden in ("classification_authority", "Protected CE label", "NO_DUAL_WRITER"):
            self.assertNotIn(forbidden, notice)

    def test_readme_covers_practical_workflows_and_current_limits(self):
        text = (self.root / "README.md").read_text()
        for phrase in ("$pod https://github.com/owner/project/issues/123",
                       "Draft a Pod Execution Spec", "Authoring in ChatGPT",
                       "Plan only", "Continue after interruption", "visible agent tab",
                       "config approve sol", "256,000 tokens",
                       "npx skills add j3w1/pod --skill pod -a codex -a claude-code -g",
                       "npx skills update pod -g", "npx skills remove pod -g", "`VERSION`"):
            self.assertIn(phrase, text)
        self.assertIn("https://github.com/j3w1/pod/blob/main/skills/pod/references/execution-spec.md",
                      text)

    def test_ci_validates_the_skill_and_publishes_nothing(self):
        import yaml
        text = (self.root / ".github" / "workflows" / "ci.yml").read_text()
        workflow = yaml.safe_load(text)
        triggers = workflow[True] if True in workflow else workflow["on"]
        self.assertEqual(set(triggers), {"pull_request", "push"})
        self.assertEqual(triggers["push"], {"branches": ["main"]})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(list(workflow["jobs"]), ["skill"])
        for forbidden in ("gh " + "release", "git " + "tag", "git push", "tags:", "contents: write",
                          "python -m build", "sha256sum", "upload-artifact", "twine"):
            self.assertNotIn(forbidden, text)
        for required in ("unittest discover -s tests", "pod.skill_validation skills/pod",
                         "tools/source_audit.py", "npx --yes skills@", "--installed",
                         "setup --global --json", "doctor --json"):
            self.assertIn(required, text)

    def test_removed_publication_modules_are_not_referenced(self):
        tracked = subprocess.run(["git", "-C", str(self.root), "ls-files"], capture_output=True,
                                 text=True, check=True).stdout.split()
        for path in ("skills/pod/release.py", "tools/" + "release.py", "install.py", "pyproject.toml",
                     "MANIFEST.in", "CHANGELOG.md"):
            self.assertNotIn(path, tracked)
        self.assertFalse(any(path.startswith("release/") for path in tracked))
        referenced = subprocess.run(
            ["git", "-C", str(self.root), "grep", "-n", "-E",
             r"from \.release import|from pod\.release|import pod\.release|pod\.internal release|\bvalidate_wheel\b",
             "--", ".", ":!tests/test_inventory.py"],
            capture_output=True, text=True)
        self.assertEqual(referenced.stdout, "")
