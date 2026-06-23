# Releasing Olympus

A release is cut by **pushing a version tag** (`vMAJOR.MINOR.PATCH`). The
`Publish to PyPI` workflow then signs the package and publishes it. This is the
only step that requires maintainer rights; everything before it is preparation
you can verify locally.

See [docs/SUPPORT.md](docs/SUPPORT.md) for the versioning/LTS policy and
[docs/SIGNING.md](docs/SIGNING.md) for signing-key custody.

## One-time setup

- **Signing seed.** Set `OLYMPUS_SIGNING_SEED` as a repository secret (a long
  random string). The publish workflow derives the Ed25519 release key from it.
  Without it the workflow's `olympus sign` step **refuses to sign** (it will not
  ship a forgeable release under the public default seed).
- **Pinned public key.** Commit the production public key to
  `olympus/witness_pubkey.txt` (print it with `olympus witness-pubkey` in an
  environment where the secret seed is set), or distribute it as
  `OLYMPUS_PINNED_PUBKEY`, so `olympus verify` trusts only that key.
- **PyPI trusted publisher.** Create the project on PyPI and add this repo +
  `publish.yml` as a Trusted Publisher (no API token needed), or set a
  `PYPI_API_TOKEN` secret and switch the publish step to use it.

## Release checklist

1. **Pick the version** per SemVer (`docs/SUPPORT.md`): PATCH for fixes only,
   MINOR for new backward-compatible capabilities, MAJOR for incompatible CLI /
   public-API / memory-`schema_version` changes (add a migration note).
2. **Update `pyproject.toml`** — it is the single source of truth for the
   version.
3. **Update `CHANGELOG.md`** — move the `[Unreleased]` items into a new
   `[X.Y.Z]` section and refresh the compare/links at the bottom. A MAJOR bump
   needs a migration note.
4. **Run the gates locally** (the same checks CI enforces):
   ```bash
   pip install --require-hashes -r requirements.lock
   python scripts/check_no_prerelease.py requirements.lock   # no pre-release pins
   python -m olympus capabilities --check                    # README numbers match code
   python scripts/check_threat_model.py                      # threat model covers tools
   pytest -q
   ```
5. **Run the reliability gate** on a real key (operator-run; needs
   `ANTHROPIC_API_KEY`) so the release is proven to run unattended end-to-end:
   ```bash
   python scripts/reliability_gate.py
   ```
   Exit 0 = all three prompts replayed reproducibly under the spend cap. An
   `INCONCLUSIVE` (provider/credit problem) is not a pass — fix the account and
   re-run.
6. **Open a release PR** with the version + changelog bump and let CI go green.
   Merge it to `main`.
7. **Tag and push** from the merged commit:
   ```bash
   git checkout main && git pull
   git tag v0.16.0          # match pyproject.toml exactly
   git push origin v0.16.0
   ```
   The `Publish to PyPI` workflow runs `olympus sign` (producing the signed
   `verification.json` inside the wheel) and publishes the sdist + wheel.

## Verify the published release

Anyone can confirm authenticity and integrity:

```bash
pip install olympus-council==0.16.0
olympus verify                             # recompute hashes, check the signature
                                           # against the pinned public key
```

`olympus verify` fails — naming the file — if any tracked file drifted or is
missing, if the manifest signature is invalid, or if the manifest was re-signed
with a key other than the pinned one. A recorded run's decision log can be
checked the same way with `olympus verify --log <run_id>`.

## If something is wrong after tagging

A tag is just a pointer; the artifact is what matters. If the published wheel is
bad, **yank** the release on PyPI and ship a fixed PATCH (`vX.Y.Z+1`) rather than
re-pushing the same tag — re-tagging breaks the integrity story for anyone who
already fetched it.
