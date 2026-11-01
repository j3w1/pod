"""Records outside the documented layouts hold handoff scope only when they could name the Run (R206)."""
import json
import unittest

from pod.ledger import objective_root
from tests.common import GroupedTable, assert_grouped_table
from tests.test_owner_handoff import HandoffCase


def older_record(**checkpoint):
    """The hand-written shape of an earlier writer's record, as the cutover tests use it."""
    value = {"schema": "pod-checkpoint/v2", "objective": "earlier",
             "native_refs": [{"runId": "elsewhere", "runtime": "rt"}],
             "next_safe_action": "continue the earlier work"}
    value.update(checkpoint)
    return {"schema": "pod-context/v4", "revision": 1, "owner": "former", "checkpoint": value,
            "admissions": {}, "interventions": {}, "source_rejections": {}, "constraints": []}


CASES = {
    # kind: (record, expected condition; None means the handoff is available)
    "older-foreign": (older_record(), None),
    "older-mentions-run": (older_record(next_safe_action="select run again"), "scope_unreadable"),
    "older-referenced": (older_record(native_refs=[{"runId": "run", "runtime": "rt"}]), "run_shared"),
    # R206: a record that cannot prove non-membership holds the Run only when it mentions it.
    "older-reference-unreadable": (older_record(native_refs=[{"runtime": "rt"}]), None),
    "future-foreign": (older_record(schema="pod-checkpoint/v9"), None),
    "unknown-family": (older_record(schema="other-checkpoint/v1"), None),
    "future-mentions-run": (older_record(schema="pod-checkpoint/v9", next_safe_action="select run again"),
                            "scope_unreadable"),
    "empty-record": ("", "scope_unreadable"),  # Undecodable: membership unknown, so it holds.
    "escaped-mention": (json.dumps(older_record(schema="pod-checkpoint/v9", next_safe_action="select run again"))
                        .replace("select run", "select r\\u0075n"), "scope_unreadable"),
    "decodable-foreign": ('["elsewhere"]', None),
    "broken-mentions-run": ('{"checkpoint": {"native_refs": [{"runId": "run"', "scope_unreadable"),
}


class OlderSchemaScopeTests(GroupedTable):
    """Each record shape, one named group method per few rows so suite lanes can spread them."""
    GROUPS = {"test_older_records": ["older-foreign", "older-mentions-run", "older-referenced"],
              "test_unproved_layouts": ["older-reference-unreadable", "future-foreign", "unknown-family"],
              "test_records_that_could_name_the_run": ["future-mentions-run", "empty-record", "broken-mentions-run"],
              "test_decoded_membership": ["escaped-mention", "decodable-foreign"]}

    def row(self, kind):
        record, expected = CASES[kind]
        case = HandoffCase(); case.setUp()
        try:
            with self.subTest(kind=kind):
                case.establish()
                peer = objective_root(case.project, "earlier") / "context.json"
                peer.parent.mkdir(parents=True)
                peer.write_text(record if isinstance(record, str) else json.dumps(record))
                case.lose()
                before = case.snapshot()
                verdict = case.status()["owner_handoff"]
                if expected is None:
                    self.assertEqual(verdict["status"], "handoff_available", verdict)
                    case.handoff()
                    self.assertEqual(case.entry()["state"], "done")
                else:
                    self.assertEqual(verdict["condition"], expected)
                    case.refused(lambda: case.handoff())
                    self.assertEqual(before, case.snapshot())
                    self.assertEqual(case.mutations, [])
                self.assertEqual(peer.read_bytes(), before[str(peer.relative_to(case.root / "state"))])
        finally:
            case.doCleanups()

    def test_older_records(self):
        self.rows()

    def test_unproved_layouts(self):
        self.rows()

    def test_records_that_could_name_the_run(self):
        self.rows()

    def test_decoded_membership(self):
        self.rows()


class OlderSchemaScopeShapeTests(unittest.TestCase):
    def test_the_scope_table_runs_every_row_once(self):
        assert_grouped_table(self, OlderSchemaScopeTests, list(CASES), limit=3)


if __name__ == "__main__":
    unittest.main()
