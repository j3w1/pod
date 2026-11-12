import os
import stat

from pod import routefile
from pod.errors import PodError
from pod.models import edit
from tests.models.support import ModelCase, ROUTES


class EditTests(ModelCase):
    def changed_lines(self, before: str, after: str) -> list:
        old, new = before.split("\n"), after.split("\n")
        return [line for line in new if line not in old] + [f"-{line}" for line in old if line not in new]

    def test_one_change_rewrites_one_line_and_keeps_every_other_byte(self):
        target = self.write_routes(ROUTES)
        result = edit.apply(routes={"codex/gpt-6.1-sol/medium": "enabled"})
        after = target.read_text()
        self.assertEqual(self.changed_lines(ROUTES, after),
                         ["  codex/gpt-6.1-sol/medium: enabled   # kept comment",
                          "-  codex/gpt-6.1-sol/medium: disabled   # kept comment"])
        self.assertEqual(result.routes["codex/gpt-6.1-sol/medium"], "enabled")
        self.assertIn("decision_policy:\n  preset: hands_off\n  categories:\n    merge_publish: any_agent\n", after)

    def test_a_new_route_is_added_inside_the_routes_block(self):
        target = self.write_routes(ROUTES)
        edit.apply(routes={"claude/claude-fable-5-2/high": "enabled"})
        self.assertEqual(target.read_text(), ROUTES.replace(
            "  claude/claude-opus-5-5/high: enabled\n",
            "  claude/claude-opus-5-5/high: enabled\n  claude/claude-fable-5-2/high: enabled\n"))

    def test_preferred_and_pin_change_only_their_own_lines(self):
        target = self.write_routes(ROUTES)
        edit.apply(preferred="codex/gpt-6.1-sol/high", pinned="claude/claude-opus-5-5/high")
        self.assertEqual(target.read_text(), ROUTES.replace("preferred: null", "preferred: codex/gpt-6.1-sol/high")
                         .replace("pinned: null", "pinned: claude/claude-opus-5-5/high"))
        edit.apply(pinned=None)
        self.assertEqual(routefile.load().pinned, None)
        self.assertEqual(routefile.load().preferred, "codex/gpt-6.1-sol/high")

    def test_windows_line_endings_and_missing_keys_survive(self):
        text = "schema: pod/v2\r\nroutes:\r\n  codex/gpt-6.1-sol/high: disabled\r\n"
        target = self.write_routes("")
        target.write_bytes(text.encode())
        edit.apply(routes={"codex/gpt-6.1-sol/high": "enabled"}, preferred="codex/gpt-6.1-sol/high")
        self.assertEqual(target.read_bytes(), b"schema: pod/v2\r\nroutes:\r\n  codex/gpt-6.1-sol/high: enabled\r\n"
                                              b"preferred: codex/gpt-6.1-sol/high\r\n")

    def test_an_inline_empty_routes_value_becomes_a_block(self):
        target = self.write_routes("schema: pod/v2\nroutes: {}\npreferred: null\n")
        edit.apply(routes={"codex/gpt-6.1-sol/low": "enabled"})
        self.assertEqual(target.read_text(),
                         "schema: pod/v2\nroutes:\n  codex/gpt-6.1-sol/low: enabled\npreferred: null\n")

    def test_the_first_explicit_change_creates_the_file_private_to_the_person(self):
        target = routefile.load().path
        self.assertFalse(target.exists())
        edit.apply(routes={"codex/gpt-6.1-sol/low": "enabled"})
        self.assertEqual(target.read_text(),
                         "schema: pod/v2\nroutes:\n  codex/gpt-6.1-sol/low: enabled\npreferred: null\npinned: null\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(routefile.load().enabled(), ["codex/gpt-6.1-sol/low"])

    def test_an_invalid_file_is_refused_and_left_untouched(self):
        target = self.write_routes("schema: pod/v2\nroutes: [a, b]\n")
        before = target.read_bytes()
        with self.assertRaises(PodError) as caught:
            edit.apply(routes={"codex/gpt-6.1-sol/low": "enabled"})
        self.assertEqual(caught.exception.code, "preferences_invalid")
        self.assertIn("pod config edit", caught.exception.fix)
        self.assertEqual(target.read_bytes(), before)

    def test_refusals_change_nothing(self):
        target = self.write_routes(ROUTES.replace("pinned: null", "pinned: codex/gpt-6.1-sol/high"))
        before = target.read_bytes()
        cases = {
            "route_not_routable": dict(routes={"codex/gpt-9/low": "enabled"}),
            "route_in_use": dict(routes={"codex/gpt-6.1-sol/high": "disabled"}),
            "route_not_enabled": dict(preferred="codex/gpt-6.1-sol/medium"),
            "route_invalid": dict(routes={"codex/gpt-6.1-sol/high": "on"}),
            "preferences_changed": dict(routes={"codex/gpt-6.1-sol/medium": "enabled"}, expected=b"earlier"),
        }
        for code, changes in cases.items():
            with self.subTest(code), self.assertRaises(PodError) as caught:
                edit.apply(**changes)
            self.assertEqual(caught.exception.code, code)
            self.assertTrue(caught.exception.fix)
            self.assertEqual(target.read_bytes(), before)

    def test_a_stored_key_outside_the_registry_can_be_disabled_but_not_enabled(self):
        self.write_routes(ROUTES.replace("routes:\n", "routes:\n  codex/gpt-9/low: enabled\n"))
        edit.apply(routes={"codex/gpt-9/low": "disabled"})
        self.assertEqual(routefile.load().routes["codex/gpt-9/low"], "disabled")
        with self.assertRaises(PodError):
            edit.apply(routes={"codex/gpt-9/low": "enabled"})

    def test_a_pin_can_be_cleared_and_its_route_disabled_in_one_change(self):
        self.write_routes(ROUTES.replace("pinned: null", "pinned: codex/gpt-6.1-sol/high"))
        result = edit.apply(routes={"codex/gpt-6.1-sol/high": "disabled"}, pinned=None)
        self.assertEqual((result.pinned, result.routes["codex/gpt-6.1-sol/high"]), (None, "disabled"))

    def test_no_change_does_not_touch_the_file(self):
        target = self.write_routes(ROUTES)
        os.utime(target, (1, 1))
        edit.apply(routes={"codex/gpt-6.1-sol/high": "enabled"}, preferred=None)
        self.assertEqual(target.stat().st_mtime, 1)
        self.assertEqual(sorted(os.listdir(target.parent)), [".config.lock", "config.yaml"])


if __name__ == "__main__":
    import unittest
    unittest.main()
