"""Sanitized 96-row acceptance shape through real report ingestion and storage."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from pod.errors import PodError
from pod.ledger import _read, _write, objective_root
from pod.history import CONTEXT_LIMIT, compact, is_reference
from pod.util import encoded
from tests.issue41_support import ProductionCase
from tests import kernel_support as boundaries
from tests.common import proof


def size(value):
    return len(encoded(value, compact=True))


class RecordsCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = cls.case = ProductionCase(); case.setUp()
        cls.addClassCleanup(case.doCleanups)
        (case.project / "AGENTS.md").write_text(boundaries.AGENTS)
        for index in range(8):
            (case.project / "src" / (f"source-{index}-" + "s" * 60 + ".py")).write_text("fixture\n")
        boundaries.git(case.project, "add", ".")
        boundaries.git(case.project, "commit", "-qm", "synthetic capacity source")
        case.base = case.candidate = boundaries.git(case.project, "rev-parse", "HEAD")
        boundaries.git(case.project, "branch", "-f", "target", case.base)
        boundaries.git(case.project, "update-ref", "refs/remotes/origin/target", case.base)
        case.intake(boundaries.governance_policy(case, "A"))
        from pod.records import source_identity
        sources = [source_identity(case.project, path.relative_to(case.project).as_posix())
                   for path in sorted((case.project / "src").glob("source*"))]
        cls.path = objective_root(case.project, "objective") / "context.json"
        attempts = []
        for group in range(12):
            frozen = case.packet(["A"], role="review", sources=sources)
            admission = case.start(f"seed-review-{group}", frozen)["admission"]; case.settle(admission)
            attempts.append(admission["admission_id"])
            state = _read(cls.path)
            start, stop = ((0, 32) if group == 0 else (32, 53) if group == 1 else (53, 53))
            corrections = {f"K{index}": {"kind": "correction", "provenance": "coordinator", "parent": "A", "state": "waiting",
                "check": "repair the retained finding", "wait": {"class": "sequenced", "referent": "O1"}} for index in range(start, stop)}
            findings = [{"finding": f"F{index}", "severity": "major", "triage": "required_correction", "correction": f"K{index}",
                         "summary": "synthetic required finding " + "f" * 145} for index in range(start, stop)]
            case.report(admission, frozen, map={"seq": state["checkpoint"]["seq"] + 1, "update": {"A": {"state": "waiting",
                "wait": {"class": "sequenced", "referent": "O1"}, "evidence": [{"attempt": key} for key in attempts]}, **corrections}}, triage=findings)
        rows = case.stored()
        rows.extend(case.sub(f"S{index}") for index in range(41))
        policy = case.core()["policy_revision"]
        for row in rows:
            if row["id"] == "A":
                row.pop("wait")
                row.update(state="blocked_external", external={"party": "provider", "need": "fixture review slot", "unblocks_when": "available"})
                continue
            row["check"] = "synthetic retained check " + "c" * 480
            receipt = proof(row["id"], case.candidate, definition=row, policy_revision=policy, reference="retained-" + row["id"])
            receipt.update(command="c" * 512, result="r" * 280)
            row["evidence"] = [receipt]
        # Changing checks is authorized only in this synthetic setup.
        case.write(rows, revision_authority={"provenance": "user_direct", "instruction": "Set the synthetic fixture checks"})
        rows = case.stored()
        for row in rows:
            if row["id"] in {f"S{index}" for index in range(7)}:
                row.pop("wait"); row.update(state="withdrawn", withdrawal={"by": "coordinator", "reason": "synthetic completed branch"})
        case.write(rows)
        state = _read(cls.path)
        # Fill legal opaque request metadata, leaving every proof binding intact.
        # The production writer still plans, chunks, preflights and publishes it.
        physical, _ = compact(state, force=True)
        allowance = 488 * 1024 - size(physical) - 64
        if allowance > 0:
            first = next(iter(state["admissions"].values()))
            first["request"]["synthetic_padding"] = "p" * allowance
        _write(cls.path, state)
        cls.before = _read(cls.path)
        cls.metrics = {"fixture": "sanitized-96-row", "before": {**cls.measure(),
            "full_map_request_bytes": size(case.core(obligations=cls.before["checkpoint"]["obligations"]))}, "groups": []}
        frozen = case.packet(["A"], role="review", sources=sources)
        admission = case.start("original-capacity-review", frozen)["admission"]; case.settle(admission)
        cls.original = admission["admission_id"]
        report_body = {"schema": "pod-report/v1", "assignment": frozen["packet_id"], "attempt": admission["native_binding"]["dispatchId"],
            "candidate": case.candidate, "outcome": "succeeded", "scope": frozen["body"]["scope"], "files": [], "checks": ["unit"],
            "failures": [], "evidence": [], "uncertainty": [], "questions": []}
        def request_bytes(request_map, findings=None):
            return size({"project": str(case.project), "objective": "objective", "admission_id": admission["admission_id"],
                         "packet": frozen, "report": report_body, "map": request_map, **({"triage": findings} if findings is not None else {})})
        for group in range(7):
            state = _read(cls.path)
            key = f"NEW{group}"
            correction = {"kind": "correction", "provenance": "coordinator", "parent": "A", "state": "waiting", "check": "repair this group",
                          "wait": {"class": "sequenced", "referent": "O1"}}
            request_map = {"seq": state["checkpoint"]["seq"] + 1, "update": {"A": {"state": "blocked_external", "external": {
                "party": "provider", "need": "fixture review slot", "unblocks_when": "available"}}, key: correction}}
            findings = [{"finding": f"new-{group}-{index}", "severity": "major", "triage": "required_correction", "correction": key,
                         "summary": "genuine report consumption in the synthetic capacity fixture"} for index in range(3 if group == 0 else 1)]
            request_size = request_bytes(request_map, findings)
            case.report(admission, frozen, map=request_map, triage=findings)
            cls.metrics["groups"].append({"request_bytes": request_size, **cls.measure()})
        cls.after = _read(cls.path)
        cls.files_before_refusal = cls.files()
        state = _read(cls.path)
        try:
            case.report(admission, frozen, map={"seq": state["checkpoint"]["seq"] + 1, "update": {
                f"TOO{i}": {"kind": "correction", "provenance": "coordinator", "parent": "A", "check": "synthetic", "state": "unassigned"} for i in range(40)}})
        except PodError as exc:
            cls.refusal = exc
        else:
            raise AssertionError("129-live refusal missing")
        cls.metrics["refusal"] = {"code": cls.refusal.code, "detail": cls.refusal.detail,
                                 "unchanged": cls.files() == cls.files_before_refusal}
        updates = {}
        for index in range(17):
            row = case.sub(f"LARGE{index}")
            row["check"] = "large legal-size check " + "c" * 480
            receipt = proof(row["id"], case.candidate, definition=row, policy_revision=policy)
            receipt.update(command="c" * 512, result="r" * 512)
            row["evidence"] = [receipt]
            updates[row["id"]] = row
        try:
            case.report(admission, frozen, map={"seq": state["checkpoint"]["seq"] + 1, "update": updates})
        except PodError as exc:
            cls.physical_refusal = exc
        else:
            raise AssertionError("physical capacity refusal missing")
        cls.metrics["physical_refusal"] = {"code": cls.physical_refusal.code, "detail": cls.physical_refusal.detail,
            "request_bytes": request_bytes({"seq": state["checkpoint"]["seq"] + 1, "update": updates}), "unchanged": cls.files() == cls.files_before_refusal}
        print("records_capacity=" + json.dumps(cls.metrics, sort_keys=True))

    @classmethod
    def files(cls):
        return {str(path.relative_to(cls.path.parent)): path.read_bytes() for path in cls.path.parent.rglob("*") if path.is_file() and path.name != ".lock"}

    @classmethod
    def measure(cls):
        state = _read(cls.path)
        rows = state["checkpoint"]["obligations"]
        assurance = next(row for row in rows if row["id"] == "A")
        physical = json.loads(cls.path.read_bytes())
        return {"logical_bytes": size(state), "physical_bytes": cls.path.stat().st_size,
                "chunk_bytes": sum(path.stat().st_size for path in cls.path.parent.glob("history/sha256/*.json")),
                "rows": len(rows), "row_bytes": size(rows), "live": sum(row["state"] not in ("withdrawn", "renewed") for row in rows),
                "states": {name: sum(row["state"] == name for row in rows) for name in ("withdrawn", "waiting", "active", "blocked_external")},
                "corrections": sum(row["kind"] == "correction" for row in rows), "assurance_bytes": size(assurance),
                "selected_bytes": size(assurance.get("evidence", [])), "finding_bytes": size(assurance.get("findings", [])), "receipt_bytes": size(assurance["receipts"]),
                "terminal_chunks": sum(is_reference(row) for row in physical["checkpoint"]["obligations"])}

    def test_acceptance_shape_and_original_three_findings_share_one_new_correction(self):
        before = self.metrics["before"]
        self.assertEqual(before["rows"], 96)
        self.assertEqual(before["states"], {"withdrawn": 7, "waiting": 87, "active": 1, "blocked_external": 1})
        self.assertEqual(before["corrections"], 53)
        self.assertGreater(before["row_bytes"], 380 * 1024)
        self.assertLess(before["row_bytes"], 420 * 1024)
        self.assertAlmostEqual(before["physical_bytes"], 488 * 1024, delta=1024)
        rows = {row["id"]: row for row in self.after["checkpoint"]["obligations"]}
        self.assertEqual(rows["NEW0"]["finding"]["findings"], ["new-0-0", "new-0-1", "new-0-2"])
        self.assertEqual(rows["NEW0"]["finding"]["attempt"], self.original)

    def test_subsequent_groups_preserve_every_historical_row_and_receipt(self):
        before = {row["id"]: row for row in self.before["checkpoint"]["obligations"]}
        after = {row["id"]: row for row in self.after["checkpoint"]["obligations"]}
        for key, row in before.items():
            if key != "A":
                self.assertEqual(after[key], row)
        self.assertEqual(after["A"]["receipts"][:-1], before["A"]["receipts"])
        self.assertEqual(after["A"]["findings"][:53], before["A"]["findings"])
        self.assertTrue(all(row["request_bytes"] < 131072 and row["physical_bytes"] <= CONTEXT_LIMIT for row in self.metrics["groups"]))
        self.assertEqual(len(self.metrics["groups"]), 7)
        self.assertEqual(self.metrics["refusal"]["detail"]["detail"], "row_limit")
        self.assertTrue(self.metrics["refusal"]["unchanged"])
        self.assertEqual(self.metrics["physical_refusal"]["code"], "record_too_large")
        self.assertTrue(self.metrics["physical_refusal"]["unchanged"])
