# Programme — Adaptive Routing

**Status:** DESIGN. Not implemented. No milestone below has started.
**Maturity of everything described:** `designed`.

---

## 1. The premise this programme refuses to assume

**More complex routing is not always better.** The current heuristic is cheap,
predictable and debuggable. A learned router that wins on average while losing
badly on a minority of cells is worse for the users in those cells, and the
average will hide it.

So the first milestone uses **existing qualification and telemetry evidence
only** — no new model, no new signal, no new training loop. If evidence-driven
substitution cannot beat the heuristic using data Olympus already collects, that
is the finding, and it is worth more than a bigger model.

## 2. What already exists

`routesub` (substitution inside measured bands, warmth ledger, counterfactual
recording) and `modelgrade` (Wilson-bounded per-cell qualification) are built
and tested. Both are **off**, with 0 decisions and 0 cards, because no campaign
has run. This programme *activates* existing machinery before extending it.

## 3. Scope

Task classification (`task_class|language|context_band|tools|structured` —
already the cell key); provider, model and specialist eligibility; quality,
latency and cost prediction; confidence and abstention; fallback; exploration
vs. exploitation; **shadow routing** (compute the counterfactual, do not act);
counterfactual evaluation; versioned routing policies; rollback; anti-feedback-
loop protection; customer overrides.

## 4. Anti-feedback-loop protections

The failure mode that makes adaptive routing dangerous: the router prefers model
A, so A gets all the traffic, so only A accumulates evidence, so A stays
preferred regardless of merit.

1. **Bounded exploration** — a floor rate of counterfactual routing per cell,
   even for a dominant model.
2. **Evidence freshness** — a card expires; a model with only stale evidence
   loses its qualification rather than coasting.
3. **Outcome independence** — quality is graded by verification, which routing
   structurally cannot reach (the verifier floor is already enforced).
4. **No self-grading** — a model never grades its own output.

## 5. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| M1 | **Shadow routing only** | counterfactual decisions recorded for ≥N cells; **no live pick changes** |
| M2 | Activate substitution on cells that pass | per-cell A/B beats the heuristic on quality at equal or lower cost, with a stated CI |
| M3 | Latency and cost prediction | prediction error bounded and measured on held-out data |
| M4 | Confidence and abstention | abstention beats a wrong confident answer on a measured task class |

## 6. Evidence floors

Activation per cell requires: qualification cards for every candidate (n ≥ MIN_N,
Wilson lower bound ≥ floor, fresh), **and** an A/B on that cell showing no
quality regression at the stated confidence. **Per cell — never globally.** A
global win that regresses one language is a regression.

## 7. Customer overrides

An operator pin always wins. Adaptive routing is a default, not a mandate, and
the override must be visible in the decision log.

## 8. Security · Privacy · Cost · Operational · Rollback

**Security:** routing must never cross the verification floor — already
structurally enforced and tested. **Privacy:** routing evidence is
content-minimised (cells and counters, never text).
**Cost:** the point is to reduce it; every decision records estimated vs. actual.
**Operational:** a routing policy is a versioned artifact with an owner.
**Rollback:** policy version pin, then flag-off to the heuristic. Both must be
one operator action.
