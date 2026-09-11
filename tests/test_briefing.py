import io
import hashlib
import json
import struct
import unittest
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from PIL import Image

from marketpulse.media import decode_dat, image_from_bytes, packed_hashes, hardlink_hashes
from marketpulse.briefing import (
    validate_brief,
    text_payload,
    analyze_images,
    mark_recirculated_briefs,
    Visual,
    refresh_attributions,
    _generate,
)


class MediaTests(unittest.TestCase):
    def test_v2_three_segments_roundtrip_and_wrong_key(self):
        stream = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(stream, format="PNG")
        plain = stream.getvalue()
        key, xor, size, tail = b"0123456789abcdef", 0x37, 32, 9
        enc = (
            b"\x07\x08V2\x08\x07"
            + struct.pack("<II", size, tail)
            + b"\0"
            + AES.new(key, AES.MODE_ECB).encrypt(pad(plain[:size], 16))
            + plain[size:-tail]
            + bytes(b ^ xor for b in plain[-tail:])
        )
        decoded = decode_dat(enc, key, xor)
        self.assertEqual(decoded, plain)
        self.assertEqual(image_from_bytes(decoded).size, (16, 16))
        with self.assertRaises(ValueError):
            decode_dat(enc, b"fedcba9876543210", xor)

    def test_rejects_overlapping_or_truncated_segments(self):
        bad = b"\x07\x08V2\x08\x07" + struct.pack("<II", 3200, 99) + b"\0" * 30
        with self.assertRaises(ValueError):
            decode_dat(bad, b"0123456789abcdef", 1)

    def test_hashes_are_bounded(self):
        good = b"a123456789abcdef0123456789abcdef"
        self.assertEqual(packed_hashes(b"\x12\x22\x0a\x20" + good), [good.decode()])
        self.assertEqual(packed_hashes(good + b"1"), [])

    def test_image_bytes_must_fully_decode(self):
        with self.assertRaises(Exception):
            image_from_bytes(b"\xff\xd8\xffbad image")


class BriefTests(unittest.TestCase):
    def test_latest_identity_name_applies_to_earlier_messages_and_image_authors(self):
        rows = [
            dict(
                id="a",
                sender_id="u",
                sender="旧名",
                message_kind="text",
                text="原文",
                sent_at="2026-09-11T09:00:00+08:00",
            ),
            dict(
                id="b", sender_id="u", sender="新名", message_kind="image", text="", sent_at="2026-09-11T10:00:00+08:00"
            ),
        ]
        _, sources, people = text_payload(rows)
        self.assertEqual(people["u"]["name"], "新名")
        self.assertEqual(sources["T001"]["sender"], "新名")
        result = dict(
            members=[dict(alias="成员1", name="旧名")],
            sources=sources,
            media=[dict(id="I01", message_id="b", sender="旧名")],
            content={"thesis": "成员1 提及旧名"},
        )
        changes = refresh_attributions(result, rows)
        self.assertEqual(len(changes), 1)
        self.assertEqual(result["media"][0]["sender"], "新名")
        self.assertEqual(result["members"][0]["sender_id"], "u")
        self.assertEqual(result["content"]["thesis"], "成员1 提及旧名")
        result["sources"]["T001"]["message_id"] = "wrong"
        with self.assertRaisesRegex(ValueError, "来源绑定"):
            refresh_attributions(result, rows)

    def test_name_only_cache_refresh_avoids_model_and_preserves_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                dict(
                    id="a",
                    sender_id="u",
                    participant_id="u",
                    sender="旧名",
                    message_kind="text",
                    text="原文",
                    group_id="g",
                    group_name="测试群",
                    sent_at="2026-09-11T09:00:00+08:00",
                )
            ]
            groups = [dict(id="g", alias="群1", name="测试群", messages=1)]
            content = dict(
                headline="测试",
                thesis="成员1 发言",
                mood="观望",
                timeline=[],
                themes=[],
                stocks=[],
                other_mentions=[],
                next_watch=[],
                group_comparison=[],
                life=[],
                off_topic=[],
                investment_advice=[],
                limitations=[],
            )
            cfg = dict(provider="codex", model=None)
            with (
                patch("marketpulse.inputs.prepare", return_value=(rows, [], groups)),
                patch("marketpulse.briefing.call_model", return_value=content) as model,
            ):
                first, _ = _generate("2026-09-11", "g", cfg, root, False, lambda *a, **k: None, root)
                self.assertEqual(model.call_count, 1)
                rows[0]["sender"] = "最新群昵称"
                second, _ = _generate("2026-09-11", "g", cfg, root, False, lambda *a, **k: None, root)
                self.assertEqual(model.call_count, 1)
            self.assertEqual(second["content"], first["content"])
            self.assertEqual(second["members"][0]["name"], "最新群昵称")
            self.assertEqual(second["sources"]["T001"]["sender"], "最新群昵称")

    def test_refuses_cross_stock_picture_and_fabricated_source(self):
        body = dict(
            headline="测试",
            thesis="测试",
            mood="观望",
            timeline=[],
            themes=[],
            other_mentions=[],
            next_watch=[],
            group_comparison=[],
            life=[],
            off_topic=[],
            investment_advice=[],
            limitations=[],
        )
        stock = dict(
            name="测试甲",
            code="",
            angle="",
            discussion="",
            chart="",
            disagreement="",
            watch="",
            image_ids=["I01"],
            sources=["T001"],
        )
        with self.assertRaisesRegex(ValueError, "图文标的"):
            validate_brief(
                {**body, "stocks": [stock]}, {"T001": {}}, [{"image_id": "I01", "kind": "chart", "names": ["测试乙"]}]
            )
        with self.assertRaisesRegex(ValueError, "不存在的引用"):
            validate_brief(
                {**body, "stocks": [], "themes": [dict(title="测试", text="测试", sources=["T999"])]}, {"T001": {}}, []
            )
        with self.assertRaisesRegex(ValueError, "不存在的引用"):
            validate_brief(
                {
                    **body,
                    "stocks": [],
                    "investment_advice": [
                        dict(title="观察", basis="群观点", action="核验", risk="失效", sources=["T999"])
                    ],
                },
                {"T001": {}},
                [],
            )

    def test_social_images_need_correct_type_and_topic_source(self):
        body = dict(
            headline="测试",
            thesis="测试",
            mood="观望",
            timeline=[],
            themes=[],
            stocks=[],
            other_mentions=[],
            next_watch=[],
            group_comparison=[],
            life=[],
            off_topic=[],
            investment_advice=[],
            limitations=[],
        )
        topic = dict(title="午饭", summary="成员分享餐食", image_ids=["I01"], sources=["I01"])
        visual = dict(image_id="I01", kind="other", readable="clear")
        self.assertEqual(validate_brief({**body, "life": [topic]}, {}, [visual])["life"][0]["image_ids"], ["I01"])
        with self.assertRaisesRegex(ValueError, "类型不符"):
            validate_brief({**body, "off_topic": [topic]}, {}, [{**visual, "kind": "chart"}])
        with self.assertRaisesRegex(ValueError, "有效来源"):
            validate_brief({**body, "life": [{**topic, "sources": ["T001"]}]}, {"T001": {}}, [visual])

    def test_image_only_senders_get_distinct_aliases_even_with_same_name(self):
        rows = [
            dict(
                id=str(i),
                sender_id=str(i),
                sender="同名",
                message_kind="image",
                text="",
                sent_at="2026-09-11T10:00:00+08:00",
            )
            for i in range(2)
        ]
        transcript, sources, people = text_payload(rows)
        self.assertEqual(transcript, [])
        self.assertNotEqual(people["0"]["alias"], people["1"]["alias"])

    def test_life_images_refresh_old_brief_cache_without_reanalyzing_charts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = []
            for iid, color, kind in [("I01", "red", "chart"), ("I02", "blue", "other")]:
                path = root / (iid + ".png")
                Image.new("RGB", (12, 12), color).save(path)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                prior = Visual(
                    image_id=iid,
                    kind=kind,
                    names=[],
                    codes=[],
                    period="",
                    visible_time="",
                    observations=["旧简述"],
                    caveats=[],
                    readable="clear",
                ).model_dump()
                (root / ("holistic-vision-v1-" + digest[:24] + ".json")).write_text(json.dumps(prior))
                media.append(
                    dict(
                        id=iid,
                        sent_at="2026-09-11T12:00:00+08:00",
                        image=dict(path=str(path), size=[12, 12], thumbnail=False),
                    )
                )
            updated = {**prior, "observations": ["生活主体与可见细节"]}
            with patch("marketpulse.briefing.call_model", return_value={"images": [updated]}) as model:
                result = analyze_images(
                    media,
                    [],
                    root,
                    {"provider": "codex", "reuse_legacy_codex_cache": True},
                    progress=lambda *a, **k: None,
                )
                again = analyze_images(
                    media,
                    [],
                    root,
                    {"provider": "codex", "reuse_legacy_codex_cache": True},
                    progress=lambda *a, **k: None,
                )
            self.assertEqual(model.call_count, 1)
            self.assertEqual(model.call_args.kwargs["images"], [media[1]["image"]["path"]])
            self.assertEqual(result[0]["observations"], ["旧简述"])
            self.assertEqual(result[1]["observations"], ["生活主体与可见细节"])
            self.assertEqual(again, result)

    def test_recoded_own_report_is_excluded_but_other_picture_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            reports.mkdir()
            im = Image.new("RGB", (1200, 3100), "white")
            im.save(reports / "brief.png", compress_level=0)
            im.save(root / "forward.png", compress_level=9)
            Image.new("RGB", im.size, "blue").save(root / "different.png")
            visuals = [
                dict(image_id=iid, kind="market_text", observations=["综合简报"], caveats=[]) for iid in ["I01", "I02"]
            ]
            media = [
                dict(id=iid, image={"path": str(root / name)})
                for iid, name in [("I01", "forward.png"), ("I02", "different.png")]
            ]
            mark_recirculated_briefs(visuals, media, reports)
            self.assertTrue(visuals[0]["excluded_from_analysis"])
            self.assertNotIn("excluded_from_analysis", visuals[1])
            body = dict(
                headline="测试",
                thesis="",
                mood="",
                timeline=[],
                themes=[dict(title="线索", text="回流", sources=["I01"])],
                stocks=[],
                other_mentions=[],
                next_watch=[],
                group_comparison=[],
                life=[],
                off_topic=[],
                investment_advice=[],
                limitations=[],
            )
            with self.assertRaisesRegex(ValueError, "不存在的引用"):
                validate_brief(body, {}, visuals)


class HardlinkTests(unittest.TestCase):
    def test_content_hash_mapping_is_scoped_to_group_month_and_safe_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "hardlink/hardlink.db"
            path.parent.mkdir()
            md5, group = "a" * 32, "b" * 32
            with closing(sqlite3.connect(path)) as db:
                db.executescript(
                    "CREATE TABLE dir2id(username TEXT); CREATE TABLE image_hardlink_info_v4(md5 TEXT,file_name TEXT,dir1 INTEGER,dir2 INTEGER);"
                )
                db.executemany(
                    "INSERT INTO dir2id VALUES (?)", [(group,), ("other-group",), ("2026-09",), ("2026-08",)]
                )
                db.executemany(
                    "INSERT INTO image_hardlink_info_v4 VALUES (?,?,?,?)",
                    [
                        (md5, "c" * 32 + ".dat", 1, 3),
                        (md5, "d" * 32 + ".dat", 2, 3),
                        (md5, "e" * 32 + ".dat", 1, 4),
                        (md5, "../" + "f" * 32 + ".dat", 1, 3),
                    ],
                )
                db.commit()

            def plain_query(path, key, sql):
                with closing(sqlite3.connect(path)) as db:
                    db.row_factory = sqlite3.Row
                    return [dict(row) for row in db.execute(sql)]

            targets = [
                dict(id="today", sent_at="2026-09-11", raw_text=f'<msg><img md5="{md5}"/></msg>'),
                dict(id="old", sent_at="2026-08-01", raw_text=f'<img md5="{md5}"/>'),
                dict(id="bad", sent_at="2026-09-11", raw_text='<msg><img md5="../invalid"/></msg>'),
            ]
            with (
                patch("marketpulse.media.key_for", return_value="fixture"),
                patch("marketpulse.media.query", side_effect=plain_query),
            ):
                found = hardlink_hashes(root, {}, targets, group)
            self.assertEqual(found, {"today": {"c" * 32}, "old": {"e" * 32}})


if __name__ == "__main__":
    unittest.main()
