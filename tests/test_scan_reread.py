"""R206: a repository scan never calls a live record unreadable because a writer rewrote it during the read.

Each test interleaves at one injection point, the scan's first read of the peer record's history after its
context.json read: a production write publishes the next revision, and a history collection reclaims the pages the
earlier revision used. No test sleeps or depends on timing.
"""
import json
import os
from unittest.mock import patch

from pod import admission, evidence_closure, governor, history, ledger, listing, retirement
from pod.errors import PodError
from pod.github import repository_context
from pod.handoff import run_scope
from pod.history_tree import plain
from pod.succession import incoming_claim, refuse_incomplete
from tests.kernel_support import KernelCase

ORIGINAL_LOAD = history.ChunkStore.load


class Interleaving:
    """At the scan's next read of one of the peer's history pages (or of the exact page `target()` names), rewrite
    the peer `times` times in all, with `rewrite` (the case's context rewrite by default)."""

    def __init__(self, case, times, *, target=None, rewrite=None):
        self.case, self.left, self.busy, self.rewrites = case, times, False, 0
        self.target, self.rewrite = target, rewrite or case.rewrite

    def load(self, store, address):
        if (self.left and not self.busy and store.root.parent.parent == self.case.peer
                and (self.target is None or address == self.target())):
            self.left -= 1
            self.busy = True  # The writer's own reads pass straight through,
            token = history._OBSERVED.set(None)  # outside the scan's own read.
            try:
                self.rewrite()
                self.rewrites += 1
            finally:
                history._OBSERVED.reset(token)
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
        # Placed records, so admission reads each one's Governor journal too.
        place = {"repository": None, "repo_key": repository_context(self.project)["repo_key"],
                 "path": str(self.project), "branch": "main"}
        self.intake(objective="healthy", worktree=place)
        # Large enough that its checkpoint is a page of its own, read lazily after context.json.
        self.intake(*self.peer_rows(), objective="peer", next_safe_action="x" * 1024, worktree=place)
        self.peer = ledger.objective_root(self.project, "peer")
        self.run = ledger.read(self.project, "peer")["checkpoint"]["native_refs"][0]["runId"]
        self.generation = 0

    def peer_rows(self):
        return []

    def publish(self, mutate, directory=None):
        """A writer publishes a record's next revision through Pod's one publication path."""
        path = (directory or self.peer) / "context.json"
        state = plain(history.load_context(path, validate=False))
        state["revision"] += 1
        mutate(state)
        history.publish_context(path, *history.prepare_context(path, state, project=self.project))

    def collect(self):
        """A collection reclaims the pages the earlier revision used. The scan's read pin would defer it; the
        injection runs it anyway, as a reader outside the pin would meet it."""
        with patch.object(history.fcntl, "flock"):
            collected = history.ChunkStore(self.peer).collect()
        self.assertEqual(collected["status"], "collected")

    def rewrite(self):
        self.generation += 1
        self.publish(lambda state: state["checkpoint"].update(
            next_safe_action=chr(ord("a") + self.generation % 26) * 1024))
        self.collect()

    def interleaved(self, scan, times, **injection):
        with Interleaving(self, times, **injection) as interleaving:
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


SOURCE = {"schema": "pod-issue-source/v1", "repository": "acme/example", "number": 82,
          "locator": "https://github.com/acme/example/issues/82", "body_sha256": "a" * 64, "amendments": []}


def page(store, physical, *keys):
    """The address of the page a record's logical path names, read with the unpatched store."""
    reference = physical["root"]["ref"] if keys and keys[0] != "facts" else physical["facts"]["ref"]
    for key in keys[1:] if keys and keys[0] == "facts" else keys:
        value = json.loads(ORIGINAL_LOAD(store, reference["sha256"]))
        assert value["level"] == 0 and value["kind"] == "dict", value["level"]
        reference = dict(value["entries"])[key]["ref"]
    return reference["sha256"]


class LateCase(ScanCase):
    """The peer's obligations have a page of their own that a scan first reads late, inside a classification that
    catches the fault and falls back; the rewrite changes exactly that page."""

    def setUp(self):
        super().setUp()
        self.late = self.obligations_page()
        self.also = lambda state: None  # What else the late rewrite changes in the same publication.

    def peer_rows(self):
        return [self.sub(f"S{index}", check=f"row-{index}" * 80) for index in range(20)]

    def obligations_page(self):
        return page(history.ChunkStore(self.peer), json.loads((self.peer / "context.json").read_bytes()),
                    "checkpoint", "obligations")

    def rewrite_obligations(self):
        self.generation += 1
        def mutate(state):
            state["checkpoint"]["obligations"][-1].update(check=f"changed-{self.generation}")
            self.also(state)
        self.publish(mutate)
        self.collect()
        self.late = self.obligations_page()

    def late_read(self, scan, times):
        return self.interleaved(scan, times, target=lambda: self.late, rewrite=self.rewrite_obligations)


class LateDescendantTests(LateCase):
    def test_the_list_rereads_a_late_page_its_schema_check_caught(self):
        listed, rewrites = self.late_read(lambda: listing.objective_list(self.project), 1)
        self.assertEqual(rewrites, 1)
        peer = next(row for row in listed["objectives"] if row["objective"] == "peer")
        self.assertEqual((peer["schema"], listed["complete"], listed["changing_records"]), ("current", True, []))

    def test_the_list_names_a_record_whose_late_page_keeps_changing(self):
        listed, rewrites = self.late_read(lambda: listing.objective_list(self.project), 12)
        self.assertGreaterEqual(rewrites, 1 + history.SCAN_RETRIES)
        self.assertEqual([row["objective"] for row in listed["objectives"]], ["healthy"])  # Never `unsupported`.
        self.assertEqual(([row["code"] for row in listed["changing_records"]], listed["unreadable_records"]),
                         ([history.RECORD_CHANGED], []))
        self.assertFalse(listed["complete"])

    def test_run_scope_holds_a_record_whose_late_page_keeps_changing(self):
        scope, rewrites = self.late_read(lambda: run_scope(self.project, "healthy", self.run), 1)
        self.assertEqual((rewrites, scope["objectives"], scope["unreadable"], scope.get("changing")),
                         (1, ["peer"], 0, None))
        scope, _ = self.late_read(lambda: run_scope(self.project, "healthy", self.run), 12)
        self.assertEqual((scope["objectives"], scope["unreadable"], scope["changing"]), ([], 0, 1))

    def recovery(self, times):
        """R146's chain: the peer shares the healthy objective's source and Run, so it is a recovery member."""
        for directory in (ledger.objective_root(self.project, "healthy"), self.peer):
            self.publish(lambda state: state["checkpoint"].update(objective_source=dict(SOURCE)), directory)
        self.late = self.obligations_page()
        raw = ledger.read(self.project, "healthy")
        proof, scan = evidence_closure._Proof("healthy"), {}

        def records():
            if not scan:
                scan["found"], scan["unreadable"] = evidence_closure._scan(self.project)
            return scan["found"], scan["unreadable"]
        doc = evidence_closure._documented(raw, current=True)
        members, rewrites = self.late_read(lambda: evidence_closure._members(
            self.project, "healthy", raw, "current", doc, proof, records), times)
        return members, proof.result(), rewrites

    def test_a_recovery_member_read_after_the_scan_is_read_again(self):
        members, verdict, rewrites = self.recovery(1)
        self.assertEqual(rewrites, 1)  # Fired only when the members read classified the scanned peer.
        self.assertEqual(sorted(row["objective"] for row in members), ["healthy", "peer"])
        self.assertEqual(verdict["conditions"], [])

    def moved(self, move):
        """The membership read sees issue 82 and the shared Run; the rebuild, met during classification, has
        moved: the peer is judged again from its rebuilt record and left out of the chain."""
        self.also = move
        members, verdict, rewrites = self.recovery(1)
        self.assertEqual(rewrites, 1)
        self.assertEqual([row["objective"] for row in members], ["healthy"])
        self.assertEqual(verdict["conditions"], [])
        raw = ledger.read(self.project, "healthy")
        chain = evidence_closure._chain_issues("healthy", evidence_closure._documented(raw, current=True), members)
        self.assertEqual([issue["number"] for issue, _ in chain], [82])

    def test_a_peer_rebuilt_onto_another_source_is_judged_again(self):
        self.moved(lambda state: state["checkpoint"].update(objective_source={
            **SOURCE, "number": 83, "locator": "https://github.com/acme/example/issues/83"}))

    def test_a_peer_rebuilt_onto_another_run_is_judged_again(self):
        self.moved(lambda state: state["checkpoint"]["native_refs"][0].update(runId="run-elsewhere"))

    def test_a_recovery_member_that_keeps_changing_holds_with_the_transient_cause(self):
        members, verdict, rewrites = self.recovery(12)
        self.assertGreaterEqual(rewrites, 1 + history.SCAN_RETRIES)
        self.assertIsNone(members)
        self.assertEqual([row["code"] for row in verdict["conditions"]], [history.RECORD_CHANGED])


class JournalTests(ScanCase):
    """Admission reads the peer's Governor journal without its lock; a journal rewrite changes no context.json."""

    def setUp(self):
        super().setUp()
        self.journal = self.peer / "governor.json"
        governor._publish_journal(self.journal, governor._empty_journal())
        self.facts = self.facts_page()

    def facts_page(self):
        return json.loads(self.journal.read_bytes())["facts"]["ref"]["sha256"]

    def rewrite_journal(self):
        journal = plain(governor._read_journal(self.journal))
        journal["revision"] += 1
        governor._publish_journal(self.journal, journal)
        self.collect()
        self.facts = self.facts_page()

    def peer_unknowns(self, result):
        return [(row["code"], row["reconciliation"].get("action")) for row in result["unknowns"]
                if row["record"] == self.peer.name]

    def journal_read(self, times):
        context = (self.peer / "context.json").read_bytes()
        result, rewrites = self.interleaved(lambda: admission.query(self.project), times,
                                            target=lambda: self.facts, rewrite=self.rewrite_journal)
        self.assertEqual((self.peer / "context.json").read_bytes(), context)  # Only the journal changed.
        return result, rewrites

    def test_a_journal_rewritten_during_the_read_is_read_again(self):
        self.assertEqual(self.peer_unknowns(admission.query(self.project)), [])
        result, rewrites = self.journal_read(1)
        self.assertEqual((rewrites, self.peer_unknowns(result)), (1, []))

    def test_a_journal_that_keeps_changing_is_transient_never_unreadable(self):
        result, rewrites = self.journal_read(12)
        self.assertGreaterEqual(rewrites, 1 + history.SCAN_RETRIES)
        self.assertEqual(self.peer_unknowns(result), [(history.RECORD_CHANGED, "repeat_query")])


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
