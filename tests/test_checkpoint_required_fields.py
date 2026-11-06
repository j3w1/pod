"""Required checkpoint/map fields refuse before any context is published (pod#254)."""

from copy import deepcopy
from unittest.mock import patch

from pod.errors import PodError
from pod.internal import run
from pod.ledger import checkpoint, objective_root, read
from pod.obligations import MAP_STORED, accept_write
from tests.kernel_support import KernelCase
from tests.test_obligations import ctx, intake


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

    def test_missing_checkpoint_core_fields_refuse_without_writing(self):
        from pod.contracts import required
        for field in sorted(required("checkpoint") - {"schema"}):
            with self.subTest(field=field):
                value = self.core(seq=1, obligations=[self.criterion()], governance={"base_ref": "target"})
                del value[field]
                before = self.files()
                with self.assertRaises(PodError) as caught:
                    checkpoint(self.project, "objective", owner="fixture", value=value,
                               native={"runtime": "runtime"})
                self.assertIn(field, caught.exception.detail["missing"])
                self.assertTrue(caught.exception.detail["next_action"])
                self.assertEqual(self.files(), before)

    def test_prior_map_required_fields_refuse_before_lookups(self):
        state = accept_write(None, intake(), ctx())
        required = MAP_STORED - {"governance_history", "delivery", "revision_decisions", "obligations"}
        for field in sorted(required):
            with self.subTest(field=field):
                prior = deepcopy(state)
                del prior[field]
                before = deepcopy(prior)
                with self.assertRaises(PodError) as caught:
                    accept_write(prior, {}, ctx())
                self.assertIn(field, caught.exception.detail["referent"]["missing"])
                self.assertTrue(caught.exception.detail["next_action"])
                self.assertEqual(prior, before)

    def test_prior_governance_required_fields_refuse_before_lookups(self):
        state = accept_write(None, intake(), ctx())
        for field in ("base_ref", "selection", "exclude", "base"):
            with self.subTest(field=field):
                prior = deepcopy(state)
                del prior["governance"][field]
                before = deepcopy(prior)
                with self.assertRaises(PodError) as caught:
                    accept_write(prior, {}, ctx())
                self.assertIn(field, caught.exception.detail["referent"]["missing"])
                self.assertTrue(caught.exception.detail["next_action"])
                self.assertEqual(prior, before)

    def test_checkpoint_prior_map_missing_governance_refuses_without_writing(self):
        self.intake()
        from pod.ledger import _path
        from pod.history import prepare_context, publish_context
        path = _path(self.project, "objective")
        state = read(self.project, "objective")
        del state["checkpoint"]["governance"]
        state["revision"] += 1
        publish_context(path, *prepare_context(path, state, project=self.project))
        before = self.files()
        with self.assertRaises(PodError) as caught:
            self.write(self.stored())
        self.assertIn("governance", caught.exception.detail["referent"]["missing"])
        self.assertTrue(caught.exception.detail["next_action"])
        self.assertEqual(self.files(), before)

    def test_persisted_row_required_fields_refuse_without_writing(self):
        from pod.history import prepare_context, publish_context
        from pod.ledger import _path
        from pod.history_tree import plain
        self.intake()
        state = plain(read(self.project, "objective"))
        path = _path(self.project, "objective")
        for field in ("id", "kind", "provenance", "state", "introduced_seq", "definition"):
            with self.subTest(field=field):
                state["revision"] += 1
                prior = deepcopy(state)
                del prior["checkpoint"]["obligations"][0][field]
                publish_context(path, *prepare_context(path, prior, project=self.project))
                before = self.files()
                with self.assertRaises(PodError) as caught:
                    checkpoint(self.project, "objective", owner="owner", value=self.core(),
                               native={"runtime": "runtime"})
                self.assertEqual(caught.exception.code, "obligation_invalid")
                self.assertIn(field, caught.exception.detail["referent"]["missing"])
                self.assertTrue(caught.exception.detail["next_action"])
                self.assertEqual(self.files(), before)
                pure = deepcopy(prior["checkpoint"])
                with self.assertRaises(PodError) as caught:
                    accept_write(pure, {}, ctx())
                self.assertIn(field, caught.exception.detail["referent"]["missing"])
                self.assertEqual(pure, prior["checkpoint"])

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
