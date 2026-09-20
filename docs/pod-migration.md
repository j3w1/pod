# Pod cutover inventory

Baseline `17a2316` and Git history preserve all legacy code, tests, recovery descriptions and acceptance evidence. This branch changes repository source and installed package surface only. It does not mutate existing host-local native state or release resources.

| Legacy area | Disposition | Replacement / retained invariant |
| --- | --- | --- |
| `src/orchestrate/controller.py`, coordination, state and bootstrap CLI | Remove | Current conversation coordinates. `pod.ledger` stores bounded private effects/checkpoints; native Orca remains authoritative. No autonomous scheduler. |
| Managed machine installer, PATH repair, WSL launcher | Remove | Explicit reviewed-checkout `install.py`, fresh selected venv, no PATH write. WSL/remote transport blocks until verified. |
| Role roster and project JSON routing | Remove | Personal/project `pod/v1` YAML, strict merge, pending starter table, immutable route decisions. |
| Source/packet/admission safety | Adapt | `pod.records` bounded packet/report/source/evidence checks, `pod.ledger` intent/reconciliation and capacity. Native prelaunch assurance blocks when unsupported. |
| Legacy public command tests and incident fixtures | Remove | `tests/test_*.py` and `tests/incidents/test_safety_boundaries.py` cover new behavior; `docs/pod-coverage.json` maps 82 scenarios, including unrun gates. |
| Native Windows bootstrap fault gates | Remove | Explicit isolated installer and fresh wheel smokes on both native CI hosts. Legacy managed PATH effects are no longer a Pod behavior. |
| Native lifecycle, uncertain effects, Delivery replay | Adapt | Ledger tests preserve no blind retry, occupancy and all-item reconciliation; live lifecycle remains unverified. |
| Review/acceptance state | Adapt | Candidate-bound evidence/acceptance records keep local, review, hosted and acceptance distinct. |

## Retired test and gate disposition

| Legacy test or gate | Keep / adapt / remove | Pod replacement |
| --- | --- | --- |
| `test_admission.py`, `test_profile_sources.py`, selected/ignored/large-source incident files | Adapt | `test_records.py`, `test_ledger.py`, `test_config.py`, and `test_safety_boundaries.py` retain bounded identity, source changes, rejected restoration and authority restrictions; legacy CE discovery paths remain historical, not a Pod authority. |
| `test_orca.py`, `test_coordination.py`, `test_controller.py`, capacity/creation-history incidents | Adapt | `test_orca_adapter.py`, `test_operations.py`, `test_ledger.py` cover exact read verbs, terminal-optional receipt, one-shot admission, shared account occupancy, uncertain effects and Delivery replay. Live native execution is still required. |
| `test_packets.py`, `test_state.py`, `test_intervention.py`, `test_efficiency.py` | Adapt | `test_records.py`, `test_ledger.py`, `test_context_quota.py`, `test_release.py` cover bounded packets, private checkpoints, diagnosis, evidence and explicit unknown usage. |
| `test_doctor.py`, `test_bootstrap.py`, `test_machine_bootstrap.py`, `test_path_faults.py`, machine-bootstrap path incident, `installed_machine_smoke.py` | Remove obsolete managed installer contract | `test_setup_cli.py`, `test_install.py`, package wheel smokes, native Windows/Linux CI and explicit no-PATH/no-hook passive checks. |
| `test_wsl.py` and `orchestrate-wsl` smoke | Remove unverified Windows-owned-controller assumption | WSL/remote transport blocks until a verified single runtime/state owner and live transport trial; A34/A58/A62 remain live `NOT_RUN`. |
| Old POSIX plus simulated-Win32 full suite | Remove obsolete provider gate | New full unit/incident suite runs on native Linux and Windows CI. Linux synthetic tests do not stand in for native Windows. |
| Old controller/managed-bootstrap installed wheel smoke | Remove | Frozen Pod wheel in fresh isolated environments on both native CI jobs, CLI help/config/doctor/setup smoke. |
| Old hosted, review, live, acceptance and publication rows | Keep as historical only | New exact-candidate rows in [validation](validation.md) and read-only `release-gate`; every external gate starts `NOT_RUN`. |

An older orchestrate-associated native resource remains in `release_unknown` despite settled execution and unverifiable liveness. Its exact recovery evidence is held outside this branch by the coordinator. Installed-state deletion or migration must wait for exact owner/native reconciliation. No code deletion here is evidence that the resource was released.

Canonical repository rename/registration, installed-state cutover, hosted/live gates, independent audit and external acceptance are owner-controlled release steps. The Pod spec is the sole active Pod product authority; [legacy contracts](contracts-and-recovery.md) and [historical first increment](live-first-increment.md) are retained as history in Git.
