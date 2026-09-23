# Pod 0.3.0 progress

The 0.3.0 implementation is a candidate, not a published release. The last
published tag remains `v0.1.2`. The specification's historical baseline is
`1b0aa6fc68ee2157f6f67ba4a60076d967791cc4`.
A read-only 2026-09-23 inspection found releases/tags `v0.1.0`, `v0.1.1` and
latest `v0.1.2`; this task authorized no release or tag mutation.

| Area | Current evidence |
| --- | --- |
| Execution Spec | Implemented locally. One installed conditional reference defines the human Markdown contract; bounded issue intake validates full body, exact issue/repository identity and source digest. Checkpoints/packets bind the source without copying it. |
| Objective worktree | Implemented locally. Git common-directory identity joins linked worktrees to one objective state; canonical private project policy remains effective and worktree policy can only narrow it. Creation, tabs, release and removal remain native Orca/host operations. |
| Runtime boundary | Implemented locally. Orca owns Runs, Tasks, Dispatches, requests, worker capacity, messaging and lifecycle. Pod retains policy admissions and native references, with no fleet-wide occupancy or cleanup model. |
| Routing and admission | Implemented locally. New policy accepts the exact six-model catalog. Routes bind effort plus requested/effective context; `256k` is a 256,000-token ceiling. Current Orca exposes no per-worker context control, so production worker routes refuse before effect while direct work remains available. |
| Recovery and migration | Implemented locally. Orca-issued request UUIDs join completed, pending and absent recovery to the same admission. Conflicting or incomplete identity remains unresolved across later observations. Explicit v1 migration archives source state and promotes only exact native joins. |
| Governor | Implemented locally. ALLOW/REUSE/DEFER decisions use candidate-bound evidence, the current Run's authority and exact objective assignments when relevant. They do not reconstruct foreign workers or physical capacity. |
| Skill and package | The skill and Python package share one `skills/pod` source. Four public helper families remain: `setup`, `config`, `doctor` and `status`; guided approval/revocation stays inside `config`. |
| Offline checks | Milestone A commit `cb55d52d3d3a9c9871c6f104d0e6447717ec27d0` passed 283 tests. The complete issue-14 working-tree candidate passed 302 tests, four explicit incident tests, compile/diff, platform, tracked-source and skill validation locally; the final commit still needs frozen packaging, hosted and independent gates. Prior evidence is not promoted automatically. |

## Orca dependency

Pod can freeze a selected route and compare Orca's requested and effective
model/effort launch evidence after start. Installed Orca 1.4.209 has no per-worker
context selector or opt-in,
noninteractive strict-startup control that proves the provider accepted the
selected model and effort before task input. Provider migration prompts may
therefore interrupt startup. This feature belongs in Orca's provider adapter and
must be scoped per launch so ordinary sessions keep their behavior. Pod does not
automate prompts or change shared provider settings.

## Open gates

- Production Codex/Sol high and Claude/Sonnet medium strict-startup probes are
  `NOT_RUN` pending that Orca control. They are not replaced by supervised
  implementation or review workers.
- The required live Codex and Claude core/delegation matrices are `NOT_RUN` on
  this candidate. Synthetic `capacity_full` cases do not prove actual runtime
  capacity behavior.
- Hosted candidate CI and external project acceptance require their own
  candidate-bound evidence. Merging development work into `main` does not
  publish a 0.3.0 tag, package or release.
- Normal native visible-tab startup, prompt report consumption/Delivery acknowledgment,
  worker release and final objective cleanup remain `NOT_RUN` on the issue-14 candidate;
  instructions and offline adapter tests are not live proof.

The [validation gates](validation.md) define the proof required for release.
Scenario paths in [coverage](pod-coverage.json) describe intended offline tests;
they do not promote live or hosted rows from `NOT_RUN`.
