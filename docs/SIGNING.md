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

1. **Pick a secret seed** and keep it out of the repo (a password-manager entry,
   a CI secret). Any strong, stable string works.

2. **Derive its public key** and pin it:
   ```bash
   OLYMPUS_SIGNING_SEED='your-long-secret' olympus witness-pubkey
   # -> 64-hex-char public key
   ```

3. **Pin that public key** for verifiers, either:
   - env: `OLYMPUS_PINNED_PUBKEY=<hex>`, or
   - committed file: `olympus/witness_pubkey.txt` (one hex key; `#` comments ok).

   With a pin configured, `verify` requires the manifest's key to **equal the
   pin** — a manifest re-signed with any other key fails, even if its own
   signature checks out.

4. **Sign releases with the secret seed** (no `--dev` needed once a real seed is
   set):
   ```bash
   OLYMPUS_SIGNING_SEED='your-long-secret' olympus sign
   ```
   The publish workflow sets `OLYMPUS_SIGNING_SEED` from a repo secret, so the
   shipped wheel's `verification.json` is signed by your production key.

5. **Verify** (anywhere, no key needed — verification uses the public pin):
   ```bash
   olympus verify
   # ✓ verified: ... signature is from the trusted key.
   ```

## Generating a strong seed

The seed must be high-entropy and stable. Generate one and store it as a secret
(never in the repo):
```bash
python -c 'import secrets; print(secrets.token_hex(32))'   # 256-bit seed
```
`olympus pubkey` prints the derived public key on stdout (and, when you're still
on the default seed, prints custody guidance on stderr), so you can pin it in one
step:
```bash
export OLYMPUS_SIGNING_SEED='<the 64-hex secret above>'
olympus pubkey > olympus/witness_pubkey.txt    # pin the derived key
# or:  export OLYMPUS_PINNED_PUBKEY="$(olympus pubkey)"
```

## Where the seed should live (HSM / KMS recommended)

The seed is the entire root of trust — treat it like a signing key, because it
is one. In order of preference:

1. **An HSM or cloud KMS** (AWS KMS, GCP KMS, Azure Key Vault, YubiHSM, …) is the
   recommended home: the secret never sits in an env var or on disk. Olympus
   reads the seed from `OLYMPUS_SIGNING_SEED`, so inject it at process start from
   the KMS/secret store (or have your launcher fetch-and-export it) rather than
   committing or baking it in. SPEC-03 deliberately does **not** bind to a
   specific KMS vendor — that integration is the recommended deployment shape,
   not a code dependency.
2. A CI/secret manager (GitHub Actions secret, Vault) injected as an env var at
   build/run time.
3. A local password-manager entry for solo/dev use.

Never echo the seed into logs, traces, or shell history. Olympus never prints or
records the seed — only the derived **public** key.

## Rotating the signing key

Rotating means switching to a new secret seed (new private key → new public key):

1. Generate a new seed and derive its public key (`olympus pubkey`).
2. **Re-pin** the new public key (`witness_pubkey.txt` / `OLYMPUS_PINNED_PUBKEY`).
3. Re-sign the current release with the new seed (`olympus sign`).
4. **Keep the previous public key(s)** if you still need to verify historical
   runs: a decision log signed under the old key remains valid *against the old
   key*. Verification matches a run/manifest's embedded key against the pin in
   effect, so to verify an old run you pin (or check against) the old public key
   that signed it. Keep an append-only list of retired pubkeys with their date
   ranges; nothing about rotation invalidates already-signed history.

## Rules

- **Never** publish a release under the default seed (`--dev` manifests are for
  local use only).
- **Never** pin the default public key — that would trust the forgeable key.
- Keep `OLYMPUS_SIGNING_SEED` secret; rotating it changes the public key, so
  update the pin when you rotate (and retain old pubkeys for historical runs).

## Dev / local use

For local integrity checks without a secret:
```bash
olympus sign --dev          # writes a dev-marked manifest
olympus verify --allow-dev  # accepts it for local use
```
This proves your files haven't drifted; it does **not** prove who signed them.
