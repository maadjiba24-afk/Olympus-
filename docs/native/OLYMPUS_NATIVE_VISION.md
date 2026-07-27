# Olympus Native Vision

**Status:** FOUNDATIONAL. Defines what Olympus is, independently of any prior
programme. Colibri is not referenced because it is not relevant to what Olympus
is for.

---

## 1. What Olympus is

> **Olympus is an evidence-driven execution platform for consequential AI work:
> it plans, coordinates a council of specialists, uses tools, preserves verified
> state, and performs approved real-world actions — while producing a durable,
> replayable record of why it did each thing.**

The proposed definition in the transition brief was *"analyse, decide,
coordinate specialists, use tools, preserve state, learn from measured outcomes,
and perform approved actions safely across users, teams, and infrastructure."*
That is accurate but under-specifies the differentiator. Three amendments, each
grounded in what the repository actually enforces:

1. **"Consequential"** replaces the neutral framing. Olympus's investment is in
   approval spines, side-effect classification, sealed journals, spend caps and
   principal isolation. That machinery is overhead for a chatbot and essential
   for an agent that can send an email, move money, or change a repository.
   The product is defined by what it will not do without permission.

2. **"Verified state"** replaces "state". Olympus does not merely persist — it
   seals a hash-linked journal, refuses to repair corruption, and can prove a
   history was not altered. Persistence is common; *provable* persistence is not.

3. **"A durable, replayable record of why"** replaces "learn from measured
   outcomes". Learning is a consequence; the record is the asset. A run can be
   replayed, its decisions diffed, and its instrumentation proven not to have
   changed the outcome. That record is what makes the learning trustworthy
   rather than merely confident.

## 2. Who Olympus serves

| Audience | What they need | What Olympus gives them today |
|---|---|---|
| **The individual operator** | an assistant that can act, without acting recklessly | 13 specialists, 130 tools, 132 CLI commands, an approval spine with 4 risk classes and 5 autonomy levels |
| **The engineering team** | an agent whose decisions can be reviewed after the fact | decision log, replay fixtures, signed attestations, `olympus verify --run <id>` |
| **The platform builder** | an API surface their existing clients already speak | two API dialects through one generation path, verified with real vendor SDKs |
| **The regulated organisation** | provable isolation, retention and deletion | per-principal isolation, verified deletion, legal hold, backup with verified restore |
| **The self-hoster** | to run it on their own infrastructure with their own keys | Docker, Compose, BYOK, sovereign mode, local-only data classes |

The audience Olympus does **not** serve today is the multi-tenant SaaS
customer — there is no organisation model, no RBAC across tenants, and no
per-tenant billing. That is the largest single gap in the platform and it drives
the roadmap.

## 3. The problems it solves

1. **Consequential actions need a boundary, not a disclaimer.** Most agent
   frameworks ask the model to be careful. Olympus classifies every action, and
   an unclassified one is denied.
2. **A confident answer is not a verified one.** Olympus runs verification as a
   distinct stage with its own model floor that routing cannot cross.
3. **You cannot improve what you cannot attribute.** Every routing decision,
   tool call, repair and refusal is recorded against the run that caused it.
4. **Autonomy without a budget is a liability.** Spend is capped, estimated
   before the fact, and refused rather than silently downgraded.
5. **Deleting a user's data means deleting all of it.** Snapshot, journal,
   derived memory, heat ledgers, documents — verified from the filesystem.

## 4. Why it exists

Frontier model quality is converging and is not defensible. What compounds is
**measured operational evidence about how models behave on your work**: which
model is actually better for which task class, in which language, at which
context length, at what cost, with what tool-call validity. Olympus is built to
accumulate that evidence as a first-class asset and to act on it only when it
crosses a stated floor.

## 5. Core principles

- **Evidence before activation.** A capability may be fully built and refuse to
  turn on. Four are in that state today.
- **Refusal over silent degradation.** Under load or over budget, Olympus
  refuses. It never quietly substitutes a cheaper model.
- **Default deny.** Tools, plugins, side effects and cross-principal access are
  denied unless explicitly permitted.
- **Reject, never repair.** A corrupt durable artifact is quarantined; reads
  stop at the verified boundary. Exactly one mutation is permitted anywhere: an
  uncommitted torn final line.
- **Sanitise and continue** — but only for ephemeral provider payloads.
- **Negative results are artifacts.** A refuted claim is registered with its
  measurement, not deleted.
- **Instrumentation must be provably non-interfering**, per commit, by gate.

## 6. Safety model

Four independent layers. A failure in one does not open the others.

1. **Classification** — every action carries a risk class and a side-effect band.
2. **Approval** — irreversible and financial actions can never auto-run,
   regardless of autonomy level.
3. **Boundary** — one enforcement point covers both API dialects, plugins and
   MCP handlers; default-deny.
4. **Budget** — pre-flight worst-case estimation plus a hard daily cap.

## 7. Approval philosophy

Autonomy is **earned per domain and revocable**, not granted globally. Five
levels from suggest-only to standing pre-approval; irreversible and financial
actions sit outside the ladder entirely. The agent never raises its own
autonomy, and every operator switch is human-only.

## 8. Evidence philosophy

Evidence carries **provenance** or it is not evidence. Six closed values, from
`synthetic` to `real-user`; an unknown one is a hard error, never a default.
Synthetic and operational evidence are never aggregated into one unlabelled
number. A floor stated in samples means *real* samples: 200 synthetic runs
satisfy `n >= 200` numerically and prove nothing.

## 9. Developer experience

Today: a CLI with 132 commands, two API dialects that existing OpenAI and
Anthropic clients already speak, a plugin and MCP surface, and `olympus doctor`
for configuration skew. The gap is the SDK layer — no official client library
exists, so every integrator writes HTTP by hand or repurposes a vendor SDK.

## 10. Enterprise direction

The honest statement of maturity: Olympus has **principals**, not **tenants**.
Per-API-key isolation is verified under concurrency and across restore, but
there is no organisation, no workspace, no team, no role, no service account,
no SSO and no MFA. Enterprise readiness is a build-out, not a polish.

## 11. Platform direction

From a single-process council to a platform with a control plane, a scheduler,
worker placement, a tenant model, a usage ledger that reconciles against
provider cost, and an extension ecosystem whose permissions are enforced before
any marketplace exists.

## 12. Long-term differentiation

Not model quality, not orchestration cleverness — both are copied in a quarter.
The durable asset is the **calibration record**: an accumulating, per-tenant,
per-task-class body of measured evidence about model and specialist behaviour,
with the governance to act on it safely and the replay machinery to explain
every action taken because of it. That is an integral over time. Its value
depends on start date, which is why evidence collection is sequenced ahead of
the machinery that consumes it.
