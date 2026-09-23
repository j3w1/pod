# Pod

Pod turns the Codex or Claude Code conversation already open in Orca into the
coordinator for a software objective. Give it a GitHub issue containing a clear
implementation contract, or simply describe a small task. Pod plans, chooses
direct work or useful workers, preserves evidence, and reports the real outcome.

## Table of contents

- [How it works](#how-it-works)
- [Installation](#installation)
- [The basic workflow](#the-basic-workflow)
- [Writing and publishing a spec](#writing-and-publishing-a-spec)
- [Direct work, planning and continuation](#direct-work-planning-and-continuation)
- [Worker routes](#worker-routes)
- [When something goes wrong](#when-something-goes-wrong)
- [What's inside](#whats-inside)
- [Philosophy](#philosophy)
- [Updating and removing](#updating-and-removing)
- [Contributing](#contributing)
- [License](#license)

## How it works

Invoke `$pod` in Codex or `/pod` in Claude Code. Your current conversation keeps
its context, model and effort and remains the coordinator.

| Owner | Responsibility |
| --- | --- |
| **Pod** | Understand the objective, apply policy, choose assignments and routes, and bind evidence. |
| **Orca** | Runs, Tasks, Dispatches, worktrees, worker tabs, messages, actual capacity and lifecycle. |
| **Your project** | Source rules, checks, review, acceptance, merge and release. |

For an issue URL, Pod reads the complete current body through authorized GitHub
access, verifies that its target matches the actual checkout, and maps its
criteria to checks or explicit dependencies. The issue supplies scope, not extra
permissions. Relevant content changes are reconciled on continuation; labels or
timestamps alone do not invalidate good work.

Implementation belongs in an isolated Orca worktree, normally on an
`orca/<task-slug>` branch. The branch, Orca display label and filesystem path are
separate identities. Pod reuses an existing worktree only for the same objective
and repository, preserves owner dirtiness, and never resets a collision.

When workers help, Orca starts each normal worker in its own visible agent tab.
The coordinator validates a worker's report, preserves the useful result, then
follows Orca's Delivery acknowledgment and release ordering promptly. Pod does
not keep another worker database or use raw terminal closure as a lifecycle API.

## Installation

Install the 0.3.0 candidate from `main` for both supported agents:

```bash
npx skills add j3w1/pod --skill pod -a codex -a claude-code -g
```

Then open the target project in Orca and start an authenticated Codex or Claude
Code conversation. Invoke Pod before configuring worker routes; direct work is
always valid. Approve a route only if delegation would actually help.

Requirements are Linux, Python 3.13+ as `python3`, PyYAML 6.x, and Orca with its
version-matched orchestration guide. Loading Pod installs nothing or changes no
provider setting.

The latest published release remains `v0.1.2`; it does not contain the candidate
behavior documented here:

```bash
npx skills add j3w1/pod#v0.1.2 --skill pod -a codex -a claude-code -g
```

Without Node, clone a reviewed published tag and use its explicit installer:

```bash
git clone --branch v0.1.2 --depth 1 https://github.com/j3w1/pod pod-release
python3 pod-release/install.py --venv ~/.local/share/pod/venv \
  --expected-commit "$(git -C pod-release rev-parse v0.1.2^{commit})"
~/.local/share/pod/venv/bin/pod setup --global
```

Source on `main`, a published tag, and the copy currently loaded by your agent
can differ. `doctor --json` shows the loaded bundle path and version.

## The basic workflow

1. **Open the correct project in Orca.** Start an authenticated agent there.
2. **Invoke the issue.** Use `$pod https://github.com/owner/project/issues/123`
   or `/pod https://github.com/owner/project/issues/123`.
3. **Review the brief.** Pod reads the complete issue, applicable instructions
   and existing evidence, then maps each Proof of Done item to a check.
4. **Use the objective worktree.** Read-only planning need not create one;
   implementation selects or creates the exact isolated worktree first.
5. **Approve only when useful.** If a worker would help and its route is not
   approved, Pod shows one concise model/account/effort/context/billing proposal
   and asks for explicit confirmation in the host conversation. You never copy
   account or route hashes. Approval does not grant paid usage or extra fan-out.
6. **Coordinate useful workers.** Worker tabs receive bounded assignments rather
   than the whole issue or conversation. Finished tabs are released after their
   results are safely consumed, not held until final delivery.
7. **Verify the project.** Local checks, independent review, hosted CI, live
   provider evidence and project acceptance remain distinct facts.
8. **Receive the outcome.** The final report names what passed, blockers,
   uncertainty, the candidate, remaining gates and any retained resource.

An unavailable worker route blocks only delegation. Pod can continue safe direct
work in the current conversation.

## Writing and publishing a spec

The canonical [Pod Execution Spec reference](https://github.com/j3w1/pod/blob/main/skills/pod/references/execution-spec.md)
contains the ordinary Markdown skeleton and interpretation rules.

- Ask: `Draft a Pod Execution Spec for owner/project: <outcome>.` Drafting returns
  a document; it does not publish anything.
- To publish through authorized GitHub access, ask: `Create a Pod Execution Spec
  issue in owner/project for <outcome>, using j3w1/pod:skills/pod/references/execution-spec.md.`
- Then execute the resulting issue URL with `$pod` or `/pod`.

Authoring in ChatGPT and executing in Orca are separate steps. Installing this
skill in an agent does not install a ChatGPT integration or grant GitHub access.
Private issues require already-authorized access; Pod never asks you to make one
public or place credentials in it.

## Direct work, planning and continuation

- Direct task: `/pod update the README to explain the latest changes`.
- Plan only: `/pod <issue-url> — plan only; do not change files`.
- Plan then execute: Pod reads and reconciles the source first, then continues
  automatically within authorization. A host's real Plan Mode still controls
  whether edits may begin.
- Continue after interruption: invoke the same objective. Pod resolves the same
  repository/worktree state, rechecks source content, reads native work, and
  preserves uncertain requests instead of creating replacements.

Small direct tasks need no issue, spec file, worker, worktree ceremony or route
approval. Users normally do not create worker tabs, choose every model, manage
release commands, or paste the entire issue into each worker.

## Worker routes

Pod's active catalog is exact:

| Complexity | Alias | Agent / model | Effort | Context profile |
| --- | --- | --- | --- | --- |
| Trivial | Luna | Codex / `gpt-6-luna` | low | `256k` |
| Simple | Sonnet | Claude / `claude-sonnet-5` | medium | `256k` |
| Standard | Sol | Codex / `gpt-6-sol` | medium | `256k` |
| Complex | Opus | Claude / `claude-opus-5-5` | high | `max` |
| Very complex | Astra | Codex / `gpt-6-astra` | xhigh | `max` |

Claude Fable (`claude-fable-5-1`) is an alternative for demanding long-horizon
work. `256k` means a conservative upper bound of **256,000 tokens**; a proven
lower native clamp stays visible. `max` means the largest limit the exact
model, agent and installed runtime can prove, bounded by the provider ceiling
(up to 1.05M for these Codex models and 1M for these Claude models). A ceiling
is not live capability proof.

Current Orca 1.4.209 exposes per-worker model and effort, but no safe per-worker
context control or strict noninteractive startup flag. Pod therefore reports all
new context-dependent worker routes unavailable before any effect. It does not
invent a flag, launch an unsupervised terminal, wrap a provider, or change shared
settings. Bootstrap workers do not count as production-adapter proof.

Guided approval is inside the existing `config` family:

```text
python3 ~/.agents/skills/pod/scripts/pod.py config approve sol
python3 "${CLAUDE_SKILL_DIR}/scripts/pod.py" config revoke sol
```

Interactive use asks for confirmation. In a host conversation, Pod uses the
read-only JSON proposal and confirmation operation internally after you say yes.
Advanced users may edit personal YAML; project/task YAML can only narrow it.

## When something goes wrong

Run helpers from the skill copy your agent actually loaded:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/pod.py" doctor       # Claude Code
python3 ~/.agents/skills/pod/scripts/pod.py doctor       # Codex, global
python3 .agents/skills/pod/scripts/pod.py status --json  # Codex, project
```

`doctor` gives concise direct-work and worker-route readiness. `config` shows
effective policy. `status` shows the objective/source, selected worktree,
relevant native work, blocker, next action and remaining gates. Add `--json` for
detailed diagnostics. These reads do not dispatch, repair or migrate anything.

An inaccessible issue is an access blocker, a wrong target is a repository
mismatch, and a changed body needs reconciliation. A closed issue needs intent
and recorded-outcome review before repeated implementation.

Lost worker-start responses retain their logical reservation and recover the
same Orca request. A definite `capacity_full` refusal is deferred without blind
retry. Silence is not completion. If state reports `migration_required`, follow
the [migration guide](docs/pod-migration.md); unrelated old state does not block
a new objective.

## What's inside

- One installed skill and conditional references, including the Execution Spec.
- Four public helper families: `setup`, `config`, `doctor` and `status`.
- One personal YAML authority with restrictive project/task layers.
- Bounded issue/source, packet, checkpoint, admission and verification evidence.
- Objective-local logical fan-out and exact Orca request recovery.
- Candidate-bound Governor decisions: `ALLOW`, `REUSE` or `DEFER`.
- One `skills/pod` tree serving as Python package, skill bundle and wheel payload.

Pod has no scheduler, issue database, polling service, provider launcher,
worktree manager, dashboard or duplicate Orca lifecycle state.

## Philosophy

- Use direct work when it is enough; a pod exists for distinct useful work.
- Source is scope, never authority. Personal, host and project controls still win.
- Preserve owner changes and uncertain effects before optimizing cleanup.
- Verify outcomes rather than promoting worker claims or offline mocks.
- Keep implemented, reviewed, hosted, accepted, merged and released separate.

## Updating and removing

Use the manager that installed the skill:

```bash
npx skills update pod -g
npx skills remove pod -g -a codex -a claude-code
```

For the Python installer, update from a reviewed checkout and run `pod setup`
again. Setup preserves modified copies and recognizes skills-CLI ownership.

Version 0.3.0 changes private state and JSON envelopes. Read the
[migration notes](docs/pod-migration.md) before upgrading or rolling back. No
0.3.0 release has been published, and no historical tag is removed automatically.

## Contributing

Read [AGENTS.md](AGENTS.md), the [specification](docs/pod-spec.md), and the
[validation gates](docs/validation.md). Start with:

```bash
PYTHONPATH=skills python -m unittest discover -s tests -v
PYTHONPATH=skills python -m pod.skill_validation skills/pod
python tools/platform_audit.py
python tools/artifact_audit.py --source .
```

[Progress](docs/pod-progress.md) distinguishes local evidence from hosted, live,
review and acceptance gates. Historical product records remain evidence only.

## License

MIT. See [LICENSE](LICENSE).
