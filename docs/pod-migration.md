# Pod cutover inventory

Baseline `17a2316` and Git history preserve all legacy code, tests, recovery descriptions and acceptance evidence. This branch changes repository source and installed package surface only. It does not mutate existing host-local native state or release resources.

| Legacy area | Disposition | Replacement / retained invariant |
| --- | --- | --- |
| `src/orchestrate/controller.py`, coordination, state and bootstrap CLI | Remove | Current conversation coordinates. `pod.ledger` stores bounded private effects/checkpoints; native Orca remains authoritative. No autonomous scheduler. |
| Managed machine installer, PATH repair and the retired transport launcher | Remove | Explicit reviewed-checkout `install.py`, a fresh selected venv and no PATH write. The supported execution environment is Linux. |
| Role roster and project JSON routing | Remove | Personal/project `pod/v1` YAML, strict merge, pending starter table, immutable route decisions. |
| Source/packet/admission safety | Adapt | `pod.records` bounded packet/report/source/evidence checks, `pod.ledger` intent/reconciliation and capacity. Route establishment blocks a launch whose billing mode is unknown or whose paid route has no grant. |
| Legacy public command tests and incident fixtures | Remove | `tests/test_*.py` and `tests/incidents/test_safety_boundaries.py` cover new behaviour; `docs/pod-coverage.json` maps every scenario, including unrun gates. |
| Retired-platform bootstrap fault gates | Remove | Explicit isolated installer and a fresh wheel smoke on the one hosted Linux job. Legacy managed PATH effects are no longer a Pod behaviour. |
| Native lifecycle, uncertain effects, Delivery replay | Adapt | Ledger tests preserve no blind retry, occupancy and all-item reconciliation; live lifecycle remains unverified. |
| Review/acceptance state | Adapt | Candidate-bound evidence/acceptance records keep local, review, hosted and acceptance distinct. |

## Retired test and gate disposition

| Legacy test or gate | Keep / adapt / remove | Pod replacement |
| --- | --- | --- |
| `test_admission.py`, `test_profile_sources.py`, selected/ignored/large-source incident files | Adapt | `test_records.py`, `test_ledger.py`, `test_config.py`, and `test_safety_boundaries.py` retain bounded identity, source changes, rejected restoration and authority restrictions; legacy CE discovery paths remain historical, not a Pod authority. |
| `test_orca.py`, `test_coordination.py`, `test_controller.py`, capacity/creation-history incidents | Adapt | `test_orca_adapter.py`, `test_operations.py`, `test_ledger.py` cover exact read verbs, terminal-optional receipt, one-shot admission, shared account occupancy, uncertain effects and Delivery replay. Live native execution is still required. |
| `test_packets.py`, `test_state.py`, `test_intervention.py`, `test_efficiency.py` | Adapt | `test_records.py`, `test_ledger.py`, `test_context_quota.py`, `test_release.py` cover bounded packets, private checkpoints, diagnosis, evidence and explicit unknown usage. |
| `test_doctor.py`, `test_bootstrap.py`, `test_machine_bootstrap.py`, `test_path_faults.py`, machine-bootstrap path incident, `installed_machine_smoke.py` | Remove obsolete managed installer contract | `test_setup_cli.py`, `test_install.py`, `test_bundle_install.py`, package wheel smokes, hosted Linux CI and explicit no-PATH/no-hook passive checks. |
| The retired transport tests and smoke | Remove with the platform they served | The supported execution environment is Linux; the retired scenarios are recorded as `RETIRED` rather than as unrun. |
| The old dual-platform simulated suite | Remove obsolete provider gate | The full unit and incident suite runs on one hosted Linux job, beside a supported-environment audit of the tracked product. |
| Old controller/managed-bootstrap installed wheel smoke | Remove | A frozen Pod wheel in a fresh isolated environment, the CLI help/config/doctor/setup smoke, a copied-bundle smoke and a skills-CLI install. |
| Old hosted, review, live, acceptance and publication rows | Keep as historical only | New exact-candidate rows in [validation](validation.md) and read-only `release-gate`; every external gate starts `NOT_RUN`. |

An older orchestrate-associated native resource remains in `release_unknown` despite settled execution and unverifiable liveness. Its exact recovery evidence is held outside this branch by the coordinator. Installed-state deletion or migration must wait for exact owner/native reconciliation. No code deletion here is evidence that the resource was released.

Canonical repository rename/registration, installed-state cutover, hosted/live gates, independent audit and external acceptance are owner-controlled release steps. The Pod spec is the sole active Pod product authority; [legacy contracts](contracts-and-recovery.md) and [historical first increment](live-first-increment.md) are retained as history in Git.

## Cutover to the `pod` identity

The repository, canonical checkout, host registration and Orca project identity move from
`orchestrate` to `pod` together. GitHub is renamed in place so the repository id, history,
issues and pull requests are preserved; the automatic legacy redirect is acceptable and is
not a request for compatibility aliases. Local remotes and active references are updated
rather than left to that redirect.

Local material moves to `~/dev/pod`, `~/worktrees/pod/<task>`, the host's per-project task
records, evidence and archive namespaces. Git-aware worktree movement and repair is used,
never a bare directory move, and every retained worktree's repository identity, HEAD,
dirty inventory and remote are verified afterwards. Virtual environments and installed
entry points are recreated at their final locations. No legacy `orchestrate` command,
package shim, skill alias or compatibility symlink is left active.

Sealed historical evidence moves without being rewritten: hashes, commit identities, quoted
historical paths and native record identifiers are preserved, and a separate relocation
index records the new locations. A historical quoted path is not an active dependency, and
no home-wide text replacement is performed. The unresolved legacy `release_unknown` native
resource is retained, not deleted; it blocks only an operation that genuinely depends on
resolving it.
