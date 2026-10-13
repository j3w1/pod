"""R109: coordinator-recorded acceptance of outside results on obligation rows."""
import json
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.ledger import checkpoint, kernel_view, map_read, objective_root, read
from pod.operations import OrcaPort
from tests.kernel_support import KernelCase

ACCEPTED = {"source": "op-a1b2c3d4e5f6", "verdict": "accepted", "candidate": "deploy 41",
            "evidence": "evidence/shn/status.json"}
REJECTED = {"source": "op-0f9e8d7c6b5a", "verdict": "rejected", "candidate": "deploy 40",
            "evidence": "evidence/shn/failed.json"}


class AcceptanceCase(KernelCase):
    def setUp(self):
        super().setUp()
        self.intake()

    def accept(self, *requests, rows=None, **fields):
        return self.write(rows if rows is not None else self.stored(),
                          acceptances=[{"obligation": "O1", **row} for row in requests], **fields)

    def recorded(self, objective="objective"):
        return next(row for row in read(self.project, objective)["checkpoint"]["obligations"]
                    if row["id"] == "O1").get("acceptances")

    def public_rows(self):
        with patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            view = kernel_view(self.project, "objective")
        return {row["id"]: row for row in view["status"]["map"]["obligations"]}


class AcceptanceTests(AcceptanceCase):
    def test_accepted_and_rejected_sources_are_recorded_with_their_sequence(self):
        written = self.accept(ACCEPTED, REJECTED)["checkpoint"]
        seq = written["seq"]
        self.assertEqual(self.recorded(), [{**ACCEPTED, "seq": seq}, {**REJECTED, "seq": seq}])
        self.assertEqual(self.public_rows()["O1"]["acceptances"], self.recorded())
        self.assertEqual(map_read(self.project, "objective")["acceptances"], {"O1": self.recorded()})
        self.assertNotIn("acceptances", map_read(self.project, "objective")["obligations"][0])

    def test_records_survive_later_writes_and_identical_restatement(self):
        first = self.accept(ACCEPTED)["checkpoint"]["seq"]
        self.write(self.stored())  # Stored rows round-trip, carrying the record unchanged.
        self.write(map_read(self.project, "objective")["obligations"])  # Restatable rows omit it.
        self.accept(ACCEPTED)  # An identical restatement adds nothing.
        later = self.accept(REJECTED)["checkpoint"]["seq"]
        self.assertEqual(self.recorded(), [{**ACCEPTED, "seq": first}, {**REJECTED, "seq": later}])

    def test_a_recorded_source_is_immutable_and_rows_cannot_forge_records(self):
        self.accept(ACCEPTED)
        path = objective_root(self.project, "objective") / "context.json"
        before = path.read_bytes()
        self.refused("obligation_invalid", "acceptance_conflict", self.accept,
                     {**ACCEPTED, "verdict": "rejected"})
        rows = self.stored()
        rows[0]["acceptances"] = [{**ACCEPTED, "seq": 99}]
        self.refused("obligation_invalid", "acceptance_kernel_field", self.write, rows)
        rows[0]["acceptances"] = [dict(REJECTED)]  # Without a sequence, the natural caller mistake.
        self.refused("obligation_invalid", "acceptance_kernel_field", self.write, rows)
        fresh = {**self.sub("S1"), "acceptances": [dict(ACCEPTED)]}
        self.refused("obligation_invalid", "acceptance_kernel_field", self.write, [*self.stored(), fresh])
        for bad in ({**REJECTED, "source": "has space"}, {**REJECTED, "verdict": "maybe"},
                    {**REJECTED, "evidence": ""}, {**REJECTED, "extra": "x"}):
            with self.subTest(bad=bad):
                self.refused("obligation_invalid", None, self.accept, bad)
        self.refused("obligation_invalid", "malformed", self.write, self.stored(),
                     acceptances=[{"obligation": "missing", **REJECTED}])
        self.assertEqual(path.read_bytes(), before)

    def test_reopening_a_satisfied_obligation_clears_its_records(self):
        rows = [self.criterion("satisfied", evidence=[self.proof("O1")])]
        self.accept(ACCEPTED, rows=rows)
        self.assertEqual(len(self.recorded()), 1)
        self.write(self.stored())
        self.assertEqual(len(self.recorded()), 1)
        self.write([self.criterion()])  # satisfied -> active
        self.assertIsNone(self.recorded())
        self.assertNotIn("acceptances", self.public_rows()["O1"])

    def test_closed_objective_keeps_records_written_with_its_closure(self):
        rows = [self.criterion("satisfied", evidence=[self.proof("O1")])]
        closed = self.accept(ACCEPTED, REJECTED, rows=rows, close=True)["checkpoint"]
        self.assertIsNotNone(closed["closure"])
        self.assertEqual([row["source"] for row in self.public_rows()["O1"]["acceptances"]],
                         [ACCEPTED["source"], REJECTED["source"]])
        self.assertNotIn("acceptances", json.dumps(closed["closure"]["report"]))
        self.refused("objective_closed", None, self.accept, {**ACCEPTED, "source": "op-later"})

    def test_only_the_recorded_coordinator_writes_records(self):
        path = objective_root(self.project, "objective") / "context.json"
        before = path.read_bytes()
        with self.assertRaises(PodError) as caught:
            checkpoint(self.project, "objective", owner="other",
                       value=self.core(obligations=self.stored(), acceptances=[{"obligation": "O1", **ACCEPTED}]),
                       native={"runtime": "runtime", "delegation": "available"})
        self.assertIn(caught.exception.code, ("coordinator_conflict", "native_authority_unverified"))
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
