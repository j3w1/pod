# Proof

Bind proof to obligation, final candidate, sources, policy, dependencies, environment, command, time and reviewer attempt. Declare non-assurance `proof_scope` at intake; assurance uses reviewed scope. Candidate movement requires explicit `reuse: {from, ids}` for unaffected obligations with Git delta rechecked at later moves; `from` is the candidate the evidence was recorded against, and a refusal names it. A root `VERSION` bump alone does not reopen a scope that merely contains it; metadata that affects behavior or installation does. Changed bindings reopen proof; project rules govern reuse, preferences do not change Governor candidates.

After a governance refresh to a target whose governance sources changed, satisfied proof is not lost: rebind it. Name the rows in `rebind` and attest once with `governance_delta: {sources, reason}`, copying `sources` exactly from the `evidence_invalidated` or `rebind_refused` refusal (Pod computes it: per path and bound blob, both blob ids and added/removed lines), with a reason the change does not affect those rows. The refresh must be the exact snapshot decision: `governance` naming the observed `base_ref` and full `base`, and `revision_authority` from the decider the decision policy names for scope changes. Edited and removed lines are recorded, not refused. Pod refuses rows whose own proof scope names a changed governance file, a change to `.pod/config.yaml`, and proof stamped under governance this record's history cannot resolve; those rows need fresh evidence. Rebind in the refresh itself rather than restating rows unsatisfied around it.

Substantial/risky work requires independent-review assurance; one independent reviewer is the normal start, and another needs a concrete independent risk or missing competence. Retain required audits through checkpoint/report/delivery; unavailable review remains incomplete. Only scoped user waiver or genuine trusted-requirement removal permits policy withdrawal; waiver never passes review. Fresh independent reviewers receive exact candidate and reproducible evidence without priming.

Commission every review in collect mode, stated in its packet:

- The reviewer stays read-only: no source or test edit, no rerun toward green, no fix and no new child.
- It does not stop at its first finding. It covers its whole assigned scope, then reports every finding in one report, with outcome `failed` when any finding is required.
- Each finding gives its severity (`blocker` for a required P1, `major` for a required P2, `minor` for a minor finding, `info` for a nit), file and line, the concrete failing scenario, the defect class and a proposed fix.
- Invocation or harness errors go in `failures` or `uncertainty` as such, never as findings.
- Only a safety event stops it at once: a guard hit, an unexpected live or native effect, or any risk of changing the host. The report then names the stop and the scope left unreviewed.

Stopping at the first finding would turn one class inventory into one finding per round; since reviewers never edit, collecting findings does not weaken "no fixing toward green". After the report, run one fix round for everything found: consolidate its required findings into corrections for one fixer, then follow the correction cycle below. The verifying review is an affected-scope delta review of the same scope by the same reviewer, not a fresh full review: retain the reviewer's tab through the correction cycle and admit the delta review with `reuse_of` its admission; when Orca has already released it, give a fresh reviewer the earlier report. Stricter project rules, such as a fresh final audit, still govern.

Triage required corrections versus advisory proposals; a required correction sets `blocks` to the outcome, requirement, PoD or policy it prevents (`policy:safety`/`policy:privacy` for those defects), and major/blocker downgrades need reasons. A repeated lack of progress is a `churn` diagnostic, never a review-round limit: narrow the approach, repair the named defect or get specialist input within your authority, and escalate only a real scope, risk or authority decision. Retain findings and consolidate corrections. Changed scope, assumptions, dependencies, governance or unknown impact requires affected-scope delta review; explicitly REUSE unaffected proof under project rules. Refuse repeat review of valid bindings. Zero/all-withdrawn assurances withhold the review label.

For each required-correction cycle, complete these steps in order:

1. Consume the fixer's report.
2. Satisfy the correction with its proof.
3. Admit the verifying review.
4. Consume the review with the assurance waiting.
5. Satisfy the assurance with that review attempt.

An unresolved required finding refuses review admission as `review_precedes_resolution` and names its correction. Older premature review evidence is invalidated as `ordering`, with the finding's resolution sequence and the attempt's admission and report sequences; it needs a fresh review after correction resolution.

Reports join packet, admission and native attempt; Git supplies changed paths. Short non-assurance proof names check, command, result and reference; settled served assurance may use its attempt. Report obligation provenance, withdrawals, blocks, proposals, boundary exceedances, triage downgrades and remaining gates. Only an authorized revision through the supported decision boundary reopens ordinary coordinator closure; evidence closure is immutable and refuses every Pod write. Synthetic proof never certifies live behavior; unobserved usage/cost stays unknown, never inferred from model labels/counts.

When another tool, such as a lead's or a project's assistant, must show that Pod accepted a worker's report, point it at `pod status --objective ID --admission ADMISSION --json`, never at Pod's records. It is the supported read-only proof: the versioned `pod-admission-view/v1` object names the packet the admission ran, its candidate and native Task and Dispatch, and the consumed report's status, settlement, attempt and ingestion, or says that no report has been consumed. It reads Pod's own record only, takes no record lock, needs no Orca terminal and carries no report or record-derived text, so its `unknown` facts stay unknown rather than being read from anywhere else.

For composite model rules, record the assurance's typed `audit_role` before admission and verify it against the served work using the router's model guidance. A requested coordinator arrangement needs actual supported effective-route evidence; unavailable native evidence stays UNAVAILABLE, and a preview is not proof. Decision authority never supplies review truth or accepts a worker result.
