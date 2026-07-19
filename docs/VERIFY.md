# Verify an Olympus run yourself

Olympus records every decision a run made and **signs** the decision path. Two
independent properties make a run trustworthy, and `olympus verify --run` checks
**both** in one command:

1. **Replay (integrity of the reasoning).** Re-execute the real orchestration
   code against the run's frozen model responses and confirm the decision path
   is **byte-identical** to what was recorded. A code/prompt change or a tampered
   response shows up as a named divergence.
2. **Signature (authenticity of the record).** Verify the decision-log Ed25519
   signature against the trusted (pinned) public key. A tampered decision record,
   or a signature from any key other than the pinned one, fails.

This is meant to be run by a skeptical evaluator from the repo alone — no trust
in us required.

## One command

```bash
olympus verify --run <RUN_ID>
```

Every `/v1/chat/completions` answer carries two audit headers:
`X-Olympus-Run-Id` (the run id to verify — non-streaming responses only, since a
stream's id isn't known until it finishes) and `X-Olympus-Audit`
(`signed-production` or `signed-dev`, the signing posture of the run). `/api/status`
reports the same posture for the instance. Example PASS:

```
Run e3ea59c9c7ae — verification (production signing posture)
  replay    : PASS — 3 decision(s) replayed byte-identically
  signature : PASS — decision-log signature valid
  RESULT    : PASS — replay-identical and signed by the trusted production key.
```

`olympus verify --run` exits **0** only if BOTH halves pass; any failure exits
non-zero and **names** the problem.

## What PASS / FAIL mean

| Output | Meaning |
| ------ | ------- |
| `replay : PASS` | The recorded reasoning re-executed byte-for-byte against the frozen responses. |
| `replay : FAIL` | A decision diverged (named by index/type) or a frozen response is missing/altered — the run is not the reasoning that was recorded. |
| `signature : PASS` | The decision log is intact and signed by the trusted key. |
| `signature : FAIL` | The decisions were altered since the run, or the signer is not the pinned key (untrusted). |
| `RESULT : PASS` | Both halves passed. |
| `RESULT : FAIL` | At least one half failed — do not trust this run as recorded. |

## The DEV / UNVERIFIED label (read this)

If the instance has **no secret signing seed** configured, it signs with a
**public, built-in default key that anyone can use**. Verification will still
report `PASS`, but labeled:

```
RESULT : PASS (integrity only) — DEV / UNVERIFIED — signed by the public default
key; proves the log is internally consistent, not that it came from a trusted
signer.
```

This means: the log is self-consistent (nothing was tampered relative to its own
signature), **but it does not prove the run came from a trusted signer** — anyone
could have produced it. Treat a dev/unverified pass as integrity only, never as
authenticity.

To require a real (authentic) run and reject the default-seed case outright:

```bash
olympus verify --run <RUN_ID> --require-production    # exits non-zero on a dev-seed run
```

To check the instance's posture before you even look at a run:

```bash
curl -s http://<host>/api/status | python -m json.tool   # see the "signing" block
# signing.posture: "production" | "dev"; signing.pinned: is a trusted key pinned
```

A production instance sets `OLYMPUS_SIGNING_SEED` to a secret and pins the
derived public key — see [SIGNING.md](SIGNING.md). On such an instance,
`--require-production` passes and the result is a genuine authenticity guarantee.

## Tamper a run yourself (prove it's not theater)

```bash
# mutate one stored decision record or one frozen response for the run, then:
olympus verify --run <RUN_ID>
# replay    : FAIL — replay divergence at request … / decision #N DIVERGED …
# signature : FAIL — decision-log signature INVALID …
# RESULT    : FAIL — this run is NOT verifiable as recorded.
```

Frozen responses live at `MEMORY_DIR/responses/<hash>.json`; decision records are
in `MEMORY_DIR/traces/<date>.jsonl`. Change either and verification fails loudly.

## Honest boundary

- The default seed proves **integrity, not authenticity** — always labeled.
- Verification is application-layer: it proves what Olympus recorded and signed,
  against the pinned key. It is not a proof about anything outside that record.
- Replay covers the **decision path** (route → plan → dispatch → verify →
  review), not the final free-text answer (which is not a decision).
