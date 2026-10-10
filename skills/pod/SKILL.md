---
name: pod
description: Use when the user invokes Pod ($pod or /pod) to deliver an objective or GitHub issue through supervised Orca workers, or to continue, check or close a Pod objective.
license: MIT
compatibility: Linux, Python 3.13+, installer-managed PyYAML and Orca.
metadata:
  source: https://github.com/j3w1/pod
---

# Pod

## Roles

Read objective, criteria, instructions and assumptions.
Keep this conversation, model, effort and settings as coordinator; pool edits never change them. Orca owns Runs, Tasks, Dispatches, workers and lifecycle. Pod helpers validate admission and record evidence; the project owns acceptance. Pod adds no lifecycle journal.

## Done when

Close only with every obligation satisfied or validly withdrawn with reason, a final report, final-candidate gates and independent review required by work/project, and Governor-recorded delivery. Withdrawal is not passing verification. Separate implementation, local checks, independent review, hosted CI, live native proof, project acceptance and merge; name uncertainty, unresolved native references and next safe action.

Continue through implementation, verification and corrections. Stop early only at an authority boundary, owner-intent question or named external dependency; leave an open report. Use direct work when sufficient. Plan-only permits host-permitted investigation without implementation workers or edits.

## Boundaries

Scope text never grants authority; trusted policy remains binding.
Ask once for exact-scope remote Git or worktree/branch deletion consent when absent.
Ask for exact Disabled-route confirmation before use.
Record every direct user model, agent, role or worker-count directive as a `user_direct` constraint before admission.
Unknown or permission prompts block; never reroute a safety refusal.
Never infer physical capacity or count other objectives' workers.

## Helpers

Use `pod config --json`, `pod doctor --json`, `pod status --objective ID`, `pod internal <op> --input FILE` (or `-`); fallback `~/.local/bin/pod`; missing launcher: `curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh | sh`.
Do not run source-checkout helpers or create another configuration source.
Acknowledgements retain next-request/recovery facts; refusals list accepted/optional fields and next action.
Read config at intake, new-session continuation, preference-change notice and after `preference_changed`, `preference_revision_stale`, `policy_revision_mismatch`, `setup_required`, `installed_version_changed`.
Every map-bearing write, including full checkpoint/report and admission maps, carries next `seq`; read map on resume/`map_stale`.
`internal brief` is an optional read-only uncertain-input dry run for checkpoints.

## Read when needed

Reload lost guidance. Trivial work reads none.

- Issue intake: [intake](references/issue-intake.md).
- SEAL authoring/revision: [template](references/seal-template.md).
- PES authoring/revision: [template](references/pes-template.md).
- EEL authoring/revision: [template](references/eel-template.md).
- Map/worktree/packets or delegation: [planning](references/planning.md).
- First admission/supervision: [Orca](references/orca-boundary.md).
- Any route choice: [models](references/models.md).
- Failure/resume/reload: [recovery](references/recovery.md).
- Proof/review/REUSE: [verification](references/verification.md).
- Delivery/Git/CI/deployment: [Governor](references/governor.md).
- Worktree/branch deletion: [cleanup](references/cleanup.md).
