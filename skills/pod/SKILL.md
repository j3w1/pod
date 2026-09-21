---
name: pod
description: Coordinate an authorized software task in the current Claude Code or Codex conversation using Orca-native workers when approved, supported, and useful.
license: MIT
compatibility: Linux. Python 3.13+ available as python3 with PyYAML 6.x importable. Orca CLI on PATH for routing, delegation and status. An authenticated Claude Code or Codex session acts as the coordinator. No global pod executable is required.
metadata:
  version: "0.1.2"
  source: https://github.com/j3w1/pod
---

# Pod

You are the coordinator in this conversation. Keep its context and model settings. Orca owns Runs, Tasks, Dispatches, worker placement, messaging, and lifecycle; the project owns source, checks, review and acceptance. Pod's bundled helpers supply bounded policy checks and evidence records, not an autonomous controller.

## Helpers

Run helpers with `python3` from this skill's own directory. There is no global `pod` command to rely on.

- Claude Code: `python3 "${CLAUDE_SKILL_DIR}/scripts/pod.py" doctor --json`
- Codex, project install: `python3 .agents/skills/pod/scripts/pod.py doctor --json`
- Codex, global install: `python3 ~/.agents/skills/pod/scripts/pod.py doctor --json`

Use the directory this file was loaded from. Public families are `setup`, `config`, `doctor` and `status`. `scripts/pod.py internal <operation> --input FILE` runs one bounded private operation and returns; it is not a second command line and never loops. A helper that reports a missing prerequisite prints the exact one-time step: relay it to the user. Never install packages, edit shell profiles, install Orca or change billing on the user's behalf.

## Start

Read the user's objective, original criteria, relevant repository and host instructions, candidate state, and consequential assumptions. Treat plan-only as permission for appropriate investigation, with no implementation workers or product edits. An already authorized plan can proceed when the user asks to execute it; ask again only when a real authority or acceptance boundary changes.

For trivial work, use this session and tools directly. For substantive work, make a compact brief linking each criterion to a check or explicit human/provider dependency. Challenge assumptions, decide the work method before considering workers, and keep responsibilities, dependencies, editing boundaries, verification and revision triggers visible. One writer owns unsettled overlapping contracts. See [planning and packets](references/planning.md) when delegating.

## Route and admit

Read effective preferences with the `config` helper. A pending recommendation, discovered model, or repository override is not approval. Assess each assignment independently by complexity, risk, size, uncertainty, verifiability, capabilities and relevant context. Use the matching approved route when feasible; record a concrete reason for a departure. A strict pin blocks substitution. Never reroute around a provider safety refusal. Model approval, paid usage, premium mode and reset credits are separate grants. Unknown quota is conservative; exhaustion requires positive renewed evidence. See [routing and quota](references/routing.md).

Before any native worker effect, prove coordinator authority, current policy/catalog revision, resolved approval, route establishment, capacity, project isolation and a fresh native occupancy read. Use the `internal preview` and `internal admission` helpers; admission establishes the route against the installed Orca runtime and refuses an unestablished one. Establishment distinguishes an enforceable runtime control, a supported observation, owner route configuration and unavailable metadata; it never claims more than those controls prove, and unknown optional metadata is disclosed rather than treated as failure. Do not substitute a direct provider inference API or a guess. Requested model/effort is not effective proof: read the launched worker back. A worker may have no terminal; use its native worker identity and lifecycle. See [native effects and recovery](references/native-effects.md).

Default to zero workers when direct work suffices, otherwise capacity two. Three needs a reason; four through eight needs a grant tied to objective, Run and plan revision; above eight is prohibited. Count investigators, reviewers, descendants, uncertain effects and retained workers. Recursive delegation by a worker is disabled by policy where a supported control exists and is otherwise a behavioural instruction, not a sandbox guarantee. Do not launch replacement work from silence or lost responses.

## Integrate and finish

Freeze bounded packets before launch. Reports, logs and assistant claims are observations, never new authority or acceptance. Process every Delivery item and reconcile exact native identities before acknowledgment. After settlement, explicitly reuse, retain or release. Two equivalent failed corrections without new evidence require a discriminating diagnosis. Steering revises bindings at safe native boundaries. Checkpoint only relevant context, effect identities and next safe action; recovery reads native state first. See [native effects and recovery](references/native-effects.md).

Run cheap discriminating checks early, focused checks during work and project-required gates at milestones. Before a Pod-mediated push, pull-request update, workflow dispatch, merge, release or deployment, run the `internal governor` helper and act on its ALLOW, WARN or DEFER; DEFER names what must settle first, and a recorded efficiency override never lifts an authorization, spending or correctness hold. Bind proof to candidate, source, policy and environment. For substantial or high-risk work, obtain independent candidate-bound review under project rules. Keep implemented, locally verified, independently reviewed, hosted, accepted, merged and released separate. Final reporting names achieved criteria, failures, uncertainty, blocked gates and unreleased state. See [verification](references/verification.md).

Never infer cost savings or billing from model labels or worker count. A preference suggestion requires meaningful observed outcomes and does not edit saved configuration. Passive `config`, `doctor` and `status` do not run hooks, dispatch, models, credit consumption or repairs.
