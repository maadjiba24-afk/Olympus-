"""Anthropic structured-output schema normalization (llm._strict_object_schema).
Regression for the live-only 400s: every object must carry
additionalProperties:false, and the bounds keywords Anthropic rejects
(maxItems, minimum, …) must be stripped — while ACCEPTED keywords
(minItems/maxLength/pattern/format) are preserved.
"""

from olympus import llm


def _walk_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield node
        for v in node.values():
            yield from _walk_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_objects(v)


def test_additional_properties_set_on_all_nested_objects():
    s = {"type": "object", "properties": {
        "a": {"type": "object", "properties": {"b": {"type": "string"}}},
        "arr": {"type": "array", "items": {
            "type": "object", "properties": {"c": {"type": "string"}}}}}}
    out = llm._strict_object_schema(s)
    objs = list(_walk_objects(out))
    assert objs and all(o.get("additionalProperties") is False for o in objs)


def test_rejected_bounds_keywords_are_stripped():
    s = {"type": "object", "properties": {
        "arr": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "n": {"type": "integer", "minimum": 0, "maximum": 9}}}
    out = llm._strict_object_schema(s)
    arr = out["properties"]["arr"]
    n = out["properties"]["n"]
    assert "maxItems" not in arr
    assert "minimum" not in n and "maximum" not in n


def test_accepted_keywords_are_preserved():
    s = {"type": "object", "properties": {
        "arr": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "s": {"type": "string", "maxLength": 10, "pattern": "^a", "format": "date"}}}
    out = llm._strict_object_schema(s)
    assert out["properties"]["arr"]["minItems"] == 1
    sp = out["properties"]["s"]
    assert sp["maxLength"] == 10 and sp["pattern"] == "^a" and sp["format"] == "date"


def test_original_schema_not_mutated():
    s = {"type": "object", "properties": {"x": {"type": "string"}}}
    llm._strict_object_schema(s)
    assert "additionalProperties" not in s


def test_explicit_additional_properties_true_is_respected():
    s = {"type": "object", "additionalProperties": True, "properties": {}}
    assert llm._strict_object_schema(s)["additionalProperties"] is True
