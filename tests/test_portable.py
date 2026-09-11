import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import httpx
from PIL import Image
from sqlcipher3 import dbapi2 as sqlite

from marketpulse.bootstrap import verify_page, validated_candidates, verified_image_key
from marketpulse.briefing import generate, text_payload, refresh_attributions
from marketpulse.demo import records
from marketpulse.models import call_model, validate_endpoint, model_identity
from marketpulse.reduction import reduce_payload


def test_rules_multi_group_is_offline_and_keeps_group_nicknames(tmp_path):
    source = tmp_path / "messages.jsonl"
    source.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records()), encoding="utf-8")
    cfg = dict(provider="rules", input=str(source))
    with (
        patch("marketpulse.briefing.call_model", side_effect=AssertionError("network/model forbidden")),
        patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
    ):
        result, _ = generate("2026-01-05", [], cfg, tmp_path / "out", work_dir=tmp_path / "work")
    alice = [m for m in result["members"] if m["sender_id"] == "alice"]
    assert len(alice) == 2
    assert len({m["alias"] for m in alice}) == 2
    assert {m["name"] for m in alice} == {"小林", "林同学（观察中）"}
    assert len(result["groups"]) == 2
    assert result["content"]["group_comparison"]
    assert result["analysis_method"] == "rules"


def test_imported_image_cannot_escape_transcript_directory(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    row = records()[0] | dict(message_kind="image", text="", image_path="../private.png")
    path = folder / "input.jsonl"
    path.write_text(json.dumps(row), encoding="utf-8")
    import pytest

    with pytest.raises(ValueError, match="目录"):
        generate(
            "2026-01-05", [], dict(provider="rules", input=str(path)), tmp_path / "out", work_dir=tmp_path / "work"
        )


def test_backend_changes_invalidate_report_cache(tmp_path):
    source = tmp_path / "messages.jsonl"
    source.write_text("".join(json.dumps(r) + "\n" for r in records()), encoding="utf-8")
    cfg = dict(provider="rules", input=str(source), model="first")
    first, _ = generate("2026-01-05", [], cfg, tmp_path / "out", work_dir=tmp_path / "work")
    second, _ = generate("2026-01-05", [], cfg | {"model": "second"}, tmp_path / "out", work_dir=tmp_path / "work")
    assert first["input_fingerprint"] != second["input_fingerprint"]


def test_local_only_rejects_remote_and_embedded_credentials():
    import pytest

    for url in (
        "https://example.test/v1",
        "http://10.1.2.3:11434",
        "https://user:pass@localhost/v1",
        "http://localhost/v1?key=secret",
    ):
        with pytest.raises(ValueError):
            validate_endpoint(url, local_only=True)
    assert validate_endpoint("http://127.0.0.1:11434", True) == "http://127.0.0.1:11434"


def mock_http(monkeypatch, handler):
    original = httpx.Client

    class MockClient(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", MockClient)


SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def test_ollama_schema_and_image_transport(tmp_path, monkeypatch):
    image = tmp_path / "image.png"
    Image.new("RGB", (8, 8), "blue").save(image)

    def handler(request):
        assert request.url == httpx.URL("http://127.0.0.1:11434/api/chat")
        body = json.loads(request.content)
        assert body["stream"] is False and body["format"] == SCHEMA
        assert body["messages"][1]["images"]
        assert "tools" not in body
        return httpx.Response(200, json={"done": True, "message": {"content": '{"answer":"可见蓝色"}'}})

    mock_http(monkeypatch, handler)
    result = call_model(
        "read", SCHEMA, dict(provider="ollama", model="local-vision", supports_images=True), images=[image]
    )
    assert result["answer"] == "可见蓝色"


def test_openai_responses_and_compatible_transports(monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "synthetic-test-value")
    for provider in ("openai", "openai-compatible"):

        def handler(request):
            data = json.loads(request.content)
            if provider == "openai":
                assert data["store"] is False
                assert data["text"]["format"]["schema"] == SCHEMA
                return httpx.Response(
                    200,
                    json={
                        "id": "r",
                        "object": "response",
                        "created_at": 0,
                        "model": "example",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "id": "m",
                                "role": "assistant",
                                "status": "completed",
                                "content": [{"type": "output_text", "text": '{"answer":"ok"}', "annotations": []}],
                            }
                        ],
                    },
                )
            assert data["response_format"]["type"] == "json_object"
            return httpx.Response(
                200,
                json={
                    "id": "r",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "example",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": '{"answer":"ok"}'},
                        }
                    ],
                },
            )

        with monkeypatch.context() as patcher:
            mock_http(patcher, handler)
            result = call_model(
                "test",
                SCHEMA,
                dict(
                    provider=provider, model="example", api_key_env="TEST_MODEL_KEY", base_url="https://example.test/v1"
                ),
            )
            assert result["answer"] == "ok"


def test_model_failure_does_not_expose_credentials_or_prompt(monkeypatch):
    import pytest

    monkeypatch.setenv("TEST_MODEL_KEY", "synthetic-private-key")

    def handler(request):
        return httpx.Response(
            401,
            json={"error": {"message": "private chat body and synthetic-private-key", "type": "authentication_error"}},
        )

    mock_http(monkeypatch, handler)
    with pytest.raises(RuntimeError) as failure:
        call_model("private chat body", SCHEMA, dict(provider="openai", model="x", api_key_env="TEST_MODEL_KEY"))
    assert "private chat" not in str(failure.value) and "synthetic-private-key" not in str(failure.value)


def test_large_chat_reduction_preserves_evidence_ids():
    chart = dict(image_id="I01", kind="chart", names=["虚构标的"], codes=["000001"], caveats=["历史光标"])
    payload = dict(messages=[dict(id=f"T{i:03d}", text="测试" * 1000) for i in range(10)], images=[chart])

    def model(prompt, schema, cfg, **kwargs):
        chunk = json.loads(prompt.split("\n")[-1])
        return {"notes": [{"text": "合并笔记", "sources": [chunk[0]["id"]]}]}

    with patch("marketpulse.reduction.call_model", side_effect=model) as model:
        result = reduce_payload(payload, dict(max_input_chars=6000))
    assert result["reduced"] and model.call_count > 1
    assert len({n["sources"][0] for n in result["evidence_notes"]}) > 1
    assert result["images"] == [chart]
    assert len(json.dumps(result, ensure_ascii=False)) <= 6000


def test_windows_candidate_is_verified_against_real_sqlcipher_page(tmp_path):
    key = os.urandom(32)
    salt = os.urandom(16)
    raw = (key + salt).hex()
    path = tmp_path / "fixture.db"
    db = sqlite.connect(str(path))
    try:
        db.execute("PRAGMA key=\"x'" + raw + "'\"")
        db.execute("CREATE TABLE example(value TEXT)")
        db.commit()
    finally:
        db.close()
    page = path.read_bytes()[:4096]
    assert verify_page(key, page)
    assert not verify_page(b"\0" * 32, page)
    found = validated_candidates(b"prefix " + ("x'" + raw + "'").encode() + b" suffix", {salt.hex(): page})
    assert found == {salt.hex(): key.hex()}
    assert not validated_candidates(("x'" + (b"\0" * 32 + salt).hex() + "'").encode(), {salt.hex(): page})


def test_cli_json_demo_and_packaged_font(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "marketpulse", "--home", str(tmp_path), "--json", "demo"],
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["ok"] and data["data"]["fictional"]
    with Image.open(data["data"]["image"]) as im:
        assert im.width == 1200 and im.height > 1000


def test_native_font_fallback_has_chinese_glyphs(monkeypatch):
    from marketpulse.fonts import font_path
    from PIL import ImageFont

    root = Path(__file__).resolve().parents[1]
    font = root / "src/marketpulse/assets/NotoSansCJKsc-Regular.otf"
    if not font.exists():
        import marketpulse

        font = Path(marketpulse.__file__).parent / "assets/NotoSansCJKsc-Regular.otf"
    monkeypatch.setenv("WECHAT_PULSE_FONT", str(font))
    f = ImageFont.truetype(font_path(), 32)
    assert f.getmask("群聊综合观察").getbbox()


def test_local_only_applies_to_separate_vision_backend(tmp_path):
    import pytest

    picture = tmp_path / "test.png"
    Image.new("RGB", (10, 10)).save(picture)
    cfg = dict(
        provider="ollama",
        model="local",
        local_only=True,
        vision=dict(provider="openai", model="cloud", supports_images=True, local_only=False),
    )
    with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
        with pytest.raises(ValueError, match="local_only"):
            call_model("test", SCHEMA, cfg, images=[picture])


def test_missing_attachment_indexes_preserve_text_report(tmp_path):
    from marketpulse.media import resolve_images

    config = tmp_path / "keys.json"
    config.write_text("{}", encoding="utf-8")
    row = dict(
        id="image",
        local_id=1,
        message_kind="image",
        group_id="123456@chatroom",
        timestamp=1,
        sent_at="2026-01-05T10:00:00+08:00",
        sender="虚构成员",
    )
    with patch("marketpulse.media.read_config", return_value=(tmp_path, {})):
        result = resolve_images([row], tmp_path / "images", config)
    assert len(result) == 1 and "image" not in result[0]
    assert result[0]["failures"][0]["stage"] == "resource_index"


def test_invalid_model_evidence_can_explicitly_fallback_to_rules(tmp_path):
    source = tmp_path / "input.jsonl"
    source.write_text("".join(json.dumps(r) + "\n" for r in records()), encoding="utf-8")
    cfg = dict(provider="rules", input=str(source))
    original, _ = generate("2026-01-05", [], cfg, tmp_path / "first", work_dir=tmp_path / "work")
    invalid = copy.deepcopy(original["content"])
    invalid["themes"][0]["sources"] = ["T99999"]
    with patch("marketpulse.briefing.call_model", return_value=invalid):
        result, _ = generate(
            "2026-01-05",
            [],
            cfg | dict(provider="ollama", model="test", fallback_rules=True),
            tmp_path / "second",
            work_dir=tmp_path / "work",
        )
    assert result["analysis_method"] == "rules-fallback"
    assert any("此前模型尝试" in text for text in result["content"]["limitations"])


def test_multi_group_report_cannot_silently_omit_comparison(tmp_path):
    from marketpulse.briefing import validate_brief
    import pytest

    source = tmp_path / "input.jsonl"
    source.write_text("".join(json.dumps(r) + "\n" for r in records()), encoding="utf-8")
    result, _ = generate(
        "2026-01-05", [], dict(provider="rules", input=str(source)), tmp_path / "out", work_dir=tmp_path / "work"
    )
    result["content"]["group_comparison"] = []
    with pytest.raises(ValueError, match="群间对照"):
        validate_brief(result["content"], result["sources"], result["visuals"], result["groups"])
