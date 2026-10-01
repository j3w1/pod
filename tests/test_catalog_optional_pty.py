"""A copied bundle stays browsable when an AA observation row is missing."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

from tests.pty_harness import ROOT, PtyCase, environment


class OptionalObservationPtyTests(PtyCase):
    def test_missing_observation_row_renders_unknown_and_reads_still_work(self):
        copied = self.root / "copy" / "pod"
        shutil.copytree(ROOT / "skills" / "pod", copied, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        path = copied / "observations.json"
        document = json.loads(path.read_text())
        rows = document["sources"]["artificial_analysis"]["rows"]
        document["sources"]["artificial_analysis"]["rows"] = [row for row in rows if row["name"] != "GPT-6 Luna (high)"]
        self.assertLess(len(document["sources"]["artificial_analysis"]["rows"]), len(rows))
        path.write_text(json.dumps(document, indent=2, sort_keys=True))
        launcher = copied / "scripts" / "pod.py"
        from tests.pty_harness import PtySession
        with PtySession(home=self.home, cwd=self.work, launcher=launcher, cols=100, rows=30) as screen:
            screen.wait_for("[Det", timeout=6)
            screen.send("/gpt-6-luna/high\r")
            screen.wait_for(lambda s: any(line.startswith("▸") and "GPT-6 Luna" in line and " high " in line
                                          and "—" in line for line in s.lines()))
            self.assertIn("1 rows", screen.text())
            screen.send("]")
            screen.wait_for("no AA row maps to this route; metrics unknown")
        for argv in (["config", "--json"], ["models", "--json"], ["doctor", "--json"]):
            result = subprocess.run([sys.executable, "-I", str(launcher), *argv], cwd=self.work,
                                    env=environment(self.home), capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, argv + [result.stdout + result.stderr])
            json.loads(result.stdout)
