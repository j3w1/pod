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


def _legacy_base64(peer):
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


# shape: (arrange, claim of TARGET held, Run held, Run-bound lookup held). The Run-bound status lookup keeps its
# skip of a record on an unsupported schema; an undecodable one holds it as unknown membership.
SHAPES = {
    "malformed-literal": (lambda peer: (peer / "context.json").write_bytes(
        b'{"continued_by":{"objective":"target-work"},"run_id":"run_12345", bad'), True, True, True),
    "malformed-escaped": (lambda peer: (peer / "context.json").write_bytes(
        b'{"continued_by":{"objective":"target-\\u0077ork"},"run_id":"r\\u0075n_12345", bad'), True, True, True),
    "split-chunks": (_split_chunks, False, True, False),
    "legacy-base64": (_legacy_base64, True, True, False),
    "over-bound": (lambda peer: (peer / "context.json").write_text(json.dumps(_nested(80, "elsewhere"))),
                   True, True, False),
    "decoded-escaped-names": (lambda peer: (peer / "context.json").write_bytes(
        b'["target-\\u0077ork", "r\\u0075n_12345"]'), True, True, False),
    "decoded-names-nothing": (lambda peer: (peer / "context.json").write_text('["elsewhere"]'), False, False, False),
}


class MembershipTests(KernelCase):
    def setUp(self):
        super().setUp()
        self.intake(objective=TARGET)
        self.root = ledger.state_root(self.project)

    def consumers(self, peer):
        """Every consumer's answer for one unreadable record beside the healthy objective."""
        found, unreadable = retirement._claimants(self.project, TARGET)
        self.assertEqual((found, unreadable), ([], [str(peer)]))
        claim = bool(retirement.could_claim(self.project, unreadable, TARGET))
        incoming = incoming_claim(self.project, TARGET, ledger.read(self.project, TARGET), full=True)
        self.assertEqual(claim, incoming is not None and incoming.get("unreadable") == [str(peer)])
        proof = evidence_closure._could_name(self.project, [{"record": peer.name}], RUN)
        scope = run_scope(self.project, TARGET, RUN)["unreadable"] == 1
        try:
            ledger.contexts_for_run(RUN)
            lookup = False
        except PodError:
            lookup = True
        self.assertEqual(proof, scope)
        return claim, scope, lookup

    def test_each_record_shape_holds_only_what_it_could_name(self):
        for shape, (arrange, claim, run, lookup) in SHAPES.items():
            peer = self.root / "zz-peer"
            with self.subTest(shape=shape):
                peer.mkdir()
                try:
                    arrange(peer)
                    self.assertEqual(self.consumers(peer), (claim, run, lookup))
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
        self.assertEqual(retirement._claimants(self.project, "healthy"), ([], [str(path.parent)]))
        self.assertEqual(run_scope(self.project, "healthy", "unrelated-run")["unreadable"], 1)  # Unknown: it holds.
        with patch.dict(os.environ):
            os.environ.pop("ORCA_TERMINAL_HANDLE", None)
            listed = listing.objective_list(self.project)
        self.assertEqual([row["objective"] for row in listed["objectives"]], ["healthy"])
        self.assertEqual((listed["complete"], listed["unreadable_records"][0]["phase"]), (False, "unavailable"))


if __name__ == "__main__":
    unittest.main()
