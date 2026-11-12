"""The exporter for records written by 0.21: complete, honest about damage, and read-only.

The fixtures under `fixtures_export/state` were produced by the 0.21 writer from invented
content, and `expected.json` is what the 0.21 reader returned for them.
"""

import base64
import hashlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import shutil

from pod import export, paths
from pod.errors import PodError
from tests.support import PodCase

FIXTURES = Path(__file__).resolve().parent / "fixtures_export"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text())
BY_OBJECTIVE = {row["objective"]: key for key, row in EXPECTED.items()}
PART = 48 * 1024


def tree_digest(root: Path) -> str:
    """Every path, type and byte under root."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(b"dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def compact(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


class ExportCase(PodCase):
    def setUp(self):
        super().setUp()
        self.source = self.tmp / "source"
        shutil.copytree(FIXTURES / "state", self.source)
        self.destination = self.tmp / "exported"

    def run_export(self) -> dict:
        return export.run(self.destination, source=self.source)

    def document(self, objective: str) -> dict:
        return json.loads((self.destination / (BY_OBJECTIVE[objective] + ".json")).read_text())

    def index(self) -> dict:
        return json.loads((self.destination / "index.json").read_text())

    def record(self, name: str, context: dict, chunks: dict | None = None) -> Path:
        directory = self.source / hashlib.sha256(name.encode()).hexdigest()
        (directory / "history" / "sha256").mkdir(parents=True)
        (directory / "context.json").write_bytes(compact(context))
        for address, data in (chunks or {}).items():
            (directory / "history" / "sha256" / (address + ".json")).write_bytes(data)
        return directory


class CompleteRecords(ExportCase):
    def test_every_fixture_record_exports_and_matches_what_the_earlier_reader_returned(self):
        result = self.run_export()
        self.assertEqual({key: result[key] for key in ("found", "exported", "incomplete", "failed", "empty")},
                         {"found": 4, "exported": 3, "incomplete": 0, "failed": 0, "empty": 1})
        for key, row in EXPECTED.items():
            document = json.loads((self.destination / (key + ".json")).read_text())
            self.assertTrue(document["complete"])
            self.assertEqual(document["problems"], [])
            value = dict(document["files"]["context.json"]["value"])
            value.pop("history", None)  # the storage discriminator of the stored-apart format
            self.assertEqual(value, row["context"], row["objective"])

    def test_a_plain_record_keeps_its_side_files_and_lists_what_is_not_json(self):
        self.run_export()
        document = self.document("fixture-plain")
        names = sorted(document["files"])
        self.assertEqual(names[:2], ["context.json", "governor.json"])
        self.assertTrue(names[2].startswith("native-start/"))
        self.assertEqual(document["files"]["governor.json"]["value"]["schema"], "pod-governor/v2")
        self.assertEqual(document["files"][names[2]]["value"]["admission_id"], "a1")
        self.assertEqual([item["path"] for item in document["other_files"]], [".lock"])
        self.assertEqual(document["chunks"], {"present": 0, "used": 0})

    def test_values_stored_apart_are_inlined_where_they_were(self):
        self.run_export()
        document = self.document("fixture-apart")
        value = document["files"]["context.json"]["value"]
        self.assertEqual(len(value["admissions"]["a1"]["report"]), 40)
        self.assertIn("gulls", value["checkpoint"]["obligations"][0]["findings"][0]["text"])
        self.assertNotIn(export.MARKER, json.dumps(document))
        self.assertEqual(document["chunks"], {"present": 2, "used": 2})

    def test_a_paged_record_is_one_document_with_its_envelope_and_attribution(self):
        self.run_export()
        document = self.document("fixture-paged")
        entry = document["files"]["context.json"]
        self.assertEqual(entry["format"], "pod-history-record/v1")
        self.assertEqual(entry["envelope"]["kind"], "context")
        self.assertEqual(len(entry["value"]["admissions"]), 90)
        self.assertEqual(list(entry["value"]["admissions"])[:3], ["a0", "a1", "a2"])  # stored order survives
        self.assertEqual(entry["attribution"], EXPECTED[BY_OBJECTIVE["fixture-paged"]]["attribution"])
        self.assertEqual(entry["maintenance"], [])
        self.assertEqual(document["chunks"]["present"], document["chunks"]["used"])
        text = json.dumps(document)
        self.assertNotIn('"ref"', text)
        self.assertNotIn(export.PAGE, text)

    def test_a_multi_part_value_in_the_wrapped_chunk_encoding_is_reassembled(self):
        stored = {"task": "fixture-task-wrapped", "report": ["wrapped part %d" % index for index in range(6000)]}
        payload = compact(stored)
        self.assertGreater(len(payload), PART)
        chunks, parts = {}, []
        for offset in range(0, len(payload), PART):
            raw = compact({"schema": "pod-history-chunk/v1",
                           "data": base64.b64encode(payload[offset:offset + PART]).decode()})
            parts.append(hashlib.sha256(raw).hexdigest())
            chunks[parts[-1]] = raw
        reference = {export.MARKER: {"sha256": hashlib.sha256(payload).hexdigest(), "parts": parts,
                                     "bytes": len(payload)}}
        directory = self.record("wrapped", {"schema": "pod-context/v4", "revision": 2,
                                            "history": {"schema": "pod-history-refs/v1"},
                                            "admissions": {"a1": reference},
                                            "checkpoint": {"objective": "fixture-wrapped"}}, chunks)
        self.run_export()
        document = json.loads((self.destination / (directory.name + ".json")).read_text())
        self.assertTrue(document["complete"])
        self.assertEqual(document["files"]["context.json"]["value"]["admissions"]["a1"], stored)

    def test_the_index_names_each_record_its_digest_its_output_and_what_was_skipped(self):
        self.run_export()
        index = self.index()
        rows = {row["record"]: row for row in index["records"]}
        self.assertEqual(len(rows), 4)
        key = BY_OBJECTIVE["fixture-paged"]
        raw = (self.source / key / "context.json").read_bytes()
        self.assertEqual(rows[key]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(rows[key]["objective"], "fixture-paged")
        self.assertEqual(rows[key]["status"], "exported")
        self.assertEqual(rows[key]["source"], str(self.source / key))
        self.assertTrue(Path(rows[key]["output"]).is_file())
        empty = [row for row in rows.values() if row["status"] == "empty"]
        self.assertEqual([row["output"] for row in empty], [None])
        self.assertEqual(index["skipped"], [])
        self.assertEqual(sum(index["counts"][key] for key in ("exported", "incomplete", "failed", "empty")),
                         index["counts"]["found"])

    def test_the_new_objective_directory_is_never_read_or_reported(self):
        (self.source / paths.objectives_home().name).mkdir()
        (self.source / paths.objectives_home().name / "one.jsonl").write_text("{}\n")
        result = self.run_export()
        self.assertEqual((result["found"], result["copied"], result["not_exported"]), (4, 1, 0))
        self.assertNotIn("one.jsonl", (self.destination / "index.json").read_text())
        self.assertEqual(list(self.destination.rglob("one.jsonl")), [])


class EverythingElse(ExportCase):
    """What 0.21 wrote beside its top-level record directories."""

    def setUp(self):
        super().setUp()
        self.key = BY_OBJECTIVE["fixture-plain"]
        self.relay = self.source / ".relay-authorizations"
        self.relay.mkdir()
        self.grant = b'{"schema":"fixture-relay/v1",  "grant": "kept byte for byte"}\n\n'
        (self.relay / ("ab" * 32 + ".json")).write_bytes(self.grant)
        (self.relay / ".lock").write_bytes(b"")
        self.quarantine = self.source / ".quarantine-fixture" / "deeper"
        shutil.copytree(self.source / self.key, self.quarantine / self.key)
        context = self.quarantine / self.key / "context.json"
        context.write_bytes(context.read_bytes().replace(b"fixture-plain", b"fixture-moved"))

    def test_record_directories_are_found_at_any_depth_and_never_overwrite_one_another(self):
        result = self.run_export()
        self.assertEqual((result["found"], result["exported"], result["empty"]), (5, 4, 1))
        nested = self.destination / "nested" / ".quarantine-fixture" / "deeper" / (self.key + ".json")
        moved = json.loads(nested.read_text())
        self.assertTrue(moved["complete"])
        self.assertEqual(moved["files"]["context.json"]["value"]["checkpoint"]["objective"], "fixture-moved")
        self.assertEqual(self.document("fixture-plain")["files"]["context.json"]["value"]["checkpoint"]["objective"],
                         "fixture-plain")
        rows = [row for row in self.index()["records"] if row["record"] == self.key]
        self.assertEqual([(row["source"], row["output"], row["objective"]) for row in rows],
                         [(str(self.quarantine / self.key), str(nested), "fixture-moved"),
                          (str(self.source / self.key), str(self.destination / (self.key + ".json")), "fixture-plain")])

    def test_every_other_json_file_is_copied_byte_for_byte_and_indexed(self):
        result = self.run_export()
        self.assertEqual(result["copied"], 2)
        copy = self.destination / "files" / ".relay-authorizations" / ("ab" * 32 + ".json")
        self.assertEqual(copy.read_bytes(), self.grant)
        self.assertEqual((self.destination / "files" / "not-a-record" / "note.json").read_bytes(),
                         (self.source / "not-a-record" / "note.json").read_bytes())
        self.assertEqual(self.index()["files"][0],
                         {"path": ".relay-authorizations/" + "ab" * 32 + ".json", "output": str(copy),
                          "bytes": len(self.grant), "sha256": hashlib.sha256(self.grant).hexdigest()})
        self.assertEqual(self.index()["counts"]["copied"], 2)

    def test_what_is_not_exported_is_counted_and_listed_in_the_index_and_the_summary(self):
        outside = self.tmp / "outside.json"
        outside.write_text('{"secret": "elsewhere"}\n')
        (self.relay / "linked.json").symlink_to(outside)
        (self.source / "linked-directory").symlink_to(self.tmp)
        expected = [{"path": ".relay-authorizations/.lock", "reason": "not a JSON file"},
                    {"path": ".relay-authorizations/linked.json", "reason": "symbolic link, not followed"},
                    {"path": "linked-directory", "reason": "symbolic link, not followed"}]
        out = io.StringIO()
        with redirect_stdout(out):
            code = export.main(["--to", str(self.destination), "--from", str(self.source)])
        self.assertEqual(code, 0, "only an incomplete or failed record makes the command fail")
        self.assertEqual(self.index()["skipped"], expected)
        self.assertEqual(self.index()["counts"]["not_exported"], 3)
        shown = out.getvalue()
        self.assertIn("5 records: 4 exported, 0 incomplete, 0 failed, 1 empty. 2 other JSON files copied as they "
                      "are. 3 not exported.", shown)
        for item in expected:
            self.assertIn(f"  not exported: {item['path']} ({item['reason']})", shown)
        self.assertNotIn("elsewhere", "".join(path.read_text() for path in self.destination.rglob("*.json")))
        out = io.StringIO()
        with redirect_stdout(out):
            export.main(["--to", str(self.destination), "--from", str(self.source), "--json"])
        self.assertEqual((json.loads(out.getvalue())["not_exported"], json.loads(out.getvalue())["skipped"]),
                         (3, expected))

    def test_the_source_stays_byte_identical_and_a_destination_around_it_is_refused(self):
        before = tree_digest(self.source)
        self.run_export()
        with self.assertRaises(PodError) as caught:
            export.run(self.tmp, source=self.source)
        self.assertEqual(caught.exception.code, "export_inside_source")
        self.assertIn("does not contain", caught.exception.fix)
        self.assertEqual(tree_digest(self.source), before)
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir() if path.name.startswith(("files", "nested"))),
                         [])


class DamagedRecords(ExportCase):
    def test_a_missing_page_is_marked_where_it_belongs_and_the_rest_is_exported(self):
        key = BY_OBJECTIVE["fixture-paged"]
        store = self.source / key / "history" / "sha256"
        root = json.loads((self.source / key / "context.json").read_text())["root"]["ref"]
        top = json.loads((store / (root["sha256"] + ".json")).read_text())
        admissions = dict(top["entries"])["admissions"]["ref"]["sha256"]
        (store / (admissions + ".json")).unlink()
        result = self.run_export()
        self.assertEqual((result["exported"], result["incomplete"], result["failed"]), (2, 1, 0))
        document = self.document("fixture-paged")
        self.assertFalse(document["complete"])
        self.assertEqual(len(document["problems"]), 1)
        self.assertIn("context.json/admissions", document["problems"][0])
        value = document["files"]["context.json"]["value"]
        self.assertEqual(set(value["admissions"]), {export.UNRESOLVED})
        self.assertEqual(value["admissions"][export.UNRESOLVED]["stored"]["ref"]["sha256"], admissions)
        self.assertEqual(value["checkpoint"], EXPECTED[key]["context"]["checkpoint"])
        row = next(row for row in self.index()["records"] if row["record"] == key)
        self.assertEqual(row["status"], "incomplete")
        self.assertEqual(row["problems"], document["problems"])

    def test_a_chunk_whose_bytes_differ_from_its_address_is_not_trusted(self):
        key = BY_OBJECTIVE["fixture-apart"]
        chunk = next((self.source / key / "history" / "sha256").iterdir())
        chunk.write_bytes(chunk.read_bytes().replace(b"gulls", b"geese"))
        self.assertEqual(self.run_export()["incomplete"], 1)
        document = self.document("fixture-apart")
        self.assertIn("differs from its address", document["problems"][0])
        self.assertNotIn("geese", json.dumps(document))

    def test_a_record_file_that_is_not_json_is_reported_with_its_digest(self):
        key = BY_OBJECTIVE["fixture-plain"]
        (self.source / key / "context.json").write_bytes(b'{"schema": "pod-context/v4", "revis')
        self.assertEqual(self.run_export()["incomplete"], 1)
        document = self.document("fixture-plain")
        entry = document["files"]["context.json"]
        self.assertIn(export.UNRESOLVED, entry)
        self.assertEqual(entry["bytes"], 35)
        self.assertEqual(document["files"]["governor.json"]["value"]["revision"], 1)  # the rest still exports

    def test_a_record_file_that_is_a_symbolic_link_is_not_followed(self):
        key = BY_OBJECTIVE["fixture-plain"]
        outside = self.tmp / "outside.json"
        outside.write_text('{"secret": "elsewhere"}\n')
        (self.source / key / "governor.json").unlink()
        (self.source / key / "governor.json").symlink_to(outside)
        self.assertEqual(self.run_export()["incomplete"], 1)
        self.assertNotIn("elsewhere", json.dumps(self.document("fixture-plain")))


class SourceIsOnlyRead(ExportCase):
    def test_the_source_tree_is_byte_identical_after_an_export_including_damage(self):
        key = BY_OBJECTIVE["fixture-paged"]
        next((self.source / key / "history" / "sha256").iterdir()).unlink()
        before = tree_digest(self.source)
        self.run_export()
        self.run_export()  # a second export over the first one's output changes nothing either
        self.assertEqual(tree_digest(self.source), before)

    def test_a_destination_inside_the_source_is_refused_before_anything_is_written(self):
        before = tree_digest(self.source)
        for inside in (self.source, self.source / "exported", self.source / BY_OBJECTIVE["fixture-plain"] / "out"):
            with self.assertRaises(PodError) as caught:
                export.run(inside, source=self.source)
            self.assertEqual(caught.exception.code, "export_inside_source")
            self.assertTrue(caught.exception.fix)
        self.assertEqual(tree_digest(self.source), before)

    def test_the_default_source_is_pods_own_state_directory(self):
        shutil.copytree(self.source, paths.state_home())
        result = export.run(self.destination)
        self.assertEqual(result["found"], 4)
        self.assertEqual(self.index()["source"], str(paths.state_home().resolve()))

    def test_a_missing_source_is_refused(self):
        with self.assertRaises(PodError) as caught:
            export.run(self.destination, source=self.tmp / "absent")
        self.assertEqual(caught.exception.code, "export_source_missing")


class CommandLine(ExportCase):
    def call(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = export.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_json_output_carries_the_counts_and_exit_zero_means_everything_was_complete(self):
        code, out, _ = self.call("--to", str(self.destination), "--from", str(self.source), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["exported"], 3)

    def test_an_incomplete_record_makes_the_command_exit_nonzero_and_say_so(self):
        (self.source / BY_OBJECTIVE["fixture-plain"] / "context.json").write_bytes(b"not json")
        code, out, _ = self.call("--to", str(self.destination), "--from", str(self.source))
        self.assertEqual(code, 1)
        self.assertIn("1 incomplete", out)

    def test_a_refusal_states_what_to_change(self):
        code, out, err = self.call("--to", str(self.source / "x"), "--from", str(self.source))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("choose a destination outside", err)
