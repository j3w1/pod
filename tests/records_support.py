"""Preceding admission fixtures; current reporting and persistence remain under test."""
import ast
from functools import cache
from pathlib import Path
import subprocess
from unittest.mock import patch

from pod import obligations


@cache
def admission_before_reservations():
    """Load the genuine 0.21.11 admission function for already-admitted fixtures.

    Older admissions have no capacity reservations. Retain the report overflow
    regressions for those records without relaxing current admission assertions.
    This is a synthetic fixture using the earlier function and current helpers,
    not a claim that a complete preceding bundle performed the write.
    """
    source = subprocess.run(["git", "show", "e443cf4:skills/pod/obligations.py"],
                            cwd=Path(__file__).resolve().parents[1], check=True,
                            capture_output=True, text=True).stdout
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "admission_refusal")
    namespace = dict(vars(obligations))
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<preceding admission fixture>", "exec"), namespace)
    return namespace["admission_refusal"]


def start_before_reservations(case, name, packet):
    with patch("pod.obligations.admission_refusal", admission_before_reservations()):
        return case.start(name, packet)
