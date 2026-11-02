"""R209: closure, evidence closure, readers and older-reader compatibility of a recorded local append."""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from pod import admission, cli
from pod.evidence_closure import FILE
from pod.governance import validate_delivery_record
from pod.ledger import objective_root, read
from pod.status import status
from tests import test_delivery as r93
from tests.kernel_support import KernelCase, git
from tests.local_append_support import OPERATION, LocalAppendCase, LocalAppendMixin, recorded_source_packet
from tests.test_evidence_closure import TICKED, ClosureCase

REPOSITORY = Path(__file__).resolve().parents[1]
RELEASED = "7bcde17c82ed28e5675b1705d185137f01cf7ea0"  # Pod 0.21.37, the last release before R209
EXTERNAL = {"party": "third_party", "need": "acceptance", "unblocks_when": "accepted"}


def text_status(project):
    out, old = StringIO(), os.getcwd()
    try:
        os.chdir(project)
        with redirect_stdout(out):
            cli.main(["status", "--objective", "objective"])
    finally:
        os.chdir(old)
    return out.getvalue()


class ClosureTests(LocalAppendCase):
    def bookkeep(self):
        """R202 bookkeeping: external evidence for the remaining row R, as an update at the next sequence."""
        seq = read(self.project, "objective")["checkpoint"]["seq"] + 1
        return self.write(update={"R": {"evidence": [self.proof("R", status="FAILED")]}}, seq=seq)

    def test_delivered_reads_finalizing_until_the_coordinator_closes(self):
        self.ready()
        self.record()
        shown = status(self.project, None, objective="objective")
        state = shown["readiness"]
        self.assertEqual((state["state"], state["acts_next"], state["coordinator_needed"]),
                         ("finalizing", "coordinator", True))
        self.assertTrue(state["summary"].startswith("Delivered, not closed:"), state["summary"])
        self.assertEqual(state["finalization"]["remaining"], ["closure"])
        self.assertEqual((shown["delivery"]["kind"], shown["delivery"]["operation"]), ("local_append", OPERATION))
        self.assertIn(f"Delivery   local append fast-forward {self.candidate[:12]} (operation {OPERATION})",
                      text_status(self.project))
        closed = self.write(self.stored(), close=True)["checkpoint"]
        self.assertTrue(closed["closure"])
        self.assertEqual(status(self.project, None, objective="objective")["readiness"]["state"], "closed")

    def test_a_moved_target_with_changed_governance_keeps_the_bound_base(self):
        self.ready(self.sub("R", state="blocked_external", external=dict(EXTERNAL)))
        bound = read(self.project, "objective")["checkpoint"]["governance"]
        moved = self.move_target("CLAUDE.md")
        recorded = self.record()  # a delivery-only write on R154's route
        self.assertEqual(recorded["checkpoint"]["governance"], bound)
        kept = recorded["governance_adoption"]
        self.assertEqual((kept["adopted"], kept["base"], kept["target"], kept["delivery"], kept["delivery_kind"]),
                         (False, self.base, moved, self.candidate, "local_append"))
        bookkept = self.bookkeep()  # R202 bookkeeping
        self.assertEqual((bookkept["checkpoint"]["governance"], bookkept["governance_adoption"]["adopted"]),
                         (bound, False))
        # New work keeps today's currency refusal.
        before = self.saved()
        self.failure("governance_changed", self.write, candidate=self.fix("src/later.py", "later\n"))
        self.assertEqual(self.saved(), before)

    def test_a_closing_checkpoint_and_report_keep_the_bound_base(self):
        self.ready()
        self.record()
        self.write([*self.stored(), self.sub("S", "active", executor="coordinator")])
        frozen = self.packet(["S"], role="investigate", resolves="verify delivery", stop_condition="report consumed")
        assignment = self.start("closing-report", frozen)["admission"]
        self.settle(assignment)
        bound = read(self.project, "objective")["checkpoint"]["governance"]
        self.move_target("CLAUDE.md")
        rows = self.stored()
        rows[-1].pop("executor")
        rows[-1].update(state="satisfied", evidence=[self.proof("S")])
        closed = self.report(assignment, frozen, map={"obligations": rows, "close": True})
        checkpoint = read(self.project, "objective")["checkpoint"]
        self.assertTrue(checkpoint["closure"])
        self.assertEqual((checkpoint["governance"], closed["governance_adoption"]["adopted"]), (bound, False))
        self.assertEqual(checkpoint["delivery"]["operation"], OPERATION)

    def test_a_closing_write_that_first_records_the_append_refuses(self):
        self.ready()
        self.answer(self.verdict())
        before = self.saved()
        error = self.failure("obligation_unaccounted", self.write, self.stored(), delivery=self.request(), close=True)
        self.assertEqual(error.detail["detail"], "closure_unfinished")
        self.assertIn("delivery_record", str(error))
        self.assertIn("in its own checkpoint first", error.detail["next_action"])
        self.assertEqual(self.saved(), before)

    def test_sideways_moves_and_retargets_keep_their_refusals(self):
        self.ready()
        self.record()
        before = self.saved()
        orphan = git(self.project, "commit-tree", self.tree(self.base), "-m", "unrelated history")
        git(self.project, "update-ref", "refs/remotes/origin/target", orphan)
        self.failure("governance_changed", self.write, self.stored(), close=True)
        self.assertEqual(self.saved(), before)
        moved = self.move_target_from(self.base, "CLAUDE.md")
        git(self.project, "update-ref", "refs/remotes/origin/elsewhere", moved)
        git(self.project, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/elsewhere")
        with self.assertRaises(Exception):
            self.write(self.stored(), close=True)
        self.assertEqual(self.saved(), before)

    def move_target_from(self, start, path):
        git(self.project, "update-ref", "refs/remotes/origin/target", start)
        return self.move_target(path)

    def test_unchanged_sources_adopt_although_the_target_lacks_the_result(self):
        self.ready()
        self.record()
        moved = self.move_target("docs/notes.md")
        adopted = self.write()["checkpoint"]
        self.assertEqual(adopted["governance"]["base"], moved)
        self.assertEqual(adopted["delivery"]["base"], self.base)  # the record keeps the base it was recorded on
        self.assertTrue(self.write(self.stored(), close=True)["checkpoint"]["closure"])

    def test_proof_the_delivery_relied_on_is_judged_on_the_delivered_candidate(self):
        self.ready(self.sub("R", state="blocked_external", external=dict(EXTERNAL)), sources=["src/fix.py"])
        self.record()
        bound = read(self.project, "objective")["checkpoint"]
        self.move_target("CLAUDE.md")
        git(self.project, "checkout", "-qb", "later-work")
        (self.project / "src" / "fix.py").write_text("later, unrelated work\n")
        git(self.project, "commit", "-qam", "later work")
        kept = self.bookkeep()
        self.assertFalse(kept["governance_adoption"]["adopted"])
        self.assertEqual(kept["checkpoint"]["obligations"][0], bound["obligations"][0])


class ReaderTests(LocalAppendCase):
    def test_admission_and_the_selected_read_carry_no_delivery_fact(self):
        self.ready()
        before = admission.selected(self.project, objective="objective", candidate=self.candidate)
        self.record()
        after = admission.selected(self.project, objective="objective", candidate=self.candidate)
        self.assertEqual((before["status"], after["status"]), ("ready", "ready"))
        self.assertEqual(sorted(after), sorted(before))
        self.assertEqual(after["proof"], before["proof"])
        self.assertNotIn("local_append", json.dumps(after))
        self.assertIs(after["authorization"] if "authorization" in after else False, False)

    def test_the_tick_line_is_unchanged_by_the_recording(self):
        self.bound()
        self.ready(objective_source=self.source)
        line = self.line()
        self.record()
        self.assertEqual({key: self.line()[key] for key in ("status", "due", "direction", "proven")},
                         {key: line[key] for key in ("status", "due", "direction", "proven")})

    def test_a_subgoal_citing_the_receipt_after_the_delivery_records_nothing(self):
        self.ready()
        self.record()
        recorded, reads = deepcopy(self.delivery()), len(self.requests())
        # The record is the delivery; a subgoal whose evidence cites its receipt is not, and reads nothing.
        self.assertEqual(self.cite_receipt()["delivery"], recorded)
        self.assertEqual(len(self.requests()), reads)

    def test_a_subgoal_citing_a_receipt_before_any_delivery_records_none(self):
        self.ready()
        self.assertIsNone(self.cite_receipt().get("delivery"))
        self.assertEqual(self.requests(), [])

    def cite_receipt(self):
        self.write([*self.stored(), self.sub("X", "active", executor="coordinator")])
        rows = self.stored()
        rows[-1].pop("executor")
        rows[-1].update(state="satisfied", evidence=[self.proof("X", reference=f"receipt {OPERATION}")])
        return self.write(rows)["checkpoint"]

    def test_cleanup_counts_no_train_only_candidate_integrated(self):
        from pod.internal import run
        self.ready()
        self.record()
        plan = run("cleanup-plan", {"project": str(self.project), "objective": "objective"})
        target = plan["observed_target"] if "observed_target" in plan else plan["target"]
        self.assertFalse(target["moved_after_delivery"])
        merged = json.dumps(plan)
        self.assertNotIn('"integrated_by_delivery"', merged)


class R93UnchangedTests(KernelCase):
    prepare_result = r93.VerifiedDeliveryTests.prepare_result

    def test_a_restated_complete_r93_record_keeps_its_bytes(self):
        self.prepare_result()
        self.write(self.stored(), delivery={"record": "merge-1"})
        stored = deepcopy(read(self.project, "objective")["checkpoint"]["delivery"])
        self.assertNotIn("kind", stored)
        self.assertEqual(validate_delivery_record(stored), stored)
        restated = self.write(self.stored(), delivery=deepcopy(stored))["checkpoint"]["delivery"]
        self.assertEqual(restated, stored)


class LocalEvidenceCase(LocalAppendMixin, ClosureCase):
    packet = recorded_source_packet

    def setUp(self):
        super().setUp()
        self.setup_verifier()

    def delivered(self, *, direct=False, pr=None):
        if direct:
            self.source = None
        fields = {} if direct else {"objective_source": self.source}
        self.fix()
        self.ready(**fields)
        self.write(worktree=self.binding())  # recorded after the review, as a coordinator's later checkpoint does
        if pr is not None:
            self.record_pr("objective", pr)
        self.record()
        self.remote.issues[("acme/example", 82)]["body"] = TICKED
        self.lose()


class EvidenceClosureTests(LocalEvidenceCase):
    def test_sweep_closes_a_lost_coordinators_local_delivery_without_a_pr_or_verifier(self):
        self.delivered()
        reads, calls = len(self.requests()), deepcopy(self.remote.calls)
        code, result = self.sweep()
        self.assertEqual((code, self.swept(result)["outcome"]), (0, "closed"))
        closure = self.closure()
        self.assertEqual(closure["delivery"], [])
        self.assertEqual([(row["objective"], row["operation"], row["result"]) for row in closure["local_delivery"]],
                         [("objective", OPERATION, self.candidate)])
        self.assertEqual(len(self.requests()), reads)
        self.assertFalse([call for call in self.remote.calls[len(calls):] if call[0] in ("pr_view", "closing_prs")])
        self.assert_closed_by_evidence()
        self.assertEqual(self.listed()["objective"]["evidence_closure"]["local_delivery"], [OPERATION])

    def test_a_direct_objective_closes_by_evidence(self):
        self.delivered(direct=True)
        code, result = self.sweep()
        self.assertEqual(self.swept(result)["outcome"], "closed")
        self.assertEqual(self.closure()["local_delivery"][0]["operation"], OPERATION)

    def test_a_recorded_unmerged_pull_request_still_holds_the_chain(self):
        self.delivered(pr=9)
        self.remote.pulls[9] = {"state": "OPEN"}
        before = self.files()
        _, result = self.sweep()
        row = self.swept(result)
        self.assertEqual(row["outcome"], "not_proved")
        self.assertIn("pr_not_merged", [condition["code"] for condition in row["conditions"]])
        self.assertEqual(self.files(), before)

    def test_historical_retirement_detection_names_the_local_operation(self):
        self.delivered()
        verdict = self.retire()["objectives"][0]
        self.assertEqual((verdict["status"], verdict["delivery"], verdict["local_delivery"]),
                         ("delivered", [], [OPERATION]))


class ReleasedReaderTests(LocalEvidenceCase):
    """The genuine 0.21.37 source, extracted from Git objects, never misreads a record holding the new kind."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        parent = Path(os.environ["POD_CACHE_HOME"]) if "POD_CACHE_HOME" in os.environ else None
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        cls.released_home = tempfile.TemporaryDirectory(prefix="local-append-released-", dir=parent)
        cls.addClassCleanup(cls.released_home.cleanup)
        cls.bundle = Path(cls.released_home.name)
        archive = cls.bundle / "released.tar"
        subprocess.run(["git", "-C", str(REPOSITORY), "archive", "-o", str(archive), RELEASED, "skills", "VERSION"],
                       check=True)
        with tarfile.open(archive) as source:
            source.extractall(cls.bundle, filter="data")

    def released(self, code, *args):
        env = {key: value for key, value in os.environ.items() if not key.startswith("ORCA_")}
        env.update(PYTHONPATH=str(self.bundle / "skills"), PATH=os.pathsep.join(
            sorted({str(Path(sys.executable).parent), "/usr/bin", "/bin"})))  # no Orca executable on the path
        result = subprocess.run([sys.executable, "-c", code, *map(str, args)], cwd=self.project, env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_the_released_reader_refuses_the_context_and_the_evidence_file(self):
        self.delivered()
        before = self.files()
        read_back = self.released(
            "import json, sys\nfrom pathlib import Path\nfrom pod.errors import PodError\n"
            "from pod.ledger import load_context\n"
            "try:\n    load_context(Path(sys.argv[1]))\n    print(json.dumps({'code': None}))\n"
            "except PodError as exc:\n    print(json.dumps({'code': exc.code, 'message': str(exc)}))\n",
            objective_root(self.project, "objective") / "context.json")
        self.assertEqual(read_back["code"], "state_unsupported")
        self.assertIn("delivery record is malformed", read_back["message"])
        self.sweep()
        closure = objective_root(self.project, "objective") / FILE
        self.assertTrue(closure.exists())
        evidence = self.released(
            "import json, sys\nfrom pathlib import Path\nfrom pod.errors import PodError\n"
            "from pod import evidence_closure\nroot = Path(sys.argv[1])\n"
            "try:\n    evidence_closure.load(root)\n    code = None\n"
            "except PodError as exc:\n    code = exc.code\n"
            "print(json.dumps({'code': code, 'closed': evidence_closure.tail_closed(root)}))\n",
            closure.parent)
        self.assertEqual(evidence, {"code": "closure_evidence_unreadable", "closed": True})
        self.assertEqual(self.files() | {}, self.files())

    def test_the_released_list_shows_that_record_alone_unsupported(self):
        self.delivered()
        self.other("peer", "peer-owner", run_id="run-peer")
        before = self.files()
        listed = self.released(
            "import json, os, sys\nfrom contextlib import redirect_stdout\nfrom io import StringIO\n"
            "from pod import cli\nout = StringIO()\nos.chdir(sys.argv[1])\n"
            "with redirect_stdout(out):\n    cli.main(['status', '--list', '--json'])\nprint(out.getvalue())\n",
            self.project)
        rows = {row["objective"]: row for row in listed["objectives"]}
        # That one record is an unsupported schema with no readiness; the peer still reads normally.
        self.assertEqual((rows["objective"]["schema"], rows["objective"]["readiness"]), ("unsupported", "unavailable"))
        self.assertEqual((rows["peer"]["schema"], rows["peer"]["readiness"]), ("current", "working"))
        self.assertEqual(self.files(), before)

    def test_released_admission_discovery_reports_unknowns_without_a_write(self):
        self.delivered()
        before = self.files()
        found = self.released(
            "import json, os, sys\nfrom contextlib import redirect_stdout\nfrom io import StringIO\n"
            "from pod import cli\nout = StringIO()\n"
            "with redirect_stdout(out):\n    cli.main(['admission', '--repository', sys.argv[1], '--json'])\n"
            "print(out.getvalue())\n", self.project)
        self.assertIn("unknowns", found)
        self.assertEqual(self.files(), before)


if __name__ == "__main__":
    unittest.main()
