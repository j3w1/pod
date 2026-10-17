"""R120: real verifier processes at the public Governor authorization boundaries."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

from pod import relay
from pod.config import defaults, personal_path, validate
from pod.errors import PodError
from pod.governor import _read_journal, _record_path, observe_candidate, prepare_candidate
from pod.ledger import _validate_context, objective_root, read, state_root
from pod.util import context_decisions
from tests.kernel_support import action, git


BASELINE = "b69795cabae3855ed0ec4c68ed1f2bfd7244ce3c"
REPOSITORY = Path(__file__).resolve().parents[1]
VERIFIER = '''import json, os, signal, sys
from pathlib import Path
controls = json.loads(Path(sys.argv[1]).read_text())
request = json.load(sys.stdin)
with Path(sys.argv[2]).open("a") as log:
    log.write(json.dumps(request) + "\\n")
mode = controls.get("mode")
if mode == "fail":
    print("verifier-private-sentinel", file=sys.stderr)
    print("verifier-private-sentinel")
    sys.exit(3)
if mode in ("timeout", "closed_stdout"):
    if mode == "closed_stdout":
        os.close(1)
    signal.pause()
if mode == "invalid_json":
    print("verifier-private-sentinel")
    sys.exit()
if mode == "invalid_utf8":
    os.write(1, b"\\xff")
    sys.exit()
if mode == "oversized":
    os.write(1, b"x" * 32768)
    signal.pause()
verdict = {**request, "verified": True, "owner_source": "authenticated-owner:fixture",
           "decided_at": "2026-09-21T00:00:00Z", "single_use": "decision-one"}
for path, value in controls.get("changes", {}).items():
    target = verdict
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
for key in controls.get("omit", []):
    verdict.pop(key)
text = json.dumps(verdict)
if mode == "duplicate":
    text = text[:-1] + ', "verified": true}'
print(text)
'''


class RelayCase(unittest.TestCase):
    def setUp(self):
        from tests.test_finalization import ResumeTests
        self.case = ResumeTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.project = self.case.project
        git(self.project, "remote", "set-url", "origin", "https://github.com/acme/widgets.git")
        self.controls = self.case.root / "verifier-controls.json"
        self.requests = self.case.root / "verifier-requests.jsonl"
        self.script = self.case.root / "verifier.py"
        self.script.write_text(VERIFIER)
        self.controls.write_text("{}")
        self.command = [sys.executable, str(self.script), str(self.controls), str(self.requests)]
        self.configure(self.command)

    def configure(self, command):
        path = personal_path(self.project)
        value = yaml.safe_load(path.read_text())
        if command is None:
            value.pop("authorization_verifier", None)
        else:
            value["authorization_verifier"] = command
        path.write_text(yaml.safe_dump(value))

    def control(self, **fields):
        self.controls.write_text(json.dumps(fields))

    def request(self, scope="publish", **authorization_fields):
        authorization = {**self.case.consent(scope), "target": "origin/target", "channel": "relayed",
                         "relay": {"reference": "owner-decision-one"}, **authorization_fields}
        if scope == "merge":
            authorization.setdefault("pr", 23)
        return action(kind="merge" if scope == "merge" else "push", unit="delivery",
                      candidate=self.case.observed["commit"],
                      target="origin/target" if scope == "merge" else "origin/candidate",
                      effects=[], authorization=authorization, **({"pr": authorization["pr"]} if scope == "merge" else {}))

    def journal(self):
        return _read_journal(_record_path(self.project, "objective"))

    def refuse(self, code, *, operation="governor-execute", request=None):
        path = _record_path(self.project, "objective")
        before = path.read_bytes()
        calls = deepcopy(self.case.remote.calls)
        with self.assertRaises(PodError) as caught:
            self.case.op(operation, action=request or self.request())
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertTrue(caught.exception.detail["next_action"])
        self.assertNotIn("verifier-private-sentinel", str(caught.exception) + json.dumps(caught.exception.detail))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.case.remote.calls, calls)
        return caught.exception

    def verdict(self, scope="publish"):
        request = self.request(scope)
        result = self.case.op("governor" if scope == "merge" else "governor-execute", action=request)
        self.assertEqual(result["decision"], "ALLOW", result)
        return self.journal()["actions"][-1]["action"]["authorization"]["relay_verification"]


class RelayAcceptanceTests(RelayCase):
    def test_publish_executes_with_verifier_source_and_honest_channel(self):
        request = self.request(relay={"reference": "owner-decision-one", "owner_source": "untrusted assertion"})
        result = self.case.op("governor-execute", action=request)
        self.assertEqual((result["decision"], result["outcome"]), ("ALLOW", "PASS"))
        journal = self.journal()
        stored = journal["actions"][-1]["action"]["authorization"]
        self.assertEqual(stored["channel"], "relayed")
        self.assertEqual(stored["relay"], {"reference": "owner-decision-one", "owner_source": "authenticated-owner:fixture"})
        self.assertEqual(stored["relay_verification"]["action"], "publish")
        self.assertEqual(journal["decision_channels"], 2)
        self.assertEqual(sum(call[0] == "push" for call in self.case.remote.calls), 1)
        sent = json.loads(self.requests.read_text().splitlines()[0])
        self.assertEqual(len(self.requests.read_text().splitlines()), 2)
        self.assertEqual(sent["target"], {"repository": "acme/widgets", "remote": "origin", "base": "target"})
        self.assertEqual(sent["objective"], "objective")
        self.assertEqual(sent["candidate"], {"commit": self.case.observed["commit"], "tree": self.case.observed["tree"]})
        self.assertIsNone(sent["pr"])

    def test_merge_binds_pr_and_replays_one_record_after_fresh_verification(self):
        request = self.request("merge")
        result = self.case.op("governor", action=request)
        again = self.case.op("governor", action=request)
        self.assertEqual((result["decision"], again["decision"]), ("ALLOW", "REUSE"))
        self.assertEqual(result["record_id"], again["record_id"])
        self.assertIn("verified PR #23 with --match-head-commit", result["next_action"])
        self.assertEqual(len(self.journal()["actions"]), 1)
        self.assertEqual(len(self.requests.read_text().splitlines()), 2)
        stored = self.journal()["actions"][0]["action"]["authorization"]
        self.assertEqual((stored["scope"], stored["pr"], stored["relay_verification"]["pr"]), (["merge"], 23, 23))
        self.assertEqual(self.case.remote.calls, [])

    def test_status_and_closure_show_relay_without_running_verifier(self):
        self.verdict()
        calls = self.requests.read_bytes()
        self.script.unlink()  # Read-only projections use stored evidence.
        from pod.status import status as read_status
        from pod.operations import OrcaPort
        with patch.object(OrcaPort, "read_native", side_effect=self.case.port.read_native):
            status = read_status(self.project, "run", objective="objective",
                                 worker_rows_fn=lambda _: {"workers": [], "scope": {"run_id": "run"}, "complete": True})
        owners = [r for r in status["owner_decisions"] if r["kind"] == "authorization"]
        self.assertEqual(owners[0]["decision"]["channel"], "relayed")
        governor = self.case.op("governor-status")
        self.assertEqual(governor["units"]["delivery"]["authorization"]["publish"], "RECORDED")
        closed = self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        report = closed["checkpoint"]["closure"]["report"]
        self.assertTrue(any(r["kind"] == "authorization" and r["decision"]["channel"] == "relayed"
                            for r in report["owner_decisions"]))
        self.assertEqual(read(self.project, "objective")["decision_channels"], 2)
        self.assertEqual(self.requests.read_bytes(), calls)

    def test_ordinary_session_consent_and_other_relay_kinds_need_no_verifier(self):
        self.configure(None)
        request = self.request()
        request["authorization"].pop("relay")
        request["authorization"]["channel"] = "session"
        result = self.case.op("governor-execute", action=request)
        self.assertEqual((result["decision"], result["outcome"]), ("ALLOW", "PASS"))
        self.assertFalse(self.requests.exists())
        self.assertEqual(self.journal()["decision_channels"], 1)


class RelayRefusalTests(RelayCase):
    def test_absent_verifier_refuses_all_forward_entries(self):
        self.configure(None)
        for operation, scope in (("governor", "publish"), ("governor-execute", "publish"), ("governor", "merge")):
            with self.subTest(operation=operation, scope=scope):
                self.refuse("relay_verifier_unconfigured", operation=operation, request=self.request(scope))
        self.assertFalse(self.requests.exists())

    def test_nonzero_and_unavailable_verifier_are_failures_without_output(self):
        self.control(mode="fail")
        self.refuse("relay_verifier_failed")
        self.configure([str(self.case.root / "absent-verifier")])
        self.refuse("relay_verifier_failed")

    def test_timeout_kills_a_blocked_verifier(self):
        self.control(mode="timeout")
        with patch.object(relay, "TIMEOUT_S", .15):
            self.refuse("relay_verifier_timeout")

    def test_timeout_also_bounds_a_verifier_that_closed_stdout(self):
        self.control(mode="closed_stdout")
        with patch.object(relay, "TIMEOUT_S", .15):
            self.refuse("relay_verifier_timeout")

    def test_malformed_json_utf8_duplicate_and_oversized_output_refuse(self):
        for mode in ("invalid_json", "invalid_utf8", "duplicate", "oversized"):
            with self.subTest(mode=mode):
                self.control(mode=mode)
                self.refuse("relay_verifier_malformed")

    def test_missing_fields_and_invalid_verdict_values_refuse(self):
        for change in ({"verified": 1}, {"decided_at": "yesterday"}, {"owner_source": ""},
                       {"single_use": "k" * 513}, {"schema": "unknown"}, {"extra": "untrusted"}):
            with self.subTest(change=change):
                self.control(changes=change)
                self.refuse("relay_verifier_malformed")
        self.control(omit=["single_use"])
        self.refuse("relay_verifier_malformed")

    def test_explicit_denial_refuses(self):
        self.control(changes={"verified": False})
        self.refuse("relay_verifier_denied")

    def test_action_objective_and_reference_mismatches_refuse(self):
        for changes in ({"action": "publish", "pr": None}, {"objective": "another"}, {"reference": "another"}):
            with self.subTest(changes=changes):
                self.control(changes=changes)
                self.refuse("relay_verifier_mismatch", operation="governor", request=self.request("merge"))

    def test_commit_and_tree_mismatches_refuse(self):
        for field in ("commit", "tree"):
            with self.subTest(field=field):
                self.control(changes={"candidate." + field: "f" * 40})
                self.refuse("relay_verifier_mismatch")

    def test_repository_remote_base_and_pr_mismatches_refuse(self):
        for field, value in (("target.repository", "other/widgets"), ("target.remote", "upstream"),
                             ("target.base", "other-base"), ("pr", 24)):
            with self.subTest(field=field):
                self.control(changes={field: value})
                self.refuse("relay_verifier_mismatch", operation="governor", request=self.request("merge"))

    def test_release_deploy_and_combined_scopes_remain_session_only(self):
        for scopes in (["release"], ["deploy"], ["publish", "merge"], ["publish", "release"]):
            request = self.request()
            request["authorization"]["scope"] = scopes
            with self.subTest(scopes=scopes):
                self.refuse("relayed_authorization_refused", request=request)
        self.assertFalse(self.requests.exists())

    def test_session_with_relay_metadata_refuses_and_stripped_origins_are_not_provable(self):
        request = self.request(channel="session")
        self.refuse("invalid_decision_relay", request=request)
        # No claim is made about hidden keystroke origins: completely stripped provenance
        # is indistinguishable from the ordinary session input tested above.
        self.assertFalse(self.requests.exists())

    def test_caller_cannot_supply_verifier_evidence_as_authority(self):
        verdict = self.verdict()
        self.refuse("invalid_authorization", request=self.request(relay_verification=verdict))


class RelayBoundaryTests(RelayCase):
    def test_governed_merge_pr_must_match_authorization_before_verification(self):
        request = self.request("merge")
        request["pr"] = 24
        self.refuse("relay_verifier_mismatch", operation="governor", request=request)
        request.pop("pr")
        self.refuse("relay_verifier_mismatch", operation="governor", request=request)
        self.assertFalse(self.requests.exists())

    def test_candidate_movement_between_admission_and_effect_refuses(self):
        remote = self.case.remote
        def moved(_project):
            (self.project / "README.md").write_text("moved at mutation boundary\n")
            git(self.project, "add", "README.md")
            git(self.project, "commit", "-qm", "boundary movement")
            current = observe_candidate(self.project, base_ref="refs/remotes/origin/target")
            prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=current)
            return remote
        with patch("pod.governor_effects._port", side_effect=moved), self.assertRaises(PodError) as caught:
            self.case.op("governor-execute", action=self.request())
        self.assertEqual(caught.exception.code, "relay_verifier_mismatch")
        self.assertEqual(remote.calls, [])
        row = self.journal()["actions"][0]
        self.assertEqual((row["outcome"], row["receipt"]["detail"]), ("FAILED", "relay_verifier_mismatch"))

    def test_revocation_between_admission_and_effect_refuses(self):
        def revoked(_project):
            self.control(changes={"verified": False})
            return self.case.remote
        with patch("pod.governor_effects._port", side_effect=revoked), self.assertRaises(PodError) as caught:
            self.case.op("governor-execute", action=self.request())
        self.assertEqual(caught.exception.code, "relay_verifier_denied")
        self.assertEqual(self.case.remote.calls, [])
        self.assertEqual(self.journal()["actions"][0]["outcome"], "FAILED")

    def test_changed_verdict_identity_between_admission_and_effect_refuses(self):
        def changed(_project):
            self.control(changes={"single_use": "different-decision"})
            return self.case.remote
        with patch("pod.governor_effects._port", side_effect=changed), self.assertRaises(PodError) as caught:
            self.case.op("governor-execute", action=self.request())
        self.assertEqual(caught.exception.code, "relay_verifier_mismatch")
        self.assertEqual(self.case.remote.calls, [])

    def test_candidate_movement_after_consent_holds_managed_effect(self):
        request = self.request()
        self.case.op("governor", action=request)
        (self.project / "README.md").write_text("changed after consent\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "changed candidate")
        self.case.candidate = git(self.project, "rev-parse", "HEAD")
        self.case.write(self.case.stored())
        current = observe_candidate(self.project, base_ref="refs/remotes/origin/target")
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=current)
        before = deepcopy(self.case.remote.calls)
        result = self.case.op("governor-execute", action=request)
        self.assertEqual(result["decision"], "DEFER")
        self.assertIn("authorization_missing", [r["code"] for r in result["reasons"]])
        self.assertEqual(self.case.remote.calls, before)

    def test_verifier_is_rechecked_and_revocation_holds_replay(self):
        self.verdict("merge")
        self.control(changes={"verified": False})
        self.refuse("relay_verifier_denied", operation="governor", request=self.request("merge"))
        self.assertEqual(len(self.requests.read_text().splitlines()), 2)

    def test_current_remote_identity_is_checked_at_the_boundary(self):
        self.verdict()
        git(self.project, "remote", "set-url", "origin", "https://github.com/other/widgets.git")
        # A verifier that still holds the original target cannot approve a changed remote.
        self.control(changes={"target.repository": "acme/widgets"})
        self.refuse("relay_verifier_mismatch")

    def test_publish_uses_the_actual_push_repository(self):
        git(self.project, "remote", "set-url", "--push", "origin", "https://github.com/other/widgets.git")
        self.control(changes={"target.repository": "acme/widgets"})
        self.refuse("relay_verifier_mismatch")

    def test_new_candidate_cannot_reuse_consumed_key(self):
        self.verdict()
        (self.project / "README.md").write_text("another authorized candidate\n")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-qm", "new candidate")
        self.case.candidate = git(self.project, "rev-parse", "HEAD")
        self.case.write(self.case.stored())
        self.case.observed = observe_candidate(self.project, base_ref="refs/remotes/origin/target")
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=self.case.observed)
        self.refuse("relay_authorization_reused")

    def test_another_objective_cannot_reuse_consumed_key(self):
        self.verdict()
        self.case.write([self.case.criterion()], objective="another", governance={"base_ref": "target"})
        prepare_candidate(self.project, "another", owner="owner", unit="delivery", observation=self.case.observed,
                          branch={"remote": "origin", "base": "target", "branch": "candidate"})
        path = _record_path(self.project, "another")
        before = path.read_bytes()
        from pod.internal import run
        with self.assertRaises(PodError) as caught:
            run("governor-execute", {"project": str(self.project), "objective": "another", "owner": "owner",
                                     "action": self.request()})
        self.assertEqual(caught.exception.code, "relay_authorization_reused")
        self.assertEqual(path.read_bytes(), before)

    def test_concurrent_different_claims_allow_only_one_use(self):
        verdict = self.verdict()
        # A fresh key exercises atomic first consumption rather than an already-used receipt.
        left = {**verdict, "single_use": "concurrent-decision"}
        right = {**left, "objective": "another"}
        def claim(value):
            try:
                relay.check_use(self.project, value, consume=True)
                return "accepted"
            except PodError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, (left, right)))
        self.assertEqual(sorted(outcomes), ["accepted", "relay_authorization_reused"])

    def test_receipts_do_not_appear_as_unattributed_objectives(self):
        self.verdict()
        from pod.listing import objective_list
        result = objective_list(self.project)
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["unattributed_records"], [])

    def test_one_key_cannot_authorize_another_action_or_pr(self):
        self.verdict("merge")
        self.refuse("relay_authorization_reused", operation="governor", request=self.request("merge", pr=24))
        self.refuse("relay_authorization_reused", request=self.request())

    def test_replay_receipts_survive_journal_loss_and_apply_across_objectives(self):
        verdict = self.verdict()
        _record_path(self.project, "objective").unlink()  # Synthetic journal-retention control; receipt stays.
        for path, value in (("objective", "other-objective"), ("candidate.commit", "f" * 40),
                            ("candidate.tree", "f" * 40), ("target.base", "other"),
                            ("target.repository", "other/widgets"), ("reference", "another-reference")):
            changed = deepcopy(verdict)
            parts = path.split(".")
            target = changed
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
            with self.subTest(path=path), self.assertRaises(PodError) as caught:
                relay.check_use(self.project, changed, consume=True)
            self.assertEqual(caught.exception.code, "relay_authorization_reused")
        relay.check_use(self.project, verdict, consume=True)  # Same authorization stays idempotent.
        receipts = list((state_root(self.project) / ".relay-authorizations").glob("*.json"))
        self.assertEqual(len(receipts), 1)

    def test_changed_prepared_base_refuses_old_authorization(self):
        self.verdict()
        prepare_candidate(self.project, "objective", owner="owner", unit="delivery", observation=self.case.observed,
                          branch={"remote": "origin", "base": "other", "branch": "candidate"})
        self.assertEqual(self.case.op("governor-status")["units"]["delivery"]["authorization"]["publish"], "MISSING")
        self.refuse("relay_verifier_mismatch")


class RelayConfigTests(unittest.TestCase):
    def test_verifier_is_absent_by_default_and_project_cannot_supply_it(self):
        self.assertNotIn("authorization_verifier", defaults())
        for value in ("verifier --check", [], ["relative-command"], ["/bin/true", ""],
                      ["/bin/true", "x" * 4097], ["/bin/true"] * 17):
            with self.subTest(value=value), self.assertRaises(PodError):
                validate({**defaults(), "authorization_verifier": value})
        with self.assertRaises(PodError):
            validate({"schema": "pod/v1", "authorization_verifier": ["/bin/true"]}, project=True)
        self.assertEqual(validate({**defaults(), "authorization_verifier": ["/bin/true"]})["authorization_verifier"],
                         ["/bin/true"])


class RelayCompatibilityTests(RelayCase):
    @classmethod
    def setUpClass(cls):
        parent = Path(os.environ["POD_CACHE_HOME"]) if "POD_CACHE_HOME" in os.environ else None
        if parent:
            parent.mkdir(parents=True, exist_ok=True)
        cls.previous_home = tempfile.TemporaryDirectory(prefix="relay-previous-", dir=parent)
        cls.addClassCleanup(cls.previous_home.cleanup)
        cls.bundle = Path(cls.previous_home.name)
        archive = cls.bundle / "source.tar"
        subprocess.run(["git", "-C", str(REPOSITORY), "archive", "-o", str(archive), BASELINE,
                        "skills", "VERSION"], check=True)
        with tarfile.open(archive) as source:
            source.extractall(cls.bundle, filter="data")

    def earlier(self, code, *args):
        result = subprocess.run([sys.executable, "-c", code, *map(str, args)], cwd=REPOSITORY,
            env={**os.environ, "PYTHONPATH": os.pathsep.join((str(self.bundle / "skills"), str(REPOSITORY)))},
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_genuine_0170_refuses_verified_journal_context_and_authorization(self):
        self.verdict()
        journal_path = _record_path(self.project, "objective")
        context_path = objective_root(self.project, "objective") / "context.json"
        self.case.write([self.case.criterion("satisfied", evidence=[self.case.proof("O1")])], close=True)
        before = (journal_path.read_bytes(), context_path.read_bytes())
        results = self.earlier('''import json, sys
from pathlib import Path
from pod import __version__
from pod.errors import PodError
from pod.governor import _read_journal, validate_authorization
from pod.ledger import _read
assert __version__ == "0.17.0"
journal, context = map(Path, sys.argv[1:])
authorization = json.loads(journal.read_bytes())["actions"][0]["action"]["authorization"]
outcomes = []
for operation in (lambda: _read_journal(journal), lambda: _read(context), lambda: validate_authorization(authorization)):
    try:
        operation()
    except PodError as exc:
        outcomes.append(exc.code)
    else:
        outcomes.append("incorrectly accepted")
print(json.dumps(outcomes))
''', journal_path, context_path)
        self.assertEqual(results, ["state_unsupported", "state_unsupported", "invalid_authorization"])
        self.assertEqual((journal_path.read_bytes(), context_path.read_bytes()), before)
        _validate_context(json.loads(context_path.read_bytes()))
        self.journal()

    def test_genuine_0170_session_journal_reads_unchanged(self):
        destination = self.case.root / "previous"
        destination.mkdir()
        result = self.earlier('''import json, sys
from pathlib import Path
from pod import __version__
from pod.governor import _read_journal, _record_path, decide
from pod.ledger import read
from tests.test_finalization import ResumeTests
from tests.kernel_support import NOW
assert __version__ == "0.17.0"
case = ResumeTests(); case.setUp()
try:
    result = decide(case.project, "objective", owner="owner", action=case.merge(), now=NOW)
    path = Path(sys.argv[1])
    path.joinpath("context.json").write_bytes(__import__("pod.ledger", fromlist=["objective_root"]).objective_root(case.project, "objective").joinpath("context.json").read_bytes())
    path.joinpath("governor.json").write_bytes(_record_path(case.project, "objective").read_bytes())
    print(json.dumps({"version": __version__, "decision": result["decision"], "writer": "b69795cabae3855ed0ec4c68ed1f2bfd7244ce3c"}))
finally:
    case.doCleanups()
''', destination)
        self.assertEqual((result["version"], result["decision"], result["writer"]), ("0.17.0", "ALLOW", BASELINE))
        for name, reader in (("context.json", lambda p: _validate_context(json.loads(p.read_bytes()))),
                             ("governor.json", _read_journal)):
            path = destination / name
            before = path.read_bytes()
            self.assertEqual(reader(path), json.loads(before))
            self.assertEqual(path.read_bytes(), before)
        self.assertTrue(all(r["decision"]["channel"] == "session"
                            for r in context_decisions({}, _read_journal(destination / "governor.json"))))
