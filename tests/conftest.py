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
