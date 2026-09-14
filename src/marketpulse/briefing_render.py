"""Precise, local typography and chart excerpts for a shareable long PNG."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from .fonts import font_path, emoji_font

W = 1200
INK, MUTED, TEAL, PAPER = "#172C35", "#607178", "#216E66", "#F3F1E9"
AMBER, RED = "#9D672A", "#A8453F"
REG, BOLD = font_path(), font_path(True)


class Canvas:
    def __init__(self, width=W, height=25000):
        self.im = Image.new("RGB", (width, height), PAPER)
        self.d = ImageDraw.Draw(self.im)
        self.fonts = {}
        self.section_count = 0

    def font(self, size, bold=False):
        return self.fonts.setdefault((size, bold), ImageFont.truetype(BOLD if bold else REG, size))

    def txt(self, x, y, text, size=34, color=INK, bold=False):
        # Keep member names visibly distinct from the editor's interpretation.
        if "【" in str(text):
            for part in re.split(r"(【[^】]+】)", str(text)):
                if not part:
                    continue
                member = part.startswith("【") and part.endswith("】")
                member_color = "#B4E0D2" if color in ("#FFFFFF", "#E0E7E3") else TEAL
                self._txt(x, y, part, size, member_color if member else color, member or bold)
                x += self.d.textlength(part, font=self.font(size, member or bold))
            return
        self._txt(x, y, text, size, color, bold)

    def _txt(self, x, y, text, size=34, color=INK, bold=False):
        # Render emoji nicknames from the system's color glyphs, preserving names.
        chunks = re.split(r"([\U0001F300-\U0001FAFF])", str(text))
        for chunk in chunks:
            if not chunk:
                continue
            if len(chunk) == 1 and ord(chunk) >= 0x1F300 and emoji_font():
                ef = ImageFont.truetype(emoji_font(), 64)
                glyph = Image.new("RGBA", (100, 100))
                ImageDraw.Draw(glyph).text((0, 0), chunk, font=ef, embedded_color=True)
                if glyph.getbbox():
                    glyph = glyph.crop(glyph.getbbox())
                    glyph.thumbnail((size, size), Image.Resampling.LANCZOS)
                    self.im.paste(glyph, (round(x), round(y + 3)), glyph)
            else:
                self.d.text((x, y), chunk, font=self.font(size, bold), fill=color)
            x += self.d.textlength(chunk, font=self.font(size, bold))

    def lines(self, text, width, size=34, bold=False):
        lines, line = [], ""
        tokens = []
        for c in re.findall(
            r"【[^】]+】[，。！？；：、）]*|[A-Za-z0-9][A-Za-z0-9.:/%+-]*[，。！？；：、）]*|[^\n][，。！？；：、）]*|\n",
            str(text),
        ):
            if len(c) > 1 and self.d.textlength(c, font=self.font(size, True)) > width:
                # Extremely long nicknames must still fit within the card.
                tokens.extend(c)
            else:
                tokens.append(c)
        for c in tokens:
            if c == "\n" or self.d.textlength(line + c, font=self.font(size, bold)) > width:
                if line:
                    lines.append(line)
                line = "" if c == "\n" else c
            else:
                line += c
        if line:
            lines.append(line)
        return lines

    def para(self, x, y, text, width=1020, size=34, color=INK, bold=False):
        for line in self.lines(text, width, size, bold):
            self.txt(x, y, line, size, color, bold)
            y += round(size * 1.48)
        return y

    def ph(self, text, width=1020, size=34, bold=False):
        return len(self.lines(text, width, size, bold)) * round(size * 1.48)

    def card(self, y, h, fill="white"):
        self.d.rounded_rectangle((44, y, W - 44, y + h), radius=24, fill=fill)

    def section(self, y, num, title, subtitle=""):
        self.section_count += 1
        self.txt(64, y, f"{self.section_count:02}", 30, TEAL, True)
        self.txt(132, y - 4, title, 42, INK, True)
        y += 62
        if subtitle:
            y = self.para(64, y, subtitle, 1072, 28, MUTED)
        return y + 20


def display_names(result):
    # Remove phone numbers from display names while preserving local source data.
    mapping = {}
    for member in result["members"]:
        name = member["name"]
        name = re.sub(r"\d{7,}", "", name).strip() or member["alias"]
        group = member.get("group_name", "")
        mapping[member["alias"]] = (group + " / " if len(result.get("groups", [])) > 1 and group else "") + name
    groups = {g["alias"]: g["name"] for g in result.get("groups", [])}

    def replace(s):
        s = re.sub(
            r"(?:【)?(成员\d+)(?:】)?", lambda m: "【" + mapping.get(m.group(1), m.group(1)).strip("【】") + "】", s
        )
        return re.sub(r"群\d+", lambda m: groups.get(m.group(), m.group()), s)

    return replace


def chart_excerpt(path, period, size=(390, 430)):
    im = Image.open(path).convert("RGB")
    # This is a literal crop of the supplied chart, never a redrawn price series.
    if im.height > im.width * 1.8:
        bounds = (0, round(im.height * 0.25), im.width, round(im.height * 0.74))
    elif im.height > im.width * 1.25:
        bounds = (0, round(im.height * 0.32), im.width, round(im.height * 0.80))
    else:
        bounds = (0, 0, im.width, im.height)
    im = im.crop(bounds)
    im.thumbnail(size, Image.Resampling.LANCZOS)
    return im


def social_section(c, y, number, title, subtitle, items, result, replace, fill):
    y = c.section(y, f"{number:02}", title, subtitle)
    media = {m["id"]: m for m in result["media"]}
    for item in items:
        title, summary = replace(item["title"]), replace(item["summary"])
        pictures = [media[iid] for iid in item["image_ids"] if media.get(iid, {}).get("image")][:2]
        labels = []
        col_w = (1024 - 20 * (len(pictures) - 1)) // max(1, len(pictures))
        for m in pictures:
            name = re.sub(r"\d{7,}", "", m["sender"]).strip().strip("【】") or "群成员"
            labels.append(
                "【"
                + name
                + "】 · "
                + m["sent_at"][11:16]
                + (" · 仅缩略图" if m["image"]["thumbnail"] else " · 群内配图")
            )
        image_h = 360 + 22 + max((c.ph(label, col_w, 25) for label in labels), default=0) + 24 if pictures else 0
        h = 28 + c.ph(title, 1024, 36, True) + 20 + c.ph(summary, 1024, 33) + 24 + image_h + 50
        c.card(y, h, fill)
        z = c.para(84, y + 28, title, 1024, 36, INK, True) + 20
        z = c.para(84, z, summary, 1024, 33) + 24
        for i, (m, label) in enumerate(zip(pictures, labels)):
            x = 84 + i * (col_w + 20)
            c.d.rounded_rectangle((x, z, x + col_w, z + 360), radius=12, fill="white")
            im = Image.open(m["image"]["path"]).convert("RGB")
            # Show complete life photos; a chart-specific crop would lose context.
            im.thumbnail((col_w - 24, 336), Image.Resampling.LANCZOS)
            c.im.paste(im, (x + (col_w - im.width) // 2, z + (360 - im.height) // 2))
            c.para(x, z + 382, label, col_w, 25, MUTED)
        times = sorted(
            {
                result["sources"][sid]["time"][:5] if sid in result["sources"] else media[sid]["sent_at"][11:16]
                for sid in item["sources"]
                if sid in result["sources"] or sid in media
            }
        )
        c.txt(84, y + h - 37, "群聊依据  " + " / ".join(times[:6]), 22, MUTED)
        y += h + 20
    if not items:
        y = c.para(80, y, "本时段暂无足够清晰、可综合的相关内容。", 1035, 31, MUTED) + 24
    return y + 18


def research_card(c, y, item):
    """Keep retrieved facts visibly separate from the adjacent chat opinions."""
    tech, news = item["technical"], item["news"]
    rows = [("日线补查", tech["summary"])]
    if tech.get("source"):
        rows.append(
            (
                "行情口径",
                f"{tech.get('instrument', item['lookup_name'])} · {tech['adjustment']} · "
                f"价格 {tech['price_unit']} / 成交量 {tech['volume_unit']}；来源 {tech['source']}；"
                f"查询于 {tech['fetched_at'][:16].replace('T', ' ')}（北京时间）。",
            )
        )
    for hit in news["items"]:
        level = {"full_text": "已取全文", "partial_text": "部分全文", "search_excerpt": "仅搜索摘要"}[hit["level"]]
        source_label = {"announcement": "公告检索", "web": "网页检索"}.get(hit["source"], hit["source"])
        rows.append(
            (
                "近期消息",
                f"{hit['date']} · {hit['title']}（{level}，{source_label}）"
                + ("；" + hit["timing_note"] if hit.get("timing_note") else ""),
            )
        )
    if not news["items"]:
        rows.append(("消息面补查", news["summary"]))
    rows.extend(
        [
            ("对群观点的影响", item.get("interpretation", "补查解读未完成，以上仅为取得的资料。")),
            ("观察条件", item.get("watch", "等待资料核对后再评估。")),
        ]
    )
    title = item["name"] + " · 外部资料与分析"
    height = 80 + c.ph(title, 1024, 35, True) + sum(44 + c.ph(text, 1024, 30) + 22 for _, text in rows)
    c.card(y, height, "#EAF0F2")
    z = c.para(84, y + 26, title, 1024, 35, TEAL, True) + 26
    for label, text in rows:
        c.txt(84, z, label, 25, TEAL, True)
        z = c.para(84, z + 44, text, 1024, 30, INK) + 22
    c.txt(84, y + height - 29, "外部依据 " + item["id"] + " · 来源链接及检索记录见附录", 21, MUTED)
    return y + height + 24


def focus_section(c, y, result):
    entries = result.get("focus_members", [])
    if not entries:
        return y
    y = c.section(y, "", "重点关注 · 指定成员", "按本人发言整理；被关注不代表观点更可靠，也不作为跟单依据。")
    by_identity = {(m.get("group_id"), m.get("sender_id")): m["name"] for m in result["members"]}
    replace = display_names(result)
    for member in entries:
        name = by_identity.get((member["group_id"], member["sender_id"]), member["name"])
        name = re.sub(r"\d{7,}", "", name).strip().strip("【】") or "关注成员"
        title = "【" + name + "】"
        meta = member["group_name"] + f" · 本时段 {member['message_count']} 条记录 / {member['image_count']} 张图片"
        if member.get("last_at"):
            meta += " · 最近 " + member["last_at"][11:16]
        rows = []
        content = member.get("content")
        if content:
            rows.append(("主要观点", content["overview"]["text"]))
            for key, label in (("positions", "关注标的与理由"), ("changes", "观点变化"), ("watch", "本人观察条件")):
                for point in content[key]:
                    rows.append((label + " · " + point["title"], point["text"]))
            if not content["changes"]:
                rows.append(("观点变化", "本时段没有足够前后证据确认观点发生变化。"))
            for text in content["limitations"]:
                rows.append(("资料边界", text))
        else:
            states = {
                "no_messages": "本时段本机未同步到该成员发言；不代表本人没有发言、空仓或改变观点。",
                "no_readable_content": "本时段有记录，但没有可分析正文或独立图片证据；不据此推断观点。",
                "unavailable": "本次重点成员分析暂未完成，不能据此判断立场；原始发言仍保留在来源附录。",
            }
            rows.append(("本时段状态", states.get(member["status"], "暂无可用分析。")))
        title_h, meta_h = c.ph(title, 1024, 40, True), c.ph(meta, 1024, 25)
        height = (
            95
            + title_h
            + meta_h
            + sum(c.ph(label, 1024, 27, True) + 12 + c.ph(replace(text), 1024, 32) + 24 for label, text in rows)
        )
        c.card(y, height, "#F3ECDD")
        z = c.para(84, y + 28, title, 1024, 40, TEAL, True) + 14
        z = c.para(84, z, meta, 1024, 25, MUTED) + 28
        for label, text in rows:
            z = c.para(84, z, label, 1024, 27, TEAL, True) + 12
            z = c.para(84, z, replace(text), 1024, 32) + 24
        y += height + 24
    return y + 18


def render(result, output):
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    c, r = (
        Canvas(
            height=25000
            + 2500 * min(12, len(result.get("market_research", {}).get("cards", [])))
            + 3000 * min(12, len(result.get("focus_members", [])))
        ),
        result["content"],
    )
    replace = display_names(result)
    visuals = {v["image_id"]: v for v in result["visuals"]}
    media = {m["id"]: m for m in result["media"]}
    research = result.get("market_research", {})
    research_items = research.get("cards", [])
    asof = result["as_of"][11:16]
    hour = int(asof[:2])
    edition = (
        "盘前观察"
        if hour < 9
        else "早盘观察"
        if hour < 11
        else "午间观察"
        if hour < 13
        else "午后观察"
        if hour < 15
        else "收盘观察"
    )
    header_height = max(
        592, 175 + c.ph(r["headline"], 1056, 62, True) + 28 + c.ph(replace(r["thesis"]), 1056, 36) + 112
    )
    c.d.rectangle((0, 0, W, header_height), fill=INK)
    label = result["group_name"][:30] + "  /  群聊综合观察"
    c.txt(64, 44, label, 29, "#B4CEC5", True)
    c.txt(64, 104, f"{result['date']}  ·  {edition}  ·  截至 {asof}", 29, "#D8E2DC")
    y = c.para(64, 175, r["headline"], 1056, 62, "#FFFFFF", True)
    y = c.para(64, y + 28, replace(r["thesis"]), 1056, 36, "#E0E7E3")
    c.d.rounded_rectangle((64, y + 22, 1136, y + 85), radius=15, fill="#2B444C")
    c.txt(86, y + 35, "群内情绪  ·  " + r["mood"], 30, "#D5E9DA", True)
    header_end = y + 112
    y = header_height + 40
    if result.get("source_notice"):
        notice = result["source_notice"]
        height = 100 + c.ph(notice, 1024, 30)
        c.card(y, height, "#F7E7D5")
        c.txt(84, y + 22, "数据同步提示", 30, AMBER, True)
        c.para(84, y + 72, notice, 1024, 30, INK)
        y += height + 30
    y = c.section(y, "01", "情绪怎样走到这里")
    for i, item in enumerate(r["timeline"]):
        text = replace(item["text"])
        h = 108 + c.ph(text, 988, 34)
        c.card(y, h)
        c.d.ellipse((74, y + 31, 92, y + 49), fill=[TEAL, AMBER, RED][min(i, 2)])
        c.txt(113, y + 23, item["title"], 34, INK, True)
        c.para(88, y + 80, text, 1024, 34)
        y += h + 16
    y += 26
    if r.get("group_comparison"):
        y = c.section(y, "", "多群对照 · 共识与分歧", "群聊热度不代表独立证据；相同成员在不同群保留各自群昵称。")
        for item in r["group_comparison"]:
            text, title = replace(item["text"]), replace(item["title"])
            h = 60 + c.ph(title, 1024, 35, True) + c.ph(text, 1024, 33) + 30
            c.card(y, h)
            z = c.para(84, y + 24, title, 1024, 35, TEAL, True) + 18
            c.para(84, z, text, 1024, 33)
            y += h + 18
    y = focus_section(c, y, result)
    y = c.section(y, "02", "主要讨论线索")
    for n, item in enumerate(r["themes"], 1):
        c.txt(66, y, f"0{n}", 28, TEAL, True)
        c.txt(132, y - 2, item["title"], 36, TEAL, True)
        y = c.para(132, y + 54, replace(item["text"]), 990, 35) + 32
    y += 16
    y = c.section(
        y,
        "03",
        "重点标的 · 群观点与依据",
        "截图只代表图中时点；外部补查单独标注数据日期与来源。"
        if research_items
        else "截图只代表图中时点；观察条件用于检验群观点。",
    )
    for n, stock in enumerate(r["stocks"], 1):
        discussion, chart, disagreement, watch = (
            replace(stock[k]) for k in ("discussion", "chart", "disagreement", "watch")
        )
        iid = next((x for x in stock["image_ids"] if media[x].get("image")), None)
        discussion_h = c.ph(discussion, 1024, 35)
        right_h = 47 + c.ph(chart, 590, 32) + 28 + 47 + c.ph(disagreement, 590, 32)
        picture_h = 510 if iid else 0
        middle_h = max(right_h, picture_h)
        height = 149 + discussion_h + 30 + middle_h + 40 + c.ph("验证条件  " + watch, 1024, 33) + 54
        c.card(y, height)
        c.txt(84, y + 28, stock["name"], 46, INK, True)
        namew = c.d.textlength(stock["name"], font=c.font(46, True))
        c.txt(104 + namew, y + 43, ("图示 " if iid and stock["code"] else "") + stock["code"], 27, MUTED)
        c.txt(86, y + 96, stock["angle"], 33, TEAL, True)
        z = c.para(86, y + 149, discussion, 1024, 35) + 30
        if iid:
            m, v = media[iid], visuals[iid]
            c.d.rounded_rectangle((82, z, 500, z + 500), radius=14, fill="#F2F4F2")
            excerpt = chart_excerpt(m["image"]["path"], v["period"])
            c.im.paste(excerpt, (96 + (390 - excerpt.width) // 2, z + 12))
            label = (v["period"] or "群内截图") + " · " + m["sent_at"][11:16] + " 发布"
            c.para(96, z + 444, label, 390, 24, MUTED)
        else:
            c.d.rounded_rectangle((82, z, 500, z + 180), radius=14, fill="#F2F4F2")
            c.para(112, z + 33, "本股暂无可用清晰图\n以下依据文字讨论", 350, 30, MUTED)
        rz = z
        c.txt(530, rz, "图上怎么看", 28, TEAL, True)
        rz = c.para(530, rz + 47, chart, 590, 32)
        c.txt(530, rz + 28, "分歧与证据缺口", 28, AMBER, True)
        c.para(530, rz + 75, disagreement, 590, 32)
        z += middle_h + 28
        c.d.line((86, z, 1114, z), fill="#E4E8E3", width=2)
        c.para(86, z + 22, "验证条件  " + watch, 1024, 33)
        times = sorted(
            {
                result["sources"][sid]["time"][:5] if sid in result["sources"] else media[sid]["sent_at"][11:16]
                for sid in stock["sources"]
                if sid in result["sources"] or sid in media
            }
        )
        c.txt(86, y + height - 36, "群聊依据  " + " / ".join(times[:6]), 22, MUTED)
        y += height + 24
        for item in research_items:
            if item.get("stock_name") == stock["name"]:
                y = research_card(c, y, item)
    y += 18
    y = c.section(y, "04", "其他个股与市场线索")
    for item in r["other_mentions"]:
        c.txt(80, y, item["title"], 33, INK, True)
        y = c.para(80, y + 53, replace(item["text"]), 1035, 33, MUTED) + 28
    for item in research_items:
        if not item.get("stock_name"):
            y = research_card(c, y, item)
    # Surface the latest clear supplementary chart actually cited by the brief.
    extra_ids = {sid for item in r["other_mentions"] for sid in item["sources"]}
    used_ids = {iid for stock in r["stocks"] for iid in stock["image_ids"]}
    extras = [
        m
        for iid, m in media.items()
        if iid in extra_ids - used_ids
        and m.get("image")
        and not m["image"]["thumbnail"]
        and visuals[iid]["kind"] == "chart"
        and len(visuals[iid]["names"]) == 1
        and visuals[iid]["readable"] == "clear"
    ]
    if extras:
        m = max(extras, key=lambda m: m["sent_at"])
        v = visuals[m["id"]]
        observations = "；".join(v["observations"][:2])
        caveats = "；".join(v["caveats"])
        h = 150 + max(510, 47 + c.ph(observations, 590, 31) + 65 + c.ph(caveats, 590, 29))
        c.card(y, h)
        c.txt(84, y + 28, v["names"][0] + " · 补充图证", 37, INK, True)
        z = y + 104
        excerpt = chart_excerpt(m["image"]["path"], v["period"])
        c.im.paste(excerpt, (96 + (390 - excerpt.width) // 2, z))
        c.para(96, z + 448, m["sent_at"][11:16] + " 发布 · 图表局部", 390, 25, MUTED)
        c.txt(530, z, "图中读数 · 未核验现价", 27, TEAL, True)
        z = c.para(530, z + 47, observations, 590, 31)
        c.txt(530, z + 22, "解读边界", 27, AMBER, True)
        c.para(530, z + 65, caveats, 590, 29, MUTED)
        y += h + 28
    y += 16
    y = c.section(y, "05", "下一时段 · 等什么来验证")
    for n, item in enumerate(r["next_watch"], 1):
        h = 110 + c.ph(replace(item["text"]), 1024, 35)
        c.card(y, h, "#E5ECE5")
        c.txt(84, y + 26, f"{n}. " + item["title"], 35, TEAL, True)
        c.para(84, y + 84, replace(item["text"]), 1024, 35)
        y += h + 18
    y += 28
    section_number = 6
    for key, title, subtitle, fill in [
        ("life", "生活区 · 今天也聊了这些", "餐食、出行与日常分享，结合照片和上下文整理。", "#F6ECDB"),
        ("off_topic", "其他话题 · 股市之外的讨论", "按话题合并观点与回应，转发消息保留来源边界。", "#EBEAF2"),
    ]:
        if key in r:
            y = social_section(c, y, section_number, title, subtitle, r[key], result, replace, fill)
            section_number += 1
    if r.get("investment_advice"):
        y = c.section(
            y,
            f"{section_number:02}",
            "投资建议 · 基于群聊的分析判断",
            "以下是分析者的条件性建议；正文群友观点以【昵称】注明来源。",
        )
        for item in r["investment_advice"]:
            title = replace(item["title"])
            rows = [
                ("判断依据", replace(item["basis"]), MUTED),
                ("建议做法", replace(item["action"]), INK),
                ("主要风险", replace(item["risk"]), AMBER),
            ]
            h = 58 + c.ph(title, 1020, 37, True) + sum(42 + c.ph(text, 1020, 33) + 22 for _, text, _ in rows)
            c.card(y, h, "#E6EEE9")
            z = c.para(84, y + 28, title, 1020, 37, TEAL, True) + 24
            for label, text, color in rows:
                c.txt(84, z, label, 26, TEAL, True)
                z = c.para(84, z + 42, text, 1020, 33, color) + 22
            y += h + 20
        y += 12
    cov = result["coverage"]
    c.d.line((64, y, 1136, y), fill="#CCD4CC", width=2)
    y += 28
    y = c.para(
        64,
        y,
        f"覆盖至 {result['date']} {asof}（北京时间）\n{cov['messages']} 条群记录 · {cov['images']} 张图片（{cov['full_images']} 张清晰附件 / {cov['thumbnail_images']} 张仅缩略图）",
        1072,
        27,
        MUTED,
    )
    if cov.get("recirculated_reports_excluded"):
        y = c.para(
            64,
            y + 12,
            f"其中 {cov['recirculated_reports_excluded']} 张为本项目简报回流图，未作为新增独立分析依据。",
            1072,
            26,
            MUTED,
        )
    for limitation in r["limitations"]:
        if research_items:
            limitation = limitation.replace("未联网核验", "群聊归纳部分未自行核验，外部补查另列")
        if research_items and any(word in limitation for word in ("未进行外部", "未外部核验")):
            limitation = "群聊归纳保留原观点和分歧；外部补查仅覆盖单独列出的标的、日期与来源。"
        y = c.para(64, y + 12, replace(limitation), 1072, 26, MUTED)
    if research.get("status") not in (None, "disabled"):
        note = (
            "外部补查："
            + research.get("checked_at", "")[:16].replace("T", " ")
            + "（北京时间）。群聊观点与外部数据分开呈现；仅补查列出的标的和资料，不代表全部说法已核验。"
        )
        if research.get("status") == "partial":
            note += "部分资料或解读未完成，以卡片缺失标记为准。"
        if research.get("error"):
            note += research["error"]
        y = c.para(64, y + 12, note, 1072, 26, MUTED)
    method = "本地规则脚本" if result.get("analysis_method", "").startswith("rules") else "AI"
    disclaimer = f"免责声明：本报告由{method}根据群聊及可读取资料生成，可能误读或遗漏；群友发言、传闻及截图未逐项核验，外部补查范围以标注为准。投资建议仅为一般性研究参考，未考虑你的资金、持仓、期限及风险承受能力，不构成个性化投资顾问服务或收益承诺。请独立核验并自主决策，投资有风险。"
    y = c.para(64, y + 22, disclaimer, 1072, 26, MUTED)
    if result.get("advice_references"):
        y = c.para(
            64, y + 18, "核验原则参考：证监会《高度警惕所谓股市“杀猪盘”风险》；链接见原文依据附录。", 1072, 23, MUTED
        )
    y = c.para(64, y + 22, "图文综合简报  /  WeChat Market Pulse", 1072, 24, TEAL, True) + 42
    if y >= c.im.height:
        raise ValueError("简报超出版面高度上限")
    dest = out / f"群聊综合简报-{result['date']}.png"
    temp_image = dest.with_suffix(".png.tmp")
    c.im.crop((0, 0, W, y)).save(temp_image, format="PNG", optimize=True)
    temp_image.chmod(0o600)
    temp_image.replace(dest)
    # A separate local appendix preserves citations without cluttering the image.
    audit = [
        "<!doctype html><meta charset='utf-8'><title>简报依据</title><style>body{font:18px/1.7 system-ui;max-width:1000px;margin:40px auto}img{max-width:600px}p{white-space:pre-wrap}</style>"
    ]
    for sid, item in result["sources"].items():
        audit.append(
            f"<p id='{sid}'><b>{sid} {html.escape(item.get('group_name', ''))} {html.escape(item['time'])} {html.escape(item['sender'])}</b><br>{html.escape(item['text'])}</p>"
        )
    for iid, v in visuals.items():
        audit.append(f"<p id='{iid}'><b>{iid}</b><br>{html.escape(json.dumps(v, ensure_ascii=False))}</p>")
    for member in result.get("focus_members", []):
        audit.append("<h2>重点关注：" + html.escape(member["group_name"] + " / " + member["name"]) + "</h2>")
        if member.get("content"):
            content = member["content"]
            for point in [content["overview"], *content["positions"], *content["changes"], *content["watch"]]:
                links = " / ".join(
                    f"<a href='#{html.escape(sid, quote=True)}'>{html.escape(sid)}</a>" for sid in point["sources"]
                )
                audit.append(
                    "<p><b>"
                    + html.escape(point["title"])
                    + "</b><br>"
                    + html.escape(point["text"])
                    + "<br>"
                    + links
                    + "</p>"
                )
    for item in research_items:
        audit.append(f"<h2 id='{item['id']}'>{html.escape(item['name'])} · 外部补查</h2>")
        audit.append("<p>触发依据：" + html.escape(item["source"] + " · " + item["quote"]) + "</p>")
        tech = item["technical"]
        if tech.get("raw"):
            from .research import safe_url

            source = html.escape(tech["source"])
            url = safe_url(tech.get("url", ""))
            if url:
                source = f"<a href='{html.escape(url, quote=True)}'>{source}</a>"
            audit.append(
                f"<p id='{item['id']}D'>{source} · 查询于 {html.escape(tech['fetched_at'])}<br>"
                + html.escape(tech.get("query", ""))
                + "</p><pre>"
                + html.escape(tech["raw"])
                + "</pre>"
            )
        for hit in item["news"]["items"]:
            title = html.escape(hit["title"])
            if hit.get("url"):
                from .research import safe_url

                url = safe_url(hit["url"])
                if url:
                    title = f"<a href='{html.escape(url, quote=True)}'>{title}</a>"
            audit.append(
                f"<p id='{hit.get('id', item['id'])}'>{html.escape(hit['date'])} · {title}<br>"
                + html.escape(hit["source"] + " · " + hit["level"])
                + "<br>"
                + html.escape(hit.get("full_text", hit["snippet"]))
                + "</p>"
            )
            if hit.get("doc_id"):
                audit.append("<p>原始公告检索句柄（无公开链接时保留来源定位）：" + html.escape(hit["doc_id"]) + "</p>")
    for ref in result.get("advice_references", []):
        audit.append(
            f"<p><a href='{html.escape(ref['url'], quote=True)}'>{html.escape(ref['title'])}</a><br>{html.escape(ref['scope'])}</p>"
        )
    (out / "briefing-sources.html").write_text("\n".join(audit), encoding="utf-8")
    page = f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'>
    <meta http-equiv='Content-Security-Policy' content="default-src 'none'; img-src 'self' file:; style-src 'unsafe-inline'">
    <title>微信群 · 图文综合简报 · {result["date"]}</title>
    <style>body{{margin:0;background:#d9ded9;font:16px system-ui;color:#172c35}}nav{{padding:18px;text-align:center}}a{{color:inherit;margin:0 12px}}main{{max-width:900px;margin:auto}}img{{display:block;width:100%;height:auto}}</style>
    <nav><a href='{html.escape(dest.name)}' download>保存分享长图</a><a href='briefing-sources.html'>查看原文依据</a></nav>
    <main><img src='{html.escape(dest.name)}' alt='{html.escape(r["headline"])}'></main></html>"""
    for filename in ("briefing.html", "all.html"):
        (out / filename).write_text(page, encoding="utf-8")
    return dest
