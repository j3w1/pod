"""Observation snapshot, cache and refresh transaction, offline with controlled time."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import partial
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pod import observations, sources
from pod.errors import PodError
from tests.test_sources import Server, aa_page, aa_row, fixture

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
AA_URL, ANTHROPIC_URL, OPENAI_URL = (source.url for source in sources.SOURCES)


def pages(**changes) -> dict:
    base = {AA_URL: fixture("aa-leaderboard.html"),
            ANTHROPIC_URL: fixture("anthropic-models-overview.html"),
            OPENAI_URL: fixture("openai-models.html")}
    return base | changes


class FakeFetch:
    """Injected fetch: returns fixture text or raises the configured SourceError."""

    def __init__(self, responses: dict, hook=None):
        self.responses, self.hook, self.calls = responses, hook, []

    def __call__(self, url, *, deadline, cancel):
        self.calls.append(url)
        if self.hook:
            self.hook(url)
        value = self.responses[url]
        if isinstance(value, BaseException):
            raise value
        return sources.Page(url, value)


def aa_names() -> list[str]:
    return [row["name"] for row in sources.parse_aa(fixture("aa-leaderboard.html"))["rows"]]


def tree_digest(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file() and "__pycache__" not in path.parts}


class ObservationCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.config = self.base / "config"
        self.config.mkdir()
        self.preferences = self.config / "config.yaml"
        self.preferences.write_bytes(b"schema: pod/v2\n# user comment kept\nworkers:\n  max_active: 2\n")
        environment = patch.dict(os.environ, {"POD_CACHE_HOME": str(self.base / "cache"),
                                              "POD_CONFIG_HOME": str(self.config),
                                              "POD_STATE_HOME": str(self.base / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = self.base / "cache" / "models"

    def refresh(self, responses=None, *, now=T0, **options):
        fetch = options.pop("fetch", None) or FakeFetch(responses or pages())
        return observations.refresh(now=now, fetch=fetch, **options), fetch

    def current_bytes(self):
        return {name: (self.root / name).read_bytes() if (self.root / name).exists() else None
                for name in ("current.json", "previous.json")}

    def record(self) -> dict:
        return json.loads((self.root / "refresh.json").read_text())


class RefreshTransactionTests(ObservationCase):
    def test_no_cache_reads_the_bundled_snapshot_then_unknown_without_network(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            view = observations.load(T0)
            self.assertEqual(view["origin"], "bundled")
            self.assertIsNotNone(view["snapshot"])
            with patch.object(observations, "BUNDLED_PATH", self.base / "missing.json"):
                view = observations.load(T0)
        self.assertEqual((view["origin"], view["snapshot"], view["stale"], view["age_s"]),
                         ("unknown", None, True, None))
        self.assertIsNone(observations.previous())

    def test_valid_update_promotes_then_rotates_previous(self):
        result, fetch = self.refresh()
        self.assertEqual((result["outcome"], result["generation"], result["base_generation"]),
                         ("promoted", "1", None))
        self.assertEqual(fetch.calls, [AA_URL, ANTHROPIC_URL, OPENAI_URL])
        first = observations.load(T0)
        self.assertEqual((first["origin"], first["snapshot"]["generation"]), ("cache", "1"))
        self.assertEqual(first["snapshot"]["sources"][sources.AA]["retrieved_at"], "2026-10-01T12:00:00Z")
        self.assertEqual(result["sources"][sources.AA], {
            "status": "ok", "rows": 36, "mapped": 33, "retrieved_at": "2026-10-01T12:00:00Z",
            "retry_after_until": None, "diagnostics": []})
        self.assertIsNone(observations.previous())

        later = T0 + timedelta(days=2)
        changed = pages(**{AA_URL: fixture("aa-leaderboard.html").replace("$0.39", "$0.41")})
        result, _ = self.refresh(changed, now=later)
        self.assertEqual((result["outcome"], result["generation"], result["base_generation"]),
                         ("promoted", "2", "1"))
        self.assertEqual(result["diff"][sources.AA]["changed"], ["GPT-6.1 Sol (xhigh)"])
        self.assertEqual(result["diff"][sources.AA]["counts"], {"added": 0, "removed": 0, "changed": 1})
        self.assertEqual(observations.previous(), first["snapshot"])
        self.assertEqual(observations.load(later)["snapshot"]["generation"], "2")
        self.assertEqual(self.record()["attempt"]["outcome"], "promoted")

    def test_rows_keep_exact_source_names_and_no_route_fields(self):
        self.refresh()
        snapshot = observations.load(T0)["snapshot"]
        rows = {row["name"]: row for row in snapshot["sources"][sources.AA]["rows"]}
        self.assertEqual(set(rows["Claude Opus 5.5 (max with fallback)"]), {"name", "creator", "qualifiers", "metrics"})
        self.assertEqual(rows["Claude Opus 5.5 (max with fallback)"]["qualifiers"], ["with fallback"])
        self.assertIn("GPT-5.3 Codex (xhigh)", rows, "unmapped rows stay visible")
        self.assertIn("GPT-6 Sol (xhigh)", rows, "older generations stay visible")
        self.assertEqual(snapshot["sources"][sources.AA]["methodology"], "Artificial Analysis Intelligence Index v4.3")

    def test_check_validates_and_diffs_without_promoting(self):
        result, fetch = self.refresh(check=True)
        self.assertEqual((result["outcome"], result["promoted"], result["generation"]), ("checked", False, None))
        self.assertEqual(len(fetch.calls), 3)
        self.assertEqual(set(result["diff"]), {source.id for source in sources.SOURCES})
        self.assertFalse((self.root / "current.json").exists())
        self.refresh()
        before = self.current_bytes()
        result, _ = self.refresh(pages(**{AA_URL: fixture("aa-leaderboard.html").replace("$3.46", "$3.50")}),
                                 now=T0 + timedelta(hours=30), check=True)
        self.assertEqual(result["outcome"], "checked")
        self.assertEqual(result["diff"][sources.AA]["changed"], ["Claude Opus 5.5 (xhigh with fallback)"])
        self.assertEqual(self.current_bytes(), before)

    def test_failed_required_source_keeps_the_prior_snapshot(self):
        self.refresh()
        before = self.current_bytes()
        for label, value in (("malformed", fixture("aa-leaderboard.html")[:9000]),
                             ("access_denied", sources.SourceError("access_denied", "HTTP 403 bot challenge")),
                             ("unavailable", sources.SourceError("unavailable", "Network error: OSError"))):
            with self.subTest(label=label):
                result, fetch = self.refresh(pages(**{AA_URL: value}), now=T0 + timedelta(days=1))
                self.assertEqual(result["outcome"], "failed")
                self.assertEqual(result["sources"][sources.AA]["status"], label)
                self.assertEqual(fetch.calls, [AA_URL], "optional sources are skipped after a required failure")
                self.assertEqual(self.current_bytes(), before)
                self.assertEqual(observations.load(T0)["origin"], "cache")
                self.assertEqual(self.record()["attempt"]["outcome"], "failed")

    def test_corrupt_cache_falls_back_with_a_diagnostic_and_refresh_recovers(self):
        self.refresh()
        (self.root / "current.json").write_text('{"schema": "pod-observations/v1", "schema": 1}')
        view = observations.load(T0)
        self.assertEqual(view["origin"], "bundled")
        self.assertTrue(any("current.json ignored" in item for item in view["diagnostics"]))
        result, _ = self.refresh(now=T0 + timedelta(hours=1))
        self.assertEqual((result["outcome"], result["generation"]), ("promoted", "1"))
        self.assertEqual(observations.load(T0)["origin"], "cache")

    def test_interruption_before_promotion_changes_nothing(self):
        self.refresh()
        before = self.current_bytes()

        def interrupt(url):
            if url == OPENAI_URL:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.refresh(fetch=FakeFetch(pages(), hook=interrupt), now=T0 + timedelta(days=2))
        self.assertEqual(self.current_bytes(), before)
        self.assertEqual(self.record()["attempt"]["outcome"], "running")
        self.assertFalse(observations.auto_refresh_due("automatic", T0 + timedelta(days=2, minutes=5)))

    def test_interruption_during_promotion_leaves_a_valid_current(self):
        self.refresh()
        original = observations.atomic_json

        def crash_on_current(path, value, **kwargs):
            if path.name == "current.json":
                raise KeyboardInterrupt
            return original(path, value, **kwargs)

        with patch.object(observations, "atomic_json", crash_on_current), self.assertRaises(KeyboardInterrupt):
            self.refresh(now=T0 + timedelta(days=2))
        view = observations.load(T0 + timedelta(days=2))
        self.assertEqual((view["origin"], view["snapshot"]["generation"]), ("cache", "1"))
        self.assertEqual(observations.previous()["generation"], "1")

    def test_cancel_before_and_during_refresh_never_promotes(self):
        event = threading.Event()
        event.set()
        result, fetch = self.refresh(cancel=event)
        self.assertEqual((result["outcome"], fetch.calls), ("cancelled", []))
        self.assertFalse((self.root / "current.json").exists())
        event = threading.Event()
        result, fetch = self.refresh(fetch=FakeFetch(pages(), hook=lambda url: event.set()), cancel=event)
        self.assertEqual(result["outcome"], "cancelled")
        self.assertEqual(fetch.calls, [AA_URL])
        self.assertFalse((self.root / "current.json").exists())
        self.assertEqual(self.record()["attempt"]["outcome"], "cancelled")

    def test_cancel_checked_again_at_promotion(self):
        calls = []

        def cancel():
            calls.append(1)
            return len(calls) > 3

        result, _ = self.refresh(cancel=cancel)
        self.assertEqual(result["outcome"], "cancelled")
        self.assertFalse((self.root / "current.json").exists())

    def test_out_of_order_refresh_cannot_overwrite_newer_data(self):
        self.refresh()
        release, entered = threading.Event(), threading.Event()
        holder = {}

        def slow(url):
            if url == AA_URL:
                entered.set()
                release.wait(5)

        older_pages = pages(**{AA_URL: fixture("aa-leaderboard.html").replace("$0.39", "$0.10")})
        worker = threading.Thread(target=lambda: holder.update(result=observations.refresh(
            now=T0 + timedelta(days=1), fetch=FakeFetch(older_pages, hook=slow))))
        worker.start()
        self.assertTrue(entered.wait(5))
        newer = pages(**{AA_URL: fixture("aa-leaderboard.html").replace("$0.39", "$0.55")})
        result, _ = self.refresh(newer, now=T0 + timedelta(days=1, minutes=1))
        self.assertEqual((result["outcome"], result["generation"]), ("promoted", "2"))
        release.set()
        worker.join(5)
        self.assertEqual(holder["result"]["outcome"], "superseded")
        self.assertIsNone(holder["result"]["generation"])
        snapshot = observations.load(T0 + timedelta(days=1))["snapshot"]
        sol = next(row for row in snapshot["sources"][sources.AA]["rows"] if row["name"] == "GPT-6.1 Sol (xhigh)")
        self.assertEqual((snapshot["generation"], sol["metrics"]["usd_per_task"]), ("2", 0.55))
        self.assertEqual(observations.previous()["generation"], "1")

    def test_generation_stays_monotonic_after_a_corrupt_current(self):
        self.refresh()
        self.refresh(now=T0 + timedelta(days=1))
        (self.root / "current.json").write_text("not json")
        result, _ = self.refresh(now=T0 + timedelta(days=2))
        self.assertEqual(result["generation"], "2")
        self.assertEqual(observations.previous()["generation"], "1")

    def test_severe_coverage_loss_is_refused(self):
        self.refresh()
        before = self.current_bytes()
        names = aa_names()
        collapsed = aa_page([aa_row(name=name, creator="Anthropic" if name.startswith("Claude") else "OpenAI")
                             for name in names[:4]])
        result, _ = self.refresh(pages(**{AA_URL: collapsed}), now=T0 + timedelta(days=1))
        self.assertEqual(result["outcome"], "refused")
        self.assertIn("Coverage collapsed", result["diagnostics"][0])
        self.assertEqual(self.current_bytes(), before)
        partial_drop = aa_page([aa_row(name=name, creator="Anthropic" if name.startswith("Claude") else "OpenAI")
                                for name in names[:20]])
        result, _ = self.refresh(pages(**{AA_URL: partial_drop}), now=T0 + timedelta(days=1))
        self.assertEqual(result["outcome"], "promoted")
        self.assertEqual(result["diff"][sources.AA]["counts"]["removed"], 16)

    def test_collapse_is_judged_against_the_bundled_snapshot_without_a_cache(self):
        collapsed = aa_page([aa_row()])
        result, _ = self.refresh(pages(**{AA_URL: collapsed}))
        self.assertEqual(result["outcome"], "refused")
        self.assertFalse((self.root / "current.json").exists())

    def test_failed_optional_source_keeps_its_own_older_rows(self):
        self.refresh()
        later = T0 + timedelta(days=3)
        denied = sources.SourceError("access_denied", "HTTP 403 bot challenge")
        result, _ = self.refresh(pages(**{OPENAI_URL: denied}), now=later)
        self.assertEqual(result["outcome"], "promoted")
        block = observations.load(later)["snapshot"]["sources"][sources.OPENAI]
        self.assertEqual((block["status"], block["retrieved_at"], len(block["rows"])),
                         ("access_denied", "2026-10-01T12:00:00Z", 3))
        self.assertIn("keeping rows retrieved at 2026-10-01T12:00:00Z", block["diagnostics"][0])
        report = observations.status(later, "automatic")
        self.assertEqual(report["sources"][sources.AA]["age_s"], 0)
        self.assertEqual(report["sources"][sources.OPENAI]["age_s"], 3 * 86400)
        self.assertEqual(report["age_s"], 0)

    def test_failed_optional_source_without_cache_keeps_bundled_rows_or_none(self):
        broken = pages(**{ANTHROPIC_URL: "<html><body><p>Models moved</p></body></html>"})
        bundled = observations.bundled()["sources"][sources.ANTHROPIC]
        result, _ = self.refresh(broken, check=True)
        self.assertEqual(result["outcome"], "checked")
        self.assertEqual(result["sources"][sources.ANTHROPIC]["retrieved_at"], bundled["retrieved_at"])
        with patch.object(observations, "BUNDLED_PATH", self.base / "missing.json"):
            result, _ = self.refresh(broken)
        self.assertEqual(result["outcome"], "promoted")
        block = observations.load(T0)["snapshot"]["sources"][sources.ANTHROPIC]
        self.assertEqual((block["status"], block["rows"], block["retrieved_at"]), ("malformed", [], None))

    def test_optional_source_collapse_fails_only_that_source(self):
        self.refresh()
        tiny = ("<table><tr><td>Claude API ID</td><td>claude-opus-5-5</td></tr>"
                "<tr><td>Context window</td><td>1M tokens</td></tr></table>")
        with patch.object(observations, "COLLAPSE_FLOOR", 2):
            result, _ = self.refresh(pages(**{ANTHROPIC_URL: tiny}), now=T0 + timedelta(days=1))
        self.assertEqual(result["outcome"], "promoted")
        block = observations.load(T0)["snapshot"]["sources"][sources.ANTHROPIC]
        self.assertEqual((block["status"], len(block["rows"])), ("malformed", 4))

    def test_retry_after_stops_further_reads_until_it_expires(self):
        self.refresh()
        limited = sources.SourceError("unavailable", "HTTP 429", 3600)
        later = T0 + timedelta(days=1)
        result, _ = self.refresh(pages(**{AA_URL: limited}), now=later)
        self.assertEqual(result["sources"][sources.AA]["retry_after_until"], "2026-10-02T13:00:00Z")
        self.assertEqual(self.record()["sources"][sources.AA]["retry_after_until"], "2026-10-02T13:00:00Z")
        result, fetch = self.refresh(now=later + timedelta(minutes=30))
        self.assertEqual((result["outcome"], fetch.calls), ("failed", []))
        self.assertIn("retry after 2026-10-02T13:00:00Z", result["diagnostics"][0])
        self.assertFalse(observations.auto_refresh_due("automatic", later + timedelta(minutes=30)))
        result, fetch = self.refresh(now=later + timedelta(hours=2))
        self.assertEqual((result["outcome"], len(fetch.calls)), ("promoted", 3))

    def test_refresh_never_touches_preferences_bundle_or_workers(self):
        bundle = observations.BUNDLED_PATH.parent
        before_bundle = tree_digest(bundle)
        before_preferences = self.preferences.read_bytes()
        with patch("subprocess.Popen", side_effect=AssertionError("process started")), \
                patch("subprocess.run", side_effect=AssertionError("process started")):
            self.refresh()
            self.refresh(check=True, now=T0 + timedelta(days=1))
            observations.status(T0, "automatic")
        self.assertEqual(self.preferences.read_bytes(), before_preferences)
        self.assertEqual(sorted(path.name for path in self.config.iterdir()), ["config.yaml"],
                         "no preference lock or file was created")
        self.assertEqual(tree_digest(bundle), before_bundle)
        self.assertFalse((self.base / "state").exists())
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         [".lock", "current.json", "refresh.json"])

    def test_end_to_end_through_a_local_http_server(self):
        server = Server()
        self.addCleanup(server.close)
        paths = {AA_URL: "/aa", ANTHROPIC_URL: "/anthropic", OPENAI_URL: "/openai"}
        server.routes["/aa"] = (200, {"Content-Encoding": "gzip"},
                                gzip.compress(fixture("aa-leaderboard.html").encode()))
        server.routes["/anthropic"] = (302, {"Location": "/anthropic-final"}, b"")
        server.routes["/anthropic-final"] = (200, {}, fixture("anthropic-models-overview.html").encode())
        server.routes["/openai"] = (403, {"cf-mitigated": "challenge"}, fixture("challenge-403.html").encode())
        local = partial(sources.fetch, policy=server.policy())

        def fetch(url, *, deadline, cancel):
            return local(server.url(paths[url]), deadline=deadline, cancel=cancel)

        result = observations.refresh(now=T0, fetch=fetch)
        self.assertEqual(result["outcome"], "promoted")
        self.assertEqual({key: value["status"] for key, value in result["sources"].items()},
                         {sources.AA: "ok", sources.ANTHROPIC: "ok", sources.OPENAI: "access_denied"})
        self.assertIn("bot challenge", result["sources"][sources.OPENAI]["diagnostics"][0])


class AutomaticRefreshTests(ObservationCase):
    def test_setting_and_cache_age_decide_due(self):
        self.assertFalse(observations.auto_refresh_due("manual", T0))
        self.assertFalse(observations.auto_refresh_due(None, T0))
        self.assertFalse(observations.auto_refresh_due("always", T0))
        self.assertTrue(observations.auto_refresh_due("automatic", T0), "no local cache")
        self.refresh()
        self.assertFalse(observations.auto_refresh_due("automatic", T0 + timedelta(hours=23, minutes=59)))
        self.assertTrue(observations.auto_refresh_due("automatic", T0 + timedelta(hours=24)))
        self.assertTrue(observations.auto_refresh_due("automatic", T0 + timedelta(days=8)))
        self.assertFalse(observations.auto_refresh_due("manual", T0 + timedelta(days=8)))

    def test_fresh_stale_and_seven_day_display(self):
        self.refresh()
        fresh = observations.load(T0 + timedelta(hours=1))
        self.assertEqual((fresh["stale"], fresh["age_s"]), (False, 3600))
        week = observations.load(T0 + timedelta(days=7))
        self.assertTrue(week["stale"])
        self.assertFalse(observations.load(T0 + timedelta(days=6, hours=23))["stale"])

    def test_reopen_restraint_after_failed_and_running_attempts(self):
        self.refresh()
        later = T0 + timedelta(days=2)
        self.refresh(pages(**{AA_URL: sources.SourceError("unavailable", "Network error")}), now=later)
        self.assertFalse(observations.auto_refresh_due("automatic", later + timedelta(minutes=1)))
        self.assertFalse(observations.auto_refresh_due("automatic", later + timedelta(hours=5, minutes=59)))
        self.assertTrue(observations.auto_refresh_due("automatic", later + timedelta(hours=6)))
        record = self.record()
        record["attempt"] = {"started_at": "2026-10-04T12:00:00Z", "finished_at": None,
                             "outcome": "running", "check": False}
        (self.root / "refresh.json").write_text(json.dumps(record))
        self.assertFalse(observations.auto_refresh_due("automatic", T0 + timedelta(days=3, minutes=5)))

    def test_status_and_auto_checks_are_offline_and_read_only(self):
        self.refresh()
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
                patch.object(sources, "fetch", side_effect=AssertionError("network")), \
                patch.object(observations, "refresh", side_effect=AssertionError("refresh")):
            report = observations.status(T0 + timedelta(days=8), "automatic")
            observations.load(T0)
            observations.previous()
            observations.auto_refresh_due("automatic", T0)
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)
        self.assertEqual((report["origin"], report["generation"], report["stale"]), ("cache", "1", True))
        self.assertEqual(report["auto_refresh"], {"setting": "automatic", "due": True,
                                                  "reason": "observations are at least 24 hours old"})
        self.assertEqual(report["sources"][sources.AA]["rows"], 36)
        self.assertEqual(report["sources"][sources.AA]["mapped"], 33)
        self.assertEqual(report["last_attempt"]["outcome"], "promoted")

    def test_corrupt_refresh_record_is_ignored(self):
        self.refresh()
        (self.root / "refresh.json").write_text("[]")
        self.assertTrue(observations.auto_refresh_due("automatic", T0 + timedelta(days=2)))
        self.assertIsNone(observations.status(T0)["last_attempt"])


class SnapshotValidationTests(ObservationCase):
    def snapshot(self) -> dict:
        self.refresh()
        return json.loads((self.root / "current.json").read_text())

    def test_strict_shape_and_values(self):
        good = self.snapshot()
        observations.validate(good)
        row = good["sources"][sources.AA]["rows"][0]
        mutations = {
            "schema": lambda doc: doc.update(schema="pod-observations/v2"),
            "extra field": lambda doc: doc.update(extra=1),
            "generation": lambda doc: doc.update(generation=3),
            "timestamp": lambda doc: doc.update(created_at="2026-10-01 12:00"),
            "status": lambda doc: doc["sources"][sources.AA].update(status="great"),
            "http url": lambda doc: doc["sources"][sources.AA].update(url="http://artificialanalysis.ai/x"),
            "creator": lambda doc: doc["sources"][sources.AA]["rows"].append(dict(row, name="x", creator="Google")),
            "duplicate": lambda doc: doc["sources"][sources.AA]["rows"].append(dict(row)),
            "control": lambda doc: doc["sources"][sources.AA]["rows"].append(dict(row, name="a\x1b[2Jb")),
            "qualifier": lambda doc: doc["sources"][sources.AA]["rows"].append(dict(row, name="y", qualifiers=["fast"])),
            "zero string": lambda doc: doc["sources"][sources.AA]["rows"].append(
                dict(row, name="z", metrics=dict(row["metrics"], intelligence="0"))),
            "nan": lambda doc: doc["sources"][sources.AA]["rows"].append(
                dict(row, name="n", metrics=dict(row["metrics"], usd_per_task=float("nan")))),
            "bool": lambda doc: doc["sources"][sources.AA]["rows"].append(
                dict(row, name="b", metrics=dict(row["metrics"], output_tps=True))),
            "context float": lambda doc: doc["sources"][sources.AA]["rows"].append(
                dict(row, name="c", metrics=dict(row["metrics"], context_tokens=1.5))),
            "missing metric": lambda doc: doc["sources"][sources.AA]["rows"].append(
                dict(row, name="m", metrics={"intelligence": 1})),
            "rows without time": lambda doc: doc["sources"][sources.AA].update(retrieved_at=None),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                document = json.loads(json.dumps(good))
                mutate(document)
                with self.assertRaises(PodError):
                    observations.validate(document)

    def test_missing_metrics_stay_null_never_zero(self):
        rows = {row["name"]: row for row in self.snapshot()["sources"][sources.AA]["rows"]}
        self.assertIsNone(rows["Claude Sonnet 5.5 (low with fallback)"]["metrics"]["intelligence"])

    def test_cache_files_are_bounded_and_not_redirected(self):
        self.snapshot()
        (self.root / "current.json").write_bytes(b" " * (observations.MAX_SNAPSHOT + 1))
        self.assertEqual(observations.load(T0)["origin"], "bundled")
        target = self.base / "elsewhere.json"
        target.write_text("{}")
        (self.root / "previous.json").symlink_to(target)
        self.assertIsNone(observations.previous())


class CacheLocationTests(unittest.TestCase):
    def test_override_default_and_bundle_refusal(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            with patch.dict(os.environ, {"POD_CACHE_HOME": str(base / "c")}):
                self.assertEqual(observations.cache_root(), base / "c" / "models")
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(base / "xdg")}):
                os.environ.pop("POD_CACHE_HOME", None)
                self.assertEqual(observations.cache_root(project=base), (base / "xdg").resolve() / "pod" / "models")
            bundle = observations.BUNDLED_PATH.parent
            with patch.dict(os.environ, {"POD_CACHE_HOME": str(bundle)}), self.assertRaises(PodError):
                observations.cache_root()
            with patch.dict(os.environ, {"POD_CACHE_HOME": "relative/path"}), self.assertRaises(PodError):
                observations.cache_root()

    def test_redirected_cache_directory_is_refused_for_writes(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            (base / "real").mkdir()
            (base / "c").mkdir()
            (base / "c" / "models").symlink_to(base / "real")
            with patch.dict(os.environ, {"POD_CACHE_HOME": str(base / "c")}), self.assertRaises(PodError):
                observations.refresh(now=T0, fetch=FakeFetch(pages()))
            self.assertEqual(list((base / "real").iterdir()), [])


class BundledSnapshotTests(unittest.TestCase):
    def test_bundled_snapshot_is_small_valid_and_attributed(self):
        raw = observations.BUNDLED_PATH.read_bytes()
        self.assertLess(len(raw), 64 * 1024)
        snapshot = observations.validate(json.loads(raw))
        self.assertEqual(set(snapshot["sources"]), {source.id for source in sources.SOURCES})
        aa = snapshot["sources"][sources.AA]
        self.assertEqual((aa["status"], aa["required"], aa["url"]),
                         ("ok", True, "https://artificialanalysis.ai/leaderboards/models"))
        self.assertTrue(aa["methodology"].startswith("Artificial Analysis Intelligence Index v"))
        self.assertTrue(all(row["creator"] in ("Anthropic", "OpenAI") for block in snapshot["sources"].values()
                            for row in block["rows"]))
        fallback = [row for row in aa["rows"] if row["name"].endswith("with fallback)")]
        self.assertTrue(fallback and all("with fallback" in row["qualifiers"] for row in fallback))


if __name__ == "__main__":
    unittest.main()
