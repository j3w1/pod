"""Affirmative record census is independent of liveness, relevance and candidate proof."""
from contextlib import contextmanager
import hashlib
import json
import shutil
import threading
from unittest.mock import patch

from pod import admission, history
from pod.errors import PodError
from pod.ledger import objective_root, state_root
from pod.orca import DEADLINE_SPENT
from tests import test_admission_query as base
from tests.test_admission_batch import BatchCase, NativeDouble, fleet_model, terminal, terminal_page


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


class CensusCase(BatchCase):
    def setUp(self):
        super().setUp()
        for target, read in (("_fleet", self._real_fleet), ("_terminals", self._real_terminals)):
            live_shape = patch("pod.admission." + target, side_effect=read)
            live_shape.start(); self.addCleanup(live_shape.stop)

    def assert_census(self, result, complete=True):
        witness = result["record_census"]
        self.assertEqual(set(witness), {"schema", "complete", "scope_sha256", "cohort_sha256",
                                        "records_sha256", "binding_sha256"})
        self.assertEqual(witness["schema"], "pod-admission-record-census/v1")
        self.assertIs(witness["complete"], complete)
        if complete:
            for key in ("scope_sha256", "cohort_sha256", "records_sha256", "binding_sha256"):
                self.assertRegex(witness[key], r"^[0-9a-f]{64}$")
            public = {key: value for key, value in result.items() if key != "record_census"}
            self.assertEqual(witness["binding_sha256"], sha({
                **{key: value for key, value in witness.items() if key != "binding_sha256"},
                "result_sha256": sha(public)}))
        else:
            self.assertTrue(all(witness[key] is None for key in witness if key.endswith("_sha256")))
        self.assertIs(result["authorization"], False)
        return witness

    def answers(self, **shape):
        return [self.query(**shape), self.batch(shape)["responses"][0]["result"]]

    @contextmanager
    def at_recheck(self, change):
        real = admission._after_cohort
        changed = False

        def after(root, budget):
            nonlocal changed
            if not changed:
                changed = True
                change(root)
            return real(root, budget)

        with patch("pod.admission._after_cohort", side_effect=after):
            yield


class AffirmativeCensusTests(CensusCase):
    def test_stable_included_and_excluded_records_bind_single_and_batch(self):
        self.intake(worktree=self.place)
        foreign = base.AttributionReadTests.foreign_record(self)
        results = self.answers(branch="main")
        self.assertEqual(results[0], results[1])
        for result in results:
            witness = self.assert_census(result)
            self.assertTrue(result["complete"])
            self.assertEqual(witness["scope_sha256"], sha({"repo_key": self.place["repo_key"],
                "path": str(self.project), "state_root": str(state_root(self.project))}))
            public = json.dumps(witness)
            for private in (foreign.parent.name, "foreign", str(self.project), "retained", "objective"):
                self.assertNotIn(private, public)
            self.assertLess(len(public), 600)

    def test_unknown_worker_and_terminal_facts_keep_claims_and_census(self):
        self.intake(worktree=self.place)
        writer = base.ReviewCorrectionTests.writer(self)
        writer.update(terminalState="retained", workerState="ready", dispatchStatus="running")
        pages = terminal_page([terminal("terminal", self.project, writable=False)])
        self.native = NativeDouble(pages, fleet=fleet_model([writer]))
        with patch("pod.admission._fleet", side_effect=self._real_fleet), \
             patch("pod.admission._read", side_effect=self.native), \
             patch("pod.admission._terminals", side_effect=self._real_terminals):
            results = self.answers(worktree=str(self.project), branch="main")
        for result in results:
            self.assert_census(result)
            self.assertFalse(result["complete"])
            self.assertFalse(result["repository_complete"])
            self.assertTrue(result["native_resources_complete"])
            self.assertEqual({row["fact"] for row in result["unknowns"]},
                             {"process_liveness", "terminal_liveness"})
            self.assertEqual(len([row for row in result["claims"] if row["kind"] == "writer"]), 2)
            self.assertTrue(result["conflicts"])

    def test_empty_census_never_supplies_exit_or_cleanup_permission(self):
        for result in self.answers(branch="main"):
            self.assert_census(result)
            self.assertEqual(result["claims"], [])
            self.assertNotIn("host_exits", result)
            self.assertNotIn("actions", result)
            self.assertNotIn("exit", result["record_census"])

    def test_unlocated_writer_remains_conservatively_relevant(self):
        self.intake(worktree=self.place)
        writer = base.ReviewCorrectionTests.writer(self)
        writer["resource"] = None
        writer["projection"].pop("workspace")
        with patch("pod.admission._fleet", return_value={"runtime": "runtime", "complete": True,
                                                         "workers": [writer]}):
            result = self.query(worktree=str(self.root / "unrelated"), branch="unrelated")
        self.assert_census(result)
        self.assertFalse(result["complete"])
        held = [row for row in result["claims"] if row["kind"] == "writer"]
        self.assertEqual(len(held), 1)
        self.assertFalse(held[0]["locations_complete"])
        self.assertIsNone(held[0]["worktree"])

    def test_full_result_binding_detects_spliced_scope_snapshot_and_unknowns(self):
        self.intake(worktree=self.place)
        result = self.query(branch="main")
        witness = self.assert_census(result)
        for field, value in (("scope", {**result["scope"], "path": "/foreign"}),
                             ("snapshot", "0" * 64), ("unknowns", [{"code": "discovery_budget"}])):
            with self.subTest(field=field):
                altered = {key: value for key, value in result.items() if key != "record_census"}
                altered[field] = value
                expected = sha({**{key: value for key, value in witness.items() if key != "binding_sha256"},
                                "result_sha256": sha(altered)})
                self.assertNotEqual(witness["binding_sha256"], expected)


class IncompleteCensusTests(CensusCase):
    def test_missing_false_and_malformed_witnesses_are_never_promoted(self):
        self.intake(worktree=self.place)
        bounded = admission._bounded

        def invalidate(result, kwargs):
            result = bounded(result, kwargs)
            self.assertIs(result["record_census"]["complete"], True)
            if shape == "missing":
                result.pop("record_census")
            elif shape == "false":
                result["record_census"]["complete"] = False
            else:
                result["record_census"]["scope_sha256"] = True
            return result

        for shape in ("missing", "false", "malformed"):
            with self.subTest(shape=shape), patch("pod.admission._bounded", side_effect=invalidate):
                self.assert_census(self.query(branch="main"), False)

    def test_stale_witness_and_foreign_final_projection_are_rejected(self):
        self.intake(worktree=self.place)
        previous = self.assert_census(self.query(branch="previous"))
        bounded = admission._bounded

        def invalidate(result, kwargs):
            result = bounded(result, kwargs)
            if shape == "stale":
                result["record_census"] = previous
            else:
                result["scope"] = {**result["scope"], "path": "/foreign"}
            return result

        for shape in ("stale", "foreign"):
            with self.subTest(shape=shape), patch("pod.admission._bounded", side_effect=invalidate):
                self.assert_census(self.query(branch="main"), False)

    def test_unreadable_record_despite_complete_snapshot_and_native_fence(self):
        self.intake(worktree=self.place)
        broken = state_root(self.project) / "broken"
        broken.mkdir(); (broken / "context.json").write_bytes(b"")
        with patch("pod.admission._fleet", side_effect=self._real_fleet), \
             patch("pod.admission._terminals", side_effect=self._real_terminals):
            answer = self.batch({"branch": "main"})
        result = answer["responses"][0]["result"]
        self.assertTrue(result["complete"])
        self.assertTrue(answer["native_fence"]["stable"])
        self.assertTrue(result["native_resources_complete"])
        self.assertRegex(result["snapshot"], r"^[0-9a-f]{64}$")
        self.assert_census(result, False)

    def test_missing_context_and_nonrecord_entries_are_not_classified(self):
        self.intake(worktree=self.place)
        root = state_root(self.project)
        (root / "empty").mkdir()
        (root / "not-a-record").write_text("private")
        for result in self.answers(branch="main"):
            self.assertTrue(result["complete"])
            self.assert_census(result, False)

    def test_unattributed_terminal_history_is_not_census_proof(self):
        self.write([self.criterion("satisfied", evidence=[self.proof("O1")])],
                   governance={"base_ref": "target"}, close=True, worktree=self.place)
        path, raw = self.raw()
        raw["checkpoint"].pop("worktree")
        self.save(path, raw)
        path.parent.rename(path.parent.with_name("unattributed-closed"))
        for result in self.answers(branch="main"):
            self.assertTrue(result["complete"])
            self.assertIn("record_attribution_unknown", result["unrelated_unknowns"]["by_code"])
            self.assert_census(result, False)

    def test_missing_claim_history_cannot_assert_completion(self):
        self.intake(worktree=self.place)
        path, raw = self.raw()
        raw["history"] = history.FORMAT
        raw["admissions"]["missing"] = base._reference({"private": "missing history"}, {})
        self.save(path, raw)
        for result in self.answers(branch="main"):
            self.assertFalse(result["repository_complete"])
            self.assert_census(result, False)

    def test_projection_omission_downgrades_witness(self):
        self.intake(worktree=self.place)
        made, witness = [], admission._RecordCensus.witness

        def observe(census, *args):
            result = witness(census, *args)
            made.append(result["complete"])
            return result

        with patch("pod.orca.MAX_OUTPUT", 500), patch.object(admission._RecordCensus, "witness", observe):
            result = self.query(branch="main")
        self.assertEqual(made, [True])
        self.assertEqual(result["snapshot"], None)
        self.assertIn("admission_projection_limit", [row["code"] for row in result["unknowns"]])
        self.assert_census(result, False)

    def test_missing_discovery_evidence_never_defaults_to_true(self):
        self.intake(worktree=self.place)
        real = admission._cohort

        def omitted(root, budget):
            real(root, budget)
            return []

        with patch("pod.admission._cohort", side_effect=omitted):
            self.assert_census(self.query(branch="main"), False)
        with patch("pod.admission._context", side_effect=PodError("unavailable", "fixture")):
            self.assert_census(self.query(branch="main"), False)


class ChangedCensusTests(CensusCase):
    def test_equivalent_journal_generation_change_is_still_census_incomplete(self):
        self.intake(worktree=self.place)
        base.ReviewCorrectionTests.journal(self)
        path = objective_root(self.project, "objective") / "governor.json"
        real = admission._after_journal
        changed = False

        def recheck(target):
            nonlocal changed
            if target == path and not changed:
                changed = True
                target.write_text(target.read_text() + "\n")
            return real(target)

        with patch("pod.admission._after_journal", side_effect=recheck):
            result = self.query(branch="main")
        self.assertTrue(result["complete"])
        self.assert_census(result, False)

    def test_changed_effect_classification_policy_cannot_assert_completion(self):
        self.intake(worktree=self.place)
        base.ReviewCorrectionTests.journal(self, kind="push", target="main")

        def policy(project):
            return {"revision": 1 if admission._SHARED.get().stage == "first" else 2,
                    "policy": {"waste_governor": {"triggers": {}}}}

        with patch("pod.config.effective", side_effect=policy):
            result = self.query(effects=["push:main"])
        self.assertIn("effect_policy_changed", [row["code"] for row in result["unknowns"]])
        self.assert_census(result, False)

    def test_unreadable_phase_closure_is_not_positive_classification(self):
        self.intake(worktree=self.place)
        directory = objective_root(self.project, "objective")
        (directory / "closure-evidence.json").write_bytes(b"not a closure")
        for result in self.answers(branch="main"):
            self.assertTrue(result["complete"])
            self.assert_census(result, False)

    def test_same_count_foreign_substitution_is_not_census_complete(self):
        self.intake(worktree=self.place)
        foreign = base.AttributionReadTests.foreign_record(self)

        def replace(root):
            shutil.rmtree(foreign.parent)
            (root / "replacement").mkdir()

        with self.at_recheck(replace):
            self.assert_census(self.query(branch="main"), False)

    def test_same_name_replacement_is_detected_even_with_identical_bytes(self):
        self.intake(worktree=self.place)
        path, _ = self.raw()

        def replace(root):
            saved = root / ".old"
            path.parent.rename(saved)
            shutil.copytree(saved, path.parent)

        with self.at_recheck(replace):
            result = self.query(branch="main")
        self.assertTrue(result["complete"])
        self.assert_census(result, False)

    def test_removed_excluded_record_keeps_old_query_semantics(self):
        self.intake(worktree=self.place)
        foreign = base.AttributionReadTests.foreign_record(self)
        before = self.query(branch="main")
        with self.at_recheck(lambda root: shutil.rmtree(foreign.parent)):
            after = self.query(branch="main")
        self.assertTrue(after["complete"])
        self.assertEqual(before["snapshot"], after["snapshot"])
        self.assert_census(after, False)

    def test_added_excluded_record_still_invalidates_census(self):
        self.intake(worktree=self.place)
        with self.at_recheck(lambda root: base.AttributionReadTests.foreign_record(self)):
            result = self.batch({"branch": "main"})["responses"][0]["result"]
        self.assertTrue(result["complete"])
        self.assert_census(result, False)

    def test_excluded_attribution_generation_is_rechecked(self):
        self.intake(worktree=self.place)
        foreign = base.AttributionReadTests.foreign_record(self)
        before = self.query(branch="main")
        with self.at_recheck(lambda root: foreign.write_text(foreign.read_text() + "\n")):
            after = self.query(branch="main")
        self.assertTrue(after["complete"])
        self.assertEqual(before["snapshot"], after["snapshot"])
        self.assert_census(after, False)

    def test_attribution_to_claims_reencoding_keeps_query_semantics_but_not_census(self):
        self.intake(worktree=self.place)
        path, _ = self.raw()
        rewritten = json.dumps(json.loads(path.read_bytes()), indent=2).encode()
        with self.claims_read_meets(path, rewritten, restore=False):
            result = self.query(branch="main")
        self.assertTrue(result["complete"])
        self.assert_census(result, False)

    def test_excluded_final_cohort_projection_cannot_assert_completion(self):
        self.intake(worktree=self.place)
        foreign = base.AttributionReadTests.foreign_record(self)
        real = admission._after_cohort

        def omitted(root, budget):
            after = real(root, budget)
            after.pop(foreign.parent.name)
            return after

        with patch("pod.admission._after_cohort", side_effect=omitted):
            self.assert_census(self.query(branch="main"), False)


class CensusDeadlineTests(CensusCase):
    def test_batch_envelope_serialization_spends_original_deadline(self):
        self.intake(worktree=self.place)
        captured, cohort = [], admission._cohort
        dumps = json.dumps

        def remember(root, budget):
            captured.append(budget)
            return cohort(root, budget)

        def serialize(value, *args, **kwargs):
            encoded = dumps(value, *args, **kwargs)
            if isinstance(value, dict) and value.get("schema") == "pod-admission-batch/v1":
                self.assertIs(value["responses"][0]["result"]["record_census"]["complete"], True)
                captured[0].deadline = captured[0]._clock() - 1
            return encoded

        with patch("pod.admission._cohort", side_effect=remember), \
             patch("pod.admission.json.dumps", side_effect=serialize):
            result = self.batch({"branch": "main"})["responses"][0]["result"]
        self.assertTrue(result["complete"])
        self.assert_census(result, False)

    def test_failed_native_end_fence_after_construction_clears_census(self):
        self.intake(worktree=self.place)
        constructed = threading.Event()
        witness, fence = admission._RecordCensus.witness, admission._fenced
        made = []

        def observe(census, *args):
            result = witness(census, *args)
            made.append(result["complete"])
            constructed.set()
            return result

        def fail(discovery):
            result = fence(discovery)
            self.assertTrue(constructed.wait(1), "per-query witness was never constructed")
            result["stable"] = False
            result["end"] = {"complete": False, "code": "fixture_late_failure", "count": None}
            return result

        with patch.object(admission._RecordCensus, "witness", observe), \
             patch("pod.admission._fenced", side_effect=fail):
            answer = self.batch({"branch": "main"})
        self.assertEqual(made, [True])
        self.assertFalse(answer["complete"])
        result = answer["responses"][0]["result"]
        self.assertTrue(result["complete"])
        self.assert_census(result, False)

    def test_interrupted_discovery_and_recheck_never_assert_completion(self):
        self.intake(worktree=self.place)
        for reader in ("_cohort", "_after_cohort", "_after_context"):
            with self.subTest(reader=reader), patch("pod.admission." + reader,
                    side_effect=PodError(DEADLINE_SPENT, "fixture cutoff")):
                self.assert_census(self.query(branch="main"), False)

    def test_one_shared_cutoff_reaches_every_batch_result(self):
        self.intake(worktree=self.place)
        real = admission._after_cohort

        def spend(root, budget):
            budget.deadline = budget._clock() - 1
            return real(root, budget)

        with patch("pod.admission._after_cohort", side_effect=spend):
            answer = self.batch({}, {"branch": "main"})
        for row in answer["responses"]:
            self.assert_census(row["result"], False)

    def test_fence_teardown_cannot_outlive_original_census_deadline(self):
        self.intake(worktree=self.place)
        close = admission._Discovery.close

        def finish(discovery):
            close(discovery)
            budget = captured[0]
            budget.deadline = budget._clock() - 1

        captured, cohort = [], admission._cohort

        def remember(root, budget):
            captured.append(budget)
            return cohort(root, budget)

        with patch("pod.admission._cohort", side_effect=remember), \
             patch.object(admission._Discovery, "close", finish):
            result = self.query(branch="main")
        self.assertTrue(result["complete"])
        self.assert_census(result, False)
