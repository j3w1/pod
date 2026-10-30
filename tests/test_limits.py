"""Prospective limit policy boundaries; no installed configuration effects."""

from dataclasses import FrozenInstanceError
import unittest

from pod.errors import PodError
from pod.limits import DEFAULT_LIMITS, HARD_LIMITS, Limits, parse_limits


class LimitsTests(unittest.TestCase):
    def test_requested_defaults_distinguish_bytes_and_logical_counts(self):
        self.assertEqual(parse_limits({}).as_dict(), {
            "record_bytes": 524288,
            "context_bytes": 2097152,
            "governance_history": 1024,
            "findings": 128,
            "successor_chain": 64,
        })

    def test_each_soft_limit_accepts_values_around_its_default(self):
        for field, default in DEFAULT_LIMITS.as_dict().items():
            for value in (default - 1, default, default + 1):
                with self.subTest(field=field, value=value):
                    actual = parse_limits({field: value}).as_dict()
                    self.assertEqual(actual, {**DEFAULT_LIMITS.as_dict(), field: value})

    def test_each_hard_boundary_accepts_below_equal_and_refuses_above(self):
        for field, ceiling in HARD_LIMITS.items():
            for value in (1, ceiling - 1, ceiling):
                with self.subTest(field=field, value=value):
                    self.assertEqual(getattr(parse_limits({field: value}), field), value)
            with self.subTest(field=field), self.assertRaises(PodError) as caught:
                parse_limits({field: ceiling + 1})
            self.assertEqual(caught.exception.code, "invalid_limits")
            self.assertEqual(caught.exception.detail, {"field": field, "hard_limit": ceiling})

    def test_invalid_numbers_never_coerce_or_select_a_default(self):
        for field in HARD_LIMITS:
            for value in (True, False, 0, -1, 1.0, "128", None, [], {}):
                with self.subTest(field=field, kind=type(value).__name__):
                    with self.assertRaises(PodError) as caught:
                        parse_limits({field: value})
                    self.assertEqual(caught.exception.code, "invalid_limits")

    def test_explicit_null_and_unknown_keys_refuse_without_echoing_values(self):
        secret = "untrusted-private-value"
        for value in (None, False, [], 128, {"hard_limits": secret}, {1: secret}):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(PodError) as caught:
                    parse_limits(value)
                self.assertNotIn(secret, str(caught.exception))
                self.assertEqual(caught.exception.code, "invalid_limits")

    def test_direct_construction_cannot_skip_the_safety_ceiling(self):
        with self.assertRaises(PodError):
            Limits(context_bytes=HARD_LIMITS["context_bytes"] + 1)
        with self.assertRaises(PodError):
            Limits(findings=True)

    def test_parsing_is_detached_and_cannot_change_defaults_or_ceilings(self):
        given = {"findings": 256}
        parsed = parse_limits(given)
        self.assertEqual(given, {"findings": 256})
        given["findings"] = 1
        exported = parsed.as_dict()
        exported["findings"] = 2
        self.assertEqual(parsed.findings, 256)
        self.assertEqual(DEFAULT_LIMITS.findings, 128)
        with self.assertRaises(FrozenInstanceError):
            parsed.findings = 512
        with self.assertRaises(TypeError):
            HARD_LIMITS["findings"] = 2048
