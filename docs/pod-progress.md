# Pod 0.3.0 progress

Specification baseline: `1b0aa6fc68ee2157f6f67ba4a60076d967791cc4`.
The current read-only observation found Orca 1.4.207; the reconciliation record in the
specification preserves the earlier 1.4.205 observation. Codex CLI 0.155.1 and Claude Code
2.1.278; version discovery alone does not prove execution. This page reports candidate
implementation, not release.

| Area | State | Evidence / limit |
| --- | --- | --- |
| Runtime boundary | IMPLEMENTED locally | Orca owns Runs, Tasks, Dispatches, requests, messaging, terminals/resources and lifecycle. Pod's mutation adapter exposes only worker start and exact same-request replay. Removed Pod Delivery, cleanup, release and retry state machines have no private helper surface. |
| Admission | IMPLEMENTED locally | Serialized v2 policy reservations re-read effective personal/project/task approval, route, source, packet, quota, spending and capacity under lock before one native start. Personal approval, quota, capacity and grants share the runtime-selected redacted account identity. Fresh projections enumerate every paginated native Run twice around exact Run worker reads; incomplete/changing scope blocks. |
| Recovery | IMPLEMENTED locally | The Orca-issued request UUID is persisted before worker readback. Completed, pending and method-less absent request paths record, join or inspect the same immutable admission; revocation blocks mutation but not completed/absent read-only reconciliation. |
| State migration | IMPLEMENTED locally | Explicit `state-migrate` validates bounded regular-file input, resolves exact effects through Orca reads only, preserves v1 in a boundedly verified archive, and atomically installs v2. Exact later release proof frees a bound legacy hold; unbound ambiguity remains occupied. |
| Packets and reports | IMPLEMENTED locally | Existing bounded packet/source/report contracts remain. Report admission joins a fresh worker-show to exact runtime and Dispatch identity. |
| Waste governor | IMPLEMENTED locally | ALLOW/REUSE/DEFER, candidate/preflight/authorization/failure/correction and remote-action semantics remain. Production decisions consume fresh complete Orca occupancy rather than persistent Delivery/cleanup state, and every preparation/admission/execution/journal mutation requires the actual native owner on the stable runtime; read-only status remains diagnostic. |
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
