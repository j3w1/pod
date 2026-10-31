"""#141: fixtures cannot reach a real checkout, and a test that tries is caught."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from tests import isolation
from tests.common import HOST_HOME, fixture
from tests.isolation import Guard, IsolationBreach

ROOT = Path(__file__).resolve().parents[1]
CLEAN = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def git(where: Path, *argv: str, env: dict | None = None) -> str:
    return subprocess.run(["git", "-C", str(where), *argv], capture_output=True, text=True, check=True,
                          env={**CLEAN, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                               **(env or {})}).stdout.strip()


def repository(where: Path, *, name: str = "main") -> Path:
    """A disposable stand-in for a canonical checkout."""
    where.mkdir(parents=True)
    git(where, "init", "-q", "-b", name)
    git(where, "config", "user.name", "Real")
    git(where, "config", "user.email", "real@example.invalid")
    (where / "tracked.txt").write_text("real\n")
    git(where, "add", ".")
    git(where, "commit", "-qm", "real")
    return where


class ScrubTests(unittest.TestCase):
    def test_inherited_git_variables_are_removed_and_config_is_pinned(self):
        env = {"GIT_DIR": "/x", "GIT_WORK_TREE": "/x", "GIT_INDEX_FILE": "/x", "GIT_COMMON_DIR": "/x",
               "GIT_OBJECT_DIRECTORY": "/x", "GIT_NAMESPACE": "x", "GIT_DEFAULT_HASH": "sha256", "git_dir": "/lowercase", "PATH": "/bin",
               "GIT_CONFIG_GLOBAL": "/host/user.gitconfig", "GIT_CONFIG_COUNT": "1",
               "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/hooks"}
        isolation.scrub_git_environment(env, ceiling="/fixture")
        self.assertEqual(env, {"PATH": "/bin", "GIT_DEFAULT_HASH": "sha256", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                               "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_CONFIG_COUNT": "1",
                               "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "false",
                               "GIT_CEILING_DIRECTORIES": "/fixture"})

    def test_the_suite_process_and_every_fixture_start_scrubbed(self):
        self.assertFalse({key for key in os.environ if key in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}})
        self.assertEqual(os.environ["GIT_CONFIG_GLOBAL"], os.devnull)
        with fixture() as root:
            self.assertEqual(os.environ["GIT_CEILING_DIRECTORIES"], str(root.parent))
            self.assertEqual(os.environ["GIT_CONFIG_NOSYSTEM"], "1")
            isolation.assert_disposable(root.parent, os.environ)

    def test_homes_and_path_outside_the_fixture_are_refused(self):
        with fixture() as root:
            for key, value in (("HOME", str(HOST_HOME)), ("XDG_STATE_HOME", "/var/tmp/state")):
                with self.subTest(key), patch.dict(os.environ, {key: value}), self.assertRaises(IsolationBreach):
                    isolation.assert_disposable(root.parent, os.environ)
            with patch.dict(os.environ, {"PATH": os.environ["PATH"] + os.pathsep + str(HOST_HOME / ".local/bin")}), \
                    self.assertRaises(IsolationBreach):
                isolation.assert_disposable(root.parent, os.environ)


class ContaminationTests(unittest.TestCase):
    """The #141 mechanism, run in a fresh interpreter whose environment points Git at a real repository."""
    PROGRAM = textwrap.dedent("""
        import subprocess, sys
        import tests
        from tests.common import HOST_HOME, fixture
        def git(*argv): subprocess.run(["git", *argv], check=True, capture_output=True)
        with fixture() as root:
            project = root / "project"; project.mkdir()
            git("init", "-q", "-b", "main", str(project))
            git("-C", str(project), "config", "user.name", "Fixture")
            git("-C", str(project), "config", "user.email", "fixture@example.invalid")
            (project / "README.md").write_text("fixture\\n")
            git("-C", str(project), "add", ".")
            git("-C", str(project), "commit", "-qm", "base")
    """)

    def run_fixture(self, env: dict, *, cwd: Path = ROOT) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", self.PROGRAM], cwd=cwd, capture_output=True, text=True,
                              env={**CLEAN, "PYTHONPATH": os.pathsep.join((str(ROOT / "skills"), str(ROOT))), **env},
                              timeout=60)

    def test_inherited_git_dir_work_tree_and_index_no_longer_redirect_a_fixture(self):
        with tempfile.TemporaryDirectory() as name:
            canonical = repository(Path(name) / "canonical")
            before = {"head": git(canonical, "rev-parse", "HEAD"), "refs": git(canonical, "for-each-ref"),
                      "index": git(canonical, "ls-files", "-s"), "config": git(canonical, "config", "--list")}
            result = self.run_fixture({"GIT_DIR": str(canonical / ".git"), "GIT_WORK_TREE": str(canonical),
                                       "GIT_INDEX_FILE": str(canonical / ".git/index")}, cwd=canonical)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(before, {"head": git(canonical, "rev-parse", "HEAD"), "refs": git(canonical, "for-each-ref"),
                                      "index": git(canonical, "ls-files", "-s"),
                                      "config": git(canonical, "config", "--list")})
            self.assertEqual(git(canonical, "log", "--format=%s"), "real")

    def test_inherited_global_config_hooks_and_identity_do_not_reach_a_fixture(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            hooks = base / "hooks"; hooks.mkdir()
            marker = base / "hook-ran"
            (hooks / "pre-commit").write_text(f"#!/bin/sh\ntouch '{marker}'\n")
            (hooks / "pre-commit").chmod(0o755)
            config = base / "gitconfig"
            config.write_text(f"[core]\n\thooksPath = {hooks}\n[user]\n\tname = Someone Real\n")
            result = self.run_fixture({"GIT_CONFIG_GLOBAL": str(config)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists(), "a host hook ran inside a fixture")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.canonical = repository(self.base / "canonical")
        self.credentials = self.base / "credentials"; self.credentials.write_text("host-secret\n")
        self.launcher = self.base / "pod"; self.launcher.write_text("#!/bin/sh\n"); self.launcher.chmod(0o755)
        self.guard = Guard([self.canonical], [self.credentials, self.launcher, self.base / "absent"])

    def stage(self):
        (self.canonical / "new.txt").write_text("x\n")
        git(self.canonical, "add", "new.txt")

    def leak(self, change) -> str:
        with self.assertRaises(IsolationBreach) as caught, isolation.watching(self.guard), fixture():
            change()
        return str(caught.exception)

    def test_a_clean_fixture_passes_even_when_it_builds_its_own_repository(self):
        with isolation.watching(self.guard), fixture() as root:
            repository(root / "project")
        self.guard.verify()

    def test_a_leaking_fixture_is_caught_for_head_branch_ref_index_and_config(self):
        cases = {
            "HEAD": lambda: git(self.canonical, "checkout", "-q", "-b", "elsewhere"),
            "ref refs/heads/main": lambda: git(self.canonical, "commit", "-q", "--allow-empty", "-m", "base"),
            "index": self.stage,
            "config": lambda: git(self.canonical, "config", "user.name", "Fixture"),
        }
        for expected, change in cases.items():
            with self.subTest(expected):
                self.guard = Guard([self.canonical], [])
                self.assertIn(f"{expected} changed", self.leak(change))
                git(self.canonical, "checkout", "-q", "main"); git(self.canonical, "reset", "-q", "--hard", "main")

    def test_a_leaking_fixture_is_caught_for_the_launcher_and_credentials(self):
        for target in (self.credentials, self.launcher):
            with self.subTest(target.name):
                self.guard = Guard([], [target])
                self.assertIn(f"file {target} changed", self.leak(lambda: target.write_text("rewritten by a test\n")))
        self.guard = Guard([], [self.base / "absent"])
        self.assertIn("changed", self.leak(lambda: (self.base / "absent").write_text("created\n")))

    def test_the_breach_is_reported_even_when_the_leaking_test_also_raises(self):
        with self.assertRaises(IsolationBreach), isolation.watching(self.guard), fixture():
            git(self.canonical, "commit", "-q", "--allow-empty", "-m", "leak")
            raise ValueError("the leaking test also failed")

    def test_refreshing_the_index_branch_tracking_and_identical_rewrites_are_not_changes(self):
        (self.canonical / "tracked.txt").touch()
        git(self.canonical, "status", "--short")
        git(self.canonical, "update-index", "--refresh")
        git(self.canonical, "config", "branch.main.remote", "origin")
        git(self.canonical, "config", "branch.another.merge", "refs/heads/main")
        self.credentials.write_text(self.credentials.read_text())
        self.guard.verify()

    def test_each_change_is_reported_once(self):
        self.leak(lambda: self.credentials.write_text("changed\n"))
        with isolation.watching(self.guard), fixture():
            pass

    def test_other_tasks_worktrees_of_the_same_repository_may_change(self):
        task = self.base / "task"
        git(self.canonical, "worktree", "add", "-q", "-b", "task-branch", str(task))
        guard = Guard([self.canonical], [])
        (task / "work.txt").write_text("other task\n")
        git(task, "add", "."); git(task, "-c", "user.name=Other", "-c", "user.email=o@example.invalid",
                  "commit", "-qm", "other task progress")
        guard.verify()

    def test_protected_checkouts_are_this_one_its_main_one_and_the_listed_ones(self):
        with tempfile.TemporaryDirectory() as extra:
            found = isolation.default_checkouts(extra=os.pathsep.join((extra, "")))
        self.assertIn(ROOT.resolve(), found)
        self.assertEqual(found[-1], Path(extra).resolve())
        self.assertEqual(len(found), len(set(found)))
        for path in found[:-1]:
            self.assertTrue((path / ".git").exists())

    def test_the_default_guard_watches_the_launcher_and_credential_files(self):
        home = self.base / "home"
        files = isolation.default_files(home, path=str(self.base))
        self.assertIn(home / ".gitconfig", files)
        self.assertIn(home / ".local/bin/pod", files)
        self.assertIn(self.launcher, files)
        self.assertTrue(isolation.host_guard().checkouts)


if __name__ == "__main__":
    unittest.main()
