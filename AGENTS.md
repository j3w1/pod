# Pod contributor instructions

Read `docs/pod-spec.md` and `docs/validation.md` before implementation. `docs/history/`
holds the retired product's records: it is evidence, never guidance. Inspect Git state and
preserve unrelated work. Pod is a skill plus bounded Python helpers over Orca; Orca owns
native Runs, Tasks, Dispatches, environments and worker lifecycle, and project governance
owns authority and acceptance.

Supported execution environment: Linux. The skill's instruction format is portable, but its
executable dependencies are not, and Pod promises no support elsewhere. Do not complicate
ordinary Python to prevent incidental use on another system, and do not spend effort
maintaining it there.

Use Python 3.13+ and standard-library facilities where practical. `skills/pod` is one
directory serving three roles at once: the importable `pod` package, the agent-skill bundle
the skills ecosystem installs, and the wheel payload. Keep it that way. There is exactly one
authoring source for each implementation and each policy; if you find yourself copying a
file so two places can stay in step, the layout is wrong. `python -m pod.skill_validation`
checks the bundle, a built wheel and any placed copy.

Never embed personal paths, runtime identifiers, credentials, private source packets or live
local bookkeeping in committed files. Captured third-party output under `tests/fixtures/` is
sanitized before it is tracked.

The main implementation owner is Sol at high effort. Use Sol xhigh for bounded
milestone review and difficult implementation knots; Astra xhigh only for a demonstrated
architecture contradiction or an unresolved rescue. A fresh Sol xhigh reviewer performs the
final independent audit. Use native Orca orchestration for supervised workers. These
bootstrap choices are project governance, not Pod's product model catalog.

One writer owns overlapping source changes. Reviews bind to a frozen candidate; reviewers
report findings and do not repair. Consolidate corrections and preserve prior findings.
Worker success, local verification, hosted CI, independent review and external project
acceptance are distinct facts and are reported separately.

Run focused tests during development and the documented final checks at milestone
boundaries. Missing live Orca, model or provider evidence must remain unavailable or
NOT_RUN. Do not weaken a check or fabricate compatibility. A retired requirement is recorded
as retired with its reason, never as a pass.

Keep README.md approachable and follow the editorial structure of
https://github.com/obra/superpowers/blob/main/README.md with original Pod wording. Document
only implemented behaviour. Keep detailed contracts in the linked documents.

Fixture execution uses disposable projects; existing host and ordinary project trials are
read-only. No production or provider mutation, and no package-registry publication.

## Upstream metadata governance

The upstream repository may use the owner's external metadata service to manage designated
GitHub labels and Project membership. Do not add competing automation for those managed
surfaces; ask a maintainer when ownership is unclear. This service is not part of Pod and is
not required to install, use or fork it. Its own [reviewed policy](https://github.com/j3w1/ce-metadata/tree/main/policy)
governs its actions.
