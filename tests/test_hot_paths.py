"""Reads shared within one call: repository identity, YAML documents and terminal text widths."""

from contextlib import redirect_stdout
from io import StringIO
import random
import subprocess
import sys
from types import SimpleNamespace
import unicodedata
import unittest
from unittest.mock import patch

from pod import cli, config, github, internal, orca, term
from pod.errors import PodError
from tests.common import fixture


class RepositoryReadsTests(unittest.TestCase):
    def setUp(self):
        self.temp = fixture()
        self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.project = self.root / "project"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.project)], check=True)
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-q", "--allow-empty", "-m", "base"],
                       check=True)
        reads = patch("pod.github._local_git", wraps=github._local_git)
        self.reads = reads.start()
        self.addCleanup(reads.stop)

    def test_one_scope_reads_each_repository_once_and_returns_independent_copies(self):
        with github.repository_reads():
            first = github.repository_context(self.project)
            first["linked_worktrees"].append("changed by a caller")
            for _ in range(4):
                again = github.repository_context(self.project)
        self.assertEqual(self.reads.call_count, 3)
        self.assertNotIn("changed by a caller", again["linked_worktrees"])
        github.repository_context(self.project)
        github.repository_context(self.project)
        self.assertEqual(self.reads.call_count, 9)  # Outside a scope every call reads Git.

    def test_nested_scopes_share_reads_until_an_effect_forgets_them(self):
        with github.repository_reads():
            before = github.repository_context(self.project)
            subprocess.run(["git", "-C", str(self.project), "worktree", "add", "-q", str(self.root / "linked")],
                           check=True, capture_output=True)
            with github.repository_reads():
                self.assertEqual(github.repository_context(self.project), before)
            github.forget_repository_reads()
            after = github.repository_context(self.project)
        self.assertEqual(len(after["linked_worktrees"]), len(before["linked_worktrees"]) + 1)
        self.assertEqual(self.reads.call_count, 6)

    def test_a_failed_read_is_never_kept(self):
        real, calls = github._local_git, []

        def flaky(project, argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                raise PodError("repository_identity_unavailable", "Git repository identity could not be read")
            return real(project, argv, **kwargs)

        with patch("pod.github._local_git", side_effect=flaky), github.repository_reads():
            with self.assertRaises(PodError):
                github.repository_context(self.project)
            self.assertEqual(github.repository_context(self.project)["worktree"], str(self.project.resolve()))
        self.assertEqual(len(calls), 4)

    def test_native_and_remote_effects_forget_reads_but_remote_reads_do_not(self):
        with github.repository_reads():
            github.repository_context(self.project)
            with patch("pod.orca.executable", return_value="/bin/false"), \
                    patch("pod.orca._mutate_allowed", return_value=True), self.assertRaises(PodError):
                orca.mutate_command(["orchestration", "run-use", "--id", "run", "--json"])
            github.repository_context(self.project)
            github._run(["true"], cwd=self.project, timeout=5, mutation=False)
            github.repository_context(self.project)
            github._run(["true"], cwd=self.project, timeout=5, mutation=True)
            github.repository_context(self.project)
        self.assertEqual(self.reads.call_count, 9)  # First read, after the Orca effect, after the mutation.

    def test_each_helper_call_and_command_is_one_scope_and_the_workspace_holds_none(self):
        scoped = []

        def probe(request):
            github.repository_context(self.project)
            github.repository_context(self.project)
            scoped.append(github._IDENTITY.get() is not None)
            return {"status": "ok"}

        with patch.dict(internal._OPERATIONS, {"probe": probe}):
            internal.run("probe", {})
            internal.run("probe", {})
        self.assertEqual((self.reads.call_count, scoped), (6, [True, True]))

        def execute(args, project):
            probe({})
            return {"schema": "pod-cli/v5", "status": "ok"}

        with patch("pod.cli.execute", side_effect=execute), redirect_stdout(StringIO()):
            cli.main(["doctor", "--json"])
        self.assertEqual(self.reads.call_count, 9)
        terminal = SimpleNamespace(isatty=lambda: True)
        workspace = []
        with patch.object(cli, "sys", SimpleNamespace(stdin=terminal, stdout=terminal, stderr=sys.stderr)), \
                patch("pod.tui.run", side_effect=lambda project: workspace.append(github._IDENTITY.get()) or 0):
            self.assertEqual(cli.main([]), 0)
        self.assertEqual(workspace, [None])


class DocumentReadsTests(unittest.TestCase):
    def test_the_same_bytes_decode_once_into_independent_documents(self):
        config._decoded.cache_clear()
        data = b"schema: fixture\nroutes: {a: enabled}\n"
        with patch("pod.config.yaml.load", wraps=config.yaml.load) as parse:
            first = config._decode(data)
            first["routes"]["a"] = "changed"
            second = config._decode(data)
        self.assertEqual((parse.call_count, second["routes"]["a"]), (1, "enabled"))

    def test_refused_bytes_are_refused_every_time(self):
        for raw in (b"a: [\n", b"a: 1\na: 2\n", b"a: &x 1\nb: *x\n", b"a: !!python/object:os.system 1\n"):
            for attempt in (1, 2):
                with self.subTest(raw=raw, attempt=attempt), self.assertRaises(PodError):
                    config._decode(raw)


def full_clean(value) -> str:
    """The complete character rules that the ASCII fast path must match."""
    text = str(value) if value is not None else ""
    return "".join(character for character in text if unicodedata.category(character) not in ("Cc", "Cf", "Cs"))


def full_width(value) -> int:
    width = 0
    for character in full_clean(value):
        if not unicodedata.combining(character):
            width += 2 if unicodedata.east_asian_width(character) in ("F", "W") else 1
    return width


class TerminalTextTests(unittest.TestCase):
    def test_fast_paths_match_the_full_character_rules(self):
        alphabet = [chr(code) for code in range(128)] + ["é", "宽", "́", "​", "😀", "‮", "\ud800"]
        rng = random.Random(53)
        samples = alphabet + ["", None, 17, "plain text", "tab\there", "naïve", "宽字符", "é"]
        samples += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 16))) for _ in range(600)]
        for sample in samples:
            with self.subTest(sample=repr(sample)):
                self.assertEqual(term.clean(sample), full_clean(sample))
                self.assertEqual(term.display_width(sample), full_width(sample))


if __name__ == "__main__":
    unittest.main()
