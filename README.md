# Pod

Pod turns your Orca coding session into a orca pod coordinator. It is an agent skill: you
invoke it inside Codex or Claude Code, and that conversation keeps its context, its model and
its effort while it plans the work, decides what deserves a worker, launches those workers
through Orca, and verifies what comes back.

Pod is not a second agent, a scheduler or a daemon. Orca owns Runs, Tasks, Dispatches, worker
placement, messaging and lifecycle. Your project owns source, checks, review and acceptance.
Pod owns the policy between them.

Supported execution environment: Linux.

## Table of contents

- [How it works](#how-it-works)
- [Installation](#installation)
- [The basic workflow](#the-basic-workflow)
- [When something goes wrong](#when-something-goes-wrong)
- [What's inside](#whats-inside)
- [Philosophy](#philosophy)
- [Updating, rolling back and removing](#updating-rolling-back-and-removing)
- [Contributing](#contributing)
- [License](#license)

## How it works

You type `$pod` in Codex or `/pod` in Claude Code. The skill loads into the conversation you
are already having. Nothing restarts, nothing forks, and your model and effort are unchanged.

From there the session reads your objective and decides how to do the work. Trivial work it
does itself. For substantial work it writes a compact brief that ties each of your criteria
to a check or to an explicit dependency, then decides whether a worker would actually help.

When it would, Pod establishes the route before anything is launched: which agent, which
model, which account, what the installed Orca runtime can actually enforce, and what it can
only observe. A route that is approved and running on a subscription login launches. A route
whose billing mode is unknown, or a paid route with no spending grant, does not.

After launch, Pod reads the worker back rather than trusting the request. It reconciles the
exact native identity, processes every Delivery item, releases the worker once, and records
what was proved and what was not.

## Installation

Pod installs through the agent-skills ecosystem. Pick the agents you use:

```bash
npx skills add j3w1/pod --skill pod
npx skills add j3w1/pod --skill pod -a codex -a claude-code
npx skills add j3w1/pod --skill pod -a codex -a claude-code -g
```

The first form installs into the current project; `-g` installs for your user. To pin a
release instead of tracking the default branch, name the tag as a fragment:

```bash
npx skills add j3w1/pod#v0.1.0 --skill pod -a codex -a claude-code -g
```

### Prerequisites

- **Linux.**
- **Python 3.13 or newer**, available as `python3`.
- **PyYAML 6.x**, importable by that interpreter. If it is missing, the helper tells you the
  one command to run. Pod never installs it for you.
- **Orca**, on `PATH` as `orca`, or named by `ORCA_CLI_COMMAND`. Needed for routing,
  delegation and status; `config` works without it.
- **An authenticated Codex or Claude Code session**, which is the coordinator.

Loading the skill installs nothing, edits no shell profile, and overwrites no project file.

### Without Node

The skills CLI is a convenience, not a runtime. To install from a reviewed release instead:

```bash
git clone --branch v0.1.0 --depth 1 https://github.com/j3w1/pod ~/src/pod
python3 ~/src/pod/install.py --venv ~/.local/share/pod/venv \
  --expected-commit "$(git -C ~/src/pod rev-parse v0.1.0^{commit})"
~/.local/share/pod/venv/bin/pod setup --global
```

The installer refuses a dirty or unexpected checkout, creates the environment itself, and
touches nothing outside it. The skill it then places carries its own helpers, so that
environment is only the installer.

## The basic workflow

1. **Invoke it.** `$pod` or `/pod`, inside the work you are already doing.
2. **Agree on the objective.** Pod restates your criteria and names the assumptions it is
   making. Plan-only stays plan-only: it investigates, it does not edit or launch.
3. **Let it choose the method.** Zero workers when direct work is enough. Two by default.
   Three needs a reason. Four to eight needs a grant naming the objective, Run and plan.
   Above eight is refused.
4. **Watch it delegate.** Each worker gets a frozen packet: scope, actions, bound sources,
   and the report it owes back. Pod counts what is running, including anything a worker
   started, and refuses to launch from silence.
5. **Let it verify.** Worker output is an observation, not an acceptance. Pod binds proof to
   the exact candidate, source, policy and environment, and keeps "implemented", "locally
   verified", "reviewed", "hosted", "accepted", "merged" and "released" apart.
6. **Read the report.** It names what was achieved, what failed, what is uncertain, and what
   is still blocked.

## When something goes wrong

Run the helpers from the skill directory. There is no global command to install:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/pod.py" doctor --json   # Claude Code
python3 ~/.agents/skills/pod/scripts/pod.py doctor --json     # Codex, global install
python3 .agents/skills/pod/scripts/pod.py doctor --json       # Codex, project install
```

- **`doctor`** reports the installed bundle, your prerequisites, which routes the runtime
  actually establishes, and where your skill copies live. It changes nothing.
- **`status`** joins a Run's native worker states to your local decision record.
- **`config`** shows the merged policy and where each value came from.

Common answers: a **blocked route** names the control that is missing, not a generic refusal.
**Unknown quota** is conservative by design, allowing one new worker on that account rather
than halting the objective. Orca caches its provider quota figures and does not refresh them
on read, so a reading is often stale enough to count as unknown. That is why two workers is
the policy default rather than a promise. An **uncertain launch** keeps its slot until native state is read
back, because a lost response is not proof that nothing started.

## What's inside

- **The skill.** One `SKILL.md` of coordination policy, with focused references for
  [planning](skills/pod/references/planning.md),
  [routing](skills/pod/references/routing.md),
  [native effects](skills/pod/references/native-effects.md) and
  [verification](skills/pod/references/verification.md), loaded only when needed.
- **Four public helper families.** `setup`, `config`, `doctor` and `status`. Nothing else is
  a command.
- **Your policy.** `~/.config/pod/config.yaml` holds your approved routes and limits.
  A project may narrow them in `.pod/config.yaml`; it can never widen them.
- **A private record per objective.** What was launched, what settled, what is still
  uncertain, and enough to resume after an interruption.

## Philosophy

**Approval, preference and payment are three different things.** A model you like is not a
model you approved, and an approved model is not permission to spend.

**Claim only what a control proves.** Pod says which controls back a route and how strongly:
some the runtime enforces, some it merely reports, some you configured, and some are simply
unavailable. Refusing a worker's own delegation is a policy decision, not a sandbox.

**Unknown stays unknown.** Missing evidence is reported as missing. Passing tests never
become permission, and a projection never withholds a readiness fact it can prove.

**An effect you cannot see still happened.** A lost response, a delayed output or an absent
terminal never justifies starting a second worker.

## Updating, rolling back and removing

One manager per installation. If the skills CLI installed it, keep using it:

```bash
npx skills update pod -g                         # update
npx skills add j3w1/pod#v0.1.0 --skill pod -g    # roll back to a release
npx skills remove pod -g -a codex -a claude-code # remove only Pod
```

If you installed with `install.py`, re-run `pod setup` after upgrading the environment.
Either way, `pod setup` recognises a copy the skills CLI owns and leaves it alone, and
repeating setup on a correct copy is a cheap no-op. Your own edits to a placed copy are
preserved, not overwritten.

## Contributing

Read [AGENTS.md](AGENTS.md), the [specification](docs/pod-spec.md) and the
[validation gates](docs/validation.md). `skills/pod` is simultaneously the Python package,
the skill bundle and the wheel payload; keep one authoring source for everything.

```bash
PYTHONPATH=skills python -m unittest discover -s tests -v
PYTHONPATH=skills python -m pod.skill_validation skills/pod
python tools/platform_audit.py
```

[Migration notes](docs/pod-migration.md) record the cutover from the retired `orchestrate`
product, whose own documents are preserved unchanged in [docs/history](docs/history/).

## License

MIT. See [LICENSE](LICENSE).
