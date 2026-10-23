# Pod specification

Status: requirements for the version in the root `VERSION` on the same commit. A test result does not revise a requirement; a behavior change updates its requirement, scenario and coverage row.

Pod coordinates the current coding conversation toward a verified objective. The coordinator skill makes task decisions; bounded Python helpers validate admission and evidence. Orca remains authoritative for Runs, Tasks, Dispatches, placement and worker lifecycle. The kernel keeps objective obligations, a serialized admission boundary, exact native references and Governor decisions. One personal YAML file controls the eligible exact model+effort routes, the Preferred and Pinned routes and the logical ceiling; the shipped registry supplies exact identities and attributed guidance, and dated public observations stay informational. Host rules, direct user authorization and project governance keep their own authority.

Requirements use **B** for behavior, **H** for hard authorization and **I** for implementation. A requirement links to observable acceptance scenarios; scenario text is the expected result, not proof. `docs/pod-coverage.json` maps scenarios to unit, PTY, installed bundle, hosted CI, live native or independent review evidence. `NOT_RUN` remains unavailable evidence.

## Domains

- [Coordination and obligations](spec/coordination.md)
- [Models](spec/models.md): [routing](spec/models/routing.md), [preferences](spec/models/preferences.md) and [catalog](spec/models/catalog.md)
- [Orca integration and recovery](spec/orca.md), including [route settlement](spec/orca.md#r160--settle-each-recorded-route-mismatch-occurrence) and [bounded record maintenance](spec/orca.md#r121--keep-settled-history-immutable-outside-the-bounded-context-record)
- [Assurance and evidence](spec/assurance.md)
- [Delivery and cleanup](spec/delivery.md), including [closure as delivery](spec/delivery.md#r144--closure-is-part-of-delivery) and [evidence closure](spec/orca.md#r145--close-delivered-work-by-evidence-when-its-coordinator-is-gone)
- [Interfaces and installation](spec/interface.md), including the [self-healing sweep](spec/interface.md#r148--sweep-provably-delivered-open-objectives)

## Requirements

### R01 — Maintain this requirement/scenario inventory as product

Type: B · Scenarios: [A38](#a38)

Maintain this requirement/scenario inventory as product authority. Explicitly revise changed requirements; implementation results cannot silently redefine them. Removed scenarios leave their IDs reserved in the coverage inventory's `withdrawn_scenarios` metadata with a reason, without an active scenario, coverage result or passing evidence. Every allocated ID is active or explicitly withdrawn, never reused.

### R02 — Keep the current conversation as coordinator

Type: B · Scenarios: [A01](#a01), [A39](#a39), [A94](#a94), [A250](spec/orca.md#a250), [A456](spec/delivery.md#a456), [A458](spec/orca.md#a458), [A467](spec/orca.md#a467), [A469](spec/interface.md#a469)

Keep the current conversation as coordinator, except the explicit Owner-authorized loss handoff in R105 and the verified window continuation in R159, whose derived authority needs no owner decision. A successor's claim (R110) ends a lost coordinator's write authority and gives it to no one; it replaces no coordinator. Delivery includes the coordinator's same-turn closure (R144). Evidence closure (R145-R147) ends a lost coordinator's objective without an owner decision; R148 supplies the sweep. The optional owner's historical retirement (R112) also ends authority and releases only Pod's own record of named worktrees, each existing or proven absent (R113). Orca is the sole runtime authority for Runs, Tasks, Dispatches, requests, placement, messaging, terminals/resources and lifecycle; projects own governance. Pod is selection, admission and evidence only. Loss detection and evidence closure read exact native liveness; only the handoff and the window continuation mutate the existing Run binding through run-use; helpers must not become a scheduler, competing native task/lifecycle database, autonomous controller or restart daemon.

## Acceptance scenarios

- <a id="a01"></a>**A01** — Invoke Pod in either host: the same conversation remains coordinator with context and coordinator configuration preserved.
- <a id="a38"></a>**A38** — Requirement/scenario references have no duplicate/orphan IDs, unclassified requirement or contradiction.
- <a id="a39"></a>**A39** — No competing native task database, scheduler, autonomous reasoning/restart loop, dashboard or marketplace.
- <a id="a94"></a>**A94** — Pod exposes no private Delivery, cleanup, release or work-lifecycle mutation operation; the owner-handoff and window-continuation exceptions read terminal show and mutate the existing Run through the exact run-use shape; Orca owns those states, while reports and Governor decisions use exact objective assignment evidence when needed.
