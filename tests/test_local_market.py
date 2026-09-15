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
from marketpulse.instrument_identity import expand_targets
from marketpulse.local_market import securities


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


def alias_report(name="东泉", code=""):
    return {
        "as_of": "2025-03-06T21:00:00+08:00",
        "visuals": [],
        "sources": {
            "T01": {"text": name + "在回购", "time": "12:00:00", "group_id": "fictional-a"},
            "T02": {"text": "消费公司的食品业务需求怎么样", "time": "12:01:00", "group_id": "fictional-a"},
        },
        "content": {
            "stocks": [{"name": name, "code": code, "sources": ["T01", "T02"]}],
            "themes": [],
            "other_mentions": [],
        },
    }


def mapping(**kw):
    return dict(
        name="东泉",
        source="T01",
        quote="东泉在回购",
        canonical_name="样本甲",
        market="A股",
        confidence="high",
        reason="同段明确谈消费与食品业务，对应食品公司。",
        sources=["T01", "T02"],
        **kw,
    )


def expanded(data, cfg, tmp_path, items):
    with connection(cfg) as conn:
        catalog = securities(conn)
    cfg["provider"] = "codex"
    with patch("marketpulse.instrument_identity.call_model", return_value={"items": items, "excluded": []}):
        return expand_targets(data, cfg, select_targets(data, cfg, 60), catalog, tmp_path / "identity")


def test_context_expands_nonliteral_nickname_then_uses_validated_daily_prices(cfg, tmp_path):
    data = alias_report()
    chosen = expanded(data, cfg, tmp_path, [mapping()])[0]
    assert chosen["name"] == "东泉" and chosen["lookup_name"] == "样本甲"
    assert chosen["code"] == "000001.SZ" and chosen["identity"]["status"] == "contextual"
    assert chosen["identity"]["sources"] == ["T01", "T02"]
    assert technical(chosen, data["as_of"], cfg)["metrics"]["close"] == 82


def test_expanded_editor_heading_rejoins_verified_literal_nickname(cfg, tmp_path):
    data = alias_report()
    data["content"]["stocks"][0]["name"] = "样本甲"
    chosen = expanded(data, cfg, tmp_path, [mapping()])
    assert len(chosen) == 1
    assert chosen[0]["stock_name"] == "样本甲" and chosen[0]["name"] == "东泉"
    assert chosen[0]["quote"] == "东泉在回购" and chosen[0]["source"] == "T01"
    assert technical(chosen[0], data["as_of"], cfg)["status"] == "available"


def test_duplicate_identity_and_one_bad_proposal_do_not_discard_valid_resolution(cfg, tmp_path):
    data = alias_report()
    bad = {**mapping(), "name": "虚构", "quote": "不存在的原文"}
    chosen = expanded(data, cfg, tmp_path, [mapping(), mapping(), bad])
    assert len(chosen) == 1 and chosen[0]["code"] == "000001.SZ"
    assert data["identity_resolution"]["accepted"] == 1
    assert data["identity_resolution"]["rejected"] == 1


def test_conflicting_identity_proposals_do_not_choose_either_company(cfg, tmp_path):
    data = alias_report()
    other = {**mapping(), "canonical_name": "样本乙"}
    chosen = expanded(data, cfg, tmp_path, [mapping(), other])
    assert chosen[0]["identity"]["status"] == "unresolved"
    assert data["identity_resolution"]["conflicts"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"canonical_name": "不存在的公司"},
        {"confidence": "medium"},
        {"sources": ["T01", "T99"]},
        {"quote": "模型编造的东泉原文"},
    ],
)
def test_ambiguous_or_unbound_model_identity_never_queries_prices(cfg, tmp_path, change):
    data = alias_report()
    chosen = expanded(data, cfg, tmp_path, [{**mapping(), **change}])[0]
    assert chosen["identity"]["status"] == "unresolved"
    with patch("marketpulse.local_market.connection", side_effect=AssertionError("no price query")):
        assert technical(chosen, data["as_of"], cfg)["status"] == "identity_unresolved"


def test_alias_context_cannot_cross_group_boundary(cfg, tmp_path):
    data = alias_report()
    data["sources"]["T02"]["group_id"] = "fictional-b"
    chosen = expanded(data, cfg, tmp_path, [mapping()])[0]
    assert chosen["identity"]["status"] == "unresolved"


@pytest.mark.parametrize("code", ["600001", "00001.HK"])
def test_context_cannot_override_explicit_different_code_or_market(cfg, tmp_path, code):
    data = alias_report(code=code)
    data["sources"]["T01"]["text"] += " " + code
    chosen = expanded(data, cfg, tmp_path, [mapping()])[0]
    assert chosen["code"] == code and chosen["identity"]["status"] == "unresolved"


def test_rules_keeps_ambiguous_candidates_without_model_or_price_claim(cfg, tmp_path):
    with sqlite3.connect(cfg["local_market"]["database"]) as c:
        c.execute("ALTER TABLE securities ADD COLUMN industry TEXT")
        c.execute("UPDATE securities SET name='东泉食品',industry='食品' WHERE id=1")
        c.execute("UPDATE securities SET name='东泉机械',industry='机械' WHERE id=2")
    data = alias_report()
    with connection(cfg) as c:
        catalog = securities(c)
    with patch("marketpulse.instrument_identity.call_model", side_effect=AssertionError("offline")):
        chosen = expand_targets(data, cfg, select_targets(data, cfg), catalog, tmp_path)[0]
    assert {c["industry"] for c in chosen["identity"]["candidates"]} == {"食品", "机械"}
    assert technical(chosen, data["as_of"], cfg)["status"] == "identity_unresolved"


def test_cited_holdings_application_brand_does_not_become_a_security(cfg):
    data = report()
    data["visuals"].append(
        {
            **data["visuals"][0],
            "image_id": "I02",
            "kind": "holdings",
            "names": ["样本甲"],
            "codes": ["000001"],
            "observations": ["样本乙软件的自选列表"],
        }
    )
    data["content"]["other_mentions"] = [{"title": "股票列表", "text": "持仓栏目", "sources": ["I02"]}]
    assert [t["name"] for t in select_targets(data, cfg)] == ["样本甲"]


def test_resolved_news_filters_by_canonical_company_not_shared_prefix(cfg, tmp_path):
    from marketpulse.research import news, options

    data = alias_report()
    chosen = expanded(data, cfg, tmp_path, [mapping()])[0]

    class Search:
        def request(self, path, body):
            assert body["query"].startswith("样本甲")
            return {
                "result": [
                    {
                        "status": "success",
                        "source": "announcement",
                        "content": [
                            {"date": "2025-03-05", "title": "东泉机械回购公告", "snippet": "其他主体"},
                            {"date": "2025-03-05", "title": "样本甲公告", "snippet": "公司消息"},
                        ],
                    }
                ]
            }, "2025-03-06T21:00:00+08:00"

    hits = news(chosen, data["as_of"], Search(), options(cfg))
    assert [n["title"] for n in hits["items"]] == ["样本甲公告"]


def test_identity_cache_respects_context_and_model(cfg, tmp_path):
    data = alias_report()
    cfg["provider"] = "codex"
    with connection(cfg) as c:
        catalog = securities(c)
    with patch(
        "marketpulse.instrument_identity.call_model", return_value={"items": [mapping()], "excluded": []}
    ) as model:
        for _ in range(2):
            expand_targets(data, cfg, select_targets(data, cfg), catalog, tmp_path)
        assert model.call_count == 1
        cfg["model"] = "fictional-model"
        expand_targets(data, cfg, select_targets(data, cfg), catalog, tmp_path)
        data["sources"]["T02"]["text"] = "这里谈的是机械公司"
        expand_targets(data, cfg, select_targets(data, cfg), catalog, tmp_path)
        assert model.call_count == 3


def test_share_image_selects_only_usable_local_data_but_keeps_audit():
    from marketpulse.briefing_render import visible_research

    states = ["available", "unavailable", "stale", "identity_unresolved", "unsupported_instrument"]
    research = {"cards": [{"name": state, "technical": {"source_kind": "local", "status": state}} for state in states]}
    assert [c["name"] for c in visible_research(research)] == ["available"]
    assert len(research["cards"]) == 5


def test_common_word_matching_stock_name_is_excluded_using_context(cfg, tmp_path):
    data = alias_report()
    data["content"]["stocks"] = []
    data["sources"]["T01"]["text"] = "这里样本甲只是问卷分组，并非证券"
    data["content"]["themes"] = [{"sources": ["T01"]}]
    cfg["provider"] = "codex"
    with connection(cfg) as c:
        catalog = securities(c)
    plan = {
        "items": [],
        "excluded": [
            {"name": "样本甲", "source": "T01", "quote": "样本甲只是问卷分组", "reason": "问卷分组不是证券提及"}
        ],
    }
    with patch("marketpulse.instrument_identity.call_model", return_value=plan):
        result = expand_targets(data, cfg, select_targets(data, cfg), catalog, tmp_path)
    assert not result


def add_financial_fixture(cfg):
    with sqlite3.connect(cfg["local_market"]["database"]) as c:
        c.executescript("""
            CREATE TABLE financial_indicator(security_id INTEGER,ann_date TEXT,end_date TEXT,
                or_yoy REAL,netprofit_yoy REAL,grossprofit_margin REAL,gross_margin REAL,roe REAL,ocfps REAL);
            INSERT INTO financial_indicator VALUES(1,'2024-09-01','2024-06-30',10,20,30,999999,5,0.5);
            INSERT INTO financial_indicator VALUES(1,'2025-03-01','2024-12-31',15,25,32,999999,8,NULL);
            INSERT INTO financial_indicator VALUES(1,'2025-04-01','2025-03-31',99,99,99,999999,99,99);
            INSERT INTO financial_indicator VALUES(1,'2025-03-06','2025-03-01',88,88,88,999999,88,88);
            CREATE TABLE daily_basic(security_id INTEGER,trade_date TEXT,close REAL,pe_ttm REAL,pb REAL);
            INSERT INTO daily_basic VALUES(1,'2025-03-06',82,20,3);
            INSERT INTO daily_basic VALUES(1,'2025-03-07',83,21,4);
        """)


def test_financials_use_announced_period_not_future_or_same_day_disclosure(cfg):
    from marketpulse.fundamentals import read

    add_financial_fixture(cfg)
    daily = technical(target(), "2025-03-06T21:00:00+08:00", cfg)
    data = read(target(), daily, cfg)
    assert data["financials"]["period_end"] == "2024-12-31"
    assert data["financials"]["metrics"]["gross_margin_pct"] == 32
    assert "operating_cashflow_per_share" not in data["financials"]["metrics"]  # no older-period fill
    assert data["financials"]["same_period_year_ago_changes"] == {}
    assert data["valuation"] == {"as_of": "2025-03-06", "pe_ttm": 20, "pb": 3}
    assert "999999" not in json.dumps(data)


@pytest.mark.parametrize("assignment", ["close=92", "trade_date='2025-03-05'", "pe_ttm=-2,pb=NULL"])
def test_valuation_refuses_mismatched_price_date_and_invalid_multiples(cfg, assignment):
    from marketpulse.fundamentals import read

    add_financial_fixture(cfg)
    with sqlite3.connect(cfg["local_market"]["database"]) as c:
        c.execute("UPDATE daily_basic SET " + assignment + " WHERE trade_date='2025-03-06'")
    daily = technical(target(), "2025-03-06T21:00:00+08:00", cfg)
    assert read(target(), daily, cfg)["valuation"] == {}


def test_local_conclusion_requires_its_own_financial_and_price_evidence(cfg):
    from marketpulse.investment_analysis import assess

    card = {
        "id": "E01",
        "name": "样本甲",
        "lookup_name": "样本甲",
        "quote": "看好",
        "technical": {"status": "available", "source_kind": "local"},
        "news": {"items": []},
        "fundamentals": {"status": "available", "financials": {"period_end": "2024-12-31"}},
    }
    answer = dict(
        id="E01",
        conclusion="增长与价格走势相互支持，但仍需验证持续性。",
        technical_view="趋势偏强。",
        fundamental_view="盈利增长。",
        news_view="",
        watch="若转弱则重新评估。",
        evidence_ids=["E01D"],
    )
    with patch("marketpulse.research.call_model", return_value={"items": [answer]}):
        with pytest.raises(ValueError, match="财务依据"):
            assess([card], {}, {"provider": "codex"})
    answer["evidence_ids"] = ["E01D", "E01F"]
    with patch("marketpulse.research.call_model", return_value={"items": [answer]}):
        assess([card], {}, {"provider": "codex"})
    assert card["analysis"]["conclusion"] == answer["conclusion"]


def test_long_evidence_is_batched_within_selected_model_budget():
    from marketpulse.investment_analysis import assess

    cards = [
        {
            "id": f"E{i:02}",
            "name": "样本",
            "lookup_name": "样本",
            "quote": "看好",
            "technical": {"status": "available", "source_kind": "local"},
            "news": {"items": [{"title": "样本公告", "snippet": "虚构资料" * 300}]},
        }
        for i in range(1, 5)
    ]
    cfg = {"provider": "codex", "max_input_chars": 4000}

    def model(prompt, schema, config, **kwargs):
        assert len(prompt) <= config["max_input_chars"]
        items = json.loads(prompt.split("\n", 1)[1])
        return {
            "items": [
                dict(
                    id=item["id"],
                    conclusion="趋势仍需后续价格验证。",
                    technical_view="依据本地价格判断。",
                    fundamental_view="",
                    news_view="",
                    watch="若价格转弱则重新评估。",
                    evidence_ids=[item["id"] + "D"],
                )
                for item in items
            ]
        }

    with patch("marketpulse.research.call_model", side_effect=model) as request:
        assess(cards, {}, cfg)
    assert request.call_count > 1
    assert all(c["analysis"]["method"] == "codex" for c in cards)


def test_positive_profit_change_does_not_make_a_loss_profitable():
    from marketpulse.investment_analysis import scripted

    daily = {"status": "available", "metrics": {"close": 10, "ma5": 11, "ma20": 12, "return20_pct": -5}}
    financial = {
        "financials": {"period_end": "2025-06-30", "metrics": {"revenue_yoy_pct": 10, "profit_yoy_pct": 20, "eps": -1}}
    }
    assert "仍亏损" in scripted(daily, financial)["fundamental_view"]


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
