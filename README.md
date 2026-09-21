# Pod

Pod helps the coding conversation you already have coordinate software work in Orca. It keeps planning and judgment in that conversation, uses a small set of local policy helpers, and leaves native Tasks, Dispatches and workers to Orca.

The current candidate is a **pre-release implementation**. Direct work, policy inspection, routing preview, bounded records and skill installation have offline coverage. Live worker delegation is blocked when the installed runtime cannot prove billing and hidden fan-out controls before launch.

## How it works

Invoke the `pod` skill in Claude Code or Codex. It first reads the objective, original success criteria and project rules. Small jobs stay in the current session. Larger jobs can use a compact plan, independently assessed assignments, explicit route approval and candidate-bound checks. Helpers explain a decision; they do not become a scheduler or launch provider inference calls.

The five starter preferences are pending recommendations. Personal YAML supplies exact agent, model and account approval. Project YAML can refine preferences and tighten restrictions. Quota, billing, capacity and native capabilities can block delegation without blocking safe direct work or diagnosis.

For a personal approval, first enter the exact agent, model and account while leaving `approved: false`. `pod config` displays that route's binding digest. After reviewing the route, set `approved: true`, `approval_ref`, and that exact `approval_route` digest in personal YAML. Changing any route identity invalidates the binding.

## Getting started

Use Python 3.13 or newer on native Linux or Windows. The invoking interpreter must already have pip 22.3 or newer; Pod does not install or upgrade that bootstrap pip. From a clean reviewed checkout, choose a fresh environment outside it:

```sh
python install.py --venv /path/to/new-venv --expected-commit REVIEWED_COMMIT
/path/to/new-venv/bin/pod setup
```

On Windows, run the environment's `Scripts\pod.exe`. The installer creates the target without pip, then uses the invoking interpreter's isolated pip with `--python` pointed at that exact environment. It removes inherited Python and pip control variables only from installer subprocesses, writes only to the selected environment, and prints the install command. `pod setup` enrolls project skills; `pod setup --global` installs user skills without changing the current project. It does not change global pip configuration, install Orca or repair PATH.

Use `$pod` in Codex or `/pod` in Claude Code after skill discovery. Existing modified skill copies are preserved for review.

## Commands

| Command | Purpose |
| --- | --- |
| `pod setup [--global]` | Install owned inline skill and references at the selected scope. |
| `pod config [--check] [--json]` | Read the effective policy and provenance without writing. |
| `pod config --edit [--scope personal|project]` | Edit canonical YAML; personal is the default scope. |
| `pod doctor [--json]` | Read installation, policy and capability diagnostics. |
| `pod status [--run RUN] [--json]` | Read native status; ambiguous Runs require selection. |

Private structured helpers are available through `python -m pod.internal`; they add no global command. The [skill](src/pod/skill/SKILL.md) links the relevant planning, routing, native effect and verification references.

## Current limits and evidence

The [Pod specification](docs/pod-spec.md) defines 59 requirements and 82 scenarios. [Coverage data](docs/pod-coverage.json), [validation gates](docs/validation.md), and [progress](docs/pod-progress.md) distinguish offline fixtures from live, hosted, independent and project acceptance evidence. Missing live controls keep affected operations blocked. WSL and remote runtime ownership have no verified transport contract here.

This branch makes a breaking package and CLI cutover from `orchestrate` to `pod`. It does not rename the repository or canonical checkout, migrate installed state, merge, deploy or publish. The [migration inventory](docs/pod-migration.md) records retired components and preserved recovery obligations.

## Development

Run the unit and incident suite with `python -m unittest discover -s tests -v`, then explicit incident discovery, compile checks and the isolated wheel smoke described in [validation](docs/validation.md). Project governance still decides review, acceptance and release.
