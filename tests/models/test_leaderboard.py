import gzip
import http.server
import threading
import unittest
from unittest.mock import patch

from pod.models import leaderboard
from tests.models.support import FIXTURES, HEADERS, ROWS, current_page, live_page, page, without


class ParseTests(unittest.TestCase):
    def test_current_grouped_table_keeps_creator_and_explicit_effort(self):
        parsed = leaderboard.parse(current_page())
        self.assertEqual(len(parsed["rows"]), 197)
        self.assertEqual(parsed["methodology"], "Artificial Analysis Intelligence Index v5.0.1")
        self.assertEqual((parsed["unknown_columns"], parsed["absent_metrics"]), ([], []))
        astra = parsed["rows"][1]
        self.assertEqual(astra, {"name": "GPT-6.2 Astra (max)", "creator": "OpenAI",
                                 "index_estimated": False, "intelligence_index": 62,
                                 "cost_per_task_usd": 4.0, "time_per_task_min": 10.7,
                                 "first_answer_token_s": 147.65, "context_window_tokens": 1000000})
        self.assertEqual(parsed["rows"][4]["name"], "GPT-6.2 Sol (max)")
        self.assertEqual(parsed["rows"][0]["name"], "Claude Fable 5.2 (max with fallback)")
        self.assertIn("Qwen4 Max", [row["name"] for row in parsed["rows"]])
        self.assertNotIn("Qwen4 (max)", [row["name"] for row in parsed["rows"]])

    def test_current_table_required_identity_and_values_are_validated(self):
        source = current_page()
        for old, new in (('<img alt="OpenAI">', '<img alt="">'),
                         ("$4.00", "4.00 USD"), (">62<", ">NaN<"),
                         (">62<", ">101<"), (">62<", ">62**<"), ("Cost per Task", "Price per Task")):
            with self.subTest(old=old, new=new), self.assertRaises(leaderboard.RefreshError):
                leaderboard.parse(source.replace(old, new, 1))

    def test_current_optional_values_are_unavailable_without_guessing(self):
        parsed = leaderboard.parse(current_page().replace(">10.7<", ">fast<", 1))
        self.assertIsNone(parsed["rows"][1]["time_per_task_min"])
        self.assertTrue(any("1 optional values" in note for note in parsed["diagnostics"]))
        missing = next(row for row in parsed["rows"] if row["name"] == "Muse Wonder Full (max)")
        self.assertIsNone(missing["cost_per_task_usd"])

    def test_current_explicit_suffix_is_required_to_be_unambiguous(self):
        marker = '<span class="font-normal text-neutral-500">Max</span>'
        for replacement in (marker + marker, '<span class="font-normal text-neutral-500"></span>',
                            marker + "extra"):
            with self.subTest(replacement=replacement), self.assertRaises(leaderboard.RefreshError):
                leaderboard.parse(current_page().replace(marker, replacement, 1))
        with self.assertRaises(leaderboard.RefreshError):
            leaderboard.parse(current_page().replace('<img alt="OpenAI">',
                                                     '<img alt="OpenAI"><img alt="Other">', 1))

    def test_current_ungrouped_suffix_is_explicit_and_not_part_of_the_base_name(self):
        rows = leaderboard.parse(current_page())["rows"]
        muse = next(row for row in rows if row["name"] == "Muse Wonder Full (max)")
        self.assertEqual((muse["creator"], muse["intelligence_index"]), ("Meta", 50))


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


class PublishedProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (FIXTURES / "leaderboard-profiles.html").read_text(encoding="utf-8")

    def test_ecmascript_padding_at_script_and_argument_boundaries(self):
        # Includes a BOM and representatives of the Unicode Space_Separator category.
        padding = "\ufeff\u00a0\u200a\u3000\t\v\f"
        padded = self.source.replace("<script>", "<script>" + padding)
        padded = padded.replace(".push([", ".push(" + padding + "[")
        padded = padded.replace("])</script>", "]" + padding + ")" + padding + ";" + padding + "</script>")
        self.assertEqual(leaderboard.parse(padded), leaderboard.parse(self.source))
        for old, new in (("<script>", "<script>\x85"), (".push([", ".push(\x85["),
                         ("])</script>", "]\x85)</script>"), (")</script>", ");\x85</script>")):
            with self.subTest(new=new), self.assertRaises(leaderboard.RefreshError):
                leaderboard.parse(self.source.replace(old, new, 1))

    def test_independently_framed_pushes_share_a_script(self):
        controls = "self.__next_f.push([0]);self.__next_f.push([2,null])"
        self.assertEqual(leaderboard.parse(page() + f"<script>{controls}</script>"), leaderboard.parse(page()))
        combined = self.source.replace("</script>\n<script>", ";").replace("<script>", f"<script>{controls};", 1)
        self.assertEqual(leaderboard.parse(combined), leaderboard.parse(self.source))
        with self.assertRaises(leaderboard.RefreshError):
            leaderboard.parse(combined.replace("21:[", "21:[[", 1))
        with self.assertRaises(leaderboard.RefreshError):
            leaderboard.parse(self.source.replace(")</script>", ")broken</script>", 1))

    def test_bootstrap_and_data_pushes_can_share_a_script(self):
        bootstrap = "(self.__next_f=self.__next_f||[]).push([0])"
        expected = leaderboard.parse(self.source)
        for separator in (";", "\n", "\r", "\r\n", "\u2028", "\u2029"):
            combined = self.source.replace("</script>\n<script>", separator)
            combined = combined.replace("<script>", "<script>" + bootstrap + separator, 1)
            with self.subTest(separator=separator):
                self.assertEqual(leaderboard.parse(combined), expected)

    def test_release_membership_uses_the_original_name_and_display_cleans_its_spacing(self):
        spaced = self.source.replace("GPT-6.2 Astra", "GPT-6.2  Astra")
        self.assertEqual(leaderboard.parse(spaced), leaderboard.parse(self.source))
        different_member = self.source.replace("GPT-6.2 Astra (Max)", "GPT-6.2  Astra (Max)", 1)
        with self.assertRaises(leaderboard.RefreshError):
            leaderboard.parse(different_member)

    def test_named_release_members_must_match_profiles_and_multiplicity(self):
        member = r'GPT-6.2 Astra (Max)\"'
        for replacement in (r'GPT-6.2 Astra (Medium)\"', member + r',\"' + member):
            with self.subTest(replacement=replacement), self.assertRaises(leaderboard.RefreshError):
                leaderboard.parse(self.source.replace(member, replacement, 1))
        with self.assertRaises(leaderboard.RefreshError):
            leaderboard.parse(self.source.replace(r'\"modelNames\":[', r'\"modelNames\":null,\"other\":[', 1))

    def test_expandable_control_does_not_impose_a_minimum_profile_count(self):
        source = self.source.replace(r',\"Claude Fable 5.2 (Low, Default Fallback)\"', "", 1)
        source = "\n".join(line for line in source.splitlines() if "Claude Fable 5.2 (Low, Default Fallback)" not in line)
        rows = leaderboard.parse(source)["rows"]
        self.assertEqual([row["profile"] for row in rows if row["release"] == "claude-fable-5-2"],
                         ["Max, Default Fallback"])

    def test_unrelated_release_list_does_not_change_a_standalone_table(self):
        unrelated = r'<script>self.__next_f.push([1,"8:[\"$\",\"nav\",null,{\"releases\":[]}]\n"])</script>'
        self.assertEqual(leaderboard.parse(page() + unrelated), leaderboard.parse(page()))

    def test_measures_each_published_profile_with_its_own_identity_precision_and_units(self):
        parsed = leaderboard.parse(self.source)
        rows = {row["name"]: row for row in parsed["rows"]}
        self.assertEqual(set(rows), {"GPT-6.2 Astra (max)", "GPT-6.2 Astra (xhigh)", "Qwen4 Max",
                                     "Claude Fable 5.2 (max with fallback)", "Claude Fable 5.2 (low with fallback)",
                                     "JT-35B-Flash"})
        row = rows["GPT-6.2 Astra (xhigh)"]
        self.assertEqual((row["model_name"], row["release"], row["profile"], row["creator"]),
                         ("GPT-6.2 Astra", "gpt-6-2-astra", "Xhigh", "OpenAI"))
        self.assertEqual(row["intelligence_index"], 61.5602325371388)
        self.assertEqual(row["cost_per_task_usd"], 2.5211433764120375)
        self.assertEqual(row["time_per_task_min"], 391.2271374821837 / 60)
        self.assertEqual(row["context_window_tokens"], 1_000_000)
        self.assertEqual(row["first_answer_token_s"], 53.87065945964923)
        self.assertEqual(row["first_token_s"], 53.87065945964923)
        self.assertEqual(row["output_tokens_per_s"], 56.840650197440034)
        self.assertEqual(row["omniscience_net_score"], 48.266666666666666)
        self.assertEqual([row[k + "_price_per_million_usd"] for k in ("input", "output", "cache_hit", "cache_write")],
                         [8, 40, 0.4, 10])
        self.assertEqual(leaderboard.UNITS["time_per_task_min"], "minutes per benchmark task")
        self.assertEqual(leaderboard.UNITS["output_tokens_per_s"], "tokens per second")
        self.assertEqual(rows["Claude Fable 5.2 (low with fallback)"]["profile"], "Low, Default Fallback")
        self.assertEqual(rows["Claude Fable 5.2 (low with fallback)"]["intelligence_index"], 53.9249388799182)
        self.assertIsNone(rows["Qwen4 Max"]["profile"])
        missing = rows["JT-35B-Flash"]
        self.assertEqual(missing["creator"], "China Mobile")
        self.assertEqual(missing["benchmark_scores"], {"critpt": 0, "gpqa-diamond": 0.829292929292929})
        for field in ("intelligence_index", "cost_per_task_usd", "time_per_task_min", "output_tokens_per_s"):
            self.assertIsNone(missing[field])
        self.assertEqual(parsed["methodology"], "Artificial Analysis Intelligence Index v5.0.1")

    def test_estimate_marks_belong_only_to_the_matching_profile(self):
        parsed = leaderboard.parse(self.source.replace(">65<", ">65*<"))
        self.assertEqual([row["name"] for row in parsed["rows"] if row["index_estimated"]],
                         ["Claude Fable 5.2 (max with fallback)"])

    def test_incomplete_or_malformed_profiles_fail_even_with_a_readable_visible_table(self):
        for old, new in ((r'\"intelligenceIndex\":61.5602325371388', r'\"changedIndex\":61.5602325371388'),
                         ("61.5602325371388", "true"), ("2.5211433764120375", "1e999"),
                         (r'\"name\":\"GPT-6.2 Astra (Xhigh)\"', r'\"name\":\"Another model\"'),
                         (r'\"suffix\":\"Xhigh\"', r'\"suffix\":4'), ("21:[", "21:[[["),
                         ("self.__next_f.push([1,", "self.__next_f.push([1,invalid+"),
                         ("initialModels", "renamedModels"), ("JT-35B-Flash</td>", "Different</td>"),
                         ('Cost per Task USD', 'Unrecognized price')):
            with self.subTest(old=old):
                self.assertIn(old, self.source)
                with self.assertRaises(leaderboard.RefreshError):
                    leaderboard.parse(self.source.replace(old, new, 1))
        with self.assertRaises(leaderboard.RefreshError):
            leaderboard.parse(self.source[:self.source.rindex("<script>")])

    def test_optional_invalid_values_and_unrelated_script_fields(self):
        source = self.source.replace("56.840650197440034", "false")
        source = source.replace(r'\"scores\":{', r'\"privateField\":\"do-not-copy\",\"scores\":{')
        parsed = leaderboard.parse(source)
        row = next(row for row in parsed["rows"] if row["name"] == "GPT-6.2 Astra (xhigh)")
        self.assertIsNone(row["output_tokens_per_s"])
        self.assertTrue(any("optional" in note for note in parsed["diagnostics"]))
        self.assertNotIn("do-not-copy", str(parsed))

    def test_ambiguous_and_incomplete_measurements_fail_the_whole_refresh(self):
        for replacement in (r'\"intelligenceIndexCostPerTask\":null',
                            r'\"intelligenceIndexCostPerTask\":0,\"intelligenceIndexCostPerTask\":8'):
            with self.subTest(replacement=replacement), self.assertRaises(leaderboard.RefreshError):
                leaderboard.parse(self.source.replace(r'\"intelligenceIndexCostPerTask\":3.9979061566801852',
                                                      replacement))
        with patch.object(leaderboard, "MAX_ROWS", 5), self.assertRaises(leaderboard.RefreshError):
            leaderboard.parse(self.source)

    def test_a_page_cut_off_in_its_first_data_script_cannot_replace_all_profiles_with_visible_rows(self):
        incomplete = self.source[:self.source.index("</script>")]
        with self.assertRaises(leaderboard.RefreshError):
            leaderboard.parse(incomplete)

    def test_explicitly_expandable_models_require_the_grouped_data(self):
        table = self.source.split("</table>", 1)[0] + "</table>"
        for tail in ("", '<script>self.__next_f.push([1,"0:T5,title"])</script>'):
            with self.subTest(tail=tail), self.assertRaises(leaderboard.RefreshError):
                leaderboard.parse(table + tail)

    def test_a_missing_published_astra_xhigh_member_fails_refresh(self):
        incomplete = "\n".join(line for line in self.source.splitlines() if "GPT-6.2 Astra (Xhigh)" not in line)
        with self.assertRaises(leaderboard.RefreshError):
            leaderboard.parse(incomplete)

    def test_unrelated_flight_records_on_a_legacy_table_leave_its_rows_readable(self):
        unrelated = r'<script>self.__next_f.push([1,"1:Tf,\"initialModels\"\n2:[\"$\",\"div\",null,{}]\n"])</script>'
        other_table = '<table><tr><td><button aria-expanded="false">Menu</button></td></tr></table>'
        for table_html in (page(), current_page()):
            with self.subTest(size=len(table_html)):
                self.assertEqual(leaderboard.parse(other_table + table_html + unrelated), leaderboard.parse(table_html))


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests += 1
        if self.path == "/incomplete-gzip":
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(gzip.compress(page().encode())[:-8])
            return
        if self.path == "/away":
            self.send_response(302)
            self.send_header("Location", "http://localhost.invalid/")
            self.end_headers()
            return
        if self.path in ("/loop", "/moved"):
            self.send_response(301 if self.path == "/moved" else 302)
            self.send_header("Location", "/loop" if self.path == "/loop" else "/")
            self.end_headers()
            return
        if self.path in ("/cut", "/garbage"):
            self.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n40\r\n<html>"
                             if self.path == "/cut" else b"\x00\x01 not HTTP\r\n\r\n")
            self.close_connection = True
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

    def test_a_redirect_is_one_request_and_a_failure_that_says_where_it_pointed(self):
        for path, code, target in (("/away", 302, "http://localhost.invalid/"), ("/loop", 302, "/loop"),
                                   ("/moved", 301, "/")):
            with self.subTest(path=path):
                before = self.server.requests
                with self.assertRaises(leaderboard.RefreshError) as caught:
                    leaderboard.fetch(self.base + path)
                self.assertEqual(str(caught.exception),
                                 f"The source redirected (HTTP {code}) to {target}; it was not followed")
                self.assertEqual(self.server.requests, before + 1)

    def test_a_transfer_that_breaks_off_is_a_failed_refresh_not_an_exception(self):
        for path, error in (("/cut", "IncompleteRead"), ("/garbage", "BadStatusLine")):
            with self.subTest(path=path):
                before = self.server.requests
                with self.assertRaises(leaderboard.RefreshError) as caught:
                    leaderboard.fetch(self.base + path)
                self.assertEqual(str(caught.exception), f"The source could not be reached ({error})")
                self.assertEqual(self.server.requests, before + 1)

    def test_plain_http_is_refused(self):
        with self.assertRaisesRegex(leaderboard.RefreshError, "HTTPS"):
            leaderboard.fetch("http://example.invalid/leaderboard")

    def test_the_request_carries_no_credentials(self):
        self.assertTrue(leaderboard.URL.startswith("https://"))
        self.assertNotIn("@", leaderboard.URL)

    def test_a_truncated_compressed_page_fails_even_if_its_table_is_already_readable(self):
        with self.assertRaises(leaderboard.RefreshError):
            leaderboard.fetch(self.base + "/incomplete-gzip")


if __name__ == "__main__":
    unittest.main()
