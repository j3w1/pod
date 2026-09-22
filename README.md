# Pod

Pod turns your Orca coding session into a pod coordinator.

A pod is a group of orcas working together. This Pod is an agent skill for
[Orca](https://www.onorca.dev/): your current Codex or Claude Code conversation
coordinates a pod of Orca-native workers, choosing where help is useful and
checking what comes back. Your conversation keeps its context, model and effort.

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

Invoke `$pod` in Codex or `/pod` in Claude Code. The session reads your objective,
chooses direct work or delegation, and coordinates through Orca. Small tasks can
stay in the current conversation; larger ones get bounded assignments, approved
routes and checks tied to your original criteria.

| Layer | Responsibility |
| --- | --- |
| **Orca** | Runs, Tasks, Dispatches, messages, environments and worker lifecycle, including Delivery and request recovery. |
| **Pod** | Coordination policy, routing, spending and objective fan-out limits, task packets, evidence and waste control. |
| **Your project** | Source, verification, review, acceptance and release decisions. |

Pod checks policy before a worker starts and verifies the effective route
afterward. It stores the admission decision and evidence bindings; Orca supplies
the authoritative native start result and owns actual workers and capacity; CE
owns physical limits. Pod never infers physical capacity from a fleet census.
The coordinator follows Orca's installed, version-matched
[orchestration guide](https://www.onorca.dev/docs/cli/orchestration) for
supervision and lifecycle.

Before a Pod-mediated Git or CI action, the Governor checks whether the candidate
is ready, an equivalent action is running, or suitable evidence already exists.
Its answer is `ALLOW`, `REUSE` or `DEFER`, with a reason and next action.

## Installation

Install Pod through the agent-skills ecosystem:

```bash
npx skills add j3w1/pod --skill pod
```

Select your agents explicitly, and add `-g` for a user-wide installation:

```bash
npx skills add j3w1/pod --skill pod -a codex -a claude-code -g
```

The default branch contains development work. To install a published release,
pin its tag; `v0.1.2` is the latest published tag at the time of this candidate:

```bash
npx skills add j3w1/pod#v0.1.2 --skill pod -a codex -a claude-code -g
```

This README describes the **0.3.0 candidate**. Earlier tags retain their own
behavior and documentation.

### Prerequisites

- Linux and Python 3.13+ available as `python3`, with PyYAML 6.x importable.
- Orca and its [orchestration skill](https://www.onorca.dev/docs/cli/skills).
  The installed skill resolves the correct CLI and loads the matching guide.
- An authenticated Codex or Claude Code conversation.
- Approved model/account routes for delegation. Subscription login, model
  preference and spending permission are separate checks.

Loading Pod installs nothing. A missing helper dependency produces an explicit
setup instruction; `config` works without a running Orca instance.

### Without Node

From a reviewed checkout of a published release, use the isolated Python installer:

```bash
git clone --branch v0.1.2 --depth 1 https://github.com/j3w1/pod pod-release
python3 pod-release/install.py --venv ~/.local/share/pod/venv \
  --expected-commit "$(git -C pod-release rev-parse v0.1.2^{commit})"
~/.local/share/pod/venv/bin/pod setup --global
```

The installer requires a clean checkout at the expected commit. The placed skill
carries its own helpers and does not depend on that checkout.

## The basic workflow

1. **State the task.** Give Pod your objective and criteria. A planning request
   stays in planning; an approved implementation proceeds within its scope.
2. **Choose useful help.** Pod assesses each assignment and selects an approved,
   usable route. Logical fan-out defaults to two assignments per objective; unknown
   quota permits one outstanding assignment on the affected objective/account route.
   Larger groups require the appropriate justification or scoped grant.
3. **Coordinate the pod.** Workers receive bounded packets with scope, sources
   and reporting expectations. Orca owns their execution and supervision.
4. **Verify the result.** Bind checks and review to the candidate. Related
   corrections converge before remote validation; the Governor reuses matching
   work and evidence when it can.
5. **Report what is proved.** Name achieved criteria, failures, uncertainty and
   remaining gates. Implementation, review, hosted checks and release each need
   their own evidence or authority.

## When something goes wrong

Run helpers from the directory containing the skill you loaded:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/pod.py" doctor --json # Claude Code
python3 ~/.agents/skills/pod/scripts/pod.py doctor --json   # Codex, global
python3 .agents/skills/pod/scripts/pod.py doctor --json     # Codex, project
```

`doctor` explains prerequisites, route establishment and installed copies.
`config` shows effective policy and its provenance. `status` combines current
Orca observations with Pod's admissions, verification context and Governor
decisions. These reads do not dispatch workers, spend credits or repair state.

For runtime recovery, follow the installed Orca guide and the native receipt's
next action. An unavailable response is not proof that a start failed. Pod keeps
unresolved admissions conservative until native evidence resolves them.
An authoritative native `capacity_full` refusal is recorded as deferred without
a successful binding or blind retry; actual capacity remains an Orca/CE fact.

A `migration_required` result needs the explicit state migration described in
the [migration notes](docs/pod-migration.md). Diagnostics never migrate silently.

## What's inside

- One [skill](skills/pod/SKILL.md), with focused references for planning, routing,
  the Orca boundary, verification and the Governor.
- Four public helper families: `setup`, `config`, `doctor` and `status`.
- Personal policy in `~/.config/pod/config.yaml`, with project restrictions in
  `.pod/config.yaml`. A project can narrow personal authority.
- Private admission, checkpoint and evidence records, plus the Governor's
  candidate and remote-action journal.

`skills/pod` is the Python package, installable skill and wheel payload. Each
implementation and policy has one authoring source.

## Philosophy

- **Orca runs the pod.** Use its orchestration features and installed guide as
  the runtime authority.
- **Delegate with purpose.** Match responsibility, context and route to the
  task. Direct work is a valid choice.
- **Keep authority explicit.** Preferences do not grant model access or spending;
  passing checks do not authorize release.
- **Make evidence useful.** Preserve candidate bindings and uncertainty. Report
  what controls prove, and leave unavailable facts unavailable.
- **Avoid repeated work.** Converge related corrections, attach to running
  validation and reuse evidence that still applies. Worker counts and model
  labels are not measurements of savings.

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
