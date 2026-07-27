# Kronos — Complete Technical Teardown

Analysis-only teardown of [shiyu-coder/Kronos](https://github.com/shiyu-coder/Kronos).
**No Kronos code is used in Olympus** — this is source-level competitive/technical
analysis, produced by cloning and reading the actual repository, running its code
paths locally, and probing its documented commands.

- **Analysed at commit:** `67b630e` (`master`, authored 2026-04-13)
- **Analysis date:** 2026-07-27
- **License:** MIT (© 2025 ShiYu)
- **Paper:** [arXiv:2508.02739](https://arxiv.org/abs/2508.02739), accepted AAAI 2026
- **Size:** ~11.2k lines of Python across 31 files, 76 commits, single `master` branch
- **Verification method:** repository cloned; `model/` instantiated and executed
  locally with PyTorch 2.13 using randomly-initialised weights to confirm control-flow
  claims; documented CLI invocations run to confirm import/dependency behaviour.

> **Evidence convention.** Statements are marked **[C]** = confirmed by reading or
> running the code (file:line cited), **[R]** = reproduced locally by executing it,
> or **[I]** = inferred (reasoning stated). Everything in §12–§14 that is marked
> **[R]** was actually run.

---

## 1. What Kronos is, in plain English

Kronos is a **pretrained "foundation model" for candlestick charts**. It treats a
sequence of OHLCV bars the way a language model treats a sentence: it first
*compresses each bar into a discrete token* using a learned quantiser, then runs a
*decoder-only Transformer* over those tokens to autoregressively generate the tokens
of future bars, then *decodes those tokens back into numbers*.

Concretely, you hand it a table with columns `open, high, low, close, volume, amount`
plus timestamps, tell it how many future bars you want, and it hands you back a table
of the same six columns for those future timestamps.

Because generation is **sampled** (temperature / top-p / top-k) rather than
deterministic, the same input can produce many different futures. Kronos exposes that
directly: `sample_count=N` draws N independent forecast paths and averages them.

Two things it is **not**:

- It is **not a trading system.** There is no order management, no position sizing,
  no risk model, no live data feed, no broker integration. The repo's own README
  says the backtest pipeline "is a simplified example and not a production-ready
  quantitative trading system" (`README.md:221`). **[C]**
- It is **not an installable package.** There is no `setup.py`, no `pyproject.toml`,
  no `__init__.py` at the repo root, no CI. You clone it and run scripts from inside
  specific directories. **[C]**

### The problem it is designed to solve

General-purpose time-series foundation models (Chronos, TimesFM, Moirai, …) are
trained on broad, mostly low-noise series and forecast a **single univariate**
channel. Financial K-lines are different: extremely low signal-to-noise, heavy tails,
regime shifts, and **jointly-constrained multivariate** structure (open/high/low/close
are not independent; volume co-moves with volatility). Kronos's bet is that a
domain-specific *tokenizer* that quantises the whole OHLCV vector into a single
hierarchical discrete symbol, plus a decoder-only Transformer over those symbols,
handles this better than continuous-valued generic TSFMs — and gives you a
*distribution* over futures for free, because sampling tokens is native.

### Intended users

| User | What they get | Where |
|---|---|---|
| **Quant researchers** | Zero-shot multivariate K-line forecasts as a signal source; a Qlib backtest harness to test them | `model/`, `finetune/qlib_test.py` |
| **ML researchers** | A reference implementation of BSQ tokenisation + hierarchical dual-head autoregression for finance; a reproducible regression test | `model/`, `tests/` |
| **Practitioners with private data** | Two fine-tuning pipelines (Qlib-backed and plain-CSV) | `finetune/`, `finetune_csv/` |
| **Casual/demo users** | A Flask+Plotly web UI and a Tkinter desktop GUI for point-and-click forecasting | `webui/`, `examples/prediction_new_GUI.py` |

---

## 2. Repository architecture map

```
Kronos/
├── model/                          ← THE PRODUCT. Everything else is scaffolding.
│   ├── __init__.py                 exports; plus dead model_dict/get_model_class
│   ├── module.py       (570 LOC)   BSQ quantiser, RoPE attention, RMSNorm, SwiGLU FFN,
│   │                               HierarchicalEmbedding, DependencyAwareLayer, DualHead,
│   │                               TemporalEmbedding
│   └── kronos.py       (662 LOC)   KronosTokenizer, Kronos (LM), sampling utils,
│                                   auto_regressive_inference(), KronosPredictor
│
├── finetune/                       ← Pipeline A: Qlib / China A-share, multi-GPU DDP
│   ├── config.py                   single Python-class config (all knobs, hardcoded paths)
│   ├── qlib_data_preprocess.py     Qlib → per-symbol DataFrames → 3 pickles
│   ├── dataset.py                  QlibDataset: random sliding windows, causal normalisation
│   ├── train_tokenizer.py          stage 1: DDP tokenizer finetune (torchrun)
│   ├── train_predictor.py          stage 2: DDP predictor finetune (torchrun)
│   ├── qlib_test.py                batch inference → 4 signals → Qlib TopkDropout backtest
│   └── utils/training_utils.py     DDP setup, seeding, param counting, time formatting
│
├── finetune_csv/                   ← Pipeline B: single CSV file, community-contributed
│   ├── config_loader.py            YAML → ConfigLoader → CustomFinetuneConfig
│   ├── configs/*.yaml              declarative config w/ {exp_name} path templating
│   ├── finetune_tokenizer.py       stage 1 (+ file logging, optional random-init)
│   ├── finetune_base_model.py      stage 2 + CustomKlineDataset
│   ├── train_sequential.py         orchestrator: stage 1 → stage 2, --skip-* flags
│   └── data/HK_ali_09988_...csv    93,912-row 5-min HK sample dataset (shipped)
│
├── webui/                          ← Flask + Plotly single-page forecasting UI
│   ├── app.py          (708 LOC)   5 REST endpoints, chart builder, result persistence
│   ├── templates/index.html (1238) SPA front end
│   ├── run.py / start.sh           launchers with dependency self-check
│   └── prediction_results/*.json   29 committed forecast artefacts (2025-08-26)
│
├── examples/                       ← 8 scripts + a sub-project; wildly uneven quality
│   ├── prediction_example.py       canonical README example (80 LOC)
│   ├── prediction_wo_vol_example.py   OHLC-only variant
│   ├── prediction_batch_example.py    predict_batch demo (hardcoded /home/csc/ paths)
│   ├── prediction_cn_markets_day.py   akshare CLI, ±10% limit-up/down clamp
│   ├── get_date_new.py, get_akshare_date_*.py   4-way A-share data scrapers
│   ├── prediction_akshare_2024-2025.py, prediction_new.py (1332 LOC)
│   ├── prediction_new_GUI.py  (1624 LOC)  Tkinter desktop app
│   ├── run_backtest_kronos.py     CSV-driven single-stock backtester
│   └── yuce/                      committed outputs + a backtester that never calls Kronos
│
├── tests/                          ← 2 pytest tests, both require network + HF download
│   ├── test_kronos_regression.py   bit-exact + MSE regression vs pinned HF revisions
│   └── data/                       input CSV + 2 golden-output CSVs + regenerator
│
├── figures/                        README images
├── requirements.txt                8 pins — incomplete (see §9)
└── README.md                       338 lines; the only top-level documentation
```

**Architectural shape:** a thin, clean, well-factored core library (`model/`,
~1.2k LOC) surrounded by four *mutually independent, non-communicating* application
layers. `finetune/` and `finetune_csv/` duplicate the same two-stage training logic
with no shared code. `webui/` and `examples/` duplicate data-loading and plotting.
There is no shared config system, no shared data abstraction, and no shared CLI.

---

## 3. How the system works, input → output

### 3.1 The model in three stages

**Stage 1 — Tokenizer (`KronosTokenizer`, `model/kronos.py:13-177`)** **[C]**

A Transformer autoencoder with a **Binary Spherical Quantizer (BSQ)** bottleneck
(the technique from [arXiv:2406.07548](https://arxiv.org/pdf/2406.07548.pdf); the
docstring at `module.py:48` says the official implementation is reused).

```
x [B,T,6]  →  Linear(6→d_model)  →  (n_enc_layers-1) × TransformerBlock
           →  Linear(d_model → s1_bits+s2_bits)
           →  L2-normalise  →  sign()  →  ±1 bits (straight-through estimator)
           →  bits packed into two integer tokens: s1 (coarse) and s2 (fine)
```

- Quantisation is **parameter-free**: the "codebook" is the corners of a hypercube on
  a sphere, so a 20-bit code addresses 2²⁰ ≈ 1.05M values without storing 1M vectors
  (`BinarySphericalQuantizer.quantize`, `module.py:82-88`). **[C]**
- The straight-through trick is `z + (zhat - z).detach()` (`module.py:88`) — forward
  uses hard ±1, backward passes gradients through unchanged. **[C]**
- Training loss = commitment loss + an **entropy penalty** that pushes per-sample codes
  to be confident while pushing the aggregate code distribution to be uniform
  (`soft_entropy_loss`, `module.py:131-155`). The codebook entropy is *approximated*
  by splitting the code into `group_size`-bit subgroups — exact entropy over 2²⁰ codes
  is intractable, so it sums per-group entropies (`module.py:150-155`). **[C]**
- `DifferentiableEntropyFunction` (`module.py:10-36`) implements a hard-entropy
  alternative with a hand-written backward pass; it is only reachable when
  `soft_entropy=False`, which is never set anywhere in the repo. **Dead code.** **[C]**
- Decoding: `indices → bits → Linear → (n_dec_layers-1) × TransformerBlock →
  Linear(d_model→6)` (`decode`, `kronos.py:161-177`). **[C]**
- `forward()` decodes **twice** — once from `s1` bits only (`z_pre`) and once from the
  full code (`z`) — so training can supervise the coarse token to be independently
  meaningful (`kronos.py:98-113`). This is what makes the hierarchy work. **[C]**

**Stage 2 — Predictor (`Kronos`, `kronos.py:180-328`)** **[C]**

A decoder-only Transformer over the token stream:

```
(s1_ids, s2_ids) → HierarchicalEmbedding → + TemporalEmbedding → token dropout
                 → n_layers × TransformerBlock(RMSNorm, RoPE-MHA, SwiGLU FFN)
                 → RMSNorm
                 → head.proj_s1  ⇒ s1_logits          (coarse token)
                 → DependencyAwareLayer(context, emb(s1)) → head.proj_s2 ⇒ s2_logits
```

- **HierarchicalEmbedding** (`module.py:400-443`): separate embedding tables for the
  coarse and fine token, concatenated and fused by a `Linear(2d→d)`. It accepts either
  a `(s1_ids, s2_ids)` pair or a single composite id which it bit-splits via
  `split_token` (`module.py:417-428`). **[C]**
- **DependencyAwareLayer** (`module.py:446-462`): cross-attention where the *query* is
  the embedding of the just-chosen `s1` token and key/value are the Transformer
  context. This is what makes `s2` conditional on `s1` — a factorised
  `P(s1|ctx)·P(s2|s1,ctx)` instead of a flat 2²⁰-way softmax. **[C]**
- **TemporalEmbedding** (`module.py:536-562`): five calendar embeddings
  (minute/hour/weekday/day/month) summed and added to the token embedding.
  `learn_te=False` gives fixed sinusoids; `True` gives learned tables. **[C]**
- All blocks are pre-norm with **RMSNorm**, **RoPE**, and **SwiGLU** — a modern
  LLaMA-style stack, not a vanilla Transformer. Attention runs through PyTorch
  **SDPA** with `is_causal=True` (`module.py:345-350`). **[C]**

**Stage 3 — Autoregressive inference (`auto_regressive_inference`, `kronos.py:389-469`)** **[C]**

```
clip → replicate batch ×sample_count → tokenizer.encode(half=True)
  → fixed-size ring buffers of length max_context
  → for i in range(pred_len):
        s1_logits, ctx = model.decode_s1(buffers, stamp_window)   # full forward pass
        s1 = sample_from_logits(s1_logits[:, -1])
        s2_logits = model.decode_s2(ctx, s1)
        s2 = sample_from_logits(s2_logits[:, -1])
        append to buffers (roll if full)
  → tokenizer.decode(last max_context tokens, half=True)
  → reshape to [B, sample_count, T, 6] → mean over sample_count → numpy
```

Notable: the buffers are **preallocated and rolled in-place** rather than
concatenated each step (`kronos.py:405-454`) — a deliberate optimisation from commit
`b62f780` ("reduce memory allocations and cpu-gpu syncs"). **[C]**

### 3.2 The user-facing wrapper

`KronosPredictor` (`kronos.py:482-661`) is the only API most users touch. `predict()`:

1. Validates the DataFrame has `open/high/low/close` (`kronos.py:524-525`). **[C]**
2. Synthesises missing columns: if `volume` absent → both `volume` and `amount` = 0;
   if only `amount` absent → `amount = volume × mean(OHLC)` (`kronos.py:528-532`). **[C]**
3. Rejects NaNs outright (`kronos.py:534-535`). **[C]**
4. Derives the 5 calendar features from the timestamps (`calc_time_stamps`,
   `kronos.py:472-479`). **[C]**
5. **Instance normalisation**: per-column z-score over the entire lookback, then clip
   to ±5 (`kronos.py:544-547`). This is why the model is scale-free across assets. **[C]**
6. Runs `auto_regressive_inference`, slices the last `pred_len` steps.
7. **Denormalises** with the same mean/std and returns a DataFrame indexed by
   `y_timestamp` (`kronos.py:556-558`). **[C]**

`predict_batch()` (`kronos.py:562-661`) does the same for a list of series, enforcing
identical lookback and `pred_len` across all of them, normalising each independently.
It is a genuine GPU batch — not a Python loop. **[C]**

Device auto-detection (cuda → mps → cpu) was added at `kronos.py:494-503`. **[C]**

---

## 4. Complete component inventory

Status legend: **PR** production-ready · **C** complete · **P** partial · **E** experimental · **D** dead/unused

### 4.A Core model library (`model/`)

| # | Item | What it does / how | Where | Inputs | Outputs | Status | Limits & deps |
|---|---|---|---|---|---|---|---|
| A1 | `BinarySphericalQuantizer` | Parameter-free quantiser: L2-normalise → `sign()` → ±1 code on a hypersphere; straight-through gradients; entropy regularisation | `module.py:39-222` | `z [B,T,D]` | `zq`, aux loss, metrics dict | **PR** | `embed_dim % group_size == 0` (asserted `module.py:58`) |
| A2 | `DifferentiableEntropyFunction` | Exact hard codebook entropy with custom backward | `module.py:10-36` | `zq, basis, K` | scalar `H` | **D** | Only reachable via `soft_entropy=False`, never set **[C]** |
| A3 | `BSQuantizer` | Wraps A1; splits the code into two halves and packs each into an integer token | `module.py:225-254` | `z`, `half`, `collect_metrics` | loss, `quantized`, indices | **PR** | `half=True` assumes `s1_bits == s2_bits` (see §12.2) |
| A4 | `RMSNorm` / `FeedForward` (SwiGLU) / `RotaryPositionalEmbedding` | Standard modern-Transformer primitives | `module.py:257-312` | tensors | tensors | **PR** | RoPE cache keyed on `seq_len` only — invalid across device/dtype change **[C]** |
| A5 | `MultiHeadAttentionWithRoPE` | Causal self-attention via PyTorch SDPA | `module.py:315-353` | `x`, optional `key_padding_mask` | `[B,T,d]` | **PR** (mask path **E**) | Passes `attn_mask` *and* `is_causal=True`; mask polarity undefined; never exercised (§12.4) |
| A6 | `MultiHeadCrossAttentionWithRoPE` | Cross-attention; `is_causal = self.training` | `module.py:356-397` | q, k, v | `[B,Tq,d]` | **P** | **Train/eval causality mismatch** (§12.3) |
| A7 | `HierarchicalEmbedding` | Two embedding tables + fusion projection; bit-splits composite ids | `module.py:400-443` | ids (tensor or pair) | `[B,T,d]` | **PR** | `split_token` added late (fix commit `a7d0d23`) |
| A8 | `DependencyAwareLayer` | Cross-attends context against the sampled `s1` embedding | `module.py:446-462` | context, sibling embed | `[B,T,d]` | **PR** | Inherits A6's causality quirk |
| A9 | `TransformerBlock` | Pre-norm RMSNorm → attn → FFN | `module.py:465-483` | `[B,T,d]` | `[B,T,d]` | **PR** | — |
| A10 | `DualHead` | Two projections + averaged CE loss; optional padding-masked loss | `module.py:486-513` | hidden states, targets | logits / loss | **PR** (mask path **D**) | `padding_mask` branch never called **[C]** |
| A11 | `TemporalEmbedding` / `FixedEmbedding` | Calendar-feature embeddings, fixed sinusoid or learned | `module.py:516-562` | `[B,T,5]` int | `[B,T,d]` | **PR** | Hardcoded cardinalities; no second/year/session features |
| A12 | `KronosTokenizer` | The stage-1 autoencoder; `encode`/`decode`/`forward`; HF Hub mixin | `kronos.py:13-177` | `[B,T,6]` float | tokens / reconstruction | **PR** | `d_in` fixed at 6 by pretrained weights |
| A13 | `Kronos` | Decoder-only LM; `forward`, `decode_s1`, `decode_s2`; HF Hub mixin | `kronos.py:180-328` | token ids + stamps | `(s1_logits, s2_logits)` | **PR** | `forward()` is **stochastic even in eval** (§12.5); `use_teacher_forcing` never used **[C]** |
| A14 | `top_k_top_p_filtering` / `sample_from_logits` | Nucleus + top-k logit filtering, multinomial or greedy sampling | `kronos.py:331-386` | logits | sampled index | **P** | top-k and top-p are **mutually exclusive**, contradicting the docstring (§12.1) |
| A15 | `auto_regressive_inference` | The generation loop: ring buffers, per-step full forward, multi-sample averaging | `kronos.py:389-469` | tokenizer, model, x, stamps | `[B,T,6]` numpy | **C** | **No KV cache** (§14.2); `pred_len < max_context` required (§12.6) |
| A16 | `KronosPredictor.predict` | End-to-end DataFrame→DataFrame forecasting | `kronos.py:519-559` | df, x/y timestamps, sampling params | forecast DataFrame | **PR** | Returns unconstrained values — no OHLC coherence, no non-negativity (§12.7) |
| A17 | `KronosPredictor.predict_batch` | Multi-series parallel forecasting, per-series normalisation | `kronos.py:562-661` | lists of df/timestamps | list of DataFrames | **C** | Requires *identical* lookback & `pred_len` across series (`kronos.py:643-646`) |
| A18 | `calc_time_stamps` | Timestamp Series → 5 calendar columns | `kronos.py:472-479` | datetime Series | DataFrame | **PR** | Requires a `.dt`-capable Series, not a `DatetimeIndex` (webui works around this, `app.py:474-477`) |
| A19 | `model_dict` / `get_model_class` | Name→class registry | `__init__.py:3-15` | string | class | **D** | Referenced nowhere **[C]** |
| A20 | HF Hub integration | `from_pretrained` / `save_pretrained` via `PyTorchModelHubMixin` | `kronos.py:13, 180` | repo id or local path | model | **PR** | Requires network on first use; `revision=` pinning supported (used in tests) |

### 4.B Qlib fine-tuning pipeline (`finetune/`)

| # | Item | What it does / how | Where | Inputs | Outputs | Status | Limits & deps |
|---|---|---|---|---|---|---|---|
| B1 | `Config` | One Python class holding ~40 knobs: paths, splits, LRs, backtest params, Comet creds | `config.py:3-131` | edit-the-file | attribute bag | **C** | No CLI/env override; ships `TODO` placeholder paths and a literal `"YOUR_COMET_API_KEY"` |
| B2 | `_set_benchmark` | Maps instrument → benchmark index | `config.py:122-131` | instrument name | ticker | **C** | Only csi300/800/1000; **raises** for anything else |
| B3 | `QlibDataPreprocessor` | Qlib → per-symbol OHLC+vol+amt frames; `amt` synthesised as typical-price×volume; drops short symbols; writes 3 pickles | `qlib_data_preprocess.py:14-121` | Qlib provider dir | `train/val/test_data.pkl` | **C** | Hard dep on `pyqlib` (not in `requirements.txt`); daily-frequency assumptions |
| B4 | `QlibDataset` | Precomputes every `(symbol, start)` window; `__getitem__` **ignores its index** and draws a random window from a seeded RNG | `dataset.py:9-123` | pickles | `(x, x_stamp)` tensors | **C** | Random sampling ⇒ sampling *with replacement*; epoch size is a config knob, not a true epoch |
| B5 | Causal normalisation | Mean/std computed on the **lookback only**, applied to the whole window | `dataset.py:109-117` | window | normalised window | **PR** | Fixed in commit `79d6d40` ("Fix data leakage in normalization window", #227). `finetune_csv` was **not** fixed (§12.9) |
| B6 | `train_tokenizer.py` | DDP stage-1 finetune: MSE(z_pre)+MSE(z)+BSQ loss, OneCycleLR, grad-clip 2.0, best-val checkpointing | `train_tokenizer.py:74-215` | pickles + pretrained tokenizer | `best_model/`, `summary.json` | **C** | `torchrun` mandatory (`:277`); unconditional `import comet_ml` (§12.10); must be run from `finetune/` (§13.1) |
| B7 | `train_predictor.py` | DDP stage-2: tokenise on the fly, next-token CE on both heads, grad-clip 3.0 | `train_predictor.py:60-179` | pickles + finetuned tokenizer + pretrained predictor | `best_model/`, `summary.json` | **C** | Same three constraints as B6 |
| B8 | Gradient accumulation | Splits each batch into `accumulation_steps` micro-batches | `train_tokenizer.py:131-148` | — | — | **P** | Does **not** enlarge the effective batch, contrary to `config.py:61` (§13.4); no `no_sync()` ⇒ all-reduce per micro-step |
| B9 | `QlibTestDataset` | Sequential (non-random) windows yielding symbol + timestamp metadata | `qlib_test.py:32-89` | `test_data.pkl` | tensors + metadata | **C** | Normalises on context only — correct |
| B10 | `generate_predictions` | Batched inference → four alpha signals (`last`/`mean`/`max`/`min` predicted close minus last close) → pivot to date×instrument | `qlib_test.py:239-295` | test pickle, models | dict of signal DataFrames | **C** | Signals are computed in **normalised space** (§12.8) |
| B11 | `QlibBacktest` | Qlib `TopkDropoutStrategy` backtest: 50 holdings, 5 dropped, 5-day min hold, ¥100M, 10bp/15bp costs, open-price fills, 9.5% limit | `qlib_test.py:96-200` | signal Series | report DataFrame + risk analysis | **C** | Hardcoded A-share assumptions; `plt.savefig("../figures/...")` overwrites a repo asset (`:199`) |
| B12 | `utils/training_utils.py` | `setup_ddp`/`cleanup_ddp`/`set_seed`/`get_model_size`/`format_time`/`reduce_tensor` | `training_utils.py:9-115` | env vars | — | **PR** | NCCL-only (`:23`) ⇒ no CPU/Gloo DDP; `reduce_tensor` unused |

### 4.C CSV fine-tuning pipeline (`finetune_csv/`)

| # | Item | What it does / how | Where | Inputs | Outputs | Status | Limits & deps |
|---|---|---|---|---|---|---|---|
| C1 | `ConfigLoader` | YAML loader with `{exp_name}` path templating; empty string ⇒ auto-generate path | `config_loader.py:6-106` | YAML path | dict | **C** | Requires `pyyaml` (not in `requirements.txt`) |
| C2 | `CustomFinetuneConfig` | Flattens YAML into attributes; back-compat for a single `epochs` key; derives all save paths | `config_loader.py:109-267` | YAML path | attribute bag | **C** | `_compute_full_paths` crashes if `base_save_path` is None |
| C3 | `CustomKlineDataset` | Single-CSV loader; derives calendar features; chronological ratio split; deterministic pseudo-random window striding (`idx*9973 + (epoch+1)*104729 mod N`) | `finetune_base_model.py:25-132` | one CSV | `(x, x_stamp)` | **C** | **Normalises over the full window including the prediction horizon** ⇒ leakage (§12.9); `fillna(method='ffill')` is deprecated pandas API (`:70`) |
| C4 | `finetune_tokenizer.py` (module) | Stage-1 trainer with rotating file logging + optional random-init from `config.json` | `finetune_tokenizer.py:151-278` | config | `best_model/`, logs | **C** | See C6 |
| C5 | `finetune_base_model.py` (module) | Stage-2 trainer, same structure | `finetune_base_model.py:239-364` | config | `best_model/`, logs | **C** | See C6 |
| C6 | DDP support | `DistributedSampler` + `DDP(...)` wrapping, all-reduced metrics | `finetune_tokenizer.py:174-176`, `finetune_base_model.py:262-264` | torchrun env | — | **Broken** | Forward is called on `model.module`, bypassing the DDP wrapper ⇒ **no gradient synchronisation** (§12.11) |
| C7 | `SequentialTrainer` | Orchestrates stage 1 → stage 2; `_setup_distributed`; `--skip-tokenizer/-basemodel/-existing` | `train_sequential.py:18-317` | YAML + flags | both checkpoints | **C** | The only entry point that actually calls `init_process_group` (`:44`) |
| C8 | Random-init ("train from scratch") | `pre_trained_tokenizer/​predictor: false` builds the architecture from a local `config.json` instead of loading weights | `finetune_tokenizer.py:308-331`, `train_sequential.py:86-224` | `config.json` | fresh model | **E** | Undocumented in the READMEs; still requires a pretrained dir to read the architecture from |
| C9 | Sample dataset | 93,912 rows of Alibaba-HK 5-minute K-lines (`amount` all zero) | `data/HK_ali_09988_kline_5min_all.csv` | — | — | **C** | The only real dataset shipped in the repo |
| C10 | Bilingual docs | `README.md` + `README_CN.md` | `finetune_csv/` | — | — | **C** | Documents a DDP path that does not work (§13.3) |

### 4.D Web UI (`webui/`)

| # | Item | What it does / how | Where | Inputs | Outputs | Status | Limits & deps |
|---|---|---|---|---|---|---|---|
| D1 | `GET /` | Serves the SPA | `app.py:330-333` | — | HTML | **C** | — |
| D2 | `GET /api/data-files` | Lists `.csv`/`.feather` in `<repo>/data` with sizes | `app.py:60-76, 335-339` | — | JSON list | **C** | `data/` **does not exist in the repo** ⇒ always empty out of the box **[R]** |
| D3 | `POST /api/load-data` | Loads a file, normalises the timestamp column (`timestamps`/`timestamp`/`date`, else synthesises hourly), coerces numerics, drops NaNs, infers the bar interval | `app.py:78-123, 341-402` | `{file_path}` | data summary JSON | **C** | **Unrestricted server-side file read** (§14.1) |
| D4 | `POST /api/predict` | Full forecast: slice lookback, optional `start_date` window, run `predictor.predict`, build a 3-series candlestick chart, attach ground truth, persist a JSON artefact | `app.py:404-624` | file path, lookback, pred_len, T, top_p, sample_count, start_date | chart JSON + rows + comparison | **C** | Same file-read exposure; synchronous & blocking; no auth/rate limit |
| D5 | `POST /api/load-model` | Loads one of three HF model/tokenizer pairs onto a chosen device into module-level globals | `app.py:626-663` | `{model_key, device}` | status JSON | **C** | Global mutable state, no locking ⇒ races under concurrency (§14.3) |
| D6 | `GET /api/available-models` · `GET /api/model-status` | Model registry and load state | `app.py:665-698` | — | JSON | **C** | — |
| D7 | `create_prediction_chart` | Plotly candlestick figure: history + forecast + actuals, with timestamp continuity logic | `app.py:209-328` | frames | Plotly JSON | **C** | Legend/title hardcode "400 data points"/"120 data points" regardless of actual params (`:233, 261, 301`) |
| D8 | `save_prediction_results` | Writes every forecast to `prediction_results/prediction_<ts>.json` with a gap analysis | `app.py:125-207` | frames + params | JSON file | **P** | Mis-indented block at `:165-196`: `first_actual` is assigned outside its guard and the continuity dict is built unconditionally ⇒ `NameError` when `prediction_results` is empty, swallowed by the outer `except` **[C]** |
| D9 | Launchers | `run.py` (dependency check, optional auto-`pip install`, auto-open browser) and `start.sh` | `run.py:1-89`, `start.sh` | — | running server | **C** | `run.py:44` will pip-install into the live environment on a `y` |
| D10 | Front end | 1,238-line single-file SPA, 23 JS functions, Plotly.js | `templates/index.html` | — | UI | **C** | Not reviewed line-by-line here |
| D11 | Committed artefacts | 29 forecast JSONs from 2025-08-26 | `prediction_results/` | — | — | **C** | Build output committed to VCS |

### 4.E Examples (`examples/`)

| # | Item | What it does | Where | Status | Notes |
|---|---|---|---|---|---|
| E1 | `prediction_example.py` | The canonical README walkthrough: load → predict 120 from 400 → plot close & volume | 80 LOC | **C** | Reads `./data/XSHG_5min_600977.csv`, **which is not in the repo** (§13.2) **[R]** |
| E2 | `prediction_wo_vol_example.py` | Same, OHLC-only, demonstrating the auto-fill path | 68 LOC | **C** | Hardcodes `device="cuda:0"` |
| E3 | `prediction_batch_example.py` | `predict_batch` over 5 overlapping windows | 72 LOC | **P** | Hardcodes `/home/csc/huggingface/...`; computes `pred_df` and never plots it |
| E4 | `prediction_cn_markets_day.py` | Proper CLI (`--symbol`): akshare download w/ 3 retries → 400-bar lookback → 120-day forecast → **±10% daily limit clamp** → CSV + PNG | 208 LOC | **C** | Best-engineered example. Uses `pd.bdate_range` (ignores exchange holidays) |
| E5 | `get_date_new.py` | A-share history via 4 fallbacks: Eastmoney JSON → manual regex parse → akshare → baostock → synthetic sample data | 660 LOC | **P** | Default save dir `D:/lianghuajiaoyi/...`; will silently emit **fabricated** data on total failure |
| E6 | `get_akshare_date_2024-2025_x.py` | Same, date-bounded | 628 LOC | **P** | ~90% duplicated from E5 |
| E7 | `prediction_akshare_2024-2025.py` | Forecast + Chinese-language report + holiday-aware future dates | 544 LOC | **P** | Hardcoded stock defaults |
| E8 | `prediction_new.py` | "Comprehensive" pipeline: Kronos forecast, then **multiplies the prediction by a hand-tuned macro/sector/fundamental adjustment factor** (`enhance_prediction_with_market_factors:702`, `calculate_enhanced_adjustment_factor:757`), capped at ±10% | 1332 LOC | **E** | The adjustment weights are arbitrary hand-set constants with no validation. Windows-only paths. Falls back to synthetic data |
| E9 | `prediction_new_GUI.py` | Tkinter desktop app wrapping E8: form inputs, threaded run, progress log, embedded matplotlib, plus prediction smoothing, post-holiday adjustment and result validation | 1624 LOC | **E** | Largest file in the repo. Duplicates ~60% of E8 |
| E10 | `run_backtest_kronos.py` | Loads a prior prediction CSV + history, generates ±2% threshold signals, simulates a long/flat portfolio, computes metrics, plots | 454 LOC | **P** | Config hardcoded in `main()` as Windows paths; uses removed pandas API `Series.replace(method='ffill')` (`:154`) |
| E11 | `yuce/historical_backtest.py` | A rolling-origin backtest harness | 383 LOC | **E** | **Never calls Kronos.** `simple_prediction` (`:87-102`) is a random walk: `price × (1 + N(0, σ))`. The docstring admits "这里应该替换为您的实际模型预测" ("this should be replaced with your actual model prediction") **[C]** |
| E12 | `yuce/*.json`, `*.png` | Committed analysis reports and charts for 4 tickers | — | **C** | Build output in VCS |

### 4.F Tests (`tests/`)

| # | Item | What it does | Where | Status | Notes |
|---|---|---|---|---|---|
| F1 | `test_kronos_predictor_regression` | Bit-exact check (`rtol=1e-5`) of an 8-step greedy forecast against golden CSVs, at context 512 and 256, on CPU, seed 123, against **pinned HF revisions** | `test_kronos_regression.py:45-88` | **C** | Requires network + ~25MB download; no `pytest.ini`, no CI, no marker to skip offline |
| F2 | `test_kronos_predictor_mse` | Averages MSE over 4 random 30-step forecasts, asserts within 1e-6 of `[0.008979, 0.003741]` | `test_kronos_regression.py:90-140` | **C** | Extremely tight tolerance ⇒ brittle across PyTorch/BLAS versions |
| F3 | `generate_regression_output.py` | Regenerates the golden fixtures | `tests/data/` | **C** | Good practice — the fixtures are reproducible |
| F4 | Coverage | — | — | **P** | **Zero** tests for `finetune/`, `finetune_csv/`, `webui/`, `examples/`, `BSQuantizer`, `predict_batch`, or any error path |

---

## 5. Feature → file/module map

| Capability | Entry point | Core implementation |
|---|---|---|
| Zero-shot OHLCV forecasting | `KronosPredictor.predict` | `kronos.py:519` → `:508` → `:389` |
| Multi-series batch forecasting | `KronosPredictor.predict_batch` | `kronos.py:562` |
| Probabilistic / multi-path forecasting | `sample_count`, `T`, `top_p`, `top_k` | `kronos.py:394-397, 465-467`, `:373-386` |
| OHLC-only (no volume) forecasting | `predict` auto-fill | `kronos.py:528-532` |
| K-line tokenisation | `KronosTokenizer.encode` | `kronos.py:142` → `module.py:245` → `:90` |
| Detokenisation | `KronosTokenizer.decode` | `kronos.py:161` → `:115` |
| Hierarchical two-token vocabulary | `BSQuantizer(half=True)`, `HierarchicalEmbedding`, `DualHead` | `module.py:225, 400, 486` |
| Calendar conditioning | `TemporalEmbedding` | `module.py:536`; features from `kronos.py:472` |
| Long-context handling (truncation) | ring buffers | `kronos.py:405-434` |
| Model download / upload | `PyTorchModelHubMixin` | `kronos.py:13, 180` |
| Device auto-detect (cuda/mps/cpu) | `KronosPredictor.__init__` | `kronos.py:494-503` |
| Qlib data ingestion | `python qlib_data_preprocess.py` | `finetune/qlib_data_preprocess.py:14` |
| CSV data ingestion | `CustomKlineDataset` | `finetune_csv/finetune_base_model.py:25` |
| Tokenizer fine-tune (multi-GPU) | `torchrun train_tokenizer.py` | `finetune/train_tokenizer.py:74` |
| Predictor fine-tune (multi-GPU) | `torchrun train_predictor.py` | `finetune/train_predictor.py:60` |
| Tokenizer/predictor fine-tune (CSV) | `python train_sequential.py --config …` | `finetune_csv/train_sequential.py:66, 148` |
| Train-from-scratch | `pre_trained_*: false` | `finetune_csv/train_sequential.py:86, 204` |
| Alpha-signal generation | `generate_predictions` | `finetune/qlib_test.py:239` |
| Portfolio backtest (Qlib) | `python qlib_test.py --device …` | `finetune/qlib_test.py:96` |
| Single-stock backtest (CSV) | `python run_backtest_kronos.py` | `examples/run_backtest_kronos.py:16` |
| Web forecasting UI | `python webui/run.py` → :7070 | `webui/app.py:404` |
| Desktop GUI | `python prediction_new_GUI.py` | `examples/prediction_new_GUI.py:35` |
| A-share data acquisition | akshare/Eastmoney/baostock scrapers | `examples/get_date_new.py:23-354` |
| Experiment tracking | `use_comet` | `finetune/train_*.py`, `config.py:75-83` |
| File logging | `setup_logging` | `finetune_csv/finetune_*.py:49/137` |
| Regression testing | `pytest tests/` | `tests/test_kronos_regression.py` |

---

## 6. Configuration system

Three unrelated configuration mechanisms, no shared schema. **[C]**

1. **`finetune/config.py`** — a Python class you edit in place. ~40 attributes, marked
   with four `TODO`s for paths. No env-var or CLI override of anything except
   `--device` in `qlib_test.py`. Ships a literal `"YOUR_COMET_API_KEY"` placeholder
   (`config.py:79`) with a comment recommending env vars that the code doesn't use.
2. **`finetune_csv/configs/*.yaml`** — declarative, five sections
   (`data`/`training`/`model_paths`/`experiment`/`device`), with `{exp_name}` path
   templating and empty-string auto-derivation. The best of the three. A
   `distributed:` section is read (`config_loader.py:178-180`) but **absent from the
   shipped YAML and never used** — `train_sequential.py` decides on DDP from
   `WORLD_SIZE` and `DIST_BACKEND` instead. **[C]**
3. **`webui/app.py`** — module-level `AVAILABLE_MODELS` dict (`:33-58`); everything
   else comes per-request from JSON. Port 7070 is hardcoded (`:708`).

Examples have **no** configuration: constants live at module top or inside `main()`,
several as absolute Windows paths (`D:\lianghuajiaoyi\Kronos\...`) or a specific
developer's home directory (`/home/csc/huggingface/...`). **[C]**

---

## 7. Deployment requirements

| Requirement | Detail |
|---|---|
| Python | 3.10+ (README); f-strings and `tuple[...]` annotations confirm ≥3.9/3.10 |
| PyTorch | ≥2.0 (SDPA required); verified working on 2.13 **[R]** |
| Inference hardware | CPU works (tests pin `DEVICE="cpu"`); CUDA or MPS auto-detected |
| Fine-tune hardware | `finetune/` **requires NVIDIA + NCCL** — `setup_ddp` hardcodes `backend="nccl"` (`training_utils.py:23`) and `main()` hardcodes `torch.device(f"cuda:{local_rank}")`. No CPU path. `finetune_csv/` does run on CPU. **[C]** |
| Network | Required on first model load (HF Hub) and for every `tests/` run |
| Disk | Model weights only; no database, no cache directory beyond HF's own |
| Containerisation | **None.** No Dockerfile, no compose file, no service manifest **[C]** |
| CI/CD | **None.** No `.github/` directory at all **[C]** |
| Packaging | **None.** No `setup.py`/`pyproject.toml`; import works only via `sys.path` hacks **[C]** |

---

## 8. Dependencies and external services

**Declared** (`requirements.txt`): `numpy`, `pandas` (pinned `2.2.2`), `torch>=2.0.0`,
`einops==0.8.1`, `huggingface_hub==0.33.1`, `matplotlib==3.9.3`, `tqdm==4.67.1`,
`safetensors==0.6.2`. Note `pandas` appears twice — unpinned then pinned. **[C]**

**Undeclared but imported** — installing `requirements.txt` alone is not sufficient
for anything beyond core inference: **[C]**

| Package | Needed by | Consequence |
|---|---|---|
| `comet_ml` | `finetune/train_tokenizer.py:15`, `train_predictor.py:12` — **unconditional top-level import**, even when `use_comet=False` | `ModuleNotFoundError` before any training starts **[R]** |
| `pyqlib` | all of `finetune/` | README mentions it separately (`:235`) but it is not in `requirements.txt` |
| `pyyaml` | `finetune_csv/config_loader.py:2` | Not mentioned anywhere |
| `pytest` | `tests/` | Not mentioned anywhere |
| `akshare` | 4 example scripts | Not mentioned anywhere |
| `baostock`, `requests` | `examples/get_date_new.py` | Not mentioned anywhere |
| `flask`, `flask-cors`, `plotly` | `webui/` | Declared separately in `webui/requirements.txt` |
| `tkinter` | `examples/prediction_new_GUI.py` | Stdlib but frequently absent on Linux |

**External services**

| Service | Purpose | Required? | Where |
|---|---|---|---|
| Hugging Face Hub (`NeoQuasar/*`) | All pretrained weights | **Yes** for any real use | `kronos.py:13, 180` |
| Comet ML | Experiment tracking | Optional (`use_comet`) — but the *import* is not | `finetune/train_*.py` |
| Qlib data provider | A-share market data | Yes for `finetune/` | `qlib_data_preprocess.py:28` |
| akshare | A-share history | Yes for 4 examples | `examples/*` |
| Eastmoney `push2his.eastmoney.com` | Undocumented direct HTTP K-line API | Fallback in scrapers | `get_date_new.py:23-189` |
| baostock | A-share history | Third fallback | `get_date_new.py:243` |

---

## 9. Storage, caching, state, persistence

There is **no database, no cache layer, and no state store**. **[C]**

| Concern | Reality |
|---|---|
| Model weights | HF Hub, cached by `huggingface_hub` in `~/.cache/huggingface` |
| Processed datasets | **Python pickles** — `train/val/test_data.pkl` (`qlib_data_preprocess.py:114-119`). Loaded with bare `pickle.load` (`dataset.py:42`) — arbitrary-code-execution risk if a pickle is untrusted |
| Checkpoints | `save_pretrained` → `safetensors` + `config.json`; **only the best-val model, weights only** — no optimizer/scheduler state ⇒ **training cannot be resumed** |
| Training metadata | `summary.json` per run (`train_tokenizer.py:266`) |
| Predictions | `predictions.pkl` (`qlib_test.py:346`), then re-read immediately from disk |
| Web UI results | One JSON per forecast, unbounded growth, no rotation (`app.py:125-207`); 29 already committed |
| In-process state | Web UI module globals `tokenizer`/`model`/`predictor` (`app.py:28-30`) — no lock |
| Caching | Only RoPE's `cos`/`sin` cache (`module.py:293-301`). **No KV cache** — see §14.2 |
| Logs | `finetune_csv` writes rotating logs (10MB × 5); `finetune/` prints to stdout only |

---

## 10. Setup and execution

### 10.1 Inference only (the path that works cleanly)

```bash
git clone https://github.com/shiyu-coder/Kronos.git && cd Kronos
pip install -r requirements.txt
python -c "
import pandas as pd
from model import Kronos, KronosTokenizer, KronosPredictor
tok = KronosTokenizer.from_pretrained('NeoQuasar/Kronos-Tokenizer-base')
mdl = Kronos.from_pretrained('NeoQuasar/Kronos-small')
p   = KronosPredictor(mdl, tok, max_context=512)          # device auto-detected
df  = pd.read_csv('your.csv'); df['timestamps'] = pd.to_datetime(df['timestamps'])
out = p.predict(df=df.loc[:399, ['open','high','low','close','volume','amount']],
                x_timestamp=df.loc[:399,'timestamps'],
                y_timestamp=df.loc[400:519,'timestamps'],
                pred_len=120, T=1.0, top_p=0.9, sample_count=1)
print(out.head())"
```

Must be run **from the repo root** (so `model/` is importable). `pred_len` must be
**less than** `max_context` (§12.6).

### 10.2 Model zoo

| Model | Tokenizer | Context | Params | Available |
|---|---|---|---|---|
| Kronos-mini | Kronos-Tokenizer-2k | 2048 | 4.1M | ✅ |
| Kronos-small | Kronos-Tokenizer-base | 512 | 24.7M | ✅ |
| Kronos-base | Kronos-Tokenizer-base | 512 | 102.3M | ✅ |
| Kronos-large | Kronos-Tokenizer-base | 512 | 499.2M | ❌ **not released** |

Source: `README.md:77-82`, mirrored in `webui/app.py:33-58`. Kronos-large is the
subject of several open access requests (issues #347, #349, #352). **[C]**

Architecture hyperparameters are not in the repo; the fallback defaults used for
random-init suggest the base predictor is `n_layers=12, d_model=832, n_heads=16,
ff_dim=2048, s1_bits=10, s2_bits=10` and the tokenizer `d_in=6, d_model=256,
n_enc_layers=4, n_dec_layers=4, s1_bits=10, s2_bits=10`
(`finetune_csv/train_sequential.py:93-224`). **[I]** — these are `.get()` fallbacks,
not authoritative; the real values live in each HF repo's `config.json` (which this
environment could not reach).

### 10.3 Qlib fine-tuning (as it actually works)

```bash
pip install pyqlib comet_ml
# edit finetune/config.py — qlib_data_path, dataset_path, save_path,
#                           pretrained_*_path, and set use_comet = False
cd finetune                       # ← MANDATORY; the README omits this (§13.1)
python qlib_data_preprocess.py
torchrun --standalone --nproc_per_node=2 train_tokenizer.py
torchrun --standalone --nproc_per_node=2 train_predictor.py
python qlib_test.py --device cuda:0
```

### 10.4 CSV fine-tuning

```bash
pip install pyyaml
cp finetune_csv/configs/config_ali09988_candle-5min.yaml my.yaml   # edit all paths
cd finetune_csv
python train_sequential.py --config my.yaml          # single GPU/CPU — use this
# NOT the multi-GPU command in the README: it produces unsynchronised gradients (§12.11)
```

### 10.5 Web UI

```bash
pip install -r webui/requirements.txt
mkdir -p data && cp your.csv data/        # ← required; `data/` is not in the repo
cd webui && python app.py                 # http://localhost:7070
```
Then, in the UI: pick a file → load a model → set parameters → predict.
**Do not expose port 7070** — see §14.1.

---

## 11. Example usage scenarios

1. **Signal research.** Roll `predict()` over a universe, take
   `mean(pred_close) − last_close` as a cross-sectional score, rank, and feed a
   top-k strategy. This is exactly what `finetune/qlib_test.py:276-282` does.
2. **Scenario / risk simulation.** Set `sample_count=100` and, instead of averaging,
   modify `auto_regressive_inference` to return all paths — you get an empirical
   predictive distribution for VaR-style analysis. *(Requires a code change: the mean
   is taken inside the function at `kronos.py:467`.)*
3. **Domain adaptation.** Fine-tune both stages on one instrument's 5-minute bars via
   `finetune_csv` — this is precisely the shipped Alibaba-HK example.
4. **Gap filling / smoothing.** Use the tokenizer alone as a learned compressor:
   `encode` → `decode` gives a denoised reconstruction of a K-line series.
5. **Interactive exploration.** Run the web UI, sweep `T` and `top_p`, and eyeball
   forecast-vs-actual candlesticks.
6. **Baseline for papers.** The pinned-revision regression tests make Kronos a
   citable, reproducible comparison point.

---

## 12. Confirmed defects, constraints, and sharp edges

Each item below was verified against source; those marked **[R]** were reproduced by
executing the code.

**12.1 `top_k` and `top_p` are mutually exclusive.** **[R]**
`top_k_top_p_filtering` (`kronos.py:347-352`) returns immediately after the top-k
branch, so `top_p` is never applied when `top_k > 0` — despite the docstring
promising "top-k and/or nucleus". Reproduced: `top_k=3, top_p=0.5` on
`[3,2,1,0,−1]` yields `[3,2,1,−inf,−inf]` (pure top-k). Additionally, passing
`top_k=None` with a non-None `top_p` raises `TypeError` at `kronos.py:376`.

**12.2 `s1_bits` must equal `s2_bits`.** **[R]**
`KronosTokenizer.indices_to_bits(half=True)` hardcodes `mask = 2**arange(codebook_dim//2)`
(`kronos.py:129`) rather than using `s1_bits`/`s2_bits`. With `s1_bits=6, s2_bits=4`
the half-mode round-trip diverges from full-mode by 0.384 (max abs), versus exactly
0.0 for a symmetric configuration. Silent corruption, no assertion, undocumented.

**12.3 Cross-attention causality flips between train and eval.** **[C]**
`MultiHeadCrossAttentionWithRoPE.forward` sets `is_causal_flag = self.training`
(`module.py:387`). The `DependencyAwareLayer` is therefore causal during training and
**bidirectional** during evaluation. Harmless for `auto_regressive_inference` (only
the final position is consumed), but any full-sequence `eval()` scoring silently lets
`s2` at step *i* see context from steps > *i*.

**12.4 The padding-mask path is untested and semantically ambiguous.** **[C]/[R]**
Both attention modules pass `attn_mask` together with `is_causal=True`
(`module.py:345-350, 389-394`), which PyTorch documents as unsupported. It does not
raise on torch 2.13 **[R]**, but SDPA's `attn_mask` convention is *True = attend*
while `key_padding_mask` conventionally means *True = pad* — so the polarity is
undefined. `padding_mask` is never passed non-`None` anywhere in the repo **[C]**, so
**variable-length batching is effectively unsupported**.

**12.5 `Kronos.forward()` is stochastic even in `eval()`.** **[C]**
Without `use_teacher_forcing`, the `s2` conditioning uses `torch.multinomial` over
sampled `s1` (`kronos.py:270-272`). `use_teacher_forcing=True` is **never passed
anywhere**, including in `train_predictor.py:108` — so training also conditions `s2`
on a random draw rather than the ground-truth `s1`. Whether that is intentional
regularisation or an oversight is not documented.

**12.6 `pred_len` must be strictly less than `max_context`.** **[R]**
With `max_context=16, pred_len=20`, `predict()` raises
`ValueError: Shape of passed values is (16, 6), indices imply (20, 6)`. The final
decode window is capped at `max_context` (`kronos.py:459-463`), so the slice at
`kronos.py:516` returns too few rows. Undocumented; the failure message gives no hint
of the real cause.

**12.7 Outputs carry no structural constraints.** **[C]**
Nothing enforces `high ≥ max(open, close) ≥ min(open, close) ≥ low`, nothing enforces
`volume ≥ 0`, and nothing enforces continuity with the last observed bar. Averaging
`sample_count` paths in feature space (`kronos.py:467`) can *itself* break OHLC
coherence even when every individual path is valid. *(A local check produced
violations on all rows, but with randomly-initialised weights, so it measures the
absence of a guard rail, not pretrained-model quality.)* Downstream code compensates
ad hoc — `examples/prediction_cn_markets_day.py:118-141` clamps to ±10%,
`prediction_new_GUI.py:1105` adds a validation pass.

**12.8 Backtest signals are computed in normalised space.** **[C]**
`qlib_test.py:277-282` computes `preds[:, -1, 3] − x[:, -1, 3]`, where both operands
come from the **z-scored** tensors (`QlibTestDataset.__getitem__` normalises at
`:85-87`; `auto_regressive_inference` never denormalises). The resulting signal is
therefore a *volatility-scaled* expected move, roughly a t-statistic, not a return.
That is arguably a sensible cross-sectional score — but it is undocumented, and it is
not what "forecasted price change" in `README.md:289` describes.

**12.9 `finetune_csv` still has the normalisation leak that `finetune/` fixed.** **[C]**
`CustomKlineDataset.__getitem__` computes mean/std over the **full**
`lookback + predict + 1` window (`finetune_base_model.py:125-127`), leaking
future statistics into the input scaling. `finetune/dataset.py:109-117` was corrected
for exactly this (commit `79d6d40`, issue #227) but the fix was never ported.

**12.10 `import comet_ml` is unconditional.** **[R]**
`finetune/train_tokenizer.py:15` and `train_predictor.py:12` import it at module
level regardless of `use_comet`. With only `requirements.txt` installed, both scripts
die with `ModuleNotFoundError: No module named 'comet_ml'` before doing anything.

**12.11 `finetune_csv` DDP does not synchronise gradients.** **[C]**
Both trainers wrap the model in `DDP(...)` and then call the **unwrapped** module for
the forward pass — `(model.module if use_ddp else model)(batch_x)`
(`finetune_tokenizer.py:201`, `finetune_base_model.py:291, 296`). DDP installs its
all-reduce hooks on the wrapper's `forward`; bypassing it means each rank trains on
its own shard with **no gradient averaging**, and rank 0 saves its private weights.
The README's `torchrun --nproc_per_node=8` instruction therefore silently produces a
model trained on ⅛ of the data. By contrast `finetune/train_predictor.py:108` calls
the DDP wrapper correctly.

**12.12 Committed placeholder credential.** **[C]**
`config.py:79` ships `"api_key": "YOUR_COMET_API_KEY"` with `use_comet = True` as the
default — a config designed to be edited in place, which is how real keys end up in
forks' git history.

**12.13 Deprecated/removed pandas APIs.** **[C]**
`finetune_base_model.py:70` uses `fillna(method='ffill')` (deprecated in pandas 2.x)
and `examples/run_backtest_kronos.py:154` uses `Series.replace(..., method='ffill')`
(removed in pandas 2.2, which `requirements.txt` pins).

**12.14 A shipped "backtester" that never runs the model.** **[C]**
`examples/yuce/historical_backtest.py:87-102` "predicts" with
`price × (1 + N(0, σ_hist))`. Its own comment says it should be replaced with the
real model. Anyone reading its accuracy/return output would be reading a random walk.

**12.15 Repo-asset overwrite during backtesting.** **[C]**
`qlib_test.py:199` does `plt.savefig("../figures/backtest_result_example.png")` —
running the documented backtest overwrites a README image in the working tree.

**12.16 Two inconsistent bit-ordering conventions.** **[C]**
`BSQuantizer.bits_to_indices` (`module.py:234-243`) packs bits LSB-first;
`BinarySphericalQuantizer.codes_to_indexes` (`module.py:163-169`) packs MSB-first.
`encode`/`decode` are internally consistent (both LSB-first), so this only corrupts
the `indices` returned in the **metrics** dict — but it is a trap for anyone reading
that field.

---

## 13. Where documentation and implementation disagree

**13.1 The documented fine-tuning commands do not run.** **[R]**
`README.md:271, 282, 293` say to run `torchrun … finetune/train_tokenizer.py` from
the repo root. Python puts the *script's* directory on `sys.path`, and the script's
own `sys.path.append("../")` resolves to the parent of the CWD — neither of which is
the repo root. Reproduced (with `comet_ml` stubbed):
`ModuleNotFoundError: No module named 'model'`. Running the identical command from
inside `finetune/` works. The README never says to `cd`.

**13.2 The example dataset does not exist.** **[R]**
`README.md:135` and `examples/prediction_example.py:44` read
`./data/XSHG_5min_600977.csv`. There is no `data/` directory in the repository, at
the root or under `examples/`. The canonical getting-started example fails on line 44
for a fresh clone. The same missing directory makes the web UI's file picker return
an empty list (`webui/app.py:62`).

**13.3 `finetune_csv`'s documented DDP command is unsafe.** **[C]**
`finetune_csv/README.md` presents
`DIST_BACKEND=nccl torchrun --nproc_per_node=8 train_sequential.py` as "for faster
training on multiple GPUs". Per §12.11, it trains without gradient synchronisation.
Nothing warns the user.

**13.4 "Gradient accumulation to simulate a larger batch size" does the opposite.** **[C]**
`config.py:61-62` claims accumulation simulates a larger batch. The implementation
(`train_tokenizer.py:131-134`) *slices the existing batch* into `accumulation_steps`
micro-batches. Effective batch size is unchanged; only peak memory drops.

**13.5 The top-k/top-p docstring promises composition the code doesn't do.** **[R]**
See §12.1.

**13.6 Silent truncation is documented, silent failure is not.** **[C]**
`README.md:99` says the predictor "will automatically handle truncation for longer
contexts" — true for long *lookbacks*. It says nothing about `pred_len ≥ max_context`
raising an opaque shape error (§12.6).

**13.7 The `distributed:` YAML section is read but never honoured.** **[C]**
`config_loader.py:178-180` parses `distributed.use_ddp`/`backend`; `train_sequential.py`
ignores both and uses `WORLD_SIZE`/`DIST_BACKEND` env vars instead. The key is also
absent from the shipped YAML.

**13.8 The web UI README oversells and misattributes.** **[C]**
It lists "Model: Hugging Face Transformers" — Kronos does not use `transformers`, only
`huggingface_hub`. It also claims "smart time window" while the chart labels are
hardcoded to 400/120 regardless of the user's settings (`app.py:233, 261, 301`).

**13.9 The README's own caveat about AI-generated comments.** **[C]**
`README.md:308` warns that many comments in `finetune/` were generated by Gemini 2.5
Pro and "may contain inaccuracies" — an unusually honest disclosure, and a reason to
trust the code over its comments throughout that directory.

---

## 14. Security, reliability, scalability, maintainability

### 14.1 Security

| Severity | Finding | Location |
|---|---|---|
| **High** | **Unrestricted server-side file read.** `/api/load-data` and `/api/predict` accept a client-supplied absolute `file_path` with no allow-list, no path normalisation, and no containment to `data/`. Any `.csv`/`.feather` readable by the process can be loaded, and its column names, row count, date range and price extrema are returned in the JSON response. | `app.py:78-123, 341-351, 404-424` **[C]** |
| **High** | **Debug server bound to all interfaces.** `app.run(debug=True, host='0.0.0.0', port=7070)` — the Werkzeug interactive debugger is reachable from the network; a PIN-protected console is one traceback away from RCE. Both launchers do the same. | `app.py:708`, `run.py:80` **[C]** |
| **Medium** | **No authentication, authorisation, CSRF protection, or rate limiting**, combined with permissive CORS (`CORS(app)` allows every origin). `/api/load-model` lets any caller trigger an arbitrary HF download and pin GPU memory. | `app.py:25, 626-663` **[C]** |
| **Medium** | **Unbounded artefact writes.** Every prediction writes a JSON file with no cap, quota, or cleanup — a trivially reachable disk-fill. | `app.py:125-207` **[C]** |
| **Medium** | **Pickle deserialisation of dataset files.** `pickle.load` on `*_data.pkl` and `predictions.pkl` executes arbitrary code if the file is untrusted. | `dataset.py:42`, `qlib_test.py:338, 353` **[C]** |
| **Medium** | **Remote code execution by design in model loading.** `from_pretrained` on a user-supplied HF id downloads and instantiates remote weights; `webui` exposes the model id indirectly via `model_key` (allow-listed — good), but `finetune*` configs take arbitrary paths/ids. | `kronos.py:13, 180` **[C]** |
| **Low** | Placeholder API key committed with `use_comet = True` default. | `config.py:75-83` **[C]** |
| **Low** | `run.py:44` offers to `pip install` into the running environment on a `y` keystroke. | `webui/run.py` **[C]** |
| **Low** | Undocumented direct calls to Eastmoney's internal HTTP endpoint with a spoofed browser UA; ToS-sensitive and fragile. | `get_date_new.py:23-146` **[C]** |
| **Low** | Scrapers **fabricate synthetic price data** as a final fallback, labelled only by a console message — silently poisoning downstream analysis if missed. | `get_date_new.py:378`, `prediction_new.py:105` **[C]** |

### 14.2 Reliability & performance

- **No KV cache.** `auto_regressive_inference` re-runs the full stack over the entire
  context at every one of `pred_len` steps (`kronos.py:436`). Cost is
  `O(pred_len × max_context²)` instead of `O(pred_len × max_context)` — for the
  README's default 400→120 forecast that is 120 full 512-token forward passes. This
  is the single largest performance lever left on the table. **[C]**
- **Sampling variance.** `sample_count=1` (the README default) gives a single
  stochastic path. Users comparing runs will see different answers with no warning.
- **No resume.** Only weights are checkpointed; a crashed multi-day fine-tune restarts
  from zero. **[C]**
- **Silent-corruption failure modes** dominate the loud ones: §12.2 (asymmetric bits),
  §12.9 (leakage), §12.11 (unsynchronised DDP) all produce plausible-looking numbers.
- **Brittle tests.** `MSE_TOLERANCE = 1e-6` against hardcoded constants
  (`test_kronos_regression.py:27`) will break on any BLAS/PyTorch change, and both
  tests require a network download with no offline skip.
- **Error handling** in the web UI is a blanket `except Exception` returning the raw
  exception string to the client (`app.py:401, 623`) — leaking filesystem paths.

### 14.3 Scalability

| Dimension | Assessment |
|---|---|
| Sequence length | Hard-capped at the pretrained `max_context` (512, or 2048 for mini). Full quadratic attention, no sliding-window or sparse variant. |
| Batch | `predict_batch` is a real GPU batch, but requires uniform lookback and `pred_len` (`kronos.py:643-646`). Memory scales with `batch × sample_count`. |
| Multi-GPU inference | None. No sharding, no tensor parallelism, no `torch.compile`, no AMP/quantisation anywhere. |
| Multi-GPU training | Works in `finetune/` (NCCL only, no CPU/Gloo); broken in `finetune_csv/` (§12.11). |
| Serving | Single-process Flask dev server, module-global model, no queue, no worker pool, no timeout, no health check. A second concurrent `/api/load-model` races the first (`app.py:629`). |
| Data volume | `QlibDataPreprocessor` holds every symbol's full history in RAM and pickles it whole (`qlib_data_preprocess.py:83, 114`). No chunking, no memory-mapping, no streaming. |

### 14.4 Maintainability

**Positives:** `model/` is genuinely clean — small focused classes, real docstrings,
type hints in the newer utilities, and a reproducible regression fixture with pinned
model revisions. Recent commits show real engineering (SDPA migration, allocation
reduction, the leakage fix, a top-k bug fix).

**Negatives:**
- **Heavy duplication.** Two complete fine-tuning pipelines sharing zero code; two
  near-identical 600-line scrapers; `prediction_new_GUI.py` re-implements ~60% of
  `prediction_new.py`.
- **`sys.path` hacking everywhere** (`sys.path.append("../")` in 12 files) in lieu of
  packaging — the direct cause of §13.1.
- **No CI, no linting, no formatting, no type checking, no pre-commit.**
- **Build artefacts in VCS**: 29 web-UI JSONs, 5 finetune PNGs, 4 `yuce/` reports.
- **Mixed-language codebase.** `examples/` is largely Chinese-commented, the rest
  English — a real barrier for either audience.
- **Absolute developer paths** hardcoded in five example scripts.
- **Documentation debt**: no API reference, no docstrings in `webui/` or `examples/`,
  no architecture doc, no CONTRIBUTING, and 198 open issues against 49 open PRs.

---

## 15. Missing capabilities

Absent, and not stubbed anywhere in the tree: **[C]**

**Modelling** — probabilistic outputs (quantiles, prediction intervals, or the raw
sample ensemble; the mean is taken inside the inference function); exogenous
covariates (news, fundamentals, cross-asset, order-book); multi-asset joint modelling
(each series is forecast independently); classification / direction heads;
uncertainty calibration; any structural constraint on OHLC or non-negativity;
KV caching; quantisation, AMP, ONNX, or `torch.compile`.

**Evaluation** — any forecasting metric implementation (no MAE/MASE/CRPS/pinball;
the only metric in the repo is a test-fixture MSE); baseline comparators (persistence,
ARIMA, a generic TSFM); walk-forward or purged cross-validation; statistical
significance testing; per-regime breakdowns. Notably, the two most substantive open
issues (#354, #355) are third-party evaluations doing exactly this work externally.

**Engineering** — packaging and PyPI distribution; a CLI; a documented inference
server (FastAPI/TorchServe/Triton); Docker; CI; model versioning or a registry;
checkpoint resume; structured metrics/observability; input validation beyond
column-presence and NaN checks.

**Data** — a data-source abstraction (every consumer re-implements loading); live/
streaming feeds; corporate-action or split adjustment; timezone or trading-session
awareness (`pd.bdate_range` ignores exchange holidays, `prediction_cn_markets_day.py:116`);
missing-bar handling beyond `dropna`.

---

## 16. Strengths and weaknesses

**Strengths**

1. **A genuinely novel, well-executed core idea** — BSQ hierarchical tokenisation of
   OHLCV with a factorised `P(s1)·P(s2|s1)` head is a clean solution to the
   million-way-softmax problem, and it is implemented tightly in ~1.2k lines.
2. **Real open weights.** Three model sizes on HF Hub under MIT, plus an academic
   paper (AAAI 2026). The first open foundation model in this niche.
3. **A three-line API.** `from_pretrained` → `KronosPredictor` → `predict(df, …)`
   with automatic normalisation, denormalisation, device selection, and column
   synthesis. Very low barrier to a first forecast.
4. **Native probabilistic forecasting.** Sampling is intrinsic, not bolted on.
5. **Reproducibility discipline where it counts.** Pinned HF revisions, committed
   golden fixtures, and a script to regenerate them.
6. **Two complete fine-tuning paths plus an end-to-end backtest**, which is more than
   most research releases ship.
7. **Live, responsive maintenance** — the recent history shows real bug fixes merged
   from outside contributors.

**Weaknesses**

1. **Efficacy is unestablished and externally contested.** Issues #354 and #355 are
   detailed third-party evaluations reporting that Kronos-mini **underperforms a
   persistence baseline** — #354 measures 13–20% worse MAPE across 1,800 rolling AAPL
   forecasts with 48.5–54.3% directional accuracy; #355 reports 45–60% direction
   accuracy on 30-minute US equities and finds that fine-tuning reduced token loss
   without improving trading outcomes. Neither has a maintainer response as of this
   analysis. *(These are unverified third-party reports on the smallest model, not
   reproduced here — but the repo contains no counter-evidence either, because it
   ships no evaluation harness at all.)*
2. **Quality falls off a cliff outside `model/`.** `examples/` includes a backtester
   that never calls the model, scripts that fabricate data, and hardcoded personal
   filesystem paths.
3. **The documented happy path is broken** — the getting-started example's dataset
   is missing (§13.2) and the fine-tuning commands fail on import (§13.1).
4. **Silent-corruption bugs** in the fine-tuning paths (§12.9, §12.11) that produce
   plausible but wrong models.
5. **The web UI is not deployable** as written (§14.1).
6. **No packaging, no CI, no evaluation code** — everything downstream of "run a
   forecast" is left to the user.
7. **Test coverage is ~2 tests over 11k lines**, both requiring network.

---

## 17. Maturity assessment and practical value

| Layer | Maturity | Verdict |
|---|---|---|
| `model/` core (tokenizer + LM + predictor) | **Production-ready for research use** | Clean, tested, correct. The one real asset. |
| `KronosPredictor` inference API | **Production-ready with caveats** | Solid; needs the §12.6 guard and output constraints. |
| `finetune/` (Qlib, DDP) | **Complete but rough** | Correct DDP; blocked by import-path and `comet_ml` issues; NVIDIA-only. |
| `finetune_csv/` | **Partially broken** | Single-process works; multi-GPU silently wrong; leaky normalisation. |
| `webui/` | **Demo only** | Functional locally; unsafe to expose. |
| `examples/` | **Mixed: reference to unusable** | `prediction_cn_markets_day.py` is good; `yuce/historical_backtest.py` is misleading. |
| `tests/` | **Minimal but principled** | Right idea, ~1% coverage. |
| Overall | **Research-grade, early-production core** | |

**Practical value.** Kronos is worth using *today* as (a) a **pretrained multivariate
K-line generator** you can call in three lines and integrate as one signal among many,
(b) a **reference implementation** of BSQ tokenisation and hierarchical autoregression
for financial series, and (c) a **starting point for domain adaptation**, since both
fine-tuning stages are provided and the CSV path is genuinely easy.

It is **not** ready to be (a) a trading system — no execution, risk, or portfolio
layer, and the repo says so itself; (b) a hosted service — the web UI has a High-severity
file-read exposure and a network-exposed debug console; or (c) a trusted alpha source
without your own validation — the repository ships **no evaluation harness, no
baselines, and no benchmark numbers**, and the only public head-to-head evidence
(issues #354/#355) is negative and unanswered.

**If adopting it, do these five things first:** pin `pred_len < max_context`; fix or
avoid `finetune_csv`'s DDP and normalisation window; never expose the web UI; build
your own walk-forward evaluation against a persistence baseline **before** trusting
any signal; and treat the average-of-samples output as a point estimate whose OHLC
relations you must repair yourself.

---

## 18. Appendix — confirmed vs. inferred, and analysis gaps

**Confirmed by source inspection or local execution:** everything in §2–§7, §9–§10.1,
§12, §13, §14, §15, and the status column of every table in §4.

**Inferred (reasoning stated inline):**
- Pretrained architecture hyperparameters in §10.2 — taken from `.get()` fallback
  defaults in `finetune_csv`, not from the authoritative HF `config.json`.
- Training-data scale ("45 global exchanges") — README claim only; no training data,
  data manifest, or pretraining script is in the repository.
- §12.7's OHLC-violation counts came from randomly-initialised weights and are
  evidence about *missing guard rails*, not about pretrained-model quality.

**Third-party, unverified:** the evaluation results in §16 (issues #354, #355) are
external user reports quoted as reported; they were not reproduced here.

**Gaps in this analysis (environment limitations, stated rather than guessed):**
- `huggingface.co` was unreachable through this session's proxy (HTTP 403), so the
  actual pretrained weights, `config.json` files, and parameter counts could not be
  downloaded or verified. No forecast from a *real* Kronos model was produced.
- The GitHub API was scope-restricted in this session, so the issue list was read from
  the rendered HTML issues page (12 titles on page 1 of 198 open issues) rather than
  enumerated exhaustively; the 49 open PRs were not reviewed.
- `webui/templates/index.html` (1,238 lines) was surveyed structurally, not audited
  line-by-line.
- Pretraining code is **not in the repository at all** — only fine-tuning. Any claim
  about how the released models were actually trained is outside what this repo can
  support.
