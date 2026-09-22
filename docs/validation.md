# Pod validation

Each result binds an exact commit and Git tree, host, UTC date, command, outcome and sanitized report reference. Commit and tree values are full lowercase Git object IDs (40 hexadecimal characters for SHA-1 repositories or 64 for SHA-256 repositories). One selected binding or validation record uses one repository-wide object format, so its commit and tree IDs must have the same width. The timestamp is canonical RFC 3339 UTC with a trailing `Z`, including seconds and at most six fractional-second digits. The report value is a repository-neutral relative artifact identifier followed by ` sha256:` and the artifact's full 64-character lowercase SHA-256 digest; the identifier begins with an ASCII letter or digit, and its path components may otherwise use ASCII letters, digits, `.`, `_`, and `-`, but must not be empty, `.` or `..`. Offline fixtures do not prove live Orca, provider behaviour, hosted CI, independent review or acceptance. All new-candidate rows start `NOT_RUN` until a matching result is recorded.

## Required candidate checks

| Gate | Command or evidence | Current boundary |
| --- | --- | --- |
| Unit and incident suite | `PYTHONPATH=skills python -m unittest discover -s tests -v` | Disposable synthetic fixtures |
| Explicit incident discovery | `PYTHONPATH=skills python -m unittest discover -s tests/incidents -t . -v` | Tests must actually be discovered |
| Compile and diff | `python -m compileall -q skills tests install.py`; `git diff --check 474a84a6d5a1f7947abc1e38d232c379adf7ff93 HEAD` | Local syntax and exact baseline-to-candidate whitespace |
| Supported environment audit | `python tools/platform_audit.py` | No unsupported-platform implementation in the tracked product |
| Artifact audit | `python tools/artifact_audit.py dist/*` | No personal path, account, runtime identifier, credential or retired-platform reference in a published artifact, outside the files that are history |
| Skill validation | `PYTHONPATH=skills python -m pod.skill_validation skills/pod` | Bundle inventory, frontmatter allowlist, helper invocation forms and references |
| Bundle parity | `python -m pod.skill_validation --wheel dist/*.whl`; `--installed PATH` for a placed copy | The wheel and every placed copy carry the tracked bundle's exact bytes |
| Frozen build | `git archive HEAD` into a disposable cache directory, then build a wheel and sdist there | Exact committed candidate |
| Fresh isolated install | Install the wheel into a new disposable venv; smoke `pod --help`, each family help, `pod config --check --json`, `pod doctor --json`, then `--installed` parity | No model or Orca mutation |
| Copied-bundle form | Run `scripts/pod.py doctor --json` from a copy, in an unrelated directory, with no `PYTHONPATH` and no checkout | The installed skill needs nothing outside itself |
| Skills-CLI install | `npx skills@1.7.0 add SOURCE --skill pod -a codex -a claude-code -g -y` in a disposable home, with `DISABLE_TELEMETRY=1` | The documented installation actually works, and `pod setup` then reports it as externally managed |
| Hosted Linux | The same suite, incident discovery, audit, frozen build, installs and smokes in one candidate-bound job | Candidate-bound CI |
| Independent audit | Fresh reviewer of the exact commit and tree with reproducible evidence | Findings only |
| Live core matrix | Codex and Claude Code, each on Linux: discovery, in-session coordination, authorized native worker and effective route, lifecycle, verification and adoption | Separate live authorization and a disposable project |
| Orca delegation | Each advertised worker adapter: request construction, account and authentication selection, launch identity, effective launch, delivery, settlement and release | Production adapter against the installed runtime |
| Project acceptance | Owner governance, merge, release and any publication decisions | External |

For disposable validation, `POD_CONFIG_HOME` may name an absolute directory containing
`config.yaml`, and `POD_STATE_HOME` may name an absolute Pod state directory. These
process-scoped Pod-only overrides reject empty, relative, or existing non-directory values;
they do not change the default personal locations, project YAML authority, `CODEX_HOME`,
`CLAUDE_CONFIG_DIR`, or Orca's native profile environment.
Child Orca processes inherit that native profile unchanged. Use fresh owned directories;
the overrides do not make an existing directory disposable.

A host may point `CODEX_HOME` somewhere other than the agents home, as one running Codex
inside another tool does. Global setup therefore also looks at `~/.agents/skills/pod`, where
the skills ecosystem keeps its canonical copy, and reports `present_elsewhere` rather than
installing a second active copy beside it. Which copy wins is the operator's decision, not
something setup makes by writing.

Inferred native homes (`XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `CODEX_HOME` and
`CLAUDE_CONFIG_DIR`) must be absolute directories outside the current project,
both lexically and after resolving existing redirects. A profile home may itself be a symlink,
as it commonly is on an ordinary machine; only the components Pod would create beneath it must
be unredirected. Configuration and state access and global setup fail before writes when that
boundary is not proven. Explicit absolute `POD_CONFIG_HOME` and
`POD_STATE_HOME` remain Pod-only disposable overrides, and local project-scope setup intentionally
writes its owned integration beneath the project.

The explicit installer requires pip 22.3 or newer on the invoking interpreter, creates the
selected environment without pip, and uses isolated bootstrap pip's documented `--python`
option to manage the exact target interpreter, with script-location warning traversal
disabled. Process-only controls remove inherited Python and pip control variables, disable
all pip configuration files for both pip phases and their target re-exec, and disable the
user site. Before success, the target interpreter runs in isolated mode and verifies the
installed `j3w1-pod` distribution and `pod` import; no system pip upgrade or profile change
is attempted.

The [scenario coverage file](pod-coverage.json) lists every acceptance scenario. Its test paths identify intended offline cases; the file itself proves only inventory integrity. Candidate-bound live and hosted rows remain `NOT_RUN` until actually exercised, and a retired scenario is recorded as `RETIRED` with its reason rather than as a pass.

Pod's automatic source and packet-reference boundary excludes conventional credential classes before opening a source: any `.env*` component; `.ssh`, `.secrets`, `secrets`, `credentials`, `.credentials`, `.aws`, `.azure`, `.kube`, `.docker`, `.gnupg` and `.password-store` components; `.netrc`, `_netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `.authinfo`, `.authinfo.gpg`, `.pgpass`, `pgpass.conf`, `.my.cnf`, `.dockercfg`, `auth.json`, `auth.yaml`, `auth.yml`, `credential.json`, `credentials.json`, `credentials.yaml`, `credentials.yml`, `token.json` and `tokens.json` components; `.config/gcloud`, `.config/gh` and `.local/share/keyrings` paths; files ending `.key`, `.pem`, `.p12` or `.pfx`; and `id_rsa`, `id_dsa`, `id_ecdsa` or `id_ed25519` private-key basenames with optional `_`, `-` or `.` variants. A basename ending `.pub` is allowed by the private-key-name rule when no other excluded path class applies. These explicit name classes are conservative exclusions, not a claim to detect every secret or to classify file contents.

Guarded reservation checks each frozen packet source and source/instruction context path under the objective admission lock. A current absent source, even one frozen as absent, or a changed source durably rejects that packet assignment when the frozen source had an actual state. A source frozen as unavailable stays unbound: even later readable bytes require a fresh packet with their actual digest, and no historical change or absence is recorded. Current unavailability holds admission without definitive rejection. This is a bounded admission check, not an atomic multi-file snapshot against external writes. Reserved, uncertain and confirmed launches occupy objective and overlapping account capacity, including before cleanup begins and when the fleet omits them or labels them released. Reserved, uncertain and retained cleanup keeps a confirmed worker occupied until exact native resource readback proves release. A confirmed launch without cleanup can also record a settled, exactly bound released resource through read-only reconciliation without repeating release. Fixture tests cover the state/cleanup/fleet matrix, Dispatch deduplication and both read-only paths without proving a live native release. An unproved absent-effect disposition remains an admission hold because the installed adapter has no exact absence proof.

The read-only release-reconciliation helper recognizes the immediate predecessor
`pod-effect/v1` binding that contains only Run, Task, Dispatch and worker identities. It keeps that
row occupied, requires an exact same-runtime worker/worktree readback plus every available optional
terminal/resource identity and unchanged launch, then durably upgrades the binding before any
release projection can settle. Malformed, ambiguous or contradictory predecessor rows remain held;
the helper never repeats the native start or release effect.

`python -m pod.internal release-gate --input RECORD.json` projects these exact rows. Every row
records host `Linux`, the one supported execution environment. A complete set without an owner
authorization returns `owner_decision_required`: technical readiness is reported, permission is
withheld. Supplying a `pod-release-authorization/v1` record naming the exact candidate, tree and
a scope containing `release` returns `authorized`; an incomplete set stays `blocked` whatever the
authorization says. Missing live or delegation subchecks remain unavailable even if a top-level
result says PASS. The gate performs no release.

## Waste governor

The governor's own evaluation runs offline: rule tests need no network, Orca, `gh` or model, and
the only remote seam is a port whose exact `git` and `gh` argument shapes have their own tests.
`tests/incidents/test_repeated_pr_cycle.py` is the sanitized regression for the incident that
motivated it: a still-converging unit crossing the pull-request and CI boundary once per
intermediate correction. It keeps the shape of the incident and none of its identifiers.

The private operations are `governor-prepare` (freeze a delivery unit's candidate generation
from Git), `governor-preflight` (bind a local check result to it), `governor` (decide),
`governor-execute` (admit and perform push, pull-request reuse or creation, workflow dispatch,
rerun or cancellation against the bound commit), `governor-outcome`, `governor-reconcile`
(settle an UNKNOWN or pending row from provider readback only), `governor-classify`,
`governor-correct` and `governor-status`. Merge, release and deployment are decided by the
governor and performed by project governance. The journal is `governor.json` beside the
objective's `context.json`; a `pod-governor/v1` journal is carried forward in place, and a
malformed one asks for migration rather than being repaired.

What the executor proves is bounded: a push lands the exact candidate commit or is reported
rejected; a dispatch is read back by workflow and commit, so a run that was started but never
answered is found rather than resubmitted; a cancellation is issued only for a pending,
cancel-safe validation with a run identity, and a cancellation that fails never strands the
action that followed it. A landed publication journals a derived validation row for each
workflow it triggers, so the run a push starts is attached to rather than doubled by a later
dispatch. Every row carries its own commit, so a superseded generation is read back by that
commit and never by the unit's current candidate. The journal keeps at most 96 rows within a
256 KB record; a `pod-governor/v1` journal above that bound is carried forward by keeping
every open row and the newest settled ones, and the number dropped is recorded in its counters. What it does not prove: that a worker or shell
holding the same credentials used the governed path. The enforcement level says `advisory`
until the owner's personal policy declares a host control, and it is recorded at the
owner-configuration tier because Pod cannot verify it. Live `gh` behaviour against a real
repository, required-check reporting and merge-queue semantics remain `NOT_RUN` until
exercised; the reference contract in [github-ci-contract](github-ci-contract.md) is documentation
of a pattern, not evidence that a repository follows it.

## Evidence record

```json
{
  "schema": "pod-validation/v1",
  "candidate": "1111111111111111111111111111111111111111",
  "tree": "2222222222222222222222222222222222222222",
  "host": "Linux",
  "utc": "2026-09-20T00:00:00Z",
  "gate": "name",
  "command": "sanitized command",
  "outcome": "PASS | FAILED | NOT_RUN | UNAVAILABLE",
  "report": "reports/unit-linux.txt sha256:4444444444444444444444444444444444444444444444444444444444444444"
}
```

Do not store raw environments, credentials, source packets, personal paths or runtime IDs in tracked evidence. A passing offline suite cannot promote live, review, hosted, acceptance, merge or release labels.

## Runtime limits

Orca caches provider rate-limit metadata and does not refresh it when read: on the machine
this was measured, the reading was hours old. A quota snapshot therefore records
`confidence: observed`, meaning the runtime reported these numbers, and its age decides how
far they are trusted. A reading older than `quota_fresh_seconds` resolves to unknown quota,
which permits one active worker on that account rather than the usual two. That is
conservative by design, and it means the documented default of two workers holds only while
a fresh reading is available.

The current installed Orca worker contract supports workers without terminals, and Pod's read adapter treats terminal identity as optional. Route establishment states which control backs each part of a route and how strongly: the effective launch and the native descendant-depth limit are enforceable controls, the billing mode, account identity, descendant count and quota windows are supported observations, route approval and the delegation setting are owner configuration, and anything else is recorded as unavailable. Pod claims no more than those controls prove. Refusing worker-initiated delegation is a Pod admission decision and a behavioural instruction to the worker, not a provider sandbox. The runtime enforces a nested-worker depth limit of its own, which Pod observes but cannot read the value of, so Pod counts descendants rather than assuming that limit is enough. The metadata adapter does not scrape credential stores or undocumented quota endpoints, and it stores a digest of an account identifier rather than the identifier. Reset credits have only an offline intent guard; no redemption transport is installed. Cross-host admission is not atomic and is disclosed as such. Exact live launch and release reconciliation, and the live core and delegation matrices, require separate evidence.
