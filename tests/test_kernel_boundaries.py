"""The obligation kernel at Pod's real boundaries: ledger checkpoint, admission, report,
acceptance, Governor, status/doctor and cutover, against disposable Git repositories."""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pod.config import effective, write_defaults
from pod.errors import PodError
from pod.github import repository_context
from pod.internal import run as internal_run
from pod.ledger import (checkpoint, constraints_update, objective_root, read, route_failure, state_inventory,
                        state_root)
from pod.operations import OrcaPort, guarded_start
from pod.records import packet
from tests.common import VERIFICATION, fake_authority, fixture, proof
from tests.test_operations import FakePort, ROUTE

AGENTS = ("# Project rules\n\nEvery change to src/ receives a fresh candidate-bound review.\n"
          "Reviews bind to a frozen candidate.\nDocs are optional.\n")


def git(project: Path, *argv: str) -> str:
    return subprocess.run(["git", "-C", str(project), *argv], capture_output=True, text=True,
                          check=True).stdout.strip()


class KernelCase(unittest.TestCase):
    def setUp(self):
        self.temp = fixture(); self.root = self.temp.__enter__()
        self.addCleanup(self.temp.__exit__, None, None, None)
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
                                           "XDG_CONFIG_HOME": str(self.root / "config")})
        self.env.__enter__(); self.addCleanup(self.env.__exit__, None, None, None)
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.project)], check=True)
        git(self.project, "config", "user.name", "Fixture")
        git(self.project, "config", "user.email", "fixture@example.invalid")
        (self.project / "AGENTS.md").write_text(AGENTS)
        (self.project / "src").mkdir()
        (self.project / "src" / "old.py").write_text("old\n")
        (self.project / "README.md").write_text("readme\n")
        git(self.project, "add", ".")
        git(self.project, "commit", "-q", "-m", "base")
        git(self.project, "branch", "target")
        self.base = git(self.project, "rev-parse", "HEAD")
        write_defaults(self.root / "config" / "pod" / "config.yaml")
        self.port = FakePort()
        context = repository_context(self.project)
        self.port.placement = {"repository": None, "repo_key": context["repo_key"],
                               "path": context["worktree"], "branch": "main", "runtime": "runtime"}
        authority = patch("pod.ledger.require_authority",
                          side_effect=lambda *args, **kwargs: fake_authority(self.port)(*args, **kwargs))
        authority.start(); self.addCleanup(authority.stop)
        self.candidate = self.base

    def core(self, **extra) -> dict:
        return {"schema": "pod-checkpoint/v3", "criteria": ["PoD#1"], "plan_revision": "plan",
                "candidate": self.candidate, "policy_revision": effective(self.project)["revision"],
                "native_refs": [], "assignments": [], "questions": [], "verification_gaps": [],
                "next_safe_action": "continue", "verification": dict(VERIFICATION), **extra}

    def proof(self, obligation, **extra):
        return proof(obligation, self.candidate, policy_revision=effective(self.project)["revision"], **extra)

    def write(self, obligations, objective="objective", **fields) -> dict:
        return checkpoint(self.project, objective, owner="owner",
                          value=self.core(obligations=obligations, **fields),
                          native={"runtime": "runtime", "delegation": "available"})

    def intake(self, *rows, **fields) -> dict:
        return self.write([self.criterion(), *rows], governance={"base_ref": "target"}, **fields)

    @staticmethod
    def criterion(state="active", **fields):
        row = {"id": "O1", "kind": "criterion", "provenance": "objective", "source": {"ref": "PoD#1"},
               "check": "PoD#1 passes", "state": state}
        if state == "active":
            row["executor"] = "coordinator"
        row.update(fields)
        return row

    @staticmethod
    def sub(key, state="waiting", **fields):
        row = {"id": key, "kind": "subgoal", "provenance": "coordinator", "parent": "O1",
               "check": f"{key} is delivered", "state": state}
        if state == "waiting":
            row["wait"] = {"class": "sequenced", "referent": "O1"}
        row.update(fields)
        return row

    def stored(self, objective="objective"):
        return [dict(row) for row in read(self.project, objective)["checkpoint"]["obligations"]]

    def packet(self, serves, *, role="implement", boundary=None, revision=None, **extra):
        from pod.config import load
        snapshot = load(self.project)
        state = read(self.project, "objective")
        return packet({"schema": "pod-packet/v3", "objective": "objective", "criteria": ["PoD#1"],
                       "responsibility": "writer", "scope": ["src"], "actions": ["edit"],
                       "candidate": self.candidate, "context": [], "dependencies": [],
                       "route": {**ROUTE, "preference_revision": snapshot["revision"]},
                       "policy_revision": snapshot["policy_revision"], "plan_revision": "plan",
                       "report_contract": "checks", "sources": [], "serves": serves, "role": role,
                       "boundary": boundary or {"paths": [], "surfaces": []},
                       "map_revision": revision or state["checkpoint"]["revision"], **extra})

    def start(self, task, frozen, accompanying=None):
        return guarded_start(self.project, "objective", owner="owner", run="run", task=task,
                             plan_revision="plan", frozen_packet=frozen, worktree="current",
                             port=self.port, accompanying=accompanying)

    def refused(self, code, detail, call, *args, **kwargs):
        with self.assertRaises(PodError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        if detail is not None:
            self.assertEqual(caught.exception.detail["detail"], detail, str(caught.exception))
        return caught.exception

    def settle(self, admission):
        self.port.workers[admission["native_binding"]["dispatchId"]]["outcome"] = "succeeded"

    def report(self, admission, frozen, *, outcome="succeeded", scope=("src",), **extra):
        body = {"schema": "pod-report/v1", "assignment": frozen["packet_id"],
                "attempt": admission["native_binding"]["dispatchId"], "candidate": self.candidate,
                "outcome": outcome, "scope": list(scope), "files": ["src/new.py"], "checks": ["unit"],
                "failures": [] if outcome == "succeeded" else ["review not completed"], "evidence": [],
                "uncertainty": [], "questions": []}
        with patch.object(OrcaPort, "show_worker", autospec=True,
                          side_effect=lambda _port, dispatch: self.port.show_worker(dispatch)):
            return internal_run("report", {"project": str(self.project), "objective": "objective",
                                           "admission_id": admission["admission_id"], "packet": frozen,
                                           "report": body, **extra})


class GovernanceTests(KernelCase):
    def policy(self, key="PA", lines="3", **fields):
        row = {"id": key, "kind": "assurance", "provenance": "project_policy",
               "source": {"path": "AGENTS.md", "lines": lines}, "check": "fresh review",
               "scope": {"paths": ["src"]}, "question": "is src correct?", "candidate": self.base,
               "existing_evidence": "unit tests", "insufficiency": "no independent review",
               "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        row.update(fields)
        return row

    def test_policy_cites_governance_at_the_base_never_the_candidate(self):
        state = self.intake(self.policy())
        source = next(row for row in state["checkpoint"]["obligations"] if row["id"] == "PA")["source"]
        import hashlib
        self.assertEqual(source["revision"], "sha256:" + hashlib.sha256(AGENTS.encode()).hexdigest())
        self.assertEqual((source["base"], source["gone"]), (self.base, False))
        self.assertEqual({row["path"]: row["revision"] is not None
                          for row in state["checkpoint"]["governance_sources"]},
                         {".pod/config.yaml": False, "AGENTS.md": True, "CLAUDE.md": False})
        (self.project / "AGENTS.md").write_text(AGENTS + "Every change needs two reviewers.\n")
        self.refused("obligation_invalid", "cite_not_found", self.write,
                     [*self.stored(), self.policy("PB", "6")])
        self.refused("obligation_invalid", "governance_source_unrecognized", self.write,
                     [*self.stored(), {**self.policy("PB"), "source": {"path": "README.md", "lines": "1"}}])
        self.refused("obligation_invalid", "cite_not_base", self.write,
                     [*self.stored(), {**self.policy("PB"), "source": {"path": "AGENTS.md", "lines": "3",
                                                                       "revision": "sha256:" + "0" * 64}}])
        rows = self.stored()
        rows[0] = {**self.criterion("withdrawn"), "withdrawal": {"by": "project_policy", "reason": "policy"}}
        rows[0].pop("executor", None)
        self.refused("obligation_invalid", "withdrawal_unauthorized", self.write, rows)
        issue_text = {"id": "PX", "kind": "criterion", "provenance": "coordinator", "check": "two reviewers",
                      "state": "unassigned"}
        self.refused("obligation_invalid", "provenance_unauthorized", self.write, [*self.stored(), issue_text])
        proposed = self.write(self.stored(), proposals=[{"id": "P1", "source": "issue", "origin_ref": "issue body",
                                                         "summary": "two reviewers", "status": "open"}])
        self.assertEqual(proposed["report"]["proposals_out_of_scope"][0]["id"], "P1")

    def test_a_git_project_binds_a_target_branch_and_unavailable_bases_hold_work(self):
        self.refused("governance_unavailable", "governance_unavailable", self.write, [self.criterion()],
                     governance={"base_ref": None})
        self.refused("governance_unavailable", "governance_unavailable", self.write, [self.criterion()],
                     governance={"base_ref": "no-such-branch"})
        self.intake(self.sub("S"))
        git(self.project, "branch", "-D", "target")
        self.refused("governance_unavailable", "governance_unavailable", self.start, "t",
                     self.packet(["S"]))

    def test_base_change_holds_new_work_until_refresh_relocates_or_marks_gone(self):
        rule = {"id": "PB", "kind": "steer", "provenance": "project_policy",
                "source": {"path": "AGENTS.md", "lines": "4"}, "check": "reviews bind to a frozen candidate",
                "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        self.intake(self.policy(), rule, self.sub("S"))
        git(self.project, "checkout", "-q", "target")
        (self.project / "AGENTS.md").write_text("# Rules\n\n\n\n" + AGENTS.replace(
            "Reviews bind to a frozen candidate.\n", ""))
        git(self.project, "commit", "-q", "-am", "move and remove rules")
        git(self.project, "checkout", "-q", "main")
        self.refused("governance_changed", "governance_changed", self.start, "t", self.packet(["S"]))
        from pod.governor import decide
        self.refused("governance_changed", "governance_changed", decide, self.project, "objective",
                     owner="owner", native_projection={"outstanding": []},
                     action={"kind": "push", "candidate": self.base, "target": "origin/x", "reason": "r"})
        refreshed = self.write(self.stored(), governance_refresh=True)["checkpoint"]
        rows = {row["id"]: row for row in refreshed["obligations"]}
        self.assertEqual((rows["PA"]["source"]["lines"], rows["PA"]["source"]["gone"]), ("7", False))
        self.assertTrue(rows["PB"]["source"]["gone"])
        self.assertEqual(refreshed["revision"], 2)
        self.assertEqual(refreshed["observations"]["policy_gone"], ["PB"])
        self.assertFalse(refreshed["observations"]["governance_stale"])
        stale = self.packet(["S"], revision=1)
        self.refused("unbound_assignment", "out_of_revision", self.start, "t", stale)
        rows = self.stored()
        for row in rows:
            if row["id"] in ("PA", "PB"):
                row.pop("wait")
                row["state"] = "withdrawn"
                row["withdrawal"] = {"by": "project_policy", "reason": "the rule was removed at the new base"}
        self.refused("obligation_invalid", "withdrawal_unauthorized", self.write, rows)
        rows = [row for row in self.stored()]
        pb = next(row for row in rows if row["id"] == "PB")
        pb.pop("wait")
        pb.update(state="withdrawn", withdrawal={"by": "project_policy", "reason": "the rule was removed"})
        self.assertEqual(self.write(rows)["checkpoint"]["revision"], 2)
        self.assertEqual(self.start("t", self.packet(["S"]))["status"], "bound")


class ResultTests(KernelCase):
    def test_changed_paths_come_from_git_and_integration_is_validated_by_ancestry(self):
        self.intake(self.sub("S", boundary={"paths": ["src"]}))
        frozen = self.packet(["S"], boundary={"paths": ["src"]})
        admission = self.start("impl", frozen)["admission"]
        self.assertEqual(read(self.project, "objective")["admissions"][admission["admission_id"]]["result"]["base"],
                         self.base)
        (self.project / "src" / "new.py").write_text("new\n")
        git(self.project, "mv", "src/old.py", "src/renamed.py")
        git(self.project, "add", "src/new.py")
        git(self.project, "commit", "-q", "-m", "worker result")
        head = git(self.project, "rev-parse", "HEAD")
        (self.project / "README.md").write_text("dirty\n")
        (self.project / "tools").mkdir()
        (self.project / "tools" / "scratch.py").write_text("untracked\n")
        self.settle(admission)
        rows = self.stored()
        s = next(row for row in rows if row["id"] == "S")
        s.pop("executor"); s.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        consumed = self.report(admission, frozen, map={"obligations": rows},
                               proposals=[{"summary": "add a Windows path mode"}])
        self.assertEqual(consumed["ingestion"]["changed_paths"],
                         ["README.md", "src/new.py", "src/old.py", "src/renamed.py", "tools/scratch.py"])
        self.assertEqual(consumed["ingestion"]["boundary_exceeded"], ["README.md", "tools/scratch.py"])
        self.assertTrue(consumed["ingestion"]["committed"])
        self.assertEqual(consumed["report"]["proposals_out_of_scope"][0]["summary"], "add a Windows path mode")
        self.assertEqual({row["id"]: row["state"] for row in self.stored()}["S"], "waiting")
        key = admission["admission_id"]
        self.refused("obligation_unaccounted", "disposition_invalid", self.write, self.stored(),
                     dispositions=[{"admission": key, "integrated_into": head}])
        self.refused("obligation_unaccounted", "disposition_invalid", self.write, self.stored(),
                     dispositions=[{"admission": key, "integrated_into": self.base, "reason": "agreed"}])
        rows = self.stored()
        s = next(row for row in rows if row["id"] == "S")
        s.pop("wait"); s.update(state="satisfied", evidence=[self.proof("S")])
        self.refused("obligation_unaccounted", "undispositioned_result", self.write, rows)
        again = self.report(admission, frozen, proposals=[{"summary": "add a Windows path mode"}])
        self.assertEqual(len(again["report"]["proposals_out_of_scope"]), 1)
        self.assertEqual(again["ingestion"]["status"], "recorded")
        state = self.write(rows, dispositions=[{"admission": key, "integrated_into": head,
                                               "reason": "the README note was requested"}])
        self.assertEqual(state["admissions"][key]["disposition"]["validated"], "ancestry")
        self.assertEqual(state["report"]["boundary_exceeded"][0]["paths"], ["README.md", "tools/scratch.py"])


class EvidenceBindingTests(KernelCase):
    def test_changed_r41_bindings_invalidate_a_satisfied_obligation_at_the_checkpoint(self):
        from pod.records import source_identity
        (self.project / "src" / "lib.py").write_text("v1\n")
        self.intake(self.sub("S", boundary={"paths": ["src"]}))
        bound = source_identity(self.project, "src/lib.py")
        rows = self.stored()
        s = next(row for row in rows if row["id"] == "S")
        s.pop("wait"); s.update(state="satisfied", evidence=[self.proof("S", sources=[bound],
                                                                        check="tests.test_lib.round_trip")])
        self.assertEqual(self.write(rows)["checkpoint"]["seq"], 2)
        self.refused("obligation_invalid", "evidence_unbound", self.write,
                     [*self.stored()[:1], {**s, "evidence": [self.proof("O1")]}])
        (self.project / "src" / "lib.py").write_text("v2\n")
        self.refused("obligation_unaccounted", "evidence_invalidated", self.write, self.stored())
        (self.project / "src" / "lib.py").write_text("v1\n")
        self.assertEqual(self.write(self.stored())["checkpoint"]["seq"], 3)
        for changed in ({"dependencies": [], "environment": "fixture"},
                        {"dependencies": ["pyyaml==6.0.3"], "environment": "another runner"}):
            with self.subTest(verification=changed):
                self.refused("obligation_unaccounted", "evidence_invalidated", self.write, self.stored(),
                             verification=changed)
        state = self.write(self.stored(), verification={"dependencies": ["pyyaml==6.0.3", "pyte==0.8.2"],
                                                        "environment": "fixture"})
        self.assertEqual(state["checkpoint"]["verification"]["dependencies"], ["pyyaml==6.0.3", "pyte==0.8.2"])
        carried = checkpoint(self.project, "objective", owner="owner",
                             value={k: v for k, v in self.core().items() if k != "verification"},
                             native={"runtime": "runtime"})
        self.assertEqual(carried["checkpoint"]["verification"], state["checkpoint"]["verification"])
        (self.project / ".pod").mkdir()
        (self.project / ".pod" / "config.yaml").write_text("schema: pod/v1\nwaste_governor: {preflight: [unit]}\n")
        self.refused("obligation_unaccounted", "evidence_invalidated", self.write, self.stored())
        rows = self.stored()
        s = next(row for row in rows if row["id"] == "S")
        s.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.assertEqual(next(row for row in self.write(rows)["checkpoint"]["obligations"]
                              if row["id"] == "S")["state"], "waiting")


class CapacityAndReviewTests(KernelCase):
    def test_capacity_wait_is_valid_only_at_the_ceiling_and_proposals_are_never_admitted(self):
        tasks = [self.sub(f"T{index}") for index in range(1, 6)]
        self.intake(*tasks, proposals=[{"id": "P1", "source": "worker_report", "origin_ref": "earlier",
                                        "summary": "an idea", "status": "open"}])
        first = self.start("t1", self.packet(["T1"]))["admission"]
        second = self.start("t2", self.packet(["T2"]))["admission"]
        self.refused("logical_capacity_full", None, self.start, "t3", self.packet(["T3"]))
        rows = self.stored()
        t3 = next(row for row in rows if row["id"] == "T3")
        t3["wait"] = {"class": "capacity", "referent": [first["admission_id"], second["admission_id"]]}
        self.assertEqual(self.write(rows)["checkpoint"]["seq"], 4)
        # A stopped failed Dispatch whose terminal is retained is exact settlement.
        self.port.workers[first["native_binding"]["dispatchId"]]["outcome"] = "stopped"
        self.assertIsNotNone(self.port.workers[first["native_binding"]["dispatchId"]]["terminal"])
        rows = self.stored()
        t1 = next(row for row in rows if row["id"] == "T1")
        t1.pop("executor"); t1.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        self.refused("wait_invalid", "capacity_below_ceiling", self.write, rows)
        self.refused("unbound_assignment", "proposed", self.start, "p", self.packet(["P1"]))
        t3 = next(row for row in rows if row["id"] == "T3")
        t3["wait"] = {"class": "sequenced", "referent": "O1"}
        admitted = self.start("t3", self.packet(["T3"]), accompanying={"obligations": rows})
        self.assertEqual(admitted["status"], "bound")
        state = read(self.project, "objective")["checkpoint"]
        self.assertEqual({row["id"]: row.get("executor") for row in state["obligations"]}["T3"],
                         admitted["admission"]["admission_id"])

    def test_review_triage_and_the_derived_label(self):
        assurance = {"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                     "candidate": self.base, "existing_evidence": "unit tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        self.intake(assurance)
        frozen = self.packet(["A"], role="review")
        review = self.start("review", frozen)["admission"]
        self.refused("unbound_assignment", "obligation_busy", self.start, "review-2", self.packet(["A"], role="review"))
        self.settle(review)
        rows = self.stored()
        a = next(row for row in rows if row["id"] == "A")
        a.pop("executor"); a.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        rows.append({"id": "K1", "kind": "correction", "provenance": "coordinator", "parent": "A",
                     "check": "the off-by-one is fixed", "boundary": {"paths": ["src"]}, "state": "unassigned"})
        consumed = self.report(review, frozen, map={"obligations": rows}, triage=[
            {"finding": "f1", "severity": "blocker", "triage": "required_correction", "summary": "off by one",
             "correction": "K1"},
            {"finding": "f2", "severity": "major", "triage": "advisory", "summary": "naming",
             "reason": "style only"}])
        self.assertEqual(consumed["report"]["triage_downgrades"][0]["reason"], "style only")
        acceptance = {"project": str(self.project), "objective": "objective", "criteria": ["PoD#1"],
                      "evidence_rows": [], "candidate": self.base, "policy_revision": "p", "sources": [],
                      "dependencies": [], "environment": "fixture", "review_required": True,
                      "hosted_required": False}
        gate = {"schema": "pod-evidence/v1", "criterion": "gate:review", "candidate": self.base,
                "sources": [], "policy_revision": "p", "dependencies": [], "environment": "fixture",
                "check": "review", "command": "review", "result": "approved",
                "timestamp": "2026-09-24T00:00:00Z", "status": "PASS", "reference": "asserted"}
        with patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            withheld = internal_run("acceptance", {**acceptance, "evidence_rows": [gate]})
        self.assertEqual(withheld["independently_reviewed"], "WITHHELD")
        self.assertEqual(withheld["assurance_unbound"], [{"obligation": "A", "gap": "unbound"}])
        # A caller's review_required=False cannot waive the recorded assurance obligation.
        passing = {**gate, "criterion": "PoD#1", "check": "unit", "result": "passed", "reference": "log"}
        owner = {"schema": "pod-acceptance-authorization/v1", "candidate": self.base, "policy_revision": "p",
                 "utc": "2026-09-24T00:00:00Z", "accepted_by": "owner"}
        waived = {**acceptance, "review_required": False, "evidence_rows": [passing], "owner_acceptance": owner}
        with patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            not_waived = internal_run("acceptance", waived)
        self.assertTrue(not_waived["required_checks_pass"])
        self.assertEqual((not_waived["independently_reviewed"], not_waived["accepted"]), ("WITHHELD", False))
        rows = self.stored()
        by = {row["id"]: row for row in rows}
        by["K1"].update(state="satisfied", evidence=[self.proof("K1")])
        by["A"].pop("wait"); by["A"].update(state="satisfied", evidence=[{"attempt": review["admission_id"]}])
        self.write(rows)
        with patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            qualified = internal_run("acceptance", acceptance)
        self.assertEqual((qualified["independently_reviewed"], qualified["assurance_unbound"]), ("QUALIFIED", []))
        self.assertEqual(qualified["report"]["label"]["label"], "QUALIFIED")
        self.refused("unbound_assignment", "satisfied", self.start, "review-3", self.packet(["A"], role="review"))
        with patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            self.assertEqual(internal_run("acceptance", waived)["accepted"], True)
            # A changed effective Governor policy invalidates the review binding (R41).
            (self.project / ".pod").mkdir()
            (self.project / ".pod" / "config.yaml").write_text("schema: pod/v1\nwaste_governor: {preflight: [unit]}\n")
            drifted = internal_run("acceptance", acceptance)
        self.assertEqual(drifted["assurance_unbound"], [{"obligation": "A", "gap": "invalidated"}])
        self.refused("obligation_unaccounted", "evidence_invalidated", self.write, self.stored())

    def test_an_incomplete_review_report_cannot_qualify_assurance(self):
        assurance = {"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                     "candidate": self.base, "existing_evidence": "unit tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        self.intake(assurance)
        frozen = self.packet(["A"], role="review")
        review = self.start("review", frozen)["admission"]
        self.settle(review)
        rows = self.stored()
        a = next(row for row in rows if row["id"] == "A")
        a.pop("executor"); a.update(state="waiting", wait={"class": "sequenced", "referent": "O1"})
        consumed = self.report(review, frozen, map={"obligations": rows}, outcome="failed")
        self.assertEqual(consumed["observation"]["outcome"], "failed")
        stored = read(self.project, "objective")["admissions"][review["admission_id"]]["report"]
        self.assertEqual((stored["outcome"], stored["status"]), ("failed", "validated_observation"))
        rows = self.stored()
        a = next(row for row in rows if row["id"] == "A")
        a.pop("wait"); a.update(state="satisfied", evidence=[{"attempt": review["admission_id"]}])
        self.refused("obligation_unaccounted", "evidence_invalidated", self.write, rows)
        rescoped = self.report(review, frozen, outcome="succeeded", scope=["src", "docs"])
        self.assertEqual(rescoped["status"], "reconciliation_required")
        self.refused("obligation_unaccounted", "evidence_invalidated", self.write, rows)
        with patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            label = internal_run("acceptance", {
                "project": str(self.project), "objective": "objective", "criteria": ["PoD#1"],
                "evidence_rows": [], "candidate": self.base, "policy_revision": "p", "sources": [],
                "dependencies": [], "environment": "fixture", "review_required": False,
                "hosted_required": False})
        self.assertEqual(label["independently_reviewed"], "WITHHELD")


class LifecycleTests(KernelCase):
    def test_trivial_work_stays_map_free_and_the_first_delegation_needs_a_map(self):
        state = checkpoint(self.project, "objective", owner="owner", value=self.core(),
                           native={"runtime": "runtime"})
        self.assertNotIn("obligations", state["checkpoint"])
        self.assertNotIn("report", state)
        from pod.config import load
        snapshot = load(self.project)
        frozen = packet({"schema": "pod-packet/v3", "objective": "objective", "criteria": ["PoD#1"],
                         "responsibility": "writer", "scope": ["src"], "actions": ["edit"],
                         "candidate": self.candidate, "context": [], "dependencies": [],
                         "route": {**ROUTE, "preference_revision": snapshot["revision"]},
                         "policy_revision": snapshot["policy_revision"], "plan_revision": "plan",
                         "report_contract": "checks", "sources": [], "serves": ["O1"], "role": "implement",
                         "boundary": {"paths": ["src"]}, "map_revision": 1})
        self.refused("unbound_assignment", "no_map", self.start, "t", frozen)
        self.assertEqual(self.port.starts, [])
        self.assertEqual(read(self.project, "objective")["admissions"], {})

    def test_quiescent_objective_stays_open_and_continues_without_reopen(self):
        external = {"party": "user", "need": "a non-Owner test identity",
                    "unblocks_when": "the user supplies the identity"}
        state = self.write([self.criterion("blocked_external", external=external),
                            self.sub("S", wait={"class": "dependency", "referent": "O1"})],
                           governance={"base_ref": "target"})
        quiescence = state["checkpoint"]["quiescence"]
        self.assertEqual(quiescence["state"], "quiescent")
        self.assertIn("a non-Owner test identity (unblocks when the user supplies the identity)",
                      quiescence["interim_report"])
        self.assertEqual(state["report"]["status"], "quiescent")
        self.assertIsNone(state["checkpoint"]["closure"])
        rows = self.stored()
        rows[0] = self.criterion("satisfied", evidence=[self.proof("O1", check="identity")])
        continued = self.start("s", self.packet(["S"]), accompanying={"obligations": rows})
        self.assertEqual(continued["status"], "bound")

    def test_closure_refuses_admission_and_every_governor_mutation_until_user_reopen(self):
        from pod import governor
        evidence = [self.proof("O1")]
        self.intake(self.sub("S"))
        rows = [self.criterion("satisfied", evidence=evidence),
                {**self.sub("S", "withdrawn"), "withdrawal": {"by": "coordinator", "reason": "folded into O1"}}]
        closed = self.write(rows, close=True)["checkpoint"]
        self.assertEqual(closed["closure"]["report"]["withdrawn"][0]["reason"], "folded into O1")
        revision = closed["revision"]
        calls = {
            "decide": lambda: governor.decide(self.project, "objective", owner="owner", native_projection={},
                                              action={"kind": "push", "candidate": self.base, "target": "o/x",
                                                      "reason": "r"}),
            "record_outcome": lambda: governor.record_outcome(self.project, "objective", owner="owner",
                                                              record_id="r", outcome="PASS"),
            "classify_failure": lambda: governor.classify_failure(self.project, "objective", owner="owner",
                                                                  record_id="r",
                                                                  classification={"class": "transient",
                                                                                  "reason": "x"}),
            "record_correction": lambda: governor.record_correction(self.project, "objective", owner="owner",
                                                                    unit="u", correction={}),
            "prepare_candidate": lambda: governor.prepare_candidate(
                self.project, "objective", owner="owner", unit="u",
                observation=governor.observe_candidate(self.project, base_ref=None)),
            "record_preflight": lambda: governor.record_preflight(self.project, "objective", owner="owner",
                                                                  unit="u", candidate="c", check="unit",
                                                                  status="PASS"),
            "record_execution": lambda: governor._record_execution(
                self.project, "objective", owner="owner", record_id="r", outcome="PASS", provider=None,
                detail=None, now=__import__("datetime").datetime.now()),
            "reconcile": lambda: governor.reconcile(self.project, "objective", owner="owner", record_id="r"),
            "admission": lambda: self.start("s", self.packet(["S"], revision=revision)),
            "constraint": lambda: constraints_update(self.project, "objective", owner="owner", action="add",
                                                     value={"id": "c", "kind": "max_workers",
                                                            "provenance": "user_direct", "value": 1}),
            "checkpoint": lambda: self.write(rows),
            "refresh": lambda: self.write(rows, governance_refresh=True),
            "internal-governor": lambda: internal_run("governor-preflight", {
                "project": str(self.project), "objective": "objective", "owner": "owner", "unit": "u",
                "candidate": "c", "check": "unit", "status": "PASS"}),
        }
        for name, call in calls.items():
            with self.subTest(mutation=name):
                self.refused("objective_closed", "objective_closed", call)
        self.assertEqual(list(objective_root(self.project, "objective").glob("governor.json")), [])
        reopened = self.write([*rows, {"id": "N", "kind": "criterion", "provenance": "user_direct",
                                       "source": {"instruction": "also support CSV"}, "check": "CSV works",
                                       "state": "active", "executor": "coordinator"}],
                              reopen=True, revision_authority={"provenance": "user_direct",
                                                               "instruction": "also support CSV"})
        self.assertIsNone(reopened["checkpoint"]["closure"])
        self.assertEqual(reopened["checkpoint"]["reopened"][0]["closed_revision"], revision)


class StatusAndCutoverTests(KernelCase):
    def status(self, *args):
        from pod.cli import execute, parser
        with patch("pod.cli.current_run", return_value={"runtime": "runtime", "run": {"id": "run"}}), \
             patch("pod.cli.worker_rows", return_value={"runtime": "runtime", "scope": "run", "workers": [],
                                                        "complete": True}), \
             patch.object(OrcaPort, "read_native", autospec=True,
                          side_effect=lambda _port, owner, **kwargs: self.port.read_native(owner, **kwargs)):
            return execute(parser().parse_args(["status", "--json", *args]), self.project)

    def test_status_shows_every_state_with_its_referent_without_writing(self):
        external = {"party": "provider", "need": "a hosted runner", "unblocks_when": "the quota resets"}
        self.intake(self.sub("S"), self.sub("B", "blocked_external", external=external))
        path = objective_root(self.project, "objective") / "context.json"
        before = path.read_bytes()
        status = self.status()
        lines = "\n".join(status["obligation_lines"])
        self.assertIn("O1   active           coordinator", lines)
        self.assertIn("S    waiting          sequenced → O1", lines)
        self.assertIn("B    blocked_external provider: a hosted runner", lines)
        self.assertIn("Assurance  none none_recorded", lines)
        self.assertEqual(status["obligations"]["counts"]["blocked_external"], 1)
        self.assertEqual(path.read_bytes(), before)

    def test_superseded_objectives_are_listed_blocked_and_never_converted(self):
        root = state_root(self.project)
        record = root / "earlier" / "context.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"schema": "pod-context/v4", "revision": 1, "owner": "owner",
                                      "checkpoint": {"schema": "pod-checkpoint/v2", "objective": "earlier",
                                                     "native_refs": [{"runId": "run05", "runtime": "runtime"}]},
                                      "admissions": {}, "interventions": {}, "source_rejections": {},
                                      "constraints": []}))
        before = record.read_bytes()
        inventory = state_inventory(self.project)
        self.assertEqual(inventory["superseded"], [{"record": "earlier", "objective": "earlier",
                                                    "schemas": ["pod-checkpoint/v2"], "blocked": True,
                                                    "converted": False}])
        status = self.status("--run", "run05")
        self.assertEqual((status["status"], status["blocker"]), ("blocked", "objective_superseded"))
        self.assertIn("start a new objective", status["next_safe_action"])
        from pod.cli import _doctor
        self.assertEqual(_doctor(self.project)["state"]["superseded"][0]["schemas"], ["pod-checkpoint/v2"])
        from pod.installer import _cutover_notice, _paths
        with redirect_stdout(StringIO()) as notice:
            _cutover_notice(_paths())
        self.assertIn("earlier: pod-checkpoint/v2", notice.getvalue())
        self.assertIn("nothing was converted", notice.getvalue())
        with self.assertRaises(PodError) as blocked:
            from pod.ledger import _read
            _read(record)
        self.assertEqual(blocked.exception.code, "state_unsupported")
        self.assertIn("pod-checkpoint/v2", str(blocked.exception))
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(self.intake()["checkpoint"]["seq"], 1)
        self.assertEqual(record.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
