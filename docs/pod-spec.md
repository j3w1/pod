# Pod specification

Status: the product requirements for the version the root `VERSION` file declares on the
same commit. It is not a claim of implementation or verification; a completed test cannot
silently revise a requirement, and a changed requirement is revised here explicitly with
its scenario and coverage row.

Pod is an adaptive coordination policy for Orca. It turns the coding session already in
use into the coordinator, choosing tools, agents, models and effort for each part of a
task and adapting the plan until the objective is verified. Requirement types: B =
behavior, H = hard authorization, I = implementation.

## Scope and ownership

Orca is the single source of truth for Runs, Tasks, Dispatches, request recovery, worker
lifecycle, messaging, placement and terminal/resource disposition. Pod is a thin
selection, admission and evidence layer: it keeps a serialized admission seam and exact native
references, but it does not copy native lifecycle status or implement Delivery, cleanup,
liveness, terminal, release or retry state machines. The installed, version-matched Orca
orchestration guide and its recovery reference govern native mutation semantics. Orca mints
request UUIDs; Pod only records and reuses them for the same immutable admission. Host
policy, explicit user authorization and project governance retain their own authority.

Pod decides desired fan-out, not physical occupancy: one logical reservation per Pod
assignment, freed by exact native assignment settlement regardless of retained terminals.
Settlement requires the exact Run/Task/Dispatch join, a settled projection outcome,
and the Dispatch's own terminal status; a projected stage status, if present,
must agree. A failed stopped attempt qualifies even when its
stage detail is `process_stopped`; an active, unverifiable or undocumented
abandoned attempt does not. Resource release alone proves nothing.
Independent objectives do not census each other, and Pod never queries, infers or claims
physical capacity. Orca's documented effect-free preflight refusals (`task_not_found`,
`task_not_startable`, `inject_rejected`) create a durable deferred admission with no binding
and no blind retry. `runtime_error` proves nothing about effects and stays unresolved until
request and worker readback settle it; unknown, malformed, lost or partial-effect responses
remain unresolved.

Pod is developed against Orca 1.4.209. Capabilities are discovered from the installed
runtime per operation through its advertised contracts and help; no minimum version is
enforced, and a missing delegation capability blocks only delegation.

Pod Execution Spec is the recommended persistent objective input while direct objectives
remain first-class. Issue and worktree source bindings create no issue or worktree managers.

## Requirements

### R01 — Requirement R01

Type: B · Scenarios: [A38](#a38)

Maintain this requirement/scenario inventory as product authority. Explicitly revise changed requirements; implementation results cannot silently redefine them.

### R02 — Requirement R02

Type: B · Scenarios: [A01](#a01), [A39](#a39), [A94](#a94)

Keep the current conversation as coordinator. Orca is the sole runtime authority for Runs, Tasks, Dispatches, requests, placement, messaging, terminals/resources and lifecycle; projects own governance. Pod is selection, admission and evidence only. Helpers must not become a scheduler, competing native task/lifecycle database, autonomous controller or restart daemon.

## Acceptance scenarios

- <a id="a01"></a>**A01** — Invoke Pod in either host: the same conversation remains coordinator with context and coordinator configuration preserved.
- <a id="a38"></a>**A38** — Requirement/scenario references have no duplicate/orphan IDs, unclassified requirement or contradiction.
- <a id="a39"></a>**A39** — No competing native task database, scheduler, autonomous reasoning/restart loop, dashboard or marketplace.
- <a id="a94"></a>**A94** — Pod exposes no private Delivery, cleanup, release, terminal or lifecycle mutation operation; Orca owns those states, while reports and Governor decisions use exact objective assignment evidence when needed.
