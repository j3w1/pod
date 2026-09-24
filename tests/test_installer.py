"""Real shell/launcher installation in scrubbed homes with an offline wheel and npx double."""

from __future__ import annotations

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

from tests.install_support import ROOT, archive_tree, local_server, run_install, run_install_pty, run_pod, sandbox
from pod.installer import BANNER


class InstallerTests(unittest.TestCase):
    def test_banner_is_small_ascii_orca_pod(self):
        self.assertEqual(len(BANNER), 6)
        self.assertGreaterEqual(BANNER[0].count("/\\"), 3)
        self.assertTrue(all(len(line) <= 64 and all(32 <= ord(char) <= 126 for char in line)
                            for line in BANNER))

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

    def installed(self):
        result = run_install(self.env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("\x1b", result.stdout + result.stderr)
        return result

    @staticmethod
    def _plain_screen(raw: bytes) -> str:
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw.decode("utf-8", "replace")).replace("\r", "")

    def test_tty_art_stages_and_compact_summary(self):
        env = {**self.env, "TERM": "xterm-256color"}
        env.pop("NO_COLOR")
        code, raw = run_install_pty(env)
        self.assertEqual(code, 0, raw.decode("utf-8", "replace"))
        text = self._plain_screen(raw)
        lines = text.splitlines()
        art = lines[:6]
        self.assertTrue(all(len(line) <= 64 and all(32 <= ord(char) <= 126 for char in line)
                            for line in art))
        self.assertGreaterEqual(art[0].count("/\\"), 3)
        patches = re.findall(r"(#+)\s+\(o\)", "\n".join(art))
        self.assertGreaterEqual(len(patches), 3)
        self.assertGreaterEqual(len({len(patch) for patch in patches}), 3)
        self.assertIn((ROOT / "VERSION").read_text().strip(), lines[6])
        self.assertIn("\x1b[", raw.decode("utf-8", "replace"))
        self.assertIn(b"38;5;255", raw)
        self.assertIn(b"38;5;30", raw)
        stage_lines = [line for line in lines if re.match(r"^\s+[✓!✗]\s+", line)]
        for stage in ("Checking prerequisites", "Preparing Python environment", "Installing skills",
                      "Installing pod command", "Preferences", "PATH"):
            self.assertEqual(sum(stage in line for line in stage_lines), 1, stage)
        for label in ("Codex skill", "Claude Code skill", "pod command", "Preferences", "PATH",
                      "Environment", "Next", "Optional"):
            self.assertIn(label, text)
        self.assertIn("~/.agents/skills/pod", text)
        self.assertIn("~/.local/bin/pod", text)
        self.assertIn("Existing agent sessions and workers unchanged", text)
        self.assertNotIn("pod-install:", text)

    def test_tty_ascii_and_no_color_stay_plain(self):
        env = {**self.env, "LC_ALL": "C", "TERM": "xterm-256color", "NO_COLOR": "1"}
        code, raw = run_install_pty(env)
        self.assertEqual(code, 0)
        text = self._plain_screen(raw)
        self.assertTrue(text.isascii())
        self.assertNotIn(b"\x1b[", raw)
        self.assertIn("[ok]", text)
        self.assertIn("[!]", text)
        self.assertNotIn("pod-install:", text)

    def test_tty_failure_names_state_next_action_and_code(self):
        foreign = self.launcher
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"foreign command\n")
        env = {**self.env, "TERM": "xterm-256color"}
        env.pop("NO_COLOR")
        code, raw = run_install_pty(env)
        self.assertEqual(code, 1)
        text = self._plain_screen(raw)
        self.assertIn("Install stopped", text)
        self.assertIn("Refuse unrelated pod command", text)
        self.assertIn("~/.local/bin/pod", text)
        self.assertIn("State", text)
        self.assertIn("previous skill and launcher", text)
        self.assertIn("Next", text)
        self.assertIn("Exit code 1", text)
        self.assertNotIn("pod-install:", text)
        self.assertEqual(foreign.read_bytes(), b"foreign command\n")
        self.assertFalse(self.receipt.exists())

    def test_tty_bootstrap_failure_uses_the_same_failure_layout(self):
        uname = self.root / "bin/uname"
        uname.write_text("#!/bin/sh\necho Darwin\n")
        uname.chmod(0o755)
        env = {**self.env, "TERM": "xterm-256color"}
        env.pop("NO_COLOR")
        code, raw = run_install_pty(env)
        self.assertEqual(code, 1)
        text = self._plain_screen(raw)
        for phrase in ("Install stopped", "Linux is required", "State", "Next", "Exit code 1"):
            self.assertIn(phrase, text)
        self.assertNotIn("pod-install:", text)
        self.assertFalse(self.receipt.exists())

    def test_tty_invalid_preferences_and_ready_path_are_explicit(self):
        self.config.parent.mkdir(parents=True)
        invalid = b"schema: [\n"
        self.config.write_bytes(invalid)
        env = {**self.env, "PATH": str(self.launcher.parent) + ":" + self.env["PATH"],
               "TERM": "xterm-256color"}
        code, raw = run_install_pty(env)
        self.assertEqual(code, 0)
        text = self._plain_screen(raw)
        self.assertIn("Preferences  needs attention", text)
        self.assertIn("PATH  ready", text)
        self.assertIn("pod config edit", text)
        self.assertEqual(self.config.read_bytes(), invalid)
        self.assertNotIn("pod-install:", text)

    def test_tty_partial_copy_reports_exit_three_and_recovery(self):
        self.installed()
        changed = (ROOT / "skills/pod/SKILL.md").read_bytes() + b"\n<!-- changed -->\n"
        newer = archive_tree(self.root / "tty-partial.tar.gz", changes={"skills/pod/SKILL.md": changed})
        env = {**self.env, "POD_INSTALL_SOURCE": newer.as_uri(),
               "POD_TEST_NPX_MODE": "partial_fail", "TERM": "xterm-256color"}
        env.pop("NO_COLOR")
        code, raw = run_install_pty(env)
        self.assertEqual(code, 3)
        text = self._plain_screen(raw)
        for phrase in ("Install stopped", "State", "Skill copy may be incomplete",
                       "receipt remains installing", "Next", "Exit code 3"):
            self.assertIn(phrase, text)
        self.assertNotIn("pod-install:", text)

    def test_fresh_rerun_placements_receipt_and_untouched_agent_configs(self):
        codex = self.home / "codex/config.toml"
        claude = self.home / "claude/settings.json"
        codex.parent.mkdir(parents=True); claude.parent.mkdir(parents=True)
        codex.write_bytes(b"agent setting\n"); claude.write_bytes(b'{"private":true}\n')
        first = self.installed()
        self.assertIn("pod-install: Preflight ready", first.stdout)
        self.assertIn("pod-install: Installed Codex skill:", first.stdout)
        from pod.bundle import BUNDLE_FILES
        from pod.catalog import IDS
        from pod.installer import bundle_digest
        from pod.config import load
        self.assertEqual(load(personal=self.config)["eligible"], list(IDS))
        self.assertEqual(self.launcher.stat().st_mode & 0o777, 0o755)
        self.assertEqual(run_pod(self.env, "--version").stdout.strip(), (ROOT / "VERSION").read_text().strip())
        self.assertEqual(self.receipt.is_file(), True)
        record = json.loads(self.receipt.read_text())
        self.assertEqual(record["status"], "installed")
        self.assertEqual(record["target"]["digest"], bundle_digest(self.canonical))
        doctor = json.loads(run_pod(self.env, "doctor", "--json").stdout)
        self.assertTrue(doctor["installation_checks"]["healthy"])
        self.assertTrue(doctor["launcher"]["ownership"]["owned"])
        self.assertTrue(doctor["installation_checks"]["venv"]["ready"])
        self.assertEqual({p.relative_to(self.canonical).as_posix() for p in self.canonical.rglob("*")
                          if p.is_file() and "__pycache__" not in p.parts}, set(BUNDLE_FILES))
        self.assertEqual((self.home / "claude/skills/pod").resolve(), self.canonical.resolve())
        self.assertFalse((self.home / "codex/skills/pod").exists())
        npx_args = json.loads((self.root / "npx.json").read_text())
        self.assertEqual(npx_args[:3], ["--yes", "skills@1.7.0", "add"])
        self.assertEqual(npx_args[4:], ["--skill", "pod", "-a", "codex", "-a", "claude-code", "-g", "-y"])
        self.assertEqual(codex.read_bytes(), b"agent setting\n")
        self.assertEqual(claude.read_bytes(), b'{"private":true}\n')
        before = self.receipt.read_bytes()
        second = self.installed()
        self.assertIn("Already current", second.stdout)
        self.assertEqual(self.receipt.read_bytes(), before)
        self.assertEqual(self.env["POD_INSTALL_SOURCE"], self.archive.as_uri())

    def test_local_http_404_and_truncated_archive_leave_home_uninstalled(self):
        with local_server(self.root) as base:
            failed = run_install({**self.env, "POD_INSTALL_SOURCE": base + "/missing.tar.gz"})
            self.assertEqual(failed.returncode, 1)
            self.assertFalse(self.receipt.exists())
            self.assertFalse(self.launcher.exists())
            good = run_install({**self.env, "POD_INSTALL_SOURCE": base + "/source.tar.gz"})
            self.assertEqual(good.returncode, 0, good.stderr)
        damaged = self.root / "damaged.tar.gz"
        damaged.write_bytes(self.archive.read_bytes()[:80])
        after = self.receipt.read_bytes()
        failed = run_install({**self.env, "POD_INSTALL_SOURCE": damaged.as_uri()})
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(self.receipt.read_bytes(), after)
        self.assertEqual(run_pod(self.env, "--version").stdout.strip(), (ROOT / "VERSION").read_text().strip())

    def test_foreign_launcher_refused_before_owned_changes(self):
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_bytes(b"foreign command\n")
        failed = run_install(self.env)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("unrelated pod command", failed.stderr)
        self.assertEqual(self.launcher.read_bytes(), b"foreign command\n")
        self.assertFalse(self.receipt.exists())
        self.assertFalse(self.config.exists())

    def test_valid_and_old_shape_preferences_are_preserved(self):
        from pod.config import DEFAULT
        import yaml
        self.config.parent.mkdir(parents=True)
        original = b"# mine\n" + yaml.safe_dump(DEFAULT, sort_keys=False).encode()
        self.config.write_bytes(original)
        self.installed()
        self.assertEqual(self.config.read_bytes(), original)
        with tempfile.TemporaryDirectory() as directory:
            env, _ = sandbox(Path(directory))
            path = Path(env["HOME"]) / "config/pod/config.yaml"
            path.parent.mkdir(parents=True)
            incompatible = b"schema: pod/v1\nmodel_approvals: {}\n"
            path.write_bytes(incompatible)
            result = run_install(env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Preferences need attention", result.stdout)
            self.assertEqual(path.read_bytes(), incompatible)
            report = json.loads(run_pod(env, "config", "--json").stdout)
            self.assertEqual(report["eligible"], [])

    def test_path_blocks_symlink_and_real_shell_resolution(self):
        self.installed()
        for file in (self.home / ".bashrc", self.home / ".profile"):
            self.assertEqual(file.read_text().count("# >>> pod path >>>"), 1)
        # Some managed hosts force HOME to their real account in /etc/profile.
        # On those hosts use an isolated login-equivalent so the test cannot
        # read the real profile; hosted Linux without that override runs -lc.
        profile_paths = [Path("/etc/profile"), *Path("/etc/profile.d").glob("*.sh")]
        redirects = any(re.search(r"(?m)^\s*(?:export\s+)?(?:HOME|XDG_[A-Z_]+)\s*=", path.read_text(errors="replace"))
                        for path in profile_paths if path.is_file())
        if redirects:
            shells = ((["bash", "-ic", "command -v pod"], None),
                      (["bash", "--noprofile", "-lc", "command -v pod"], self.home / ".profile"),
                      (["sh", "-c", '. "$HOME/.profile"; command -v pod'], None))
        else:
            shells = ((["bash", "-ic", "command -v pod"], None),
                      (["bash", "-lc", "command -v pod"], None),
                      (["sh", "-lc", "command -v pod"], None))
        for command, bash_env in shells:
            env = {**self.env, **({"BASH_ENV": str(bash_env)} if bash_env else {})}
            result = subprocess.run(command, env=env, cwd=self.root / "work",
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, (command, result.stdout, result.stderr))
            self.assertIn(str(self.launcher), result.stdout)
        with tempfile.TemporaryDirectory() as directory:
            env, _ = sandbox(Path(directory))
            env["SHELL"] = "/usr/bin/zsh"
            result = run_install(env)
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = Path(env["HOME"]) / ".local/bin/pod"
            resolved = subprocess.run(["zsh", "-ic", "command -v pod"], env=env,
                                      capture_output=True, text=True, timeout=10)
            self.assertEqual(resolved.returncode, 0, resolved.stderr)
            self.assertIn(str(expected), resolved.stdout)
        with tempfile.TemporaryDirectory() as directory:
            env, _ = sandbox(Path(directory))
            home = Path(env["HOME"])
            target = Path(directory) / "foreign-rc"
            target.write_bytes(b"my rc\n")
            (home / ".bashrc").symlink_to(target)
            result = run_install(env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("symlink and was not edited", result.stdout)
            self.assertEqual(target.read_bytes(), b"my rc\n")
        with tempfile.TemporaryDirectory() as directory:
            env, _ = sandbox(Path(directory))
            env["PATH"] = str(Path(env["HOME"]) / ".local/bin") + ":" + env["PATH"]
            result = run_install(env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(env["HOME"]) / ".bashrc").exists())

    def test_shadowing_duplicate_and_drift_are_reported(self):
        foreign = self.root / "bin/pod"
        foreign.write_text("#!/bin/sh\nexit 0\n"); foreign.chmod(0o755)
        duplicate = self.home / "codex/skills/pod"
        duplicate.mkdir(parents=True)
        (duplicate / "SKILL.md").write_text("foreign")
        result = self.installed()
        self.assertIn("shadows this launcher", result.stdout)
        self.assertIn("Duplicate skill", result.stdout)
        self.assertEqual((duplicate / "SKILL.md").read_text(), "foreign")
        doctor = json.loads(run_pod(self.env, "doctor", "--json").stdout)
        self.assertTrue(doctor["launcher"]["shadowed"])
        self.assertIn(str(duplicate), doctor["installation_checks"]["duplicates"])
        (self.canonical / "SKILL.md").write_text((self.canonical / "SKILL.md").read_text() + "\n# local edit\n")
        doctor = json.loads(run_pod(self.env, "doctor", "--json").stdout)
        self.assertFalse(doctor["installation_checks"]["digest_matches"])
        self.assertEqual(doctor["installation_checks"]["receipt_status"], "installed")

    def test_update_a_to_b_preserves_edit_and_fast_path(self):
        self.installed()
        prior = json.loads(self.receipt.read_text())["target"]["digest"]
        skill = (ROOT / "skills/pod/SKILL.md").read_bytes()
        changed = skill + b"\n<!-- catalog copy updated -->\n"
        newer = archive_tree(self.root / "newer.tar.gz", changes={"skills/pod/SKILL.md": changed})
        env = {**self.env, "POD_INSTALL_SOURCE": newer.as_uri()}
        result = run_pod(env, "update")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Reload active coordinators", result.stdout)
        current = json.loads(self.receipt.read_text())["target"]["digest"]
        self.assertNotEqual(current, prior)
        self.assertEqual((self.canonical / "SKILL.md").read_bytes(), changed)
        self.assertIn("Already current", run_pod(env, "update").stdout)
        (self.canonical / "SKILL.md").write_bytes(changed + b"\n# my edit\n")
        result = run_pod(env, "update")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Preserved changed skill", result.stdout)
        preserved = list((self.home / "data/pod/preserved").glob("skill-*/SKILL.md"))
        self.assertTrue(any(b"my edit" in path.read_bytes() for path in preserved))
        self.assertEqual((self.canonical / "SKILL.md").read_bytes(), changed)

    def test_missing_wheel_keeps_previous_bundle_and_half_copy_recovers(self):
        self.installed()
        before = self.receipt.read_bytes()
        empty = self.root / "empty-wheelhouse"
        empty.mkdir()
        original_installer = (ROOT / "skills/pod/installer.py").read_bytes()
        changed_pin = original_installer.replace(b'PIN = "PyYAML==6.0.3"', b'PIN = "PyYAML==6.0.2"')
        self.assertNotEqual(changed_pin, original_installer)
        pin_archive = archive_tree(self.root / "new-pin.tar.gz", changes={"skills/pod/installer.py": changed_pin})
        failed = run_pod({**self.env, "POD_INSTALL_SOURCE": pin_archive.as_uri(),
                          "PIP_FIND_LINKS": str(empty)}, "update")
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertIn("PyYAML installation failed", failed.stderr)
        self.assertEqual(self.receipt.read_bytes(), before)
        self.assertEqual(run_pod(self.env, "--version").stdout.strip(), (ROOT / "VERSION").read_text().strip())
        changed = (ROOT / "skills/pod/SKILL.md").read_bytes() + b"\n<!-- changed -->\n"
        newer = archive_tree(self.root / "changed.tar.gz", changes={"skills/pod/SKILL.md": changed})
        env = {**self.env, "POD_INSTALL_SOURCE": newer.as_uri(), "POD_TEST_NPX_MODE": "half_copy"}
        process = subprocess.Popen([str(self.launcher), "update"], env=env, cwd=self.root / "work",
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and (self.canonical / "scripts/pod.py").exists():
                time.sleep(.05)
            self.assertFalse((self.canonical / "scripts/pod.py").exists())
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=3)
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "installing")
        incomplete = run_pod(self.env, "doctor", "--json")
        self.assertEqual(incomplete.returncode, 2)
        self.assertIn("installation incomplete", incomplete.stderr)
        recovered = run_install({**self.env, "POD_INSTALL_SOURCE": newer.as_uri()})
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "installed")
        self.assertEqual((self.canonical / "SKILL.md").read_bytes(), changed)

    def test_concurrent_installer_and_sigint(self):
        env = {**self.env, "POD_TEST_NPX_MODE": "sleep"}
        first = subprocess.Popen(["sh", str(ROOT / "install.sh")], env=env, cwd=self.root / "work",
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and not self.receipt.exists():
                time.sleep(.05)
            self.assertTrue(self.receipt.exists())
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
        self.assertEqual(run_install(self.env).returncode, 0)

    def test_preflight_refusals_half_pipe_and_no_profile_mutation(self):
        short = (ROOT / "install.sh").read_bytes()
        cut = short[:short.index(b"main \"$@\"") // 2]
        result = subprocess.run(["sh"], input=cut, env=self.env, cwd=self.root / "work",
                                capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.receipt.exists())
        for command, body in (("uname", "#!/bin/sh\necho Darwin\n"),
                              ("id", "#!/bin/sh\necho 0\n")):
            path = self.root / "bin" / command
            path.write_text(body); path.chmod(0o755)
            failed = run_install(self.env)
            self.assertEqual(failed.returncode, 1)
            self.assertFalse(self.receipt.exists())
            path.unlink()
        npx = self.root / "bin/npx"
        npx.unlink()
        for command in ("sh", "uname", "id"):
            (self.root / "bin" / command).symlink_to(shutil.which(command))
        failed = run_install({**self.env, "PATH": str(self.root / "bin")})
        self.assertEqual(failed.returncode, 1)
        self.assertFalse(self.receipt.exists())
        for candidate in ("python3", "python3.14", "python3.13"):
            path = self.root / "bin" / candidate
            if path.is_symlink(): path.unlink()
            path.write_text("#!/bin/sh\nexit 1\n")
            path.chmod(0o755)
        failed = run_install({**self.env, "PATH": str(self.root / "bin")})
        self.assertEqual(failed.returncode, 1)
        self.assertIn("Python 3.13+", failed.stderr)

    def test_partial_copy_returns_three_and_integrity_gate_is_json(self):
        self.installed()
        changed = (ROOT / "skills/pod/SKILL.md").read_bytes() + b"\n<!-- changed -->\n"
        newer = archive_tree(self.root / "partial.tar.gz", changes={"skills/pod/SKILL.md": changed})
        failed = run_install({**self.env, "POD_INSTALL_SOURCE": newer.as_uri(),
                              "POD_TEST_NPX_MODE": "partial_fail"})
        self.assertEqual(failed.returncode, 3, failed.stdout + failed.stderr)
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "installing")
        self.assertEqual(run_pod(self.env, "doctor", "--json").returncode, 2)
        recovered = run_install({**self.env, "POD_INSTALL_SOURCE": newer.as_uri()})
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        receipt = json.loads(self.receipt.read_text())
        receipt["status"] = "installing"
        self.receipt.write_text(json.dumps(receipt))
        (self.canonical / "SKILL.md").write_bytes(changed + b"\n# middle state\n")
        gate = run_pod(self.env, "doctor", "--json")
        self.assertEqual(gate.returncode, 2)
        self.assertEqual(json.loads(gate.stdout)["error"]["code"], "install_incomplete")

    def test_sigkill_during_dependency_download_recovers(self):
        import http.server
        import threading
        request = threading.Event()
        release = threading.Event()
        class SlowWheel(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                request.set()
                release.wait(30)
            def log_message(self, *_args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowWheel)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        env = {**self.env, "PIP_FIND_LINKS": f"http://127.0.0.1:{server.server_port}/"}
        process = subprocess.Popen(["sh", str(ROOT / "install.sh")], env=env, cwd=self.root / "work",
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            self.assertTrue(request.wait(12), "pip never requested the local wheel index")
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)
            self.assertFalse(self.receipt.exists())
            self.assertFalse(self.launcher.exists())
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=3)
            release.set()
            server.shutdown(); server.server_close()
        recovered = run_install(self.env)
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
