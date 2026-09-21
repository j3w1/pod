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
        self.assertEqual(requirements, [f"R{i:02}" for i in range(1, len(requirements) + 1)])
        self.assertEqual(scenarios, [f"A{i:02}" for i in range(1, len(scenarios) + 1)])
        self.assertEqual([row["id"] for row in coverage["scenarios"]], scenarios)
        for row in coverage["scenarios"]:
            with self.subTest(row=row["id"]):
                self.assertTrue(row["required_evidence"])
                self.assertIn(row["candidate_status"], ("NOT_RUN", "PASS", "RETIRED"))
                if row["candidate_status"] == "RETIRED":
                    self.assertTrue(row.get("retired"))

    def test_retired_scenarios_say_so_in_both_places(self):
        """A retirement is recorded as a decision, never silently reported as a pass."""
        root = Path(__file__).resolve().parents[1]
        spec = (root / "docs" / "pod-spec.md").read_text()
        coverage = json.loads((root / "docs" / "pod-coverage.json").read_text())
        retired_in_spec = {match for match in re.findall(r"^\| (A\d{2}) \| RETIRED", spec, flags=re.M)}
        retired_in_coverage = {row["id"] for row in coverage["scenarios"]
                               if row["candidate_status"] == "RETIRED"}
        self.assertEqual(retired_in_spec, retired_in_coverage)
        self.assertTrue(retired_in_spec)
        for row in coverage["scenarios"]:
            if row["candidate_status"] == "RETIRED":
                self.assertNotEqual(row["candidate_status"], "PASS")

    def test_no_evidence_type_survives_a_retired_platform(self):
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
