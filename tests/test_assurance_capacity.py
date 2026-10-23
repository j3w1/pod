"""Bounded retained findings through the production admission/report boundaries."""
from copy import deepcopy
import shutil
import tempfile
import unittest
from unittest.mock import patch

from pod.contracts import bound
from pod.errors import PodError
from pod.ledger import _read, objective_root
from pod.obligations import MAX_FINDINGS, MAX_REPORT_FINDINGS
from tests.issue41_support import ProductionCase
from tests import kernel_support as boundaries
from tests.records_support import start_before_reservations


class AssuranceCapacityTests(unittest.TestCase):
    @staticmethod
    def correction(key):
        return {"id": key, "kind": "correction", "provenance": "coordinator", "parent": "A",
                "check": f"repair {key}", "boundary": {"paths": ["src"]}, "state": "unassigned"}

    @staticmethod
    def advisory(number):
        return {"finding": f"note-{number}", "severity": "minor", "triage": "advisory",
                "summary": f"original reviewer observation {number}"}

    @staticmethod
    def required(key):
        return {"finding": key, "severity": "blocker", "triage": "required_correction",
                "summary": f"original required defect {key}", "correction": key}

    @classmethod
    def setUpClass(cls):
        from tests.test_history_regressions import previous_history_bundle
        previous = tempfile.TemporaryDirectory(prefix="finding-previous-")
        cls.addClassCleanup(previous.cleanup)
        cls.previous_bundle = previous_history_bundle(previous.name)
        case = cls.case = ProductionCase()
        case.setUp()
        cls.addClassCleanup(case.doCleanups)
        (case.project / "AGENTS.md").write_text(boundaries.AGENTS)
        boundaries.git(case.project, "add", "AGENTS.md")
        boundaries.git(case.project, "commit", "-qm", "required fixture audit")
        case.base = case.candidate = boundaries.git(case.project, "rev-parse", "HEAD")
        boundaries.git(case.project, "branch", "-f", "target", case.base)
        boundaries.git(case.project, "update-ref", "refs/remotes/origin/target", case.base)
        case.intake(boundaries.governance_policy(case, "A"))
        cls.path = objective_root(case.project, "objective") / "context.json"
        cls.snapshots = {}

        def begin(name):
            packet = case.packet(["A"], role="review")
            admission = start_before_reservations(case, name, packet)["admission"]
            case.settle(admission)
            return admission, packet

        def consume(admission, packet, findings, *, corrections=(), outcome="succeeded"):
            rows = case.stored()
            assurance = next(row for row in rows if row["id"] == "A")
            assurance.pop("executor")
            assurance.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
            case.report(admission, packet, map={"obligations": [*rows, *corrections]},
                        triage=findings, outcome=outcome)

        def snapshot(count):
            admission, packet = begin(f"pending-at-{count}")
            cls.snapshots[count] = {"files": cls.files(), "port": deepcopy(case.port.__dict__),
                                    "admission": admission, "packet": packet}
            return admission, packet

        # Three real reports build the expensive history once. Native ports are
        # injected; the admission, triage, chronology and persistence are real.
        admission, packet = begin("original-failed-review")
        case.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = "failed"
        consume(admission, packet, [cls.required("K0"), cls.required("KW"),
                                    *(cls.advisory(i) for i in range(30))],
                corrections=[cls.correction("K0"), cls.correction("KW")], outcome="failed")
        cls.failed_admission = admission["admission_id"]
        cls.failed_packet = packet
        rows = case.stored()
        next(row for row in rows if row["id"] == "K0").update(
            state="satisfied", evidence=[case.proof("K0")])
        next(row for row in rows if row["id"] == "KW").update(
            state="withdrawn", withdrawal={"by": "user_direct", "reason": "the user removed this requirement"})
        case.write(rows, revision_authority={"provenance": "user_direct",
                                            "instruction": "Withdraw KW; preserve the original review finding"})
        admission, packet = snapshot(32)
        consume(admission, packet, [cls.advisory(i) for i in range(30, 61)])
        admission, packet = snapshot(63)
        consume(admission, packet, [cls.advisory(61)])
        snapshot(64)

    @classmethod
    def files(cls):
        return {path.relative_to(cls.case.root / "state"): path.read_bytes()
                for path in (cls.case.root / "state").rglob("*") if path.is_file()}

    def restore(self, count):
        saved = self.snapshots[count]
        state = self.case.root / "state"
        shutil.rmtree(state)
        for relative, data in saved["files"].items():
            path = state / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.case.port.__dict__.clear()
        self.case.port.__dict__.update(deepcopy(saved["port"]))
        return saved["admission"], saved["packet"]

    def report_required(self, admission, packet, *, outcome="succeeded"):
        rows = self.case.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("executor")
        assurance.update(state="waiting", wait={"class": "dependency", "referent": "KNEW"})
        return self.case.report(admission, packet, map={"obligations": [*rows, self.correction("KNEW")]},
                                triage=[self.required("KNEW")], outcome=outcome)

    def assert_added(self, count):
        admission, packet = self.restore(count)
        before = _read(self.path)
        prior = next(row for row in before["checkpoint"]["obligations"] if row["id"] == "A")
        self.report_required(admission, packet)
        after = _read(self.path)
        rows = {row["id"]: row for row in after["checkpoint"]["obligations"]}
        self.assertEqual(rows["A"]["findings"][:-1], prior["findings"])
        self.assertEqual(len(rows["A"]["findings"]), count + 1)
        self.assertEqual(rows["A"]["receipts"][:-1], prior["receipts"])
        self.assertEqual(rows["KNEW"]["finding"]["attempt"], admission["admission_id"])
        self.assertEqual(rows["A"]["findings"][-1]["recorded_seq"], rows["KNEW"]["introduced_seq"])
        for key in ("K0", "KW"):
            self.assertEqual(rows[key], next(row for row in before["checkpoint"]["obligations"] if row["id"] == key))
        self.assertEqual(after["admissions"][self.failed_admission], before["admissions"][self.failed_admission])
        self.assertEqual(after["admissions"][self.failed_admission]["report"]["outcome"], "failed")
        from pod.obligations import label_qualification
        context = {"admissions": after["admissions"], "candidate": self.case.candidate,
                   "verification": self.case.core()["verification"],
                   "policy_revision": self.case.core()["policy_revision"], "source_current": lambda entry: True}
        self.assertEqual(label_qualification(after["checkpoint"], context, self.case.candidate)["label"], "WITHHELD")
        # Exact report replay adds neither a finding nor a logical/physical write.
        files = self.files()
        self.case.report(admission, packet)
        self.assertEqual(self.files(), files)

    def test_retained_32_plus_one_required_finding_preserves_every_prior_record(self):
        self.assert_added(32)

    def test_retained_63_plus_one_required_finding_preserves_every_prior_record(self):
        self.assert_added(63)

    def test_retained_64_plus_one_refuses_atomically_and_names_pending_consumption(self):
        admission, packet = self.restore(64)
        before = self.files()
        with self.assertRaises(PodError) as caught:
            self.report_required(admission, packet)
        self.assertEqual(caught.exception.code, "obligation_invalid")
        self.assertEqual(caught.exception.detail["detail"], "finding_limit")
        self.assertEqual(caught.exception.detail["referent"]["limit"], 64)
        self.assertEqual(caught.exception.detail["referent"]["admission"], admission["admission_id"])
        self.assertEqual(caught.exception.detail["next_action"],
                         f"consume the report for admission {admission['admission_id']} before retrying this write")
        self.assertEqual(self.files(), before)

    def test_new_admission_reserves_the_full_finding_budget(self):
        # Consume the preceding pending result, so refusal is about the new
        # attempt's 32-record budget, not an older pending result.
        admission, packet = self.restore(63)
        self.case.report(admission, packet, outcome="failed", map={"seq": _read(self.path)["checkpoint"]["seq"] + 1,
            "update": {"A": {"state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}}})
        packet = self.case.packet(["A"], role="review")
        before = self.files()
        workers = deepcopy(self.case.port.workers)
        with self.assertRaises(PodError) as caught:
            self.case.start("must-renew-first", packet)
        detail = caught.exception.detail
        self.assertEqual(detail["detail"], "finding_limit")
        self.assertEqual({key: detail["referent"][key] for key in ("retained", "proposed", "reserved", "limit")},
                         {"retained": 63, "proposed": 95, "reserved": 32, "limit": 64})
        action = detail["next_action"]
        self.assertEqual(action, {"seq": _read(self.path)["checkpoint"]["seq"] + 1,
            "renew": [{"id": "A", "as": "<fresh-id>", "state": "waiting",
                       "wait": {"class": "sequenced", "referent": "O1"}}]})
        self.assertEqual(self.files(), before)
        self.assertEqual(self.case.port.workers, workers)
        action["renew"][0]["as"] = "A2"
        self.case.write(**action)
        fresh = self.case.start("review-renewal", self.case.packet(["A2"], role="review"))
        self.assertEqual(fresh["admission"]["serves"], ["A2"])

    def test_report_33_refuses_even_with_32_history_slots_remaining(self):
        admission, packet = self.restore(32)
        before = self.files()
        with self.assertRaises(PodError) as caught:
            self.case.report(admission, packet, triage=[self.advisory(i) for i in range(100, 133)])
        self.assertEqual(caught.exception.code, "obligation_invalid")
        self.assertEqual(caught.exception.detail["detail"], "malformed")
        self.assertEqual(self.files(), before)
        self.assertEqual((MAX_FINDINGS, MAX_REPORT_FINDINGS), (64, 32))
        self.assertEqual(bound("obligation", "findings", "max_items"), 64)
        self.assertEqual(bound("report request", "triage", "max_items"), 32)

    def test_exact_replay_of_32_old_findings_spends_no_new_slots(self):
        self.restore(32)
        before = self.files()
        admission = _read(self.path)["admissions"][self.failed_admission]
        self.case.report(admission, self.failed_packet, outcome="failed", triage=[
            self.required("K0"), self.required("KW"), *(self.advisory(i) for i in range(30))])
        self.assertEqual(self.files(), before)

    def test_persisted_history_refuses_33_records_from_the_same_attempt(self):
        from pod.assurance import validate_findings
        self.restore(63)
        state = _read(self.path)["checkpoint"]
        rows = {row["id"]: row for row in state["obligations"]}
        assurance = deepcopy(rows["A"])
        assurance["findings"][32]["attempt"] = assurance["findings"][0]["attempt"]
        before = self.files()
        with self.assertRaises(PodError) as caught:
            validate_findings(assurance, rows, seq=state["seq"])
        self.assertEqual(caught.exception.code, "obligation_invalid")
        self.assertEqual(caught.exception.detail["detail"], "malformed")
        self.assertIn("per review attempt", str(caught.exception))
        self.assertEqual(self.files(), before)

    def test_interrupted_report_publication_retries_without_losing_or_duplicating_findings(self):
        admission, packet = self.restore(32)
        before = self.files()
        with patch("pod.history.atomic_json", side_effect=OSError("synthetic context publication failure")):
            with self.assertRaises(OSError):
                self.report_required(admission, packet)
        self.assertEqual(self.files(), before)
        self.report_required(admission, packet)
        after = self.files()
        self.case.report(admission, packet)
        self.assertEqual(self.files(), after)
        self.assertEqual(len(next(row for row in self.case.stored() if row["id"] == "A")["findings"]), 33)

    def previous_read(self):
        from tests.test_history_regressions import previous_history_call
        from pod.ledger import kernel_context
        state = _read(self.path)
        bound = state["checkpoint"]["governance"]
        observed = kernel_context(self.case.project, "objective", state, None)["bound_governance"](bound)
        context = {"admissions": state["admissions"], "criteria": ["PoD#1"],
                   "candidate": self.case.candidate, "verification": self.case.core()["verification"],
                   "policy_revision": self.case.core()["policy_revision"], "governance": observed}
        return previous_history_call(self.previous_bundle, state, context=context)

    def test_new_required_finding_still_blocks_the_governor_release_boundary(self):
        admission, packet = self.restore(32)
        self.report_required(admission, packet)
        binding = self.case.op("governor-prepare", unit="default", branch={
            "remote": "origin", "base": "target", "branch": "delivery"})["candidate"]
        decision = self.case.op("governor", action={"kind": "merge", "candidate": binding["id"],
            "unit": "default", "target": "origin/target", "reason": "deliver the fixture candidate",
            "effects": [], "authorization": boundaries.authorization(
                candidate=binding["commit"], tree=binding["tree"], scope=("merge",), target="origin/target")})
        self.assertEqual(decision["purpose"], "release")
        self.assertEqual(decision["decision"], "DEFER")
        self.assertIn("assurance_unbound", [reason["code"] for reason in decision["reasons"]])
        self.assertFalse(self.case.remote.calls)

    def test_33_record_history_keeps_admission_before_resolution_qualification_guard(self):
        admission, packet = self.restore(32)
        self.report_required(admission, packet)
        rows = self.case.stored()
        next(row for row in rows if row["id"] == "KNEW").update(
            state="satisfied", evidence=[self.case.proof("KNEW")])
        next(row for row in rows if row["id"] == "A")["wait"] = {"class": "sequenced", "referent": "O1"}
        self.case.write(rows)
        rows = self.case.stored()
        assurance = next(row for row in rows if row["id"] == "A")
        assurance.pop("wait")
        assurance.update(state="satisfied", evidence=[{"attempt": admission["admission_id"]}])
        with self.assertRaises(PodError) as caught:
            self.case.write(rows)
        self.assertEqual(caught.exception.detail["detail"], "evidence_invalidated")
        self.case.write(renew=[{"id": "A", "as": "A2"}])
        frozen = self.case.packet(["A2"], role="review")
        fresh = self.case.start("admitted-after-new-resolution", frozen)["admission"]
        self.case.settle(fresh)
        rows = self.case.stored()
        assurance = next(row for row in rows if row["id"] == "A2")
        assurance.pop("executor")
        assurance.update(state="satisfied", evidence=[{"attempt": fresh["admission_id"]}])
        self.case.report(fresh, frozen, map={"obligations": rows})
        from pod.ledger import kernel_view
        self.assertEqual(kernel_view(self.case.project, "objective")["label"]["label"], "QUALIFIED")
        self.assertEqual(len(next(row for row in self.case.stored() if row["id"] == "A")["findings"]), 33)

    def test_genuine_previous_reader_keeps_32_record_meaning_unchanged(self):
        self.restore(32)
        before = self.files()
        result = self.previous_read()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["state"], _read(self.path))
        self.assertTrue(result["unchanged"])
        self.assertEqual(self.files(), before)

    def test_genuine_previous_reader_refuses_33_records_without_rewriting(self):
        admission, packet = self.restore(32)
        self.report_required(admission, packet)
        before = self.files()
        result = self.previous_read()
        self.assertFalse(result["accepted"])
        self.assertEqual(result["code"], "obligation_invalid")
        self.assertEqual(result["detail"]["detail"], "malformed")
        self.assertIn("finding history is a bounded list", result["message"])
        self.assertTrue(result["unchanged"])
        self.assertEqual(self.files(), before)
