"""Teardown-loop iteration 3: webhook rate-limiting, rotation thread-safety,
and bounded background-thread registry."""

import threading

from olympus import openai_compat as oc
from olympus import webhook_gateway as wh


# --- webhook rate limiting --------------------------------------------------

def _reset_rl():
    with wh._HITS_LOCK:
        wh._HITS.clear()


def test_webhook_rate_limit_trips(monkeypatch):
    _reset_rl()
    monkeypatch.setenv("OLYMPUS_WEBHOOK_RATE_LIMIT", "3")
    allowed = [not wh._rate_limited("1.2.3.4") for _ in range(5)]
    assert allowed == [True, True, True, False, False]


def test_webhook_rate_limit_per_ip(monkeypatch):
    _reset_rl()
    monkeypatch.setenv("OLYMPUS_WEBHOOK_RATE_LIMIT", "2")
    assert not wh._rate_limited("a")
    assert not wh._rate_limited("b")      # different IP has its own budget
    assert not wh._rate_limited("a")
    assert wh._rate_limited("a")          # a's third within the window → limited
    assert not wh._rate_limited("b")      # b still under budget


def test_webhook_rate_limit_disabled(monkeypatch):
    _reset_rl()
    monkeypatch.setenv("OLYMPUS_WEBHOOK_RATE_LIMIT", "0")
    assert not any(wh._rate_limited("x") for _ in range(50))


def test_webhook_secret_still_enforced(monkeypatch):
    monkeypatch.setenv("OLYMPUS_WEBHOOK_SECRET", "s3cret")
    assert wh._secret_ok("s3cret") and not wh._secret_ok("nope")


# --- rotation state thread-safety -------------------------------------------

def test_rotation_state_concurrent_use_is_consistent(monkeypatch):
    oc._key_cursor.clear()
    oc._key_stats.clear()
    oc._exhausted.clear()

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return (b'{"choices":[{"message":{"content":"ok"}}],'
                    b'"usage":{"prompt_tokens":1,"completion_tokens":1}}')

    monkeypatch.setattr(oc.urllib.request, "urlopen",
                        lambda req, timeout=600: _Resp())
    from olympus import config
    s = config.Settings(provider="openai", model="m", base_url="http://x/v1",
                        api_key="k1", api_keys=("k1", "k2", "k3"))

    def hammer():
        for _ in range(25):
            oc._post(s, {"model": "m", "messages": []})

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    # 8 threads * 25 calls = 200 successes recorded, none lost to a race.
    total = sum(oc._key_stats.get("http://x/v1", {}).values())
    assert total == 200


# --- bounded bg-thread registry ---------------------------------------------

def test_bg_registry_does_not_leak_finished_threads(monkeypatch):
    from olympus import orchestrator, tui
    monkeypatch.setattr(orchestrator.Olympus, "ask", lambda self, q: "done")
    tui._BG_THREADS.clear()
    bot = orchestrator.Olympus(user="bgreg")
    for i in range(30):
        tui.start_background_task(bot, f"task {i}")
    for t in list(tui._BG_THREADS):
        t.join(timeout=5)
    # Fire one more; the registry compacts finished threads on each call.
    tui.start_background_task(bot, "final")
    tui._BG_THREADS[-1].join(timeout=5)
    alive_or_recent = len(tui._BG_THREADS)
    assert alive_or_recent < 30           # not an ever-growing list
