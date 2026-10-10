# PES authoring

A Pod Execution Spec (PES) is a readable implementation contract; an Execution Spec issue is its recommended persistent home. Ordinary Markdown and equivalent clear headings are valid, not a parser or DSL. Omit empty optional sections. Publishing puts the title in GitHub's title field.

```markdown
# <Title>
> **Outcome:** <Result.>
**Target:** owner/repository<br>
**Format:** Pod Execution Spec v1<br>
**Delivery:** <Endpoint.>
## Objective
## Context
## Requirements
## Non-goals
## Design decisions
## Proof of Done
- [ ] **PoD#1 — <Outcome>.** <Observable condition.>
## Validation
## Completion
```

Number every Proof of Done item; name verification, delivery, cleanup and authority boundaries.
