import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import unittest
from unittest.mock import patch

from pod.cli import execute, parser
from pod.errors import PodError
from pod.setup import _relative_key, canonical, setup
from pod.ledger import checkpoint
from tests.common import fixture


class SetupCliTests(unittest.TestCase):
    def test_nested_windows_skill_paths_use_canonical_manifest_keys(self):
        root = PureWindowsPath(r"C:\project\.agents\skills\pod")
        nested = root / "references" / "planning.md"
        self.assertEqual(_relative_key(nested, root), "references/planning.md")

    def test_local_idempotent_and_modified_skill_preserved(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "codex"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
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
                                                        "APPDATA": str(root / "config"), "EDITOR": f'"{sys.executable}" -c pass'}):
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
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "codex"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude"),
                                                        "XDG_CONFIG_HOME": str(root / "config")}):
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
