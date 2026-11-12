"""Fetch and read the public leaderboard: one bounded request, no retries, no credentials.

Identity and the two measurements Pod compares are required. Every other column Pod knows is
its own named metric with its unit; one that is absent reads as unavailable, never as zero. A
column Pod does not know is reported and not shown. A renamed column is therefore a new,
unknown column: no metric is ever read from a header it was not defined for.
"""

from html.parser import HTMLParser
import http.client
import math
import re
import unicodedata
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

URL = "https://artificialanalysis.ai/leaderboards/models"
ATTRIBUTION = "Artificial Analysis"
USER_AGENT = "Pod public-model-observations (+https://github.com/j3w1/pod)"
MAX_BYTES = 8 * 1024 * 1024
TIMEOUT_S = 20.0          # per read
DEADLINE_S = 60.0         # for the whole answer
MAX_ROWS = 2000
MAX_CELLS = 40
MAX_CELL_TEXT = 512

# Normalized header -> (metric name, unit). The first four are required.
REQUIRED = {"model": ("name", None), "creator": ("creator", None),
            "artificialanalysisintelligenceindex": ("intelligence_index", "index points"),
            "costpertaskusd": ("cost_per_task_usd", "US dollars per benchmark task")}
OPTIONAL = {"contextwindow": ("context_window_tokens", "tokens"),
            "timepertaskminutes": ("time_per_task_min", "minutes per benchmark task"),
            "firstanswertoken(s)": ("first_answer_token_s", "seconds")}
IGNORED = ("furtheranalysis",)   # a link column, not a measurement
METRICS = tuple(name for name, unit in [*REQUIRED.values(), *OPTIONAL.values()] if unit)
UNITS = {name: unit for name, unit in [*REQUIRED.values(), *OPTIONAL.values()] if unit}
_MISSING = ("", "--", "—", "–", "-", "n/a")
_NUMBER = re.compile(r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,9})(?:\.[0-9]{1,6})?")
_METHOD = re.compile(r"Intelligence Index v([0-9]{1,2}(?:\.[0-9]{1,3}){0,3})\b")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


class RefreshError(Exception):
    """The refresh failed as a whole. The message is safe to store and show."""


def clean(value: str, limit: int = MAX_CELL_TEXT) -> str:
    """Bounded display text with terminal sequences and control characters removed."""
    kept = []
    for char in _ANSI.sub("", value[:limit * 4]):
        category = unicodedata.category(char)
        if char.isspace() or category in ("Zs", "Zl", "Zp"):
            kept.append(" ")
        elif category not in ("Cc", "Cf", "Cs", "Co", "Cn"):
            kept.append(char)
    return " ".join("".join(kept).split())[:limit]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is never followed: following one would be a second request."""

    def http_error_302(self, req, fp, code, msg, headers):
        fp.close()
        target = clean(headers.get("Location") or "", 200) or "an unnamed location"
        raise RefreshError(f"The source redirected (HTTP {code}) to {target}; it was not followed")

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def fetch(url: str = URL, *, max_bytes: int = MAX_BYTES, timeout: float = TIMEOUT_S) -> str:
    """One GET. HTTPS only, except a loopback address; never retried, and no redirect is followed."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname == "127.0.0.1"):
        raise RefreshError("The source must be fetched over HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html",
                                                   "Accept-Encoding": "gzip"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        deadline = time.monotonic() + DEADLINE_S
        with opener.open(request, timeout=timeout) as response:
            chunks, size = [], 0
            while size <= max_bytes:              # bounded in bytes and in total time, however slowly the source sends
                if time.monotonic() > deadline:
                    raise RefreshError(f"The source did not finish answering within {int(DEADLINE_S)} seconds")
                chunk = response.read(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b"".join(chunks)
            encoding = (response.headers.get("Content-Encoding") or "").lower()
    except RefreshError:
        raise
    except urllib.error.HTTPError as exc:
        exc.close()
        raise RefreshError(f"The source answered HTTP {exc.code}") from None
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise RefreshError(f"The source could not be reached ({type(exc).__name__})") from None
    if len(raw) > max_bytes:
        raise RefreshError("The source page is larger than the fetch limit")
    try:
        if encoding == "gzip":
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            raw = inflater.decompress(raw, max_bytes + 1)
            if len(raw) > max_bytes or inflater.unconsumed_tail:
                raise RefreshError("The source page is larger than the fetch limit")
        elif encoding not in ("", "identity"):
            raise RefreshError("The source used an unsupported content encoding")
        return raw.decode("utf-8")
    except (zlib.error, UnicodeError):
        raise RefreshError("The source page could not be decoded") from None


class _Tables(HTMLParser):
    """Tables as rows of cleaned cell text; script and style text is ignored."""
    SKIP = ("script", "style", "svg", "noscript", "template")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._open, self._row, self._cell, self._skip, self._size = [], [], None, None, 0, 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "table":
            if len(self._open) >= 4 or len(self.tables) + len(self._open) >= 16:
                raise RefreshError("The page has too many tables")
            self._open.append([])
        elif self._open and tag == "tr":
            self._row = []
        elif self._open and self._row is not None and tag in ("td", "th"):
            self._cell, self._size = [], 0
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif not self._open:
            return
        elif tag in ("td", "th") and self._cell is not None:
            if len(self._row) >= MAX_CELLS:
                raise RefreshError("A table row has too many cells")
            self._row.append(clean("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if len(self._open[-1]) > MAX_ROWS:
                raise RefreshError("The leaderboard has too many rows")
            self._open[-1].append(self._row)
            self._row = self._cell = None
        elif tag == "table":
            self.tables.append(self._open.pop())
            self._row = self._cell = None

    def handle_data(self, data):
        if self._cell is not None and not self._skip and self._size < MAX_CELL_TEXT * 4:
            self._cell.append(data)
            self._size += len(data)


def _key(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _number(text: str, *, prefix: str = "", high: float = 100000):
    """A finite decimal with its exact unit prefix. None for an explicit missing mark."""
    body = text.strip()
    if body.lower() in _MISSING:
        return None
    if prefix:
        if not body.startswith(prefix):
            raise ValueError("wrong unit")
        body = body[len(prefix):]
    if not _NUMBER.fullmatch(body):
        raise ValueError("not a number")
    value = float(body.replace(",", ""))
    if not math.isfinite(value) or not 0 <= value <= high:
        raise ValueError("outside its plausible range")
    return int(value) if "." not in body else value


def _tokens(text: str):
    """Context sizes such as 1M, 1.05M or 872k, as whole tokens."""
    body = text.strip()
    if body.lower() in _MISSING:
        return None
    match = re.fullmatch(r"([0-9]{1,4}(?:\.[0-9]{1,3})?)\s?([kKmM])(?: tokens)?", body)
    if not match:
        raise ValueError("unsupported unit")
    return round(float(match.group(1)) * (1000 if match.group(2) in "kK" else 1_000_000))


_READERS = {"context_window_tokens": _tokens, "time_per_task_min": _number, "first_answer_token_s": _number}


def _methodology(text: str) -> str | None:
    """The index version the page states, when every mention agrees with the most specific one."""
    versions = sorted(set(_METHOD.findall(text)), key=lambda value: len(value.split(".")))
    if not versions:
        return None
    best = versions[-1]
    if any(best != value and not best.startswith(value + ".") for value in versions):
        return None
    return "Artificial Analysis Intelligence Index v" + best


def parse(text: str) -> dict:
    """Rows, the columns found and diagnostics. Raises RefreshError; never returns a partial result."""
    parser = _Tables()
    try:
        parser.feed(text)
        parser.close()
    except RefreshError:
        raise
    except Exception as exc:    # html.parser raises assorted errors on hostile markup
        raise RefreshError(f"The page could not be parsed ({type(exc).__name__})") from None
    for table in parser.tables:
        header = next((index for index, row in enumerate(table[:4])
                       if {"model", "creator"} <= {_key(cell) for cell in row}), None)
        if header is not None:
            break
    else:
        raise RefreshError("Leaderboard table with Model and Creator headers is missing")
    keys = [_key(cell) for cell in table[header]]
    known = REQUIRED | OPTIONAL
    columns: dict = {}
    for index, key in enumerate(keys):
        if key in known:
            if known[key][0] in columns:
                raise RefreshError(f"Leaderboard has the column {table[header][index]!r} twice")
            columns[known[key][0]] = index
    missing = sorted(name for name, _unit in REQUIRED.values() if name not in columns)
    if missing:
        raise RefreshError("Leaderboard lacks required columns: " + ", ".join(missing))
    unknown = [table[header][index] for index, key in enumerate(keys)
               if key and key not in known and key not in IGNORED]
    absent = sorted(name for name, _unit in OPTIONAL.values() if name not in columns)
    body = table[header + 1:]
    if not body:
        raise RefreshError("Leaderboard has no rows")
    rows, unread = [], 0
    for number, cells in enumerate(body, 1):
        if len(cells) != len(keys):
            raise RefreshError(f"Leaderboard row {number} has the wrong number of cells")
        name, creator = cells[columns["name"]], cells[columns["creator"]]
        if not name or not creator or len(name) > 120:
            raise RefreshError(f"Leaderboard row {number} has no usable model or creator name")
        score = cells[columns["intelligence_index"]]
        estimated = score.endswith("*")
        try:
            row = {"name": name, "creator": creator, "index_estimated": estimated,
                   "intelligence_index": _number(score.rstrip("*"), high=100),
                   "cost_per_task_usd": _number(cells[columns["cost_per_task_usd"]], prefix="$")}
        except ValueError as exc:
            raise RefreshError(f"Leaderboard row {number} ({name}) has an unreadable index or cost: {exc}") from None
        for metric, reader in _READERS.items():
            row[metric] = None
            if metric in columns:
                try:
                    row[metric] = reader(cells[columns[metric]])
                except ValueError:
                    unread += 1     # shown as unavailable and counted, never guessed
        rows.append(row)
    methodology = _methodology(text)
    diagnostics = []
    if unknown:
        diagnostics.append("Columns not known to Pod, not shown: " + ", ".join(unknown))
    if absent:
        diagnostics.append("Known columns absent from the page, shown as unavailable: " + ", ".join(absent))
    if unread:
        diagnostics.append(f"{unread} optional values could not be read and are shown as unavailable")
    if methodology is None:
        diagnostics.append("The page does not state one Intelligence Index version")
    return {"rows": rows, "methodology": methodology, "unknown_columns": unknown, "absent_metrics": absent,
            "diagnostics": diagnostics}
