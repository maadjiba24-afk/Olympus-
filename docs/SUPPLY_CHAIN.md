# Supply-chain integrity

Olympus pins its dependencies, refuses pre-releases, and publishes a Software
Bill of Materials. This is **catch-up to parity**, stated honestly: Ruflo
already locks (`pnpm-lock.yaml` + `package-lock.json`) and audits. Olympus
previously declared only `>=` ranges and shipped **no lockfile** — it was behind
here. This closes that gap, and edges slightly ahead with a pre-release ban and
an SBOM.

## Two files, two jobs

| File | Role |
| --- | --- |
| `requirements.txt` | The human-edited top-level runtime dependencies (loose `>=` ranges — what Olympus *wants*). |
| `requirements.lock` | The fully-resolved, **hash-pinned** dependency set (every transitive package, exact version, SHA-256 hashes — what CI and reproducible installs *get*). |

### Regenerating the lock

The lock is generated with [`uv`](https://docs.astral.sh/uv/):

```bash
uv pip compile requirements.txt --generate-hashes --universal --python-version 3.10 --output-file requirements.lock
```

Re-run this whenever `requirements.txt` changes, then commit the updated lock.

### Installing from the lock

```bash
pip install --require-hashes -r requirements.lock   # exact, verified versions
pip install -e . --no-deps                          # the olympus package itself
```

`--require-hashes` makes pip refuse any package whose download doesn't match a
recorded SHA-256, so a compromised or substituted artifact fails the install.
The lock also carries `cffi` and `pycparser` (cryptography's native backend), so
a lock-based install always has a working vault — no silently-skipped crypto.

## No pre-release dependencies

`scripts/check_no_prerelease.py` scans the lock and exits non-zero if any pin is
a PEP 440 pre-release (`a`/`b`/`c`/`rc`/`alpha`/`beta`/`pre`/`dev`).
Post-releases (`1.0.post1`) and normal releases are allowed. It uses `packaging`
when available and a regex fallback otherwise, so it runs even before
dependencies are installed.

```bash
$ python scripts/check_no_prerelease.py requirements.lock
✓ requirements.lock: no pre-release dependencies.

$ # with a pre-release pin injected:
$ python scripts/check_no_prerelease.py bad.lock
✗ bad.lock: 1 pre-release dependency(ies):
    shady==9.9.9rc1
Refusing pre-release dependencies (1 found). Pin a stable release instead.
```

## SBOM (CycloneDX)

CI generates a CycloneDX 1.6 Software Bill of Materials from the lock and
uploads it as a build artifact (`sbom-cyclonedx`):

```bash
cyclonedx-py requirements requirements.lock --of JSON -o sbom.json
```

The SBOM enumerates every component and version in the dependency tree, so
downstream consumers can scan Olympus's supply chain against vulnerability feeds.

## CI wiring

`.github/workflows/ci.yml`:

- the **test** job runs a Python **version matrix** (3.10–3.13, the full range
  `pyproject` declares) and each leg installs from `requirements.lock` with
  `--require-hashes`, runs the pre-release guard, then the capability gate and
  the test suite. `requirements.lock` is a *universal* hash lock —
  `uv --generate-hashes` records every distribution's hash (all platform/ABI
  wheels + sdist), so one lock satisfies `--require-hashes` on every matrix
  Python; no per-version lock files, no unpinned installs. `test_ci_matrix.py`
  fails the build if the matrix ever drifts from the declared support;
- the **sbom** job generates and uploads the CycloneDX SBOM.

## What this is not (yet)

Real Moat-E differentiation — LTS guarantees, indemnity, SOC2 — is deliberately
**not** here; it's Tier 2/3 of the build plan (`docs/SUPPORT.md` comes with
Task 5). This task is integrity and reproducibility: lock, verify, ban
pre-releases, publish the bill of materials.
