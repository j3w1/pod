import subprocess
import sys

from pod import routefile
from pod.errors import PodError
from tests.support import PodCase, ROOT

FULL = """schema: pod/v2
routes:
  codex/gpt-6.1-sol/high: enabled   # the usual one
  claude/claude-opus-5-5/max: disabled
preferred: codex/gpt-6.1-sol/high
pinned: null
workers:
  max_active: 6
authorization_verifier:
- /usr/local/bin/verifier
- --strict
decision_policy:
  preset: hands_off
  categories:
    merge_publish: any_agent
tool_permissions:
- tool: x
  decision:
    actor: {kind: person}
"""


class RouteFileTests(PodCase):
    def test_it_reads_routes_preferred_pin_and_the_verifier_and_ignores_keys_it_does_not_use(self):
        found = routefile.load(self.write_routes(FULL))
        self.assertEqual((found.status, found.problem), ("ok", None))
        self.assertEqual(found.enabled(), ["codex/gpt-6.1-sol/high"])
        self.assertEqual((found.preferred, found.pinned), ("codex/gpt-6.1-sol/high", None))
        self.assertEqual(found.authorization_verifier, ["/usr/local/bin/verifier", "--strict"])

    def test_only_the_person_setting_lets_the_coordinator_authorize(self):
        self.assertEqual(routefile.load(self.write_routes(FULL)).merge_authorization, "person")
        found = routefile.load(self.write_routes(FULL + "merge_authorization: coordinator\n"))
        self.assertEqual(found.merge_authorization, "coordinator")
        self.assertEqual(routefile.load(self.write_routes(FULL + "merge_authorization: anyone\n")).status, "invalid")

    def test_reading_never_writes_the_file(self):
        path = self.write_routes(FULL)
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        routefile.load(path).check("codex/gpt-6.1-sol/high")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_a_route_that_is_not_enabled_is_refused_with_the_enabled_ones(self):
        found = routefile.load(self.write_routes(FULL))
        for route, code in (("claude/claude-opus-5-5/max", "route_not_enabled"), ("codex/unlisted/high", "route_not_enabled"),
                            ("nonsense", "route_invalid")):
            with self.assertRaises(PodError) as caught:
                found.check(route)
            self.assertEqual(caught.exception.code, code)
            self.assertIn("codex/gpt-6.1-sol/high", caught.exception.fix)

    def test_while_a_pin_is_set_no_other_route_is_used(self):
        text = FULL.replace("pinned: null", "pinned: codex/gpt-6.1-sol/high").replace("max: disabled", "max: enabled")
        found = routefile.load(self.write_routes(text))
        found.check("codex/gpt-6.1-sol/high")
        with self.assertRaises(PodError) as caught:
            found.check("claude/claude-opus-5-5/max")
        self.assertEqual(caught.exception.code, "route_not_pinned")

    def test_an_absent_or_unreadable_file_makes_no_route_eligible_and_is_left_alone(self):
        absent = routefile.load()
        self.assertEqual((absent.status, absent.enabled()), ("absent", []))
        for text in ("schema: pod/v1\nmodels: {}\n", "schema: pod/v2\nroutes:\n  bad key: enabled\n",
                     "schema: pod/v2\nroutes:\n  a/b/c: maybe\n", "schema: pod/v2\npreferred: a/b/c\n",
                     "schema: pod/v2\nroutes: [a, b]\n", "\tschema: pod/v2\n", "schema: pod/v2\nschema: pod/v2\n"):
            path = self.write_routes(text)
            found = routefile.load(path)
            self.assertEqual((found.status, found.enabled(), found.merge_authorization), ("invalid", [], "person"), text)
            self.assertTrue(found.problem)
            self.assertEqual(path.read_text(), text)
            with self.assertRaises(PodError) as caught:
                found.check("a/b/c")
            self.assertEqual(caught.exception.code, "routes_unavailable")

    def test_coordination_loads_no_model_terminal_or_network_code(self):
        code = ("import sys; import pod.cli, pod.commands, pod.gate, pod.routefile; "
                "bad = [m for m in sys.modules if m.split('.')[0] in ('curses', 'urllib', 'html', 'http', 'yaml') "
                "or m.startswith('pod.models')]; print(bad); raise SystemExit(bool(bad))")
        done = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
                              env={"PYTHONPATH": str(ROOT / "skills"), "PATH": "/usr/bin:/bin"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
