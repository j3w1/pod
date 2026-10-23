"""The list stops waiting at one deadline and never publishes late discovery facts."""

import builtins
import errno
from contextlib import ExitStack
from contextvars import ContextVar
from copy import deepcopy
import fcntl
import io
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, current_thread
import time
import traceback
from unittest.mock import patch

from pod import catalog, config, github, history, ledger, orca, window
from pod.errors import PodError
from tests.history_support import synthetic_state
from tests.kernel_support import git
from tests.test_window_continuation import WindowCase


class BoundTests(WindowCase):
    def setUp(self):
        super().setUp()
        self.establish(admissions=False)
        self.lose()

    def listed(self, rows=None):
        rows = rows if rows is not None else [{"objective": "objective", "phase": "open"}]
        window.discover(rows, project=self.project)
        return rows

    def assert_budget(self, row):
        self.assertEqual((row["continuation"]["status"], row["continuation"]["condition"]),
                         ("unknown", "discovery_budget"))

    def clock(self):
        now = {"value": 0.0}

        class Clock(window.Budget):
            def __init__(self, seconds):
                self._clock = lambda: now["value"]
                self.deadline = seconds

        return now, Clock

    def test_worker_has_private_rows_copied_context_and_isolated_repository_cache(self):
        marker = ContextVar("discovery-test-marker", default=None)
        token = marker.set("copied")
        self.addCleanup(marker.reset, token)
        rows = [{"objective": "objective", "phase": "open", "extra": []}]
        observed = []
        caller = current_thread()

        def discover(private, *, project, budget, publish):
            observed.append((current_thread(), marker.get(), orca._DEADLINE.get(), github._IDENTITY.get()))
            private[0]["extra"].append("private")
            private[0]["continuation"] = {"status": "available"}
            github.repository_context(project)
            publish(private[0])

        with github.repository_reads():
            github.repository_context(self.project)
            cache = github._IDENTITY.get()
            before = deepcopy(cache)
            with patch.object(window, "_discover", side_effect=discover):
                self.listed(rows)
            self.assertEqual(cache, before)
        self.assertEqual(len(observed), 1)
        worker, copied, budget, private_cache = observed[0]
        self.assertIsNot(worker, caller)
        self.assertTrue(worker.daemon)
        self.assertEqual(copied, "copied")
        self.assertIsInstance(budget, window.Budget)
        self.assertIsNot(private_cache, cache)
        self.assertIsNone(orca._DEADLINE.get())
        self.assertEqual(rows[0]["extra"], [])
        self.assertEqual(rows[0]["continuation"]["status"], "available")

    def late_verdict(self, status):
        now, Clock = self.clock()

        def detect(*args, **kwargs):
            if status != "window_available" or kwargs.get("verify", True):
                now["value"] = window.DISCOVERY_SECONDS
            return {"status": status, "condition": "eligible" if status == "window_available" else "worktree_changed",
                    "message": "synthetic verdict", "next_action": "inspect"}

        with patch.object(window, "Budget", Clock), patch.object(window, "detect", side_effect=detect):
            rows = self.listed()
        self.assert_budget(rows[0])

    def test_late_available_verdict_is_discarded(self):
        self.late_verdict("window_available")

    def test_late_refusal_is_discarded(self):
        self.late_verdict("window_unavailable")

    def test_only_rows_completed_before_the_deadline_are_applied(self):
        now, Clock = self.clock()
        state = self.state()

        def detect(project, objective, *args, **kwargs):
            if objective == "unfinished":
                now["value"] = window.DISCOVERY_SECONDS
            return {"status": "window_unavailable", "condition": "worktree_changed",
                    "message": "synthetic refusal", "next_action": "inspect"}

        rows = [{"objective": name, "phase": phase} for name, phase in
                (("objective", "open"), ("unfinished", "open"), ("closed", "closed"))]
        with patch.object(window, "Budget", Clock), patch.object(ledger, "read", return_value=state), \
                patch.object(window, "detect", side_effect=detect):
            self.listed(rows)
        self.assertEqual(rows[0]["continuation"]["status"], "unavailable")
        self.assert_budget(rows[1])
        self.assertNotIn("continuation", rows[2])

    def test_blocking_read_does_not_hold_the_list_past_its_deadline(self):
        entered, release = Event(), Event()
        state = self.state()

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(1):
                raise AssertionError("test did not release the read")
            return state

        with patch.object(window, "DISCOVERY_SECONDS", 0.05), patch.object(window, "_enabled", return_value=True), \
                patch.object(ledger, "read", side_effect=blocked):
            try:
                started = time.monotonic()
                rows = self.listed()
                elapsed = time.monotonic() - started
                self.assertTrue(entered.is_set())
                self.assertLess(elapsed, 0.5)
                self.assert_budget(rows[0])
                before = deepcopy(rows)
                worker = window._DISCOVERY_WORKER
                self.assertTrue(worker.is_alive())
            finally:
                release.set()
                window._DISCOVERY_WORKER.join(0.5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(rows, before)

    def test_a_second_call_starts_no_worker_while_the_first_read_is_blocked(self):
        entered, release = Event(), Event()
        read = ledger.read
        caller = current_thread()

        def blocked(*args, **kwargs):
            if current_thread() is not caller:
                entered.set()
                if not release.wait(1):
                    raise AssertionError("test did not release the read")
            return read(*args, **kwargs)

        with patch.object(window, "DISCOVERY_SECONDS", 0.05), patch.object(window, "_enabled", return_value=True), \
                patch.object(ledger, "read", side_effect=blocked), patch.object(window, "Thread", wraps=window.Thread) as start:
            try:
                first = self.listed()
                self.assertTrue(entered.is_set())
                second = self.listed()
                self.assert_budget(first[0])
                self.assert_budget(second[0])
                self.assertEqual(start.call_count, 1)
                # A later ordinary reader needs no lock held by the abandoned discovery.
                self.assertIsNotNone(ledger.read(self.project, "objective"))
            finally:
                release.set()
                window._DISCOVERY_WORKER.join(0.5)
        self.assertFalse(window._DISCOVERY_WORKER.is_alive())

    def test_initial_worker_exceptions_become_unknown_with_a_reason(self):
        for failure in (ValueError("private detail"), PodError("unsafe_config", "private detail"), SystemExit()):
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(window, "_enabled", side_effect=failure):
                result = self.listed()[0]["continuation"]
            self.assertEqual(result["status"], "unknown")
            self.assertIn("failed", result["message"])
            self.assertNotIn("private detail", result["message"])

    def test_row_worker_exceptions_become_unknown_with_a_reason(self):
        for target, failure in ((ledger, OSError("private detail")), (window, ValueError("private detail")),
                                (window, SystemExit())):
            with self.subTest(target=target.__name__), patch.object(target, "read" if target is ledger else "detect",
                                                                  side_effect=failure):
                result = self.listed()[0]["continuation"]
            self.assertEqual((result["status"], result["condition"]), ("unknown", "discovery_failed"))
            self.assertIn(type(failure).__name__, result["message"])
            self.assertNotIn("private detail", result["message"])

    def test_worker_start_failure_is_unknown(self):
        with patch.object(window, "Thread", side_effect=RuntimeError("private detail")):
            result = self.listed()[0]["continuation"]
        self.assertEqual((result["status"], result["condition"]), ("unknown", "discovery_failed"))
        self.assertIn("RuntimeError", result["message"])
        self.assertNotIn("private detail", result["message"])

    def test_discovery_writes_no_file_cache_or_temporary_and_takes_no_file_lock(self):
        path = ledger.objective_root(self.project, "synthetic-capacity") / "context.json"
        ledger._write(path, synthetic_state())
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        violations = []
        open_file, open_io, open_fd = builtins.open, io.open, os.open

        def forbidden(*args, **kwargs):
            violations.append([(Path(frame.filename).name, frame.name, frame.lineno)
                               for frame in traceback.extract_stack()[-10:]])
            raise AssertionError("discovery attempted a mutation or lock")

        def regular(function):
            def checked(file, mode="r", *args, **kwargs):
                if any(flag in mode for flag in "wax+"):
                    return forbidden()
                return function(file, mode, *args, **kwargs)
            return checked

        def descriptor(file, flags, *args, **kwargs):
            # subprocess opens the existing null device for stdin; it creates no file or persistent content.
            if file == os.devnull and not flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                return open_fd(file, flags, *args, **kwargs)
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                return forbidden()
            return open_fd(file, flags, *args, **kwargs)

        config._decoded.cache_clear()
        with ExitStack() as stack:
            stack.enter_context(patch.object(catalog, "_DEFAULT", None))
            stack.enter_context(patch.object(builtins, "open", side_effect=regular(open_file)))
            stack.enter_context(patch.object(io, "open", side_effect=regular(open_io)))
            stack.enter_context(patch.object(os, "open", side_effect=descriptor))
            for name in ("mkdir", "makedirs", "write", "replace", "rename", "unlink", "remove", "link",
                         "symlink", "truncate", "ftruncate"):
                stack.enter_context(patch.object(os, name, side_effect=forbidden))
            stack.enter_context(patch.object(fcntl, "flock", side_effect=forbidden))
            stack.enter_context(patch.object(github, "worktree_inventory", side_effect=forbidden))
            stack.enter_context(patch.object(orca, "mutate_command", side_effect=forbidden))
            chunks = stack.enter_context(patch.object(history.ChunkStore, "_load", autospec=True,
                                                       side_effect=history.ChunkStore._load))
            result = self.listed()[0]["continuation"]
        self.assertEqual(violations, [])
        self.assertEqual(result["status"], "available")
        self.assertGreater(chunks.call_count, 0)
        self.assertEqual({str(path.relative_to(self.root)): path.read_bytes()
                          for path in self.root.rglob("*") if path.is_file()}, before)
        self.assertEqual(self.mutations, [])

    def test_repository_deadline_is_rethrown_before_fallback_or_binding_refusal(self):
        failure = PodError("discovery_budget", "spent")
        with patch.object(github, "_local_git", side_effect=failure), \
                patch.object(Path, "exists", side_effect=AssertionError("no ancestor fallback")):
            with self.assertRaises(PodError) as caught:
                github._read_repository_context(self.project)
        self.assertIs(caught.exception, failure)
        local = github._local_git

        def remote(project, argv, **kwargs):
            if argv == ["remote", "get-url", "origin"]:
                raise failure
            return local(project, argv, **kwargs)

        with patch.object(github, "_local_git", side_effect=remote):
            result = self.listed()[0]["continuation"]
        self.assertEqual((result["status"], result["condition"]), ("unknown", "discovery_budget"))

    def test_request_target_deadline_is_not_translated_to_window_unverified(self):
        failure = PodError("discovery_budget", "spent")
        with patch.object(window, "_request", side_effect=failure):
            result = self.listed()[0]["continuation"]
        self.assertEqual((result["status"], result["condition"]), ("unknown", "discovery_budget"))
        self.assertEqual(self.host_requests, [])

    def test_unreadable_inventory_never_proves_a_worktree_refusal(self):
        local = github._local_git

        def oversized(project, argv, **kwargs):
            if argv == ["worktree", "list", "--porcelain", "-z"]:
                raise PodError("git_contract", "inventory exceeds output bound")
            return local(project, argv, **kwargs)

        with patch.object(github, "_local_git", side_effect=oversized):
            result = self.listed()[0]["continuation"]
        self.assertEqual((result["status"], result["condition"]), ("unknown", "git_contract"))

    def origin_exit(self, code, observe):
        run = subprocess.run
        failures = []

        def failed(argv, *args, **kwargs):
            if list(argv[-3:]) == ["remote", "get-url", "origin"]:
                failures.append(code)
                return subprocess.CompletedProcess(argv, code, stdout="", stderr="synthetic Git failure")
            return run(argv, *args, **kwargs)

        with patch.object(subprocess, "run", side_effect=failed):
            result = observe()
        self.assertTrue(failures)
        return result

    def test_fatal_origin_process_exit_lists_unknown(self):
        before = self.snapshot()
        result = self.origin_exit(128, lambda: self.listed()[0]["continuation"])
        self.assertEqual((result["status"], result["condition"]),
                         ("unknown", "repository_identity_unavailable"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.mutations, [])

    def test_absent_origin_process_exit_still_reads_no_origin(self):
        with orca.read_deadline(window.Budget(5)):
            result, complete = self.origin_exit(2, lambda: github._read_repository_context(self.project))
        self.assertIsNone(result["repository"])
        self.assertTrue(complete)
        self.assertEqual(result["worktree"], str(self.project))

    def test_unbudgeted_fatal_origin_keeps_the_ordinary_no_origin_result(self):
        result, complete = self.origin_exit(128, lambda: github._read_repository_context(self.project))
        self.assertIsNone(result["repository"])
        self.assertTrue(complete)
        with self.assertRaises(PodError) as caught:
            self.origin_exit(128, lambda: github._local_git(self.project, ["remote", "get-url", "origin"]))
        self.assertEqual(caught.exception.code, "repository_identity_unavailable")
        self.assertEqual(str(caught.exception), "Git repository identity could not be read")
        self.assertEqual(caught.exception.detail["returncode"], 128)

    def worktree_stat_failure(self, failure, *, skip=0, observe=None):
        original = os.stat
        observed = []

        def stat(path, *args, **kwargs):
            if (os.fspath(path) == str(self.project)
                    and sys._getframe(1).f_code.co_name in ("isdir", "_worktree_directory")):
                observed.append(failure.errno)
                if len(observed) > skip:
                    raise failure
            return original(path, *args, **kwargs)

        with patch.object(os, "stat", new=stat):
            result = observe() if observe else self.listed()[0]["continuation"]
        self.assertGreater(len(observed), skip)
        return result

    def test_denied_worktree_stat_lists_unknown(self):
        before = self.snapshot()
        result = self.worktree_stat_failure(PermissionError(errno.EACCES, "synthetic denied metadata"))
        self.assertEqual((result["status"], result["condition"]), ("unknown", "worktree_unreadable"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.mutations, [])

    def test_failed_worktree_stat_lists_unknown(self):
        result = self.worktree_stat_failure(OSError(errno.EIO, "synthetic failed metadata"))
        self.assertEqual((result["status"], result["condition"]), ("unknown", "worktree_unreadable"))

    def test_denied_recorded_worktree_stat_lists_unknown(self):
        result = self.worktree_stat_failure(PermissionError(errno.EACCES, "synthetic denied metadata"), skip=1)
        self.assertEqual((result["status"], result["condition"]), ("unknown", "worktree_unreadable"))

    def test_denied_worktree_stat_keeps_the_status_refusal(self):
        result = self.worktree_stat_failure(PermissionError(errno.EACCES, "synthetic denied metadata"),
                                            observe=lambda: self.status()["window_continuation"])
        self.assertEqual((result["status"], result["condition"]), ("window_unavailable", "worktree_changed"))

    def test_denied_worktree_stat_keeps_the_command_refusal(self):
        self.worktree_stat_failure(PermissionError(errno.EACCES, "synthetic denied metadata"),
                                   observe=lambda: self.assert_lead_refusal("worktree_changed"))

    def test_unreadable_evidence_closure_lists_unknown_not_available(self):
        from pod import evidence_closure
        original, observed = os.lstat, []

        def lstat(path, *args, **kwargs):
            if os.fspath(path).endswith(os.sep + evidence_closure.FILE):
                observed.append(path)
                raise PermissionError(errno.EACCES, "synthetic denied metadata")
            return original(path, *args, **kwargs)

        before = self.snapshot()
        with patch.object(os, "lstat", new=lstat):
            result = self.listed()[0]["continuation"]
        self.assertTrue(observed)
        self.assertEqual((result["status"], result["condition"]), ("unknown", "closure_unreadable"))
        self.assertEqual(self.host_requests, [])
        self.assertEqual(self.snapshot(), before)

    def test_a_truly_missing_worktree_still_lists_worktree_changed(self):
        state = self.state()
        state["checkpoint"]["worktree"]["path"] = str(self.root / "missing")
        with patch.object(ledger, "read", return_value=state):
            result = self.listed()[0]["continuation"]
        self.assertEqual((result["status"], result["condition"]), ("unavailable", "worktree_changed"))

    def test_a_non_directory_worktree_still_lists_worktree_changed(self):
        file = self.root / "not-a-directory"
        file.write_text("synthetic")
        for path in (file, file / "child"):
            with self.subTest(path=path.name):
                state = self.state()
                state["checkpoint"]["worktree"]["path"] = str(path)
                with patch.object(ledger, "read", return_value=state):
                    result = self.listed()[0]["continuation"]
                self.assertEqual((result["status"], result["condition"]), ("unavailable", "worktree_changed"))

    def test_denied_ancestor_git_marker_does_not_prove_a_non_repository(self):
        run, original = subprocess.run, os.stat
        observed = []

        def failed(argv, *args, **kwargs):
            if "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, 128, stdout="", stderr="synthetic Git failure")
            return run(argv, *args, **kwargs)

        def stat(path, *args, **kwargs):
            if os.fspath(path) == str(self.project / ".git"):
                observed.append(errno.EACCES)
                raise PermissionError(errno.EACCES, "synthetic denied marker")
            return original(path, *args, **kwargs)

        with orca.read_deadline(window.Budget(5)), patch.object(subprocess, "run", side_effect=failed), \
                patch.object(os, "stat", new=stat), self.assertRaises(OSError) as caught:
            github._read_repository_context(self.project)
        self.assertTrue(observed)
        self.assertEqual(caught.exception.errno, errno.EACCES)

    def test_in_memory_inventory_preserves_control_characters_in_worktree_paths(self):
        linked = self.root / "linked\r\nworktree"
        git(self.project, "worktree", "add", "-qb", "linked", str(linked))
        ordinary = github.repository_context(self.project)
        with orca.read_deadline(window.Budget(5)), \
                patch.object(github, "worktree_inventory", side_effect=AssertionError("no temporary spool")):
            discovered = github.repository_context(self.project)
        self.assertEqual(discovered, ordinary)

    def test_late_repository_identity_is_not_compared_as_a_definite_binding(self):
        from pod.operations import _check_worktree_binding
        now, Clock = self.clock()
        binding = self.binding()

        def context(*args, **kwargs):
            now["value"] = window.DISCOVERY_SECONDS
            return {"repository": "different"}

        with orca.read_deadline(Clock(window.DISCOVERY_SECONDS)), \
                patch.object(github, "repository_context", side_effect=context), self.assertRaises(PodError) as caught:
            _check_worktree_binding(self.project, binding)
        self.assertEqual(caught.exception.code, "discovery_budget")

    def test_repeated_catalog_reads_can_finish_late_without_publishing_a_verdict(self):
        now, Clock = self.clock()
        stat = Path.stat
        starts = []

        def expensive(path, *args, **kwargs):
            if path == catalog.CATALOG_PATH:
                starts.append(now["value"])
                now["value"] += 3
            return stat(path, *args, **kwargs)

        with patch.object(window, "Budget", Clock), patch.object(Path, "stat", expensive):
            rows = self.listed()
        self.assertTrue(starts)
        self.assert_budget(rows[0])
