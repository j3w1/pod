"""A sparse pod/v2 route map stays sparse through a real-terminal edit and a restart."""

from __future__ import annotations

from pod.config import load
from tests.pty_harness import PtyCase

SPARSE = ("schema: pod/v2 # keep this comment\n"
          "routes:\n"
          "  codex/gpt-6-luna/low: enabled # only explicit choice\n"
          "workers:\n"
          "  max_active: 2\n"
          "refresh: manual\n")


class SparseTogglePtyTests(PtyCase):
    def test_terra_toggles_and_restarts_while_native_access_is_unknown(self):
        self.config.write_text(SPARSE)
        first = self.open(cols=100, rows=30)
        first.send("/gpt-6.1-terra/high\r")
        first.wait_for("GPT-6.1 Terra")
        self.assertIn("Native access unknown", first.text())
        self.assertNotIn('codex/gpt-6.1-terra/high', load(personal=self.config)['routes'])
        first.send(" ")
        first.wait_for(lambda _s: load(personal=self.config)['routes'].get('codex/gpt-6.1-terra/high') == 'enabled')
        first.close()
        second = self.open(cols=100, rows=30)
        second.send("/gpt-6.1-terra/high\r")
        second.wait_for("GPT-6.1 Terra")
        self.assertIn("Native access unknown", second.text())
        second.send(" ")
        second.wait_for(lambda _s: load(personal=self.config)['routes'].get('codex/gpt-6.1-terra/high') == 'disabled')
        self.assertEqual(load(personal=self.config)['routes'],
                         {'codex/gpt-6-luna/low': 'enabled', 'codex/gpt-6.1-terra/high': 'disabled'})
        self.assertIn('# only explicit choice', self.config.read_text())

    def test_sparse_map_edits_one_exact_route_and_survives_restart(self):
        self.config.write_text(SPARSE)
        first = self.open(cols=100, rows=30)
        self.assertIn("1/40 enabled", first.text())
        self.assertIn("not set", first.text())
        first.send("/gpt-6-luna/medium\r ")
        first.wait_for(lambda _s: load(personal=self.config)["routes"].get("codex/gpt-6-luna/medium") == "enabled")
        first.wait_for("2/40 enabled")
        saved = load(personal=self.config)
        self.assertEqual(saved["routes"], {"codex/gpt-6-luna/low": "enabled", "codex/gpt-6-luna/medium": "enabled"})
        text = self.config.read_text()
        self.assertIn("# keep this comment", text)
        self.assertIn("# only explicit choice", text)
        first.close()
        second = self.open(cols=100, rows=30)
        self.assertIn("2/40 enabled", second.text())
        second.send("/gpt-6-luna/medium\r ")
        second.wait_for(lambda _s: load(personal=self.config)["routes"].get("codex/gpt-6-luna/medium") == "disabled")
        self.assertEqual(len(load(personal=self.config)["routes"]), 2, "no other route is written")
