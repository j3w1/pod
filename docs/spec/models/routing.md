# Model routing

How the coordinator chooses an exact route for an assignment, and how route failures hold.
Route authority and editing live in [preferences](preferences.md); registry, observations and
the joined projection live in the [catalog](catalog.md).

## Requirements

### R09 — Keep role, agent, model and effort distinct

Type: B · Scenarios: [A05](../coordination.md#a05), [A28](#a28), [A44](../coordination.md#a44)

Keep role, agent application, exact model id and exact effort distinct. A route is `(agent, model, effort)` with the stable key `agent/model/effort`; records also keep the three fields separately. Do not merge versioned models or effort variants through a display-name alias. Do not encode a universal intelligence/cost ladder or permanently expensive reviewer role.

### R15 — The coordinator selects a suitable exact route

Type: B,H · Scenarios: [A05](../coordination.md#a05), [A06](../coordination.md#a06), [A07](#a07), [A36](#a36), [A123](#a123), [A179](#a179), [A182](#a182)

The coordinator selects a suitable enabled route for each assignment and records a short reason covering task fit, effort, preference/pin handling and the decisive tradeoff. Without a pin it uses the Preferred route when that route is suitable; choosing another eligible route needs a material assignment-specific reason, and the decision records the chosen route, the Preferred route and that reason. A valid personal pin governs every new Pod-routed implementation, investigation, correction and review assignment at its exact model and effort, or nothing dispatches: there is no fallback or effort change, and a pin conflict is reported, never solved by overriding the pin. The pin overrides repository role/model selection only; direct user constraints, host restrictions, review independence, tool permissions and spending limits still hold. Coordinator direct work and host-created helpers outside Pod routing remain outside the pin, and Pod never delegates just to apply a pin. A stricter repository rule names its actual source in the route reason with `repository` provenance. A deterministic boundary validates eligibility, the exact pin, constraints, effort, agent and native context support without reranking or another model call. An attempt keeps its route; safe replacement needs a fresh decision. Safety refusal bars rerouting the same Task.

### R101 — Quality first, efficiency within suitability

Type: B · Scenarios: [A209](#a209), [A210](#a210)

The best outcome meets the assignment's explicit acceptance and verification bar within authority while minimizing expected total cost, delay and avoidable rework, not a benchmark score or the next call's price. Selection uses direct work when sufficient; applies hard eligibility, authority and the exact pin; establishes suitability plus justified capability margin; uses the Preferred route unless a material task-specific reason favors another eligible route; compares total-work efficiency only among suitable routes; and records the reason before deterministic admission. AA intelligence, benchmark cost and latency may inform that comparison after suitability only; `pod models --json --eligible-only` is the compact eligible guidance-bearing form; its rows retain the existing AA comparison provenance. There is no weighted score, intelligence/cost ratio, predicted-success percentage, model-selection API call, fixed role-to-model rule or mandatory evaluation of every route, and missing AA data never blocks a supported route.

### R19 — Keep actual rate-limit, unavailable and authentication failures attempt-local

Type: B,H · Scenarios: [A13](#a13), [A14](#a14), [A15](#a15), [A16](#a16), [A52](preferences.md#a52), [A127](#a127), [A184](#a184)

Keep actual rate-limit, unavailable and authentication failures on the failed attempt with source and native retry-after. Honor meaningful native retry-after. Temporary unavailable evidence without one gets a 60-second attempt-local reconsideration point; a later coordinator continuation/assignment may reconsider, without automatic launch or inferred recovery. Relevant validated successful use clears suppression and retains history. Suppression is keyed by model, so it covers every effort of that model. Auth and capacity faults retain their existing protections; alternatives require known settlement or proven no-start. There is no rotation or blind retry loop.

### R47 — Record relevant observed outcomes and suggest only after a pattern

Type: B,H · Scenarios: [A37](../coordination.md#a37), [A70](#a70)

Record relevant observed outcomes and suggest preferences only after a meaningful pattern for one exact route. Never silently rewrite preferences, infer savings from model labels or launch paid A/B experiments automatically.

### R78 — Reactive failure records are attempt-local

Type: B,H · Scenarios: [A127](#a127), [A141](#a141), [A184](#a184)

Reactive failure records are attempt-local, preserving observed source, stage evidence and time. Readiness timeout alone leaves cause unknown and cannot imply bad credentials or login repair. Honor native retry-after and R19 local reconsideration; stale failure/memory cannot establish permanent model/family bans. Require settlement before alternatives, retain an unavailable pin without changing its model or effort, and bar same-Task rerouting after safety refusal. No shared health cache, probes or scheduler. Routine questions use Orca reply; advisories keep the route, informational warnings need no input, and unknown or permission prompts block locally.

## Acceptance scenarios

- <a id="a07"></a>**A07** — An explicit model restriction produces no silent substitution.
- <a id="a13"></a>**A13** — A recorded native rate limit holds the same route until retry-after or a meaningful change.
- <a id="a14"></a>**A14** — Missing service-limit metadata does not lower ordinary worker concurrency.
- <a id="a15"></a>**A15** — A recorded unavailable or authentication failure is not retried blindly.
- <a id="a16"></a>**A16** — An alternative route waits for exact settlement or proven no-start of the failed attempt.
- <a id="a28"></a>**A28** — Substantial or high-risk changes receive candidate-bound independent review through recorded assurance obligations, including under stricter project rules.
- <a id="a36"></a>**A36** — No fallback intended to bypass a safety refusal.
- <a id="a70"></a>**A70** — Feedback may suggest but never auto-edits preferences or launches unauthorized live work.
- <a id="a123"></a>**A123** — Coordinator choice is proportionate and independently reasoned; a deterministic guard validates but never ranks suitability.
- <a id="a127"></a>**A127** — Actual route failures honor retry-after and require settlement before alternate dispatch; a safety refusal bars same-Task reroute.
- <a id="a141"></a>**A141** — Routine worker questions get coordinator replies; advisories retain route, while permission or unknown prompts block and safety refusal does not reroute.
- <a id="a179"></a>**A179** — An unpinned assignment that overrides the Preferred route records the chosen and Preferred routes with a material task-specific reason; a stricter repository model restriction is recorded with `repository` provenance and its source, while the running coordinator is unchanged.
- <a id="a182"></a>**A182** — Every supported Pod role and delegated correction obeys the pin at its exact model and effort, despite repository model mandates; any other effort is a pin mismatch, terminal reuse must match the pinned route exactly, and wrong or unknown effective evidence is not success. Direct user and non-model restrictions still hold. Selection/final admission bind current preferences, while submitted/pending replay retains its original route; the coordinator and outside helpers remain outside the pin.
- <a id="a184"></a>**A184** — Temporary unavailable evidence holds immediate equivalent starts, honors meaningful native retry-after or a 60-second local fallback, and expires only for a subsequent coordinator decision. Relevant successful reuse clears suppression without erasing history or changing preferences. Earlier failures impose no permanent ban; uncertain requests require reconciliation, alternatives require settlement/no-start, pins never fallback, and real auth/capacity/permission/safety protections remain. Readiness alone leaves cause unknown.
- <a id="a209"></a>**A209** — Installed guidance and human review cover trivial direct work, strongly testable work, high-risk judgment, blocking latency, recurring tasks, Preferred overrides and pin conflicts. The conditional models reference also keeps the best-outcome rule, the valid override reasons as an open list, the negative routing signals, strong starts with diagnosis instead of an effort ladder, the post-suitability AA comparison rule, review diversity that never defeats a pin or adds reviews, and no delegation merely to apply a pin; no test claims that a deterministic benchmark winner proves semantic suitability.
- <a id="a210"></a>**A210** — Selection and admission use no benchmark score, ratio or winner: AA data may only compare already-suitable routes, missing or stale data never blocks a supported route, and observation changes never alter an admitted route.

## Selection criteria

A **capability floor** is the minimum credible ability for the assignment. **Capability margin**
is additional strength justified by difficult-to-detect errors, uncertainty or costly
correction. Both are qualitative judgments, not invented success probabilities.

| Criterion | Question and consequence |
| --- | --- |
| Reasoning and ambiguity | Is the solution specified or must it be discovered? Subtle reasoning and unresolved intent need more margin. |
| Failure cost | How damaging is a missed defect? Security, authority and concurrency deserve proportionate margin, not an automatic max route. |
| Verifiability | Can independent deterministic checks expose errors? Strong checks may support cheaper execution; self-authored tests alone are not a complete oracle. |
| Breadth and horizon | Does work span interacting modules or sustained steps? File count alone does not imply difficulty. |
| Tool and capability needs | Does the route support the actual tools and operations? A model score cannot compensate for missing capability. |
| Useful context | Is the packet sufficient, relevant and bounded? Preserve working and verification headroom; narrow or decompose an insufficient packet. |
| Latency | Is the assignment blocking or background? Consider the workflow, not first-response time alone or a guessed duration. |
| Recurrence | Will equivalent work repeat? Once quality is protected, repetition strengthens the case for cost efficiency. |

Valid reasons to override the Preferred route include insufficient margin, missing capability or
context, observed unavailability, disproportionate cost, materially unsuitable latency and
useful independent-review diversity. The reason names the tradeoff, not merely that another
model ranks higher. The registry's guide profiles are starting points, not role mandates; they
do not require exhausting cheaper models first, a known hard task may start strong, and repeated
failure needs diagnosis rather than a blind effort ladder. Review requires independent judgment;
family diversity is useful only among sufficiently capable eligible routes and never defeats a
pin or creates extra reviews.

After suitability, AA intelligence, benchmark cost and latency may support a comparison. A
comparably measured route that is no better on any selected dimension and worse on at least one
normally needs a task-specific reason. This is a judgment aid, not an automatic exclusion or an
acceptance gate; unknown data, different benchmark versions or harnesses, rounded ties and
measurement noise limit the conclusion. The highest AA index, lowest price, provider brand, file
count, reviewer role, a free slot or a stale failure alone never decides the route.

## Route contract

The coordinator proposes `{agent, model, effort, context: native_default, reason}` after reading
the pool. The effort is one of the route's exact supported efforts; `native_default` effort is
refused for new decisions and remains readable only in older admissions and their recovery.
Deterministic selection validates the proposed id, eligibility, the exact pin, constraints,
agent, effort and native context control; it does not rank choices. Refusals name `effort_required`,
`effort_unsupported`, `unsupported_model`, `route_ineligible`, `empty_pool`, `pin_mismatch`,
`setup_required` or the specific constraint or failure. Context stays orthogonal: select useful
packet content now and record `native_default` while Orca lacks an enforceable scoped selector;
documented ceilings and observed context are shown separately and never multiply routes.

The route decision keeps the earlier fields and adds `route`, `preferred_route`, `pinned_route`
and `preference_schema`; `pinned_model` is the pinned route's model. New rows no longer write
`mode`; earlier rows keep it and stay readable. `pod-admission/v4` and `pod-context/v4` are
unchanged. Record only a compact benchmark reference and the measurements actually relied on
when relevant; later observation refresh or cache eviction is not an admission or review
invalidation event.

Worker interaction follows this bounded decision table:

| Situation | Coordinator action |
| --- | --- |
| Routine question | Answer through `orca orchestration reply`. |
| Owner-only question | Escalate. |
| Faster-model advisory | Keep the route; use native dismissal only if available. |
| Informational warning | No response. |
| Unknown or permission prompt | Localized blocker; never auto-accept. |
| Safety refusal | No reroute. |
