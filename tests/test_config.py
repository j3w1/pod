import os
from contextlib import chdir
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pod.config import effective, personal_path, read_yaml, route_identity
from pod.errors import PodError
from pod.ledger import checkpoint, read, state_root
from pod.util import native_home
from tests.common import fixture


class ConfigTests(unittest.TestCase):
    def test_fixture_ignores_and_restores_inherited_pod_homes(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("POD_TEST_ROOT")) as cache_name:
            cache = Path(cache_name)
            inherited = {"POD_CONFIG_HOME": str(cache / "inherited-config"),
                         "POD_STATE_HOME": str(cache / "inherited-state")}
            with patch.dict(os.environ, {**inherited, "POD_FIXTURE_PRESERVED": "yes"}):
                with fixture() as root:
                    self.assertIsNone(os.environ.get("POD_CONFIG_HOME"))
                    self.assertIsNone(os.environ.get("POD_STATE_HOME"))
                    self.assertEqual(os.environ["POD_FIXTURE_PRESERVED"], "yes")
                    project = root / "project"
                    project.mkdir()
                    personal = personal_path()
                    personal.parent.mkdir(parents=True)
                    personal.write_text("schema: pod/v1\n")
                    policy = effective(project)
                    checkpoint(project, "fixture isolation", owner="owner",
                               native={"runtime": "runtime"},
                               value={"schema": "pod-checkpoint/v1", "criteria": ["isolated"],
                                      "plan_revision": "plan", "candidate": "candidate",
                                      "policy_revision": policy["revision"], "native_refs": [],
                                      "assignments": [], "questions": [],
                                      "verification_gaps": ["isolated"],
                                      "next_safe_action": "verify"})
                    self.assertTrue(personal.is_relative_to(root.parent))
                    self.assertTrue(state_root().is_relative_to(root.parent))
                    self.assertFalse(personal.is_relative_to(root))
                    self.assertFalse(state_root().is_relative_to(root))
                    self.assertFalse((cache / "inherited-config").exists())
                    self.assertFalse((cache / "inherited-state").exists())
                self.assertEqual({key: os.environ[key] for key in inherited}, inherited)
                self.assertFalse((cache / "inherited-config").exists())
                self.assertFalse((cache / "inherited-state").exists())

    def test_pod_only_homes_isolate_personal_policy_and_state(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            config_home = root / "pod-config"
            state_home = root / "pod-state"
            config_home.mkdir()
            (config_home / "config.yaml").write_text("schema: pod/v1\n")
            native = {"APPDATA": str(root / "native-appdata"),
                      "LOCALAPPDATA": str(root / "native-localappdata"),
                      "CODEX_HOME": str(root / "native-codex"),
                      "CLAUDE_CONFIG_DIR": str(root / "native-claude")}
            overrides = {"POD_CONFIG_HOME": str(config_home), "POD_STATE_HOME": str(state_home)}
            with patch.dict(os.environ, {**native, **overrides}):
                policy = effective(project)
                self.assertEqual(personal_path(), config_home / "config.yaml")
                self.assertFalse(policy["policy"]["models"]["sol"]["approved"])
                checkpoint(project, "objective", owner="owner", native={"runtime": "runtime"},
                           value={"schema": "pod-checkpoint/v1", "criteria": ["works"],
                                  "plan_revision": "plan", "candidate": "candidate",
                                  "policy_revision": policy["revision"], "native_refs": [],
                                  "assignments": [], "questions": [], "verification_gaps": ["works"],
                                  "next_safe_action": "verify"})
                self.assertEqual(state_root(), state_home)
                self.assertIsNotNone(read(project, "objective"))
                self.assertTrue(any(state_home.glob("*/context.json")))
                self.assertEqual({key: os.environ[key] for key in native}, native)
                self.assertFalse((root / "native-appdata").exists())
                self.assertFalse((root / "native-localappdata").exists())

                (project / ".pod").mkdir()
                (project / ".pod" / "config.yaml").write_text(
                    "schema: pod/v1\npolicy: {max_workers: 4}\n")
                with self.assertRaises(PodError) as expanded:
                    effective(project)
                self.assertEqual(expanded.exception.code, "authority_expansion")

    def test_pod_home_overrides_require_absolute_directories(self):
        with fixture() as root:
            for name, resolve in (("POD_CONFIG_HOME", personal_path),
                                  ("POD_STATE_HOME", state_root)):
                for value in ("", ".", "relative/path"):
                    with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                        with self.assertRaises(PodError) as caught:
                            resolve()
                        self.assertEqual(caught.exception.code, "invalid_location_override")
                existing_file = root / f"{name}.txt"
                existing_file.write_text("not a directory")
                with patch.dict(os.environ, {name: str(existing_file)}):
                    with self.assertRaises(PodError) as caught:
                        resolve()
                    self.assertEqual(caught.exception.code, "invalid_location_override")

    def test_no_file_is_pending_and_read_only(self):
        with fixture() as root:
            personal = root / "none.yaml"
            value = effective(root, personal=personal)
            self.assertFalse(value["policy"]["models"]["sol"]["approved"])
            self.assertFalse(personal.exists())
            self.assertFalse((root / ".pod").exists())

    def test_personal_approval_and_project_restriction(self):
        with fixture() as root:
            personal = root / "personal.yaml"
            binding = route_identity({"agent": "codex", "model": "gpt-5.6-sol", "account": "acct"})
            personal.write_text(f"""schema: pod/v1
models:
  sol:
    agent: codex
    model: gpt-5.6-sol
    account: acct
    approved: true
    approval_ref: reviewed-grant
    approval_route: {binding}
    billing: included
    efforts: [high, medium]
policy:
  max_workers: 3
  allowed_agents: [codex, claude]
""")
            (root / ".pod").mkdir()
            (root / ".pod" / "config.yaml").write_text("""schema: pod/v1
models:
  sol:
    efforts: [high]
policy:
  max_workers: 2
  allowed_agents: [codex]
""")
            value = effective(root, personal=personal)
            self.assertEqual(value["policy"]["models"]["sol"]["efforts"], ["high"])
            self.assertEqual(value["policy"]["policy"]["max_workers"], 2)
            self.assertEqual(value["provenance"]["models.sol"], "project")
            self.assertEqual(value["policy"]["routing"]["complex"]["model"], "sol")

    def test_project_cannot_grant_approval_or_capacity(self):
        with fixture() as root:
            (root / ".pod").mkdir()
            path = root / ".pod" / "config.yaml"
            for content in (
                "models: {sol: {approved: true, approval_ref: forged}}",
                "policy: {max_workers: 4}",
                "policy: {spending_grants: [{id: fake, action: paid_usage, account: a, valid_until: '2099-01-01T00:00:00Z'}]}",
            ):
                path.write_text("schema: pod/v1\n" + content + "\n")
                with self.assertRaises(PodError):
                    effective(root, personal=root / "none")

    def test_project_cannot_extend_personal_quota_freshness(self):
        with fixture() as root:
            personal = root / "personal.yaml"
            personal.write_text("schema: pod/v1\npolicy: {quota_fresh_seconds: 60}\n")
            (root / ".pod").mkdir()
            local = root / ".pod" / "config.yaml"
            local.write_text("schema: pod/v1\npolicy: {quota_fresh_seconds: 3600}\n")
            with self.assertRaises(PodError) as caught:
                effective(root, personal=personal)
            self.assertEqual(caught.exception.code, "authority_expansion")
            local.write_text("schema: pod/v1\npolicy: {quota_fresh_seconds: 30}\n")
            self.assertEqual(effective(root, personal=personal)["policy"]["policy"]["quota_fresh_seconds"], 30)

    def test_local_routing_can_tighten_but_not_weaken_or_replace_strict_pin(self):
        with fixture() as root:
            personal = root / "personal.yaml"
            personal.write_text("schema: pod/v1\nrouting:\n  complex: {model: sol, effort: high, strict: true}\n")
            (root / ".pod").mkdir()
            local = root / ".pod" / "config.yaml"
            for row in ("{strict: false}", "{model: terra}"):
                local.write_text(f"schema: pod/v1\nrouting:\n  complex: {row}\n")
                with self.assertRaises(PodError) as caught:
                    effective(root, personal=personal)
                self.assertEqual(caught.exception.code, "authority_expansion")
            local.write_text("schema: pod/v1\nrouting:\n  complex: {effort: xhigh}\n")
            row = effective(root, personal=personal)["policy"]["routing"]["complex"]
            self.assertEqual(row, {"model": "sol", "effort": "xhigh", "strict": True})
            with self.assertRaises(PodError) as task_layer:
                effective(root, personal=personal,
                          task={"schema": "pod/v1", "routing": {"complex": {"strict": False}}})
            self.assertEqual(task_layer.exception.code, "authority_expansion")

            personal.write_text("schema: pod/v1\nrouting:\n  complex: {model: sol, effort: high}\n")
            local.write_text("schema: pod/v1\nrouting:\n  complex: {model: terra, strict: true}\n")
            row = effective(root, personal=personal)["policy"]["routing"]["complex"]
            self.assertEqual(row["model"], "terra")
            self.assertTrue(row["strict"])

    def test_native_home_variables_reject_relative_or_malformed_values(self):
        with fixture() as root:
            for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "APPDATA", "LOCALAPPDATA",
                         "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
                for value in ("", ".", "relative/path", "bad\x00path"):
                    environment = {**os.environ, name: value}
                    with self.subTest(name=name, value=repr(value)), patch.object(os, "environ", environment):
                        with self.assertRaises(PodError) as caught:
                            native_home(name, default=root / "safe-default")
                        self.assertEqual(caught.exception.code, "invalid_native_home")
                existing_file = root / f"{name}.txt"
                existing_file.write_text("not a directory")
                with patch.dict(os.environ, {name: str(existing_file)}):
                    with self.assertRaises(PodError) as caught:
                        native_home(name, default=root / "safe-default")
                    self.assertEqual(caught.exception.code, "invalid_native_home")

    def test_relative_xdg_homes_cannot_place_policy_or_state_in_project(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            previous = {name: os.environ[name] for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative-config",
                                         "XDG_STATE_HOME": "relative-state"}):
                with self.assertRaises(PodError):
                    personal_path()
                with self.assertRaises(PodError):
                    state_root()
                self.assertEqual(list(project.iterdir()), [])
            self.assertEqual({name: os.environ[name] for name in previous}, previous)

    def test_native_homes_reject_absolute_and_symlink_project_containment(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            contained = project / "native-home"
            contained.mkdir()
            redirected = root.parent / "redirected-native-home"
            redirected.symlink_to(contained, target_is_directory=True)
            original = dict(os.environ)
            for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "APPDATA", "LOCALAPPDATA",
                         "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
                for value in (contained, redirected):
                    with self.subTest(name=name, value=value), patch.dict(os.environ, {name: str(value)}):
                        with self.assertRaises(PodError) as caught:
                            native_home(name, project=project)
                        self.assertEqual(caught.exception.code, "project_contained_native_home")
            self.assertEqual(dict(os.environ), original)

    def test_default_config_and_state_calls_reject_project_containment_but_pod_overrides_remain_explicit(self):
        with fixture() as root:
            project = root / "project"
            project.mkdir()
            contained_config = project / "config-home"
            contained_state = project / "state-home"
            overrides = {"XDG_CONFIG_HOME": str(contained_config),
                         "XDG_STATE_HOME": str(contained_state)}
            with chdir(project), patch.dict(os.environ, overrides):
                with self.assertRaises(PodError):
                    personal_path()
                with self.assertRaises(PodError):
                    effective(project)
                with self.assertRaises(PodError):
                    state_root()
                with self.assertRaises(PodError):
                    checkpoint(project, "blocked", owner="owner", native={"runtime": "runtime"},
                               value={"schema": "pod-checkpoint/v1", "criteria": ["safe"],
                                      "plan_revision": "plan", "candidate": "candidate",
                                      "policy_revision": "policy", "native_refs": [],
                                      "assignments": [], "questions": [],
                                      "verification_gaps": ["safe"],
                                      "next_safe_action": "choose external native homes"})
                self.assertEqual(list(project.iterdir()), [])
            pod_config = project / "explicit-pod-config"
            pod_state = project / "explicit-pod-state"
            with chdir(project), patch.dict(os.environ, {
                    **overrides, "POD_CONFIG_HOME": str(pod_config),
                    "POD_STATE_HOME": str(pod_state)}):
                self.assertEqual(personal_path(), pod_config / "config.yaml")
                self.assertEqual(state_root(), pod_state)

    def test_changed_model_identity_invalidates_prior_approval_binding(self):
        with fixture() as root:
            personal = root / "personal.yaml"
            binding = route_identity({"agent": "codex", "model": "original", "account": "account"})
            personal.write_text(f"""schema: pod/v1
models:
  sol:
    agent: codex
    model: changed
    account: account
    approved: true
    approval_ref: original-review
    approval_route: {binding}
""")
            with self.assertRaises(PodError) as caught:
                effective(root, personal=personal)
            self.assertEqual(caught.exception.code, "invalid_config")

    def test_duplicate_alias_tag_include_size_and_type_fail(self):
        with fixture() as root:
            path = root / "p.yaml"
            for value in (
                "schema: pod/v1\nschema: pod/v1\n",
                "schema: !!python/object/apply:os.system ['echo bad']\n",
                "schema: pod/v1\nx: !include secret\n",
                "schema: pod/v1\npolicy: [wrong]\n",
                "schema: pod/v1\nmodels: {a: &x {agent: codex}, b: *x}\n",
            ):
                path.write_text(value)
                with self.assertRaises(PodError):
                    read_yaml(path)
            path.write_text("a" * 65537)
            with self.assertRaises(PodError):
                read_yaml(path)
