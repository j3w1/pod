"""R119: derived adoptions retain every row without spending owner-decision slots."""
from copy import deepcopy
import subprocess
import unittest

from pod.errors import PodError
from pod.history import compact, hydrate
from pod.ledger import _validate_context, objective_root, read
from pod.obligations import validate_map_record
from pod.util import encoded
from tests.kernel_support import KernelCase, USER, git


class LongAdoptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Real transitions and Git ancestry, built once outside individual test timing.
        case = KernelCase()
        case.setUp()
        try:
            case.intake()
            cls.initial = read(case.project, "objective")
            tree = git(case.project, "rev-parse", "HEAD^{tree}")
            target = case.base
            cls.targets, cls.refusal = [], None
            for number in range(80):
                target = subprocess.run(["git", "-C", str(case.project), "commit-tree", tree, "-p", target],
                                        input=f"independent advance {number}\n", text=True,
                                        capture_output=True, check=True).stdout.strip()
                git(case.project, "update-ref", "refs/remotes/origin/target", target)
                try:
                    case.write(case.stored())
                    validate_map_record(read(case.project, "objective")["checkpoint"])
                except PodError as exc:
                    cls.refusal = (number + 1, exc.code, exc.detail)
                    break
                cls.targets.append(target)
            cls.state = read(case.project, "objective")
            cls.owner_count, cls.owner_refusal, cls.bound_unchanged = 0, None, False
            if cls.refusal is None:
                path = objective_root(case.project, "objective") / "context.json"
                for number in range(65):
                    before = path.read_bytes()
                    try:
                        case.write(case.stored(), governance_refresh=True,
                            governance={"base_ref": "refs/remotes/origin/target", "base": target},
                            revision_authority={**USER, "instruction": f"Owner snapshot decision {number}"})
                    except PodError as exc:
                        cls.owner_refusal = (exc.code, exc.detail)
                        cls.bound_unchanged = path.read_bytes() == before
                        break
                    cls.owner_count += 1
        finally:
            case.doCleanups()

    def test_eighty_plain_checkpoints_preserve_the_complete_adoption_chain(self):
        self.assertIsNone(self.refusal)
        self.assertEqual(len(self.targets), 80)
        checkpoint = self.state["checkpoint"]
        decisions = checkpoint["governance_history"]["decisions"]
        self.assertEqual(len(decisions), 80)
        self.assertEqual(checkpoint["governance"]["proof_base"], self.initial["checkpoint"]["governance"]["base"])
        sources = [{"path": row["path"], "revision": row["revision"]}
                   for row in self.initial["checkpoint"]["governance_sources"]]
        previous = checkpoint["governance"]["proof_base"]
        for index, (decision, target) in enumerate(zip(decisions, self.targets), 2):
            self.assertEqual(decision, {"seq": index, "base_ref": checkpoint["governance"]["base_ref"],
                "base": target, "previous_base": previous, "provenance": "derived",
                "kind": "unchanged_sources", "sources": sources})
            previous = target
        validate_map_record(checkpoint)

    def test_every_derived_row_survives_relocation_and_validation(self):
        self.assertIsNone(self.refusal)
        record, chunks = compact(self.state, force=True)
        self.assertTrue(chunks)
        restored = hydrate(record, chunks.__getitem__)
        self.assertEqual(restored, self.state)
        validate_map_record(restored["checkpoint"])

    def test_settled_owner_history_frees_live_slots_after_eighty_derived_adoptions(self):
        self.assertIsNone(self.refusal)
        self.assertEqual(self.owner_count, 65)
        self.assertIsNone(self.owner_refusal)
        self.assertEqual(len(self.state["checkpoint"]["governance_history"]["decisions"]), 80)



class GovernanceCapacityTests(KernelCase):
    def capacity_refusal(self, operation, section):
        with self.assertRaises(PodError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "governance_unavailable")
        self.assertEqual(caught.exception.detail["detail"], "governance_history_full")
        self.assertIn(section, caught.exception.detail["next_action"])

    def full_owner_history(self):
        self.intake(revision_authority=USER)
        state = read(self.project, "objective")
        checkpoint = state["checkpoint"]
        # Synthetic count-bound control: otherwise valid immutable owner decisions.
        first = checkpoint["governance_history"]["decisions"][0]
        checkpoint["governance_history"]["decisions"] = [
            {**first, "instruction": f"Owner snapshot decision {index}"} for index in range(8192)]
        path = objective_root(self.project, "objective") / "context.json"
        path.write_bytes(encoded(state, compact=True))
        return state, path

    def test_hard_owner_history_refuses_checkpoint_and_closure_without_write(self):
        state, path = self.full_owner_history()
        validate_map_record(state["checkpoint"])
        before = path.read_bytes()
        rows = [self.criterion("satisfied", evidence=[self.proof("O1")])]
        for close in (False, True):
            with self.subTest(close=close):
                self.capacity_refusal(lambda: self.write(rows, **({"close": True} if close else {}), governance_refresh=True,
                    governance={"base_ref": "refs/remotes/origin/target", "base": self.base},
                    revision_authority={**USER, "instruction": "Another exact owner snapshot"}), "owner")
                self.assertEqual(path.read_bytes(), before)

    def check_stored_overflow(self, section):
        state, path = self.full_owner_history()
        history = state["checkpoint"]["governance_history"]
        if section == "owner":
            history["decisions"].append({**history["decisions"][-1], "instruction": "Overflow decision"})
        else:
            history["candidates"] = [f"{index + 1:040x}" for index in range(8193)]
        before = encoded(state, compact=True)
        path.write_bytes(before)
        for operation in (lambda: _validate_context(deepcopy(state)),
                          lambda: validate_map_record(state["checkpoint"]),
                          lambda: self.write(self.stored()),
                          lambda: self.write(self.stored(), close=True)):
            with self.subTest(operation=operation):
                self.capacity_refusal(operation, section)
                self.assertEqual(path.read_bytes(), before)

    def test_well_formed_owner_overflow_uses_capacity_refusal_on_every_path(self):
        self.check_stored_overflow("owner")

    def test_well_formed_candidate_overflow_uses_capacity_refusal_on_every_path(self):
        self.check_stored_overflow("candidate")
