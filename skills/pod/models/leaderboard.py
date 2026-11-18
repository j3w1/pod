"""Fetch and read the public leaderboard: one bounded request, no retries, no credentials.

Identity and the two measurements Pod compares are required. Every other column Pod knows is
its own named metric with its unit; one that is absent reads as unavailable, never as zero. A
column Pod does not know is reported and not shown. A renamed column is therefore a new,
unknown column: no metric is ever read from a header it was not defined for.
"""

from collections import Counter
from html.parser import HTMLParser
import http.client
import io
import json
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
# The grouped response uses seconds for task duration. Prices are USD per million tokens.
_PROFILE_FIELDS = {
    "intelligence_index": "intelligenceIndex", "cost_per_task_usd": "intelligenceIndexCostPerTask",
    "context_window_tokens": "contextWindowTokens", "time_per_task_min": "intelligenceIndexTimePerTask",
    "first_answer_token_s": "timeToFirstAnswerToken", "first_token_s": "timeToFirstToken",
    "output_tokens_per_s": "outputSpeed", "omniscience_net_score": "omniscience",
    "input_price_per_million_usd": "price1mInputTokens", "output_price_per_million_usd": "price1mOutputTokens",
    "cache_hit_price_per_million_usd": "cacheHitPrice", "cache_write_price_per_million_usd": "cacheWritePrice",
}
UNITS.update({"first_token_s": "seconds", "output_tokens_per_s": "tokens per second",
              "omniscience_net_score": "net score points", "benchmark_scores": "each benchmark's native score"})
UNITS.update({name: "US dollars per million tokens" for name in _PROFILE_FIELDS if name.endswith("_per_million_usd")})
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
                chunk = response.read1(min(65536, max_bytes + 1 - size))     # whatever has arrived, so the deadline is seen
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
            if not inflater.eof:
                raise RefreshError("The compressed source page is incomplete")
        elif encoding not in ("", "identity"):
            raise RefreshError("The source used an unsupported content encoding")
        return raw.decode("utf-8")
    except (zlib.error, UnicodeError):
        raise RefreshError("The source page could not be decoded") from None


class _Tables(HTMLParser):
    """Visible cells and the JSON string chunks carrying the published profiles."""
    SKIP = ("script", "style", "svg", "noscript", "template")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._open, self._row, self._cell, self._skip, self._size = [], [], None, None, 0, 0
        self._creators, self._suffix, self._suffix_count, self._in_suffix = [], [], 0, 0
        self.chunks = []
        self._inline_script = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            if tag == "script":
                self._inline_script = not self._skip and "src" not in dict(attrs)
            self._skip += 1
        elif tag == "table":
            if len(self._open) >= 4 or len(self.tables) + len(self._open) >= 16:
                raise RefreshError("The page has too many tables")
            self._open.append([])
        elif self._open and tag == "tr":
            self._row = []
        elif self._open and self._row is not None and tag in ("td", "th"):
            self._cell, self._size = [], 0
            self._creators, self._suffix, self._suffix_count, self._in_suffix = [], [], 0, 0
            self._expandable = False
        elif self._cell is not None and not self._skip:
            attrs = dict(attrs)
            if tag == "img":
                self._creators.append(clean(attrs.get("alt") or "", 120))
            elif tag == "button" and attrs.get("aria-expanded") in ("true", "false"):
                self._expandable = True
            elif tag == "span":
                if {"font-normal", "text-neutral-500"} <= set(attrs.get("class", "").split()):
                    self._suffix_count += 1
                    self._in_suffix += 1
                elif self._in_suffix:
                    self._in_suffix += 1
            elif tag == "br":
                self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            if tag == "script":
                self._inline_script = False
            self._skip = max(0, self._skip - 1)
        elif not self._open:
            return
        elif tag == "span":
            self._in_suffix = max(0, self._in_suffix - 1)
        elif tag in ("td", "th") and self._cell is not None:
            if len(self._row) >= MAX_CELLS:
                raise RefreshError("A table row has too many cells")
            cell = _Cell(clean("".join(self._cell)))
            cell.creators, cell.suffix = self._creators, clean("".join(self._suffix))
            cell.suffix_count = self._suffix_count
            cell.expandable = self._expandable
            self._row.append(cell)
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
        if self._inline_script and data.lstrip().startswith("self.__next_f.push("):
            call = data.strip().removesuffix(";")
            if not call.endswith(")"):
                raise RefreshError("Incomplete leaderboard data chunk")
            chunk = json.loads(call[len("self.__next_f.push("):-1])
            if not isinstance(chunk, list) or not chunk or type(chunk[0]) is not int:
                raise RefreshError("Unreadable leaderboard data chunk")
            if chunk[0] == 1:
                if len(chunk) != 2 or not isinstance(chunk[1], str) or len(self.chunks) >= 4096:
                    raise RefreshError("Invalid leaderboard text chunks")
                self.chunks.append(chunk[1])
        if self._cell is not None and not self._skip and self._size < MAX_CELL_TEXT * 4:
            self._cell.append(data)
            self._size += len(data)
            if self._in_suffix:
                self._suffix.append(data)


class _Cell(str):
    """Cell text plus the current table's explicitly marked creator and profile."""

    def identity(self):
        if len(self.creators) != 1 or not self.creators[0]:
            raise RefreshError("A model cell has no unambiguous creator image alt text")
        name = str(self)
        if self.suffix_count > 1 or (self.suffix_count and not self.suffix):
            raise RefreshError("A model cell has an ambiguous or empty profile suffix")
        if self.suffix:
            if not name.endswith(" " + self.suffix):
                raise RefreshError("A model cell has an unreadable profile suffix")
            base = name[:-len(self.suffix)].strip()
            if not base:
                raise RefreshError("A model cell has no model name")
            name = _profile_name(base, self.suffix)
        return name, self.creators[0]


def _profile_name(base, suffix):
    if not suffix:
        return base
    qualifier = suffix.lower()
    if qualifier.endswith(", default fallback"):
        qualifier = qualifier.removesuffix(", default fallback") + " with fallback"
    return f"{base} ({qualifier})"


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


def _unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise RefreshError("The grouped leaderboard contains a duplicate JSON field")
    return fields


def _models_from_chunks(chunks):
    """Read initialModels only from component props in a complete JSON Flight record."""
    found = []
    for line in io.StringIO("".join(chunks)):
        if '"initialModels"' not in line:
            continue
        number, _, body = line.partition(":")
        if not re.fullmatch(r"[0-9a-f]+", number) or not body.lstrip().startswith("["):
            continue
        try:
            pending = [json.loads(body, object_pairs_hook=_unique_fields)]
        except (ValueError, RecursionError):
            raise RefreshError("The grouped leaderboard record is incomplete or unreadable") from None
        while pending:
            node = pending.pop()
            if not isinstance(node, list):
                continue
            if len(node) == 4 and node[0] == "$" and isinstance(node[3], dict):
                props = node[3]
                if "initialModels" in props:
                    found.append(props["initialModels"])
                if "children" in props:
                    pending.append(props["children"])
            else:
                pending.extend(node)
    if not found:
        return None
    if len(found) != 1 or not isinstance(found[0], list) or not 0 < len(found[0]) <= MAX_ROWS:
        raise RefreshError("The page does not contain one complete grouped leaderboard")
    return found[0]


def _identity_text(value):
    if not isinstance(value, str) or len(value) > 120 or not (text := clean(value, 120)):
        raise RefreshError("A grouped leaderboard profile has an unreadable identity")
    return text


def _measurement(value, *, low=0, high=1e18):
    if value is None or (type(value) in (int, float) and low <= value <= high):
        return value
    raise ValueError


def _profile_identity(model):
    if (not isinstance(model, dict) or not isinstance(model.get("release"), dict)
            or "suffix" not in model or "intelligenceIndex" not in model):
        raise RefreshError("The grouped leaderboard contains an incomplete profile")
    release = _identity_text(model["release"].get("slug"))
    base = _identity_text(model["release"].get("name"))
    creator = _identity_text(model.get("modelCreatorName"))
    profile = None if model["suffix"] is None else _identity_text(model["suffix"])
    published_name = _identity_text(model.get("name"))
    expected = f"{base} ({profile})" if profile else base
    if published_name.casefold() != expected.casefold():
        raise RefreshError("A grouped leaderboard name disagrees with its release and profile")
    name = _profile_name(base, profile)
    return {"name": name, "creator": creator, "release": release, "model_name": base, "profile": profile}


def _read_profile_metrics(model, row, supplied):
    unread = 0
    for metric, source in _PROFILE_FIELDS.items():
        if source in model:
            supplied.add(metric)
        try:
            if metric == "intelligence_index":
                value = _measurement(model[source], high=100)
            elif metric == "omniscience_net_score":
                value = _measurement(model.get(source), low=-100, high=100)
            else:
                value = _measurement(model.get(source))
            if metric == "time_per_task_min" and value is not None:
                value /= 60
            if metric == "context_window_tokens" and value is not None and type(value) is not int:
                raise ValueError
        except ValueError:
            if metric in ("intelligence_index", "cost_per_task_usd"):
                raise RefreshError("A grouped leaderboard profile has an unreadable index or cost") from None
            value = None
            unread += 1
        row[metric] = value
    # Benchmark results use different units/scales. Keep the published keys and raw scores.
    scores = model.get("scores")
    row["benchmark_scores"] = None
    if isinstance(scores, dict) and len(scores) <= 128:
        supplied.add("benchmark_scores")
        row["benchmark_scores"] = {}
        for benchmark, value in scores.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", benchmark):
                unread += 1
                continue
            try:
                row["benchmark_scores"][benchmark] = _measurement(value, low=-1e18)
            except ValueError:
                row["benchmark_scores"][benchmark] = None
                unread += 1
    elif scores is not None:
        unread += 1
    return unread


def _profile_rows(models, visible, expandable):
    visible_by_name = {(row["creator"], row["name"]): row for row in visible}
    if len(visible_by_name) != len(visible):
        raise RefreshError("The visible leaderboard repeats a profile identity")
    rows, seen, matched, supplied = [], set(), set(), set()
    unread = 0
    for model in models:
        row = _profile_identity(model)
        identity = (row["creator"], row["release"], row["profile"])
        if identity in seen:
            raise RefreshError("The grouped leaderboard repeats a release profile")
        seen.add(identity)
        unread += _read_profile_metrics(model, row, supplied)
        visible_key = (row["creator"], row["name"])
        rendered = visible_by_name.get(visible_key)
        row["index_estimated"] = bool(rendered and rendered["index_estimated"])
        if rendered is not None:
            if visible_key in matched:
                raise RefreshError("A visible profile matches more than one grouped release")
            matched.add(visible_key)
            if any(rendered[key] is not None and row[key] is None for key in ("intelligence_index", "cost_per_task_usd")):
                raise RefreshError("The grouped leaderboard is missing a visible measurement")
        rows.append(row)
    if visible_by_name.keys() != matched:
        raise RefreshError("The grouped leaderboard is missing a visible profile")
    counts = Counter((creator, release) for creator, release, _ in seen)
    for row in rows:
        if (row["creator"], row["name"]) in expandable and counts[row["creator"], row["release"]] < 2:
            raise RefreshError("An expandable release is missing its additional profiles")
    absent = sorted(UNITS.keys() - supplied - {"intelligence_index", "cost_per_task_usd"})
    return rows, absent, unread


def parse(text: str) -> dict:
    """Rows, the columns found and diagnostics. Raises RefreshError; never returns a partial result."""
    parser = _Tables()
    if len(text) > MAX_BYTES:
        raise RefreshError("The source page is larger than the fetch limit")
    try:
        parser.feed(text)
        parser.close()
        if parser._inline_script:
            raise RefreshError("The source page ended inside a script")
    except RefreshError:
        raise
    except Exception as exc:    # html.parser raises assorted errors on hostile markup
        raise RefreshError(f"The page could not be parsed ({type(exc).__name__})") from None
    for table in parser.tables:
        header = next((index for index, row in enumerate(table[:4])
                       if "model" in {_key(cell) for cell in row} and
                       {"creator", "artificialanalysisintelligenceindex"} & {_key(cell) for cell in row}), None)
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
    inline_identity = "creator" not in columns
    missing = sorted(name for name, _unit in REQUIRED.values()
                     if name not in columns and not (name == "creator" and inline_identity))
    if missing:
        raise RefreshError("Leaderboard lacks required columns: " + ", ".join(missing))
    unknown = [table[header][index] for index, key in enumerate(keys)
               if key and key not in known and key not in IGNORED]
    absent = sorted(name for name, _unit in OPTIONAL.values() if name not in columns)
    body = table[header + 1:]
    if not body:
        raise RefreshError("Leaderboard has no rows")
    rows, unread, expandable = [], 0, set()
    for number, cells in enumerate(body, 1):
        if len(cells) != len(keys):
            raise RefreshError(f"Leaderboard row {number} has the wrong number of cells")
        name, creator = (cells[columns["name"]].identity() if inline_identity else
                         (cells[columns["name"]], cells[columns["creator"]]))
        if not name or not creator or len(name) > 120:
            raise RefreshError(f"Leaderboard row {number} has no usable model or creator name")
        if cells[columns["name"]].expandable:
            expandable.add((creator, name))
        score = cells[columns["intelligence_index"]]
        estimated = score.endswith("*")
        try:
            row = {"name": name, "creator": creator, "index_estimated": estimated,
                   "intelligence_index": _number(score.removesuffix("*"), high=100),
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
    models = _models_from_chunks(parser.chunks)
    if models is not None:
        rows, absent, unread = _profile_rows(models, rows, expandable)
    elif expandable:
        raise RefreshError("The expandable leaderboard is missing its grouped profile data")
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
