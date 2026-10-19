"""Unit-scoped required audits through production boundaries and recording ports."""

from copy import deepcopy
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import patch

from pod.config import effective
from pod.errors import PodError
from pod.governor import decide, _read_journal, _record_path
from pod.ledger import kernel_view, objective_root, read, logical_projection
from pod.operations import OrcaPort
from tests.issue41_support import ProductionCase
from tests import kernel_support as ks

REPOSITORY = Path(__file__).resolve().parents[1]
PREVIOUS = "29a57a5e3a42e2e075d806f3a7da4610a4ffe8c6"


class UnitAuditTests(ProductionCase):
    def audits(self, *, bound=True):
        (self.project / "AGENTS.md").write_text(ks.AGENTS)
        ks.git(self.project, "add", "AGENTS.md")
        ks.git(self.project, "commit", "-qm", "trusted audit rule")
        self.base = self.candidate = ks.git(self.project, "rev-parse", "HEAD")
        ks.git(self.project, "branch", "-f", "target", self.base)
        ks.git(self.project, "update-ref", "refs/remotes/origin/target", self.base)
        self.intake(*(ks.governance_policy(self, "audit-" + unit,
                          scope={"paths": ["src" if unit == "A" else "README.md"]},
                          **({"unit": unit} if bound else {}))
                      for unit in ("A", "B")))
        self.bindings = {unit: self.op("governor-prepare", unit=unit, branch={
            "remote": "origin", "base": "target", "branch": "delivery-" + unit})["candidate"]
                         for unit in ("A", "B")}

    def decision(self, unit):
        binding = self.bindings[unit]
        return self.op("governor", action={"kind": "merge", "candidate": binding["id"], "unit": unit,
            "target": "origin/target", "reason": "deliver unit", "effects": [],
            "authorization": ks.authorization(candidate=binding["commit"], tree=binding["tree"],
                                               scope=("merge",), target="origin/target")})

    def finish(self, unit):
        frozen = self.packet(["audit-" + unit], role="review")
        admission = self.start("review-" + unit, frozen)["admission"]
        self.settle(admission)
        rows = self.stored()
        audit = next(row for row in rows if row["id"] == "audit-" + unit)
        audit.pop("executor")
        audit.update(state="satisfied", evidence=[{"attempt": admission["admission_id"]}])
        self.report(admission, frozen, map={"obligations": rows})

    def test_two_units_merge_without_withdrawing_later_audit(self):
        self.audits()
        self.finish("A")
        first = self.decision("A")
        self.assertEqual(first["decision"], "ALLOW", first)
        self.assertEqual(next(row for row in self.stored() if row["id"] == "audit-B")["state"], "waiting")
        second = self.decision("B")
        self.assertEqual(second["decision"], "DEFER", second)
        gap = next(reason for reason in second["reasons"] if reason["code"] == "assurance_unbound")
        self.assertIn("audit-B", gap["detail"])
        self.assertNotIn("audit-A", gap["detail"])
        self.finish("B")
        self.assertEqual(self.decision("B")["decision"], "ALLOW")
        self.assertFalse(self.remote.calls)  # Merge remains decide-only.

    def test_unbound_audit_gates_both_units(self):
        self.audits(bound=False)
        self.finish("A")
        for unit in ("A", "B"):
            with self.subTest(unit=unit):
                result = self.decision(unit)
                self.assertEqual(result["decision"], "DEFER")
                self.assertIn("assurance_unbound", [reason["code"] for reason in result["reasons"]])

    def test_running_later_audit_still_holds_objective_wide_release(self):
        self.audits()
        self.finish("A")
        self.start("review-B", self.packet(["audit-B"], role="review"))
        result = self.decision("A")
        self.assertEqual(result["decision"], "DEFER")
        codes = [reason["code"] for reason in result["reasons"]]
        self.assertIn("integration_unsettled", codes)
        self.assertNotIn("assurance_unbound", codes)

    def test_label_and_acceptance_need_every_assurance(self):
        self.audits()
        self.finish("A")
        view = kernel_view(self.project, "objective")
        self.assertEqual(view["label"]["label"], "WITHHELD")
        self.assertEqual(view["label"]["assurance_unbound"], [{"obligation": "audit-B", "gap": "unbound"}])
        from tests.test_issue29_boundaries import RequiredAuditTests
        acceptance = RequiredAuditTests.acceptance(self)
        self.assertFalse(acceptance["accepted"])
        self.assertEqual(acceptance["independently_reviewed"], "WITHHELD")
        self.finish("B")
        self.assertEqual(kernel_view(self.project, "objective")["label"]["label"], "QUALIFIED")
        self.assertTrue(RequiredAuditTests.acceptance(self)["accepted"])

    def test_an_existing_unbound_audit_cannot_be_rebound(self):
        self.audits(bound=False)
        rows = self.stored()
        rows[1]["unit"] = "B"
        with self.assertRaises(PodError) as caught:
            self.write(rows, revision_authority=ks.USER)
        self.assertEqual((caught.exception.code, caught.exception.detail["detail"]),
                         ("obligation_invalid", "redefinition_unauthorized"))
        self.assertNotIn("unit", self.stored()[1])

    def test_binding_is_assurance_only_validated_and_immutable(self):
        self.audits()
        before = read(self.project, "objective")
        cases = (("audit-A", "B"), ("audit-A", None), ("audit-A", "bad/name"), ("O1", "A"))
        for key, value in cases:
            rows = deepcopy(self.stored())
            row = next(row for row in rows if row["id"] == key)
            if value is None:
                row.pop("unit")
            else:
                row["unit"] = value
            with self.subTest(key=key, value=value), self.assertRaises(PodError) as caught:
                self.write(rows, revision_authority=ks.USER)
            self.assertEqual(caught.exception.code, "obligation_invalid")
            self.assertEqual(read(self.project, "objective"), before)
        # Unit existence is checked at delivery, never at obligation creation.
        self.write([*self.stored(), ks.governance_policy(self, "future", unit="future-unit", scope={"paths": ["future.py"]})])
        self.assertEqual(self.stored()[-1]["unit"], "future-unit")


class SingleUnitCompatibilityTests(ks.KernelCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        parent = Path(os.environ["POD_CACHE_HOME"]) if "POD_CACHE_HOME" in os.environ else None
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        cls.previous_home = tempfile.TemporaryDirectory(prefix="unit-audit-previous-", dir=parent)
        cls.addClassCleanup(cls.previous_home.cleanup)
        cls.bundle = Path(cls.previous_home.name)
        archive = cls.bundle / "source.tar"
        subprocess.run(["git", "-C", str(REPOSITORY), "archive", "-o", str(archive), PREVIOUS,
                        "skills", "VERSION"], check=True)
        with tarfile.open(archive) as source:
            source.extractall(cls.bundle, filter="data")
        cls.recorded = cls.bundle / "recorded"
        cls.recorded.mkdir()
        completed = subprocess.run([sys.executable, str(REPOSITORY / "tests/unit_audit_previous_writer.py"),
                                    str(cls.recorded)], cwd=REPOSITORY,
            env={**os.environ, "PYTHONPATH": os.pathsep.join((str(cls.bundle / "skills"), str(REPOSITORY)))},
            capture_output=True, text=True, timeout=15)
        if completed.returncode:
            raise AssertionError(completed.stderr)

    def test_genuine_0150_single_unit_decisions_codes_and_journal_rows_match(self):
        destination = self.root / "previous"
        shutil.copytree(self.recorded, destination)
        self.assertEqual(json.loads((destination / "provenance.json").read_text())["version"], "0.15.0")
        self.project = destination / "project"
        self.base = self.candidate = ks.git(self.project, "rev-parse", "HEAD")
        native = patch.object(OrcaPort, "read_native", autospec=True,
            side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs))
        native.start(); self.addCleanup(native.stop)
        for stage in ("missing", "satisfied", "attached"):
            snapshot = json.loads((destination / (stage + ".json")).read_text())
            self.port.workers = snapshot["workers"]
            path = objective_root(self.project, "objective") / "context.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(snapshot["state"]))
            journal_path = _record_path(self.project, "objective")
            journal_path.write_text(json.dumps(snapshot["journal_before"]))
            self.write(self.stored())  # Ordinary checkpoint reloads the new bundle identity.
            state = read(self.project, "objective")
            observed = self.port.read_native("owner", authority_runs=("run",),
                                             assignments=tuple(state["admissions"].values()))
            result = decide(self.project, "objective", owner="owner", action=snapshot["action"], now=ks.NOW,
                            native_projection=logical_projection(self.project, observed, objective="objective"))
            # The one intended change since 0.15.0 (R93): a merge admitted now also records the exact target commit
            # currency accepted, here the bound policy base because this objective has no delivery yet.
            expected = deepcopy(snapshot["journal_after"])
            before = {row["record_id"] for row in snapshot["journal_before"]["actions"]}
            base = state["checkpoint"]["governance"]["base"]
            for row in expected["actions"]:
                if row["record_id"] not in before and row["action"]["kind"] == "merge" and "target_binding" in row:
                    row["target_binding"]["commit"] = base
            with self.subTest(stage=stage):
                self.assertEqual(result, snapshot["decision"])
                self.assertEqual(_read_journal(journal_path), expected)
