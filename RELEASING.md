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
   `release-signing` environment scope** (never `pypi`) and **rotate it (completed)**. The retired repository-scoped seed was treated as potentially
   over-shared because every workflow job that requested it could access it.
   **This blocker is now CLOSED**: the replacement seed is stored only in the
   protected `release-signing` environment, the repository-scoped copy has
   been deleted, and `pypi` holds no signing seed. The rotation is recorded in
   [docs/RELEASE_SIGNING_KEYS.md](docs/RELEASE_SIGNING_KEYS.md). Publishing
   remains disabled while the remaining activation blockers are open. The
   exact ordered procedure is
   [Rotating the release signing key](#rotating-the-release-signing-key-flag-day)
   below; it is a flag-day replacement performed while publishing stays
   disabled.

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

- **Signing seed.** `OLYMPUS_SIGNING_SEED` is stored only as a secret of
  the protected **`release-signing` environment**. The repository-scoped copy
  of the retired seed has been deleted, and `pypi` holds no signing seed.
  Repository-scoped placement remains forbidden because it exposes the seed
  to every workflow job that asks for it (activation blocker 4, closed). The
  signing job derives the Ed25519 key from it, and the narrow signer
  **refuses to sign** unless the derived public key equals the pinned key in
  `olympus/witness_pubkey.txt` exactly.

- **Pinned public key.** Commit the production public key to
  `olympus/witness_pubkey.txt` — **exactly one active key** for a release, so
  signer identity is unambiguous. `olympus verify`, the build job, and the
  independent inspect job all trust only that key. Multi-key overlap is a
  runtime/instance facility (`OLYMPUS_PINNED_PUBKEY`, see
  [docs/SIGNING.md](docs/SIGNING.md)) and must never be applied to this file.
  Every activation and retirement is recorded in
  [docs/RELEASE_SIGNING_KEYS.md](docs/RELEASE_SIGNING_KEYS.md).
- **PyPI trusted publisher.** Create the project on PyPI and add this repo +
  `publish.yml` + environment `pypi` as a Trusted Publisher. The pipeline is
  OIDC-only by design: there is no API-token path, because a long-lived
  token secret would outlive and outrank every gate in the workflow.

## Rotating the release signing key (flag day)

The release trust anchor holds **exactly one active key**, so rotation is not
an overlap — it is a **fail-closed, forward-only flag-day replacement**. Run
it while publishing is disabled. The overlap-rotation section of
[docs/SIGNING.md](docs/SIGNING.md) covers a separate deployment trust domain
and must not be used here.

**Why a flag day is safe.** Nothing already published stops verifying. A
released wheel carries its own copy of `witness_pubkey.txt`, so
`pip install olympus-council==X && olympus verify` keeps checking that wheel
against the key that was active when it was built. Retired keys stay
recoverable through [docs/RELEASE_SIGNING_KEYS.md](docs/RELEASE_SIGNING_KEYS.md)
and can be pinned explicitly out of band
(`OLYMPUS_PINNED_PUBKEY=<retired key>`) to check a historical manifest — they
are never returned to the active set.

**Ordering is enforced by the signer, not by discipline.**
`scripts/release_pipeline.py` refuses to sign unless the derived public key
equals the pinned key exactly, so a half-finished rotation cannot produce a
signature: it can only fail closed.

1. **Confirm publishing is disabled.** `publish.yml` must be
   `disabled_manually`, with no release in flight. **Do not enable the
   workflow at any point in this procedure, and do not create a disposable
   tag or test release to exercise it.** Rotation is complete when the
   material and the pin agree; proving it end-to-end belongs to the separate,
   reviewed activation decision (blocker 7).
2. **Generate the new seed on a clean machine, off-platform.**
   ```bash
   olympus keygen --out <path>          # writes mode 0600
   ```
   It prints **only** the derived public key and where the seed lives — the
   seed itself is never printed. Record the public key. Never transcribe,
   echo, copy, or log the seed, and never place it on a clipboard.
3. **Back the new seed up securely, before anything is merged.** Take an
   offline backup of the mode-`0600` seed file — an encrypted volume or a
   password manager's file attachment, held by whoever may need to retry
   step 5. Do this **before** the PR in step 4 merges. After that merge the
   new key is the only key the pipeline will accept, so losing the seed at
   that point means rotating forward again rather than recovering.
4. **Replace the pin and record it, in ONE reviewed PR carrying TWO
   commits.** A ledger event cites the commit that changed the pin, and a
   commit cannot cite its own hash — so the pin lands first and the ledger
   references it:

   - **Commit A** — replace the single line in `olympus/witness_pubkey.txt`
     with the new public key. Nothing else.
   - **Capture commit A's SHA *and its UTC committer date* now**, after
     committing A and *before* writing commit B. At this point `HEAD` is
     commit A:
     ```bash
     TZ=UTC0 git show -s --format='%H %cd' --date=format-local:%Y-%m-%d HEAD
     ```
     That prints the full 40-character SHA and the committer date **in UTC**
     on one line — save both. `TZ=UTC0` matters: without it the date is
     rendered in the committer's local zone and can land a day either side of
     the UTC date the ledger requires.
   - **Commit B** — append **two events** to
     [docs/RELEASE_SIGNING_KEYS.md](docs/RELEASE_SIGNING_KEYS.md): `RETIRED`
     for the outgoing key and `ACTIVATED` for its replacement. **Both rows
     carry the SHA and the UTC date captured above** — not today's date, not
     the date commit B was written. The two rows describe one event evidenced
     by one commit, so they share one date.
   - **Verify afterwards.** Once commit B exists, `git rev-parse HEAD~1`
     confirms B's parent is A and should match the cited SHA. That is a
     *check*, not the way A's SHA is obtained — run before B exists, `HEAD~1`
     names the commit **before** A and would put the wrong SHA in the ledger.

   Push both commits together and open one PR. Do not edit existing ledger
   events. **Merge with a merge commit — never squash, never rebase**, so
   commit A survives as an ancestor of `main` and the SHA the ledger cites
   stays resolvable forever. A squash merge rewrites commit A out of
   existence and turns every ledger citation into a dangling reference.

   Do this *before* installing the secret. Until the pin matches, every
   signing attempt fails closed; install the secret first and you get the
   same failure with less clarity about why.
5. **Install the matching seed in the `release-signing` environment**, by
   streaming the seed file straight from disk into the secret — it is never
   rendered, never on a clipboard, never an argument:
   ```bash
   gh secret set OLYMPUS_SIGNING_SEED --env release-signing < <path>
   ```
   `gh` reads the secret from stdin when no `--body` flag is given. Never use
   `--body`, never pass the value as a command argument, never `echo` or
   `cat` it to a terminal, never let it reach shell history, and never paste
   it through a clipboard. Never add it to `pypi`.
6. **Confirm the new environment secret is really there — before deleting
   anything.** Deleting first would leave no signing material at all if step 5
   silently failed:
   ```bash
   # expect total 1, names ["OLYMPUS_SIGNING_SEED"]
   gh api repos/<owner>/<repo>/environments/release-signing/secrets \
     --jq '{total:.total_count, names:[.secrets[].name]}'
   ```
   Do not proceed until this shows the name at environment scope.
7. **Now delete the repository-scoped secret.** Until this is done the old,
   over-shared value remains reachable by every workflow job that asks for
   it.
   ```bash
   gh secret delete OLYMPUS_SIGNING_SEED          # repository scope
   ```
8. **Verify the final placement by metadata only** — no value is ever read,
   because the API cannot return one:
   ```bash
   # repository scope is gone                      → HTTP 404
   gh api repos/<owner>/<repo>/actions/secrets/OLYMPUS_SIGNING_SEED
   # environment scope still holds the name        → total 1
   gh api repos/<owner>/<repo>/environments/release-signing/secrets \
     --jq '{total:.total_count, names:[.secrets[].name]}'
   # it never landed in the publishing environment → 0
   gh api repos/<owner>/<repo>/environments/pypi/secrets --jq '.total_count'
   ```
   Be precise about what this proves: **metadata confirms only that a secret
   of that NAME exists in that SCOPE.** It cannot show the stored value, so it
   cannot prove the installed seed derives the pinned key — a secret of the
   right name holding the wrong bytes passes every check above. Also confirm
   `olympus/witness_pubkey.txt` holds exactly the new key and that the
   ledger's replayed state agrees — `tests/test_release_signing_keys.py`
   checks both on every CI run.
9. **The next signing attempt is the only cryptographic proof.** Whenever a
   release is next authorised, the `sign` job either derives exactly the
   pinned key or refuses. A mismatch — wrong seed installed, pin not merged,
   secret in the wrong scope — cannot yield a signed manifest. That
   fail-closed check, not any metadata query, is what establishes that the
   right material is in place.

### Rotation is forward-only

**Before the step-4 PR merges, the rotation can be abandoned safely.** Close
the PR and destroy the new seed and its backup; `main` is untouched, the old
key remains active, and nothing has changed.

**Merging the step-4 PR is the point of no return.** From that moment the new
key is the only key `pinned_key()` accepts, and the old key is retired in the
ledger. The retired key is also the over-shared one this rotation exists to
replace, so it is **never reactivated** — not to unblock a failed secret
install, not to cut an urgent release. Reverting the pin would put a key known
to have been exposed to every workflow job back into the active trust set.

Recovery is therefore always forward:

- **Secret installation fails or was wrong** — retry step 5 using the
  securely retained seed from step 3. The pin does not change.
- **The new seed is lost** — generate another replacement (step 2) and
  perform **another forward rotation**: a new PR with the same two-commit
  shape, retiring the key that was just activated and activating its
  successor. Two rotations in the ledger is a correct history, not a mess to
  be tidied.

`CORRECTION` events exist only to fix **provenance** — a mistyped commit SHA
or date in an earlier row. They never change which key is active, and they are
never a way to undo a rotation.

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
