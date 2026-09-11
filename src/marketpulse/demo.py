"""Small, entirely fictional multi-group demo, no network/model/WeChat access."""

import json
import sys
from pathlib import Path


def records():
    messages = [
        ("study", "研究示例群", "alice", "小林", "09:35:00", "宁德时代今天反弹，先核对公告和成交量。"),
        ("study", "研究示例群", "bob", "阿青", "10:12:00", "半导体虽然上涨，我还是担心指数走弱。"),
        ("study", "研究示例群", "alice", "小林", "12:10:00", "午饭吃牛肉面，这家还不错。"),
        ("study", "研究示例群", "bob", "阿青", "14:32:00", "比亚迪不是只看销量，还得核对利润。"),
        ("friends", "交流示例群", "alice", "林同学（观察中）", "09:40:00", "宁德时代反弹以后是否追高，我想再观察。"),
        ("friends", "交流示例群", "carol", "小岚", "13:08:00", "黄金和汇率的讨论很多，消息日期要核对。"),
        ("friends", "交流示例群", "carol", "小岚", "15:12:00", "本地模型需要多大显卡？我想用脚本先整理聊天。"),
        ("friends", "交流示例群", "alice", "林同学（观察中）", "18:20:00", "今天晚饭做了披萨，旅游照片下次发。"),
    ]
    return [
        dict(
            id=f"demo-{i}",
            group_id=g,
            group_name=gn,
            sender_id=u,
            sender=n,
            sent_at="2026-01-05T" + t + "+08:00",
            message_kind="text",
            text=text,
        )
        for i, (g, gn, u, n, t, text) in enumerate(messages, 1)
    ]


def run_demo(out):
    from .briefing import generate
    from .briefing_render import render

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    source = out / "messages.jsonl"
    source.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records()), encoding="utf-8")
    cfg = dict(provider="rules", input=str(source.resolve()), period="all", local_only=True)
    result, destination = generate(
        "2026-01-05", [], cfg, out, work_dir=out / "work", progress=lambda *a, **k: print(*a, file=sys.stderr, **k)
    )
    path = render(result, destination)
    return dict(
        image=str(path),
        html=str(destination / "briefing.html"),
        method="rules",
        fictional=True,
        groups=len(result["groups"]),
    )
