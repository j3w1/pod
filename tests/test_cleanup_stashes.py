"""Real Git material proofs, with explicit fixture-only native readback adapters."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch

from pod import cleanup_archives as archives, cleanup_observations as observations, cleanup_stashes as stashes
from pod.cleanup import plan
from pod.errors import PodError
from pod.util import digest
from tests import test_cleanup_plan as plans


class SourceFixture:
    def __init__(self):
        self.case = plans.CleanupPlanTests()
        self.case.setUp()
        case = self.case
        plans.git(case.child, "checkout", "-qb", "live")
        (case.child / "README.md").write_text("index fixture\n")
        plans.git(case.child, "add", "README.md")
        (case.child / "README.md").write_text("worktree fixture\n")
        (case.child / "untracked.txt").write_text("untracked fixture\n")
        plans.git(case.child, "stash", "push", "-uqm", "synthetic stash")
        self.ref = "refs/heads/objective"
        self.root = plans.git(case.project, "rev-parse", "refs/stash")
        self.common = case.project / ".git"
        self.private = Path(plans.git(case.child, "rev-parse", "--absolute-git-dir"))
        self.extra = {}
        self.unknown_holder = False
        self.reads = []

    def close(self):
        self.case.doCleanups()

    def reader(self, argv, **kwargs):
        case = self.case
        self.reads.append(list(argv))
        if argv[:2] == ["worktree", "show"]:
            path = argv[argv.index("--worktree") + 1].removeprefix("path:")
            branch = plans.git(Path(path), "symbolic-ref", "--short", "HEAD")
            row = {"id": "fixture-" + Path(path).name, "path": path, "branch": branch,
                   "isBare": False, "isMainWorktree": Path(path) == case.project,
                   "git": {"path": path, "branch": branch, "isBare": False}, **self.extra}
            return {"runtime": "fixture-runtime", "result": {"worktree": row}}
        if argv[:2] == ["terminal", "list"]:
            path = argv[argv.index("--worktree") + 1].removeprefix("path:")
            terminals = ([{"handle": "fixture-holder", "worktreePath": path,
                           "worktreeId": "fixture-" + Path(path).name}] if Path(path) == case.child else [])
            return {"runtime": "fixture-runtime", "result": {"terminals": terminals, "truncated": False}}
        if argv[:2] == ["terminal", "show"]:
            return {"runtime": "fixture-runtime", "result": {"terminal": {
                "handle": "fixture-holder", "worktreeId": "fixture-" + case.child.name,
                "connected": None if self.unknown_holder else True, "writable": True,
                "orphaned": False, **self.extra}}}
        raise AssertionError(argv)

    def observe(self, *, objective=None, kind="local_branch", name=None, **kwargs):
        return plan(self.case.project, objective, observe={"kind": kind, "name": name or self.ref},
                    orca_reader=self.reader, native_port=self.case.port, **kwargs)

    def row(self, **kwargs):
        return self.observe(**kwargs)["resources"][0]


class StashPathBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SourceFixture()
        cls.addClassCleanup(cls.source.close)
        cls.parent = cls.source.case.root / "outside-ancestor"
        cls.parent.mkdir()
        child = cls.parent / "checkout"
        plans.git(cls.source.case.project, "worktree", "move", str(cls.source.case.child), str(child))
        cls.source.case.child = child

    def context(self):
        repository = archives.repository_observation(self.source.case.project)
        return repository, stashes._context(self.source.case.project, repository)

    def replace_checkout_parent(self):
        child = self.source.case.child
        parked = self.source.case.root / "parked-ancestor"
        def restore():
            child.rmdir()
            self.parent.rmdir()
            parked.rename(self.parent)
        self.parent.rename(parked)
        self.addCleanup(restore)
        child.mkdir(parents=True)

    def test_shared_context_tolerates_unrelated_outside_ancestor_entries(self):
        repository, before = self.context()
        with plans.directory_entry_changes(self.source.case.root) as changes:
            after = stashes._context(self.source.case.project, repository)
        self.assertTrue(changes)
        self.assertEqual(after, before)

    def test_complete_catalog_tolerates_unrelated_outside_ancestor_entries(self):
        _, context = self.context()
        before = stashes._catalog(self.source.case.project, context, time.monotonic() + 30)
        with plans.directory_entry_changes(self.parent) as changes:
            after = stashes._catalog(self.source.case.project, context, time.monotonic() + 30)
        self.assertTrue(changes)
        self.assertEqual(after, before)
        self.assertEqual({row["path"] for row in after},
                         {str(self.source.case.project), str(self.source.case.child)})
        self.assertTrue(all(observations.reliable(row["identity"]) for row in after))

    def test_catalog_rejects_detached_checkout_binding_before_name_exclusion(self):
        _, context = self.context()
        original, identities = observations.filesystem, {}
        def inspect(path):
            identities[str(path)] = original(path)
            return identities[str(path)]
        with patch.object(observations, "filesystem", side_effect=inspect), \
                plans.path_change_on_fstat(self.parent, self.replace_checkout_parent) as changes, \
                self.assertRaises(archives.Unavailable):
            stashes._catalog(self.source.case.project, context, time.monotonic() + 30)
        self.assertTrue(changes)
        self.assertIsNone(identities[str(self.source.case.child)])

    def worktree_facts(self):
        repository, context = self.context()
        facts = archives.resource_observation(self.source.case.child, "refs/heads/live",
                                              self.source.case.base, repository)
        self.assertTrue(facts["complete"], facts)
        return facts, context

    def test_worktree_layout_tolerates_unrelated_outside_ancestor_entries(self):
        facts, context = self.worktree_facts()
        before = stashes._worktree_footprint(facts, context)
        self.assertEqual(before["status"], "observed")
        with plans.directory_entry_changes(self.parent) as changes:
            after = stashes._worktree_footprint(facts, context)
        self.assertTrue(changes)
        self.assertEqual(after, before)

    def test_worktree_layout_holds_detached_checkout_binding(self):
        facts, context = self.worktree_facts()
        with plans.path_change_on_fstat(self.parent, self.replace_checkout_parent) as changes:
            footprint = stashes._worktree_footprint(facts, context)
        self.assertTrue(changes)
        self.assertEqual(footprint["status"], "unavailable")
        self.assertEqual(footprint["opaque"], ["effect_footprint_unavailable"])

    def local_footprint(self, repository, context):
        storage, raw = observations.storage_once(self.source.case.project, self.source.ref, repository)
        self.assertEqual(storage["status"], "observed")
        def read():
            return stashes._footprint(self.source.case.project, self.source.ref, self.source.case.base,
                                     "local_branch", storage, context, packed_raw=raw.get("packed"),
                                     object_format=repository["object_format"])
        self.assertEqual(read()["status"], "observed")
        return read

    def test_ref_lock_appearance_after_genuine_absence_holds_the_footprint(self):
        repository, context = self.context()
        read = self.local_footprint(repository, context)
        lock, pending = self.source.common / "packed-refs.lock", b"fixture pending transaction\n"
        self.assertFalse(lock.exists())
        self.addCleanup(lock.unlink, missing_ok=True)
        with plans.path_change_on_fstat(self.source.common, lambda: lock.write_bytes(pending), occurrence=2) as changes:
            footprint = read()
        self.assertTrue(changes)
        self.assertEqual(footprint["status"], "unavailable")
        self.assertEqual(lock.read_bytes(), pending)

    def test_ref_lock_cannot_borrow_absence_from_detached_outside_ancestor(self):
        repository, context = self.context()
        read = self.local_footprint(repository, context)
        project, common = self.source.case.project, self.source.common
        parked, pending = self.source.case.root / "parked-project", b"fixture pending transaction\n"
        lock = common / "packed-refs.lock"
        ref_bytes, log_bytes = (common / "refs/stash").read_bytes(), (common / "logs/refs/stash").read_bytes()
        def replace():
            project.rename(parked)
            project.mkdir()
            (parked / ".git").rename(common)
            lock.write_bytes(pending)
            self.addCleanup(restore)
        def restore():
            lock.unlink()
            common.rename(parked / ".git")
            project.rmdir()
            parked.rename(project)
        original, slots = observations.slot, {}
        def inspect(path, root, **kwargs):
            value = original(path, root, **kwargs)
            slots[str(path)] = value[0]
            return value
        self.assertFalse(lock.exists())
        with patch.object(observations, "slot", side_effect=inspect), \
                plans.path_change_on_fstat(project, replace) as changes:
            footprint = read()
        self.assertTrue(changes)
        self.assertEqual(slots[str(lock)]["status"], "unavailable")
        self.assertEqual(footprint["status"], "unavailable")
        self.assertEqual(lock.read_bytes(), pending)
        self.assertEqual((common / "refs/stash").read_bytes(), ref_bytes)
        self.assertEqual((common / "logs/refs/stash").read_bytes(), log_bytes)


class StashSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SourceFixture()
        cls.addClassCleanup(cls.source.close)

    def setUp(self):
        self.source.extra = {}
        self.source.unknown_holder = False
        self.source.reads = []

    def preserve(self, path):
        data = path.read_bytes() if path.exists() else None
        def restore():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        self.addCleanup(restore)

    def assert_unknown(self, row):
        record = row["stash_observation"]
        self.assertTrue(stashes.holds(record), record)
        self.assertTrue(all(value["relation"] == "unknown" for value in record["relations"]), record)
        self.assertIsNone(row["delete_argv"])
        return record

    def controlled_record(self, record, before, after, *, query_names, liveness):
        """Pure-predicate fixture: the owned Git reader's entire request set is known.

        Native placement metadata cannot supply these facts in production. This
        helper adds no admission field and never turns the held plan into argv.
        """
        result = deepcopy(record)
        source = {"argv": ["git", "-C", str(self.source.case.child), "cat-file", "--batch"],
                  "queries": query_names, "read_result": before, "liveness": liveness}
        binding = {"argv": source["argv"], "requests": query_names}
        selected = record["resource"]["ref"]
        holder = {"binding_sha256": digest(binding), "resource_path": str(self.source.case.child),
                  "role": "unknown", "liveness": liveness,
                  "dependency": "inside" if selected in query_names else "outside", "source_sha256": digest(source)}
        result["holders"] = {"status": "observed", "observations": [holder], "reasons": [],
                             "witnesses": [observations.witness("git_read", "holder_read", source,
                                                                  reference="fixture.controlled_git_reader")]}
        stable = (before == after and record["complete"]
                  and record["freshness"]["before_sha256"] == record["freshness"]["after_sha256"]
                  and set(record["freshness"]["reasons"]) <= {"holder_unavailable"})
        result["freshness"] = {"before_sha256": digest([record["freshness"]["before_sha256"], source]),
                               "after_sha256": digest([record["freshness"]["after_sha256"], {**source, "read_result": after}]),
                               "stable": stable, "reasons": [] if stable else ["holder_changed"]}
        result["relations"] = []
        result["relations"] = stashes.relations(result)
        return result

    def complete_material(self, *, name=None, query=None):
        record = self.source.row(name=name)["stash_observation"]
        queries = [query] if query else plans.git(self.source.case.project, "rev-list", "--objects", "--no-object-names", self.source.root).splitlines()
        def read():
            result = subprocess.run(["git", "-C", str(self.source.case.child), "cat-file", "--batch"],
                                    input=("\n".join(queries) + "\n").encode(), capture_output=True, check=True)
            return hashlib.sha256(result.stdout).hexdigest()
        return self.controlled_record(record, read(), read(), query_names=queries, liveness="exited")

    def test_equal_base_full_material_positive_and_exact_fixture_effect_preserve_live_sibling(self):
        source, case = self.source, self.source.case
        self.assertEqual(plans.git(case.child, "rev-parse", "HEAD"), case.base)
        self.assertEqual(plans.git(case.project, "rev-parse", source.ref), case.base)
        row = source.row()
        record = row["stash_observation"]
        self.assertTrue(record["complete"], record)
        # The live sibling terminal belongs to another checkout outside this exact ref effect:
        # complete Git facts exclude it, so it is neither read nor a veto.
        self.assertEqual({value["relation"] for value in record["relations"]}, {"unrelated"}, record)
        self.assertEqual(record["executor"]["profile"], stashes.PROFILE)
        self.assertEqual((record["holders"]["status"], record["holders"]["observations"]), ("observed", []))
        self.assertEqual(source.reads, [])
        self.assertEqual(row["class"], "protected")
        self.assertIsNone(row["delete_argv"])  # Material proof supplies no missing Pod admission/authority.
        self.assertIsNone(row["archive_argv"])
        log_before = (source.common / "logs/refs/stash").read_bytes()
        ref_before = (source.common / "refs/stash").read_bytes()
        oids = plans.git(case.project, "rev-list", "--objects", "--no-object-names", source.root).splitlines()
        reader = subprocess.Popen(["git", "-C", str(case.child), "cat-file", "--batch"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        def contents():
            reader.stdin.write(("\n".join(oids) + "\n").encode())
            reader.stdin.flush()
            result = {}
            for oid in oids:
                header = reader.stdout.readline().decode().split()
                self.assertEqual(header[0], oid)
                body = reader.stdout.read(int(header[2]))
                self.assertEqual(reader.stdout.read(1), b"\n")
                result[oid] = hashlib.sha256(body).hexdigest()
            return result
        try:
            before = contents()
            self.assertIsNone(reader.poll())
            complete = self.controlled_record(record, before, contents(), query_names=oids, liveness="live")
            self.assertEqual({value["relation"] for value in complete["relations"]}, {"unrelated"}, complete)
            self.assertEqual(complete["relations"][0]["basis"]["kind"], "removal_dependency_exclusion")
            self.assertEqual(complete["holders"]["observations"][0]["dependency"], "outside")
            self.addCleanup(plans.git, case.project, "update-ref", source.ref, case.base)
            # Explicitly authorized disposable ref-only effect, independent of the held Pod row.
            command = stashes.local_argv(case.project, source.ref, case.base)
            self.assertEqual(command, ["git", "--no-optional-locks", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
                                       "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-C", str(case.project),
                                       "update-ref", "--no-deref", "-d", source.ref, case.base])
            subprocess.run(command, capture_output=True, check=True)
            self.assertEqual(contents(), before)
            self.assertIsNone(reader.poll())
            self.assertEqual((source.common / "logs/refs/stash").read_bytes(), log_before)
            self.assertEqual((source.common / "refs/stash").read_bytes(), ref_before)
            self.assertEqual(plans.git(case.project, "rev-parse", "refs/stash"), source.root)
            self.assertFalse((source.common / "packed-refs").exists())
        finally:
            reader.stdin.close()
            reader.wait(timeout=30)
            reader.stdout.close()

    def test_live_selected_binding_is_a_removal_dependency_even_when_all_bytes_survive(self):
        row = self.source.row(name="refs/heads/live")
        record = row["stash_observation"]
        # The live terminal is the deleted name's own checkout: an observed removal dependency.
        self.assertEqual({value["relation"] for value in record["relations"]}, {"related"}, record)
        self.assertTrue({"holder_live", "ref_alias_overlap"} <= set(record["relations"][0]["basis"]["reasons"]))
        complete = self.complete_material(name="refs/heads/live", query="refs/heads/live")
        self.assertEqual({value["relation"] for value in complete["relations"]}, {"related"}, complete)
        self.assertIn("ref_alias_overlap", complete["relations"][0]["basis"]["reasons"])
        self.assertIsNone(row["delete_argv"])

    def test_unknown_current_holder_keeps_even_a_complete_material_snapshot_held(self):
        # The holder is the deleted name's own checkout, so its unknown liveness is relevant.
        self.source.unknown_holder = True
        record = self.assert_unknown(self.source.row(name="refs/heads/live"))
        self.assertIn("holder_unavailable", record["relations"][0]["basis"]["reasons"])
        self.assertEqual(record["holders"]["observations"][0]["liveness"], "unverifiable")

    def test_footprint_reaching_another_checkout_is_overlap_not_exclusion(self):
        source, case = self.source, self.source.case
        repository = archives.repository_observation(case.project)
        context = stashes._context(case.project, repository)
        deadline = time.monotonic() + 30
        catalog = stashes._catalog(case.project, context, deadline)
        self.assertEqual({row["path"] for row in catalog}, {str(case.project), str(case.child)})
        refs = [{"name": "refs/heads/unlisted", "scope": "exact", "operation": "delete",
                 "expected_oid": case.base, "resolution": ["refs/heads/unlisted"]}]
        def holders(path):
            footprint = {"status": "observed", "refs": refs,
                         "paths": [{"path": path, "identity": None, "operation": "unlink"}]}
            return stashes._holders(case.project, footprint, None, "local_branch", source.reader, context,
                                    catalog, [], deadline)[0]
        reached = holders(str(case.child / "README.md"))
        self.assertEqual((reached["status"], reached["reasons"]), ("observed", ["storage_overlap"]))
        # The main checkout's Git directory is storage, not its working files.
        storage = holders(str(source.common / "refs/heads/unlisted"))
        self.assertEqual((storage["status"], storage["reasons"], storage["observations"]), ("observed", [], []))
        self.assertEqual(source.reads, [])
        unreadable = stashes._holders(case.project, {"status": "observed", "refs": refs, "paths": []}, None,
                                      "local_branch", source.reader, context, None, ["inspection_error"], deadline)[0]
        self.assertEqual(unreadable["status"], "unavailable")
        self.assertIn("inspection_error", unreadable["reasons"])

    def test_selected_helper_facts_name_only_applicable_drivers_without_running_them(self):
        case = self.source.case
        marker = case.root / "helper-facts-marker"
        for name in ("filter.a.b.clean", "filter.unused.process", "filter.smudged.smudge"):
            plans.git(case.project, "config", name, f"sh -c 'echo ran >> {marker}; cat'")
            self.addCleanup(plans.git, case.project, "config", "--unset", name)
        attributes = self.source.common / "info" / "attributes"
        self.preserve(attributes)
        attributes.parent.mkdir(exist_ok=True)
        attributes.write_text("[attr]dotted filter=a.b\n* dotted\nnot-tracked.txt filter=smudged\n*.none filter=unused\n")
        facts = stashes.helper_facts(case.child)
        # Host-level drivers such as an installed LFS filter may also be configured; inactive ones never apply.
        self.assertTrue({"a.b", "smudged", "unused"} <= set(facts["configured"]), facts)
        self.assertEqual(facts["applicable"], ["a.b"])
        self.assertGreater(facts["tracked_paths"], 0)
        self.assertFalse(marker.exists())
        attributes.write_text("*.none filter=unused\n")
        self.assertEqual(stashes.helper_facts(case.child)["applicable"], [])

    def test_configured_drivers_spelled_like_attribute_states_are_never_inactive(self):
        case = self.source.case
        marker = case.root / "sentinel-driver-marker"
        attributes = self.source.common / "info" / "attributes"
        self.preserve(attributes)
        attributes.parent.mkdir(exist_ok=True)
        def facts(text, *drivers):
            for driver in drivers:
                plans.git(case.project, "config", f"filter.{driver}.clean", f"sh -c 'echo ran >> {marker}; cat'")
            try:
                attributes.write_text(text)
                return stashes.helper_facts(case.child)["applicable"]
            finally:
                for driver in drivers:
                    plans.git(case.project, "config", "--remove-section", "filter." + driver)
        for name in ("set", "unset", "unspecified"):
            with self.subTest(literal=name):
                self.assertEqual(facts(f"* filter={name}\n", name), [name])
        # check-attr prints the same text for the attribute states themselves.
        self.assertEqual(facts("* filter\n", "set"), ["set"])
        self.assertEqual(facts("* -filter\n", "unset"), ["unset"])
        self.assertEqual(facts("", "unspecified"), ["unspecified"])
        # Undefined sentinel spellings and ordinary unselected drivers stay inactive.
        self.assertEqual(facts("* filter=set\n", "unused"), [])
        self.assertEqual(facts("* filter\n*.none filter=unused\n", "unused"), [])
        self.assertFalse(marker.exists())

    def test_shared_base_without_complete_effect_binding_is_unknown(self):
        record = self.complete_material()
        self.assertEqual({row["relation"] for row in record["relations"]}, {"unrelated"})
        record["executor"]["status"] = "unavailable"
        record["executor"]["source_binding_sha256"] = None
        record["executor"]["reasons"] = ["executor_binding_unavailable"]
        result = stashes.relations(record)
        self.assertEqual({row["relation"] for row in result}, {"unknown"})

    def test_own_head_referent_extra_reflog_effect_is_not_profiled(self):
        row = self.source.row(name="refs/heads/main")
        record = self.assert_unknown(row)
        self.assertEqual(record["footprint"]["status"], "unavailable")
        self.assertIn("effect_footprint_unavailable", record["footprint"]["reasons"])

    def test_direct_stash_ref_without_reflog_is_unknown_never_verified_empty(self):
        log = self.source.common / "logs/refs/stash"
        self.preserve(log)
        log.unlink()
        record = self.assert_unknown(self.source.row())
        self.assertEqual(record["reflog"]["status"], "unavailable")
        self.assertIsNone(record["reflog"]["entries"])
        self.assertFalse(record["complete"])
        self.assertEqual(plans.git(self.source.case.project, "rev-parse", "refs/stash"), self.source.root)

    def test_repeated_order_and_nonzero_old_only_root_are_all_preserved(self):
        source, case = self.source, self.source.case
        for path in (source.common / "refs/stash", source.common / "logs/refs/stash"):
            self.preserve(path)
        (case.child / "README.md").write_text("another supported stash\n")
        plans.git(case.child, "stash", "push", "-qm", "synthetic older root")
        old = plans.git(case.project, "rev-parse", "refs/stash")
        plans.git(case.project, "update-ref", "refs/stash", source.root)
        first = f"{old} {source.root} Fixture <fixture@example.invalid> 1 +0000\tsynthetic one\n"
        repeated = f"{source.root} {source.root} Fixture <fixture@example.invalid> 2 +0000\tsynthetic repeat\n"
        (source.common / "logs/refs/stash").write_text(first + repeated)
        record = source.row()["stash_observation"]
        self.assertTrue(record["complete"], record)
        self.assertEqual(record["reflog"]["entries"], [source.root, source.root])
        self.assertEqual({value["oid"] for value in record["snapshot"]}, {source.root, old})
        self.assertEqual(set(record["support"]["closure"]["roots"]), {source.root, old})
        self.assertEqual(len(record["relations"]), 2)

    def test_symbolic_selected_ref_does_not_borrow_stash_target_identity(self):
        path = self.source.common / self.source.ref
        self.preserve(path)
        plans.git(self.source.case.project, "symbolic-ref", self.source.ref, "refs/stash")
        row = self.source.row()
        record = self.assert_unknown(row)
        self.assertEqual(row["identity"]["ref_storage"]["current"]["symref"], "refs/stash")
        self.assertIn("ref_symbolic", row["reasons"])
        self.assertIn("ref_symbolic", record["resource"]["reasons"])

    def test_loose_and_packed_shadowed_storage_is_observed_but_not_cleared(self):
        source = self.source
        self.preserve(source.common / "packed-refs")
        plans.git(source.case.project, "pack-refs", "--all", "--no-prune")
        row = source.row()
        storage = row["identity"]["ref_storage"]
        self.assertEqual(storage["status"], "observed")
        self.assertEqual((storage["loose"]["status"], storage["packed"]["status"]), ("present", "present"))
        self.assert_unknown(row)

    def test_alternate_with_needed_untracked_object_in_selected_admin_is_unsupported(self):
        source, case = self.source, self.source.case
        oid = plans.git(case.project, "rev-parse", source.root + "^3:untracked.txt")
        original = source.common / "objects" / oid[:2] / oid[2:]
        alternate = source.private / "fixture-objects"
        destination = alternate / oid[:2] / oid[2:]
        destination.parent.mkdir(parents=True)
        original.rename(destination)
        self.addCleanup(destination.rename, original)
        config = source.common / "objects/info/alternates"
        self.preserve(config)
        config.write_text(str(alternate) + "\n")
        self.assertEqual(plans.git(case.project, "cat-file", "-t", oid), "blob")
        self.assert_unknown(source.row())

    def test_missing_alias_or_incarnation_fact_is_unknown(self):
        original = observations.identity
        def missing(*args, **kwargs):
            return {**original(*args, **kwargs), "mount_id": None}
        with patch.object(observations, "identity", side_effect=missing):
            self.assert_unknown(self.source.row())

    def test_needed_private_name_alias_is_not_excluded_by_surviving_object_bytes(self):
        source, case = self.source, self.source.case
        private = "refs/worktree/needed-name"
        plans.git(case.child, "symbolic-ref", private, source.ref)
        self.addCleanup(plans.git, case.child, "symbolic-ref", "--delete", private)
        qualified = "worktrees/" + source.private.name + "/" + private
        self.assertEqual(plans.git(case.project, "rev-parse", "--verify", qualified), case.base)
        record = self.complete_material(query=qualified)
        self.assertEqual({row["relation"] for row in record["relations"]}, {"related"}, record)
        self.assertIn("ref_alias_overlap", record["relations"][0]["basis"]["reasons"])

    def test_symlinked_storage_parent_is_not_successful_alternate_absence(self):
        source = self.source
        original = source.common / "objects/info"
        parked = source.case.root / "parked-object-info"
        original.rename(parked)
        original.symlink_to(parked, target_is_directory=True)
        def restore():
            original.unlink()
            parked.rename(original)
        self.addCleanup(restore)
        self.assert_unknown(source.row())

    def test_trace_destination_is_refused_before_git_can_spool_metadata(self):
        destination = self.source.case.root / "synthetic-trace"
        with patch.dict(os.environ, {"GIT_TRACE": str(destination)}), self.assertRaises(PodError):
            self.source.row()
        self.assertFalse(destination.exists())

    def test_malformed_current_stash_commit_is_not_an_empty_success(self):
        source = self.source
        for path in (source.common / "refs/stash", source.common / "logs/refs/stash"):
            self.preserve(path)
        plans.git(source.case.project, "update-ref", "-m", "synthetic malformed shape", "refs/stash", source.case.base)
        record = self.assert_unknown(source.row())
        self.assertFalse(record["complete"])
        self.assertEqual(record["reflog"]["ref"]["oid"], source.case.base)

    def test_missing_untracked_blob_in_required_closure_keeps_unknown(self):
        source, case = self.source, self.source.case
        oid = plans.git(case.project, "rev-parse", source.root + "^3:untracked.txt")
        original = source.common / "objects" / oid[:2] / oid[2:]
        parked = case.root / "parked-untracked-object"
        original.rename(parked)
        self.addCleanup(parked.rename, original)
        self.assert_unknown(source.row())

    def test_stash_log_changes_during_inspection_are_not_stable_even_with_same_root(self):
        source = self.source
        log = source.common / "logs/refs/stash"
        self.preserve(log)
        original, changed = archives._run, False
        def run(root, argv, **kwargs):
            nonlocal changed
            result = original(root, argv, **kwargs)
            if argv[0] == "fsck" and not changed:
                with log.open("ab") as output:
                    output.write(f"{source.root} {source.root} Fixture <fixture@example.invalid> 1 +0000\tsynthetic race\n".encode())
                changed = True
            return result
        with patch.object(archives, "_run", side_effect=run):
            record = self.assert_unknown(source.row())
        self.assertTrue(changed)
        self.assertFalse(record["freshness"]["stable"])
        self.assertIn("reflog_changed", record["freshness"]["reasons"])

    def test_budget_exhaustion_is_unavailable_not_empty(self):
        with patch.object(archives, "MAX_OUTPUT", 64), self.assertRaises(PodError) as caught:
            self.source.row()
        self.assertEqual(caught.exception.code, "cleanup_unavailable")

    def test_verified_empty_requires_actual_ref_and_log_absence(self):
        source = self.source
        for path in (source.common / "refs/stash", source.common / "logs/refs/stash"):
            self.preserve(path)
        plans.git(source.case.project, "stash", "clear")
        record = source.row()["stash_observation"]
        self.assertTrue(record["complete"], record)
        self.assertEqual(record["snapshot"], [])
        self.assertEqual(record["relations"], [])
        self.assertEqual(record["reflog"]["status"], "absent")
        self.assertEqual(record["reflog"]["entries"], [])
        self.assertFalse(stashes.holds(record))
        changed = deepcopy(record)
        changed["reflog"]["ref"]["status"] = "present"
        changed["reflog"]["ref"]["oid"] = source.root
        self.assertTrue(stashes.holds(changed))

    def test_config_or_native_or_cache_extra_effect_is_not_the_narrow_primitive(self):
        original = self.complete_material()
        self.assertEqual({row["relation"] for row in original["relations"]}, {"unrelated"})
        for key, value in (("config", {"section": "branch.objective", "operation": "remove_section", "sha256": None}),
                           ("native", {"kind": "cache", "operation": "remove", "binding_sha256": None}),
                           ("paths", {"path": str(self.source.case.root / "cache"), "identity": None, "operation": "remove_tree"})):
            record = deepcopy(original)
            record["footprint"][key].append(value)
            record["footprint"]["sha256"] = digest({key: record["footprint"][key]
                                                     for key in ("paths", "refs", "config", "native", "opaque")})
            self.assertEqual({row["relation"] for row in stashes.relations(record)}, {"unknown"})

    def test_missing_null_or_unrecognized_required_fact_cannot_be_unrelated(self):
        original = self.complete_material()
        self.assertEqual({row["relation"] for row in original["relations"]}, {"unrelated"})
        for part, key, value in (("support", "refs", None), ("support", "object_dirs", None),
                                 ("holders", "observations", None), ("executor", "profile", "unknown/v1"),
                                 ("footprint", "opaque", ["unsupported_primitive"])):
            record = deepcopy(original)
            record[part][key] = value
            self.assertEqual({row["relation"] for row in stashes.relations(record)}, {"unknown"})

    def test_unrelated_material_does_not_forge_unique_or_clear_an_independent_admission(self):
        complete = self.complete_material()
        row = {"kind": "local_branch", "class": "unique", "reasons": ["branch has unique work or a repository stash"],
               "delete_argv": None, "archive_argv": ["git", "bundle", "create", "fixture.bundle"]}
        stashes.apply(row, complete, branch_integrated=True)
        self.assertEqual(row["class"], "protected")
        self.assertIsNone(row["delete_argv"])
        self.assertIsNone(row["archive_argv"])
        unique = {"kind": "local_branch", "class": "unique", "reasons": ["branch has commits outside the observed target"],
                  "delete_argv": None, "archive_argv": ["git", "bundle", "create", "fixture.bundle"]}
        stashes.apply(unique, complete, branch_integrated=False)
        self.assertEqual(unique["class"], "unique")
        self.assertIsNone(unique["delete_argv"])


if __name__ == "__main__":
    unittest.main()
