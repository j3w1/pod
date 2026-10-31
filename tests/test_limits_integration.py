"""Configured request/context bounds and retained-history safety in disposable stores."""

from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from pod.config import defaults, load, personal_path, set_refresh, validate
from pod.errors import PodError
from pod.history import context_limit, load_context, publish_context
from pod.ledger import _empty
from pod.limits import DEFAULT_LIMITS, HARD_LIMITS, Limits, configured, current, using_limits
from pod.util import atomic_json, bounded_json, bounded_stdin_json, encoded
from tests.common import fixture
from tests.issue41_support import ProductionCase


class ConfiguredLimitsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture()
        self.root = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        self.path = personal_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.document = defaults()
        self.save()

    def save(self):
        self.path.write_text(yaml.safe_dump(self.document, sort_keys=False))

    def test_absent_key_preserves_existing_configuration_and_selects_defaults(self):
        before = self.path.read_bytes()
        self.assertEqual(load()["limits"], DEFAULT_LIMITS.as_dict())
        with configured():
            self.assertEqual(current(), DEFAULT_LIMITS)
        self.assertEqual(self.path.read_bytes(), before)

    def test_configured_snapshot_survives_unrelated_preference_edit(self):
        self.document["limits"] = {"record_bytes": 65536, "findings": 256}
        self.save()
        routes = deepcopy(self.document["routes"])
        shown = load()
        set_refresh(self.path, "manual", displayed=shown)
        self.assertEqual(yaml.safe_load(self.path.read_text())["limits"], self.document["limits"])
        self.assertEqual(load()["routes"], routes)
        with configured():
            self.assertEqual((current().record_bytes, current().findings), (65536, 256))

    def test_invalid_limits_hold_mutation_and_preserve_file_bytes(self):
        self.document["limits"] = {"record_bytes": HARD_LIMITS["record_bytes"] + 1}
        self.save()
        before = self.path.read_bytes()
        self.assertEqual(load()["status"], "invalid")
        with self.assertRaises(PodError) as caught:
            with configured():
                self.fail("invalid limits must not enter an operation")
        self.assertEqual(caught.exception.code, "invalid_limits")
        self.assertEqual(self.path.read_bytes(), before)
        with configured(strict=False):
            self.assertEqual(current(), DEFAULT_LIMITS)

    def test_project_cannot_set_personal_limits(self):
        with self.assertRaises(PodError):
            validate({"schema": "pod/v1", "limits": {"findings": 512}}, project=True)

    def test_unreadable_limits_do_not_become_a_mutation_default_in_nested_helpers(self):
        self.path.write_text("schema: [invalid YAML\n")
        before = self.path.read_bytes()
        with configured(strict=False):
            with self.assertRaises(PodError):
                with configured():
                    self.fail("a diagnostic snapshot cannot authorize mutation")
        self.assertEqual(self.path.read_bytes(), before)

    def test_routing_error_does_not_widen_an_independently_validated_limit(self):
        self.document["limits"] = {"record_bytes": 64}
        self.document["pinned"] = "codex/not-a-supported-route/high"
        self.save()
        snapshot = load()
        self.assertEqual(snapshot["status"], "invalid")
        self.assertEqual(snapshot["limits_status"], "validated")
        with configured():
            self.assertEqual(current().record_bytes, 64)
            with self.assertRaises(PodError):
                atomic_json(self.root / "too-large.json", "x" * 65)

    def test_changed_limits_refuse_publication_without_creating_a_record(self):
        self.document["limits"] = {"record_bytes": 4096}
        self.save()
        output = self.root / "record.json"
        with configured():
            self.document["limits"]["record_bytes"] = 8192
            self.save()
            with configured():
                self.assertEqual(current().record_bytes, 4096)
            with self.assertRaises(PodError) as caught:
                atomic_json(output, {"value": "bounded"})
            self.assertEqual(caught.exception.code, "limits_changed")
        self.assertFalse(output.exists())
        with configured():
            self.assertEqual(current().record_bytes, 8192)

    def test_new_personal_file_during_default_operation_is_rechecked(self):
        self.path.unlink()
        output = self.root / "record.json"
        with configured():
            self.document["limits"] = {"record_bytes": 64}
            self.save()
            with self.assertRaises(PodError) as caught:
                atomic_json(output, {"value": "bounded"})
            self.assertEqual(caught.exception.code, "limits_changed")
        self.assertFalse(output.exists())

    def test_limits_recheck_precedes_decision_receipt_consumption(self):
        from pod.relay import publish_decision
        output = self.root / "context.json"
        with configured(), patch("pod.relay.current_decision", side_effect=AssertionError("receipt touched")):
            self.document["limits"] = {"record_bytes": 64}
            self.save()
            with self.assertRaises(PodError) as caught:
                publish_decision(output, {}, lambda: self.fail("stale publication"))
            self.assertEqual(caught.exception.code, "limits_changed")
        self.assertFalse(output.exists())

    def test_request_and_write_boundaries_use_the_same_snapshot(self):
        self.document["limits"] = {"record_bytes": 64}
        self.save()
        with configured():
            for payload_size in (63, 64, 65):
                # JSON string plus quotes and LF has exactly payload_size bytes.
                value = "x" * (payload_size - 3)
                raw = encoded(value, compact=True)
                self.assertEqual(len(raw), payload_size)
                stream = io.TextIOWrapper(io.BytesIO(raw))
                with self.subTest(bytes=payload_size), patch("sys.stdin", stream):
                    output = self.root / f"request-{payload_size}.json"
                    if payload_size <= 64:
                        self.assertEqual(bounded_stdin_json(), value)
                        atomic_json(output, value, compact=True)
                        self.assertEqual(output.read_bytes(), raw)
                    else:
                        with self.assertRaises(PodError):
                            bounded_stdin_json()
                        with self.assertRaises(PodError):
                            atomic_json(output, value, compact=True)
                        self.assertFalse(output.exists())


class ContextLimitsTests(unittest.TestCase):
    def test_default_accepts_existing_context_above_the_previous_half_megabyte(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            state = _empty()
            state["source_rejections"]["synthetic"] = "x" * (600 * 1024)
            publish_context(path, state, {})
            self.assertEqual(load_context(path), state)

    def test_reduced_soft_limit_keeps_old_context_readable_but_blocks_large_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            state = _empty()
            state["source_rejections"]["synthetic"] = "x" * 4096
            publish_context(path, state, {})
            before = path.read_bytes()
            with using_limits(Limits(context_bytes=1024)):
                self.assertEqual(context_limit(), 1024)
                self.assertEqual(load_context(path), state)
                with self.assertRaises(PodError) as caught:
                    publish_context(path, state, {})
                self.assertEqual(caught.exception.detail["limit_bytes"], 1024)
            self.assertEqual(path.read_bytes(), before)

    def test_context_limit_is_independent_of_request_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            state = _empty()
            state["source_rejections"]["synthetic"] = "x" * 2048
            with using_limits(Limits(record_bytes=512, context_bytes=8192)):
                publish_context(path, state, {})
                self.assertEqual(load_context(path), state)
                self.assertEqual(bounded_json(path), state)


class ClosureContextLimitsTests(unittest.TestCase):
    def test_tiny_close_request_can_materialize_over_half_megabyte_without_losing_history(self):
        case = ProductionCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.intake()
        case.write([case.criterion(state="satisfied", evidence=[case.proof("O1")])])
        from pod.ledger import _path, _read, _write, read
        from pod.internal import run
        path = _path(case.project, "objective")
        state = read(case.project, "objective")
        # Sanitized earlier accepted decision shapes, never person/native proof.
        history = [{"seq": index + 1, "kind": "revision", "provenance": "user_direct", "channel": "session",
                    "instruction": "synthetic retained decision " + "x" * 960} for index in range(400)]
        state["checkpoint"]["seq"] = 401
        state["checkpoint"]["revision"] = 401
        state["checkpoint"]["revision_decisions"] = history
        _write(path, state)
        before = _read(path)
        request = {"project": str(case.project), "objective": "objective", "owner": "owner",
                   "value": {"schema": "pod-checkpoint/v3", "seq": 402, "close": True}}
        self.assertLess(len(encoded(request, compact=True)), 4096)
        run("checkpoint", request)
        after = read(case.project, "objective")
        self.assertTrue(after["checkpoint"]["closure"])
        self.assertGreater(path.stat().st_size, 512 * 1024)
        self.assertLessEqual(path.stat().st_size, DEFAULT_LIMITS.context_bytes)
        self.assertEqual(after["checkpoint"]["revision_decisions"], history)
        self.assertEqual(after["checkpoint"]["obligations"], before["checkpoint"]["obligations"])
        self.assertEqual(after["checkpoint"]["candidate"], before["checkpoint"]["candidate"])
        self.assertEqual(after["checkpoint"]["native_refs"], before["checkpoint"]["native_refs"])
        self.assertNotIn("capacity_continued_by", after)
