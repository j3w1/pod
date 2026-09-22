# Pod 0.3.0 progress

Specification baseline: `1b0aa6fc68ee2157f6f67ba4a60076d967791cc4`.
Read-only observations found Orca 1.4.206, Codex CLI 0.155.1 and Claude Code
2.1.278; version discovery alone does not prove execution. This page reports candidate
implementation, not release.

| Area | State | Evidence / limit |
| --- | --- | --- |
| Runtime boundary | IMPLEMENTED locally | Orca owns Runs, Tasks, Dispatches, requests, messaging, terminals/resources and lifecycle. Pod's mutation adapter exposes only worker start and exact same-request replay. Removed Pod Delivery, cleanup, release and retry state machines have no private helper surface. |
| Admission | IMPLEMENTED locally | Serialized v2 policy reservations re-read effective approval, route, source, packet, quota, spending and capacity under lock before one native start. Fresh Orca projections count exact admissions, foreign workers and descendants conservatively and deduplicate bindings. |
| Recovery | IMPLEMENTED locally | The Orca-issued request UUID is persisted before worker readback. Completed, pending and absent request paths record, join or inspect the same immutable admission; missing, invalid, ambiguous and contradictory evidence holds without a fresh start. |
| State migration | IMPLEMENTED locally | Explicit `state-migrate` validates and archives v1, reads Orca only, preserves spent grants, maps uncertainty to `legacy_hold`, and atomically installs v2. Failure leaves v1 active. Doctor/status only report `migration_required`. |
| Packets and reports | IMPLEMENTED locally | Existing bounded packet/source/report contracts remain. Report admission joins a fresh worker-show to exact runtime and Dispatch identity. |
| Waste governor | IMPLEMENTED locally | ALLOW/REUSE/DEFER, candidate/preflight/authorization/failure/correction and remote-action semantics remain. Production decisions now consume fresh read-only Orca occupancy rather than persistent Delivery/cleanup state. |
| Skill and package | IMPLEMENTED locally | Package and skill are 0.3.0 candidates; CLI/helper envelopes are v2. The lean skill loads the installed Orca guide and on-demand Pod references. The last published installation pin remains `v0.1.2`; no v0.3.0 tag or publication is claimed. |
| Candidate validation | IN PROGRESS | Focused offline behavior passes. Full suite, incidents, compile, audits, frozen build and install checks are rerun after the candidate is frozen. Hosted CI, live matrices, independent review, project acceptance, merge, release and publication remain distinct external gates. |

## Candidate evidence

The coordinator receives the exact commit and tree after the frozen-candidate checks. Coverage
rows remain `NOT_RUN` until candidate-bound evidence is recorded; a test path is an intended
offline case, not proof by itself.

## Remaining external gates

- Required Codex and Claude Code live core and Orca delegation evidence is `NOT_RUN` here.
- Hosted candidate CI, fresh independent review and project acceptance are `NOT_RUN` here.
- Merge, tag, release, deployment and package-registry publication are not authorized by this
  implementation task.
