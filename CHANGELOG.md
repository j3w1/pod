# Changelog

## 0.3.0 — candidate

- Orca is now the only runtime and lifecycle authority. Pod retains a serialized policy
  admission seam, frozen packets, evidence and governance, while its private Delivery,
  cleanup, release, terminal, liveness and retry state machines are removed.
- `pod-context/v2` records compact admissions and exact native references. Capacity and the
  waste governor use fresh Orca projection; missing, ambiguous, retained or unresolved work
  remains conservative, and exact released projection can free capacity.
- Native recovery records Orca's request UUID before readback and handles completed, pending
  and method-less absent request states without a second semantic start. Read-only completed
  or absent reconciliation remains possible after revocation; only pending replay rechecks
  current mutation authority. Pod never selects a retry UUID.
- Explicit `state-migrate` archives and hashes v1, performs read-only native reconciliation,
  preserves spent grants and uncertain holds, then atomically installs v2 from bounded regular
  files. Exact later release proof frees a bound legacy hold; doctor and status report migration
  requirements without changing state.
- Package/skill metadata is 0.3.0 and CLI/helper envelopes are v2. The last published install
  pin remains `v0.1.2`; this candidate does not claim a tag or publication.

## 0.2.0 — 2026-09-22

- The governor is now a waste-governor kernel: a small deterministic control layer inside the
  existing execution path that prevents a still-converging piece of work from repeatedly
  crossing the pull-request, CI and release boundary. Work is grouped into delivery units,
  each with explicitly prepared candidate generations bound to commit, tree, base, workflow
  digests, verification commands, toolchain, environment and policy revision, so a changed
  workflow or base is a new candidate even on the same commit. Decisions are ALLOW, REUSE or
  DEFER with the reason and the next useful action; WARN is an annotation. What an action
  triggers is judged rather than its verb, so a push that starts CI is validation and needs
  the configured local preflight first.
- `internal governor-execute` admits and performs push, pull-request reuse or creation,
  workflow dispatch, rerun and cancellation against the bound commit through an exact `git`
  and `gh` allowlist; a lost response stays UNKNOWN and `internal governor-reconcile` settles
  it from readback without resubmitting. Two callers requesting the same validation admit one
  execution. A push that triggers CI journals the run it started, so a later dispatch of the
  same workflow attaches to it instead of doubling it. Supersedence cancels only a pending,
  cancel-safe validation, never a deployment or a worker.
- A remote failure is classified before another attempt: a code defect goes through the
  ledger's existing intervention rule, so a third equivalent correction needs a diagnosis; a
  remote-only question gets a bounded diagnostic; a transient failure gets the configured
  retry; an external blocker is reported as one.
- `waste_governor` joins the personal and project policy: mode, consolidation, cancellation
  authority, retry budget, preflight checks, trigger mapping, a host-control declaration and
  scoped exception grants. A project may only narrow it. The enforcement level is reported
  as advisory unless the owner declares a host control, and never as a proven sandbox.
- `status` and `internal governor-status` show units, candidates, preflight, the last decision
  and blocker, active and unresolved validation, counters, enforcement and a read-only trigger
  proposal discovered from the repository's workflows.

## 0.1.2

- `doctor` reports a skill where it actually is. An agent reads the conventional
  `~/.agents/skills` as well as its configured home, so on a host whose `CODEX_HOME` points
  elsewhere the diagnostic called the skill `missing` for an agent that was loading it
  perfectly well. It now reports `present_elsewhere` with both the real path and the
  configured one. 0.1.1 stopped setup writing a duplicate in that situation; this stops the
  diagnostic recommending one.

## 0.1.1

- Global setup no longer installs a second active copy beside the one the skills ecosystem
  already placed. A host may point `CODEX_HOME` somewhere other than the agents home, as one
  running Codex inside another tool does; setup now consults the conventional
  `~/.agents/skills/pod` as well, recognises the canonical copy from either side of the
  per-agent symlink, and reports `present_elsewhere` rather than writing. Which copy wins is
  the operator's decision. Found by installing 0.1.0 on such a host.

## 0.1.0

First release under the name `pod`. This is a rewrite, not an upgrade: the retired
`orchestrate` controller is gone, and nothing from it is kept as a compatibility alias.

### What Pod is

An agent skill you invoke inside a Codex or Claude Code session, plus four bounded helper
families. The conversation you are already in stays the coordinator and keeps its context,
model and effort. Orca owns Runs, Tasks, Dispatches, worker placement, messaging and
lifecycle; your project owns source, checks, review and acceptance.

### Installation

- Installs through the agent-skills ecosystem: `npx skills add j3w1/pod --skill pod`, with
  per-agent and global forms, and a release-pinned `j3w1/pod#v0.1.0` source.
- The installed skill carries its own helpers. It needs no source checkout, no editable
  install, no `PYTHONPATH` and no global `pod` executable.
- A Node-free path remains: the explicit `install.py` against a reviewed release, followed by
  `pod setup`.
- A copy the skills CLI owns is detected and never overwritten, removed or claimed. Repeating
  setup on a correct copy is a cheap no-op, and your edits to a placed copy are preserved.

### Routing and delegation

- Each route is established against the installed Orca runtime, replacing a universal
  attestation that no command could satisfy. Establishment names which control backs each
  part of a route and how strongly: enforceable control, supported observation, owner
  configuration, or unavailable.
- An approved route on a subscription login launches even when optional metadata such as a
  quota bucket is unavailable; the gap is disclosed rather than treated as a failure.
- An unknown billing mode, or a paid route without an exact spending grant, still fails
  closed before any native effect.
- A structured launch failure keeps its capacity slot with its recovery metadata instead of
  being retried. A lost response is resolved by reading native state back, never by starting
  a second worker.
- A worker this coordinator never launched no longer blocks admission. A worker started by a
  worker is counted, and refused when policy does not allow it.
- Delivery, settlement and release run through the production adapter: a settlement releases
  once, and a Delivery can only be acknowledged after every item has a durable effect.

### Governance

- A bounded decision check returns ALLOW, WARN or DEFER before a Pod-mediated push, pull
  request update, workflow dispatch, merge, release or deployment, reading only records the
  objective already keeps. A recorded efficiency override never lifts an authorization,
  spending or correctness hold.
- Release authorization is an owner record naming the exact candidate, tree and scope. The
  gate reports technical readiness accurately and separately; passing checks never grant
  permission.

### Removed

- Support for the second platform and its transport: implementation, tests, continuous
  integration, packaging and documentation, not merely the gates. Supported execution
  environment: Linux.
- The comparative three-mode benchmark as a release prerequisite.
- The hard-coded negative assurance values that made every delegation fail closed.

The retired product's own records are preserved unchanged in `docs/history/`.
