# Pod validation

Each result binds an exact commit and Git tree, host, UTC date, command, outcome and sanitized report reference. Offline fixtures do not prove live Orca, provider behavior, native Windows, hosted CI, independent review or acceptance. All new-candidate rows start `NOT_RUN` until a matching result is recorded.

## Required candidate checks

| Gate | Command or evidence | Current boundary |
| --- | --- | --- |
| Unit and incident suite | `PYTHONPATH=src python -m unittest discover -s tests -v` | Disposable synthetic fixtures |
| Explicit incident discovery | `PYTHONPATH=src:. python -m unittest discover -s tests/incidents -t . -v` | Tests must actually be discovered |
| Compile and diff | `python -m compileall -q src tests`; `git diff --check 474a84a6d5a1f7947abc1e38d232c379adf7ff93 HEAD` | Local syntax and exact baseline-to-candidate whitespace |
| Skill validation | `PYTHONPATH=src python -m pod.skill_validation src/pod/skill`; additionally run the current system skill validator when available | Canonical packaged source, frontmatter, file inventory and references |
| Frozen build | `git archive HEAD` into a disposable cache directory, then build a wheel there | Exact committed candidate |
| Fresh isolated install | Install wheel into new disposable venv; smoke `pod --help`, each family help, `pod config --check --json`, `pod doctor --json` | No model/Orca mutation |
| Hosted Windows/Linux | Same suite, incident discovery, frozen wheel and installed CLI smokes in both jobs | Candidate-bound CI |
| Independent audit | Fresh reviewer of exact commit/tree and reproducible evidence | Findings only |
| Live core matrix | Codex and Claude Code, each on native Linux and Windows: discovery, in-session coordination, authorized native worker/effective route, lifecycle, verification and adoption | Separate live authorization and disposable project |
| Project acceptance | Owner governance, merge, release and any publication decisions | External |

For disposable validation, `POD_CONFIG_HOME` may name an absolute directory containing
`config.yaml`, and `POD_STATE_HOME` may name an absolute Pod state directory. These
process-scoped Pod-only overrides reject empty, relative, or existing non-directory values;
they do not change the default personal locations, project YAML authority, `APPDATA`,
`LOCALAPPDATA`, `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, or Orca's native profile environment.
Child Orca processes inherit that native profile unchanged. Use fresh owned directories;
the overrides do not make an existing directory disposable.

Inferred native homes (`XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `APPDATA`, `LOCALAPPDATA`,
`CODEX_HOME`, and `CLAUDE_CONFIG_DIR`) must be absolute directories outside the current project,
both lexically and after resolving existing redirects. Configuration/state access and global setup
fail before writes when that boundary is not proven. Explicit absolute `POD_CONFIG_HOME` and
`POD_STATE_HOME` remain Pod-only disposable overrides, and local project-scope setup intentionally
writes its owned integration beneath the project.

The explicit installer requires pip 22.3 or newer on the invoking interpreter, creates the
selected environment without pip, and uses isolated bootstrap pip's documented `--python`
option to manage the exact target interpreter, with script-location warning traversal
disabled. Process-only controls remove inherited Python and pip control variables, disable
all pip configuration files for both pip phases and their target re-exec, and disable the
user site. Before success, the target interpreter runs in isolated mode and verifies the
installed `j3w1-pod` distribution and `pod` import; no system pip upgrade or profile change
is attempted. The prior ALLY run failed in `ensurepip` with WinError 448 while traversing
the Codex `bin` mount; this candidate avoids `ensurepip`, but `install.py` end-to-end on
native Windows remains `NOT_RUN` until an owner-shell rerun. The complete Windows unit,
repeat-setup/global-reuse/doctor, frozen-wheel, installed-CLI, and live rows likewise remain
`NOT_RUN`; Linux fixtures do not promote them.

The [scenario coverage file](pod-coverage.json) lists all A01–A82. Its test paths identify intended offline cases; the file itself proves only inventory integrity. Candidate-bound live and hosted rows remain `NOT_RUN` until actually exercised. The direct-agent, native-Orca and Pod matched evaluation also remains `NOT_RUN` without bounded live authorization.

Pod's automatic source and packet-reference boundary excludes conventional credential classes before opening a source: any `.env*` component; `.ssh`, `.secrets`, `secrets`, `credentials`, `.credentials`, `.aws`, `.azure`, `.kube`, `.docker`, `.gnupg` and `.password-store` components; `.netrc`, `_netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `.authinfo`, `.authinfo.gpg`, `.pgpass`, `pgpass.conf`, `.my.cnf`, `.dockercfg`, `auth.json`, `auth.yaml`, `auth.yml`, `credential.json`, `credentials.json`, `credentials.yaml`, `credentials.yml`, `token.json` and `tokens.json` components; `.config/gcloud`, `.config/gh` and `.local/share/keyrings` paths; files ending `.key`, `.pem`, `.p12` or `.pfx`; and `id_rsa`, `id_dsa`, `id_ecdsa` or `id_ed25519` private-key basenames with optional `_`, `-` or `.` variants. A basename ending `.pub` is allowed by the private-key-name rule when no other excluded path class applies. These explicit name classes are conservative exclusions, not a claim to detect every secret or to classify file contents.

Guarded reservation checks each frozen packet source and source/instruction context path under the objective admission lock. A current absent source, even one frozen as absent, or a changed source durably rejects that packet assignment when the frozen source had an actual state. A source frozen as unavailable stays unbound: even later readable bytes require a fresh packet with their actual digest, and no historical change or absence is recorded. Current unavailability holds admission without definitive rejection. This is a bounded admission check, not an atomic multi-file snapshot against external writes. Reserved, uncertain and confirmed launches occupy objective and overlapping account capacity, including before cleanup begins and when the fleet omits them or labels them released. Reserved, uncertain and retained cleanup keeps a confirmed worker occupied until exact native resource readback proves release. A confirmed launch without cleanup can also record a settled, exactly bound released resource through read-only reconciliation without repeating release. Fixture tests cover the state/cleanup/fleet matrix, Dispatch deduplication and both read-only paths without proving a live native release. An unproved absent-effect disposition remains an admission hold because the installed adapter has no exact absence proof.

The read-only release-reconciliation helper recognizes the immediate predecessor
`pod-effect/v1` binding that contains only Run, Task, Dispatch and worker identities. It keeps that
row occupied, requires an exact same-runtime worker/worktree readback plus every available optional
terminal/resource identity and unchanged launch, then durably upgrades the binding before any
release projection can settle. Malformed, ambiguous or contradictory predecessor rows remain held;
the helper never repeats the native start or release effect.

`python -m pod.internal release-gate --input RECORD.json` projects these exact rows. Hosted CI
is represented by independent `hosted_ci_linux` and `hosted_ci_windows` gates. Every gate ending
in `_linux` requires host `Linux`, every gate ending in `_windows` requires host `Windows`, and
other gates still require one of those two documented host values. A complete set returns
`owner_decision_required`; it never authorizes publication, merge or installed-state cutover.
Missing live subchecks remain unavailable even if a top-level result says PASS.

## Evidence record

```json
{
  "schema": "pod-validation/v1",
  "candidate": "exact Git commit",
  "tree": "exact Git tree",
  "host": "Linux or Windows",
  "utc": "timestamp",
  "gate": "name",
  "command": "sanitized command",
  "outcome": "PASS | FAILED | NOT_RUN | UNAVAILABLE",
  "report": "sanitized artifact reference and digest"
}
```

Do not store raw environments, credentials, source packets, personal paths or runtime IDs in tracked evidence. A passing offline suite cannot promote live, review, hosted, acceptance, merge or release labels.

## Runtime limits

The current installed Orca worker contract supports workers without terminals. Pod's read adapter treats terminal identity as optional. Its pre-dispatch billing eligibility and hidden descendant fan-out controls have not been verified, so the guarded admission path fails closed. The metadata adapter does not scrape credential stores or undocumented quota endpoints. Reset credits have only an offline intent guard; no redemption transport is installed. WSL and remote ownership, exact live launch and release reconciliation, and both native OS core matrices require separate evidence.
