"""#141: an inherited Git environment cannot send a fixture to a real repository, and the wrapper hides the host."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from tests import boundary, isolation
from tools import isolate
from tests.common import fixture
from tests.isolation import IsolationBreach

ROOT = Path(__file__).resolve().parents[1]
HOSTILE = {"GIT_DIR": "x", "GIT_WORK_TREE": "x", "GIT_INDEX_FILE": "x", "GIT_COMMON_DIR": "x"}


def git(where: Path, *argv: str, env: dict | None = None) -> str:
    return subprocess.run(["git", "-C", str(where), *argv], capture_output=True, text=True, check=True,
                          env={**os.environ, **(env or {})}).stdout.strip()


def repository(where: Path) -> Path:
    """A disposable stand-in for a real checkout."""
    where.mkdir(parents=True)
    git(where, "init", "-q", "-b", "main")
    git(where, "config", "user.name", "Real")
    git(where, "config", "user.email", "real@example.invalid")
    (where / "tracked.txt").write_text("real\n")
    git(where, "add", ".")
    git(where, "commit", "-qm", "real")
    return where


def state(repo: Path) -> dict:
    """Everything a leak could change in a repository."""
    gitdir = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return {"head": git(repo, "rev-parse", "HEAD"), "refs": git(repo, "for-each-ref"),
            "index": git(repo, "ls-files", "-s"), "config": (gitdir / "config").read_text(),
            "worktrees": git(repo, "worktree", "list", "--porcelain")}


def child(program: str, args: dict, *, nested: bool = True, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a program in a fresh interpreter. A nested child gets its own private root."""
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT / "skills"), str(ROOT))), **(env or {})}
    if nested:
        environment[boundary.SWITCH] = "nested"
    return subprocess.run([sys.executable, "-c", textwrap.dedent(program), json.dumps(args)], cwd=cwd or ROOT,
                          env=environment, capture_output=True, text=True, timeout=60)


class ScrubTests(unittest.TestCase):
    def test_inherited_git_variables_are_removed_and_config_is_pinned(self):
        env = {"GIT_DIR": "/x", "GIT_WORK_TREE": "/x", "GIT_INDEX_FILE": "/x", "GIT_COMMON_DIR": "/x",
               "GIT_OBJECT_DIRECTORY": "/x", "GIT_NAMESPACE": "x", "GIT_DEFAULT_HASH": "sha256", "git_dir": "/lower",
               "PATH": "/bin", "GIT_CONFIG_GLOBAL": "/host/user.gitconfig", "GIT_CONFIG_COUNT": "1",
               "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/hooks"}
        isolation.scrub_git_environment(env, ceiling="/fixture")
        self.assertEqual(env, {"PATH": "/bin", "GIT_DEFAULT_HASH": "sha256", "GIT_CONFIG_GLOBAL": os.devnull,
                               "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true",
                               "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "maintenance.auto",
                               "GIT_CONFIG_VALUE_0": "false", "GIT_CEILING_DIRECTORIES": "/fixture"})

    def test_this_process_started_inside_the_private_root_with_no_credentials(self):
        root = boundary.PRIVATE_ROOT
        self.assertIsNotNone(root)
        # Other tests may unset the Pod homes for good, so those are proven in a fresh boundary of their own.
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "CODEX_HOME",
                    "CLAUDE_CONFIG_DIR", "TMPDIR"):
            self.assertTrue(Path(os.environ[key]).resolve().is_relative_to(root), key)
        self.assertEqual(Path(tempfile.gettempdir()).resolve(), (root / "tmp").resolve())
        result = child("""
            import json, os
            import tests
            from tests import boundary
            keys = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "CODEX_HOME",
                    "CLAUDE_CONFIG_DIR", "POD_STATE_HOME", "POD_CONFIG_HOME", "POD_CACHE_HOME", "TMPDIR")
            print(json.dumps({"root": str(boundary.PRIVATE_ROOT),
                              "inside": {key: os.path.realpath(os.environ[key]).startswith(str(boundary.PRIVATE_ROOT.resolve()))
                                         for key in keys},
                              "git": [os.environ["GIT_CONFIG_GLOBAL"], os.environ.get("GIT_DIR")]}))
        """, {}, env={"GIT_DIR": "/host/.git", "HOME": "/host/home", "POD_STATE_HOME": "/host/state"})
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(result.stdout)
        self.assertEqual(set(seen["inside"].values()), {True}, seen)
        self.assertEqual(seen["git"], [os.devnull, None])

    def test_a_child_cannot_have_credentials_or_git_redirection_reintroduced_by_its_env(self):
        result = child("""
            import json, os
            import tests
            print(json.dumps(sorted(key for key in os.environ if key.startswith(("GIT_DIR", "GIT_WORK", "GH_", "ORCA_",
                  "SSH_", "ANTHROPIC", "OPENAI")) or "TOKEN" in key)))
        """, {}, nested=False, env={**HOSTILE, "GH_TOKEN": "t", "ORCA_HOOK": "h", "SSH_AUTH_SOCK": "s",
                                    "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "SOME_TOKEN": "t"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])

    def test_homes_and_path_outside_the_fixture_are_refused(self):
        with fixture() as root:
            for key, value in (("HOME", "/host/home"), ("XDG_STATE_HOME", "/var/tmp/state")):
                with self.subTest(key), patch.dict(os.environ, {key: value}), self.assertRaises(IsolationBreach):
                    isolation.assert_disposable(root.parent, os.environ)
            with patch.dict(os.environ, {"PATH": os.environ["PATH"] + os.pathsep + "/host/home/.local/bin"}), \
                    self.assertRaises(IsolationBreach):
                isolation.assert_disposable(root.parent, os.environ)

    def test_every_fixture_starts_scrubbed_with_a_ceiling_at_itself(self):
        with fixture() as root:
            self.assertEqual(os.environ["GIT_CEILING_DIRECTORIES"], str(root.parent))
            self.assertEqual(os.environ["GIT_CONFIG_NOSYSTEM"], "1")
            isolation.assert_disposable(root.parent, os.environ)

    def test_the_off_switch_is_only_for_the_maintenance_command_and_changes_nothing_else(self):
        result = child("""
            import json
            import tests
            from tests import boundary
            print(json.dumps(boundary.PRIVATE_ROOT is None))
        """, {}, nested=False, env={boundary.SWITCH: "off"})
        self.assertTrue(json.loads(result.stdout), result.stderr)


class ContaminationTests(unittest.TestCase):
    """The #141 mechanism: a run whose environment points Git at a real repository."""
    PROGRAM = """
        import subprocess
        import tests
        from tests.common import fixture
        def git(*argv): subprocess.run(["git", *argv], check=True, capture_output=True)
        with fixture() as root:
            project = root / "project"; project.mkdir()
            git("init", "-q", "-b", "main", str(project))
            git("-C", str(project), "config", "user.name", "Fixture")
            git("-C", str(project), "config", "user.email", "fixture@example.invalid")
            (project / "README.md").write_text("fixture\\n")
            git("-C", str(project), "add", ".")
            git("-C", str(project), "commit", "-qm", "base")
    """

    def test_inherited_git_dir_work_tree_and_index_no_longer_redirect_a_fixture(self):
        with fixture() as root:
            canonical = repository(root / "canonical")
            before = state(canonical)
            # Not nested: this child shares the stand-in's root, so only the scrub protects it.
            result = child(self.PROGRAM, {}, nested=False, cwd=canonical, env={
                "GIT_DIR": str(canonical / ".git"), "GIT_WORK_TREE": str(canonical),
                "GIT_INDEX_FILE": str(canonical / ".git/index")})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(state(canonical), before)
            self.assertEqual(git(canonical, "log", "--format=%s"), "real")

    def test_inherited_global_config_hooks_and_identity_do_not_reach_a_fixture(self):
        with fixture() as root:
            hooks = root / "hooks"; hooks.mkdir()
            marker = root / "hook-ran"
            (hooks / "pre-commit").write_text(f"#!/bin/sh\ntouch '{marker}'\n")
            (hooks / "pre-commit").chmod(0o755)
            config = root / "gitconfig"
            config.write_text(f"[core]\n\thooksPath = {hooks}\n[user]\n\tname = Someone Real\n")
            result = child(self.PROGRAM, {}, nested=False, env={"GIT_CONFIG_GLOBAL": str(config)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists(), "a host hook ran inside a fixture")

    def test_an_unrelated_actor_changing_a_repository_never_fails_a_clean_fixture(self):
        with fixture() as root:
            elsewhere = repository(root / "elsewhere")
            with fixture():
                git(elsewhere, "-c", "user.name=Other", "-c", "user.email=o@example.invalid",
                    "commit", "--allow-empty", "-qm", "legitimate work by someone else")
            self.assertEqual(git(elsewhere, "log", "-1", "--format=%s"), "legitimate work by someone else")


class IsolateTests(unittest.TestCase):
    """The command line tools/isolate.py builds. `python tools/isolate.py --self-check` runs it for real."""

    @staticmethod
    def bound(argv, path) -> bool:
        return any(argv[i:i + 3] == ["--ro-bind", str(path), str(path)] for i in range(len(argv)))

    def argv(self, **options):
        env = {"PATH": "/usr/bin", "HOME": "/home/x", "GH_TOKEN": "t", "ORCA_HOOK": "h", "SSH_AUTH_SOCK": "s",
               "GIT_DIR": "/host/.git", "LC_ALL": "C", "POD_REQUIRE_PTY": "1", "PYTHONPATH": str(ROOT / "skills")}
        return isolate.arguments(["true"], env=env, **options)

    def test_the_host_is_read_only_and_home_credentials_and_scratch_are_empty(self):
        argv = self.argv()
        self.assertEqual(argv[argv.index("--ro-bind"):argv.index("--ro-bind") + 3], ["--ro-bind", "/", "/"])
        hidden = [argv[i + 1] for i, item in enumerate(argv) if item == "--tmpfs"]
        self.assertTrue({"/home", "/tmp"} <= set(hidden), hidden)
        self.assertTrue(self.bound(argv, ROOT))
        self.assertGreater(argv.index(str(ROOT)), max(i for i, item in enumerate(argv) if item == "--tmpfs"),
                           "the checkout is put back after /home is hidden")
        self.assertNotIn("--bind", argv)

    def test_nothing_is_inherited_except_an_allowlist(self):
        argv = self.argv()
        self.assertIn("--clearenv", argv)
        passed = {argv[i + 1] for i, item in enumerate(argv) if item == "--setenv"}
        self.assertEqual(passed, {"PATH", "LC_ALL", "POD_REQUIRE_PTY", "PYTHONPATH", "HOME", "TMPDIR"})

    def test_the_command_follows_the_separator_and_the_process_dies_with_its_parent(self):
        argv = self.argv()
        self.assertEqual(argv[-2:], ["--", "true"])
        self.assertIn("--die-with-parent", argv)
        self.assertIn("--unshare-all", argv)

    def test_a_linked_worktree_gets_its_git_directories_read_only_and_extras_come_back_read_only(self):
        with fixture() as root:
            common = root / "common.git"; common.mkdir()
            gitdir = common / "worktrees" / "w"; gitdir.mkdir(parents=True)
            (gitdir / "commondir").write_text("../..\n")
            checkout = root / "checkout"; checkout.mkdir()
            (checkout / ".git").write_text(f"gitdir: {gitdir}\n")
            extra = root / "extra"; extra.mkdir()
            argv = self.argv(checkout=checkout, readable=(str(extra),))
            for path in (checkout, gitdir.resolve(), common.resolve(), extra):
                self.assertTrue(self.bound(argv, path), path)


if __name__ == "__main__":
    unittest.main()
