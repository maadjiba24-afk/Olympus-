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

## Rules

- **Never** publish a release under the default seed (`--dev` manifests are for
  local use only).
- **Never** pin the default public key — that would trust the forgeable key.
- Keep `OLYMPUS_SIGNING_SEED` secret; rotating it changes the public key, so
  update the pin when you rotate.

## Dev / local use

For local integrity checks without a secret:
```bash
olympus sign --dev          # writes a dev-marked manifest
olympus verify --allow-dev  # accepts it for local use
```
This proves your files haven't drifted; it does **not** prove who signed them.
