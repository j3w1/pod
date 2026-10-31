"""#141: a test process cannot reach a real checkout, home or credential, however Git is invoked."""
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
from tests.common import fixture
from tests.isolation import IsolationBreach

ROOT = Path(__file__).resolve().parents[1]
CONFINED = unittest.skipUnless(boundary.CONFINED, "Landlock is unavailable here; the CI lanes forbid skips")
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


def child(program: str, args: dict, *, nested: bool = True, readable=(), env: dict | None = None,
          cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a program in a fresh interpreter. A nested child gets its own, narrower private root, so
    everything the parent created (the stand-ins) is outside it, as a real checkout is."""
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT / "skills"), str(ROOT))), **(env or {})}
    if nested:
        environment[boundary.SWITCH] = "nested"
    if readable:
        environment[boundary.READABLE] = os.pathsep.join(map(str, readable))
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
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "CODEX_HOME",
                    "CLAUDE_CONFIG_DIR", "POD_STATE_HOME", "POD_CONFIG_HOME", "POD_CACHE_HOME", "TMPDIR"):
            self.assertTrue(Path(os.environ[key]).resolve().is_relative_to(root), key)
        self.assertEqual(Path(tempfile.gettempdir()).resolve(), (root / "tmp").resolve())
        self.assertEqual(os.environ["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertFalse({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"} & set(os.environ))

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
            print(json.dumps([boundary.PRIVATE_ROOT is None, boundary.CONFINED]))
        """, {}, nested=False, env={boundary.SWITCH: "off"})
        self.assertEqual(json.loads(result.stdout), [True, False], result.stderr)


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


@CONFINED
class BoundaryTests(unittest.TestCase):
    """Leaks aimed at stand-ins for the host, from children whose boundary excludes them."""

    def setUp(self):
        self.temp = fixture(); self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.canonical = repository(self.root / "canonical")
        self.home = self.root / "real-home"
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex/auth.json").write_text("synthetic-credential\n")
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / ".local/bin/pod").write_text("#!/bin/sh\n")
        self.before = state(self.canonical)

    def assert_untouched(self):
        self.assertEqual(state(self.canonical), self.before)
        self.assertEqual((self.home / ".codex/auth.json").read_text(), "synthetic-credential\n")
        self.assertEqual((self.home / ".local/bin/pod").read_text(), "#!/bin/sh\n")

    def test_this_process_cannot_write_the_checkout_or_its_git_metadata(self):
        for target in (ROOT / "tests" / ".leak-canary", ROOT / ".git-leak-canary"):
            with self.subTest(target.name), self.assertRaises(PermissionError):
                target.write_text("leak")
            self.assertFalse(target.exists())

    def test_a_child_can_neither_write_nor_read_a_real_home_launcher_or_credentials(self):
        result = child("""
            import json, sys
            import tests
            from pathlib import Path
            home, out = Path(json.loads(sys.argv[1])["home"]), {}
            for name, action in (("write credentials", lambda: (home / ".codex/auth.json").write_text("x")),
                                 ("write launcher", lambda: (home / ".local/bin/pod").write_text("x")),
                                 ("create", lambda: (home / "new").write_text("x")),
                                 ("read credentials", lambda: (home / ".codex/auth.json").read_text()),
                                 ("delete", lambda: (home / ".local/bin/pod").unlink())):
                try: action(); out[name] = "allowed"
                except PermissionError: out[name] = "denied"
            print(json.dumps(out))
        """, {"home": str(self.home)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(json.loads(result.stdout).values()), {"denied"}, result.stdout)
        self.assert_untouched()

    def test_an_import_during_discovery_cannot_write_outside_the_boundary(self):
        discovery = self.root / "discovery"; discovery.mkdir()
        outside = self.home / "import-leak"
        (discovery / "test_canary.py").write_text(textwrap.dedent(f"""
            import unittest
            import tests.common
            try:
                open({str(outside)!r}, "w").write("leak"); LEAKED = True
            except PermissionError:
                LEAKED = False
            class Canary(unittest.TestCase):
                def test_import_time_write_was_prevented(self): self.assertFalse(LEAKED)
        """))
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT / "skills"), str(ROOT))),
                       boundary.SWITCH: "nested", boundary.READABLE: str(discovery)}
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(discovery), "-t",
                                 str(discovery)], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(outside.exists())

    GIT = """
        import json, subprocess, sys
        import tests
        from pathlib import Path
        from tests.common import fixture
        args, out = json.loads(sys.argv[1]), {}
        def run(where, *argv, env=None):
            return subprocess.run(["git", "-C", str(where), "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
                                   *argv], capture_output=True, text=True, env=env).returncode
        with fixture() as root:
            own = root / "own"; own.mkdir()
            out["control init"] = run(own, "init", "-q", "-b", "main")
            (own / "f").write_text("f")
            out["control commit"] = run(own, "add", ".") + run(own, "commit", "-qm", "own")
            %s
        print(json.dumps(out))
    """

    def test_a_dot_git_pointer_to_an_external_repository_cannot_write_it(self):
        result = child(self.GIT % """
            project = root / "project"; project.mkdir()
            (project / ".git").write_text("gitdir: " + args["gitdir"] + "\\n")
            out["config"] = run(project, "config", "user.name", "Fixture")
            out["commit"] = run(project, "commit", "--allow-empty", "-qm", "base")
            out["branch"] = run(project, "branch", "leak")
        """, {"gitdir": str(self.canonical / ".git")}, readable=[self.canonical])
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = json.loads(result.stdout)
        self.assertEqual([outcome["control init"], outcome["control commit"]], [0, 0], outcome)
        self.assertTrue(all(outcome[key] != 0 for key in ("config", "commit", "branch")), outcome)
        self.assert_untouched()

    def test_explicit_hostile_git_environment_and_cwd_cannot_write_the_external_repository(self):
        hostile = {**os.environ, "GIT_DIR": str(self.canonical / ".git"), "GIT_WORK_TREE": str(self.canonical),
                   "GIT_INDEX_FILE": str(self.canonical / ".git/index")}
        result = child(self.GIT % """
            for name, argv in (("config", ["config", "user.name", "Fixture"]), ("add", ["add", "-A"]),
                               ("commit", ["commit", "--allow-empty", "-qm", "base"]), ("reinit", ["init", "-q"])):
                out[name] = run(Path(args["canonical"]), *argv, env=args["env"])
        """, {"canonical": str(self.canonical), "env": hostile}, readable=[self.canonical])
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = json.loads(result.stdout)
        self.assertEqual([outcome["control init"], outcome["control commit"]], [0, 0], outcome)
        self.assertTrue(all(outcome[key] != 0 for key in ("config", "commit")), outcome)
        self.assert_untouched()

    def test_a_linked_worktree_and_its_common_directory_cannot_be_written_from_outside(self):
        linked = self.root / "linked"
        git(self.canonical, "worktree", "add", "-q", "-b", "task", str(linked))
        self.before = state(self.canonical)
        before_linked = state(linked)
        result = child(self.GIT % """
            linked = Path(args["linked"])
            out["commit"] = run(linked, "commit", "--allow-empty", "-qm", "leak")
            out["common branch"] = run(linked, "branch", "leak")
            out["tag"] = run(linked, "tag", "leak")
        """, {"linked": str(linked)}, readable=[self.canonical, linked], cwd=linked)
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = json.loads(result.stdout)
        self.assertEqual([outcome["control init"], outcome["control commit"]], [0, 0], outcome)
        self.assertTrue(all(outcome[key] != 0 for key in ("commit", "common branch", "tag")), outcome)
        self.assert_untouched()
        self.assertEqual(state(linked), before_linked)


if __name__ == "__main__":
    unittest.main()
