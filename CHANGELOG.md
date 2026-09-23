# Changelog

The section whose heading matches `pod.__version__` is the body of that version's GitHub
Release. A push to `main` that declares a version with no tag publishes it.

## 0.4.0 — 2026-09-23

### Coordination boundary

- Orca owns Runs, Tasks, Dispatches, request recovery, placement, messaging, actual
  capacity and worker lifecycle. Pod keeps objective-level admission, routing, spending and
  evidence: one logical reservation per assignment, freed by exact native assignment
  settlement, with no fleet census, Delivery, cleanup or release state of its own.
- Normal workers start through native `worker-start` into visible agent tabs. After a
  report is validated and preserved, Delivery acknowledgment and worker release follow
  Orca's order promptly; uncertain or protected resources are retained.

### Issue-first workflow

- A GitHub issue URL or a direct objective enters one workflow. The complete issue is read
  through authorized `gh` access, its target is checked against the actual checkout, and
  checkpoints and packets bind its identity and body digest without copying the body.
- The `references/execution-spec.md` reference defines the readable Pod Execution Spec
  with numbered Proof of Done items and an explicit delivery endpoint.
- Implementation binds an Orca-managed objective worktree, normally on an
  `orca/<task-slug>` branch, by Git common directory, branch and path. Linked worktrees
  share one objective state, and canonical private project policy can only be narrowed.

### Routing and admission

- The catalog is exactly Luna, Sol and Astra on Codex and Sonnet, Opus and Fable on
  Claude. Routes carry effort plus a `256k` (256,000-token upper bound) or `max` context
  profile and keep requested and proven effective limits distinct. Where the installed Orca
  exposes no per-worker context control, a context-dependent route refuses before effect.
- `config approve ALIAS` and `config revoke ALIAS` recheck the joined native account, auth
  and billing evidence, show a redacted proposal, require explicit confirmation and edit
  only the personal YAML. Direct work needs no approved route.
- Orca's redacted selected-account identity is the one key for approval, quota and grants.
  Identity and billing proof come from the same selected context and are re-read
  immediately before a start or pending replay; a rotated account is never relabeled.

### Recovery

- Orca-issued request UUIDs join completed, pending and absent recovery to the same
  admission. A contradictory request identity stays unresolved across later incomplete
  responses, and nothing starts a replacement.
- Orca's documented effect-free refusals `task_not_found`, `task_not_startable` and
  `inject_rejected` are recorded as durable `deferred` admissions with no binding and no
  blind retry. `runtime_error` holds `unresolved` until request and worker readback settle
  it. Any other refusal code stays fail-closed.

### Governor

- The governor is a deterministic kernel inside the execution path. Work is grouped into
  delivery units with candidate generations frozen from Git: commit, tree, base, workflow
  digests, verification commands, toolchain, environment and policy revision. A changed
  workflow or base is a new candidate even on the same commit.
- Decisions are `ALLOW`, `REUSE` or `DEFER` with the reason and next action; `WARN` is an
  annotation. A push that starts CI is validation and needs the configured local preflight.
  An identical running action is attached to, and a passing result for the same candidate
  and context is reused.
- `internal governor-execute` performs push, pull-request reuse or creation, workflow
  dispatch, rerun and cancellation against the bound commit through an exact `git` and `gh`
  allowlist. A lost response stays `UNKNOWN` and `internal governor-reconcile` settles it
  from provider readback without resubmitting. Supersedence cancels only a pending,
  cancel-safe validation.
- Remote failures are classified before another attempt: a code defect goes through the
  intervention ledger, a remote-only question gets a bounded diagnostic, and enforcement is
  reported as advisory unless the owner declares a host control.

### Interfaces and state

- Public helper families remain `setup`, `config`, `doctor` and `status`. Private state is
  `pod-context/v3` with admissions `reserved`, `bound`, `unresolved`, `closed` and
  `deferred`; CLI envelopes are `pod-cli/v3`. A state record or governor journal in another
  schema is reported by `doctor` and `status`, blocks that objective, and is never converted.
- `policy.retain_idle_minutes` is not a configuration key; worker retention and disposition
  are explicit Orca operations.

### Release process

- A push to `main` whose declared version has no tag creates the annotated tag and the
  GitHub Release with the wheel, sdist, skill bundle archive and checksums. This file is
  the only release-notes source, and a version bump without a section fails the checks.

### Compatibility

- Developed against Orca 1.4.209. Capabilities are discovered from the installed runtime per
  operation; no minimum version is enforced. Supported execution environment: Linux with
  Python 3.13+ and PyYAML 6.x.

## 0.1.2 — 2026-09-21

- `doctor` reports a skill where it actually is. An agent reads the conventional
  `~/.agents/skills` as well as its configured home, so on a host whose `CODEX_HOME` points
  elsewhere the diagnostic called the skill `missing` for an agent that was loading it
  perfectly well. It now reports `present_elsewhere` with both the real path and the
  configured one. 0.1.1 stopped setup writing a duplicate in that situation; this stops the
  diagnostic recommending one.

## 0.1.1 — 2026-09-21

- Global setup no longer installs a second active copy beside the one the skills ecosystem
  already placed. A host may point `CODEX_HOME` somewhere other than the agents home, as one
  running Codex inside another tool does; setup now consults the conventional
  `~/.agents/skills/pod` as well, recognises the canonical copy from either side of the
  per-agent symlink, and reports `present_elsewhere` rather than writing. Which copy wins is
  the operator's decision. Found by installing 0.1.0 on such a host.

## 0.1.0 — 2026-09-21

First release under the name `pod`. Pod replaces the earlier controller and keeps nothing
from it as a compatibility alias.

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

### Governance

- A bounded decision check returns ALLOW, WARN or DEFER before a Pod-mediated push, pull
  request update, workflow dispatch, merge, release or deployment, reading only records the
  objective already keeps. A recorded efficiency override never lifts an authorization,
  spending or correctness hold.
- Release authorization is an owner record naming the exact candidate, tree and scope. The
  gate reports technical readiness accurately and separately; passing checks never grant
  permission.

### Removed

- Support for a second platform and its transport: implementation, tests, continuous
  integration, packaging and documentation. Supported execution environment: Linux.
- The comparative three-mode benchmark as a release prerequisite.
- The hard-coded negative assurance values that made every delegation fail closed.
