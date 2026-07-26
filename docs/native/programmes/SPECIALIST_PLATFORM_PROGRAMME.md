# Programme — Specialist Platform and Marketplace

**Status:** DESIGN. Not implemented. No milestone below has started.
**Maturity of everything described:** `designed`.

---

## 1. Two separate things

**The internal specialist platform** — how Olympus defines, qualifies, versions,
promotes, demotes and retires the specialists it runs. This is a correctness and
quality concern and comes first.

**A public marketplace** — third parties distributing specialists to other
users. This is a distribution and governance concern and comes last.

They are separated because the platform is valuable on its own, and because a
marketplace without qualification would distribute unmeasured specialists.

## 2. What exists

13 first-party specialists with output contracts and tool scoping, plus an agent
registry that can add specialists and structurally cannot shadow a built-in.

Missing: a manifest, declared capabilities, per-specialist qualification,
versioning with promotion/demotion, reputation, and any third-party path.

## 3. Specialist manifest

Identity and version; declared capabilities (task classes it claims); required
tools with their side-effect bands; required model class; output contract;
resource profile; cost profile; evaluation suite reference; and the author with
a trust level.

## 4. Qualification — the same bar as a model

A specialist is qualified **per task cell**, on: output-contract conformance
(a violation is a failure, not a retry), tool-call validity, verified-answer
rate against an independent oracle, cost per task, latency, and refusal
appropriateness — refusing when it should is a *pass*, and this is the criterion
most easily forgotten.

## 5. Lifecycle

```
proposed → qualified (per cell) → promoted (eligible for routing)
        → demoted (evidence regressed) → retired (no longer offered)
```

**Demotion must be automatic on evidence regression** and must take effect
within one request. A specialist that degrades and keeps receiving work is worse
than one that was never promoted.

## 6. Permissions and cost controls

A specialist inherits the invoking principal's permissions and can only narrow
them — never widen. It cannot request a tool outside its manifest. Per-specialist
cost ceilings prevent one expensive specialist from consuming a tenant's budget.

## 7. Reputation

Reputation is **measured, not voted**: qualification history, demotion events,
verified-answer rate, cost efficiency. Ratings are advisory metadata and never
feed routing — routing follows evidence, or the feedback loop the Adaptive
Routing programme guards against reappears here.

## 8. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| M1 | Manifest + declared capabilities for first-party specialists | every shipped specialist has one |
| M2 | Per-specialist qualification + evaluation suites | ≥1 specialist qualified from executed evidence |
| M3 | Versioning, promotion, automatic demotion | a regressed specialist is demoted within one request |
| M4 | Private enterprise specialists | an org publishes to itself; isolation holds |
| M5 | **Public marketplace** | governance, review, revocation, and billing attribution all operational |

**Prerequisites:** Model Qualification (shares the card machinery) and Billing
(attribution) before M5.
