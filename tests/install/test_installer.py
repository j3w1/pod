"""The real shell entry point and launcher, run in scrubbed homes with a stand-in for npx."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from pod.installer import BANNER, bundle_digest, bundle_files
from tests.install.install_support import (BUNDLE, ROOT, VERSION, archive_tree, doctor, ignored_sigint, local_server,
                                           restore_child_signals, run_install, run_install_pty, run_pod, sandbox)

SKILL = "skills/pod/SKILL.md"
REGISTRY = "skills/pod/models/registry.json"
EXISTING = (b"# mine\nschema: pod/v2\nroutes:\n  codex/gpt-6.1-sol/high: enabled\n  claude/claude-opus-5-5/max: disabled\n"
            b"preferred: codex/gpt-6.1-sol/high\npinned: null\nworkers:\n  max_active: 6\nrefresh: automatic\n"
            b"authorization_verifier:\n- /usr/local/bin/verifier\ndecision_policy:\n  preset: hands_off\n"
            b"  categories:\n    merge_publish: any_agent\n")


class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env, self.archive = sandbox(self.root)
        self.home = Path(self.env["HOME"])
        self.canonical = self.home / ".agents/skills/pod"
        self.launcher = self.home / ".local/bin/pod"
        self.config = self.home / "config/pod/config.yaml"
        self.receipt = self.home / "data/pod/install.json"

    def installed(self, env=None):
        result = run_install(env or self.env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("\x1b", result.stdout + result.stderr)
        return result

    def newer(self, name="newer.tar.gz", **changes):
        """An archive whose skill text differs by one trailing line, so its digest differs."""
        changed = (BUNDLE / "SKILL.md").read_bytes() + b"\n"
        return archive_tree(self.root / name, changes={SKILL: changed, **changes}), changed


class TerminalOutputTests(InstallerCase):
    @staticmethod
    def screen(raw: bytes) -> str:
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw.decode("utf-8", "replace")).replace("\r", "")

    def colour(self, **extra):
        env = {**self.env, "TERM": "xterm-256color", **extra}
        env.pop("NO_COLOR")
        return env

    def test_banner_is_small_ascii(self):
        self.assertEqual(len(BANNER), 6)
        self.assertTrue(all(len(line) <= 64 and all(32 <= ord(char) <= 126 for char in line) for line in BANNER))

    def test_terminal_shows_art_one_line_per_stage_and_a_compact_summary(self):
        code, raw = run_install_pty(self.colour())
        self.assertEqual(code, 0, raw.decode("utf-8", "replace"))
        text = self.screen(raw)
        lines = text.splitlines()
        self.assertGreaterEqual(len(re.findall(r"#+\s+\(o\)", "\n".join(lines[:6]))), 3)
        self.assertIn(VERSION, lines[6])
        self.assertIn(b"38;5;255", raw)
        stage_lines = [line for line in lines if re.match(r"^\s+[✓!✗]\s+", line)]
        for stage in ("Checking prerequisites", "Installing skills", "Installing pod command", "Preferences", "PATH"):
            self.assertEqual(sum(stage in line for line in stage_lines), 1, stage)
        for label in ("Codex skill", "Claude Code skill", "pod command", "Environment", "Next", "~/.agents/skills/pod",
                      "~/.local/bin/pod", "Existing agent sessions and workers unchanged"):
            self.assertIn(label, text)
        self.assertNotIn("pod-install:", text)

    def test_ascii_locale_and_no_color_stay_plain(self):
        code, raw = run_install_pty({**self.env, "LC_ALL": "C", "TERM": "xterm-256color", "NO_COLOR": "1"})
        self.assertEqual(code, 0)
        text = self.screen(raw)
        self.assertTrue(text.isascii())
        self.assertNotIn(b"\x1b[", raw)
        self.assertIn("[ok]", text)
        self.assertIn("[!]", text)

    def test_a_refusal_names_state_next_action_and_exit_code(self):
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_bytes(b"foreign command\n")
        code, raw = run_install_pty(self.colour())
        self.assertEqual(code, 1)
        text = self.screen(raw)
        for phrase in ("Install stopped", "Refuse unrelated pod command", "~/.local/bin/pod", "State",
                       "previous skill and launcher", "Next", "Exit code 1"):
            self.assertIn(phrase, text)
        self.assertEqual(self.launcher.read_bytes(), b"foreign command\n")
        self.assertFalse(self.receipt.exists())

    def test_a_bootstrap_refusal_uses_the_same_layout(self):
        uname = self.root / "bin/uname"
        uname.write_text("#!/bin/sh\necho Darwin\n")
        uname.chmod(0o755)
        code, raw = run_install_pty(self.colour())
        self.assertEqual(code, 1)
        for phrase in ("Install stopped", "Linux is required", "State", "Next", "Exit code 1"):
            self.assertIn(phrase, self.screen(raw))
        self.assertFalse(self.receipt.exists())

    def test_unreadable_preferences_and_a_ready_path_are_explicit(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_bytes(b"schema: [\n")
        env = {**self.env, "PATH": str(self.launcher.parent) + ":" + self.env["PATH"], "TERM": "xterm-256color"}
        code, raw = run_install_pty(env)
        self.assertEqual(code, 0)
        text = self.screen(raw)
        self.assertIn("Preferences  needs attention", text)
        self.assertIn("PATH  ready", text)
        self.assertIn("pod config edit", text)
        self.assertEqual(self.config.read_bytes(), b"schema: [\n")

    def test_a_partial_copy_reports_exit_three_and_recovery(self):
        self.installed()
        newer, _ = self.newer()
        code, raw = run_install_pty(self.colour(POD_INSTALL_SOURCE=newer.as_uri(), POD_TEST_NPX_MODE="partial_fail"))
        self.assertEqual(code, 3)
        for phrase in ("Install stopped", "Skill copy may be incomplete", "receipt remains installing", "Exit code 3"):
            self.assertIn(phrase, self.screen(raw))


class PlacementTests(InstallerCase):
    def test_fresh_install_places_one_bundle_and_leaves_everything_else_alone(self):
        codex, claude = self.home / "codex/config.toml", self.home / "claude/settings.json"
        state = self.home / "state/pod/objectives/record.jsonl"
        for path, data in ((codex, b"agent setting\n"), (claude, b'{"private":true}\n'), (state, b'{"kept":1}\n')):
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
        first = self.installed()
        self.assertIn("pod-install: Preflight ready", first.stdout)
        self.assertEqual((codex.read_bytes(), claude.read_bytes(), state.read_bytes()),
                         (b"agent setting\n", b'{"private":true}\n', b'{"kept":1}\n'))
        self.assertEqual(sorted(path.name for path in (self.home / "state/pod").rglob("*")), ["objectives", "record.jsonl"])
        self.assertEqual(bundle_files(self.canonical), bundle_files(BUNDLE))
        self.assertFalse((self.canonical / "VERSION").is_symlink())
        self.assertEqual((self.home / "claude/skills/pod").resolve(), self.canonical.resolve())
        self.assertFalse((self.home / "codex/skills/pod").exists())
        self.assertEqual(self.launcher.stat().st_mode & 0o777, 0o755)
        self.assertEqual(run_pod(self.env, "--version").stdout.strip(), VERSION)
        record = json.loads(self.receipt.read_text())
        self.assertEqual((record["status"], record["target"]),
                         ("installed", {"version": VERSION, "digest": bundle_digest(self.canonical)}))
        self.assertEqual(record["launcher_digest"], hashlib.sha256(self.launcher.read_bytes()).hexdigest())
        report = doctor(self.env)
        self.assertTrue(report["installation"]["healthy"])
        self.assertEqual((report["preferences"]["status"], report["version"]), ("ok", VERSION))
        npx = json.loads((self.root / "npx.json").read_text())
        self.assertEqual(npx[:3] + npx[4:], ["--yes", "skills@1.7.0", "add", "--skill", "pod", "-a", "codex",
                                             "-a", "claude-code", "-g", "-y"])
        before = self.receipt.read_bytes()
        self.assertIn("Already current", self.installed().stdout)
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_a_failed_download_or_damaged_archive_changes_nothing(self):
        with local_server(self.root) as base:
            failed = run_install({**self.env, "POD_INSTALL_SOURCE": base + "/missing.tar.gz"})
            self.assertEqual(failed.returncode, 1)
            self.assertFalse(self.receipt.exists() or self.launcher.exists())
            self.installed({**self.env, "POD_INSTALL_SOURCE": base + "/source.tar.gz"})
        damaged = self.root / "damaged.tar.gz"
        damaged.write_bytes(self.archive.read_bytes()[:80])
        after = self.receipt.read_bytes()
        for source in (damaged.as_uri(), "ftp://example.invalid/pod.tar.gz"):
            self.assertNotEqual(run_install({**self.env, "POD_INSTALL_SOURCE": source}).returncode, 0)
        self.assertEqual(self.receipt.read_bytes(), after)
        self.assertEqual(run_pod(self.env, "--version").stdout.strip(), VERSION)

    def test_a_source_that_fails_validation_is_never_placed(self):
        broken = archive_tree(self.root / "broken.tar.gz", changes={"skills/pod/errors.py": b"def broken(:\n"})
        failed = run_install({**self.env, "POD_INSTALL_SOURCE": broken.as_uri()})
        self.assertEqual(failed.returncode, 1)
        self.assertIn("Staged bundle validation failed", failed.stderr)
        self.assertFalse(self.receipt.exists() or self.canonical.exists() or self.launcher.exists())

    def test_a_placed_copy_that_differs_from_the_validated_source_is_not_accepted(self):
        failed = run_install({**self.env, "POD_TEST_NPX_MODE": "tamper"})
        self.assertEqual(failed.returncode, 3, failed.stdout + failed.stderr)
        self.assertIn("Placed skill differs from validated source", failed.stderr)
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "installing")
        self.assertFalse(self.launcher.exists())
        self.installed()

    def test_a_foreign_launcher_is_refused_before_any_owned_change(self):
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_bytes(b"foreign command\n")
        failed = run_install(self.env)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("unrelated pod command", failed.stderr)
        self.assertEqual(self.launcher.read_bytes(), b"foreign command\n")
        self.assertFalse(self.receipt.exists() or self.config.exists() or self.canonical.exists())

    def test_foreign_skill_paths_and_redirected_directories_are_refused(self):
        for prepare, message in (
                (lambda: (self.canonical.parent.mkdir(parents=True), self.canonical.symlink_to(self.root)), "foreign canonical"),
                (lambda: (self.home / "claude/skills/pod").mkdir(parents=True), "foreign Claude skill"),
                (lambda: (self.root / "elsewhere").mkdir() or (self.home / "data").mkdir()
                 or (self.home / "data/pod").symlink_to(self.root / "elsewhere"), "redirected directory")):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                self.env, _ = sandbox(self.root)
                self.home = Path(self.env["HOME"])
                self.canonical = self.home / ".agents/skills/pod"
                prepare()
                failed = run_install(self.env)
                self.assertEqual(failed.returncode, 1)
                self.assertIn(message, failed.stderr)
                self.assertFalse((self.home / ".local/bin/pod").exists())


class RedirectedPathTests(InstallerCase):
    def untouched(self, home=None):
        home = home or self.home
        self.assertFalse((home / "data/pod").exists() or (home / ".agents").exists()
                         or (home / "config/pod").exists() or (home / ".bashrc").exists())

    def test_every_redirected_destination_is_refused_before_anything_changes_and_says_what_to_do(self):
        def claude(home, env):
            (home / ".claude").symlink_to(home / "claude", target_is_directory=True)
            env.pop("CLAUDE_CONFIG_DIR")
            return home / ".claude", "Set CLAUDE_CONFIG_DIR=" + str(home / "claude")

        def launcher_directory(home, env):
            (home / ".local").mkdir()
            (home / ".local/bin").symlink_to(home / "tmp", target_is_directory=True)
            return home / ".local/bin", "Replace the link with a real directory"

        def linked_home(home, env):
            link = home.parent / "home-link"
            link.symlink_to(home, target_is_directory=True)
            env["HOME"] = str(link)
            return link, "Set HOME=" + str(home)

        def agents_with_codex_home_set(home, env):
            (home / ".agents").symlink_to(home / "tmp/elsewhere", target_is_directory=True)   # nothing may be made there
            env["CODEX_HOME"] = str(home / "codex")           # naming another Codex home never moves the canonical copy
            return home / ".agents", "Replace the link with a real directory"

        for prepare in (claude, launcher_directory, linked_home, agents_with_codex_home_set):
            with self.subTest(prepare=prepare.__name__), tempfile.TemporaryDirectory() as directory:
                env, _ = sandbox(Path(directory))
                home = Path(env["HOME"])
                (home / "claude").mkdir()
                path, remedy = prepare(home, env)
                refused = run_install(env)
                self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                self.assertIn(f"Refuse redirected directory: {path} is a symbolic link", refused.stderr)
                self.assertIn(remedy + ", then rerun the installer", refused.stderr)
                self.untouched(home)
                self.assertEqual(list((home / "tmp").iterdir()), [])
                self.assertEqual(run_install(env).returncode, 1, "a rerun is refused the same way")
                self.untouched(home)

    def test_a_launcher_directory_redirected_after_an_install_leaves_that_install_as_it_was(self):
        self.installed()
        before = (self.receipt.read_bytes(), bundle_digest(self.canonical), self.launcher.read_bytes())
        self.launcher.parent.rename(self.home / ".local/real-bin")
        self.launcher.parent.symlink_to(self.home / ".local/real-bin", target_is_directory=True)
        newer, _ = self.newer()
        refused = run_install({**self.env, "POD_INSTALL_SOURCE": newer.as_uri()})
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("Refuse redirected directory", refused.stderr)
        self.assertEqual((self.receipt.read_bytes(), bundle_digest(self.canonical), self.launcher.read_bytes()), before)
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "installed")

    def test_following_the_advice_installs_and_a_redirected_claude_directory_never_blocks_a_command(self):
        (self.home / "claude").mkdir()
        (self.home / ".claude").symlink_to(self.home / "claude", target_is_directory=True)
        self.installed()        # the sandbox names the real directory with CLAUDE_CONFIG_DIR, as the refusal advises
        env = {name: value for name, value in self.env.items() if name != "CLAUDE_CONFIG_DIR"}
        self.assertEqual(run_pod(env, "--version").stdout.strip(), VERSION)
        status = run_pod(env, "status", "--all", "--json")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertNotIn("install_incomplete", status.stdout + status.stderr)
        report = run_pod(env, "doctor", "--json")
        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
        self.assertTrue(json.loads(report.stdout)["installation"]["healthy"])
        self.assertIn("Set CLAUDE_CONFIG_DIR=", run_install(env).stderr)

    def test_a_receipt_that_cannot_be_read_is_reported_by_doctor_and_blocks_nothing(self):
        self.installed()
        self.receipt.write_text("{not json")
        self.assertEqual(run_pod(self.env, "--version").stdout.strip(), VERSION)
        report = run_pod(self.env, "doctor", "--json")
        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
        self.assertEqual(json.loads(report.stdout)["installation"]["status"], "needs attention")


class PreferenceTests(InstallerCase):
    def test_an_existing_file_is_byte_identical_after_install_and_update(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_bytes(EXISTING)
        mode = self.config.stat().st_mode
        self.assertIn("(kept)", self.installed().stdout)
        newer, _ = self.newer()
        updated = run_pod({**self.env, "POD_INSTALL_SOURCE": newer.as_uri()}, "update")
        self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_mode), (EXISTING, mode))
        report = doctor(self.env)["preferences"]
        self.assertEqual((report["status"], report["enabled"], report["preferred"]), ("ok", 1, "codex/gpt-6.1-sol/high"))

    def test_a_first_install_that_stopped_part_way_still_creates_the_file_when_it_is_run_again(self):
        from pod import installer
        self.assertTrue(installer._first_install(None))
        self.assertTrue(installer._first_install({"status": "installing", "previous": None}))
        self.assertFalse(installer._first_install({"status": "installing", "previous": {"version": "0.21.54"}}))
        self.assertFalse(installer._first_install({"status": "installed", "previous": None}))
        failed = run_install({**self.env, "POD_TEST_NPX_MODE": "fail"})           # the first attempt stops after its receipt
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.config.exists())
        again = run_install(self.env)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertTrue(self.config.exists())
        self.assertGreater(doctor(self.env)["preferences"]["enabled"], 0)

    def test_an_update_never_creates_a_preference_file_the_person_removed(self):
        self.assertIn("(created)", self.installed().stdout)
        self.config.unlink()
        newer, _ = self.newer()
        for env in (self.env, {**self.env, "POD_INSTALL_SOURCE": newer.as_uri()}):      # already current, then a newer bundle
            updated = run_pod(env, "update")
            self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
            self.assertFalse(self.config.exists(), updated.stdout)
        report = doctor(self.env)["preferences"]
        self.assertEqual((report["status"], report["enabled"]), ("absent", 0))

    def test_an_unreadable_file_and_a_linked_file_are_kept_and_reported(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_bytes(b"schema: pod/v1\nmodel_approvals: {}\n")
        result = self.installed()
        self.assertIn("Preferences need attention", result.stdout)
        self.assertIn("pod config edit", result.stdout)
        self.assertEqual(self.config.read_bytes(), b"schema: pod/v1\nmodel_approvals: {}\n")
        self.config.unlink()
        target = self.root / "elsewhere.yaml"
        self.config.symlink_to(target)
        self.assertIn("Preferences need attention", self.installed().stdout)
        self.assertTrue(self.config.is_symlink())
        self.assertFalse(target.exists())

    def test_a_first_file_enables_registry_routes_but_never_ultra(self):
        registry = {"schema": "fixture", "models": [
            {"id": "model-a", "agent": "codex", "efforts": ["low", "high", "ultra"]},
            {"id": "model-b", "agent": "claude", "efforts": ["medium"]}]}
        source = archive_tree(self.root / "registry.tar.gz", changes={REGISTRY: json.dumps(registry).encode()})
        self.assertIn("(created)", self.installed({**self.env, "POD_INSTALL_SOURCE": source.as_uri()}).stdout)
        self.assertEqual(self.config.read_text(), "schema: pod/v2\nroutes:\n  codex/model-a/low: enabled\n"
                         "  codex/model-a/high: enabled\n  claude/model-b/medium: enabled\npreferred: null\n"
                         "pinned: null\nmerge_authorization: person\n")
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_without_a_readable_registry_the_first_file_enables_nothing_and_says_so(self):
        source = archive_tree(self.root / "bare.tar.gz", changes={REGISTRY: b"not json"})
        result = self.installed({**self.env, "POD_INSTALL_SOURCE": source.as_uri()})
        self.assertIn("no route is enabled; choose routes with pod models", result.stdout)
        self.assertEqual(self.config.read_text(), "schema: pod/v2\nroutes: {}\npreferred: null\npinned: null\n"
                                                  "merge_authorization: person\n")
        self.assertEqual(doctor(self.env)["preferences"]["enabled"], 0)


class PathTests(InstallerCase):
    def test_guarded_block_real_shell_resolution_and_untouched_linked_files(self):
        self.installed()
        for file in (self.home / ".bashrc", self.home / ".profile"):
            self.assertEqual(file.read_text().count("# >>> pod path >>>"), 1)
        # Some hosts set HOME in /etc/profile; there a login-equivalent shell reads only the sandbox profile.
        profiles = [Path("/etc/profile"), *Path("/etc/profile.d").glob("*.sh")]
        redirects = any(re.search(r"(?m)^\s*(?:export\s+)?(?:HOME|XDG_[A-Z_]+)\s*=", path.read_text(errors="replace"))
                        for path in profiles if path.is_file())
        shells = [(["bash", "-ic", "command -v pod"], None)]
        shells += ([(["bash", "--noprofile", "-lc", "command -v pod"], self.home / ".profile"),
                    (["sh", "-c", '. "$HOME/.profile"; command -v pod'], None)] if redirects else
                   [(["bash", "-lc", "command -v pod"], None), (["sh", "-lc", "command -v pod"], None)])
        for command, bash_env in shells:
            env = {**self.env, **({"BASH_ENV": str(bash_env)} if bash_env else {})}
            result = subprocess.run(command, env=env, cwd=self.root / "work", capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, (command, result.stdout, result.stderr))
            self.assertIn(str(self.launcher), result.stdout)
        with tempfile.TemporaryDirectory() as directory:
            env, _ = sandbox(Path(directory))
            env["SHELL"] = "/usr/bin/zsh"
            self.installed(env)
            resolved = subprocess.run(["zsh", "-ic", "command -v pod"], env=env, capture_output=True, text=True, timeout=10)
            self.assertIn(str(Path(env["HOME"]) / ".local/bin/pod"), resolved.stdout, resolved.stderr)
        with tempfile.TemporaryDirectory() as directory:
            env, _ = sandbox(Path(directory))
            target = Path(directory) / "foreign-rc"
            target.write_bytes(b"my rc\n")
            (Path(env["HOME"]) / ".bashrc").symlink_to(target)
            result = self.installed(env)
            self.assertIn("symlink and was not edited", result.stdout)
            self.assertIn("manual PATH step required", result.stdout)
            self.assertEqual(target.read_bytes(), b"my rc\n")

    def test_a_ready_path_edits_nothing_and_a_later_run_repairs_a_missing_block_once(self):
        ready = {**self.env, "PATH": str(self.launcher.parent) + ":" + self.env["PATH"]}
        self.installed(ready)
        self.assertFalse((self.home / ".bashrc").exists() or (self.home / ".profile").exists())
        receipt = self.receipt.read_bytes()
        repaired = self.installed()
        self.assertIn("Already current", repaired.stdout)
        self.assertIn("PATH: open a new shell", repaired.stdout)
        self.installed()
        for file in (self.home / ".bashrc", self.home / ".profile"):
            self.assertEqual(file.read_text().count("# >>> pod path >>>"), 1)
        self.assertEqual(self.receipt.read_bytes(), receipt)

    def test_a_shadowing_command_a_duplicate_and_a_local_edit_are_reported_not_changed(self):
        foreign = self.root / "bin/pod"
        foreign.write_text("#!/bin/sh\nexit 0\n")
        foreign.chmod(0o755)
        duplicate = self.home / "codex/skills/pod"
        duplicate.mkdir(parents=True)
        (duplicate / "SKILL.md").write_text("foreign")
        result = self.installed()
        self.assertIn("shadows this launcher", result.stdout)
        self.assertIn("Duplicate skill", result.stdout)
        self.assertEqual((duplicate / "SKILL.md").read_text(), "foreign")
        report = doctor(self.env)["installation"]
        self.assertTrue(report["launcher"]["shadowed"])
        self.assertEqual((report["duplicates"], report["healthy"]), ([str(duplicate)], True))
        (self.canonical / "SKILL.md").write_text((self.canonical / "SKILL.md").read_text() + "\n# local edit\n")
        report = doctor(self.env)["installation"]
        self.assertEqual((report["digest_matches"], report["healthy"], report["receipt_status"]), (False, False, "installed"))
        self.assertIn("one-shot installer", report["next"])


class UpdateAndRecoveryTests(InstallerCase):
    def test_update_replaces_the_bundle_preserves_a_local_edit_and_is_then_current(self):
        self.installed()
        prior = json.loads(self.receipt.read_text())["target"]["digest"]
        newer, changed = self.newer()
        env = {**self.env, "POD_INSTALL_SOURCE": newer.as_uri()}
        result = run_pod(env, "update")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Reload active coordinators", result.stdout)
        self.assertNotEqual(json.loads(self.receipt.read_text())["target"]["digest"], prior)
        self.assertEqual((self.canonical / "SKILL.md").read_bytes(), changed)
        self.assertIn("Already current", run_pod(env, "update").stdout)
        (self.canonical / "SKILL.md").write_bytes(changed + b"\n# my edit\n")
        result = run_pod(env, "update")
        self.assertIn("Preserved changed skill", result.stdout)
        preserved = list((self.home / "data/pod/preserved").glob("skill-*/SKILL.md"))
        self.assertTrue(any(b"my edit" in path.read_bytes() for path in preserved))
        self.assertEqual((self.canonical / "SKILL.md").read_bytes(), changed)

    def test_update_repairs_a_bundle_whose_package_cannot_start(self):
        self.installed()
        (self.canonical / "errors.py").unlink()
        blocked = run_pod(self.env, "doctor", "--json")
        self.assertEqual(blocked.returncode, 2)
        self.assertEqual(json.loads(blocked.stdout)["error"]["code"], "bundle_incomplete")
        self.assertNotIn("Traceback", blocked.stderr)
        repaired = run_pod(self.env, "update")
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        self.assertTrue(doctor(self.env)["installation"]["healthy"])

    def test_an_earlier_installation_with_its_own_environment_is_replaced_cleanly(self):
        data = self.home / "data/pod"
        environment, unrelated = data / "venv-0123456789ab", data / "venv-of-mine"
        for path in (environment / "bin", unrelated):
            path.mkdir(parents=True)
        (environment / ".pod-venv").write_text("{}")
        (unrelated / "keep").write_text("mine")
        earlier = b"#!/bin/sh\n# pod-launcher/v1\nexec " + str(environment).encode() + b"/bin/python -I script \"$@\"\n"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_bytes(earlier)
        self.receipt.write_text(json.dumps({
            "schema": "pod-install/v1", "status": "installed", "previous": None,
            "target": {"version": "0.21.54", "digest": "a" * 64}, "launcher": str(self.launcher),
            "launcher_digest": hashlib.sha256(earlier).hexdigest(), "updated_at": "2026-01-01T00:00:00+00:00"}))
        self.config.parent.mkdir(parents=True)
        self.config.write_bytes(EXISTING)
        self.installed()
        self.assertNotEqual(self.launcher.read_bytes(), earlier)
        self.assertNotIn(b"venv", self.launcher.read_bytes())
        self.assertFalse(environment.exists())
        self.assertEqual((unrelated / "keep").read_text(), "mine")
        self.assertEqual(self.config.read_bytes(), EXISTING)
        self.assertEqual(run_pod(self.env, "--version").stdout.strip(), VERSION)
        self.assertTrue(doctor(self.env)["installation"]["healthy"])

    def test_a_killed_copy_blocks_the_launcher_until_a_rerun_recovers_it(self):
        self.installed()
        newer, changed = self.newer()
        env = {**self.env, "POD_INSTALL_SOURCE": newer.as_uri(), "POD_TEST_NPX_MODE": "half_copy"}
        process = subprocess.Popen([str(self.launcher), "update"], env=env, cwd=self.root / "work",
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and (self.canonical / "scripts/pod.py").exists():
                time.sleep(.05)
            self.assertFalse((self.canonical / "scripts/pod.py").exists())
        finally:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "installing")
        incomplete = run_pod(self.env, "doctor", "--json")
        self.assertEqual(incomplete.returncode, 2)
        self.assertIn("installation incomplete", incomplete.stderr)
        self.installed({**self.env, "POD_INSTALL_SOURCE": newer.as_uri()})
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "installed")
        self.assertEqual((self.canonical / "SKILL.md").read_bytes(), changed)

    def test_a_failed_copy_returns_three_and_the_gate_answers_in_json(self):
        self.installed()
        newer, changed = self.newer()
        failed = run_install({**self.env, "POD_INSTALL_SOURCE": newer.as_uri(), "POD_TEST_NPX_MODE": "partial_fail"})
        self.assertEqual(failed.returncode, 3, failed.stdout + failed.stderr)
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "installing")
        self.installed({**self.env, "POD_INSTALL_SOURCE": newer.as_uri()})
        receipt = json.loads(self.receipt.read_text())
        self.receipt.write_text(json.dumps({**receipt, "status": "installing"}))
        (self.canonical / "SKILL.md").write_bytes(changed + b"\n# neither previous nor target\n")
        gate = run_pod(self.env, "doctor", "--json")
        self.assertEqual(gate.returncode, 2)
        self.assertEqual(json.loads(gate.stdout), {"status": "blocked", "error": json.loads(gate.stdout)["error"]})
        self.assertEqual(json.loads(gate.stdout)["error"]["code"], "install_incomplete")
        untouched = run_install({**self.env, "POD_TEST_NPX_MODE": "fail"})
        self.assertEqual(untouched.returncode, 1, "a copy that never began leaves the placed bundle as it was")

    def test_a_copy_without_its_installer_is_incomplete_and_answers_cleanly(self):
        self.installed()
        self.receipt.write_text(json.dumps({**json.loads(self.receipt.read_text()), "status": "installing"}))
        (self.canonical / "installer.py").unlink()
        for arguments in (("doctor", "--json"), ("status", "--all", "--json"), ("--version",), ("models", "--plain")):
            blocked = run_pod(self.env, *arguments)
            self.assertEqual(blocked.returncode, 2, (arguments, blocked.stdout, blocked.stderr))
            self.assertNotIn("Traceback", blocked.stderr)
            shown = blocked.stdout if "--json" in arguments else blocked.stderr
            self.assertIn("incomplete (missing installer.py)", shown)
            self.assertIn("/main/install.sh", shown)
            if "--json" in arguments:
                self.assertEqual(json.loads(blocked.stdout)["error"]["code"], "bundle_incomplete")
        self.installed()
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "installed")
        self.assertEqual(run_pod(self.env, "--version").stdout.strip(), VERSION)

    def test_only_the_recorded_target_may_replace_an_interrupted_copy(self):
        self.installed()

        def interrupt(contents: dict[str, bytes]) -> None:
            self.receipt.write_text(json.dumps({**json.loads(self.receipt.read_text()), "status": "installing"}))
            shutil.rmtree(self.canonical)
            self.canonical.mkdir()
            for name, data in contents.items():
                (self.canonical / name).parent.mkdir(parents=True, exist_ok=True)
                (self.canonical / name).write_bytes(data)
        for contents in ({}, {"references/pes-template.md": b"partial copy"}):
            interrupt(contents)
            self.installed()
            self.assertEqual(json.loads(self.receipt.read_text())["status"], "installed")
        interrupt({"foreign.txt": b"unrelated contents"})
        refused = run_install(self.env)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("foreign canonical skill path", refused.stderr)
        self.assertEqual((self.canonical / "foreign.txt").read_bytes(), b"unrelated contents")
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "installing")

    def test_a_second_installer_is_refused_and_an_interrupt_claims_nothing(self):
        started = self.root / "npx-started"
        env = {**self.env, "POD_TEST_NPX_MODE": "sleep", "POD_TEST_NPX_STARTED": str(started)}
        with ignored_sigint():
            first = subprocess.Popen(["sh", str(ROOT / "install.sh")], env=env, cwd=self.root / "work",
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     start_new_session=True, preexec_fn=restore_child_signals)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not started.exists():
                time.sleep(.05)
            self.assertTrue(started.exists())
            second = run_install(self.env)
            self.assertEqual(second.returncode, 1)
            self.assertIn("Another Pod installer", second.stderr)
            os.killpg(first.pid, signal.SIGINT)
            output, error = first.communicate(timeout=8)
            self.assertEqual(first.returncode, 130, output + error)
        finally:
            if first.poll() is None:
                os.killpg(first.pid, signal.SIGKILL)
                first.communicate(timeout=3)
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "installing")
        self.installed()


class BootstrapTests(InstallerCase):
    def uninstalled(self):
        self.assertFalse(self.receipt.exists() or self.launcher.exists() or self.canonical.exists())

    def test_other_systems_root_and_missing_node_are_refused(self):
        for command, body in (("uname", "#!/bin/sh\necho Darwin\n"), ("id", "#!/bin/sh\necho 0\n")):
            path = self.root / "bin" / command
            path.write_text(body)
            path.chmod(0o755)
            self.assertEqual(run_install(self.env).returncode, 1)
            path.unlink()
            self.uninstalled()
        (self.root / "bin/npx").unlink()
        for command in ("sh", "uname", "id"):
            (self.root / "bin" / command).symlink_to(shutil.which(command))
        failed = run_install({**self.env, "PATH": str(self.root / "bin")})
        self.assertEqual(failed.returncode, 1)
        self.assertIn("npx", failed.stderr)
        self.uninstalled()

    def test_only_a_stable_python3_is_used_and_no_named_minor_is_a_fallback(self):
        for command in ("sh", "uname", "id"):
            (self.root / "bin" / command).symlink_to(shutil.which(command))
        python = self.root / "bin/python3"
        python.unlink()
        # A prerelease interpreter fails the bootstrap's own assertion, as this stand-in does.
        python.write_text("#!/bin/sh\nexit 1\n")
        python.chmod(0o755)
        (self.root / "bin/python3.9").symlink_to(shutil.which("python3"))
        failed = run_install({**self.env, "PATH": str(self.root / "bin")})
        self.assertEqual(failed.returncode, 1)
        self.assertIn("stable python3", failed.stderr)
        self.uninstalled()

    def test_the_staged_installer_repeats_the_refusals_for_the_update_path(self):
        """`pod update` reaches the installer without the shell bootstrap, so it checks for itself."""
        from pod import installer
        stage = self.root / "stage"
        (stage / "skills/pod").mkdir(parents=True)
        (stage / "skills/pod/SKILL.md").write_text("text")
        (stage / "skills/pod/VERSION").write_text("1.2.3\n")
        prerelease = type("V", (), {"releaselevel": "candidate"})()
        for target, value, message in (("platform.system", lambda: "Darwin", "non-root user on Linux"),
                                       ("os.geteuid", lambda: 0, "non-root user on Linux"),
                                       ("sys.version_info", prerelease, "stable CPython")):
            errors = StringIO()
            with self.subTest(target=target), patch.dict(os.environ, self.env, clear=True), \
                    patch("pod.installer." + target, value), redirect_stderr(errors), redirect_stdout(StringIO()):
                self.assertEqual(installer.main([str(stage)]), 1)
            self.assertIn(message, errors.getvalue())
            self.uninstalled()

    def test_no_prefix_of_the_piped_script_can_install_or_report_success(self):
        script = (ROOT / "install.sh").read_bytes()
        self.assertEqual((script[:1], script[-1:]), (b"{", b"}"))
        length = len(script)
        prefixes = sorted(set(range(1, min(80, length))) | set(range(80, length, max(1, length // 160)))
                          | set(range(max(1, length - 256), length)))
        for count in prefixes:
            result = subprocess.run(["/bin/sh"], input=script[:count], env=self.env, cwd=self.root / "work",
                                    capture_output=True, timeout=3)
            self.assertNotEqual(result.returncode, 0, count)
        self.uninstalled()
