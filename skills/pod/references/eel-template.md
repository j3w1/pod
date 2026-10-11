# EEL authoring

An Evidence Evaluation Ledger evaluates observations, competing explanations and justified next actions. It is evidence, not implementation authority. Reproduction procedures are appropriate; proposed fixes do not become executable requirements. A supported no-change conclusion is valid.

GitHub title: `EEL: [Question or behavior being evaluated]`

Copy only the fenced body. Replace placeholders; remove unused optional fields, sections and template comments. Keep the format declaration first and the title outside the body. Use ordinary Markdown; equivalent clear headings are valid. Identifiers are document-local; qualify cross-issue references.

```markdown
**Format:** Evidence Evaluation Ledger v1<br>
**Scope:** [Question, system, incident, or behavior being evaluated.]

> **Question:** [The bounded question this ledger helps answer.]

**Use:** Evidence and evaluation only; not implementation authority.

## Context and boundaries
[Why the evaluation is needed, the relevant baseline, and what is outside scope.
Identify related investigations instead of duplicating their ownership.]

## Sources
[For each material source, record its locator, relevant commit/version or run,
observation date, environment, and coverage limitations as applicable.
Link original artifacts rather than pasting complete logs. State missing evidence.]

## Findings
### F1 — [Descriptive finding name]
**Observation:** [What was actually seen; separate expected and observed behavior.]
**Evidence:** [Exact supporting source, artifact, or reproducible check.]
**Assessment:** [What the evidence supports, how firmly, and likely ownership when known.]
**Impact:** [Actual or plausible consequence; distinguish measured impact from inference.]
**Uncertainty and counter-evidence:** [What limits, contradicts, or could change the assessment.]
**Next check:** [Optional bounded investigation or reproduction needed; omit if settled.]

[Repeat as needed. A suspected cause is not a confirmed defect.]

## Evidence threshold
[What additional evidence, if any, would justify a requirements change or separate
PES. Identify what would instead support no change, further observation, or an
existing owner. Investigation procedures may be included; implementation procedures
are proposals only and confer no authority.]

## Disposition
[Current conclusion: no change justified, further evidence needed, existing work
owns the question, or a SEAL/PES should be proposed. Link actual follow-up issues.
State unresolved uncertainty and the stopping condition for this evaluation.
Closing the EEL settles its evaluation scope, not implementation of a fix.]
```
