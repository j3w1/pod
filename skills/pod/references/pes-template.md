# PES authoring

A Pod Execution Spec is a bounded executable contract, standalone or implementing selected SEAL requirements. It defines results, proof and delivery, not necessarily an implementation procedure. The implementer chooses the approach within fixed requirements and authority; genuinely mandatory protocol or safety ordering still applies.

GitHub title: `PES: [Bounded change or deliverable]`

Copy only the fenced body. Replace placeholders; remove unused optional fields, sections and template comments. Keep the format declaration first and the title outside the body. Use ordinary Markdown; equivalent clear headings are valid. Identifiers are document-local; qualify cross-issue references.

```markdown
**Format:** Pod Execution Spec v1<br>
**Target:** `owner/repository`<br>
**Delivery:** [Verified local result, draft PR, reviewed PR, merge, or other explicit endpoint.]<br>
**Requirements source:** [Optional SEAL issue and applicable requirement identifiers; omit for standalone work.]

> **Outcome:** [The bounded result this objective will deliver.]

## Objective
[What this execution must accomplish and where its responsibility ends.]

## Context
[Relevant current baseline, evidence links, and dependencies.
For a child PES, identify the parent requirements it implements and relevant limits.
Do not copy the entire SEAL, EEL, or conversation.]

## Requirements
### R1 — [Descriptive requirement name]
[Required behavior and preservation conditions for this bounded objective.
Reference the parent requirement when applicable; add only necessary local detail.]

[Repeat with stable identifiers as needed.]

<!-- Optional: omit if the boundary is already unambiguous. -->
## Non-goals
[Excluded changes and adjacent work this objective does not own.]

<!-- Optional: omit if there are no special decisions to explain. -->
## Design decisions
**Fixed:** [Genuinely mandatory decisions and their source.]
**Left to the implementer:** [Research, design, sequencing, and technical choices
within the requirements and existing authority.]

[Any proposed approach is advisory unless explicitly identified as a fixed decision.
A detailed implementation plan is not required by this format.]

## Proof of Done
- [ ] **PoD#1 — [Observable result].** [Evidence-backed completion condition; identify the local and parent requirements covered where applicable.]

[Repeat as needed. Use result conditions, not a list of editing steps.]

## Validation
[Applicable checks and required review or live environments. Distinguish local,
mocked, native, hosted, and installed evidence where relevant. Name genuine external
dependencies and who supplies their results. The agent determines detailed test
procedures unless an existing rule or requirement makes them mandatory.]

## Completion
[The final report and evidence needed for the stated delivery endpoint, including
settlement of this objective's assignments and reconciliation of external results.
Include installation, release, or cleanup only when in scope and authorized.
Record genuine remaining waits explicitly; do not report them as completion.
Distinguish unperformed or withdrawn checks from passing evidence.]
```
