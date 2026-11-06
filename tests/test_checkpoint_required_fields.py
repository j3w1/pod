"""Required checkpoint/map fields refuse before any context is published (pod#254)."""

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
