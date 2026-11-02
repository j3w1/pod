"""R206: an unreadable record holds only what its decoded logical record names, or everything when it cannot be
decoded within Pod's read bounds; a record's lazy reads never abort the scan of other objectives."""
import base64
import hashlib
import json
import os
import shutil
import unittest
from unittest.mock import patch

from pod import evidence_closure, history, ledger, listing, retirement
from pod.errors import PodError
from pod.handoff import record_mentions, run_scope
from pod.history_tree import plain
from pod.succession import incoming_claim
from tests.kernel_support import KernelCase

TARGET, RUN = "target-work", "run_12345"


def _split_chunks(peer):
    """A JSON string naming the Run, split across two production reference chunks, behind an unsupported primary."""
    payload = b'"' + b"x" * (history.PART_BYTES - 5) + RUN.encode() + b'"'
    chunks = {}
    reference = history._reference(json.loads(payload), chunks)
    history.ChunkStore(peer).put(chunks)
    assert all(RUN.encode() not in part for part in chunks.values())
    (peer / "context.json").write_text(json.dumps(["unsupported", reference]))


def _preceding_base64(peer):
    """The preceding base64 chunk format, naming the objective and the Run, behind a primary that names neither."""
    payload = history._bytes({"objective": TARGET, "run_id": RUN})
    wrapped = history._bytes({"schema": "pod-history-chunk/v1", "data": base64.b64encode(payload).decode()})
    address = hashlib.sha256(wrapped).hexdigest()
    history.ChunkStore(peer).put({address: wrapped})
    reference = {history.MARKER: {"sha256": hashlib.sha256(payload).hexdigest(), "parts": [address],
                                  "bytes": len(payload)}}
    (peer / "context.json").write_text(json.dumps(["unsupported", reference]))


def _nested(depth, leaf):
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


# shape: (arrange, claim of TARGET held, Run held). Every consumer of a Run, the Run-bound status lookup included,
# agrees: a record holds it when its decoded record names the Run or cannot be decoded within bounds.
SHAPES = {
    "malformed-literal": (lambda peer: (peer / "context.json").write_bytes(
        b'{"continued_by":{"objective":"target-work"},"run_id":"run_12345", bad'), True, True),
    "malformed-escaped": (lambda peer: (peer / "context.json").write_bytes(
        b'{"continued_by":{"objective":"target-\\u0077ork"},"run_id":"r\\u0075n_12345", bad'), True, True),
    "split-chunks": (_split_chunks, False, True),
    "preceding-base64": (_preceding_base64, True, True),
    "over-bound": (lambda peer: (peer / "context.json").write_text(json.dumps(_nested(80, "elsewhere"))),
                   True, True),
    "decoded-escaped-names": (lambda peer: (peer / "context.json").write_bytes(
        b'["target-\\u0077ork", "r\\u0075n_12345"]'), True, True),
    "decoded-names-nothing": (lambda peer: (peer / "context.json").write_text('["elsewhere"]'), False, False),
    "unsupported-names-run": (lambda peer: (peer / "context.json").write_text(json.dumps(
        {"schema": "pod-context/v99", "checkpoint": {"objective": "peer", "note": "resume " + RUN}})), False, True),
    "unsupported-names-nothing": (lambda peer: (peer / "context.json").write_text(json.dumps(
        {"schema": "pod-context/v99", "checkpoint": {"objective": "peer", "note": "elsewhere"}})), False, False),
}


class MembershipTests(KernelCase):
    def setUp(self):
        super().setUp()
        self.intake(objective=TARGET)
        self.root = ledger.state_root(self.project)

    def consumers(self, peer):
        """Every consumer's answer for one unreadable record beside the healthy objective."""
        found, unreadable, changing = retirement._claimants(self.project, TARGET)
        self.assertEqual((found, changing), ([], []))
        self.assertIn(unreadable, ([], [str(peer)]))  # A readable record of any schema is scanned for claims.
        claim = bool(retirement.could_claim(self.project, unreadable, TARGET))
        incoming = incoming_claim(self.project, TARGET, ledger.read(self.project, TARGET), full=True)
        self.assertEqual(claim, incoming is not None and incoming.get("unreadable") == [str(peer)])
        proof = evidence_closure._could_name(self.project, [{"record": peer.name}], RUN)
        scope = run_scope(self.project, TARGET, RUN)
        scope = scope["unreadable"] == 1 or bool(scope["objectives"])
        try:
            ledger.contexts_for_run(RUN)
            lookup = False
        except PodError:
            lookup = True
        self.assertEqual(len({proof, scope, lookup}), 1, (proof, scope, lookup))
        return claim, scope

    def test_each_record_shape_holds_only_what_it_could_name(self):
        for shape, (arrange, claim, run) in SHAPES.items():
            peer = self.root / "zz-peer"
            with self.subTest(shape=shape):
                peer.mkdir()
                try:
                    arrange(peer)
                    self.assertEqual(self.consumers(peer), (claim, run))
                finally:
                    shutil.rmtree(peer)

    def test_an_outer_deadline_still_refuses_on_its_own(self):
        peer = self.root / "zz-peer"
        peer.mkdir()
        (peer / "context.json").write_text('["elsewhere"]')
        with patch("pod.history.load_context", side_effect=PodError("discovery_budget", "spent")), \
                self.assertRaises(PodError) as caught:
            record_mentions(peer, RUN)
        self.assertEqual(caught.exception.code, "discovery_budget")


class RunLookupStatusTests(KernelCase):
    """A record that names the selected Run holds `pod status --run`; a superseded one reads as its own condition."""

    def peer(self, context, checkpoint):
        record = ledger.state_root(self.project) / "earlier" / "context.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"schema": context, "revision": 1, "owner": "owner",
                                      "checkpoint": {"schema": checkpoint, "objective": "earlier",
                                                     "native_refs": [{"runId": RUN, "runtime": "runtime"}]},
                                      "admissions": {}, "interventions": {}, "source_rejections": {},
                                      "constraints": []}))

    def run_status(self):
        from pod.status import status
        return status(self.project, RUN)

    def test_a_superseded_record_reads_objective_superseded(self):
        self.peer("pod-context/v4", "pod-checkpoint/v2")
        shown = self.run_status()
        self.assertEqual((shown["status"], shown["blocker"]), ("blocked", "objective_superseded"))
        self.assertIn("start a new objective", shown["next_safe_action"])

    def current_unsupported(self):
        """A current-schema record that does not validate, naming the Run."""
        from pod.installer import STATE_SCHEMAS
        record = ledger.state_root(self.project) / "current-broken" / "context.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"schema": STATE_SCHEMAS["context"], "owner": 7,
                                      "checkpoint": {"schema": STATE_SCHEMAS["checkpoint"], "objective": "broken",
                                                     "native_refs": [{"runId": RUN, "runtime": "runtime"}]},
                                      "admissions": {}}))

    def test_a_mixed_hold_keeps_its_code_and_names_every_record(self):
        # Reviewer probe 1: an unrelated superseded peer never relabels a current record's refusal.
        self.peer("pod-context/v4", "pod-checkpoint/v2")
        self.current_unsupported()
        shown = self.run_status()
        self.assertEqual(shown["status"], "blocked")
        self.assertNotEqual(shown["blocker"], "objective_superseded")
        self.assertNotIn("start a new objective", shown["next_safe_action"])
        self.assertEqual(sorted((row["record"], row["class"]) for row in shown["held_by"]),
                         [("current-broken", "unsupported"), ("earlier", "superseded")])

    def test_the_condition_comes_from_the_same_read(self):
        # Reviewer probe 2: status reads nothing again, so a peer removed afterwards cannot clear the hold.
        self.peer("pod-context/v4", "pod-checkpoint/v2")
        with patch("pod.ledger.superseded_for_run", side_effect=AssertionError("second read")):
            shown = self.run_status()
        self.assertEqual((shown["status"], shown["blocker"]), ("blocked", "objective_superseded"))
        self.assertEqual([row["record"] for row in shown["superseded"]], ["earlier"])

    def test_a_non_object_or_schema_less_record_is_never_superseded(self):
        # Delta probe 1: only R90's superseded schema family reads objective_superseded.
        for text in (json.dumps([RUN]), json.dumps(RUN), json.dumps({"checkpoint": {"native_refs": [{"runId": RUN}]}})):
            record = ledger.state_root(self.project) / "earlier" / "context.json"
            with self.subTest(text=text):
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text(text)
                shown = self.run_status()
                self.assertEqual(shown["status"], "blocked")
                self.assertNotEqual(shown["blocker"], "objective_superseded")
                self.assertEqual([(row["record"], row["class"]) for row in shown["held_by"]], [("earlier", "unsupported")])

    def test_a_missing_page_while_classifying_keeps_the_hold_and_its_reason(self):
        # Delta probe 2: a production-writer record that names the Run loses its checkpoint page.
        self.intake(objective="peer", next_safe_action="x" * 1024)  # Large enough for its own checkpoint page.
        run = ledger.read(self.project, "peer")["checkpoint"]["native_refs"][0]["runId"]
        path = ledger.objective_root(self.project, "peer") / "context.json"
        physical, chunks = history.prepare_context(path, plain(ledger.read(self.project, "peer")), project=self.project)
        history.ChunkStore(path.parent).put(chunks)
        path.write_bytes(history._bytes(physical))
        page = json.loads(chunks[physical["root"]["ref"]["sha256"]])
        entry = next(entry for entry in page["entries"] if entry[0] == "checkpoint")
        (path.parent / "history" / "sha256" / (entry[1]["ref"]["sha256"] + ".json")).unlink()
        from pod.status import status
        shown = status(self.project, run)
        self.assertEqual(shown["status"], "blocked")
        self.assertNotEqual(shown["blocker"], "objective_superseded")
        self.assertEqual([(row["record"], row["class"], row["reason"]) for row in shown["held_by"]],
                         [(path.parent.name, "undecodable", "history_missing")])

    def test_an_undecodable_record_that_names_the_run_still_holds(self):
        record = ledger.state_root(self.project) / "earlier" / "context.json"
        record.parent.mkdir(parents=True)
        record.write_text('{"native_refs": [{"runId": "%s"}], ' % RUN)  # Not in the superseded inventory.
        shown = self.run_status()
        self.assertEqual((shown["status"], shown["blocker"]), ("blocked", "invalid_record"))


class ListDeadlineTests(KernelCase):
    def test_an_outer_deadline_at_the_first_record_read_refuses_the_whole_list(self):
        self.intake(objective=TARGET)
        with patch.dict(os.environ), patch("pod.listing.load_context",
                                           side_effect=PodError("discovery_budget", "spent")), \
                self.assertRaises(PodError) as caught:
            os.environ.pop("ORCA_TERMINAL_HANDLE", None)
            listing.objective_list(self.project)
        self.assertEqual(caught.exception.code, "discovery_budget")


class LazyPageTests(KernelCase):
    def test_a_missing_descendant_page_stays_inside_its_record(self):
        # The reviewer's probe: a valid current record from the production writer loses one checkpoint page.
        self.intake(objective="healthy")
        self.intake(objective="peer", next_safe_action="x" * 1024)
        path = ledger.objective_root(self.project, "peer") / "context.json"
        physical, chunks = history.prepare_context(path, plain(ledger.read(self.project, "peer")), project=self.project)
        history.ChunkStore(path.parent).put(chunks)
        path.write_bytes(history._bytes(physical))
        page = json.loads(chunks[physical["root"]["ref"]["sha256"]])
        entry = next(entry for entry in page["entries"] if entry[0] == "checkpoint")
        (path.parent / "history" / "sha256" / (entry[1]["ref"]["sha256"] + ".json")).unlink()
        found, unreadable = evidence_closure._scan(self.project)
        self.assertEqual([row["objective"] for row in found], ["healthy"])
        self.assertEqual([row["record"] for row in unreadable], [path.parent.name])
        self.assertEqual(retirement._claimants(self.project, "healthy"), ([], [str(path.parent)], []))
        self.assertEqual(run_scope(self.project, "healthy", "unrelated-run")["unreadable"], 1)  # Unknown: it holds.
        with patch.dict(os.environ):
            os.environ.pop("ORCA_TERMINAL_HANDLE", None)
            listed = listing.objective_list(self.project)
        self.assertEqual([row["objective"] for row in listed["objectives"]], ["healthy"])
        self.assertEqual((listed["complete"], listed["unreadable_records"][0]["phase"]), (False, "unavailable"))


if __name__ == "__main__":
    unittest.main()
