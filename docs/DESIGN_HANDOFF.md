# Attested Human Handoff

## The problem, and why it's a moat (not a gap)

Every browser agent eventually hits a **human-verification checkpoint** —
a CAPTCHA, a one-time-code / 2FA prompt, a "verify it's you" step-up. The
common answer is to *defeat* it: integrate a CAPTCHA-solving service, spoof
fingerprints, run stealth plugins. That path is an arms race, frequently
against sites' terms, and — decisively — it **destroys provenance**. Once you
have bypassed the human, you can never prove a human was involved.

Olympus takes the opposite stance and turns the boundary into a moat:

> **Never solve. Detect, hand off to the human, and cryptographically attest
> that the human cleared it.**

Having *respected* the control, Olympus can produce something a bypass-first
agent structurally cannot: a signed, auditable chain proving a real human
cleared exactly the checks that require a human. That is the differentiator.

## The loop

1. **Detect, don't defeat** (`browser_checkpoint` / `browser.detect_checkpoint`).
   Recognize the checkpoint by stable markers and return **only a type enum**
   (`captcha` / `otp` / `step_up`) — never page prose, never a solve attempt.
   A cross-origin challenge frame is *seen* by its fingerprint without reaching
   into it: the same-origin boundary is honored, not defeated.

2. **Hand off.** When a checkpoint blocks an operate step, `operator.execute`
   reports a **handoff** ("clear it in the browser, then I'll continue"), never
   mistaking it for template drift and never attempting a bypass. The human
   clears it in the operator's manual-mode browser — the same live session.

3. **Verify, then attest** (`browser_attest_human`). Before minting anything,
   Olympus **re-checks the live page**; it records a signed attestation only
   once the checkpoint is verifiably gone — never on the model's say-so. The
   attestation ("a human cleared a `<kind>` verification on `<domain>` at
   `<time>`") is signed with the **same Ed25519 root-of-trust that signs the
   decision log** (`olympus.witness`), and is tamper-evident and pin-verifiable
   by a third party holding the expected key (`OLYMPUS_ATTEST_PIN`).

4. **Audit** (`operator_attestations`). The signed ledger is the proof a human
   was in the loop for each verification — each entry rendered with its verify
   status.

5. **Evolve — need it less over time.** Paired with `browser_save_auth`, a
   cleared 2FA session persists, so the checkpoint rate on that site falls. The
   operator review cycle surfaces the heaviest sites
   (`attest.evolution_report`) and prompts saving their sessions. The moat
   compounds: clear once, reuse many, and every action stays attested.

## Policy: honest automation

- **No CAPTCHA solvers, no anti-bot / fingerprint evasion, no 2FA bypass.**
  These are refused by design; the detector script carries no solving
  machinery, pinned by a test.
- Where a site offers an official API or agent channel, prefer it. Otherwise
  operate transparently and let the human clear challenges. Olympus is
  **detectable and attested** — and treats that as the trust feature it is.

## Governance

`browser_checkpoint` and `browser_attest_human` are operator-gated,
domain-authorized, and capability-separated (in `security.ACTION_TOOLS`), so an
injected/prose-ingesting run can neither probe your authenticated tabs for
checkpoints nor forge a human-in-the-loop proof. `operator_attestations` is a
first-party read of your own signed records.
