# Pod's Enduring Design Principles

These principles govern every change to Pod. They exist because Pod once grew, one reasonable
correction at a time, into a system whose coordinators spent their turns administering Pod
instead of the work. They describe what Pod is for and what it must not become. They do not
describe today's implementation, and they outlive any one design.

[AGENTS.md](../AGENTS.md) is the entry point: it carries the short form and sends contributors
here before an architectural change.

## How to use this document

Each principle has two kinds of statement.

- A **Rule** is a hard constraint. A change that breaks one states why in its pull request and
  needs j3w1's approval before it merges.
- A **Preference** is the default. Depart from it when the work is better for it, and say why in
  one sentence in the pull request.

Ordinary implementation that stays inside the principles needs no policy discussion. Code that
predates this document is measured against it when it is next changed; its presence is never a
reason to add more of the same.

## 1. Purpose and proportion

Pod is a lightweight, skill-first coordinator for software development. It helps one conversation
decide what to do directly, what to delegate, what is proven and what may be delivered.

- **Rule.** Pod is not a workflow-governance platform, scheduler, task system, lifecycle store,
  dashboard or host service, and does not grow into one.
- **Rule.** The process a task needs is set by the task. Direct work needs no objective record,
  worker, review or map unless the task or the project requires one.
- **Preference.** Scale by composing small objectives, each with one deliverable, rather than by
  growing one objective, one record or one conversation.

*Prevents:* a small edit paying for the machinery of a programme, and a programme outgrowing the
conversation that coordinates it.

## 2. One owner for every fact

Orca owns worker execution and lifecycle. Git owns commits, trees and changed paths. GitHub owns
pull requests, checks and merge state. The host owns its infrastructure, resources and host-level
authority. The project owns acceptance. Pod owns only what none of them holds: the objective, its
criteria, the evidence gathered for it and the decision to deliver.

- **Rule.** Pod keeps identifiers of other systems' objects, never copies of their state. It reads
  that state when it needs it.
- **Rule.** Pod builds no competing version of something another system owns, and does not answer
  questions on that system's behalf.
- **Rule.** Dependencies point one way. Nothing Pod needs in order to finish may wait on a system
  that is itself waiting on Pod.
- **Preference.** When a native capability is missing, report it as missing and ask for it there
  before imitating it here.

*Prevents:* two records of one fact drifting apart, and the reconciliation code that follows.

## 3. Derive, do not restate

- **Rule.** Pod persists only what is authoritative here and cannot be reliably derived. Status,
  applicability of proof, closure and anything else computable from stored facts and native state
  is computed when read.
- **Rule.** The coordinator is never asked to supply something the software can compute: a
  sequence number, a restatement of unchanged state, a list of unaffected evidence, a copy of what
  Git or Orca already reports.
- **Preference.** Record facts once and append-only. Prefer a derived view to a second stored
  representation, and a single write to a chain of transitions.

*Prevents:* state machines the coordinator must advance by hand, and refusals for getting the
bookkeeping wrong.

## 4. The coordinator's turns are the budget

Every helper call is a model turn that re-reads the whole conversation. Turns and sessions, not
bytes, are what Pod costs.

- **Rule.** An ordinary result is accepted with one call, and Pod does the accounting.
- **Rule.** Storage layout, validation protocol and record maintenance are internal. They never
  appear as a routine coordinator task or as required reading.
- **Rule.** A refusal names what is wrong and the one thing to do next. A refusal the caller
  cannot act on is a defect in Pod.
- **Preference.** A change that claims to help efficiency reports helper calls per result, refusal
  rate and coordinator turns before and after, measured on real work.
- **Preference.** Guidance loads only for the action in hand, and a routine action needs none.

*Prevents:* intelligence spent operating Pod rather than solving the problem.

## 5. Evidence is true and bound to content

- **Rule.** Pod never records or reports a success, a process exit, a test result, a review or an
  acceptance that was not observed. Unknown stays unknown. Unavailable proof is reported as
  unavailable.
- **Rule.** Evidence names the exact commit and tree it was produced against. Proof for one
  content never qualifies another.
- **Rule.** Unchanged proof carries forward by itself when Pod can establish from Git that what it
  covered is unchanged, and never otherwise.
- **Rule.** An external effect acts on an exact candidate, under authority that covers it, and an
  uncertain outcome is settled by reading the provider, never by repeating the action.
- **Rule.** Independent review that a project requires is preserved: the reviewer did not write
  the change, and a required finding is resolved or waived by someone with authority to waive it.
- **Rule.** Worker success, local checks, hosted checks, independent review, project acceptance
  and merge are separate facts and are reported separately.

*Prevents:* false completion, stale proof and unauthorized effects. Simplification never buys
speed with these.

## 6. Failure is ordinary

- **Rule.** A failed, abandoned or interrupted attempt is a recorded outcome. It never becomes a
  standing blocker for work that does not depend on it.
- **Rule.** Records of other objectives, or of the past, do not hold an unrelated decision.
- **Rule.** Continuing an objective never depends on a particular terminal, session or process
  still existing.
- **Rule.** Operations that can be repeated are safe to repeat. Genuine uncertainty about an
  effect is kept, and conflicting effects are prevented.
- **Preference.** One general way to continue beats a mechanism per way of failing.

*Prevents:* recovery subsystems that grow with every incident, and objectives stranded by their
own records.

## 7. Bound work, not lifetimes

- **Rule.** Pod limits what one operation may read, write or spend. It sets no ceiling on how much
  legitimate evidence an objective may accumulate over its life.
- **Rule.** An ordinary decision does not need the full record of an objective, or any record of
  another objective.
- **Preference.** Choose the plainest storage that is correct at ten times today's size, and
  measure before building anything cleverer.

*Prevents:* capacity limits that turn into coordinator procedures.

## 8. Verification in proportion to risk

- **Rule.** An administrative convention is never enforced as a correctness requirement. A check
  exists because skipping it could let a wrong result through.
- **Preference.** Review and testing are sized by what changed and what could break. Affected
  checks run during the work; the complete suite runs once, on the final candidate, in hosted CI.
- **Preference.** A correction is reviewed for what it changed. A full review is repeated only
  when what it covered changed.

*Prevents:* review rounds and test runs caused by the process rather than by the change.

## 9. Remove before adding

Each past addition was locally reasonable. The sum was not. This principle governs how Pod itself
is changed.

- **Rule.** An incident is first answered by removing the state, step or dependency that made it
  possible. Something is added only when that cannot work.
- **Rule.** A new subsystem, service, coordinator-facing command, persisted record kind or
  validation layer needs j3w1's approval, with the answers below in the pull request. A field or
  refusal inside an existing one needs the answers only.
- **Rule.** One complexity problem is not solved by introducing another.
- **Preference.** Prefer an existing capability, then the simplest sufficient implementation.
- **Preference.** Pod's own development carries no parallel inventory that must be kept in
  agreement by hand. Tests state behaviour, and each policy has one authoring source.

The answers an addition owes:

1. What recurring problem does it solve, shown by more than one incident?
2. Why can the problem not be removed instead?
3. Which of Orca, Git, GitHub, the host or the project could own it, and why does it not?
4. What must a coordinator newly know, type or read, what is newly persisted, and what can newly
   refuse?
5. What does the same change remove?

*Prevents:* this document being needed a second time.

## Decisions by j3w1

These are product decisions, not conclusions of the principles. They change only by j3w1's
decision.

- **Python.** Pod uses the latest stable CPython, selected dynamically. One interpreter is resolved
  per CI run and shared by every job. No minor version is written into the repository, there is no
  compatibility matrix and no fallback to an older interpreter.
- **Coordinator.** The current conversation is the coordinator. Direct work is preferred when it
  is sufficient. Delegation is justified by independent work and real time saved. Concurrency is a
  limit, never a target.
- **Runtime.** Orca is the only worker runtime. Linux is the only supported environment.
- **Distribution.** One distribution, installed from `main`, with one authored version.
- **Merge authority.** Pull requests to this repository merge under j3w1's own authority.

## Checks

Where a rule can be verified cheaply, an existing test or source check verifies it. There is no
separate policy-enforcement system, and none is to be built.

| What | Where |
| --- | --- |
| The Python decision | `tests/test_suite_lanes.py`, against `.github/workflows/ci.yml` |
| `AGENTS.md` points here | `tests/test_inventory.py` |

A check added for a principle is listed here in the same change.

## What is not here

Current requirements, the design of any one renovation, incident histories and the defects of a
particular implementation do not belong in this document. They live in issues, pull requests and
tests. A lesson is added here only when it would still be true after the code it came from is
gone.
