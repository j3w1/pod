"""R206: a repository scan never calls a live record unreadable because a writer rewrote it during the read.

Each test interleaves at one injection point, the scan's first read of the peer record's history after its
context.json read: a production write publishes the next revision, and a history collection reclaims the pages the
earlier revision used. No test sleeps or depends on timing.
"""
import json
import os
from unittest.mock import patch

from pod import admission, evidence_closure, history, ledger, listing, retirement
from pod.errors import PodError
from pod.handoff import run_scope
from pod.history_tree import plain
from pod.succession import incoming_claim, refuse_incomplete
from tests.kernel_support import KernelCase

ORIGINAL_LOAD = history.ChunkStore.load


class Interleaving:
    """At the scan's next read of one of the peer's history pages, rewrite the peer `times` times in all."""

    def __init__(self, case, times):
        self.case, self.left, self.busy, self.rewrites = case, times, False, 0

    def load(self, store, address):
        if self.left and not self.busy and store.root.parent.parent == self.case.peer:
            self.left -= 1
            self.busy = True  # The writer's own reads pass straight through.
            try:
                self.case.rewrite()
                self.rewrites += 1
            finally:
                self.busy = False
        return ORIGINAL_LOAD(store, address)

    def __enter__(self):
        def load(store, address):
            return self.load(store, address)
        self.patch = patch.object(history.ChunkStore, "load", load)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


class ScanCase(KernelCase):
    def setUp(self):
        super().setUp()
        for name, value in (("_fleet", {"runtime": "runtime", "complete": True, "workers": []}),
                            ("_terminals", {"runtime": "runtime", "complete": True, "terminals": []}),
                            ("_owner_observation", {"status": "unknown", "code": "fixture_unobserved"})):
            native = patch(f"pod.admission.{name}", return_value=value)
            native.start()
            self.addCleanup(native.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("ORCA_TERMINAL_HANDLE", None)  # The plain list stays record-only.
        self.intake(objective="healthy")
        # Large enough that its checkpoint is a page of its own, read lazily after context.json.
        self.intake(objective="peer", next_safe_action="x" * 1024)
        self.peer = ledger.objective_root(self.project, "peer")
        self.run = ledger.read(self.project, "peer")["checkpoint"]["native_refs"][0]["runId"]
        self.generation = 0

    def rewrite(self):
        """The peer's writer publishes its next revision through Pod's one publication path, then a collection
        reclaims the pages the earlier revision used. The scan's read pin would defer that collection; the injection
        runs it anyway, as a reader outside the pin would meet it."""
        self.generation += 1
        path = self.peer / "context.json"
        state = plain(history.load_context(path, validate=False))
        state["revision"] += 1
        state["checkpoint"]["next_safe_action"] = chr(ord("a") + self.generation % 26) * 1024
        history.publish_context(path, *history.prepare_context(path, state, project=self.project))
        with patch.object(history.fcntl, "flock"):
            collected = history.ChunkStore(self.peer).collect()
        self.assertEqual(collected["status"], "collected")

    def interleaved(self, scan, times):
        with Interleaving(self, times) as interleaving:
            result = scan()
        return result, interleaving.rewrites


class RereadTests(ScanCase):
    """One write and collection between a scan's context read and its page read: the scan reads again."""

    def reread(self, scan):
        result, rewrites = self.interleaved(scan, 1)
        self.assertEqual(rewrites, 1)  # The injection ran inside this scan's read.
        return result

    def test_the_list_reads_the_rewritten_record(self):
        listed = self.reread(lambda: listing.objective_list(self.project))
        self.assertEqual([row["objective"] for row in listed["objectives"]], ["healthy", "peer"])
        self.assertEqual((listed["unreadable_records"], listed["changing_records"], listed["complete"]),
                         ([], [], True))

    def test_claim_scans_read_the_rewritten_record(self):
        self.assertEqual(self.reread(lambda: retirement._claimants(self.project, "healthy")), ([], [], []))
        state = ledger.read(self.project, "healthy")
        self.assertIsNone(self.reread(lambda: incoming_claim(self.project, "healthy", state, full=True)))

    def test_the_evidence_scan_reads_the_rewritten_record(self):
        found, problems = self.reread(lambda: evidence_closure._scan(self.project))
        self.assertEqual((sorted(row["objective"] for row in found), problems), (["healthy", "peer"], []))

    def test_run_scope_and_lookup_read_the_rewritten_record(self):
        scope = self.reread(lambda: run_scope(self.project, "healthy", self.run))
        self.assertEqual((scope["objectives"], scope["unreadable"], scope.get("changing")), (["peer"], 0, None))
        matches = self.reread(lambda: ledger.contexts_for_run(self.run))
        self.assertIn(self.peer, [path for path, _ in matches])  # The fixture's objectives share a Run.

    def test_admission_discovery_reads_the_rewritten_record(self):
        def codes(result):
            return sorted((row["record"], row["code"]) for row in result["unknowns"])
        baseline = admission.query(self.project)
        queried = self.reread(lambda: admission.query(self.project))
        self.assertEqual((queried["unavailable_records"], codes(queried)), ([], codes(baseline)))

    def test_storage_under_reclamation_is_read_again(self):
        # A reader that meets a collection holding the peer's storage is refused history_busy at a stable revision.
        original, refused = history.ChunkStore.pin, []

        def pin(store, **kwargs):
            if store.root.parent.parent == self.peer and not refused:
                refused.append(True)
                raise PodError("history_busy", "Immutable storage is being reclaimed; retry a fresh read")
            return original(store, **kwargs)
        with patch.object(history.ChunkStore, "pin", pin):
            listed = listing.objective_list(self.project)
        self.assertEqual(refused, [True])
        self.assertEqual(([row["objective"] for row in listed["objectives"]], listed["complete"]),
                         (["healthy", "peer"], True))


class ChangingTests(ScanCase):
    """A record rewritten on every bounded attempt reads as changing, a transient cause, and never as unreadable."""
    changed = history.RECORD_CHANGED

    def changing(self, scan):
        # Enough rewrites for the scan's bounded attempts and every membership check after them.
        result, rewrites = self.interleaved(scan, 12)
        self.assertGreaterEqual(rewrites, 1 + history.SCAN_RETRIES)
        return result

    def test_the_list_names_a_changing_record_apart_with_a_retry_step(self):
        listed = self.changing(lambda: listing.objective_list(self.project))
        self.assertEqual([row["objective"] for row in listed["objectives"]], ["healthy"])
        self.assertEqual(listed["unreadable_records"], [])
        self.assertEqual([(row["record"], row["code"], row["phase"]) for row in listed["changing_records"]],
                         [(self.peer.name, self.changed, "unavailable")])
        self.assertFalse(listed["complete"])
        self.assertIn("retry the list", listed["next_safe_action"])
        self.assertNotIn("repaired", listed["next_safe_action"])

    def test_claim_scans_hold_with_the_transient_cause(self):
        self.assertEqual(self.changing(lambda: retirement._claimants(self.project, "healthy")),
                         ([], [], [str(self.peer)]))
        state = ledger.read(self.project, "healthy")
        # Its membership cannot be decoded while it keeps changing, so it holds, with its own cause.
        self.assertEqual(self.changing(lambda: incoming_claim(self.project, "healthy", state, full=True)),
                         {"predecessor": None, "changing": [str(self.peer)]})
        with self.assertRaises(PodError) as caught:
            self.changing(lambda: refuse_incomplete(self.project, "healthy", state, full=True))
        self.assertEqual(caught.exception.code, self.changed)
        self.assertEqual(caught.exception.detail, {"changing": [str(self.peer)],
                                                   "next_action": history.RECORD_CHANGED_ACTION})

    def test_the_evidence_scan_names_the_transient_cause(self):
        _, problems = self.changing(lambda: evidence_closure._scan(self.project))
        self.assertEqual(problems, [{"record": self.peer.name, "code": self.changed}])
        reason = evidence_closure._scope_reason(problems)
        self.assertIn("retry the sweep", reason)
        self.assertNotIn("cannot be read", reason)

    def test_run_scope_and_lookup_hold_with_the_transient_cause(self):
        scope = self.changing(lambda: run_scope(self.project, "healthy", self.run))
        self.assertEqual((scope["unreadable"], scope["changing"]), (0, 1))
        with Interleaving(self, 12), self.assertRaises(PodError) as caught:
            ledger.contexts_for_run(self.run)
        self.assertEqual(caught.exception.code, self.changed)
        self.assertEqual(caught.exception.detail["holding"],
                         [{"record": self.peer.name, "code": self.changed, "class": "changing"}])
        self.assertEqual(caught.exception.detail["next_action"], history.RECORD_CHANGED_ACTION)
        from pod.status import status
        with Interleaving(self, 12):
            shown = status(self.project, self.run)
        self.assertEqual((shown["status"], shown["blocker"], shown["next_safe_action"]),
                         ("blocked", self.changed, history.RECORD_CHANGED_ACTION))
        self.assertEqual(shown["held_by"], [{"record": self.peer.name, "code": self.changed, "class": "changing"}])

    def test_admission_discovery_holds_with_the_transient_cause(self):
        queried = self.changing(lambda: admission.query(self.project))
        self.assertEqual(queried["unavailable_records"], [{"record": self.peer.name, "code": self.changed}])
        self.assertEqual(queried["unrelated_unknowns"]["by_code"], {self.changed: 1})
        self.assertFalse(queried["repository_complete"])


class StableDamageTests(ScanCase):
    def test_a_missing_page_at_a_stable_revision_is_still_unreadable_with_todays_code(self):
        # A valid current record from the production writer loses its checkpoint page; nothing rewrites it.
        path = self.peer / "context.json"
        physical, chunks = history.prepare_context(path, plain(ledger.read(self.project, "peer")), project=self.project)
        history.ChunkStore(self.peer).put(chunks)
        path.write_bytes(history._bytes(physical))
        page = json.loads(chunks[physical["root"]["ref"]["sha256"]])
        entry = next(entry for entry in page["entries"] if entry[0] == "checkpoint")
        (self.peer / "history" / "sha256" / (entry[1]["ref"]["sha256"] + ".json")).unlink()
        with patch("pod.history._load", wraps=history._load) as loads:
            listed = listing.objective_list(self.project)
        # A fault at a stable revision is classified from that one read; the record is not read again.
        self.assertEqual(sum(call.args[0] == path for call in loads.call_args_list), 1)
        self.assertEqual([(row["record"], row["code"]) for row in listed["unreadable_records"]],
                         [(self.peer.name, "history_missing")])
        self.assertEqual(listed["changing_records"], [])
        self.assertIn("repaired or removed", listed["next_safe_action"])
        self.assertEqual(retirement._claimants(self.project, "healthy"), ([], [str(self.peer)], []))
        with self.assertRaises(PodError) as caught:
            refuse_incomplete(self.project, "healthy", ledger.read(self.project, "healthy"), full=True)
        self.assertEqual(caught.exception.code, "claim_unverified")
        self.assertEqual(caught.exception.detail["next_action"], "repair or remove the unreadable record, then retry")
        self.assertEqual(evidence_closure._scan(self.project)[1], [{"record": self.peer.name, "code": "history_missing"}])
        scope = run_scope(self.project, "healthy", self.run)
        self.assertEqual((scope["unreadable"], scope.get("changing")), (1, None))
        with self.assertRaises(PodError) as caught:
            ledger.contexts_for_run(self.run)
        self.assertEqual(caught.exception.code, "history_missing")
        self.assertEqual([(row["class"], row["reason"]) for row in caught.exception.detail["holding"]],
                         [("undecodable", "history_missing")])
        queried = admission.query(self.project)
        self.assertEqual(queried["unavailable_records"], [{"record": self.peer.name, "code": "history_missing"}])


if __name__ == "__main__":
    import unittest
    unittest.main()
