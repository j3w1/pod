import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from pod.cli import execute, parser
from pod.errors import PodError
from pod.bundle import BUNDLE_FILES, canonical, version
from pod.setup import _relative_key, inspect, setup
from pod.ledger import checkpoint
from tests.common import fixture


class SetupCliTests(unittest.TestCase):
    def test_nested_skill_paths_use_canonical_manifest_keys(self):
        root = Path("/project/.agents/skills/pod")
        nested = root / "references" / "planning.md"
        self.assertEqual(_relative_key(nested, root), "references/planning.md")

    def test_local_idempotent_and_modified_skill_preserved(self):
        with fixture() as root:
            first = setup(root)
            self.assertEqual(first["skills"]["codex"], "installed")
            self.assertEqual(setup(root)["skills"]["codex"], "reused")
            target = root / ".agents" / "skills" / "pod" / "SKILL.md"
            target.write_text("user edit")
            result = setup(root)
            self.assertEqual(result["skills"]["codex"], "preserved_modified")
            self.assertEqual(target.read_text(), "user edit")

    def test_compatible_global_reuses_and_prunes_only_owned_unchanged_local(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "codex"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = root / "project"
            project.mkdir()
            self.assertEqual(setup(project)["skills"]["codex"], "installed")
            local_codex = project / ".agents" / "skills" / "pod"
            local_claude = project / ".claude" / "skills" / "pod"
            (local_claude / "SKILL.md").write_text("personal change")
            setup(project, global_scope=True)
            outcome = setup(project)
            self.assertEqual(outcome["skills"]["codex"], "reused_global_removed_owned_local")
            self.assertFalse(local_codex.exists())
            self.assertEqual(outcome["skills"]["claude"], "reused_global_preserved_local")
            self.assertEqual((local_claude / "SKILL.md").read_text(), "personal change")
            self.assertFalse(local_codex.exists())

    def test_global_setup_leaves_project_unchanged(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "codex"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = root / "project"
            project.mkdir()
            result = setup(project, global_scope=True)
            self.assertEqual(result["scope"], "global")
            self.assertEqual(list(project.iterdir()), [])
            self.assertEqual((root / "codex" / "skills" / "pod" / "SKILL.md").read_bytes(), canonical()["SKILL.md"])

    def test_relative_native_skill_homes_fail_before_global_project_write(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
                values = {"CODEX_HOME": str(root / "codex"),
                          "CLAUDE_CONFIG_DIR": str(root / "claude"), name: "relative-home"}
                with self.subTest(name=name), patch.dict(os.environ, values):
                    with self.assertRaises(PodError) as caught:
                        setup(project, global_scope=True)
                    self.assertEqual(caught.exception.code, "invalid_native_home")
                    self.assertEqual(list(project.iterdir()), [])
                    self.assertFalse((project / "relative-home").exists())

    def test_global_skill_homes_reject_absolute_and_symlink_project_containment_before_writes(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            contained = project / "native-home"
            contained.mkdir()
            redirected = root.parent / "skill-home-link"
            redirected.symlink_to(contained, target_is_directory=True)
            safe = root.parent / "safe-skill-home"
            original = dict(os.environ)
            for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
                for value in (contained, redirected):
                    homes = {"CODEX_HOME": str(safe / "codex"),
                             "CLAUDE_CONFIG_DIR": str(safe / "claude"), name: str(value)}
                    with self.subTest(name=name, value=value), patch.dict(os.environ, homes):
                        with self.assertRaises(PodError) as caught:
                            setup(project, global_scope=True)
                        self.assertEqual(caught.exception.code, "project_contained_native_home")
                        self.assertEqual(list(contained.iterdir()), [])
                        self.assertFalse(safe.exists())
            self.assertEqual(dict(os.environ), original)

    def test_doctor_diagnoses_duplicate_scopes_without_repair(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "codex"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = root / "project"
            project.mkdir()
            setup(project)
            setup(project, global_scope=True)
            with patch("pod.cli.contract", return_value={"status": "unavailable"}), \
                 patch("pod.cli.account_metadata", side_effect=PodError("unavailable", "offline")):
                report = execute(parser().parse_args(["doctor"]), project)
            self.assertEqual(report["integration_overlap"]["codex"], "duplicate_current_copies")
            self.assertTrue((project / ".agents" / "skills" / "pod" / "SKILL.md").is_file())

    def test_four_public_families(self):
        help_text = parser().format_help()
        self.assertIn("setup", help_text)
        self.assertIn("config", help_text)
        self.assertIn("doctor", help_text)
        self.assertIn("status", help_text)
        self.assertNotIn("implement", help_text)
        with self.assertRaises(SystemExit):
            parser().parse_args(["worker-preflight"])

    def test_config_edit_scope_and_invalid_edit_preserved(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "config"),
                                                        "EDITOR": f'"{sys.executable}" -c pass'}):
            project = root / "project"
            project.mkdir()
            args = parser().parse_args(["config", "--edit"])
            value = execute(args, project)
            self.assertEqual(value["scope"], "personal")
            self.assertFalse((project / ".pod").exists())
            personal = root / "config" / "pod" / "config.yaml"
            self.assertTrue(personal.exists())
            args = parser().parse_args(["config", "--edit", "--scope", "project"])
            self.assertEqual(execute(args, project)["scope"], "project")
            local = project / ".pod" / "config.yaml"
            local.write_text("schema: wrong\n")
            with self.assertRaises(PodError):
                execute(parser().parse_args(["config", "--check"]), project)
            self.assertEqual(local.read_text(), "schema: wrong\n")

    def test_doctor_passive(self):
        with fixture() as root, patch.dict(os.environ, {
                "CODEX_HOME": str(root.parent / "codex-home"),
                "CLAUDE_CONFIG_DIR": str(root.parent / "claude-home"),
                "XDG_CONFIG_HOME": str(root.parent / "config-home")}):
            result = execute(parser().parse_args(["doctor"]), root)
            self.assertEqual(result["native_probe"], "not_run")
            self.assertFalse((root / ".pod").exists())
            self.assertFalse((root / "codex").exists())
            self.assertFalse((root / "config").exists())

    def test_status_joins_selected_run_to_checkpoint_compactly(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
            project = root / "project"
            project.mkdir()
            checkpoint(project, "objective", owner="owner", native={"runtime": "runtime"},
                       value={"schema": "pod-checkpoint/v1", "criteria": ["works"],
                              "plan_revision": "p", "candidate": "c", "policy_revision": "r",
                              "native_refs": [{"runId": "run"}], "assignments": [], "questions": [],
                              "verification_gaps": ["works"], "next_safe_action": "run checks"})
            workers = {"runtime": "runtime", "scope": {"source": "flag"}, "complete": True,
                       "workers": [{"terminalState": "active", "projection": {"attention": {"requiresAction": True}}},
                                   {"terminalState": "released", "projection": {}}]}
            with patch("pod.cli.worker_rows", return_value=workers):
                result = execute(parser().parse_args(["status", "--run", "run"]), project)
            self.assertEqual(result["verification_gaps"], ["works"])
            self.assertEqual(result["next_safe_action"], "run checks")
            self.assertEqual(result["native"]["workers_by_state"], {"active": 1, "released": 1})
            self.assertNotIn("workers", result["native"])


class SkillOwnershipTests(unittest.TestCase):
    """Who owns a placed copy, and what repeating setup is allowed to touch."""

    def install_global(self, root):
        project = root / "project"
        project.mkdir(exist_ok=True)
        setup(project, global_scope=True)
        return project

    def test_a_skills_cli_symlink_is_recognised_and_left_alone(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = self.install_global(root)
            canonical_copy = root / "agents" / "skills" / "pod"
            linked = root / "claude" / "skills" / "pod"
            for name in sorted(BUNDLE_FILES, reverse=True):
                path = linked / name
                if path.is_file():
                    path.unlink()
            for directory in ("references", "agents", "scripts"):
                if (linked / directory).is_dir():
                    (linked / directory).rmdir()
            linked.rmdir()
            linked.symlink_to(canonical_copy, target_is_directory=True)
            report = inspect(project, global_scope=True)
            self.assertEqual(report["claude"]["status"], "managed_by_skills_cli")
            self.assertEqual(report["claude"]["manager"], "skills_cli")
            outcome = setup(project, global_scope=True)
            self.assertEqual(outcome["skills"]["claude"], "managed_by_skills_cli")
            self.assertTrue(linked.is_symlink())
            self.assertEqual(linked.resolve(), canonical_copy.resolve())

    def test_the_canonical_copy_behind_a_link_is_also_recognised(self):
        """The shape `npx skills add` actually produces, including without a lock file."""
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = root / "project"
            project.mkdir()
            canonical_copy = root / "agents" / "skills" / "pod"
            canonical_copy.mkdir(parents=True)
            for name, data in canonical().items():
                target = canonical_copy / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            linked = root / "claude" / "skills"
            linked.mkdir(parents=True)
            (linked / "pod").symlink_to(canonical_copy, target_is_directory=True)
            report = inspect(project, global_scope=True)
            self.assertEqual(report["codex"]["status"], "managed_by_skills_cli")
            self.assertEqual(report["claude"]["status"], "managed_by_skills_cli")
            before = {name: (canonical_copy / name).read_bytes() for name in BUNDLE_FILES}
            outcome = setup(project, global_scope=True)
            self.assertEqual(set(outcome["skills"].values()), {"managed_by_skills_cli"})
            self.assertEqual(set(outcome["ownership"].values()), {"skills_cli"})
            after = {name: (canonical_copy / name).read_bytes() for name in BUNDLE_FILES}
            self.assertEqual(before, after)
            self.assertTrue((linked / "pod").is_symlink())

    def test_a_lock_entry_marks_the_global_copy_as_externally_managed(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude"),
                                                        "XDG_STATE_HOME": str(root / "state")}):
            project = self.install_global(root)
            lock = root / "state" / "skills" / ".skill-lock.json"
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(json.dumps({"version": 3, "skills": {"pod": {
                "source": "j3w1/pod", "ref": "v0.1.0", "installedAt": "2026-09-21T00:00:00Z"}}}))
            report = inspect(project, global_scope=True)
            self.assertEqual(report["codex"]["status"], "managed_by_skills_cli")
            outcome = setup(project, global_scope=True)
            self.assertEqual(outcome["skills"]["codex"], "managed_by_skills_cli")
            self.assertEqual(outcome["ownership"]["codex"], "skills_cli")

    def older_bundle(self):
        """The bytes a previous release would have placed."""
        source = dict(canonical())
        source["references/routing.md"] = b"# Routing and quota\n\nAn older release.\n"
        return source

    def test_a_version_change_upgrades_only_an_untouched_owned_copy(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            with patch("pod.setup.canonical", return_value=self.older_bundle()):
                self.assertEqual(setup(project)["skills"]["codex"], "installed")
            manifest_path = project / ".pod" / "skills.json"
            self.assertEqual(json.loads(manifest_path.read_text())["version"], version())
            placed = project / ".agents" / "skills" / "pod" / "references" / "routing.md"
            self.assertEqual(placed.read_bytes(), self.older_bundle()["references/routing.md"])
            upgraded = setup(project)
            self.assertEqual(upgraded["skills"]["codex"], "upgraded")
            self.assertEqual(placed.read_bytes(), canonical()["references/routing.md"])
            self.assertIn("codex", json.loads(manifest_path.read_text())["owned_hosts"])
            self.assertEqual(setup(project)["skills"]["codex"], "reused")

    def test_an_edited_copy_is_never_replaced_by_an_upgrade(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            with patch("pod.setup.canonical", return_value=self.older_bundle()):
                setup(project)
            edited = project / ".agents" / "skills" / "pod" / "references" / "routing.md"
            edited.write_text("# my own notes\n")
            self.assertEqual(setup(project)["skills"]["codex"], "preserved_modified")
            self.assertEqual(edited.read_text(), "# my own notes\n")

    def test_a_differing_version_is_preserved_with_its_own_next_step(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = self.install_global(root)
            skill = root / "agents" / "skills" / "pod"
            text = (skill / "SKILL.md").read_text(encoding="utf-8")
            (skill / "SKILL.md").write_text(text.replace('version: "', 'version: "9.'),
                                            encoding="utf-8")
            report = inspect(project, global_scope=True)
            self.assertEqual(report["codex"]["status"], "other_version")
            self.assertTrue(report["codex"]["version"].startswith("9."))
            outcome = setup(project, global_scope=True)
            self.assertEqual(outcome["skills"]["codex"], "preserved_other_version")
            self.assertTrue((skill / "SKILL.md").read_text(encoding="utf-8").count('version: "9.'))

    def test_an_installed_copy_carries_and_runs_its_own_helpers(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = self.install_global(root)
            skill = root / "agents" / "skills" / "pod"
            for name in BUNDLE_FILES:
                self.assertTrue((skill / name).is_file(), name)
            helped = subprocess.run([sys.executable, str(skill / "scripts" / "pod.py"), "--help"],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(helped.returncode, 0, helped.stderr)
            self.assertIn("doctor", helped.stdout)

    def test_interpreter_caches_in_a_placed_copy_are_not_a_modification(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            setup(project)
            cache = project / ".agents" / "skills" / "pod" / "__pycache__"
            cache.mkdir()
            (cache / "cli.cpython-313.pyc").write_bytes(b"cached")
            self.assertEqual(setup(project)["skills"]["codex"], "reused")
            self.assertEqual(inspect(project)["codex"]["status"], "current")

    def test_a_symlinked_native_home_root_is_accepted(self):
        """Ordinary machines redirect ~/.claude; only paths Pod creates must be plain."""
        with fixture() as root:
            real = root / "claude-real"
            real.mkdir()
            linked = root / "claude-link"
            linked.symlink_to(real, target_is_directory=True)
            project = root / "project"
            project.mkdir()
            with patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                         "CLAUDE_CONFIG_DIR": str(linked)}):
                outcome = setup(project, global_scope=True)
            self.assertEqual(outcome["skills"]["claude"], "installed")
            self.assertTrue((real / "skills" / "pod" / "SKILL.md").is_file())

    def test_doctor_reports_the_bundle_prerequisites_and_manager(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude"),
                                                        "XDG_STATE_HOME": str(root / "state")}):
            project = self.install_global(root)
            with patch("pod.cli.contract", return_value={"status": "unavailable",
                                                         "reason": "orca_unavailable"}), \
                 patch("pod.cli.account_metadata", side_effect=PodError("unavailable", "offline")), \
                 patch("pod.cli.executable", side_effect=PodError("orca_unavailable", "absent")):
                report = execute(parser().parse_args(["doctor"]), project)
            self.assertEqual(report["bundle"]["version"], version())
            self.assertEqual(report["prerequisites"]["python"], "ok")
            self.assertIn("Pod never installs it", report["prerequisites"]["orca"])
            self.assertEqual(report["routes"], {"status": "unavailable", "reason": "orca_unavailable"})
            self.assertIsNone(report["skills_cli"]["entry"])
            self.assertEqual(report["global_skills"]["codex"]["manager"], "unowned")
            self.assertEqual(report["global_skills"]["codex"]["status"], "current")
