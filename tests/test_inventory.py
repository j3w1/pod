import json
from pathlib import Path
import re
import unittest

from pod.errors import PodError
from pod.skill_validation import validate_skill
from tests.common import fixture


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
        scenarios = re.findall(r"^\| (A\d{2}) \|", spec, flags=re.M)
        self.assertEqual(requirements, [f"R{i:02}" for i in range(1, 60)])
        self.assertEqual(scenarios, [f"A{i:02}" for i in range(1, 83)])
        self.assertEqual([row["id"] for row in coverage["scenarios"]], scenarios)
        self.assertTrue(all(row["required_evidence"] and row["candidate_status"] == "NOT_RUN"
                            for row in coverage["scenarios"]))

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
