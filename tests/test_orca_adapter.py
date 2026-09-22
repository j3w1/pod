import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pod.errors import PodError
from pod.orca import (account_metadata, account_metadata_raw, agent_login_mode, contract,
                      effective_launch, executable, hosts, identity, mutate_command, read_command,
                      require_route_establishment, route_establishment, run_rows, worker_rows,
                      worktree_selector)
from tests.common import envelope, receipt


class OrcaAdapterTests(unittest.TestCase):
    def test_desktop_linux_uses_orca_ide_discovery_without_bare_orca_fallback(self):
        with patch.dict(os.environ, {}, clear=False):
            for key in ("ORCA_CLI_COMMAND", "ORCA_DEV_REPO_ROOT", "ORCA_TERMINAL_HANDLE"):
                os.environ.pop(key, None)
            with patch("pod.orca.shutil.which", return_value="/fixture/orca-ide") as which, \
                 patch("pod.orca.Path.is_file", return_value=True):
                self.assertEqual(executable(), Path("/fixture/orca-ide"))
            which.assert_called_once_with("orca-ide")
            with patch("pod.orca.shutil.which", return_value=None) as missing:
                with self.assertRaises(PodError) as caught:
                    executable()
            self.assertEqual(caught.exception.code, "orca_unavailable")
            missing.assert_called_once_with("orca-ide")

    def test_pod_location_overrides_do_not_replace_native_subprocess_profile(self):
        native = {
                  "CODEX_HOME": "native-codex", "CLAUDE_CONFIG_DIR": "native-claude"}
        overrides = {"POD_CONFIG_HOME": "/isolated/pod-config", "POD_STATE_HOME": "/isolated/pod-state"}

        def completed(*args, **kwargs):
            self.assertNotIn("env", kwargs)
            self.assertEqual({key: os.environ[key] for key in native}, native)
            return subprocess.CompletedProcess(args[0], 0, "orca 1.0\n", "")

        with patch.dict(os.environ, {**native, **overrides}), \
             patch("pod.orca.executable", return_value=Path("/native/orca")), \
             patch("pod.orca.subprocess.run", side_effect=completed):
            self.assertEqual(read_command(["--version"])["version"], "orca 1.0")

    def test_read_adapter_rejects_consuming_and_guessed_verbs_before_process(self):
        with patch("pod.orca.subprocess.run") as runner:
            for argv in (
                ["orchestration", "check", "--json"],
                ["orchestration", "check", "--ack", "delivery", "--json"],
                ["orchestration", "dispatch-list", "--json"],
                ["orchestration", "worker-list", "--include-remote", "--json", "--run", "r", "--ack", "d"],
            ):
                with self.assertRaises(PodError):
                    read_command(argv)
            runner.assert_not_called()

    def test_paginated_worker_list_requires_stable_runtime_and_complete_pages(self):
        pages = [
            {"runtime": "r", "result": {"scope": {"source": "all"}, "workers": [{"dispatchId": "a"}],
                                         "page": {"hasMore": True, "nextCursor": "next"}}},
            {"runtime": "r", "result": {"scope": {"source": "all"}, "workers": [{"dispatchId": "b"}],
                                         "page": {"hasMore": False}}},
        ]
        with patch("pod.orca.read_command", side_effect=pages) as reader:
            fleet = worker_rows()
            self.assertEqual([x["dispatchId"] for x in fleet["workers"]], ["a", "b"])
            self.assertTrue(fleet["complete"])
            self.assertIn("--cursor", reader.call_args.args[0])

    def test_paginated_run_inventory_requires_stable_runtime_and_unique_ids(self):
        pages = [
            {"runtime": "r", "result": {"runs": [{"id": "run-a"}], "nextCursor": "next"}},
            {"runtime": "r", "result": {"runs": [{"id": "run-b"}], "nextCursor": None}},
        ]
        with patch("pod.orca.read_command", side_effect=pages) as reader:
            inventory = run_rows()
        self.assertEqual([row["id"] for row in inventory["runs"]], ["run-a", "run-b"])
        self.assertTrue(inventory["complete"])
        self.assertIn("--cursor", reader.call_args.args[0])
        with patch("pod.orca.read_command", side_effect=[pages[0],
                  {"runtime": "other", "result": {"runs": [], "nextCursor": None}}]):
            with self.assertRaises(PodError) as changed:
                run_rows()
        self.assertEqual(changed.exception.code, "orca_runtime_changed")
        with patch("pod.orca.read_command", return_value={
                "runtime": "r", "result": {"runs": []}}), \
             self.assertRaises(PodError) as missing:
            run_rows()
        self.assertEqual(missing.exception.code, "orca_pagination")
        looping = [
            {"runtime": "r", "result": {"runs": [{"id": "run-a"}],
                                           "nextCursor": "next"}},
            {"runtime": "r", "result": {"runs": [{"id": "run-b"}],
                                           "nextCursor": "next"}},
        ]
        with patch("pod.orca.read_command", side_effect=looping), \
             self.assertRaises(PodError) as repeated:
            run_rows()
        self.assertEqual(repeated.exception.code, "orca_pagination")

    def test_cached_metadata_redacts_accounts_and_keeps_timestamps(self):
        result = {"runtime": "r", "result": {"claude": [{"token": "secret"}], "rateLimits": {
            "claude": {"status": "ok", "updatedAt": 1000,
                       "session": {"usedPercent": 20, "windowMinutes": 300, "resetsAt": 2000},
                       "usageMetadata": {"private": "secret"}},
            "codex": {"status": "unknown", "updatedAt": None}}}}
        with patch("pod.orca.read_command", return_value=result):
            metadata = account_metadata()
        self.assertEqual(metadata["providers"]["claude"]["updated_at_ms"], 1000)
        self.assertEqual(metadata["providers"]["claude"]["freshness"], "stale")
        self.assertEqual(metadata["providers"]["claude"]["windows"]["session"]["usedPercent"], 20)
        self.assertNotIn("secret", json.dumps(metadata))
        self.assertEqual(metadata["providers"]["claude"]["account_association"], "host_login")

    def test_actual_receipt_shape_and_optional_terminal(self):
        request = {"agent": "codex", "model": "m", "effort": "high"}
        effective_launch(request, {"state": "ready", "runId": "r", "taskId": "t", "dispatchId": "d",
                                   "launch": {"requested": request, "effective": request}})
        with self.assertRaises(PodError):
            effective_launch(request, {"state": "ready", "runId": "r", "taskId": "t", "dispatchId": "d",
                                       "launch": {"requested": request, "effective": {**request, "effort": "medium"}}})


class RouteEstablishmentTests(unittest.TestCase):
    """Establishment against sanitized captures of the installed runtime."""

    def snapshot(self):
        with patch("pod.orca.read_command", side_effect=[{"version": "1.4.206", "executable": "/fixture/orca"},
                                                          envelope("status")]):
            return contract()

    def accounts(self):
        with patch("pod.orca.read_command", return_value=envelope("account-list")):
            return account_metadata_raw()

    def fleet(self):
        with patch("pod.orca.read_command", return_value=envelope("host-list")):
            return hosts()

    def established(self, *, agent="codex", model="gpt-5.6-sol", billing="included",
                    login=None, bucket=None, delegation=False):
        accounts = self.accounts()
        identity_digest = accounts["providers"][agent].get("identity_digest")
        login = login or {"auth": "oauth", "subscription": True,
                          "identity_digest": identity_digest or "a" * 64}
        identity_digest = identity_digest or login.get("identity_digest")
        route = {"agent": agent, "model": model, "account": identity_digest,
                 "bucket": bucket, "effort": "high"}
        return route, route_establishment(
            route, {"billing": billing},
            snapshot=self.snapshot(), accounts=accounts, login=login,
            fleet=self.fleet(), child_delegation=delegation)

    def test_contract_reports_the_runtime_and_advertised_contracts(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot["status"], "observed")
        self.assertEqual(snapshot["version"], "1.4.206")
        self.assertTrue(snapshot["runtime"])
        self.assertTrue(snapshot["capabilities"]["contract_v1"])
        self.assertTrue(snapshot["capabilities"]["launch_preferences_v1"])
        for retired in ("billing_preflight", "fanout_control", "provider_quota"):
            self.assertNotIn(retired, snapshot)

    def test_controls_are_tiered_and_carry_no_account_identifier(self):
        route, established = self.established()
        tiers = {name: control["tier"] for name, control in established["controls"].items()}
        self.assertEqual(tiers["effective_launch"], "enforceable_control")
        self.assertEqual(tiers["descendant_depth"], "enforceable_control")
        self.assertEqual(tiers["descendant_count"], "runtime_observation")
        self.assertEqual(tiers["child_delegation"], "owner_route_config")
        self.assertEqual(tiers["billing_mode"], "runtime_observation")
        self.assertEqual(tiers["quota_bucket"], "runtime_observation")
        self.assertEqual(established["login"]["mode"], "host_login")
        self.assertEqual(established["billing"], {"observed": "subscription", "approved": "included"})
        self.assertEqual(established["hard_stops"], [])
        encoded = json.dumps(established)
        source = receipt("account-list")["result"]["codex"]["systemDefault"]
        for leak in (source["email"], source["providerAccountId"], source["workspaceLabel"]):
            self.assertNotIn(leak, encoded)
        self.assertEqual(len(established["login"]["identity_digest"]), 64)
        require_route_establishment(established, route)

    def test_an_included_route_survives_an_unavailable_quota_bucket(self):
        """The regression: absent optional metadata is a disclosure, not a blocker."""
        route, established = self.established(agent="claude", model="sonnet")
        observed_identity = "a" * 64
        empty = {"runtime": "uuid-0001", "providers": {"claude": {
                     "managed_accounts": 0, "default_identity": observed_identity, "windows": {},
                     "account_association": "host_login"}}}
        sparse = route_establishment(route, {"billing": "included"},
                                     snapshot=self.snapshot(),
                                     accounts=empty,
                                     login={"auth": "oauth", "subscription": True,
                                            "identity_digest": None},
                                     fleet=self.fleet())
        self.assertEqual(sparse["hard_stops"], [])
        self.assertIn("provider quota windows are unavailable to the installed runtime",
                      sparse["disclosures"])
        require_route_establishment(sparse, route)

    def test_account_rotation_and_unavailable_identity_fail_before_route_use(self):
        route = {"agent": "codex", "model": "gpt-5.6-sol", "account": "a" * 64,
                 "bucket": "default", "effort": "high"}
        approved = "a" * 64
        base_accounts = {"runtime": "uuid-0001", "providers": {"codex": {
            "managed_accounts": 1, "active_account": approved, "default_identity": None,
            "selected_auth": "oauth", "selected_has_auth": True,
            "default_auth": "oauth", "default_has_auth": True, "windows": {}}}}
        login = {"auth": "oauth", "subscription": True, "identity_digest": None}
        for active in ("b" * 64, None):
            accounts = json.loads(json.dumps(base_accounts))
            accounts["providers"]["codex"]["active_account"] = active
            established = route_establishment(
                route, {"billing": "paid"},
                snapshot=self.snapshot(), accounts=accounts, login=login, fleet=self.fleet())
            with self.subTest(active=active), self.assertRaises(PodError) as caught:
                require_route_establishment(established, route)
            self.assertEqual(caught.exception.code, "account_binding_unverified")
        matched = route_establishment(
            route, {"billing": "included"},
            snapshot=self.snapshot(), accounts=base_accounts, login=login, fleet=self.fleet())
        require_route_establishment(matched, route)

    def test_managed_account_cannot_borrow_unrelated_host_subscription_proof(self):
        identity_digest = "a" * 64
        route = {"agent": "codex", "model": "gpt-5.6-sol", "account": identity_digest,
                 "bucket": "default", "effort": "high"}
        accounts = {"runtime": "uuid-0001", "providers": {"codex": {
            "managed_accounts": 1, "active_account": identity_digest,
            "selected_auth": None, "selected_has_auth": False, "windows": {}}}}
        established = route_establishment(
            route, {"billing": "included"}, snapshot=self.snapshot(), accounts=accounts,
            login={"auth": "oauth", "subscription": True, "identity_digest": "b" * 64},
            fleet=self.fleet())
        self.assertIn("billing_mode_unverified", established["hard_stops"])
        with self.assertRaises(PodError) as caught:
            require_route_establishment(established, route)
        self.assertEqual(caught.exception.code, "billing_mode_unverified")

    def test_billing_and_paid_fallback_fail_closed(self):
        route, unknown = self.established(login={"auth": "unknown", "subscription": None,
                                                 "identity_digest": "a" * 64},
                                          agent="claude", model="sonnet")
        self.assertEqual(unknown["hard_stops"], ["billing_mode_unverified"])
        with self.assertRaises(PodError) as blocked:
            require_route_establishment(unknown, route)
        self.assertEqual(blocked.exception.code, "billing_mode_unverified")
        route, paid = self.established(agent="claude", model="sonnet", billing="unknown",
                                       login={"auth": "api_key", "subscription": False,
                                              "identity_digest": "a" * 64})
        self.assertEqual(paid["hard_stops"], ["paid_route_forbidden"])
        with self.assertRaises(PodError) as refused:
            require_route_establishment(paid, route)
        self.assertEqual(refused.exception.code, "paid_route_forbidden")

    def test_delegation_is_described_as_policy_not_as_a_sandbox(self):
        _, established = self.established()
        control = established["controls"]["child_delegation"]
        self.assertFalse(control["enabled"])
        self.assertIn("not a provider sandbox", control["note"])
        self.assertIn("worker-initiated delegation is refused by Pod admission, not by a provider sandbox",
                      established["disclosures"])

    def test_a_route_mismatch_and_an_unproven_runtime_both_refuse(self):
        route, established = self.established()
        with self.assertRaises(PodError) as mismatch:
            require_route_establishment(established, {**route, "account": "other"})
        self.assertEqual(mismatch.exception.code, "account_binding_unverified")
        with self.assertRaises(PodError) as authority:
            require_route_establishment({**established, "runtime": None}, route)
        self.assertEqual(authority.exception.code, "native_authority_unverified")
        with self.assertRaises(PodError) as absent:
            require_route_establishment({"schema": "other/v1"}, route)
        self.assertEqual(absent.exception.code, "route_establishment_missing")

    def test_a_missing_agent_executable_is_unknown_not_a_global_failure(self):
        with patch("pod.orca.shutil.which", return_value=None):
            observed = agent_login_mode("codex")
        self.assertEqual(observed["auth"], "unknown")
        self.assertEqual(observed["reason"], "agent_executable_absent")
        with self.assertRaises(PodError):
            agent_login_mode("cursor")

    def test_a_login_line_on_stderr_is_still_read(self):
        """Codex answers `login status` on stderr; only a live probe showed that."""
        on_stderr = subprocess.CompletedProcess([], 0, "", "Logged in using ChatGPT\n")
        with patch("pod.orca.shutil.which", return_value="/fixture/agent"), \
             patch("pod.orca.subprocess.run", return_value=on_stderr):
            self.assertEqual(agent_login_mode("codex"),
                             {"auth": "oauth", "subscription": True, "identity_digest": None})
        api_on_stderr = subprocess.CompletedProcess([], 0, "", "Logged in using an API key\n")
        with patch("pod.orca.shutil.which", return_value="/fixture/agent"), \
             patch("pod.orca.subprocess.run", return_value=api_on_stderr):
            self.assertEqual(agent_login_mode("codex")["auth"], "api_key")

    def test_login_probes_read_only_the_non_secret_mode(self):
        codex = subprocess.CompletedProcess([], 0, "Logged in using ChatGPT\n", "")
        claude = subprocess.CompletedProcess([], 0, json.dumps({
            "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
            "email": "person@example.invalid", "orgId": "org-secret"}), "")
        with patch("pod.orca.shutil.which", return_value="/fixture/agent"), \
             patch("pod.orca.subprocess.run", return_value=codex) as runner:
            observed = agent_login_mode("codex")
            self.assertEqual(runner.call_args.args[0][1:], ["login", "status"])
        self.assertEqual(observed, {"auth": "oauth", "subscription": True, "identity_digest": None})
        with patch("pod.orca.shutil.which", return_value="/fixture/agent"), \
             patch("pod.orca.subprocess.run", return_value=claude) as runner:
            observed = agent_login_mode("claude")
            self.assertEqual(runner.call_args.args[0][1:], ["auth", "status", "--json"])
        self.assertEqual(observed["auth"], "oauth")
        self.assertTrue(observed["subscription"])
        self.assertNotIn("org-secret", json.dumps(observed))
        self.assertTrue(observed["identity_digest"])


class MutationAllowlistTests(unittest.TestCase):
    def test_only_known_mutations_reach_a_process(self):
        with patch("pod.orca.subprocess.run") as runner:
            for argv in (["orchestration", "reset", "--json"],
                         ["orchestration", "worker-release", "--dispatch", "d", "--json"],
                         ["orchestration", "worker-start", "--task", "t", "--json"],
                         ["orchestration", "worker-start", "--task", "t", "--run", "r",
                          "--worktree", "new-child", "--agent", "codex", "--model", "m",
                          "--effort", "high", "--json"],
                         ["repo", "add", "--path", "/tmp/x", "--json"],
                         ["orchestration", "check"],
                         ["orchestration", "check", "--run", "r", "--wait", "--types", "x",
                          "--timeout-ms", "9000000", "--json"]):
                with self.subTest(argv=argv):
                    with self.assertRaises(PodError) as caught:
                        mutate_command(argv)
                    self.assertEqual(caught.exception.code, "unsupported_orca_mutation")
            runner.assert_not_called()

    def test_a_structured_failure_exit_is_returned_not_raised(self):
        payload = {"ok": True, "result": {"state": "failed", "failedStage": "dispatch_input",
                                          "dispatchId": "ctx_x0001"},
                   "_meta": {"runtimeId": "runtime"}}
        completed = subprocess.CompletedProcess([], 1, json.dumps(payload), "")
        argv = ["orchestration", "worker-start", "--task", "t", "--run", "r", "--worktree",
                "current", "--agent", "codex", "--model", "m", "--effort", "high", "--json"]
        with patch("pod.orca.executable", return_value=Path("orca")), \
             patch("pod.orca.subprocess.run", return_value=completed):
            receipt_value = mutate_command(argv, accept_exit=(0, 1))
            self.assertEqual(receipt_value["exit"], 1)
            self.assertEqual(receipt_value["result"]["state"], "failed")
            with self.assertRaises(PodError) as unexpected:
                mutate_command(argv)
            self.assertEqual(unexpected.exception.code, "native_effect_uncertain")

    def test_worktree_selectors_accept_existing_placements_only(self):
        for value in ("current", "path:/fixture/repo", "id:abc", "name:task", "branch:main"):
            self.assertEqual(worktree_selector(value), value)
        for value in ("new-child", "new-top-level", "", "--worktree", None, "path:"):
            self.assertIsNone(worktree_selector(value))

    def test_identity_twins_are_read_but_conflicts_refuse(self):
        self.assertEqual(identity({"taskId": "t"}, "taskId"), "t")
        self.assertEqual(identity({"task_id": "t"}, "taskId"), "t")
        self.assertEqual(identity({"taskId": "t", "task_id": "t"}, "taskId"), "t")
        with self.assertRaises(PodError) as caught:
            identity({"taskId": "t", "task_id": "other"}, "taskId")
        self.assertEqual(caught.exception.code, "orca_contract")
        self.assertIsNone(identity({"stage": "x"}, "dispatchId"))

    def test_the_read_allowlist_covers_the_verbs_establishment_needs(self):
        with patch("pod.orca.subprocess.run") as runner:
            for argv in (["orchestration", "check", "--run", "r", "--json"],
                         ["orchestration", "worker-start", "--task", "t", "--run", "r",
                          "--worktree", "current", "--agent", "codex", "--model", "m",
                          "--effort", "high", "--json"],
                         ["orchestration", "run-create", "--objective", "o", "--json"]):
                with self.subTest(argv=argv):
                    with self.assertRaises(PodError) as caught:
                        read_command(argv)
                    self.assertEqual(caught.exception.code, "unsupported_orca_read")
            runner.assert_not_called()
        for argv in (["status", "--json"], ["host", "list", "--json"],
                     ["orchestration", "run-current", "--json"],
                     ["orchestration", "request-show", "--request",
                      "11111111-1111-4111-8111-111111111111", "--json"],
                     ["orchestration", "task-list", "--run", "r", "--json"],
                     ["orchestration", "worker-read", "--dispatch", "d", "--limit", "50", "--json"]):
            with self.subTest(argv=argv):
                with patch("pod.orca.executable", return_value=Path("orca")), \
                     patch("pod.orca.subprocess.run",
                           return_value=subprocess.CompletedProcess([], 0, json.dumps(
                               {"ok": True, "result": {}, "_meta": {"runtimeId": "r"}}), "")):
                    self.assertEqual(read_command(argv)["runtime"], "r")


class ReviewFindingRegressions(unittest.TestCase):
    """Each of these reproduces a defect an independent review found."""

    def login(self, agent, stdout="", stderr="", code=0):
        completed = subprocess.CompletedProcess([], code, stdout, stderr)
        with patch("pod.orca.shutil.which", return_value="/fixture/agent"), \
             patch("pod.orca.subprocess.run", return_value=completed):
            return agent_login_mode(agent)

    def test_an_absent_codex_login_is_never_read_as_a_subscription(self):
        """`logged in` is a substring of `not logged in`.

        Reading it as a match reported an absent login as an approved subscription, which
        is the single condition the billing hard stop exists to catch.
        """
        for text in ("Not logged in", "not logged in\n", "You are not signed in",
                     "Logged out", "", "unrecognised output"):
            with self.subTest(text=text):
                observed = self.login("codex", stderr=text)
                self.assertEqual(observed["auth"], "unknown", text)
                self.assertIsNone(observed["subscription"], text)
        self.assertEqual(self.login("codex", stderr="Logged in using ChatGPT")["auth"], "oauth")
        self.assertEqual(self.login("codex", stderr="Logged in using an API key")["auth"], "api_key")

    def test_a_claude_session_that_is_not_logged_in_is_unknown(self):
        observed = self.login("claude", stdout=json.dumps({"loggedIn": False}))
        self.assertEqual(observed["auth"], "unknown")
        self.assertIsNone(observed["subscription"])

    def test_the_quota_bucket_is_derived_and_cannot_be_asserted(self):
        """A caller could name the bucket and have it reported back as an observation."""
        snapshot = {"status": "observed", "runtime": "r", "version": "1.4.206",
                    "executable": "/fixture/orca", "capabilities": {}}
        accounts = {"runtime": "r", "providers": {"claude": {"managed_accounts": 0,
                                             "windows": {"weekly": {}},
                                             "account_association": "host_login"}}}
        asserted = {"agent": "claude", "model": "fable-5", "account": "a" * 64,
                    "bucket": "default", "effort": "high"}
        established = route_establishment(asserted, {"billing": "included"}, snapshot=snapshot,
                                          accounts=accounts,
                                          login={"auth": "oauth", "subscription": True,
                                                 "identity_digest": None},
                                          fleet={"runtime": "r", "hosts": ["local"],
                                                 "local_only": True})
        self.assertEqual(established["route"]["bucket"], "fable")
        self.assertEqual(established["controls"]["quota_bucket"]["bucket"], "fable")
        with self.assertRaises(PodError) as caught:
            require_route_establishment(established, asserted)
        self.assertEqual(caught.exception.code, "account_binding_unverified")

    def test_a_fleet_page_cannot_introduce_a_scope_the_first_page_lacked(self):
        pages = [{"runtime": "r", "result": {"scope": None, "workers": [],
                                             "page": {"hasMore": True, "nextCursor": "next"}}},
                 {"runtime": "r", "result": {"scope": {"source": "all"}, "workers": [],
                                             "page": {"hasMore": False}}}]
        with patch("pod.orca.read_command", side_effect=pages):
            with self.assertRaises(PodError) as caught:
                worker_rows()
        self.assertEqual(caught.exception.code, "orca_scope_changed")

    def test_a_dispatch_identity_is_read_across_both_spellings(self):
        self.assertEqual(identity({"dispatch_id": "ctx_x"}, "dispatchId"), "ctx_x")
        with self.assertRaises(PodError):
            identity({"dispatchId": "ctx_a", "dispatch_id": "ctx_b"}, "dispatchId")
