"""R203 decider-attested rebind across a governance change; R166 undated earlier decisions; R164 question writes.

Real-record regressions, sanitized in fixtures/governance-delta-rebind/real-shapes.json.
"""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path

from pod import config, ledger
from pod.errors import PodError
from pod.ledger import objective_root, read
from pod.records import source_identity
from tests.decision_policy_support import LOCAL, PERSON
from tests.issue41_support import ProductionCase
from pod.config import effective
from tests.common import proof
from tests.kernel_support import (AGENTS, KernelCase, assurance_review, git, governance_attestation, governance_commit,
                                  snapshot)

SHAPES = json.loads((Path(__file__).parent / "fixtures" / "governance-delta-rebind" / "real-shapes.json").read_text())
CHANGE = SHAPES["governance_change"]
ADDED = "\n".join(CHANGE["paragraph"]) + "\n"
SUBGOALS = [f"S{index}" for index in range(1, 9)]
EXTERNAL = {"party": "third_party", "need": "an outside event", "unblocks_when": "the event arrives"}
def satisfy(case, rows, skip=("assurance",)):
    """Satisfy rows with short PASS receipts, reading the record once rather than once per row."""
    revision = effective(case.project)["revision"]
    for row in rows:
        if row["kind"] not in skip:
            row.pop("executor", None); row.pop("wait", None)
            row.update(state="satisfied", evidence=[proof(row["id"], case.candidate, definition=row, policy_revision=revision)])
    return rows


REASON = "The added paragraph names a final check before a pull request; no rule these rows were proved under changed."


class DeltaCase(KernelCase):
    """Ten satisfied rows (more than the eight the old refusal listed): a criterion, eight subgoals with short
    receipts and a review that read AGENTS.md as bound context; then the target gains one added paragraph."""

    review = assurance_review
    commit = governance_commit

    def setUp(self):
        super().setUp()
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        assurance = {"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                     "candidate": self.base, "existing_evidence": "tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        self.write([self.criterion(), *[self.sub(key, boundary={"paths": ["src"], "surfaces": []})
                                       for key in SUBGOALS], assurance],
                   governance={"base_ref": "target"}, **self.intake_fields())
        self.review(sources=[source_identity(self.project, "AGENTS.md")])
        self.write(satisfy(self, self.stored()))
        self.bound = deepcopy(read(self.project, "objective")["checkpoint"])
        self.target = self.move_target(AGENTS + ADDED)

    def intake_fields(self):
        return {}

    def move_target(self, text, path="AGENTS.md"):
        git(self.project, "switch", "-q", "target")
        (self.project / path).parent.mkdir(parents=True, exist_ok=True)
        target = self.commit(path, text, "governance change")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        git(self.project, "switch", "-q", "main")
        return target

    def satisfied(self):
        return [row["id"] for row in self.stored() if row["state"] == "satisfied"]

    def refresh(self, **fields):
        return self.write(self.stored(), governance_refresh=True, **fields)

    def attested(self, **fields):
        return self.refresh(governance=snapshot(self.project), revision_authority=LOCAL, rebind=self.satisfied(),
                            governance_delta=governance_attestation(self.project, reason=REASON), **fields)

    def record(self):
        return (objective_root(self.project, "objective") / "context.json").read_bytes()

    def refused_unchanged(self, detail, call, *args, **kwargs):
        before = self.record()
        error = self.refused("obligation_unaccounted", detail, call, *args, **kwargs)
        self.assertEqual(self.record(), before)
        return error


class DiagnosticsTests(DeltaCase):
    def test_plain_refresh_lists_every_rebindable_row_and_the_exact_delta(self):
        # The real refusal listed eight ids in map order and said "ineligible; record fresh evidence".
        error = self.refused_unchanged("evidence_invalidated", self.refresh)
        referent = error.detail["referent"]
        self.assertEqual(sorted(referent["eligible_rebind"].split(",")), sorted(self.satisfied()))
        self.assertGreater(len(self.satisfied()), 8)
        self.assertEqual(referent["eligible_rebind_remaining"], 0)
        blob = lambda commit: git(self.project, "rev-parse", f"{commit}:AGENTS.md")
        self.assertEqual(referent["governance_delta"], [{"path": "AGENTS.md", "bound": blob(self.base),
                                                         "current": blob(self.target), "added": CHANGE["added"],
                                                         "removed": CHANGE["removed"]}])
        self.assertTrue(all(item["eligibility"] == "rebindable" and "fresh evidence" not in item["correction"]
                            for item in referent["invalidations"]))
        self.assertIn("governance_delta", error.detail["next_action"])
        self.assertIn("revision_authority", error.detail["next_action"])


class AttestedRebindTests(DeltaCase):
    def test_an_unattested_rebind_across_changed_sources_is_refused(self):
        # Before R203 this exact write (every satisfied row named in rebind) was accepted with no delta check.
        error = self.refused_unchanged("rebind_refused", self.refresh, governance=snapshot(self.project),
                                       revision_authority=LOCAL, rebind=self.satisfied())
        self.assertEqual(error.detail["referent"]["predicate"], "rebind_unattested")
        self.assertEqual(error.detail["referent"]["governance_delta"],
                         governance_attestation(self.project)["sources"])

    def test_attested_rebind_keeps_receipts_and_records_delta_reason_and_authority(self):
        before = {row["id"]: row for row in self.bound["obligations"]}
        self.attested()
        stored = read(self.project, "objective")["checkpoint"]
        self.assertEqual(stored["governance"]["base"], self.target)
        strip = lambda items: [{key: value for key, value in item["evidence"].items() if key != "governance"} for item in items]
        for row in stored["obligations"]:
            self.assertEqual(row["state"], "satisfied")
            self.assertEqual(strip(row["receipts"]), strip(before[row["id"]]["receipts"]))
        decision = stored["governance_history"]["decisions"][-1]
        self.assertEqual((decision["seq"], decision["kind"], decision["provenance"], decision["channel"]),
                         (stored["seq"], "snapshot", "agent_decision", "agent_session"))
        blob = lambda commit: git(self.project, "rev-parse", f"{commit}:AGENTS.md")
        self.assertEqual(decision["rebind"], {
            "rows": sorted(self.satisfied()), "reason": REASON, "bound_context": ["A"],
            "delta": [{"path": "AGENTS.md", "bound": blob(self.base), "current": blob(self.target),
                       "added": CHANGE["added"], "removed": CHANGE["removed"]}]})
        # Status shows it plainly, in JSON and text.
        from pod.status import render, status
        shown = status(self.project, None, objective="objective", current_run_fn=lambda: {"run": None})
        rows = [row for row in shown["owner_decisions"] if row["kind"] == "governance"]
        self.assertEqual(rows[-1]["decision"]["rebind"]["reason"], REASON)
        text = StringIO()
        with redirect_stdout(text):
            render(shown)
        self.assertIn(f"Governance rebind at seq {stored['seq']}: 10 rows across AGENTS.md +11 -0", text.getvalue())

    def test_edited_and_removed_lines_are_recorded_not_refused(self):
        text = AGENTS.replace("Docs are optional.\n", "").replace("Reviews bind", "Reviews always bind") + ADDED
        self.target = self.move_target(text)
        self.attested()
        delta = read(self.project, "objective")["checkpoint"]["governance_history"]["decisions"][-1]["rebind"]["delta"]
        self.assertEqual([(entry["path"], entry["removed"]) for entry in delta], [("AGENTS.md", 2)])

    def test_the_declared_delta_must_equal_the_delta_pod_reads(self):
        declared = governance_attestation(self.project, reason=REASON)
        declared["sources"][0]["added"] += 1
        error = self.refused_unchanged("rebind_refused", self.refresh, governance=snapshot(self.project),
                                       revision_authority=LOCAL, rebind=self.satisfied(), governance_delta=declared)
        self.assertEqual(error.detail["referent"]["predicate"], "rebind_delta_mismatch")

    def test_an_attested_rebind_is_a_snapshot_decision_by_the_policy_decider(self):
        attestation = governance_attestation(self.project, reason=REASON)
        for fields in ({}, {"revision_authority": LOCAL}):
            with self.subTest(fields=sorted(fields)):
                error = self.refused_unchanged("rebind_refused", self.refresh, rebind=self.satisfied(),
                                               governance_delta=attestation, **fields)
                self.assertEqual(error.detail["referent"]["predicate"], "rebind_needs_snapshot_decision")
        config.set_decision_policy(self.project, "careful", decision=PERSON)
        before = self.record()
        with self.assertRaises(PodError) as caught:
            self.attested()
        self.assertEqual(caught.exception.code, "decision_policy_denied")
        self.assertEqual(self.record(), before)

    def test_a_pod_policy_source_change_refuses(self):
        self.target = self.move_target("schema: pod/v1\n", path=".pod/config.yaml")
        error = self.refused_unchanged("rebind_refused", self.refresh, governance=snapshot(self.project),
                                       revision_authority=LOCAL, rebind=self.satisfied())
        self.assertIn(".pod/config.yaml", str(error))
        self.assertEqual(error.detail["referent"]["predicate"], "rebind_row_blocked")

    def test_a_changed_row_definition_stays_invalid(self):
        rows = self.stored()
        next(row for row in rows if row["id"] == "S1")["check"] = "S1 is delivered differently"
        error = self.refused_unchanged("evidence_invalidated", self.write, rows, governance_refresh=True,
                                       governance=snapshot(self.project), revision_authority=LOCAL,
                                       rebind=self.satisfied(), governance_delta=governance_attestation(self.project, reason=REASON))
        entries = {item["obligation"]: item for item in error.detail["referent"]["invalidations"]}
        self.assertEqual(entries["S1"]["classification"], "definition change")

    def test_an_empty_delta_rebinds_without_attestation(self):
        # The target moves without touching a governance source; a refresh still moves the base and the stamps.
        git(self.project, "switch", "-q", "target")
        git(self.project, "reset", "-q", "--hard", self.base)
        git(self.project, "switch", "-q", "main")
        self.target = self.move_target("notes\n", path="docs/notes.md")
        self.refresh(rebind=self.satisfied())
        stored = read(self.project, "objective")["checkpoint"]
        self.assertEqual(stored["governance"]["base"], self.target)
        self.assertTrue(all(row["state"] == "satisfied" for row in stored["obligations"]))


class ScopedRowTests(KernelCase):
    """A row whose proof scope names AGENTS.md, beside a review that only read it as bound context."""

    review = assurance_review
    commit = governance_commit

    def setUp(self):
        super().setUp()
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        assurance = {"id": "A", "kind": "assurance", "provenance": "coordinator", "parent": "O1",
                     "check": "independent review", "scope": {"paths": ["src"]}, "question": "is src right?",
                     "candidate": self.base, "existing_evidence": "tests", "insufficiency": "no review",
                     "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}
        policy = self.sub("POLICY", proof_scope=["AGENTS.md"], boundary={"paths": ["AGENTS.md"], "surfaces": []})
        self.write([self.criterion(), policy, assurance], governance={"base_ref": "target"})
        self.review(sources=[source_identity(self.project, "AGENTS.md")])
        self.write(satisfy(self, self.stored()))
        git(self.project, "switch", "-q", "target")
        target = self.commit("AGENTS.md", AGENTS + ADDED, "governance change")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        git(self.project, "switch", "-q", "main")

    def attestation(self):
        return dict(governance=snapshot(self.project), revision_authority=LOCAL,
                    governance_delta=governance_attestation(self.project, reason=REASON))

    def test_a_row_whose_scope_names_the_changed_source_refuses(self):
        satisfied = [row["id"] for row in self.stored() if row["state"] == "satisfied"]
        before = (objective_root(self.project, "objective") / "context.json").read_bytes()
        error = self.refused("obligation_unaccounted", "rebind_refused", self.write, self.stored(),
                             governance_refresh=True, rebind=satisfied, **self.attestation())
        self.assertEqual(error.detail["referent"]["obligation"], "POLICY")
        self.assertIn("proof scope includes the changed governance source AGENTS.md", str(error))
        self.assertEqual((objective_root(self.project, "objective") / "context.json").read_bytes(), before)
        # The plain refresh's diagnostics agree: POLICY ineligible with the reason, the review rebindable.
        error = self.refused("obligation_unaccounted", "evidence_invalidated", self.write, self.stored(),
                             governance_refresh=True)
        entries = {item["obligation"]: item for item in error.detail["referent"]["invalidations"]}
        self.assertEqual(entries["POLICY"]["eligibility"], "ineligible")
        self.assertIn("AGENTS.md", entries["POLICY"]["correction"])
        self.assertEqual(entries["A"]["eligibility"], "rebindable")

    def test_a_review_that_read_the_changed_source_rebinds_until_the_candidate_takes_it(self):
        rows = self.stored()
        policy = next(row for row in rows if row["id"] == "POLICY")
        policy.pop("evidence"); policy.update(state="blocked_external", external=EXTERNAL)
        self.write(rows, governance_refresh=True, rebind=["O1", "A"], **self.attestation())
        decision = read(self.project, "objective")["checkpoint"]["governance_history"]["decisions"][-1]
        self.assertEqual(decision["rebind"]["bound_context"], ["A"])
        # A candidate that later takes the new AGENTS.md fails the review's binding as before.
        (self.project / "AGENTS.md").write_text(AGENTS + ADDED)
        error = self.refused("obligation_unaccounted", "evidence_invalidated", self.write, self.stored())
        entries = {item["obligation"]: item for item in error.detail["referent"]["invalidations"]}
        self.assertEqual(entries["A"]["classification"], "binding change")


class LinkScopeTests(KernelCase):
    """A scope that reaches a changed governance file through a committed symlink is that file (review F1)."""

    commit = governance_commit

    def scoped(self, scope, links=(), gitlinks=()):
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        for name, target in links:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)
        if links or gitlinks:
            git(self.project, "add", "-A")
            for name in gitlinks:
                # A committed submodule entry (mode 160000); its subtree is another repository's, never read.
                (self.project / name).mkdir()
                git(self.project, "update-index", "--add", "--cacheinfo", f"160000,{self.base},{name}")
            git(self.project, "commit", "-qm", "links")
            self.base = self.candidate = git(self.project, "rev-parse", "HEAD")
            git(self.project, "branch", "-f", "target", self.base)
            git(self.project, "update-ref", "refs/remotes/origin/target", self.base)
        self.write([self.criterion(), self.sub("L", proof_scope=[scope], boundary={"paths": [scope], "surfaces": []})],
                   governance={"base_ref": "target"})
        self.write(satisfy(self, self.stored()))
        git(self.project, "switch", "-q", "target")
        target = self.commit("AGENTS.md", AGENTS + ADDED, "governance change")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        git(self.project, "switch", "-q", "main")
        before = (objective_root(self.project, "objective") / "context.json").read_bytes()
        try:
            self.write(self.stored(), governance_refresh=True, governance=snapshot(self.project),
                       revision_authority=LOCAL, rebind=["O1", "L"],
                       governance_delta=governance_attestation(self.project, reason=REASON))
        except PodError as exc:
            self.assertEqual((objective_root(self.project, "objective") / "context.json").read_bytes(), before)
            return exc
        return None

    def refused(self, scope, links, phrase, gitlinks=()):
        error = self.scoped(scope, links, gitlinks)
        self.assertIsNotNone(error, scope)
        self.assertEqual((error.detail["detail"], error.detail["referent"]["obligation"]), ("rebind_refused", "L"))
        self.assertIn(phrase, str(error))

    def test_a_file_link_to_the_changed_source_refuses(self):
        self.refused("rules-link", [("rules-link", "AGENTS.md")], "changed governance source AGENTS.md (through rules-link)")

    def test_a_parent_directory_link_refuses(self):
        self.refused("cfg/AGENTS.md", [("cfg", ".")], "changed governance source AGENTS.md (through cfg/AGENTS.md)")

    def test_a_link_loop_refuses(self):
        self.refused("loop-a", [("loop-a", "loop-b"), ("loop-b", "loop-a")], "does not resolve")

    def test_an_over_long_chain_refuses(self):
        chain = [(f"hop{index}", f"hop{index + 1}") for index in range(9)] + [("hop9", "AGENTS.md")]
        self.refused("hop0", chain, "does not resolve")

    def test_the_direct_spelling_still_refuses(self):
        self.refused("./AGENTS.md", [("code-link", "src/old.py")], "changed governance source AGENTS.md")

    def test_an_unrelated_link_rebinds(self):
        self.assertIsNone(self.scoped("code-link", [("code-link", "src/old.py")]))

    def test_a_link_inside_a_submodule_refuses(self):
        # The reviewed case: module/rules-link -> ../AGENTS.md lives in the submodule's own, opaque tree.
        self.refused("module/rules-link", [], "does not resolve", gitlinks=["module"])

    def test_a_scope_that_is_a_gitlink_or_lies_under_one_refuses(self):
        self.refused("module", [], "does not resolve", gitlinks=["module"])

    def test_a_link_into_a_submodule_refuses(self):
        self.refused("into", [("into", "module/src")], "does not resolve", gitlinks=["module"])

    def test_any_mode_outside_the_allow_list_is_unresolved(self):
        # Git normalizes modes on write, so the unknown mode is written as a literal, correctly hashed tree.
        import subprocess
        from pod.governance import scope_resolver

        def literal(data):
            return subprocess.run(["git", "-C", str(self.project), "hash-object", "-w", "-t", "tree", "--literally",
                                   "--stdin"], input=data, check=True, capture_output=True).stdout.decode().strip()
        old = bytes.fromhex(git(self.project, "rev-parse", "HEAD:src/old.py"))
        src = literal(b"100644 old.py\0" + old)
        top = literal(b"40000 src\0" + bytes.fromhex(src) + b"100664 weird\0" + old)
        commit = git(self.project, "commit-tree", top, "-m", "literal modes")
        resolve = scope_resolver(self.project, [commit])
        self.assertIsNone(resolve("weird"))
        self.assertIsNone(resolve("weird/below"))
        self.assertEqual(resolve("src/old.py"), ["src/old.py"])
        self.assertEqual(resolve("src/new.py"), ["src/new.py"])


class ExemptedAdoptionTests(KernelCase):
    """An attested rebind in the same write as an adoption through the first-parent exemption (R119 adoption-only):
    an unrecorded prefix changes governance and becomes the target, and the rebased candidate is its suffix."""

    commit = governance_commit

    def test_the_attested_rebind_carries_proof_through_an_exempted_adoption(self):
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        self.write([self.criterion(proof_scope=["README.md"]), self.sub("S1", proof_scope=["README.md"])],
                   governance={"base_ref": "target"})
        self.write(satisfy(self, self.stored()))
        git(self.project, "checkout", "-qb", "work")
        prefix = self.commit("AGENTS.md", "Changed target governance.\n", "unrecorded prefix")
        suffix = self.commit("src/old.py", "candidate work\n", "unrecorded suffix")
        git(self.project, "update-ref", "refs/remotes/origin/target", prefix)
        fields = dict(governance_refresh=True, governance=snapshot(self.project), candidate=suffix,
                      rebind=["O1", "S1"], reuse={"from": self.base, "ids": ["O1", "S1"]},
                      governance_delta=governance_attestation(self.project, reason=REASON))
        record = objective_root(self.project, "objective") / "context.json"
        before = record.read_bytes()
        # Without a policy-valid authority the adoption-only rule refuses first.
        error = self.refused("governance_unavailable", "governance_unavailable", self.write, self.stored(), **fields)
        self.assertEqual(error.detail["referent"]["reason"], "adoption_only")
        self.assertEqual(record.read_bytes(), before)
        # The decider's own exact snapshot decision adopts, moves the candidate and rebinds in one write.
        self.write(self.stored(), revision_authority=LOCAL, **fields)
        stored = read(self.project, "objective")["checkpoint"]
        self.assertEqual((stored["governance"]["base"], stored["candidate"]), (prefix, suffix))
        self.assertTrue(all(row["state"] == "satisfied" for row in stored["obligations"]))
        rebind = stored["governance_history"]["decisions"][-1]["rebind"]
        self.assertEqual((rebind["rows"], [entry["removed"] for entry in rebind["delta"]]), (["O1", "S1"], [5]))


class EarlierStepTests(KernelCase):
    """Proof bound under an earlier governance step that the record's own history names (its initial decision)."""

    review = assurance_review
    commit = governance_commit

    def setUp(self):
        super().setUp()
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        self.write([self.criterion(), self.sub("S1", boundary={"paths": ["src"], "surfaces": []})],
                   governance={"base_ref": "target"}, revision_authority=PERSON)
        self.write(satisfy(self, self.stored()))

    def step(self, line):
        git(self.project, "switch", "-q", "target")
        target = self.commit("AGENTS.md", (self.project / "AGENTS.md").read_text() + line, "governance step")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        git(self.project, "switch", "-q", "main")
        return target

    def test_a_restated_row_rebinds_later_across_both_steps(self):
        first = self.step("A first added rule.\n")
        rows = self.stored()
        s1 = next(row for row in rows if row["id"] == "S1")
        receipt = deepcopy(s1["receipts"][0]["evidence"])
        s1.pop("evidence"); s1.update(state="blocked_external", external=EXTERNAL)
        self.write(rows, governance_refresh=True, governance=snapshot(self.project), revision_authority=LOCAL,
                   rebind=["O1"], governance_delta=governance_attestation(self.project, reason=REASON))
        self.step("A second added rule.\n")
        rows = self.stored()
        s1 = next(row for row in rows if row["id"] == "S1")
        s1.pop("external"); s1.update(state="satisfied", evidence=[{key: value for key, value in receipt.items() if key != "governance"}])
        error = self.refused("obligation_unaccounted", "rebind_refused", self.write, rows, governance_refresh=True,
                             governance=snapshot(self.project), revision_authority=LOCAL, rebind=["O1", "S1"])
        delta = error.detail["referent"]["governance_delta"]
        # O1 crosses the second step from the first step's blob; S1 crosses both steps from the initial blob.
        blob = lambda commit: git(self.project, "rev-parse", f"{commit}:AGENTS.md")
        self.assertEqual(sorted((entry["bound"], entry["added"], entry["removed"]) for entry in delta),
                         sorted([(blob(self.base), 2, 0), (blob(first), 1, 0)]))
        self.write(rows, governance_refresh=True, governance=snapshot(self.project), revision_authority=LOCAL,
                   rebind=["O1", "S1"], governance_delta={"sources": delta, "reason": REASON})
        decision = read(self.project, "objective")["checkpoint"]["governance_history"]["decisions"][-1]
        self.assertEqual((decision["rebind"]["rows"], decision["rebind"]["earlier"]), (["O1", "S1"], ["S1"]))


class SizeTests(KernelCase):
    def test_a_realistic_attestation_fits_and_reads_back(self):
        # Thirty long row ids, two changed sources and a full-length reason.
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        (self.project / "CLAUDE.md").write_text("@AGENTS.md\n")
        git(self.project, "add", "CLAUDE.md"); git(self.project, "commit", "-qm", "claude")
        self.base = self.candidate = git(self.project, "rev-parse", "HEAD")
        git(self.project, "update-ref", "refs/remotes/origin/target", self.base)
        git(self.project, "branch", "-f", "target", self.base)
        keys = [f"fix-release-transaction-boundary-{index:02d}" for index in range(30)]
        self.write([self.criterion(), *[self.sub(key, boundary={"paths": ["src"], "surfaces": []}) for key in keys]],
                   governance={"base_ref": "target"})
        self.write(satisfy(self, self.stored()))
        git(self.project, "switch", "-q", "target")
        governance_commit(self, "AGENTS.md", AGENTS + ADDED, "rules")
        target = governance_commit(self, "CLAUDE.md", "@AGENTS.md\nRead the notes first.\n", "claude")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        git(self.project, "switch", "-q", "main")
        reason = ("R" * 1000 + " see evidence sha256:" + "0" * 4)[:1024]
        attestation = governance_attestation(self.project, reason=reason)
        self.assertEqual([entry["path"] for entry in attestation["sources"]], ["AGENTS.md", "CLAUDE.md"])
        self.write(self.stored(), governance_refresh=True, governance=snapshot(self.project), revision_authority=LOCAL,
                   rebind=[row["id"] for row in self.stored()], governance_delta=attestation)
        stored = read(self.project, "objective")["checkpoint"]
        recorded = stored["governance_history"]["decisions"][-1]["rebind"]
        self.assertEqual((len(recorded["rows"]), recorded["reason"]), (31, reason))
        self.assertEqual(recorded["delta"], attestation["sources"])


class UndatedPriorTests(KernelCase):
    """R166: earlier decisions recorded without a time do not block a decision made at this write."""

    def setUp(self):
        super().setUp()
        config.set_decision_policy(self.project, "hands_off", decision=PERSON)
        self.write([self.criterion()], governance={"base_ref": "target"})
        rows = self.stored()
        rows[0]["check"] = "PoD#1 passes, as revised"
        # An earlier-format revision decision: recorded without a decision time.
        self.write(rows, revision_authority={"provenance": "user_direct", "instruction": "Revise the check"})
        self.assertNotIn("decided_at", read(self.project, "objective")["checkpoint"]["revision_decisions"][-1])
        git(self.project, "switch", "-q", "target")
        target = governance_commit(self, "AGENTS.md", AGENTS + ADDED, "rules")
        git(self.project, "update-ref", "refs/remotes/origin/target", target)
        git(self.project, "switch", "-q", "main")

    def test_an_agent_decision_at_this_write_orders_after_undated_decisions(self):
        before = deepcopy(read(self.project, "objective")["checkpoint"]["revision_decisions"])
        self.write(self.stored(), governance_refresh=True, governance=snapshot(self.project), revision_authority=LOCAL)
        stored = read(self.project, "objective")["checkpoint"]
        self.assertEqual(stored["governance"]["base"], git(self.project, "rev-parse", "refs/remotes/origin/target"))
        self.assertEqual(stored["revision_decisions"][:len(before)], before)
        self.assertNotIn("decided_at", stored["revision_decisions"][0])

    def test_a_decider_the_policy_does_not_name_still_refuses(self):
        config.set_decision_policy(self.project, "careful", decision=PERSON)
        with self.assertRaises(PodError) as caught:
            self.write(self.stored(), governance_refresh=True, governance=snapshot(self.project), revision_authority=LOCAL)
        self.assertEqual(caught.exception.code, "decision_policy_denied")


class QuestionTests(ProductionCase):
    """R164: asking, answering, cancelling and replacing a question need no governance refresh."""

    def setUp(self):
        super().setUp()
        config.set_decision_policy(self.project, "delegate", decision=PERSON)
        self.write([self.criterion()], governance={"base_ref": "refs/remotes/origin/target"})
        self.question = ledger.tool_decision(self.project, "objective", action="check", tool="fixture-tool",
                                             category="secrets", operation="read")
        git(self.project, "switch", "-q", "target")
        (self.project / "AGENTS.md").write_text("# Rules\n" + ADDED)
        git(self.project, "add", "AGENTS.md"); git(self.project, "commit", "-qm", "rules")
        git(self.project, "update-ref", "refs/remotes/origin/target", git(self.project, "rev-parse", "HEAD"))
        git(self.project, "switch", "-q", "main")
        self.bound = deepcopy(read(self.project, "objective")["checkpoint"]["governance"])

    def governance(self):
        return read(self.project, "objective")["checkpoint"]["governance"]

    def test_cancel_and_replacement_skip_currency_and_change_no_binding(self):
        from pod.governance import require_governance_current
        with self.assertRaises(PodError) as caught:
            require_governance_current(self.project, read(self.project, "objective")["checkpoint"])
        self.assertEqual(caught.exception.code, "governance_changed")
        cancelled = ledger.tool_decision(self.project, "objective", action="cancel", invocation=self.question["invocation"])
        self.assertEqual(cancelled["status"], "cancelled")
        replacement = ledger.tool_decision(self.project, "objective", action="check", tool="fixture-tool",
                                           category="secrets", operation="read once")
        self.assertNotEqual(replacement["invocation"], self.question["invocation"])
        self.assertEqual(self.governance(), self.bound)
        # Owner, invocation identity and state checks still apply.
        self.refused("tool_invocation_unavailable", None, ledger.tool_decision, self.project, "objective",
                     action="cancel", invocation=self.question["invocation"])

    def test_an_answer_records_but_using_the_approval_still_needs_current_governance(self):
        ledger.tool_decision(self.project, "objective", action="answer", invocation=self.question["invocation"],
                             answer="ask_each_time", decision=PERSON)
        operation = ledger.tool_decision(self.project, "objective", action="check", tool="fixture-tool",
                                         category="secrets", operation="read")
        ledger.tool_decision(self.project, "objective", action="answer", invocation=operation["invocation"],
                             answer="approve", decision=PERSON)
        self.assertEqual(self.governance(), self.bound)
        self.refused("governance_changed", None, ledger.tool_decision, self.project, "objective", action="check",
                     tool="fixture-tool", category="secrets", operation="read", invocation=operation["invocation"])
