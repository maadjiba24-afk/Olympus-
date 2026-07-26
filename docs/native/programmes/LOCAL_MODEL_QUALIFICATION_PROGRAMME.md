# Programme — Local Model Qualification

**Status:** DESIGN. Not implemented. No milestone below has started.
**Maturity of everything described:** `designed`.

---

## 1. The central discipline

**Loading a model is not qualifying it.** A model that starts, responds, and
produces plausible text may still fail structured output, emit invalid tool
calls, ignore cancellation, or degrade past a context threshold. Qualification
is a measurement against a floor, per task cell — the same bar hosted providers
must clear.

## 2. Why local matters

Privacy (data never leaves the host), data residency (a hard requirement in some
jurisdictions), cost (no per-token charge), and availability (no provider
outage). None of these justifies routing work to a model that cannot do it —
hence the qualification requirement.

## 3. Scope

Supported runtimes (llama.cpp, Ollama, vLLM — chosen on evidence, not
popularity); model formats and quantisation; hardware detection (CPU features,
GPU presence, VRAM, system RAM); resource profiles; benchmark suites;
**tool-use qualification**; **structured-output qualification**; cost model
(amortised hardware + power, not "free"); latency including cold start; thermal
and sustained-load constraints; promotion, demotion and retirement; fallback to
hosted providers.

## 4. Qualification criteria (a card is issued only on all of these)

| Dimension | Floor |
|---|---|
| Request success | ≥ 99% over n ≥ MIN_N |
| Structured output | ≥ the hosted floor for that cell — a schema violation is a failure, not a retry |
| Tool-call validity | valid name + schema-conforming arguments, first attempt, at the hosted rate |
| Cancellation | honoured within a bounded window |
| Quality | Wilson lower bound ≥ the cell floor, graded by an independent oracle |
| Context | measured degradation curve, with the usable band stated |
| Sustained load | no thermal or memory degradation over a stated duration |

**A card is per (model, quantisation, runtime, hardware profile, cell).** The
same weights at a different quantisation on different hardware is a different
model for qualification purposes — this is the mistake most local-model claims
make.

## 5. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| M1 | Runtime abstraction + hardware detection | a runtime can be added without touching the council |
| M2 | Benchmark suites with independent oracles | no self-confirming test |
| M3 | **Execute the campaign** | ≥1 card issued from executed evidence |
| M4 | Promotion, demotion, retirement, fallback | a demoted model stops receiving work within one request; fallback is transparent and recorded |

## 6. Risks

Hardware heterogeneity makes a card non-portable — hence the profile in the key.
Thermal throttling makes a short benchmark optimistic — hence sustained load.
Local models fail *differently* (silent truncation, format drift), so the
existing streamguard and structured-output contracts apply unchanged.

## 7. Cost and operational impact

Local inference trades variable token cost for fixed hardware cost; the model
must be honest about amortisation. Operationally it adds a new failure domain
(the local runtime) that needs its own health signal and a fallback path.
