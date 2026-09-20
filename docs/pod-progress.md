# Pod rewrite progress

Specification baseline: `17a2316`. Read-only P00 observations found Orca 1.4.205, Codex CLI 0.155.1 and Claude Code 2.1.278; versions alone do not prove Pod execution. Candidate commit/tree and final gate results are recorded in the sanitized task report after the branch is frozen. This page reports implementation work, not product release.

| Phase | State | Evidence / limit |
| --- | --- | --- |
| P00 | DONE | Clean baseline, host doctor, runtime/help readback, legacy keep/adapt/remove inventory in [migration](pod-migration.md). |
| P01 | DONE for inventory; PARTIAL for behavior | All R01–R59 and A01–A82 remain in [spec](pod-spec.md); 82-row [coverage](pod-coverage.json) names evidence types. Offline tests cover a subset; document ID checks do not prove behavior. |
| P02 | IMPLEMENTED, local fixtures | Strict YAML, pending defaults, restrictive merge, provenance, approval, deterministic preview/replay. |
| P03 | PARTIAL | Cached native quota metadata is redacted and retains freshness/unknown account binding; deterministic routing, billing/reset guards, strict read adapter, serialized native-first admission and one-shot effect reconciliation pass synthetic tests. Actual native billing/fan-out preflight and account-bucket binding are unverified; production dispatch blocks. |
| P04 | PARTIAL | Canonical skill, bounded packets/reports, correction diagnosis, no-repeat launch/release intentions, terminal-optional worker identity and recovery guidance pass synthetic tests. Live supervision, effective launch and cleanup remain unverified. |
| P05 | PARTIAL | Candidate-bound criteria/evidence projection, context binding, private checkpoint, steering, feedback and compact Run status have offline tests. Worker reports and validation records do not confer project acceptance; live adoption remains unverified. |
| P06 | IMPLEMENTED locally | Package/CLI cutover, local/global skill setup, explicit isolated installer, legacy machinery removed in this branch. Canonical repository rename and installed-state cutover remain owner gates. |
| P07 | OFFLINE IN PROGRESS | Unit/incident, compile/diff, frozen wheel/isolated smokes to record below. The private release gate requires all candidate-bound rows and never authorizes release itself. Hosted, independent, live and matched evaluation remain `NOT_RUN`. |

## Candidate evidence

The coordinator receives an exact commit/tree report after frozen archive checks. No earlier commit's result fills that row. The 82-row coverage file retains `NOT_RUN` as its initial per-candidate state; test paths are intended offline cases, not proof by themselves.

## Release blockers

- Current Orca contract lacks verified pre-dispatch billing and hidden fan-out assurance for Pod. Guarded worker admission blocks affected operations.
- Required Claude Code and Codex live core matrices on native Linux and Windows are `NOT_RUN`.
- Hosted candidate CI, fresh independent review, matched evaluation and project acceptance are `NOT_RUN`.
- Legacy `release_unknown` native resource needs owner/native reconciliation before destructive installed-state cutover. No native identifiers or private paths are tracked here.
- Canonical repository rename/registration and external release actions are outside this implementation dispatch.
