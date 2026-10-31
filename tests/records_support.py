"""Preceding admission fixtures; current reporting and persistence remain under test."""
import ast
from functools import cache
from pathlib import Path
import subprocess
from unittest.mock import patch

from pod import obligations


@cache
def _admission_function(revision):
    source = subprocess.run(["git", "show", f"{revision}:skills/pod/obligations.py"],
                            cwd=Path(__file__).resolve().parents[1], check=True,
                            capture_output=True, text=True).stdout
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "admission_refusal")
    namespace = dict(vars(obligations))
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<preceding admission fixture>", "exec"), namespace)
    return namespace["admission_refusal"]


def admission_before_reservations():
    """Load the genuine 0.21.11 admission function for already-admitted fixtures.

    Older admissions have no capacity reservations. Retain the report overflow
    regressions for those records without relaxing current admission assertions.
    This is a synthetic fixture using the earlier function and current helpers,
    not a claim that a complete preceding bundle performed the write.
    """
    return _admission_function("e443cf4")


def start_before_reservations(case, name, packet):
    with patch("pod.obligations.admission_refusal", admission_before_reservations()):
        return case.start(name, packet)


def start_before_review_guard(case, name, packet):
    """Synthetic older admission using 0.21.18's genuine function and current persistence.

    This preserves report/checkpoint regressions for previously admitted reviews;
    it is neither current admission behavior nor a complete earlier-writer trial.
    """
    with patch("pod.obligations.admission_refusal", _admission_function("4830f224")):
        return case.start(name, packet)


@cache
def _triage_before_goal_links():
    """Exact preceding triage function with current helpers, a synthetic older-shape control."""
    from pod import assurance
    source = subprocess.check_output(["git", "show", "deda03a2:skills/pod/assurance.py"], text=True)
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "triage")
    namespace = dict(vars(assurance))
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<preceding triage fixture>", "exec"), namespace)
    return namespace["triage"]


def report_before_goal_links(case, admission, packet, **fields):
    """Preserve old finding bytes for genuine preceding-reader comparisons; not current triage proof."""
    with patch("pod.obligations.triage", _triage_before_goal_links()):
        return case.report(admission, packet, **fields)
