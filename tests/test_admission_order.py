"""Reservation ordering remains immutable through native recovery."""

from copy import deepcopy

from pod.errors import PodError
from pod.ledger import _validate_admission, read, update_admission
from tests import kernel_support as boundaries


class AdmissionOrderTests(boundaries.KernelCase):
    def reserve(self):
        self.intake()
        frozen = self.packet(["O1"])
        return frozen, self.start("work", frozen)["admission"]

    def test_reservation_sequence_is_atomic_and_same_request_replay_preserves_it(self):
        frozen, admission = self.reserve()
        state = read(self.project, "objective")
        sequence = admission["admitted_seq"]
        self.assertEqual(sequence, state["checkpoint"]["seq"])
        repeated = self.start("work", frozen)["admission"]
        self.assertEqual(repeated["admitted_seq"], sequence)
        self.assertEqual(read(self.project, "objective")["checkpoint"]["seq"], sequence)
        self.assertEqual(len(self.port.starts), 1)

    def test_admission_updates_cannot_change_or_remove_the_reservation_sequence(self):
        _, admission = self.reserve()
        key = admission["admission_id"]
        before = read(self.project, "objective")
        for update in (lambda row: row.update(admitted_seq=row["admitted_seq"] + 1),
                       lambda row: row.pop("admitted_seq")):
            with self.subTest(update=update), self.assertRaises(PodError) as caught:
                update_admission(self.project, "objective", owner="owner", admission_id=key,
                                 update=update)
            self.assertEqual(caught.exception.code, "admission_conflict")
            self.assertEqual(read(self.project, "objective"), before)

    def test_missing_sequence_is_not_fabricated_and_malformed_values_are_refused(self):
        _, admission = self.reserve()
        key = admission["admission_id"]
        row = deepcopy(read(self.project, "objective")["admissions"][key])
        row.pop("admitted_seq")
        _validate_admission(key, row)
        self.assertNotIn("admitted_seq", row)
        for value in (None, True, 0, -1, "1", 1.5):
            with self.subTest(value=value), self.assertRaises(PodError) as caught:
                _validate_admission(key, {**row, "admitted_seq": value})
            self.assertEqual(caught.exception.code, "state_unsupported")
