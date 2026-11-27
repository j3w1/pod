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

    def test_an_identical_replacement_a_symlinked_output_and_a_vanished_output_are_refused(self):
        target = paths.route_file()
        original = target.read_bytes()

        def swap_same_bytes():
            other = target.with_name("replacement.yaml")           # made while the old file still exists: a new inode
            other.write_bytes(original)
            os.replace(other, target)
        with self.editor(change=swap_same_bytes):
            with self.assertRaises(PodError) as same:
                edit.edit_in_editor(target, ["ed"])
        self.assertEqual(same.exception.code, "preferences_changed")
        self.kept()[0].unlink()

        def symlink_output(copy):
            real = Path(copy).with_suffix(".real")
            real.write_bytes(b"x: 1\n")
            os.unlink(copy)
            os.symlink(real, copy)
        with self.editor(mutate=symlink_output):
            with self.assertRaises(PodError) as linked:
                edit.edit_in_editor(target, ["ed"])
        self.assertEqual(linked.exception.code, "editor_failed")
        self.assertEqual(target.read_bytes(), original)
        for leftover in self.kept():
            leftover.unlink()
        with self.editor(mutate=lambda copy: os.unlink(copy), status=3):
            with self.assertRaises(PodError) as vanished:
                edit.edit_in_editor(target, ["ed"])
        self.assertEqual(vanished.exception.code, "editor_failed")

    def test_a_concurrent_valid_save_beats_an_invalid_result_with_a_conflict(self):
        target = paths.route_file()
        with self.editor(text="\t: not: [yaml\n", change=lambda: edit.apply(preferred="codex/sol/high")):
            with self.assertRaises(PodError) as caught:
                edit.edit_in_editor(target, ["ed"])
        self.assertEqual(caught.exception.code, "preferences_changed")

    def test_a_directory_or_unreadable_output_is_refused_without_deleting_a_regular_copy(self):
        target = paths.route_file()
        before = target.read_bytes()

        def directory_output(copy):
            os.unlink(copy)
            os.mkdir(copy)
        with self.editor(mutate=directory_output):
            with self.assertRaises(PodError) as caught:
                edit.edit_in_editor(target, ["ed"])
        self.assertEqual(caught.exception.code, "editor_failed")
        self.kept()[0].rmdir()

        def unreadable(copy):
            Path(copy).write_text("# edited\n")
            os.chmod(copy, 0)
        with self.editor(mutate=unreadable):
            with self.assertRaises(PodError) as unread:
                edit.edit_in_editor(target, ["ed"])
        self.assertEqual(unread.exception.code, "editor_failed")
        self.assertEqual(target.read_bytes(), before)
        kept = self.kept()
        self.assertEqual(len(kept), 1)                          # the person's only copy of the edit survives
        os.chmod(kept[0], 0o600)
        self.assertEqual(kept[0].read_text(), "# edited\n")

    def test_an_editor_save_that_lands_while_waiting_for_the_lock_is_kept_not_discarded(self):
        target = paths.route_file()
        before = target.read_bytes()
        real = edit._locked

        def later_save(path):
            for copy in path.parent.glob(".config-edit-*"):
                copy.write_text(copy.read_text() + "# later\n")
            return real(path)
        for variant in ("regular", "directory"):
            with self.subTest(variant=variant):
                def hook(path, variant=variant):
                    if variant == "regular":
                        return later_save(path)
                    for copy in path.parent.glob(".config-edit-*"):
                        copy.unlink()
                        copy.mkdir()
                    return real(path)
                with self.editor(), patch.object(edit, "_locked", side_effect=hook):
                    with self.assertRaises(PodError) as caught:
                        edit.edit_in_editor(target, ["ed"])
                self.assertEqual(caught.exception.code, "preferences_changed")
                self.assertEqual(target.read_bytes(), before)
                kept = self.kept()
                self.assertEqual(len(kept), 1)
                if variant == "regular":
                    self.assertTrue(kept[0].read_text().endswith("# later\n"))        # the newer save survives
                    kept[0].unlink()
                else:
                    kept[0].rmdir()

    def test_a_save_that_lands_after_install_is_kept_and_reported(self):
        target = paths.route_file()
        real = os.replace
        calls = []

        def replace(src, dst):
            calls.append(Path(src).name)
            if str(src).endswith(".yaml") and Path(dst) != target and Path(src).name.startswith(".config-edit-"):
                Path(src).write_text(Path(src).read_text() + "# after\n")      # the editor saves again just now
            return real(src, dst)
        with self.editor(), patch("os.replace", side_effect=replace):
            with self.assertRaises(PodError) as caught:
                edit.edit_in_editor(target, ["ed"])
        self.assertEqual(caught.exception.code, "preferences_changed")
        self.assertTrue(target.read_text().endswith("# edited\n"))            # the validated snapshot was installed
        kept = list(target.parent.glob(".config-edit-*.done"))
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0].read_text().endswith("# after\n"))

    def test_a_late_save_that_cannot_be_read_is_still_reported_with_its_path(self):
        target = paths.route_file()
        real = os.replace

        def replace(src, dst):
            if Path(src).name.startswith(".config-edit-") and Path(dst) != target:
                Path(src).write_text("# unreadable later\n")
                os.chmod(src, 0)
            return real(src, dst)
        with self.editor(), patch("os.replace", side_effect=replace):
            with self.assertRaises(PodError) as caught:
                edit.edit_in_editor(target, ["ed"])
        self.assertEqual(caught.exception.code, "preferences_changed")
        kept = list(target.parent.glob(".config-edit-*.done"))
        self.assertEqual(len(kept), 1)
        self.assertIn(str(kept[0]), str(caught.exception))
        os.chmod(kept[0], 0o600)
