# 0.8.1 verification contract

 Every result names the exact commit,
host, UTC date, command, outcome and sanitized evidence reference. Unit proof,
PTY/subprocess behavior, installed-bundle proof, hosted CI, live native proof,
visual review and independent review are separate labels. A check not exercised
is `NOT_RUN`; a passing fixture never promotes live or project acceptance.

## Checks

| Check | Command or evidence | What it proves |
| --- | --- | --- |
| Unit and incident suite | `PYTHONPATH=skills python -m unittest discover -s tests -v` | Current behavior with disposable synthetic fixtures. |
| Explicit incidents | `PYTHONPATH=skills python -m unittest discover -s tests/incidents -t . -v` | Incident cases are discovered. |
| Compile and whitespace | `python -m compileall -q skills tests tools`; `git diff --check $(git hash-object -t tree /dev/null) HEAD` | Local syntax and whole-candidate whitespace. |
| Skill validation | `PYTHONPATH=skills python -m pod.skill_validation skills/pod` | Bundle inventory, root VERSION link, frontmatter, launcher forms and word budgets. |
| Catalog check | `PYTHONPATH=skills python -m pod.catalog --check` | The `pod-catalog/v3` registry: exact supported ids and efforts, unambiguous source aliases, attributed guidance and provider sources. |
| Focused route, refresh and workspace tests | `PYTHONPATH=skills python -m unittest tests.test_catalog tests.test_config tests.test_pin tests.test_selection tests.test_routes tests.test_route_upgrade tests.test_sources tests.test_observations tests.test_cli tests.test_workspace_state tests.test_tui_render -v` | Registry, preferences, exact pin, admission validation, the joined projection, upgrade setup, bounded public-source reads and refresh, CLI and workspace state with local fixtures, controlled time and local HTTP servers only. |
| Source hygiene | `python tools/source_audit.py .` | Tracked-source privacy and removed-mechanism guard; no Pod package, tag or release path. |
| Recorded trace checks | `python tools/trace_check.py tests/fixtures/traces/*.json` | Deterministic record checks and observation-only counts; a trace without maps remains `NOT_EVALUABLE`, not kernel proof. |
| PTY and subprocess | `POD_REQUIRE_PTY=1 PYTHONPATH=skills python -m unittest tests.test_tui_pty tests.test_workspace_pty tests.test_sparse_toggle_pty tests.test_catalog_optional_pty -v`; `PYTHONPATH=skills python -m unittest tests.test_installer -v` | Actual terminal and shell entrypoints, immediate persistence, responsive focus, install interruption and safe recovery. |
| Copied bundle | Run installed `pod --version`, `pod config --json` and `pod doctor --json` from an unrelated directory with no checkout or `PYTHONPATH` | One placed bundle works independently. |
| Disposable installer | `POD_INSTALL_SOURCE=file://… sh install.sh` in a scrubbed temporary home, then `--installed` parity and launcher checks | Real install, dependencies, both skills, receipt, preferences, PATH and update. |
| SHA-pinned public install | Download `install.sh` from `raw.githubusercontent.com` at the commit SHA and set `POD_INSTALL_SOURCE` to the matching `codeload.github.com` SHA tarball in hosted CI | Public endpoints serve the reviewed commit. |
| Hosted Linux | The applicable checks above in the single `linux` job on each PR and push to `main` | Hosted result for the exact commit. |
| Independent audit | Fresh candidate-bound reviewer under the installed mechanism preceding the change, with reproducible evidence | Findings only. Corrections affecting reviewed scope, assumptions, dependencies or governance, or with unknown impact, receive affected-scope delta review. Bind unaffected evidence to the candidate through explicit REUSE with the Git delta; honor stricter project rules. |
| Live core matrix | Codex and Claude Code, each on Linux with disposable objective | Separate native install/discovery, in-session coordination, authorized worker start/effective route, supervision, request recovery, verification and interruption/adoption. |
| Orca delegation | Real worker through each advertised adapter | Request construction, launch identity/effective values, messaging, settlement, Delivery and release. |
| Public-source reads | One bounded real `pod models refresh` into a disposable `POD_CACHE_HOME`, with a manual check of representative exact rows, units and profile labels against each page | Source-by-source availability, unavailable fields and access or parser limits. Labelled separately from fixtures; a cached fetch is never fresh proof. |
| Live exact routes | In authorized disposable objectives, normal Pod/Orca launch on both adapters with effective model and effort readback, report and release, including the new Sonnet 5.5 and GPT-6.1 Sol identities where advertised | Effective exact-route evidence. Every route is not tested; refresh starts no probe workers. |
| Project acceptance | Owner decision, merge and `main` checks | External acceptance and merged truth. |
| Post-merge public command | Literally run `curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh \| sh` in a clean disposable home and on the intended host | `main` distribution, installed version and receipt match the merged commit. |

`python tools/gates.py --output DIR` runs the local command-table gates with the
current interpreter and `PYTHONPATH=skills`, writing a JSON summary (its checkout's
commit and dirty state, host and UTC time) and per-gate logs. `--sigint-ignored` runs each child with SIGINT ignored. The table above
defines the gates; the runner is a convenience for a checkout.

The inventory check reads `docs/pod-spec.md` and every `docs/spec/**/*.md`, including
the nested model files, and sorts `R\d{2,3}` and `A\d{2,3}` ids numerically. It
requires one definition per requirement and scenario, valid B/H/I types,
resolving scenario and relative document links, complete coverage rows and real
offline test pointers. A document assertion cannot certify a behavioral scenario.

The 0.6 kernel has focused boundary tests for R81–R91 and A143–A163:
provenance, independently selected target governance and base-revision refresh;
trusted initial target selection, guarded candidate-containing base updates,
exact-snapshot user authorization, retained candidate/decision history, shared
intermediate ancestry, unresolved candidate refusal before a write, and direct-user
target retarget refusal;
all obligation states and
wait referents; one coordinator slot and boundary ownership; Git-derived
result paths including over-limit refusal before boundary decisions, ancestry or
noncommit attestation, discard then replacement; immutable settled report replay,
fresh review attempts, objective-bound route holds, normalized partial launch
observations, assurance triage, affected-scope delta review and explicit REUSE; quiescence,
closure and direct-user reopen; and hard cutover of older objectives. Seeded
stdlib generative tests check P1–P6 after every accepted transition and print
counterexamples on failure. P1 checks structural obligation accounting, not
eventual progress. P5 checks binding integrity, not review truth. Late-report and finding-overflow regressions preserve required corrections and
order proof by accepted map history independently of wall-clock timestamps.
Receipt integrity regressions exercise pure kernel and production checkpoint/admission/report/acceptance boundaries: omission of an
accepted assurance, candidate metadata edits, changed definitions on stable ids,
fresh qualifying attempts, ordinary receipt relabeling/restoration, source and
environment invalidation, governance refresh, explicit unaffected Git REUSE,
unrelated steering, authorized withdrawal and new uncovered risk. Immutable
receipt history stays inside the map and refuses overflow without eviction.
Definition identity remains separate from natural-language outcomes and test
names. Scripted record checks run the slot-filling, serialization, amplification, churn and
scope-inflation cases on sanitized traces; counts and durations are
observations, not gates. A150 requires two accepted writes, positive delegation
availability, free capacity, and no bounded current-input rationale/revisit
binding; capacity below the ceiling is invalid. Zero ceiling or unknown or
unavailable delegation suppresses that flag.

Live Codex and Claude trials additionally exercise A143, A150, A158 and A159
in disposable objectives. A missing live case remains `NOT_RUN`. Stage-1
review is the fresh final independent audit under the mechanism installed
before the change; for 0.7.0 that is installed 0.6.7, and for the 0.7.1 routing-guidance
correction it is installed 0.7.0; for 0.8.0 A2/A3/A4/A8 it is installed 0.7.1. For 0.7.0, preference/pin
precedence, trust separation and the preference cutover are reviewed as
governance changes. After merge and installation, a separate disposable
objective on the new version records an assurance obligation, review attempt,
triage and label decision as self-hosting evidence; it cannot promote stage-1
evidence.

If stage 2 fails, preserve the failing exact evidence and keep the objective
incomplete. Classify product defects separately from environment or trial
faults and do not retry blindly. A product defect creates a linked corrective
delivery unit and follow-up PR from current `main`; the first post-merge patch
uses the next root `VERSION`. Review the correction independently under the mechanism installed
before it, run full milestone gates plus affected regressions, obtain fresh
candidate authorization and hosted CI, reinstall corrected public `main`,
then repeat failed and affected live and self-hosting cases. If the defective
mechanism cannot support a required gate, use safe diagnosis and owner
recovery. Keep earlier stage-1 evidence distinct.

The PTY suite runs with pinned test-only `pyte` and `wcwidth`; it may skip in a
local dev environment without them, but `POD_REQUIRE_PTY=1` makes missing
dependencies fail CI. The suite drives the real model workspace: palette and
keyboard navigation, state, Preferred, Pin and bulk actions, hidden selections,
identity-preserving sort and filter, the bottom inspector at 160×45, 100×30,
80×24, 60×20 and 40×12 including the short-height dock, resize, ASCII and
monochrome output, no results, help, invalid and missing YAML, route setup,
save failure, concurrent workspaces, external edits and the non-TTY table.
Pure workspace state and frame tests (`tests.test_workspace_state`,
`tests.test_tui_render`) cover sorting, grouping, filters, compare, the
frontier, changed data, bulk scopes, the setup screen and automatic refresh with
controlled time, without a terminal. The latency
measurement (`python -m tests.pty_harness measure`) records p50/p95/max for focus,
save, external-change repaint and refreshed-data repaint (`data_refresh`), plus
`startup_to_first_frame` and `import_pod_tui`, with kernel, CPU, Python, ncurses, TERM, locale
and terminal size. Focus and save p95 must be under 500 ms, and an external
change or refreshed data must repaint within one second; startup and import
times are recorded for the footprint review, not gated. Visual review examines `tests/tui_snapshot.py` SVG/PNG output at 160×45,
100×30, 80×24, 60×20 and 40×12 in pink, dark and light palettes, plus
NO_COLOR, ASCII, each inspector tab, compare, the bulk preview, route setup and
help. `frame(strict=True)` and a 40–200-column sweep at short and tall heights
check that no line overflows, and no layout uses a right-hand panel. Inspect actual TUI and installer
terminal output, including the multi-orca banner.

The installer suite uses scrubbed disposable homes, a local tarball and HTTP
server, an offline PyYAML wheel and an npx test double; it makes no external
network request. It exercises first and repeated install,
update, custom profile paths, command collision, PATH absent/present, foreign or
changed files, invalid preferences, download and dependency failure, interruption,
concurrent installers and recovery. It does not touch ordinary host profiles.
The installed copy must match the bundle; an incomplete installation reports a
recoverable failure. The post-merge literal command is checked separately
because a cached `main` endpoint can lag the SHA-pinned CI endpoint.
An update trial begins with an installed genuine 0.6.7 copy, built from Git
object `ebec0f5257fc1673859caec09e85e42a4d4d64a1` with its own `install.sh`, in a
scrubbed disposable home. It places a genuine 0.6.7 preference file, updates to
the candidate, checks that the preference bytes survive and are reported as
`setup_required`, then applies the explicit route setup and verifies the new
file, the kept `config.yaml.pod-v1` copy, the installed bundle, launcher and
receipt. The genuine 0.6.7 parser, run in its own package from the same Git
objects, refuses `pod/v2`. The preference fixtures under
`tests/fixtures/pod-0.6.7/` were written by the genuine 0.6.7 writer. The public
0.6.5 and 0.6.6 upgrade tests also read genuine Git objects; hosted checkout uses
`fetch-depth: 0` to make that history available.

Issue 29 focused regressions cover normal sourced final-audit intake through production checkpoint/admission/report/acceptance, pin storage/atomic edits and every supported role, immutable pending replay, and controlled-time local failure reconsideration/success. PTY tests cover moving and clearing the Pin and Preferred route, focus, restart, hidden selections, invalidating-edit refusal, concurrency, resize and ASCII/monochrome. Existing `tests.tui_snapshot.render` accepts the normal configured PTY screen for visual review; no special pin capture framework is needed.

Public 0.6.4 consent fixtures under `tests/fixtures/pod-0.6.4/` were emitted by exact public main `372bcfc60845b5d49aa2f355023e33c47212aaf4` with native-authority/Git-observation stubs. They prove released-writer representability, not an installed-launcher/live/owner-state reproduction. Production current checkpoint/wait tests cover both unbound prepared and default/missing units; synthetic current-target controls remain labeled separately. Existing A147 tests retain legitimate trusted source removal and history controls. No migration or policy parser is introduced.

Pod 0.7.0 focused regressions cover exact routes. `tests.test_catalog` checks the
`pod-catalog/v3` registry, exact aliases and removed `ultra` (PoD#1);
`tests.test_config` and `tests.test_pin` check route states, Preferred, Pin,
invalidating edits, bulk scope and concurrent saves (PoD#2); `tests.test_operations`
and `tests.test_issue29_boundaries` check exact pin enforcement for every role,
correction and reused terminal, readback mismatch, preference races, frozen and
submitted replay, and observation refresh, eviction or absence during selection
through the production admission path (PoD#3, PoD#5); `tests.test_route_upgrade`
and `tests.test_installer` check the genuine 0.6.7 upgrade and setup (PoD#4);
`tests.test_sources` and `tests.test_observations` check bounded public reads,
parsers, metric integrity and the refresh transaction with sanitized fixtures,
local HTTP servers and controlled time (PoD#7–#10); `tests.test_routes` checks the
joined projection and discovery (PoD#11); `tests.test_cli` checks the commands and
runs config, status, doctor and the route views under a socket guard (PoD#10,
PoD#16); and the workspace tests above cover PoD#12–#15. Public-source reads
(PoD#21), live exact routes (PoD#22) and installed proof (PoD#23) are separate
evidence and stay `NOT_RUN` until exercised.

Pod 0.6.7 focused regressions cover A1–A5. A1 status tests compare a verified
read with the exact 0.6.6 `status.py` read from Git object
`f4101feb8e517382cb4cc626a1afc702c6429f37` on the same fixture, including a
checkpoint whose real authority join recorded the consumer generation, and show
unverified rows, the failed read's code, both runtimes and Orca-labelled
attention for a changed runtime without a write (R97, A192–A194). A2 tests drive
the real authority join through a fake Orca port that reports the current-Run
binding: proven rebinds for a checkpoint, Governor mutation, report read, new
admission and reserved-admission recovery; byte-identical refusals for each
ambiguous case; exact-scope Owner decisions reclassified at write time; every
compared identity disproven with the path's existing code; no start, replay,
Run creation, adoption or owner change; the eight-entry history bound; a bind
whose worker read preceded a Dispatch-destroying runtime change refusing as in
0.6.6, also after another call's rebind, and a report read before another
call's rebind refusing without consuming; and a Governor reconcile rebind
written while the objective lock is held (R29, R98, A195–A199). Sanitized R15
and R17 incidents keep only counts and
which identities changed. A4 runs every `pod internal` operation through the
helper entry with a sentinel value (R99, A200–A201). A5 uses disposable Git
repositories and injected issue ports (R62–R64, A202–A204). Live native A1 and
A2 need an Orca restart in a disposable objective the Owner schedules (on this
host a restart ends live sessions and asks DRAINED), and live A5 needs a
coordinator handed an EEL URL in a disposable repository; each stays `NOT_RUN`
until exercised.

Governor 0.6.6 journal fixtures under `tests/fixtures/governor-0.6.6/` were
emitted by the unmodified 0.6.6 writer at
`f4101feb8e517382cb4cc626a1afc702c6429f37` with stubbed native authority and an
in-memory GitHub port carrying synthetic run events. They prove released-writer
representability for A3's exact CI proof identity (A187–A191), not hosted or
live provider behaviour. One UNKNOWN-outcome variant is a labelled synthetic
control. A3's hosted or live proof needs a project with selective pull-request
CI and remains `NOT_RUN`.

## Boundaries and evidence

For disposable validation, `POD_CONFIG_HOME` may name an absolute directory
containing `config.yaml`, and `POD_STATE_HOME` an absolute Pod state directory.
They are process-scoped Pod-only overrides, not changes to `CODEX_HOME`,
`CLAUDE_CONFIG_DIR` or Orca's native profile. Use fresh owned directories; an
existing ordinary profile does not become disposable because an override exists.
Inferred native homes must be absolute and outside known linked worktrees,
including redirected components Pod would create. Project policy remains in its
canonical project/worktree authority and cannot become model preferences.

Repository/worktree identity uses Git reads that do not infer cleanliness or
trigger optional index updates. Dirtiness is unknown unless separately observed.
Issue fixtures use disposable Git repositories and injected read ports; they do
not prove private GitHub access or authorize writes. A live issue read retrieves
the full body, verifies target and digest, and rechecks material amendments.

The [scenario coverage file](pod-coverage.json) has one current scenario per row.
Its single `offline_test` authoring field may be a test reference string, a
nonempty list of unique existing deterministic test references, or `null` when
no offline test pointer is recorded. `null` identifies
work scheduled in a later milestone or evidence that requires another kind of
check. All rows stay `NOT_RUN` until actual evidence is recorded. A requirement
change updates its scenario and coverage row in the same commit.

A valid sparse or empty `pod/v2` route map remains usable for its explicitly
enabled routes and reports omitted routes as “not set (not eligible)”. A missing
file, invalid YAML or required structure, an unsupported route key and an invalid
Preferred or Pin have no eligible pool, and a kept `pod/v1` file is
`setup_required`. These are distinct test cases; none permits an accidental
all-routes authorization.

Public observations are optional dated facts. A missing or unknown metric is
shown as unknown and never blocks a supported route, config read, selection or
admission. Tests never rely on live website stability: sources use sanitized
fixtures under `tests/fixtures/sources/`, local HTTP servers and controlled time,
and observation refresh or cache eviction during selection is exercised through
the production admission path. Private helpers
accept `--input -` for bounded JSON on stdin, while a path such as `/dev/stdin`
remains subject to regular-file checks. A source-absence refusal names the
missing input and tells the coordinator to put future output files in scope.

Instruction budgets are whitespace-delimited: SKILL at most 750 words, each
conditional reference at most 700, all references together at most 2200.
The Execution Spec reference loads only for issue/spec work.

Automatic source and packet-reference reads exclude credential path classes:
`.env*`; `.ssh`, `.secrets`, `secrets`, `credentials`, `.credentials`, `.aws`,
`.azure`, `.kube`, `.docker`, `.gnupg`, `.password-store`; `.netrc`, `_netrc`,
`.npmrc`, `.pypirc`, `.git-credentials`, `.authinfo`, `.authinfo.gpg`, `.pgpass`,
`pgpass.conf`, `.my.cnf`, `.dockercfg`, `auth.json`, `auth.yaml`, `auth.yml`,
`credential.json`, `credentials.json`, `credentials.yaml`, `credentials.yml`,
`token.json`, `tokens.json`; `.config/gcloud`, `.config/gh`,
`.local/share/keyrings`; `.key`, `.pem`, `.p12`, `.pfx`; and private-key
basenames `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519` with common separators.
An otherwise safe `.pub` basename is allowed. These name classes are
conservative exclusions, not a claim to detect all secrets.

Admission checks frozen packet sources under the objective lock. Changed or
absent sources reject that assignment; unavailable reads hold it until bytes
can be bound. The check does not promise an atomic snapshot against external
writers. `pod-context/v4` stores compact policy/evidence admissions: `reserved`,
`bound`, `unresolved`, `closed`, `deferred`. Exact native settlement requires
matching Run/Task/Dispatch identities, a settled projection outcome and the
Dispatch's own terminal status. A projected stage status may be absent but,
when present, must agree. A failed stopped attempt frees its logical slot even
when its stage detail is `process_stopped`; retained or released resources do
not decide settlement. Active, unverifiable and undocumented abandoned
attempts remain outstanding. Orca owns worker/resource occupancy and all
lifecycle mutations.

Request recovery starts with Orca's UUID. A completed request binds its
receipt; pending replays the same request with its original route; absent
permits only unique exact Run/Task/Dispatch readback. Pending replay checks
current authority, runtime, placement, issue body and checkpoint core, but
**does not re-check current model preferences**. A changed preference after
row persistence governs only later starts. Invalid UUID, contradictory receipt
or ambiguous native attempt holds. Effect-free native refusals defer without a
blind replacement; uncertain errors remain unresolved until readback. Native start
responses, including unknown refusals, malformed output, timeouts and unexpected
exit statuses, retain immutable private evidence. Recovery checks its integrity;
no test or new recording can reconstruct unavailable earlier responses. A native
contact failure cannot prevent recording facts for the existing owned reservation.
If the archive write fails but the journal remains writable, retain the observed
UUID and report the missing evidence. A storage failure never grants effect
authority. The evidence bound counts distinct observations, not retries; Pod
adds no retry counter or automatic retry.

The Governor's deterministic local kernel keeps ALLOW/REUSE/DEFER, candidate
binding, preflight, CI reuse, bounded failure classification and safe
supersedence. `policy_revision` reflects effective Governor policy only, so a
model edit alone does not supersede a candidate. Governor mutations require a
stable exact native current-Run/coordinator/generation binding; read-only status
can diagnose without it. Project authorization remains separate from technical
readiness. Unknown cost stays unknown; reuse and deferral counters are not an
estimate of savings.

Each checkpoint records the running root version and the installer's canonical
bundle digest. A same-version bundle change, or an older checkpoint without a
digest, blocks new admission and Governor mutation as
`installed_version_changed`; status names the checkpoint and running identities,
and doctor names the running and receipt identities. Existing request recovery
stays available. A fresh authorized checkpoint after skill reload and a new
`pod config --json` read establishes the new identity.

## Live runtime boundary

The installed Orca worker contract supplies per-worker model and effort
preferences, plus request and worker readback, but no scoped context flag.
Pod uses `native_default` for context, omitting any invented flag. If actual
context is inadequate, narrow the packet or decompose the assignment.
Requested settings are not effective proof; missing effective values stay
unknown and a mismatch blocks acceptance. Missing launch-preferences capability
blocks delegation while safe direct work and diagnosis remain available.

Live trials use disposable objectives and authenticated included usage. They
do not mutate production or provider settings. The core matrix covers Codex
and Claude Code separately; each advertised adapter needs a real native worker
and exact effective-route/request evidence. Routine questions, preference
changes before and after dispatch, Delivery order and continuation are observed.
Rate limits, lost replies, safety refusals and provider prompts remain
`NOT_RUN` unless genuinely observed; offline fixtures are labeled separately.


The 0.8.0 production offline matrix is `tests.test_issue41`. It exercises A222–A233
through internal, status and doctor entrypoints in disposable Git projects,
replacing only Orca/GitHub ports. Genuine 0.7.1 records are emitted at test time
by the archived Git-object writer described in
`tests/fixtures/pod-0.7.1/provenance.md`; restored records are byte copies, not
hand-edited approximations. The full-id proof fixture includes criterion,
subgoal, correction with its old finding shape, and assurance. Its abbreviated
counterpart invalidates proof on full-id restatement. The old journal validator
also reads the new reservation marker. These are offline compatibility facts,
not live native or self-hosting review evidence.

Authoring keeps SKILL at most 748 words, leaving two words of formal 750-word
validator headroom for the unchanged duplicate-VERSION guard. The production
matrix checks that effective budget, each 700-word reference and their combined
2,200-word bound. Independent 0.8.0 audit remains under installed 0.7.1; A2,
A3, A4 and A8 receive admission/assurance scrutiny before acceptance.

The 0.8.1 production offline matrix is `tests.test_owner_handoff` (A234–A250).
Disposable Git objectives enter through real checkpoint/admission joins; Orca
alone is replaced at its port. Related detection and transition rows share
state invariants: only pending/history and authorized completion fields may
change, and Governor/other-objective bytes remain identical. Genuine 0.8.0
reader proof comes from the complete bundle archived from merged Git object
`86d8b90db7c1d0f6a38c3c9167b950548ff79fad`, with its own unchanged status and
context validator; no reconstructed older parser is used. Original R98 and R15
incident controls and the full 0.8.0 offline suite stay separate regressions.
No terminal-show pane or presentation fields enter handoff evidence.
The closure table also emits completed review peers through real admission and
report consumption, checks satisfied and withdrawn assurances, and corrupts
each consumed-report field and its native observation digest. Durable settlement
is used only for an already recorded closure; ordinary boundary readback and
unreported bound, reserved and unresolved admissions remain unchanged.
Related retained-history siblings vary withdrawal, optional acceptance/report
markers, review role and exact served assurance, duplicate/order contradictions,
and readable immutable REUSE targets/deltas. Genuine unknown/unconsumed attempts
and retained REUSE remain positive controls. Genuine no-verification succeeded/failed review observations
and unconsumed cross-role/cross-assurance references additionally exercise ordinary
carry-forward, restatement, coordinator/user withdrawal and fresh/pending peers;
readability never promotes them to qualifying assurance proof. A separate bounded continuity table
checks runtime strings and malformed lineage envelopes before fingerprint
collection through JSON/text status and every authority boundary, fresh and pending.
The direct Owner hold decision additionally checks genuine wider report scopes,
authorized scope/question changes and non-assurance proof_scope changes: ordinary
reads/writes/closure stay valid, while Run-sharing peer exclusion holds with named
unverifiable history in fresh and pending handoff. Confirmation cannot override
the hold, pending stays pending, and all work/peer/Governor bytes remain unchanged.
This changes the prior changed-definition handoff expectation only; its earlier
passing evidence is retained separately, with no additional A243 exception.

R105 is an authority/governance change: the fresh final independent audit uses
installed 0.8.0, never candidate 0.8.1 evidence about itself. Corrections receive
affected-scope delta review and explicit reuse under this contract. Real
coordinator loss and restart proof requires an Owner-scheduled disposable trial;
missing stale/gone and real restart evidence stays NOT_RUN. Local gate success,
hosted Linux, independent review, self-hosting proof and Owner acceptance remain
separate.

The unseen referenced-Run subcase of A243 is the sole explicit exception to
production/released-writer fixture origin: it is a labeled synthetic defensive
restored-record control. Supported writers preserve singleton references; genuine
pre-guard multi-reference records are superseded and are never converted. The
control is not genuine-writer, live or acceptance evidence. All other B1 rows
retain their production-boundary or genuine archived-writer requirements.

The shared fixture defaults to its synthetic recorded owner, with explicit
different/absent callers retained. Stopall tests prove caller and home restoration.
Run broad local checks with fresh process-scoped POD_STATE_HOME, POD_CONFIG_HOME
and POD_CACHE_HOME in disposable task storage as an outer containment layer;
fixtures clear them locally and restore them afterward. Failed isolation runs
and host-state recovery are separate evidence, never passing candidate proof.
