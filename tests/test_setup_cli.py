import json
import os
import io
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from pod.cli import execute, main, parser
from pod.config import read_yaml
from pod.errors import PodError
from pod.bundle import BUNDLE_FILES, canonical, version
from pod.setup import _relative_key, inspect, setup
from pod.ledger import checkpoint
from tests.common import fixture

ACCOUNT_IDENTITY = "a" * 64


class SetupCliTests(unittest.TestCase):
    def approval_native(self, *, identity=ACCOUNT_IDENTITY, auth="oauth"):
        billing_login = {"auth": auth, "subscription": auth == "oauth",
                         "identity_digest": None}
        accounts = {"runtime": "runtime", "providers": {"codex": {
            "managed_accounts": 0, "default_present": True,
            "default_identity": identity, "default_auth": auth,
            "default_has_auth": auth in ("oauth", "api_key"), "windows": {}}}}
        return accounts, billing_login

    def test_guided_approve_and_revoke_write_only_after_bound_confirmation(self):
        with fixture() as root:
            accounts, login = self.approval_native()
            native = (patch("pod.cli.contract", return_value={
                          "status": "observed", "runtime": "runtime", "capabilities": {}}),
                      patch("pod.cli.account_metadata_raw", return_value=accounts),
                      patch("pod.cli.agent_login_mode", return_value=login))
            with native[0], native[1], native[2]:
                proposal = execute(parser().parse_args(["config", "approve", "sol", "--json"]), root)
            path = Path(os.environ["XDG_CONFIG_HOME"]) / "pod" / "config.yaml"
            self.assertEqual(proposal["status"], "confirmation_required")
            self.assertFalse(proposal["written"])
            self.assertFalse(path.exists())
            self.assertNotIn("account", proposal["proposal"])
            self.assertEqual(proposal["proposal"]["account_display"], "…aaaaaaaa")
            self.assertEqual(proposal["proposal"]["context"]["status"], "unavailable")
            with patch("pod.cli.contract", return_value={
                       "status": "observed", "runtime": "runtime", "capabilities": {}}), \
                 patch("pod.cli.account_metadata_raw", return_value=accounts), \
                 patch("pod.cli.agent_login_mode", return_value=login):
                approved = execute(parser().parse_args([
                    "config", "approve", "sol", "--confirm", proposal["proposal"]["proposal"]]), root)
            self.assertEqual(approved["status"], "approved")
            saved = read_yaml(path)
            self.assertTrue(saved["models"]["sol"]["approved"])
            self.assertEqual(saved["models"]["sol"]["account"], ACCOUNT_IDENTITY)
            self.assertRegex(saved["models"]["sol"]["approval_ref"], r"^guided:[0-9a-f]{64}$")
            self.assertRegex(saved["models"]["sol"]["approval_route"], r"^[0-9a-f]{64}$")

            revoke = execute(parser().parse_args(["config", "revoke", "sol", "--json"]), root)
            self.assertTrue(path.exists())
            revoked = execute(parser().parse_args([
                "config", "revoke", "sol", "--confirm", revoke["proposal"]["proposal"]]), root)
            self.assertEqual(revoked["status"], "revoked")
            saved = read_yaml(path)["models"]["sol"]
            self.assertFalse(saved["approved"])
            self.assertNotIn("account", saved)
            self.assertNotIn("approval_route", saved)

    def test_guided_approval_rechecks_account_and_config_and_preserves_no_write_failures(self):
        with fixture() as root:
            accounts, login = self.approval_native()
            with patch("pod.cli.contract", return_value={
                       "status": "observed", "runtime": "runtime", "capabilities": {}}), \
                 patch("pod.cli.account_metadata_raw", return_value=accounts), \
                 patch("pod.cli.agent_login_mode", return_value=login):
                proposal = execute(parser().parse_args(["config", "approve", "sol"]), root)
            path = Path(os.environ["XDG_CONFIG_HOME"]) / "pod" / "config.yaml"
            changed, changed_login = self.approval_native(identity="b" * 64)
            with patch("pod.cli.contract", return_value={
                       "status": "observed", "runtime": "runtime", "capabilities": {}}), \
                 patch("pod.cli.account_metadata_raw", return_value=changed), \
                 patch("pod.cli.agent_login_mode", return_value=changed_login), \
                 self.assertRaises(PodError) as caught:
                execute(parser().parse_args([
                    "config", "approve", "sol", "--confirm", proposal["proposal"]["proposal"]]), root)
            self.assertEqual(caught.exception.code, "approval_evidence_changed")
            self.assertFalse(path.exists())

            with patch("pod.cli.contract", return_value={
                       "status": "observed", "runtime": "runtime", "capabilities": {}}), \
                 patch("pod.cli.account_metadata_raw", return_value=accounts), \
                 patch("pod.cli.agent_login_mode", return_value=login):
                proposal = execute(parser().parse_args(["config", "approve", "sol"]), root)
            path.parent.mkdir(parents=True)
            path.write_text("schema: pod/v1\npolicy: {max_workers: 2}\n")
            with patch("pod.cli.contract", return_value={
                       "status": "observed", "runtime": "runtime", "capabilities": {}}), \
                 patch("pod.cli.account_metadata_raw", return_value=accounts), \
                 patch("pod.cli.agent_login_mode", return_value=login), \
                 self.assertRaises(PodError) as changed_config:
                execute(parser().parse_args([
                    "config", "approve", "sol", "--confirm", proposal["proposal"]["proposal"]]), root)
            self.assertEqual(changed_config.exception.code, "approval_evidence_changed")
            self.assertNotIn("models:", path.read_text())

            with self.assertRaises(PodError) as wrong:
                execute(parser().parse_args(["config", "approve", "terra"]), root)
            self.assertEqual(wrong.exception.code, "unknown_model_alias")
            self.assertNotIn("models:", path.read_text())

    def test_guided_approval_reports_paid_and_unknown_without_granting_spending(self):
        for auth, expected in (("api_key", "paid"), ("unknown", "unknown")):
            with self.subTest(auth=auth), fixture() as root:
                accounts, login = self.approval_native(auth=auth)
                # Preserve a selected identity even when authentication proof is unknown.
                accounts["providers"]["codex"]["default_has_auth"] = auth != "unknown"
                with patch("pod.cli.contract", return_value={
                           "status": "observed", "runtime": "runtime", "capabilities": {}}), \
                     patch("pod.cli.account_metadata_raw", return_value=accounts), \
                     patch("pod.cli.agent_login_mode", return_value=login):
                    proposal = execute(parser().parse_args(["config", "approve", "sol"]), root)
                self.assertEqual(proposal["proposal"]["billing"], expected)
                self.assertFalse((Path(os.environ["XDG_CONFIG_HOME"]) / "pod" / "config.yaml").exists())

    def test_guided_interactive_cancellation_writes_nothing(self):
        with fixture() as root:
            accounts, login = self.approval_native()
            with patch("pod.cli.Path.cwd", return_value=root), \
                 patch("pod.cli.contract", return_value={
                       "status": "observed", "runtime": "runtime", "capabilities": {}}), \
                 patch("pod.cli.account_metadata_raw", return_value=accounts), \
                 patch("pod.cli.agent_login_mode", return_value=login), \
                 patch("pod.cli.sys.stdin.isatty", return_value=True), \
                 patch("builtins.input", return_value="no"):
                self.assertEqual(main(["config", "approve", "sol"]), 0)
            self.assertFalse((Path(os.environ["XDG_CONFIG_HOME"]) / "pod" / "config.yaml").exists())

    def test_noninteractive_human_proposal_hides_internal_digests(self):
        with fixture() as root:
            accounts, login = self.approval_native()
            output = io.StringIO()
            with patch("pod.cli.Path.cwd", return_value=root), \
                 patch("pod.cli.contract", return_value={
                       "status": "observed", "runtime": "runtime", "capabilities": {}}), \
                 patch("pod.cli.account_metadata_raw", return_value=accounts), \
                 patch("pod.cli.agent_login_mode", return_value=login), \
                 patch("pod.cli.sys.stdin.isatty", return_value=False), \
                 redirect_stdout(output):
                self.assertEqual(main(["config", "approve", "sol"]), 0)
            rendered = output.getvalue()
            self.assertIn("…aaaaaaaa", rendered)
            self.assertIn("No change written", rendered)
            self.assertIn("Configured effort: medium", rendered)
            self.assertIn("Configured context: 256k", rendered)
            self.assertIn("model-specific support is unverified", rendered)
            self.assertNotIn("requestable_not_model_verified", rendered)
            self.assertNotIn(ACCOUNT_IDENTITY, rendered)
            self.assertNotRegex(rendered, r"[0-9a-f]{64}")

    def test_plain_config_hides_revision_and_skill_only_guidance_avoids_global_pod(self):
        with fixture() as root:
            output = io.StringIO()
            with patch("pod.cli.Path.cwd", return_value=root), redirect_stdout(output):
                self.assertEqual(main(["config"]), 0)
            rendered = output.getvalue()
            self.assertNotIn("Revision:", rendered)
            self.assertNotRegex(rendered, r"[0-9a-f]{64}")
            source = Path(__file__).resolve().parents[1] / "skills" / "pod" / "cli.py"
            self.assertNotIn("`pod doctor --json`", source.read_text())

    def test_guided_approval_refuses_redirected_personal_path_before_write(self):
        with fixture() as root:
            real = root.parent / "real-config"
            real.mkdir()
            redirected = root.parent / "redirected-config"
            redirected.symlink_to(real, target_is_directory=True)
            accounts, login = self.approval_native()
            with patch.dict(os.environ, {"POD_CONFIG_HOME": str(redirected)}), \
                 patch("pod.cli.contract", return_value={
                       "status": "observed", "runtime": "runtime", "capabilities": {}}), \
                 patch("pod.cli.account_metadata_raw", return_value=accounts), \
                 patch("pod.cli.agent_login_mode", return_value=login):
                proposal = execute(parser().parse_args(["config", "approve", "sol"]), root)
                with self.assertRaises(PodError) as caught:
                    execute(parser().parse_args([
                        "config", "approve", "sol", "--confirm",
                        proposal["proposal"]["proposal"]]), root)
            self.assertEqual(caught.exception.code, "unsafe_config")
            self.assertFalse((real / "config.yaml").exists())

    def test_guided_approval_allows_symlinked_native_profile_root(self):
        with fixture() as root:
            real = root.parent / "real-native-config"
            real.mkdir()
            linked = root.parent / "linked-native-config"
            linked.symlink_to(real, target_is_directory=True)
            accounts, login = self.approval_native()
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(linked)}), \
                 patch("pod.cli.contract", return_value={
                       "status": "observed", "runtime": "runtime", "capabilities": {}}), \
                 patch("pod.cli.account_metadata_raw", return_value=accounts), \
                 patch("pod.cli.agent_login_mode", return_value=login):
                proposal = execute(parser().parse_args(["config", "approve", "sol"]), root)
                approved = execute(parser().parse_args([
                    "config", "approve", "sol", "--confirm",
                    proposal["proposal"]["proposal"]]), root)
                revoke = execute(parser().parse_args(["config", "revoke", "sol"]), root)
                revoked = execute(parser().parse_args([
                    "config", "revoke", "sol", "--confirm",
                    revoke["proposal"]["proposal"]]), root)
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(revoked["status"], "revoked")
            self.assertTrue((real / "pod" / "config.yaml").is_file())
            self.assertFalse(read_yaml(real / "pod" / "config.yaml")["models"]["sol"]["approved"])
    def test_nested_skill_paths_use_canonical_manifest_keys(self):
        root = Path("/project/.agents/skills/pod")
        nested = root / "references" / "planning.md"
        self.assertEqual(_relative_key(nested, root), "references/planning.md")

    def test_local_idempotent_and_modified_skill_preserved(self):
        with fixture() as root:
            first = setup(root)
            self.assertEqual(first["skills"]["codex"], "installed")
            self.assertEqual(setup(root)["skills"]["codex"], "reused")
            target = root / ".agents" / "skills" / "pod" / "SKILL.md"
            target.write_text("user edit")
            result = setup(root)
            self.assertEqual(result["skills"]["codex"], "preserved_modified")
            self.assertEqual(target.read_text(), "user edit")

    def test_compatible_global_reuses_and_prunes_only_owned_unchanged_local(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "codex"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = root / "project"
            project.mkdir()
            self.assertEqual(setup(project)["skills"]["codex"], "installed")
            local_codex = project / ".agents" / "skills" / "pod"
            local_claude = project / ".claude" / "skills" / "pod"
            (local_claude / "SKILL.md").write_text("personal change")
            setup(project, global_scope=True)
            outcome = setup(project)
            self.assertEqual(outcome["skills"]["codex"], "reused_global_removed_owned_local")
            self.assertFalse(local_codex.exists())
            self.assertEqual(outcome["skills"]["claude"], "reused_global_preserved_local")
            self.assertEqual((local_claude / "SKILL.md").read_text(), "personal change")
            self.assertFalse(local_codex.exists())

    def test_global_setup_leaves_project_unchanged(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "codex"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = root / "project"
            project.mkdir()
            result = setup(project, global_scope=True)
            self.assertEqual(result["scope"], "global")
            self.assertEqual(list(project.iterdir()), [])
            self.assertEqual((root / "codex" / "skills" / "pod" / "SKILL.md").read_bytes(), canonical()["SKILL.md"])

    def test_relative_native_skill_homes_fail_before_global_project_write(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
                values = {"CODEX_HOME": str(root / "codex"),
                          "CLAUDE_CONFIG_DIR": str(root / "claude"), name: "relative-home"}
                with self.subTest(name=name), patch.dict(os.environ, values):
                    with self.assertRaises(PodError) as caught:
                        setup(project, global_scope=True)
                    self.assertEqual(caught.exception.code, "invalid_native_home")
                    self.assertEqual(list(project.iterdir()), [])
                    self.assertFalse((project / "relative-home").exists())

    def test_global_skill_homes_reject_absolute_and_symlink_project_containment_before_writes(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            contained = project / "native-home"
            contained.mkdir()
            redirected = root.parent / "skill-home-link"
            redirected.symlink_to(contained, target_is_directory=True)
            safe = root.parent / "safe-skill-home"
            original = dict(os.environ)
            for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
                for value in (contained, redirected):
                    homes = {"CODEX_HOME": str(safe / "codex"),
                             "CLAUDE_CONFIG_DIR": str(safe / "claude"), name: str(value)}
                    with self.subTest(name=name, value=value), patch.dict(os.environ, homes):
                        with self.assertRaises(PodError) as caught:
                            setup(project, global_scope=True)
                        self.assertEqual(caught.exception.code, "project_contained_native_home")
                        self.assertEqual(list(contained.iterdir()), [])
                        self.assertFalse(safe.exists())
            self.assertEqual(dict(os.environ), original)

    def test_doctor_diagnoses_duplicate_scopes_without_repair(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "codex"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = root / "project"
            project.mkdir()
            setup(project)
            setup(project, global_scope=True)
            with patch("pod.cli.contract", return_value={"status": "unavailable"}), \
                 patch("pod.cli.account_metadata_raw", side_effect=PodError("unavailable", "offline")):
                report = execute(parser().parse_args(["doctor"]), project)
            self.assertEqual(report["integration_overlap"]["codex"], "duplicate_current_copies")
            self.assertTrue((project / ".agents" / "skills" / "pod" / "SKILL.md").is_file())

    def test_four_public_families(self):
        help_text = parser().format_help()
        self.assertIn("setup", help_text)
        self.assertIn("config", help_text)
        self.assertIn("doctor", help_text)
        self.assertIn("status", help_text)
        self.assertNotIn("implement", help_text)
        with self.assertRaises(SystemExit):
            parser().parse_args(["worker-preflight"])

    def test_config_edit_scope_and_invalid_edit_preserved(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "config"),
                                                        "EDITOR": f'"{sys.executable}" -c pass'}):
            project = root / "project"
            project.mkdir()
            args = parser().parse_args(["config", "--edit"])
            value = execute(args, project)
            self.assertEqual(value["scope"], "personal")
            self.assertFalse((project / ".pod").exists())
            personal = root / "config" / "pod" / "config.yaml"
            self.assertTrue(personal.exists())
            args = parser().parse_args(["config", "--edit", "--scope", "project"])
            self.assertEqual(execute(args, project)["scope"], "project")
            local = project / ".pod" / "config.yaml"
            local.write_text("schema: wrong\n")
            with self.assertRaises(PodError):
                execute(parser().parse_args(["config", "--check"]), project)
            self.assertEqual(local.read_text(), "schema: wrong\n")

    def test_doctor_passive(self):
        with fixture() as root, patch.dict(os.environ, {
                "CODEX_HOME": str(root.parent / "codex-home"),
                "CLAUDE_CONFIG_DIR": str(root.parent / "claude-home"),
                "XDG_CONFIG_HOME": str(root.parent / "config-home")}):
            result = execute(parser().parse_args(["doctor"]), root)
            self.assertEqual(result["native_probe"], "not_run")
            self.assertFalse((root / ".pod").exists())
            self.assertFalse((root / "codex").exists())
            self.assertFalse((root / "config").exists())

    def test_human_doctor_names_the_version_and_where_the_skill_is(self):
        with fixture() as root, \
             patch("pod.cli.contract", return_value={
                   "status": "observed", "runtime": "runtime", "capabilities": {}}), \
             patch("pod.cli.account_metadata_raw", return_value={"runtime": "runtime", "providers": {}}), \
             patch("pod.cli.agent_login_mode", return_value={}):
            out = io.StringIO()
            previous = Path.cwd()
            os.chdir(root)
            try:
                with redirect_stdout(out):
                    main(["doctor"])
            finally:
                os.chdir(previous)
        text = out.getvalue()
        self.assertIn(f"Pod {version()} ready for direct work", text)
        self.assertIn("skill installed at", text)

    def test_doctor_reports_unsupported_state_objective_scoped(self):
        with fixture() as root:
            foreign = Path(os.environ["XDG_STATE_HOME"]) / "pod" / "other-objective"
            foreign.mkdir(parents=True)
            (foreign / "context.json").write_text(json.dumps({"schema": "pod-context/v9"}))
            with patch("pod.cli.contract", return_value={
                       "status": "observed", "runtime": "runtime", "capabilities": {}}), \
                 patch("pod.cli.account_metadata_raw", return_value={
                       "runtime": "runtime", "providers": {}}), \
                 patch("pod.cli.agent_login_mode", return_value={}):
                report = execute(parser().parse_args(["doctor", "--json"]), root)
            self.assertEqual(report["readiness"]["direct_work"], "ready")
            self.assertEqual(report["readiness"]["state"]["unsupported"], 1)
            self.assertTrue(report["readiness"]["state"]["blocked"])
            self.assertEqual(report["readiness"]["state"]["scope"],
                             "affected_objectives_only")
            self.assertFalse(report["readiness"]["state"]["blocks_unrelated_objectives"])

    def test_doctor_requires_runtime_identity_before_reporting_orca_connected(self):
        with fixture() as root, \
             patch("pod.cli.contract", return_value={
                   "status": "observed", "runtime": None, "reason": "orca_read_failed",
                   "capabilities": {}}), \
             patch("pod.cli.account_metadata_raw", return_value={
                   "runtime": "runtime", "providers": {}}), \
             patch("pod.cli.agent_login_mode", return_value={}):
            report = execute(parser().parse_args(["doctor", "--json"]), root)
        self.assertEqual(report["readiness"]["orca"], "unavailable")
        self.assertIn("orca_read_failed", report["readiness"]["limitations"])

        output = io.StringIO()
        with fixture() as root, \
             patch("pod.cli.Path.cwd", return_value=root), \
             patch("pod.cli.contract", return_value={
                   "status": "observed", "runtime": None, "reason": "orca_read_failed",
                   "capabilities": {}}), \
             patch("pod.cli.account_metadata_raw", return_value={
                   "runtime": "runtime", "providers": {}}), \
             patch("pod.cli.agent_login_mode", return_value={}), \
             patch("pod.cli.executable", side_effect=PodError("orca_unavailable", "offline")), \
             redirect_stdout(output):
            self.assertEqual(main(["doctor"]), 0)
        self.assertIn("Orca did not answer; start or reconnect Orca", output.getvalue())

    def test_doctor_exposes_only_redacted_account_identities_for_personal_approval(self):
        with fixture() as root, \
             patch("pod.cli.contract", return_value={"status": "observed", "runtime": "runtime",
                                                       "capabilities": {}}), \
             patch("pod.cli.account_metadata_raw", return_value={"runtime": "runtime", "providers": {
                 "codex": {"managed_accounts": 0, "default_identity": "a" * 64},
                 "claude": {"managed_accounts": 0, "default_identity": None}}}), \
             patch("pod.cli.agent_login_mode", return_value={"identity_digest": "b" * 64}):
            report = execute(parser().parse_args(["doctor", "--json"]), root)
        self.assertEqual(report["account_identities"], {
            "codex": {"identity_digest": "a" * 64, "source": "orca_system_default"},
            "claude": {"identity_digest": "b" * 64, "source": "agent_login_status"},
        })
        self.assertNotIn("email", str(report["account_identities"]))

    def test_doctor_never_substitutes_host_login_for_an_unidentified_managed_account(self):
        with fixture() as root, \
             patch("pod.cli.contract", return_value={"status": "observed", "runtime": "runtime",
                                                       "capabilities": {}}), \
             patch("pod.cli.account_metadata_raw", return_value={"runtime": "runtime", "providers": {
                 "codex": {"managed_accounts": 1, "active_account": None},
                 "claude": {"managed_accounts": 0, "default_identity": None}}}), \
             patch("pod.cli.agent_login_mode", return_value={"identity_digest": "b" * 64}):
            report = execute(parser().parse_args(["doctor", "--json"]), root)
        self.assertEqual(report["account_identities"]["codex"], {
            "identity_digest": None, "source": "unavailable"})
        self.assertEqual(report["account_identities"]["claude"]["identity_digest"], "b" * 64)

    def test_doctor_does_not_fall_through_a_partial_native_default_to_host_login(self):
        with fixture() as root, \
             patch("pod.cli.contract", return_value={"status": "observed", "runtime": "runtime",
                                                       "capabilities": {}}), \
             patch("pod.cli.account_metadata_raw", return_value={"runtime": "runtime", "providers": {
                 "codex": {"managed_accounts": 0, "default_present": True,
                           "default_identity": None, "default_auth": "api_key",
                           "default_has_auth": True},
                 "claude": {"managed_accounts": 0, "default_present": False,
                            "default_identity": None}}}), \
             patch("pod.cli.agent_login_mode", return_value={"auth": "oauth",
                                                               "subscription": True,
                                                               "identity_digest": "b" * 64}):
            report = execute(parser().parse_args(["doctor", "--json"]), root)
        self.assertEqual(report["account_identities"]["codex"], {
            "identity_digest": None, "source": "unavailable"})
        self.assertEqual(report["account_identities"]["claude"]["identity_digest"], "b" * 64)

    def test_status_joins_selected_run_to_checkpoint_compactly(self):
        with fixture() as root, patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
            project = root / "project"
            project.mkdir()
            source = {"schema": "pod-issue-source/v1", "repository": "acme/widgets",
                      "number": 7, "locator": "https://github.com/acme/widgets/issues/7",
                      "body_sha256": "a" * 64, "amendments": []}
            worktree_binding = {"repository": "acme/widgets", "repo_key": "b" * 64,
                                "path": str(project), "branch": "orca/issue-7"}
            checkpoint(project, "objective", owner="owner", native={"runtime": "runtime"},
                       value={"schema": "pod-checkpoint/v1", "criteria": ["works"],
                              "plan_revision": "p", "candidate": "c", "policy_revision": "r",
                              "native_refs": [{"runId": "run"}], "assignments": [], "questions": [],
                              "verification_gaps": ["works"], "next_safe_action": "run checks",
                              "objective_source": source, "worktree": worktree_binding,
                              "blocker": "hosted CI", "remaining_gates": ["hosted", "review"]})
            workers = {"runtime": "runtime", "scope": {"source": "flag"}, "complete": True,
                       "workers": [{"terminalState": "active", "projection": {"attention": {"requiresAction": True}}},
                                   {"terminalState": "released", "projection": {}}]}
            native = {"runtime": "runtime", "scope": "objective_assignments", "complete": True,
                      "assignments": [], "physical_capacity": "unavailable"}
            with patch("pod.cli.worker_rows", return_value=workers), \
                 patch("pod.operations.OrcaPort.read_native", return_value=native):
                result = execute(parser().parse_args(["status", "--run", "run"]), project)
                output = io.StringIO()
                with patch("pod.cli.Path.cwd", return_value=project), redirect_stdout(output):
                    self.assertEqual(main(["status", "--run", "run"]), 0)
            self.assertEqual(result["verification_gaps"], ["works"])
            self.assertEqual(result["next_safe_action"], "run checks")
            self.assertEqual(result["objective"], "objective")
            self.assertEqual(result["source"]["locator"], source["locator"])
            self.assertEqual(result["selected_worktree"], worktree_binding)
            self.assertEqual(result["blocker"], "hosted CI")
            self.assertEqual(result["remaining_gates"], ["hosted", "review"])
            self.assertEqual(result["native"]["workers_by_state"], {"active": 1, "released": 1})
            self.assertNotIn("workers", result["native"])
            rendered = output.getvalue()
            self.assertIn(source["locator"], rendered)
            self.assertIn("orca/issue-7", rendered)
            self.assertIn("hosted CI", rendered)
            self.assertNotIn("a" * 64, rendered)


class SkillOwnershipTests(unittest.TestCase):
    """Who owns a placed copy, and what repeating setup is allowed to touch."""

    def install_global(self, root):
        project = root / "project"
        project.mkdir(exist_ok=True)
        setup(project, global_scope=True)
        return project

    def test_a_skills_cli_symlink_is_recognised_and_left_alone(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = self.install_global(root)
            canonical_copy = root / "agents" / "skills" / "pod"
            linked = root / "claude" / "skills" / "pod"
            for name in sorted(BUNDLE_FILES, reverse=True):
                path = linked / name
                if path.is_file():
                    path.unlink()
            for directory in ("references", "agents", "scripts"):
                if (linked / directory).is_dir():
                    (linked / directory).rmdir()
            linked.rmdir()
            linked.symlink_to(canonical_copy, target_is_directory=True)
            report = inspect(project, global_scope=True)
            self.assertEqual(report["claude"]["status"], "managed_by_skills_cli")
            self.assertEqual(report["claude"]["manager"], "skills_cli")
            outcome = setup(project, global_scope=True)
            self.assertEqual(outcome["skills"]["claude"], "managed_by_skills_cli")
            self.assertTrue(linked.is_symlink())
            self.assertEqual(linked.resolve(), canonical_copy.resolve())

    def test_the_canonical_copy_behind_a_link_is_also_recognised(self):
        """The shape `npx skills add` actually produces, including without a lock file."""
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = root / "project"
            project.mkdir()
            canonical_copy = root / "agents" / "skills" / "pod"
            canonical_copy.mkdir(parents=True)
            for name, data in canonical().items():
                target = canonical_copy / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            linked = root / "claude" / "skills"
            linked.mkdir(parents=True)
            (linked / "pod").symlink_to(canonical_copy, target_is_directory=True)
            report = inspect(project, global_scope=True)
            self.assertEqual(report["codex"]["status"], "managed_by_skills_cli")
            self.assertEqual(report["claude"]["status"], "managed_by_skills_cli")
            before = {name: (canonical_copy / name).read_bytes() for name in BUNDLE_FILES}
            outcome = setup(project, global_scope=True)
            self.assertEqual(set(outcome["skills"].values()), {"managed_by_skills_cli"})
            self.assertEqual(set(outcome["ownership"].values()), {"skills_cli"})
            after = {name: (canonical_copy / name).read_bytes() for name in BUNDLE_FILES}
            self.assertEqual(before, after)
            self.assertTrue((linked / "pod").is_symlink())

    def test_a_lock_entry_marks_the_global_copy_as_externally_managed(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude"),
                                                        "XDG_STATE_HOME": str(root / "state")}):
            project = self.install_global(root)
            lock = root / "state" / "skills" / ".skill-lock.json"
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(json.dumps({"version": 3, "skills": {"pod": {
                "source": "j3w1/pod", "ref": "v0.1.0", "installedAt": "2026-09-21T00:00:00Z"}}}))
            report = inspect(project, global_scope=True)
            self.assertEqual(report["codex"]["status"], "managed_by_skills_cli")
            outcome = setup(project, global_scope=True)
            self.assertEqual(outcome["skills"]["codex"], "managed_by_skills_cli")
            self.assertEqual(outcome["ownership"]["codex"], "skills_cli")

    def older_bundle(self):
        """The bytes a previous release would have placed."""
        source = dict(canonical())
        source["references/models.md"] = b"# Routing and quota\n\nAn older release.\n"
        return source

    def test_a_version_change_upgrades_only_an_untouched_owned_copy(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            with patch("pod.setup.canonical", return_value=self.older_bundle()):
                self.assertEqual(setup(project)["skills"]["codex"], "installed")
            manifest_path = project / ".pod" / "skills.json"
            self.assertEqual(json.loads(manifest_path.read_text())["version"], version())
            placed = project / ".agents" / "skills" / "pod" / "references" / "models.md"
            self.assertEqual(placed.read_bytes(), self.older_bundle()["references/models.md"])
            upgraded = setup(project)
            self.assertEqual(upgraded["skills"]["codex"], "upgraded")
            self.assertEqual(placed.read_bytes(), canonical()["references/models.md"])
            self.assertIn("codex", json.loads(manifest_path.read_text())["owned_hosts"])
            self.assertEqual(setup(project)["skills"]["codex"], "reused")

    def test_an_edited_copy_is_never_replaced_by_an_upgrade(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            with patch("pod.setup.canonical", return_value=self.older_bundle()):
                setup(project)
            edited = project / ".agents" / "skills" / "pod" / "references" / "models.md"
            edited.write_text("# my own notes\n")
            self.assertEqual(setup(project)["skills"]["codex"], "preserved_modified")
            self.assertEqual(edited.read_text(), "# my own notes\n")

    def test_a_differing_version_is_preserved_with_its_own_next_step(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = self.install_global(root)
            skill = root / "agents" / "skills" / "pod"
            (skill / "VERSION").write_text("9.0.0\n", encoding="ascii")
            report = inspect(project, global_scope=True)
            self.assertEqual(report["codex"]["status"], "other_version")
            self.assertTrue(report["codex"]["version"].startswith("9."))
            outcome = setup(project, global_scope=True)
            self.assertEqual(outcome["skills"]["codex"], "preserved_other_version")
            self.assertEqual((skill / "VERSION").read_text(encoding="ascii"), "9.0.0\n")

    def test_an_installed_copy_carries_and_runs_its_own_helpers(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = self.install_global(root)
            skill = root / "agents" / "skills" / "pod"
            for name in BUNDLE_FILES:
                self.assertTrue((skill / name).is_file(), name)
            helped = subprocess.run([sys.executable, str(skill / "scripts" / "pod.py"), "--help"],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(helped.returncode, 0, helped.stderr)
            self.assertIn("doctor", helped.stdout)

    def test_interpreter_caches_in_a_placed_copy_are_not_a_modification(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            setup(project)
            cache = project / ".agents" / "skills" / "pod" / "__pycache__"
            cache.mkdir()
            (cache / "cli.cpython-313.pyc").write_bytes(b"cached")
            self.assertEqual(setup(project)["skills"]["codex"], "reused")
            self.assertEqual(inspect(project)["codex"]["status"], "current")

    def test_a_symlinked_native_home_root_is_accepted(self):
        """Ordinary machines redirect ~/.claude; only paths Pod creates must be plain."""
        with fixture() as root:
            real = root / "claude-real"
            real.mkdir()
            linked = root / "claude-link"
            linked.symlink_to(real, target_is_directory=True)
            project = root / "project"
            project.mkdir()
            with patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                         "CLAUDE_CONFIG_DIR": str(linked)}):
                outcome = setup(project, global_scope=True)
            self.assertEqual(outcome["skills"]["claude"], "installed")
            self.assertTrue((real / "skills" / "pod" / "SKILL.md").is_file())

    def test_doctor_reports_the_bundle_prerequisites_and_manager(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude"),
                                                        "XDG_STATE_HOME": str(root / "state")}):
            project = self.install_global(root)
            with patch("pod.cli.contract", return_value={"status": "unavailable",
                                                         "reason": "orca_unavailable"}), \
                 patch("pod.cli.account_metadata_raw", side_effect=PodError("unavailable", "offline")), \
                 patch("pod.cli.executable", side_effect=PodError("orca_unavailable", "absent")):
                report = execute(parser().parse_args(["doctor"]), project)
            self.assertEqual(report["bundle"]["version"], version())
            self.assertEqual(report["prerequisites"]["python"], "ok")
            self.assertIn("Pod never installs it", report["prerequisites"]["orca"])
            self.assertEqual(report["routes"], {"status": "unavailable", "reason": "orca_unavailable"})
            self.assertIsNone(report["skills_cli"]["entry"])
            self.assertEqual(report["global_skills"]["codex"]["manager"], "unowned")
            self.assertEqual(report["global_skills"]["codex"]["status"], "current")


class OwnershipClaimRegressions(unittest.TestCase):
    """Pod must not claim a copy it cannot prove it placed."""

    def test_a_correct_copy_pod_did_not_write_is_not_claimed(self):
        """A single-agent install from a lock-free source leaves no peer link and no entry.

        Reporting that copy as `pod_setup` claims another manager's installation, which is
        exactly what repeat setup must never do.
        """
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude")}):
            project = root / "project"
            project.mkdir()
            placed = root / "agents" / "skills" / "pod"
            placed.mkdir(parents=True)
            for name, data in canonical().items():
                target = placed / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            outcome = setup(project, global_scope=True)
            self.assertEqual(outcome["skills"]["codex"], "reused")
            self.assertEqual(outcome["ownership"]["codex"], "unowned")
            self.assertEqual(outcome["skills"]["claude"], "installed")
            self.assertEqual(outcome["ownership"]["claude"], "pod_setup")

    def test_an_unreadable_lock_never_blocks_a_diagnostic(self):
        with fixture() as root, patch.dict(os.environ, {"CODEX_HOME": str(root / "agents"),
                                                        "CLAUDE_CONFIG_DIR": str(root / "claude"),
                                                        "XDG_STATE_HOME": "relative-home"}):
            project = root / "project"
            project.mkdir()
            with patch("pod.cli.contract", return_value={"status": "unavailable",
                                                         "reason": "orca_unavailable"}), \
                 patch("pod.cli.account_metadata_raw", side_effect=PodError("unavailable", "offline")), \
                 patch("pod.cli.executable", side_effect=PodError("orca_unavailable", "absent")):
                report = execute(parser().parse_args(["doctor"]), project)
            self.assertEqual(report["status"], "observed")
            self.assertIsNone(report["skills_cli"]["lock_file"])
            self.assertFalse(report["skills_cli"]["readable"])


class SeparateAgentHomeRegressions(unittest.TestCase):
    """A host may point CODEX_HOME somewhere other than the agents home.

    Running Codex inside another tool does exactly that. Pod must still see the canonical
    copy the skills ecosystem placed, or a repeat setup installs a second active one.
    """

    def install_like_the_skills_cli(self, root):
        agents = root / ".agents"
        canonical_copy = agents / "skills" / "pod"
        canonical_copy.mkdir(parents=True)
        for name, data in canonical().items():
            target = canonical_copy / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        claude = root / "claude" / "skills"
        claude.mkdir(parents=True)
        (claude / "pod").symlink_to(canonical_copy, target_is_directory=True)
        return canonical_copy

    def test_a_codex_home_elsewhere_does_not_get_a_second_copy(self):
        with fixture() as root:
            canonical_copy = self.install_like_the_skills_cli(root)
            elsewhere = root / "runtime-home"
            elsewhere.mkdir()
            project = root / "project"
            project.mkdir()
            with patch.dict(os.environ, {"HOME": str(root),
                                         "CODEX_HOME": str(elsewhere),
                                         "CLAUDE_CONFIG_DIR": str(root / "claude")}):
                report = inspect(project, global_scope=True)
                self.assertEqual(report["claude"]["status"], "managed_by_skills_cli")
                outcome = setup(project, global_scope=True)
            self.assertEqual(outcome["skills"]["claude"], "managed_by_skills_cli")
            self.assertEqual(outcome["skills"]["codex"], "present_elsewhere")
            self.assertEqual(outcome["ownership"]["codex"], "skills_cli")
            self.assertFalse((elsewhere / "skills" / "pod").exists(),
                             "a second active copy must not be created")
            self.assertTrue((canonical_copy / "SKILL.md").is_file())

    def test_a_skill_present_elsewhere_is_reported_where_it_actually_is(self):
        """Reporting `missing` for an agent that plainly has the skill is worse than
        unhelpful: it sends an operator to install the duplicate copy setup refuses."""
        with fixture() as root:
            canonical_copy = self.install_like_the_skills_cli(root)
            elsewhere = root / "runtime-home"
            elsewhere.mkdir()
            project = root / "project"
            project.mkdir()
            with patch.dict(os.environ, {"HOME": str(root),
                                         "CODEX_HOME": str(elsewhere),
                                         "CLAUDE_CONFIG_DIR": str(root / "claude")}):
                report = inspect(project, global_scope=True)
            codex = report["codex"]
            self.assertEqual(codex["status"], "present_elsewhere")
            self.assertEqual(codex["path"], str(canonical_copy))
            self.assertEqual(codex["configured_path"], str(elsewhere / "skills" / "pod"))
            self.assertEqual(codex["manager"], "skills_cli")
            self.assertEqual(codex["version"], version())

    def test_a_genuinely_absent_skill_is_still_missing(self):
        with fixture() as root:
            elsewhere = root / "runtime-home"
            elsewhere.mkdir()
            project = root / "project"
            project.mkdir()
            with patch.dict(os.environ, {"HOME": str(root),
                                         "CODEX_HOME": str(elsewhere),
                                         "CLAUDE_CONFIG_DIR": str(root / "claude")}):
                report = inspect(project, global_scope=True)
            self.assertEqual(report["codex"]["status"], "missing")
            self.assertNotIn("configured_path", report["codex"])

    def test_a_differing_version_there_is_still_not_duplicated(self):
        """Which copy wins is the operator's call, not something setup decides by writing."""
        with fixture() as root:
            canonical_copy = self.install_like_the_skills_cli(root)
            (canonical_copy / "VERSION").write_text("9.0.0\n", encoding="ascii")
            elsewhere = root / "runtime-home"
            elsewhere.mkdir()
            project = root / "project"
            project.mkdir()
            with patch.dict(os.environ, {"HOME": str(root),
                                         "CODEX_HOME": str(elsewhere),
                                         "CLAUDE_CONFIG_DIR": str(root / "claude")}):
                outcome = setup(project, global_scope=True)
            self.assertEqual(outcome["skills"]["codex"], "present_elsewhere")
            self.assertFalse((elsewhere / "skills" / "pod").exists())

    def test_an_agents_home_inside_the_project_is_not_consulted(self):
        with fixture() as root:
            project = root / "project"
            (project / ".agents" / "skills" / "pod").mkdir(parents=True)
            (project / ".agents" / "skills" / "pod" / "SKILL.md").write_text("---\nname: pod\n---\n")
            elsewhere = root / "runtime-home"
            elsewhere.mkdir()
            with patch.dict(os.environ, {"HOME": str(project),
                                         "CODEX_HOME": str(elsewhere),
                                         "CLAUDE_CONFIG_DIR": str(root / "claude")}):
                outcome = setup(project, global_scope=True)
            self.assertEqual(outcome["skills"]["codex"], "installed")
            self.assertTrue((elsewhere / "skills" / "pod" / "SKILL.md").is_file())
