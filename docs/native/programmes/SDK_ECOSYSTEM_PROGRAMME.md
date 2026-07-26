# Programme — SDK Ecosystem

**Status:** DESIGN. Not implemented. No milestone below has started.
**Maturity of everything described:** `designed`.

---

## 1. Recommended order, with reasoning

**Do not launch five SDKs simultaneously.** Each is a permanent compatibility
commitment. Order by actual target users:

| # | Language | Why this position |
|---|---|---|
| 1 | **Python** | Olympus is written in Python; its users self-host it, script it, and extend it in Python. The first SDK should serve the people already here. |
| 2 | **TypeScript** | the largest integration surface — web apps, edge functions, Node services. This is where *new* users arrive. |
| 3 | **Go** | infrastructure and platform teams embedding Olympus in services; also the natural CLI language for a future control-plane client. |
| 4 | **Java** | enterprise integration; follows enterprise tenancy being real, not before. |
| 5 | **Rust** | smallest current demand; revisit on evidence of pull rather than shipping it speculatively. |

**Prerequisite for all five:** a stable auth model. Baking today's credential
scheme into five languages before Identity lands would guarantee a breaking
change in every one.

## 2. Generated vs. handwritten

**Generated** from an OpenAPI/typespec description: request and response models,
endpoint stubs, pagination. **Handwritten**: streaming, tool-call ergonomics,
retries, cancellation, error mapping. The generated layer keeps the surface
honest; the handwritten layer is where an SDK is actually good or bad.

## 3. Contract requirements

Semantic versioning with a documented deprecation window; auth via a single
credential abstraction; **streaming as a first-class iterator** with backpressure
and clean cancellation; tool calls typed; retries with jitter that never retry a
non-idempotent operation; a typed error model distinguishing client, auth,
quota, provider and internal errors; cursor pagination; webhook signature
verification; a test double so users can test without a live instance;
runnable examples; automated release with signed, provenance-attested packages.

## 4. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| M1 | API description + contract tests | the description is generated from the server, not maintained beside it |
| M2 | Python SDK | a real integration built with it; streaming and cancellation verified against a live instance |
| M3 | TypeScript SDK | same bar, plus browser and edge runtimes |
| M4 | Go SDK | same bar |
| M5 | Java, then Rust on demand | evidence of pull |

## 5. Compatibility promise

The `/v1` dialects already work with real vendor SDKs — verified 25/25 over real
HTTP. That compatibility is a **feature to preserve**, not a substitute for a
first-party SDK: it gives integrators a zero-effort path, while the native SDK
exposes what the vendor dialects cannot (approvals, evidence, replay, tenancy).
