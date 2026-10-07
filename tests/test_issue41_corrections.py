"""Audit corrections through production entries; only native/provider ports are fake."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

from pod import internal
from pod.errors import PodError
from pod.ledger import objective_root
from tests.issue41_support import ProductionCase
from tests.kernel_support import git
from tests import test_issue41


@contextmanager
def production():
    case = ProductionCase()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def records(case):
    return {str(path): path.read_bytes() for path in
            objective_root(case.project, "objective").rglob("*.json")}


def entry(case, operation, request):
    source = case.root / "request.json"
    source.write_text(json.dumps(request))
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = internal.main([operation, "--input", str(source)])
    return code, json.loads(out.getvalue()), out.getvalue() + err.getvalue()


def assurance(case, **fields):
    return {"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
            "check": "review", "scope": {"paths": ["src"]}, "question": "correct?",
            "candidate": case.base, "existing_evidence": "tests", "insufficiency": "no review",
            "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}, **fields}


class IntakeCorrectionTests(ProductionCase):
    def test_malformed_brief_inputs_keep_kernel_identity_before_candidate_scan(self):
        # Predicate: caller-shaped containers/rows always produce a kernel refusal.
        missing = self.criterion(state="satisfied")
        missing.pop("id")
        cases = [(None, "obligation_unaccounted", "map_missing"),
                 (17, "obligation_unaccounted", "map_missing"),
                 ([17], "obligation_unaccounted", "map_missing")]
        cases += [({"obligations": rows, "governance": {"base_ref": None}}, code, detail)
                  for rows, code, detail in [
                      (None, "obligation_unaccounted", "map_missing"),
                      (17, "obligation_unaccounted", "map_missing"),
                      ({"id": "O1"}, "obligation_unaccounted", "map_missing"),
                      ("rows", "obligation_unaccounted", "map_missing"),
                      ([17], "obligation_invalid", "malformed"),
                      ([None], "obligation_invalid", "malformed"),
                      (["PRIVATE-SENTINEL"], "obligation_invalid", "malformed"),
                      ([missing], "obligation_invalid", "malformed"),
                      ([self.criterion(state="satisfied"), 17], "obligation_invalid", "malformed")]]
        cases.append(({"obligations": [self.criterion()] * 97, "governance": {"base_ref": None}},
                      "obligation_invalid", "malformed"))
        request = {"criteria": ["PoD#1"], "coverage": [{"criterion": "PoD#1", "check": "check"}]}
        for value, expected_code, detail in cases:
            with self.subTest(value=value):
                before = records(self)
                code, result, output = entry(self, "brief", {**request, "map": value})
                error = result["error"]
                self.assertEqual((code, error["code"], error["detail"]["detail"]),
                                 (1, expected_code, detail))
                self.assertEqual(error["detail"]["operation"], "brief")
                self.assertNotIn("Traceback", output)
                self.assertNotIn("PRIVATE-SENTINEL", output)
                self.assertEqual(records(self), before)
                if value and isinstance(value, dict) and value.get("obligations") == [missing]:
                    self.assertEqual(error["detail"]["referent"]["missing"], ["id"])
        rows = [self.criterion(state="satisfied")]
        _, result, _ = entry(self, "brief", {**request, "map": {"obligations": rows, "governance": {"base_ref": None}}})
        self.assertEqual(result["error"]["detail"]["referent"]["field"], "candidate")
        self.assertEqual(result["error"]["detail"]["referent"]["obligations"], "O1")


class InvalidationCorrectionTests(ProductionCase):
    def test_satisfied_invalidation_table_accounts_for_every_proof_state(self):
        # Predicate: once evidence_invalidated is selected, each failing satisfied
        # row occurs exactly once in invalidations[:8] or the remaining count.
        cases = [("ordinary", state) for state in
                 ("missing", "empty", "FAILED", "NOT_RUN", "UNAVAILABLE", "stale", "touched", "PASS")]
        cases += [("assurance", state) for state in
                  ("missing", "empty", "failed", "stale", "touched", "PASS")]
        for kind, proof_state in cases:
            with self.subTest(kind=kind, proof_state=proof_state), production() as case:
                external = {"party": "user", "need": "fix", "unblocks_when": "fixed"}
                extras = [f"E{i}" for i in range(9)]
                selected = "S" if kind == "ordinary" else "B"
                case.intake(assurance(case), assurance(case, id="B", scope={"paths": ["README.md"]}),
                            case.sub("S", proof_scope=["src"]), case.sub("R"),
                            *[case.sub(key) for key in extras])
                attempts = {}
                for key in ("A", "B"):
                    frozen = case.packet([key], role="review")
                    attempt = case.start("review-" + key, frozen)["admission"]
                    case.settle(attempt)
                    attempts[key] = attempt
                    rows = case.stored()
                    row = next(row for row in rows if row["id"] == key)
                    row.pop("executor")
                    failed = key == "B" and proof_state == "failed"
                    if key == "A" or failed:
                        row.update(state="blocked_external", external=external)
                    else:
                        row.update(state="satisfied", evidence=[{"attempt": attempt["admission_id"]}])
                    triage = []
                    if key == "A":
                        rows.append({"id": "CA", "kind": "correction", "provenance": "coordinator",
                                     "parent": "A", "check": "fix", "state": "blocked_external",
                                     "external": external})
                        triage = [{"finding": "F1", "severity": "major", "triage": "required_correction",
                                   "summary": "violated invariant", "correction": "CA"}]
                    case.report(attempt, frozen, outcome="failed" if failed else "succeeded",
                                map={"obligations": rows}, triage=triage)
                rows = case.stored()
                for row in rows:
                    if row["id"] in ("O1", "S"):
                        row.pop("executor", None)
                        row.pop("wait", None)
                        row.update(state="satisfied", evidence=[case.proof(row["id"])])
                    elif row["id"] == "R":
                        row.pop("wait")
                        row.update(state="active", executor="coordinator")
                    elif row["id"] in extras:
                        row.pop("wait")
                        row.update(state="blocked_external", external=external)
                case.write(rows)  # Valid ordinary proof and completed review are accepted.
                original = deepcopy(next(row for row in case.stored() if row["id"] == selected).get("evidence"))
                if proof_state in ("stale", "touched"):
                    path = "README.md" if (kind == "ordinary") == (proof_state == "stale") else "src/old.py"
                    case.move(path)
                    rows = case.stored()
                    for row in rows:
                        if row["state"] == "satisfied":
                            row.pop("evidence")
                            row.update(state="blocked_external", external=external)
                    case.write(rows)
                frozen = case.packet(["R"], role="investigate", resolves="which proof failed?",
                                     stop_condition="answer")
                reporting = case.start("investigate", frozen)["admission"]
                case.settle(reporting)
                rows = case.stored()
                rows_by_id = {row["id"]: row for row in rows}
                rows_by_id["R"].pop("executor")
                rows_by_id["R"].update(state="blocked_external", external=external)
                case.write(rows)
                base = {row["id"]: row for row in case.stored()}
                for placement in ("before", "after"):
                    for extra in (0, 9):
                        for operation in ("checkpoint", "report"):
                            with self.subTest(placement=placement, extra=extra, operation=operation):
                                by_id = deepcopy(base)
                                row = by_id[selected]
                                row.pop("external", None)
                                row["state"] = "satisfied"
                                if proof_state == "missing":
                                    row.pop("evidence", None)
                                elif proof_state == "empty":
                                    row["evidence"] = []
                                elif kind == "ordinary" and proof_state in ("FAILED", "NOT_RUN", "UNAVAILABLE"):
                                    row["evidence"] = [case.proof(selected, status=proof_state, reference="nonpassing")]
                                else:
                                    row["evidence"] = original or [{"attempt": attempts[selected]["admission_id"]}]
                                by_id["A"].pop("external")
                                by_id["A"].update(state="satisfied", evidence=[{"attempt": attempts["A"]["admission_id"]}])
                                for index, key in enumerate(extras[:extra]):
                                    row = by_id[key]
                                    row.pop("external")
                                    row["state"] = "satisfied"
                                    sibling = ("missing", "empty", "FAILED", "NOT_RUN", "UNAVAILABLE")[index % 5]
                                    if sibling == "empty":
                                        row["evidence"] = []
                                    elif sibling != "missing":
                                        row["evidence"] = [case.proof(key, status=sibling)]
                                order = ([selected, "A"] if placement == "before" else ["A", selected])
                                order += [key for key in by_id if key not in order]
                                proposed = [by_id[key] for key in order]
                                before = records(case)
                                with self.assertRaises(PodError) as caught:
                                    if operation == "checkpoint":
                                        case.write(proposed)
                                    else:
                                        case.report(reporting, frozen, map={"obligations": proposed})
                                error = caught.exception
                                self.assertEqual(error.code, "obligation_unaccounted")
                                self.assertEqual(error.detail["operation"], operation)
                                referent = error.detail["referent"]
                                if placement == "before" and proof_state in ("missing", "empty"):
                                    self.assertEqual(error.detail["detail"], "satisfied_without_evidence")
                                    self.assertEqual(referent, {"obligation": selected,
                                        "settled_attempts": "" if kind == "ordinary" else attempts["B"]["admission_id"]})
                                else:
                                    self.assertEqual(error.detail["detail"], "evidence_invalidated")
                                    self.assertEqual((referent["obligation"], referent["correction"]), ("A", "CA"))
                                    failing = {"A", *extras[:extra]}
                                    if proof_state != "PASS":
                                        failing.add(selected)
                                    expected = [key for key in order if key in failing]
                                    invalid = referent["invalidations"]
                                    self.assertEqual([item["obligation"] for item in invalid], expected[:8])
                                    self.assertEqual(referent["remaining"], max(0, len(expected) - 8))
                                    self.assertEqual(len({item["obligation"] for item in invalid}), len(invalid))
                                    self.assertTrue(all(item["classification"] and item["correction"] for item in invalid))
                                    a = next(item for item in invalid if item["obligation"] == "A")
                                    self.assertEqual((a["corrections"], a["eligibility"]), (["CA"], "ineligible"))
                                    if proof_state in ("stale", "touched"):
                                        sibling = next(item for item in invalid if item["obligation"] == selected)
                                        self.assertEqual((sibling["classification"], sibling["eligibility"]),
                                                         ("candidate-only", "eligible" if proof_state == "stale" else "touched"))
                                self.assertEqual(records(case), before)

    def test_all_invalid_satisfied_rows_include_correction_siblings_and_overflow(self):
        # Predicate: every satisfied row with invalid proof or an open correction is listed.
        for correction_state in ("blocked_external", "satisfied", "withdrawn"):
            for extra in (0, 8):
                with self.subTest(correction_state=correction_state, extra=extra), production() as case:
                    external = {"party": "user", "need": "fix", "unblocks_when": "fixed"}
                    case.intake(assurance(case), *[case.sub(f"S{i}", proof_scope=["src"]) for i in range(extra)])
                    frozen = case.packet(["A"], role="review")
                    first = case.start("review", frozen)["admission"]
                    case.settle(first)
                    rows = case.stored()
                    rows[1].pop("executor")
                    rows[1].update(state="blocked_external", external=external)
                    rows.append({"id": "C", "kind": "correction", "provenance": "coordinator", "parent": "A",
                                 "check": "fix", "state": "blocked_external", "external": external})
                    case.report(first, frozen, map={"obligations": rows}, triage=[{
                        "finding": "F1", "severity": "major", "triage": "required_correction",
                        "summary": "violated invariant", "correction": "C"}])
                    rows = case.stored()
                    for row in rows:
                        if row["id"] not in ("A", "C"):
                            row.pop("executor", None)
                            row.pop("wait", None)
                            row.update(state="satisfied", evidence=[case.proof(row["id"])])
                    correction = rows[-1]
                    authority = {}
                    if correction_state != "blocked_external":
                        correction.pop("external")
                        correction["state"] = correction_state
                        if correction_state == "satisfied":
                            correction["evidence"] = [case.proof("C")]
                        else:
                            correction["withdrawal"] = {"by": "user_direct", "reason": "owner waiver"}
                            authority = {"revision_authority": {"provenance": "user_direct", "instruction": "waive C"}}
                    case.write(rows, **authority)
                    # The original attempt cannot cover even a resolved correction.
                    stale = case.stored()
                    stale[1].pop("external")
                    stale[1].update(state="satisfied", evidence=[{"attempt": first["admission_id"]}])
                    before = records(case)
                    with self.assertRaises(PodError) as caught:
                        case.write(stale)
                    self.assertEqual(caught.exception.detail["detail"], "evidence_invalidated")
                    self.assertEqual([row["obligation"] for row in caught.exception.detail["referent"]["invalidations"]], ["A"])
                    self.assertEqual(records(case), before)
                    if correction_state != "blocked_external":
                        frozen = case.packet(["A"], role="review")
                        fresh = case.start("delta-review", frozen)["admission"]
                        case.settle(fresh)
                        rows = case.stored()
                        rows[1].pop("executor")
                        rows[1].update(state="satisfied", evidence=[{"attempt": fresh["admission_id"]}])
                        case.report(fresh, frozen, map={"obligations": rows})
                    case.move()
                    rows = case.stored()
                    if correction_state == "blocked_external":
                        rows[1].pop("external")
                        rows[1].update(state="satisfied", evidence=[{"attempt": first["admission_id"]}])
                    before = records(case)
                    with self.assertRaises(PodError) as caught:
                        case.write(rows)
                    error = caught.exception
                    self.assertEqual((error.code, error.detail["detail"]), ("obligation_unaccounted", "evidence_invalidated"))
                    referent = error.detail["referent"]
                    expected = [row["id"] for row in rows if row["state"] == "satisfied"]
                    self.assertEqual([row["obligation"] for row in referent["invalidations"]], expected[:8])
                    self.assertEqual(referent["remaining"], max(0, len(expected) - 8))
                    self.assertTrue(all(row["classification"] and row["eligibility"] and row["correction"]
                                        for row in referent["invalidations"]))
                    a = referent["invalidations"][1]
                    if correction_state == "blocked_external":
                        self.assertEqual((referent["obligation"], referent["correction"]), ("A", "C"))
                        self.assertEqual(a["corrections"], ["C"])
                        self.assertEqual(a["eligibility"], "ineligible")
                        self.assertIn("resolve C", a["correction"])
                    else:
                        self.assertEqual(referent["obligation"], "O1")
                        self.assertEqual((a["classification"], a["eligibility"]), ("candidate-only", "eligible"))
                    self.assertEqual(records(case), before)

    def test_report_collects_open_corrections_and_other_invalid_proof_atomically(self):
        self.intake(assurance(self), self.sub("R"))
        frozen = self.packet(["A"], role="review")
        review = self.start("review", frozen)["admission"]
        self.settle(review)
        external = {"party": "user", "need": "fix", "unblocks_when": "fixed"}
        rows = self.stored()
        rows[1].pop("executor")
        rows[1].update(state="blocked_external", external=external)
        corrections = [f"C{i}" for i in range(10)]
        rows += [{"id": key, "kind": "correction", "provenance": "coordinator", "parent": "A", "check": "fix",
                  "state": "blocked_external", "external": external} for key in corrections]
        self.report(review, frozen, map={"obligations": rows}, triage=[{
            "finding": f"F{i}", "severity": "major", "triage": "required_correction",
            "summary": "violated invariant", "correction": key} for i, key in enumerate(corrections)])
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        rows[2].pop("wait")
        rows[2].update(state="active", executor="coordinator")
        self.write(rows)
        old_proof = rows[0]["evidence"]
        frozen = self.packet(["R"], role="investigate", resolves="what proof changed?", stop_condition="answer")
        reporting = self.start("investigate", frozen)["admission"]
        self.move()
        rows = self.stored()
        rows[0].update(state="blocked_external", external=external)
        self.write(rows)
        self.settle(reporting)
        rows = self.stored()
        rows[0].pop("external")
        rows[0].update(state="satisfied", evidence=old_proof)
        rows[1].pop("external")
        rows[1].update(state="satisfied", evidence=[{"attempt": review["admission_id"]}])
        rows[2].pop("executor")
        rows[2].update(state="blocked_external", external=external)
        before = records(self)
        with self.assertRaises(PodError) as caught:
            self.report(reporting, frozen, map={"obligations": rows})
        error = caught.exception
        self.assertEqual((error.code, error.detail["detail"], error.detail["operation"]),
                         ("obligation_unaccounted", "evidence_invalidated", "report"))
        referent = error.detail["referent"]
        self.assertEqual((referent["obligation"], referent["correction"]), ("A", "C0"))
        self.assertEqual([row["obligation"] for row in referent["invalidations"]], ["O1", "A"])
        self.assertEqual(referent["invalidations"][1]["corrections"], corrections[:8])
        self.assertEqual(referent["invalidations"][1]["remaining_corrections"], 2)
        self.assertEqual(records(self), before)


class RefusalCorrectionTests(ProductionCase):
    def test_nested_refusals_keep_known_obligation_and_hide_refused_values(self):
        # Predicate: nested failure => known obligation is retained, refused value is absent.
        sentinel = "PRIVATE-SENTINEL"
        (self.project / "AGENTS.md").write_text("review required\n\n")
        git(self.project, "add", "AGENTS.md")
        git(self.project, "commit", "-qm", "policy")
        self.base = self.candidate = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", self.base)
        short = {"check": "check", "command": "check", "result": "pass", "reference": "initial"}
        cases = []
        def add(row, code="obligation_invalid", detail="malformed"):
            cases.append((row, code, detail))
        for boundary in ({"unexpected": sentinel}, sentinel, {"paths": sentinel}, {"paths": ["src"] * 65},
                         {"surfaces": sentinel}, {"surfaces": ["valid"] * 33}, {"surfaces": [sentinel + "!"]},
                         {"paths": ["../" + sentinel]}):
            add(self.criterion(boundary=boundary))
            add(assurance(self, scope=boundary))
        add(self.criterion(proof_scope=["../" + sentinel]))
        for source in ({"unexpected": sentinel}, sentinel, {"ref": sentinel * 30}):
            add(self.criterion(source=source))
        for source in ({"unexpected": sentinel}, sentinel, {"instruction": sentinel * 100}):
            add(self.sub("U", provenance="user_direct", source=source))
        for source, detail in [({"unexpected": sentinel}, "malformed"),
                               ({"path": sentinel, "lines": "1"}, "governance_source_unrecognized"),
                               ({"path": "AGENTS.md", "lines": sentinel}, "cite_not_found"),
                               ({"path": "AGENTS.md", "lines": "4"}, "cite_not_found"),
                               ({"path": "AGENTS.md", "lines": "2"}, "cite_not_found"),
                               ({"path": "AGENTS.md", "lines": "1", "revision": sentinel}, "cite_not_base"),
                               ({"path": "AGENTS.md", "lines": "1", "quote_sha256": sentinel}, "cite_not_base")]:
            add(self.sub("P", provenance="project_policy", source=source), detail=detail)
        for external, detail in [({"unexpected": sentinel}, "malformed"), (sentinel, "malformed"),
                                 ({"party": "user", "need": sentinel * 100, "unblocks_when": "fixed"}, "external_invalid"),
                                 ({"party": "user", "need": "input", "unblocks_when": sentinel * 100}, "external_invalid")]:
            add(self.criterion(state="blocked_external", external=external), "obligation_unaccounted", detail)
        add(self.criterion(check=sentinel * 100))
        add(self.sub("S", state="withdrawn", withdrawal={"unexpected": sentinel}))
        add(self.sub("S", state="withdrawn", withdrawal={"by": "coordinator", "reason": sentinel * 100}))
        for wait in ({"class": "dependency", "unexpected": sentinel},
                     {"class": "sequenced", "referent": "O1", "rationale": sentinel * 100},
                     {"class": "sequenced", "referent": "O1", "revisit_when": sentinel * 100},
                     {"class": "authority", "referent": {"unexpected": sentinel}}):
            add(self.sub("S", wait=wait), "wait_invalid")
        add(self.sub("S", wait={"class": "authority", "referent": {"kind": "permission", "need": sentinel * 100}}),
            "wait_invalid", "unknown_referent")
        for sources in (sentinel, [sentinel] * 65, [sentinel], [{"unexpected": sentinel}],
                        [{"path": "../" + sentinel, "state": "absent"}], [{"path": "src", "state": sentinel}]):
            add(self.criterion(state="satisfied", evidence=[{**short, "sources": sources}]))
        for dependencies in (sentinel, [sentinel] * 65, [sentinel * 100]):
            add(self.criterion(state="satisfied", evidence=[{**short, "dependencies": dependencies}]))
        add(self.criterion(state="satisfied", evidence=[{"unexpected": sentinel}]))
        detailed = self.proof("O1")
        detailed.pop("check")
        detailed["unexpected"] = sentinel
        add(self.criterion(state="satisfied", evidence=[detailed]))
        add(self.criterion(state="satisfied", evidence=[{**self.proof("O1"), "criterion": sentinel}]),
            detail="evidence_unbound")
        for reuse in ({"unexpected": sentinel}, sentinel, {"from": sentinel * 100}):
            add(self.criterion(state="satisfied", evidence=[short], reuse=reuse))
        for evidence in ({"unexpected": sentinel}, sentinel, {"attempt": sentinel * 100}):
            row = assurance(self, state="satisfied", evidence=[evidence])
            row.pop("wait")
            add(row)
        for index, (row, expected_code, detail) in enumerate(cases):
            with self.subTest(case=index, obligation=row["id"]):
                rows = [row] if row["id"] == "O1" else [self.criterion(), row]
                before = records(self)
                request = {"project": str(self.project), "objective": "objective", "owner": "owner",
                           "value": self.core(obligations=rows, governance={"base_ref": "refs/remotes/origin/target"})}
                code, result, output = entry(self, "checkpoint", request)
                error = result["error"]
                self.assertEqual((code, error["code"], error["detail"]["detail"]), (1, expected_code, detail))
                self.assertEqual(error["detail"]["referent"]["obligation"], row["id"])
                self.assertNotIn(sentinel, output)
                self.assertNotIn("Traceback", output)
                self.assertEqual(records(self), before)

    def test_review_record_siblings_keep_known_assurance_and_hide_refused_values(self):
        self.intake(assurance(self))
        frozen = self.packet(["A"], role="review")
        admission = self.start("review", frozen)["admission"]
        self.settle(admission)
        rows = self.stored()
        rows[1].pop("executor")
        rows[1].update(state="blocked_external", external={"party": "user", "need": "input", "unblocks_when": "provided"})
        sentinel = "PRIVATE-SENTINEL"
        finding = {"finding": "F1", "severity": "minor", "triage": "advisory", "summary": "finding"}
        group = {"findings": [{"finding": "F1", "severity": "minor"}], "triage": "advisory", "summary": "group", "root_cause": "cause"}
        cases = [({"triage": sentinel}, "malformed"),
                 ({"triage": [finding] * 33}, "malformed"),
                 ({"triage": [{"unexpected": sentinel}]}, "malformed"),
                 ({"triage": [sentinel]}, "malformed"),
                 ({"triage": [{**finding, "finding": sentinel * 100}]}, "malformed"),
                 ({"triage": [{**finding, "summary": sentinel * 100}]}, "malformed"),
                 ({"triage": [{**finding, "severity": sentinel}]}, "malformed"),
                 ({"triage": [{**finding, "severity": "major"}]}, "downgrade_unreasoned"),
                 ({"triage": [{**finding, "severity": "major", "reason": sentinel * 100}]}, "malformed"),
                 ({"triage": [{**group, "findings": [{"unexpected": sentinel}]}]}, "malformed"),
                 ({"triage": [{**group, "findings": [sentinel]}]}, "malformed"),
                 ({"triage": [{**group, "findings": group["findings"] * 9}]}, "malformed"),
                 ({"triage": [{**group, "root_cause": sentinel * 100}]}, "malformed"),
                 ({"proposals": sentinel}, "malformed"),
                 ({"proposals": [sentinel] * 17}, "malformed"),
                 ({"proposals": [{"unexpected": sentinel}]}, "malformed"),
                 ({"proposals": [{"summary": sentinel * 100}]}, "malformed"),
                 ({"proposals": [{"summary": "proposal", "source": sentinel}]}, "malformed")]
        for index, (fields, detail) in enumerate(cases):
            with self.subTest(case=index):
                before = records(self)
                with self.assertRaises(PodError) as caught:
                    self.report(admission, frozen, map={"obligations": rows}, **fields)
                error = caught.exception
                self.assertEqual((error.code, error.detail["detail"], error.detail["operation"]),
                                 ("obligation_invalid", detail, "report"))
                self.assertEqual(error.detail["referent"]["obligation"], "A")
                self.assertNotIn(sentinel, str(error))
                self.assertEqual(records(self), before)

    def test_kernel_operation_tags_preserve_codes_details_and_first_referents(self):
        # Predicate: every kernel helper refusal adds only the entry operation context.
        for operation in ("checkpoint", "brief", "admission", "report"):
            with self.subTest(operation=operation), production() as case:
                bad = case.criterion(kind="PRIVATE-SENTINEL")
                owned = {"project": str(case.project), "objective": "objective", "owner": "owner"}
                if operation == "checkpoint":
                    request = {**owned, "value": case.core(obligations=[bad], governance={"base_ref": "target"})}
                elif operation == "brief":
                    request = {"criteria": ["PoD#1"], "coverage": [{"criterion": "PoD#1", "check": "check"}],
                               "map": {"obligations": [bad], "governance": {"base_ref": None}}}
                else:
                    case.intake(case.sub("S"))
                    frozen = case.packet(["S"], role="investigate", resolves="which branch?", stop_condition="answer")
                    if operation == "admission":
                        request = {**owned, "run": "run", "task": "work", "plan_revision": "plan", "packet": frozen,
                                   "map": {"obligations": [bad, case.sub("S")]}}
                    else:
                        admission = case.start("work", frozen)["admission"]
                        case.settle(admission)
                        request = {"project": str(case.project), "objective": "objective", "packet": frozen,
                                   "admission_id": admission["admission_id"], "map": {"obligations": [bad, case.sub("S")]},
                                   "report": {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                                              "attempt": admission["native_binding"]["dispatchId"], "candidate": case.base,
                                              "outcome": "succeeded", "scope": frozen["body"]["scope"], "files": [],
                                              "checks": [], "failures": [], "evidence": [], "uncertainty": [], "questions": []}}
                before = records(case)
                with self.assertRaises(PodError) as baseline:
                    internal._OPERATIONS[operation](deepcopy(request))
                code, result, output = entry(case, operation, request)
                error = result["error"]
                self.assertEqual((code, error["code"]), (1, baseline.exception.code))
                self.assertEqual({key: value for key, value in error["detail"].items() if key != "operation"}, baseline.exception.detail)
                self.assertEqual(error["detail"]["operation"], operation)
                self.assertIn(f"internal {operation}:", error["message"])
                self.assertNotIn("PRIVATE-SENTINEL", output)
                self.assertNotIn("Traceback", output)
                self.assertEqual(records(case), before)


class OfflineOmissionTests(ProductionCase):
    cli = test_issue41.DiagnosticsBoundaryTests.cli

    def test_every_executable_reservation_cancel_then_executes_exactly_once(self):
        # Predicate: a canceled decision-only reservation permits exactly one managed effect.
        from pod.governor import EXECUTABLE_KINDS
        kinds = ("push", "pr_update", "workflow_dispatch", "validation_rerun", "remote_diagnostic", "cancel_validation")
        self.assertEqual(set(kinds), set(EXECUTABLE_KINDS))
        for kind in kinds:
            with self.subTest(kind=kind), production() as case:
                case.prepare()
                original = case.remote._start
                def start(*args, **kwargs):
                    run = original(*args, **kwargs)
                    run["created_at"] = datetime.now(timezone.utc).isoformat()
                    return run
                case.remote._start = start
                if kind not in ("push", "pr_update"):
                    case.op("governor-execute", action=case.action())
                target = {}
                if kind in ("validation_rerun", "cancel_validation"):
                    dispatch = case.op("governor-execute", action=case.action("workflow_dispatch"))
                    if kind == "validation_rerun":
                        case.remote.complete(dispatch["receipt"]["provider"]["run_id"], "failure")
                        case.op("governor-reconcile", record_id=dispatch["record_id"])
                        case.op("governor-classify", record_id=dispatch["record_id"], classification={
                            "class": "transient", "reason": "fixture interruption"})
                    else:
                        case.move()
                        case.write()
                        candidate = case.op("governor-prepare", unit="default")["candidate"]
                        case.authorization = {**case.authorization, "candidate": candidate["commit"], "tree": candidate["tree"]}
                        target = {"target": dispatch["record_id"]}
                action = case.action(kind, **target)
                decided = case.op("governor", action=action)
                self.assertEqual(decided["decision"], "ALLOW", decided)
                calls = len(case.remote.calls)
                attached = case.op("governor-execute", action=action)
                self.assertEqual(attached["reuse"]["kind"], "attach")
                self.assertEqual(len(case.remote.calls), calls)
                case.op("governor-outcome", record_id=decided["record_id"], outcome="CANCELED")
                executed = case.op("governor-execute", action=action)
                effects = [call[0] for call in case.remote.calls[calls:]]
                expected = {"push": ["push"], "pr_update": ["push", "pr_create"],
                            "workflow_dispatch": ["dispatch"], "validation_rerun": ["rerun"],
                            "remote_diagnostic": ["dispatch"], "cancel_validation": ["cancel"]}[kind]
                for effect in expected:
                    self.assertEqual(effects.count(effect), 1, effects)
                calls = len(case.remote.calls)
                case.op("governor-execute", action=action)
                self.assertEqual(len(case.remote.calls), calls)
                journal = json.loads((objective_root(case.project, "objective") / "governor.json").read_text())
                reserved = next(row for row in journal["actions"] if row["record_id"] == decided["record_id"])
                managed = next(row for row in journal["actions"] if row["record_id"] == executed["record_id"])
                self.assertTrue(reserved["decision_reserved"])
                self.assertNotIn("decision_reserved", managed)

    def test_status_state_read_error_and_released_superseded_entries_are_read_only(self):
        # Predicate: every blocked status entry renders exit 1 + blocker/action without writes.
        self.intake()
        root = objective_root(self.project, "objective")
        record = root / "context.json"
        genuine = root / "genuine.json"
        # The superseded record is emitted by the genuine 0.5.0 production helper.
        released = self.root / "released-050"
        released.mkdir()
        archive = self.root / "released-050.tar"
        repository = Path(__file__).resolve().parents[1]
        commit = "9c8639a5d7406b708b67b96a5ab26677efae1581"
        subprocess.run(["git", "-C", str(repository), "archive", "-o", str(archive), commit, "skills", "VERSION"], check=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(released, filter="data")
        request = self.root / "released-request.json"
        request.write_text(json.dumps({"project": str(self.project), "objective": "earlier", "owner": "owner",
            "value": {"schema": "pod-checkpoint/v1", "criteria": ["PoD#1"], "plan_revision": "plan", "candidate": self.base,
                      "policy_revision": "fixture", "native_refs": [{"runId": "run05", "runtime": "runtime"}],
                      "assignments": [], "questions": [], "verification_gaps": [], "next_safe_action": "inspect"}}))
        program = ("from unittest.mock import patch\nfrom pod import __version__, internal\nimport sys\n"
                   "assert __version__ == '0.5.0'\n"
                   "with patch('pod.orca.contract', return_value={'status':'observed','runtime':'runtime'}):\n"
                   " sys.exit(internal.main(['checkpoint','--input',sys.argv[1]]))\n")
        emitted = subprocess.run([sys.executable, "-c", program, str(request)], cwd=released,
                                 env={**os.environ, "PYTHONPATH": str(released / "skills")}, capture_output=True, text=True)
        self.assertEqual(emitted.returncode, 0, emitted.stderr + emitted.stdout)
        cases = [( ("--objective", "absent"), "objective_unknown", "name a recorded objective", False),
                 (("--objective", "objective", "--run", "foreign"), "objective_run_mismatch", "select a Run recorded", False),
                 (("--objective", "objective"), "unsafe_record", "inspect the objective state", True),
                 (("--run", "run05"), "objective_superseded", "start a new objective", False)]
        for args, blocker, action, fault in cases:
            with self.subTest(blocker=blocker):
                if fault:
                    record.rename(genuine)
                    record.symlink_to(genuine.name)  # Real writer bytes + an actual read-boundary fault.
                before = {str(path): path.read_bytes() for path in Path(os.environ["XDG_STATE_HOME"]).rglob("*.json")}
                code, out, err = self.cli("status", *args)
                self.assertEqual(code, 1)
                self.assertIn(blocker, out)
                self.assertIn(action, out)
                self.assertFalse(err)
                code, out, err = self.cli("status", *args, "--json")
                result = json.loads(out)
                self.assertEqual((code, result["status"], result["blocker"]), (1, "blocked", blocker))
                self.assertIn(action, result["next_safe_action"])
                self.assertTrue({"schema", "preferences", "routes", "constraints", "bundle_identity", "next_safe_action"} <= set(result))
                if blocker == "objective_superseded":
                    self.assertEqual(result["superseded"][0]["objective"], "earlier")
                    self.assertFalse(result["superseded"][0]["converted"])
                self.assertFalse(err)
                self.assertEqual({str(path): path.read_bytes() for path in Path(os.environ["XDG_STATE_HOME"]).rglob("*.json")}, before)
                if fault:
                    self.assertTrue(record.is_symlink())
                    record.unlink()
                    genuine.rename(record)
