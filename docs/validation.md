# 0.6.5 verification contract

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
| Catalog check | `PYTHONPATH=skills python -m pod.catalog --check` | Six exact identities, efforts, attributed guidance and coherent dated reference metrics. |
| Source hygiene | `python tools/source_audit.py .` | Tracked-source privacy and removed-mechanism guard; no Pod package, tag or release path. |
| Recorded trace checks | `python tools/trace_check.py tests/fixtures/traces/*.json` | Deterministic record checks and observation-only counts; a trace without maps remains `NOT_EVALUABLE`, not kernel proof. |
| PTY and subprocess | `POD_REQUIRE_PTY=1 PYTHONPATH=skills python -m unittest tests.test_tui_pty -v`; `PYTHONPATH=skills python -m unittest tests.test_installer -v` | Actual terminal and shell entrypoints, immediate persistence, responsive focus, install interruption and safe recovery. |
| Copied bundle | Run installed `pod --version`, `pod config --json` and `pod doctor --json` from an unrelated directory with no checkout or `PYTHONPATH` | One placed bundle works independently. |
| Disposable installer | `POD_INSTALL_SOURCE=file://… sh install.sh` in a scrubbed temporary home, then `--installed` parity and launcher checks | Real install, dependencies, both skills, receipt, preferences, PATH and update. |
| SHA-pinned public install | Download `install.sh` from `raw.githubusercontent.com` at the commit SHA and set `POD_INSTALL_SOURCE` to the matching `codeload.github.com` SHA tarball in hosted CI | Public endpoints serve the reviewed commit. |
| Hosted Linux | The applicable checks above in the single `linux` job on each PR and push to `main` | Hosted result for the exact commit. |
| Independent audit | Fresh candidate-bound reviewer under the installed mechanism preceding the change, with reproducible evidence | Findings only. Corrections affecting reviewed scope, assumptions, dependencies or governance, or with unknown impact, receive affected-scope delta review. Bind unaffected evidence to the candidate through explicit REUSE with the Git delta; honor stricter project rules. |
| Live core matrix | Codex and Claude Code, each on Linux with disposable objective | Separate native install/discovery, in-session coordination, authorized worker start/effective route, supervision, request recovery, verification and interruption/adoption. |
| Orca delegation | Real worker through each advertised adapter | Request construction, launch identity/effective values, messaging, settlement, Delivery and release. |
| Project acceptance | Owner decision, merge and `main` checks | External acceptance and merged truth. |
| Post-merge public command | Literally run `curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh \| sh` in a clean disposable home and on the intended host | `main` distribution, installed version and receipt match the merged commit. |

`python tools/gates.py --output DIR` runs the local command-table gates with the
current interpreter and `PYTHONPATH=skills`, writing a JSON summary and per-gate
logs. `--sigint-ignored` runs each child with SIGINT ignored. The table above
defines the gates; the runner is a convenience for a checkout.

The inventory check reads `docs/pod-spec.md` and `docs/spec/*.md` together. It
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
before 0.6. After merge and installation, a separate 0.6 disposable objective
records an assurance obligation, review attempt, triage and label decision as
self-hosting evidence; it cannot promote stage-1 evidence.

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
dependencies fail CI. The suite covers six focus-driven Details panels,
identity-preserving sort/filter, 80×24 and narrow/resized terminals, ASCII and
monochrome output, no results, help, invalid/missing YAML, save failure,
concurrent TUIs, toggle across restart and a non-TTY summary. The latency
measurement records p50/p95/max for focus/save and external refresh with kernel,
CPU, Python, ncurses, TERM, locale and terminal size; focus/save p95 must be
under 500 ms and refresh within one second. Visual review examines `tests/tui_snapshot.py` SVG/PNG output at 160×45,
100×30, 80×24, 60×20 and 40×12 in pink, dark and light palettes, plus
NO_COLOR, ASCII, expanded detail and help. Inspect actual TUI and installer
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
An update trial begins with an installed 0.6.4 copy in a scrubbed disposable
home, applies the 0.6.5 installer, checks user-file preservation and verifies
the installed bundle, launcher and receipt.

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

A valid sparse custom preference map remains usable for its explicitly eligible
models, survives All models → My selection unchanged, and reports omitted ids as
“Not set (not eligible)” in custom mode. A missing file or
invalid YAML/required structure has no eligible pool. These are distinct test
cases; neither permits an accidental All models selection.

Benchmark rows are optional reference observations. A missing or incomplete row
is shown as unknown and does not block a supported model, config read or
admission; `pod.catalog --check` names a maintenance warning. Private helpers
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
