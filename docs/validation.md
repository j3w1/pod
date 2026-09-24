# Pod validation

This is the 0.5.0 verification contract. Every result names the exact commit,
host, UTC date, command, outcome and sanitized evidence reference. Unit proof,
PTY/subprocess behavior, installed-bundle proof, hosted CI, live native proof,
visual review and independent review are separate labels. A check not exercised
is `NOT_RUN`; a passing fixture never promotes live or project acceptance.

## Checks

| Check | Command or evidence | What it proves |
| --- | --- | --- |
| Unit and incident suite | `PYTHONPATH=skills python -m unittest discover -s tests -v` | Current behavior with disposable synthetic fixtures. |
| Explicit incidents | `PYTHONPATH=skills python -m unittest discover -s tests/incidents -t . -v` | Incident cases are discovered. |
| Compile and whitespace | `python -m compileall -q skills tests tools`; `git diff --check` | Local syntax and changed-file whitespace. |
| Skill validation | `PYTHONPATH=skills python -m pod.skill_validation skills/pod` | Bundle inventory, root VERSION link, frontmatter, launcher forms and word budgets. |
| Catalog check | `PYTHONPATH=skills python -m pod.catalog --check` | Six exact identities, efforts, attributed guidance and coherent dated reference metrics. |
| Source hygiene | `python tools/source_audit.py .` | Tracked-source privacy and removed-mechanism guard; no Pod package, tag or release path. |
| PTY and subprocess | `POD_REQUIRE_PTY=1 PYTHONPATH=skills python -m unittest tests.test_tui_pty -v`; `PYTHONPATH=skills python -m unittest tests.test_installer -v` | Actual terminal and shell entrypoints, immediate persistence, responsive focus, install interruption and safe recovery. |
| Copied bundle | Run installed `pod --version`, `pod config --json` and `pod doctor --json` from an unrelated directory with no checkout or `PYTHONPATH` | One placed bundle works independently. |
| Disposable installer | `POD_INSTALL_SOURCE=file://… sh install.sh` in a scrubbed temporary home, then `--installed` parity and launcher checks | Real install, dependencies, both skills, receipt, preferences, PATH and update. |
| SHA-pinned public install | Download `install.sh` from `raw.githubusercontent.com` at the commit SHA and set `POD_INSTALL_SOURCE` to the matching `codeload.github.com` SHA tarball in hosted CI | Public endpoints serve the reviewed commit. |
| Hosted Linux | The applicable checks above in the single `linux` job on each PR and push to `main` | Hosted result for the exact commit. |
| Independent audit | Fresh candidate-bound reviewer with reproducible evidence | Findings only; corrections need re-review. |
| Live core matrix | Codex and Claude Code, each on Linux with disposable objective | Separate native install/discovery, in-session coordination, authorized worker start/effective route, supervision, request recovery, verification and interruption/adoption. |
| Orca delegation | Real worker through each advertised adapter | Request construction, launch identity/effective values, messaging, settlement, Delivery and release. |
| Project acceptance | Owner decision, merge and `main` checks | External acceptance and merged truth. |
| Post-merge public command | Literally run `curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh \| sh` in a clean disposable home and on the intended host | `main` distribution, installed version and receipt match the merged commit. |

The PTY suite runs with pinned test-only `pyte` and `wcwidth`; it may skip in a
local dev environment without them, but `POD_REQUIRE_PTY=1` makes missing
dependencies fail CI. The suite covers six focus-driven Details panels,
identity-preserving sort/filter, 80×24 and narrow/resized terminals, ASCII and
monochrome output, no results, help, invalid/missing YAML, save failure,
concurrent TUIs, toggle across restart and a non-TTY summary. The latency
measurement records p50/p95/max for focus/save and external refresh with kernel,
CPU, Python, ncurses, TERM, locale and terminal size; focus/save p95 must be
under 500 ms and refresh within one second. Visual review examines actual TUI
and installer terminal output, including the multi-orca banner.

The installer suite uses scrubbed disposable homes, a local tarball and HTTP
server, an offline PyYAML wheel and an npx test double; it makes no external
network request. It exercises first and repeated install,
update, custom profile paths, command collision, PATH absent/present, foreign or
changed files, invalid preferences, download and dependency failure, interruption,
concurrent installers and recovery. It does not touch ordinary host profiles.
The installed copy must match the bundle; an incomplete installation reports a
recoverable failure. The post-merge literal command is checked separately
because a cached `main` endpoint can lag the SHA-pinned CI endpoint.

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
`offline_test` names an existing deterministic test only; `null` identifies
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
matching Run/Task/Dispatch identities, a settled projection outcome and a
terminal Dispatch status. A failed stopped attempt frees its logical slot even
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
blind replacement; uncertain errors remain unresolved until readback.

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
