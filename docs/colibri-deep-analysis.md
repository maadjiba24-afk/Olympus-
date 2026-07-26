# Colibri — Exhaustive Technical Inventory & Reverse-Engineering Analysis

**Subject repository:** [`JustVugg/colibri`](https://github.com/JustVugg/colibri) — "colibrì: tiny engine, immense model"
**Analyzed at:** commit `81f08a0` (v1.1.1, 2026-07-23) — full-tree inspection of all 268 files: C engine sources, GPU backends, Python tooling, web/desktop UIs, docs, CI, tests.
**Method:** every major source file was read end-to-end (including the 6,751-line `c/colibri.c`); findings below cite files, functions, and the project's own issue numbers (`#NNN`), which are used pervasively in code comments as design provenance.

---

## 1. Executive overview

**What Colibri is.** A pure-C, single-translation-unit LLM inference engine whose founding constraint is running **GLM-5.2 — a 744-billion-parameter Mixture-of-Experts model** — on a consumer machine with **~25 GB of RAM** (proven floor; scales up to 6×RTX 5090 rigs). The core insight: a 744B MoE activates only ~40B parameters per token, and only ~11 GB of routed-expert weights change token to token. Colibri keeps the dense ~17B-parameter part resident in RAM at int4 (~9.9 GB) and treats the 19,456 routed experts (75 MoE layers × 256 experts + MTP head, ~19 MB each at int4, ~370 GB total) as a **heterogeneous storage hierarchy**: VRAM → pinned RAM hot-store → RAM LRU → OS page cache → NVMe, streamed on demand. The project's own tagline for the algorithm: *"a JIT, but for weights."*

**Fidelity doctrine.** Placement only ever decides *speed*, never answers: the engine is validated **token-exactly** against HuggingFace `transformers` oracles (teacher-forcing 32/32 on a tiny true-architecture fixture), and nearly every optimization ships with a "byte-identical to baseline" proof or an honest measured caveat.

**Composition:**
- `c/colibri.c` (6,751 lines) — the GLM-5.2 engine; plus ~15 header-only modules (safetensors, io_uring, tokenizer, grammar, quantization kernels, sampling, KV persistence, telemetry, portability).
- `c/olmoe.c` (1,018 lines) — the standalone Stage-A OLMoE engine (predecessor/proving ground).
- `c/backend_cuda.cu` / `c/backend_metal.mm` / `c/backend_loader.c` — opt-in GPU backends (CUDA, HIP/ROCm via a compat shim, Metal, Windows runtime DLL).
- `c/openai_server.py` (1,695 lines) — stdlib-only OpenAI **and** Anthropic-compatible HTTP gateway.
- `c/coli` — Python CLI launcher (`chat`, `serve`, `web`, `run`, `plan`, `doctor`, `bench`, `convert`, `info`, `build`, `stop`).
- `web/` — React/Vite dashboard (Chat, live "Brain" expert-cortex visualization, Profiling).
- `desktop/` — minimal Tauri v2 shell around the same web UI.
- `site/` — self-contained static landing page with a 3-D "expert atlas" galaxy.
- Extensive Python research/ops tooling (`c/tools/`): converters, quantization ablation, expert atlas, route-coupling analysis, efficiency regression harness, oracles, benchmark fetchers.

**Headline measured performance (community-sourced, each linked to a GitHub issue):** 6×RTX 5090 full residency **5.8–6.8 tok/s** (TTFT ~13 s); 128 GB CPU desktop ~1.8 tok/s warm; M5 Max + Metal 2.06–2.24 tok/s; single RTX 5070 Ti 1.07 tok/s; 25 GB dev box 0.05–0.1 tok/s cold. Quality: int4 container 62.5% mean acc_norm (0-shot hellaswag/arc/mmlu); measured quantization cost isolated via A/B ablation at −8.2 pp for per-row int4, ~63% recovered by grouped scales.

**License:** Apache-2.0. Single maintainer + community-datapoint governance; PRs to `dev`, fast-forwarded to `main`; v1.0.0 (2026-07-19) → v1.1.0 "community release" (27 PRs, 20+ contributors) → v1.1.1.

---

## 2. Repository map

| Path | Contents |
|---|---|
| `c/colibri.c` | GLM-5.2 engine (MLA + DSA + MoE + MTP), all execution modes |
| `c/olmoe.c` | Standalone OLMoE engine (Stage A) |
| `c/st.h`, `c/uring.h`, `c/kv_persist.h`, `c/tier.h` | Safetensors reader, io_uring streaming, KV persistence, tier policy |
| `c/tok.h`, `c/tok_unicode*.h` | Byte-level BPE tokenizer (cl100k + o200k), generated Unicode tables |
| `c/grammar.h`, `c/schema_gbnf.h` | GBNF PDA walker + JSON-Schema→GBNF compiler (speculative draft source) |
| `c/quant.h`, `c/fse_coli.h`, `c/cfse_pack.c` | Quantization formats/kernels, rANS entropy codec + packer |
| `c/sample.h`, `c/json.h`, `c/telemetry.h`, `c/compat.h`, `c/decode_batch.h` | Sampling, JSON parser, dashboard telemetry, Windows/macOS shims, ragged-decode seams |
| `c/backend_cuda.{cu,h}`, `c/backend_metal.{mm,h}`, `c/backend_loader.c`, `c/backend_gpu_compat.h` | GPU backends + Windows DLL loader + CUDA/HIP vendor shim |
| `c/openai_server.py`, `c/resource_plan.py`, `c/doctor.py`, `c/coli` | HTTP gateway, hardware planner, diagnostics, CLI |
| `c/tools/` | Converters, ablation, expert atlas, route coupling, efficiency, oracles, benchmarks |
| `c/tests/` (~60 files) | C unit tests, CUDA/Metal tests, Python tests, fixtures, micro-benchmarks |
| `web/`, `desktop/`, `site/` | React dashboard, Tauri shell, landing page |
| `docs/` | 16 documents: quickstart, tuning, ENVIRONMENT, SETTINGS, api, cuda, metal, benchmarks, windows, serve protocol, grammar, cache-route, perf reports, experiments |
| `Makefile`, `c/Makefile`, `flake.nix`, `docker/`, `.github/workflows/` | Build system, Nix, Docker, CI/release |

---

## 3. Core architecture

### 3.1 Design philosophy
- **Single-file build:** the whole engine is one translation unit (`colibri.c` + header modules) plus opt-in GPU objects — no object soup, no build dependencies. Default build is pure C with zero external libraries (no BLAS anywhere; OpenMP is the only concurrency framework for matmuls).
- **Measurement-driven engineering:** nearly every optimization carries a measured justification in comments (GB/s figures, µs costs, issue refs). Negative results are preserved as opt-in flags with written eulogies rather than deleted (e.g., `EXPERT_BUDGET` quarantine, P2P star-sharding, n-gram drafting).
- **Fail-soft for accelerators, fail-hard for data:** grammar/prefetch/mirror failures degrade silently to correct-but-slower paths; corrupt or hostile model containers cause immediate `exit(1)` with honest errors ("reject, never repair").
- **Untrusted-mirror threat model:** safetensors headers, config.json, tokenizer.json, and quantized layouts are all validated against crafted-file attacks (size caps, overflow checks, byte-count-exact format resolution — `#413`).

### 3.2 Supported model architectures
- **GLM-5.2 (`glm_moe_dsa`)** — the primary target: a DeepSeek-V3-lineage MoE with **MLA attention** (low-rank q/kv projections, partial interleaved RoPE), **sigmoid router with `noaux_tc` bias correction** (`n_group != 1` configs are refused), shared expert + 256 routed experts/layer (top-8), first `first_k_dense_replace` (3) layers dense, **DSA "lightning indexer"** sparse attention, and a native **MTP (multi-token-prediction) head** stored as layer `n_layers` (78).
- **OLMoE** (via `c/olmoe.c`) — standard GQA attention with q/k-norm, f32 dense + int8-streamed experts; the research workhorse for ablations.
- **Tiny/medium oracles** — random-weight true-architecture fixtures (`glm_tiny`, bench model) for token-exact validation and CI.
- **REAP-pruned checkpoints** supported (expert-count auto-detection probes the last expert by index, not hardcoded 255).
- **Planned (roadmap/site):** Kimi K2 (1T), Qwen3 MoE, MiniMax.

### 3.3 Main data structures (colibri.c)
- `Cfg` — all hyperparameters incl. MLA dims (`q_lora`, `kv_lora=512`, `qk_nope=192`, `qk_rope=64`, `v_head`), MoE dims (`n_experts`, `topk`, `moe_inter`, `n_shared`, `routed_scale`, `norm_topk`), DSA dims (`index_topk`, `index_nh`, `index_hd`, per-layer full/shared indexer type), up to 8 stop ids.
- `QT` — a quantized tensor `[O,I]` in one of 7 formats with optional CUDA mirror pointer and per-tensor failure latch (`cuda_failed`).
- `Layer` — norms, MLA projections, dense MLP or router + shared expert, CUDA extras (head-sharded kv_b, device router state).
- `ESlot` — one streamed expert: gate/up/down `QT` views into a single coalesced weight `slab` + scale `fslab`, with pin-arena ownership markers.
- `KVState` — per-sequence KV (`Lc` latent, `Rc` rope, `Ic` DSA index keys), per-layer `kv_start`, disk persistence handle.
- `Model` — shards index, resident tensors, per-layer expert tiers (`pin` hot-store; `ecache` LRU; `ws[64]` working set), usage/heat/recency counters, DSA weights, MTP head state, and a large profiling-counter block.
- `DecodeRow` (`decode_batch.h`) — `{KVState*, token, pos}`: one ragged-batch decode row of one independent sequence.

### 3.4 Threading model
- **OpenMP** for all matmuls (`schedule(static)`), plus a self-tuning trick: on startup the engine seeds `OMP_WAIT_POLICY=active`, `GOMP_SPINCOUNT=200000`, `KMP_BLOCKTIME=200`, `OMP_PROC_BIND=close`, `OMP_DYNAMIC=FALSE` and **re-execs itself once** (`/proc/self/exe`) because libgomp reads env in a pre-main constructor (measured matmul 66.9→20.9 s). CPU affinity is reset before re-exec (#471: an inherited one-core mask jailed the whole team, ~20× slowdown). Kill switch `COLI_NO_OMP_TUNE=1`; skipped under CUDA/Metal (on Metal the active spin steals the SoC power budget from the GPU — see §10.3).
- **pthreads** for: the PIPE I/O worker pool (≤16 threads, lock-free generation-tagged job cursor), a single detached PILOT prefetch worker, and (on Linux) io_uring's kernel-managed io-wq.
- Main decode loop is single-threaded; GPU work is issued asynchronously with CPU overlap.

---

## 4. Model loading pipeline

### 4.1 Safetensors reader (`c/st.h`, 479 lines)
- **pread-based by design, not mmap** — pread + `posix_fadvise(DONTNEED)` keeps streamed pages out of the process RSS (fixed a bug where mmap made peak RAM = whole model).
- Indexes up to 512 shards; ~120k tensor names go into an open-addressing FNV-1a hash (linear scan cost was tens of seconds/token, measured).
- **Hostile-file hardening** (untrusted-mirror threat model): 512 MB header cap, per-tensor dtype/offset/shape validation, int64 shape-product overflow rejection, `numel×esize == nbytes` cross-checks (blocks an OOB-write primitive), `st_read_f32_cap` refuses to write past caller-sized buffers.
- Every file gets an eagerly opened **O_DIRECT twin fd** (Linux O_DIRECT / macOS F_NOCACHE / Windows FILE_FLAG_NO_BUFFERING): buffered ext4-in-VHDX reads choke at ~0.8 GB/s vs 2.3+ GB/s direct (measured).
- `st_pread_full` — chunked (1 GiB default; tests force 7-byte chunks), EINTR-safe, honest short-read errors (#236: previously printed "Success").
- BF16/F16→F32 conversion; `st_read_raw` for pre-quantized U8 payloads; `st_read_slice_f32` reads a sub-range of fused `[E,…]` expert tensors; `st_prefetch` issues WILLNEED readahead.
- **Multi-drive split** (`COLI_MODEL_DIRS`): extra dirs act as a search path, each shard lives on exactly one drive — parallelizes expert loads across drives and adds capacity.
- **Dual-SSD mirror** (`COLI_MODEL_MIRROR`): a second byte-identical copy (size + full header memcmp validated); a deterministic hash `expert_route(layer,eid)` splits reads by `COLI_DISK_WEIGHTS=<p>,<m>` or a startup O_DIRECT bandwidth probe; falls back to primary on error; never written; partial mirrors allowed.
- Windows support entirely via `compat.h` shims (see §11.3).

### 4.2 Configuration parsing
`config.json` + `generation_config.json` parsed with the in-repo `json.h`; EOS ids are the **union** of both files (GLM-5.2 has 3 stop tokens; missing one printed control tokens into chat, #298). Every dimension range-checked at a single choke point (`CKR` macro). Bounded 256 MB slurp against crafted-file DoS.

### 4.3 Quantized container convention & format resolution
- Convention: for weight `X.weight` (U8 packed bytes) a sibling **`X.weight.qs`** (F32) holds the scales. Format is inferred **from byte counts, never metadata**.
- `qt_resolve_fmt` is a security boundary (#413): weight bytes must match a known layout exactly (int8/int4/int2/int3-g64/E8), scale bytes must match expected cardinality, else `exit(1)` — the old fallthrough silently mis-tagged short tensors as int2 and read out of bounds.
- Group size for grouped int4 is **derived from the `.qs` array size** (`detect_group_size`, candidates 16..256, multiples of 16 for AVX2).
- Runtime-quantization fallback: full-precision tensors without `.qs` are quantized at load (tiny-oracle path).

### 4.4 `model_init`
Loads embed/lm_head at `io_bits` precision, all layers, then **auto-detects** the MTP head (complete tensor set at layer `n_layers`) and the DSA indexer (presence of `self_attn.indexer.wq_b.weight`); computes resident bytes. `COLI_MMAP=1` switches experts to zero-copy mmap views registered with Metal (llama.cpp-style), with CPU pre-touch (GPU demand-faulting of file pages was "measured catastrophic") and deferred mlock wiring.

### 4.5 File-format magic numbers
- KV persistence: `"COLIKV1\0"` + 8×int32 model-fingerprint header.
- Expert-coupling table: text header `COLIPAIRS 1 <n>`.
- CFSE compressed tensor: `"CFS1"` + mode byte + raw length (§5.4).
- WSL/9p detection: statfs magic `0x01021997` (warns fadvise is a no-op there).

---

## 5. Quantization subsystem

### 5.1 Format taxonomy (`QT.fmt`)

| fmt | Format | Storage | Scales | Notes |
|---|---|---|---|---|
| 0 | F32 | 4 B/w | — | oracle/IO tensors |
| 1 | INT8 per-row | 1 B/w | f32[O] | activation-IDOT capable |
| 2 | INT4 per-row | 2 vals/byte, offset encoding (v+8, **not** two's complement) | f32[O] | the dominant GLM format (0.5 B/param) |
| 3 | INT2 per-row | 4 vals/byte | f32[O] | experimental floor ("craters" in ablation) |
| 4 | INT4 grouped (#242/#334) | same nibbles as fmt 2 | f32[O·⌈I/gs⌉], gs∈16..256 | detected from `.qs` size; g128 matches FP8 block granularity |
| 5 | INT3-g64 dual-plane (#132) | 24 B per 64-group (16 B 2-bit plane + 8 B 1-bit plane) | one f32 per group | 3.5 bpw effective; beat per-row int4 in ablation; CPU-only |
| 6 | E8/IQ3 lattice (#452) | 98 B per 256 weights = 3.0625 bpw | in-block (fp16 super-scale + 4-bit sub-scales); `.qs` is a 4-byte tag (6.0) | grid from ggml IQ3_XXS; requires activation rotation; CPU-only |

fmt 6 details: per 256-weight super-block — 64 grid indices (4-dim magnitude blocks), 8×u32 with four 7-bit sign words (8th sign recovered from **odd parity**) + 4-bit sub-scale codes, fp16 super-scale. Weights are stored **pre-rotated** (`W@Q`, QuaRot/QuIP#-family `Q = diag(signs)·H/√n`); the engine applies `Qᵀx` to activations at runtime via `e8_fwht` (iterative FWHT), with the sign diagonal **regenerated deterministically** (xorshift64* seeded `417+n` — "the constants are the spec", pinned by tests) and non-power-of-two dims tiled block-diagonally. All routed experts of a layer share one rotated input row (~1.4 ms/token vs ~11 ms per-expert). The converter enforces all-or-nothing e8 across gate/up/down.

Source formats decoded by the converter: **FP8 e4m3** with 128×128 block scales (FBGEMM convention: `_scale_inv` actually stores the multiplier) and **NVFP4 e2m1** (modelopt; per-16 fp8 scales + global f32 scale, with an assertion guarding against the reciprocal-storing llm-compressor convention).

### 5.2 CPU matmul kernels (`c/quant.h`, header-only, no engine dependencies)
- **Float-activation kernels:** `matmul` (f32), `matmul_q` (int8), `matmul_i4` (AVX2 nibble-unpack / NEON vzip / AVX-512 `dot_i4f_avx512` fast path), `matmul_i4_grouped` (fmt 4), `matmul_i2`, `matmul_i3` (fmt 5; NEON SIMD, **x86 scalar — stated TODO**), `matmul_e8` (fmt 6; per-sub-block expansion, compiler autovectorized).
- **Fused pairs:** `matmul_i4_pair` / `matmul_i4_grouped_pair` compute gate+up reading x once (~33% expert-matmul saving at decode).
- **IDOT family** (int8-quantized activations, default on): `dot_i8i8` / `dot_i4i8` with a full SIMD ladder — AVX-512 VNNI (`vpdpbusd` with sign-normalization trick), 128-bit AVX-VNNI (4 independent accumulators; single-accumulator chaining was latency-bound), plain AVX2 (`maddubs`), NEON+DOTPROD (4× `vdotq_s32`; measured 26→63 GB/s, 2.4×), plain NEON, POWER VSX (`vec_msum`). **ARM i8mm SMMLA** tiled drivers (2×2 `vmmlaq_s32` tiles) engage for S≥2. Integer accumulation is exact — tests demand **bit-exact** equality vs plain-C references.
- **Dispatch policy** (`matmul_qt_ex`): Metal GEMM (S≥16) → CUDA (resident, fmt≠5/6) → grouped/e8 CPU → IDOT (fmt 1/2, subject to `I4S` batch threshold — int4 IDOT pays at S=1 only on VNNI/SDOT-class hardware, measured +5.5% on Xeon 8370C) → exact f32 kernels. **Attention projections force exact kernels** (IDOT there costs measured +0.117 nats/token ≈ +12% perplexity).
- **AVX-512 absorb kernels** (`dot_i4f_avx512`/`axpy_i4f_avx512`, #442/#477): +13% decode; a startup self-test (`I4_ACC512_TEST`) validates vs scalar; test criterion is "tree reduction no worse than 2× sequential-order error" (it's usually 2–4× *better*).
- **XEXP** (`XEXP=1`, #475): S=1, all-resident, all-int4 → one OpenMP region across the whole expert block instead of ~2 fork/joins per expert; byte-identical; +11.6% on 48-core Ice Lake but neutral/negative on a 24-core box — hence opt-in.

### 5.3 Activation quantization
`qrow_i8`: per-row absmax → scale `amax/127`, `lrintf` rounding; per-thread scratch buffers. Python converters mirror the C math exactly (`np.rint` ↔ `lrintf`) to keep the "token identical" conversion claim.

### 5.4 FSE/CFSE entropy compression (`c/fse_coli.h`, `c/cfse_pack.c`) — *local, not yet wired into the engine*
- Despite the name, a static **order-0 rANS over nibbles** (2 interleaved states, 12-bit normalized frequencies, byte renorm). Order-0 is provably sufficient: int4 weights measured statistically white (conditional-entropy gain +0.000) — H = 2.924 bits/weight ⇒ ~1.37× lossless shrink of int4 experts (verified vs zstd's FSE stage).
- Container `CFS1`: mode byte (raw fallback whenever compression doesn't strictly win), freq table, two states, backward-written payload. Decoder is safety-first (weights corrupt silently): fused 16 KB decode table, hot loop with mathematically-proven bounds-check elision, and integrity seals (final states must equal RANS_L exactly — ~2⁻⁴⁶ silent-corruption probability; exact length/frequency checks).
- `cfse_pack` CLI converts/verifies/certifies safetensors (`__metadata__ {"cfse":"1"}`), with a mandatory in-memory round-trip before writing anything.
- **Status:** codec + packer + ASAN-fuzzed test battery exist; decode-on-load engine integration is pending (explicit TODO).

### 5.5 Conversion pipelines (`c/tools/`)
- **`convert_fp8_to_int4.py`** — the main pipeline (GLM-5.2-FP8 → colibri container): downloads one ~5 GB shard at a time (peak disk = 1 shard + output, ~372 GB int4, never 756 GB), dequants FP8/NVFP4/BF16 → f32, classifies each tensor (see §5.6), quantizes, emits shards + `.qs`. Operational armor born of real incidents: a `[PLAN]` line at second 1 (#383), a parameter manifest that refuses resume-with-different-flags (#355: a second `--mtp` pass silently overwrote 137/141 finished shards), an outdir lock, index-driven shard selection for `--mtp`/`--indexer` modes, atomic progress writes, metadata copying, and a custom multi-stream Range downloader with sparse `.part` + per-segment checkpoint sidecars ("NO byte is lost however the connection dies"; HF single-stream measured ~2 MB/s).
- **`convert_olmoe.py` / `convert_olmoe_merged.py`** — OLMoE per-tensor int-N; the merged variant concatenates each expert's gate/up/down into one tensor so `olmoe.c` loads an expert in **one disk read instead of three**.
- **`repair_mtp_int8.py`** — in-place container surgery: detects int4 MTP tensors by blob size, re-downloads only the ~355 MB affected dense tensors via anonymous HTTP Range reads (pure-numpy BF16/FP8 decode), requantizes at int8 with the converter's exact math, rewrites shards preserving byte identity elsewhere (originals kept as `.bak-int4`).
- **`glm_fp8_emit.py`** — the inverse (FP8 test-fixture emitter) incl. `unfuse_experts` for HF's fused 3-D expert tensors.
- **`iq3_pack.py`** — the bytes-true fmt 6 codec (bit-exact mirror of the C decoder, incl. the frozen sign-PRNG spec).
- **`download_fp8.py` / `download_glm52.py`** — parallel downloaders (ModelScope-first with HF fallback; revision pinning "for supply-chain integrity"; size-verified resume; curl fallback). Limitations: size-only verification (no hashes), hard-coded personal paths (`I:\glm52_fp8`, `/home/vincenzo/glm52`).

### 5.6 Mixed-precision policy (the converter's `classify()`)

| Tensor class | Precision | Rationale |
|---|---|---|
| Router `mlp.gate.weight` | F32 always | routing sensitivity (explicitly distinguished from `gate_proj`) |
| Norms, biases, `e_score_correction_bias` | F32 | tiny + sensitive |
| embed / lm_head | int8 (`--io-bits`) | ablation: fp16 head is *not* the fix; int8 suffices |
| Routed experts | int4 (per-row/g128), optionally int3-g64 up_proj or e8 | biggest byte pool; up-only int3 ≈ −8% bytes for free |
| Shared expert / o_proj / kv_b / attn / dense-MLP | per-class `--*-bits` overrides | "fires on every token" / "reconstructs output" / "reconstructs KV every decode step" |
| **MTP head (layer 78)** | **int8 mandatory** | issue #8: `eh_proj` column halves differ 20–30× in scale; per-row int4 rounds the entire embedding half to exact zeros → draft acceptance 0–4%; at int8: 39–59% |
| DSA indexer | skipped by default (int8 if extracted) | no-op in the main container |

### 5.7 Quality tooling
- **`quant_ablation.py`** — engine-free A/B: quantize→dequantize an HF model *in place with colibri's exact math* and score both with the same harness — "the delta IS the quantization cost." Scheme grammar `int{2,3,4,8}[-gN][-e8|-e8u|-iq3][-rot][-nohead]` including QuaRot rotation, an IQ3_XXS float simulation, and true **E8-lattice** quantization (Conway–Sloane nearest-point, rate-scaled ball radius) — the #453 study that selected fmt 6. Guards against the transformers fused-3-D-expert trap (a naive `ndim==2` filter silently leaves ~85% of an MoE in fp16 — `--min-coverage 95%` hard-fails) and the `[gMASK]<sop>` prefix OOD hazard (#108: without it perplexity 9.4→29.2 and quantization deltas *flip sign*).
- Headline ablation (OLMoE, #108): fp16 57.0% → int4 per-row 48.8% (−8.2 pp, concentrated on MMLU) → **int4-g128 54.0%** (recovers ~63% for +0.25 bpw).
- Fixtures/oracles: `make_e8_fixture.py` (Python-reference-only binary fixture; the C kernel must match it, incl. the rotation path), `test_iq3_pack.py` (byte budget, frozen PRNG constants, parity closure, plausibility-banded reconstruction error), `test_i4_grouped.c` (the CPU oracle the CUDA fmt 4 path must reproduce), `test_int3.c` (the #132 quality claim shipped as a regression test: per-group int3 must beat per-row int4 on outlier-seeded rows).

---

## 6. Inference pipeline

### 6.1 MLA attention with compressed KV & weight absorption (`attention_rows`)
- q path: `q_b(rmsnorm(q_a(x)))` → per-head `[qk_nope | qk_rope]`, RoPE'd **interleaved** (per-thread pos-keyed cos/sin cache; `qk_rope ≤ 256` hard cap).
- kv path: `kv_a(x)` → `[kv_lora | qk_rope]`; the latent is rmsnorm'd then stored.
- **Compressed KV:** per token only 512 latent + 64 rope floats = **576 floats vs 32,768** for 64 full heads (57× smaller) — the enabler for long context in 15 GB, and what makes KV persistence cheap.
- **Two compute paths:** (1) **weight absorption** (DeepSeek-style; auto for S≤4 and always for ragged rows): scores against the latent directly via absorbed `W_K`, value reconstructed via `W_V` on the attention-weighted latent — O(T·kv_lora)/step; SIMD'd dot+AXPY (#442). (2) **full reconstruction** (prefill): one batched `kv_b` matmul over the whole cache, then dense causal attention (`collapse(2)` OpenMP).
- No GQA/MHA in colibri.c (pure MLA); olmoe.c uses standard GQA with q/k-norm.
- Layer forward: pre-norm RMSNorm (double accumulation) → attention → residual → post-norm → MoE or dense SwiGLU → residual. Activation: SiLU everywhere.

### 6.2 DSA sparse attention (the "lightning indexer")
Active only when indexer weights exist and context > `index_topk` (2048): per full layer, cached `k_idx = rope(layernorm(ix_wk·x))` (the engine's only classic LayerNorm); per query, ReLU-scored weighted head sum selects the **top-`index_topk`** context positions via `partial_select_desc` — a median-of-three quickselect (#356) replacing a full qsort, with a two-scan position-ordered rebuild that keeps the selected set **element-wise identical** to the old algorithm. "Shared" indexer layers reuse the previous full layer's selection. For seq ≤ index_topk the indexer is a no-op, so the tiny oracle validates the dense path. `DSA=0` disables; `DSA_FORCE=1` forces selection for testing.

### 6.3 MoE routing & expert compute (`moe()`, phases FASE A–E)
- **FASE A (routing):** batch matmul → sigmoid logits, `choice = logit + router_bias` (noaux_tc), plain top-K by choice, **gate weight = the sigmoid logit (not the biased score)**, optional norm_topk renorm, ×`routed_scaling_factor`. Overrides: `TOPK` (fewer experts), `TOPP` (per-position adaptive expert top-p — measured −30–40% disk at slight quality cost). Every selection updates persistent usage, decaying heat, LFRU recency, and dashboard hit-bitmap counters. GPU pre-routing (Metal layer command buffer or CUDA device router) can skip FASE A entirely.
- **CACHE_ROUTE** (opt-in; arXiv:2412.00099 "max-rank"): keep the true top-J always, fill remaining K slots preferring **already-resident** experts within rank M (or mass P), `ROUTE_ALPHA` down-weights substitutes; full agreement telemetry (swap%, overlap, KL). This is the *routing-side* lever (can change which experts run); PILOT is the prefetch-side one (never does).
- **EXPERT_BUDGET** — a fully implemented per-layer distinct-expert cap that is **quarantined at startup** (#303: hellaswag 30% vs 90%, MTP acceptance 0%, slower than baseline — "empty operating window"); requires `EXPERT_BUDGET_EXPERIMENTAL=1`.
- **FASE B (batch-union):** unique experts of the whole batch computed once — each unique expert's weights are read from disk once for all positions using it (the key prefill I/O amortization, and why speculative verify batches are nearly free on I/O).
- **FASE C/D (resolve & compute, blocks of 64):** per expert search pin → LRU → miss into working set; misses loaded via PIPE/io_uring or blocking parallel preads; next block gets WILLNEED readahead while this block computes. Compute path priority: Metal MoE command buffers (resident subset submitted *before* disk loads → GPU/disk overlap) → CUDA expert groups (async issue/take; resident-on-device accumulation) → XEXP single-region CPU → default per-expert loop (fused gate+up pair → SiLU·up → down → weighted accumulate). End-of-block LRU promotion via swap-buffer exchange.
- **FASE E (shared expert):** SwiGLU matmuls added after routed accumulation; skipped when the GPU already produced it.

### 6.4 Sampling (`c/sample.h`)
- xorshift64* RNG (`SEED`); greedy at `temp ≤ 0`; softmax/temperature + **top-p nucleus** via a **max-heap partial selection** (#335 — no full-151,936-vocab qsort; token-id-indexed probability buffer with exactly-zeroed tail, verified bit-tight vs the old algorithm).
- Defaults deliberately tighter than GLM's official: `NUCLEUS=0.90` (vs 0.95) and auto temp 0.7 in chat (vs 1.0) — "the int4 tail is quantization noise."
- **NaN/Inf armor** (#369): non-finite logits → one-time warning + finite-argmax fallback (a single NaN once produced a silent unbroken stream of token 0).
- Stop set: config ∪ generation_config EOS ∪ every tokenizer `special:true` control token; **in SERVE mode filtered to EOS only** (#401: role-marker stops truncated tool-call blocks because int4 argmax noise picked stop ids over `<`).
- Not implemented: token-level top-k sampling, repetition/frequency/presence penalties, per-request seeds.
- `TEMP` env alias honored only if purely numeric (#509 — `$TEMP` is a directory on Windows/ROCm; `atof("C:\...\Temp")==0` would silently force greedy, and a numeric TEMP crashed ROCm's comgr init, which the CUDA backend works around by unsetenv).

### 6.5 Speculative decoding — three draft sources, one lossless verifier
`spec_decode` implements Leviathan rejection sampling: greedy verify = exact match; sampling verify accepts with `p(draft)` and on rejection **resamples with the rejected token banned and renormalized** — output distribution exactly preserved ("speculation invisible even under sampling"). All drafts verify through the batch-union forward, so in a disk-streaming MoE accepted drafts convert almost directly into disk reads avoided.
1. **Grammar-forced drafts (method F, #48/#70/#146)** — highest priority; see §6.6.
2. **MTP head** — DeepSeek-V3-style chained draft head (layer 78) with its own decode-only KV window; `mtp_absorb` batch-syncs it on verified tokens; soft acceptance guard (#163: <10% over a 24-proposal window → pause 256 tokens, re-arm); `SPEC_PIN=1` pins the S=1 kernel family across draft+verify (kernel-family divergence collapses acceptance); measured 2.2–2.8 tokens/forward. Off by default under CUDA (`COLI_CUDA_MTP=1` opts in) because CPU/GPU FP-order divergence on cold experts tanks acceptance.
3. **n-gram prompt lookup (method E)** — bigram continuation from context; zero cost but default OFF (measured 5% acceptance → 3× slower on cold disk); `DRAFT=n` opt-in for repetitive text.
`DRAFT=-1` auto: 3 with MTP present. In the multiplexed server, MTP/n-gram are force-disabled (not ragged-safe); grammar drafts remain (a drafting slot briefly leaves the shared batch for its verify forward).

### 6.6 Grammar-constrained drafting (`c/grammar.h`, `c/schema_gbnf.h`)
- **Design decision:** the grammar is **never a sampling constraint** — only a draft source. Wherever the grammar admits exactly one legal next byte (braces, key names, separators), the forced span is tokenized and injected as draft tokens. A wrong/desynced grammar can only cost rejected drafts; output is byte-identical by construction.
- GBNF subset (llama.cpp-style, byte-level): literals with escapes, byte classes (incl. negation), refs, groups, `? * +`, alternation, comments; no `{m,n}` or Unicode classes. Compiled to a **set-of-stacks PDA** (≤64 parallel stacks, depth ≤64); left recursion trips the depth cap and the walker turns itself off — "never a block, never a crash."
- Lazy arming (skips `<think>` preambles), desync re-arm, adaptive kill (<50% acceptance after 32 proposals), `GRAMMAR_DRAFT` span cap (24, max 48). Measured: conforming NDJSON 1.60 tok/forward; sloppy-spacing 1.21–1.22 at 87% acceptance.
- **JSON-Schema → GBNF compiler** (`schema_gbnf.h`): objects with strict OpenAI `required` semantics, strings/enums/consts, numbers/integers/booleans/null, arrays with `minItems` 0|1, ≤32 nesting; everything else fails closed (never silently looser). Key lesson baked in: an optional-whitespace rule `jws` at every separator — a compact-only grammar desyncs at the first stray space and forfeits every later span.
- Per-request grammars ride the serve protocol's optional 7th `SUBMIT` field (≤1 MiB); HTTP-side `response_format: json_object | json_schema | gbnf`.

### 6.7 Batched/multiplexed decode
`step_decode_batch`: one **ragged batch row per active KV slot** (distinct KVState required, S≤512), heavy per-row validation. The mux server (§9.2) prefills serially per submission (with KV-prefix reuse and a loud `CONTEXT_EXCEEDED` refusal instead of the old silent truncation, #401/#506) and decodes all active slots in one forward per step.

---

## 7. Memory management & the storage hierarchy

### 7.1 Tier model
**VRAM (CUDA/Metal expert store) → RAM pinned hot-store → RAM per-layer LRU (`ecache`) → OS page cache → disk (primary + optional mirror)**. Telemetry encodes per-expert tier + 6-bit log₂ heat in the `EMAP` line (the web "Brain" view's data source).

### 7.2 Expert loading (`expert_load_impl`)
- **One coalesced ~19 MB pread** when gate/up/down are contiguous in the shard (offset-sorted contiguity check), else 3 preads; O_DIRECT twin with 4K alignment under `DIRECT=1`; EINTR/short-read-safe; non-fatal mode for speculative pilot loads (a misprediction must never kill the server); optional post-read `fadvise(DONTNEED)` (`DROP=1`).
- Slab reuse across layers with realloc guards (MTP int8 experts are 2× the size of main int4).
- `COLI_MMAP=1` alternative: zero-copy page-cache-backed views (see §4.4).

### 7.3 Pinning, learning cache, live re-pinning
- **PIN hot-store:** `PIN=<stats file>` or `PIN=auto` (prefers live `.coli_usage` over frozen stats) ranks experts by usage and pins the top set within `PIN_GB` (or `PIN_GB=all` clamped to the RAM budget). `PIN_FILL` pads with all remaining experts (multi-GPU default).
- **AUTOPIN ("the cache that learns"):** `.coli_usage` accumulates expert selections across sessions (atomic tmp+rename writes — the Windows CRT rename bug silently starved this pipeline until `compat_rename`); at startup, ≥5000 recorded selections auto-pin with a confidence-scaled budget. Profile quality beats capacity: the same 150 GB tier measured 0.94–1.64 tok/s hot-first vs 0.29 heat-blind.
- **REPIN (`REPIN=n`):** at safe points every n tokens, `tier.h`'s LFRU policy (score `(heat<<8)|recency` — frequency dominates, recency tie-breaks; 25%+4 hysteresis against ping-pong; periodic heat halving) swaps the coldest pinned experts for the hottest unpinned; the CUDA variant migrates VRAM slots in place via `tensor_update`. Best measured config: 16-token cadence, ≤16 swaps (part of the 6×5090 6.28 tok/s ladder).

### 7.4 RAM budgeting & guards
- `cap_for_ram`: honest slack accounting (working-set slabs, KV pool, scratch, 1.2 GB activations, and a **mandatory 2.5 GB page-cache reserve** — measured 800→180 MB/s pread collapse without it); auto budget = 88% of boot MemAvailable (per-OS probes); **refuses to start** if the projected peak exceeds physical RAM (silent OOM-kill prevention, #305; override `COLI_RAM_OVERCOMMIT=1`); `CAP_RAISE` auto-grows the LRU cap when the budget allows (#12: a 128 GB box once ran with a 16 GB box's cache).
- **RSS guard (#403):** every ~16 tokens, if measured RSS exceeds the budget, frees least-used LRU slabs in place and permanently lowers the cap.
- **NUMA (#82/#419):** `COLI_NUMA=1` interleaves *expert slabs only* across nodes via raw `SYS_mbind` (no libnuma; EPERM probe downgrades gracefully); the pinned hot-store binds **one arena per layer** to avoid `vm.max_map_count` exhaustion. Measured +13% (2-socket) / +40% (4-socket); blanket `numactl --interleave` measured up to 10× *regression*.
- mlock wiring (`MLOCK`, auto-on for macOS's memory compressor; Windows VirtualLock).

### 7.5 The prefetch stack (each level measured, several deliberately default-off)
- **SPEC** (default on): WILLNEED of the previous token's routing.
- **PILOT** (#PILOT/PILOT_REAL/PILOT_TWO): router-guided next-layer prediction — predict L+1's top-K from L's post-attention state (**71.6% recall vs 41.3%** for previous-token heuristics; PILOT_TWO's shared-expert-corrected state +2.3%). Hints flow through a lock-free 1P/1C ring to a dedicated I/O thread (inline fadvise measured 0.5 ms × 169k calls = +92 s per 48 tokens). `PILOT_REAL=1` performs *real loads* into the next layer's LRU under a strict two-part safety invariant (generation barrier + mutex; matmuls can never touch a half-loaded slot), with an **eviction guard** (#441/#490: speculation may not evict a genuinely warm resident — a prior bug dropped ~100% of speculations).
- **COUPLE (#176):** offline cross-layer co-activation table (`route_pairs.py` from `ROUTE_TRACE` dumps; median lift 1.8×, p99 40×; +3.6..+9.4 pp prefetch recall over marginal heat, transfers across workloads) feeding the same ring, 1–2 layers ahead.
- **LOOKA:** pure measurement mode — a 4-predictor routing-predictability report printed at exit.
- **PREFETCH** proper is default-off: real parallel loads made bare WILLNEED superfluous (measured).

### 7.6 Load/compute overlap: PIPE and io_uring
- **PIPE:** persistent pthread pool (≤16) runs miss preads into distinct working-set slots while the main thread does all matmuls; per-slot ready flags with spin or condvar wait (`COLI_PIPE_BLOCK`, #159: yield storms fight OpenMP). Lock-free **generation-tagged cursor** `(gen<<8)|idx` — the CAS comparand carries the generation, so no ABA and no torn batch state. Default ON on Windows, opt-in elsewhere (−18% disk service time documented).
- **URING (Linux):** raw-syscall io_uring (no liburing) batches up to 64 expert loads / 512 SQEs per block into **one submit syscall**; `IOSQE_ASYNC` always set (cold buffered-file reads otherwise execute *inline* in `io_uring_enter`, destroying overlap); separate rings for PIPE and PILOT; io-wq worker caps; **strict** — refuses mmap/unquantized layouts with ENOTSUP rather than silently falling back; incompatible with `COLI_MMAP`.
- `iobench.c` measures exactly what the engine does (N threads × random 19 MB expert-sized reads, buffered vs direct), with documented Windows portability traps (RAND_MAX=32767 offset composition, LLP64 `long`, `_aligned_malloc` free).

---

## 8. KV cache

### 8.1 In-memory layout
Per layer: `Lc[max_t × kv_lora]` (normalized MLA latent) + `Rc[max_t × qk_rope]` (roped keys) + optional `Ic[max_t × index_hd]` (DSA index keys) — all f32; no KV quantization, no paging (flat per-slot buffers sized by `CTX`). One extra row set for the MTP head's decode-only KV window. Metal registers Lc/Rc zero-copy; CUDA keeps per-layer device shadows with bulk sync and invalidation-on-rebind.

### 8.2 Slots & persistence (`c/kv_persist.h`)
- **KV slots** (`KV_SLOTS`, ≤16 interactive / ≤512 mux): independent conversations, each with its own history and disk file — the server's session mechanism.
- **On-disk format:** `<model>/.coli_kv[.slot]`, magic `COLIKV1\0`, an 8×int32 header doubling as a **model fingerprint** (mismatch → file ignored), then fixed-size append-only records `[token id][all layers' Lc+Rc][Ic]` (~182 KB/token). `nrec` is written **last** → crash-safe. Prefix matching in serve mode truncates and appends only new positions; loading resumes conversations warm with **zero re-prefill** (byte-identical). `KVSAVE=0` disables.
- Limitations noted: f32 records (no compression), fingerprint doesn't hash weights, fflush without fsync.
- A war story in the CLI: a resumed 670-token Italian session made every reply Italian and "looked like a quantization bug for a day" — hence the prominent resume notice.

---

## 9. Scheduling, execution modes & server protocols

### 9.1 Engine execution modes (all env-selected; positional args `cap ebits dbits`)
1. **Oracle self-test** (default, no PROMPT): greedy generation vs `ref_glm.json`, with a guard that detects a tiny-oracle/real-model mismatch by max token id (#76).
2. **`TF=1`** — teacher-forcing prefill validation over the whole sequence (expected 32/32 on the tiny model — the canonical correctness gate).
3. **`SCORE=<file>`** — lm-eval-style log-likelihood scoring (one forward per option; auto-prepends `[gMASK]<sop>` with ids looked up from tokenizer.json, #108).
4. **`REPLAY=1`** — fixed-token decode benchmark (CPU/CUDA see identical inputs).
5. **`PROMPT`/`COLI_PROMPT`** — one-shot generation with streaming output, heartbeat stats, and a rich end-of-run report. (On Windows, `PROMPT` containing cmd.exe `$`-metacodes is ignored — #271.)
6. **`SERVE=1`** — persistent single/multi-slot chat over a stdin/stdout byte protocol (READY/END sentinels, `\x02RESET`/`\x02MORE`/`\x02PROMPT` control frames, STAT lines, GLM chat template with `<think></think>` nothink default).
7. **`SERVE=1 SERVE_BATCH=1`** — the **continuous-batching multiplexer**: `SUBMIT id slot bytes max_tokens temp top_p [gbytes]` (+payload+optional grammar), `CANCEL`, `DATA`/`PROF`/`DONE`/`ERROR(BAD_REQUEST|BAD_FRAME|SLOT_BUSY|DUPLICATE_ID|EMPTY_PROMPT|CONTEXT_EXCEEDED|CANCELLED|NOT_FOUND)` frames; serial prefill with KV-prefix reuse, ragged batched decode across slots; `select()`/`PeekNamedPipe` idle polling. No HTTP inside the C engine — HTTP lives in Python.
- Telemetry lines interleaved on stdout: `HWINFO`, `TIERS`, `EMAP`, `HITS`, `PROF`, `PERF`, `ENTROPY`, `GPUS`, `REPIN`, `STAT` — consumed by the CLI, HTTP server, and dashboard.
- Windows: both stdio handles forced binary (#195 — CRLF translation corrupted sentinels).

### 9.2 HTTP gateway (`c/openai_server.py` — stdlib-only, threaded)
- **OpenAI API:** `POST /v1/chat/completions` (JSON + SSE streaming, tool calling, `response_format` json_object/json_schema/raw `gbnf`, `reasoning_effort`/`enable_thinking`, `stream_options.include_usage`, non-standard `cache_slot`), `POST /v1/completions`, `GET /v1/models[/id]`.
- **Anthropic Messages API** (`POST /v1/messages`, #343): a full translation layer — system blocks, tool_use/tool_result, all `tool_choice` modes, extended thinking, named SSE events (`message_start`→`content_block_*`→`message_stop`) with protocol pings, Anthropic stop reasons/usage/error envelope, `x-api-key` auth. Claude Code is the reference client. Unsupported fields (`stop_sequences`, `top_k`, non-text blocks) are refused loudly.
- **Telemetry endpoints:** `/health` (always-200 liveness; scheduler/kv_slots/tiers/hwinfo only when authed — #SEC-8), `/experts` (routing map for the Brain view; authed), `/profile` (rolling 120-turn PROF window; ungated — a noted inconsistency).
- **Static hosting** of `web/dist` on the same port (traversal-safe, SPA fallback) — `coli web` is one process.
- **Engine proxying:** one persistent engine subprocess in mux mode; the HTTP port is bound *before* the engine launches (a busy port fails in ms, not after loading 370 GB); a single dispatcher thread demuxes engine stdout by request id into per-request queues; `CANCEL` wired to client disconnects (MSG_PEEK probe).
- **Admission control (`GenerationScheduler`):** bounded FIFO over `kv_slots` capacity; `COLI_MAX_QUEUE` (8), `COLI_QUEUE_TIMEOUT` (300 s); 429 `queue_full`/`queue_timeout` with Retry-After; per-slot fair admission (no head-of-line blocking across slots); counters exposed via `/health`; `x-colibri-queue-wait-ms` and `x-request-id` response headers.
- **GLM tool calling:** byte-exact official template rendering (`# Tools`/`<tools>` block; invented preambles make the model hallucinate other frameworks' syntax); strict regex parse of `<tool_call>` blocks with **schema-typed argument coercion** (string-typed "12345" stays a string), **unclosed-tail recovery** for out-of-budget calls (#401/#505 — only when unambiguous; prose can never fabricate a call), and an opt-in de-mangler (`COLI_TOOL_SALVAGE=1`) for heavily-quantized output; streaming marker suppression with hold-back across chunk boundaries.
- **Keepalive pump:** cold prefill can block for minutes, so a background thread emits a `reasoning_content: "."` delta after 10 s of silence (lands in clients' thinking panels; write-lock-serialized so `[DONE]` can't interleave).
- **Security:** constant-time API-key compare (Bearer + x-api-key), DNS-rebinding Host-header guard (#SEC-7), fail-closed non-loopback bind without a key (#SEC-6), CORS allowlist (localhost + Tauri origins), 30 s socket timeout (Slowloris), 4 MiB body / 1 MiB grammar caps.
- **Deliberate non-features** (explicit 400s, never silently ignored): embeddings, logprobs, n>1, stop sequences, penalties, per-request seed.

---

## 10. GPU acceleration

### 10.1 Backend architecture
- **Four linkage models, one C ABI:** Linux CUDA (nvcc object linked in), Linux HIP/ROCm (same `.cu` via hipcc), Windows CUDA (**runtime-loaded `coli_cuda.dll`** — MinGW host can't link MSVC/nvcc objects; `backend_loader.c` resolves ~48 `coli_cuda_*` symbols all-or-nothing, with DLL-hijack-safe search paths that never include CWD), macOS Metal (Objective-C++ object, **runtime-compiled shaders** — no Xcode needed).
- **CUDA/HIP single-source doctrine:** all vendor differences live in `backend_gpu_compat.h` (~37 symbol mappings); contributor rule: never `#ifdef __HIP__` in the `.cu`. `COLI_GPU_HAS_WMMA` compile-gates tensor-core paths off under HIP (AMD reports cc≥7 but would dispatch empty WMMA bodies; rocWMMA is the noted follow-up). Validated on RX 9070 XT / ROCm 7.2, token-exact.
- **Fallback discipline:** every GPU entry point has a CPU fallback; per-tensor `cuda_failed` latching; an explicit CUDA request with no runtime **fails at startup** rather than silently running on CPU (#121: silently-CPU "GPU benchmarks" once got published). Fault-injection hook `COLI_GPU_FAIL_AFTER=N` for testing the fallback lifecycle.
- Windows bare runs **auto-enable** the GPU when the DLL + nvidia-smi are present (`COLI_CUDA=0`/`--gpu none` are hard off switches).

### 10.2 CUDA backend (`backend_cuda.cu`)
- **Kernel inventory:** generic quantized GEMV/GEMM (fmt 0–4, shared-memory tree reduction); fused SiLU·up; **W4A16 WMMA tensor-core GEMM** and fused gate+up variant (fp16 tiles, fp32 accumulate — lossless); **native s4×s4 IMMA** (`wmma::experimental::precision::s4`, W4A4, lossy, opt-in); grouped expert GEMVs in four flavors (generic, packed-W4 exact, dual gate+up, grouped-g4 with per-group scales — the #298 fix class); MLA absorption attention (single, batched-causal, **ragged** per-sequence with device-resident paged KV registries and grow-with-copy appends); GPU router (float-faithful single-thread selection clone — argmax order and tie-breaking match the CPU exactly); resident-pipeline primitives (rmsnorm/rope/silu/residual adds, all fixed-order, **no atomics** — determinism doctrine).
- **Expert VRAM tier:** experts placed **whole** (never sharded) on the least-loaded fitting device; `CUDA_EXPERT_GB=auto` fills measured free VRAM minus dense set minus reserve; multi-GPU defaults `PIN_FILL=1` + `CUDA_RELEASE_HOST=1` (host slabs freed after upload; `expert_host_ensure` re-materializes from disk when the CPU path needs one back).
- **Pipelines:** layer-resident prefill attention; **PIPE2** (`COLI_CUDA_PIPE=2`) keeps the residual stream on-device across layers with P2P hops at group boundaries (**+49% decode on a single 5070 Ti**; gated off multi-GPU where per-layer hops cancel it); device router; async expert-group **issue/take** split at decode scale (sync host-wait tax measured ~70%); resident expert-group accumulation with zero host bytes (#431 PR-C0).
- **Perf accounting:** thread-local current-device cache (naive `cudaSetDevice` per call doubled expert time on 2 GPUs); grow-only scratch and pinned staging; 27 persistent pipe scratch slots (avoids ~780 cudaMalloc/frees per request); `COLI_CUDA_PROFILE` event timing (H2D/kernel/D2H).
- **Why streaming experts stay on CPU:** copying NVMe→GPU per use would swap the disk bottleneck for a PCIe bottleneck; only resident tensors earn VRAM — "a tuned AVX-512 CPU can match a 5090 on expert matmul" (#101, empirically confirmed on a 9800X3D+5090).

### 10.3 Metal backend (`backend_metal.mm`)
- **Unified-memory zero-copy:** page-aligned expert slabs registered once as `MTLBuffer`s (bindless `gpuAddress` addressing inside kernels); the whole MoE path reads weights from the RAM they already occupy. `DIRECT=1` is required for the zero-copy registration path (measured ~2×: 2.16 vs 1.15 tok/s).
- **Batching against submit latency:** fused decode attention (one command buffer: q/kv projections, RoPE, cache writes, absorption, o_proj) and a **full-layer command buffer** (norms+attention+residual+shared+router+top-K in one submit) — both hard-gated to exact GLM-5.2 int4 dimensions. MoE block submits put **resident experts on GPU before disk loads start** — GPU compute and disk I/O overlap. Unlike CUDA, streaming experts *do* run on the GPU here (no PCIe tax).
- **Bitwise-exact parallel router:** `r_top8_par` (one simdgroup per row, ~93× the serial kernel) reproduces serial first-max-wins tie ordering exactly — memcmp-enforced in tests across adversarial tie/denormal/topp-edge cases and expert counts 24/168/200/256/257 (out-of-contract auto-falls back to serial).
- **Experiments:** `COLI_METAL_RESSET` (macOS 15 `MTLResidencySet` replacing per-CB `useResource:` — the E5 experiment fully documented in `SUMMARY.md`), `COLI_METAL_SPIN` keep-alive (GPU down-clock probe).
- **M5 Max report:** the OMP hot-team active spin **steals the shared SoC power budget** and throttles the GPU (attention kernel time 3×, 76→223 s, identical dispatches); the winning config is `COLI_NO_OMP_TUNE=1 PIPE=1 PIPE_WORKERS=8` → **2.24 tok/s** (+8.5% over the old base). Confirmed run-to-run argmax non-determinism under threaded FP reassociation — the "token-exact" claim holds under serial validation configs.

### 10.4 Multi-GPU: the 6×5090 experiment (`docs/experiments/glm52-6x5090-2026-07-12.md`)
- PCIe-star topology (no NVLink), dual Xeon, 251 GiB RAM. Auto-placement (`CUDA_EXPERT_GB=auto PIN_GB=all RAM_GB=auto`) landed **9,343 experts in VRAM (176.7 GB across 6 cards) + 10,113 in RAM — zero disk during decode**.
- Optimization ladder: 0.12 (partial residency) → 2.30 (fixed placement) → 5.77 (full residency) → 6.00 (REPIN=16) → +39.6% from `OMP_PROC_BIND=spread` at 24 physical cores → one prefill routing-correction pass → **6.28 tok/s** (96-token greedy), **6.84** at 256 tokens. Beat vLLM-Moet on the same box (best 2.6 tok/s) because vLLM replays the whole step on any expert miss.
- Rejected-with-data: second prefill correction pass (overfits prompt routing), lazy demotion, D2H recovery, CPU/GPU overlap threads, OpenMP restructuring (kills NUMA locality), THP/interleave variants, and **next-layer expert prediction with GPU staging** — real signal (70–79% recall) but PCIe staging contended with expert/attention streams for a net loss; "revisit with dedicated streams."
- **MTP structurally loses at full residency on this MoE:** verify positions route to mostly different experts, so per-unique-expert cost scales ~linearly with S; even a 79%-acceptance int8 head was −5%. Inverts if TC grouped GEMM makes S=4 ≈ S=1 — an explicitly flagged future direction.

---

## 11. CPU platform layer

### 11.1 SIMD coverage
AVX2, AVX-512F/BW, AVX-512 VNNI, 128-bit AVX-VNNI, ARM NEON, NEON+DOTPROD (SDOT), ARM i8mm (SMMLA), POWER VSX — all in `quant.h` with a reported `IDOT_KERNEL` banner string; portable scalar fallbacks keep PowerPC (validated token-exact on POWER8) and generic builds working. Reassociation policy is explicit per call-site (accepted where softmax follows; strictly ordered AXPY where bit-identity matters).

### 11.2 OpenMP self-tuning
See §3.4 (env seeding + one-shot self-re-exec + affinity reset + Metal/CUDA exemptions).

### 11.3 Portability layer (`c/compat.h` — "every platform difference lives HERE")
- **Windows (MinGW-w64):** compile-time `#error` without `_FILE_OFFSET_BITS=64` (a 32-bit off_t silently wraps >4 GB offsets → wrong weights → silent token corruption); `compat_pread` via OVERLAPPED ReadFile (thread-safe, 64-bit, 2 GB chunking, per-thread real GetLastError preservation — #307); WILLNEED emulated by fire-and-forget overlapped reads into scratch (populates the standby cache; never called inline on the hot path); `VirtualLock` + working-set growth; `_aligned_malloc`/`compat_aligned_free`; `MoveFileEx` rename-replace; `GlobalMemoryStatusEx` meminfo; `FILE_FLAG_NO_BUFFERING` O_DIRECT with the same 4K contract; `GetFileSizeEx` (CRT lseek fails on unbuffered fds); **`getenv_utf8`** via the wide environment (the ANSI codepage corrupted Cyrillic/CJK prompts before the byte-level tokenizer saw them).
- **macOS:** `F_RDADVISE` readahead; DONTNEED no-op (XNU has no per-range drop); `F_NOCACHE` direct mode.
- Audited by dedicated test binaries (`audit_win_shims.c` proves rename-over-existing and a 5 GiB-offset pread on a sparse NTFS file; `test_compat_direct.c` proves misaligned direct reads fail rather than corrupt).

---

## 12. Tokenizer (`c/tok.h` + generated Unicode tables)

- Pure-C **byte-level BPE**, a faithful replica of HF `tokenizer.json` semantics (`ignore_merges=true`, ByteLevel without prefix space, added-token atomicity, merge rank = list order), using the standard GPT-2 byte↔unicode table.
- **Two pre-tokenizer families auto-detected:** the **cl100k** regex (GLM-5.2) and the **o200k** regex (GPT-4o family) — recognized because o200k's Split pattern contains `\p{Lu}` case classes. Both are hand-compiled regex engines over codepoint arrays; the o200k one "replays the regex engine's backtracking order exactly" for case-aware letter runs.
- **Special vs added tokens:** `special:true` entries (role markers, `<sop>`…) are control tokens that feed the stop set and can never leak as text; `special:false` (`<think>`, `<tool_call>`) are renderable.
- **Load hardening** (untrusted tokenizer.json): 1 GiB size cap, non-negative numeric ids (a negative id = OOB write), max-id cap `1<<21` (an id near INT_MAX overflowed the calloc).
- Unicode tables are **generated** (`tools/gen_unicode.py` iterates all 0x110000 codepoints via Python `unicodedata`) into binary-searched range arrays (`uni_L/N/S`; o200k adds `Lu+Lt` and `Lm+Lo+M` classes).
- Tests validate against HF-produced oracle files, including a synthetic o200k tokenizer fixture.

---

## 13. Command-line interface (`c/coli`, 1,023 lines — Python, no extension)

Global behavior: raises RLIMIT_NOFILE to 65536 (144+ shards), UTF-8 stdout on Windows, ANSI pixel-art hummingbird banner, engine discovery (`COLI_ENGINE` → `./colibri` → `./glm` → installed libexec).

| Command | Function |
|---|---|
| `coli build` | `make -C c colibri` |
| `coli info` | config/shard/RAM/engine summary |
| `coli plan [--json]` | resource plan (see §17.2) |
| `coli doctor [--json]` | diagnostics (see §17.1) |
| `coli run "prompt"` | one-shot generation with the GLM chat template |
| `coli chat` | REPL; **attach mode** auto-detects a running server (warm engine: 4%→55% hit, ~10×) or spawns a private engine over the legacy protocol; `:reset`/`:more`; two-stage Ctrl-C; streaming-markdown renderer with split-marker hold-back; extensive documented Windows pipe rules; OOM-kill forensics in `engine_diag()` |
| `coli serve` | pidfile + HTTP gateway |
| `coli stop` | pidfile + `/proc`-scan kill of demonstrably-ours processes (the engine self-execs and is named `exe`; `pkill -x glm` once left double ghost engines → OOM) |
| `coli web [--no-browser]` | serve + browser auto-open when `/health` answers (up to 20 min poll — "the 744B engine takes minutes to load") |
| `coli bench [tasks…]` | auto-downloads eval datasets, runs `eval_glm.py` |
| `coli convert` | two converter passes: main model + **MTP head always ≥ int8** (#8) |

Common flags on every subcommand: `--model --ram --auto-tier --ctx --gpu --vram --policy --repin --cap --ngen --topp --topk --temp`. `env_for()` centralizes measured Windows defaults (`DIRECT=1`, `PIPE=1`, `PILOT_REAL=1` — all setdefault, all lossless), the Windows OMP block, plan application, and the Windows CUDA auto-enable, plus hard errors for `--gpu` against a CPU-only binary.

Packaging: `pip install -e .` (`colibri-engine`; console script `coli` is a `runpy` shim onto `c/coli`; version singled-sourced from `c/version.py`, #394). Extras: `convert`, `oracle`, `bench`.

---

## 14. Web dashboard (`web/` — React 18 + TS + Vite + Tailwind v4, no router, no chart libs)

### 14.1 Chat view
Sidebar (connection probe, API key memory-only by policy, runtime hwinfo, scheduler 2×2 grid, **stacked expert-tier bar** over all 19,456 experts, session token stats), model + **KV-session** selectors, temperature/max-tokens/Reasoning toggle; topbar live badges (flashing token counter, tok/s, TTFT, usage, queue-wait, slot); per-slot conversations (in-memory only — no persistence, a stated limitation); Enter/Shift+Enter with IME awareness; Stop button aborts the stream; plain-text rendering (no markdown — limitation); auto-connect when served by the engine itself (port-5173 heuristic).

### 14.2 Brain view — live expert-cortex visualization
A canvas grid with **one cell per expert** (76 rows × 256 cols = 19,456 for GLM-5.2, incl. the MTP row). Polls `GET /experts` every 1.5 s: per-expert byte = 2-bit tier (disk/RAM/VRAM → base color) + 6-bit log₂ heat (luminance), plus a hits bitmask — freshly routed experts **flash white and decay** (per-frame ×0.94) so you "watch the model think." Hover tooltips show real layer/expert/tier/heat and — when the published **expert atlas** `experts.json` is present — measured specialist labels, entropy, and top-3 topic affinities; otherwise a depth-based heuristic role description.

### 14.3 Profiling view
Polls `GET /profile` every 2 s for the rolling per-turn PROF window; five fixed-color phases (I/O wait, expert matmul, attention, LM head, other); stat tiles (tok/s, wall, tokens/forward batching, disk service "overlapped with compute"); 100% stacked share bars (last turn + window); two hand-rolled SVG column charts over the last 40 turns with hover linking; a detail table with a footnote explaining why disk *service* time doesn't appear in the wall stack.

### 14.4 Infrastructure
Fetch + SSE only (no WebSockets; telemetry is polling); `serverEndpoint()` strips `/v1` so `/health`/`/experts`/`/profile` resolve beside the OpenAI prefix; `cache_slot` sent only when the server advertises `kv_slots` (feature detection — the UI degrades cleanly against any OpenAI-compatible backend); SSE parser handles split frames/CRLF and `[DONE]`; i18n (en, zh-CN, zh-TW, it) via a hand-rolled provider; dark theme only; responsive to phone widths; `prefers-reduced-motion` respected; ErrorBoundary with a CSS-independent fallback; localStorage stores only baseUrl/model/locale and **deletes** any legacy stored API key.

---

## 15. Desktop application (`desktop/` — Tauri v2)

A deliberately minimal shell around the same web build: 12 lines of Rust total, **no commands, plugins, sidecars, process spawning, or model downloads** — the README's rationale: "the model is hundreds of gigabytes and must remain an external, user-selected resource rather than an opaque application sidecar." Window 1280×820; capability set is only `core:default`; CSP restricts `connect-src` to localhost endpoints (a real functional difference vs the browser). Engine lifecycle management and downloads are explicitly deferred future work. Mobile entry-point attribute present for potential mobile builds.

---

## 16. Project site (`site/index.html` — one self-contained file, zero deps)

Animated 3-D **expert-atlas galaxy** hero (the real measured atlas: 1,358 experts embedded as data, positioned by 10-topic affinity blend on a sphere); a "watch it think" demo that is an **honest replay** (fixed transcript paced at *measured* community decode rates, with an on-page disclaimer and issue links — "nothing here is invented") synchronizing a fake terminal, a Brain-style heat canvas, and the galaxy; the measured hardware ladder table (each row linked to its GitHub issue); model status (GLM-5.2 live, OLMoE live; Kimi K2/Qwen3/MiniMax planned); manifesto ("Frontier models should not be sealed inside datacenters"; the engine as a "microscope" — first published expert atlas of a 700B-class model). Deployed by `site.yml` to GitHub Pages.

---

## 17. Diagnostics, planning & ops tooling

### 17.1 `coli doctor` (`c/doctor.py`)
Read-only checks (pass/warn/fail/skip, JSON-able, schema-versioned): model path/config/tokenizer, persistence-dir writability, engine binary, a CUDA matrix (requested GPUs × detection × **linkage** — on Windows it detects DLL-mode builds by scanning the binary for a baked marker string, since LoadLibrary leaves no import-table entry), shard header validity, disk space, RAM budget feasibility, and plan warnings.

### 17.2 `coli plan` (`c/resource_plan.py`)
Dependency-free hardware detection (safetensors header parsing; per-OS "reclaimable without swapping" memory probes; nvidia-smi GPU discovery incl. unified-memory `[N/A]` handling; **physical**-core counting with declared ctypes signatures — a silent fallback to 1 once pinned decode to one core, #325) → tier plan (dense + runtime + KV budgeting with the 2.5 GB page-cache reserve, VRAM hot tier needing no RAM backing, projected hit rate, bottleneck classification disk/mixed/compute/memory) → **auto-tune env emission with reasons** (`DRAFT=0` when compute-bound per #389 or low-hit disk-bound per #467; `COLI_CUDA_PIPE`; `PIPE`; `COLI_NUMA`; `COLI_NO_OMP_TUNE` under Metal; `PIN_GB=all` when fully resident) — applied via `setdefault` only (user env always wins; deliberately never sets OMP affinity vars, #325).

### 17.3 Ops scripts
`scripts/run.sh` (WSL bring-up incl. 9p refusal), `scripts/supervisor.sh` (flock-singleton conversion babysitter that kills zombie downloads stalled >180 s), `setup.sh` (per-OS dependency checks + build + tiny-oracle self-test + RAM report), `warmup.ps1` (overnight cache priming across 30 topic-diverse prompts — single-topic warming overfits the pin; NGEN=32 because usage saves only on clean completion).

---

## 18. Telemetry, monitoring & profiling

- **`PROF=1`:** startup config header; per-forward latency ring with p50/p90/p99/max; expert I/O GB and MB/token; hit split pin/LRU; read-service vs *felt* wait; phase time shares; a P0 execution split (CPU GB/s, GPU critical path, straggler ratio, router, P2P hops, orchestration); and a **plain-language verdict naming the knob to turn** (I/O-bound → RAM_GB/PIPE/DIRECT; compute-bound → cores/IDOT/GPU; attention-bound → CTX/DSA). Additive-only guarantee: with PROF unset, output is byte-identical.
- **DISK-CLASS:** cold/warm classification of every expert load against a *private* clone of the recency clock — deliberately isolated so "byte-identical with PROF=0" is provable by construction; calibration coordinates documented in comments.
- **Dashboard protocol** (`telemetry.h`): `HWINFO`, `TIERS`, `EMAP` (the brain map), `HITS`, `PROF`, `ENTROPY` (per-layer routing entropy), `GPUS`, `REPIN` events; `.coli_usage` persisted atomically every turn.
- Aux instrumentation: `STATS=<file>` usage histograms, `ROUTE_TRACE` per-position routing dumps, `DISK_SPLIT` (draft/absorb/verify byte attribution), `TOKENS=1` id dumps for exact A/B, `LOOKA` predictability tables, `MIRROR:` line, CUDA/Metal counters (`COLI_CUDA_PROFILE`, Metal moe/attention/resset stats).

---

## 19. Research tooling & implemented research ideas

### 19.1 Expert Atlas (`c/tools/expert_atlas/`, #175)
Probe-based semantic mapping of all 19,456 experts: 10 topic categories × 3 prompts; `sweep.sh` controls **four measured confounds** (TOPP hides 38% of distinct experts; speculative drafts count unemitted routing; `.coli_usage` accumulation; CUDA tier nondeterminism) plus a fifth in analysis (**autocorrelation** — one prompt = one observation; a replication gate across independent prompts removed 587 fake specialists). Results: leave-one-prompt-out validation **29/30 = 96.7%** (chance 10%); routing follows **task over language** (the single miss: a Chinese poem classified as poetry); only **7.9%** of experts are strong specialists; specialization rises with depth. Output feeds the dashboard (`experts.json`) and the site galaxy.

### 19.2 Route coupling (#176)
`route_coupling_report.py` — copula/Fréchet-bound dependence screening of cross-layer expert co-activation, plus equal-budget prefetch simulations (marginal-heat vs coupled scoring, depth 1–2, train/test transfer). `route_pairs.py` productionizes it into the `.coli_pairs` table the engine consumes (`COUPLE=`), measured +3.6..+9.4 pp prefetch recall.

### 19.3 Implemented research-paper ideas
- Cache-aware max-rank MoE routing (arXiv:2412.00099) → `CACHE_ROUTE`.
- DeepSeek-V3 MLA weight absorption; DSA lightning-indexer sparse attention; MTP speculative head.
- QuaRot/QuIP#-family rotation preconditioning + E8-lattice/IQ3_XXS codebook quantization (#81/#452/#453).
- Leviathan rejection-sampling speculative decoding with three heterogeneous draft sources, incl. the novel **grammar-as-draft-source** design (#48).
- rANS entropy coding of int4 weights with a whiteness proof (§5.4).
- Router-state next-layer prediction (PILOT, 71.6% recall) and its measured negative GPU-staging variant.

### 19.4 Performance-regression harness
`tools/efficiency.py` (25+ regexes anchored to exact engine printf strings, shared with the CUDA benchmark fixture to prevent drift) → `test_inefficiency.py` (CI-gating floors on the tiny model: tok/s, phase sanity, determinism, CUDA-actually-used, CPU/CUDA teacher-forcing agreement ≥70%) and `test_efficiency_report.py` (opt-in real-model **"optimization dossier"** — 9 sections, advisory FLAG thresholds naming concrete knobs, never fails CI). `diag_harness.py` runs a 5-phase model-qualification campaign (system, smoke prompts with repetition detection, deep diagnostics, quality eval, MTP A/B) into JSON+Markdown reports. `bench_ux.sh` measures TTFT/decode on fixed scenarios with median discipline.

---

## 20. Benchmarking & evaluation

- **`iobench`** — expert-shaped disk microbenchmark (19 MB random reads, N threads, buffered vs O_DIRECT), with page-cache-pollution methodology caveats documented (#86).
- **`eval_glm.py`** — lm-eval-harness-style log-likelihood MCQ scoring through the engine's `SCORE` mode (one forward per option — feasible at 0.05 tok/s); streams partial results; acc + length-normalized acc_norm; `[gMASK]<sop>` prefix by default; the published-reference table is an explicit TODO.
- **`fetch_benchmarks.py`** — hellaswag/arc/mmlu/winogrande/piqa/openbookqa → JSONL with atomic writes (a truncated file would block re-download forever).
- **CUDA benchmark fixture** — a 313M-param random-weight model preserving real MLA/MoE/streaming shapes; A/Bs five placement modes with rotated run order and medians.
- **`docs/benchmarks.md`** — "everything on this page is a measurement, not a promise": the reference 25 GB dev box (~11 GB reads/token cold; 0.05–0.1 tok/s), an estimate table, ~18 community datapoints with issue links, and takeaways (the RAM cap binds before disk on small machines; disk×5.8 bought tokens×2.9; GPU tier only pays when the CPU is the weak link; selective NUMA +13/+40% vs blanket interleave up to 10× worse).
- Community benchmark protocol + a dedicated **performance-report GitHub issue template** requiring commit, hardware/storage, env, commands, warm-up policy, run count, medians, and profile timings.

---

## 21. Build system & packaging

- **`c/Makefile`:** toolchain-probe platform detection (`$(CC) -dumpmachine`, not `uname` — correct under native Windows and cross-compiles, #129); per-platform flags (macOS Homebrew-libomp verification; MinGW `-static -lpsapi` + `x86-64-v3` default; *BSD explicit `-pthread`; PowerPC `-mcpu`); GPU knobs `CUDA=1 / CUDA_DLL=1 / HIP=1 / METAL=1` with `CUDA_ARCH=portable` fat binaries (sm_80→120 + PTX) and `NVCC_CCBIN`/`ROCM_HOME`/`HIP_ARCH` overrides; a **`.build-config` stamp** forcing relink on flag changes (a stale CPU-only binary once shipped silently, #306/#478 — written via GNU Make's `$(file)` because `printf` doesn't exist under cmd.exe); ~40 targets incl. per-test binaries, `portable`, `check` (= clean + portable + full tests), `install` into `libexec/colibri`, cross-shell `clean`/`test` via Python helpers.
- **Nix flake:** pinned nixpkgs, package build with `make test-c` as check phase, wrapped `coli` with bundled engine, dev shell; carries stale pre-rename paths (a latent `apps.glm` bug) and a stale version string — noted drift.
- **Docker:** debian-slim, **clones the repo at build time** (unpinned — reproducibility limitation), builds via setup.sh; no ENTRYPOINT (user passes `./coli chat`); beginner README with WSL2 memory tuning and an honest 0.01–0.04 tok/s no-GPU datapoint.
- **Releases (`release.yml`):** three-platform matrix (Linux x86-64-v3 / macOS arm64 / Windows MSYS2), plain-named engine in the archive (versioned names broke the launcher), a **behavioral verification step** (unpack + run `coli info` in a clean dir — caught the v1.1.0 "colibrì-banner" failure), `SHA256SUMS.txt` (#530, part of the antivirus-false-positive response), release notes auto-extracted from CHANGELOG by tag.
- Install scripts `install.sh`/`install.ps1` at repo root; prebuilt releases need no compiler (Python 3 only for launcher/gateway).

---

## 22. CI (`.github/workflows/`)

| Workflow | Jobs |
|---|---|
| `check.yml` | `make check` on Linux, Windows (MSYS2 UCRT64 — "the job that would have caught #68/#137"), macOS; uploads `colibri.exe` as an artifact so antivirus reports are verifiable per-PR (#527/#532) |
| `ci.yml` | native engine build+tests; **CUDA syntax-only** compile (nvcc 12.6.2, sm_80 — with the famous comment about the old `| head -40` that masked all errors: "a check that cannot fail is worse than no check"); **HIP syntax-only** (rocm/dev container, gfx1100, disk-freeing preamble); **Windows CUDA DLL build** (windows-2022 pinned — newer images ship a VS version CUDA rejects; covers the toolchain nothing else touches); web build+vitest; Python unittest |
| `release.yml` | §21 |
| `site.yml` | GitHub Pages deploy of `site/` |

Notable absences (acknowledged): no sanitizer/fuzz/coverage jobs; GPU jobs are compile-only (no hosted GPUs) — kernel behavior relies on maintainer/community hardware.

---

## 23. Testing infrastructure

- **Categories:** ~24 dependency-free C unit-test binaries (many compile the *entire engine* into the test TU via `#define main …/#include "../colibri.c"`); GPU correctness tests (CUDA/HIP/Metal, run on real hardware; `gpu-compile` for CI); stdlib-only Python unittest suite (server, Anthropic contract, e2e tools over a mock engine speaking the real wire protocol, CLI, doctor, plan, env defaults, converters, **and the Makefile itself** — `test_makefile_platform.py` dry-runs `make -n` with injected triplets); the efficiency suite (§19.4); micro-benchmarks explicitly excluded from gates.
- **Oracle strategy:** `make_glm_oracle.py` builds a true-architecture tiny GLM (with a **transformers ≥5.11 hard gate** — older versions' split-half RoPE produced a silently-wrong oracle, #281; `--fp8` computes the reference *after* the FP8 round-trip so it matches what the converter ingests); `SNAP=./glm_tiny TF=1` → 32/32 is the canonical gate; medium bench model for CPU/CUDA A/B; real-OLMoE oracle + a torch-free bootstrap variant.
- **Noteworthy test engineering:** bit-exactness demanded of integer kernels ("any float bit mismatch is a driver bug, not rounding"); adversarial format-gate tests (int3 vs grouped-int4 byte-count ambiguity); Metal router bitwise-tie suites incl. a lane-straddling E=200 case; corruption/truncation fuzz for the rANS codec under ASAN; fault-injection for GPU fallback lifecycles; sparse-file 5 GiB pread audits; regression tests that encode measured *quality* claims (int3-beats-int4-on-outliers).

---

## 24. Documentation inventory

16 docs + 4 READMEs + per-directory READMEs; highlights: `quickstart.md` (zero-to-running, 3 OSes), `tuning.md` (knob cookbook with measured effects), `ENVIRONMENT.md` (**~130 env vars, generated by scanning every `getenv()` site**, with defaults and rationale), `SETTINGS.md` (generated CLI reference), `api.md` (endpoints + coding-CLI recipes for aider/crush/Continue + the brutal-prefill warning), `serve_protocol.md` ("if this document and the code disagree, the code wins"), `grammar-draft.md`, `CACHE_ROUTE.md`, `cuda.md`/`metal.md`/`windows.md` (incl. Smart App Control and MSYS2 traps), `benchmarks.md`, `MAINTAINING-DOCS.md` — a documented, AI-assistant-oriented regeneration procedure for the generated docs ("truth lives only at `getenv()` sites… If a knob isn't at one of those call sites, it isn't real"), with a proposed-but-unimplemented CI drift check. READMEs in English, Italian, Simplified and Traditional Chinese. `SUMMARY.md` is *not* a summary — it's the E5 Metal residency-set experiment write-up, showcasing the project's validator-round/uncertainty-labeling process culture.

---

## 25. Configuration surface (summary)

- **~130 environment variables** across: core/model (`SNAP`, `COLI_MODEL_DIRS`, `REF`, `MTP`, `DSA`, `COLI_POLICY`), quantization/kernels (`IDOT`, `I4S`, `XEXP`, `I4_ACC512`, `SPEC_PIN`, `NOPACK`), sampling (`COLI_TEMP`, `NUCLEUS`, `SEED`, `CTX`, `NGEN`, `THINK`, `CHAT_TEMPLATE`), speculation (`DRAFT`, `GRAMMAR`, `SCHEMA`, `GRAMMAR_DRAFT`, `MTP_*`), I/O (`PIPE`, `PIPE_WORKERS`, `URING`, `DIRECT`, `DROP`, `COLI_MMAP`, `COLI_NUMA`, mirror/split vars), prefetch (`SPEC`, `PILOT*`, `COUPLE*`, `PREFETCH`), memory (`RAM_GB`, `PIN*`, `AUTOPIN`, `REPIN`, `MLOCK`, `CAP_RAISE`, `RSS_GUARD_GB`, `COLI_RAM_OVERCOMMIT`), routing research (`CACHE_ROUTE`, `ROUTE_*`, `EXPERT_BUDGET*`, `LOOKA`), serve (`SERVE`, `SERVE_BATCH`, `KV_SLOTS`, `KVSAVE`), modes/telemetry (`SCORE`, `TF`, `REPLAY`, `PROF`, `STATS`, `TOKENS`, `DISK_SPLIT`), ~25 CUDA vars, 6 Metal vars, OMP-tuning vars, and Python-side server vars (`COLI_API_KEY`, `COLI_MAX_QUEUE`, `COLI_TOOL_SALVAGE`, `COLI_DEBUG`, …). All catalogued in `docs/ENVIRONMENT.md`.
- **CLI flags** map onto these (documented in `SETTINGS.md`); the HTTP server adds its own argparse surface.

---

## 26. Hidden & less obvious capabilities

1. **The antivirus easter egg (#527):** `GrDraft` deliberately has *no* static initializer — a 107 KB near-zero struct in `.data` looked like a packer payload to Windows Defender's ML heuristics; the fix shrank the Linux binary 22.5% and is memorialized in comments, the changelog, and a CI artifact-upload countermeasure.
2. **Self-re-exec for OpenMP tuning** (§3.4) — the binary appears in `ps` as `exe`, which broke naive `pkill` and is handled by `coli stop`'s forensic process matching.
3. **`TEMP` vs `$TEMP`** (#509): the sampling alias is only honored if purely numeric; a numeric TEMP also crashes ROCm's comgr, worked around by conditional `unsetenv` inside the CUDA backend.
4. **EXPERT_BUDGET quarantine** — a complete feature shipped disabled behind a double opt-in with an empirical eulogy in `main()`.
5. **DISK-CLASS private clocks** — profiling instrumentation architected so byte-identity with profiling off is provable *by construction*.
6. **`.coli_usage` cross-session learning** and confidence-scaled AUTOPIN — the engine literally gets faster the more you use it.
7. **KV-resume language bleed** — resuming persisted KV mid-conversation carries conversational state (the Italian-session war story), hence the explicit resume notice.
8. **Grammar drafts as an *economics* play** — in a disk-streaming MoE, forced JSON syntax spans convert into disk reads avoided, not FLOPs saved.
9. **o200k tokenizer support** — the engine quietly supports GPT-4o-family vocabularies, auto-detected, ahead of any shipped model that needs it.
10. **The mux protocol's additive grammar field** — 6-field and 7-field `SUBMIT` headers are both valid, giving forward/backward wire compatibility.
11. **WSL/9p detection** via statfs magic, with an honest "fadvise is a no-op here" warning.
12. **`coli chat` attach mode** — transparent client/daemon topology switching based on a millisecond health probe.
13. **Deterministic mirror routing** — the dual-SSD read split is a pure hash so prefetch and demand reads always hit the same drive's page cache.
14. **Metal `r_top8_par`** — a parallel GPU kernel contractually **bitwise-identical** to serial CPU routing, enforced by memcmp in tests.
15. **CFSE codec** — a complete, fuzz-hardened weight-compression container sitting in-tree awaiting engine integration.

---

## 27. Limitations & future work (as stated in the repository)

**Engine:** single architecture family (`n_group==1` enforced); no KV quantization or paged KV; no repetition/frequency penalties or token top-k; speculation (MTP/n-gram) disabled in the multiplexer; grammar drafts greedy-only in mux; fmt 5/6 have no GPU kernels; `matmul_i3` x86 SIMD and int3-IDOT are stated follow-ups; URING is Linux-only and mmap-incompatible; DSA selection assumes full windows; Metal fused paths hardcode GLM-5.2 dims (REAP E=168 admission contemplated); CUDA ragged attention lacks fmt 4 group scales; experts never shard across devices.
**Ecosystem:** CFSE not wired into loading; downloader integrity is size-only; hardcoded personal paths in download tools; `eval_glm.py` reference table empty; Python dispatcher violates the documented ignore-unknown-lines rule; `serve_protocol.md` documents frames the server doesn't emit; web chat lacks markdown/persistence; desktop shell defers engine lifecycle; Docker unpinned; Nix flake carries pre-rename drift; `PIPE` default documented inconsistently; no CI sanitizers/fuzzing/GPU execution.
**Roadmap (README/site):** smarter placement beyond LRU+pin, CPU/GPU expert-compute overlap, routing-aware speculation, tensor-core grouped GEMM to invert the MTP-at-residency economics, numerics-matched integer GPU kernels for cross-backend token identity, rocWMMA, and new model families (Kimi K2, Qwen3 MoE, MiniMax).

---

## 28. Version history (CHANGELOG)

- **v1.0.0** (2026-07-19) — first tag after ~a month in production: 3-tier placement, CUDA multi-GPU, Metal, MTP speculation, OpenAI API + dashboard, cross-platform incl. PowerPC, auto-tune, token-exact oracles, persistent KV, DSA, PILOT, PIPE/URING, NUMA, the full SIMD ladder, int2/int4/int8/grouped formats, 30+ community datapoints.
- **v1.1.0** (2026-07-22) — "community release" (27 PRs, 20+ contributors, 216 commits): HIP/ROCm, dual-SSD mirror + N-drive split, int3-g64 (fmt 5) and E8 lattice (fmt 6), tool-calling fixes (#401 trilogy), fmt 4 CUDA fix, security hardening for untrusted mirrors (#368/#413), +4.7× MLA-absorb / +13% AVX-512 / +11.6% XEXP / +5.5% VNNI performance work, `glm.c`→`colibri.c` rename, serve stage 2 (response_format, per-request grammars).
- **v1.1.1** (2026-07-23) — the `.data`-blob antivirus fix (−22.5% binary), the Anthropic Messages API, `SHA256SUMS.txt`, per-PR Windows artifact uploads.
