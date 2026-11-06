"""Required checkpoint/map fields refuse before any context is published (pod#254)."""

from copy import deepcopy
from unittest.mock import patch

from pod.errors import PodError
from pod.internal import run
from pod.ledger import checkpoint, objective_root, read
from tests.kernel_support import KernelCase


class CheckpointRequiredFieldsTests(KernelCase):
    def files(self):
        root = objective_root(self.project, "objective")
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*") if path.is_file() and path.name != ".lock"}

    def test_first_checkpoint_missing_map_fields_refuses_without_writing(self):
        for supplied, missing in (({"seq": 1}, ["governance", "obligations"]),
                                  ({"seq": 1, "obligations": [self.criterion()]}, ["governance"]),
                                  ({"governance": {"base_ref": "target"}}, ["obligations"])):
            with self.subTest(missing=missing):
                before = self.files()
                # Use the real internal checkpoint entry; only its native read is synthetic.
                with patch("pod.handoff.refuse_nonowner"), patch("pod.orca.contract", return_value={
                        "status": "observed", "runtime": "runtime", "capabilities": {}}):
                    with self.assertRaises(PodError) as caught:
                        run("checkpoint", {"project": str(self.project), "objective": "objective",
                                           "owner": "fixture", "value": self.core(**supplied)})
                error = caught.exception
                self.assertEqual(error.code, "obligation_invalid")
                self.assertEqual(error.detail["referent"]["record"], "map")
                self.assertEqual(error.detail["referent"]["missing"], missing)
                self.assertEqual(error.detail["operation"], "checkpoint")
                self.assertIn("governance", error.detail["referent"]["accepted"])
                self.assertTrue(error.detail["next_action"])
                self.assertEqual(self.files(), before)
                self.assertIsNone(read(self.project, "objective"))

    def test_persisted_row_required_fields_refuse_without_writing(self):
        from pod.history import prepare_context, publish_context
        from pod.ledger import _path
        from pod.history_tree import plain
        self.intake()
        state = plain(read(self.project, "objective"))
        path = _path(self.project, "objective")
        for field in ("id", "kind"):
            with self.subTest(field=field):
                state["revision"] += 1
                prior = deepcopy(state)
                del prior["checkpoint"]["obligations"][0][field]
                publish_context(path, *prepare_context(path, prior, project=self.project))
                before = self.files()
                if field == "kind":
                    # Loading this row was valid before the checkpoint-site guard.
                    self.assertNotIn("kind", read(self.project, "objective")["checkpoint"]["obligations"][0])
                    self.assertEqual(self.files(), before)
                with self.assertRaises(PodError) as caught:
                    checkpoint(self.project, "objective", owner="owner", value=self.core(),
                               native={"runtime": "runtime"})
                self.assertEqual(caught.exception.code, "obligation_invalid")
                self.assertIn(field, caught.exception.detail["referent"]["missing"])
                self.assertTrue(caught.exception.detail["next_action"])
                self.assertEqual(self.files(), before)

    def test_persisted_unknown_row_field_keeps_loading_without_writing(self):
        from pod.history import prepare_context, publish_context
        from pod.history_tree import plain
        from pod.ledger import _path
        self.intake()
        state = plain(read(self.project, "objective"))
        state["revision"] += 1
        state["checkpoint"]["obligations"][0]["extra"] = True
        path = _path(self.project, "objective")
        publish_context(path, *prepare_context(path, state, project=self.project))
        before = self.files()
        self.assertTrue(read(self.project, "objective")["checkpoint"]["obligations"][0]["extra"])
        self.assertEqual(self.files(), before)

    def test_unhashable_caller_ids_refuse_without_writing(self):
        for identity in ([], {}):
            with self.subTest(identity=identity):
                value = self.core(seq=1, governance={"base_ref": "target"},
                                  obligations=[{**self.criterion(), "id": identity}])
                before = self.files()
                with self.assertRaises(PodError) as caught:
                    checkpoint(self.project, "objective", owner="owner", value=value,
                               native={"runtime": "runtime"})
                self.assertEqual(caught.exception.code, "obligation_invalid")
                self.assertEqual(caught.exception.detail["referent"]["field"], "obligation id")
                self.assertTrue(caught.exception.detail["next_action"])
                self.assertEqual(self.files(), before)

    def test_unhashable_caller_ids_with_prior_map_refuse_without_writing(self):
        self.intake()
        for identity in ([], {}):
            with self.subTest(identity=identity):
                before = self.files()
                with self.assertRaises(PodError) as caught:
                    self.write([{**self.criterion(), "id": identity}])
                self.assertEqual(caught.exception.code, "obligation_invalid")
                self.assertEqual(caught.exception.detail["referent"]["field"], "obligation id")
                self.assertEqual(self.files(), before)

    def test_closed_map_unhashable_caller_ids_keep_objective_closed_first(self):
        self.intake()
        self.write([self.criterion("satisfied", evidence=[self.proof("O1")])], close=True)
        for identity in ([], {}):
            with self.subTest(identity=identity):
                before = self.files()
                with self.assertRaises(PodError) as caught:
                    self.write([{**self.criterion(), "id": identity}])
                self.assertEqual(caught.exception.code, "objective_closed")
                self.assertEqual(self.files(), before)

    def test_valid_mapless_and_mapped_checkpoints_keep_working(self):
        checkpoint(self.project, "objective", owner="owner", value=self.core(), native={"runtime": "runtime"})
        state = self.intake()
        updated = self.write(self.stored(), seq=state["checkpoint"]["seq"] + 1)
        self.assertEqual(updated["checkpoint"]["governance"], state["checkpoint"]["governance"])
        carried = checkpoint(self.project, "objective", owner="owner",
                             value=self.core(seq=updated["checkpoint"]["seq"] + 1),
                             native={"runtime": "runtime"})
        patched = checkpoint(self.project, "objective", owner="owner",
                             value=self.core(seq=carried["checkpoint"]["seq"] + 1,
                                             update={"O1": {"state": "active", "executor": "coordinator"}}),
                             native={"runtime": "runtime"})
        self.assertEqual(patched["checkpoint"]["obligations"], carried["checkpoint"]["obligations"])
