# Pod 0.3.0 progress

Specification baseline: `1b0aa6fc68ee2157f6f67ba4a60076d967791cc4`.
The current read-only observation found Orca 1.4.207; the reconciliation record in the
specification preserves the earlier 1.4.205 observation. Codex CLI 0.155.1 and Claude Code
2.1.278; version discovery alone does not prove execution. This page reports candidate
implementation, not release.

| Area | State | Evidence / limit |
| --- | --- | --- |
| Runtime boundary | IMPLEMENTED locally | Orca owns Runs, Tasks, Dispatches, requests, messaging, terminals/resources and lifecycle. Pod's mutation adapter exposes only worker start and exact same-request replay. Removed Pod Delivery, cleanup, release and retry state machines have no private helper surface. |
| Admission | IMPLEMENTED locally | Serialized v2 policy reservations re-read effective personal/project/task approval, route, source, packet, quota, spending and objective fan-out under lock before one native start. Identity and authentication/billing come from one selected account context and are rejoined immediately before start or pending replay; rotation and partial/contradictory native defaults block without relabeling. Objective-local reservations and exact bound-assignment settlement drive logical admission; all-Run and unscoped worker enumeration are rejected by the adapter. Independent objectives do not consume each other's slots. |
| Recovery | IMPLEMENTED locally | The Orca-issued request UUID is persisted before worker readback. Completed, pending and method-less absent request paths record, join or inspect the same immutable admission; revocation blocks mutation but not completed/absent read-only reconciliation. |
| State migration | IMPLEMENTED locally | Explicit `state-migrate` validates bounded regular-file input, resolves exact assignments through Orca reads only, preserves v1 in a boundedly verified archive, and atomically installs v2. Exact assignment settlement closes a migrated binding regardless of terminal retention; unbound ambiguity remains outstanding. |
| Packets and reports | IMPLEMENTED locally | Existing bounded packet/source/report contracts remain. Report admission joins a fresh worker-show to exact runtime and Dispatch identity. |
| Waste governor | IMPLEMENTED locally | ALLOW/REUSE/DEFER, candidate/preflight/authorization/failure/correction and remote-action semantics remain. Production decisions use objective-local logical admissions and exact assignment evidence when needed, never a fleet-completeness prerequisite. Every preparation/admission/execution/journal mutation joins a stable native current Run/coordinator/generation to the objective's exact Run refs, runtime and existing Pod owner; read-only status remains diagnostic. |
| Skill and package | IMPLEMENTED locally | Package and skill are 0.3.0 candidates; CLI/helper envelopes are v2. The lean skill loads the installed Orca guide and on-demand Pod references. The last published installation pin remains `v0.1.2`; no v0.3.0 tag or publication is claimed. |
| Candidate validation | OFFLINE PASS | The 248-test suite and explicit four-test incident discovery pass on Python 3.13.15, as do compile, supported-platform audit and repository skill validation. Focused cases cover configured two-slot fan-out, exact settlement, independent objectives, unresolved duplicate prevention, authoritative `capacity_full` deferral and partial-effect uncertainty. Frozen build/install checks are recorded in the private implementation report. Native real-capacity enforcement remains unavailable unless a genuine Orca refusal is observed; fixtures prove only the boundary. |

## Candidate evidence

The coordinator receives the exact commit and tree after the frozen-candidate checks. Coverage
rows remain `NOT_RUN` until candidate-bound evidence is recorded; a test path is an intended
offline case, not proof by itself.

## Remaining external gates

- Required Codex and Claude Code live core and Orca delegation evidence is `NOT_RUN` here.
- Native real-capacity enforcement is unavailable; synthetic `capacity_full` coverage is not live evidence.
- Hosted candidate CI, fresh independent review and project acceptance are `NOT_RUN` here.
- Merge, tag, release, deployment and package-registry publication are not authorized by this
  implementation task.
