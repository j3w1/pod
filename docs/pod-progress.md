# Pod rewrite progress

Specification baseline: `17a2316`. Read-only P00 observations found Orca 1.4.205, Codex CLI 0.155.1 and Claude Code 2.1.278; versions alone do not prove Pod execution. Candidate commit/tree and final gate results are recorded in the sanitized task report after the branch is frozen. This page reports implementation work, not product release.

| Phase | State | Evidence / limit |
| --- | --- | --- |
| P00 | DONE | Clean baseline, host doctor, runtime/help readback, legacy keep/adapt/remove inventory in [migration](pod-migration.md). |
| P01 | DONE for inventory; PARTIAL for behavior | All R01–R59 and A01–A82 remain in [spec](pod-spec.md); 82-row [coverage](pod-coverage.json) names evidence types. Six formerly unmapped cases now have deterministic fixture slices. A02, A03, A04, A40, A41, A44 and A63 still lack complete local behavioral proof; document ID checks prove inventory only. |
| P02 | IMPLEMENTED, local fixtures | Strict YAML, pending defaults, restrictive merge including quota freshness, provenance, approval, provider/account/bucket/window-bound quota preview and replay pass local fixtures. |
| P03 | PARTIAL | Admission derives hard capacity and scoped exceptional grants from current effective policy; quota exhaustion and renewal bind route bucket. Billing/reset guards, strict read adapter, serialized native-first admission and one-shot effect reconciliation pass synthetic tests. Actual native billing/fan-out preflight and account-bucket binding remain unverified; production dispatch blocks. |
| P04 | PARTIAL | Typed bounded packet references, native-bound report observations, per-item durable Delivery effects, stable criterion/failure correction history, no-repeat launch/release intentions and recovery guidance pass synthetic tests. Native Delivery receipt integration, live supervision, effective launch and cleanup remain unverified. |
| P05 | PARTIAL | Criterion-to-check/dependency brief, candidate-bound evidence projection, context binding, private checkpoint, steering, feedback and compact Run status have offline tests. Worker reports and validation records do not confer project acceptance; live adoption remains unverified. |
| P06 | IMPLEMENTED locally | Package/CLI cutover, local/global skill setup, explicit isolated installer, legacy machinery removed in this branch. Canonical repository rename and installed-state cutover remain owner gates. |
| P07 | OFFLINE IN PROGRESS | The corrected working-tree suite passes 61 unit/incident tests; frozen candidate gates will be recorded in the durable task report. The private release gate requires all candidate-bound rows and never authorizes release itself. Hosted, fresh independent, live and matched evaluation remain `NOT_RUN`. |

## Candidate evidence

The coordinator receives an exact commit/tree report after frozen archive checks. No earlier commit's result fills that row. The 82-row coverage file retains `NOT_RUN` as its initial per-candidate state; test paths are intended offline cases, not proof by themselves.

## Release blockers

- Current Orca contract lacks verified pre-dispatch billing and hidden fan-out assurance for Pod. Guarded worker admission blocks affected operations.
- Required Claude Code and Codex live core matrices on native Linux and Windows are `NOT_RUN`.
- Hosted candidate CI, fresh independent review, matched evaluation and project acceptance are `NOT_RUN`.
- Legacy `release_unknown` native resource needs owner/native reconciliation before destructive installed-state cutover. No native identifiers or private paths are tracked here.
- Canonical repository rename/registration and external release actions are outside this implementation dispatch.
