"""Integration test that the docker backend's hardening actually takes effect at
RUNTIME (not just in the argv). Opt-in: skipped unless a working docker daemon +
the base image are available, so it is a no-op in environments without docker
(CI without a daemon, this dev sandbox) and real proof where docker exists.

test_sandbox.py already unit-tests that the flags land in the argv; this proves
they change container behaviour."""

import pytest

from olympus import sandbox


@pytest.fixture()
def docker_ws(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path / "ws"))
    monkeypatch.setenv("OLYMPUS_EXEC_BACKEND", "docker")
    # Probe: skip unless docker can actually run the base image here.
    probe = sandbox.run("echo docker-ok", be="docker", timeout=60)
    if not (probe.ok and "docker-ok" in probe.output):
        pytest.skip(f"docker backend unavailable: {probe.output[:120]}")
    return tmp_path


def test_all_capabilities_are_dropped_by_default(docker_ws):
    # --cap-drop ALL means the container's effective capability set is empty.
    res = sandbox.run("grep CapEff /proc/self/status", be="docker", timeout=60)
    assert res.ok, res.output
    # CapEff is a hex mask; dropped-all => all zeros.
    hexmask = res.output.split()[-1]
    assert set(hexmask) == {"0"}, f"expected empty CapEff, got {res.output!r}"


def test_no_new_privileges_is_set(docker_ws):
    res = sandbox.run("grep NoNewPrivs /proc/self/status", be="docker", timeout=60)
    assert res.ok and res.output.strip().endswith("1"), res.output


def test_network_is_isolated_by_default(docker_ws):
    # --network none: a DNS/connect attempt must fail inside the container.
    res = sandbox.run(
        "python3 -c \"import socket; socket.create_connection(('1.1.1.1',80),2)\"",
        be="docker", timeout=60)
    assert not res.ok, "network should be unreachable under --network none"


def test_caps_can_be_re_enabled_by_opt_out(docker_ws, monkeypatch):
    monkeypatch.setenv("OLYMPUS_EXEC_CAPS", "on")
    res = sandbox.run("grep CapEff /proc/self/status", be="docker", timeout=60)
    assert res.ok, res.output
    # With caps NOT dropped, the effective set is non-empty.
    assert set(res.output.split()[-1]) != {"0"}
