import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.orca import account_metadata, effective_launch, read_command, worker_rows


class OrcaAdapterTests(unittest.TestCase):
    def test_pod_location_overrides_do_not_replace_native_subprocess_profile(self):
        native = {
                  "CODEX_HOME": "native-codex", "CLAUDE_CONFIG_DIR": "native-claude"}
        overrides = {"POD_CONFIG_HOME": "/isolated/pod-config", "POD_STATE_HOME": "/isolated/pod-state"}

        def completed(*args, **kwargs):
            self.assertNotIn("env", kwargs)
            self.assertEqual({key: os.environ[key] for key in native}, native)
            return subprocess.CompletedProcess(args[0], 0, "orca 1.0\n", "")

        with patch.dict(os.environ, {**native, **overrides}), \
             patch("pod.orca.executable", return_value=Path("/native/orca")), \
             patch("pod.orca.subprocess.run", side_effect=completed):
            self.assertEqual(read_command(["--version"])["version"], "orca 1.0")

    def test_read_adapter_rejects_consuming_and_guessed_verbs_before_process(self):
        with patch("pod.orca.subprocess.run") as runner:
            for argv in (
                ["orchestration", "check", "--json"],
                ["orchestration", "check", "--ack", "delivery", "--json"],
                ["orchestration", "dispatch-list", "--json"],
                ["orchestration", "worker-list", "--include-remote", "--json", "--run", "r", "--ack", "d"],
            ):
                with self.assertRaises(PodError):
                    read_command(argv)
            runner.assert_not_called()

    def test_paginated_worker_list_requires_stable_runtime_and_complete_pages(self):
        pages = [
            {"runtime": "r", "result": {"scope": {"source": "all"}, "workers": [{"dispatchId": "a"}],
                                         "page": {"hasMore": True, "nextCursor": "next"}}},
            {"runtime": "r", "result": {"scope": {"source": "all"}, "workers": [{"dispatchId": "b"}],
                                         "page": {"hasMore": False}}},
        ]
        with patch("pod.orca.read_command", side_effect=pages) as reader:
            fleet = worker_rows()
            self.assertEqual([x["dispatchId"] for x in fleet["workers"]], ["a", "b"])
            self.assertTrue(fleet["complete"])
            self.assertIn("--cursor", reader.call_args.args[0])

    def test_cached_metadata_redacts_accounts_and_keeps_timestamps(self):
        result = {"runtime": "r", "result": {"claude": [{"token": "secret"}], "rateLimits": {
            "claude": {"status": "ok", "updatedAt": 1000,
                       "session": {"usedPercent": 20, "windowMinutes": 300, "resetsAt": 2000},
                       "usageMetadata": {"private": "secret"}},
            "codex": {"status": "unknown", "updatedAt": None}}}}
        with patch("pod.orca.read_command", return_value=result):
            metadata = account_metadata()
        self.assertEqual(metadata["providers"]["claude"]["updated_at_ms"], 1000)
        self.assertEqual(metadata["providers"]["claude"]["freshness"], "stale")
        self.assertEqual(metadata["providers"]["claude"]["windows"]["session"]["usedPercent"], 20)
        self.assertNotIn("secret", json.dumps(metadata))
        self.assertEqual(metadata["providers"]["claude"]["account_association"], "host_login")

    def test_actual_receipt_shape_and_optional_terminal(self):
        request = {"agent": "codex", "model": "m", "effort": "high"}
        effective_launch(request, {"state": "ready", "runId": "r", "taskId": "t", "dispatchId": "d",
                                   "launch": {"requested": request, "effective": request}})
        with self.assertRaises(PodError):
            effective_launch(request, {"state": "ready", "runId": "r", "taskId": "t", "dispatchId": "d",
                                       "launch": {"requested": request, "effective": {**request, "effort": "medium"}}})
