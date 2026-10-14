# GitHub reference CI contract

This is the repository-side half of the waste governor: a pattern a repository may adopt so
that superseded validation is canceled where that is safe, required checks stay correct, and
a merge queue keeps its own validation context. It is documentation of a pattern. Pod never
rewrites a workflow, removes branch protection or applies this to another repository.

## Scoped concurrency

Cancel a superseded pull-request run, and only that:

```yaml
concurrency:
  group: ci-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

The group is one pull request or one ref, so a newer push to the same pull request replaces
the run it superseded and a push to `main` is never canceled by anything.
This repository's own `ci.yml` uses exactly this. A deployment job belongs in a workflow of
its own with no `cancel-in-progress`, which is also why the governor's own
cancellation touches only a pending validation run.

## Required checks and selective jobs

GitHub reports a required workflow that never ran as a pending check, not a passed one. A
selective workflow that uses `paths` or `paths-ignore` filters therefore blocks merging on
an unrelated change unless the required check is reported anyway. The safe shapes are a
cheap preparation job that always runs and gates the expensive jobs, or a job that always
reports the required check and skips its expensive steps with a condition. Do not present a
skipped required workflow as success; the governor reads a `skipped` conclusion as FAILED so
it is never reused as proof. This repository's required `linux` check is such an
always-reporting job: it runs with `if: always()` after every test lane share and the `checks`
job, and passes only when each of them passed.

## Merge queue

A merge queue validates the candidate against the target branch plus the queued changes
ahead of it, and only a workflow with a `merge_group` trigger runs there. That context is not
interchangeable with an earlier pull-request check, so the governor never treats a passing
pull-request run as merge-queue evidence, and a repository with a queue keeps the
`merge_group` trigger on its required workflow.

## What the governor reads back

The governor binds a validation row to a run only when the run matches what the row's own effect
started: one of its events, its exact head commit, created at or after the submission, and its own
attempt (R115). A push or pull-request update starts `push` runs and, on a branch with an open pull
request, `pull_request` or `pull_request_target` runs, so a row derived from a publication binds
only those; a requested dispatch or diagnostic binds only a `workflow_dispatch` run. Every row
binds its run's first attempt, except a rerun. Before rerunning, the governor records the run, its
latest attempt and the submission start, and right after GitHub accepts the call it records that
acceptance; one rule binds the rerun's attempt on every path. The count proves it: with the
acceptance recorded, the single new attempt is the one Pod's call created. GitHub sets a rerun's
`startedAt` at the request in whole seconds, so that start is only checked against the second
holding the recorded submission start, and a start in an earlier second holds as a contradiction.
Every other state, an attempt bound without that provenance included, holds the row, since GitHub
names no requester per attempt. A run already bound to a row is checked the same way as a new
one, and its workflow by appearing in the workflow-filtered list, because `gh run view` names only the
workflow's display name.
It reads runs with `gh run list --workflow FILE --commit SHA --json …,attempt --limit 21`:
GitHub filters by workflow and commit, and the extra run tells the governor that the newest 20 were
not all of them. GitHub lists only the runs it still retains. A missing run or a truncated list
leaves the row pending with that reason, even when a visible run matches or is the row's own bound
run; it is never read as a failure or as proof that nothing ran.

## What the governor consumes

The project maps what its actions trigger in `.pod/config.yaml`:

```yaml
schema: pod/v1
waste_governor:
  preflight: [unit-and-incident-suite, skill-validation, compile]
  triggers:
    push: ["workflow:ci.yml"]
    pr_update: ["workflow:ci.yml"]
```

`internal governor-status` proposes this mapping from the workflow files it can read, and a
proposal is never policy until it is written here. The target is no unnecessary duplicate
validation of one candidate and context, not exactly one workflow run regardless of what the
repository requires: local preflight receipts are preparation evidence and never replace an
independently required remote proof.
