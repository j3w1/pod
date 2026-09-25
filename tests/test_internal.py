"""Characterization of the private operation boundary."""

from __future__ import annotations

import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from unittest.mock import patch

from pod.errors import PodError
from pod.internal import main, run


OPERATIONS = ("brief", "packet", "report", "source", "verify-sources", "acceptance",
              "integration-observe", "issue-intake", "issue-recheck", "project-context",
              "checkpoint", "admission", "constraint", "route-failure", "governor",
              "governor-outcome", "governor-prepare", "governor-preflight",
              "governor-classify", "governor-correct", "governor-execute",
              "governor-reconcile", "governor-status")


class InternalErrorBoundaryTests(unittest.TestCase):
    def test_each_operation_rejects_an_empty_request_with_its_existing_code(self):
        for operation in OPERATIONS:
            with self.subTest(operation=operation), self.assertRaises(PodError) as caught:
                run(operation, {})
            self.assertEqual(caught.exception.code,
                             "invalid_packet" if operation == "packet" else "invalid_request")

    def test_unexpected_failure_has_bounded_envelope_and_stderr_traceback(self):
        output, errors = StringIO(), StringIO()
        with patch("pod.internal.bounded_stdin_json", side_effect=RuntimeError("private detail")), \
             redirect_stdout(output), redirect_stderr(errors):
            exit_code = main(["source", "--input", "-"])
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue()),
                         {"schema": "pod-cli/v4", "status": "blocked",
                          "error": {"code": "internal_error", "message": "Internal helper failed (RuntimeError)"}})
        self.assertIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
