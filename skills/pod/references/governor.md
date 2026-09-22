# Governor

The Governor evaluates expensive Pod-mediated actions at decision boundaries,
with no model calls or background process. It owns candidate and remote-action
evidence. Orca owns worker lifecycle.

## Prepare a candidate

Governor mutations require this terminal to own a stable native current Run that
the objective checkpoint or admissions reference on the same runtime, with the
same existing Pod owner. Establish or adopt that Run explicitly through Orca's
installed guide; Pod never creates or adopts one implicitly. Without this proof,
safe direct work and read-only `internal governor-status` remain available.

Group related corrections into a delivery unit: the work reviewed and delivered
together. Use `internal governor-prepare` to name its Tasks, branch, base,
workflow files and verification commands. The helper reads Git and freezes commit,
tree, base, workflow digests, toolchain, environment and policy revision.
Changed inputs create a new generation; unchanged inputs reuse it.

Record configured local checks through `internal governor-preflight`. Project
`waste_governor.preflight` names the checks; `waste_governor.triggers` maps
downstream effects. An action may declare its effects explicitly. A push that
starts CI is judged as validation. Uncommitted tracked changes leave the
candidate unfrozen.

## Decide and execute

Before push, pull-request update, workflow dispatch, rerun, remote diagnostic,
merge, release, deployment or cancellation, obtain a Governor decision:

- `ALLOW`: perform the exact bound action.
- `REUSE`: attach to equivalent running work or use matching passing evidence.
- `DEFER`: resolve the reported reason before acting.

Warnings annotate these decisions. Checks proceed through authority, candidate
and effects, equivalent work, supersedence and unresolved actions, readiness,
then failure history. Readiness uses this unit's fresh native observations,
admission holds, corrections and preflight. Independent units can proceed
separately. Review is not a universal prerequisite for CI.

Use `internal governor-execute` for supported push, pull-request, workflow,
rerun and cancellation actions. It admits and performs one action against the
bound commit. Calling `internal governor` separately can reserve that action;
a later execution must not duplicate the reservation. Merge, release and
deployment remain project-governed actions.

A lost response remains `UNKNOWN`; `internal governor-reconcile` uses provider
readback without resubmitting. Each row retains its candidate, including after
supersedence. Successful publication records the validation it triggers so a
later workflow request can attach to that run.

## Failures and supersedence

Classify a failed remote action with `internal governor-classify` before another
attempt:

- `code_defect`: return to local correction and the existing diagnosis rule.
- `remote_only`: name the question, local limitation, discriminating remote
  check and stopping condition.
- `transient`: use the configured bounded retry.
- `external`: report the dependency blocking progress.

Unclassified failures defer. Repeated corrections retain their history.
Supersedence may cancel only pending, cancel-safe validation of the older
candidate in the same unit, when policy permits. Its result cannot approve the
new candidate.

Legacy Governor rows lack the bindings needed for evidence reuse. Settled rows
remain history; unresolved rows remain holds. They never become proof merely
because a logical key matches.

## Authority and reporting

An efficiency exception must rejoin a scoped personal grant and identify who
applied it and why. It cannot lift authorization, correctness or spending holds.
Observe mode converts only efficiency deferrals into warnings. Project policy
may narrow these controls.

Enforcement is advisory unless personal policy names host controls; that claim
remains owner configuration. `internal governor-status` reports candidates,
preflight, decisions, pending validation and counters. Workflow trigger proposals
are suggestions until explicitly configured.

Unknown authority, candidate or effects defer; unknown cost warns. Verification
gaps block merge, release or deployment. Local checks do not replace independently
required hosted proof. Follow the project's required-check and merge-queue
contract; distinct validation contexts need their own evidence.
