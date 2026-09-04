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

## The other side of the moat: earned autonomy

The handoff is about *not overreaching* — never defeating a human-check. Its
counterpart is *not under-reaching*: Olympus should not have to ask permission
for every safe, reversible action on a site it has already proven itself on.
Both are governed by the same principle — **the human sits only at the gate that
can't be walked back.**

`olympus/trust.py` grades a **domain's** trust from Olympus's own witnessed
action history:

1. **Earn slowly.** An unbroken run of clean, governed successes on that exact
   domain raises its tier: `probation` → `trusted` (5 clean runs) →
   `established` (20).
2. **Snap back fast.** A single surprise — a failed run, an undo, a rejection,
   or a human-verification checkpoint (all of which land as non-success in the
   immutable audit log) — resets the domain to zero. Regaining a tier means
   rebuilding the whole streak. The asymmetry is the point.
3. **Pure function of the audit log.** Trust is derived, never stored — there is
   no counter for a prompt-injected agent to inflate.

Two invariants keep it inside the moat, mirroring the handoff's "never solve"
line:

- Earned trust can only ever widen auto-execution for **reversible** actions.
  Money, deletions, and other irreversible steps always still ask, however
  trusted the site (their min-to-auto level stays 99).
- The boost is always **re-capped by the conversation's capability profile**, so
  an ingesting or guest run can never be lifted by it.

It is OFF until opted in (`OLYMPUS_EARNED_AUTONOMY=1` or per-user
`olympus earned-autonomy on`), never bypasses a permission scope, and is visible
at any time via `operator_trust`. The same attestation root-of-trust that proves
a human cleared a checkpoint is what makes unattended autonomy safe: every
autonomous action is capability-separated, gated, and written to the
tamper-evident decision log — so freedom is earned by accountability, not by
removing the human.

### Self-tightening: earned autonomy absorbed into feature evolution

Earned autonomy is not only self-evolving per-domain (the streak); its *policy*
is absorbed into `evolve.py`'s feature loop — and absorbed **asymmetrically**,
the same shape as the moat itself.

`operator.execute` records OK / DEGRADED / FAIL to feature evolution (feature
`operator`). When the operator degrades, the periodic review auto-tunes three
security-relevant knobs — `establish_after` (higher bar to earn trust),
`cooldown_secs` (longer settle after a surprise), `daily_ceiling` (fewer
unattended runs/day) — **only ever toward more caution.** `trust.py` reads the
live values, so a failing actuator narrows its own freedom with no human in the
loop, and a once-established site can be demoted until it re-earns trust.

The tighten-only guarantee is structural, not a matter of the reviewer behaving:
a `Tunable.tighten_only` flag is validated at registration (the default must sit
at the loose bound, with `on_fail` pointing at the tight bound), and a defensive
clamp in `review()` refuses any loosening step. Auto-*tightening* a trust gate is
safe — its worst case is "ask a human more often", the safe default —
auto-*loosening* one never is, so only a human widens it back
(`olympus evolve reset operator`). This is the same principle as the handoff and
the earned-autonomy invariants: the machine may move toward caution on its own;
it may move toward freedom only with a human's hand.

The stored policy is part of that security boundary. A genuinely absent
evolution blob means a first run and uses the registered defaults. Invalid
JSON, a non-object root, malformed parameter containers, non-numeric values,
non-finite values, and storage read failures do **not** mean "use defaults": a
stricter policy may have been lost. They mark the policy evidence unavailable,
force every domain to probation, and disable the earned-autonomy boost until an
operator repairs the state. Best-effort telemetry also refuses to overwrite the
bad blob, preserving the evidence for diagnosis. This prevents corruption from
becoming an unauthorized equivalent of `olympus evolve reset operator`.
The explicit `olympus evolve repair` command is the recovery boundary: it
quarantines the original bytes and restores registered defaults. Because that
can widen previously tightened knobs, no background path invokes it.
