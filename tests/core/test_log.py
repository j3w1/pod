import json
import os

from pod import log, paths
from pod.errors import PodError
from pod.state import fold
from tests.support import PodCase


class LogTests(PodCase):
    def setUp(self):
        super().setUp()
        self.path = log.path("repo", "one")

    def start(self):
        with log.writer(self.path, create=True) as writer:
            writer.append({"kind": "objective", "id": "one", "source": {"kind": "text", "text": "x"}})

    def test_entries_are_numbered_by_the_log_and_read_back_in_order(self):
        self.start()
        with log.writer(self.path) as writer:
            written = writer.append({"kind": "fact", "fact": "note", "text": "a"}, {"kind": "fact", "fact": "note", "text": "b"})
        self.assertEqual([entry["n"] for entry in written], [1, 2])
        self.assertEqual([entry["n"] for entry in log.read(self.path)], [0, 1, 2])
        self.assertTrue(all(entry["at"].endswith("Z") for entry in log.read(self.path)))

    def test_an_append_never_changes_bytes_already_written(self):
        self.start()
        before = self.path.read_bytes()
        with log.writer(self.path) as writer:
            writer.append({"kind": "fact", "fact": "note", "text": "later"})
        self.assertTrue(self.path.read_bytes().startswith(before))

    def test_a_partial_last_line_is_not_an_entry_and_the_next_append_clears_it(self):
        self.start()
        with open(self.path, "ab") as stream:
            stream.write(b'{"kind": "fact", "fact": "no')
        self.assertEqual(len(log.read(self.path)), 1)
        with log.writer(self.path) as writer:
            writer.append({"kind": "fact", "fact": "note", "text": "whole"})
        entries = log.read(self.path)
        self.assertEqual([entry["n"] for entry in entries], [0, 1])
        self.assertEqual(entries[1]["text"], "whole")
        self.assertTrue(all(json.loads(line) for line in self.path.read_text().splitlines()))

    def test_a_writer_working_from_an_older_view_is_refused(self):
        self.start()
        with log.writer(self.path) as writer:
            with open(self.path, "ab") as stream:      # another process appended without the lock
                stream.write(b'{"kind":"fact","fact":"note","n":1,"at":"2026-01-01T00:00:00Z"}\n')
            with self.assertRaises(PodError) as caught:
                writer.append({"kind": "fact", "fact": "note"})
        self.assertEqual(caught.exception.code, "log_moved")
        self.assertEqual(len(log.read(self.path)), 2)

    def test_a_damaged_entry_is_refused_and_never_repaired(self):
        self.start()
        with open(self.path, "ab") as stream:
            stream.write(b"not json\n")
        damaged = self.path.read_bytes()
        with self.assertRaises(PodError) as caught:
            log.read(self.path)
        self.assertEqual(caught.exception.code, "log_damaged")
        self.assertEqual(self.path.read_bytes(), damaged)

    def test_there_is_no_limit_on_entries_and_the_file_is_private(self):
        self.start()
        with log.writer(self.path) as writer:
            writer.append(*({"kind": "fact", "fact": "note", "text": "x" * 500} for _ in range(3000)))
        self.assertEqual(len(log.read(self.path)), 3001)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_logs_live_in_their_own_directory_of_the_state_home(self):
        self.assertEqual(self.path, paths.state_home() / "objectives" / "repo" / "one.jsonl")
        with self.assertRaises(PodError):
            log.path("repo", "../escape")

    def test_only_the_five_concepts_are_persisted(self):
        self.start()
        with log.writer(self.path) as writer:
            writer.append({"kind": "obligation", "id": "o1"})
        with self.assertRaises(PodError) as caught:
            fold(log.read(self.path))
        self.assertEqual(caught.exception.code, "log_damaged")
