"""W2-C5 — the persistent artifact ingestion gate.

Three layers of evidence, matching the spec's three test obligations:

1. **The permanent malformed corpus** (`tests/fixtures/ingest_corpus/*.json`) —
   one committed file per real malformation, positive controls included. It
   grows with every real-world malformation we meet; it never shrinks.
2. **Seeded property-style sweeps** (stdlib `random`, no `hypothesis`) that
   mutate a valid artifact 500+ ways and assert every outcome is either
   accepted-and-byte-identical or REFUSED — never silently repaired (W2-I5.4).
3. **Targeted invariant tests** for W2-I5.1 (fail-closed registry), W2-I5.2
   (never repair a persistent artifact), W2-I5.3 (retained signed evidence),
   the ephemeral/persistent boundary, and the flag-off pass-through.

Corpus case schema (the four spec-mandated keys, plus optional keys that JSON
cannot otherwise express):

    {"name", "kind", "expect": {"refused": bool, "reasons": [...]},
     # exactly one payload form:
     "payload":       <json value>,        # handed to check() as a dict
     "payload_text":  "...",               # handed to check() as str
     "payload_b64":   "...",               # handed to check() as bytes
     "payload_build": {"op": ...},        # generated (oversize/depth bomb)
     # optional:
     "source": "...", "provenance": {...}, "note": "..."}

`provenance.sha256 == "auto"` is resolved by this loader to the real payload
hash, so a fixture can exercise the *valid provenance* path without embedding a
hash that would rot the moment the payload is edited.
"""

from __future__ import annotations

import base64
import copy
import json
import random
from pathlib import Path

import pytest

from olympus import ingestgate

CORPUS_DIR = Path(__file__).parent / "fixtures" / "ingest_corpus"

# The kinds the spec enumerates. A registry that loses one of these silently
# reopens an ungated import path, so the list is asserted, not assumed.
SPEC_KINDS = (
    "plugin", "skill", "mcp_payload", "memory_import", "session_import",
    "model_endpoint", "config_bundle", "replay_fixture", "eval_corpus",
    "policy_file", "tool_schema", "provider_metadata",
)


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    """Every test here exercises the ENFORCING gate unless it says so."""
    monkeypatch.setenv("OLYMPUS_INGESTGATE", "1")
    yield


# --- corpus loading ---------------------------------------------------------


def _build_payload(build: dict):
    op = build["op"]
    if op == "fill":
        base = copy.deepcopy(build["base"])
        base[build["field"]] = build["char"] * build["length"]
        return base
    if op == "nest":
        depth = build["depth"]
        return build["prefix"] + "[" * depth + "]" * depth + build["suffix"]
    if op == "keys":
        base = copy.deepcopy(build["base"])
        base[build["field"]] = {f"k{i}": i for i in range(build["count"])}
        return base
    raise AssertionError(f"unknown corpus build op {op!r}")


def _payload_of(case: dict):
    forms = [k for k in ("payload", "payload_text", "payload_b64",
                         "payload_build") if k in case]
    assert len(forms) == 1, f"{case['name']}: exactly one payload form"
    if "payload" in case:
        return case["payload"]
    if "payload_text" in case:
        return case["payload_text"]
    if "payload_b64" in case:
        return base64.b64decode(case["payload_b64"])
    return _build_payload(case["payload_build"])


def _provenance_of(case: dict, payload):
    prov = case.get("provenance")
    if prov is None:
        return None
    prov = dict(prov)
    if prov.get("sha256") == "auto":
        prov["sha256"] = ingestgate.payload_sha256(payload)
    return prov


def _load_corpus() -> list[dict]:
    cases = []
    for path in sorted(CORPUS_DIR.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        case["_path"] = str(path)
        cases.append(case)
    return cases


CORPUS = _load_corpus()
CORPUS_IDS = [c["name"] for c in CORPUS]


def test_corpus_is_permanent_and_covers_the_failure_classes():
    """The corpus is a committed asset, not generated at test time."""
    assert len(CORPUS) >= 15, "the malformed corpus must not shrink"
    names = set(CORPUS_IDS)
    assert len(names) == len(CORPUS), "corpus case names must be unique"
    required = {
        "unregistered_kind", "oversize_plugin", "nul_byte_skill",
        "invalid_utf8_plugin", "depth_bomb_config_bundle",
        "key_bomb_config_bundle", "wrong_type_skill_instructions",
        "missing_required_plugin_code", "unknown_major_skill",
        "provenance_sha_mismatch_plugin", "provenance_missing_memory_import",
        "path_traversal_source_skill", "duplicate_keys_skill",
        "empty_payload_plugin", "non_json_policy_file",
    }
    assert required <= names, sorted(required - names)
    # A positive control for every PERSISTENT kind: a gate that refuses
    # everything is not a gate.
    for kind, policy in ingestgate.KINDS.items():
        if policy["persistent"]:
            assert f"valid_{kind}" in names, f"no positive control for {kind}"


@pytest.mark.parametrize("case", CORPUS, ids=CORPUS_IDS)
def test_corpus_case(case):
    payload = _payload_of(case)
    provenance = _provenance_of(case, payload)
    expect = case["expect"]
    try:
        artifact = ingestgate.check(case["kind"], payload,
                                    source=case.get("source", ""),
                                    provenance=provenance)
    except ingestgate.IngestRefused as refused:
        assert expect["refused"], (
            f"{case['name']}: expected acceptance, got {refused.reasons}")
        assert refused.reasons, "a refusal must carry at least one reason"
        assert refused.kind == case["kind"]
        assert refused.evidence_ref, "W2-I5.3: refusal must reference evidence"
        for wanted in expect["reasons"]:
            assert any(a.startswith(wanted) for a in refused.reasons), (
                f"{case['name']}: wanted reason {wanted!r}, "
                f"got {refused.reasons}")
    else:
        assert not expect["refused"], (
            f"{case['name']}: expected refusal {expect['reasons']}, accepted")
        # W2-I5.2/I5.4: accepted means byte-identical to the canonicalized
        # input — nothing added, dropped, defaulted or coerced.
        assert (ingestgate.canonical_bytes(artifact)
                == ingestgate.canonical_bytes(
                    ingestgate.canonicalize(case["kind"], payload)))


# --- W2-I5.1 fail-closed registry -------------------------------------------


def test_registry_covers_every_spec_kind():
    assert set(ingestgate.registered_kinds()) == set(SPEC_KINDS)
    for kind, policy in ingestgate.KINDS.items():
        for field in ("max_bytes", "schema", "persistent",
                      "provenance_required", "version_key", "known_majors"):
            assert field in policy, f"{kind} missing policy field {field}"
        assert policy["max_bytes"] > 0
        assert policy["schema"].get("required"), f"{kind} has no required keys"
        assert policy["known_majors"], f"{kind} declares no known majors"


def test_unregistered_kind_is_refused_not_passed():
    """W2-I5.1 — the should_wrap inversion applied to ingestion."""
    with pytest.raises(ingestgate.IngestRefused) as exc:
        ingestgate.check("brand_new_import_path",
                         {"version": "1.0", "name": "x"})
    assert "unregistered_kind" in exc.value.reasons


def test_unregistered_kind_refused_even_when_payload_is_perfect():
    """A well-formed artifact of an unknown kind is still refused — the gate
    keys off registration, never off how healthy the payload looks."""
    good = json.loads(
        (CORPUS_DIR / "valid_skill.json").read_text(
            encoding="utf-8"))["payload"]
    with pytest.raises(ingestgate.IngestRefused):
        ingestgate.check("skill_v2", good)


def test_is_persistent_fails_closed_for_unknown_kinds():
    assert ingestgate.is_persistent("never_registered") is True


# --- W2-I5.2 never repair a persistent artifact ------------------------------


def _valid(name: str):
    case = json.loads(
        (CORPUS_DIR / name).read_text(encoding="utf-8"))
    payload = _payload_of(case)
    return case, payload, _provenance_of(case, payload)


PERSISTENT_KINDS = tuple(k for k, p in ingestgate.KINDS.items()
                         if p["persistent"])


@pytest.mark.parametrize("kind", PERSISTENT_KINDS)
def test_accepted_artifact_is_byte_identical(kind):
    """W2-I5.4's operational form: the accepted artifact is byte-identical to
    the canonicalized input. Canonicalization (utf-8 → NFC → JSON) is the ONLY
    transformation the gate is allowed to make; equality here means no field
    was defaulted, coerced, reordered-in-content, dropped or truncated."""
    case, payload, prov = _valid(f"valid_{kind}.json")
    artifact = ingestgate.check(kind, payload, source=case.get("source", ""),
                                provenance=prov)
    canonical = ingestgate.canonicalize(kind, payload)
    assert (ingestgate.canonical_bytes(artifact)
            == ingestgate.canonical_bytes(canonical))
    assert set(artifact) == set(canonical)


def test_canonicalize_is_idempotent():
    _case, payload, _prov = _valid("valid_skill.json")
    once = ingestgate.canonicalize("skill", payload)
    twice = ingestgate.canonicalize("skill", once)
    assert (ingestgate.canonical_bytes(once)
            == ingestgate.canonical_bytes(twice))


def test_missing_field_is_refused_not_defaulted():
    """The single sharpest expression of reject-never-repair."""
    _case, payload, prov = _valid("valid_plugin.json")
    broken = dict(payload)
    del broken["code"]
    prov = {"source": "x", "sha256": ingestgate.payload_sha256(broken)}
    with pytest.raises(ingestgate.IngestRefused) as exc:
        ingestgate.check("plugin", broken, provenance=prov)
    assert any(r.startswith("missing_required:code")
               for r in exc.value.reasons)


def test_oversize_is_refused_not_truncated():
    payload = {"version": "1.0", "name": "big",
               "code": "A" * (ingestgate.KINDS["plugin"]["max_bytes"] + 10)}
    prov = {"source": "x", "sha256": ingestgate.payload_sha256(payload)}
    with pytest.raises(ingestgate.IngestRefused) as exc:
        ingestgate.check("plugin", payload, provenance=prov)
    assert any(r.startswith("oversize") for r in exc.value.reasons)


# --- structural bombs (bounds before parse) ---------------------------------


def test_depth_bomb_dies_before_the_parser():
    depth = ingestgate.MAX_JSON_DEPTH + 40
    text = '{"version":"1.0","settings":' + "[" * depth + "]" * depth + "}"
    with pytest.raises(ingestgate.IngestRefused) as exc:
        ingestgate.check("config_bundle", text)
    assert "max_depth_exceeded" in exc.value.reasons


def test_depth_bomb_as_a_python_object_is_also_bounded():
    """A pre-parsed dict bypasses the text scan, so the object walk must bound
    it too — and it must not blow the interpreter stack doing so."""
    node: dict = {"version": "1.0", "settings": {}}
    cur = node["settings"]
    for _ in range(ingestgate.MAX_JSON_DEPTH + 40):
        cur["n"] = {}
        cur = cur["n"]
    with pytest.raises(ingestgate.IngestRefused) as exc:
        ingestgate.check("config_bundle", node)
    assert "max_depth_exceeded" in exc.value.reasons


def test_self_referential_payload_terminates():
    node: dict = {"version": "1.0", "settings": {}}
    node["settings"]["self"] = node
    with pytest.raises(ingestgate.IngestRefused):
        ingestgate.check("config_bundle", node)


def test_key_bomb_is_bounded():
    payload = {"version": "1.0",
               "settings": {f"k{i}": i
                            for i in range(ingestgate.MAX_TOTAL_KEYS + 5)}}
    with pytest.raises(ingestgate.IngestRefused) as exc:
        ingestgate.check("config_bundle", payload)
    assert "too_many_keys" in exc.value.reasons


# --- version + provenance ---------------------------------------------------


@pytest.mark.parametrize("version,reason", [
    ("2.0", "unknown_major_version"),
    ("0.9", "unknown_major_version"),
    ("", "malformed_version"),
    ("latest", "malformed_version"),
    ("-1.0", "malformed_version"),
])
def test_version_policy(version, reason):
    payload = {"version": version, "name": "x", "instructions": "hi"}
    with pytest.raises(ingestgate.IngestRefused) as exc:
        ingestgate.check("skill", payload)
    assert any(r.startswith(reason) for r in exc.value.reasons)


def test_provenance_hash_must_cover_the_exact_bytes():
    _case, payload, _prov = _valid("valid_plugin.json")
    tampered = dict(payload)
    tampered["code"] = payload["code"] + "\nimport os\n"
    stale = {"source": "https://example.invalid/p.json",
             "sha256": ingestgate.payload_sha256(payload)}
    with pytest.raises(ingestgate.IngestRefused) as exc:
        ingestgate.check("plugin", tampered, provenance=stale)
    assert "provenance_sha_mismatch" in exc.value.reasons


@pytest.mark.parametrize("source", [
    "../../etc/passwd", "skills/../../../root/.ssh/id_rsa",
    "%2e%2e/%2e%2e/etc/shadow", "~/.aws/credentials",
])
def test_traversal_shaped_sources_are_refused(source):
    _case, payload, _prov = _valid("valid_skill.json")
    with pytest.raises(ingestgate.IngestRefused) as exc:
        ingestgate.check("skill", payload, source=source)
    assert "unsafe_source" in exc.value.reasons


def test_ordinary_sources_are_accepted():
    _case, payload, _prov = _valid("valid_skill.json")
    for source in ("https://example.invalid/skills/a.json",
                   "/srv/olympus/imports/a.json", "operator-upload"):
        ingestgate.check("skill", payload, source=source)


# --- W2-I5.3 refusal evidence ------------------------------------------------


def test_refusal_writes_retained_evidence():
    before = len(ingestgate.refusals())
    with pytest.raises(ingestgate.IngestRefused) as exc:
        ingestgate.check("skill", {"version": "1.0", "name": "x"},
                         source="operator-upload")
    records = ingestgate.refusals()
    assert len(records) == before + 1
    rec = records[-1]
    assert rec["kind"] == "skill"
    assert rec["source"] == "operator-upload"
    assert rec["evidence_ref"] == exc.value.evidence_ref
    assert rec["reasons"] == exc.value.reasons
    assert rec["sha256"] and rec["ts"]
    assert "signed" in rec
    # Retained: still on disk, still readable, after the exception unwound.
    assert ingestgate.refusals_path().exists()


def test_every_refusal_gets_its_own_evidence_ref():
    refs = set()
    for i in range(5):
        with pytest.raises(ingestgate.IngestRefused) as exc:
            ingestgate.check("skill", {"version": "1.0", "name": f"x{i}"})
        refs.add(exc.value.evidence_ref)
    assert len(refs) == 5


@pytest.mark.requires_crypto
def test_refusal_evidence_is_signed_and_verifies():
    with pytest.raises(ingestgate.IngestRefused):
        ingestgate.check("plugin", {"version": "1.0", "name": "x"},
                         provenance={"source": "s", "sha256": "0" * 64})
    rec = ingestgate.refusals()[-1]
    assert rec["signed"] is True
    assert ingestgate.verify_refusal(rec)
    tampered = dict(rec, reasons=["nothing_to_see_here"])
    assert not ingestgate.verify_refusal(tampered)


def test_evidence_path_never_raises(monkeypatch):
    """A gate an attacker can crash through its own audit path is a gate that
    can be switched off. Recording failure must not suppress the refusal."""
    def boom(*_a, **_kw):
        raise OSError("disk full")
    monkeypatch.setattr(ingestgate, "refusals_path", boom)
    with pytest.raises(ingestgate.IngestRefused) as exc:
        ingestgate.check("skill", {"version": "1.0"})
    assert exc.value.reasons


def test_unsigned_evidence_is_still_retained(monkeypatch):
    class _Broken:
        SEED_DERIVATION = "x"

        @staticmethod
        def public_key_hex():
            raise RuntimeError("no crypto backend")

        @staticmethod
        def sign(_data):
            raise RuntimeError("no crypto backend")

    import olympus
    monkeypatch.setattr(olympus, "witness", _Broken)
    with pytest.raises(ingestgate.IngestRefused):
        ingestgate.check("skill", {"version": "1.0"})
    rec = ingestgate.refusals()[-1]
    assert rec["signed"] is False
    assert "unsigned" in rec["note"]
    assert ingestgate.verify_refusal(rec) is False


# --- W2-I5.4 seeded property-style mutation sweeps ---------------------------

SWEEP_ITERATIONS = 520          # >= 500 per spec
SWEEP_SEED = 20260726


def _mutate(rng: random.Random, obj: dict):
    """Return (label, mutated_payload). Four families: bit flips and
    truncations against the serialized bytes, key deletion and type swaps
    against the parsed object."""
    strategy = rng.choice(("bitflip", "truncate", "delete_key", "type_swap"))
    if strategy in ("bitflip", "truncate"):
        raw = bytearray(ingestgate.canonical_bytes(obj))
        if strategy == "bitflip":
            for _ in range(rng.randint(1, 3)):
                idx = rng.randrange(len(raw))
                raw[idx] ^= 1 << rng.randrange(8)
        else:
            raw = raw[:rng.randrange(0, len(raw))]
        return strategy, bytes(raw)

    mutated = copy.deepcopy(obj)
    keys = sorted(mutated)
    if strategy == "delete_key":
        del mutated[rng.choice(keys)]
        return strategy, mutated
    swap = rng.choice([None, 42, True, [], {}, "", 3.5, ["x"], {"a": 1}])
    mutated[rng.choice(keys)] = swap
    return strategy, mutated


@pytest.mark.parametrize("kind", ["skill", "eval_corpus", "tool_schema",
                                  "model_endpoint"])
def test_mutation_sweep_never_silently_repairs(kind):
    """W2-I5.4. Every mutation of a valid artifact must end in exactly one of:

      * REFUSED, with at least one honest reason and retained evidence; or
      * ACCEPTED, and then byte-identical to the canonicalized input —
        `canonical_bytes(check(k, p)) == canonical_bytes(canonicalize(k, p))`.

    That equality is the proof of "never silently repaired": if the gate had
    defaulted a missing key, coerced a type, dropped an unparsable member or
    truncated an oversize field, the accepted artifact would differ from the
    canonicalization of what was actually handed in, and this assertion fires.
    Acceptance additionally has to survive the authoritative schema, so an
    accepted mutant is genuinely still valid rather than merely tolerated.
    """
    _case, base, _prov = _valid(f"valid_{kind}.json")
    schema = ingestgate.KINDS[kind]["schema"]
    rng = random.Random(SWEEP_SEED)
    accepted = refused = 0

    for _ in range(SWEEP_ITERATIONS):
        _label, mutated = _mutate(rng, base)
        try:
            artifact = ingestgate.check(kind, mutated)
        except ingestgate.IngestRefused as exc:
            refused += 1
            assert exc.reasons, "a refusal must name a reason"
            assert exc.evidence_ref, "W2-I5.3: refusal must retain evidence"
            continue
        accepted += 1
        canonical = ingestgate.canonicalize(kind, mutated)
        assert (ingestgate.canonical_bytes(artifact)
                == ingestgate.canonical_bytes(canonical)), (
            f"{kind}: gate REPAIRED a mutated artifact")
        assert set(artifact) == set(canonical), "keys added or dropped"
        # Accepted really means valid, not tolerated.
        ingestgate.assert_schema(artifact, schema, kind=kind)

    assert refused >= 1, "sweep produced no refusals — it proves nothing"
    assert accepted >= 1, ("sweep refused everything — the acceptance branch "
                           "of the invariant was never exercised")
    assert accepted + refused == SWEEP_ITERATIONS


def test_mutation_sweep_is_deterministic():
    """Seeded stdlib random, so a sweep regression is reproducible from the
    seed alone — no hypothesis dependency, no example database."""
    def run():
        _case, base, _prov = _valid("valid_skill.json")
        rng = random.Random(SWEEP_SEED)
        return [(label, ingestgate.canonical_bytes(payload)
                 if not isinstance(payload, bytes) else payload)
                for label, payload in
                (_mutate(rng, base) for _ in range(25))]

    first, second = run(), run()
    assert first == second
    assert len({label for label, _ in first}) == 4, (
        "all four mutation families must fire")


def test_sweep_refusals_are_all_retained():
    _case, base, _prov = _valid("valid_skill.json")
    rng = random.Random(SWEEP_SEED + 1)
    refs = []
    for _ in range(60):
        _label, mutated = _mutate(rng, base)
        try:
            ingestgate.check("skill", mutated)
        except ingestgate.IngestRefused as exc:
            refs.append(exc.evidence_ref)
    assert refs, "expected some refusals"
    retained = {r["evidence_ref"] for r in ingestgate.refusals(10_000)}
    assert set(refs) <= retained


# --- the ephemeral half ------------------------------------------------------


EPHEMERAL_KINDS = tuple(k for k, p in ingestgate.KINDS.items()
                        if not p["persistent"])


def test_there_is_at_least_one_of_each_half():
    assert EPHEMERAL_KINDS and PERSISTENT_KINDS


def test_sanitize_ephemeral_applies_only_the_declared_rules():
    payload = {"version": "1.0",
               "result": {"text": "ok\x07\x00  spaced   out " + "z" * 50},
               "server": "files"}
    out = ingestgate.sanitize_ephemeral(
        "mcp_payload", payload,
        rules={"strip_control": True, "max_len": 20,
               "collapse_whitespace": True, "normalize": "NFC"})
    text = out["result"]["text"]
    assert "\x07" not in text and "\x00" not in text
    assert len(text) <= 20
    assert "  " not in text
    # Untouched keys survive verbatim — sanitation is not a rewrite.
    assert out["server"] == "files"
    assert out["version"] == "1.0"


def test_sanitize_ephemeral_max_items_bounds_lists():
    payload = {"version": "1.0", "models": [f"m{i}" for i in range(100)]}
    out = ingestgate.sanitize_ephemeral("provider_metadata", payload,
                                        rules={"max_items": 5})
    assert out["models"] == [f"m{i}" for i in range(5)]


@pytest.mark.parametrize("kind", PERSISTENT_KINDS)
def test_sanitize_ephemeral_refuses_persistent_kinds(kind):
    """The doctrine boundary, enforced in code: sanitize-and-continue must
    NEVER be reachable for a durable artifact."""
    _case, payload, _prov = _valid(f"valid_{kind}.json")
    with pytest.raises(ingestgate.IngestRefused) as exc:
        ingestgate.sanitize_ephemeral(kind, payload,
                                      rules={"strip_control": True})
    assert "ephemeral_sanitation_on_persistent_kind" in exc.value.reasons
    assert exc.value.evidence_ref
    assert ingestgate.refusals()[-1]["evidence_ref"] == exc.value.evidence_ref


def test_sanitize_ephemeral_refuses_persistent_kinds_even_with_gate_off(
        monkeypatch):
    """Misapplying the ephemeral path is a doctrine violation, not a policy
    choice — the enforcement flag does not make it legal."""
    monkeypatch.setenv("OLYMPUS_INGESTGATE", "0")
    _case, payload, _prov = _valid("valid_plugin.json")
    with pytest.raises(ingestgate.IngestRefused):
        ingestgate.sanitize_ephemeral("plugin", payload, rules={})


def test_sanitize_ephemeral_refuses_unregistered_kind():
    with pytest.raises(ingestgate.IngestRefused) as exc:
        ingestgate.sanitize_ephemeral("mystery", {"a": 1}, rules={})
    assert "unregistered_kind" in exc.value.reasons


@pytest.mark.parametrize("rules", [
    {"repair_json": True},
    {"fill_missing_keys": True},
    {"coerce_types": True},
    {"strip_control": True, "guess_encoding": True},
])
def test_sanitize_rule_vocabulary_is_closed(rules):
    """"Sanitize" must never become an open-ended repair vocabulary."""
    with pytest.raises(ValueError):
        ingestgate.sanitize_ephemeral("mcp_payload", {"version": "1.0"},
                                      rules=rules)


@pytest.mark.parametrize("rules", [
    {"normalize": "NFKD"}, {"max_len": -1}, {"max_len": True},
    {"max_items": "5"},
])
def test_sanitize_rule_values_are_contract_checked(rules):
    with pytest.raises(ValueError):
        ingestgate.sanitize_ephemeral("mcp_payload", {"version": "1.0"},
                                      rules=rules)


def test_sanitized_output_still_has_to_pass_the_authoritative_schema():
    """Sanitation makes a value safe to LOOK AT, never safe to ACT ON.

    This provider response is hostile-but-cleanable: the control characters go,
    the over-long string is bounded, the unicode is normalized — and the result
    is still NOT a valid mcp_payload, because sanitation does not and must not
    invent the missing `result` field. Only assert_schema catches that, which
    is why every caller has to run it before execution.
    """
    payload = {"version": "1.0", "output": "ok\x07\x00", "tool": "read"}
    sanitized = ingestgate.sanitize_ephemeral(
        "mcp_payload", payload,
        rules={"strip_control": True, "max_len": 40})
    assert "\x07" not in sanitized["output"]      # sanitation did its job
    schema = ingestgate.KINDS["mcp_payload"]["schema"]
    with pytest.raises(ingestgate.IngestRefused) as exc:
        ingestgate.assert_schema(sanitized, schema, kind="mcp_payload")
    assert "missing_required:result" in exc.value.reasons


def test_sanitized_output_that_is_valid_passes_the_schema():
    payload = {"version": "1.0", "result": {"text": "ok  "}, "tool": "read"}
    sanitized = ingestgate.sanitize_ephemeral(
        "mcp_payload", payload,
        rules={"strip_control": True, "collapse_whitespace": True})
    schema = ingestgate.KINDS["mcp_payload"]["schema"]
    assert ingestgate.assert_schema(sanitized, schema,
                                    kind="mcp_payload") is sanitized


def test_assert_schema_is_side_effect_free():
    before = len(ingestgate.refusals())
    with pytest.raises(ingestgate.IngestRefused):
        ingestgate.assert_schema({"nope": 1},
                                 ingestgate.KINDS["skill"]["schema"],
                                 kind="skill")
    assert len(ingestgate.refusals()) == before


# --- flag-off pass-through ---------------------------------------------------


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("OLYMPUS_INGESTGATE", raising=False)
    assert ingestgate.enabled() is False


@pytest.mark.parametrize("value", ["", "0", "off", "no", "false", "maybe"])
def test_flag_parsing(monkeypatch, value):
    monkeypatch.setenv("OLYMPUS_INGESTGATE", value)
    assert ingestgate.enabled() is False


def test_flag_off_is_a_pass_through_that_records_nothing(monkeypatch):
    """Wiring call sites must be safe before enforcement is switched on."""
    monkeypatch.setenv("OLYMPUS_INGESTGATE", "0")
    hostile = {"version": "99.0", "name": "\x00", "instructions": 5}
    assert ingestgate.check("skill", hostile) is hostile
    # Even an unregistered kind passes through untouched while off.
    assert ingestgate.check("never_registered", hostile) is hostile
    assert ingestgate.check("plugin", b"not json at all") == b"not json at all"
    assert ingestgate.refusals() == []
    assert not ingestgate.refusals_path().parent.joinpath(
        "refusals.jsonl").exists()


def test_flag_on_refuses_what_flag_off_admitted(monkeypatch):
    hostile = {"version": "99.0", "name": "x", "instructions": 5}
    monkeypatch.setenv("OLYMPUS_INGESTGATE", "0")
    assert ingestgate.check("skill", hostile) is hostile
    monkeypatch.setenv("OLYMPUS_INGESTGATE", "1")
    with pytest.raises(ingestgate.IngestRefused):
        ingestgate.check("skill", hostile)


# --- classification ----------------------------------------------------------


def test_classification_uses_the_egress_vocabulary():
    _case, payload, prov = _valid("valid_plugin.json")
    verdict = ingestgate.check_detailed("plugin", payload, provenance=prov)
    assert verdict.data_class in ingestgate.DATA_CLASSES
    assert verdict.sha256 and verdict.canonical
    assert verdict.persistent is True
    assert verdict.artifact == ingestgate.canonicalize("plugin", payload)


def test_secret_shaped_content_classifies_sensitive():
    payload = {"version": "1.0", "name": "leaky",
               "instructions": "use sk-abcdefghijklmnop to authenticate"}
    verdict = ingestgate.check_detailed("skill", payload)
    assert verdict.data_class == ingestgate.C2


def test_check_detailed_reports_no_verdict_when_gate_is_off(monkeypatch):
    monkeypatch.setenv("OLYMPUS_INGESTGATE", "0")
    verdict = ingestgate.check_detailed("skill", {"a": 1})
    assert verdict.data_class == ""
    assert verdict.canonical == b""


# --- canonicalization edges -------------------------------------------------


def test_nfc_normalization_is_applied_consistently():
    """A decomposed and a composed spelling of the same skill canonicalize to
    the same bytes — normalization, not repair, and applied identically by
    canonicalize() so the byte-identity invariant still holds."""
    composed = {"version": "1.0", "name": "caf\u00e9", "instructions": "hi"}
    decomposed = {"version": "1.0", "name": "cafe\u0301",
                  "instructions": "hi"}
    assert composed != decomposed, "the spellings must differ on the wire"
    a = ingestgate.check("skill", composed)
    b = ingestgate.check("skill", decomposed)
    assert ingestgate.canonical_bytes(a) == ingestgate.canonical_bytes(b)
    assert (ingestgate.canonical_bytes(b)
            == ingestgate.canonical_bytes(
                ingestgate.canonicalize("skill", decomposed)))


def test_bytes_text_and_dict_forms_agree():
    _case, payload, _prov = _valid("valid_skill.json")
    text = json.dumps(payload)
    forms = [ingestgate.check("skill", payload),
             ingestgate.check("skill", text),
             ingestgate.check("skill", text.encode("utf-8"))]
    canonical = {ingestgate.canonical_bytes(f) for f in forms}
    assert len(canonical) == 1


def test_payload_sha256_matches_across_forms():
    text = '{"version":"1.0","name":"x","instructions":"hi"}'
    assert (ingestgate.payload_sha256(text)
            == ingestgate.payload_sha256(text.encode("utf-8")))


def test_canonicalize_refuses_without_writing_evidence():
    before = len(ingestgate.refusals())
    with pytest.raises(ingestgate.IngestRefused):
        ingestgate.canonicalize("skill", b"\xff\xfe")
    assert len(ingestgate.refusals()) == before
