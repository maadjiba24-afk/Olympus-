# Signing & key custody

Olympus signs two things with one Ed25519 root of trust: the **release manifest**
(`olympus sign` → `verification.json`) and the **decision log** (per run, at
flush). This doc is how to hold the key so signatures mean *authenticity*, not
just internal consistency.

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
   - env: `OLYMPUS_PINNED_PUBKEY=<hex>` (comma-separated for several), or
   - committed file: `olympus/witness_pubkey.txt` (**one hex key per line**;
     `#` comments ok). All listed keys are trusted — that's what makes
     rotation below flag-day-free.

   With a pin configured, `verify` requires the manifest's key to **match one
   of the pinned keys** — a manifest re-signed with any other key fails, even
   if its own signature checks out.

4. **Sign releases with the secret seed** (no `--dev` needed once a real seed is
   set):
   ```bash
   OLYMPUS_SIGNING_SEED_FILE=/etc/olympus/signing_seed olympus sign
   ```
   The publish workflow sets `OLYMPUS_SIGNING_SEED` from a repo secret, so the
   shipped wheel's `verification.json` is signed by your production key.

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

## Rotating the signing key

Multi-key pinning makes rotation an **overlap window, not a flag day**: every
key in `witness_pubkey.txt` (one per line) and `OLYMPUS_PINNED_PUBKEY`
(comma-separated) is trusted, so the old and new keys verify side by side
while you switch.

1. **Generate the new seed**: `olympus keygen --out /etc/olympus/signing_seed.new`
   (prints the new public key).
2. **Append the new public key** to `olympus/witness_pubkey.txt` (keep the old
   line) — both keys are now pinned; nothing already signed stops verifying.
3. **Switch the seed file**: point `OLYMPUS_SIGNING_SEED_FILE` at the new file
   (or move it over the old path — it must stay mode `0600`).
4. **Re-sign at the next release** (`olympus sign`) — it is now signed by the
   new key, which verifiers already trust.
5. **Remove the old public key** from `witness_pubkey.txt` after the overlap
   window — when everything you still need to verify against the *current* pin
   set is signed by the new key. Keep an append-only record of retired pubkeys
   with their date ranges: a historical run signed under a retired key can
   always be verified by checking against that retired key explicitly; nothing
   about rotation invalidates already-signed history.

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
