# Model catalog and observations

The shipped route registry, dated public observations, their refresh, and the one joined
projection every view reads. Authority over routes lives in [preferences](preferences.md);
how the coordinator uses these facts lives in [routing](routing.md).

| Source | Owns | Cannot do |
| --- | --- | --- |
| Shipped registry | Supported exact identities and efforts, aliases and authored routing guidance | Prove account access |
| Personal preferences | Eligibility, Preferred, Pin and worker ceiling | Prove native capability or authorize spending |
| Public provider pages | Attributed identity and context observations | Rewrite the registry or preferences |
| Public AA leaderboard | Dated measurements of an identified benchmark profile | Grant permission, prove task fitness or account access |
| Existing native readback | Observed adapter capabilities and effective route | Widen the user pool |

## Requirements

### R14 — Maintain one shipped route registry

Type: B · Scenarios: [A10](#a10), [A45](preferences.md#a45), [A48](#a48), [A116](#a116), [A122](#a122), [A178](#a178)

Maintain one shipped `pod-catalog/v3` registry of exact supported model ids, verified exact efforts, explicit per-source aliases, attributed provider guidance of 20–70 words, provider sources with checked dates, concise nonbinding Pod guide profiles and documented context ceilings where known. A short not-routable list keeps earlier generations and specialized models readable in older records. The registry holds no benchmark measurements. Adding an ordinary model is a registry edit, never a scattered code limit, and web data never extends the registry. `python -m pod.catalog --check` validates it.

### R17 — Expose one dispatch-free joined route projection

Type: B · Scenarios: [A34](../interface.md#a34), [A50](#a50), [A125](#a125), [A178](#a178)

Expose one dispatch-free joined route projection (registry, personal preferences, cached observations and native observation) for the CLI, the workspace, doctor and status, and validate coordinator-proposed routes deterministically. Display order, sorting, comparisons, frontier marks and benchmark data never become routing inputs or trigger model calls; the projection computes no rank, score or recommendation.

### R72 — The normal pool is six bases at exact efforts

Type: B,H · Scenarios: [A116](#a116), [A122](#a122), [A129](../orca.md#a129)

The normal supported bases are Claude Opus 5.5, Fable 5.1 and Sonnet 5.5 (agent `claude`) and GPT-6 Astra, GPT-6.1 Sol and GPT-6 Luna (agent `codex`) at their exact native ids, each with the efforts `low`, `medium`, `high`, `xhigh` and `max`. New decisions name one of those exact efforts; `native_default` effort remains readable only in older records and recovery. `ultra` is not supported, and a non-reasoning (`none`) variant stays informational until an adapter documents that exact value. Context is `native_default` while Orca lacks a per-worker context flag; documented ceilings are not effective proof.

### R102 — Public observations refresh as one validated snapshot

Type: B,H · Scenarios: [A211](#a211), [A212](#a212), [A213](#a213), [A214](#a214), [A221](../interface.md#a221)

Public observations are dated facts read from a fixed set of public HTTPS pages within bounded time, size, redirect, decompression and parser limits. A refresh validates and promotes one snapshot atomically, or promotes nothing; it never writes the registry, preferences or bundle, never holds the preference lock and never starts workers. Readers fall back from the local cache to the bundled snapshot to unknown without network. Config, status, doctor, selection and admission never fetch. Automatic refresh happens only when opening the workspace with `refresh: automatic`, as one bounded asynchronous attempt; `refresh: manual` disables automatic network access. Access denials and bot challenges are reported, never bypassed.

### R103 — Discovery stays observation, never authorization

Type: B,H · Scenarios: [A10](#a10), [A215](#a215)

New Anthropic and OpenAI observations appear automatically but stay new, unmapped or unsupported until the shipped registry supports the exact route and the user explicitly enables it. A refresh, registry update or software update cannot expand an existing enabled pool, transfer a pin or Preferred route to a newer generation, or erase Disabled intent, and a disappearing webpage row never deletes a saved choice or record. Discovered, supported, enabled and native-observed facts stay separate. Native access is `unknown` unless an existing documented read-only native interface reports it; there is no new inventory endpoint, credential inspection, probe worker or menu scraping.

## Acceptance scenarios

- <a id="a10"></a>**A10** — Discovery, a refresh or a registry update alone cannot enable a route, transfer a pin or prove installed capability.
- <a id="a48"></a>**A48** — Registry identities, verified efforts and runtime capability remain separate; an unsupported or replaced model, an unsupported effort (`ultra`, `none`) or `native_default` effort for a new decision is refused.
- <a id="a50"></a>**A50** — Captured choices validate against a supplied preference snapshot without a ranking formula, model call or observation read.
- <a id="a116"></a>**A116** — The six current bases are routable only at their verified exact efforts; replaced, specialized and unmapped ids cannot launch; context uses native default without invented flags, and effective values are separately observed.
- <a id="a122"></a>**A122** — The registry validates exact identities, verified efforts, provider sources and check dates, attributed guidance and unambiguous explicit source aliases, and holds no benchmark measurements; adding an ordinary model is a registry edit.
- <a id="a125"></a>**A125** — Workspace sorting, filtering, comparison, frontier marks and AA metrics cannot influence the selected route or start a worker.
- <a id="a178"></a>**A178** — Config JSON supplies every supported route's state and the guide profiles; the joined projection pairs each route's AA metrics with their source row, qualifiers and dates, and the workspace renders them without a model call or dispatch.
- <a id="a211"></a>**A211** — A refresh promotes a valid update atomically with previous rotation; `--check`, cancellation, interruption, a concurrent or out-of-order refresh, malformed or truncated required input and severe coverage loss promote nothing, keep current and previous coherent, and leave preferences and the bundle unchanged.
- <a id="a212"></a>**A212** — Fetches stay on the HTTPS allowlist and within time, size, redirect and decompression limits; invalid numbers, duplicates, hostile text and control sequences are refused or cleaned, access denial and challenges are reported, and Retry-After is honored.
- <a id="a213"></a>**A213** — Exact profile association, aliases, units, source and methodology labels, unknown fields and the fallback and estimated-index qualifiers survive parsing and projection; token prices never appear as cost per task.
- <a id="a214"></a>**A214** — Automatic refresh starts only for an absent cache or data at least 24 hours old with `refresh: automatic`, respects Retry-After and the restraint after an unsuccessful or running attempt, shows data at least seven days old as stale, and is cancellable; config, status, doctor and admission make no network call.
- <a id="a215"></a>**A215** — New, unsupported and missing rows, provider disagreement and unknown native access remain honest observations, and a refresh or software update leaves the enabled pool, Preferred, Pin and Disabled routes unchanged.

## Registry

`skills/pod/catalog.json` (`pod-catalog/v3`) is the single authored registry. Each supported
base carries its exact id, display name, agent and provider, its verified efforts in
vocabulary order, a documented context ceiling or `null`, attributed provider guidance, provider
source URLs with checked dates on the provider's own hosts, a guide profile and explicit
aliases. The not-routable list carries `claude-sonnet-5`, `gpt-6-sol` and the Daybreak models
with a note and their own aliases. Validation checks exact fields, id and source-id shape, agent
and provider pairing, efforts as an ordered subset of `low, medium, high, xhigh, max`, provider
hosts, no future check dates, unique ids, aliases that are unambiguous across entries and never
equal a registry id, and a 128 KB size bound.

Aliases map a source's exact row name to an effort of that base, to `none` for an informational
non-reasoning variant, or to `null` for a model-level row. Matching is exact: a source's explicit
alias, or a row name exactly equal to a registry id. There is no fuzzy matching, guessed slug or
model-generated extraction; unknown names stay unmapped. Adding an ordinary model is a registry
edit followed by `pod.catalog --check`.

The nonbinding guide profiles are data that the workspace renders and the coordinator may read;
they are not role mandates:

| Base | Starting use |
| --- | --- |
| GPT-6 Luna | Low/medium; high/xhigh for bounded reasoning. Mechanical and focused work with cheap, strong verification. |
| GPT-6.1 Sol | Medium; high/xhigh for harder work. Ordinary implementation reference point. |
| Claude Sonnet 5.5 | Medium/high; xhigh where justified. Responsive, well-scoped agentic and tool work. |
| Claude Opus 5.5 | Medium/high; xhigh for justified margin. Planning, architecture and substantive review. |
| GPT-6 Astra | Medium/high; higher effort only for a specific need. Demanding, broad or tool-heavy reasoning. |
| Claude Fable 5.1 | High and above. Specialized demanding, long-horizon work when that profile is justified. |
| Any max route | Exceptional capability-first work with acceptable cost and latency; never chosen solely because it tops AA. |

## Observations and refresh

Public observations come from a fixed set of public HTTPS pages: the Artificial Analysis
leaderboard (`artificial_analysis`, required), Anthropic's models overview (`anthropic_models`,
optional) and OpenAI's developer models page (`openai_models`, optional). The hosts form a fixed
allowlist. A fetch uses stdlib `urllib` with no cookies, credentials, API clients, keys, accounts,
browser or script execution, follows at most three redirects inside the allowlist, stops at
about 20 seconds for the whole refresh, caps bodies and gzip/deflate decompression at 8 MiB and
accepts only UTF-8 HTML. 401, 403, 407 and 451 are `access_denied` (a challenge is noted), 429,
5xx and network failures are `unavailable`, and parse or shape problems are `malformed`.
Retry-After is honoured, capped at seven days. Fetched URLs, commands and instructions are data,
never execution authority.

Isolated `html.parser` parsers bound tables, nesting, rows, cells and text, strip control and
escape sequences, find AA columns by header name, and validate finite numbers in range with their
exact units, duplicates and required columns. Rows keep the exact source name and are limited to
Anthropic and OpenAI creators; they are matched to routes at read time through the registry, so a
registry update takes effect without re-fetching. Missing metrics are `null` and shown as unknown,
never zero. AA's "with fallback" describes AA's harness and never enables Pod fallback; an
asterisked index carries the `estimated index` qualifier. Provider token prices are not recorded
and never stand in for cost per task. Retrieval time is not a measurement date; AA publishes none.
The AA methodology label comes from the page's single "Intelligence Index vX.Y" mention, or stays
`null`.

`pod models refresh` reads the required source first and then the optional ones, and promotes one
`pod-observations/v1` snapshot atomically under the cache lock with a compare-and-swap on the
current bytes, so an older or concurrent request cannot overwrite newer data (`superseded`); the
old current becomes `previous.json`. Nothing is promoted when the required source fails, is
malformed or truncated, or loses more than half of its rows or mapped rows from a reference of at
least eight, or when the run is `--check`, cancelled or superseded. A failed optional source keeps
its earlier rows with their own retrieval time, and every source shows its own age; mixed ages are
never hidden behind one timestamp.

Only `current.json`, `previous.json` and `refresh.json` (last attempt, Retry-After and per-source
diagnostics; source-fetch metadata, not model-health policy) live in
`${XDG_CACHE_HOME:-~/.cache}/pod/models/`, or `$POD_CACHE_HOME/models/` for disposable validation,
never inside the bundle, a worktree or the preference YAML. A corrupt cache falls back with a
diagnostic. With `refresh: automatic`, opening the workspace starts one asynchronous refresh when
no local cache exists or the data is at least 24 hours old, unless the required source's
Retry-After is active or an unsuccessful or still-running attempt started within the last six
hours. Data at least seven days old is shown as stale.
There is no daemon, timer or watcher; quitting cancels an automatic refresh.

The bundled `skills/pod/observations.json` is a small fallback snapshot taken from one real
refresh. Regenerating it is a manual maintenance step: run one real `pod models refresh` into a
disposable `POD_CACHE_HOME`, copy its `current.json` unchanged, and keep it under the 64 KB bundle
text limit.

## Joined projection

`routes.project()` returns `pod-routes/v1`: the preference summary (path, schema, status,
revision, eligible, Preferred, Pin, worker ceiling, refresh, errors, setup and bounded
diagnostics), the observation summary (status, origin, generation, creation time, staleness,
diagnostics and per-source URL, attribution, status, times, methodology, row count and staleness),
one row per supported route, the unmapped observations, native access `unknown` and a count
summary. Each route row carries its key, agent, model, display name, effort and provider; state
`enabled`, `disabled` or `not_set`; `preferred` and `pinned` flags; `supported` and `discovery`;
every metric with its own value, source, row, qualifiers, methodology and dates; every matched
observation, including model-level provider rows, so provider disagreement stays visible; the
documented context ceiling; the guide profile, with the max-route note for `max` routes; the
attributed guidance and sources; and native `{access: unknown}`. One source row supplies all of a
route's metrics, so a score never pairs with another row's cost. Unmapped Anthropic and OpenAI rows
are listed separately as `new`, or `unsupported` when they match a non-route id or variant, and are
never routable. The projection never ranks; display order is the reader's choice.
