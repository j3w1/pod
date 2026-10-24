# Planning

Map criteria, delivery, dependencies and required audits with provenance and checks. Bind trusted audit rules at intake as cited `project_policy` assurances, never optional model constraints, with scope, question, candidate, existing proof and insufficiency. Record each delivery unit's audit with the assurance's `unit` field. Trivial direct work needs no map. Keep one coordinator-held active obligation; unfinished work needs action, an external block or controlling wait, reconsidered when resolved.

Before isolated implementation, reuse the objective worktree and its branch or explicitly request `--branch orca/<task-slug>`; bind repository/common-dir, branch and path; display labels prove nothing. Preserve unrelated work. Concurrent writers use per-assignment `placement` worktrees and merge ancestry; settle shared contracts before overlapping edits.

Fix governance paths at intake: canonical project policy, `.pod/config.yaml`, tracked root `AGENTS.md`/`CLAUDE.md`, subject to direct-user exclusions. Adopting proposals requires direct user revision or cited policy. Initial target snapshots are trusted; Git cannot prove authorship. Candidate-containing governance updates need exact-snapshot user authority. Reconcile base changes before admission/effects, retaining ids/history/cited text; withdraw only genuinely gone policy. Unavailable sources hold work.

Optimize time to a verified result. At transitions dispatch ready distinct work that shortens that path; waiting is valid, spare capacity creates no duty, lean is no one-worker rule.

Packets name outcome, done-when (verification to perform/fix), served obligations, boundary/actions, candidate, route, digested sources and report format. Investigations name decision and stop condition. Include no secrets or whole issue/conversation; future outputs belong in scope, not sources. Preserve discarded results/branches; disposition satisfies nothing.

Corrections use direct/delegation judgment and current pool/pin. Retain findings; briefs identify failed rule, alternate entrypoints, supported records and status/wait consumers. Diagnose repeats with example, hypothesis and discriminating check. For recurring delta-review defects, state one predicate and cover sibling states in one table test.

A `receipt_limit` or retained-history `finding_limit` refusal names the exact pending admission to settle first. Settle its native work and consume its report; if a route hold prevents consumption, use the applicable route-settlement step below before retrying. If a worker failed without a verdict, record a truthful failed or blocked observation through the ordinary report path; it supplies no passing proof. Only when no report is pending does the refusal give the next checkpoint's `renew` request. Read the map's `next_seq`, replace the fresh-id placeholder, and use the supplied state fields with that `seq`. An active coordinator replacement is valid only when the coordinator slot is free; when another row holds it, use `state: "waiting", wait: {class: "sequenced", referent: HOLDER}`. A blocked replacement can supply its matching `external`. Omit state fields only when OLD is non-terminal and its existing state can continue. This is an identical mechanical renewal for every kind and provenance, with no owner question or revision. Supply no check, source, boundary or other definition fields: changing those uses the existing revision authority. OLD stays frozen as renewed history, with policy citations still following governance adoption; NEW carries the definition and accepted criterion mapping, has no proof, and needs fresh evidence. Physical parents and frozen admission bindings remain recorded; effective links and waits follow the new tip. New unrelated children name that tip.

Settle and consume every serving review or investigation before renewal, except for the terminal route discard below; dispositions apply only to implementation results. An implementation needs its ordinary disposition before renewal, even after a matching report settles its route. Assurance admission reserves one receipt and 32 findings per pending serving attempt, including the new one, within each 64-record bound. An admission that cannot reserve these slots names the pending report to consume, the held admission's applicable settlement step, or the exact renewal request when none is pending. Native settlement alone does not release a reservation, and ordinary checkpoint receipts cannot use those slots. Consumption or a validated terminal route discard releases it. For a large map, use small `update` patches and bounded triage groups within the discovered input and row limits; terminal rows may be omitted and remain in history. A count-legal request can still exceed the byte limit.

## Settle a route hold

An admission's current unsettled route mismatch holds the Governor and objective acceptance. A settlement preserves the original flag and evidence; a later mismatch starts another occurrence and holds again. A flag from an earlier record format still needs a fresh settlement. Missing effective model or effort remains an independent hold. The project lead chooses the applicable step; the coordinator records it on the open objective:

- **Matching report:** consume through `pod internal report` after a matching worker readback, or replay the identical already consumed report when the normal route and replay checks permit it. Pod records `matching_report` automatically. A consumed report alone does not settle a later mismatch; failed work stays failed.
- **Redone implementation:** record `route_settlements: [{"admission": "<held-admission-id>", "basis": "redone", "replacement": "<replacement-admission-id>"}]`. The original needs an ordinary disposition. The replacement must be a later implementation with a validated consumed `succeeded` report, an `integrated_into` disposition, no route hold or unknown effective route, and effective agent/model/effort matching the original admitted request. It must serve exactly the same obligations at their current renewal tips, with unchanged definitions and the same editing boundary. `retry_of` or `reuse_of` alone proves none of this. Changed route or scope needs a lead decision.
- **Lead decision:** record `route_settlements: [{"admission": "<held-admission-id>", "basis": "lead_decision", "decision": "accept", "source": {"reference": "<lead-decision-reference>"}, "reason": "<lead's reason>"}]`. `decision` is `accept` or `discard`; `source.reference` is nonempty and at most 512 characters, and `reason` nonempty and at most 1024. For an implementation, `accept` needs ordinary disposition `integrated_into`, and `discard` needs `discarded`, recorded earlier or in the same write. Acceptance/integration is available only after its report is consumed, except that a result without a Git base integrates by attestation. An unreported implementation with a Git base can instead be discarded or redone. For a review or investigation with an already consumed report, only `accept` is valid: it accepts the later route evidence while the consumed report and its outcome stand unchanged, including after a runtime rebind that prevents identical replay.

For an unconsumed review or investigation, the lead may instead use `lead_decision` with `decision: "discard"` after exact native settlement. Add the required `corrections: ["<correction-id>"]` to that entry, naming an ordinary post-write correction row for every known required finding from the refused attempt. Use `[]` only with a reason explaining why no required findings are known. This discard is terminal: every later report of that admission refuses `route_settled_discarded`; further proof needs a fresh admission. It releases the attempt's receipt/finding reservations and renewal hold atomically, while the corrections and incomplete review remain.

The coordinator relays the project lead's delegated decision; Pod does not authenticate the lead. This supplies no `user_direct` authority, passing review, receipts, REUSE or provider authority. Settlement never creates an unreported outcome or supplies passing proof.

Put at most 16 settlement entries in one checkpoint value or report-accompanying map, never an admission map. Use the next `seq` from `internal map`; dispositions, corrections and settlement may share that write. For example, after native settlement of an unconsumed route-held review serving assurance `A`, when no required findings are known, this checkpoint-value fragment both discards the attempt and renews the assurance:

```json
{
  "seq": 12,
  "renew": [{"id": "A", "as": "A2", "state": "waiting", "wait": {"class": "sequenced", "referent": "O1"}}],
  "route_settlements": [{
    "admission": "<held-review-admission-id>",
    "basis": "lead_decision",
    "decision": "discard",
    "source": {"reference": "<lead-decision-reference>"},
    "reason": "No required findings are known from the refused attempt; obtain a fresh review.",
    "corrections": []
  }]
}
```

Here `12` stands for the actual next sequence, `A2` must be fresh, and `O1` is the live coordinator obligation. Preserve the checkpoint's other required fields. The replacement assurance has no proof and needs fresh review. Every map and settlement check must pass before publication; a refused write, including a byte-limit refusal, settles nothing.

## Current decision instructions

Packet freeze returns a bounded `decision_context` and `task_instructions` from the current machine policy, remembered tool permissions and decision generation. Re-freezing a returned packet ignores its supplied summary and derives current context again; it never treats a copied summary as authority. Include that private context in the actual native Task instructions and deliver current instructions to the exact current Dispatch through supported native mail. It tells a worker to check current authority at every sensitive use; it is no operation approval. A source packet, worker summary or stale generation never widens authority. Keep original rule words and decision evidence out of public summaries.
