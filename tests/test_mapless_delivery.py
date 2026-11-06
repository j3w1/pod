"""A mapless checkpoint carrying a delivery refuses by name and writes nothing (pod#274)."""

from pod.errors import PodError
from pod.ledger import checkpoint, objective_root, read
from tests.kernel_support import KernelCase
from tests.test_runtime_continuity import ContinuityCase

NATIVE = {"runtime": "runtime", "delegation": "available"}


class MaplessDeliveryTests(KernelCase):
    def files(self):
        root = objective_root(self.project, "objective")
        return {str(path.relative_to(root)): path.read_bytes()
                for path in root.rglob("*") if path.is_file() and path.name != ".lock"}

    def mapless(self, **extra):
        return checkpoint(self.project, "objective", owner="owner", value=self.core(**extra), native=NATIVE)

    def test_mapless_delivery_refuses_by_name_without_writing(self):
        for label, prepare in (("first write", lambda: None), ("after a mapless write", lambda: self.mapless())):
            for delivery in ({"record": "0" * 64}, {"local_append": {"operation": "op-1"}}):
                with self.subTest(case=label, delivery=sorted(delivery)):
                    prepare()
                    before, stored = self.files(), read(self.project, "objective")
                    with self.assertRaises(PodError) as caught:
                        self.mapless(delivery=delivery)
                    error = caught.exception
                    self.assertEqual((error.code, error.detail["detail"]), ("delivery_unverified", "map_missing"))
                    self.assertIn("obligation map", error.detail["next_action"])
                    self.assertEqual(self.files(), before)
                    self.assertEqual(read(self.project, "objective"), stored)

    def test_mapless_checkpoint_without_delivery_behaves_as_today(self):
        accepted = self.mapless()
        self.assertEqual(accepted["checkpoint"]["next_safe_action"], "continue")
        self.assertNotIn("delivery", read(self.project, "objective")["checkpoint"])
        again = self.mapless(next_safe_action="again")
        self.assertEqual(again["checkpoint"]["next_safe_action"], "again")
        self.assertNotIn("obligations", read(self.project, "objective")["checkpoint"])


class MaplessDeliveryContinuityTests(ContinuityCase):
    """Through the real authority join, a proven runtime change is not persisted by the refusal."""

    def test_mapless_delivery_refusal_defers_runtime_rebind(self):
        checkpoint(self.project, "objective", owner="owner", value=self.core(), native={"runtime": "runtime"})
        for delivery in ({"record": "0" * 64}, {"local_append": {"operation": "op-1"}}):
            for extra in ({}, {"seq": 1}):
                with self.subTest(delivery=sorted(delivery), fields=sorted(extra)):
                    before = self.snapshot()
                    self.change_runtime()
                    with self.assertRaises(PodError) as caught:
                        checkpoint(self.project, "objective", owner="owner",
                                   value=self.core(delivery=delivery, **extra), native={"runtime": "runtime-2"})
                    self.assertEqual((caught.exception.code, caught.exception.detail["detail"]),
                                     ("delivery_unverified", "map_missing"))
                    self.assertEqual(self.snapshot(), before)
                    self.change_runtime("runtime")
