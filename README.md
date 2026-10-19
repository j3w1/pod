# Pod

Pod helps a Codex or Claude Code conversation coordinate a software objective. It can use direct work, tools, or Orca workers while keeping the current conversation in charge.

## Install

On Linux, with Python 3.13+ (including `venv`), Node/npx, curl or wget, and access to GitHub and PyPI:

```sh
curl -fsSL https://raw.githubusercontent.com/j3w1/pod/main/install.sh | sh
```

This installs the Pod skill for Codex and Claude Code, a user-local `pod` command, and a small isolated Python dependency environment. If you have no personal preference file, it enables every supported model+effort route; an existing file is never rewritten. Open a new shell if the installer adds `~/.local/bin` to your PATH. For a download, inspect, then run path and removal details, see [installation and security](docs/installation.md).

## Contents

- [How it works](#how-it-works)
- [The basic workflow](#the-basic-workflow)
- [Plan, direct work, and continuation](#plan-direct-work-and-continuation)
- [Models and the terminal view](#models-and-the-terminal-view)
- [Delivery and cleanup](#delivery-and-cleanup)
- [Troubleshooting](#troubleshooting)
- [What's inside](#whats-inside)
- [Philosophy](#philosophy)
- [Updating and removing](#updating-and-removing)
- [Contributing](#contributing)

## How it works

Your existing authenticated session remains the coordinator, with its current model and effort. Orca owns Runs, Tasks, Dispatches, worker tabs, messages, request recovery, worktrees, and lifecycle. Pod makes assignment choices, checks them at admission, and binds evidence to the objective. Your project decides what counts as accepted.

Pod reads an issue completely before using it as scope. It checks that the issue belongs to the actual repository and notices material body changes. Issue text cannot grant authority. A SEAL supplies requirements; Pod refuses it as the primary executable issue, including from another repository or a directory without GitHub repository identity. Use read-only SEAL delivery planning, a bounded PES citing selected SEAL requirements, or an explicitly scoped analysis request. An Evidence Evaluation Ledger issue, titled `EEL:` or marked with its format line, is evidence only: Pod refuses to execute from it, and implementation needs a separate Pod Execution Spec that cites it. A direct objective follows the same process without an issue.

## The basic workflow

1. Open the project in Orca and start a Codex or Claude Code conversation.
2. In Codex, say `$pod https://github.com/owner/project/issues/123`; in Claude Code, use `/pod <issue-url>`. You can also give either skill a direct objective.
3. Review the objective and criterion-to-check map when the task warrants one. Plan Mode remains read-only until you accept the plan.
4. Let the coordinator do simple work directly and delegate bounded independent assignments only when useful. Workers follow Orca's native launch path and your setting for new agent tabs. `pod status` reports placement and any native discoverability warning.
5. Review the local checks, independent review, hosted CI, and project acceptance as separate evidence. Pod reports remaining gates and uncertain native work instead of assuming success.

For persistent issue-backed work, the [Issue intake reference](https://github.com/j3w1/pod/blob/main/skills/pod/references/issue-intake.md) explains issue intake; the canonical [SEAL](https://github.com/j3w1/pod/blob/main/skills/pod/references/seal-template.md), [PES](https://github.com/j3w1/pod/blob/main/skills/pod/references/pes-template.md) and [EEL](https://github.com/j3w1/pod/blob/main/skills/pod/references/eel-template.md) authoring templates explain their separate roles and section responsibilities. The PES authoring template includes numbered Proof of Done items. Parent requirements are traceable sources, with their selected requirements and Limits reconciled; they do not replace the child repository target. Authoring in ChatGPT and executing in Orca are separate steps; installation adds no ChatGPT integration.

## Plan, direct work, and continuation

- Direct task: `/pod update the README to explain this feature`.
- Plan only: `/pod <issue-url> — plan only; do not change files`.
- Plan SEAL delivery: `/pod https://github.com/owner/project/issues/<SEAL>` reconciles existing delivery first. If work remains, Pod confirms the outcome with you before proposing bounded PESs; if nothing remains, it reports that and stops. Planning writes nothing without authorization and stops at the published PESs.
- Run an approved PES: `/pod https://github.com/owner/project/issues/<PES>` starts that bounded objective in its target repository, with its own proof and delivery checks.
- Plan then execute: accept the host plan and continue in the same conversation.
- Continue after interruption: invoke the same objective; Pod reads the native state and reconciles unresolved requests before another start.

A small task can finish with zero workers. Missing Orca delegation support blocks delegation while safe direct work and diagnosis continue. Pod does not configure Orca or sign in to your agent applications.

## Models and the terminal view

`pod`, or `pod models`, opens the model workspace in a terminal; without a TTY it prints the route table. Each supported row is one exact route, a model at one effort such as `codex/gpt-6.1-sol/high`. The supported models are Claude Opus 5.5, Fable 5.1, Sonnet 5.5 and Haiku 5.5 (`claude-opus-5-5`, `claude-fable-5-1`, `claude-sonnet-5-5`, `claude-haiku-5-5`) and GPT-6 Astra, GPT-6.1 Sol and GPT-6 Luna (`gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-luna`), each at `low`, `medium`, `high`, `xhigh` and `max`.

The table starts sorted by Artificial Analysis (AA) index, with unknown values last. `s` and `S` change the sort, `g` groups routes by model, `/` searches, `f` and `o` filter by provider and discovery, `:` or Ctrl+P opens the command palette, and `?` lists every key. A full-width inspector at the bottom has Details, Benchmarks, Routing and Sources tabs. `c` marks up to four routes to compare, and `e` marks the AA frontier within each like-for-like set of shown rows; both are display only. A † beside a value means AA measured that profile "with fallback", its own harness, which never enables Pod fallback. AA benchmark cost is not your subscription charge, quota or Pod invoice, and benchmark response time is not worker task duration.

Space enables or disables the focused route, and the palette can return it to not set. `P` makes it your Preferred route and `p` pins it; pressing again clears them. `b` previews a bulk change, such as all efforts of one model, and saves it once. Edits save immediately to your personal file; browsing, sorting, filtering and comparing never do.

The personal YAML at `${XDG_CONFIG_HOME:-~/.config}/pod/config.yaml` is the single route preference authority. `pod config --json` shows its path, status, enabled routes, Preferred and Pin; `pod config edit` opens it in your editor. A route the file does not list is not set, which means not eligible.

The coordinator chooses a suitable enabled route and an exact effort for each assignment. It uses your Preferred route when that route suits the work; choosing another needs a material task-specific reason, which Pod records. A Pin sends every new Pod-routed worker role, including corrections, to that exact model and effort with precedence over repository model rules, or nothing is dispatched; it never falls back or changes effort. It leaves the running coordinator, direct work and outside helpers unchanged, and clearing it restores ordinary routing. Orca has per-worker model and effort preferences but no scoped context flag, so Pod omits a context flag and records `native_default`. Benchmarks are dated observations, not a promise of native availability, billing, or the route Pod will choose.

With the default `refresh: automatic`, opening the workspace refreshes public model data in the background when it is missing or at least a day old. `pod models refresh` refreshes explicitly, `pod models refresh --check` validates and compares without saving, and `pod models status` diagnoses sources and the cache offline. Set `refresh: manual` to stop automatic network access. Press `R` to refresh now and see AA rows added, changed and removed. Newly discovered and unsupported observations appear in the default view with clear labels; `o` cycles filters, including supported routes only. A refresh never changes your preferences, and an update leaves newly supported routes not set until you enable them.

Temporary readiness failures remain local observations with unknown cause. Pod honors native retry-after or a 60-second reconsideration point; a later coordinator decision can retry, while expiry itself starts nothing. Preferences and the pin stay unchanged.

## Delivery and cleanup

Pod ticks proven PoDs on its bound PES issue and removes its own ticks when proof lapses; status names ambiguous ticks and blocked requests, and closure waits for writable due ticks.

Before the first remote Git change without applicable authorization, Pod asks once whether to merge remotely, keep the result local, or defer. The decision shows the repository, target and exact candidate commit/tree. Merge consent includes publication, required CI, merge of that candidate and the stated post-merge verification. A changed candidate or target needs a new decision. Local-only performs no remote mutation and reports hosted checks as not run.

When you trust an objective end to end, turn on yolo mode: `/pod <issue-url> yolo` (`$pod` in Codex), or say "use yolo from now on" later in the session. Pod then stops waiting for your consent to publish, merge, release, install and clean up each exact candidate, while every test, hosted CI run, independent review and merge check still runs. Production deployment, new paid or cloud resources, secrets, deleting unique work and anything beyond the objective's scope still ask you, as do the host's own rules. Say "stop yolo" to turn it off. A material change to the issue's requirements, an expiry you set, closure and a successor objective also end or suspend it, and it never carries over on its own. Status and the final report show the mode and every effect it authorized.

After an authorized merge, Pod verifies the exact delivery record before closing the objective. The coordinator records the merge outcome, the delivery record, every validation readback, report consumption and closure, in that order, closing in the same turn that proves delivery and consumes the final report, before it ends, is released or its tab closes; helper results name what still remains, and a closing write waits until the rest are recorded. Before cleanup, `pod sweep` closes stranded objectives by delivery evidence without an owner decision; `pod status --list --delivered` previews the proof. For cleanup, `pod internal cleanup-plan` reports this objective's integrated, unique and protected resources without deleting them. Deletion needs scoped consent and a fresh guarded plan; unique work is retained or archived and verified first, unless separately confirmed for discard. Worker release is an explicit coordinator step. Finished tabs that Orca can release hold closure unless you have recorded an instruction to keep them; report acknowledgements name the exact release or close step.

For managed remote actions, `governor-execute` admits and performs the supported action. A `governor` decision reserves it for the caller; an identical execution attaches to that reservation. Evidence binds full Git commit ids, and named REUSE carries unaffected proof across candidate changes using its declared scope. See [coordination](docs/spec/coordination.md) and [delivery](docs/spec/delivery.md).

## Troubleshooting

`pod doctor --json` reads installation ownership, version and bundle integrity, placements, preferences, cached model observations, and available Orca capability without starting a worker or fetching anything. `pod status --objective ID --json` selects an objective and shows scope, assignments, gates, progress and the next safe action. Its first line says in plain words whether the objective is working, waiting on someone, delivered but still finalizing, closed, or has lost its coordinator and how that can be recovered; `--json` carries the same answer as `readiness`, with the records that remain and whether the coordinator is still needed, and the objective list adds a short readiness label. The `worker_tabs` field lists finished workers’ connected tabs and their release, close or readback steps, including tabs whose agent liveness is stale or unverifiable. Owner-kept tabs are marked and named in the final report. A merged pull request or an idle coordinator alone never reads as closed. If Pod cannot verify native settlement, for example after an Orca runtime change, open assignments are shown as unverified rather than active, with the failed read and both runtimes. The next mutation rebinds an objective whose Run, coordinator and workers are unchanged, and asks for your decision when it cannot tell. When a Run has several objectives, status lists choices instead of picking one. A closed objective's assignments read as settled by its closure even after a runtime change. `pod status --list --json` lists every objective recorded for this repository (or `--worktree PATH`), with phase, worktrees and Runs, from Pod's own state; it needs no Orca terminal, so an administrator outside Orca can use it. A missing or invalid preference file leaves no eligible routes; use `pod config edit` to correct it. For preference upgrade setup, see [installation](docs/installation.md#updating-and-recovery).

If Orca reports the previous coordinator's handle stale or gone, `pod status --objective ID` may show `handoff_available`. For proved delivery, evidence closure is the next action. For unfinished work, an optional handoff asks one plain question to make the current terminal the objective coordinator. With your exact confirmation it records a pending handoff before rebinding the existing Run, then verifies and completes it; a lost response can resume without a second rebind. Work is preserved, and a manual `run-use` cannot establish handoff lineage. Status names any blocking identity, live coordinator or shared Run. A closed peer on the same Run can also hold handoff when its retained REUSE changed definition and the original scope was never recorded. Ordinary work remains available; status names the unverifiable peer, and confirmation cannot override this hold.

When a coordinator is gone and its work settled, a successor objective can continue it instead. After the successor's first checkpoint, `pod internal succession` names the predecessor and returns one plain question; with your instruction the predecessor records `continued_by` and refuses every later write, including from its old terminal, with `objective_continued`. Its assignments read as settled by the claim. Its unsettled Governor effects that Pod can read back move to the successor, which settles them only by reading them back and never repeats them; a release or deploy still pending holds the claim. The list shows it `continued`, with `successor_open` until the successor closes, then `successor_closed`. An evidence-closed successor also ends the chain. Delivered lost work can close directly by evidence without a successor claim. Coordinators record which outside results an obligation accepted or rejected with a checkpoint's `acceptances`, and status shows them. Orca has no send that reaches only a Run's current coordinator, so `pod doctor` reports that route unavailable and names the Orca request.

When a lost objective is not proved delivered, it stays open as a task for the project lead, never an owner question. Delivery-proved objectives leave the historical-retirement question and close by evidence; `proof: delivery` is accepted in place of a decision. As an optional decision path, when an objective's coordinator is permanently gone and its worktree needs releasing, `pod internal historical-retirement` names the objectives and paths, checks with Orca that no coordinator, Dispatch or terminal could still be at work there, and returns one plain question. With your instruction each objective records its historical retirement: status and the list show the phase `retired` with the released paths, the pending, unknown and failed records kept exactly as they were, the unfinished obligations and your delivery references. It is never closed, completed or QUALIFIED, and its record accepts no further writes. A recorded worktree that is already gone can be released too, once this host, Git's worktree registry and Orca's complete inventories all show it absent; the question names each path as existing or absent, the recorded worktrees the objective keeps holding, and whether retirement ends cleanup's hold on locations its record never listed. Retirement deletes nothing; your cleanup tool decides about the worktree itself.

Lost worker-start replies retain the same Orca request for reconciliation. A known effect-free refusal is deferred; an uncertain response remains unresolved until exact native readback. A provider safety refusal never triggers a same-Task model switch. See [installation troubleshooting](docs/installation.md) for PATH, duplicate skills, and interrupted installs.

## What's inside

- One `skills/pod` tree serves as the skill and importable Python package.
- One personal YAML file stores exact route states, the Preferred and Pinned routes, the logical worker ceiling and the refresh setting.
- One shipped registry holds exact model ids, efforts and attributed guidance; a small bundled snapshot and a user cache hold dated public model observations.
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

Run `pod update` to fetch the current `main` bundle through the same staged installer. It never rewrites your preference file, preserves changed skill copies, and tells active coordinators to reload before new starts; running workers are untouched. The installer owns only identified user-local paths. See [the removal map](docs/installation.md#removal) before deleting anything. There are no release packages or tags.

## Contributing

Start with [AGENTS.md](AGENTS.md) for contributor reading triggers and completion criteria. The root `VERSION` is the only authored product version.

## License

MIT. See [LICENSE](LICENSE).
