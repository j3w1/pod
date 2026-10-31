"""Sixty actual admission/report transitions through injected native ports (R121)."""
from tests.history_support import compact
from copy import deepcopy
import unittest

from pod.history import (CONTEXT_LIMIT, hydrate)
from pod.ledger import _read, _outstanding_ids, objective_root
from pod.util import encoded
from tests.issue41_support import ProductionCase
from tests.history_support import size


class LongObjectiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Build once: method timing and tests never wait for time to pass.
        case = ProductionCase()
        case.setUp()
        try:
            case.write([case.criterion(), case.sub("S")], governance={"base_ref": "refs/remotes/origin/target"})
            cls.physical_sizes = []
            for number in range(60):
                if number and number % 3 == 0:
                    case.move()
                    case.write(case.stored())
                frozen = case.packet(["S"], role="investigate", resolves="Is this candidate consistent?",
                                     stop_condition="Report the observed result", report_contract="observe " * 300)
                admission = case.start(f"synthetic-investigation-{number}", frozen)["admission"]
                case.settle(admission)
                rows = deepcopy(case.stored())
                sub = rows[1]
                sub.pop("executor")
                sub.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
                report = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                          "attempt": admission["native_binding"]["dispatchId"], "candidate": case.candidate,
                          "outcome": "succeeded", "scope": frozen["body"]["scope"], "files": [], "checks": ["unit"],
                          "failures": [], "evidence": [f"synthetic-observation-{i}:" + "x" * 480 for i in range(30)],
                          "uncertainty": [], "questions": []}
                case.op("report", admission_id=admission["admission_id"], packet=frozen, report=report,
                        map={"obligations": rows})
                path = objective_root(case.project, "objective") / "context.json"
                cls.physical_sizes.append(path.stat().st_size)
            cls.logical = _read(path)
            cls.native = case.port.read_native("owner", authority_runs=("run",), assignments=tuple(cls.logical["admissions"].values()))
            cls.record, cls.chunks = compact(cls.logical, force=True)
            cls.restored = hydrate(cls.record, cls.chunks.__getitem__)
            from pod.governor import _read_journal
            cls.journal = _read_journal(path.parent / "governor.json")
            print("production_long_objective=" + str({"admissions": len(cls.logical["admissions"]),
                "candidates": len(cls.logical["checkpoint"]["governance_history"]["candidates"]),
                "logical_bytes": size(cls.logical), "physical_bytes": path.stat().st_size,
                "largest_transition_bytes": max(cls.physical_sizes)}))
        finally:
            case.doCleanups()

    def test_sixty_real_admissions_and_consumed_reports_remain_bounded(self):
        self.assertEqual(len(self.logical["admissions"]), 60)
        self.assertTrue(all(row["report"]["status"] == "validated_observation" for row in self.logical["admissions"].values()))
        self.assertEqual(len(self.logical["checkpoint"]["governance_history"]["candidates"]), 20)
        self.assertLess(max(self.physical_sizes), CONTEXT_LIMIT)
        self.assertGreater(size(self.logical), CONTEXT_LIMIT)
        self.assertLess(max(self.physical_sizes), CONTEXT_LIMIT)
        self.assertEqual(self.logical, self.restored)

    def test_readiness_quiescence_report_and_governor_outcomes_are_identical(self):
        from pod.governor import finalization
        from pod.obligations import quiescence
        from pod.readiness import base
        def projections(state):
            outstanding = _outstanding_ids(state, self.native)
            return {"outstanding": outstanding, "finalization": finalization(state, self.journal),
                    "readiness": base(state, self.journal, outstanding=set(outstanding)),
                    "quiescence": quiescence(state["checkpoint"])}
        self.assertEqual(encoded(projections(self.logical), compact=True), encoded(projections(self.restored), compact=True))
