import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from olympus import config, memory  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    """Every test gets a fresh memory dir and the shared user namespace."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    memory.set_user("shared")
    yield


@pytest.fixture(autouse=True)
def configured_model(monkeypatch):
    """Olympus assumes NO model (vendor-neutral), so a chosen model is now an
    explicit precondition rather than an ambient default. Most tests exercise a
    *configured* system, so choose one here; tests of the unconfigured path
    (tests/test_no_default_model.py) delete it again explicitly."""
    monkeypatch.setenv("OLYMPUS_MODEL", "claude-opus-4-8")
    yield


@pytest.fixture(autouse=True)
def preserve_environ():
    """Snapshot and restore os.environ around every test. Some code paths (the
    setup wizard's `firstrun._save`) write provider config straight into
    os.environ; without this, a test that configures e.g. OLYMPUS_PROVIDER=openai
    would leak that into later tests, which then attempt real network calls."""
    import os
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture
def qualify_sleeptime(monkeypatch):
    """Owned model fixture traversing proposal persistence and signed graduation.

    This is synthetic test evidence, never a genuine-data checkpoint. Tests
    cannot mint qualification by incrementing the obsolete counter document.
    """
    from olympus import sleeptime, usermem
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "owned-graduation-test-only")
    def qualify(count=1):
        owner = "fixture:graduation"
        if not usermem.active_memories(owner):
            for suffix in ("this year", "as scheduled"):
                usermem.add_memory(owner, type="project", confidence=.9,
                    content="Owned Alpha fixture ships in the first quarter " + suffix)
        for _ in range(count):
            summary = sleeptime.refine_user(owner,
                generator=lambda rows: "Owned Alpha fixture ships in the first quarter.",
                verifier=lambda rows, text: {"supported": True, "confidence": .95},
                auto_apply=False)
            assert summary["clean"] and summary["proposed"] == 1
            sleeptime._record_cycle(True, 1, 0,
                proposal_refs=[(owner, key) for key in summary["proposal_ids"]])
        return sleeptime.state()
    return qualify


@pytest.fixture
def owned_prompt_benchmark(monkeypatch):
    from olympus import evals
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "owned-prompt-fixture-only")
    cases = [{"id": "owned-argus", "specialist": "argus", "task": "Owned test task",
              "criteria": "Owned deterministic benchmark"}]
    monkeypatch.setattr(evals, "load_benchmarks", lambda **kwargs: cases)
    monkeypatch.setattr(evals, "run", lambda *args, **kwargs: {
        "avg": 8.0, "items": [{"id": "owned-argus", "score": 8.0,
                               "justification": "Owned mock result"}]})


# --- crypto-backend gating (shared skip mechanism) --------------------------
# `cryptography` is a REQUIRED dependency, so in any correctly-provisioned
# environment (CI, normal dev) the vault (Fernet) and signing (Ed25519) paths
# work and their tests run in full. But the native backend can be *present yet
# broken* — a missing `_cffi_backend` or a panicking pyo3 build makes the vault
# and witness modules set their own `_HAVE_CRYPTO=False` and refuse to encrypt /
# sign. In that (defective) environment the affected tests can't exercise their
# subject at all, so they should SKIP with a clear reason rather than erroring in
# a way that looks like a code regression. This is the single, consistent gate
# for that (replacing the previous ad-hoc mix of `vault._HAVE_CRYPTO` /
# `vault.available()` inline guards): mark such a test `@pytest.mark.requires_crypto`.

def _crypto_backend_works() -> bool:
    """True only if the cryptography native backend can actually run — both the
    Fernet (vault) and Ed25519 (signing) primitives the suite depends on."""
    try:
        from cryptography.fernet import Fernet
        Fernet(Fernet.generate_key())                      # vault path
        from cryptography.hazmat.primitives.asymmetric import ed25519
        ed25519.Ed25519PrivateKey.generate()               # signing path
        return True
    except BaseException:      # ImportError, or a broken/panicking native build
        return False


_CRYPTO_OK = _crypto_backend_works()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_crypto: skip when the cryptography native backend is "
        "non-functional (vault encryption / Ed25519 signing can't run)")


def pytest_collection_modifyitems(config, items):
    if _CRYPTO_OK:
        return
    skip = pytest.mark.skip(
        reason="cryptography backend non-functional (missing/broken native "
        "lib) — vault encryption / signing can't be exercised")
    for item in items:
        if "requires_crypto" in item.keywords:
            item.add_marker(skip)
