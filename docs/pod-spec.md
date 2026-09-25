# Pod specification

Status: requirements for the version in the root `VERSION` on the same commit. A test result does not revise a requirement; a behavior change updates its requirement, scenario and coverage row.

Pod coordinates the current coding conversation toward a verified objective. The coordinator skill makes task decisions; bounded Python helpers validate admission and evidence. Orca remains authoritative for Runs, Tasks, Dispatches, placement and worker lifecycle. The kernel keeps objective obligations, a serialized admission boundary, exact native references and Governor decisions. One personal YAML file controls the eligible worker pool and logical ceiling; the bundled catalog supplies attributed model guidance and informational benchmarks. Host rules, direct user authorization and project governance keep their own authority.

Requirements use **B** for behavior, **H** for hard authorization and **I** for implementation. A requirement links to observable acceptance scenarios; scenario text is the expected result, not proof. `docs/pod-coverage.json` maps scenarios to unit, PTY, installed bundle, hosted CI, live native or independent review evidence. `NOT_RUN` remains unavailable evidence.

## Domains

- [Coordination and obligations](spec/coordination.md)
- [Models and preferences](spec/models.md)
- [Orca integration and recovery](spec/orca.md)
- [Assurance and evidence](spec/assurance.md)
- [Delivery and cleanup](spec/delivery.md)
- [Interfaces and installation](spec/interface.md)

## Requirements

### R01 — Maintain this requirement/scenario inventory as product

Type: B · Scenarios: [A38](#a38)

Maintain this requirement/scenario inventory as product authority. Explicitly revise changed requirements; implementation results cannot silently redefine them.

### R02 — Keep the current conversation as coordinator

Type: B · Scenarios: [A01](#a01), [A39](#a39), [A94](#a94)

Keep the current conversation as coordinator. Orca is the sole runtime authority for Runs, Tasks, Dispatches, requests, placement, messaging, terminals/resources and lifecycle; projects own governance. Pod is selection, admission and evidence only. Helpers must not become a scheduler, competing native task/lifecycle database, autonomous controller or restart daemon.

## Acceptance scenarios

- <a id="a01"></a>**A01** — Invoke Pod in either host: the same conversation remains coordinator with context and coordinator configuration preserved.
- <a id="a38"></a>**A38** — Requirement/scenario references have no duplicate/orphan IDs, unclassified requirement or contradiction.
- <a id="a39"></a>**A39** — No competing native task database, scheduler, autonomous reasoning/restart loop, dashboard or marketplace.
- <a id="a94"></a>**A94** — Pod exposes no private Delivery, cleanup, release, terminal or lifecycle mutation operation; Orca owns those states, while reports and Governor decisions use exact objective assignment evidence when needed.
