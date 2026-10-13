"""Older-schema records prove handoff non-membership only from readable, unmentioned Run references."""
import json
import unittest

from pod.ledger import objective_root
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
    "older-reference-unreadable": (older_record(native_refs=[{"runtime": "rt"}]), "scope_unreadable"),
    "future-foreign": (older_record(schema="pod-checkpoint/v9"), "scope_unreadable"),
    "unknown-family": (older_record(schema="other-checkpoint/v1"), "scope_unreadable"),
}


class OlderSchemaScopeTests(HandoffCase):
    def test_older_schema_records_prove_nonmembership_only_without_the_run(self):
        for kind, (record, expected) in CASES.items():
            case = HandoffCase(); case.setUp()
            try:
                with self.subTest(kind=kind):
                    case.establish()
                    peer = objective_root(case.project, "earlier") / "context.json"
                    peer.parent.mkdir(parents=True)
                    peer.write_text(json.dumps(record))
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


if __name__ == "__main__":
    unittest.main()
