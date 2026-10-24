"""Unknown personal authority stays unknown at public read-only projections."""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pod import config, display, internal, readiness
from pod.cli import execute, main, parser
from pod.errors import PodError
from tests.decision_policy_support import MachineCase
from tests.test_display import inventories, state_fixture


class UnknownDecisionViewsTests(MachineCase, unittest.TestCase):
    def unavailable(self, view, reason):
        self.assertEqual(view["decision_policy"], {"status": "unavailable", "reason": reason})
        self.assertEqual(view["waiting_decisions"]["status"], "unavailable")
        self.assertEqual(view["waiting_decisions"]["reason"], reason)
        self.assertIsNone(view["waiting_decisions"]["total"])
        self.assertIsNone(view["tool_permissions"])
        self.assertIsNone(view["needs_you"])
        self.assertIsNone(view["setup_question"])

    def public_views(self, reason):
        for command in ("config", "policy"):
            with self.subTest(command=command):
                self.unavailable(execute(parser().parse_args([command, "--json"]), None), reason)
                output = StringIO()
                with redirect_stdout(output), patch("sys.stdin.isatty", return_value=False):
                    self.assertEqual(main([command]), 1 if command == "config" else 0)
                text = output.getvalue().lower()
                self.assertIn("decision waits: unavailable", text)
                self.assertIn(reason, text)
                self.assertNotIn("decision policy: careful", text)
                self.assertNotIn("who should decide", text)

    def test_unsupported_schema_is_unavailable_in_public_json_and_text_without_writes(self):
        path = config.personal_path()
        path.write_text("schema: broken-fixture-schema\n")
        before = path.read_bytes()
        reason = config.load()["errors"][0]["code"]
        self.public_views(reason)
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_yaml_is_unavailable_without_false_setup_or_wait_facts(self):
        path = config.personal_path()
        path.write_text("schema: [\n")
        before = path.read_bytes()
        self.public_views("invalid_yaml")
        self.assertEqual(path.read_bytes(), before)

    def test_unreadable_config_is_unavailable_instead_of_a_read_error_or_setup_offer(self):
        path = config.personal_path()
        before = path.read_bytes()
        read = config._read_bytes
        def unavailable(selected):
            if selected == path:
                raise PodError("unsafe_config", "private fixture diagnostic")
            return read(selected)
        with patch.object(config, "_read_bytes", side_effect=unavailable):
            self.public_views("unsafe_config")
        self.assertEqual(path.read_bytes(), before)

    def test_unsafe_config_symlink_is_unavailable_and_target_bytes_are_unchanged(self):
        path = config.personal_path()
        target = self.root / "private-config.yaml"
        path.rename(target)
        before = target.read_bytes()
        path.symlink_to(target)
        self.public_views("unsafe_config")
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_bytes(), before)

    def test_missing_file_and_readable_absent_policy_keep_implicit_careful_offer(self):
        for missing in (False, True):
            if missing:
                config.personal_path().unlink()
            for command in ("config", "policy"):
                with self.subTest(missing=missing, command=command):
                    view = execute(parser().parse_args([command, "--json"]), None)
                    self.assertEqual(view["decision_policy"]["preset"], "careful")
                    self.assertEqual(view["waiting_decisions"]["total"], 0)
                    self.assertFalse(view["needs_you"])
                    self.assertIsNotNone(view["setup_question"])
        self.assertFalse(config.personal_path().exists())

    def test_readable_v1_setup_keeps_implicit_careful_without_rewriting_old_bytes(self):
        path = config.personal_path()
        raw = (Path(__file__).parent / "fixtures/pod-0.6.7/config-default.yaml").read_bytes()
        path.write_bytes(raw)
        for command in ("config", "policy"):
            view = execute(parser().parse_args([command, "--json"]), None)
            self.assertEqual(view["decision_policy"]["preset"], "careful")
            self.assertFalse(view["needs_you"])
            self.assertIsNotNone(view["setup_question"])
        self.assertEqual(path.read_bytes(), raw)

    def test_invalid_config_never_offers_interactive_policy_setup(self):
        path = config.personal_path()
        path.write_text("schema: broken-fixture-schema\n")
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input") as prompt:
            view = execute(parser().parse_args(["policy"]), None)
        prompt.assert_not_called()
        self.unavailable(view, "invalid_config")

    def test_shared_none_and_invalid_snapshots_explain_the_same_unknown_facts(self):
        self.unavailable(readiness.decision_view(None), "decision_input_unavailable")
        data = {"status": "invalid", "reason": "unsafe_config", "policy": None,
                "permissions": [], "pending": [], "generation": 0}
        before = deepcopy(data)
        self.unavailable(readiness.decision_view(data), "unsafe_config")
        self.assertEqual(data, before)
        broken = {"permissions": None}
        projected = readiness.decision_view(broken)
        self.unavailable(projected, projected["decision_policy"]["reason"])

    def test_display_boundary_carries_invalidity_transiently_without_native_writes(self):
        path = config.personal_path()
        path.write_text("schema: broken-fixture-schema\n")
        before_config = path.read_bytes()
        state = state_fixture()
        state["checkpoint"]["questions"] = [{"id": "owner-choice", "state": "pending", "categories": ["secrets"]}]
        before = deepcopy(state)
        terms, trees = inventories(state)
        reads = [terms, trees, {"version": "synthetic-control"}]
        supplied = []
        project = display.projection
        def observe(transient, *facts):
            supplied.append(deepcopy(transient))
            return project(transient, *facts)
        with patch("pod.ledger.read", return_value=state), patch("pod.orca.read_command", side_effect=reads) as native, patch("pod.display.projection", side_effect=observe):
            result = internal.run("display", {"project": str(self.root), "objective": "objective"})
        reason = config.load()["errors"][0]["code"]
        self.unavailable(result, reason)
        self.assertEqual(supplied[0]["decision_input"]["status"], "invalid")
        self.assertEqual(result["rows"][1]["expected"]["title"], "Coordinator")
        self.assertNotIn("needs you", json.dumps(result).lower())
        self.assertEqual(state, before)
        self.assertEqual(path.read_bytes(), before_config)
        self.assertEqual(native.call_count, 3)


if __name__ == "__main__":
    unittest.main()
