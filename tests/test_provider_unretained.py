"""R252: a validation row whose run GitHub no longer retains settles `provider_unretained` from Pod's own reads."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.github import GhPort, gh_allowed, presence_path
from pod.governor import _read_journal, _record_path, _write_journal
from pod.internal import acknowledge, run
from pod.obligations import _closure_record
from tests.test_evidence_closure import ClosureCase, MUTATIONS

OLD = "2026-10-19T21:48:45+00:00"  # Long past any absence floor, as the expired live row.
SETTLE = {"outcome": "UNKNOWN", "settlement": "provider_unretained"}


class UnretainedCase(ClosureCase):
    """A claim-only successor of a lost, undelivered predecessor, carrying one publication-derived validation."""

    def setUp(self):
        super().setUp()
        self.absent = set()   # (kind, ident) pairs GitHub answers 404 for; runs default to 404.
        self.present = set()  # runs GitHub still holds.
        self.failing = set()  # kinds whose read gives no exact answer.
        self.reads = []
        self.remote.presence = self.presence

    def presence(self, *, kind, ident):
        self.reads.append((kind, ident))
        if kind in self.failing:
            raise PodError("remote_read_failed", "gh read gave no exact presence answer")
        held = (kind, ident) in self.present if kind == "run" else (kind, ident) not in self.absent
        return {"kind": kind, "id": ident, "status": 200 if held else 404, "present": held,
                **({"state": "active"} if kind == "workflow" and held else {})}

    def malformed_list(self):
        """GitHub's run list answered with a member that is not a run, as the port refuses it (R252)."""
        def runs(**_):
            raise PodError("gh_contract", "gh run list returned an unsupported shape")
        self.remote.runs = runs

    def carried(self):
        self.shipped(pr=False, merged=False, ticked=False, unknown_start=False)
        rows = self.claimed_direct()
        path = _record_path(self.project, "successor")
        journal = _read_journal(path)
        row = next(row for row in journal["actions"] if row.get("carried_from"))
        row["receipt"]["started_at"] = OLD
        _write_journal(path, journal)
        return rows, row["record_id"], path

    def unretain(self, record_id, **fields):
        return run("governor-outcome", {"project": str(self.project), "objective": "successor", "owner": "next",
                                        "record_id": record_id, **SETTLE, **fields})

    def refusal(self, record_id, detail, **fields):
        error = self.refused(lambda: self.unretain(record_id, **fields), code="outcome_evidence_required")
        self.assertEqual(error.detail["detail"], detail, error.detail)
        return error

    def close(self, rows):
        request = {"project": str(self.project), "objective": "successor", "owner": "next",
                   "value": self.core(obligations=rows, close=True)}
        return acknowledge("checkpoint", request, run("checkpoint", request))

    def extra(self, path, source, **fields):
        """Another row on the successor's journal, as an earlier record would hold it."""
        journal = _read_journal(path)
        base = next(row for row in journal["actions"] if row["record_id"] == source)
        row = {**deepcopy(base), "record_id": fields.pop("record_id"), "logical_key": "k" * 64, **fields}
        journal["actions"].append(row)
        _write_journal(path, journal)
        return row["record_id"]


class SettlementClosesTests(UnretainedCase):
    def test_an_unlisted_carried_validation_settles_and_its_no_delivery_successor_closes(self):
        """PoD#1: absence proved by exact reads, the row stays UNKNOWN, and the closure preserves it."""
        rows, record_id, path = self.carried()
        commit = _read_journal(path)["actions"][-1]["commit"]
        start = len(self.remote.calls)
        self.refused(lambda: self.close(rows), code="obligation_unaccounted")  # Today's rule before settlement.
        settled = acknowledge("governor-outcome", {"objective": "successor"}, self.unretain(record_id))
        marker = settled["unretained"]
        self.assertEqual((settled["outcome"], marker["settlement"], marker["workflow"], marker["commit"],
                          marker["run_id"]), ("UNKNOWN", "provider_unretained", "ci.yml", commit, None))
        self.assertEqual([(read["kind"], read["status"]) for read in marker["reads"]],
                         [("workflow", 200), ("commit", 200)])
        self.assertEqual((marker["listing"]["listed"], marker["listing"]["limit"], marker["listing"]["truncated"]),
                         (0, 20, False))
        self.assertIn("never created", marker["statement"])
        self.assertIn("Nothing shows that a run ever existed", marker["statement"])
        self.assertTrue(settled["receipt"]["detail"].startswith("provider_unretained: "))
        recorded = path.read_bytes()
        self.assertEqual(self.unretain(record_id)["unretained"], marker)  # A replay answers from the record.
        self.assertEqual(path.read_bytes(), recorded)
        closed = self.close(rows)
        preserved = {"record": record_id, "kind": "workflow_dispatch", "recorded": "UNKNOWN", "outcome": "UNKNOWN",
                     "reason": "provider_unretained", "workflow": "ci.yml", "commit": commit}
        self.assertEqual((closed["closure_preserved"], closed["finalization"]["finished"]), ([preserved], True))
        self.assertEqual(path.read_bytes(), recorded)  # The closure stamps nothing on the journal.
        self.assertEqual(self.status(objective="successor")["closure_preserved"], [preserved])
        self.assertFalse([call for call in self.remote.calls[start:] if call[0] in MUTATIONS])

    def test_a_delivery_or_unreadable_journal_in_the_chain_or_a_run_listed_again_keeps_todays_refusal(self):
        """A delivered chain, or a predecessor journal Pod cannot read, keeps today's rules; a closing read that no
        longer proves absence holds the row."""
        rows, record_id, path = self.carried()
        self.unretain(record_id)
        before = self.everything()
        self.remote._start("ci.yml", _read_journal(path)["actions"][-1]["commit"], event="push")
        error = self.refused(lambda: self.close(rows), code="obligation_unaccounted")
        held = error.detail["closure_preservation"]["held"]
        self.assertEqual([row["record"] for row in held], [record_id])
        self.assertIn("GitHub still lists 1 runs of ci.yml", held[0]["reason"])
        self.assertEqual(self.everything(), before)
        self.remote.runs_by.clear()
        predecessor = _record_path(self.project, "objective")
        saved = predecessor.read_bytes()
        predecessor.unlink()
        predecessor.symlink_to(predecessor.parent / "missing.json")  # A dangling link is unreadable, not absent.
        own = self.files("successor")
        error = self.refused(lambda: self.close(rows), code="obligation_unaccounted")
        self.assertNotIn("provider_unretained", json.dumps(error.detail))
        self.assertEqual(self.files("successor"), own)
        predecessor.unlink()
        predecessor.write_bytes(saved)
        journal = _read_journal(predecessor)
        journal["actions"].append({**deepcopy(journal["actions"][0]), "record_id": "m" * 64, "logical_key": "m" * 64,
                                   "decision": "ALLOW", "outcome": "PASS",
                                   "action": {**journal["actions"][0]["action"], "kind": "merge"}})
        _write_journal(predecessor, journal)
        error = self.refused(lambda: self.close(rows), code="obligation_unaccounted")
        self.assertNotIn("provider_unretained", json.dumps(error.detail))


class SettlementRefusedTests(UnretainedCase):
    def test_merge_push_and_other_kinds_are_refused_before_any_read(self):
        """PoD#2: only validation rows settle this way; nothing is read or written for another kind."""
        _, record_id, path = self.carried()
        for kind, purpose in (("merge", "release"), ("push", "checkpoint"), ("pr_update", "validation"),
                              ("remote_diagnostic", "diagnostic"), ("cancel_validation", "cancellation"),
                              ("workflow_dispatch", "diagnostic")):
            with self.subTest(kind=kind, purpose=purpose):
                other = self.extra(path, record_id, record_id=(kind[0] + purpose[0]) * 32, purpose=purpose,
                                   action={**_read_journal(path)["actions"][0]["action"], "kind": kind})
                before, self.reads = path.read_bytes(), []
                error = self.refusal(other, "not_validation")
                self.assertIn("not a validation run", str(error))
                self.assertEqual((path.read_bytes(), self.reads), (before, []))
        for fields in ({"outcome": "FAILED"}, {"provider": {"run_id": "1"}}, {"detail": "gone"},
                       {"settlement": "expired"}):
            with self.subTest(fields=fields):
                self.refused(lambda: self.unretain(record_id, **fields), code="invalid_outcome")

    def test_a_listed_or_retained_run_and_an_unproved_listing_are_refused(self):
        """PoD#2: a run still listed, a bound run GitHub still holds, or reads that do not make the list meaningful."""
        _, record_id, path = self.carried()
        commit = _read_journal(path)["actions"][-1]["commit"]

        def listed():
            self.remote._start("ci.yml", commit, event="pull_request", created_at="2026-01-01T00:00:00+00:00")
        bound = self.extra(path, record_id, record_id="b" * 64, derived_from=None,
                           receipt={**_read_journal(path)["actions"][-1]["receipt"], "provider": {"run_id": "555"}})
        cases = (
            (record_id, listed, "unretained_unproved", "still lists 1 runs"),  # Any run on the commit, any event.
            (bound, lambda: self.present.add(("run", "555")), "unretained_unproved", "still retains run 555"),
            (record_id, lambda: self.absent.add(("workflow", "ci.yml")), "unretained_unproved", "workflow ci.yml"),
            (record_id, lambda: self.absent.add(("commit", commit)), "unretained_unproved", f"commit {commit}"),
            (record_id, lambda: self.failing.add("commit"), "readback_unavailable", "remote_read_failed"),
            (record_id, self.malformed_list, "readback_unavailable", "gh_contract"),
        )
        recorded = path.read_bytes()
        for target, arrange, detail, text in cases:
            with self.subTest(detail=detail, text=text):
                arrange()
                self.assertIn(text, str(self.refusal(target, detail)))
                self.assertEqual(path.read_bytes(), recorded)  # Nothing is recorded.
                self.remote.runs_by.clear(); self.present.clear(); self.absent.clear(); self.failing.clear()
                self.remote.__dict__.pop("runs", None)
        receipt = {**_read_journal(path)["actions"][0]["receipt"], "started_at": datetime.now(timezone.utc).isoformat()}
        fresh = self.extra(path, record_id, record_id="f" * 64, receipt=receipt)
        self.assertIn("may still be in flight", str(self.refusal(fresh, "unretained_unproved")))

    def test_a_stored_run_id_never_stands_in_for_the_empty_list(self):
        """A stored run id that reads 404 settles only beside a complete, empty list, whatever event it records: an
        earlier Pod recorded `run_event` without R115's commit and workflow checks, so a retained run on the commit
        may still be the row's own."""
        _, record_id, path = self.carried()
        receipt = {**_read_journal(path)["actions"][-1]["receipt"], "provider": {"run_id": "555"}}
        rows = {"dispatch": self.extra(path, record_id, record_id="d" * 64, derived_from=None, receipt=receipt,
                                       run_event="workflow_dispatch"),
                "event-bearing": self.extra(path, record_id, record_id="e" * 64, receipt=receipt, run_event="push"),
                "no event": self.extra(path, record_id, record_id="a" * 64, receipt=receipt)}
        commit = _read_journal(path)["actions"][-1]["commit"]
        self.remote._start("ci.yml", commit, event="push")  # A retained run that may be any of these rows' own.
        recorded = path.read_bytes()
        for name, row in rows.items():
            with self.subTest(row=name):
                self.assertIn("still lists 1 runs", str(self.refusal(row, "unretained_unproved")))
                self.assertEqual(path.read_bytes(), recorded)
        self.remote.runs_by.clear()
        for name, row in rows.items():
            with self.subTest(row=name):
                marker = self.unretain(row)["unretained"]
                self.assertEqual((marker["run_id"], marker["listing"]["listed"],
                                  [(read["kind"], read["status"]) for read in marker["reads"]]),
                                 ("555", 0, [("workflow", 200), ("commit", 200), ("run", 404)]))


class StoredShapeTests(unittest.TestCase):
    ROW = {"record": "r" * 64, "kind": "workflow_dispatch", "recorded": "UNKNOWN", "outcome": "UNKNOWN",
           "reason": "provider_unretained", "workflow": "ci.yml", "commit": "c" * 40}

    def test_the_closure_record_accepts_only_the_exact_unretained_shape(self):
        closure = {"seq": 3, "revision": 2, "report": {}}
        self.assertEqual(_closure_record({**closure, "preserved": [self.ROW]})["preserved"], [self.ROW])
        for change in ({"recorded": "pending"}, {"reason": "superseded_by_merged_head"}, {"kind": "merge"},
                       {"commit": "c" * 12}, {"pr": 7}, {"workflow": ""}):
            with self.subTest(change=change):
                with self.assertRaises(PodError):
                    _closure_record({**closure, "preserved": [{**self.ROW, **change}]})


class PresenceReadTests(unittest.TestCase):
    def answer(self, stdout, returncode):
        completed = subprocess.CompletedProcess([], returncode, stdout, "")
        with patch("pod.github._run", return_value=completed) as ran:
            try:
                return GhPort(Path(".")).presence(kind=self.kind, ident=self.ident), ran
            except PodError as exc:
                return exc.code, ran

    def test_only_an_exact_200_or_404_answers(self):
        self.kind, self.ident = "run", "555"
        found, ran = self.answer("HTTP/2.0 404 Not Found\nX-A: b\n\n{\"message\":\"Not Found\"}", 1)
        self.assertEqual(found, {"kind": "run", "id": "555", "status": 404, "present": False})
        self.assertEqual(ran.call_args[0][0][1:], ["api", "--include", "repos/{owner}/{repo}/actions/runs/555"])
        self.assertEqual(self.answer("HTTP/2.0 200 OK\n\n{\"id\": 555}", 0)[0]["present"], True)
        for stdout, code, expected in (("HTTP/2.0 200 OK\n\n{\"id\": 556}", 0, "gh_contract"),
                                       ("HTTP/2.0 404 Not Found\n\n{}", 0, "remote_read_failed"),
                                       ("HTTP/2.0 500 Oops\n\n{}", 1, "remote_read_failed"),
                                       ("HTTP/2.0 403 Forbidden\n\n{}", 1, "remote_read_failed"),
                                       ("not http", 1, "remote_read_failed")):
            with self.subTest(stdout=stdout):
                self.assertEqual(self.answer(stdout, code)[0], expected)
        self.kind, self.ident = "workflow", "ci.yml"
        self.assertEqual(self.answer('HTTP/2.0 200 OK\n\n{"id": 1, "path": ".github/workflows/ci.yml", '
                                     '"state": "active"}', 0)[0]["state"], "active")
        self.assertEqual(self.answer('HTTP/2.0 200 OK\n\n{"id": 1, "path": ".github/workflows/other.yml", '
                                     '"state": "active"}', 0)[0], "gh_contract")
        self.kind, self.ident = "commit", "a" * 40
        self.assertEqual(self.answer('HTTP/2.0 200 OK\n\n{"sha": "' + "a" * 40 + '"}', 0)[0]["present"], True)

    def test_a_run_list_member_that_is_not_a_run_is_no_answer(self):
        completed = subprocess.CompletedProcess([], 0, "[null]", "")
        with patch("pod.github._run", return_value=completed):
            with self.assertRaises(PodError) as refused:
                GhPort(Path(".")).runs(workflow="ci.yml", commit="a" * 40)
        self.assertEqual(refused.exception.code, "gh_contract")

    def test_the_allowlist_names_each_exact_presence_path(self):
        for kind, ident in (("run", "1"), ("workflow", "ci.yml"), ("commit", "a" * 40)):
            self.assertTrue(gh_allowed(["api", "--include", presence_path(kind, ident)]))
        for path in ("repos/{owner}/{repo}/actions/runs/1/logs", "repos/{owner}/{repo}/git/commits/abc",
                     "repos/other/repo/actions/runs/1", "repos/{owner}/{repo}/actions/workflows/../x.yml"):
            self.assertFalse(gh_allowed(["api", "--include", path]), path)
        self.assertIsNone(presence_path("issue", "1"))
        with self.assertRaises(PodError):
            GhPort(Path(".")).presence(kind="run", ident="1/logs")


if __name__ == "__main__":
    unittest.main()
