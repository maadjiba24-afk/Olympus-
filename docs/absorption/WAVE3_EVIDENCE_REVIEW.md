# Wave 3 — Evidence Review (Phase 3 gate)

**Rule being applied:** *"Wave 3 capabilities are not automatically approved.
Each requires evidence from Wave 1 and Wave 2. Each capability must begin with an
evidence review. If its required floor is not met, record the negative result and
defer it."*

**Date:** 2026-07-26 · **Tree:** `claude/colibri-deep-analysis-gpit35` @ `762bf99`
**Method:** floors were **measured by running the code**, not asserted. Every
number below is reproducible with the command shown.

---

## 1. Result

| # | Candidate | Floor | Measured | Verdict |
|---|---|---|---|---|
| 1 | Lossless council speculation | qualified draft **and** verify model per cell + per-cell A/B showing acceptance pays for the second call | `modelgrade` cards = **0**; flag off; no acceptance data anywhere | **NO-GO — deferred** |
| 2 | Coupling-driven local pre-work | recall@2 ≥ 0.6, Wilson CI half-width ≤ 0.1, **n ≥ 200** | `n_runs=0`, `status=insufficient_data`, `floors.pass=False`, `qualifying=[]` | **NO-GO — deferred** |
| 3 | Local inference qualification | `modelgrade` cards qualifying the local member per cell (n ≥ MIN_N, Wilson ≥ floor, fresh) | 0 cards; no campaign run; no local runtime present | **NO-GO — deferred** |
| 4 | Provider mirror routing | a measured provider-unavailability rate worth mitigating + deterministic selection | `routesub.agreement_stats` → `decisions=0`; no unavailability rate exists | **NO-GO — deferred** |
| 5 | Anthropic-compatible API handler | **not** operational-evidence-gated — it is a compatibility surface; gate is "tested against representative real clients" | no `/v1/messages` route exists; the `anthropic` SDK **is** a required dependency and can type-check the wire shapes | **CONDITIONAL GO** |

**4 NO-GO · 1 CONDITIONAL GO.**

### Reproduction

```
python -c "from olympus import coupling; print(coupling.predictability_report(days=30))"
#   {'n_runs': 0, 'status': 'insufficient_data',
#    'floors': {'pass': False, 'qualifying': [],
#               'rule': 'recall@2 >= 0.6 with Wilson-CI half-width <= 0.1 on n >= 200'}}
python -c "from olympus import modelgrade; print(len(modelgrade.cards()), modelgrade.enabled())"
#   0 False
python -c "from olympus import routesub; print(routesub.agreement_stats(days=30)['decisions'])"
#   0
```

Evidence-store inventory at review time: `traces/`, `modelgrade/`, `routesub/`,
`ctxheat/`, `streamguard/`, `watchdog/`, `usage/` — **all absent; 0 recorded
runs.**

---

## 2. The structural finding (this is the important part)

**Four of five Wave-3 gates require operational data that only Phase 5 (staging
+ shadow traffic) produces.** This is not a defect and not a gap in Wave 1/2 —
those waves built the *measurement substrate* and the *policy layer* correctly;
what does not yet exist is **accumulated evidence**, because the system has never
served a request in this tree.

The declared programme order is:

> Phase 3 (Wave 3) → Phase 4 (validation) → Phase 5 (staging/shadow) → Phase 6 (canary)

but candidates 1–4 have a hard dependency on Phase 5:

```
shadow traffic ──► traces + usage ──► modelgrade cards ──► speculation (1), local tier (3)
               └─► trajectories ────► predictability     ──► prefetch (2)
               └─► provider errors ─► unavailability rate ──► mirror (4)
```

**Recommendation (not a unilateral reorder):** treat Wave 3 candidates 1–4 as
*Phase 5-gated* — re-run this review after shadow traffic has accumulated, using
`next_review = 2026-10-24` now recorded in `experiments.json`. Attempting them
today would mean either inventing evidence or shipping adaptive behaviour on
zero data, which is precisely what this programme's gates exist to prevent.

This ordering dependency is surfaced, not silently resolved. If you want them
sooner, the honest path is to run shadow traffic first (Phase 5), not to lower
the floors.

---

## 3. Per-candidate detail

### 1. Lossless council speculation — NO-GO
The floor needs two things that do not exist: a `modelgrade` card qualifying a
**draft** model and one qualifying a **verify** model *for the same cell*, and a
per-cell A/B proving acceptance pays for the second call. With zero cards, the
substitution/qualification input is empty; with zero runs, there is no
acceptance-rate history to A/B against. Colibri's own 6×5090 experiment is the
cautionary precedent: speculation there was *structurally* loss-making at full
residency despite a 69–79 %-acceptance head, and only per-cell measurement
revealed it. Deferred with that precedent recorded.

### 2. Coupling-driven local pre-work — NO-GO
The floor is explicit and was measured directly: `n=0` against a required
`n ≥ 200`. `coupling.predictability_report()` correctly abstains
(`insufficient_data`) rather than reporting a flattering number on a tiny
sample — the abstention is the module working as designed. **Network
speculation remains prohibited regardless of any future measurement.**

### 3. Local inference qualification — NO-GO
Two independent blockers: no qualification campaign has run (0 cards), and no
local runtime is present in this environment to qualify. The doctrine that
matters is already written down and unchanged: local execution is **not**
automatically safer or cheaper, and must clear the same task-cell floors as a
cloud model.

### 4. Provider mirror routing — NO-GO
No measured provider-unavailability rate exists, so the premise ("worth
mitigating") is unevidenced. Worth noting what today's failover actually is:
**key rotation *within* a provider** on 429/402, which is not cross-provider
mirroring and does not produce the cross-provider health data the floor needs.

### 5. Anthropic-compatible API handler — CONDITIONAL GO
This is the one candidate whose gate is **not** operational-evidence-dependent:
it is a protocol-compatibility surface, and its stated gate is *"do not claim
compatibility until tested against representative real clients."* That is
testable now, with one honest bound:

- **Can be verified here:** the wire contract against the **real `anthropic`
  SDK types** (`anthropic>=0.92.0` is a *required* dependency, so its
  `types.Message`, content blocks, and streaming event models are importable and
  can validate what the handler emits) plus the documented protocol shapes.
- **Cannot be verified here:** an actual third-party client (Claude Code, the
  SDK in another process) driving the endpoint over a socket.

**Therefore:** implement, and report compatibility as **SDK-type-verified, not
real-client-verified** — no compatibility claim beyond what was tested. It must
go **inside** the existing server (`web.py`'s `/v1` surface, alongside
`_handle_v1_chat`), never as a parallel server, and must inherit cancellation,
admission control, usage accounting, tool-call safety, streaming defense,
replay support and consistent error semantics.

---

## 4. Actions taken

1. All four NO-GO entries in `olympus/experiments.json` updated with the
   **measured** evidence string, `last_tested = 2026-07-26`,
   `next_review = 2026-10-24`, and `outcome = "deferred: evidence floor not met
   (Phase-3 review)"`. Registry check: clean.
2. Negative results are preserved, not deleted — per the programme rule that
   failed capabilities are quarantined rather than forced.
3. Candidate 5 proceeds to `WAVE3_IMPLEMENTATION_SPEC.md`.

**No floor was lowered to admit a candidate.**
