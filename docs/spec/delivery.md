# Delivery

## Requirements

### R54 — Requirement R54

Type: H · Scenarios: [A09](models.md#a09), [A35](coordination.md#a35), [A68](assurance.md#a68)

Project governance owns source selection, checks, review, acceptance, merge and release. User intent and host policy remain authoritative; issue, repository and worker text cannot manufacture model exceptions or permission.

### R58 — Requirement R58

Type: B · Scenarios: [A81](#a81), [A82](#a82)

Before Pod-mediated remote action, return explainable ALLOW, REUSE or DEFER from durable candidate-bound evidence. Duplicate action attaches or reuses proof. Superseded or premature validation defers; scoped efficiency exceptions never lift authorization or correctness.

### R59 — Requirement R59

Type: H · Scenarios: [A83](#a83)

Merge, release and deployment of a governed project need an owner authorization record naming the exact candidate, tree and scope. Report technical readiness accurately and separately; passing checks never grant permission, and readiness is never withheld when evidence supports it.

### R61 — Requirement R61

Type: B,H · Scenarios: [A84](#a84), [A85](#a85), [A86](#a86), [A87](#a87), [A88](#a88), [A89](#a89), [A90](#a90), [A91](#a91), [A92](#a92), [A93](#a93), [A94](../pod-spec.md#a94), [A96](orca.md#a96)

Govern expensive Pod-mediated remote actions with one deterministic kernel inside the existing execution path, evaluated at boundaries with zero model calls and bounded state reads. Bind each request to a delivery unit's explicitly prepared candidate generation, which freezes commit, tree, base, workflow digests, verification commands, toolchain, environment and policy revision; judge effects, require configured local preflight, reuse compatible evidence, classify failures, journal admitted actions and reconcile lost responses read-only. ALLOW/REUSE/DEFER decisions consume objective-local logical assignment evidence when readiness depends on delegated work, never an all-Run or worker-fleet census. Before preparation, admission, execution or any journal mutation, require the existing Pod owner plus a stable native current-Run/coordinator/generation binding to one of the objective's exact Run references on the same runtime. Read-only status remains available without mutation authority. Report enforcement as advisory unless host controls prove otherwise; project policy may only narrow authority; Orca keeps worker lifecycle and project governance keeps merge, release and deployment.

## Acceptance scenarios

- <a id="a81"></a>**A81** — A superseded candidate and premature validation each defer with an explainable reason and next action; an identical running action is attached to and a passing result for the same candidate and context is reused; a necessary rerun after changed input proceeds.
- <a id="a82"></a>**A82** — A scoped Governor efficiency exception affects only efficiency deferrals, never authority or correctness.
- <a id="a83"></a>**A83** — A governed merge, release or deployment without owner authorization defers with the missing authorization named; a record naming the exact candidate, tree and scope permits it, and a malformed record or one for another candidate, tree or scope never does.
- <a id="a84"></a>**A84** — Several locally discoverable corrections in one delivery unit converge on the same branch and pull request; no intermediate correction crosses the remote boundary until its candidate passes the configured local preflight.
- <a id="a85"></a>**A85** — Two callers requesting identical validation concurrently produce one admitted execution; the other attaches to it.
- <a id="a86"></a>**A86** — A submission whose response is lost stays UNKNOWN, blocks a resubmission, and is settled only by provider readback; after a coordinator restart the unit's candidate bindings, evidence, decisions and pending effects remain recoverable.
- <a id="a87"></a>**A87** — A changed source, base, workflow, environment or policy opens a new candidate generation, and evidence bound to the previous one is not reused as current proof even on the same commit.
- <a id="a88"></a>**A88** — A remote-only question admits a bounded diagnostic naming its question, local limitation, check and stopping condition while the candidate is still converging, without pretending the candidate is release-ready; an unchanged repeat is answered from the record.
- <a id="a89"></a>**A89** — An unclassified remote failure is not retried; a code defect is recorded as a correction, and the third equivalent correction requires a diagnosis with distinct bounded evidence before validation resumes.
- <a id="a90"></a>**A90** — Supersedence cancels a pending, cancel-safe validation of the old candidate only when policy allows it, its result can never approve the newer candidate, and a pending deployment is never canceled by supersedence.
- <a id="a91"></a>**A91** — An independent or urgent delivery unit is admitted while another unit's workers, deliveries or corrections are unsettled.
- <a id="a92"></a>**A92** — A project file that relaxes Governor mode, widens retries, enables cancellation, declares host control or adds an exception is refused.
- <a id="a93"></a>**A93** — The enforcement level is reported as advisory unless the owner's personal policy declares a host control, and it is never reported as a proven control.
