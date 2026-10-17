"""R114: one plain readiness answer for every reader, projected from Pod's records; new fields only."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import types
import unittest
from unittest.mock import patch

from pod import orca, readiness, status as current_status
from pod.governor import _read_journal, _record_path, _write_journal, finalization
from pod.internal import run
from pod.ledger import objective_root, read
from tests.kernel_support import git
from tests.test_historical_retirement import RetirementCase
from tests.test_succession import SuccessionCase

BASELINE = "5f7298bab47c4a3263496dc2a7fe50ecfcda0530"  # Pod 0.12.0, the last status without readiness
REPOSITORY = Path(__file__).resolve().parents[1]
NEW_FIELDS = ("readiness", "successor_claim", "record_capacity", "worker_tabs", "pod_ticks", "recorded_questions")
DECISION_FIELDS = ("owner_decisions", "derived_governance_decisions")


def baseline_status() -> types.ModuleType:
    """The 0.12.0 status module from genuine Git objects, joined to the current package."""
    source = subprocess.run(["git", "-C", str(REPOSITORY), "show", f"{BASELINE}:skills/pod/status.py"],
                            capture_output=True, text=True, check=True).stdout
    module = types.ModuleType("pod._status_0120")
    module.__package__ = "pod"
    exec(compile(source, "status-0.12.0.py", "exec"), module.__dict__)
    return module


def offline(*_args, **_kwargs):
    raise AssertionError("the objective list reads no Orca state")


class ReadinessCase(RetirementCase):
    def read_as(self, caller, objective="objective", *, text=False):
        if caller is None:
            os.environ.pop("ORCA_TERMINAL_HANDLE", None)  # A reader outside any Orca terminal.
        else:
            os.environ["ORCA_TERMINAL_HANDLE"] = caller
        return self.status(objective=objective, text=text)

    def rows(self, *rows):
        """Governor rows appended through the journal's own writer, as an executed effect leaves them."""
        path = _record_path(self.project, "objective")
        journal = _read_journal(path)
        journal["actions"].extend(rows)
        _write_journal(path, journal)

    @staticmethod
    def row(record_id, kind, outcome, **extra):
        return {"record_id": record_id, "logical_key": record_id, "attempt": 1,
                "action": {"kind": kind, "candidate": "candidate-1",
                           "target": "ci.yml" if kind != "merge" else "origin/target", "unit": "default"},
                "decision": "ALLOW", "outcome": outcome, "receipt": {"provider": {}}, "warnings": [],
                "reasons": [], "classifications": [], **extra}

    def closed(self, objective="objective", **fields):
        value = self.core(obligations=[self.criterion("satisfied", evidence=[self.proof("O1")])],
                          governance={"base_ref": "refs/remotes/origin/target"}, close=True, **fields)
        run("checkpoint", {"project": str(self.project), "objective": objective, "owner": "owner", "value": value})

    def readiness(self, caller, objective="objective"):
        return self.read_as(caller, objective)["readiness"]


class StateTests(ReadinessCase):
    def test_working_reads_the_same_for_the_owner_a_live_peer_and_a_reader_outside_orca(self):
        self.establish()
        before = self.snapshot()
        owner = self.read_as("owner")
        readiness = owner["readiness"]
        self.assertEqual((readiness["contract"], readiness["state"], readiness["coordinator_needed"],
                          readiness["acts_next"]), ("pod-readiness/v1", "working", True, "coordinator"))
        self.assertEqual(readiness["coordinator"], {"handle": "owner", "run": "run", "runs": ["run"],
                                                    "generation": 1, "runtime": "runtime", "liveness": "live",
                                                    "code": None})
        self.assertEqual(readiness["finalization"]["remaining"], ["report_consumption", "closure"])
        self.assertNotIn("successor_claim", owner)  # The recorded coordinator needs no successor view.
        for caller in ("next", None):
            with self.subTest(caller=caller):
                other = self.read_as(caller)
                self.assertEqual(other["readiness"], readiness)
                self.assertEqual((other["successor_claim"]["status"], other["successor_claim"]["condition"]),
                                 ("claim_unavailable", "predecessor_live"))
        text = self.read_as("next", text=True)
        self.assertEqual(text.splitlines()[0], readiness["summary"])
        self.assertIn("Successor claim: refused (predecessor_live)", text)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.mutations, [])

    def test_waiting_names_the_party_and_what_it_must_supply(self):
        self.prepare()
        self.write([self.criterion("blocked_external", external={
            "party": "owner", "need": "choose the release channel", "unblocks_when": "the owner answers"})])
        readiness = self.readiness("owner")
        self.assertEqual((readiness["state"], readiness["acts_next"]), ("waiting", "owner"))
        self.assertEqual(readiness["waiting_on"], [{"obligation": "O1", "party": "owner",
                                                    "need": "choose the release channel"}])
        self.assertIn("Waiting on the owner: choose the release channel (O1)", readiness["summary"])
        self.assertEqual(self.readiness("next"), readiness)

    def test_a_merged_candidate_with_a_pending_readback_never_reads_safe(self):
        self.establish()
        self.op("checkpoint", value=self.core(obligations=self.stored(), worktree=self.binding()))
        published = self.op("governor-execute", action=self.action(effects=["workflow:ci.yml"]))
        derived = next(row for row in _read_journal(_record_path(self.project, "objective"))["actions"]
                       if row.get("derived_from") == published["record_id"])
        self.rows(self.row("merge-1", "merge", "PASS"))
        self.settle(self.admission)
        before = self.snapshot()
        readiness = self.readiness("owner")
        self.assertEqual((readiness["state"], readiness["coordinator_needed"]), ("finalizing", True))
        pending = readiness["finalization"]
        # One list of what remains: the Governor's finalization projection, embedded unchanged.
        self.assertEqual(pending, finalization(self.state(), _read_journal(_record_path(self.project, "objective"))))
        self.assertEqual(pending["remaining"], ["delivery_record", "validation_readback", "report_consumption",
                                                "closure"])
        self.assertEqual([(row["record"], row["kind"], row["state"]) for name in pending["remaining"]
                          for row in pending["records"][name]],
                         [("merge-1", "merge", "PASS"), (derived["record_id"], "workflow_dispatch", "pending"),
                          (self.admission["admission_id"], "implement", "bound")])
        self.assertEqual(pending["records"]["validation_readback"][0]["derived_from"], published["record_id"])
        self.assertTrue(readiness["summary"].startswith(
            "Delivered but finalizing: Pod still needs the delivery record of merge merge-1, the validation "
            f"readback of publication {published['record_id'][:12]} ({derived['record_id'][:12]}), the implement "
            "worker's report"), readiness["summary"])
        self.assertIn("not safe to close", readiness["summary"])
        self.assertEqual(self.readiness("next"), readiness)
        self.assertEqual(before, self.snapshot())

    def test_merge_decided_but_unrecorded_names_the_merge_outcome(self):
        self.prepare()
        self.rows(self.row("merge-1", "merge", "UNKNOWN"))
        readiness = self.readiness("owner")
        self.assertEqual(readiness["state"], "finalizing")
        self.assertEqual(readiness["finalization"]["remaining"], ["merge_outcome", "closure"])
        self.assertEqual(readiness["finalization"]["records"]["merge_outcome"],
                         [{"record": "merge-1", "kind": "merge", "state": "UNKNOWN", "derived_from": None}])
        self.assertIn("the merge outcome (merge-1) and closure", readiness["summary"])

    def test_closed_reads_closed_after_main_advances(self):
        self.prepare()
        self.closed()
        readiness = self.readiness("owner")
        self.assertEqual((readiness["state"], readiness["coordinator_needed"], readiness["acts_next"],
                          readiness["finalization"]["remaining"], readiness["finalization"]["finished"]),
                         ("closed", False, "none", [], True))
        self.assertEqual(readiness["summary"], "Closed: Pod needs nothing more; its coordinator may close.")
        self.assertEqual(readiness["coordinator"]["liveness"], "not_read")
        other = self.read_as("next")
        self.assertEqual(other["readiness"], readiness)
        self.assertEqual(other["successor_claim"]["condition"], "predecessor_closed")
        (self.project / "README.md").write_text("main moved on\n")
        git(self.project, "commit", "-qam", "later work on main")
        git(self.project, "update-ref", "refs/remotes/origin/target", git(self.project, "rev-parse", "HEAD"))
        self.assertEqual(self.readiness("owner"), readiness)

    def test_closed_with_external_work_names_each_gate_and_who_acts(self):
        self.prepare()
        self.closed(remaining_gates=[{"kind": "hosted_ci", "workflow": "ci.yml"}, "owner publishes the notes"])
        readiness = self.readiness("owner")
        self.assertEqual((readiness["state"], readiness["coordinator_needed"], readiness["acts_next"]),
                         ("closed_external", False, "owner"))
        self.assertEqual(readiness["external"], [{"gate": {"kind": "hosted_ci", "workflow": "ci.yml"},
                                                  "party": "provider"},
                                                 {"gate": "owner publishes the notes", "party": None}])
        self.assertIn("its coordinator may close", readiness["summary"])
        self.assertEqual(self.readiness("next"), readiness)

    def test_a_closed_objective_still_reads_closed_after_its_worktree_is_removed(self):
        self.prepare()
        linked = self.root / "linked"
        git(self.project, "worktree", "add", "-q", str(linked), "-b", "linked")
        context = __import__("pod.github", fromlist=["repository_context"]).repository_context(linked)
        value = self.core(obligations=[self.criterion("satisfied", evidence=[self.proof("O1")])],
                          governance={"base_ref": "refs/remotes/origin/target"}, close=True,
                          worktree={"repository": context["repository"], "repo_key": context["repo_key"],
                                    "path": context["worktree"], "branch": context["branch"]})
        run("checkpoint", {"project": str(linked), "objective": "objective", "owner": "owner", "value": value})
        self.assertEqual(self.readiness("owner")["state"], "closed")
        git(self.project, "worktree", "remove", "--force", str(linked))
        self.assertFalse(linked.exists())
        self.assertEqual(self.readiness("owner")["state"], "closed")


class LostCoordinatorTests(ReadinessCase):
    def test_a_lost_coordinator_names_its_recovery_routes_for_every_non_owner_reader(self):
        self.predecessor()
        before = self.snapshot()
        peer = self.read_as("next")
        readiness = peer["readiness"]
        self.assertEqual((readiness["state"], readiness["acts_next"], readiness["coordinator_needed"]),
                         ("coordinator_lost", "owner", True))
        self.assertEqual((readiness["coordinator"]["liveness"], readiness["coordinator"]["code"]),
                         ("lost", "terminal_handle_stale"))
        routes = {row["route"]: (row["status"], row["condition"]) for row in readiness["recovery"]["routes"]}
        self.assertEqual(routes, {"same_run_resume": ("blocked", "owner_not_live"),
                                  "owner_handoff": ("available", "fresh"),
                                  "successor_claim": ("available", "claimable")})
        self.assertEqual(readiness["recovery"]["route"], "owner_handoff")
        self.assertIn("recoverable through owner handoff", readiness["summary"])
        claim = peer["successor_claim"]
        self.assertEqual((claim["status"], claim["facts"]["predecessor_owner"], claim["facts"]["runs"],
                          claim["unreported"]), ("claim_available", "owner", ["run"], [self.admission["admission_id"]]))
        self.assertEqual(self.read_as(None)["readiness"], readiness)
        self.terminals["owner"] = "exited"
        closed_tab = self.readiness("next")
        routes = {row["route"]: (row["status"], row["condition"]) for row in closed_tab["recovery"]["routes"]}
        self.assertEqual(routes["owner_handoff"], ("blocked", "owner_not_lost"))
        self.assertEqual(closed_tab["recovery"]["route"], "successor_claim")
        self.assertIn("Orca reports its terminal closed; recoverable through successor claim", closed_tab["summary"])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.mutations, [])

    def test_a_lost_coordinator_with_a_pending_effect_names_the_carry_or_the_exact_refusal(self):
        self.establish()
        self.op("checkpoint", value=self.core(obligations=self.stored(), worktree=self.binding()))
        published = self.op("governor-execute", action=self.action(effects=["workflow:ci.yml"]))
        self.settle(self.admission)
        self.lose()
        self.terminals["owner"] = "exited"
        readiness = self.readiness("next")
        self.assertEqual(readiness["state"], "coordinator_lost")
        # R118: a successor's claim carries the pending derived validation and settles it by readback.
        self.assertEqual(readiness["recovery"]["route"], "successor_claim")
        routes = {row["route"]: (row["status"], row["condition"]) for row in readiness["recovery"]["routes"]}
        self.assertEqual(routes, {"same_run_resume": ("blocked", "owner_not_live"),
                                  "owner_handoff": ("blocked", "owner_not_lost"),
                                  "successor_claim": ("available", "claimable")})
        derived = readiness["finalization"]["records"]["validation_readback"]
        self.assertEqual([(row["kind"], row["state"], row["derived_from"]) for row in derived],
                         [("workflow_dispatch", "pending", published["record_id"])])
        self.assertEqual(self.read_as("next")["successor_claim"]["facts"]["carried"], [derived[0]["record"]])
        self.assertIn(f"Pod still needs the validation readback of publication {published['record_id'][:12]}",
                      readiness["summary"])
        self.assertEqual(self.read_as(None)["readiness"], readiness)
        # A pending release has no readback: the claim refuses it, and the summary names that exact refusal.
        self.rows(self.row("r" * 64, "release", "pending", receipt={"started_at": "2026-01-01T00:00:00+00:00"}))
        blocked = self.readiness("next")
        routes = {row["route"]: (row["status"], row["condition"]) for row in blocked["recovery"]["routes"]}
        self.assertEqual(routes["successor_claim"], ("blocked", "predecessor_effect_pending"))
        self.assertIsNone(blocked["recovery"]["route"])
        self.assertIn("recovery blocked: successor claim refused (predecessor_effect_pending: The predecessor's "
                      "pending release rrrrrrrrrrrr cannot be carried: a release is performed by the project and has "
                      "no readback.)", blocked["summary"])


class EndedTests(ReadinessCase):
    def test_an_owner_retirement_says_why_pod_needs_no_coordinator(self):
        self.delivered()
        self.retire(self.decide())
        ended = self.read_as("next")
        readiness = ended["readiness"]
        self.assertEqual((readiness["state"], readiness["coordinator_needed"], readiness["acts_next"]),
                         ("retired", False, "none"))
        self.assertEqual(readiness["reason"]["retired_at"], ended["retirement"]["retired_at"])
        self.assertEqual(readiness["reason"]["reason"], "delivered; coordinator permanently gone")
        self.assertEqual(ended["successor_claim"]["condition"], "predecessor_retired")
        self.assertEqual(self.read_as(None)["readiness"], readiness)

    def test_a_continued_record_names_its_successor_and_the_successor_reads_its_own_work(self):
        self.predecessor(); self.successor()
        self.claim(SuccessionCase.decide(self))
        continued = self.read_as("next")
        readiness = continued["readiness"]
        self.assertEqual((readiness["state"], readiness["coordinator_needed"], readiness["acts_next"]),
                         ("continued", False, "successor"))
        self.assertEqual(readiness["reason"], {"successor": "successor", "succession_state": "successor_open"})
        self.assertEqual(continued["successor_claim"]["condition"], "predecessor_claimed")
        successor = self.read_as("next", "successor")
        self.assertEqual(successor["readiness"]["state"], "working")
        self.assertNotIn("successor_claim", successor)

    def test_unknown_and_unreadable_objectives_are_unavailable_with_the_reason(self):
        self.prepare()
        unknown = self.read_as("owner", "missing")
        self.assertEqual((unknown["readiness"]["state"], unknown["readiness"]["reason"]["code"],
                          unknown["readiness"]["coordinator_needed"]), ("unavailable", "objective_unknown", None))
        self.assertEqual(unknown["blocker"], "objective_unknown")
        _record_path(self.project, "objective").write_text("{")
        broken = self.read_as("owner")
        self.assertEqual((broken["readiness"]["state"], broken["readiness"]["reason"]["code"]),
                         ("unavailable", "governor_unreadable"))
        self.assertTrue(broken["readiness"]["summary"].startswith("Unavailable: the Governor journal cannot be read"))


class ListLabelTests(ReadinessCase):
    def test_the_list_carries_a_record_only_label_without_orca(self):
        self.prepare()
        self.closed(objective="closed-one")
        self.rows(self.row("merge-1", "merge", "PASS"))
        with patch("pod.orca.read_command", side_effect=offline):
            out = StringIO()
            old = os.getcwd()
            try:
                os.chdir(self.project)
                with redirect_stdout(out):
                    from pod import cli
                    cli.main(["status", "--list", "--json"])
                    cli.main(["status", "--list"])
            finally:
                os.chdir(old)
        listed, text = out.getvalue().split("\n", 1)
        rows = {row["objective"]: row["readiness"] for row in json.loads(listed)["objectives"]}
        self.assertEqual(rows, {"objective": "finalizing", "closed-one": "closed"})
        self.assertIn("| readiness finalizing", text)
        _record_path(self.project, "closed-one").write_text("{")
        with patch("pod.orca.read_command", side_effect=offline):
            from pod.listing import objective_list
            rows = {row["objective"]: row["readiness"] for row in objective_list(self.project)["objectives"]}
        self.assertEqual(rows["closed-one"], "unavailable")


class ProjectionTests(unittest.TestCase):
    """The record projection alone, on minimal records: wait classes, gate parties and bounds."""

    @staticmethod
    def state(obligations, admissions=None, **checkpoint):
        return {"checkpoint": {"objective": "o", "obligations": obligations, **checkpoint},
                "admissions": admissions or {}}

    def test_wait_classes_name_their_party_or_none(self):
        quiescence = {"state": "quiescent", "blocked": [
            {"obligation": "O1", "via": "O1", "class": "authority", "referent": "merge consent"},
            {"obligation": "O2", "via": "O2", "class": "input_unavailable", "referent": "fixture data"},
            {"obligation": "O3", "via": "O1", "class": "authority", "referent": "merge consent"}]}
        block = readiness.base(self.state([{"id": "O1", "state": "waiting"}], quiescence=quiescence),
                               {"actions": []}, outstanding=set())
        self.assertEqual((block["state"], block["acts_next"]), ("waiting", "owner"))
        self.assertEqual([(row["obligation"], row["party"]) for row in block["waiting_on"]],
                         [("O1", "owner"), ("O2", None)])
        self.assertEqual(readiness._summary(block), "Waiting on the owner: authority merge consent (O1) and 1 more; "
                                                    "not finished.")
        block["waiting_on"] = block["waiting_on"][1:]
        self.assertEqual(readiness._summary(block), "Waiting for: input_unavailable fixture data (O2); not finished.")

    def test_delivery_under_way_finalizes_and_the_line_stays_short(self):
        rows = [ReadinessCase.row(f"v{index}", "validation_rerun", "pending") for index in range(6)]
        for obligations, actions, expected in (
                ([{"id": "O1", "state": "active", "executor": "coordinator"}], rows, "working"),
                ([{"id": "O1", "state": "satisfied"}], [], "finalizing"),
                ([{"id": "O1", "state": "active", "executor": "coordinator"}],
                 [ReadinessCase.row("m", "merge", "pending")], "finalizing"),
                ([{"id": "O1", "state": "satisfied"}], rows + [ReadinessCase.row("m", "merge", "PASS")], "finalizing")):
            with self.subTest(expected=expected, actions=len(actions)):
                block = readiness.base(self.state(obligations), {"actions": actions}, outstanding=set())
                self.assertEqual(block["state"], expected)
                self.assertEqual(block["finalization"], finalization(self.state(obligations), {"actions": actions}))
        self.assertIn("readback (v1), 4 more record(s) and closure", readiness._summary(block))
        mapless = readiness.base({"checkpoint": {"objective": "o"}, "admissions": {}}, {"actions": rows},
                                 outstanding=set())
        self.assertEqual((mapless["state"], mapless["finalization"]["remaining"]), ("working", ["validation_readback"]))
        self.assertIn("no obligation map is recorded", readiness._summary(mapless))

    def test_the_plain_line_names_each_record_by_its_kind(self):
        rows = [ReadinessCase.row("r1", "release", "UNKNOWN"), ReadinessCase.row("p1", "push", "pending"),
                ReadinessCase.row("m1", "merge", "PASS")]
        admissions = {"i1": {"role": "implement", "state": "bound", "report": {"status": "validated_observation"},
                             "disposition": None},
                      "v1": {"role": "review", "state": "bound", "report": None}}
        block = readiness.base(self.state([{"id": "O1", "state": "satisfied"}], admissions), {"actions": rows},
                               outstanding=set())
        self.assertEqual(readiness._phrase(block["finalization"]),
                         "the delivery record of merge m1, the release outcome (r1), the push readback (p1), "
                         "2 more record(s) and closure")
        block["finalization"]["records"]["validation_readback"] = []
        self.assertEqual(readiness._phrase(block["finalization"]),
                         "the delivery record of merge m1, the disposition of implementation result i1, "
                         "the review worker's report (v1) and closure")

    def test_gate_parties_decide_who_acts_after_closure(self):
        closure = {"seq": 1, "revision": 1, "report": {}}
        for gates, acts in (([], "none"), ([{"kind": "hosted_ci"}], "provider"),
                            ([{"kind": "x", "party": "third_party"}, {"party": "third_party"}], "third_party"),
                            ([{"kind": "hosted_ci"}, "notes"], "owner"), (["notes"], "owner")):
            with self.subTest(gates=gates):
                block = readiness.base(self.state([{"id": "O1", "state": "satisfied"}], closure=closure,
                                                  remaining_gates=gates), {"actions": []}, outstanding=set())
                self.assertEqual((block["state"], block["acts_next"], block["coordinator_needed"]),
                                 ("closed_external" if gates else "closed", acts, False))
                self.assertIn(block["state"], readiness.STATES)


class CompatibilityTests(ReadinessCase):
    """Every prior field keeps its exact value; readiness and R120 inventories are additive."""

    def compare(self, caller, objective="objective"):
        if caller is None:
            os.environ.pop("ORCA_TERMINAL_HANDLE", None)
        else:
            os.environ["ORCA_TERMINAL_HANDLE"] = caller
        results = []
        for module in (baseline_status(), current_status):
            old = os.getcwd()
            output = StringIO()
            try:
                os.chdir(self.project)
                result = module.status(self.project, None, objective=objective, current_run_fn=orca.current_run,
                                       worker_rows_fn=orca.worker_rows)
                with redirect_stdout(output):
                    module.render(result)
            finally:
                os.chdir(old)
            results.append((json.loads(json.dumps(result)), output.getvalue()))
        (expected, expected_text), (observed, observed_text) = results
        self.assertTrue(set(NEW_FIELDS) & set(observed) and not set(NEW_FIELDS) & set(expected))
        self.assertEqual({key: value for key, value in observed.items() if key not in NEW_FIELDS + DECISION_FIELDS}, expected)
        lines = observed_text.splitlines(keepends=True)
        self.assertEqual(lines[0].rstrip("\n"), observed["readiness"]["summary"])
        added = ("Successor claim: ", "Decision ", "Derived governance adoption: ", "Worker tab: ", "PoD#")
        self.assertEqual(sum(line.startswith("Worker tab: ") for line in lines), len(observed.get("worker_tabs", [])))
        self.assertEqual("".join(line for line in lines[1:] if not line.startswith(added)),
                         expected_text)
        for row in observed.get("owner_decisions", []):
            self.assertIn(row["decision"]["channel"], ("session", "relayed"))
        return observed

    def test_open_work_for_the_owner_and_a_peer(self):
        self.establish()
        self.assertEqual(self.compare("owner")["readiness"]["state"], "working")
        self.assertIn("owner_handoff", self.compare("next"))

    def test_a_lost_coordinator_and_its_handoff_fields(self):
        self.predecessor()
        observed = self.compare("next")
        self.assertEqual((observed["readiness"]["state"], observed["owner_handoff"]["status"]),
                         ("coordinator_lost", "handoff_available"))
        self.assertEqual(self.compare(None)["owner_handoff"]["condition"], "caller_not_live")

    def test_a_closed_objective(self):
        self.prepare()
        self.closed(remaining_gates=["owner publishes the notes"])
        self.assertEqual(self.compare("owner")["readiness"]["state"], "closed_external")
        self.assertEqual(self.compare("next")["blocker"], "handoff_unavailable")

    def test_a_claimed_predecessor_and_its_successor(self):
        self.predecessor(); self.successor()
        self.claim(SuccessionCase.decide(self))
        self.assertEqual(self.compare("next")["readiness"]["state"], "continued")
        self.assertEqual(self.compare("next", "successor")["succession"]["state"], "claimed")

    def test_a_retired_objective(self):
        self.delivered()
        self.retire(self.decide())
        self.assertEqual(self.compare("next")["readiness"]["state"], "retired")


if __name__ == "__main__":
    unittest.main()
