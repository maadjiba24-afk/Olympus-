"""Owned image decoder/provider fixtures. No provider or network access."""
import base64
import io
import json

import pytest

from olympus import image_validation as validation, media
from olympus.gallery_state import GalleryError


def image_bytes(fmt="PNG", size=(3, 2), color="red"):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format=fmt)
    return out.getvalue()


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"),
    ("GIF", "image/gif"), ("WEBP", "image/webp"), ("BMP", "image/bmp")])
def test_full_decoder_valid_and_truncated(fmt, mime):
    data = image_bytes(fmt)
    result = validation.validate_image(data)
    assert result["mime"] == mime
    assert (result["width"], result["height"]) == (3, 2)
    with pytest.raises(GalleryError):
        validation.validate_image(data[:len(data) // 2])


@pytest.mark.parametrize("blob", [b"\x89PNG fake", b"GIF89a<script>", b"<svg></svg>", b"", b"BMbad"])
def test_signature_or_active_content_never_accepted(blob):
    with pytest.raises(GalleryError):
        validation.validate_image(blob)


def test_dimensions_and_aggregate(monkeypatch):
    monkeypatch.setattr(validation, "MAX_DIMENSION", 2)
    with pytest.raises(GalleryError) as err:
        validation.validate_image(image_bytes())
    assert err.value.status == 413
    monkeypatch.setattr(validation, "MAX_DIMENSION", 8192)
    monkeypatch.setattr(validation, "MAX_TOTAL_PIXELS", 5)
    with pytest.raises(GalleryError) as err:
        validation.validate_image(image_bytes())
    assert err.value.status == 413


def test_frame_count_bound(monkeypatch):
    from PIL import Image
    out = io.BytesIO()
    frames = [Image.new("RGB", (2, 2), c) for c in ("red", "blue", "green")]
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:])
    monkeypatch.setattr(validation, "MAX_FRAMES", 2)
    with pytest.raises(GalleryError) as err:
        validation.validate_image(out.getvalue())
    assert err.value.status == 413


def test_truncated_global_setting_fails_closed(monkeypatch):
    from PIL import ImageFile
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    with pytest.raises(GalleryError) as err:
        validation.validate_image(image_bytes())
    assert err.value.code == "unavailable"


@pytest.mark.parametrize("value", [None, [], {}, {"data": []}, {"data": [{"b64_json": 3}]},
    {"data": [{"b64_json": "!!!!"}]}, {"data": [{"url": "https://example.invalid"}]},
    {"data": [{"b64_json": "AA=="}, {"b64_json": "AA=="}]}])
def test_provider_shape_and_base64(value):
    with pytest.raises(GalleryError):
        media._image_response(json.dumps(value).encode())


def test_duplicate_provider_keys_refused():
    with pytest.raises(GalleryError):
        media._image_response(b'{"data":[],"data":[]}')


def test_provider_response_limits(monkeypatch):
    monkeypatch.setattr(media, "_MAX_IMAGE_JSON", 8)
    class Response:
        def read(self, count):
            assert count == 9
            return b"x" * count
    with pytest.raises(GalleryError):
        media._image_read(Response())


@pytest.fixture
def owned_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "owned-fixture-key")
    monkeypatch.setenv("OLYMPUS_EXEC_WORKDIR", str(tmp_path))
    calls = []
    def provider(endpoint, key, payload, timeout=120):
        calls.append(payload)
        return json.dumps({"data": [{"b64_json": base64.b64encode(image_bytes()).decode()}]}).encode()
    monkeypatch.setattr(media, "_post_image", provider)
    return calls


@pytest.mark.parametrize("kwargs", [dict(prompt=""), dict(prompt=[]), dict(prompt="x" * 32769),
    dict(prompt="x", filename="../bad.png"), dict(prompt="x", filename="same.svg"),
    dict(prompt="x", owner=""), dict(prompt="x", owner=None), dict(prompt="x", operation_id=None)])
def test_admission_before_provider(owned_provider, kwargs):
    args = dict(owner="owner:one", operation_id="1" * 32)
    args.update(kwargs)
    with pytest.raises(GalleryError):
        media.generate_image_result(**args)
    assert owned_provider == []


def test_exact_owner_saved_retry_and_conflict(owned_provider):
    from olympus.gallery_state import Store
    result = media.generate_image_result("cat", "cat.png", owner="a:b", operation_id="2" * 32)
    assert result["status"] == "complete"
    assert media.generate_image_result("cat", "cat.png", owner="a:b", operation_id="2" * 32)["status"] == "complete"
    assert len(owned_provider) == 1
    with pytest.raises(GalleryError):
        media.generate_image_result("changed", "cat.png", owner="a:b", operation_id="2" * 32)
    with pytest.raises(GalleryError):
        media.generate_image_result("cat", "cat.png", owner="a:b", operation_id="3" * 32)
    assert len(owned_provider) == 1
    assert Store("a:b").status("2" * 32)["image"]["name"] == "cat.png"


def test_timeout_is_not_replayed(owned_provider, monkeypatch):
    def timeout(*args, **kwargs):
        owned_provider.append("attempt")
        raise TimeoutError("owned timeout")
    monkeypatch.setattr(media, "_post_image", timeout)
    first = media.generate_image_result("cat", owner="owner", operation_id="4" * 32)
    assert first["status"] in ("started", "indeterminate")
    second = media.generate_image_result("cat", owner="owner", operation_id="4" * 32)
    assert second["status"] in ("started", "indeterminate")
    assert len(owned_provider) == 1


def test_missing_decoder_before_provider(owned_provider, monkeypatch):
    def missing():
        raise GalleryError("unavailable", "missing fixture decoder", 503)
    monkeypatch.setattr(validation, "require_decoder", missing)
    with pytest.raises(GalleryError):
        media.generate_image_result("cat", owner="owner", operation_id="5" * 32)
    assert owned_provider == []


def test_completed_retry_needs_no_credentials_or_decoder(owned_provider, monkeypatch):
    first = media.generate_image_result("cat", owner="owner", operation_id="6" * 32)
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("OLYMPUS_MEDIA_API_KEY", raising=False)
    def no_decoder():
        raise AssertionError("completed retry must not decode or call provider")
    monkeypatch.setattr(validation, "require_decoder", no_decoder)
    retry = media.generate_image_result("cat", owner="owner", operation_id="6" * 32)
    assert retry["status"] == first["status"] == "complete"
    assert len(owned_provider) == 1


def test_edit_owned_source_multipart_and_original_preserved(owned_provider, monkeypatch):
    from olympus.gallery_state import Store
    original = media.generate_image_result("cat", "cat.png", owner="owner:one", operation_id="7" * 32)["image"]
    source = {k: original[k] for k in ("name", "id", "revision")}
    before, _ = Store("owner:one").read_source(source)
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, bound):
            assert bound == media._MAX_IMAGE_JSON + 1
            return json.dumps({"data": [{"b64_json": base64.b64encode(image_bytes(color="blue")).decode()}]}).encode()
    def urlopen(req, timeout):
        calls.append(req)
        return Response()
    monkeypatch.setattr(media.urllib.request, "urlopen", urlopen)
    result = media.edit_image_result("blue", "cat.png", "blue.png", owner="owner:one",
        operation_id="8" * 32, source_id=source["id"], source_revision=source["revision"])
    assert result["status"] == "complete"
    assert len(calls) == 1 and calls[0].full_url.endswith("/images/edits")
    assert calls[0].headers["Content-type"].startswith("multipart/form-data")
    assert Store("owner:one").read_source(source)[0] == before
    with pytest.raises(GalleryError):
        media.edit_image_result("blue", "cat.png", "other.png", owner="owner_one",
            operation_id="9" * 32, source_id=source["id"], source_revision=source["revision"])
    assert len(calls) == 1


def test_tools_capture_exact_owner_and_required_operation(monkeypatch):
    from olympus import tools, memory
    captured = {}
    def generate(prompt, filename, **kwargs):
        captured.update(kwargs)
        return {"status": "complete"}
    monkeypatch.setattr(media, "generate_image_result", generate)
    with memory.user_context("punctuation:Exact OWNER"):
        tools.HANDLERS["generate_image"]("x", "a" * 32)
    assert captured == {"owner": "punctuation:Exact OWNER", "operation_id": "a" * 32}
    assert "operation_id" in tools.GENERATE_IMAGE["input_schema"]["required"]
    assert "operation_id" in tools.EDIT_IMAGE["input_schema"]["required"]
    assert {"source_id", "source_revision"}.issubset(tools.EDIT_IMAGE["input_schema"]["properties"])


def test_decompression_warning_is_failure(monkeypatch):
    from PIL import Image
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 4)
    with pytest.raises(GalleryError) as err:
        validation.validate_image(image_bytes())
    assert err.value.status == 413


def test_provider_encoded_and_decoded_caps(monkeypatch):
    monkeypatch.setattr(validation, "MAX_IMAGE_BYTES", 2)
    for value in ("AAAA", "AAAAAA=="):
        with pytest.raises(GalleryError):
            media._image_response(json.dumps({"data": [{"b64_json": value}]}).encode())


@pytest.mark.parametrize("value", ["http://user:password@example.invalid/v1", "https://example.invalid/v1?key=secret",
    "ftp://example.invalid/v1", "https://example.invalid/\r\nheader"])
def test_invalid_config_before_provider(owned_provider, monkeypatch, value):
    monkeypatch.setenv("OLYMPUS_MEDIA_BASE_URL", value)
    with pytest.raises(GalleryError):
        media.generate_image_result("cat", owner="owner", operation_id="b" * 32)
    assert not owned_provider


def test_provider_changes_ambient_owner_without_stealing_output(owned_provider, monkeypatch):
    from olympus import memory
    original = media._post_image
    def changing(*args, **kwargs):
        memory.set_user("other-owner")
        return original(*args, **kwargs)
    monkeypatch.setattr(media, "_post_image", changing)
    with memory.user_context("captured:owner"):
        result = media.generate_image_result("cat", operation_id="c" * 32)
    from olympus.gallery_state import Store
    assert Store("captured:owner").status("c" * 32)["status"] == "complete"
    with pytest.raises(GalleryError):
        Store("other-owner").status("c" * 32)
    assert result["status"] == "complete"


def test_lost_finalize_ack_recovers_without_replay(owned_provider, monkeypatch):
    from olympus.gallery_state import Store
    original = Store.finalize
    def lost(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise GalleryError("unavailable", "owned lost acknowledgement", 503)
    monkeypatch.setattr(Store, "finalize", lost)
    result = media.generate_image_result("cat", owner="owner", operation_id="d" * 32)
    assert result["status"] == "complete"
    assert len(owned_provider) == 1


def test_start_ack_failure_does_not_call_provider(owned_provider, monkeypatch):
    from olympus.gallery_state import Store
    original = Store.start
    def lost(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise GalleryError("unavailable", "owned lost start acknowledgement", 503)
    monkeypatch.setattr(Store, "start", lost)
    result = media.generate_image_result("cat", owner="owner", operation_id="e" * 32)
    assert result["status"] == "indeterminate"
    assert not owned_provider


def test_wrong_actual_output_type_fails_and_is_not_replayed(owned_provider):
    result = media.generate_image_result("cat", "cat.jpg", owner="owner", operation_id="f" * 32)
    assert result["status"] == "failed"
    retry = media.generate_image_result("cat", "cat.jpg", owner="owner", operation_id="f" * 32)
    assert retry["status"] == "failed"
    assert len(owned_provider) == 1


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP", "BMP"])
def test_missing_final_byte_refused(fmt):
    with pytest.raises(GalleryError):
        validation.validate_image(image_bytes(fmt)[:-1])


def test_generated_image_can_be_analyzed_by_owned_identity(owned_provider, monkeypatch):
    result = media.generate_image_result("cat", "cat.png", owner="owner:one", operation_id="0" * 32)
    record = result["image"]
    seen = []
    monkeypatch.setattr(media, "_vision_describe", lambda src, question: seen.append(src) or "a cat")
    assert media.analyze_image("cat.png", gallery_id=record["id"],
        gallery_revision=record["revision"], owner="owner:one") == "a cat"
    assert seen[0]["url"].startswith("data:image/png;base64,")
    assert media.analyze_image("cat.png", gallery_id=record["id"],
        gallery_revision=record["revision"], owner="owner_one").startswith("Error")
    assert len(seen) == 1
    assert media.analyze_image("cat.png", gallery_id=record["id"],
        gallery_revision="0" * 64, owner="owner:one").startswith("Error")
    assert len(seen) == 1


def test_tool_modes_require_no_provider_and_respect_owner(owned_provider):
    from olympus import memory, tools
    with memory.user_context("owner:one"):
        result = tools.HANDLERS["generate_image"](prompt="cat", operation_id="1a" * 16)
        assert result["status"] == "complete"
        for mode in ("status", "recover"):
            assert tools.HANDLERS["generate_image"](operation_id="1a" * 16, mode=mode)["status"] == "complete"
            assert tools.HANDLERS["edit_image"](operation_id="1a" * 16, mode=mode)["status"] == "complete"
        assert tools.HANDLERS["generate_image"](operation_id="1a" * 16, mode="bogus")["status"] == "error"
    with memory.user_context("owner_one"):
        for mode in ("status", "recover"):
            assert tools.HANDLERS["generate_image"](operation_id="1a" * 16, mode=mode)["code"] == "missing"
    assert len(owned_provider) == 1


def test_tool_recover_adopts_durable_output(owned_provider, monkeypatch):
    from olympus import memory, tools
    from olympus.gallery_state import Store
    original = Store._adopt
    def unavailable(*args):
        raise GalleryError("unavailable", "owned manifest publication failure", 503)
    with memory.user_context("owner"):
        monkeypatch.setattr(Store, "_adopt", unavailable)
        first = tools.HANDLERS["generate_image"](prompt="cat", operation_id="2b" * 16)
        assert first["status"] == "error"
        assert tools.HANDLERS["generate_image"](operation_id="2b" * 16, mode="status")["status"] == "indeterminate"
        monkeypatch.setattr(Store, "_adopt", original)
        recovered = tools.HANDLERS["generate_image"](operation_id="2b" * 16, mode="recover")
        assert recovered["status"] == "complete"
    assert len(owned_provider) == 1
