# Pod 0.3.0 progress

The 0.3.0 implementation is a candidate, not a published release. The last
published tag remains `v0.1.2`. The specification's historical baseline is
`1b0aa6fc68ee2157f6f67ba4a60076d967791cc4`.

| Area | Current evidence |
| --- | --- |
| Runtime boundary | Implemented locally. Orca owns Runs, Tasks, Dispatches, requests, worker capacity, messaging and lifecycle. Pod retains policy admissions and native references, with no fleet-wide occupancy or cleanup model. |
| Routing and admission | Implemented locally. An admitted model, effort and account route stays fixed. Objective-local reservations enforce logical fan-out; exact native assignment settlement frees a slot, even if a terminal is retained. Orca's definite `capacity_full` refusal creates a durable deferred admission. |
| Recovery and migration | Implemented locally. Orca-issued request UUIDs join completed, pending and absent recovery to the same admission. Conflicting or incomplete identity remains unresolved across later observations. Explicit v1 migration archives source state and promotes only exact native joins. |
| Governor | Implemented locally. ALLOW/REUSE/DEFER decisions use candidate-bound evidence, the current Run's authority and exact objective assignments when relevant. They do not reconstruct foreign workers or physical capacity. |
| Skill and package | The skill and Python package share one `skills/pod` source. Four public helper families remain: `setup`, `config`, `doctor` and `status`. |
| Offline checks | Implementation commit `914ac0523c3330c577d23c2ff9a6d12988d198ba` passed 270 unit tests, four explicit incident tests, 25 frozen build/install gates and 24 independent recovery sequences on Linux/Python 3.13. A fresh Sol/xhigh correction audit found no actionable issue. The main integration candidate receives its own checks before push. |

## Orca dependency

Pod can freeze a selected route and compare Orca's requested and effective
launch evidence after start. The installed Orca launcher has no opt-in,
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

The [validation gates](validation.md) define the proof required for release.
Scenario paths in [coverage](pod-coverage.json) describe intended offline tests;
they do not promote live or hosted rows from `NOT_RUN`.
