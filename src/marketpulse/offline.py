"""Deterministic fallback: observable wording and topics, never invented insights."""

from collections import Counter, defaultdict
import re

from .core import period_at

POSITIVE = ("看好", "看多", "反弹", "上涨", "涨停", "盈利", "突破", "走强")
NEGATIVE = ("看空", "下跌", "跌停", "亏损", "套牢", "恐慌", "走弱", "减仓")
MARKET = ("股票", "股价", "大盘", "沪指", "指数", "仓位", "主力", "资金", "行情", "利率", "黄金")
SECTORS = {
    "科技与算力": ("算力", "半导体", "芯片", "光模块", "光纤"),
    "消费与制造": ("汽车", "电池", "新能源", "白酒", "消费股"),
    "宏观与市场": ("加息", "降息", "CPI", "美股", "黄金", "汇率", "大盘"),
}
LIFE = {
    "餐食与日常": ("午饭", "晚饭", "牛肉面", "甜品", "披萨", "吃饭", "好吃"),
    "出行与消费": ("旅游", "加油", "排队", "外卖", "手机", "旅行"),
}
OTHER = {
    "科技与工具": ("模型", "AI", "GPT", "服务器", "显卡", "编程"),
    "其他交流": ("游戏", "论文", "医疗", "控烟", "电影", "读书"),
}
DEFAULT_STOCKS = ("宁德时代", "比亚迪", "中际旭创", "长飞光纤", "生益科技", "贵州茅台", "三环集团")


def wording(text):
    positive = sum(word in text and "不" + word not in text and "别" + word not in text for word in POSITIVE)
    negative = sum(word in text for word in NEGATIVE) + sum("不" + word in text for word in POSITIVE)
    return positive, negative


def excerpts(rows, limit=3):
    return "；".join(f"{r['speaker']}：{r['text'][:55].replace(chr(10), ' ')}" for r in rows[:limit])


def analyze(transcript, groups, cfg):
    stock_words = tuple(cfg.get("stock_keywords", DEFAULT_STOCKS))
    market, stocks = [], defaultdict(list)
    for row in transcript:
        names = set(word for word in stock_words if word and word in row["text"])
        names.update(re.findall(r"(?<!\d)(?:00|30|60|68)\d{4}(?!\d)", row["text"]))
        if names or any(w in row["text"] for w in (*MARKET, *POSITIVE, *NEGATIVE)):
            market.append(row)
        for name in names:
            stocks[name].append(row)
    pos = sum(wording(r["text"])[0] > 0 for r in market)
    neg = sum(wording(r["text"])[1] > 0 for r in market)
    mood = (
        "乐观与谨慎措辞并存"
        if pos and neg
        else "乐观措辞较多"
        if pos
        else "谨慎措辞较多"
        if neg
        else "缺少明确方向措辞"
    )
    themes = []
    for title, words in SECTORS.items():
        rows = [r for r in market if any(w in r["text"] for w in words)]
        if rows:
            themes.append(
                dict(
                    title=title,
                    text=f"命中相关话题 {len(rows)} 条。代表原文：" + excerpts(rows, 2),
                    sources=[r["id"] for r in rows[:2]],
                )
            )
    if not themes and market:
        themes.append(dict(title="市场讨论摘录", text=excerpts(market), sources=[r["id"] for r in market[:3]]))
    periods = defaultdict(list)
    for row in market:
        periods[period_at("2000-01-01T" + row["time"] + "+08:00")].append(row)
    titles = {"premarket": "盘前", "morning": "早盘", "lunch": "午间", "afternoon": "下午盘", "afterhours": "盘后"}
    timeline = []
    for period, rows in periods.items():
        p = sum(wording(r["text"])[0] > 0 for r in rows)
        n = sum(wording(r["text"])[1] > 0 for r in rows)
        timeline.append(
            dict(
                title=titles[period],
                text=f"市场相关记录 {len(rows)} 条，乐观词命中 {p} 条、谨慎词命中 {n} 条；同条可同时命中，仅统计措辞。",
                sources=[rows[0]["id"]],
            )
        )
    cards = []
    for name, rows in sorted(stocks.items(), key=lambda item: -len(item[1]))[:6]:
        cards.append(
            dict(
                name=name,
                code="",
                angle=f"原文提及 {len(rows)} 次 · 规则识别",
                discussion=excerpts(rows),
                chart="规则模式不读取 K 线走势，也不确认数字是否为证券代码。",
                disagreement="关键词不能判定转述、反讽或真实交易；以上为原文线索。",
                watch="核对公司名称、公告与行情，再判断讨论是否有事实支持。",
                image_ids=[],
                sources=[r["id"] for r in rows[:3]],
            )
        )
    market_ids = {r["id"] for r in market}

    def social(categories):
        items = []
        for title, words in categories.items():
            rows = [r for r in transcript if r["id"] not in market_ids and any(w in r["text"] for w in words)]
            if rows:
                items.append(
                    dict(
                        title=title,
                        summary=f"相关文字 {len(rows)} 条。" + excerpts(rows, 2),
                        image_ids=[],
                        sources=[r["id"] for r in rows[:2]],
                    )
                )
        return items

    comparison = []
    if len(groups) > 1:
        for group in groups:
            rows = [r for r in transcript if r.get("group") == group["alias"]]
            if rows:
                topics = Counter(
                    name for name, hits in stocks.items() for r in hits if r.get("group") == group["alias"]
                )
                comparison.append(
                    dict(
                        title=group["alias"],
                        text=f"文字/回复 {len(rows)} 条；常见证券线索：{('、'.join(name for name, _ in topics.most_common(4))) or '暂无明确提及'}。不同群的发言量不等于观点可信度。",
                        sources=[rows[0]["id"]],
                    )
                )
        shared = [name for name, rows in stocks.items() if len({r.get("group") for r in rows}) > 1]
        if shared:
            refs = [r["id"] for name in shared[:3] for r in stocks[name]][:8]
            comparison.append(
                dict(
                    title="跨群共同话题",
                    text="多群均提到："
                    + "、".join(shared[:6])
                    + "。重复原文或转发不代表独立证据；规则模式无法核实共识。",
                    sources=refs,
                )
            )
    advice = (
        [
            dict(
                title="先核验讨论线索",
                basis="规则统计只发现文字提及，未核对行情和公司资料。",
                action="将反复提及的股票列为研究线索；结合公告、量价和自己的风险约束复核后再作决策。",
                risk="群热度、晒单和关键词统计都不能证明收益或交易机会。",
                sources=[market[0]["id"]],
            )
        ]
        if market
        else []
    )
    return dict(
        headline="群聊观察 · 本地规则版",
        thesis=f"覆盖 {len(groups)} 个群、{len(transcript)} 条文字与回复。市场关键词相关 {len(market)} 条；整理时段、证券提及与生活话题。",
        mood=mood,
        timeline=timeline,
        themes=themes,
        stocks=cards,
        other_mentions=[],
        next_watch=[],
        group_comparison=comparison,
        life=social(LIFE),
        off_topic=social(OTHER),
        investment_advice=advice,
        limitations=[
            "本报告由本地脚本生成，未调用大语言模型。关键词可能误判语义、反讽和上下文。",
            "股票名称来自可配置词表，六位数字仅为待核对线索；不补全未知代码。图片未作视觉解读。",
        ],
    )
