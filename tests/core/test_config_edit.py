import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pod import paths
from pod.errors import PodError
from pod.models import edit
from tests.core.support import CoreCase


class ConfigEditLockTests(CoreCase):
    def editor(self, change=None, *, status=0, text="# edited\n", mutate=None):
        def run(command, **_):
            copy = command[-1]
            if mutate:
                mutate(copy)
            elif text is not None:
                with open(copy, "a", encoding="utf-8") as handle:
                    handle.write(text)
            if change:
                change()
            return SimpleNamespace(returncode=status)
        return patch("subprocess.run", side_effect=run)

    def kept(self):
        return list(paths.route_file().parent.glob(".config-edit-*"))

    def test_an_unchallenged_edit_is_saved_and_the_copy_is_gone(self):
        target = paths.route_file()
        with self.editor():
            edit.edit_in_editor(target, ["ed"])
        self.assertTrue(target.read_text().endswith("# edited\n"))
        self.assertEqual(self.kept(), [])

    def test_a_save_made_while_the_editor_was_open_is_never_lost(self):
        target = paths.route_file()
        with self.editor(change=lambda: edit.apply(preferred="codex/sol/high")):
            with self.assertRaises(PodError) as caught:
                edit.edit_in_editor(target, ["ed"])
        self.assertEqual(caught.exception.code, "preferences_changed")
        self.assertIn("preferred: codex/sol/high", target.read_text())
        self.assertTrue(self.kept()[0].read_text().endswith("# edited\n"))
        self.assertIn(str(self.kept()[0]), str(caught.exception))

    def test_a_failed_or_missing_editor_installs_nothing(self):
        target = paths.route_file()
        before = target.read_bytes()
        with self.editor(status=7):
            with self.assertRaises(PodError) as caught:
                edit.edit_in_editor(target, ["ed"])
        self.assertEqual(caught.exception.code, "editor_failed")
        self.assertEqual(target.read_bytes(), before)
        self.assertIn(str(self.kept()[0]), str(caught.exception))       # the edit it made is reported
        self.kept()[0].unlink()
        with patch("subprocess.run", side_effect=FileNotFoundError(2, "No such file")):
            with self.assertRaises(PodError) as missing:
                edit.edit_in_editor(target, ["nope"])
        self.assertEqual(missing.exception.code, "editor_failed")
        self.assertEqual((target.read_bytes(), self.kept()), (before, []))

    def test_an_unreadable_result_is_not_installed_but_an_invalid_original_may_be_repaired(self):
        target = paths.route_file()
        before = target.read_bytes()
        with self.editor(text="\t: not: [yaml\n"):
            with self.assertRaises(PodError) as caught:
                edit.edit_in_editor(target, ["ed"])
        self.assertEqual(caught.exception.code, "preferences_invalid")
        self.assertEqual(target.read_bytes(), before)
        self.kept()[0].unlink()
        target.write_text("\t: not: [yaml\n")
        with self.editor(mutate=lambda copy: Path(copy).write_text(before.decode())):
            edit.edit_in_editor(target, ["ed"])
        self.assertEqual(target.read_bytes(), before)

    def test_a_symlink_or_a_removed_file_is_a_conflict_that_keeps_the_edit(self):
        target = paths.route_file()
        real = target.parent / "real.yaml"
        real.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(real)
        with self.assertRaises(PodError) as refused:
            edit.edit_in_editor(target, ["ed"])
        self.assertEqual(refused.exception.code, "preferences_invalid")
        self.assertTrue(target.is_symlink())
        target.unlink()
        target.write_bytes(real.read_bytes())
        with self.editor(change=target.unlink):
            with self.assertRaises(PodError) as gone:
                edit.edit_in_editor(target, ["ed"])
        self.assertEqual(gone.exception.code, "preferences_changed")
        self.assertIn(str(self.kept()[0]), str(gone.exception))

    def test_the_saved_file_keeps_the_live_files_mode_whatever_the_editor_left(self):
        target = paths.route_file()
        target.chmod(0o600)

        def atomic_save(copy):
            data = Path(copy).read_text() + "# edited\n"
            os.unlink(copy)
            with open(copy, "w") as handle:
                handle.write(data)
            os.chmod(copy, 0o644)
        with self.editor(mutate=atomic_save):
            edit.edit_in_editor(target, ["ed"])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
