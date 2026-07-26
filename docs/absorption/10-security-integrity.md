# Absorption 10 — Security & Integrity

**Colibri domain:** the untrusted-model-mirror threat model — byte-count-exact format
validation (#413), reject-never-repair, header caps, overflow checks (§3.1, §4.1, §4.3);
tokenizer/config hardening (§4.2, §12); supply-chain revision pinning and its honest
size-only-verification gap (§5.5, §21, §27); the SHA256SUMS + antivirus-false-positive
incident response (#527/#530/#532, §21, §22, §26.1); fail-closed non-loopback binds (SEC-6,
§9.2); DNS-rebinding Host-header guards (SEC-7, §9.2); auth-gated telemetry and the ungated
`/profile` inconsistency (SEC-8, §9.2); constant-time API-key compares (§9.2);
DLL-hijack-safe backend loading (§10.1); rANS integrity seals and verify-before-write
(§5.4).
**Olympus target:** platform integrity — `olympus/security.py`, `olympus/cmdguard.py`,
`olympus/egress.py`, `olympus/vault.py` + `olympus/secretref.py`, `olympus/witness.py` +
`olympus/attest.py`, `olympus/skillpack.py` + `olympus/pluginstore.py`, `olympus/store.py`,
`olympus/backup.py`, `olympus/web.py` and the channel gateways, plus `docs/THREAT_MODEL.md`,
`docs/SUPPLY_CHAIN.md`, `docs/SIGNING.md`, `docs/SECURITY_RESIDUALS.md`,
`docs/SOVEREIGNTY.md`.

## Domain thesis

Colibri's entire security posture flows from one asymmetry stated in §3.1: **"fail-soft for
accelerators, fail-hard for data."** A slow path is an inconvenience; a corrupted or hostile
*artifact* (a mirrored safetensors shard, a crafted tokenizer.json) silently becomes wrong
weights, wrong tokens, or an out-of-bounds write — so artifacts are validated byte-count-
exactly and **rejected, never repaired**, while everything performance-shaped degrades
silently to correct-but-slower. Olympus faces the same asymmetry in a different currency:
its "weights" are the durable things that shape every future run — skills, plugins, prompts,
memory, config, the vault, the signed ledgers — and its "accelerators" are ephemeral context
(a web page, a transcript) that is consumed once and discarded. Olympus already practices
both halves *piecemeal* (`skillpack.scan_reason` refuses, `security.sanitize_for_prompt`
repairs, `pluginstore` pins, `witness` signs) but has never stated the boundary as doctrine,
so the two disciplines are applied by module-local habit rather than by rule. This domain
absorbs Colibri's line and draws it precisely: **anything that persists is validated
reject-never-repair at a single chokepoint; anything ephemeral is sanitized-and-continued**
— then extends Colibri where Olympus is already ahead (signatures, not just checksums; a
signed decision log, not just a changelog) and closes the gaps Colibri exposes (unauthenticated
telemetry drift, bind-time fail-open, unsealed persistent stores). The accumulated asset is
the **integrity ledger**: every refusal, seal verification, and incident regression test is a
signed, dated record — evidence a hosted competitor cannot backfill (`docs/MOAT_ANALYSIS.md`
Asset 1).

## Summary table

| Capability | Colibri mechanism | Verdict | Olympus home module(s) |
|---|---|---|---|
| Untrusted-artifact validation: byte-count-exact formats, reject-never-repair, header caps, overflow checks, mirror memcmp | `qt_resolve_fmt` exact byte-count match or `exit(1)` (#413); 512 MB header cap; `numel×esize==nbytes`; int64 overflow rejection; mirror size+header memcmp (§4.1, §4.3) | **redesign** | **new `olympus/ingestgate.py`**, `skillpack.py`, `pluginstore.py`, `connectors.py`, memory import paths |
| Tokenizer/config hardening (single-chokepoint range checks, bounded slurps, union-of-sources stop sets) | `CKR` macro choke; 256 MB config slurp cap; 1 GiB tokenizer cap; negative-id / max-id rejection; EOS = union of both configs (#298) (§4.2, §12) | **absorb-principle** | `ingestgate.py`, `config.py`, `providers.py`, `connectors.py` manifest parsing |
| Supply-chain pinning, SHA256SUMS, SBOM doctrine | revision-pinned downloaders (but size-only verification, §5.5/§27); `SHA256SUMS.txt` (#530); unpinned Docker (§21) | **absorb-principle** (Olympus already ahead; extend to every fetched artifact) | `pluginstore.py`, `skillpack.py`, `witness.py`, `docs/SUPPLY_CHAIN.md`, `selfupdate.py` |
| Incident-response-as-code (#527 AV false positive) | fix memorialized in comments/CHANGELOG; per-PR CI artifact uploads so reports are verifiable (#527/#532, §22, §26.1) | **absorb-principle** | **new `docs/INCIDENTS.md`** + `docs/runbooks/`, `tests/` incident-regression convention, CI |
| SEC-6 fail-closed non-loopback binds + constant-time key compares | server refuses to bind non-loopback without an API key; `hmac`-style compare for Bearer/x-api-key (§9.2) | **redesign** | **new `olympus/authkit.py`** (shared with absorption 06), `web.py`, `a2a_server.py`, `mcp_server.py`, `adminpanel.py`, `dashboard.py` |
| SEC-7 DNS-rebinding Host-header guard (inbound) | Host header validated against expected origins; CORS allowlist (§9.2) | **redesign** | `authkit.py`, `web.py`; complements the existing *outbound* `security.resolve_pinned_ip` |
| SEC-8 auth-gated telemetry (and the ungated `/profile` lesson) | `/health` always-200 liveness, rich fields only when authed; `/experts` authed; `/profile` ungated — a documented drift (§9.2) | **redesign** | `authkit.py`, `web.py`, `dashboard.py`, `otel.py`, **new `scripts/check_route_auth.py`** (CI) |
| DLL-hijack-safe loading (all-or-nothing symbol resolution, no-CWD search path, hard off-switches) | `backend_loader.c` resolves ~48 symbols all-or-nothing; search paths never include CWD; `COLI_CUDA=0` is a hard off (§10.1) | **absorb-principle** | `pluginstore.py`, `connectors.py`, `cli.py` (sys.path hygiene) |
| rANS integrity seals + verify-before-write | decoder final-states must equal `RANS_L` (~2⁻⁴⁶ silent corruption); exact length/frequency checks; mandatory in-memory round-trip before writing; model-fingerprint headers (`COLIKV1`) (§4.5, §5.4, §8.2) | **new-subsystem** | **new `olympus/seals.py`**, `store.py`, `backup.py`, `skills.py`, `memory` stores, `witness.py` |
| *(beyond Colibri)* model-endpoint fingerprinting & drift detection | — (Colibri fingerprints local KV to a model; Olympus's "model" is a remote endpoint that can be silently swapped) | **new-subsystem** | `providers.py`, `modelpin.py`, `evals.py` golden probes, `calibration.py` |

---

## 1. Untrusted-artifact validation — byte-count-exact, reject-never-repair

**1. What Colibri does.** Model containers may come from untrusted mirrors, so every layer of
the loading pipeline validates against crafted-file attacks: a 512 MB safetensors header cap,
per-tensor dtype/offset/shape validation, int64 shape-product overflow rejection,
`numel×esize == nbytes` cross-checks that block an OOB-write primitive, and `st_read_f32_cap`
refusing to write past caller-sized buffers (§4.1). The keystone is `qt_resolve_fmt` (#413):
a quantized tensor's format is inferred **from byte counts, never metadata**, and the counts
must match a known layout *exactly* or the process exits — the old fallthrough silently
mis-tagged short tensors and read out of bounds. Dual-SSD mirrors are admitted only after a
size check plus a **full header memcmp** against the primary (§4.1). Doctrine: corrupt or
hostile containers cause immediate `exit(1)` with honest errors — "reject, never repair"
(§3.1).

**2. Why it exists.** The model is the largest attacker-controllable input the engine will
ever parse, it arrives from community mirrors, and a single mis-parsed offset silently
corrupts *every subsequent token*. Repair is impossible to do safely because the validator
cannot know which interpretation the publisher intended.

**3. How it works internally.** All validation happens at load time, before any weight byte
influences computation; format resolution is a pure function of observable sizes; every check
failure is fatal with a message naming the offending tensor.

**4. Strengths.** The validation surface is *complete by construction* — there is no metadata
field an attacker can lie in, because metadata is never consulted for layout. Fatal-on-failure
means no partially-validated state ever exists. The mirror memcmp makes a poisoned second
copy structurally unable to serve reads.

**5. Weaknesses & trade-offs.** `exit(1)` is the right severity for a single-model inference
engine but would be catastrophic for a long-running council serving many users — one hostile
skill import must not take down the Telegram gateway. Colibri's checks are also bespoke
per-format (safetensors, `.qs`, CFSE, KV files each carry their own validator); nothing forces
a *new* artifact type to be validated at all. And the doctrine is enforced by culture, not by
a chokepoint: a contributor adding a new loader could silently skip it.

**6. Security implications.** For Olympus the equivalent attack class is real and broader:
skillpacks are durable *instructions* (a poisoned SKILL.md steers every future run that loads
it), plugins are durable *code*, memory imports are durable *beliefs*, MCP server descriptors
and connector manifests are durable *capability grants*. Today `skillpack.scan_reason`
refuses-not-sanitizes (correct) and `pluginstore.install` refuses unpinned code (correct),
but memory/backup restore paths and connector manifests have no equivalent single gate, and
nothing structurally prevents a new import path from shipping ungated —
`security.should_wrap` solved exactly this drift problem for *prompt* content by failing
closed; imports need the same inversion.

**7. Scalability implications.** A single chokepoint scales with artifact *types* (one
validator registration per type), not with call sites; per-artifact validation cost is
trivial next to a model call.

**8. Performance implications.** Validation is at ingest time only — zero cost on the hot
answer path, matching Colibri's load-time-only placement and the DISK-CLASS "byte-identical
when off" ethic (§18).

**9. Maintainability implications.** One module to audit instead of N import paths; the
fail-closed default means a forgotten registration produces a loud refusal in testing, not a
silent bypass in production — the same property `TRUSTED_TOOLS` already gives the envelope.

**10. How Olympus should redesign it.** **Adopt the doctrine, invert the enforcement.**
State the boundary explicitly: *durable ingest is reject-never-repair; ephemeral context is
sanitize-and-continue.* Build one chokepoint, `olympus/ingestgate.py`, through which every
artifact that will *persist or execute* must pass before it is written: skill imports
(`skillpack.import_file`, `_import_text`, `_import_tarball`), plugin installs
(`pluginstore.install`), connector/MCP manifests (`connectors`), memory/lesson imports,
backup restores (`backup.py`), and site profiles. The gate is a registry of typed validators
(byte caps, schema shape, injection/credential scan via the existing `security` regexes,
declared-vs-actual size cross-checks — the `numel×esize` analogue is "frontmatter-declared
kind must match parsed structure exactly"). Unregistered artifact kinds are **refused**, the
`should_wrap` inversion applied to ingestion. Refusal is per-artifact (typed `IngestRefused`
with an honest reason), never process-fatal — the redesign of Colibri's `exit(1)` for a
multi-tenant daemon. Every refusal is recorded as a signed `ingest` decision in the trace
(`trace.py`), feeding the integrity ledger.

**11. Final Olympus architecture.**
- **New `olympus/ingestgate.py`**: `validate(kind, payload, meta) -> Validated | raise
  IngestRefused(reason, rule_id)`; `KINDS` registry (`skill`, `plugin`, `connector_manifest`,
  `memory_import`, `backup_archive`, `site_profile`, `mcp_descriptor`); per-kind byte caps
  (`_MAX_*` constants following `skillpack._MAX_ARCHIVE`); every path that persists an
  external artifact calls it — enforced by a `behavioral_contracts.yaml` entry
  (`ingest.gated`) like the existing `skill.import` contract.
- **Data model**: refusals appended to the signed trace as
  `{"decision":"ingest","kind":...,"rule":...,"verdict":"refused"}` — same shape as
  `egress._record`.
- **Env/CLI**: `OLYMPUS_INGEST_STRICT` (default `enforce`; `audit` logs-but-admits for
  migration; unknown value → `enforce`, the `cmdguard.mode()` typo-fail-closed pattern).
  `olympus ingest verify <path>` dry-runs the gate.
- **Integration**: Aletheia's lesson-write path and Metis's daily distillation call the gate
  for the `memory_import` kind; the security gate (`cmdguard`) stays the *execution* twin of
  this *ingestion* gate; heartbeat's plugin/skill loaders consult `pluginstore.verified_names`
  as today.

**12. Why the Olympus approach is superior.** Colibri validates three artifact types with
three bespoke validators and a cultural rule; Olympus validates an open-ended set with one
fail-closed registry, per-artifact (not per-process) refusal, and a *signed record of every
refusal* — turning the doctrine into an accumulating, auditable asset instead of a comment.

---

## 2. Tokenizer & config hardening — single-chokepoint bounded parsing

**1. What Colibri does.** `config.json`/`generation_config.json` are slurped with a 256 MB
cap and every dimension range-checked at a single choke macro (`CKR`) (§4.2); tokenizer.json
gets a 1 GiB cap, rejection of negative ids (an OOB-write primitive) and of ids above
`1<<21` (a calloc overflow) (§12); EOS is the **union** of both config files plus every
`special:true` token, because missing one stop id printed control tokens into chat (#298).

**2. Why it exists.** Config files ride along with untrusted model mirrors; a crafted
dimension or token id is an allocation-size or index attack, and a *missing* value (the
third stop token) is a correctness attack that looks like a model bug.

**3. How it works internally.** Bounded read → parse with the in-repo `json.h` → every
numeric field passes through one macro that names the field on failure; stop-set assembly
merges all sources.

**4. Strengths.** The single choke means adding a config field without a range check is
visible in review; caps precede parsing so a zip-bomb-shaped file dies before allocation;
union-of-sources encodes the lesson that *omission* is also an attack/bug surface.

**5. Weaknesses & trade-offs.** The checks live in C macros — the discipline doesn't
transfer to Colibri's own Python tooling (the downloaders parse HF JSON with no such choke).
Range checks are hand-maintained against one architecture family.

**6. Security implications.** Olympus's config surfaces are broader and *more* dynamic:
`OLYMPUS_MODELS` JSON (with SecretRefs), provider catalogs fetched at runtime
(`providers.fetch_models`/`fetch_pricing` — remote JSON!), connector manifests, plugin
metadata, webhook payloads, `behavioral_contracts.yaml`. A crafted `fetch_pricing` response
or a connector manifest with an absurd field is today handled by ad-hoc `try/except` per
module — repair-by-default, exactly what #413 warns against for durable inputs.

**7. Scalability implications.** Bounded parses cap memory per request regardless of how
many channels/gateways multiply the parse sites.

**8. Performance implications.** Negligible; all cold paths.

**9. Maintainability implications.** A shared `bounded_json(blob, cap, schema)` helper in
`ingestgate.py` gives every parser the same caps and the same honest errors; new config
surfaces inherit hardening by calling one function instead of reinventing it.

**10. How Olympus should redesign it.** Absorb the *principle* (bounds before parse; range
checks at one choke; treat omission as a failure mode) rather than any mechanism: route
remote-origin JSON (`fetch_models`, `fetch_pricing`, connector manifests, MCP descriptors)
through `ingestgate.bounded_json` with per-source caps and a minimal shape check; refuse —
never default-fill — malformed *durable* config (a manifest), while tolerating malformed
*advisory* data (a pricing hint) with a logged downgrade. The union-EOS lesson maps to stop
conditions and safety lists: e.g. `security.ACTION_TOOLS`/`INGESTION_TOOLS`/`TRUSTED_TOOLS`
completeness is already CI-bound (`test_m0_envelope_failclosed.py`) — extend the same
union/completeness test pattern to route-auth classes (rubric 6) and ingest kinds (rubric 1).

**11. Final Olympus architecture.** `ingestgate.bounded_json(data, *, cap, kind)` +
per-kind shape validators; `providers.py` and `connectors.py` adopt it; caps as module
constants, overridable only downward. No new env vars — hardening that needs a flag to be on
isn't hardening.

**12. Why the Olympus approach is superior.** Colibri hardened the C engine but not its
Python periphery (§27's "hardcoded personal paths", size-only downloaders); Olympus applies
one bounded-parse discipline across *all* of its periphery, with the durable/advisory
distinction made explicit instead of implied.

---

## 3. Supply-chain doctrine — pinning, checksums, signatures, SBOM

**1. What Colibri does.** Downloaders pin HF/ModelScope **revisions** "for supply-chain
integrity" with size-verified resume (§5.5); releases ship `SHA256SUMS.txt` (#530); the
converter writes a parameter manifest that refuses resume-with-different-flags (#355);
honest gaps are documented: verification is size-only (no hashes) on downloads, Docker
clones the repo unpinned (§21, §27).

**2. Why it exists.** A 370 GB artifact assembled over days from community mirrors, plus a
binary release that antivirus engines already flag (#527), demand provenance the user can
check — and Colibri's single maintainer needed resumable, tamper-evident pipelines more than
elegance.

**3. How it works internally.** Revision ids pinned in download scripts; per-segment
checkpoint sidecars; release workflow computes SHA256SUMS and behaviorally verifies the
unpacked archive before publishing (§21).

**4. Strengths.** Pinning + resume checkpoints mean "NO byte is lost however the connection
dies"; the release-time behavioral verification (run `coli info` in a clean dir) caught a
real shipping failure; gaps are *stated*, not hidden.

**5. Weaknesses & trade-offs.** Checksums without signatures authenticate nothing — anyone
who can replace the artifact can replace `SHA256SUMS.txt` beside it. Size-only download
verification detects truncation, not substitution. Docker unpinned undoes the pinning story
for the container path.

**6. Security implications.** Olympus is already *ahead* on the core: `requirements.lock`
with `--require-hashes`, a pre-release ban, a CycloneDX SBOM (`docs/SUPPLY_CHAIN.md`), and —
beyond anything Colibri has — an **Ed25519-signed release manifest** with pinned-key
verification and enforced production key custody (`witness.py`, `docs/SIGNING.md`,
`SECURITY_RESIDUALS.md` §2). The absorbable gap is *coverage*: Colibri pins the thing it
fetches at runtime (the model); Olympus fetches skills, plugins, and (soon) MCP servers at
runtime, and only plugins are hash-pinned today. Remote skill imports
(`skillpack._fetch_bytes`) are SSRF-gated, size-capped, scanned, and forced provisional —
but not pinnable.

**7. Scalability implications.** Pinning is O(artifacts); the manifest pattern
(`pluginstore .manifest.json`) generalizes without new infrastructure.

**8. Performance implications.** None at answer time; hash checks at install only.

**9. Maintainability implications.** One doctrine sentence keeps future surfaces honest:
*nothing fetched from outside becomes durable without a recorded content hash, and nothing
is trusted as a release without a signature from a pinned key.*

**10. How Olympus should redesign it.** (a) Extend hash-pinning to skill imports: optional
`sha256=` on `import_url`, recorded in a skills manifest mirroring `pluginstore`'s, with
`skillpack.verify()` re-hashing on-disk skills; unpinned remote imports stay allowed but
remain provisional *and* are recorded with their observed hash so later tamper is
detectable (tamper-evidence even without pre-pinning — Colibri's mirror-memcmp idea applied
temporally). (b) MCP/connector installs go through `pluginstore`'s existing pin flow — no
second installer. (c) `selfupdate.py` verifies the witness-signed manifest before applying
an upgrade (`olympus upgrade` must never install what `olympus verify` would reject).
(d) Adopt Colibri's release-time *behavioral verification* into the publish workflow: unpack
the wheel in a clean venv, run `olympus capabilities` + `olympus verify`, fail the release on
drift — the #306/#478 stale-binary lesson in Python form.

**11. Final Olympus architecture.** `pluginstore.py` (unchanged trust model, reused for MCP),
`skillpack.py` + skills manifest, `witness.py`/`selfupdate.py` wiring, CI: publish workflow
gains the clean-room behavioral check. Env: existing `OLYMPUS_PLUGIN_ALLOW`,
`OLYMPUS_PLUGIN_ENFORCE`, `OLYMPUS_PINNED_PUBKEY`; new `OLYMPUS_SKILL_ENFORCE` mirroring the
plugin flag. Docs: `docs/SUPPLY_CHAIN.md` gains the "nothing durable without a hash; nothing
release without a signature" doctrine paragraph.

**12. Why the Olympus approach is superior.** Colibri's chain ends at checksums; Olympus's
ends at a pinned-key signature over a manifest that also covers its own source, and the
same root of trust signs the decision log — provenance and audit share one key custody story
(`SIGNING.md`), which Colibri has no analogue of.

---

## 4. Incident-response-as-code — the #527 antivirus false positive

**1. What Colibri does.** When Windows Defender's ML heuristics flagged the release binary
(a 107 KB zero-filled `.data` struct looked like a packer payload), the response was: root-
cause to a specific struct, fix it (−22.5% binary size), memorialize the incident in code
comments *and* the changelog, ship `SHA256SUMS.txt` so users can verify what they scanned,
and add per-PR CI artifact uploads so any future AV report is verifiable against an exact
build (#527/#530/#532, §21, §22, §26.1).

**2. Why it exists.** A trust incident against a security-adjacent project is existential
for a single-maintainer repo; the countermeasure is *reproducible evidence*, not assertion.

**3. How it works internally.** No init for `GrDraft` → static initializer removed; CI
uploads `colibri.exe` per PR; release notes auto-extracted; the story lives where the code
lives.

**4. Strengths.** The incident *permanently upgraded the pipeline* — every future report is
verifiable per-PR. The write-up is in-tree, so the knowledge survives maintainer turnover.
This is exactly Colibri's negative-results culture (§3.1) applied to security.

**5. Weaknesses & trade-offs.** Ad-hoc: there is no incident index, no template, no
regression test encoding the fix (the countermeasure is in CI config, which can drift
silently). Institutional memory is comments + changelog grep.

**6. Security implications.** Olympus's honesty culture already half-implements this:
`DEFERRED.md`, `docs/SECURITY_RESIDUALS.md` (an *index of accepted limits* — the residual
counterpart of an incident index), issue-numbered fixes in docstrings. What's missing is the
same for *actual incidents*: a place where "what happened, what we changed, what test now
pins it" is mandatory structure, and the norm that every incident lands a regression test
(Colibri does this for *quality* claims — `test_int3.c` ships the #132 claim as a test —
but not for #527 itself).

**7–8. Scalability / performance implications.** Zero runtime cost; the cost is process
discipline, which is exactly what a small team can afford when it is a template, not a
ceremony.

**9. Maintainability implications.** An incident ledger converts one-off firefights into
compounding institutional memory — searchable by the next maintainer and by Prometheus's
self-audit (which already reads Olympus's own source and docs).

**10. How Olympus should redesign it.** Create `docs/INCIDENTS.md`: an append-only,
numbered ledger (`INC-001 …`) with a fixed schema — *symptom, root cause, fix commit,
regression test, residual*. Binding rule (enforced by a CI check in the spirit of
`scripts/check_threat_model.py`): every `INC-` entry must name a test file that exists, and
every test named `test_inc_*.py` must have a ledger entry — the drift-gated-capability-count
pattern applied to incidents. Runbooks for the two incident classes Olympus can predict
(key compromise — already sketched in `SIGNING.md`; store corruption/tamper detection —
rubric 8's seals) move into `docs/runbooks/`.

**11. Final Olympus architecture.** `docs/INCIDENTS.md`, `docs/runbooks/*.md`,
`scripts/check_incidents.py` (CI), test convention `tests/test_inc_*.py`. Prometheus's
weekly audit ingests the ledger (it already reads docs) so recurring incident classes become
upgrade proposals. No env vars — culture, encoded.

**12. Why the Olympus approach is superior.** Colibri memorializes incidents in prose;
Olympus memorializes them as *executable regression tests indexed by a CI-enforced ledger*,
so the memory cannot rot — and each entry is another dated record in the accumulated-
evidence moat.

---

## 5. Gateway fail-closed defaults — SEC-6 binds, SEC-7 rebinding, constant-time auth

**1. What Colibri does.** The HTTP gateway refuses to bind a non-loopback address without an
API key configured (SEC-6 — fail-closed exposure); validates the Host header against
expected origins to stop DNS-rebinding attacks on the localhost server (SEC-7); compares
API keys constant-time (Bearer + `x-api-key`); CORS-allowlists localhost + Tauri origins;
30 s socket timeout against Slowloris; 4 MiB body / 1 MiB grammar caps (§9.2).

**2. Why it exists.** A localhost LLM server is a classic drive-by target: a malicious web
page can talk to `127.0.0.1` (rebinding defeats same-origin), and users habitually bind
`0.0.0.0` to reach a server from another machine — the fail-open default of every dev
server. Colibri makes exposure an explicit, authenticated act.

**3. How it works internally.** Bind-time check: non-loopback listen address + no
`COLI_API_KEY` → startup refusal; per-request Host validation; `hmac.compare_digest`-class
comparison so timing doesn't leak key prefixes.

**4. Strengths.** All checks are *default-on and structural* — the user cannot forget them,
only explicitly satisfy them. Bind-time refusal fails in milliseconds at startup, the same
philosophy as binding the port before loading 370 GB (§9.2).

**5. Weaknesses & trade-offs.** Single-server scope: the rules live in one Python file; a
second listener (the dashboard, a future endpoint) would re-implement or forget them —
which is precisely how `/profile` shipped ungated (rubric 6). Host-guard allowlists need
care behind reverse proxies.

**6. Security implications.** Olympus runs *many* listeners: `web.py` (chat + `/v1/*`),
`a2a_server.py`, `mcp_server.py`, `adminpanel.py`, `dashboard.py`, webhook gateways. Each
currently decides its own bind/auth posture. Olympus's *outbound* rebinding defense is
excellent (`security.resolve_pinned_ip` pins the validated IP at socket-connect; sub-resource
CDP blocks) — but the *inbound* twin (a hostile page rebinding to Olympus's own local
endpoints) is exactly SEC-7's territory and is not uniformly enforced. An unauthenticated
local web UI is also a privilege escalation path for any malware on the host.

**7. Scalability implications.** A shared kit is O(1) per new listener; per-request cost is
a string compare and a set lookup.

**8. Performance implications.** Negligible; constant-time compare is microseconds.

**9. Maintainability implications.** One module to audit for all inbound surfaces; new
gateways inherit hardening by construction — the `compat.h` "every platform difference lives
HERE" pattern (§11.3) applied to inbound security.

**10. How Olympus should redesign it.** Build **`olympus/authkit.py`** (already proposed by
absorption 06 for route auth — this domain co-owns it; one module, not two): (a)
`guard_bind(host, has_key)` — refuse non-loopback bind without a configured key; every
listener calls it before `listen()`; override only via explicit
`OLYMPUS_ALLOW_UNAUTH_BIND=1` with a loud startup warning (the `--allow-dev` posture from
`witness.py`: dev is allowed, never silent). (b) `check_host(request_host)` against
loopback + `OLYMPUS_ALLOWED_HOSTS`. (c) `check_key(presented, expected)` via
`hmac.compare_digest` — replacing any `==` key comparison in `web.py`/gateways (audit them).
(d) Shared body-size caps and socket timeouts as constants.

**11. Final Olympus architecture.** `olympus/authkit.py`; adopters: `web.py`,
`a2a_server.py`, `mcp_server.py`, `adminpanel.py`, `dashboard.py`, `webhook_gateway.py`.
Env: `OLYMPUS_ALLOWED_HOSTS` (comma-separated; loopback implicit),
`OLYMPUS_ALLOW_UNAUTH_BIND` (human-only switch, listed in `SECURITY_RESIDUALS.md` §4's
table). A CI test asserts every module calling `.bind(`/`serve_forever` imports authkit —
the fail-closed completeness pattern again. Sovereign mode composes: `assert_egress_allowed`
governs what leaves; authkit governs what may connect in.

**12. Why the Olympus approach is superior.** Colibri hardened one server by hand; Olympus
hardens a *family* of servers through one kit plus a CI completeness check, so the SEC-6/7
guarantees hold for listeners that don't exist yet — structural, not artisanal.

---

## 6. SEC-8 — auth-gated telemetry, and the ungated `/profile` lesson

**1. What Colibri does.** `/health` is always-200 *liveness*, but scheduler/kv_slots/tiers/
hwinfo fields appear only when authenticated; `/experts` (the per-expert routing map) is
authed; `/profile` (rolling per-turn profiling) shipped **ungated — a noted inconsistency**
(§9.2).

**2. Why it exists.** Telemetry is an information-disclosure surface: expert routing maps,
hardware inventory, and per-turn timings leak workload and machine details to anything that
can reach the port.

**3. How it works internally.** Field-level gating inside the `/health` handler; endpoint-
level auth on `/experts`; nothing on `/profile` — because gating was applied per-endpoint by
hand, and one endpoint was added later.

**4. Strengths.** The liveness/richness split is exactly right: monitoring probes need
always-200 with zero information; humans with the key get everything.

**5. Weaknesses & trade-offs.** Hand-applied policy drifts — `/profile` is the proof, *inside
the same file* that implements SEC-8. The lesson is not "gate telemetry" but "**per-endpoint
manual gating cannot be trusted; classification must be declared and audited**."

**6. Security implications.** Olympus telemetry is richer and more sensitive than Colibri's:
`dashboard.py` and `adminpanel.py` expose run traces, scores, memory summaries; `otel.py`
exports spans; `/health`-style probes exist across gateways. Some of this is per-user data
(C1/C2 in `egress.DataClass` terms). An unauthenticated dashboard leaks conversation
metadata to the LAN.

**7. Scalability implications.** Declarative route classes cost nothing at runtime and scale
to every future endpoint.

**8. Performance implications.** None measurable.

**9. Maintainability implications.** Olympus already runs the antidote pattern elsewhere:
`docs/THREAT_MODEL.md` is CI-enforced against the live tool surface. Routes deserve the
identical treatment.

**10. How Olympus should redesign it.** Reuse the taxonomy that already exists rather than
inventing one: every HTTP route declares a **data class** (`egress.DataClass`: C0 liveness/
public, C1 operational, C2 sensitive) and authkit enforces *auth required for anything above
C0*. `scripts/check_route_auth.py` (CI) walks the registered routes of every server module
and fails if any route lacks a declared class — an undeclared route is treated as C2
(fail-closed), so the build breaks loudly instead of shipping an ungated `/profile`.
Liveness endpoints return a bare `{"ok": true}` with no fields — the field-level split
adopted wholesale.

**11. Final Olympus architecture.** `authkit.route(cls=DataClass.…)` decorator (or a route
table) in `web.py`/`dashboard.py`/`adminpanel.py`/`a2a_server.py`/`mcp_server.py`;
`scripts/check_route_auth.py` in CI beside `check_threat_model.py`; env:
`OLYMPUS_TELEMETRY_AUTH=off` exists only as a human-only dev switch, logged at startup,
listed in `SECURITY_RESIDUALS.md` §4.

**12. Why the Olympus approach is superior.** Colibri wrote the policy and violated it in
the same file because nothing checked; Olympus makes the classification a machine-checked
declaration sharing one data-class vocabulary with the egress gateway — inbound disclosure
and outbound egress governed by the same C0/C1/C2 ladder, and drift caught in CI, not in a
reverse-engineering report.

---

## 7. DLL-hijack-safe loading — all-or-nothing capability resolution

**1. What Colibri does.** The Windows CUDA backend is a runtime-loaded DLL;
`backend_loader.c` resolves ~48 `coli_cuda_*` symbols **all-or-nothing** (a partial backend
is refused, never half-used), with search paths that **never include CWD** (the classic DLL
planting attack), hard off-switches (`COLI_CUDA=0` / `--gpu none`), and fail-at-startup when
an explicitly requested backend is missing — never a silent CPU fallback presented as GPU
(#121) (§10.1).

**2. Why it exists.** Runtime code loading is the one place a *local* attacker (or a stale
build) can substitute logic; and a half-resolved backend is worse than none — it fails at an
unpredictable call site.

**3. How it works internally.** Explicit `LoadLibrary` with a controlled search path;
resolve every symbol before flipping the "backend present" flag; per-tensor `cuda_failed`
latching thereafter for *runtime* faults (fail-soft), distinct from *load-time* integrity
(fail-hard).

**4. Strengths.** The load-time/run-time severity split mirrors the data/accelerator
doctrine perfectly; the no-CWD rule closes an attack users cannot see; #121's honesty rule
("an explicit request that can't be honored fails loudly") prevented fake benchmarks.

**5. Weaknesses & trade-offs.** C-specific mechanics; the doctrine, not the loader,
transfers.

**6. Security implications.** Python's equivalent hazards are real for Olympus: (a)
**sys.path CWD injection** — running `olympus` in a directory containing a malicious
`telegram.py`/`requests.py` shadows a lazily-imported extra; (b) **partial plugin
registration** — a plugin that registers 3 of its 5 tools leaves the loadout in an
undefined capability state, and `security.filter_tools`/`should_wrap` classifications may
not cover the half-registered names; (c) **silent capability downgrade** — a missing
optional backend (Postgres store, browser CDP, `cryptography`) silently degrading changes
the security posture (vault already fails safe here; others should match).

**7. Scalability implications.** All-or-nothing registration is O(plugin) at load; no
runtime cost.

**8. Performance implications.** None.

**9. Maintainability implications.** "A plugin is either fully present or absent" removes
an entire class of undefined intermediate states from every downstream reasoner (toolselect,
threat-model CI, capability counts).

**10. How Olympus should redesign it.** (a) `cli.main()` strips `''`/CWD from `sys.path`
before importing extras (one line, closes the planting analogue). (b)
`connectors.load_plugins` becomes transactional: import the module, validate *every*
declared tool (name collision check against `tools.HANDLERS`, classification present or
wrapped-by-default), then register all or none; a failure unregisters and reports —
Colibri's 48-symbols rule in plugin form. (c) Explicit-request honesty: if
`OLYMPUS_PLUGIN_ENFORCE` is on and a manifest-listed plugin fails verification, startup
*says so* per plugin (it already only loads verified ones — add the loud report); if a
config names a store/backend that can't load, refuse rather than degrade
(`vault.py`'s posture, generalized).

**11. Final Olympus architecture.** `connectors.py` (transactional registration),
`pluginstore.verified_names` (unchanged), `cli.py` (sys.path hygiene),
capability-count CI already catches tool-surface drift — half-registration now becomes
impossible rather than merely detectable.

**12. Why the Olympus approach is superior.** Colibri protects one DLL on one OS; Olympus
applies load-time atomicity to *every* dynamically loaded capability on every OS, and ties
it into the existing drift-gated capability counts so the loader's honesty is CI-verified.

---

## 8. Integrity seals & verify-before-write — from rANS final-states to sealed stores

**1. What Colibri does.** The CFSE/rANS decoder is "safety-first (weights corrupt
silently)": mathematically-proven bounds-check elision *plus* integrity seals — the two
final rANS states must equal `RANS_L` exactly (~2⁻⁴⁶ probability of silent corruption
passing), with exact length and frequency-table checks; `cfse_pack` performs a **mandatory
in-memory round-trip before writing anything**; the whole battery is ASAN-fuzzed (§5.4).
Relatedly, KV persistence files carry a magic + 8×int32 **model fingerprint** header —
mismatch means the file is *ignored*, and `nrec` is written last for crash safety (§4.5,
§8.2). Atomic tmp+rename writes guard the usage store (§7.3).

**2. Why it exists.** Compressed weights and persisted KV corrupt *silently* — the failure
mode isn't a crash but subtly wrong outputs forever after. Seals convert silent corruption
into loud refusal; round-trip-before-write converts encoder bugs into build-time failures;
fingerprints prevent semantically-wrong reuse (yesterday's model's KV against today's).

**3. How it works internally.** Structural invariants of the codec double as checksums
(final states, exact lengths); the fingerprint is cheap ints, not a hash of weights
(honestly documented as such); crash-safety via write-ordering.

**4. Strengths.** Seals are nearly free and *by construction* (no separate checksum pass);
the verify-before-write rule means no corrupt artifact is ever the only copy; the
fingerprint-mismatch policy is reject-never-repair applied to *stale* data, not just hostile
data.

**5. Weaknesses & trade-offs.** Coverage is codec-local: `.coli_usage`, route-pairs tables,
and stats files have atomic writes but no seals; the KV fingerprint "doesn't hash weights"
(§8.2) — acknowledged, but it means a re-quantized model with identical dims reuses stale
KV. Nothing detects *truncation* of append-only files.

**6. Security implications.** Olympus's durable stores are its moat substrate — the
Calibration Record thesis (`MOAT_ANALYSIS.md` Asset 1) is only worth something if the
records are tamper-evident. Coverage today is uneven: the vault is authenticated
(Fernet HMAC — corruption detected, `vault.decrypt_bytes`), the decision log and release
manifest are signed (`witness.py`), the ledger is hash-chained with an optional external
head anchor against truncation (`SECURITY_RESIDUALS.md` §3) — but memory JSONL, skills
files, `quality_baseline.json`, benchmarks, attestations JSONL, and backups have at most
atomic writes. `attest.list_attestations` explicitly *tolerates* malformed lines ("tolerant
of a hand-edited ledger") — hand-editable evidence is weak evidence.

**7. Scalability implications.** Hashing store segments is trivial at Olympus's data
volumes; sealing per-append (chained) costs one SHA-256 per record.

**8. Performance implications.** Must follow the DISK-CLASS discipline (§18): seals are
verified at *load* and written at *write* — zero cost on the model-call hot path, and the
whole layer must be behavior-identical when disabled (measurement-first: ship with a
before/after on store open latency).

**9. Maintainability implications.** One seal helper beats five bespoke integrity schemes;
the fingerprint idea gives every store file a schema/owner identity so a version migration
refuses cleanly instead of mis-parsing.

**10. How Olympus should redesign it.** **New `olympus/seals.py`**, three primitives:
(a) `fingerprint_header(kind, schema_version, owner)` — every persistent file Olympus writes
gets a first-line header (the `COLIKV1` analogue); mismatch on read → the file is set aside
(`.quarantine` suffix) and reported, never repaired, never fatal to the process.
(b) `sealed_append(path, record)` — JSONL append where each line carries
`h = sha256(prev_h + canonical_json(record))`, reusing `witness.canonical_json`; `verify()`
walks the chain; the existing ledger keeps its own richer chain — seals.py is for the stores
that have *nothing* (memory, skills index, attestations, outcomes). Periodic heads can be
signed by the witness key and pushed to the existing `OLYMPUS_ANCHOR` for truncation
evidence — reusing, not duplicating, `anchor.py`.
(c) `roundtrip_verified_write(path, encode, decode)` — the cfse_pack rule: `backup.py`
restores every archive *in memory* and compares before declaring a backup good;
`olympus backup verify` exposed as a CLI verb.

**11. Final Olympus architecture.** `olympus/seals.py`; adopters: `store.py` (file backend),
`skills.py`, `attest.py` (`record_attestation` → `sealed_append`; `list_attestations` stops
tolerating malformed lines when seals are on), `outcomes.py`, `backup.py`. Env:
`OLYMPUS_STORE_SEALS` (default on for new files; existing unsealed files are read as legacy
and sealed on next rewrite — no flag-day), `OLYMPUS_ANCHOR` unchanged. Heartbeat gains a
weekly `seals.verify_all()` sweep whose result lands in the trace; a failed verification is
an `INC-` ledger event (rubric 4).

**12. Why the Olympus approach is superior.** Colibri seals one codec and fingerprints one
cache; Olympus seals *the evidence layer itself* — the stores whose integrity the entire
moat argument rests on — chains them, optionally anchors heads off-host, and signs with the
same audited root of trust, so "our three-year calibration record is real" is a verifiable
claim, not an assertion.

---

## 9. Beyond Colibri — model-endpoint fingerprinting & drift detection

**1–3. What & why (design, not port).** Colibri's KV fingerprint answers "is this cache
from *this* model?" for a local artifact. Olympus's models are **remote endpoints**: a
provider can silently swap a snapshot, quantize harder, or route to a different backend —
the untrusted-mirror threat model where the mirror is the API itself. Colibri has no
analogue (its weights are local bytes it can hash); Olympus needs one, because every
accumulated calibration record is *keyed by model identity*, and silent model drift corrupts
the time series exactly the way silent weight corruption corrupts Colibri's outputs.

**4–5. Strengths/weaknesses of the inherited idea.** The fingerprint principle (cheap
identity check; mismatch → refuse to attribute, never guess) transfers cleanly; the
weakness to avoid is Colibri's own caveat — "fingerprint doesn't hash weights" — a
fingerprint that's too weak to detect what matters. An API client cannot hash weights at
all, so the fingerprint must be *behavioral*.

**6. Security implications.** Drift detection is also tamper detection for the moat data:
without it, a comparative-evidence record (Asset 2) can be poisoned by an upstream change
Olympus never noticed, and `gate_prompt`'s before/after benchmarks can mis-attribute a
regression to a prompt when the model moved underneath.

**7–8. Scalability/performance.** A probe set of ~20 fixed, greedy, cheap prompts per
(provider, model) run on the heartbeat cadence costs cents/day — bounded per the gate-cost
rule (ROADMAP §0 rule 2); it never touches the request path.

**9. Maintainability.** Reuses the golden-eval machinery (`evals.py`, the token-exact-oracle
translation the doctrine mandates): a drift probe is a tiny frozen eval whose *stability*,
not score, is the signal.

**10–11. Design & architecture.** Extend `modelpin.py` (it already exists to pin model
choices): `modelpin.fingerprint(provider, model)` = distribution over the frozen probe set
(greedy completions + logprob-free response hashes where providers allow, response-shape
metrics elsewhere); stored per (provider, model, date) in the store, sealed (rubric 8).
Heartbeat compares against the last accepted fingerprint; a shift beyond threshold emits a
`model_drift` event into the trace and *annotates* — never blocks — subsequent calibration
records, and notifies the operator. Env: `OLYMPUS_MODEL_FINGERPRINT` (default on when the
heartbeat runs), `OLYMPUS_FINGERPRINT_BUDGET` (per-day cap, refuses to exceed —
measurement-first cost honesty). Research spike (bounded): 2 weeks to establish the
false-positive rate of behavioral fingerprints under normal provider temperature-0
nondeterminism *before* wiring the annotation, using the same rotated-run-order/median
discipline as Colibri's CUDA fixture (§20).

**12. Why superior.** This is a capability Colibri structurally cannot need and hosted
orchestrators structurally won't build (flagging your own provider's silent swaps is
counter-positioned, per `MOAT_ANALYSIS.md` Asset 2) — and it directly protects the
integrity of the accumulated record that constitutes the moat.

---

## Open questions & research spikes

1. **`authkit.py` co-ownership with absorption 06.** Both domains propose it (06 for
   admission/route plumbing, 10 for bind/Host/auth/telemetry classes). Resolution needed:
   one module, security semantics owned here, serving ergonomics owned there. The
   synthesizer must merge the two specs before either lands.
2. **Egress guard default.** `egress.guard()` is OFF by default
   (`config.egress_guard_enabled()`); this domain's fail-closed doctrine argues for
   default-on, but that is a behavior change to every channel and must go through a
   before/after (false-HOLD rate on real traffic) — proposal: `audit` mode default first,
   one release of telemetry, then `enforce`. Needs a measured decision, not a doctrine vote.
3. **Reject-never-repair vs. sanitize-and-continue boundary cases.** Imported SARIF
   (`assess_import_sarif`) currently redacts-and-admits although it persists into the
   findings store — under the rubric-1 doctrine it is *durable* and should refuse on
   injection markers instead. Audit all `ingests untrusted → first-party write` flows in
   `THREAT_MODEL.md` for which side of the line they sit on.
4. **Seal migration cost.** Sealing memory JSONL retroactively is impossible (no trusted
   history); the plan seals from now on and marks the epoch. Is a signed "sealing began at
   T" statement sufficient for the moat-evidence claim, or should pre-epoch records be
   demoted in calibration analyses? (Leaning: demote — honesty-first.)
5. **Behavioral fingerprint spike (rubric 9).** Bounded 2-week spike on drift-probe
   false-positive rates under provider nondeterminism before any calibration annotation
   ships. Success criterion: <5% weekly false-alarm rate on a stable pinned snapshot.
6. **Host-header guard behind reverse proxies.** `OLYMPUS_ALLOWED_HOSTS` must document the
   X-Forwarded-Host question explicitly (trust the proxy only when
   `OLYMPUS_TRUST_PROXY=1`), or SEC-7 enforcement will break legitimate deployments and get
   turned off — a fail-closed control that users disable is worse than none.
7. **Route-auth CI scanner feasibility.** `scripts/check_route_auth.py` needs a reliable way
   to enumerate routes across `http.server`-style handlers and any framework-style tables in
   `web.py`/`dashboard.py`; if static enumeration proves fragile, fall back to a
   registration-time runtime assertion (undeclared route → refuse to serve) with a smoke
   test, which is fail-closed anyway.
