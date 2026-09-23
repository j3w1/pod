# Pod

Pod turns your Orca coding session into the coordinator of a pod of orcas.

Your current Codex or Claude Code conversation decides when help is useful,
assigns bounded work to [Orca](https://www.onorca.dev/) workers, and checks the
result against your goal. The conversation keeps its context and model. Orca
runs and tracks the workers.

## Table of contents

- [How it works](#how-it-works)
- [Installation](#installation)
- [The basic workflow](#the-basic-workflow)
- [When something goes wrong](#when-something-goes-wrong)
- [What's inside](#whats-inside)
- [Philosophy](#philosophy)
- [Updating and removing](#updating-and-removing)
- [Contributing](#contributing)
- [License](#license)

## How it works

Invoke `$pod` in Codex or `/pod` in Claude Code and describe the outcome you
want. Pod can work directly or ask Orca to start workers. It chooses each
assignment's approved model, effort and account route, limits how much work the
objective fans out, and keeps enough evidence to avoid duplicate starts after
a lost response.

| Layer | Responsibility |
| --- | --- |
| **Pod** | Task decisions, approved routes, spending, logical fan-out and evidence. |
| **Orca** | Runs, Tasks, Dispatches, messages, actual worker capacity and lifecycle. |
| **Your project** | Source, checks, review, acceptance and release. |

Pod freezes a route for each admitted assignment and checks Orca's effective
launch result. An unexpected route does not authorize a silent replacement.
Orca's current worker launcher does not yet provide a scoped, noninteractive
startup guarantee before task input. That [runtime dependency](skills/pod/references/orca-boundary.md)
is tracked separately; Pod does not handle provider prompts itself.

For supervision and recovery, the coordinator follows Orca's installed,
version-matched [orchestration guide](https://www.onorca.dev/docs/cli/orchestration).

Before a Pod-mediated Git or CI action, the Governor checks whether the candidate
is ready, an equivalent action is running, or suitable evidence already exists.
Its answer is `ALLOW`, `REUSE` or `DEFER`, with a reason and next action.

## Installation

To try the 0.3.0 code on `main`, install the skill for your agents:

```bash
npx skills add j3w1/pod --skill pod -a codex -a claude-code -g
```

The latest published tag is still `v0.1.2`. Pin that tag if you want the
published version; it does not include the 0.3.0 changes described here:

```bash
npx skills add j3w1/pod#v0.1.2 --skill pod -a codex -a claude-code -g
```

This README describes the **0.3.0 candidate** on `main` after integration.

### Prerequisites

- Linux and Python 3.13+ available as `python3`, with PyYAML 6.x importable.
- Orca and its [orchestration skill](https://www.onorca.dev/docs/cli/skills).
  The installed skill resolves the correct CLI and loads the matching guide.
- An authenticated Codex or Claude Code conversation.
- Approved model/account routes for delegation. Subscription login, model
  preference and spending permission are separate checks.

Loading Pod installs nothing. Missing helper dependencies produce explicit
setup instructions; `config` works without a running Orca instance.

### Without Node

From a reviewed checkout of the published tag, use the explicit Python installer:

```bash
git clone --branch v0.1.2 --depth 1 https://github.com/j3w1/pod pod-release
python3 pod-release/install.py --venv ~/.local/share/pod/venv \
  --expected-commit "$(git -C pod-release rev-parse v0.1.2^{commit})"
~/.local/share/pod/venv/bin/pod setup --global
```

The installer requires a clean checkout at that commit. The placed skill then
runs without the checkout.

## The basic workflow

1. **State the goal.** Give Pod your objective and success criteria. A planning
   request stays in planning; an authorized implementation can proceed.
2. **Choose useful help.** Direct work is often enough. When delegation helps,
   Pod chooses approved routes and normally allows up to two logical assignments
   per objective. Unknown quota permits one outstanding assignment on the
   affected account route; larger groups need justification or a scoped grant.
3. **Coordinate through Orca.** Workers get bounded tasks. Orca handles their
   execution, messages and lifecycle. Pod keeps policy decisions and native
   references, not a second worker manager.
4. **Verify and report.** Checks and review stay tied to the candidate. Pod
   reports what passed, what remains uncertain, and which gates remain open.

## When something goes wrong

Run helpers from the directory containing the skill you loaded:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/pod.py" doctor --json # Claude Code
python3 ~/.agents/skills/pod/scripts/pod.py doctor --json   # Codex, global
python3 .agents/skills/pod/scripts/pod.py doctor --json     # Codex, project
```

`doctor` checks prerequisites and route evidence; `config` shows effective
policy; `status` summarizes Pod decisions alongside current Orca observations.
These reads do not dispatch workers or repair state.

For a lost or uncertain native response, follow Orca's request recovery
instructions. Pod holds the logical assignment until the same attempt is
resolved. If Orca definitely refuses a start with `capacity_full`, Pod records
the task as deferred. It does not audit the fleet or retry blindly.

If `doctor` reports `migration_required`, use the explicit
[state migration guide](docs/pod-migration.md). Diagnostics do not migrate state.

## What's inside

- One [skill](skills/pod/SKILL.md), with focused references and bundled helpers.
- Four public helper families: `setup`, `config`, `doctor` and `status`.
- Personal policy in `~/.config/pod/config.yaml`, with project restrictions in
  `.pod/config.yaml`. A project can narrow personal authority.
- Private records for Pod admissions, checkpoints, evidence and Governor
  decisions. Orca remains the source of truth for workers.

`skills/pod` is the Python package, installable skill and wheel payload. Each
implementation and policy has one authoring source.

## Philosophy

- A pod is useful only when its members have distinct work. Pod can choose zero
  workers for a small task.
- Pod decides how many assignments an objective should request. Orca decides
  whether the runtime can start them.
- A chosen route stays fixed for that assignment. Failed starts and uncertain
  responses become evidence for a new decision, never permission to improvise.
- Verification, hosted checks, live provider behavior and release each need
  their own proof. [Current progress](docs/pod-progress.md) names the open gates.

## Updating and removing

Use the manager that installed the skill:

```bash
npx skills update pod -g
npx skills remove pod -g -a codex -a claude-code
```

For the Python installer, upgrade the selected environment from a reviewed
checkout and run its `pod setup` again. Setup preserves modified copies and
recognizes installations owned by the skills CLI.

Version 0.3.0 changes private state and JSON envelopes. Read the
[migration notes](docs/pod-migration.md) before upgrading or rolling back;
installing an older skill does not downgrade existing state.

## Contributing

Read [AGENTS.md](AGENTS.md), the [specification](docs/pod-spec.md) and the
[validation gates](docs/validation.md). Start with:

```bash
PYTHONPATH=skills python -m unittest discover -s tests -v
PYTHONPATH=skills python -m pod.skill_validation skills/pod
python tools/platform_audit.py
```

[Progress](docs/pod-progress.md) records the evidence and open gates. Retired
product records remain unchanged in [docs/history](docs/history/).

## License

MIT. See [LICENSE](LICENSE).
