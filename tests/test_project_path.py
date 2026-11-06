"""Project input validation precedes Git, native reads and command dispatch."""

from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pod.cli import execute, main as cli_main
from pod.errors import PodError
from pod.github import _local_git, repository_context
from pod.internal import _OPERATIONS, main as internal_main, run
from pod.util import project_path


class ProjectPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.file = self.directory / 'private-file'
        self.file.write_text('private contents')

    def invalid_values(self):
        return ('private-name', './private/path', self.directory / 'private-missing',
                self.file, '', None, 17, [], {}, '/private\x00path')

    def refused(self, call, value):
        with self.assertRaises(PodError) as caught:
            call(value)
        error = caught.exception
        self.assertEqual(error.code, 'project_path_invalid')
        self.assertEqual(error.detail['field'], 'project')
        self.assertTrue(error.detail['kind'])
        self.assertEqual(error.detail['next_action'], 'pass the absolute worktree path')
        self.assertIn('project', str(error))
        self.assertIn('pass the absolute worktree path', str(error))
        self.assertNotIn('private', str(error) + json.dumps(error.detail))

    def test_validator_rejects_invalid_paths_without_subprocess(self):
        with patch('subprocess.run') as process:
            for value in self.invalid_values():
                with self.subTest(value_type=type(value).__name__):
                    self.refused(project_path, value)
            process.assert_not_called()

    def test_valid_absolute_directory_and_directory_symlink(self):
        self.assertEqual(project_path(self.directory), self.directory)
        self.assertEqual(project_path(str(self.directory)), self.directory)
        link = self.directory / 'link'
        link.symlink_to(self.directory, target_is_directory=True)
        self.assertEqual(project_path(link), link)

    def test_every_internal_operation_checks_before_dispatch(self):
        for operation in _OPERATIONS:
            with self.subTest(operation=operation), \
                    patch.dict(_OPERATIONS, {operation: unittest.mock.Mock()}) as handlers, \
                    patch('subprocess.run') as process:
                for value in self.invalid_values():
                    self.refused(lambda item: run(operation, {'project': item}), value)
                handlers[operation].assert_not_called()
                process.assert_not_called()

    def test_public_dispatch_checks_before_command_specific_reads(self):
        # Missing command-specific arguments would fail if dispatch preceded validation.
        for command in (None, 'admission', 'policy', 'tool', 'config', 'models',
                        'doctor', 'status', 'sweep', 'continue', 'wake'):
            with self.subTest(command=command), patch('subprocess.run') as process:
                for value in self.invalid_values():
                    self.refused(lambda item: execute(Namespace(command=command), item), value)
                process.assert_not_called()

    def test_git_discovery_checks_before_resolve_or_subprocess(self):
        for call in (repository_context,
                     lambda path: _local_git(path, ['remote', 'get-url', 'origin'])):
            with patch('subprocess.run') as process, \
                    patch.object(Path, 'resolve', side_effect=AssertionError('resolved too early')):
                for value in self.invalid_values():
                    self.refused(call, value)
                process.assert_not_called()

    def test_valid_directory_keeps_git_unavailable(self):
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired('git', 20)):
            with self.subTest(failure=type(failure).__name__), \
                    patch('pod.github.git_executable', return_value=Path('/usr/bin/git')), \
                    patch('subprocess.run', side_effect=failure) as process:
                with self.assertRaises(PodError) as caught:
                    _local_git(self.directory, ['remote', 'get-url', 'origin'])
                self.assertEqual(caught.exception.code, 'git_unavailable')
                process.assert_called_once()

    def test_internal_issue_intake_prints_safe_refusal(self):
        output = StringIO()
        with patch('pod.internal.bounded_stdin_json', return_value={
                'project': 'private-name', 'locator': 'https://github.com/acme/widgets/issues/1'}), \
                patch('subprocess.run') as process, redirect_stdout(output):
            result = internal_main(['issue-intake', '--input', '-'])
        self.assertEqual(result, 1)
        error = json.loads(output.getvalue())['error']
        self.assertEqual(error['code'], 'project_path_invalid')
        self.assertNotIn('private-name', output.getvalue())
        process.assert_not_called()

    def test_public_main_prints_safe_refusal(self):
        output = StringIO()
        with patch.object(Path, 'cwd', return_value=Path('private-name')), \
                patch('subprocess.run') as process, redirect_stdout(output):
            result = cli_main(['doctor', '--json'])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())['error']['code'], 'project_path_invalid')
        self.assertNotIn('private-name', output.getvalue())
        process.assert_not_called()
