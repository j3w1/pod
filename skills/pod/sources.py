"""Fixed public observation sources: bounded HTTPS reads and isolated page parsers.

Fetched pages are data only. Nothing here executes page scripts, follows links found in
a page, calls an API, or uses credentials; an HTTP redirect is followed only within the
fixed host allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import http.client
import math
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib

AA = "artificial_analysis"
ANTHROPIC = "anthropic_models"
OPENAI = "openai_models"
CREATORS = ("Anthropic", "OpenAI")
STATUSES = ("ok", "access_denied", "unavailable", "malformed", "not_attempted")
QUALIFIERS = ("with fallback", "estimated index")


@dataclass(frozen=True)
class Source:
    id: str
    url: str
    attribution: str
    required: bool


SOURCES = (
    Source(AA, "https://artificialanalysis.ai/leaderboards/models", "Artificial Analysis", True),
    Source(ANTHROPIC, "https://platform.claude.com/docs/en/models/overview", "Anthropic", False),
    Source(OPENAI, "https://developers.openai.com/api/docs/models", "OpenAI", False),
)
BY_ID = {source.id: source for source in SOURCES}


@dataclass(frozen=True)
class Policy:
    """Network bounds. Production uses DEFAULT; tests substitute a local HTTP policy."""
    scheme: str = "https"
    hosts: frozenset = frozenset(urllib.parse.urlsplit(source.url).netloc for source in SOURCES)
    max_redirects: int = 3
    max_bytes: int = 8 * 1024 * 1024
    timeout_s: float = 20.0


DEFAULT = Policy()
MAX_RETRY_AFTER_S = 7 * 24 * 3600
USER_AGENT = "Pod public-model-observations (+https://github.com/j3w1/pod)"
_REDIRECTS = (301, 302, 303, 307, 308)
_DENIED = (401, 403, 407, 451)

# Parser bounds: rows, cells and text are cut off well above the real pages' sizes.
MAX_TABLES = 16
MAX_TABLE_ROWS = 2000
MAX_CELLS = 40
MAX_CELL_TEXT = 512
MAX_NAME = 120
MAX_TOKENS = 20000
MAX_SCOPED_ROWS = 400
MAX_DIAGNOSTIC = 200


class SourceError(Exception):
    """One source attempt failed; status is a snapshot source status."""

    def __init__(self, status: str, message: str, retry_after_s: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class Page:
    url: str
    text: str


_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def clean(value: str, limit: int = MAX_CELL_TEXT) -> str:
    """Bounded display text: terminal sequences and control/format characters removed."""
    kept = []
    for char in _ANSI.sub("", value[:limit * 4]):
        category = unicodedata.category(char)
        if char.isspace() or category in ("Zs", "Zl", "Zp"):
            kept.append(" ")
        elif category not in ("Cc", "Cf", "Cs", "Co", "Cn"):
            kept.append(char)
    return " ".join("".join(kept).split())[:limit]


def diagnostic(text: str) -> str:
    return clean(str(text), MAX_DIAGNOSTIC)


def retry_after(value: str | None, now: datetime | None = None) -> int | None:
    """Seconds from a Retry-After header, bounded to seven days; None when absent or invalid."""
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{1,10}", value):
        return min(int(value), MAX_RETRY_AFTER_S)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        return None
    delta = (when - (now or datetime.now(timezone.utc))).total_seconds()
    return max(0, min(int(math.ceil(delta)), MAX_RETRY_AFTER_S))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _allowed(url: str, policy: Policy) -> urllib.parse.SplitResult:
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port
    except ValueError as exc:
        raise SourceError("unavailable", "Destination URL is invalid") from exc
    if (parts.scheme != policy.scheme or parts.netloc not in policy.hosts
            or parts.username or parts.password):
        raise SourceError("unavailable", "Destination is outside the fixed source allowlist")
    return parts


def cancelled(cancel) -> bool:
    if cancel is None:
        return False
    return bool(cancel.is_set() if hasattr(cancel, "is_set") else cancel())


def _decode(raw: bytes, encoding: str, limit: int) -> bytes:
    if encoding in ("", "identity"):
        return raw
    if encoding not in ("gzip", "x-gzip", "deflate"):
        raise SourceError("malformed", "Response uses an unsupported content encoding")
    wbits = 16 + zlib.MAX_WBITS if encoding != "deflate" else zlib.MAX_WBITS
    try:
        inflater = zlib.decompressobj(wbits)
        data = inflater.decompress(raw, limit + 1)
    except zlib.error as exc:
        raise SourceError("malformed", "Compressed response is corrupt") from exc
    if len(data) > limit or inflater.unconsumed_tail:
        raise SourceError("malformed", "Decompressed response exceeds the size limit")
    if not inflater.eof:
        raise SourceError("malformed", "Compressed response is truncated")
    return data


def fetch(url: str, *, policy: Policy = DEFAULT, deadline: float | None = None,
          cancel=None, opener=None) -> Page:
    """One bounded GET of a fixed source URL; raises SourceError on any refusal."""
    deadline = time.monotonic() + policy.timeout_s if deadline is None else deadline
    opener = opener or urllib.request.build_opener(_NoRedirect)
    for _ in range(policy.max_redirects + 1):
        _allowed(url, policy)
        if cancelled(cancel):
            raise SourceError("not_attempted", "Refresh was cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceError("unavailable", "Source read exceeded the time limit")
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": "text/html", "Accept-Encoding": "gzip, deflate"})
        try:
            response = opener.open(request, timeout=remaining)
        except urllib.error.HTTPError as exc:
            status, headers = exc.code, exc.headers
            exc.close()
            if status in _REDIRECTS:
                location = headers.get("Location")
                if not location:
                    raise SourceError("unavailable", f"HTTP {status} redirect has no destination")
                url = urllib.parse.urljoin(url, location)
                continue
            wait = retry_after(headers.get("Retry-After"))
            if status in _DENIED:
                challenge = (headers.get("cf-mitigated") or "").lower() == "challenge"
                raise SourceError("access_denied", f"HTTP {status}" + (" bot challenge" if challenge else " access denied"), wait)
            raise SourceError("unavailable", f"HTTP {status}", wait)
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError):
                raise SourceError("unavailable", "Source read exceeded the time limit") from exc
            raise SourceError("unavailable", "Network error: " + type(reason).__name__) from exc
        with response:
            return _read(response, url, policy, deadline, cancel)
    raise SourceError("unavailable", "Too many redirects")


def _read(response, url: str, policy: Policy, deadline: float, cancel) -> Page:
    if response.status != 200:
        raise SourceError("unavailable", f"HTTP {response.status}")
    content_type = (response.headers.get("Content-Type") or "").lower()
    media, _, params = content_type.partition(";")
    charset = re.search(r"charset=\"?([a-z0-9_-]+)", params)
    if media.strip() != "text/html" or (charset and charset.group(1) not in ("utf-8", "utf8")):
        raise SourceError("malformed", "Response is not UTF-8 HTML")
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) > policy.max_bytes:
        raise SourceError("malformed", "Response exceeds the size limit")
    chunks, size = [], 0
    try:
        while True:
            if cancelled(cancel):
                raise SourceError("not_attempted", "Refresh was cancelled")
            if time.monotonic() > deadline:
                raise SourceError("unavailable", "Source read exceeded the time limit")
            chunk = response.read1(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > policy.max_bytes:
                raise SourceError("malformed", "Response exceeds the size limit")
            chunks.append(chunk)
    except http.client.IncompleteRead as exc:
        raise SourceError("malformed", "Response is truncated") from exc
    except (OSError, http.client.HTTPException) as exc:
        if isinstance(exc, TimeoutError):
            raise SourceError("unavailable", "Source read exceeded the time limit") from exc
        raise SourceError("unavailable", "Network error: " + type(exc).__name__) from exc
    raw = _decode(b"".join(chunks), (response.headers.get("Content-Encoding") or "").strip().lower(),
                  policy.max_bytes)
    try:
        return Page(url, raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise SourceError("malformed", "Response is not valid UTF-8") from exc


# ---------------------------------------------------------------- parsing helpers


class ParseError(Exception):
    pass


_SKIP = ("script", "style", "svg", "noscript", "template")


class _Tables(HTMLParser):
    """Closed tables only, as rows of cleaned cell text; script/style text is ignored."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._open, self._row, self._cell, self._skip = [], [], None, None, 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag == "table":
            if len(self._open) >= 4 or len(self.tables) + len(self._open) >= MAX_TABLES:
                raise ParseError("Page has too many or too deeply nested tables")
            self._open.append([])
        elif self._open and tag == "tr":
            self._row = []
        elif self._open and self._row is not None and tag in ("td", "th"):
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif not self._open:
            return
        elif tag in ("td", "th") and self._cell is not None:
            if len(self._row) >= MAX_CELLS:
                raise ParseError("Table row has too many cells")
            self._row.append(clean("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if len(self._open[-1]) >= MAX_TABLE_ROWS:
                raise ParseError("Table has too many rows")
            self._open[-1].append(self._row)
            self._row = self._cell = None
        elif tag == "table":
            self.tables.append(self._open.pop())
            self._row = self._cell = None

    def handle_data(self, data):
        if self._cell is not None and not self._skip and sum(map(len, self._cell)) < MAX_CELL_TEXT * 4:
            self._cell.append(data)


class _Tokens(HTMLParser):
    """Visible text nodes in document order, excluding script/style/svg content."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if self._skip:
            return
        text = clean(data)
        if text:
            if len(self.tokens) >= MAX_TOKENS:
                raise ParseError("Page has too much text")
            self.tokens.append(text)


def _feed(parser: HTMLParser, text: str):
    try:
        parser.feed(text)
        parser.close()
    except ParseError:
        raise
    except (AssertionError, ValueError, RecursionError) as exc:
        raise ParseError("Page HTML cannot be parsed") from exc
    return parser


def _key(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


_NUMBER = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d{1,9})(?:\.\d{1,6})?")
_MISSING = ("", "--", "—", "–", "-", "n/a")


def number(text: str, *, low: float = 0, high: float = 100000, prefix: str = "", suffix: str = "") -> int | float | None:
    """A finite bounded decimal with an exact unit prefix/suffix; None for an explicit missing mark."""
    if text.strip().lower() in _MISSING:
        return None
    body = text.strip()
    if prefix:
        if not body.startswith(prefix):
            raise ParseError("Value has the wrong unit")
        body = body[len(prefix):]
    if suffix:
        if not body.endswith(suffix):
            raise ParseError("Value has the wrong unit")
        body = body[:-len(suffix)]
    if not _NUMBER.fullmatch(body):
        raise ParseError("Value is not a finite decimal number")
    value = float(body.replace(",", ""))
    if not math.isfinite(value) or not low <= value <= high:
        raise ParseError("Value is outside its plausible range")
    return int(value) if "." not in body else value


def tokens_count(text: str) -> int | None:
    """Context sizes such as 1M, 1.05M, 872k or 200K tokens, as whole tokens."""
    body = text.strip()
    if body.lower() in _MISSING:
        return None
    match = re.fullmatch(r"(\d{1,4}(?:\.\d{1,3})?)\s?([kKmM])(?: tokens)?", body)
    if not match:
        raise ParseError("Context window has an unsupported unit")
    value = round(float(match.group(1)) * (1000 if match.group(2) in "kK" else 1_000_000))
    if not 1000 <= value <= 100_000_000:
        raise ParseError("Context window is outside its plausible range")
    return value


_ROW_NAME = re.compile(r"[^\s()][^()]{0,79}(?: \([^()]{1,60}\))?")
_MODEL_ID = re.compile(r"[a-z][a-z0-9.\-]{1,63}")
EMPTY_METRICS = {"intelligence": None, "usd_per_task": None, "output_tps": None,
                 "first_response_s": None, "total_response_s": None, "context_tokens": None}


def _row(name: str, creator: str, qualifiers: list[str], **metrics) -> dict:
    return {"name": name, "creator": creator, "qualifiers": qualifiers,
            "metrics": EMPTY_METRICS | metrics}


def _unique(rows: list[dict]) -> list[dict]:
    names = [row["name"] for row in rows]
    if len(names) != len(set(names)):
        raise ParseError("Source has duplicate rows for one exact name")
    if len(rows) > MAX_SCOPED_ROWS:
        raise ParseError("Source has too many rows")
    return rows


# ---------------------------------------------------------------- Artificial Analysis

AA_COLUMNS = {"model": "model", "contextwindow": "context", "creator": "creator",
              "artificialanalysisintelligenceindex": "intelligence", "costpertaskusd": "usd_per_task",
              "mediantokens/s": "output_tps", "latencyfirstchunk(s)": "first_response_s",
              "totalresponse(s)": "total_response_s"}
_AA_METHOD = re.compile(r"Intelligence Index v(\d{1,2}(?:\.\d{1,2})?)\b")


def parse_aa(text: str) -> dict:
    """The leaderboard's static table, Anthropic and OpenAI rows only, exact row names kept."""
    tables = _feed(_Tables(), text).tables
    for table in tables:
        header = next((index for index, row in enumerate(table[:4]) if row and _key(row[0]) == "model"), None)
        if header is not None:
            break
    else:
        raise ParseError("Leaderboard table with a Model header is missing")
    keys = [_key(cell) for cell in table[header]]
    columns = {}
    for index, key in enumerate(keys):
        if key in AA_COLUMNS:
            if AA_COLUMNS[key] in columns:
                raise ParseError("Leaderboard has a duplicate column")
            columns[AA_COLUMNS[key]] = index
    missing = sorted(set(AA_COLUMNS.values()) - set(columns))
    if missing:
        raise ParseError("Leaderboard lacks required columns: " + ", ".join(missing))
    body = table[header + 1:]
    if not body:
        raise ParseError("Leaderboard has no rows")
    rows = []
    for cells in body:
        if len(cells) != len(keys):
            raise ParseError("Leaderboard row has the wrong number of cells")
        creator = cells[columns["creator"]]
        if creator not in CREATORS:
            continue
        name = cells[columns["model"]]
        if len(name) > MAX_NAME or not _ROW_NAME.fullmatch(name):
            raise ParseError("Leaderboard row name is not a model name")
        qualifiers = ["with fallback"] if name.endswith(" with fallback)") else []
        score = cells[columns["intelligence"]]
        if score.endswith("*"):
            score = score[:-1]
            qualifiers.append("estimated index")
        rows.append(_row(name, creator, qualifiers,
                         intelligence=number(score, high=100),
                         usd_per_task=number(cells[columns["usd_per_task"]], prefix="$"),
                         output_tps=number(cells[columns["output_tps"]]),
                         first_response_s=number(cells[columns["first_response_s"]]),
                         total_response_s=number(cells[columns["total_response_s"]]),
                         context_tokens=tokens_count(cells[columns["context"]])))
    if not rows:
        raise ParseError("Leaderboard has no Anthropic or OpenAI rows")
    # The table has no methodology label; the page's embedded text names the index version.
    versions = set(_AA_METHOD.findall(text))
    methodology = ("Artificial Analysis Intelligence Index v" + versions.pop()) if len(versions) == 1 else None
    diagnostics = [] if methodology else ["Intelligence Index version is not stated unambiguously"]
    return {"rows": _unique(rows), "published_at": None, "methodology": methodology,
            "diagnostics": diagnostics}


# ---------------------------------------------------------------- Anthropic models overview


def parse_anthropic(text: str) -> dict:
    """The models overview comparison table: one column per model, keyed by its API id."""
    for table in _feed(_Tables(), text).tables:
        labels = {_key(row[0]): row for row in table if row}
        if "claudeapiid" in labels and "contextwindow" in labels:
            break
    else:
        raise ParseError("Model comparison table with API ids is missing")
    ids, contexts = labels["claudeapiid"][1:], labels["contextwindow"][1:]
    if not ids or len(ids) != len(contexts):
        raise ParseError("Model comparison columns do not line up")
    rows = []
    for model_id, context in zip(ids, contexts):
        if not _MODEL_ID.fullmatch(model_id) or not model_id.startswith("claude-"):
            raise ParseError("Model comparison has an invalid API id")
        rows.append(_row(model_id, "Anthropic", [], context_tokens=tokens_count(context)))
    return {"rows": _unique(rows), "published_at": None, "methodology": None, "diagnostics": []}


# ---------------------------------------------------------------- OpenAI models page


def parse_openai(text: str) -> dict:
    """Model cards: each "Model ID" label and the card's "Context window" value."""
    tokens = _feed(_Tokens(), text).tokens
    starts = [index for index, token in enumerate(tokens) if token == "Model ID"]
    if not starts:
        raise ParseError("Model cards with a Model ID label are missing")
    rows = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else min(len(tokens), start + 200)
        card = tokens[start:end]
        model_id = card[1] if len(card) > 1 else ""
        if not _MODEL_ID.fullmatch(model_id) or not model_id.startswith("gpt-"):
            raise ParseError("Model card has an invalid model id")
        context = None
        if "Context window" in card:
            label = card.index("Context window")
            context = tokens_count(card[label + 1] if label + 1 < len(card) else "")
        rows.append(_row(model_id, "OpenAI", [], context_tokens=context))
    return {"rows": _unique(rows), "published_at": None, "methodology": None, "diagnostics": []}


PARSERS = {AA: parse_aa, ANTHROPIC: parse_anthropic, OPENAI: parse_openai}
