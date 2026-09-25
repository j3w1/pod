# Pod

Pod helps a Codex or Claude Code conversation coordinate a software objective. It can use direct work, tools, or Orca workers while keeping the current conversation in charge.

## Install

On Linux, with Python 3.13+ (including `venv`), Node/npx, curl or wget, and access to GitHub and PyPI:

```sh
curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh | sh
```

This installs the Pod skill for Codex and Claude Code, a user-local `pod` command, and a small isolated Python dependency environment. It creates six Available model preferences if no personal file exists. Open a new shell if the installer adds `~/.local/bin` to your PATH. For a download, inspect, then run path and removal details, see [installation and security](docs/installation.md).

## Contents

- [How it works](#how-it-works)
- [The basic workflow](#the-basic-workflow)
- [Plan, direct work, and continuation](#plan-direct-work-and-continuation)
- [Models and the terminal view](#models-and-the-terminal-view)
- [Troubleshooting](#troubleshooting)
- [What's inside](#whats-inside)
- [Philosophy](#philosophy)
- [Updating and removing](#updating-and-removing)
- [Contributing](#contributing)

## How it works

Your existing authenticated session remains the coordinator, with its current model and effort. Orca owns Runs, Tasks, Dispatches, worker tabs, messages, request recovery, worktrees, and lifecycle. Pod makes assignment choices, checks them at admission, and binds evidence to the objective. Your project decides what counts as accepted.

Pod reads an issue completely before using it as scope. It checks that the issue belongs to the actual repository and notices material body changes. Issue text cannot grant authority. A direct objective follows the same process without an issue.

## The basic workflow

1. Open the project in Orca and start a Codex or Claude Code conversation.
2. In Codex, say `$pod https://github.com/owner/project/issues/123`; in Claude Code, use `/pod <issue-url>`. You can also give either skill a direct objective.
3. Review the objective and criterion-to-check map when the task warrants one. Plan Mode remains read-only until you accept the plan.
4. Let the coordinator do simple work directly and delegate bounded independent assignments only when useful. Workers follow Orca's native launch path and your setting for new agent tabs. `pod status` reports placement and any native discoverability warning.
5. Review the local checks, independent review, hosted CI, and project acceptance as separate evidence. Pod reports remaining gates and uncertain native work instead of assuming success.

For persistent issue-backed work, the [Pod Execution Spec reference](https://github.com/j3w1/pod/blob/main/skills/pod/references/execution-spec.md) gives a readable format with numbered Proof of Done items. Authoring in ChatGPT and executing in Orca are separate steps; installation adds no ChatGPT integration.

## Plan, direct work, and continuation

- Direct task: `/pod update the README to explain this feature`.
- Plan only: `/pod <issue-url> — plan only; do not change files`.
- Plan then execute: accept the host plan and continue in the same conversation.
- Continue after interruption: invoke the same objective; Pod reads the native state and reconciles unresolved requests before another start.

A small task can finish with zero workers. Missing Orca delegation support blocks delegation while safe direct work and diagnosis continue. Pod does not configure Orca or sign in to your agent applications.

## Models and the terminal view

`pod` opens an optional model TUI in a terminal; without a TTY it prints a short summary. The TUI shows the six supported base models, focus-driven Details, native capability information, and a dated Artificial Analysis reference. Space cycles Available, Preferred, and Disabled; `r` switches My selection and All models while retaining saved choices. Search, sort, and provider filters affect display only.

The personal YAML at `${XDG_CONFIG_HOME:-~/.config}/pod/config.yaml` is the single model preference authority. `pod config --json` shows the effective pool and path; `pod config edit` opens that file in your editor. A custom map can leave a model unset, which means not eligible. The six exact model ids are `claude-opus-5-5`, `claude-fable-5-1`, `claude-sonnet-5`, `gpt-6-astra`, `gpt-6-sol`, and `gpt-6-luna`.

The coordinator chooses a suitable eligible model and supported effort for each assignment. Preferred is only a modest tie-breaker. Orca has per-worker model and effort preferences but no scoped context flag, so Pod omits a context flag and records `native_default`. Catalog benchmarks are reference data, not a promise of native availability, billing, or the model Pod will choose.

## Troubleshooting

`pod doctor --json` reads installation ownership, version and bundle integrity, placements, preferences, catalog age, and available Orca capability without starting a worker. `pod status --json` shows objective scope, constraints, native references, route decisions, blockers, and the next safe action. A missing or invalid preference file leaves no eligible models; use `pod config edit` to correct it.

Lost worker-start replies retain the same Orca request for reconciliation. A known effect-free refusal is deferred; an uncertain response remains unresolved until exact native readback. A provider safety refusal never triggers a same-Task model switch. See [installation troubleshooting](docs/installation.md) for PATH, duplicate skills, and interrupted installs.

## What's inside

- One `skills/pod` tree serves as the skill and importable Python package.
- One personal YAML file stores model states and the logical worker ceiling.
- One bundled catalog holds official model guidance and a dated benchmark snapshot.
- A bounded admission/checkpoint record joins Pod decisions to Orca references.
- The Governor keeps candidate-bound `ALLOW`, `REUSE`, and `DEFER` decisions.

Pod has no scheduler, provider launcher, model account manager, billing system, dashboard, or parallel Orca lifecycle database.

## Philosophy

- Choose direct work and tools before deciding to delegate.
- Let the user control eligibility while the coordinator judges suitability.
- Preserve user edits and uncertain native effects.
- Verify outcomes against the project's criteria.
- Name local, reviewed, hosted, live, accepted, and merged results separately.

## Updating and removing

Run `pod update` to fetch the current `main` bundle through the same staged installer. It preserves valid preferences and changed skill copies, and tells active coordinators to reload before new starts; running workers are untouched. The installer owns only identified user-local paths. See [the removal map](docs/installation.md#removal) before deleting anything. There are no release packages or tags.

## Contributing

Read [AGENTS.md](AGENTS.md), the [specification](docs/pod-spec.md), and the [validation gates](docs/validation.md). The root `VERSION` is the only authored product version. Run the full offline and PTY gates before proposing a change.

## License

MIT. See [LICENSE](LICENSE).
