# Pod

Pod helps a Codex or Claude Code conversation deliver a software objective through Orca. The
conversation stays in charge. Pod keeps the few things nothing else keeps: what was asked,
what proves it, and one gate before a merge.

## Install

On Linux, with the latest stable Python, Node/npx, curl or wget, and access to GitHub:

```sh
curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh | sh
```

This installs the Pod skill for Codex and Claude Code and a user-local `pod` command. Pod has
no third-party Python dependency. An existing preference file is never rewritten. Open a new
shell if the installer adds `~/.local/bin` to your PATH. For a download, inspect, then run
path and removal details, see [installation and security](docs/installation.md).

## Contents

- [How it works](#how-it-works)
- [The basic workflow](#the-basic-workflow)
- [Review and evidence](#review-and-evidence)
- [Continuing later](#continuing-later)
- [Models and routes](#models-and-routes)
- [What's inside](#whats-inside)
- [Philosophy](#philosophy)
- [Updating and removing](#updating-and-removing)
- [Contributing](#contributing)

## How it works

Your existing authenticated session is the coordinator, with its current model and effort.
Orca owns Runs, Tasks, workers, messages and worktrees. Git owns content. GitHub owns checks
and merges. Your project decides what counts as accepted. Pod stores identifiers for all of
those and reads them when it needs an answer, so its own record is small: an objective, its
criteria, the facts that prove them, and the effects it performed.

Simple work needs no objective. Open one when the work is delivered through a pull request,
uses workers, or must survive the conversation.

## The basic workflow

1. Open the project in Orca and start a Codex or Claude Code conversation.
2. In Codex, say `$pod https://github.com/owner/project/issues/123`; in Claude Code, use
   `/pod <issue-url>`. You can also give a direct objective in a sentence.
3. The coordinator runs `pod open`, which reads the issue and returns its criteria. A Proof
   of Done checklist, when the issue has one, becomes the criteria.
4. It works directly, or starts workers with `pod dispatch`: an implementer on declared
   paths, a reviewer on a committed candidate, an investigator that only reads. Workers run
   only on model routes you enabled.
5. When a worker reports, `pod accept` reads its result from Orca and its diff from Git, and
   flags anything outside the declared paths. The coordinator states only what it did with
   the result.
6. `pod deliver` runs the gate. It lists what is missing, or merges the exact reviewed
   candidate once, ticks the proved checklist items on the issue and comments the evidence.

`pod status` shows the picture at any time: criteria, workers, open findings and what
delivery still needs.

## Review and evidence

Whether an independent review is required is your project's rule, kept in `.pod/config.yaml`
on the target branch. When it is required, every file the candidate changes needs a passing
review by a worker that changed nothing.

Pod derives what a review still covers from Git: a review holds for a file while that file's
content is unchanged. After a correction only the corrected files need another look, and
merging the target branch reopens only files whose content changed in your candidate. Nobody
declares what carries over.

Evidence is recorded only when it was observed and always names its exact commit. Worker
success, local checks, hosted checks, independent review and the merge are separate facts.
Something that could not be read stays unknown.

A merge needs your authorization: your words recorded for the exact commit, a standing one
for the objective, or your host's verifier. Set `merge_authorization: coordinator` in your
preference file if you want the coordinator to decide.

## Continuing later

Objectives are not tied to a terminal or a conversation. Start a new conversation in the
objective's worktree and it runs `pod resume`: Orca moves the Run to it, and the status shows
where things stand, including the note the earlier conversation left. An objective closes by
itself once its delivery is merged and no criterion is open.

## Models and routes

`pod models` opens a keyboard-driven workspace in your terminal: every exact model and effort
route Pod can use, what the installed agents report, public benchmark observations, and your
choices. Enable or disable routes, set a Preferred route or a Pin. Press `r`, or run
`pod models refresh`, to fetch the public leaderboard; nothing else touches the network.

A green `*` marks a benchmark cost that is high value: nothing cheaper scores within two
index points, among the stronger half of your routable routes. Benchmark cost is not a
subscription charge, a quota or a task duration, and a public listing never makes a route
usable or changes a preference. Only you change your preferences.

`pod config` shows the preference file and what it enables.

## What's inside

- **The skill** (`skills/pod/SKILL.md`): how a coordinator decides how much process a task
  needs, and the eight commands.
- **The command** (`pod open`, `dispatch`, `accept`, `record`, `status`, `deliver`, `resume`,
  `drop`; plus `config`, `models`, `doctor`, `update`, `export`).
- **Authoring templates** for PES, SEAL and EEL documents, for those who use them.
- [Architecture](docs/architecture.md): the five concepts, derived proof, the gate and the
  interfaces a host may use.

## Philosophy

Pod is a lightweight coordinator. The process a task needs is set by the task. Pod keeps only
what no other system owns, derives the rest, never asks a coordinator for what software can
compute, and never records what was not observed.
[Pod's Enduring Design Principles](docs/design-principles.md) hold the reasoning.

## Updating and removing

Run `pod update` to fetch the current `main` bundle through the same staged installer. It
never rewrites your preference file. Records written by Pod 0.21 are left untouched;
`pod export --to DIR` writes them to plain JSON. See
[the removal map](docs/installation.md#removal) before deleting anything. There are no release
packages or tags.

## Contributing

Start with [AGENTS.md](AGENTS.md) for contributor reading triggers and completion criteria.
The root `VERSION` is the only authored product version.

## License

MIT. See [LICENSE](LICENSE).
