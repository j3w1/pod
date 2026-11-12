# Pod contributor instructions

Apply every matching read-when row before implementation; a small diff is no
exemption. Inspect Git state and preserve unrelated work.

| Read when | Document or check |
| --- | --- |
| Adding a subsystem, a coordinator-facing command or a persisted record kind, or answering an incident by adding one | `docs/design-principles.md` |
| Changing how objectives, facts, proof or the gate work | `docs/architecture.md` |
| Editing skill text | `skills/pod/SKILL.md` front matter and `python -m pod.skill_validation` |
| Changing the model registry, refresh or workspace | `docs/architecture.md` "Models" |
| Changing the installer | `docs/installation.md` |
| Changing CI | `tests/install/test_workflow.py` |
| Fixing outside these areas | Nothing extra |

Pod is a skill plus a small Python command over Orca. Pod owns the objective, its criteria,
the facts that prove them and the delivery gate; Orca owns Runs, Tasks, Dispatches, workers and
who coordinates a Run; Git owns content; GitHub owns checks and merges; the host owns
worktrees, cleanup and process liveness; project governance owns authority and acceptance.

Supported execution environment: Linux. The skill's instruction format is portable, but its
executable dependencies are not, and Pod promises no support elsewhere. Do not complicate
ordinary Python to prevent incidental use on another system, and do not spend effort
maintaining it there.

Use the latest stable CPython, one interpreter per CI run, and the standard library only; Pod has no third-party dependency. `skills/pod` is one
directory serving two roles at once: the importable `pod` package and the agent skill that
the one-shot installer from `main` places through the skills CLI. The installer also places
a user-local global `pod` launcher. This is Pod's only distribution; there are no packages,
releases or tags. There is exactly one authoring source for each
implementation and each policy, including the version: the root `VERSION`, which
`skills/pod/VERSION` links to. If you find yourself copying a file so two places can stay in
step, the layout is wrong. `python -m pod.skill_validation` checks the bundle and any placed
copy.

Never embed personal paths, runtime identifiers, credentials, private source packets or live
local bookkeeping in committed files. Captured third-party output under `tests/fixtures/` is
sanitized before it is tracked.

Choose implementation and review workers for each task based on its complexity, risk,
size, uncertainty, verifiability, required capabilities and available context. A fresh
independent reviewer performs the final audit. Use native Orca orchestration for
supervised workers. These worker selection and audit rules are project governance,
not Pod's product model catalog.

One writer owns overlapping source changes. Reviews bind to a frozen candidate; reviewers
report findings and do not repair; implementers make corrections. A review keeps covering a
file while its content is unchanged, so a correction is followed by a review of the changed
paths only. Worker success, local verification, hosted CI, independent review and external
project acceptance are distinct facts and are reported separately.

A change to Pod's own gate, proof or closure rules is reviewed and merged under the mechanism
installed before the change. Evidence the new mechanism produces about itself is recorded
separately as self-hosting evidence and never substitutes for that review.

Complete requested behaviour with tests that fail when the behaviour is removed, the hosted
`linux` check green on the exact head, and one fresh independent review with no open required
finding, before claiming completion. Tests are the contract for behaviour and
`docs/design-principles.md` is the constraint on architecture; there is no requirement,
scenario or coverage inventory to keep. Missing live Orca, model or provider evidence must
remain unavailable or NOT_RUN. Do not weaken a check or fabricate compatibility. Removing
behaviour removes its tests and its documentation in the same change.

Keep README.md approachable and follow the editorial structure of
https://github.com/obra/superpowers/blob/main/README.md with original Pod wording. Document
only implemented behaviour. Keep detailed contracts in the linked documents.

Local tests may run without asking; `tests.support.PodCase` gives each test fresh `POD_STATE_HOME`,
`POD_CONFIG_HOME` and `POD_CACHE_HOME` locations. Installer trials may also run without asking, using a truly scrubbed
fresh temporary home: every installer base-directory variable (`HOME`, `XDG_CONFIG_HOME`,
`XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME`, `CODEX_HOME`, `CLAUDE_CONFIG_DIR`,
`POD_CONFIG_HOME`, `POD_STATE_HOME`, `POD_CACHE_HOME`, `ZDOTDIR`) is contained there or unset, and PATH
cannot select the real Pod launcher. These permissions cover only that process; never affect real
launchers, skills, rc files, data, config or state.

Every pull request and `main` push runs three plain `unittest` jobs (`core`, `models`,
`install`) in hosted CI, which is free for this public repository. A pull request, a release
(a root `VERSION` change) included, merges only when its independent review passes and the
required `linux` check is green on its exact head. Locally, run the test module you are
changing (`PYTHONPATH=skills python -m unittest tests.core.test_gate`); the hosted run is the
complete one. Keep tests fast: build fixtures once and never sleep for time to pass. A job
timeout is the only clock.

Fixture execution uses disposable projects; existing host and ordinary project trials are
read-only. No production or provider mutation, and no package or release publication.

## Pod's Enduring Design Principles

`docs/design-principles.md` holds the reasoning. The short form binds every change:

- Pod is a lightweight coordinator; the process a task needs is set by the task.
- Keep only what no other system owns; never copy Orca, Git, GitHub or host state; derive the rest.
- Never ask a coordinator for what the software can compute.
- Never record a success, exit, result or acceptance that was not observed; bind evidence to the
  exact commit and tree.
- Only the person changes a preference; a public observation enables nothing.
- Answer an incident by removing what allowed it before adding anything.

A change that adds a subsystem, a coordinator-facing command or a persisted record kind answers
that document's three questions in its pull request description. A change that breaks a Rule
there says so and why. Removing or simplifying owes neither.

## Upstream metadata governance

The upstream repository may use the owner's external metadata service to manage designated
GitHub labels and Project membership. Do not add competing automation for those managed
surfaces; ask a maintainer when ownership is unclear. This service is not part of Pod and is
not required to install, use or fork it. Its own [reviewed policy](https://github.com/j3w1/ce-meta/tree/main/policy)
governs its actions.
