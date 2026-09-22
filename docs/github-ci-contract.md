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
the run it superseded and a push to `main` or a release tag is never canceled by anything.
This repository's own `ci.yml` uses exactly this. A deployment or migration job belongs in
a workflow of its own with no `cancel-in-progress`, which is also why the governor's own
cancellation touches only a pending validation run.

## Required checks and selective jobs

GitHub reports a required workflow that never ran as a pending check, not a passed one. A
selective workflow that uses `paths` or `paths-ignore` filters therefore blocks merging on
an unrelated change unless the required check is reported anyway. The safe shapes are a
cheap preparation job that always runs and gates the expensive jobs, or a job that always
reports the required check and skips its expensive steps with a condition. Do not present a
skipped required workflow as success; the governor reads a `skipped` conclusion as FAILED so
it is never reused as proof.

## Merge queue

A merge queue validates the candidate against the target branch plus the queued changes
ahead of it, and only a workflow with a `merge_group` trigger runs there. That context is not
interchangeable with an earlier pull-request check, so the governor never treats a passing
pull-request run as merge-queue evidence, and a repository with a queue keeps the
`merge_group` trigger on its required workflow.

## What the governor consumes

The project maps what its actions trigger in `.pod/config.yaml`:

```yaml
schema: pod/v1
waste_governor:
  preflight: [unit, skill-validation]
  triggers:
    push: ["workflow:ci.yml"]
    pr_update: ["workflow:ci.yml"]
```

`internal governor-status` proposes this mapping from the workflow files it can read, and a
proposal is never policy until it is written here. The target is no unnecessary duplicate
validation of one candidate and context, not exactly one workflow run regardless of what the
repository requires: local preflight receipts are preparation evidence and never replace an
independently required remote proof.

Pod 0.3.0 also supplies Governor decisions with a fresh read-only Orca projection for the
selected objective and Tasks. That projection affects whether related native work is still
active; Pod does not reconstruct worker lifecycle from its remote-action journal.
