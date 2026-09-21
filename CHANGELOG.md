# Changelog

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
