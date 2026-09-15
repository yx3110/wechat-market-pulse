"""Read-only daily prices from a user-configured StockTrade-compatible SQLite DB.

No network requests, database discovery, imports from another checkout, or writes.
Names and tickers come from cited chat text or image identity extraction.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sqlite3

from .core import digest, now

VERSION = "local-daily-v1"


def enabled(cfg):
    return bool((cfg.get("local_market") or {}).get("enabled"))


def db_path(cfg):
    value = (cfg.get("local_market") or {}).get("database")
    if not isinstance(value, str) or not value:
        raise ValueError("尚未配置本地行情库 local_market.database。")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError("配置的本地行情库不存在。")
    return path


def cache_identity(cfg):
    opt = cfg.get("local_market") or {}
    try:
        path = db_path(cfg)
        stamps = []
        for file in (path, Path(str(path) + "-wal")):
            stat = file.stat() if file.exists() else None
            stamps.append((stat.st_size, stat.st_mtime_ns) if stat else None)
        signature = digest([str(path), stamps])
    except (ValueError, OSError):
        signature = "unavailable"
    return {"version": VERSION, "database_signature": signature, "settings": digest(opt), "enabled": enabled(cfg)}


@contextmanager
def connection(cfg):
    conn = sqlite3.connect(db_path(cfg).as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")  # Includes committed WAL rows in one consistent read.
        yield conn
    finally:
        conn.close()


def securities(conn):
    return [dict(r) for r in conn.execute("SELECT id,code,name,type,exchange FROM securities WHERE type='A股'")]


def resolve(name, code, catalog, cfg):
    aliases = (cfg.get("local_market") or {}).get("aliases", {})
    if not isinstance(aliases, dict):
        raise ValueError("本地证券简称映射必须是名称到代码的对象。")
    configured = aliases.get(name)
    if configured is not None and not isinstance(configured, str) or not isinstance(code, str):
        raise ValueError("本地证券代码及简称映射必须使用字符串。")
    if configured and code and configured.upper().split(".")[0] != code.upper().split(".")[0]:
        raise ValueError("原文代码与已配置简称不一致，未匹配行情。")
    code = code or configured or ""
    match = re.fullmatch(r"(\d{6})(?:\.(SH|SZ|BJ))?", code.upper()) if code else None
    if code and not match:
        raise ValueError("本地适配器仅接受明确的 A 股代码，未匹配其他市场或商品。")
    matches = (
        [s for s in catalog if s["code"] == match[1] and (not match[2] or s["exchange"] == match[2])]
        if match
        else [s for s in catalog if s["name"] == name]
    )
    if len(matches) != 1:
        raise ValueError("本地 A 股库中未找到唯一标的；不猜测简称、市场或合约。")
    security = matches[0]
    if not re.fullmatch(r"\d{6}", security["code"]) or security["exchange"] not in ("SH", "SZ", "BJ"):
        raise ValueError("本地证券代码或交易所格式不支持。")
    if configured and "." in configured and configured.upper().split(".")[1] != security["exchange"]:
        raise ValueError("简称映射的交易所与本地证券不一致。")
    if name not in (security["name"], security["code"], code) and not configured:
        raise ValueError("名称与代码在本地证券表中不一致，未计算技术指标。")
    return security


def select_targets(result, cfg, limit=12):
    """Prefer report stocks, then literal mentions in cited market material.

    Do not infer a listed company from life photos, products, or uncited prose.
    A failed lookup remains an explicit card for an already selected stock.
    """
    from .research import evidence_catalog

    evidence = evidence_catalog(result)
    stocks = result["content"]["stocks"]
    try:
        with connection(cfg) as conn:
            catalog = securities(conn)
    except (ValueError, OSError, sqlite3.Error):
        catalog = []  # Still show each primary stock's explicit local-data gap.
    targets, seen, by_name, verified = [], set(), {}, set()

    def add(name, code, sid, stock_name=""):
        quote = evidence.get(sid, "")
        # Full source text stays private in the audit artifact. No invented ticker.
        if not name or name not in quote or code and code not in quote:
            return
        key = (name, code)
        if key in seen:
            return
        try:
            matched = resolve(name, code, catalog, cfg)
            key = (matched["code"], matched["exchange"])
            resolved = True
        except ValueError:
            resolved = False
        if key in seen:
            return
        if name in by_name and (name in verified or not resolved):
            return
        seen.add(key)
        if resolved:
            verified.add(name)
        kind = "equity"
        if not resolved and any(word in name for word in ("原油", "WTI", "布伦特", "黄金", "白银", "期货")):
            kind = "commodity"
        elif not resolved and (name.upper() == "A50" or "指数" in name):
            kind = "index"
        target = dict(
            name=name,
            lookup_name=name,
            code=code,
            kind=kind,
            stock_name=stock_name,
            source=sid,
            quote=quote,
            technical_missing=True,
            news_missing=True,
        )
        if name in by_name:
            index = by_name[name]
            target["stock_name"] = targets[index]["stock_name"] or stock_name
            targets[index] = target  # Use the separately verifiable mention.
        else:
            by_name[name] = len(targets)
            targets.append(target)

    for stock in stocks:
        for sid in stock["sources"]:
            text = evidence.get(sid, "")
            if stock["name"] in text:
                code = stock["code"] if stock.get("code") and stock["code"] in text else ""
                add(stock["name"], code, sid, stock["name"])
                break
        else:
            # Keep the data gap visible without querying for an inferred identity.
            sid = stock["sources"][0]
            by_name[stock["name"]] = len(targets)
            targets.append(
                dict(
                    name=stock["name"],
                    lookup_name=stock["name"],
                    code="",
                    kind="equity",
                    stock_name=stock["name"],
                    source=sid,
                    quote=evidence.get(sid, ""),
                    technical_missing=True,
                    news_missing=False,
                    identity_unverified=True,
                )
            )

    items = result["content"].get("other_mentions", []) + result["content"].get("themes", [])
    for member in result.get("focus_members", []):
        if member.get("content"):
            items += member["content"]["positions"]
    ids = list(dict.fromkeys(sid for item in items for sid in item["sources"]))
    charts = [v for v in result["visuals"] if v.get("kind") == "chart" and not v.get("excluded_from_analysis")]
    for visual in charts:
        sid = visual["image_id"]
        for name in visual.get("names", []):
            codes = visual.get("codes", [])
            code = codes[0] if len(codes) == len(visual["names"]) == 1 else ""
            add(name, code, sid)
    for sid in ids:
        text = evidence.get(sid, "")
        for security in catalog:
            if len(security["name"]) >= 3 and security["name"] in text:
                add(security["name"], "", sid)
    return targets[:limit]


def enrich(result, cfg, cache, progress=print):
    from .research import Gateway, assess, news, options

    opt = options(cfg)
    report = {
        "version": VERSION,
        "status": "partial",
        "technical_source": "local",
        "checked_at": now(),
        "as_of": result["as_of"],
        "cards": [],
    }
    gateway, news_error = None, "未启用外部消息面检索；本地行情库不提供新闻核验。"
    if opt["enabled"]:
        try:
            gateway = Gateway(opt, cache)
        except (RuntimeError, OSError, ValueError):
            news_error = "外部消息面服务不可用；本地日线分析照常保留。"
    targets = select_targets(result, cfg, opt["max_targets"])
    for i, target in enumerate(targets, 1):
        progress(f"本地日线 {i}/{len(targets)}：{target['name']}", flush=True)
        daily = technical(target, result["as_of"], cfg)
        recent = {"status": "unavailable" if opt["enabled"] else "disabled", "items": [], "summary": news_error}
        if gateway and not target.get("identity_unverified"):
            try:
                recent = news(target, result["as_of"], gateway, opt)
            except (RuntimeError, ValueError, OSError, TypeError, KeyError):
                recent = {"status": "unavailable", "items": [], "summary": "消息面检索未完成，保留本地日线。"}
        refs = [f"E{i:02}D"] if daily["status"] in ("available", "stale") else []
        report["cards"].append(
            {
                **target,
                "id": f"E{i:02}",
                "technical": daily,
                "news": recent,
                "evidence_ids": refs,
                "interpretation": "日线指标由本地脚本计算；群友观点需结合数据日期独立核验。"
                if refs
                else "本地行情不足，暂不作技术走势判断。",
                "watch": "观察后续交易日价格与MA20、成交量的变化；历史区间不保证构成支撑或压力。"
                if daily["status"] == "available"
                else "等待可核对的最新本地日线。",
            }
        )
    if cfg.get("provider", "rules") != "rules" and result.get("analysis_method") != "rules-fallback":
        try:
            assess(report["cards"], result, cfg)
        except (RuntimeError, ValueError, OSError, TypeError, KeyError):
            report["error"] = "模型解读未完成，保留已计算的本地日线指标和数据缺口。"
    report["status"] = (
        "partial"
        if report.get("error")
        or any(
            c["technical"]["status"] != "available" or c["news"]["status"] in ("unavailable", "empty")
            for c in report["cards"]
        )
        else "completed"
    )
    return report


def technical(target, as_of, cfg):
    from .research import cutoff_day, indicators

    try:
        if target.get("identity_unverified"):
            raise ValueError("标的名称没有可核对的原文或图片识别依据，暂不匹配本地行情。")
        with connection(cfg) as conn:
            security = resolve(target["name"], target.get("code", ""), securities(conn), cfg)
            cutoff = cutoff_day(as_of, {"kind": "equity", "code": security["code"]}, "A股财务行情数据库")
            latest_market = conn.execute(
                "SELECT MAX(trade_date) FROM daily_quotes WHERE trade_date<=?", (cutoff,)
            ).fetchone()[0]
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT trade_date,open,high,low,close,volume,adj_factor,is_suspend FROM daily_quotes "
                    "WHERE security_id=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 80",
                    (security["id"], cutoff),
                )
            ][::-1]
        if len(rows) < 21:
            raise ValueError("本地可用日线不足21个交易日，暂不判断趋势。")
        for row in rows:
            datetime.strptime(row["trade_date"], "%Y-%m-%d")
            for field in ("open", "high", "low", "close", "volume", "adj_factor"):
                value = row[field]
                if (
                    value is None
                    or not math.isfinite(float(value))
                    or float(value) < 0
                    or (field != "volume" and float(value) == 0)
                ):
                    raise ValueError("本地日线价格、成交量或复权因子缺失/无效，暂不计算指标。")
            if row["high"] < max(row["open"], row["close"], row["low"]) or row["low"] > min(row["open"], row["close"]):
                raise ValueError("本地日线高低价冲突，暂不计算指标。")
        anchor = rows[-1]["adj_factor"]
        bars = [
            {
                "date": row["trade_date"],
                "volume": row["volume"],
                **{
                    field: round(row[field] * row["adj_factor"] / anchor, 8)
                    for field in ("open", "high", "low", "close")
                },
            }
            for row in rows
        ]
        data = indicators(
            dict(
                bars=bars,
                name=security["name"],
                adjustment="前复权（以本次末日因子归一）",
                price_unit="元",
                volume_unit="本地库原单位（仅计算比值）",
            ),
            cutoff,
        )
        label = f"{security['name']} {security['code']}.{security['exchange']} · A股"
        data["summary"] = label + "。" + data["summary"]
        if rows[-1]["trade_date"] != latest_market or rows[-1]["is_suspend"]:
            data["status"] = "stale"
            data["summary"] = "该标的数据落后于本地库最新交易日或末日停牌，不能判断当前走势。" + data["summary"]
        data.update(
            source="本地行情库",
            source_kind="local",
            instrument=label,
            code=security["code"] + "." + security["exchange"],
            fetched_at=now(),
            query_cutoff=cutoff,
            local_market_date=latest_market,
            raw=json.dumps(
                {
                    "security": {k: v for k, v in security.items() if k != "id"},
                    "daily_quotes": rows,
                    "adjusted_bars": bars,
                },
                ensure_ascii=False,
            ),
            query="只读 securities / daily_quotes；不晚于 " + cutoff + "；末日因子归一计算，未采用截图读数。",
        )
        return data
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        return {
            "status": "unavailable",
            "source_kind": "local",
            "summary": str(exc) if isinstance(exc, ValueError) else "本地行情库读取或格式校验失败。",
        }
