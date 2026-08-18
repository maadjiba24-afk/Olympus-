# Releasing Olympus

A release is cut by tagging a reviewed commit on `main` and then **manually
dispatching** the `Publish to PyPI` workflow with that tag as its input. A
tag on its own publishes nothing. The workflow verifies, signs, builds,
independently inspects, and publishes in five isolated jobs. Dispatching is
the only step that requires maintainer rights; everything before it is
preparation you can verify locally.

At activation, both credentialed environments must admit only the protected `main` branch. The supplied `v*` value is an input to that
branch-triggered run; tag immutability is enforced separately by the tag
ruleset.

See [docs/SUPPORT.md](docs/SUPPORT.md) for the versioning/LTS policy and
[docs/SIGNING.md](docs/SIGNING.md) for signing-key custody.

## PUBLICATION IS CURRENTLY DISABLED — activation blockers

The `Publish to PyPI` workflow is **disabled** on GitHub
(`disabled_manually`, workflow ID 295240176) and pushing a `v*` tag will not
publish anything. It must NOT be re-enabled until every precondition below is
met, in order, and none of them can be delivered by a change to this
repository's files — they are GitHub/PyPI **settings**:

1. **Create and protect the `release-signing` environment.** It does not
   exist yet. The pipeline signs in a job bound to `release-signing` and
   publishes from a job bound to `pypi`, so that the signing seed and the
   PyPI OIDC credential are never reachable from the same environment.
   Require reviewers and restrict deployments to the protected `main` branch
   only. In **Deployment branches and tags**, select the branch `main`; do not
   add a `v*` tag pattern. A dispatched run's triggering ref is `main` even
   though its separately validated input names a release tag.
2. **Protect the `pypi` environment.** It currently has no protection rules,
   no deployment branch/tag policy, and admin bypass enabled — any ref could
   reach the OIDC credential. Require reviewers, restrict deployments to the
   protected `main` branch only, and address `can_admins_bypass` (an admin
   bypass makes every other rule advisory). As above, do not configure `v*`
   as an environment deployment tag: it is an input, not this run's ref.
3. **Protect `v*` tags with TWO rulesets, not one.** Any push-capable
   identity could otherwise create, move, or delete a release tag. The
   split is not stylistic — see the correction below.

   - **`immutable-tags`** — target `refs/tags/v*`; rules **Restrict
     updates** and **Restrict deletions**; **no bypass** (empty bypass
     list), enforcement Active. A published tag can never be repointed or
     removed, by anyone, administrators included.
   - **`controlled-tag-creation`** — target `refs/tags/v*`; rule **Restrict
     creations** only; bypass **RepositoryRole `admin` only**, mode
     `always`; enforcement Active. Cutting a release stays possible for an
     authorized operator and impossible for everyone else.

   **Why not one ruleset.** Carrying creation + update + deletion on a
   single ruleset with an empty bypass list — the instruction this document
   used to give — would block **all future `v*` tag creation**, including by
   the release operator. Ruleset bypass is explicit opt-in: repository
   administrators are *not* exempt unless added. That configuration makes
   releasing impossible, which is why it must not be used.

   **Proven, not assumed.** The composition was exercised on a disposable
   `refs/tags/ztest-step1l-*` pattern with two temporary rulesets of the
   same shape: creating the tag as the admin was allowed (GitHub logged
   `Bypassed rule violations`), while force-moving it and deleting it were
   both refused with `GH013`, and the tag still pointed at its original
   commit afterwards. Rules from separate rulesets aggregate, and a bypass
   grant applies only to the ruleset that carries it. The temporary
   rulesets and tag were removed after the proof; no real `v*` tag was
   touched.
4. **Move `OLYMPUS_SIGNING_SEED` from repository scope to the protected
   `release-signing` environment scope** (never `pypi`), and assess
   **rotation**: as a repository-scoped secret it has been exposable to
   every workflow job that requested it, so it must be treated as
   potentially over-shared and rotated before first use.
5. **Verify the PyPI trusted publisher binding manually**: the PyPI project
   must bind publisher `publish.yml` in this repository AND environment
   `pypi`. This cannot be verified from the repository without credentials
   (recorded as PYPI_TRUST_BINDING=UNVERIFIED until an operator confirms).
6. **Resolve the mutable publisher container**, tracked as
   **MUTABLE_PUBLISH_CONTAINER=BLOCKED** — see below.
7. **A separate, reviewed activation authorization** — re-enabling the
   workflow is its own decision with its own review, never a side effect of
   merging this or any other change.

### MUTABLE_PUBLISH_CONTAINER=BLOCKED

**This section previously described the wrong mechanism.** The blocker is
real, but the reason recorded through v6 was not. It is corrected here
because a reviewer cannot validate a blocker whose stated mechanism is
false.

Inspected at the pinned commit `dc37677b2e1c63e2034f94d8a5b11f265b73ba33`
(v1.14.2): `pypa/gh-action-pypi-publish` is a composite action that writes a
Docker action at run time via `create-docker-action.py`. That script selects
the image from the **consumer's** repository id:

```python
REPO_ID_GH_ACTION = '178055147'          # the ACTION's own repository

def set_image(ref, repo, repo_id):
    if repo_id == REPO_ID_GH_ACTION:
        return str(ACTION_SHELL_CHECKOUT_PATH / 'Dockerfile')
    return f'docker://ghcr.io/{repo}:{ref.replace("/", "-")}'
```

`action.yml` supplies `REPO_ID` as `github.repository_id` — *ours*, not the
action's. The Dockerfile branch therefore fires **only when a workflow runs
inside `pypa/gh-action-pypi-publish` itself** (its own CI). Every external
consumer, this repository included, takes the second branch and pulls the
**prebuilt** image:

```
ghcr.io/pypa/gh-action-pypi-publish:dc37677b2e1c63e2034f94d8a5b11f265b73ba33
```

So no base image is resolved on our runner: whatever base PyPA built from
was fixed when *they* built and pushed that image. The earlier claim that a
mutable base tag is resolved at our run time was simply wrong, and the
parenthetical attributing the GHCR path to "forks" was backwards — it is
the path every consumer takes.

**The blocker survives the correction, for a different reason.** That
reference is a **tag**, and a tag is a mutable pointer: the GHCR package
owner can repoint `:dc37677b…` at a different manifest **digest** at any
time. So **this pipeline is NOT fully content-addressed.** Every GitHub
Action is pinned by commit SHA, but the container that ultimately handles
the OIDC token and uploads to PyPI is reached through a name that can move.

**What v7 adds.** `inspect` — which holds neither the signing seed nor the
OIDC credential — resolves that tag anonymously and fails closed unless it
still yields the audited digest
`sha256:a68d05519f6d7e47372aeaddab80b851b69afa89be179ec41775c72c4e3ab2d5`,
pinned in `scripts/release_pipeline.py` and exercised by
`tests/test_release_runtime_image.py`. State its limits honestly:

- it **blocks a mismatch that is observable while `inspect` runs** — that is
  the whole of what it guarantees;
- it does **not bind the tag**. A repoint landing *after* `inspect` has read
  the digest is still pulled by `publish`, because the two jobs resolve the
  same mutable name at different times. This is a **TOCTOU window**, not a
  closed door, and the gate must not be described as making `publish`
  ineligible or as catching every repoint;
- it does **not** make the pipeline content-addressed.

Treat it as an alarm on a door that is still unlocked: worth having, and no
substitute for a lock.

**Why the digest is not pinned directly.** The image reference is computed
inside `create-docker-action.py` from `github.action_ref`, which must be a
git ref — an OCI digest is not a valid `uses:` target — and the action
exposes no input to override it. Invoking the container directly as
`docker://ghcr.io/pypa/gh-action-pypi-publish@sha256:…` would be genuinely
content-addressed and would reuse the same audited container (it does *not*
require reimplementing the OIDC exchange, contrary to the v6 note), but it
is **unsupported** by upstream: it bypasses the composite action's PATH
reset, Python discovery, and input normalization, and makes us own the
upgrade path. It is therefore **not adopted**.

Until this is resolved — upstream digest-pinned container execution, or a
reviewed vendored equivalent — activation stays blocked and the claim
"fully content-addressed" must not be made anywhere.

## One-time setup

- **Signing seed.** Set `OLYMPUS_SIGNING_SEED` as a secret of the protected
  **`release-signing` environment**. Repository-scoped placement is
  forbidden: it exposes the seed to every workflow job that asks for it
  (activation blocker 4). The signing job derives the Ed25519 key from it, and
  the narrow signer **refuses to sign** unless the derived public key equals
  the pinned key in `olympus/witness_pubkey.txt` exactly.
- **Pinned public key.** Commit the production public key to
  `olympus/witness_pubkey.txt` — exactly one key for a release, so signer
  identity is unambiguous. `olympus verify`, the build job, and the independent
  inspect job all trust only that key.
- **PyPI trusted publisher.** Create the project on PyPI and add this repo +
  `publish.yml` + environment `pypi` as a Trusted Publisher. The pipeline is
  OIDC-only by design: there is no API-token path, because a long-lived
  token secret would outlive and outrank every gate in the workflow.

## Release checklist

1. **Pick the version** per SemVer (`docs/SUPPORT.md`): PATCH for fixes only,
   MINOR for new backward-compatible capabilities, MAJOR for incompatible CLI /
   public-API / memory-`schema_version` changes (add a migration note).
2. **Bump the version + cut the changelog in one step:**
   ```bash
   python scripts/bump_version.py patch      # or minor / major / --set X.Y.Z
   ```
   This updates `pyproject.toml` (the single source of truth), the
   `olympus/__init__.py` fallback, and `CHANGELOG.md` (promotes `[Unreleased]`
   to a dated `[X.Y.Z]` section, adds a fresh `[Unreleased]`, fixes the compare
   links). Use `--dry-run` to preview. Then polish the new changelog notes by
   hand (a MAJOR bump needs a migration note).
3. **Run the gates locally.** These are the gates the `verify` job runs; it
   installs the unified `requirements-publish.lock` (runtime + test + build
   resolved together) rather than several locks in sequence. Release lock
   evidence is produced on Ubuntu 24.04 with CPython 3.12.3 and pip 24.0;
   use that exact environment when regenerating or proving the locks:
   ```bash
   pip install --require-hashes -r requirements-publish.lock
   pip install -e . --no-deps --no-build-isolation
   python scripts/check_no_prerelease.py requirements.lock
   python scripts/check_no_prerelease.py requirements-publish.lock
   python scripts/check_no_prerelease.py requirements-signing.lock
   python -m compileall -q olympus
   python -m olympus capabilities --check      # README numbers match code
   python scripts/check_threat_model.py        # threat model covers tools
   python scripts/noninterference_gate.py
   pytest -q
   ```
4. **Run the reliability gate** on a real key (operator-run; needs
   `ANTHROPIC_API_KEY`) so the release is proven to run unattended end-to-end:
   ```bash
   python scripts/reliability_gate.py
   ```
   Exit 0 = all three prompts replayed reproducibly under the spend cap. An
   `INCONCLUSIVE` (provider/credit problem) is not a pass — fix the account and
   re-run.
5. **Open a release PR** with the version + changelog bump and let CI go green.
   Merge it to `main`.
6. **Tag the merged commit.** Pushing the tag publishes NOTHING — it only
   creates the pointer the release run will be checked against:
   ```bash
   git checkout main && git pull
   git tag v0.16.0          # must match pyproject.toml exactly
   git push origin v0.16.0
   ```
7. **Dispatch the release run** (only once activation is authorized — see
   the blockers above). The workflow is `workflow_dispatch`-only: on the
   Actions tab choose `Publish to PyPI`, press **Run workflow**, select the
   `main` branch, and supply the tag as the `tag` input; or:
   ```bash
   gh workflow run publish.yml --ref main -f tag=v0.16.0
   ```
   The run is dispatch-only by design: a tag-push trigger would let an old
   tag execute an obsolete copy of the workflow. The `verify` job then
   proves, at runtime, that this run's commit is the protected `main` tip,
   that the supplied tag exists and peels exactly to that commit, and that
   the tag, source, and runtime versions are identical. `sign` produces the
   signed `verification.json` in an isolated environment and uploads it by
   immutable artifact ID. `build` verifies that external manifest against its
   own checkout before creating an untrusted dist artifact. On another fresh
   runner, `inspect` downloads both artifacts by ID, cryptographically verifies
   the manifest again against the expected commit, and validates the exact
   wheel and sdist. Only then does `publish` download the inspect-forwarded
   dist artifact ID and upload it.

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
