# Pod rewrite progress

Specification baseline: `17a2316`. Read-only observations found Orca 1.4.206, Codex CLI 0.155.1 and Claude Code 2.1.278; versions alone do not prove Pod execution. Candidate commit/tree and final gate results are recorded in the sanitized task report after the branch is frozen. This page reports implementation work, not product release.

| Phase | State | Evidence / limit |
| --- | --- | --- |
| P00 | DONE | Clean baseline, host doctor, runtime/help readback, legacy keep/adapt/remove inventory in [migration](pod-migration.md). |
| P01 | DONE for inventory; PARTIAL for behavior | Every requirement and scenario remains in the [spec](pod-spec.md), with the 2026-09-21 owner retirements recorded in place; [coverage](pod-coverage.json) names each scenario's evidence type. Six formerly unmapped cases now have deterministic fixture slices. A02, A03, A04, A40, A41, A44 and A63 still lack complete local behavioral proof; document ID checks prove inventory only. |
| P02 | IMPLEMENTED, local fixtures | Strict YAML, pending defaults, restrictive merge including quota freshness, provenance, approval, provider/account/bucket/window-bound quota preview and replay pass local fixtures. |
| P03 | PARTIAL | Admission derives hard capacity and scoped exceptional grants from current effective policy; per-window quota holds preserve the latest exhaustion and require later fresh positive evidence. Global serialized admission projects reserved, uncertain and confirmed launches with reserved, uncertain and retained cleanup into objective and shared-account occupancy, deduplicated by exact runtime/Dispatch where available in synthetic fixtures. Fleet omission and coarse released labels do not free local launch evidence; exact worker readback can reconcile cleanup or a confirmed launch without cleanup without another release effect. Billing and reset guards, the strict read adapter and one-shot effect reconciliation pass synthetic tests. Route establishment now binds each route to the installed runtime's actual controls, so an approved subscription route launches and an unknown billing mode or unbacked paid route still fails closed. |
| P04 | PARTIAL | Typed bounded packet references, retained no-follow single-file source reads, and guarded packet-source and source/instruction-context checks pass synthetic fixtures at reservation. Proven changed or absent sources durably reject the packet assignment; a source frozen as unavailable stays unbound until a fresh packet binds actual bytes, and current unavailability holds without definitive rejection. Native-bound report observations, per-item durable Delivery effects, and correction history bound to a confirmed Task and accepted criterion pass synthetic tests. A third correction requires diagnosis with a distinct bounded source observation; labels and descriptions cannot reset that threshold. Delivery receipt integration, live supervision, effective launch and cleanup are implemented against the production adapter and remain to be proved live. |
| P05 | PARTIAL | Criterion-to-check/dependency brief, candidate-bound evidence projection, context binding, private checkpoint, steering, feedback and compact Run status have offline tests. Worker reports and validation records do not confer project acceptance; live adoption remains unverified. |
| P06 | IMPLEMENTED locally | The package directory is now the skill bundle, with a bundled launcher, skills-CLI ownership detection, version-aware upgrade, the explicit isolated installer and the legacy machinery removed. The canonical repository rename and installed-state cutover remain owner-context steps. |
| P07 | OFFLINE IN PROGRESS | The full local suite, incident discovery, compile, supported-environment audit, bundle validation and parity, copied-bundle and installer smokes pass. The private release gate reports technical readiness and takes owner authorization as an input. Hosted, fresh independent review and the live matrices remain `NOT_RUN`. |

## Candidate evidence

The coordinator receives an exact commit/tree report after frozen archive checks. No earlier commit's result fills that row. The coverage file retains `NOT_RUN` as its initial per-candidate state; test paths are intended offline cases, not proof by themselves.

## Release blockers

- Required Claude Code and Codex live core matrices on Linux, and both Orca delegation adapters, are `NOT_RUN`.
- Hosted candidate CI, fresh independent review and project acceptance are `NOT_RUN`.
- Legacy `release_unknown` native resource needs owner/native reconciliation before destructive installed-state cutover. No native identifiers or private paths are tracked here.
- Canonical repository rename/registration and external release actions are outside this implementation dispatch.
