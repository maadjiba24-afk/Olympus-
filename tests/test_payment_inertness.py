"""Inertness invariants for the live payment path (complements test_paylive.py).

These prove — without needing the crypto backend — that real money cannot move
in the shipped build and, crucially, that NO module anywhere in the codebase can
perform either of the two human acts that going live requires. This is the
structural backstop: even a future refactor (a new tool, an evolve step, a
scaffold change) cannot silently flip the switch without failing a test.
"""
import ast
import pathlib
import re

import pytest

from olympus import payrail, paylive


@pytest.fixture(autouse=True)
def _reset_adapter():
    paylive.reset_live_adapter()
    yield
    paylive.reset_live_adapter()


class _DummyLive(paylive.LiveAdapter):
    """A stand-in real adapter for tests — it never actually charges."""
    name = "dummy-test"

    def charge(self, **_k):
        return {"ok": False, "charge_id": ""}

    def lookup(self, _cid):
        return {}


# --- is_inert(): the single safety predicate ----------------------------

def test_is_inert_in_shipped_state(monkeypatch):
    monkeypatch.delenv("OLYMPUS_PAYMENT_LIVE", raising=False)
    assert paylive.is_inert() is True
    assert paylive.adapter_ready() is False


def test_flag_alone_stays_inert(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PAYMENT_LIVE", "1")
    # flag set but no adapter → still inert
    assert paylive.is_inert() is True


def test_adapter_alone_stays_inert(monkeypatch):
    monkeypatch.delenv("OLYMPUS_PAYMENT_LIVE", raising=False)
    paylive.register_live_adapter(_DummyLive())
    assert paylive.adapter_ready() is True
    assert paylive.is_inert() is True          # flag still unset → inert


def test_only_both_acts_lift_inertness(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PAYMENT_LIVE", "1")
    paylive.register_live_adapter(_DummyLive())
    assert paylive.is_inert() is False         # both acts → no longer inert
    # (a charge would STILL have to clear mandate/key/caps/contract gates)


def test_reset_returns_to_inert(monkeypatch):
    monkeypatch.setenv("OLYMPUS_PAYMENT_LIVE", "1")
    paylive.register_live_adapter(_DummyLive())
    paylive.reset_live_adapter()
    assert paylive.is_inert() is True


# --- installing a real adapter is never silent --------------------------

def test_registering_real_adapter_logs_a_warning(caplog):
    with caplog.at_level("WARNING", logger="olympus.paylive"):
        paylive.register_live_adapter(_DummyLive())
    assert any("LIVE PAYMENT ADAPTER INSTALLED" in r.message for r in caplog.records)


def test_resetting_to_disabled_is_quiet(caplog):
    with caplog.at_level("WARNING", logger="olympus.paylive"):
        paylive.reset_live_adapter()
    assert not any("LIVE PAYMENT ADAPTER INSTALLED" in r.message
                   for r in caplog.records)


def test_disabled_default_refuses_and_is_not_ready():
    assert isinstance(paylive._adapter, paylive.DisabledLiveAdapter)
    with pytest.raises(payrail.PaymentLiveError):
        paylive.DisabledLiveAdapter().charge(
            amount=1, currency="USD", merchant="m", idempotency_key="k")


# --- the structural backstop: NO module performs either human act -------

def _olympus_modules():
    root = pathlib.Path(__file__).resolve().parent.parent / "olympus"
    return sorted(root.glob("*.py"))


def test_no_module_calls_register_live_adapter():
    """No shipped module installs a live adapter (human act #2). Only the
    definition in paylive.py and test code may mention it — a CALL anywhere in
    `olympus/` fails this test."""
    offenders = []
    for f in _olympus_modules():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = (fn.attr if isinstance(fn, ast.Attribute)
                        else getattr(fn, "id", ""))
                if name == "register_live_adapter":
                    offenders.append(f"{f.name}:{node.lineno}")
    assert offenders == [], f"a shipped module installs a live adapter: {offenders}"


def test_no_module_writes_the_live_flag():
    """No shipped module sets OLYMPUS_PAYMENT_LIVE (human act #1). Reads
    (`os.environ.get(...)`) are fine; writes are not."""
    write = re.compile(
        r"""(environ\[\s*['"]OLYMPUS_PAYMENT_LIVE['"]\s*\]\s*=(?!=)"""
        r"""|(?:setenv|putenv|setdefault)\(\s*['"]OLYMPUS_PAYMENT_LIVE['"])""")
    offenders = []
    for f in _olympus_modules():
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if write.search(line):
                offenders.append(f"{f.name}:{i}")
    assert offenders == [], f"a shipped module writes the live flag: {offenders}"


def test_paylive_imports_no_network_stack():
    """'There is no network code here at all.' — the module imports no network
    library, so it cannot itself reach a processor."""
    tree = ast.parse((pathlib.Path(paylive.__file__)).read_text(encoding="utf-8"))
    banned = {"socket", "ssl", "http", "urllib", "requests", "httpx",
              "aiohttp", "urllib3", "asyncio"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & banned), f"paylive imports a network stack: {imported & banned}"
