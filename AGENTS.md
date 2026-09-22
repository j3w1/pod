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

The main implementation owner is GPT-5.6 Sol at high effort. Use Sol xhigh for bounded
milestone review and difficult implementation knots; Astra xhigh only for a demonstrated
architecture contradiction or an unresolved rescue. A fresh Sol xhigh reviewer performs the
final independent audit. Use native Orca orchestration for supervised workers.

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

## CE Metadata integration

This repository joined the CE Metadata portfolio on 2026-09-22. Nothing is installed here and
nothing is imported: CE Metadata is a service that reads this repository through one GitHub App
installation, and this section is the cooperation contract an agent working in it needs.

**Right now it writes nothing here.** Being in the portfolio means this repository is read and
censused and its objects are visible. It does not mean any writer reaches them. Reviewed
classification rules may decide labels only in the repositories named by
`classification_authority.repositories` in [`policy/object-metadata.yaml`](https://github.com/j3w1/ce-metadata/blob/main/policy/object-metadata.yaml),
and the canonical label definitions are written only in the repositories named by
`coverage_repositories` in [`policy/label-management.yaml`](https://github.com/j3w1/ce-metadata/blob/main/policy/label-management.yaml).
This repository is in neither yet. Each is its own reviewed change, made once this repository's
corpus has been classified — so the first labels that appear here will have been reviewed before
they were written, not after.

**Protected CE label prefixes:** `ce-systems`, `cross-repo`, `historical-evidence`, `type:`,
`area:`, `concern:`. Labels outside them are never touched — including the ones GitHub creates by
default and the ones Dependabot applies. Within them CE Metadata is authoritative once a writer
reaches this repository: a reviewed rule states an object's whole managed label set, so a CE label
added by hand and absent from that rule is drift, and the sweep removes it.

**Do not hand-label to steer it.** An object labelled by hand to influence classification is not
configuration, it is drift the next sweep removes — and it spends a breaker budget doing so. If
the labels are wrong, the reviewed policy is wrong: report the exact object, the policy digest,
the plan and the readback, and the fix is a policy change.

**Classification decides labels; it is not only evidence.** Every sweep classifies uncovered
objects, and since [ADR 0038](https://github.com/j3w1/ce-metadata/blob/main/docs/adr/0038-reviewed-classification-rules-as-label-authority.md)
a complete, canonical, unambiguous classification derived from reviewed rules *is* an object's
exact managed label set where no explicit reviewed rule covers it — in the repositories that
declaration names.

The half that fails closed matters more here. An object whose evidence does not decide a single
`type:` and a single `area:` sits at `NEEDS_REVIEW` and writes nothing. Since
[ADR 0039](https://github.com/j3w1/ce-metadata/blob/main/docs/adr/0039-semantic-pr-evidence.md) a pull request is classified from the
files it changed **as well as** its title: where they disagree in an exclusive namespace structure
wins and the title rule's whole contribution is set aside, and where they agree they merge. Within
one class of evidence there is no principled winner, so two conflicting title rules and two
conflicting structural rules both fail closed. An incomplete changed-file list fails a *universal*
fact closed — "every path here is documentation" cannot be established from a truncated list — but
an *existential* one can still hold.

Since [ADR 0046](https://github.com/j3w1/ce-metadata/blob/main/docs/adr/0046-classification-evidence-from-title-convention.md) the
classifier reads this portfolio's own title conventions: a conventional-commit prefix
(`feat:`, `fix:`, `ci:`), a leading imperative verb, or an identifier or bracketed tag followed by
one. The identifier itself is skipped and cannot be read — the patterns answer identically for any
scheme — so a `CE-####` prefix implies neither a type nor an area. What decides a type is the verb
after it.

**No CE task identifier is allocated by any of this.** CE Metadata cannot create `CE-GD`, `HQ`,
`IAR`, `IAP`, `MQ`, `D3` or `DONE` state, approve anything, mark anything ready, or merge. This
repository's existing delivery process is untouched.

**Do not create a competing writer.** A second workflow, Action or agent writing the same labels or
the same Project membership is exactly the failure `NO_DUAL_WRITER` exists to prevent. Project
membership is written by the owner-authenticated Project bridge, never by the App, and Project
Status belongs to GitHub's own native workflows rather than to CE Metadata.

**This repository's profile is reviewed policy, not a claim made here.** Its role is `agent-orchestration`, its
membership writer is `OWNER_AUTHENTICATED_BRIDGE`, its default area is `area:agents`, and the prefixes
above are what protected policy currently allows it. Read them from
[`policy/repositories.yaml`](https://github.com/j3w1/ce-metadata/blob/main/policy/repositories.yaml) rather than from this file, and
verify the live grant before assuming a writer is active — a grant names exact repositories and,
at a canary ring, exact objects, so being in the allowlist is not the same as being covered by a
live grant.
