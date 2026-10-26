# Verification contract

Every result names the exact commit, host, UTC date, command, outcome and sanitized
evidence reference. Implementation, unit/local verification, PTY/subprocess behavior,
installed-bundle proof, hosted CI, live native proof, visual review, independent review,
project acceptance and merge are separate facts. A check not exercised is `NOT_RUN`;
a passing fixture never promotes live proof or project acceptance. Worker success
records an assignment outcome, not acceptance of the objective.

## What a change needs

| Change class | Required checks |
| --- | --- |
| Docs or tests | Affected inventory/content or test checks; `tools/suite_lanes.py check`; source hygiene and whitespace. |
| Skill text | Skill validation, inventory and affected authority anchors. |
| Python behavior or boundaries | Focused production-boundary tests and affected incident cases, then compile; retain refusal and byte/effect invariants. |
| Routes, preferences or public observations | Catalog, config, pin, selection, routes, route-upgrade, sources, observations and CLI tests; workspace state/render tests when affected. |
| Terminal behavior | Required PTY suite, frame/size checks and affected visual snapshots. |
| Installation or bundle inventory | Installer and copied-bundle tests in scrubbed disposable homes, including `tests.test_bundle_install`. |
| Admission, assurance or closure governance | The preceding installed mechanism's independent final audit, with affected policy and production-boundary evidence. Candidate self-hosting evidence is separate. |
| Milestone | A root `VERSION` bump defines a milestone: all local gates below, then separate hosted, review, live and acceptance evidence as required. |

Use cheap discriminating checks early and focused checks during development.
Do not run the full suite after every small edit. At a milestone run all gates against
the frozen candidate. Corrections repeat affected checks; new failures or uncertainty
justify broader checks. A requirement change updates its scenario and coverage row
in the same commit. Removing a requirement removes both. Never weaken a gate or
fabricate compatibility to make a result pass.

## Milestone gates

| Check | Command or evidence | What it proves |
| --- | --- | --- |
| Unit and incident suite | `PYTHONPATH=skills python tools/suite_lanes.py run` | Current behavior in disposable fixtures; every discovered test once, in its lane. `python -m unittest discover -s tests -v` is the equivalent serial run. |
| Suite lanes | `PYTHONPATH=skills python tools/suite_lanes.py check` | Each test has one lane, tests the timing heuristic flags are classified, recorded entries name real tests, incident modules yield tests and every lane fits its target. |
| Explicit incidents | `PYTHONPATH=skills python -m unittest discover -s tests/incidents -t . -v` | Incident cases are discovered. |
| Compile and whitespace | `python -m compileall -q skills tests tools`; `git diff --check $(git hash-object -t tree /dev/null) HEAD` | Syntax and whole-candidate whitespace. |
| Skill validation | `PYTHONPATH=skills python -m pod.skill_validation skills/pod` | Inventory, root VERSION link, frontmatter, descriptors, triggered links and launcher safety. |
| Catalog check | `PYTHONPATH=skills python -m pod.catalog --check` | Exact supported ids/efforts, source aliases, attributed guidance and provider sources. |
| Focused routes/refresh/workspace | `PYTHONPATH=skills python -m unittest tests.test_catalog tests.test_config tests.test_pin tests.test_selection tests.test_routes tests.test_route_upgrade tests.test_sources tests.test_observations tests.test_cli tests.test_workspace_state tests.test_tui_render -v` | Deterministic affected-domain checks; also included in the full unit gate. |
| Source hygiene | `python tools/source_audit.py .` | Tracked-source privacy and removed-mechanism guards; no package/tag/release publication path. |
| Recorded traces | `python tools/trace_check.py tests/fixtures/traces/*.json` | Deterministic record checks; a mapless trace is `NOT_EVALUABLE`, never kernel proof. |
| PTY/subprocess | `POD_REQUIRE_PTY=1 PYTHONPATH=skills python -m unittest tests.test_tui_pty tests.test_workspace_pty tests.test_sparse_toggle_pty tests.test_catalog_optional_pty -v` | Real terminal behavior and persistence. |
| Installer/copied bundle | `PYTHONPATH=skills python -m unittest tests.test_installer tests.test_bundle_install -v` | Shell entrypoints, interruption/recovery, parity and an independent placed bundle. |
| Disposable installer | `POD_INSTALL_SOURCE=file://… sh install.sh` in scrubbed temporary home; `--installed` parity and launcher checks | Dependencies, both skills, receipt, preferences, PATH and update. |
| SHA-pinned public install | Fetch `install.sh` from `raw.githubusercontent.com` at candidate SHA and matching `codeload.github.com` tarball in hosted CI | Public endpoints serve the candidate. |
| Hosted Linux | Every lane share of the `tests` matrix and the `checks` job for PRs and `main`; the required `linux` job always reports and passes only when all of them passed | Exact-commit hosted result. |
| Independent audit | Fresh reviewer, exact candidate and reproducible unprimed evidence | Findings and review proof under project governance. |
| Live core/delegation | Codex and Claude Code separately on Linux with disposable objectives | Native launch/effective route, supervision, request recovery, continuation, Delivery and release. |
| Public-source reads | One bounded real refresh in disposable `POD_CACHE_HOME`; manually compare representative rows, units and profile labels | Per-source availability and parser limits; a cached fetch is not fresh proof. |
| Project acceptance | Owner decision and merge/target checks | External acceptance and merged truth. |
| Post-merge public command | Literal `curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh \| sh` in a clean disposable home and authorized intended host | Distribution, version and receipt match merged `main`. |

`python tools/gates.py --output DIR` runs the local gates with the current
interpreter and `PYTHONPATH=skills`, recording checkout commit/dirty state, host,
UTC time, per-gate commands, exits and logs. `--sigint-ignored` ignores SIGINT in
children. The runner is a convenience; this contract defines the checks.
Copied-bundle checks run `pod --version`, `pod config --json` and `pod doctor --json`
outside the checkout without `PYTHONPATH`. The source archive must include the whole
bundle, and its placed bytes must match. Installer tests use a local tarball/HTTP
server, offline PyYAML wheel and npx double; they make no external request. Public
installation proof remains separate, including endpoint lag after merge.

## Test lanes

`tools/suite_lanes.py` routes each discovered test to one lane. A test that imports the
terminal harness (`tests/pty_harness.py`), directly or through a `tests` helper, runs in
`serial`, alone on its runner; one that imports the installer harness runs in `installer`;
every other test runs in `parallel`. A heuristic flags a test outside `serial` whose method,
or whose class's helpers, calls `monotonic`, `perf_counter` or `time` (any `_ns` form included) and whose
method calls `assertLess` or `assertLessEqual`; it misses other bounds, such as a timeout
argument, so authors classify those themselves. A flagged test needs an override in
`tests/suite_lanes.json` with a reason: `serial` needs no decision, and keeping it out of
`serial` needs the owner. Recorded CI durations of tests that take a second or more balance
each lane's shards and workers longest-first; an unrecorded test is planned at the measured
fast mean. Class and module fixtures are not timed, and a lane may repeat a class's setUpClass
in several workers, so keep them cheap. Every pull request and `main` push runs the full suite:
lanes decide where a test runs, never whether it runs. Every share computes the same plan from
the checkout, so test definitions must not depend on the runner; `refresh` refuses a run whose
shares disagree on the plan digest or commit identity, or have missing/inconsistent chunks, duplicate
listed test IDs, invalid timings or incomplete passing-test accounting. Identical chunk replay is
harmless. New logs also attest clean worker completion; earlier logs lack that extra evidence.
A complete earlier run may have a different shard topology from today's manifest.

New timing lines record `planned_worker_seconds` from that share's original checkout plan alongside
its wall seconds. Refresh stores every share's pair and plan/commit identity in optional manifest
`calibration`; each lane uses its maximum wall/planned ratio, floored at one. `check` reports the
raw largest worker, ratio and projected share wall time. It never reconstructs a planned denominator
from observed test seconds or the updated manifest. A earlier manifest without calibration uses one.
Earlier logs can still refresh durations and preserve previous calibration, but cannot create a
measurement without an independently verified original plan. The checked-in earlier calibration
is marked `verified_plan`; future direct log measurements use `timing_lines`. Ratios describe the
recorded run, including overhead, and do not guarantee future runner speed.

Each hosted lane share proves that every planned test ran exactly once and that each worker
finished cleanly after its last test, and fails on any skip. Suite-health limits bound hosted
CI time: a share fails for an unrecorded test over 2 s, any test over the 60 s ceiling, or a
recorded test at more than twice its recorded duration and 10 s over it, once timing the test
again alone confirms it (up to five per share, ceiling breaches and new slow tests first; past
that, those two fail unconfirmed and drift only warns);
it also fails beyond twice its lane target. These limits govern the suite's own runtime, not product performance. Record a
necessarily slow test from its pull-request run with `python tools/suite_lanes.py refresh --pr N`
and commit the manifest; `refresh` reads only that run's log lines. Split a growing table into
named group methods (`tests.common.GroupedTable`) before any method nears the ceiling. When
`check` projects a share over its target, raise that lane's shards in the manifest and add the
matching `ci.yml` rows, up to `max_parallel_shards` for parallel. Raw largest-worker overload remains
a hard finding. Below that parallel cap, calibrated share overload is also a hard finding. At the
parallel cap, calibrated-only overload is a warning naming projected seconds, target and residual;
it does not suggest another shard. Report actual hosted share times and any residual after the
bounded remedy. The existing hosted failure beyond twice the target stays unchanged. Budgets, lane targets,
`max_parallel_shards` and any override that keeps a flagged test out of `serial` change only by
owner decision.

## Evidence and review

R33, R41, R43, R87–R91 and [R173](spec/assurance.md#r173--keep-findings-goal-scoped-and-bound-review-rounds) define source, receipt, obligation, triage, review allowance and closure
contracts; R29/R98/R105 define exact native authority and continuity. Tests exercise
those production boundaries with disposable projects and injected ports. Seeded
stdlib generative checks test P1–P6 after each accepted transition and print
counterexamples; structural accounting and binding integrity do not certify progress
or review truth. Recorded trace counts and durations are observations, not gates.

One reuse rule applies: corrections affecting reviewed scope, assumptions,
dependencies or governance, or with unknown impact, receive affected-scope delta
review; bind genuinely unaffected evidence to the candidate through explicit REUSE
with the Git delta, honoring stricter project rules. Preserve findings and prior
results. Reviewers report findings and do not repair; one writer consolidates
corrections. Missing audit remains incomplete, and withdrawal never passes review.
A change to admission, assurance or closure is audited under the mechanism installed
before it. Proof produced by the changed mechanism about itself is labeled
self-hosting and cannot substitute for that audit.

Apply R173's goal-scoped findings, per-requirement reuse statement and three-round
allowance during review. Exercise report ingestion, review admission, checkpoint
question deduplication, sourced delegated lead decisions, current machine policy and unsourced/reviewer/worker refusals through their production
boundaries; fixture records stay separate from native and preceding-mechanism audit evidence.

For the lead allowance, feed actual candidate public status to the currently installed
host owner-question consumer in disposable host/Pod stores. Absent, present and timed-out
lead facts must create no owner request or claimable card through due, carried clean-clear,
answered/continue/simplify/stop, renewal and next-due transitions. Exercise carried, omitted,
empty and public-round-tripped inputs and forged ownership claims;
ordinary, earlier, tool/sensitive, policy, mixed and lookalike controls must still reach
the owner feed. Record the installed consumer hash. This compatibility fixture is
separate from live host notification proof; no real card or service operation is permitted.

If a post-merge trial fails, preserve exact evidence and leave the objective
incomplete. Distinguish product defects from environment/trial faults. Product fixes
use a linked corrective delivery unit, independent audit under the preceding
mechanism, full milestone gates, fresh authorization/hosted checks and affected
live trials. An unavailable mechanism calls for owner recovery, never blind retries.

The inventory check reads `docs/pod-spec.md` and all nested domain files, requires
unique numeric R/A ids, valid B/H/I types, resolving links, complete coverage rows
and existing offline test pointers. `offline_test` is a reference string, nonempty
unique reference list or `null`; all coverage rows remain `NOT_RUN` until evidence
is recorded. Content anchors preserve guidance; they do not prove behavioral
suitability. Genuine earlier-writer fixtures and synthetic controls retain their
provenance; neither is live proof. Hosted checkout fetches complete Git objects for
upgrade and genuine-writer tests.

The documentation contract is [R71](spec/interface.md#r71--maintain-the-documentation-contract).
Skill validation checks bundle structure and installed parity; the location-aware
anchor table checks that required topics occur once in their owning files. Independent
review checks the guidance against R71 for clarity, completeness, task coherence and
authoritative placement. Stored per-file word counts and their total are informational
and do not measure the context an agent loads. Observed runs, rather than static
checks, establish which references an agent actually read and why.
Metadata and VERSION restrictions are reported before body checks. Every reference
has one unique triggered router link and no reference-to-reference links. Runtime
guidance lives in the installed bundle's applicable task references, with authoring
templates serving authoring and revision; contributor checks live here.

## Containment and live boundary

Run broad gates with fresh process-scoped `POD_STATE_HOME`, `POD_CONFIG_HOME` and
`POD_CACHE_HOME` in disposable task storage. Test fixtures clear these locally and
restore them. Installer trials use a scrubbed fresh temporary HOME with every
base-directory variable contained or unset: XDG config/data/state/cache homes,
CODEX_HOME, CLAUDE_CONFIG_DIR, ZDOTDIR and Pod overrides. PATH excludes real launchers.
No real launcher, skill, rc, data, config or state may change. Failed isolation and
host-state recovery are separate evidence, never passing candidate proof.
Ordinary host/project trials are read-only; no production/provider mutation.

The PTY suite pins test-only pyte/wcwidth; `POD_REQUIRE_PTY=1` makes missing
prerequisites fail. R73–R76/R103–R104 define workspace behavior. Test frame widths
40–200 and short/tall heights, plus snapshots at 160×45, 100×30, 80×24, 60×20 and
40×12 in supported palettes, ASCII and NO_COLOR. Focus/save p95 must stay below
500 ms; external/refresh changes repaint within one second. Startup/import times
are recorded, not gated. Inspect real TUI and installer output as well as frames.

Live work uses authenticated included usage and disposable objectives, never probe
workers. R51/R65/R68 require honest effective model/effort and `native_default`
context; requested settings do not prove them. Missing launch capability blocks
delegation while safe direct diagnosis remains possible. Missing restart, provider,
safety/rate-limit or recognized-update-menu evidence stays `NOT_RUN`. The bounded
Codex update opt-out requires an exact live menu and displayed Skip for now control;
fixtures cannot prove that interaction. Owner scheduling/authority governs restarts.
Unobserved cost stays unknown; model labels, counts and local passes cannot infer it.
