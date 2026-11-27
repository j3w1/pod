from unittest.mock import patch

from pod import paths
from pod.errors import PodError
from pod.models import edit
from tests.core.support import CoreCase


class ConfigEditLockTests(CoreCase):
    def editor(self, change=None):
        def run(command, **_):
            copy = command[-1]
            with open(copy, "a", encoding="utf-8") as handle:
                handle.write("# edited\n")
            if change:
                change()
        return patch("subprocess.run", side_effect=run)

    def test_an_unchallenged_edit_is_saved_and_the_copy_is_gone(self):
        target = paths.route_file()
        with self.editor():
            edit.edit_in_editor(target, ["ed"])
        self.assertTrue(target.read_text().endswith("# edited\n"))
        self.assertEqual([p.name for p in target.parent.glob(".config-edit-*")], [])

    def test_a_save_made_while_the_editor_was_open_is_never_lost(self):
        target = paths.route_file()
        with self.editor(change=lambda: edit.apply(preferred="codex/sol/high")):
            with self.assertRaises(PodError) as caught:
                edit.edit_in_editor(target, ["ed"])
        self.assertEqual(caught.exception.code, "preferences_changed")
        self.assertIn("preferred: codex/sol/high", target.read_text())
        kept = list(target.parent.glob(".config-edit-*"))
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0].read_text().endswith("# edited\n"))
