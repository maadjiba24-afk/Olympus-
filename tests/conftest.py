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
