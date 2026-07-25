# Moat Analysis — Can the Labs Copy This in Six Months?

**Applied to:** `docs/ROADMAP.md` engines E1–E6.
**Test:** if a competent team at OpenAI / Anthropic / Google / an OSS collective
could reproduce an engine in ~6 months, **it is not a moat.**

**Verdict up front: by that test, every engine in ROADMAP.md fails.** Not one of
E1–E6 is a moat as designed. This document says why, then redesigns around the
only advantages that survive contact with an adversary who has 1000× the
engineers.

---

## 1. The scorecard (honest)

| Engine | OpenAI | Anthropic | Google | OSS team | Verdict |
|---|---|---|---|---|---|
| **E1** Capability-effect typing | 4 wks | 4 wks — *already ships tool permissioning in Claude Code* | 4 wks | 6 wks — an effect lint is a weekend project | **NOT A MOAT.** 1990s PL tech (Gifford–Lucassen) |
| **E2** Protocol verification (TLA+) | 8 wks | 8 wks | 8 wks — *has Ivy/formal groups* | 12 wks | **NOT A MOAT.** AWS *invented* the industrial practice; MSFT ships P/P#. And a spec of *Olympus's* approval spine is worthless to them |
| **E3** Durable execution | 6 wks | 6 wks | 6 wks | **Already exists** — Temporal is OSS; LangGraph ships checkpointing | **ALREADY COMMODITIZED** |
| **E4** Statistical audit | 6 wks | 6 wks | 6 wks | 8 wks | **NOT A MOAT** as a mechanism — standard audit statistics |
| **E5** Knowledge consistency | 12 wks | 12 wks | 6 wks — *operates the world's largest Knowledge Graph* | 16 wks | **NOT A MOAT.** Google has decades of KR lead |
| **E6** Adaptive loop | 2 wks — *industrial RLHF is their core competency* | 2 wks | 2 wks | 8 wks | **NOT A MOAT.** Their home turf, and Olympus is at ~10⁻⁶ of their data scale |

**Every engine is a static feature.** Each is equally good on day 1 and day 1000,
so any team with more engineers closes the gap in one quarter. Building them is
necessary — they are the **cost of entry** — but calling them a moat was wrong.

---

## 2. Why the original framing was doomed

The question "what technology can we build that they can't?" has no good answer
for a small team facing frontier labs. **Any pure-technology advantage is ≤6
months by construction**, because technology is exactly what an org with 1000×
the engineering can buy. The roadmap was optimizing the one variable Olympus
cannot win on.

Durable advantage does not come from *what you have built*. It comes from:

1. **Accumulation** — an asset whose value is an *integral over time*, so a
   competitor starting today is N years behind **by arithmetic**, not by skill.
2. **Counter-positioning** — something the incumbent **will not** copy because
   copying damages their core business. Not "can't" — *won't*.
3. **Structural data locality** — data the incumbent **cannot legally or
   architecturally see**.

Only these three are available to Olympus. Everything in the roadmap must be
re-oriented so its *primary output* is one of them.

---

## 3. Olympus's three genuinely defensible assets

### Asset 1 — The Calibration Record (accumulation; the strongest)

A multi-year, per-domain, per-model time series of **measured, human-verified**
reliability: violation rates, approval/decline outcomes, corrections, regression
history — each entry signed and causally anchored in the ledger.

**Why it cannot be copied in six months, by anyone, ever:** *you cannot backfill
time.* Three years of measured behavior on a real workload is not purchasable,
scrapeable, or engineerable. A lab starting today has a zero-length series.
Every day Olympus runs, the gap widens by a day — the definition of compounding.

**Seeds that already exist:** `ledger` (signed, hash-chained steps), `attest`
(signed attestations + receipts), `outcomes` (what worked, what the user changed,
what they declined), `liveeval` (sampled scoring of recent runs), `trust`
(earned per-domain autonomy), `quality_baseline.json` + its `_provenance` history.

### Asset 2 — Cross-Provider Comparative Evidence (counter-positioning)

An accumulating, blind, per-customer record of **which model actually serves this
workload best** — measured on the customer's real tasks, across Claude, GPT,
Gemini, Bedrock, and local models, under one identical governance harness.

**Why the labs won't build it — ever:** publishing "GPT-5 beats Claude on your
workload" is against Anthropic's interest; the mirror is against OpenAI's and
Google's. A lab's governance layer will always be *optimized for lock-in to its
own model*, because provider-neutral governance **commoditizes the model** —
their core asset. This is textbook counter-positioning: they have the engineers,
and they will still decline.

**Seeds that already exist and are honest:** `compare.py` — *blind* multi-model
comparison where "picks accumulate into a per-user tally, so over time you learn
which model actually serves *you* best"; `providers.py` (multi-provider CATALOG +
`fetch_models`/`fetch_pricing`); `backend.py` `_fallback_chain` across providers;
`bedrock_converse`, `openai_compat`, `claude_code`; `modelpin`.

### Asset 3 — Customer-Side Execution Data (structural locality)

Labs see **API calls**. They do not see — and increasingly, contractually *must
not* see — which answers the user edited, which they declined, what was approved
and by whom, what the business outcome was, or the private context that made the
decision correct.

Olympus runs **in the customer's environment**. That data is structurally
inaccessible to a lab operating behind an inference endpoint under zero-retention
enterprise terms. This is not an engineering advantage; it is a *topology*
advantage, and topology is much harder to change than code.

**Seeds:** `outcomes`, `usermem`, `emem`, `facts`, `relgraph`, the local store.

---

## 4. Redesigned engines — from features to accumulators

The rule applied to every engine: **the deliverable is no longer a capability; it
is the asset the capability accumulates.** Same code, reoriented output.

| Engine | Was (static feature) | Now (compounding asset) | Moat class |
|---|---|---|---|
| **E1** | An effect lint | **Portable signed effect attestations** covering third-party/MCP tools — a corpus that grows with the ecosystem and travels across deployments | Weak. Accept as **table stakes**; its real job is to make Assets 1–2 *machine-comparable* |
| **E2** | TLA+ specs | *(unchanged — and honestly demoted)* | **None. Hygiene only.** Keep bounded per its stop rule |
| **E3** | Resumable runs | **The signed execution corpus** — the substrate Asset 1 is computed from | Table stakes as a feature; **essential as a data generator** |
| **E4** | Audit sampling | **THE CALIBRATION RECORD (Asset 1)** — statistically bounded, human-verified reliability per domain per model, accumulating for years | **Strong.** Cannot be backfilled |
| **E5** | Contradiction detection | **Customer-private provenance graph** — facts + justifications + contradiction history specific to this deployment | Moderate. Defensible only because it is *private* (Asset 3), never because it out-engineers Google |
| **E6** | Exemplar/routing tuning | **Cross-provider comparative evidence (Asset 2)** — the accumulating blind record of which model serves this customer best | **Strong.** Counter-positioned; labs won't |

### The reorientation that matters most: E4 + E6 merge into one loop

Separately they are features. Together they are a **compounding flywheel** no lab
will replicate:

```
E3 signed execution corpus
      │
      ▼
E4 stratified human-verified audit ──> Calibration Record (per domain × per model × over time)
      │                                          │
      │                                          ▼
      │                              E6 blind cross-provider comparison
      │                                          │
      ▼                                          ▼
trust.py raises autonomy only where          model selection routes to whatever
evidence bounds the violation rate           actually performs on THIS workload
      │                                          │
      └──────────────> more governed runs <──────┘
                        (corpus grows; both bounds tighten)
```

Each cycle: more measured evidence → tighter bounds → more justified autonomy →
more runs → more evidence. **The output is a per-customer, provider-neutral,
time-integrated reliability dataset.** A lab cannot build it (Asset 3 locality),
would not publish it (Asset 2 counter-positioning), and cannot backfill it
(Asset 1 time). That is the only structure here that satisfies all three tests.

---

## 5. What this means for the roadmap

**Reprioritization** (the sequencing in ROADMAP.md §2 optimized for buildability;
this optimizes for moat accrual):

1. **Start the clock immediately on Assets 1–2.** The compounding assets are
   integrals — their value depends on *start date*, so the highest-return action
   available today is to begin recording calibration and comparative data, even
   crudely. A rough calibration record started now beats a rigorous one started in
   18 months. **Move E4's data capture ahead of its analysis machinery**, and turn
   on `compare.py` + `liveeval` accumulation now.
2. **Build E1/E3 as hygiene, on schedule, without moat claims.** They are the cost
   of entry and the data substrate. Do not over-invest.
3. **Demote E2 hard.** Formal methods is the area where the labs are *strongest*
   and the accumulation value is *zero*. Keep the bounded M1 experiment; expect to
   stop.
4. **Reframe E5 as customer-private, not competitive.** Never pitch it against
   Google's Knowledge Graph. Its defensibility is locality, not capability.
5. **Never again claim a technology moat.** Claim the accumulation.

**Honest residual risk.** Counter-positioning erodes if a *neutral* third party
(a cloud vendor, a compliance platform, an OSS consortium) builds provider-neutral
governance — they have no model to protect, so they *would* copy Asset 2. That is
the real competitive threat, not the labs. The defense is Asset 1's head start:
be years into the calibration record before a neutral competitor starts theirs.
