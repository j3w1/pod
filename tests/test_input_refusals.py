"""A4: exact() refusals name the operation, record and fields, never a value."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from pod.errors import FieldRefusal, PodError
from pod.internal import _OPERATIONS, main, run
from pod.obligations import _exact
from pod.records import PACKET_REQUIRED
from pod.util import MAX_FIELD_NAME, MAX_NAMED_FIELDS, exact

SENTINEL = "SENTINEL-VALUE-7f3c9e"
OWNED = ("project", "objective", "owner")
# Required request fields of every `pod internal` operation; a new operation needs its row.
REQUIRED = {
    "brief": ("criteria", "coverage"),
    "packet": tuple(sorted(PACKET_REQUIRED)),
    "report": ("report", "packet", "project", "objective", "admission_id"),
    "source": ("project", "path"),
    "verify-sources": ("project", "sources"),
    "issue-intake": ("project", "locator"),
    "issue-recheck": ("project", "source"),
    "project-context": ("project",),
    "acceptance": ("criteria", "evidence_rows", "candidate", "policy_revision", "sources",
                   "dependencies", "environment", "review_required", "hosted_required"),
    "integration-observe": ("project", "candidate"),
    "checkpoint": (*OWNED, "value"),
    "constraint": (*OWNED, "action"),
    "route-failure": (*OWNED, "admission_id", "kind", "source"),
    "governor": (*OWNED, "action"),
    "governor-outcome": (*OWNED, "record_id", "outcome"),
    "governor-prepare": (*OWNED, "unit"),
    "governor-preflight": (*OWNED, "unit", "candidate", "check", "status"),
    "governor-classify": (*OWNED, "record_id", "classification"),
    "governor-correct": (*OWNED, "unit", "correction"),
    "governor-execute": (*OWNED, "action"),
    "governor-reconcile": (*OWNED, "record_id"),
    "governor-status": OWNED,
    "admission": (*OWNED, "run", "task", "plan_revision", "packet"),
    "cleanup-plan": ("project", "objective"),
    "map": ("project", "objective"),
    "runtime-continuity": (*OWNED, "decision"),
    "owner-handoff": (*OWNED, "decision"),
}


class OperationFieldRefusalTests(unittest.TestCase):
    def invoke(self, operation: str, request: dict) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "request.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = main([operation, "--input", str(path)])
        return code, output.getvalue(), errors.getvalue()

    def test_every_operation_names_operation_record_and_both_fields_without_values(self):
        self.assertEqual(set(REQUIRED), set(_OPERATIONS))
        for operation, required in REQUIRED.items():
            dropped = required[-1]
            request = {key: SENTINEL for key in required if key != dropped}
            request["unexpected_field"] = SENTINEL
            with self.subTest(operation=operation):
                code, stdout, stderr = self.invoke(operation, request)
                self.assertEqual(code, 1)
                envelope = json.loads(stdout)
                record = "packet" if operation == "packet" else "request"
                self.assertEqual(envelope["error"]["code"], f"invalid_{record}")
                message = envelope["error"]["message"]
                for name in (operation, record, dropped, "unexpected_field"):
                    self.assertIn(name, message)
                self.assertEqual(envelope["error"]["detail"],
                                 {"operation": operation, "record": record, "missing": [dropped],
                                  "unsupported": ["unexpected_field"], "omitted": 0,
                                  "accepted": envelope["error"]["detail"]["accepted"],
                                  "optional": envelope["error"]["detail"]["optional"]})
                shape = envelope["error"]["detail"]
                self.assertEqual(set(shape["accepted"]) - set(shape["optional"]), set(required))
                self.assertEqual(shape["accepted"], sorted(shape["accepted"]))
                self.assertNotIn(SENTINEL, stdout + stderr)

    def test_non_object_names_only_the_expected_type(self):
        code, stdout, stderr = self.invoke("map", [SENTINEL])
        error = json.loads(stdout)["error"]
        self.assertEqual((code, error["code"]), (1, "invalid_request"))
        self.assertEqual(error["detail"], {"operation": "map", "record": "request", "expected": "object",
                                         "accepted": ["objective", "project"], "optional": []})
        self.assertNotIn(SENTINEL, stdout + stderr)


class ExactRefusalTests(unittest.TestCase):
    def test_constant_shape_names_are_complete_through_both_refusal_paths(self):
        fields = {f"field{index:02d}" for index in range(24)} | {"clean\u202ename"}
        required = {"field00", "field01"}
        expected = sorted({name.replace("\u202e", "") for name in fields})
        for check in (exact, _exact):
            for value in ({"unexpected": SENTINEL}, None):
                with self.subTest(check=check.__name__, value=value), self.assertRaises(PodError) as caught:
                    check(value, fields, required, "record") if check is _exact else check(value, fields, required, name="record")
                detail = caught.exception.detail
                shape = detail["referent"] if check is _exact else detail
                self.assertEqual(shape["accepted"], expected)
                self.assertEqual(shape["optional"], [name for name in expected if name not in required])
                self.assertNotIn(SENTINEL, str(caught.exception) + json.dumps(detail))
                self.assertNotIn("\u202e", str(caught.exception))

    def test_large_real_obligation_shape_names_every_optional_field(self):
        from pod.obligations import _structure, _OBLIGATION_FIELDS
        with self.assertRaises(PodError) as caught:
            _structure({"wrong": SENTINEL})
        referent = caught.exception.detail["referent"]
        self.assertEqual(set(referent["accepted"]), _OBLIGATION_FIELDS)
        self.assertEqual(set(referent["optional"]), _OBLIGATION_FIELDS - {"id", "kind", "provenance"})
    def test_echoed_names_are_bounded_and_stripped_of_control_characters(self):
        value = {f"extra{index:02d}": SENTINEL for index in range(MAX_NAMED_FIELDS + 3)}
        value["bad\x1b[31m‮name" + "x" * 200] = SENTINEL
        with self.assertRaises(FieldRefusal) as caught:
            exact(value, {"known"}, {"known"}, name="record")
        detail = caught.exception.detail
        self.assertEqual(detail["missing"], ["known"])
        self.assertEqual(len(detail["unsupported"]), MAX_NAMED_FIELDS)
        self.assertEqual(detail["omitted"], 4)
        self.assertTrue(all(len(name) <= MAX_FIELD_NAME for name in detail["unsupported"]))
        self.assertIn("bad[31mname" + "x" * (MAX_FIELD_NAME - 11), detail["unsupported"])
        self.assertNotIn("\x1b", str(caught.exception))
        self.assertNotIn(SENTINEL, str(caught.exception) + json.dumps(detail))
        self.assertIn("(+4 more)", str(caught.exception))

    def test_acceptance_and_codes_are_unchanged(self):
        accepted = {"a": 1, "b": None}
        self.assertIs(exact(accepted, {"a", "b", "c"}, {"a"}, name="thing"), accepted)
        for value in ({}, {"a": 1, "z": 2}, "text", None, [1]):
            with self.subTest(value=value), self.assertRaises(PodError) as caught:
                exact(value, {"a", "b"}, {"a"}, name="thing")
            self.assertEqual(caught.exception.code, "invalid_thing")

    def test_caller_supplied_error_keeps_obligation_refusals_unchanged(self):
        with self.assertRaises(PodError) as caught:
            _exact({"unknown": SENTINEL}, {"id"}, {"id"}, "obligation")
        self.assertNotIsInstance(caught.exception, FieldRefusal)
        self.assertEqual(caught.exception.code, "obligation_invalid")
        self.assertTrue(str(caught.exception).startswith(
            "malformed: obligation has missing or unsupported fields:"))
        self.assertEqual(caught.exception.detail["detail"], "malformed")
        self.assertEqual(caught.exception.detail["referent"]["unsupported"], ["unknown"])
        self.assertEqual(caught.exception.detail["referent"]["missing"], ["id"])

    def test_other_invalid_refusals_do_not_gain_an_operation(self):
        with self.assertRaises(PodError) as caught:
            run("source", {"project": "/nonexistent", "path": ""})
        self.assertNotIsInstance(caught.exception, FieldRefusal)
        self.assertNotIn("operation", caught.exception.detail or {})


if __name__ == "__main__":
    unittest.main()
