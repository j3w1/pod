#!/usr/bin/env python3
"""Scripted record checks over sanitized recorded coordination traces (R91).

Each argument is one trace file. Slot filling, amplification and scope inflation are
behaviours Pod's boundaries refuse, so any occurrence fails the run. Serialization and
churn are flags; wall time and admission counts are observations and never gate. A trace
without obligation maps, such as a 0.5 native capture, is reported as not evaluable.
There is no paid or live run here: the checks read files only.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Name one or more trace files.")
        return 2
    sys.path.insert(0, str(ROOT / "skills"))
    from pod.errors import PodError
    from pod.traces import evaluate_trace
    failed = False
    results = {}
    for name in argv[1:]:
        try:
            trace = json.loads(Path(name).read_text(encoding="utf-8"))
            results[name] = evaluate_trace(trace)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, PodError) as exc:
            results[name] = {"failed": True, "error": type(exc).__name__}
        failed = failed or bool(results[name].get("failed"))
    print(json.dumps(results, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
