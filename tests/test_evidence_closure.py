"""R144-R148: delivered objectives always close, by evidence when the coordinator is gone; no owner decision."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from io import StringIO
import json
import os
import re
import unittest
from unittest.mock import patch

from pod import cli, readiness
from pod.errors import PodError
from pod.evidence_closure import FILE, STATEMENT, close_by_evidence, prove
from pod.github import issue_intake, repository_context, repository_reads
from pod.governor import _read_journal, _record_path, _write_journal
from pod.installer import STATE_SCHEMAS
from pod.internal import run
from pod.ledger import objective_root, read
from pod.operations import OrcaPort
from tests.common import GroupedTable, assert_grouped_table
from tests.kernel_support import REQUEST, git
from tests.test_historical_retirement import RetirementCase
from tests.test_owner_handoff import SENTINEL

LOCATOR = "https://github.com/acme/example/issues/82"
UNTICKED = "**Format:** Pod Execution Spec v1\n\n- [ ] **PoD#1 — widgets work**\n"
TICKED = UNTICKED.replace("- [ ]", "- [x]")
MERGED = {"state": "MERGED", "merged_at": "2026-10-19T00:00:00Z", "merge_commit": "a" * 40}
MUTATIONS = ("push", "pr_create", "dispatch", "rerun", "cancel")


def _older(schema: str) -> str:
    """The previous version of a current schema family, spelled from the current one."""
    family, number = schema.rsplit("/v", 1)
    return f"{family}/v{int(number) - 1}"


def _questions(value) -> list:
    """Every `question` key anywhere in an output: none may appear in an evidence-closure path (C8)."""
    if isinstance(value, dict):
        return [key for key in value if key == "question"] + [item for row in value.values() for item in _questions(row)]
    if isinstance(value, list):
        return [item for row in value for item in _questions(row)]
    return []


class ClosureCase(RetirementCase):
    """Production entry points over disposable Git, with only the Orca and GitHub ports replaced."""

    def setUp(self):
        super().setUp()
        # One repository identity read per checkout for the whole test, as each Pod command shares its own; a
        # check that binds identity before an effect still reads Git afresh.
        reads = repository_reads()
        reads.__enter__(); self.addCleanup(reads.__exit__, None, None, None)
        self.remote.issues[("acme/example", 82)] = {"body": UNTICKED, "state": "OPEN"}
        reader = patch("pod.github.GhPort", return_value=self.remote)
        reader.start(); self.addCleanup(reader.stop)
        # No path in this family reaches a process-backed remote.
        for target in ("pod.github._run", "pod.operations.mutate_command"):
            guard = patch(target, side_effect=AssertionError("test escaped its recording ports"))
            guard.start(); self.addCleanup(guard.stop)
        self.source = issue_intake(self.project, LOCATOR, port=self.remote)["source"]
        self.exits = {}
        self.holder_omitted = set()  # Runs whose run-show reply leaves out coordinator_handle entirely.

    def native_read(self, argv, **kwargs):
        if argv[:2] == ["terminal", "show"] and argv[3] in self.exits:
            self.native_calls.append(list(argv))
            return {"runtime": self.port.runtime, "result": {"terminal": {
                "handle": argv[3], "connected": False, "writable": False, "orphaned": False,
                "exitCause": {"kind": self.exits[argv[3]]}}}}
        answer = super().native_read(argv, **kwargs)
        if argv[:2] == ["orchestration", "run-show"] and argv[3] in self.holder_omitted:
            answer["result"]["run"].pop("coordinator_handle", None)
        return answer

    def packet(self, serves, **fields):
        return super().packet(serves, objective_source=self.source, **fields)

    def prepare(self):
        self.write([self.criterion()], governance={"base_ref": "refs/remotes/origin/target"},
                   objective_source=self.source)
        candidate = self.op("governor-prepare", unit="default",
                            branch={"remote": "origin", "branch": "delivery", "base": "target"})["candidate"]
        self.authorization = {
            "schema": "pod-authorization/v1", "candidate": candidate["commit"], "tree": candidate["tree"],
            "scope": ["publish"], "authorized_by": "owner", "utc": "2026-01-01T00:00:00Z",
            "reference": "fixture-authorization"}
        return candidate

    def shipped(self, *, pr=True, merged=True, ticked=True, settle=True, unknown_start=True):
        """A delivered objective: a pull request opened through Pod's own pr_update, then the coordinator lost."""
        self.establish()
        if unknown_start:
            lost = PodError("native_effect_uncertain", SENTINEL)
            lost.native_reference = {"runtime": self.port.runtime, "request_uuid": REQUEST, "_request_conflict": False}
            with patch.object(OrcaPort, "start_worker", side_effect=lost):
                with self.assertRaises(PodError):
                    self.start("task-2", self.packet(["S2"], boundary={"paths": ["area2"]}))
            self.unknown = next(key for key, row in self.state()["admissions"].items() if row["state"] == "unresolved")
            self.port.state = "absent"
        self.op("checkpoint", value=self.core(obligations=self.stored(), worktree=self.binding()))
        published = self.op("governor-execute", action=self.action(kind="pr_update" if pr else "push",
                                                                   effects=["workflow:ci.yml"]))
        self.publication = published["record_id"]
        if settle:
            self.settle(self.admission)
        self.lose()
        if pr:
            self.remote.pulls[7] = dict(MERGED) if merged else {"state": "OPEN"}
        self.remote.issues[("acme/example", 82)]["body"] = TICKED if ticked else UNTICKED

    def files(self, objective="objective"):
        root = objective_root(self.project, objective)
        return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in ("context.json", "governor.json") if (root / name).exists()}

    def closure(self, objective="objective"):
        path = objective_root(self.project, objective) / FILE
        return json.loads(path.read_text()) if path.exists() else None

    def sweep(self, *args, project=None, text=False):
        out = StringIO()
        old = os.getcwd()
        try:
            os.chdir(project or self.project)
            with redirect_stdout(out):
                code = cli.main(["sweep", *args] + ([] if text else ["--json"]))
        finally:
            os.chdir(old)
        return (code, out.getvalue()) if text else (code, json.loads(out.getvalue()))

    def swept(self, result, objective="objective"):
        return next(row for repository in result["repositories"] for row in repository["objectives"]
                    if row["objective"] == objective)

    def assert_closed_by_evidence(self, objective="objective", *, before=None):
        """Status and list read closed by evidence, coordinator gone; nothing else changed (C4, C8)."""
        shown = self.status(objective=objective)
        self.assertEqual((shown["status"], shown["blocker"], shown["phase"], shown["closure_basis"]),
                         ("observed", "objective_closed", "closed", "evidence"))
        state = shown["readiness"]
        self.assertEqual((state["state"], state["acts_next"], state["coordinator_needed"], state["reason"]["basis"]),
                         ("closed", "none", False, "evidence"))
        self.assertTrue(state["summary"].startswith("Closed by evidence, coordinator gone: "), state["summary"])
        block = shown["evidence_closure"]
        self.assertEqual((block["schema"], block["objective"], block["basis"], block["at"] is not None,
                          sorted(block["counts"])), ("pod-evidence-closure/v1", objective, "evidence", True,
                                                     ["failed", "reconciled", "unknown", "unreported"]))
        self.assertEqual((shown["evidence_closure"]["statement"], shown["retained_terminals"],
                          shown["assignments"]["active"], shown["assignments"]["unverified"]),
                         (STATEMENT, [], [], []))
        self.assertEqual(_questions(shown), [])
        row = self.listed()[objective]
        self.assertEqual((row["phase"], row["closure_basis"], row["readiness"], row["evidence_closure"]["statement"]),
                         ("closed", "evidence", "closed", STATEMENT))
        if before is not None:
            self.assertEqual(self.files(objective), before)
        return shown


    def rewrite(self, objective="objective", **fields):
        """Edit a stored record's raw JSON, as another Pod version or a damaged write would have left it."""
        path = objective_root(self.project, objective) / "context.json"
        raw = json.loads(path.read_text())
        for key, value in fields.items():
            if callable(value):
                value(raw)
            else:
                raw[key] = value
        path.write_text(json.dumps(raw))
        return raw

    def other(self, name, owner, *, run_id, pr=None, source=True):
        """Another objective of this repository on its own coordinator, recorded through checkpoint; `pr` adds a
        delivery pull request to its journal as an executed pr_update leaves it."""
        os.environ["ORCA_TERMINAL_HANDLE"] = owner
        self.current = {"id": run_id, "coordinator_handle": owner, "consumer_generation": 2}
        self.terminals[owner] = "live"
        run("checkpoint", {"project": str(self.project), "objective": name, "owner": owner,
                           "value": self.core(obligations=[self.criterion()],
                                              governance={"base_ref": "refs/remotes/origin/target"},
                                              worktree=self.binding(),
                                              **({"objective_source": self.source} if source else {}))})
        if pr is not None:
            self.record_pr(name, pr)
        os.environ["ORCA_TERMINAL_HANDLE"] = "next"

    def record_pr(self, name, pr):
        """A delivery pull request in an objective's journal, as its own executed pr_update leaves it."""
        path = _record_path(self.project, name)
        journal = _read_journal(path)
        journal["actions"].append({
            "record_id": f"{name}-pr", "logical_key": f"{name}-pr", "attempt": 1,
            "action": {"kind": "pr_update", "candidate": self.candidate, "target": "origin/delivery",
                       "unit": "default"},
            "decision": "ALLOW", "outcome": "PASS",
            "receipt": {"started_at": "2026-10-01T00:00:00+00:00", "provider": {"pr": pr}},
            "warnings": [], "reasons": [], "classifications": [], "run_event": None, "rejected_run": None})
        _write_journal(path, journal)

    def recovery(self, *, ended=None, live=True, pr=None):
        """A recovery coordinator rebound the objective's Run (at another generation) for its own objective, which
        shares the Run and the source issue; `ended` closes it or puts it under historical retirement."""
        self.other("recovery", "recovery", run_id="run", pr=pr)
        self.run_holders["run"] = "recovery"
        self.generations["run"] = 2
        self.terminals["recovery"] = "live" if live else "stale"
        if ended == "closed":
            def close(raw):
                checkpoint = raw["checkpoint"]
                checkpoint["closure"] = {"seq": checkpoint["seq"], "revision": checkpoint["revision"], "report": {}}
            self.rewrite("recovery", closure=close)
        elif ended == "retired":
            from tests.test_historical_retirement import RecordShapeTests
            self.rewrite("recovery", historical_retirement=deepcopy(RecordShapeTests.ENTRY))

    def successor_bound(self):
        """A recorded, not yet started successor bound to the same issue, then its claim of the objective."""
        from tests.test_succession import SuccessionCase
        os.environ["ORCA_TERMINAL_HANDLE"] = "next"
        self.current = {"id": "run-s", "coordinator_handle": "next", "consumer_generation": 1}
        run("checkpoint", {"project": str(self.project), "objective": "successor", "owner": "next",
                           "value": self.core(obligations=[self.criterion()],
                                              governance={"base_ref": "refs/remotes/origin/target"},
                                              worktree=self.binding(), objective_source=self.source)})
        claimed = self.claim(SuccessionCase.decide(self))
        self.assertEqual(claimed["status"], "claimed", claimed)

    def claimed_direct(self):
        """A claim-only successor with no issue binding of its own: a direct objective claims the delivered one and
        satisfies its own criterion with evidence. It delivers nothing new, so it records no delivery of its own."""
        from tests.test_succession import SuccessionCase
        self.successor()
        self.assertEqual(self.claim(SuccessionCase.decide(self))["status"], "claimed")
        rows = deepcopy(read(self.project, "successor")["checkpoint"]["obligations"])
        for row in rows:
            row.pop("executor", None)
            row.update(state="satisfied", evidence=[self.proof(row["id"])])
        run("checkpoint", {"project": str(self.project), "objective": "successor", "owner": "next",
                           "value": self.core(obligations=rows)})
        return rows

    def claimed_chain(self, count, *, tail_pr=None):
        """`count` supported succession claims, each objective on its own Run claiming the one before, each
        coordinator then lost; `tail_pr` is a pull request the tail records after its claim. Returns its name."""
        previous = "objective"
        for index in range(1, count + 1):
            name, owner, run_id = f"chain-{index}", f"owner-{index}", f"run-{index}"
            self.other(name, owner, run_id=run_id)
            self.run_holders[run_id] = owner
            os.environ["ORCA_TERMINAL_HANDLE"] = owner
            request = {"project": str(self.project), "objective": name, "owner": owner, "predecessor": previous}
            facts = run("succession", request)["facts"]
            claimed = run("succession", {**request, "decision": {
                "provenance": "user_direct", "instruction": "Continue the delivered predecessor", **facts}})
            self.assertEqual(claimed["status"], "claimed", claimed)
            self.terminals[owner] = "stale"
            previous = name
        if tail_pr is not None:
            self.record_pr(previous, tail_pr)
        os.environ["ORCA_TERMINAL_HANDLE"] = "next"
        return previous

    def finished(self, count):
        """Synthetic record-only controls: `count` objectives an earlier coordinator closed, each sorting before the
        delivered objective, so the presentation list's first rows are all of them. They add no work."""
        seed = json.loads((objective_root(self.project, "objective") / "context.json").read_text())
        for index in range(count):
            raw = deepcopy(seed)
            raw["admissions"] = {}
            checkpoint = raw["checkpoint"]
            checkpoint.update(objective=f"aaa-{index:03d}", objective_source=None,
                              native_refs=[{"runId": "finished-run", "runtime": "runtime"}],
                              closure={"seq": checkpoint["seq"], "revision": checkpoint["revision"], "report": {}})
            root = objective_root(self.project, checkpoint["objective"])
            root.mkdir()
            (root / "context.json").write_text(json.dumps(raw))

    def published_twice(self, *, reconcile_head=True):
        """A live coordinator's delivery (C11): the earlier head C0 and then the merged head C1 published through
        pr_update, each leaving a derived ci.yml validation; PR 7 merged at C1 carrying both; PoD#1 ticked by Pod."""
        self.prepare()
        earlier = self.candidate
        self.op("governor-execute", action=self.action(kind="pr_update", effects=["workflow:ci.yml"]))
        head = self.move()
        self.op("checkpoint", value=self.core(obligations=self.stored()))
        prepared = self.op("governor-prepare", unit="default",
                           branch={"remote": "origin", "branch": "delivery", "base": "target"})["candidate"]
        self.authorization = {**self.authorization, "candidate": prepared["commit"], "tree": prepared["tree"]}
        self.op("governor-execute", action=self.action(kind="pr_update", effects=["workflow:ci.yml"]))
        derived = {row["commit"]: row["record_id"] for row in _read_journal(_record_path(self.project, "objective"))[
            "actions"] if row.get("derived_from")}
        if reconcile_head:
            self.ci(head, "success")
            self.assertEqual(self.op("governor-reconcile", record_id=derived[head])["status"], "PASS")
        self.remote.pulls[7] = {**MERGED, "head": head, "commits": [earlier, head]}
        rows = self.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[self.proof("O1")])
        self.op("checkpoint", value=self.core(obligations=rows))
        self.assertEqual(self.op("pod-tick", pod=1)["status"], "confirmed")
        return derived[earlier], derived[head]

    def ci(self, commit, conclusion):
        later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        ran = self.remote._start("ci.yml", commit, event="pull_request", created_at=later)
        self.remote.complete(ran["id"], conclusion)

    def close_live(self):
        """The live coordinator's own closing checkpoint, answered as the CLI acknowledges it."""
        from pod.internal import acknowledge
        request = {"project": str(self.project), "objective": "objective", "owner": "owner",
                   "value": self.core(obligations=self.stored(), close=True)}
        return acknowledge("checkpoint", request, run("checkpoint", request))

    def held(self, *, reason=None, reconcile=None):
        """The closing write refuses and writes nothing; returns the refusal."""
        before = self.everything()
        error = self.refused(self.close_live, code="obligation_unaccounted")
        self.assertEqual(self.everything(), before)
        if reason is not None:
            self.assertIn(reason, [row["reason"] for row in error.detail["closure_preservation"]["held"]])
        if reconcile is not None:
            self.assertEqual(error.detail["next_action"], f"run governor-reconcile for record {reconcile}, then write "
                                                          "the closing checkpoint again")
        return error

    def everything(self):
        root = self.root / "state"
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
                if path.is_file() and path.name != ".lock"}


class DeliveryClosesTests(unittest.TestCase):
    """PoD#1 (R144): a delivered objective names its own coordinator until closure, and closing needs no owner."""

    def setUp(self):
        reads = repository_reads()
        reads.__enter__(); self.addCleanup(reads.__exit__, None, None, None)

    def merged(self):
        """A coordinator's own merge and verified delivery record, through Pod's Governor and checkpoint."""
        from tests.test_finalization import ResumeTests
        case = ResumeTests("test_a_closed_objective_stays_finished_after_its_target_advances")
        case.setUp()
        self.addCleanup(case.doCleanups)
        decided = case.op("governor", action=case.merge())
        git(case.project, "checkout", "-q", "target")
        git(case.project, "merge", "--no-ff", "-qm", "merge", "candidate")
        result = git(case.project, "rev-parse", "HEAD")
        git(case.project, "update-ref", "refs/remotes/origin/target", result)
        case.op("governor-outcome", record_id=decided["record_id"], outcome="PASS", provider={"merge_commit": result})
        case.checkpoint(delivery={"record": decided["record_id"]})
        rows = case.stored()
        rows[0].pop("executor")
        rows[0].update(state="satisfied", evidence=[case.proof("O1")])
        return case, rows

    @staticmethod
    def waiting(wait):
        quiescence = {"state": "quiescent", "blocked": [{"obligation": "S1", "via": "S1", **wait}]}
        delivery = {"seq": 2, "record": "m" * 64, "target_ref": "refs/remotes/origin/target", "base": "b" * 40,
                    "result": "r" * 40, "candidate": "c" * 40, "tree": "t" * 40, "method": "merge",
                    "authorization_reference": "consent", "provider": {}}
        return {"checkpoint": {"objective": "o", "obligations": [{"id": "O1", "state": "satisfied"},
                                                                 {"id": "S1", "state": "waiting"}],
                               "quiescence": quiescence, "delivery": delivery}, "admissions": {}}

    def test_delivered_not_closed_names_the_coordinator_even_with_an_owner_or_user_wait(self):
        for wait, party in (({"class": "authority", "referent": "release consent"}, "owner"),
                            ({"class": "user_hold", "referent": "a pause"}, "user"),
                            ({"party": "owner", "need": "choose a channel"}, "owner")):
            with self.subTest(wait=wait):
                block = readiness.base(self.waiting(wait), {"actions": []}, outstanding=set())
                self.assertEqual(block["waiting_on"][0]["party"], party)  # The wait is still shown,
                self.assertEqual((block["state"], block["acts_next"], block["coordinator_needed"],
                                  block["finalization"]["remaining"]),
                                 ("finalizing", "coordinator", True, ["closure"]))  # yet the coordinator acts.
                summary = readiness._summary(block)
                self.assertTrue(summary.startswith("Delivered, not closed:"), summary)
                self.assertIn("no owner decision is needed", summary)
                self.assertIn("not safe to close the coordinator", summary)
                self.assertNotIn("may close", summary)
        case, _ = self.merged()
        state = read(case.project, "objective")
        block = readiness.base(state, _read_journal(_record_path(case.project, "objective")), outstanding=set())
        self.assertEqual((block["state"], block["acts_next"], block["coordinator_needed"]),
                         ("finalizing", "coordinator", True))
        self.assertTrue(readiness._summary(block).startswith("Delivered, not closed:"))

    def test_the_closing_checkpoint_after_its_merge_needs_no_owner_decision(self):
        case, rows = self.merged()
        root = objective_root(case.project, "objective")
        bound = read(case.project, "objective")["checkpoint"]["governance"]
        # The target moves past the verified delivery and changes policy that the delivery never saw.
        (case.project / "CLAUDE.md").write_text("# New rule after delivery\n")
        git(case.project, "add", "CLAUDE.md")
        git(case.project, "commit", "-qm", "later policy")
        git(case.project, "update-ref", "refs/remotes/origin/target", git(case.project, "rev-parse", "HEAD"))
        # The exemption is narrow: an ordinary write still asks for the owner's snapshot decision.
        before = (root / "context.json").read_bytes()
        refused = case.refusal("checkpoint", value=case.core(obligations=case.stored()))
        self.assertEqual(refused.code, "governance_changed")
        self.assertEqual((root / "context.json").read_bytes(), before)
        closed = case.op("checkpoint", value=case.core(obligations=rows, close=True))
        self.assertTrue(closed["checkpoint"]["closure"])
        self.assertEqual(closed["finalization"]["remaining"], [])
        adoption = closed["governance_adoption"]
        self.assertEqual((adoption["adopted"], adoption["base"], adoption["target"]),
                         (False, bound["base"], git(case.project, "rev-parse", "HEAD")))
        stored = read(case.project, "objective")["checkpoint"]
        self.assertEqual(stored["governance"], bound)  # Closure keeps the trusted baseline and adopts nothing.
        self.assertEqual([row for row in stored["governance_history"]["decisions"]
                          if row.get("provenance") == "derived" or row.get("kind") == "snapshot"], [])
        self.assertNotIn("question", json.dumps(closed))

    def test_a_superseded_earlier_head_validation_is_preserved_at_closure(self):
        case = _live_case(self)
        earlier, head = case.published_twice()
        state = case.status()["readiness"]
        self.assertEqual((state["state"], state["acts_next"]), ("finalizing", "coordinator"))
        self.assertTrue(state["summary"].startswith("Delivered, not closed: settle each of the 1 publication-derived"))
        self.assertNotIn("superseded_by_merged_head", state["summary"])  # Nothing is claimed before the check.
        journal = _record_path(case.project, "objective").read_bytes()
        closed = case.close_live()
        row = {"record": earlier, "kind": "workflow_dispatch", "recorded": "pending", "outcome": "UNKNOWN",
               "reason": "superseded_by_merged_head", "pr": 7, "merged_head": case.candidate}
        self.assertEqual((closed["closure_preserved"], closed["finalization"]["preserved_at_closure"],
                          closed["finalization"]["finished"]), ([row], [row], True))
        self.assertEqual(_record_path(case.project, "objective").read_bytes(), journal)  # Nothing is stamped.
        self.assertEqual({entry["record_id"]: entry["outcome"] for entry in _read_journal(
            _record_path(case.project, "objective"))["actions"]}[earlier], "pending")
        shown = case.status()
        self.assertEqual((shown["readiness"]["state"], shown["closure_preserved"]), ("closed", [row]))
        from pod.succession import _closure_recorded
        self.assertTrue(_closure_recorded(case.project, "objective", read(case.project, "objective")))
        self.assertFalse([call for call in case.remote.calls if call[0] in MUTATIONS[2:]])
        self.assertEqual(_questions(closed), [])

    def test_the_merged_head_unreadable_commits_and_unproved_delivery_still_hold_the_closure(self):
        case = _live_case(self)
        earlier, head = case.published_twice(reconcile_head=False)
        case.held(reason="the merged delivery head itself")  # Its validation decides the merged result.
        case.ci(case.candidate, "success")
        self.assertEqual(case.op("governor-reconcile", record_id=head)["status"], "PASS")
        pull = case.remote.pulls[7]
        commits = pull.pop("commits")
        error = case.held(reason="a merged delivery pull request's commits could not be read completely")
        self.assertEqual([row["record"] for row in error.detail["closure_preservation"]["held"]], [earlier])
        pull.update(commits=commits, truncated=True)
        case.held(reason="a merged delivery pull request's commits could not be read completely")
        pull.update(truncated=False, state="OPEN")
        error = case.held()
        self.assertEqual((error.detail["closure_preservation"]["proof"]["status"],
                          error.detail["closure_preservation"]["proof"]["conditions"][0]["code"]),
                         ("not_proved", "pr_not_merged"))

    def test_a_settling_readback_and_other_unsettled_kinds_still_hold_the_closure(self):
        case = _live_case(self)
        earlier, _ = case.published_twice()
        case.ci(case.remote.pulls[7]["commits"][0], "failure")
        case.held(reconcile=earlier)  # Pod's readback settles it now: reconcile first.
        case.remote.runs_by.clear()
        path = _record_path(case.project, "objective")
        recorded = path.read_bytes()
        journal = _read_journal(path)
        base = next(row for row in journal["actions"] if row["record_id"] == earlier)
        for kind, fields in (("workflow_dispatch", {"derived_from": None}),  # A requested dispatch, zero runs.
                             ("release", {"derived_from": None})):
            with self.subTest(kind=kind):
                extra = {**deepcopy(base), **fields, "record_id": kind[0] * 64, "logical_key": kind[0] * 64,
                         "action": {**base["action"], "kind": kind}}
                _write_journal(path, {**deepcopy(journal), "actions": journal["actions"] + [extra]})
                error = case.held()
                self.assertNotIn("closure_preservation", error.detail)  # Today's rule, with no provider read.
                path.write_bytes(recorded)
        self.assertEqual(case.close_live()["closure_preserved"][0]["record"], earlier)

    def test_a_live_claim_only_successor_closes_on_its_chains_source(self):
        """F1 at the closing write (C11): a successor with no issue binding of its own is delivered by the issue its
        claimed predecessor binds, read at that write; that source still unticked and open still holds it."""
        from pod.internal import acknowledge
        case = _live_case(self)
        case.shipped(unknown_start=False, ticked=False)
        rows = case.claimed_direct()
        path = _record_path(case.project, "successor")
        carried = next(row for row in _read_journal(path)["actions"] if row.get("carried_from"))
        self.assertEqual((carried["action"]["kind"], carried["outcome"], bool(carried["derived_from"])),
                         ("workflow_dispatch", "pending", True))
        # Pull request 7 merged at a later head that carries the commit the carried validation ran on.
        case.remote.pulls[7] = {**MERGED, "head": "f" * 40, "commits": [carried["commit"], "f" * 40]}
        request = {"project": str(case.project), "objective": "successor", "owner": "next",
                   "value": case.core(obligations=rows, close=True)}
        journal, before = path.read_bytes(), case.everything()
        error = case.refused(lambda: run("checkpoint", request), code="obligation_unaccounted")
        proof = error.detail["closure_preservation"]["proof"]
        self.assertEqual((proof["status"], [row["code"] for row in proof["conditions"]]),
                         ("not_proved", ["source_unproved"]))
        self.assertIn(f"Source issue {LOCATOR} of objective", proof["conditions"][0]["message"])
        self.assertEqual(case.everything(), before)
        case.remote.issues[("acme/example", 82)]["body"] = TICKED
        closed = acknowledge("checkpoint", request, run("checkpoint", request))
        self.assertEqual([(row["record"], row["reason"], row["pr"]) for row in closed["closure_preserved"]],
                         [(carried["record_id"], "superseded_by_merged_head", 7)])
        self.assertTrue(closed["finalization"]["finished"])
        self.assertEqual(path.read_bytes(), journal)  # Nothing is stamped.
        self.assertEqual(_questions(closed), [])


def _live_case(test):
    case = ClosureCase(); case.setUp()
    test.addCleanup(case.doCleanups)
    return case


def _tab_closed(case):
    case.exits["owner"] = "operator_close"


def _exited(case):
    case.exits["owner"] = "exited"


def _signaled(case):
    case.exits["owner"] = "signaled"


def _stale(case):
    case.terminals["owner"] = "stale"


def _gone(case):
    case.terminals["owner"] = "gone"


def _rebound(ended=None, live=True):
    def arrange(case):
        case.recovery(ended=ended, live=live)
    return arrange


def _older_schema(case):
    older = _older(STATE_SCHEMAS["context"])
    case.rewrite(schema=older)
    # Pod reads an older record only to report it: R90 refuses it, and nothing converts it.
    case.assertEqual(case.status()["blocker"], "state_unsupported")


# kind: (arrange after delivery, closes, condition when it does not)
LOSSES = {
    "tab_closed": (_tab_closed, True, None),
    "exited": (_exited, True, None),
    "signaled": (_signaled, True, None),
    "restart_stale": (_stale, True, None),
    "restart_gone": (_gone, True, None),
    "rebound_recovery_closed": (_rebound("closed"), True, None),
    "rebound_recovery_retired": (_rebound("retired"), True, None),
    "rebound_recovery_live_open": (_rebound(), False, "run_held"),
    "older_schema": (_older_schema, True, None),
}


class LostCoordinatorTests(GroupedTable):
    """PoD#2 (R145, R146): a lost coordinator's delivered objective closes by evidence, with no decision."""
    GROUPS = {"test_a_closed_tab": ["tab_closed"], "test_an_exited_coordinator": ["exited"],
              "test_a_signaled_coordinator": ["signaled"], "test_a_restart_with_a_stale_handle": ["restart_stale"],
              "test_a_restart_with_a_gone_handle": ["restart_gone"],
              "test_a_run_rebound_to_a_closed_recovery": ["rebound_recovery_closed"],
              "test_a_run_rebound_to_a_recovery_under_retirement": ["rebound_recovery_retired"],
              "test_a_run_held_by_a_live_open_recovery": ["rebound_recovery_live_open"],
              "test_an_older_schema_record": ["older_schema"]}

    def rows(self):
        assert_grouped_table(self, type(self), LOSSES, limit=1)
        super().rows()

    def row(self, kind):
        arrange, closes, condition = LOSSES[kind]
        case = ClosureCase(); case.setUp()
        try:
            case.shipped(unknown_start=False)
            arrange(case)
            before = case.files()
            code, result = case.sweep()
            self.assertEqual(code, 0)
            verdict = case.swept(result)
            self.assertEqual(_questions(result), [])
            if not closes:
                self.assertEqual((verdict["outcome"], verdict["party"], verdict["conditions"][0]["code"]),
                                 ("not_proved", "lead", condition), verdict)
                self.assertIsNone(case.closure())
                shown = case.status()
                self.assertEqual((shown["readiness"]["state"], shown["readiness"]["acts_next"]),
                                 ("coordinator_lost", "lead"))
                self.assertEqual(case.files(), before)
                return
            self.assertEqual(verdict["outcome"], "closed", verdict)
            stored = case.closure()
            self.assertEqual((stored["statement"], stored["relation"], stored["delivery"][0]["pr"],
                              stored["source"]["proof"]), (STATEMENT, "tail", 7, "pod_ticks"))
            self.assertIn(stored["coordinator"]["liveness"], ("lost", "exited"))
            if kind.startswith("rebound"):
                held = next(row for row in stored["runs"] if row["run"] == "run")
                self.assertEqual((held["holder"], held["generation"]), ("recovery", 2))
            self.assertEqual(stored["record"]["class"], "older" if kind == "older_schema" else "current")
            shown = case.assert_closed_by_evidence(before=before)
            self.assertEqual(shown["checkpoint_join"]["native_refs"][0]["runId"], "run")
            if kind == "older_schema":
                self.assertEqual(case.listed()["objective"]["schema"], "superseded")
        finally:
            case.doCleanups()

    def test_a_closed_tab(self): self.rows()
    def test_an_exited_coordinator(self): self.rows()
    def test_a_signaled_coordinator(self): self.rows()
    def test_a_restart_with_a_stale_handle(self): self.rows()
    def test_a_restart_with_a_gone_handle(self): self.rows()
    def test_a_run_rebound_to_a_closed_recovery(self): self.rows()
    def test_a_run_rebound_to_a_recovery_under_retirement(self): self.rows()
    def test_a_run_held_by_a_live_open_recovery(self): self.rows()
    def test_an_older_schema_record(self): self.rows()


class UndeliveredTests(ClosureCase):
    """R145: undelivered or live work is never closed; it stays open as a task for the project lead."""

    def stays_open(self, condition, *, status="not_proved", lost=True):
        before = self.everything()
        code, result = self.sweep()
        verdict = self.swept(result)
        self.assertEqual(code, 0)
        if status == "skipped":
            self.assertEqual((verdict["outcome"], verdict["conditions"][0]["code"]), ("skipped", condition), verdict)
        else:
            self.assertEqual((verdict["outcome"], verdict["party"]), (status, "lead"), verdict)
            self.assertIn(condition, [row["code"] for row in verdict["conditions"]])
        self.assertIsNone(self.closure())
        self.assertEqual(self.everything(), before)
        self.assertEqual(_questions(result), [])
        if lost:
            shown = self.status()
            state = shown["readiness"]
            self.assertEqual((state["state"], state["acts_next"]), ("coordinator_lost", "lead"))
            route = state["recovery"]["routes"][0]
            self.assertEqual((route["route"], route["status"]), ("evidence_closure", "blocked" if status ==
                                                                 "not_proved" else "unverified"))
            self.assertTrue(state["summary"].startswith("Coordinator lost; delivery not proved ("), state["summary"])
            self.assertIn("a task for the project lead", state["summary"])
            self.assertEqual(_questions({key: value for key, value in shown.items() if key != "owner_handoff"}), [])
            # The lead's task, never an owner question: status's own next action never asks the owner (R145,
            # PES R2); the owner_handoff block keeps its R105 facts as an optional route.
            for action in (shown["next_safe_action"], shown["next_action"]):
                self.assertNotIn("Ask the Owner", action)
            self.assertTrue(shown["next_safe_action"].startswith("Coordinator lost; delivery not proved ("),
                            shown["next_safe_action"])
            self.assertIn("a task for the project lead", shown["next_safe_action"])
            self.assertEqual(self.everything(), before)
        return verdict

    def test_unticked_pods_and_an_open_issue_stay_open_until_the_issue_closes_completed(self):
        self.shipped(ticked=False)
        self.stays_open("source_unproved")
        self.remote.issues[("acme/example", 82)].update(state="CLOSED", state_reason="NOT_PLANNED")
        self.stays_open("source_unproved")
        self.remote.issues[("acme/example", 82)].update(state_reason="COMPLETED")
        self.assertEqual(self.swept(self.sweep()[1])["outcome"], "closed")
        self.assertEqual(self.closure()["source"]["proof"], "issue_completed")

    def test_a_delivery_pull_request_that_is_not_merged_stays_open(self):
        self.shipped(merged=False)
        self.stays_open("pr_not_merged")
        self.remote.unreadable.add("pr")
        self.stays_open("pr_unreadable", status="unverified")

    def test_no_recorded_delivery_pull_request_stays_open(self):
        self.shipped(pr=False)
        self.stays_open("delivery_unrecorded")

    def test_a_live_dispatch_stays_open_and_unreadable_workers_read_unverified(self):
        self.shipped(settle=False)
        self.stays_open("work_live")
        self.worker_list_failure = True
        self.stays_open("workers_unverified", status="unverified")

    def test_a_run_reply_that_omits_its_coordinator_reads_unverified(self):
        """F2: only an explicit null proves a Run unbound; a reply that leaves the field out proves nothing."""
        self.shipped()
        self.holder_omitted.add("run")
        verdict = self.stays_open("workers_unverified", status="unverified")
        self.assertEqual([row["code"] for row in verdict["conditions"]], ["workers_unverified"])
        self.assertEqual(prove(self.project, "objective")["facts"]["runs"][0]["holder_known"], False)
        # Positive control: Orca names no coordinator explicitly, so nobody holds the Run.
        self.holder_omitted.clear()
        self.run_holders["run"] = None
        self.assertEqual(self.swept(self.sweep()[1])["outcome"], "closed")
        self.assertEqual([(row["run"], row["holder"]) for row in self.closure()["runs"]], [("run", None)])

    def test_a_start_request_orca_still_holds_pending_stays_open(self):
        self.shipped()
        self.port.state = "pending"
        self.stays_open("work_live")

    def test_a_live_coordinator_is_never_closed_by_evidence(self):
        self.shipped()
        self.terminals["owner"] = "live"
        self.stays_open("coordinator_live", status="skipped", lost=False)
        # The live coordinator closes it itself: readiness names it, never the lead or the owner.
        self.assertEqual(self.status()["readiness"]["acts_next"], "coordinator")

    def test_an_unknown_schema_is_never_closed(self):
        self.shipped()
        family, number = STATE_SCHEMAS["context"].rsplit("/v", 1)
        self.rewrite(schema=f"{family}/v{int(number) + 1}")
        self.stays_open("schema_unknown", status="skipped", lost=False)
        self.assertEqual(self.listed()["objective"]["schema"], "unsupported")


class PreservedResultsTests(ClosureCase):
    """R145: pending results are read back where the provider answers; the rest stay UNKNOWN, never stamped."""

    def test_pending_rows_are_read_back_and_the_rest_preserved_without_writing(self):
        self.shipped()
        path = _record_path(self.project, "objective")
        journal = _read_journal(path)
        derived = next(row for row in journal["actions"] if row.get("derived_from") == self.publication)
        later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        ran = self.remote._start("ci.yml", derived["commit"], event="pull_request", created_at=later)
        self.remote.complete(ran["id"], "success")

        def pending(record_id, kind, **fields):
            return {"record_id": record_id, "logical_key": record_id, "attempt": 1,
                    "action": {"kind": kind, "candidate": self.candidate,
                               "target": "origin/delivery" if kind == "push" else "v1", "unit": "default"},
                    "decision": "ALLOW", "outcome": "pending", "receipt": {"started_at": later},
                    "warnings": [], "reasons": [], "classifications": [], "run_event": None, "rejected_run": None,
                    **fields}
        journal["actions"] += [pending("p" * 64, "push", commit="b" * 40),  # The branch never moved: FAILED.
                               pending("r" * 64, "release"),  # Performed by the project: no readback.
                               {**pending("f" * 64, "release"), "outcome": "FAILED"}]
        _write_journal(path, journal)
        before, recorded = self.files(), path.read_bytes()
        self.assertEqual(self.swept(self.sweep()[1])["outcome"], "closed")
        rows = {row["id"]: row for row in self.closure()["preserved"] if row["kind"] == "governor"}
        self.assertEqual((rows[derived["record_id"]]["outcome"], rows[derived["record_id"]]["reconciled"]),
                         ("PASS", True))
        self.assertEqual((rows["p" * 64]["outcome"], rows["p" * 64]["reconciled"]), ("FAILED", True))
        self.assertIn("not_pushed", rows["p" * 64]["evidence"])
        self.assertEqual((rows["r" * 64]["outcome"], rows["r" * 64]["reconciled"]), ("UNKNOWN", False))
        self.assertIn("no readback", rows["r" * 64]["reason"])
        self.assertEqual((rows["f" * 64]["recorded"], rows["f" * 64]["outcome"]), ("FAILED", "FAILED"))
        self.assertEqual(self.closure()["counts"], {"reconciled": 2, "unknown": 2, "failed": 2, "unreported": 1})
        # Nothing is stamped: the journal still holds every result exactly as recorded.
        self.assertEqual((path.read_bytes(), self.files()), (recorded, before))
        self.assertEqual({row["record_id"]: row["outcome"] for row in _read_journal(path)["actions"]}[
            derived["record_id"]], "pending")
        self.assertFalse([call for call in self.remote.calls if call[0] in MUTATIONS[2:]])


class ChainTests(ClosureCase):
    """R146: chains and Run-sharing recovery objectives close together, each with its own entry."""

    def test_a_claimed_predecessor_and_its_claim_only_successor_close_together(self):
        self.shipped(unknown_start=False)
        self.successor_bound()
        self.terminals["next"] = "stale"
        frozen, own = self.files(), self.files("successor")
        code, result = self.sweep()
        # The predecessor's entry was written by this sweep, with its tail: it reads closed now, never replayed.
        self.assertEqual((self.swept(result, "successor")["outcome"], self.swept(result)["outcome"]),
                         ("closed", "closed"))
        self.assertEqual((self.swept(result, "successor")["party"], result["counts"]["closed"]), (None, 2))
        # The continued predecessor keeps reading continued, with its own entry beside its frozen record.
        shown = self.status()
        self.assertEqual((shown["readiness"]["state"], shown["evidence_closure"]["relation"],
                          shown["evidence_closure"]["schema"]), ("continued", "predecessor", "pod-evidence-closure/v1"))
        tail, entry = self.closure("successor"), self.closure()
        self.assertEqual((tail["relation"], [row["pr"] for row in tail["delivery"]]), ("tail", [7]))
        self.assertEqual(tail["delivery"][0]["recorded_by"], "objective")
        self.assertEqual((entry["relation"], entry["tail"]), ("predecessor", {"objective": "successor",
                                                                            "at": tail["at"]}))
        self.assertEqual((entry["record"]["class"], tail["record"]["class"]), ("current", "current"))
        self.assertEqual((self.files(), self.files("successor")), (frozen, own))  # The R110 freeze holds.
        listed = self.listed()
        self.assertEqual((listed["objective"]["phase"], listed["objective"]["succession_state"],
                          listed["objective"]["closure_basis"]), ("continued", "successor_closed", "evidence"))
        self.assertEqual((listed["successor"]["phase"], listed["successor"]["closure_basis"]), ("closed", "evidence"))
        self.assert_closed_by_evidence("successor")
        before = self.everything()
        again = self.sweep()[1]
        self.assertEqual({row["outcome"] for repository in again["repositories"] for row in repository["objectives"]},
                         {"already_closed"})
        self.assertEqual(self.everything(), before)

    def test_run_sharing_recovery_objectives_contribute_their_pull_requests(self):
        self.shipped(unknown_start=False)
        self.recovery(live=False, pr=8)
        self.remote.pulls[8] = {"state": "OPEN"}
        code, result = self.sweep()
        for name in ("objective", "recovery"):
            verdict = self.swept(result, name)
            self.assertEqual((verdict["outcome"], [row["code"] for row in verdict["conditions"]]),
                             ("not_proved", ["pr_not_merged"]), verdict)
        self.remote.pulls[8] = dict(MERGED)
        result = self.sweep()[1]
        self.assertEqual((self.swept(result)["outcome"], self.swept(result, "recovery")["outcome"]), ("closed", "closed"))
        for name in ("objective", "recovery"):
            stored = self.closure(name)
            self.assertEqual((stored["relation"], [row["pr"] for row in stored["delivery"]]), ("tail", [7, 8]))
        self.assertEqual({row["objective"]: row["relation"] for row in self.closure()["chain"]},
                         {"objective": "objective", "recovery": "recovery"})

    def test_a_claim_only_successor_without_its_own_issue_closes_on_its_chains_source(self):
        """F1: the claimed chain's issue proves a successor bound to none, and stays its proof: unticked and open, it
        holds both; a standalone direct objective keeps its recorded-criteria rule (SweepTests' lead task)."""
        self.shipped(unknown_start=False, ticked=False)
        self.claimed_direct()
        self.terminals["next"] = "stale"
        before = self.everything()
        verdict = self.swept(self.sweep()[1], "successor")
        self.assertEqual((verdict["outcome"], [row["code"] for row in verdict["conditions"]]),
                         ("not_proved", ["source_unproved"]), verdict)
        self.assertIn(f"Source issue {LOCATOR} of objective has 0 of 1 PoD line(s) ticked and is open",
                      verdict["conditions"][0]["message"])
        self.assertEqual(self.everything(), before)
        self.remote.issues[("acme/example", 82)]["body"] = TICKED
        result = self.sweep()[1]
        self.assertEqual((self.swept(result, "successor")["outcome"], self.swept(result)["outcome"]),
                         ("closed", "closed"))
        tail = self.closure("successor")
        self.assertEqual((tail["source"]["proof"], tail["source"]["bound_by"], tail["source"]["locator"],
                          [row["bound_by"] for row in tail["sources"]], [row["pr"] for row in tail["delivery"]]),
                         ("pod_ticks", "objective", LOCATOR, ["objective"], [7]))
        self.assertEqual((self.closure()["relation"], self.closure()["sources"]), ("predecessor", tail["sources"]))
        self.assert_closed_by_evidence("successor")

    def test_a_chain_within_eight_predecessors_needs_every_pull_request_and_gives_each_its_entry(self):
        """F3 control: at the bound the whole chain is read; its oldest pull request still decides."""
        self.shipped(unknown_start=False)
        self.remote.pulls[7] = {"state": "OPEN"}
        tail = self.claimed_chain(8)
        before = self.everything()
        verdict = self.swept(self.sweep()[1], tail)
        self.assertEqual((verdict["outcome"], [row["code"] for row in verdict["conditions"]]),
                         ("not_proved", ["pr_not_merged"]), verdict)
        self.assertEqual(self.everything(), before)
        self.remote.pulls[7] = dict(MERGED)
        result = self.sweep()[1]
        names = ["objective"] + [f"chain-{index}" for index in range(1, 8)]
        self.assertEqual({row["objective"]: row["outcome"] for row in result["repositories"][0]["objectives"]},
                         {tail: "closed", **{name: "closed" for name in names}})
        self.assertEqual([row["objective"] for row in self.closure(tail)["chain"]][1:], names[::-1])
        for name in names:
            self.assertEqual(self.closure(name)["tail"]["objective"], tail, name)

    def test_a_chain_past_eight_predecessors_or_with_a_broken_link_is_never_closed(self):
        """F3: a chain the walk cannot read whole refuses, never certifying the part it read."""
        self.shipped(unknown_start=False)
        self.remote.pulls[7] = {"state": "OPEN"}  # Only the predecessor past the bound records it,
        tail = self.claimed_chain(9, tail_pr=8)
        self.remote.pulls[8] = dict(MERGED)  # while the part of the chain within the bound reads delivered.
        before = self.everything()
        verdict = self.swept(self.sweep()[1], tail)
        self.assertEqual((verdict["outcome"], [row["code"] for row in verdict["conditions"]]),
                         ("not_proved", ["scope_too_large"]), verdict)
        self.assertEqual(close_by_evidence(self.project, tail, caller="sweep")["status"], "not_proved")
        self.assertEqual(self.everything(), before)
        # A link the other side does not confirm, and a loop, read unverified: nothing is written.
        cut = self.rewrite("chain-8", continued_by=lambda raw: raw["continued_by"].update(objective="elsewhere"))
        self.assertIn("does not name chain-9 as its successor", " ".join(
            row["message"] for row in prove(self.project, tail)["conditions"] if row["code"] == "chain_inconsistent"))
        self.rewrite("chain-8", continued_by=lambda raw: raw["continued_by"].update(objective=tail))
        self.rewrite("chain-7", continues=lambda raw: raw["continues"].update(objective=tail))
        looped = prove(self.project, tail)
        self.assertEqual((looped["status"], [row["code"] for row in looped["conditions"]]),
                         ("unverified", ["chain_inconsistent"]))
        self.assertIn("loops back to chain-9", looped["conditions"][0]["message"])
        self.assertEqual(close_by_evidence(self.project, tail, caller="sweep")["status"], "unverified")
        self.assertIsNone(self.closure(tail))
        self.assertEqual(cut["continued_by"]["objective"], "elsewhere")

    def test_a_chain_member_on_an_unknown_schema_is_never_closed_and_an_older_one_keeps_its_class(self):
        """F6: every member is classified before the tail's proof uses it or an entry is written for it."""
        self.shipped(unknown_start=False)
        self.successor_bound()
        self.terminals["next"] = "stale"
        family, number = STATE_SCHEMAS["context"].rsplit("/v", 1)
        self.rewrite(schema=f"{family}/v{int(number) + 1}")
        before = self.everything()
        verdict = self.swept(self.sweep()[1], "successor")
        self.assertEqual((verdict["outcome"], [row["code"] for row in verdict["conditions"]]),
                         ("unverified", ["schema_unknown"]), verdict)
        self.assertEqual(self.everything(), before)
        self.assertEqual((self.listed()["objective"]["schema"], self.listed()["objective"]["closure_basis"]),
                         ("unsupported", None))
        # A supported older predecessor closes with its tail, and its entry names the class it was read as.
        self.rewrite(schema=_older(STATE_SCHEMAS["context"]))
        frozen = self.files()
        result = self.sweep()[1]
        self.assertEqual((self.swept(result, "successor")["outcome"], self.swept(result)["outcome"]),
                         ("closed", "closed"))
        self.assertEqual((self.closure()["record"]["class"], self.closure("successor")["record"]["class"]),
                         ("older", "current"))
        self.assertEqual(self.files(), frozen)

    def recovery_of_the_predecessors_run(self, *, bound):
        """F8: a recovery objective on the predecessor's own Run and source records pull request 8, still open, and
        its coordinator is gone; then a successor on its own Run claims the predecessor, bound to the same issue or
        claim-only, and its coordinator is gone too."""
        self.shipped(unknown_start=False)
        self.recovery(live=False, pr=8)
        self.remote.pulls[8] = {"state": "OPEN"}
        control = prove(self.project, "objective")
        self.assertEqual((control["status"], [row["code"] for row in control["conditions"]]),
                         ("not_proved", ["pr_not_merged"]))
        if bound:
            self.successor_bound()
        else:
            self.claimed_direct()
        self.run_holders["run-s"] = "next"
        self.terminals["next"] = "stale"
        # Across the supported claim the same open pull request still holds the whole chain; nothing is written.
        before = self.everything()
        verdict = self.swept(self.sweep()[1], "successor")
        self.assertEqual((verdict["outcome"], [row["code"] for row in verdict["conditions"]]),
                         ("not_proved", ["pr_not_merged"]), verdict)
        self.assertIn("Pull request #8 is open", verdict["conditions"][0]["message"])
        self.assertEqual(close_by_evidence(self.project, "successor", caller="sweep")["status"], "not_proved")
        self.assertEqual(self.everything(), before)
        # Once it merges, the proof includes both pull requests, and the recovery closes on its own proof.
        self.remote.pulls[8] = dict(MERGED)
        result = self.sweep()[1]
        self.assertEqual({name: self.swept(result, name)["outcome"] for name in ("successor", "objective", "recovery")},
                         {"successor": "closed", "objective": "closed", "recovery": "closed"})
        tail = self.closure("successor")
        self.assertEqual([(row["pr"], row["recorded_by"]) for row in tail["delivery"]],
                         [(7, "objective"), (8, "recovery")])
        self.assertEqual({row["objective"]: row["relation"] for row in tail["chain"]},
                         {"successor": "objective", "objective": "predecessor", "recovery": "recovery"})
        self.assertEqual((self.closure()["relation"], [row["pr"] for row in self.closure()["delivery"]]),
                         ("predecessor", [7, 8]))
        self.assertEqual((self.closure("recovery")["relation"], [row["pr"] for row in self.closure("recovery")[
            "delivery"]]), ("tail", [7, 8]))
        self.assertEqual({name: data for name, data in self.everything().items() if name in before}, before)

    def test_a_recovery_of_a_predecessors_run_holds_a_bound_successors_chain_until_it_merges(self):
        self.recovery_of_the_predecessors_run(bound=True)

    def test_a_recovery_of_a_predecessors_run_holds_a_claim_only_successors_chain_until_it_merges(self):
        self.recovery_of_the_predecessors_run(bound=False)

    def test_a_live_recovery_on_a_predecessors_run_holds_its_chain_and_a_settled_one_closes_it(self):
        """F9: each record the closure writes an entry for has its own Runs and work proved fresh, and its entry
        records the facts read: a resumed live recovery coordinator of an open objective on the predecessor's Run
        holds the chain (`run_held`); once its own objective ended, the chain closes."""
        self.shipped(unknown_start=False)
        self.recovery(live=True)
        control = prove(self.project, "objective")
        self.assertEqual([row["code"] for row in control["conditions"]], ["run_held"])
        self.terminals["recovery"] = "stale"  # Stale long enough for a supported claim on another Run,
        self.claimed_direct()
        self.run_holders["run-s"] = "next"
        self.terminals["next"] = "stale"
        self.terminals["recovery"] = "live"  # then the recovery coordinator resumes, its own objective still open.
        before = self.everything()
        verdict = self.swept(self.sweep()[1], "successor")
        self.assertEqual((verdict["outcome"], [row["code"] for row in verdict["conditions"]]),
                         ("not_proved", ["run_held"]), verdict)
        self.assertTrue(verdict["conditions"][0]["message"].startswith("Predecessor objective objective: "),
                        verdict["conditions"][0]["message"])
        self.assertEqual(close_by_evidence(self.project, "successor", caller="sweep")["status"], "not_proved")
        self.assertEqual(self.everything(), before)
        self.assertIsNone(self.closure())
        # Positive control: the recovery's own objective is closed, so the Run settles with it.
        self.rewrite("recovery", closure=lambda raw: raw["checkpoint"].update(closure={
            "seq": raw["checkpoint"]["seq"], "revision": raw["checkpoint"]["revision"], "report": {}}))
        frozen = self.files()
        result = self.sweep()[1]
        self.assertEqual((self.swept(result, "successor")["outcome"], self.swept(result)["outcome"]),
                         ("closed", "closed"))
        entry = self.closure()
        self.assertEqual(entry["runs"], [{"run": "run", "generation": 2, "holder": "recovery",
                                          "rebound": "rebound_settled", "settled_with": ["recovery"]}])
        self.assertEqual(entry["dispatches_settled"], 1)
        self.assertEqual([(row["run"], row["holder"]) for row in self.closure("successor")["runs"]],
                         [("run-s", "next")])
        self.assertEqual(self.files(), frozen)

    def interrupted(self):
        """A tail closure interrupted after the tail's own entry was published and before its predecessor's."""
        from pod import evidence_closure
        self.shipped(unknown_start=False)
        self.successor_bound()
        self.terminals["next"] = "stale"
        create = evidence_closure._create

        def stop(root, content):
            if content["relation"] == "predecessor":
                raise OSError("interrupted before the predecessor entry")
            return create(root, content)
        with patch("pod.evidence_closure._create", side_effect=stop):
            with self.assertRaises(OSError):
                close_by_evidence(self.project, "successor", caller="sweep")
        self.assertIsNotNone(self.closure("successor"))
        self.assertIsNone(self.closure())
        return self.everything()

    def test_a_retry_after_an_interrupted_chain_closure_names_the_entries_it_writes(self):
        """F7: the retry writes only the missing predecessor entry and says so; a complete replay writes nothing."""
        before = self.interrupted()
        result = close_by_evidence(self.project, "successor", caller="sweep")
        self.assertEqual((result["status"], result["predecessors"]), ("chain_completed", ["objective"]))
        after = self.everything()
        self.assertEqual([name.rsplit("/", 1)[1] for name in set(after) - set(before)], [FILE])
        self.assertEqual({name: data for name, data in after.items() if name in before}, before)
        self.assertEqual(self.closure()["tail"]["objective"], "successor")
        again = close_by_evidence(self.project, "successor", caller="sweep")
        self.assertEqual((again["status"], "predecessors" in again), ("replayed", False))
        self.assertEqual(self.everything(), after)

    def test_a_retry_proves_an_owed_predecessor_entry_fresh_before_it_writes_it(self):
        """F9 on the retry: an owed entry's predecessor has its Runs and work proved fresh before it is written; a
        live recovery coordinator of an open objective on its Run holds it, and the entry records the facts read."""
        self.interrupted()
        self.other("recovery", "recovery", run_id="run")
        self.run_holders["run"] = "recovery"
        before = self.everything()
        held = close_by_evidence(self.project, "successor", caller="sweep")
        self.assertEqual((held["status"], [row["code"] for row in held.get("conditions", [])], "predecessors" in held),
                         ("not_proved", ["run_held"], False))
        self.assertEqual(held["closure"]["relation"], "tail")
        self.assertIn("Predecessor objective objective: ", held["conditions"][0]["message"])
        self.assertEqual(self.everything(), before)
        self.terminals["recovery"] = "gone"
        result = close_by_evidence(self.project, "successor", caller="sweep")
        self.assertEqual((result["status"], result["predecessors"]), ("chain_completed", ["objective"]))
        self.assertEqual((self.closure()["runs"], self.closure()["dispatches_settled"]),
                         ([{"run": "run", "generation": 1, "holder": "recovery"}], 1))
        self.assertEqual({name: data for name, data in self.everything().items() if name in before}, before)

    def test_an_older_predecessor_closed_with_its_chain_reads_continued_in_its_status(self):
        """F10: individual status reads an older evidence-closed predecessor through the documented-field path, as a
        current predecessor reads; an older record without its closure keeps its refusal."""
        self.shipped(unknown_start=False)
        self.successor_bound()
        self.terminals["next"] = "stale"
        self.rewrite(schema=_older(STATE_SCHEMAS["context"]))
        self.assertEqual(self.status()["blocker"], "state_unsupported")  # No closure yet: R90's refusal stays.
        frozen = self.files()
        result = self.sweep()[1]
        self.assertEqual((self.swept(result, "successor")["outcome"], self.swept(result)["outcome"]),
                         ("closed", "closed"))
        shown = self.status()
        self.assertEqual((shown["status"], shown["blocker"], shown.get("phase"), shown.get("record_class"),
                          "closure_basis" in shown), ("observed", "objective_continued", "continued", "older", False))
        state = shown["readiness"]
        self.assertEqual((state["state"], state["acts_next"], state["coordinator_needed"], state["reason"]),
                         ("continued", "successor", False, {"successor": "successor",
                                                            "succession_state": "successor_closed"}))
        self.assertEqual(state["summary"], "Continued by successor objective successor (successor closed); read that "
                                           "objective, as this record accepts no writes.")
        self.assertEqual((shown["succession"]["continued_by"], shown["succession"]["state"]),
                         ("successor", "successor_closed"))
        entry = shown["evidence_closure"]
        self.assertEqual((entry["status"], entry["relation"], entry["record"]["class"], entry["tail"]["objective"]),
                         ("recorded", "predecessor", "older", "successor"))
        self.assertEqual(shown["next_safe_action"], "Read the successor objective with pod status --objective "
                                                    "successor; this record accepts no writes.")
        self.assertEqual({row["settlement"] for row in shown["assignments"]["settled"] if "settlement" in row},
                         {"claim"})
        self.assertEqual(_questions(shown), [])
        text = self.text_status()
        self.assertIn("Continued by successor objective successor (successor closed)", text)
        self.assertIn("Closed with its chain tail successor", text)
        listed = self.listed()["objective"]
        self.assertEqual((listed["schema"], listed["phase"], listed["succession_state"]),
                         ("superseded", "continued", "successor_closed"))
        self.assertEqual(self.files(), frozen)

    def test_a_sweep_after_an_interrupted_chain_closure_reports_the_entry_it_wrote_as_closed(self):
        """F7 through the sweep: the tail it found closed is already_closed, the predecessor it closed now closed."""
        before = self.interrupted()
        code, result = self.sweep()
        self.assertEqual((code, self.swept(result, "successor")["outcome"], self.swept(result)["outcome"]),
                         (0, "already_closed", "closed"))
        rows = result["repositories"][0]["objectives"]
        self.assertEqual(result["counts"], {name: sum(row["outcome"] == name for row in rows)
                                            for name in result["counts"]})
        written = set(self.everything()) - set(before)
        self.assertEqual(len(written), 1)
        settled = self.everything()
        again = self.sweep()[1]
        self.assertEqual({row["outcome"] for row in again["repositories"][0]["objectives"]}, {"already_closed"})
        self.assertEqual(self.everything(), settled)


class SweepTests(ClosureCase):
    """R148: one idempotent sweep closes every provably delivered open objective, touching nothing else."""

    def second_repository(self):
        """A second repository with one delivered objective of its own."""
        other = self.root / "second"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        git(other, "config", "user.name", "Fixture")
        git(other, "config", "user.email", "fixture@example.invalid")
        (other / "README.md").write_text("second\n")
        git(other, "add", "."); git(other, "commit", "-qm", "base")
        git(other, "branch", "target")
        git(other, "remote", "add", "origin", "https://github.com/acme/other.git")
        git(other, "update-ref", "refs/remotes/origin/target", git(other, "rev-parse", "HEAD"))
        git(other, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/target")
        self.remote.issues[("acme/other", 5)] = {"body": TICKED, "state": "OPEN"}
        source = issue_intake(other, "https://github.com/acme/other/issues/5", port=self.remote)["source"]
        context = repository_context(other)
        os.environ["ORCA_TERMINAL_HANDLE"] = "elsewhere"
        self.current = {"id": "run-2", "coordinator_handle": "elsewhere", "consumer_generation": 1}
        self.terminals["elsewhere"] = "live"
        run("checkpoint", {"project": str(other), "objective": "widgets", "owner": "elsewhere",
                           "value": {**self.core(obligations=[self.criterion()], objective_source=source,
                                                 governance={"base_ref": "refs/remotes/origin/target"},
                                                 worktree={"repository": context["repository"],
                                                           "repo_key": context["repo_key"],
                                                           "path": context["worktree"], "branch": context["branch"]}),
                                     "candidate": git(other, "rev-parse", "HEAD")}})
        path = objective_root(other, "widgets") / "governor.json"
        journal = _read_journal(path)
        journal["actions"].append({"record_id": "w" * 64, "logical_key": "w" * 64, "attempt": 1,
                                   "action": {"kind": "merge", "candidate": "c", "target": "origin/target",
                                              "unit": "default", "pr": 9},
                                   "decision": "ALLOW", "outcome": "PASS", "receipt": {"provider": {}},
                                   "warnings": [], "reasons": [], "classifications": [], "run_event": None,
                                   "rejected_run": None})
        _write_journal(path, journal)
        self.remote.pulls[9] = dict(MERGED)
        self.terminals["elsewhere"] = "gone"
        os.environ["ORCA_TERMINAL_HANDLE"] = "next"
        return other

    def test_two_repositories_close_every_proved_objective_and_a_second_run_writes_nothing(self):
        self.shipped(unknown_start=False)
        self.other("lead-task", "lead", run_id="run-3", source=False)  # Undelivered: a task for the lead.
        self.terminals["lead"] = "stale"
        self.other("unread", "unread", run_id="run-4", pr=10)  # Delivered, but its coordinator is unproven gone.
        self.remote.pulls[10] = dict(MERGED)
        self.terminals["unread"] = "unreadable"
        other = self.second_repository()
        listed = self.listed_delivered()
        self.assertEqual({name: row["delivery_proof"]["status"] for name, row in listed.items()},
                         {"objective": "proved", "lead-task": "not_proved", "unread": "unverified"})
        self.assertNotIn("delivery_proof", self.listed()["objective"])  # The plain list stays record-only.
        before = self.everything()
        code, result = self.sweep("--repository", str(self.project), "--repository", str(other))
        self.assertEqual((code, result["schema"], result["status"], len(result["repositories"])),
                         (0, "pod-cli/v5", "swept", 2))
        self.assertEqual([sorted(row) for row in result["repositories"]],
                         [["objectives", "path", "reason", "repo_key", "repository", "status"]] * 2)
        self.assertEqual({tuple(sorted(row)) for repository in result["repositories"]
                          for row in repository["objectives"]}, {("conditions", "objective", "outcome", "party")})
        self.assertTrue(result["next_safe_action"])
        self.assertEqual(result["counts"], {"closed": 2, "already_closed": 0, "not_proved": 1, "unverified": 1,
                                            "skipped": 0})
        self.assertEqual(self.swept(result, "widgets")["outcome"], "closed")
        lead, unread = self.swept(result, "lead-task"), self.swept(result, "unread")
        self.assertEqual((lead["party"], [row["code"] for row in lead["conditions"]]),
                         ("lead", ["source_unproved", "delivery_unrecorded"]))
        self.assertEqual((unread["outcome"], unread["conditions"][0]["code"]), ("unverified", "owner_unverified"))
        self.assertEqual(_questions(result), [])
        written = sorted(set(self.everything()) - set(before))
        self.assertEqual(len(written), 2)
        self.assertTrue(all(name.endswith("/" + FILE) for name in written), written)
        self.assertEqual({name: data for name, data in self.everything().items() if name in before}, before)
        self.assertEqual((self.listed()["objective"]["closure_basis"], self.listed()["lead-task"]["closure_basis"]),
                         ("evidence", None))
        settled = self.everything()
        code, again = self.sweep("--repository", str(self.project), "--repository", str(other))
        self.assertEqual(again["counts"], {"closed": 0, "already_closed": 2, "not_proved": 1, "unverified": 1,
                                           "skipped": 0})
        self.assertEqual(self.everything(), settled)
        code, text = self.sweep("--repository", str(self.project), text=True)
        self.assertEqual(code, 0)
        self.assertIn("a task for the project lead", text)
        code, missing = self.sweep("--repository", str(self.root / "missing"), "--repository", str(other))
        self.assertEqual((code, [row["status"] for row in missing["repositories"]]), (0, ["unavailable", "swept"]))
        self.assertTrue(missing["repositories"][0]["reason"])

    def listed_delivered(self):
        out = StringIO()
        old = os.getcwd()
        try:
            os.chdir(self.project)
            with redirect_stdout(out):
                cli.main(["status", "--list", "--delivered", "--json"])
        finally:
            os.chdir(old)
        return {row["objective"]: row for row in json.loads(out.getvalue())["objectives"]}

    def test_after_closure_every_write_path_refuses_objective_closed(self):
        self.shipped()
        self.assertEqual(close_by_evidence(self.project, "objective", caller="sweep")["status"], "closed")
        self.returning()
        root = objective_root(self.project, "objective")
        before, starts, sent, closed = self.everything(), len(self.port.starts), len(self.remote.calls), self.files()
        project = str(self.project)
        calls = {
            "checkpoint": lambda: self.op("checkpoint", value=self.core()),
            "governor-prepare": lambda: self.op("governor-prepare", unit="next"),
            "governor-execute": lambda: self.op("governor-execute", action=self.action()),
            "governor-outcome": lambda: self.op("governor-outcome", record_id=self.publication, outcome="PASS"),
            "admission": lambda: self.start("task-3", self.packet(["S2"], boundary={"paths": ["area2"]})),
            "report": lambda: self.report(self.admission, self.frozen),
            "runtime-continuity": lambda: self.op("runtime-continuity", decision={
                "provenance": "user_direct", "instruction": "rebind", "objective": "objective",
                "recorded_runtime": "runtime", "current_runtime": "runtime-new", "ambiguity": ["x"]}),
            "standing-authorization": lambda: self.op("standing-authorization", action="activate",
                                                      decision={"provenance": "user_direct",
                                                                "instruction": "use yolo from now on"}),
            "pod-tick": lambda: self.op("pod-tick", pod=1),
            "constraint": lambda: self.op("constraint", action="add", value={"kind": "x"}),
            "record-compaction": lambda: run("record-compaction", {"project": project, "objective": "objective"}),
            "owner-handoff": lambda: run("owner-handoff", {"project": project, "objective": "objective",
                                                           "owner": "owner", "decision": {
                                                               "provenance": "user_direct", "instruction": "move",
                                                               "objective": "objective", "from_owner": "owner",
                                                               "to_owner": "owner", "run": "run",
                                                               "recorded_generation": 1,
                                                               "recorded_runtime": "runtime",
                                                               "current_runtime": "runtime-new", "ambiguity": []}}),
            "succession": lambda: self.claim(predecessor="objective", name="objective-next"),
            "context backstop": lambda: __import__("pod.history", fromlist=["publish_context"]).publish_context(
                root / "context.json", {}, []),
            "journal backstop": lambda: _write_journal(root / "governor.json", _read_journal(root / "governor.json")),
        }
        os.environ["ORCA_TERMINAL_HANDLE"] = "owner"
        for name, call in calls.items():
            with self.subTest(path=name):
                if name == "succession":
                    self.other("objective-next", "next", run_id="run-s")
                    os.environ["ORCA_TERMINAL_HANDLE"] = "next"
                    self.current = {"id": "run-s", "coordinator_handle": "next", "consumer_generation": 2}
                    before = self.everything()
                error = self.refused(call, code="objective_closed")
                self.assertEqual(error.detail["basis"], "evidence", name)
                self.assertEqual(self.everything(), before, name)
        detected = self.retire()
        self.assertEqual(detected["objectives"][0]["condition"], "objective_closed")
        self.assertNotIn("question", detected)
        # A directory Pod cannot inspect never reads as closed, and its writes still refuse, by their own code.
        mode = root.stat().st_mode & 0o777
        root.chmod(0)
        try:
            self.refused(calls["journal backstop"], code="unsafe_state")
        finally:
            root.chmod(mode)
        # CE's cleanup reads it as closed, like an objective its coordinator closed (R94); its remote branch read
        # is answered locally, never by the network.
        from pod import cleanup
        local = cleanup._git
        with patch("pod.cleanup._git", side_effect=lambda where, argv: None if argv[:1] == ["ls-remote"]
                   else local(where, argv)):
            plan = run("cleanup-plan", {"project": project, "objective": "objective"})
        self.assertEqual(plan["closure_basis"], "evidence")
        self.assertEqual((len(self.port.starts), self.mutations), (starts, []))
        self.assertFalse([call for call in self.remote.calls[sent:] if call[0] in MUTATIONS])
        self.assertEqual(self.files(), closed)

    def test_the_sweep_reads_every_record_past_the_list_bound_and_names_what_it_cannot_read(self):
        """F4: the sweep's own bounded scan, never the list's 128 rows; named objectives resolve directly; a
        repository it cannot read whole is `incomplete` with its reason, and nothing claims it all closed."""
        self.shipped()
        self.finished(128)
        self.assertNotIn("objective", self.listed())  # The presentation list stops at its first 128 rows.
        code, first = self.sweep()
        repository = first["repositories"][0]
        self.assertEqual((code, repository["status"], repository["reason"], self.swept(first)["outcome"]),
                         (0, "swept", None, "closed"))
        self.assertEqual(first["counts"], {"closed": 1, "already_closed": 0, "not_proved": 0, "unverified": 0,
                                           "skipped": 128})
        self.assertEqual(first["next_safe_action"],
                         "every provably delivered objective is closed; nothing else needs the sweep")
        settled = self.everything()
        # Named objectives are resolved from their own records, never filtered from a list; a repeat writes nothing.
        named = self.sweep("--objective", "objective", "--objective", "missing")[1]
        self.assertEqual([(row["objective"], row["outcome"], [item["code"] for item in row["conditions"]])
                          for row in named["repositories"][0]["objectives"]],
                         [("objective", "already_closed", []), ("missing", "skipped", ["objective_unknown"])])
        self.assertEqual(self.everything(), settled)
        # Records the scan cannot read: one unreadable, one directory of Pod files no record names, and every
        # directory past a scan bound lowered for the test.
        (objective_root(self.project, "aaa-000") / "context.json").write_text("{broken")
        stray = objective_root(self.project, "objective").parent / ("0" * 64)
        stray.mkdir()
        (stray / "governor.json").write_text("{}")
        with patch("pod.listing.MAX_SCAN", 0):
            code, blind = self.sweep()
            named = self.sweep("--objective", "objective")[1]
        repository = blind["repositories"][0]
        self.assertEqual((code, blind["status"], repository["status"], repository["objectives"]),
                         (0, "swept", "incomplete", []))
        self.assertIn("130 record directory(ies) lie beyond the sweep's 0-directory scan bound", repository["reason"])
        self.assertNotIn("every provably delivered objective is closed", blind["next_safe_action"])
        self.assertIn("could not all be read", blind["next_safe_action"])
        self.assertEqual((named["repositories"][0]["status"], self.swept(named)["outcome"]), ("swept", "already_closed"))
        code, partial = self.sweep()
        repository = partial["repositories"][0]
        self.assertEqual(repository["status"], "incomplete")
        self.assertIn("1 record(s) cannot be read (" + objective_root(self.project, "aaa-000").name[:16],
                      repository["reason"])
        self.assertIn("1 director(ies) hold Pod files without a record naming them (" + "0" * 16, repository["reason"])
        rows = repository["objectives"]
        self.assertEqual((len(rows), partial["counts"]), (128, {"closed": 0, "already_closed": 1, "not_proved": 0,
                                                                 "unverified": 0, "skipped": 127}))
        self.assertTrue(all(row["outcome"] in ("closed", "already_closed", "not_proved", "unverified", "skipped")
                            for row in rows))

    def test_an_older_record_closed_by_evidence_reads_closed_in_its_cleanup_plan(self):
        """F5: cleanup-plan reads an older evidence-closed record through the evidence path, keeping every guard;
        an older open record keeps its refusal."""
        from pod import cleanup
        self.shipped()
        self.rewrite(schema=_older(STATE_SCHEMAS["context"]))
        project, local = str(self.project), cleanup._git
        remote = patch("pod.cleanup._git", side_effect=lambda where, argv: None if argv[:1] == ["ls-remote"]
                       else local(where, argv))
        remote.start(); self.addCleanup(remote.stop)
        self.refused(lambda: run("cleanup-plan", {"project": project, "objective": "objective"}),
                     code="state_unsupported")
        self.assertEqual(self.swept(self.sweep()[1])["outcome"], "closed")
        before = self.everything()
        plan = run("cleanup-plan", {"project": project, "objective": "objective"})
        self.assertEqual((plan["closure_basis"], plan["record_class"]), ("evidence", "older"))
        self.assertTrue(plan["resources"])
        self.assertEqual({row["class"] for row in plan["resources"]}, {"protected"})
        self.assertTrue(all(row["delete_argv"] is None for row in plan["resources"]))
        self.assertIn("Older record closed by evidence, read through its documented fields only", plan["notes"][-1])
        worktree = next(row for row in plan["resources"] if row["kind"] == "worktree")
        self.assertEqual(worktree["identity"]["path"], self.where())
        self.assertEqual(self.everything(), before)

    def test_the_sweep_needs_no_terminal_or_run_binding_and_mutates_nothing(self):
        self.shipped()
        os.environ.pop("ORCA_TERMINAL_HANDLE", None)
        self.binding_failure = True  # No Run binding either: run-current fails.
        refs = git(self.project, "for-each-ref")
        before, starts, calls = self.everything(), len(self.port.starts), len(self.remote.calls)
        self.native_calls.clear()
        code, result = self.sweep()
        self.assertEqual((code, self.swept(result)["outcome"]), (0, "closed"))
        self.assertNotIn(["orchestration", "run-current", "--json"], self.native_calls)
        self.assertEqual((len(self.port.starts), self.mutations), (starts, []))
        self.assertFalse([call for call in self.remote.calls[calls:] if call[0] in MUTATIONS])
        self.assertEqual(git(self.project, "for-each-ref"), refs)
        added = set(self.everything()) - set(before)
        self.assertEqual([name.rsplit("/", 1)[1] for name in added], [FILE])
        self.assertEqual({name: data for name, data in self.everything().items() if name in before}, before)


class RetirementProofTests(ClosureCase):
    """R147: delivered history needs no owner decision; a decision stays optional and works as before."""

    def test_detection_names_a_delivered_objective_outside_the_owner_question(self):
        self.shipped()
        before = self.everything()
        # Status of the lost coordinator: delivery proved, the evidence closure first, and no owner question even
        # where an owner handoff is available to this reader.
        shown = self.status()
        state = shown["readiness"]
        self.assertEqual((state["state"], state["acts_next"], state["recovery"]["route"]),
                         ("coordinator_lost", "lead", "evidence_closure"))
        self.assertEqual(state["summary"], "Coordinator lost; delivery proved: pod sweep closes it by evidence, "
                                           "with no owner decision.")
        self.assertEqual((shown["owner_handoff"]["status"], _questions(shown)), ("handoff_available", []))
        for action in (shown["next_action"], shown["owner_handoff"]["next_action"],
                       shown["successor_claim"]["next_action"]):
            self.assertTrue(action.startswith("Pod sweep closes it by evidence; no owner decision is needed"), action)
        found = self.retire()
        verdict = found["objectives"][0]
        self.assertEqual((found["status"], verdict["status"], verdict["condition"], verdict["delivery"]),
                         ("retirement_unavailable", "delivered", "delivered", [7]))
        self.assertEqual(verdict["next_action"], "pod sweep closes it by evidence; no owner decision is needed")
        self.assertEqual((_questions(found), found["delivered"]), ([], ["objective"]))
        self.assertIn("pod sweep", found["next_action"])
        self.assertEqual(self.everything(), before)

    def test_proof_delivery_closes_by_evidence_and_refuses_the_rest_as_lead_tasks(self):
        self.shipped()
        request = {"project": str(self.project), "proof": "delivery",
                   "objectives": [{"objective": "objective", "release": [self.where()]}, {"objective": "missing"}]}
        result = run("historical-retirement", request)
        closed, missing = result["objectives"]
        self.assertEqual((result["status"], closed["status"], missing["status"], missing["condition"],
                          missing["party"]), ("partially_closed", "closed", "refused", "objective_unknown", "lead"))
        self.assertEqual((self.closure()["caller"], _questions(result)), ("historical-retirement", []))
        self.assertNotIn("historical_retirement", self.state())
        self.assertFalse(objective_root(self.project, "missing").exists())
        self.assertEqual(run("historical-retirement", request)["objectives"][0]["status"], "replayed")
        self.refused(lambda: run("historical-retirement", {**request, "decision": {}}), code="invalid_request")
        self.assert_closed_by_evidence()

    def test_a_user_direct_decision_still_works(self):
        self.shipped(unknown_start=False)  # R110 still refuses a claim of an unresolved start.
        # A successor's claim keeps its decision; for delivered work its next action is the evidence closure.
        self.other("successor", "next", run_id="run-s")
        self.current = {"id": "run-s", "coordinator_handle": "next", "consumer_generation": 2}
        os.environ["ORCA_TERMINAL_HANDLE"] = "next"
        claim = self.claim()
        self.assertEqual((claim["status"], claim["delivered"], _questions(claim)), ("claim_available", True, []))
        self.assertTrue(claim["next_action"].startswith("Pod sweep closes it by evidence"), claim["next_action"])
        decision = self.decide()
        self.assertEqual(decision["provenance"], "user_direct")
        applied = self.retire(decision)
        self.assertEqual((applied["status"], applied["objectives"][0]["status"]), ("retired", "retired"))
        self.assertEqual(self.status()["phase"], "retired")
        self.assertIsNone(self.closure())
        self.assertEqual(prove(self.project, "objective")["conditions"][0]["code"], "objective_retired")


if __name__ == "__main__":
    unittest.main()
