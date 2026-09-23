---
name: pod
description: Coordinate a software task as a pod of Orca-native workers from the current Codex or Claude Code conversation, with routing, admission policy and candidate-bound evidence.
license: MIT
compatibility: Linux. Python 3.13+ as python3 with PyYAML 6.x. Orca CLI and its version-matched orchestration guide for delegation. An authenticated Codex or Claude Code conversation coordinates. No global pod executable required.
metadata:
  version: "0.3.0"
  source: https://github.com/j3w1/pod
---

# Pod

Turn this Orca coding session into a pod coordinator. Keep the conversation's
context and model settings. Orca owns runtime orchestration; Pod supplies
coordination policy, routing, evidence and waste control; the project owns
verification, review and acceptance.

Before native orchestration, load Orca's installed orchestration skill and its
version-matched guide using the executable that skill resolves. Follow that guide
for runtime actions. Read [the Orca boundary](references/orca-boundary.md) when
admitting work or recovering Pod state.

## Choose and bound the work

Read the objective, original criteria, relevant host/project instructions and
candidate. Challenge consequential assumptions. Plan-only permits investigation,
with no implementation workers or product edits. An authorized plan proceeds
when the user requests implementation; stop dependent work only at a changed
authority or acceptance boundary.

Choose direct work, tools or delegation before choosing a model. For substantive
work, connect every criterion to a check or explicit dependency. Keep the brief,
editing boundaries and revision triggers compact. Read [planning and
packets](references/planning.md) when decomposing or delegating.

## Route and admit

Read effective preferences with `config`. Assess each assignment's complexity,
risk, size, uncertainty, verifiability, capabilities and context. Prefer the
matching approved route; record a reason for a departure and honor strict pins.
Once admitted, the exact model, effort and account route is immutable: never
silently substitute it or ask a worker or user to choose another model mid-attempt.
An unavailable route or startup failure holds/fails that attempt and requires a
fresh policy decision. Never reroute around a provider safety refusal. Read [routing and
quota](references/routing.md) for approval, spending and availability decisions.

Freeze the packet, then use `internal preview` and `internal admission` before a
Pod-managed start. Admission enforces current authority, route, spending,
objective-local logical fan-out and source bindings and checks native effective
launch evidence. Pod does not inspect or infer physical worker capacity.
Unproven required controls block the affected route; optional gaps remain visible.

Use zero workers when sufficient, otherwise default logical fan-out two. Three needs a
reason; four through eight needs a grant bound to the objective, Run and plan;
above eight is prohibited. Investigators, reviewers and authorized descendants
share the ceiling through one reservation per assignment. Worker-initiated
delegation requires explicit authority. Native `capacity_full` defers that
delegation; do not retry it blindly or audit the fleet to challenge Orca.

## Integrate and verify

Use Orca's supervision and lifecycle contract. Treat worker reports as
observations; validate them against the frozen packet and current native binding.
Steering invalidates affected packets and evidence. After two equivalent failed
corrections without new evidence, require a discriminating diagnosis. Checkpoint
only relevant decisions, evidence gaps, native references and the next safe action.

Run focused checks during work and the project's required milestone gates.
Obtain independent review when project policy or substantial risk requires it.
Read [verification](references/verification.md) to bind proof and report outcomes.

Before a Pod-mediated Git, CI, release or deployment action, read [the
Governor](references/governor.md). Prepare the candidate and required preflight,
then follow `ALLOW`, `REUSE` or `DEFER`. Efficiency exceptions never lift
authorization, spending or correctness holds.

## Helpers

Use `python3` and the directory this skill was loaded from:

- Claude Code: `python3 "${CLAUDE_SKILL_DIR}/scripts/pod.py" doctor --json`
- Codex, project: `python3 .agents/skills/pod/scripts/pod.py doctor --json`
- Codex, global: `python3 ~/.agents/skills/pod/scripts/pod.py doctor --json`

Public families are `setup`, `config`, `doctor` and `status`.
`scripts/pod.py internal <operation> --input FILE` performs one bounded private
operation. Relay missing-prerequisite instructions; loading the skill never
installs packages, changes profiles or billing, or overwrites project files.
Passive diagnostics do not dispatch, spend or repair state.

Final reporting names achieved criteria, failures, uncertainty, candidate evidence
and remaining gates. Keep implemented, locally verified, independently reviewed,
hosted, accepted, merged and released distinct. Never infer cost savings from
worker count or model labels, or edit preferences from observations alone.
