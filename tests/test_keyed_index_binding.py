"""R239: a shared keyed index binds a review to its own rows and the file's schema (pod#272).

The kernel fixture: a base commit holding a coverage index, a candidate that adds its own row and edits src,
and a target that later adds or edits other rows and is merged into the candidate.
"""

import hashlib
import json
import unittest
from unittest.mock import patch

from pod import keyed_index
from pod.errors import PodError
from pod.keyed_index import MAX_ROWS, binding, current, parse
from pod.ledger import kernel_context, read, tree_source_current
from pod.records import source_identity
from tests.kernel_support import KernelCase, git

COVERAGE = "docs/pod-coverage.json"


def coverage(*rows, schema="pod-scenario-coverage/v1", **extra):
    return {"schema": schema, "note": "one row per scenario", **extra,
            "scenarios": [{"id": key, "status": status} for key, status in rows], "withdrawn_scenarios": []}


def dump(document):
    return (json.dumps(document, indent=2) + "\n").encode()


def lanes(durations, **extra):
    return {"schema": "suite-lanes/v1", "budgets": {"test_max_seconds": 60}, "durations": durations,
            "overrides": {}, **extra}


def entry(data, path=COVERAGE):
    return {"path": path, "state": "present", "sha256": hashlib.sha256(data).hexdigest()}


BASE = coverage(("A1", "NOT_RUN"), ("A2", "NOT_RUN"))
OWN = coverage(("A1", "NOT_RUN"), ("A2", "NOT_RUN"), ("A10", "NOT_RUN"))


class ParseTests(unittest.TestCase):
    def test_rows_are_keyed_by_collection_and_key_and_members_by_name(self):
        schema, rows = parse(dump(OWN))
        self.assertEqual(sorted(rows), ['["note"]', '["scenarios","A1"]', '["scenarios","A10"]',
                                        '["scenarios","A2"]'])
        _, lane_rows = parse(dump(lanes({"tests.a.T.test_x": 1.5})))
        self.assertIn('["durations","tests.a.T.test_x"]', lane_rows)
        # Row order and member order are not bound; the schema is the same.
        reordered = {**OWN, "scenarios": list(reversed(OWN["scenarios"]))}
        self.assertEqual(parse(dump(reordered)), (schema, rows))

    def test_anything_not_exactly_a_known_index_is_not_parsed(self):
        cases = {
            "invalid JSON": b"{",
            "not UTF-8": b'{"schema": "\xff"}',
            "NaN": dump(BASE).replace(b'"NOT_RUN"', b"NaN", 1),
            "unknown format": dump({**BASE, "schema": "other/v1"}),
            "not an object": b"[]",
            "collection of the wrong type": dump({**BASE, "scenarios": {}}),
            "a list row without a text id": dump({**BASE, "scenarios": [{"id": 1}]}),
            "a duplicate row key": dump(coverage(("A1", "NOT_RUN"), ("A1", "PASS"))),
            "a duplicate member name": dump(BASE).replace(b'"note"', b'"note": "x", "note"', 1),
            "a duplicate name inside a row": dump(BASE).replace(b'"status"', b'"status": "x", "status"', 1),
            "not bytes": None,
        }
        for label, data in cases.items():
            with self.subTest(label):
                self.assertIsNone(parse(data))


class BindingTests(unittest.TestCase):
    def bound(self, now=OWN, base=BASE):
        return binding(entry(dump(now)), dump(now), None if base is None else dump(base))

    def test_binding_keeps_identity_and_binds_only_the_changed_rows(self):
        now = coverage(("A1", "PASS"), ("A10", "NOT_RUN"))   # edits A1, removes A2, adds A10
        bound = self.bound(now)
        self.assertEqual({key: bound[key] for key in ("path", "state", "sha256")}, entry(dump(now)))
        rows = bound["index"]["rows"]
        self.assertEqual(sorted(rows), ['["scenarios","A1"]', '["scenarios","A10"]', '["scenarios","A2"]'])
        self.assertIsNone(rows['["scenarios","A2"]'])
        self.assertTrue(keyed_index.valid(bound["index"]))

    def test_whole_content_binding_is_kept_when_rows_cannot_be_bound_exactly(self):
        many = coverage(*BASE_ROWS, *((f"B{number}", "NOT_RUN") for number in range(MAX_ROWS + 1)))
        cases = {"no base": self.bound(base=None),
                 "an unparseable base": binding(entry(dump(OWN)), dump(OWN), b"{"),
                 "an unparseable file": binding(entry(b"{"), b"{", dump(BASE)),
                 "bytes that are not the bound identity": binding(entry(dump(BASE)), dump(OWN), dump(BASE)),
                 "more rows than the bound": self.bound(many)}
        for label, result in cases.items():
            with self.subTest(label):
                self.assertNotIn("index", result)

    def test_other_rows_do_not_change_the_binding(self):
        bound = self.bound()
        for label, later in {
                "a row added elsewhere": coverage(("A0", "NOT_RUN"), ("A1", "NOT_RUN"), ("A2", "NOT_RUN"),
                                                  ("A10", "NOT_RUN")),
                "a row edited elsewhere": coverage(("A1", "PASS"), ("A2", "NOT_RUN"), ("A10", "NOT_RUN")),
                "a row removed elsewhere": coverage(("A1", "NOT_RUN"), ("A10", "NOT_RUN")),
                "a member edited elsewhere": {**OWN, "note": "changed"},
                "rows reordered": {**OWN, "scenarios": list(reversed(OWN["scenarios"]))}}.items():
            with self.subTest(label):
                self.assertTrue(current(bound, dump(later)))

    def test_bound_rows_schema_and_unparseable_files_invalidate(self):
        bound = self.bound(coverage(("A1", "NOT_RUN"), ("A10", "NOT_RUN")))   # adds A10, removes A2
        for label, later in {
                "an own row edited": dump(coverage(("A1", "NOT_RUN"), ("A10", "PASS"))),
                "an own row removed": dump(coverage(("A1", "NOT_RUN"))),
                "a removed row restored": dump(coverage(("A1", "NOT_RUN"), ("A2", "NOT_RUN"), ("A10", "NOT_RUN"))),
                "a top-level member added": dump(coverage(("A1", "NOT_RUN"), ("A10", "NOT_RUN"), extra="x")),
                "the format changed": dump(lanes({})),
                "an unparseable file": b"{",
                "a duplicate key elsewhere": dump(coverage(("A1", "NOT_RUN"), ("A1", "PASS"), ("A10", "NOT_RUN"))),
                "an unreadable file": None}.items():
            with self.subTest(label):
                self.assertFalse(current(bound, later))

    def test_a_malformed_index_never_holds(self):
        bound = self.bound()
        for index in ({}, {**bound["index"], "schema": "x"}, {**bound["index"], "extra": 1},
                      {**bound["index"], "rows": {"k": "not a digest"}},
                      {**bound["index"], "rows": {f"k{n}": None for n in range(MAX_ROWS + 1)}}):
            with self.subTest(index=str(index)[:40]):
                self.assertFalse(keyed_index.valid(index))
                self.assertFalse(current({**bound, "index": index}, dump(OWN)))

    def test_lane_rows_bind_by_test_id(self):
        base = lanes({"tests.a.T.test_x": 1.5})
        now = lanes({"tests.a.T.test_x": 1.5, "tests.b.T.test_y": 3.0})
        bound = binding(entry(dump(now), "tests/suite_lanes.json"), dump(now), dump(base))
        self.assertEqual(list(bound["index"]["rows"]), ['["durations","tests.b.T.test_y"]'])
        self.assertTrue(current(bound, dump(lanes({**now["durations"], "tests.c.T.test_z": 2.5}))))
        self.assertFalse(current(bound, dump(lanes({**now["durations"], "tests.b.T.test_y": 4.0}))))


BASE_ROWS = (("A1", "NOT_RUN"), ("A2", "NOT_RUN"))


class ReviewCarryTests(KernelCase):
    """A review bound by admission carries across a merge of the target's unrelated rows, and no further."""

    def commit(self, document, message, path=COVERAGE):
        (self.project / path).write_bytes(dump(document) if isinstance(document, dict) else document)
        git(self.project, "add", path)
        git(self.project, "commit", "-qm", message)
        return git(self.project, "rev-parse", "HEAD")

    def setUp(self):
        super().setUp()
        (self.project / "docs").mkdir()
        # Own rows go at the end and the target's at the start, so Git merges them cleanly.
        self.base = self.commit(coverage(*BASE_ROWS, ("A3", "NOT_RUN"), ("A4", "NOT_RUN")), "index")
        git(self.project, "branch", "-f", "target", self.base)
        git(self.project, "update-ref", "refs/remotes/origin/target", self.base)
        (self.project / "src" / "old.py").write_text("own\n")
        git(self.project, "add", "src/old.py")
        self.candidate = self.commit(coverage(*BASE_ROWS, ("A3", "NOT_RUN"), ("A4", "NOT_RUN"),
                                              ("A10", "NOT_RUN")), "own row")

    def reviewed(self, sources=(COVERAGE, "src/old.py"), scope=("src", COVERAGE)):
        self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": list(scope)}, "question": "right?",
                     "candidate": self.candidate, "existing_evidence": "tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}})
        frozen = self.packet(["A"], role="review", sources=[source_identity(self.project, path) for path in sources])
        admission = self.start("review", frozen)["admission"]
        self.settle(admission)
        rows = self.stored()
        row = next(row for row in rows if row["id"] == "A")
        row.pop("executor")
        row.update(state="satisfied", evidence=[{"attempt": admission["admission_id"]}])
        self.report(admission, frozen, map={"obligations": rows})
        self.reviewed_at = self.candidate
        return read(self.project, "objective")["admissions"][admission["admission_id"]]["binding"]

    def target(self, document, path=COVERAGE):
        """The target takes another change, and the candidate merges it."""
        git(self.project, "checkout", "-q", "target")
        self.commit(document, "another change", path)
        git(self.project, "update-ref", "refs/remotes/origin/target", "target")
        git(self.project, "checkout", "-q", "main")
        git(self.project, "merge", "-q", "--no-edit", "target")
        self.candidate = git(self.project, "rev-parse", "HEAD")

    def later(self, document, path=COVERAGE):
        """The candidate itself takes a later edit, as a merge resolution would."""
        self.candidate = self.commit(document, "later edit", path)

    def carry(self):
        rows = self.stored()
        next(row for row in rows if row["id"] == "A")["reuse"] = {"from": self.reviewed_at}
        return self.write(rows)

    def refused_carry(self, classification="binding change", paths=()):
        """The carry refuses: the bound sources no longer hold, or the Git delta touches the scope."""
        with self.assertRaises(PodError) as caught:
            self.carry()
        error = caught.exception
        self.assertEqual((error.code, error.detail["detail"]), ("obligation_unaccounted", "evidence_invalidated"))
        invalidation = error.detail["referent"]["invalidations"][0]
        self.assertEqual((invalidation["classification"], invalidation["paths"], invalidation["reuse_failure"]),
                         (classification, list(paths), {"predicate": "reuse_scope_untouched"}))
        return error

    def test_admission_binds_the_reviewed_change_s_own_rows_and_keeps_ordinary_files_whole(self):
        sources = {item["path"]: item for item in self.reviewed()["sources"]}
        self.assertEqual(list(sources[COVERAGE]["index"]["rows"]), ['["scenarios","A10"]'])
        self.assertEqual(sources["src/old.py"], source_identity(self.project, "src/old.py"))

    def test_a_review_carries_across_a_merge_of_unrelated_rows(self):
        self.reviewed()
        self.target(coverage(("A0", "NOT_RUN"), ("A1", "PASS"), ("A2", "NOT_RUN"), ("A3", "NOT_RUN"),
                             ("A4", "NOT_RUN")))
        self.assertIn('"A0"', (self.project / COVERAGE).read_text())
        self.carry()
        row = next(row for row in self.stored() if row["id"] == "A")
        self.assertEqual((row["state"], row["reuse"]["from"]), ("satisfied", self.reviewed_at))
        self.assertIn(COVERAGE, row["reuse"]["delta"])   # the recorded Git delta is unchanged
        self.write(self.stored())                        # the retained REUSE stays valid on a later write

    def test_without_row_binding_the_same_merge_invalidates_as_before(self):
        with patch("pod.keyed_index.binding", side_effect=lambda item, now, base: item):
            self.reviewed()
        self.target(coverage(("A0", "NOT_RUN"), *BASE_ROWS, ("A3", "NOT_RUN"), ("A4", "NOT_RUN")))
        self.refused_carry()

    def test_an_edit_to_an_own_row_invalidates(self):
        self.reviewed()
        self.target(coverage(("A0", "NOT_RUN"), *BASE_ROWS, ("A3", "NOT_RUN"), ("A4", "NOT_RUN")))
        self.later(coverage(("A0", "NOT_RUN"), *BASE_ROWS, ("A3", "NOT_RUN"), ("A4", "NOT_RUN"), ("A10", "PASS")))
        self.refused_carry()

    def test_a_schema_change_invalidates(self):
        self.reviewed()
        self.target(coverage(*BASE_ROWS, ("A3", "NOT_RUN"), ("A4", "NOT_RUN"), ("A10", "NOT_RUN"), added="x"))
        self.refused_carry()

    def test_an_unparseable_index_invalidates(self):
        self.reviewed()
        self.later(b"{ not json\n")
        self.refused_carry()

    def test_a_duplicate_key_invalidates(self):
        self.reviewed()
        self.target(coverage(("A1", "PASS"), *BASE_ROWS, ("A3", "NOT_RUN"), ("A4", "NOT_RUN"), ("A10", "NOT_RUN")))
        self.refused_carry()

    def test_an_ordinary_bound_file_keeps_whole_content_binding(self):
        self.reviewed(sources=(COVERAGE, "README.md"), scope=("src", COVERAGE, "README.md"))
        self.target(b"readme changed elsewhere\n", "README.md")
        self.refused_carry()

    def test_a_scope_index_path_without_a_row_binding_stays_touched(self):
        self.reviewed(sources=("src/old.py",))
        self.target(coverage(("A0", "NOT_RUN"), *BASE_ROWS, ("A3", "NOT_RUN"), ("A4", "NOT_RUN")))
        self.refused_carry("candidate-only", [COVERAGE])

    def test_rows_are_judged_against_the_merge_base(self):
        """A review admitted after the merge binds only its own rows, not the target's."""
        self.target(coverage(("A0", "NOT_RUN"), *BASE_ROWS, ("A3", "NOT_RUN"), ("A4", "NOT_RUN")))
        sources = {item["path"]: item for item in self.reviewed()["sources"]}
        self.assertEqual(list(sources[COVERAGE]["index"]["rows"]), ['["scenarios","A10"]'])

    def test_the_delivered_tree_reader_judges_bound_rows(self):
        bound = next(item for item in self.reviewed()["sources"] if item["path"] == COVERAGE)
        self.target(coverage(("A0", "NOT_RUN"), *BASE_ROWS, ("A3", "NOT_RUN"), ("A4", "NOT_RUN")))
        merged = self.candidate
        self.assertTrue(tree_source_current(self.project, merged)(bound))
        self.assertFalse(tree_source_current(self.project, self.base)(bound))   # A10 is absent there
        view = kernel_context(self.project, "objective", read(self.project, "objective"), None)
        self.assertTrue(view["source_current"](bound))
        self.assertTrue(view["source_current_at"](merged)(bound))

    def test_a_caller_cannot_supply_bound_rows(self):
        self.intake()
        forged = {**source_identity(self.project, COVERAGE), "index": {"schema": "0" * 64, "rows": {}}}
        with self.assertRaises(PodError) as caught:
            self.packet(["O1"], sources=[forged])
        self.assertIn("index", str(caught.exception))

    def test_map_evidence_cannot_supply_bound_rows(self):
        self.intake()
        forged = {**source_identity(self.project, COVERAGE), "index": {"schema": "0" * 64, "rows": {}}}
        with self.assertRaises(PodError) as caught:
            self.write([self.criterion(state="satisfied", evidence=[self.proof("O1", sources=[forged])])])
        self.assertEqual(caught.exception.code, "obligation_invalid", str(caught.exception))
        self.assertEqual(caught.exception.detail["referent"]["unsupported"], ["index"])
