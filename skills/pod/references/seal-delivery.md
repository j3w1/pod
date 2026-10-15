# SEAL delivery planning

Use this reference when the owner gives Pod a bare SEAL link, or asks to plan, decompose or continue a SEAL's delivery. Tell the owner the issue is a SEAL and that planning is read-only.

Planning creates no Pod objective, Orca Run, Orca worker or Pod record. Host-permitted read-only investigation is allowed, as in any plan-only request. Creating or editing an issue, a comment or the SEAL needs the owner's authorization in this conversation. The GitHub SEAL issue remains the only record of requirements, linked deliveries and acceptance.

## 1. Reconcile existing delivery

Read the SEAL, its amendments and its Delivery outline. Read the PESs, pull requests and delivery records that the SEAL links or that cite it, in every repository involved. Read a related SEAL only for the requirements it shares with this one, and never crawl further links transitively.

Classify each applicable acceptance criterion with its evidence:

- **Verified:** evidence establishes the criterion as worded, in the required environment.
- **Partial:** some evidence exists; name what is missing.
- **Uncovered:** no delivery or evidence yet.
- **Blocked:** a named external dependency prevents progress, such as an unsupported native capability.
- **Withdrawn:** only an owner-recorded disposition is a withdrawal.

A merged pull request or a closed child issue alone does not verify a parent criterion. Reuse established evidence instead of proving it again. When the SEAL changed, reopen only the evidence tied to the changed requirements.

If no applicable criterion remains open, report the reconciliation and stop, with no question round and no decomposition.

## 2. Ask before decomposing

Before proposing any breakdown, ask one batched round of questions, each with a recommended option first. Explain what each answer changes. Always confirm the outcome to deliver now and the criteria it includes. Otherwise ask only what the sources cannot settle:

- duplicated or overlapping requirements, including work another SEAL or PES owns;
- requirements whose cost looks disproportionate to their value, or that existing behavior already meets;
- ambiguity or conflict in the outcome, an acceptance criterion or a delivery boundary.

Never ask about implementation choices; they belong to the implementer. Ask one follow-up round only when an answer raises a new decision. Pod never withdraws, merges or weakens a requirement itself; propose a SEAL amendment for the owner instead.

## 3. Decompose into the smallest sufficient set

Propose the smallest set of coherent, independently verifiable deliveries. Test every proposed boundary:

1. Can it produce a meaningful result that is verified on its own? If not, combine it with the work it depends on.
2. Does it own distinct contracts, state and files? If two proposals would change the same responsibility, merge them or reassign ownership.
3. Can it be delivered or safely staged without breaking dependent projects? If not, define the compatibility contract, or deliver the coupled changes together.
4. Does splitting reduce expected total work, risk or delay? If not, prefer fewer PESs.

Prefer separate PESs when outcomes, ownership, compatibility boundaries, risks or delivery endpoints are meaningfully independent. This applies to single-project and cross-project SEALs alike. Cross-project work with a stable interface usually splits, with each part keeping its own review, delivery and rollback boundary.

Keep work in one PES, even across repositories, when separating it would create artificial intermediate objectives, duplicate approvals or more integration risk. Each repository still keeps its own pull request, gates and permissions. Order work by real dependencies, not phase numbers, and run deliveries in parallel only with a demonstrated benefit. Create no PES for a lifecycle stage or an in-scope bug fix, and none only to tick a criterion that existing evidence already establishes.

Present a coverage map: every open applicable criterion has exactly one owning delivery, which may cite reused evidence. A criterion that needs several deliveries names its combined proof and stays open until that proof exists. A criterion that reused evidence fully establishes needs no delivery.

## 4. Prepare approved PESs

After the owner approves the plan, draft only the PESs that are ready, not a speculative backlog. Read the PES template to draft them. Each PES names its target, delivery endpoint, the parent requirement and criterion identifiers it implements, the applicable Limits, its dependencies and its own Proof of Done.

Publish only drafts the owner approved. With authorization, update the SEAL's Delivery outline links, reading the SEAL template for that revision.

## 5. Hand over published PESs

Report each published PES's `/pod` invocation and dependencies, then stop. Start one ready PES in this conversation only on explicit request, as an ordinary objective. Invoking Pod with the SEAL again starts with reconciliation.

A child's passing proof supports parent criteria but never closes them. The SEAL closes only when its applicable criteria are verified or the owner explicitly disposes of them.
