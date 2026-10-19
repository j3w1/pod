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

from pod import cli, config, github, internal, operations, orca, term
from pod.errors import PodError
from tests.common import fixture
from tests.issue41_support import ProductionCase
from tests.kernel_support import git


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
        inventories = patch("pod.github.worktree_inventory", wraps=github.worktree_inventory)
        self.inventories = inventories.start()
        self.addCleanup(inventories.stop)

    @property
    def native_read_count(self):
        return self.reads.call_count + self.inventories.call_count

    def test_one_scope_reads_each_repository_once_and_returns_independent_copies(self):
        with github.repository_reads():
            first = github.repository_context(self.project)
            first["linked_worktrees"].append("changed by a caller")
            for _ in range(4):
                again = github.repository_context(self.project)
        self.assertEqual(self.native_read_count, 3)
        self.assertNotIn("changed by a caller", again["linked_worktrees"])
        github.repository_context(self.project)
        github.repository_context(self.project)
        self.assertEqual(self.native_read_count, 9)  # Outside a scope every call reads Git.

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
        self.assertEqual(self.native_read_count, 6)

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
        self.assertEqual(len(calls) + self.inventories.call_count, 4)

    def test_the_binding_check_before_an_effect_reads_git_fresh(self):
        with github.repository_reads():
            bound = github.repository_context(self.project)
            binding = {"repository": bound["repository"], "repo_key": bound["repo_key"],
                       "path": bound["worktree"], "branch": bound["branch"]}
            operations._check_worktree_binding(self.project, binding)
            subprocess.run(["git", "-C", str(self.project), "switch", "-qc", "elsewhere"], check=True)
            with self.assertRaises(PodError) as caught:
                operations._check_worktree_binding(self.project, binding)
            self.assertEqual(caught.exception.code, "worktree_binding_changed")
            # The call's later reads agree with what the check saw.
            self.assertEqual(github.repository_context(self.project)["branch"], "elsewhere")
            operations._check_worktree_binding(self.project, None)
        self.assertEqual(self.native_read_count, 9)  # The first read and each binding check; no binding, no read.

    def test_issue_target_checks_read_the_remote_fresh(self):
        subprocess.run(["git", "-C", str(self.project), "remote", "add", "origin",
                        "https://github.com/acme/example.git"], check=True)
        offline = SimpleNamespace(issue=lambda **kwargs: self.fail("the target check must refuse before GitHub"))
        readable = SimpleNamespace(issue=lambda **kwargs: {
            "number": 7, "url": "https://github.com/acme/example/issues/7", "state": "OPEN",
            "title": "PES: implement example", "body": "Complete executable scope"})
        with github.repository_reads():
            github.repository_context(self.project)
            subprocess.run(["git", "-C", str(self.project), "remote", "set-url", "origin",
                            "https://github.com/other/elsewhere.git"], check=True)
            for check in (lambda: github.issue_intake(self.project, "https://github.com/acme/example/issues/7",
                                                      port=readable),
                          lambda: github.issue_recheck(self.project, {
                              "schema": "pod-issue-source/v1", "locator": "https://github.com/acme/example/issues/7",
                              "repository": "acme/example", "number": 7, "body_sha256": "0" * 64,
                              "amendments": []}, port=offline)):
                with self.subTest(check=check), self.assertRaises(PodError) as caught:
                    check()
                self.assertEqual(caught.exception.code, "repository_mismatch")

    def test_a_transient_remote_failure_is_not_kept(self):
        subprocess.run(["git", "-C", str(self.project), "remote", "add", "origin",
                        "https://github.com/acme/example.git"], check=True)
        real, failed = github._local_git, []

        def slow_remote(project, argv, **kwargs):
            if argv[:2] == ["remote", "get-url"] and not failed:
                failed.append(argv)
                raise PodError("git_unavailable", "Git did not answer the repository discovery read")
            return real(project, argv, **kwargs)

        with patch("pod.github._local_git", side_effect=slow_remote), github.repository_reads():
            self.assertIsNone(github.repository_context(self.project)["repository"])
            self.assertEqual(github.repository_context(self.project)["repository"], "acme/example")
            self.assertEqual(github.repository_context(self.project)["repository"], "acme/example")

    def test_the_config_editor_runs_as_an_effect(self):
        path = self.root / "config-home" / "pod" / "config.yaml"
        with patch.dict("os.environ", {"VISUAL": "true", "EDITOR": "true"}), github.repository_reads():
            github.repository_context(self.project)
            cli._edit(path)
            github.repository_context(self.project)
        self.assertEqual(self.native_read_count, 6)

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
        self.assertEqual(self.native_read_count, 9)  # First read, after the Orca effect, after the mutation.

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
        self.assertEqual((self.native_read_count, scoped), (6, [True, True]))

        def execute(args, project):
            probe({})
            return {"schema": "pod-cli/v5", "status": "ok"}

        with patch("pod.cli.execute", side_effect=execute), redirect_stdout(StringIO()):
            cli.main(["doctor", "--json"])
        self.assertEqual(self.native_read_count, 9)
        terminal = SimpleNamespace(isatty=lambda: True)
        workspace = []
        with patch.object(cli, "sys", SimpleNamespace(stdin=terminal, stdout=terminal, stderr=sys.stderr)), \
                patch("pod.tui.run", side_effect=lambda project: workspace.append(github._IDENTITY.get()) or 0):
            self.assertEqual(cli.main([]), 0)
        self.assertEqual(workspace, [None])


class BindingBeforeStartTests(ProductionCase):
    def test_a_branch_switch_during_an_admission_still_refuses_the_start(self):
        context = github.repository_context(self.project)
        binding = {"repository": context["repository"], "repo_key": context["repo_key"],
                   "path": context["worktree"], "branch": context["branch"]}
        self.intake(self.sub("S"), worktree=binding)
        recheck = operations._check_objective_source

        def recheck_then_switch(*args, **kwargs):
            # Another actor switches the branch while this helper call rechecks its issue source.
            result = recheck(*args, **kwargs)
            git(self.project, "switch", "-qc", "elsewhere")
            return result

        with patch("pod.operations._check_objective_source", side_effect=recheck_then_switch), \
                self.assertRaises(PodError) as caught:
            self.start("work", self.packet(["S"], worktree=binding))
        self.assertEqual(caught.exception.code, "worktree_binding_changed")
        self.assertEqual(self.port.starts, [])
        git(self.project, "switch", "-q", context["branch"])
        self.assertEqual(self.start("work", self.packet(["S"], worktree=binding))["status"], "bound")


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
