# Signing & key custody

Olympus signs two things with one Ed25519 root of trust: the **release manifest**
(`olympus sign` → `verification.json`) and the **decision log** (per run, at
flush). This doc is how to hold the key so signatures mean *authenticity*, not
just internal consistency.

> ## ⚠️ Two signing domains, two rotation policies
>
> This document covers seed custody and signing generally; most of it applies
> to both domains. What differs is **rotation**, and the split is drawn at
> [Rotating a RUNTIME / instance signing key](#rotating-a-runtime--instance-signing-key-overlap-window)
> — that section, and only that section, is scoped to runtime signing.
>
> **Runtime signing** is a separate deployment trust domain: an Olympus
> deployment signs its own decision logs and local manifests, and **may** use
> an instance-specific key rather than the release key. That domain **may**
> trust several keys at once, which is what makes an overlap-window rotation
> possible there.
>
> **Release signing is different and stricter.** The published-artifact trust
> anchor, `olympus/witness_pubkey.txt`, holds **exactly one active key**.
> `scripts/release_pipeline.py` refuses to sign, verify, or check
> distributions when that file lists anything other than one key, and CI
> enforces the same rule — so appending a second key to it does not create an
> overlap window, it **breaks the release pipeline**.
>
> - Rotating the **release** key: see
>   [RELEASING.md → Rotating the release signing key](../RELEASING.md#rotating-the-release-signing-key-flag-day).
>   It is a fail-closed **flag-day replacement**, performed while publishing is
>   disabled.
> - Historical release keys: [docs/RELEASE_SIGNING_KEYS.md](RELEASE_SIGNING_KEYS.md),
>   an append-only event ledger. Retired release keys are recorded there and
>   are **not** kept in the active trust set.
>
> The multi-key overlap procedure in this document **does not apply to the
> release key**. Do not apply it to `olympus/witness_pubkey.txt`.

## The threat the default seed does NOT defend against

The signing key is derived from a seed: `ed25519(sha256(OLYMPUS_SIGNING_SEED))`.
If you don't set `OLYMPUS_SIGNING_SEED`, Olympus uses a **public, built-in
default seed** — which means *anyone* can derive the same private key, re-sign
tampered content, and produce a "valid" signature. So:

- Manifests signed under the default seed are marked **`"dev": true`** and are
  treated as **local integrity only** — never release authenticity.
- `olympus sign` **refuses** to write a manifest under the default seed unless
  you pass `--dev`.
- `olympus verify` **rejects** a dev manifest unless you pass `--allow-dev`.

## Production setup (do this before a real release)

1. **Generate the seed** — one command, correct entropy (256-bit), correct
   permissions (the file is created `0600`, never overwritten without
   `--force`), and it prints the public key to pin:
   ```bash
   olympus keygen --out /etc/olympus/signing_seed
   # Wrote a new 256-bit signing seed: /etc/olympus/signing_seed  (mode 0600)
   #   public key: <64-hex-char key>
   ```
   The seed itself is never printed or logged — only where it lives and the
   derived **public** key. Do not hand-pick a seed string.

2. **Point Olympus at the seed file**:
   ```bash
   export OLYMPUS_SIGNING_SEED_FILE=/etc/olympus/signing_seed
   ```
   File-based custody is preferred over `OLYMPUS_SIGNING_SEED` (the env var is
   readable via `/proc/<pid>/environ`, `docker inspect`, crash dumps, and shell
   history). Acquisition rules — misconfiguration is always a **hard error**,
   never a silent downgrade to the forgeable default key:
   - The file is read and stripped; a **missing, unreadable, or empty** file
     raises a `WitnessError` naming the path.
   - On POSIX the file must not be group/other-readable — mode `0600`
     (`chmod 600 <path>` is named in the error otherwise).
   - Setting **both** `OLYMPUS_SIGNING_SEED` and `OLYMPUS_SIGNING_SEED_FILE`
     is ambiguous custody and refuses with an error. Use exactly one.

3. **Pin the public key** for verifiers, either:
   - env: `OLYMPUS_PINNED_PUBKEY=<hex>` (comma-separated for several) — this
     is the runtime pin, and it is where a multi-key overlap belongs; or
   - committed file: `olympus/witness_pubkey.txt`. The *verifier* accepts one
     hex key per line, but this file is the **release** trust anchor and its
     policy is **exactly one active key** — the release pipeline and CI both
     refuse anything else. Never add a second line to it.

   With a pin configured, `verify` requires the manifest's key to **match one
   of the pinned keys** — a manifest re-signed with any other key fails, even
   if its own signature checks out.

4. **Sign releases with the secret seed** (no `--dev` needed once a real seed is
   set):
   ```bash
   OLYMPUS_SIGNING_SEED_FILE=/etc/olympus/signing_seed olympus sign
   ```
   The publish workflow reads `OLYMPUS_SIGNING_SEED` in the `sign` job, so the
   shipped wheel's `verification.json` is signed by your production key. The
   replacement seed is stored only in the protected `release-signing`
   environment; repository-scoped placement remains forbidden because it
   exposes the seed to every workflow job that asks for it. The
   repository-scoped copy of the retired seed has been deleted, and `pypi`
   holds no signing seed. See the closed activation blocker 4 in
   [RELEASING.md](../RELEASING.md).

5. **Verify** (anywhere, no key needed — verification uses the public pin):
   ```bash
   olympus verify
   # ✓ verified: ... signature is from the trusted key.
   ```

### Custody recipes (copy-paste)

**systemd** — the seed is delivered by the service manager, never in the unit's
environment or the process env:
```ini
[Service]
LoadCredential=signing_seed:/etc/olympus/signing_seed
Environment=OLYMPUS_SIGNING_SEED_FILE=%d/signing_seed
```

**Docker Compose secrets**:
```yaml
services:
  olympus:
    environment:
      OLYMPUS_SIGNING_SEED_FILE: /run/secrets/olympus_seed
    secrets: [olympus_seed]
secrets:
  olympus_seed:
    file: /etc/olympus/signing_seed
```

### Sovereign mode fails closed on the seed

Sovereign posture (`OLYMPUS_SOVEREIGN=1`) is the production switch, so a
sovereign instance **refuses to run on the public default seed** — otherwise it
would silently sign every decision log and backup with a key anyone can forge:

- **At boot**, `olympus <anything>` exits with one actionable line naming the
  fix (`olympus keygen` + `OLYMPUS_SIGNING_SEED_FILE`). `olympus keygen` itself
  is exempt — it *is* the fix.
- **At sign time** (defense in depth), `witness.sign()` raises the same error —
  catching sovereign mode enabled after boot or entry points that bypass the CLI.
- **Escape hatch** for labs/CI only: `OLYMPUS_SOVEREIGN_ALLOW_DEV_SEED=1`
  permits it and logs an unmissable forgeability warning on every use. Never
  set it in production.

Non-sovereign instances are unchanged: the default seed works, artifacts are
labeled `dev`, and `verify` warns exactly as before.

### Decision logs use a separate pin

The **release manifest** pin above (`OLYMPUS_PINNED_PUBKEY` / `witness_pubkey.txt`)
is the CI production key. **Decision logs** are signed at runtime by each running
instance's own key (the default dev seed unless you set `OLYMPUS_SIGNING_SEED`),
which is a different trust domain — so `verify_run`/`verify_log` do NOT pin them
against the release key (that would reject every default-seed log on a normal
install). By default an instance verifies its own logs. A third-party auditor who
holds the expected signer's public key out-of-band can bind verification to it:

```bash
export OLYMPUS_LOG_PIN="$(olympus pubkey)"   # or pass verify_run(..., pin=<hex>)
```

With a log pin set, a decision log re-signed under any other seed is rejected,
even if its own signature is self-consistent.

## Generating a strong seed

`olympus keygen` is the canonical path (256-bit entropy, `0600` file, prints
the public key, never prints the seed) — see “Production setup” above. If you
must generate a seed elsewhere (e.g. straight into a CI secret store), match
its entropy:
```bash
python -c 'import secrets; print(secrets.token_hex(32))'   # 256-bit seed
```
`olympus pubkey` prints the derived public key on stdout (and, when you're still
on the default seed, prints custody guidance on stderr), so you can pin it in one
step:
```bash
export OLYMPUS_SIGNING_SEED_FILE=/etc/olympus/signing_seed
olympus pubkey >> olympus/witness_pubkey.txt   # append the derived key (one per line)
# or:  export OLYMPUS_PINNED_PUBKEY="$(olympus pubkey)"
```

## Where the seed should live (HSM / KMS recommended)

The seed is the entire root of trust — treat it like a signing key, because it
is one. In order of preference:

1. **An HSM or cloud KMS** (AWS KMS, GCP KMS, Azure Key Vault, YubiHSM, …) is the
   recommended home: the secret never sits in an env var or on disk
   unprotected. Have the KMS/secret store deliver the seed to a `0600` file at
   process start and point `OLYMPUS_SIGNING_SEED_FILE` at it (systemd
   `LoadCredential` and container secrets do exactly this — recipes above).
   SPEC-03 deliberately does **not** bind to a specific KMS vendor — that
   integration is the recommended deployment shape, not a code dependency.
2. A CI/secret manager (GitHub Actions secret, Vault) injected as
   `OLYMPUS_SIGNING_SEED` at build/run time (fine for ephemeral CI jobs; on
   long-lived hosts prefer the file — env vars leak via `/proc`, `docker
   inspect`, and crash dumps).
3. A local `olympus keygen` seed file for solo/dev use.

Never echo the seed into logs, traces, or shell history. Olympus never prints or
records the seed — only the derived **public** key.

## Rotating a RUNTIME / instance signing key (overlap window)

**Scope: this section only, and runtime signing only** — decision logs and an
instance's own manifests, in a deployment that may use an instance-specific
key. This procedure **does not apply to the release key**. For that, see
[RELEASING.md → Rotating the release signing key](../RELEASING.md#rotating-the-release-signing-key-flag-day),
which is a forward-only flag-day replacement; applying the steps below to
`olympus/witness_pubkey.txt` breaks the release pipeline, because that file
must list exactly one key.

Multi-key pinning makes *runtime* rotation an **overlap window, not a flag
day**: every key in `OLYMPUS_PINNED_PUBKEY` (comma-separated) is trusted, so
the old and new keys verify side by side while you switch.

1. **Generate the new seed**: `olympus keygen --out /etc/olympus/signing_seed.new`
   (prints the new public key).
2. **Add the new public key to the runtime pin set** — extend
   `OLYMPUS_PINNED_PUBKEY` to list both keys. Both are now trusted; nothing
   already signed stops verifying. Do **not** add a line to
   `olympus/witness_pubkey.txt`.
3. **Switch the seed file**: point `OLYMPUS_SIGNING_SEED_FILE` at the new file
   (or move it over the old path — it must stay mode `0600`).
4. **Re-sign** what must remain trusted (`olympus sign`) — now signed by the
   new key, which verifiers already trust.
5. **Drop the old public key** from `OLYMPUS_PINNED_PUBKEY` after the overlap
   window — when everything you still need to verify against the *current* pin
   set is signed by the new key. Record the retired key and its date range: a
   historical run signed under a retired key can always be verified by pinning
   that retired key explicitly; nothing about rotation invalidates
   already-signed history. Retired **release** keys are recorded in
   [docs/RELEASE_SIGNING_KEYS.md](RELEASE_SIGNING_KEYS.md).

## Compromise response (suspected or confirmed seed exposure)

Planned rotation (above) uses an overlap window. A **compromise is different:
there is no overlap window** — the attacker holds a key your verifiers still
trust, and every minute of overlap is forgeable history.

1. **Revoke the pin first, immediately.** Remove the compromised public key
   from every pin set — `olympus/witness_pubkey.txt`, `OLYMPUS_PINNED_PUBKEY`,
   and any `OLYMPUS_LOG_PIN` — *before* anything else. From that moment,
   artifacts signed by the stolen key stop verifying anywhere the pin is used.
2. **Generate the replacement on a clean machine** — not the possibly-
   compromised host: `olympus keygen --out <new path>` (0600; prints only the
   public key). Do not reuse the old path until the host is cleared.
3. **Close the leak channel before installing.** Determine how the seed
   escaped — env var via `/proc`/`docker inspect`/crash dump, a
   world-readable file, a backup, shell history, a CI secret — and fix that
   channel first, or the new seed follows the old one out.
4. **Install and pin the new key** (`OLYMPUS_SIGNING_SEED_FILE=<new path>`).
   For the runtime pin set, add the new key to `OLYMPUS_PINNED_PUBKEY`. For
   the **release** anchor, `olympus/witness_pubkey.txt`, *replace* the single
   line — never append, and never leave the compromised key listed. Then
   **re-sign** what must remain trusted (`olympus sign`).
5. **Bound the forgery window.** Everything signed by the compromised key
   after the earliest plausible exposure time is SUSPECT — its signature
   proves nothing. Use out-of-band records to separate before from after: the
   external anchor sink (`olympus verify-anchor` divergences during the
   window are evidence), CI logs of legitimate signings, and release
   timestamps. When in doubt, treat it as forged.
6. **Record it, permanently.** Append a `RETIRED` event for the compromised
   public key to [docs/RELEASE_SIGNING_KEYS.md](RELEASE_SIGNING_KEYS.md) (or
   the equivalent runtime record), with its date range and the evidence,
   marked **COMPROMISED — never re-trust**. Unlike an ordinarily-retired key,
   a compromised key must never be used to re-verify "historical" artifacts
   from inside the forgery window.

## Rules

- **Never** publish a release under the default seed (`--dev` manifests are for
  local use only).
- **Never** pin the default public key — that would trust the forgeable key.
- Keep the seed secret wherever it lives (`OLYMPUS_SIGNING_SEED_FILE` file,
  mode `0600`, or the `OLYMPUS_SIGNING_SEED` env var); rotating it changes the
  public key, so update the pin set when you rotate (and retain old pubkeys
  for historical runs).

## Dev / local use

For local integrity checks without a secret:
```bash
olympus sign --dev          # writes a dev-marked manifest
olympus verify --allow-dev  # accepts it for local use
```
This proves your files haven't drifted; it does **not** prove who signed them.

## Trust-root invariants (Milestone 0.1)

The signing *mechanism* above is the whole of it — there is no second trust
root. Milestone 0.1 pins its guarantees as a re-runnable regression suite
(`tests/test_m0_trust_root.py`) and closes one gap: default-seed signatures are
now **unattested at the programmatic layer**, not only in the CLI display.

- **Attestation is a first-class result.** `witness.verify_run()` returns an
  `attested` boolean alongside `ok`. `ok` means *integrity* (the signature is
  self-consistent and matches the expected key); `attested` means *authenticity*
  (a real, non-default key signed it). A run signed by the public default seed
  reports `ok: true, attested: false` — it self-verifies but anyone could have
  forged it, so it is never treated as authentic. Fail-closed: `attested` is
  true only when a configured secret key signed the run.
- **Provisioning is an operator action, by design.** A secret seed is never
  committed to the repo (that would defeat it). Provision one at deploy:
  `olympus keygen --out /etc/olympus/signing_seed` (writes `0600`), then set
  `OLYMPUS_SIGNING_SEED_FILE=/etc/olympus/signing_seed`. Until then the instance
  runs on the public default seed — `posture() == "dev"`, every signature
  `attested: false`. Custody location and rotation are documented in the
  sections above (file `0600`, one of env **or** file never both, rotate by
  overlapping pins).
- **Verified by test:** `is_default_seed()` flips false and `posture()` becomes
  `production` under a configured seed; default-seed runs are `attested: false`
  even when correctly verified against the default key; broken custody
  (world-readable / empty seed file) fails closed with no silent downgrade; a
  release manifest refuses to sign under the default seed.
