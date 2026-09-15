"""Optional local financial indicators and valuation, with disclosure-date guards."""

from datetime import date
import math
import sqlite3

from .core import now

FIELDS = {
    "or_yoy": ("revenue_yoy_pct", "营业收入同比（%）"),
    "netprofit_yoy": ("profit_yoy_pct", "归母净利润同比（%）"),
    "dt_netprofit_yoy": ("adjusted_profit_yoy_pct", "扣非净利润同比（%）"),
    "grossprofit_margin": ("gross_margin_pct", "销售毛利率（%）"),
    "netprofit_margin": ("net_margin_pct", "销售净利率（%）"),
    "roe": ("roe_pct", "报告期ROE（%，非年化）"),
    "debt_to_assets": ("debt_to_assets_pct", "资产负债率（%）"),
    "ocfps": ("operating_cashflow_per_share", "报告期每股经营现金流（元）"),
    "eps": ("eps", "报告期每股收益（元）"),
}


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def read(target, daily, cfg):
    from .local_market import connection, resolve, securities

    result = {
        "status": "unavailable",
        "source": "本地财务与估值库",
        "source_kind": "local",
        "fetched_at": now(),
        "financials": {},
        "valuation": {},
    }
    if daily.get("status") != "available":
        return result
    cutoff = daily["metrics"]["as_of"]
    try:
        with connection(cfg) as conn:
            identity = target.get("identity", {})
            security = resolve(
                identity.get("canonical_name", target["name"]), target.get("code", ""), securities(conn), cfg
            )
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "financial_indicator" in tables:
                columns = {r[1] for r in conn.execute("PRAGMA table_info(financial_indicator)")}
                selected = [k for k in FIELDS if k in columns]
                if selected and {"security_id", "ann_date", "end_date"} <= columns:
                    # Date-only disclosures on the cutoff day are conservatively
                    # excluded; end_date is the fiscal period, not availability.
                    rows = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT ann_date,end_date," + ",".join(selected) + " FROM financial_indicator "
                            "WHERE security_id=? AND ann_date IS NOT NULL AND ann_date<? AND end_date<=? "
                            "ORDER BY end_date DESC,ann_date DESC LIMIT 24",
                            (security["id"], cutoff, cutoff),
                        )
                    ]
                    valid = []
                    for row in rows:
                        try:
                            period, announced = date.fromisoformat(row["end_date"]), date.fromisoformat(row["ann_date"])
                        except (TypeError, ValueError):
                            continue
                        if period <= announced and 0 <= (date.fromisoformat(cutoff) - period).days <= 550:
                            valid.append(row)
                    if valid:
                        latest = valid[0]
                        metrics = {FIELDS[k][0]: latest[k] for k in selected if finite(latest[k])}
                        # Gross_margin in the upstream schema is an amount, not
                        # a percentage; only grossprofit_margin is used above.
                        prior_date = str(int(latest["end_date"][:4]) - 1) + latest["end_date"][4:]
                        prior = next((r for r in valid if r["end_date"] == prior_date), None)
                        changes = {}
                        if prior:
                            for key in ("grossprofit_margin", "netprofit_margin", "roe", "debt_to_assets"):
                                if finite(latest.get(key)) and finite(prior.get(key)):
                                    changes[FIELDS[key][0] + "_yoy_pp"] = round(latest[key] - prior[key], 4)
                        if metrics:
                            result["financials"] = {
                                "period_end": latest["end_date"],
                                "announced": latest["ann_date"],
                                "metrics": metrics,
                                "same_period_year_ago_changes": changes,
                            }
            if "daily_basic" in tables:
                columns = {r[1] for r in conn.execute("PRAGMA table_info(daily_basic)")}
                fields = [f for f in ("close", "pe_ttm", "pb") if f in columns]
                if fields and {"security_id", "trade_date"} <= columns:
                    row = conn.execute(
                        "SELECT trade_date,"
                        + ",".join(fields)
                        + " FROM daily_basic WHERE security_id=? AND trade_date=? LIMIT 1",
                        (security["id"], cutoff),
                    ).fetchone()
                    if row:
                        row = dict(row)
                        if finite(row.get("close")) and abs(row["close"] - daily["metrics"]["close"]) <= 0.02:
                            values = {k: row[k] for k in ("pe_ttm", "pb") if finite(row.get(k)) and row[k] > 0}
                            if values:
                                result["valuation"] = {"as_of": cutoff, **values}
        result["status"] = "available" if result["financials"] or result["valuation"] else "unavailable"
        result["metric_definitions"] = {value[0]: value[1] for value in FIELDS.values()}
        result["notes"] = [
            "财务指标为截至所列报告期的累计口径，非单季度；ROE不自动年化。",
            "同比变化只比较去年相同报告期，利润同比高增长可能受低基数影响。",
            "估值仅保留与本地日线同日、价格一致的正值PE(TTM)/PB；缺失和亏损PE不作为便宜的证据。",
            "按本地保存的公告日期过滤，不保证恢复后来追溯修订前的历史版本；不混用更早期现金流补齐最新报告。",
        ]
        return result
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
        return result
