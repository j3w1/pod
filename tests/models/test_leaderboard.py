import http.server
import threading
import unittest

from pod.models import leaderboard
from tests.models.support import HEADERS, ROWS, live_page, page, without


class ParseTests(unittest.TestCase):
    def test_the_current_public_table_parses_with_its_methodology(self):
        parsed = leaderboard.parse(live_page())
        self.assertEqual(len(parsed["rows"]), 284)
        self.assertEqual(parsed["methodology"], "Artificial Analysis Intelligence Index v5.0.1")
        self.assertEqual(parsed["unknown_columns"], [])
        self.assertEqual(parsed["absent_metrics"], [])
        first = parsed["rows"][0]
        self.assertEqual(first, {"name": "Claude Fable 5.2 (max)", "creator": "Anthropic", "index_estimated": False,
                                 "intelligence_index": 65, "cost_per_task_usd": 26.54,
                                 "context_window_tokens": 1_000_000, "time_per_task_min": 47.8,
                                 "first_answer_token_s": 245.58})

    def test_a_missing_required_column_fails_the_whole_refresh(self):
        for column, metric in (("Cost per Task USD", "cost_per_task_usd"), ("Creator", "creator"),
                               ("Artificial Analysis Intelligence Index", "intelligence_index")):
            with self.subTest(column=column):
                with self.assertRaises(leaderboard.RefreshError) as caught:
                    leaderboard.parse(page(*without(column)))
                if column != "Creator":
                    self.assertIn(metric, str(caught.exception))

    def test_an_absent_optional_column_reads_as_unavailable_never_zero(self):
        parsed = leaderboard.parse(page(*without("Time per Task Minutes")))
        self.assertEqual(parsed["absent_metrics"], ["time_per_task_min"])
        self.assertTrue(all(row["time_per_task_min"] is None for row in parsed["rows"]))
        self.assertEqual(parsed["rows"][0]["first_answer_token_s"], 245.58)

    def test_a_missing_value_is_unavailable_not_zero(self):
        rows = [["Muse Wonder Full (max)", "1M", "Meta", "50", "--", "--", "--", "x"]]
        row = leaderboard.parse(page(rows=rows))["rows"][0]
        self.assertIsNone(row["cost_per_task_usd"])
        self.assertIsNone(row["time_per_task_min"])
        self.assertEqual(row["intelligence_index"], 50)

    def test_an_unknown_column_is_reported_and_is_not_a_metric(self):
        headers = HEADERS[:5] + ["Median Tokens/s"] + HEADERS[5:]
        rows = [row[:5] + ["123"] + row[5:] for row in ROWS]
        parsed = leaderboard.parse(page(headers, rows))
        self.assertEqual(parsed["unknown_columns"], ["Median Tokens/s"])
        self.assertIn("Median Tokens/s", parsed["diagnostics"][0])
        self.assertEqual(set(parsed["rows"][0]), {"name", "creator", "index_estimated", *leaderboard.METRICS})

    def test_a_renamed_column_is_never_read_under_the_earlier_metric(self):
        headers = [cell.replace("Time per Task Minutes", "Time per Task Seconds") for cell in HEADERS]
        parsed = leaderboard.parse(page(headers))
        self.assertEqual(parsed["unknown_columns"], ["Time per Task Seconds"])
        self.assertEqual(parsed["absent_metrics"], ["time_per_task_min"])
        self.assertIsNone(parsed["rows"][0]["time_per_task_min"])

    def test_an_estimated_index_is_marked(self):
        rows = [["Claude Fable 5.2 (max)", "1M", "Anthropic", "65*", "$26.54", "47.8", "245.58", "x"]]
        row = leaderboard.parse(page(rows=rows))["rows"][0]
        self.assertTrue(row["index_estimated"])
        self.assertEqual(row["intelligence_index"], 65)

    def test_a_malformed_page_never_reads_as_a_partial_success(self):
        bad_cost = [ROWS[0], ["GPT-6.1 Sol (high)", "1M", "OpenAI", "50", "0.59 USD", "7.2", "34.91", "x"]]
        short = [ROWS[0], ROWS[1][:5]]
        for name, text in (("unreadable cost", page(rows=bad_cost)), ("short row", page(rows=short)),
                           ("no rows", page(rows=[])), ("no table", "<html><p>maintenance</p></html>"),
                           ("duplicate column", page(HEADERS + ["Creator"], [row + ["x"] for row in ROWS]))):
            with self.subTest(name), self.assertRaises(leaderboard.RefreshError):
                leaderboard.parse(text)

    def test_an_unreadable_optional_value_is_unavailable_and_counted(self):
        rows = [["Claude Fable 5.2 (max)", "huge", "Anthropic", "65", "$26.54", "fast", "245.58", "x"]]
        parsed = leaderboard.parse(page(rows=rows))
        self.assertIsNone(parsed["rows"][0]["context_window_tokens"])
        self.assertIsNone(parsed["rows"][0]["time_per_task_min"])
        self.assertTrue(any(line.startswith("2 optional values") for line in parsed["diagnostics"]))

    def test_methodology_is_stated_only_when_the_page_agrees_with_itself(self):
        self.assertIsNone(leaderboard.parse(page(text="Intelligence Index v5.0.1 and Intelligence Index v4.2"))
                          ["methodology"])
        self.assertIsNone(leaderboard.parse(page(text="no version here"))["methodology"])
        self.assertEqual(leaderboard.parse(page(text="Intelligence Index v5 … Intelligence Index v5.0.1"))
                         ["methodology"], "Artificial Analysis Intelligence Index v5.0.1")

    def test_terminal_control_sequences_are_removed_from_names(self):
        rows = [["Evil\x1b[31m Model\x07 (high)", "1M", "Anthropic", "1", "$1.00", "1.0", "1.0", "x"]]
        self.assertEqual(leaderboard.parse(page(rows=rows))["rows"][0]["name"], "Evil Model (high)")


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests += 1
        if self.path == "/away":
            self.send_response(302)
            self.send_header("Location", "http://localhost.invalid/")
            self.end_headers()
            return
        body = b"x" * 5000 if self.path == "/big" else b"<html>ok</html>"
        self.send_response(200 if self.path != "/missing" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.requests = 0
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_one_request_and_a_bounded_body(self):
        before = self.server.requests
        self.assertEqual(leaderboard.fetch(self.base + "/"), "<html>ok</html>")
        self.assertEqual(self.server.requests, before + 1)
        with self.assertRaisesRegex(leaderboard.RefreshError, "larger than the fetch limit"):
            leaderboard.fetch(self.base + "/big", max_bytes=1000)

    def test_a_failure_is_not_retried(self):
        before = self.server.requests
        with self.assertRaisesRegex(leaderboard.RefreshError, "HTTP 404"):
            leaderboard.fetch(self.base + "/missing")
        self.assertEqual(self.server.requests, before + 1)

    def test_a_redirect_to_another_site_and_plain_http_are_refused(self):
        with self.assertRaisesRegex(leaderboard.RefreshError, "another site"):
            leaderboard.fetch(self.base + "/away")
        with self.assertRaisesRegex(leaderboard.RefreshError, "HTTPS"):
            leaderboard.fetch("http://example.invalid/leaderboard")

    def test_the_request_carries_no_credentials(self):
        self.assertTrue(leaderboard.URL.startswith("https://"))
        self.assertNotIn("@", leaderboard.URL)


if __name__ == "__main__":
    unittest.main()
