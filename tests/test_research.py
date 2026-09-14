"""Synthetic daily bars and HTTP fixtures; no live credentials or market data."""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import httpx
import pytest

from marketpulse.research import (
    Target,
    Plan,
    Gateway,
    options,
    cache_identity,
    validate_targets,
    parse_daily,
    indicators,
    cutoff_day,
    technical,
    news,
    enrich,
    assess,
    safe_url,
)


def target(**changes):
    return (
        dict(
            name="样本甲",
            lookup_name="样本甲",
            code="",
            kind="equity",
            stock_name="样本甲",
            source="T001",
            quote="样本甲只说看好",
            technical_missing=True,
            news_missing=True,
        )
        | changes
    )


def table(n=65):
    lines = ["| 公司名称 | 时间 | 指标名称 | 数值 | 单位 |", "| --- | --- | --- | --- | --- |"]
    for i in range(n):
        date = (datetime(2025, 1, 1) + timedelta(days=i)).strftime("%Y%m%d")
        for metric, value, unit in (
            ("前复权收盘价", 100 + i, "元"),
            ("前复权最高价", 102 + i, "元"),
            ("前复权最低价", 98 + i, "元"),
            ("成交量", 1000, "手"),
        ):
            lines.append(f"| 样本甲 | {date} | {metric} | {value} | {unit} |")
    return "\n".join(lines)


def test_daily_metrics_and_prior_range_exclude_current_day():
    result = indicators(parse_daily(table(), target(), "2025-03-06"), "2025-03-06")
    m = result["metrics"]
    assert m["close"] == 164
    assert m["ma5"] == 162 and m["ma60"] == 134.5
    assert m["return5_pct"] == round((164 / 159 - 1) * 100, 2)
    assert m["prior20_high"] == 165  # today's high (166) is excluded
    assert m["volume_vs_prior5"] == 1


@pytest.mark.parametrize(
    "bad",
    [
        lambda t: t.replace("样本甲", "样本乙"),
        lambda t: t.replace("前复权收盘价", "收盘价"),
        lambda t: t.replace("| 100 | 元 |", "| NaN | 元 |", 1),
        lambda t: t.replace("| 100 | 元 |", "| 100 | 美元 |", 1),
        lambda t: t + "\n| 样本甲 | 20250101 | 前复权收盘价 | 999 | 元 |",
        lambda t: t.replace("| 102 | 元 |", "| 90 | 元 |", 1),
    ],
)
def test_refuse_mismatched_or_corrupt_bars(bad):
    with pytest.raises(ValueError):
        parse_daily(bad(table()), target(), "2025-03-06")


def test_future_rows_filtered_and_short_stale_series_not_current():
    parsed = parse_daily(table(), target(), "2025-02-01")
    assert parsed["bars"][-1]["date"] == "2025-02-01"
    assert indicators(parsed, "2025-02-15")["status"] == "stale"
    with pytest.raises(ValueError, match="不足21"):
        parse_daily(table(20), target(), "2025-03-06")


def test_expanded_name_must_match_not_just_shared_short_alias():
    with pytest.raises(ValueError, match="名称不能核对"):
        parse_daily(table(), target(lookup_name="样本甲科技"), "2025-03-06")


def test_close_cutoff_uses_returned_market_not_chinese_name_guess():
    assert cutoff_day("2025-03-06T21:00:00+08:00", target()) == "2025-03-05"
    assert cutoff_day("2025-03-06T21:00:00+08:00", target(), "A股财务行情数据库") == "2025-03-06"
    assert cutoff_day("2025-03-06T10:30:00+08:00", target(), "A股财务行情数据库") == "2025-03-05"


def test_literal_mentions_and_card_binding_required():
    data = target()
    assert validate_targets(Plan(targets=[Target(**data)]), {"T001": data["quote"]}, {"样本甲"})
    for change in (
        {"quote": "虚构样本甲"},
        {"code": "123456"},
        {"stock_name": "样本乙"},
        {"lookup_name": "样本甲 https://private.example"},
    ):
        with pytest.raises(ValueError):
            validate_targets(Plan(targets=[Target(**(data | change))]), {"T001": data["quote"]}, {"样本甲", "样本乙"})


def test_offline_rules_and_local_only_never_load_key_or_query(tmp_path):
    for cfg in ({"provider": "rules"}, {"provider": "ollama"}, {"provider": "codex", "local_only": True}):
        cfg["market_research"] = {"enabled": True}
        with patch("marketpulse.research.Gateway", side_effect=AssertionError("network")):
            assert enrich({}, cfg, tmp_path) == {"status": "disabled", "cards": []}
        assert cache_identity(cfg) == {"enabled": False}


def test_gateway_cached_and_error_sanitized(tmp_path):
    cfg = {"provider": "codex", "market_research": {"enabled": True}}
    with patch.dict("os.environ", {"YIXIN_API_KEY": "fixture-secret"}):
        gateway = Gateway(options(cfg), tmp_path)
    result = httpx.Response(200, json={"success": True, "result": []})
    with patch("httpx.Client.post", return_value=result) as post:
        first = gateway.request("/v2/search", {"query": "样本甲"})
        assert gateway.request("/v2/search", {"query": "样本甲"}) == first
        assert post.call_count == 1
        assert post.call_args.kwargs["headers"] == {"X-API-KEY": "fixture-secret"}
    bad = httpx.Response(402, text="fixture-secret request refused")
    with patch("httpx.Client.post", return_value=bad) as post:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="HTTP 402"):
                gateway.request("/v1/fin_db", {"query": "样本乙"})
        assert post.call_count == 1
    assert "fixture-secret" not in "".join(p.read_text() for p in tmp_path.glob("*.json"))


class FakeGateway:
    def __init__(self, response):
        self.response, self.calls = response, []

    def request(self, path, body):
        self.calls.append((path, body))
        return self.response, "2025-03-06T21:30:00+08:00"


def test_retrieval_payload_does_not_contain_chat_quote_or_speaker():
    gateway = FakeGateway(
        {"success": True, "result": [{"status": "success", "source": "A股财务行情数据库", "content": table()}]}
    )
    result = technical(target(quote="样本甲 用户私人观点", source="T999"), "2025-03-06T21:00:00+08:00", gateway)
    assert result["status"] == "available"
    payload = json.dumps(gateway.calls, ensure_ascii=False)
    assert "用户私人观点" not in payload and "T999" not in payload
    assert technical(target(technical_missing=False), "2025-03-06T21:00:00+08:00", gateway)["status"] == "not_requested"
    assert len(gateway.calls) == 1


def test_commodity_benchmark_explicit_and_wrong_series_rejected():
    gateway = FakeGateway(
        {"success": True, "result": [{"status": "success", "content": table().replace("样本甲", "黄金期货")}]}
    )
    data = technical(target(name="黄金", lookup_name="黄金", kind="commodity"), "2025-03-06T21:00:00+08:00", gateway)
    assert data["status"] == "unavailable"
    assert "伦敦现货黄金" in data["summary"]
    assert "伦敦现货黄金" in gateway.calls[0][1]["query"]


def test_oil_spot_is_dated_public_reference_without_key_or_futures_claim(tmp_path):
    text = "observation_date,DCOILWTICO\n" + "\n".join(
        (datetime(2025, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d") + f",{100 + i}" for i in range(65)
    )
    gateway = FakeGateway({})
    gateway.cache, gateway.opt = tmp_path, {"cache_minutes": 60}
    with patch("httpx.Client.get", return_value=httpx.Response(200, text=text)) as get:
        data = technical(
            target(name="原油", lookup_name="原油", kind="commodity"), "2025-03-06T21:00:00+08:00", gateway
        )
    assert data["status"] == "available"
    assert data["metrics"]["as_of"] == "2025-03-05"  # exclude same dated session
    assert "非期货K线" in data["summary"] and "成交量" in data["summary"]
    assert "headers" not in get.call_args.kwargs
    assert gateway.calls == []


def test_news_filters_undated_future_unrelated_and_unsafe_links():
    hits = [
        dict(title="样本甲 公告", snippet="正文摘要", date=d, link="javascript:alert(1)")
        for d in ("2025-03-05", "2025-03-06", "2025-03-07", "", "2024-01-01")
    ]
    hits.append(dict(title="样本乙 公告", snippet="无关", date="2025-03-05"))
    gateway = FakeGateway(
        {"success": True, "result": [{"status": "success", "source": "announcement", "content": hits}]}
    )
    data = news(target(), "2025-03-06T21:00:00+08:00", gateway, {"news_days": 14})
    assert len(data["items"]) == 1
    assert data["items"][0]["date"] == "2025-03-05"
    assert data["items"][0]["url"] == "" and data["items"][0]["level"] == "search_excerpt"
    assert safe_url("https://example.com/announcement")


def test_announcement_fetch_preserves_handle_and_marks_truncation():
    gateway = FakeGateway({})
    hits = {
        "success": True,
        "result": [
            {
                "status": "success",
                "source": "announcement",
                "content": [
                    dict(title="样本甲 公告", snippet="摘要", date="2025-03-05", extra={"doc_id": "opaque-handle"})
                ],
            }
        ],
    }
    with patch.object(
        gateway,
        "request",
        side_effect=[
            (hits, "2025-03-06"),
            ({"success": True, "title": "样本甲 公告", "content": "部分全文", "truncated": True}, "2025-03-06"),
        ],
    ) as call:
        data = news(target(), "2025-03-06T21:00:00+08:00", gateway, {"news_days": 14})
    assert call.call_args.args == ("/v2/fetch", {"doc_id": "opaque-handle", "source": "announcement"})
    assert data["items"][0]["level"] == "partial_text"


def test_analysis_cannot_borrow_other_instrument_evidence():
    cards = [{**target(), "id": "E01", "technical": {"status": "available"}, "news": {"items": []}}]
    with patch(
        "marketpulse.research.call_model",
        return_value={"items": [dict(id="E01", interpretation="看好", watch="观察", evidence_ids=["E02D"])]},
    ):
        with pytest.raises(ValueError, match="其他标的"):
            assess(cards, {}, {"provider": "codex"})
    assert cards[0]["interpretation"].startswith("外部资料不足")
