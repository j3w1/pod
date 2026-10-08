"""Sanitized offline B1 incident shapes; the original R15 disproven control remains."""
import json

from tests.test_owner_handoff import HandoffCase, SENTINEL


class ReplacementHandleIncident(HandoffCase):
    def test_same_pane_replacement_requires_confirmation_and_pending_lineage(self):
        self.establish(); self.lose()
        # The native port's opaque pane fields are the same for both handles and prove nothing.
        self.assertEqual(self.status()["owner_handoff"]["condition"], "fresh")
        before = self.snapshot()
        self.refused(lambda: self.handoff({}))
        self.assertEqual(self.snapshot(), before)
        self.handoff()
        self.assertEqual(self.entry()["state"], "done")
        self.assertNotIn(SENTINEL, json.dumps(self.state()))
        self.assertEqual(len(self.mutations), 1)
        self.assertEqual(len(self.port.starts), 1)


class ManualRebindIncident(HandoffCase):
    def test_manual_run_use_without_pending_never_proves_lineage(self):
        self.establish(); self.lose()
        self.current.update(coordinator_handle="next", consumer_generation=2)
        before = self.snapshot()
        observed = self.status()["owner_handoff"]
        self.assertEqual(observed["condition"], "lineage_unproven")
        self.assertNotIn("original pane", observed["next_action"])
        self.refused(lambda: self.handoff())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.mutations, [])
