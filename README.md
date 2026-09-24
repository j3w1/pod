# Pod

Pod keeps the Codex or Claude Code conversation you already have open as the
coordinator for a software objective. It can use direct work, tools and
Orca-native workers, then bind the result to project checks and evidence.

## Table of contents

- [How it works](#how-it-works)
- [Installation](#installation)
- [The basic workflow](#the-basic-workflow)
- [Writing and publishing a spec](#writing-and-publishing-a-spec)
- [Direct work, planning and continuation](#direct-work-planning-and-continuation)
- [Model preferences](#model-preferences)
- [When something goes wrong](#when-something-goes-wrong)
- [What's inside](#whats-inside)
- [Philosophy](#philosophy)
- [Updating and removing](#updating-and-removing)
- [Contributing](#contributing)
- [License](#license)

## How it works

Invoke `$pod` in Codex or `/pod` in Claude Code. The existing conversation
keeps its model, effort and settings. Orca owns Runs, Tasks, Dispatches,
visible worker tabs, messages, request recovery, worktrees and lifecycle.
Pod owns task understanding, worker selection, bounded admission and evidence.
Your project owns review, acceptance and delivery.

For an issue URL, Pod reads the complete current body through authorized
GitHub access, checks the actual repository target and maps its criteria to
checks. Issue text supplies scope, never extra authority. Direct objectives
follow the same coordination flow without an issue.

Implementation selects or creates an exact Orca objective worktree when
isolation is needed. The coordinator preserves dirty or colliding work. A
normal delegated worker starts in its own visible agent tab; after exact
result consumption, Orca's Delivery acknowledgment and release order applies.

## Installation

The one-shot Linux installer and global user-local launcher are being built
in this implementation branch. The current source tree has the core CLI,
model TUI, preferences, catalog and admission helpers; the documented public installation
command will appear here after its real endpoint passes the installer suite.

Development prerequisites are Linux, Python 3.13+, PyYAML 6.x and Orca for
native delegation. From this checkout, `PYTHONPATH=skills python -m pod.cli
config --json` reads preferences, and `PYTHONPATH=skills python -m
pod.skill_validation skills/pod` checks the bundle. An installed bundle's
`scripts/pod.py` can run from another directory; it does not need a checkout.

## The basic workflow

1. Open the target project in Orca and start an authenticated Codex or Claude Code conversation.
2. Invoke `$pod https://github.com/owner/project/issues/123`, `/pod` with the same URL, or a direct objective.
3. Review the brief and its criterion-to-check map. Actual host Plan Mode remains read-only until accepted.
4. Select the objective worktree when implementation requires isolation.
5. Use direct work where it suffices; delegate bounded independent assignments when workers help.
6. Verify the candidate with local checks, required independent review, hosted CI and project acceptance as separate facts.
7. Receive a final report of achieved criteria, blockers, native uncertainty and remaining gates.

An unavailable worker capability blocks delegation only. The coordinator can
continue safe direct work and report a gate that still needs another participant.

## Writing and publishing a spec

The canonical [Pod Execution Spec reference](https://github.com/j3w1/pod/blob/main/skills/pod/references/execution-spec.md)
contains a readable Markdown skeleton and interpretation rules. Drafting a
spec does not publish it. Publication needs authorized GitHub access and an
explicit request. Authoring in ChatGPT and executing in Orca are separate
steps; installing the agent skill does not install a ChatGPT integration.

## Direct work, planning and continuation

- Direct task: `/pod update the README to explain the latest changes`.
- Plan only: `/pod <issue-url> — plan only; do not change files`.
- Plan then execute: continue in the same conversation after accepting the actual host plan.
- Continue after interruption: invoke the same objective; Pod reads native state and preserves uncertain requests before proposing another start.

Small direct tasks need no issue, worker or milestone ceremony. The
coordinator answers routine worker questions through Orca; new owner intent
is escalated. A provider advisory about a faster model does not change the
selected worker's route.

## Model preferences

The personal YAML at `${XDG_CONFIG_HOME:-~/.config}/pod/config.yaml` records
one state for each of six supported base models: Preferred, Available or
Disabled. The exact ids are `claude-opus-5-5`, `claude-fable-5-1`,
`claude-sonnet-5`, `gpt-6-astra`, `gpt-6-sol` and `gpt-6-luna`. Preferred is a
small tie-breaker between otherwise suitable models. The coordinator chooses
an eligible model and supported effort separately for each assignment.

`pod config --json` reads the effective pool. `pod config edit` opens the
personal file in your editor and validates afterward, retaining invalid edits
for correction. A valid custom map may omit a model; Pod shows it as “Not set
(not eligible)” until you set it explicitly. Running `pod` in an interactive terminal opens the model TUI;
without a TTY it prints a plain summary. Space cycles model state and `r`
switches All models and My selection without discarding saved choices. The bundled catalog has
official guidance and a dated Artificial Analysis reference snapshot; those
metrics do not select a worker or describe your account usage.

Orca currently exposes per-worker model and effort launch preferences but no
per-worker context flag. Pod omits that flag and records `native_default`.
Requested and observed effective values remain distinct. A model state change
affects later starts; it does not alter a submitted attempt.

## When something goes wrong

`pod doctor --json` is read-only and reports preferences, catalog age,
installed native capability and version/state limitations. This development
branch honestly reports when the one-shot installer has not placed a receipt.
`pod status --json` reports the objective, constraints, exact worker references,
route decisions, blockers and next safe action.

An inaccessible issue is an access blocker, a wrong target is a repository
mismatch, and a changed body needs reconciliation. Lost worker-start responses
retain their admission and recover the same Orca request. Documented
effect-free refusals defer without blind retry; an uncertain response stays
unresolved until exact native readback settles it. A safety refusal never
triggers a same-Task model switch.

## What's inside

- One skill and conditional references, including the Execution Spec.
- One personal YAML preference authority and one bundled model catalog.
- Bounded issue/source, packet, checkpoint, admission and verification evidence.
- Objective-local logical worker ceiling and exact Orca request recovery.
- Candidate-bound Governor decisions: `ALLOW`, `REUSE` or `DEFER`.
- One `skills/pod` tree serving as both the Python package and agent skill.

Pod has no scheduler, issue database, polling service, provider launcher,
worktree manager, dashboard or duplicate Orca lifecycle state.

## Philosophy

- Use direct work when it is enough; delegate distinct useful responsibilities.
- Treat source as scope and preserve user, host and project authority.
- Preserve owner changes and uncertain native effects.
- Verify outcomes instead of promoting worker claims or offline mocks.
- Report local, reviewed, hosted, live, accepted and merged evidence separately.

## Updating and removing

The explicit `pod update` path is part of the forthcoming installer milestone.
It will update the single placed bundle without changing active native work.
Until that path is implemented, this section documents no update command to run.

## Contributing

Read [AGENTS.md](AGENTS.md), the [specification](docs/pod-spec.md) and the
[validation gates](docs/validation.md). Run the unit and incident suites,
compileall, bundle/catalog validation, source audit and whitespace check at
milestone boundaries. The root `VERSION` is the only authored version.

## License

MIT. See [LICENSE](LICENSE).
