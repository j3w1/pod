# SEAL authoring

Scope, Expectations, Acceptance & Limits is a requirements specification for a capability, renovation, or one or more systems. It may have zero or more PES deliveries; decomposition is optional. It is not a parent execution coordinator. Describe required outcomes and limits. Mark suggested phases as advisory, not mandatory implementation steps.

GitHub title: `SEAL: [Required capability or renovation]`

Copy only the fenced body. Replace placeholders; remove unused optional fields, sections and template comments. Keep the format declaration first and the title outside the body. Use ordinary Markdown; equivalent clear headings are valid. Identifiers are document-local; qualify cross-issue references.

```markdown
**Format:** SEAL v1<br>
**Scope:** [System, repository, or repositories covered.]

> **Outcome:** [What must be true when the requirements are satisfied.]

## Context
[Problem or opportunity, relevant baseline, and supporting evidence links.
Distinguish established facts from assumptions and proposed explanations.]

## Scope
**Included:** [Capabilities, behaviors, or areas covered.]
**Excluded:** [What this specification does not require.]

## Requirements
### R1 — [Descriptive requirement name]
[Required behavior or quality, including relevant conditions and failure cases.
Specify what must be true, not a particular implementation.]

[Repeat with stable identifiers as needed.]

## Limits
[Actual security, authority, compatibility, resource, and preservation constraints.
Identify fixed decisions and their source. Reference existing rules rather than
copying them. Leave other technical choices to the implementer.]

## Acceptance
- [ ] **AC1 — [Observable result] (R1).** [What demonstrates satisfaction, including any required operating conditions and evidence.]

[Repeat as needed. Every requirement must have acceptance coverage; one criterion
may cover several requirements.]

<!-- Optional: omit this section when decomposition is unnecessary. -->
## Delivery outline
[Proposed phases or work areas. Distinguish suggestions from mandatory dependencies.
Link PES issues when their scope is established; do not invent issues or procedures.]

| Requirements | PES or planned work area | Dependency, if any |
|---|---|---|
| R1 | [Issue reference or proposed area] | [Required dependency, or none] |

<!-- Optional: omit when there are no unresolved decisions. -->
## Open questions
[Unresolved requirements or constraints, the information needed, and affected scope.
Do not treat ordinary implementer choices as owner decisions.]

## Completion
[Who consolidates acceptance evidence and where it is recorded. The SEAL is complete
when its applicable acceptance criteria are supported by evidence. Child issue
closure or PR merges alone do not establish this. Record authorized scope changes
explicitly; never describe a withdrawn requirement as verified.]
```
