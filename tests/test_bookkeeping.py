"""Restatable map reads and concise, fully validated writes."""

from __future__ import annotations

from pod.errors import PodError
from pod.internal import run as internal_run
from pod.ledger import checkpoint, map_read, read
from pod.operations import OrcaPort
from pod.util import bounded_text
from tests.kernel_support import KernelCase, git
from unittest.mock import patch


class BookkeepingTests(KernelCase):
    def patch_map(self, seq, update, **fields):
        return checkpoint(self.project, "objective", owner="owner",
                          value={"schema": "pod-checkpoint/v3", "seq": seq, "update": update, **fields},
                          native={"runtime": "runtime", "delegation": "available"})

    def test_map_read_is_restatable_and_carries_objective_evidence(self):
        state = self.intake()["checkpoint"]
        viewed = map_read(self.project, "objective")
        self.assertEqual((viewed["seq"], viewed["next_seq"], viewed["revision"]),
                         (state["seq"], state["seq"] + 1, state["revision"]))
        self.assertEqual((viewed["candidate"], viewed["delivery"], viewed["closure"]),
                         (self.base, None, None))
        self.assertEqual(viewed["obligations"][0]["id"], "O1")
        self.assertNotIn("definition", viewed["obligations"][0])
        self.assertNotIn("introduced_seq", viewed["obligations"][0])
        self.assertEqual(viewed["outstanding"], [])
        self.assertEqual(viewed["undispositioned"], [])
        self.assertIn("continue O1", viewed["next_actions"][0])
        self.assertEqual(internal_run("map", {"project": str(self.project),
                                              "objective": "objective"})["seq"], viewed["seq"])

    def test_update_patch_carries_core_and_drops_previous_state_record(self):
        first = self.intake()["checkpoint"]
        written = self.patch_map(first["seq"] + 1,
                                 {"O1": {"state": "blocked_external",
                                         "external": {"party": "provider", "need": "runner access",
                                                      "unblocks_when": "quota resets"}}})["checkpoint"]
        self.assertEqual(written["criteria"], first["criteria"])
        self.assertEqual(written["candidate"], first["candidate"])
        self.assertEqual(written["native_refs"], first["native_refs"])
        row = written["obligations"][0]
        self.assertNotIn("executor", row)
        self.assertEqual(row["state"], "blocked_external")

    def test_short_evidence_expands_and_stale_update_names_map_read(self):
        first = self.intake()["checkpoint"]
        with self.assertRaises(PodError) as stale:
            self.patch_map(first["seq"], {"O1": {"state": "satisfied"}})
        self.assertEqual(stale.exception.code, "map_stale")
        self.assertEqual(stale.exception.detail["referent"]["current_seq"], first["seq"])
        self.assertIn("pod internal map", str(stale.exception))
        short = {"check": "unit", "command": "python -m unittest", "result": "passed",
                 "reference": "unit-log", "timestamp": "2026-09-25T15:01:22-05:00"}
        written = self.patch_map(first["seq"] + 1,
                                 {"O1": {"state": "satisfied", "evidence": [short]}})["checkpoint"]
        evidence = written["obligations"][0]["evidence"][0]
        self.assertEqual((evidence["schema"], evidence["criterion"], evidence["candidate"], evidence["status"]),
                         ("pod-evidence/v1", "O1", self.base, "PASS"))
        self.assertEqual(evidence["timestamp"], "2026-09-25T20:01:22Z")
        self.assertEqual(evidence["dependencies"], first["verification"]["dependencies"])
        self.assertEqual(evidence["environment"], first["verification"]["environment"])
        self.assertIn("definition", evidence)

    def test_restating_identical_short_evidence_is_idempotent(self):
        first = self.intake()["checkpoint"]
        short = {"check": "unit", "command": "python -m unittest", "result": "passed", "reference": "unit-log"}
        written = self.patch_map(first["seq"] + 1, {"O1": {"state": "satisfied", "evidence": [short]}})
        recorded = written["checkpoint"]["obligations"][0]["evidence"][0]
        again = self.patch_map(written["checkpoint"]["seq"] + 1, {"O1": {"evidence": [short]}})
        self.assertEqual(again["checkpoint"]["obligations"][0]["evidence"][0], recorded)
        with self.assertRaises(PodError) as changed:
            self.patch_map(again["checkpoint"]["seq"] + 1, {"O1": {"evidence": [{**short, "result": "failed"}]}})
        self.assertEqual(changed.exception.detail["detail"], "receipt_conflict")

    def test_served_settled_review_can_use_its_attempt_automatically(self):
        self.intake({"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src correct?",
                     "candidate": self.base, "existing_evidence": "unit", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}})
        frozen = self.packet(["A"], role="review")
        admission = self.start("review", frozen)["admission"]
        self.settle(admission)
        current = read(self.project, "objective")["checkpoint"]
        self.report(admission, frozen, map={"seq": current["seq"] + 1,
                                            "update": {"A": {"state": "satisfied"}}})
        accepted = next(row for row in self.stored() if row["id"] == "A")
        self.assertEqual(accepted["evidence"][0]["attempt"], admission["admission_id"])
        with patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            viewed = map_read(self.project, "objective")
            self.assertEqual(viewed["review_eligibility"]["A"], "bound")
            self.assertEqual(viewed["settled_attempts"]["A"], [admission["admission_id"]])

    def test_bounded_text_code_and_detail_include_field_and_length(self):
        with self.assertRaises(PodError) as refused:
            bounded_text("too long", name="route reason", limit=3)
        self.assertEqual(refused.exception.code, "invalid_route_reason")
        self.assertEqual(refused.exception.detail, {"field": "route reason", "limit": 3, "length": 8})

    def test_integration_observation_uses_bound_target_and_rejects_equal_sha_alias(self):
        self.intake()
        observed = internal_run("integration-observe", {"project": str(self.project),
                                                         "objective": "objective", "candidate": self.base})
        self.assertEqual(observed["base_ref"], "refs/remotes/origin/target")
        git(self.project, "update-ref", "refs/remotes/origin/other", self.base)
        with self.assertRaises(PodError) as mismatch:
            internal_run("integration-observe", {"project": str(self.project), "objective": "objective",
                                                 "candidate": self.base, "base_ref": "origin/other"})
        self.assertEqual(mismatch.exception.code, "target_mismatch")

    def test_governor_prepare_proves_remote_base_alias_against_bound_target(self):
        self.intake()
        git(self.project, "remote", "add", "origin", str(self.project))
        request = {"project": str(self.project), "objective": "objective", "owner": "owner",
                   "unit": "delivery", "branch": {"remote": "origin", "base": "target",
                                                   "branch": "delivery"}}
        with patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            prepared = internal_run("governor-prepare", request)
            self.assertEqual(prepared["candidate"]["base"]["ref"], "refs/remotes/origin/target")
            git(self.project, "update-ref", "refs/remotes/origin/other", self.base)
            with self.assertRaises(PodError) as wrong:
                internal_run("governor-prepare", {**request,
                    "branch": {"remote": "origin", "base": "other", "branch": "delivery"}})
            self.assertEqual(wrong.exception.code, "target_mismatch")

    def test_new_patch_row_is_full_and_update_cannot_mix_with_restatement(self):
        first = self.intake()["checkpoint"]
        with self.assertRaises(PodError) as mixed:
            checkpoint(self.project, "objective", owner="owner",
                       value=self.core(seq=first["seq"] + 1, update={"O1": {}},
                                       obligations=self.stored()),
                       native={"runtime": "runtime", "delegation": "available"})
        self.assertEqual(mixed.exception.detail["detail"], "malformed")
        row = {"kind": "subgoal", "provenance": "coordinator", "parent": "O1",
               "check": "inspect edge case", "state": "waiting",
               "wait": {"class": "sequenced", "referent": "O1"}}
        written = self.patch_map(first["seq"] + 1, {"S": row})["checkpoint"]
        self.assertEqual([item["id"] for item in written["obligations"]], ["O1", "S"])

    def test_boundary_disposition_refusal_names_missing_reason_and_seq(self):
        self.intake()
        frozen = self.packet(["O1"], boundary={"paths": ["src"], "surfaces": []})
        admission = self.start("work", frozen)["admission"]
        (self.project / "outside.txt").write_text("outside scope\n")
        git(self.project, "add", "outside.txt")
        git(self.project, "commit", "-qm", "outside result")
        result = git(self.project, "rev-parse", "HEAD")
        self.settle(admission)
        rows = self.stored()
        rows[0]["executor"] = "coordinator"
        self.report(admission, frozen, result_commit=result, map={"obligations": rows})
        current = read(self.project, "objective")["checkpoint"]
        with self.assertRaises(PodError) as refused:
            self.write(self.stored(), dispositions=[{"admission": admission["admission_id"],
                                                     "integrated_into": result}])
        self.assertEqual(refused.exception.detail["detail"], "disposition_invalid")
        self.assertEqual(refused.exception.detail["current_seq"], current["seq"])
        self.assertEqual(refused.exception.detail["revision"], current["revision"])
        self.assertIn("reason", refused.exception.detail["next_action"])


if __name__ == "__main__":
    import unittest
    unittest.main()
