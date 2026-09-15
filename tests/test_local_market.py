"""Fictional SQLite prices: identity, cutoff, adjustment, isolation and offline use."""

from datetime import date, timedelta
import hashlib
import json
import sqlite3
from unittest.mock import patch

from PIL import Image
import pytest

from marketpulse.briefing import Visual, analyze_images, identity_visual
from marketpulse.local_market import connection, technical, select_targets
from marketpulse.research import cache_identity, enrich, evidence_catalog


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "本地日线.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE securities(id INTEGER PRIMARY KEY,code TEXT,name TEXT,type TEXT,exchange TEXT);
            CREATE TABLE daily_quotes(security_id INTEGER,trade_date TEXT,open REAL,high REAL,low REAL,
                close REAL,volume REAL,adj_factor REAL,is_suspend INTEGER);
            INSERT INTO securities VALUES(1,'000001','样本甲','A股','SZ');
            INSERT INTO securities VALUES(2,'600001','样本乙','A股','SH');
        """)
        for i in range(66):
            # A 2:1 corporate action must not produce a fake -50% technical crash.
            factor = 1 if i < 60 else 2
            price = (100 + i) / factor
            conn.execute(
                "INSERT INTO daily_quotes VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    1,
                    (date(2025, 1, 1) + timedelta(days=i)).isoformat(),
                    price,
                    price + 1,
                    price - 1,
                    price,
                    1000,
                    factor,
                    0,
                ),
            )
    return {"provider": "rules", "local_market": {"enabled": True, "database": str(path)}}


def target(**kw):
    return {"name": "样本甲", "code": "000001", "technical_missing": False, **kw}


def report():
    stock = dict(
        name="样本甲", code="000001", sources=["I01"], discussion="群友关注", disagreement="证据不足", watch="等待验证"
    )
    return {
        "as_of": "2025-03-06T21:00:00+08:00",
        "sources": {},
        "content": {"stocks": [stock], "other_mentions": [], "themes": []},
        "visuals": [
            Visual(
                image_id="I01",
                kind="chart",
                names=["样本甲"],
                codes=["000001"],
                period="日线",
                visible_time="2025-03-06",
                observations=["价格999，突破MA20"],
                caveats=["历史价格999"],
                readable="clear",
            ).model_dump()
        ],
    }


def test_local_prices_use_factors_and_ended_sessions_even_with_chart(cfg):
    daily = technical(target(), "2025-03-06T21:00:00+08:00", cfg)
    assert daily["status"] == "available"
    assert daily["metrics"]["as_of"] == "2025-03-06"  # March 7 row excluded.
    assert daily["metrics"]["close"] == 82
    assert daily["metrics"]["ma5"] == 81
    assert daily["metrics"]["return5_pct"] == round((164 / 159 - 1) * 100, 2)
    assert daily["metrics"]["volume_vs_prior5"] == 1
    morning = technical(target(), "2025-03-06T10:00:00+08:00", cfg)
    assert morning["metrics"]["as_of"] == "2025-03-05"
    assert str(cfg["local_market"]["database"]) not in json.dumps(daily)


@pytest.mark.parametrize("change", [{"name": "样本乙"}, {"code": "00001.HK"}, {"name": "未核对简称", "code": ""}])
def test_wrong_market_name_or_code_never_silently_rebound(cfg, change):
    assert technical(target(**change), "2025-03-06T21:00:00+08:00", cfg)["status"] == "unavailable"


def test_explicit_alias_resolves_but_never_overrides_conflicting_code(cfg):
    cfg["local_market"]["aliases"] = {"样甲": "000001.SZ"}
    assert technical(target(name="样甲", code=""), "2025-03-06T21:00:00+08:00", cfg)["status"] == "available"
    assert technical(target(name="样甲", code="600001"), "2025-03-06T21:00:00+08:00", cfg)["status"] == "unavailable"


@pytest.mark.parametrize("assignment", ["adj_factor=NULL", "adj_factor=0", "high=1", "volume=-1"])
def test_bad_local_prices_do_not_become_indicators(cfg, assignment):
    with sqlite3.connect(cfg["local_market"]["database"]) as conn:
        conn.execute("UPDATE daily_quotes SET " + assignment + " WHERE trade_date='2025-03-06'")
    daily = technical(target(), "2025-03-06T21:00:00+08:00", cfg)
    assert daily["status"] == "unavailable" and "metrics" not in daily


def test_stale_short_missing_and_read_only(cfg, tmp_path):
    assert technical(target(), "2025-03-20T21:00:00+08:00", cfg)["status"] == "stale"
    assert technical(target(), "2025-01-10T21:00:00+08:00", cfg)["status"] == "unavailable"
    with connection(cfg) as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM daily_quotes")
    cfg["local_market"]["database"] = str(tmp_path / "missing.db")
    data = enrich(report(), cfg, tmp_path)
    assert data["cards"][0]["technical"]["status"] == "unavailable"
    assert not (tmp_path / "missing.db").exists()


def test_rules_local_pipeline_uses_no_model_key_or_network(cfg, tmp_path):
    cfg["market_research"] = {"enabled": True}
    with (
        patch("marketpulse.research.Gateway", side_effect=AssertionError("key/network forbidden")),
        patch("marketpulse.research.call_model", side_effect=AssertionError("model forbidden")),
        patch("marketpulse.research.technical", side_effect=AssertionError("remote prices forbidden")),
    ):
        data = enrich(report(), cfg, tmp_path)
    assert data["technical_source"] == "local"
    assert len(data["cards"]) == 1
    assert data["cards"][0]["technical"]["metrics"]["close"] == 82
    assert data["cards"][0]["news"]["status"] == "disabled"


def test_local_cache_changes_on_committed_wal_update(cfg):
    with sqlite3.connect(cfg["local_market"]["database"]) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        before = cache_identity(cfg)
        writer.execute("UPDATE daily_quotes SET volume=2000 WHERE trade_date='2025-03-06'")
        writer.commit()
        assert cache_identity(cfg) != before
        daily = technical(target(), "2025-03-06T21:00:00+08:00", cfg)
        assert daily["metrics"]["volume_vs_prior5"] == 2


def test_chart_identity_only_but_life_screenshot_is_still_analyzed(tmp_path):
    old = report()["visuals"][0]
    chart = identity_visual(old)
    assert chart["names"] == ["样本甲"] and chart["codes"] == ["000001"]
    assert chart["observations"] == [] and not chart["period"] and not chart["visible_time"]
    assert "999" not in json.dumps(chart, ensure_ascii=False)
    assert "999" not in str(evidence_catalog(report()))
    life = {
        **old,
        "image_id": "I02",
        "kind": "other",
        "names": [],
        "codes": [],
        "observations": ["餐食照片：米饭和蔬菜"],
        "caveats": ["不能确认谁食用"],
    }
    media = []
    for visual, color in [(old, "red"), (life, "green")]:
        path = tmp_path / (visual["image_id"] + ".png")
        Image.new("RGB", (16, 16), color).save(path)
        media.append(
            dict(
                id=visual["image_id"],
                sent_at="2025-03-06T12:00:00+08:00",
                image=dict(path=str(path), size=[16, 16], thumbnail=False),
            )
        )
    with patch("marketpulse.briefing.call_model", return_value={"images": [old, life]}) as model:
        images = analyze_images(media, [], tmp_path / "cache", {"provider": "codex"})
    assert model.call_count == 1
    assert images == [chart, life]
    assert "生活或非市场图也要认真分析" in model.call_args.args[0]


def test_old_backend_chart_cache_cannot_reintroduce_technical_observations(tmp_path):
    from marketpulse.core import digest
    from marketpulse.models import model_identity

    cfg = {"provider": "codex"}
    path = tmp_path / "chart.png"
    Image.new("RGB", (16, 16)).save(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()[:24]
    key = "holistic-context-images-v2-" + digest(model_identity(cfg))[:16] + "-" + sha + ".json"
    (tmp_path / key).write_text(json.dumps(report()["visuals"][0]), encoding="utf-8")
    media = [dict(id="I01", image=dict(path=str(path)))]
    with patch("marketpulse.briefing.call_model", side_effect=AssertionError("cached")):
        result = analyze_images(media, [], tmp_path, cfg)
    assert "999" not in json.dumps(result, ensure_ascii=False)


def test_life_product_mentions_never_trigger_stock_lookup(cfg):
    data = report()
    data["visuals"].append(
        {
            **data["visuals"][0],
            "image_id": "I02",
            "kind": "other",
            "names": ["样本乙"],
            "codes": [],
            "observations": ["产品品牌样本乙"],
        }
    )
    assert len(select_targets(data, cfg)) == 1


def test_valid_later_identity_replaces_bad_code_without_duplicate_stock(cfg):
    data = report()
    data["content"]["stocks"] = []
    data["visuals"][0]["codes"] = ["bad-code"]
    data["visuals"].append({**data["visuals"][0], "image_id": "I02", "codes": ["000001"]})
    targets = select_targets(data, cfg)
    assert len(targets) == 1 and targets[0]["code"] == "000001" and targets[0]["source"] == "I02"


def test_news_auth_and_model_failure_do_not_hide_local_indicators(cfg, tmp_path):
    cfg.update(provider="codex", market_research={"enabled": True})
    with (
        patch("marketpulse.research.Gateway", side_effect=RuntimeError("no key")),
        patch("marketpulse.research.call_model", side_effect=RuntimeError("model unavailable")),
        patch("marketpulse.research.technical", side_effect=AssertionError("remote prices forbidden")),
    ):
        data = enrich(report(), cfg, tmp_path)
    assert data["status"] == "partial"
    card = data["cards"][0]
    assert card["technical"]["metrics"]["ma5"] == 81
    assert card["news"]["status"] == "unavailable"
    assert "本地脚本" in card["interpretation"]


def test_full_report_refreshes_local_prices_without_model_or_chart_crop(cfg, tmp_path):
    from marketpulse.briefing import generate
    from marketpulse.briefing_render import render
    from marketpulse.demo import records

    source = tmp_path / "messages.jsonl"
    row = {**records()[0], "sent_at": "2025-03-06T21:00:00+08:00", "text": "样本甲看好，待核对。"}
    source.write_text(json.dumps(row), encoding="utf-8")
    cfg.update(input=str(source), stock_keywords=["样本甲"])
    with (
        patch("socket.socket.connect", side_effect=AssertionError("network forbidden")),
        patch("marketpulse.briefing.call_model", side_effect=AssertionError("model forbidden")),
        patch("marketpulse.briefing_render.chart_excerpt", side_effect=AssertionError("chart crop forbidden")),
    ):
        first, out = generate("2025-03-06", [], cfg, tmp_path / "out", work_dir=tmp_path / "work")
        with sqlite3.connect(cfg["local_market"]["database"]) as conn:
            conn.execute("UPDATE daily_quotes SET volume=3000 WHERE trade_date='2025-03-06'")
        second, _ = generate("2025-03-06", [], cfg, out, work_dir=tmp_path / "work")
        path = render(second, out)
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert second["chart_policy"] == "identity_only"
    assert second["content"]["stocks"][0]["chart"] == ""
    assert first["market_research"]["cards"][0]["technical"]["metrics"]["volume_vs_prior5"] == 1
    assert second["market_research"]["cards"][0]["technical"]["metrics"]["volume_vs_prior5"] == 3
    with Image.open(path) as image:
        assert image.width == 1200 and image.height > 1000
