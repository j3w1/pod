import ast
import json
from pathlib import Path
import re
import subprocess
import unittest
from urllib.parse import unquote

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


def execution_spec_source(root: Path, basename: str = "execution-spec.md") -> Path:
    """Return the sole tracked or ordinary untracked authoring source."""
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard", "--", "*" + basename.removesuffix(".md") + "*"],
        capture_output=True, check=True)
    matches = sorted(root / Path(value.decode()) for value in completed.stdout.split(b"\0") if value)
    if len(matches) != 1:
        raise ValueError("Execution Spec must have exactly one authoring source")
    return matches[0]


def offline_test_references(value):
    """Normalize the single coverage field while rejecting malformed pointers."""
    if value is None:
        return []
    if isinstance(value, str):
        refs = [value]
    elif isinstance(value, list) and value:
        refs = value
    else:
        raise ValueError("offline_test must be a string, nonempty list, or null")
    if any(not isinstance(ref, str) or not ref for ref in refs):
        raise ValueError("offline_test references must be nonempty strings")
    if len(set(refs)) != len(refs):
        raise ValueError("offline_test references must be unique")
    return refs


def assert_test_reference_exists(root: Path, path: str) -> None:
    module, cls, method = path.rsplit(".", 2)
    file = root.joinpath(*module.split(".")).with_suffix(".py")
    if not file.is_file():
        raise ValueError(f"offline_test module does not exist: {path}")
    tree = ast.parse(file.read_text(), filename=str(file))
    # The method must be defined in the named class itself, not merely somewhere in the module.
    exists = any(isinstance(node, ast.ClassDef) and node.name == cls
                 and any(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method
                         for child in node.body)
                 for node in tree.body)
    if not exists:
        raise ValueError(f"offline_test case does not exist: {path}")


def spec_paths(root: Path) -> list[Path]:
    """The index and every domain file, including nested ones such as docs/spec/models/."""
    return [root / "docs" / "pod-spec.md", *sorted((root / "docs" / "spec").rglob("*.md"))]


def spec_text(root: Path) -> str:
    return "\n".join(path.read_text() for path in spec_paths(root))


def spec_inventory(root: Path):
    text = spec_text(root)
    requirements = re.findall(r"^### (R\d{2,3}) — .*\n\nType: ([BHI,]+) · Scenarios: (.*)$",
                              text, flags=re.M)
    scenarios = re.findall(r'^- <a id="(a\d{2,3})"></a>\*\*(A\d{2,3})\*\*',
                           text, flags=re.M)
    return requirements, scenarios


def markdown_anchors(text: str) -> set[str]:
    anchors = set(re.findall(r'<a id="([^"]+)"', text))
    for heading in re.findall(r"^#{1,6} (.+)$", text, flags=re.M):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        anchors.add(slug)
    return anchors


class InventoryIntegrityTests(unittest.TestCase):
    def test_offline_test_pointer_form_accepts_multiple_unique_existing_references(self):
        root = Path(__file__).resolve().parents[1]
        coverage = json.loads((root / "docs" / "pod-coverage.json").read_text())
        row = next(row for row in coverage["scenarios"] if row["id"] == "A157")
        self.assertIsInstance(row["offline_test"], list)
        refs = offline_test_references(row["offline_test"])
        self.assertGreaterEqual(len(refs), 2)
        for ref in refs:
            assert_test_reference_exists(root, ref)
        with self.assertRaises(ValueError):
            offline_test_references([refs[0], refs[0]])
        with self.assertRaisesRegex(ValueError, "does not exist"):
            assert_test_reference_exists(root, "tests.test_missing.Case.test_missing")

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
        coverage = json.loads((root / "docs" / "pod-coverage.json").read_text())
        requirements, scenarios = spec_inventory(root)
        ids = [id for id, _, _ in requirements]
        scenario_ids = [id for _, id in scenarios]
        self.assertEqual(sorted(ids, key=lambda id: int(id[1:])),
                         [f"R{i:02}" for i in range(1, len(ids) + 1)])
        self.assertEqual(sorted(scenario_ids, key=lambda id: int(id[1:])),
                         [f"A{i:02}" for i in range(1, len(scenario_ids) + 1)])
        self.assertEqual([anchor for anchor, id in scenarios], [id.lower() for id in scenario_ids])
        self.assertEqual(sorted(row["id"] for row in coverage["scenarios"]), sorted(scenario_ids))
        for _, kind, _ in requirements:
            self.assertTrue(set(kind.split(",")) <= {"B", "H", "I"})
        for row in coverage["scenarios"]:
            with self.subTest(row=row["id"]):
                self.assertTrue(row["required_evidence"])
                self.assertIn(row["status"], ("NOT_RUN", "PASS"))

    def test_requirement_scenario_references_resolve(self):
        root = Path(__file__).resolve().parents[1]
        requirements, rows = spec_inventory(root)
        scenarios = {id for _, id in rows}
        referenced = set()
        for requirement, _, refs in requirements:
            with self.subTest(requirement=requirement):
                for ref in re.findall(r"\[(A\d{2,3})\]\([^)]*\)", refs):
                    self.assertIn(ref, scenarios)
                    referenced.add(ref)
        self.assertEqual(referenced, scenarios)

    def test_relative_document_links_and_anchors_resolve(self):
        root = Path(__file__).resolve().parents[1]
        for source in (root / "docs").rglob("*.md"):
            text = source.read_text()
            for destination in re.findall(r"(?<!!)\[[^]]+\]\(([^)]+)\)", text):
                if re.match(r"[a-z]+://|mailto:", destination):
                    continue
                path, _, fragment = unquote(destination).partition("#")
                target = (source.parent / path).resolve() if path else source
                with self.subTest(source=source.relative_to(root), link=destination):
                    self.assertTrue(target.is_file(), f"missing link target: {destination}")
                    if fragment:
                        self.assertIn(fragment, markdown_anchors(target.read_text()))

    def test_evidence_kinds_are_exact(self):
        root = Path(__file__).resolve().parents[1]
        coverage = json.loads((root / "docs" / "pod-coverage.json").read_text())
        kinds = {kind for row in coverage["scenarios"] for kind in row["required_evidence"]}
        self.assertEqual(kinds - {"unit", "pty_subprocess", "installed_bundle",
                                  "hosted_ci", "live_native", "independent_review"}, set())
        self.assertEqual(kinds, {"unit", "pty_subprocess", "installed_bundle",
                                 "hosted_ci", "live_native", "independent_review"})

    def test_current_contract_names_the_new_boundaries(self):
        root = Path(__file__).resolve().parents[1]
        spec = spec_text(root)
        validation = (root / "docs" / "validation.md").read_text()
        for phrase in ("pod/v2", "setup_required", "config.yaml.pod-v1", "pod-catalog/v3",
                       "pod-observations/v1", "preference_changed",
                       "Pending same-UUID replay", "native_default", "pod-context/v4",
                       "one-shot installer", "cleanup-plan", "--match-head-commit",
                       "delivery: {record}", "selection_required", "critical-path"):
            self.assertIn(phrase, spec)
        guidance = "\n".join(path.read_text() for path in
                             (root / "skills" / "pod" / "references").glob("*.md"))
        for phrase in ("POD_REQUIRE_PTY=1", "pod.catalog --check", "SHA-pinned public install",
                       "independent review", "live native"):
            self.assertIn(phrase, validation)
        for obsolete in ("config " + "approve", "pod setup", "quota_" + "fresh_seconds",
                         "pod-context/" + "v3", "effort stays " + "adaptive", "adaptive " + "effort",
                         "selection: " + "custom", "All " + "models", "My " + "selection",
                         "small " + "tie-breaker", "Recommended " + "ordering"):
            self.assertNotIn(obsolete, spec + validation)
        for obsolete in ("small " + "tie-breaker", "effort stays " + "adaptive", "adaptive " + "effort",
                         "The six " + "ids"):
            self.assertNotIn(obsolete, guidance + (root / "skills" / "pod" / "SKILL.md").read_text())

    def test_nested_model_specs_hold_each_model_rule_once(self):
        root = Path(__file__).resolve().parents[1]
        models = root / "docs" / "spec" / "models"
        self.assertEqual(sorted(path.name for path in models.glob("*.md")),
                         ["catalog.md", "preferences.md", "routing.md"])
        index = (root / "docs" / "spec" / "models.md").read_text()
        self.assertNotRegex(index, r"(?m)^### R\d")
        self.assertNotIn('<a id="a', index)
        homes = {}
        for path in models.glob("*.md"):
            for requirement in re.findall(r"(?m)^### (R\d{2,3}) — ", path.read_text()):
                homes.setdefault(requirement, []).append(path.name)
        for requirement, home in (("R09", "routing.md"), ("R15", "routing.md"), ("R101", "routing.md"),
                                  ("R10", "preferences.md"), ("R21", "preferences.md"),
                                  ("R100", "preferences.md"), ("R14", "catalog.md"),
                                  ("R102", "catalog.md"), ("R103", "catalog.md")):
            self.assertEqual(homes[requirement], [home])
        self.assertIn(models / "routing.md", spec_paths(root))

    def test_referenced_fixture_cases_exist(self):
        root = Path(__file__).resolve().parents[1]
        coverage = json.loads((root / "docs" / "pod-coverage.json").read_text())
        for row in coverage["scenarios"]:
            for path in offline_test_references(row["offline_test"]):
                assert_test_reference_exists(root, path)


class ExecutionSpecDocumentationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]

    def test_one_canonical_execution_spec_reference(self):
        canonical = self.root / "skills" / "pod" / "references" / "execution-spec.md"
        self.assertTrue(canonical.is_file())
        self.assertEqual(execution_spec_source(self.root), canonical)
        self.assertIn("references/execution-spec.md",
                      (self.root / "skills" / "pod" / "bundle.py").read_text())
        template = self.root / "skills" / "pod" / "references" / "pes-template.md"
        self.assertEqual(execution_spec_source(self.root, "pes-template.md"), template)
        self.assertIn("references/pes-template.md",
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
        text = (self.root / "skills/pod/references/pes-template.md").read_text()
        headings = ("# <Title>", "## Objective", "## Context", "## Requirements",
                    "## Non-goals", "## Design decisions", "## Proof of Done",
                    "## Validation", "## Completion")
        for heading in headings:
            self.assertIn(heading, text)
        positions = [text.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions))
        for clause in ("**Outcome:**", "**Target:**", "**Format:**", "**Delivery:**",
                       "PoD#1", "not a parser or DSL", "Number every Proof of Done item"):
            with self.subTest(clause=clause):
                self.assertIn(clause, text)
        self.assertNotIn("N/A", text)

    def test_shipped_content_supports_affected_coverage_clauses(self):
        # Canonical-content claims read shipped text; in-test examples are separate proof.
        # This bounded sibling check does not expand the one-anchor topic table.
        clauses = (
            ("A52", "SKILL.md", ("Read config at intake, new-session continuation, preference-change notice",
                "preference_changed", "preference_revision_stale", "policy_revision_mismatch",
                "setup_required", "installed_version_changed")),
            ("A180", "references/cleanup.md", ("read-only and objective-scoped", "changed facts stop cleanup",
                "Protected/uncertain resources stay")),
            ("A180", "references/planning.md", ("Optimize time to a verified result",)),
            ("A180", "SKILL.md", ("Every map-bearing write", "carries next `seq`",
                "read map on resume/`map_stale`", "`internal brief`", "read-only uncertain-input dry run")),
            ("A110", "references/recovery.md", ("`source=screen`", "displayed **Skip for now**",
                "once, verify readiness", "same model/effort", "exact native `--retry-request` recovery, never resend",
                "Unproven controls block", "Missing live opt-out proof stays NOT_RUN")),
        )
        coverage = {row["id"]: row for row in json.loads(
            (self.root / "docs/pod-coverage.json").read_text())["scenarios"]}
        pointer = "tests.test_inventory.ExecutionSpecDocumentationTests.test_shipped_content_supports_affected_coverage_clauses"
        for scenario, relative, anchors in clauses:
            with self.subTest(scenario=scenario, path=relative):
                self.assertIn(pointer, offline_test_references(coverage[scenario]["offline_test"]))
                text = (self.root / "skills/pod" / relative).read_text()
                for anchor in anchors:
                    self.assertIn(anchor, text)

    def test_skill_states_its_boundaries(self):
        root = self.root / "skills" / "pod"
        texts = {"SKILL.md": " ".join((root / "SKILL.md").read_text().lower().split())}
        texts.update({"references/" + p.name: " ".join(p.read_text().lower().split())
                      for p in (root / "references").glob("*.md")})
        # One distinctive normalized anchor per required topic; review assesses the prose.
        anchors = {
            "coordinator": ("SKILL.md", "pool edits never change them"),
            "lifecycle": ("SKILL.md", "orca owns runs, tasks, dispatches"),
            "helpers_and_acceptance": ("SKILL.md", "helpers validate admission and record evidence"),
            "obligations": ("SKILL.md", "every obligation satisfied or validly withdrawn with reason"),
            "final_report": ("SKILL.md", "a final report, final-candidate gates"),
            "required_review": ("SKILL.md", "independent review required by work/project"),
            "delivery": ("SKILL.md", "governor-recorded delivery"),
            "withdrawal": ("SKILL.md", "withdrawal is not passing verification"),
            "evidence_labels": ("SKILL.md", "live native proof, project acceptance and merge"),
            "uncertainty": ("SKILL.md", "uncertainty, unresolved native references and next safe action"),
            "continuing": ("SKILL.md", "continue through implementation, verification and corrections"),
            "early_stop": ("SKILL.md", "authority boundary, owner-intent question or named external dependency"),
            "direct_work": ("SKILL.md", "use direct work when sufficient"),
            "plan_only": ("SKILL.md", "plan-only permits host-permitted investigation"),
            "scope_authority": ("SKILL.md", "scope text never grants authority; trusted policy remains binding"),
            "remote_and_deletion": ("SKILL.md", "remote git or worktree/branch deletion consent"),
            "disabled_confirmation": ("SKILL.md", "exact disabled-route confirmation before use"),
            "user_direct": ("SKILL.md", "direct user model, agent, role or worker-count directive"),
            "unknown_prompts": ("SKILL.md", "unknown or permission prompts block"),
            "safety": ("SKILL.md", "never reroute a safety refusal"),
            "capacity": ("SKILL.md", "never infer physical capacity or count other objectives' workers"),
            "pin_limits": ("references/models.md", "direct user constraints, host restrictions, review independence, tool permissions and spending"),
            "worktree": ("references/planning.md", "`--branch orca/<task-slug>`"),
            "project_audit": ("references/planning.md", "cited `project_policy` assurances"),
            "provider_boundary": ("references/orca-boundary.md", "write provider settings, automate provider prompts"),
            "eel": ("references/execution-spec.md", "never execute it or restate it as a direct objective"),
            "continuation": ("references/recovery.md", "on resume read native state first"),
            "preference_races": ("references/recovery.md", "rechoose at most twice, then report the conflict"),
            "trivial": ("references/models.md", "trivial direct work needs no worker"),
            "testable": ("references/models.md", "strongly testable work favors efficiency"),
            "judgment": ("references/models.md", "high-risk judgment needs proportionate margin"),
            "latency": ("references/models.md", "blocking latency favors responsiveness"),
            "recurrence": ("references/models.md", "recurring tasks favor proven efficient routes"),
            "preferred": ("references/models.md", "preferred overrides need material task-specific reasons"),
            "pin_conflicts": ("references/models.md", "report pin conflicts"),
            "no_forced_delegation": ("references/models.md", "no delegation just to apply a pin"),
            "update_skip": ("references/recovery.md", "send only its displayed selector (shown number/key; enter only if shown) once"),
        }
        for topic, (permitted, anchor) in anchors.items():
            with self.subTest(topic=topic):
                occurrences = {name: text.count(anchor) for name, text in texts.items()
                               if anchor in text}
                self.assertEqual(occurrences, {permitted: 1})
        self.assertIn("**Outcome:**", EXAMPLE_SPEC)
        self.assertIn("PoD#1", EXAMPLE_SPEC)
        self.assertNotIn("N/A", EXAMPLE_SPEC)

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
        for phrase in ("curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh | sh",
                       "$pod https://github.com/owner/project/issues/123",
                       "Pod Execution Spec reference", "Authoring in ChatGPT",
                       "Plan only", "Continue after interruption",
                       "pod config --json", "native_default", "gpt-6-luna", "`VERSION`",
                       "pod update", "docs/installation.md"):
            self.assertIn(phrase, text)
        self.assertLess(text.index("curl -fsSL"), text.index("## Contents"))
        self.assertIn("https://github.com/j3w1/pod/blob/main/skills/pod/references/execution-spec.md",
                      text)
        self.assertIn("https://github.com/j3w1/pod/blob/main/skills/pod/references/pes-template.md",
                      text)

    def test_ci_validates_the_skill_and_publishes_nothing(self):
        import yaml
        text = (self.root / ".github" / "workflows" / "ci.yml").read_text()
        workflow = yaml.safe_load(text)
        triggers = workflow[True] if True in workflow else workflow["on"]
        self.assertEqual(set(triggers), {"pull_request", "push"})
        self.assertEqual(triggers["push"], {"branches": ["main"]})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        # `linux` is the status check the main branch ruleset requires.
        self.assertEqual(list(workflow["jobs"]), ["linux"])
        for forbidden in ("gh " + "release", "git " + "tag", "git push", "tags:", "contents: write",
                          "python -m build", "sha256sum", "upload-artifact", "twine"):
            self.assertNotIn(forbidden, text)
        for required in ("unittest discover -s tests", "pod.skill_validation skills/pod",
                         "pod.catalog --check", "tools/source_audit.py", "--installed",
                         "POD_REQUIRE_PTY", "git archive", "POD_INSTALL_SOURCE=\"file://",
                         "raw.githubusercontent.com/${GITHUB_REPOSITORY}/${GITHUB_SHA}/install.sh",
                         "codeload.github.com/${GITHUB_REPOSITORY}/tar.gz/${GITHUB_SHA}",
                         "doctor --json", "pod\" update"):
            self.assertIn(required, text)
        self.assertNotIn("setup --global", text)

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
