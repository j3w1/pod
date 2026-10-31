from tests.history_support import publish_preceding
"""R119/R120: automatic adoptions have bounded public projections and complete history."""
from tests.history_support import compact
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import subprocess
import unittest

from pod.history import (hydrate, publish_context, CONTEXT_LIMIT)
from pod.ledger import objective_root, read
from pod.obligations import validate_map_record
from pod.status import render, status
from pod.util import encoded
from tests.kernel_support import KernelCase, USER, git


class DerivedProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = cls.case = KernelCase()
        case.setUp()
        cls.addClassCleanup(case.doCleanups)
        case.intake(revision_authority=USER)
        # A single Git import builds 600 real, unchanged-tree forward targets cheaply.
        commands = []
        for number in range(1, 601):
            message = f"Independent advance {number}\n"
            commands.append(f"commit refs/heads/adoption-fixture\nmark :{number}\n"
                            "committer Fixture <fixture@example.invalid> 1790000000 +0000\n"
                            f"data {len(message)}\n{message}"
                            f"from {case.base if number == 1 else ':' + str(number - 1)}\n\n"
                            f"get-mark :{number}\n")
        cls.targets = subprocess.run(["git", "-C", str(case.project), "fast-import", "--quiet"],
                                     input="".join(commands), text=True, capture_output=True,
                                     check=True).stdout.splitlines()
        git(case.project, "update-ref", "refs/remotes/origin/target", cls.targets[0])
        case.write(case.stored())
        state = read(case.project, "objective")
        history = state["checkpoint"]["governance_history"]
        owner, adoption = history["decisions"]
        # Synthetic retained history uses the production row shape and real ancestry;
        # the last adoption and the closing write still cross the production boundary.
        history["decisions"] = [owner, *[
            {**deepcopy(adoption), "seq": index + 2, "base": target,
             "previous_base": cls.targets[index - 1] if index else case.base}
            for index, target in enumerate(cls.targets[:-1])]]
        state["checkpoint"]["seq"] = state["revision"] = 600
        state["checkpoint"]["governance"]["base"] = cls.targets[-2]
        cls.path = objective_root(case.project, "objective") / "context.json"
        cls.path.write_bytes(encoded(state, compact=True))
        git(case.project, "update-ref", "refs/remotes/origin/target", cls.targets[-1])
        case.write(case.stored())
        cls.state = read(case.project, "objective")
        validate_map_record(cls.state["checkpoint"])

    def setUp(self):
        self.path.write_bytes(encoded(self.state, compact=True))
        git(self.case.project, "update-ref", "refs/remotes/origin/target", self.targets[-1])

    def observe(self):
        return status(self.case.project, run=None, objective="objective",
                      current_run_fn=lambda: {"run": None},
                      worker_rows_fn=lambda _: {"workers": [], "scope": {"run": None},
                                                "complete": True, "runtime": "runtime"})

    def expected(self, count=600):
        rows = self.state["checkpoint"]["governance_history"]["decisions"]
        return {"count": count, "previous_base": rows[1]["previous_base"],
                "base": rows[count]["base"], "base_ref": rows[count]["base_ref"],
                "sources": rows[count]["sources"],
                "history": {"reference": "governance_history/decisions", "provenance": "derived",
                            "first": 1, "last": count}}

    def test_six_hundred_adoptions_close_with_a_bounded_report(self):
        case = self.case
        case.write([case.criterion("satisfied", evidence=[case.proof("O1")])], close=True)
        closed = read(case.project, "objective")
        report = closed["checkpoint"]["closure"]["report"]
        self.assertEqual(report["status"], "closed")
        self.assertEqual(report["derived_governance_decisions"], self.expected())
        self.assertEqual(report["governance_decisions"], [
            self.state["checkpoint"]["governance_history"]["decisions"][0]])
        self.assertEqual(closed["checkpoint"]["governance_history"], self.state["checkpoint"]["governance_history"])
        self.assertLess(len(encoded(report, compact=True)), 4096)
        self.assertLess(self.path.stat().st_size, CONTEXT_LIMIT)
        self.assertEqual(self.observe()["report"]["status"], "closed")

    def test_status_size_stays_bounded_as_adoptions_accumulate(self):
        sizes, observations = [], []
        for count in (1, 80, 600):
            state = deepcopy(self.state)
            state["checkpoint"]["governance_history"]["decisions"] = state["checkpoint"]["governance_history"]["decisions"][:count + 1]
            state["checkpoint"]["governance"]["base"] = self.targets[count - 1]
            self.path.write_bytes(encoded(state, compact=True))
            git(self.case.project, "update-ref", "refs/remotes/origin/target", self.targets[count - 1])
            observed = self.observe()
            sizes.append(len(encoded(observed, compact=True)))
            observations.append((count, observed))
        self.assertLess(max(sizes) - min(sizes), 512, sizes)
        for count, observed in observations:
            self.assertEqual(observed["derived_governance_decisions"], self.expected(count))
            self.assertEqual(observed["report"]["derived_governance_decisions"], self.expected(count))
            self.assertTrue(all(row["provenance"] != "derived" for row in observed["report"]["governance_decisions"]))

    def test_relocation_preserves_every_row_and_the_same_projection(self):
        record, chunks = compact(self.state, force=True)
        self.assertTrue(chunks)
        self.assertEqual(hydrate(record, chunks.__getitem__), self.state)
        publish_preceding(self.path, record, chunks)
        self.assertEqual(read(self.case.project, "objective"), self.state)
        self.assertEqual(self.observe()["derived_governance_decisions"], self.expected())

    def test_owner_decisions_between_adoptions_keep_their_full_channels(self):
        state = deepcopy(self.state)
        rows = state["checkpoint"]["governance_history"]["decisions"]
        owner = {**rows[0], "seq": 301, "kind": "snapshot", "base": self.targets[299],
                 "instruction": "Owner snapshot between independent advances", "channel": "relayed",
                 "relay": {"reference": "verified-relay", "owner_source": "owner-message"}}
        rows.insert(301, owner)
        self.path.write_bytes(encoded(state, compact=True))
        expected = self.expected()
        expected["history"]["last"] = 601
        observed = self.observe()
        self.assertEqual(observed["derived_governance_decisions"], expected)
        self.assertEqual(observed["report"]["derived_governance_decisions"], expected)
        self.assertEqual(observed["report"]["governance_decisions"], [rows[0], owner])
        self.assertEqual([row["decision"] for row in observed["owner_decisions"] if row["kind"] == "governance"],
                         [rows[0], owner])

    def test_text_status_prints_one_adoption_summary(self):
        observed = self.observe()
        output = StringIO()
        with redirect_stdout(output):
            render(observed)
        text = output.getvalue()
        self.assertEqual(text.count("Derived governance adoption"), 1)
        self.assertIn("600", text)
        self.assertIn(self.expected()["previous_base"], text)
        self.assertIn(self.expected()["base"], text)
