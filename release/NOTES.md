# Pod 0.4.0

### Coordination boundary

- Orca owns Runs, Tasks, Dispatches, request recovery, placement, messaging, actual
  capacity and worker lifecycle. Pod keeps objective-level admission, routing, spending and
  evidence: one logical reservation per assignment, freed by exact native assignment
  settlement, with no fleet census, Delivery, cleanup or release state of its own.
- Normal workers start through native `worker-start` into visible agent tabs. After a
  report is validated and preserved, Delivery acknowledgment and worker release follow
  Orca's order promptly; uncertain or protected resources are retained.

### Issue-first workflow

- A GitHub issue URL or a direct objective enters one workflow. The complete issue is read
  through authorized `gh` access, its target is checked against the actual checkout, and
  checkpoints and packets bind its identity and body digest without copying the body.
- The `references/execution-spec.md` reference defines the readable Pod Execution Spec
  with numbered Proof of Done items and an explicit delivery endpoint.
- Implementation binds an Orca-managed objective worktree, normally on an
  `orca/<task-slug>` branch, by Git common directory, branch and path. Linked worktrees
  share one objective state, and canonical private project policy can only be narrowed.

### Routing and admission

- The catalog is exactly Luna, Sol and Astra on Codex and Sonnet, Opus and Fable on
  Claude. Routes carry effort plus a `256k` (256,000-token upper bound) or `max` context
  profile and keep requested and proven effective limits distinct. Where the installed Orca
  exposes no per-worker context control, a context-dependent route refuses before effect.
- `config approve ALIAS` and `config revoke ALIAS` recheck the joined native account, auth
  and billing evidence, show a redacted proposal, require explicit confirmation and edit
  only the personal YAML. Direct work needs no approved route.
- Orca's redacted selected-account identity is the one key for approval, quota and grants.
  Identity and billing proof come from the same selected context and are re-read
  immediately before a start or pending replay; a rotated account is never relabeled.

### Recovery

- Orca-issued request UUIDs join completed, pending and absent recovery to the same
  admission. A contradictory request identity stays unresolved across later incomplete
  responses, and nothing starts a replacement.
- Orca's documented effect-free refusals `task_not_found`, `task_not_startable` and
  `inject_rejected` are recorded as durable `deferred` admissions with no binding and no
  blind retry. `runtime_error` holds `unresolved` until request and worker readback settle
  it. Any other refusal code stays fail-closed.

### Governor

- The governor is a deterministic kernel inside the execution path. Work is grouped into
  delivery units with candidate generations frozen from Git: commit, tree, base, workflow
  digests, verification commands, toolchain, environment and policy revision. A changed
  workflow or base is a new candidate even on the same commit.
- Decisions are `ALLOW`, `REUSE` or `DEFER` with the reason and next action; `WARN` is an
  annotation. A push that starts CI is validation and needs the configured local preflight.
  An identical running action is attached to, and a passing result for the same candidate
  and context is reused.
- `internal governor-execute` performs push, pull-request reuse or creation, workflow
  dispatch, rerun and cancellation against the bound commit through an exact `git` and `gh`
  allowlist. A lost response stays `UNKNOWN` and `internal governor-reconcile` settles it
  from provider readback without resubmitting. Supersedence cancels only a pending,
  cancel-safe validation.
- Remote failures are classified before another attempt: a code defect goes through the
  intervention ledger, a remote-only question gets a bounded diagnostic, and enforcement is
  reported as advisory unless the owner declares a host control.

### Interfaces and state

- Public helper families remain `setup`, `config`, `doctor` and `status`. Private state is
  `pod-context/v3` with admissions `reserved`, `bound`, `unresolved`, `closed` and
  `deferred`; CLI envelopes are `pod-cli/v3`. A state record or governor journal in another
  schema is reported by `doctor` and `status`, blocks that objective, and is never converted.
- `policy.retain_idle_minutes` is not a configuration key; worker retention and disposition
  are explicit Orca operations.

### Release process

- A push to `main` whose declared version has no tag publishes the GitHub Release with the
  wheel, sdist, skill bundle archive and checksums, but only when the committed
  `release/evidence.json` passes Pod's release gate for that exact candidate with owner
  authorization. A blocked gate leaves `main` merged and unpublished. `release/NOTES.md`
  holds only the current version's notes; earlier notes live on the releases page.

### Compatibility

- Developed against Orca 1.4.209. Capabilities are discovered from the installed runtime per
  operation; no minimum version is enforced. Supported execution environment: Linux with
  Python 3.13+ and PyYAML 6.x.
