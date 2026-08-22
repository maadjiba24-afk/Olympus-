# Content-addressed PyPI publisher provenance

This local Docker-action descriptor reuses the official PyPA publisher
container without copying or changing its entrypoint, OIDC exchange, Twine
upload code, or installed runtime. It exists only because the supported
upstream composite action generates a mutable SHA-named GHCR tag reference.

Audited upstream identity:

- repository: `pypa/gh-action-pypi-publish`
- release: `v1.14.2`
- commit: `dc37677b2e1c63e2034f94d8a5b11f265b73ba33`
- upstream `action.yml` SHA-256:
  `4833fe3c15180e27fc7af77018b1d8c670abf1f17958d45d5e0feb4f9acc5d3d`
- upstream `create-docker-action.py` SHA-256:
  `28924f16b01d3a63e0a9ef5733d7d261329efcff255d8004a96d27d373d33e8c`
- audited GHCR manifest digest:
  `sha256:a68d05519f6d7e47372aeaddab80b851b69afa89be179ec41775c72c4e3ab2d5`

The descriptor mirrors the canonical inputs that the upstream composite
passes to its generated inner Docker action. The defaults are the effective
upstream v1.14.2 defaults. The sole execution change is that `runs.image`
names the audited manifest digest directly instead of the mutable tag
`dc37677b2e1c63e2034f94d8a5b11f265b73ba33`.

The upstream wrapper's other steps are deliberately unnecessary here:

- Linux is fixed by the workflow's `ubuntu-24.04` runner.
- PATH reset and Python discovery exist only to run the upstream generator.
- The committed descriptor replaces that generator.
- Canonical inputs are already normalized and are consumed by the unchanged
  container entrypoint.

Upgrade as a single reviewed change while publishing is disabled:

1. choose an immutable upstream release commit;
2. audit the upstream action, generator, image build, entrypoint, OIDC
   exchange, dependencies, and release notes;
3. resolve and record the new manifest digest;
4. update this descriptor, provenance file, runtime constants,
   documentation, and tests together;
5. run the full CI suite and merge only after review;
6. keep the publish workflow disabled until a separate activation decision.

The upstream BSD-3-Clause license is retained in `LICENSE.md` beside this
file.
