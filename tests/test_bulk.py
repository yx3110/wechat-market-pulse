import json
import os
import selectors
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from marketpulse.bulk import export_group, query, snapshot, parse_group_nicknames
from sqlcipher3 import dbapi2 as sqlite
from contextlib import closing
from marketpulse.core import TZ


class BulkTests(unittest.TestCase):
    def setUp(self):
        from marketpulse._vendor.wechat_message import table_for

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.keys = {}
        self.uid = "123456@chatroom"
        self.table = table_for(self.uid)
        self.stamp = int(datetime(2026, 9, 4, 9, 30, tzinfo=TZ).timestamp())
        self.create(
            "contact/contact.db",
            "CREATE TABLE contact(username TEXT,remark TEXT,nick_name TEXT);"
            "INSERT INTO contact VALUES ('123456@chatroom','','示例测试群'),('wxid_alice','','小葱'),('other@chatroom','','其他群');",
        )
        schema = f'CREATE TABLE "{self.table}"(local_id INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content BLOB,WCDB_CT_message_content INTEGER,server_id INTEGER);'
        schema += "CREATE TABLE Name2Id(user_name TEXT); INSERT INTO Name2Id(rowid,user_name) VALUES(1,'wxid_alice');"
        self.schema = schema
        rows = "".join(
            f"INSERT INTO \"{self.table}\" VALUES({i},1,1,{self.stamp + i},'wxid_alice:\n看好芯片[捂脸]{i}',0,{1000 + i});"
            for i in range(1, 206)
        )
        self.create("message/message_0.db", schema + rows)
        self.create(
            "message/message_1.db",
            schema + f"INSERT INTO \"{self.table}\" VALUES(1,3,1,{self.stamp + 300},'<img />',0,9999);",
        )
        self.cfg = self.root / "keys.json"
        self.save_config()
        self.out = self.root / "out/chat.jsonl"

    def save_config(self):
        self.cfg.write_text(json.dumps({"db_root": str(self.root), "keys": self.keys}))

    def create(self, name, sql):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        key, salt = os.urandom(32).hex(), os.urandom(16).hex()
        raw = key + salt
        with closing(sqlite.connect(str(path))) as db:
            db.execute("PRAGMA key=\"x'" + raw + "'\"")
            db.executescript(sql)
            db.commit()
        self.assertEqual(path.read_bytes()[:16].hex(), salt)
        self.keys[salt] = key
        return path, raw

    def export(self):
        return export_group("示例", self.out, key_config=self.cfg)

    def test_collector_keeps_old_messages_and_reads_new_wal(self):
        from marketpulse.inputs import collect
        from marketpulse.core import digest
        from unittest.mock import patch

        work = self.root / "collection"
        cfg = dict(key_config=self.cfg, analysis_seconds=300)
        self.assertEqual(collect("2026-09-04", [self.uid], cfg, work), 206)
        with patch("marketpulse.bulk.query", wraps=query) as calls:
            self.assertEqual(collect("2026-09-04", [self.uid], cfg, work), 206)
        self.assertFalse(any("MAX(local_id)" in str(call) for call in calls.call_args_list))
        from marketpulse.bulk import key_for

        source = self.root / "message/message_0.db"
        with closing(sqlite.connect(str(source))) as db:
            db.execute("PRAGMA key=\"x'" + key_for(source, self.keys) + "'\"")
            db.execute(
                f'INSERT INTO "{self.table}" VALUES(206,1,1,?, ?,0,1206)', (self.stamp + 400, "wxid_alice:\n新消息")
            )
            db.commit()
        self.assertEqual(collect("2026-09-04", [self.uid], cfg, work), 207)
        saved = work / ("group-" + digest(self.uid)[:16] + ".jsonl")
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8").splitlines()[-1])["text"], "新消息")

    def test_all_shards_more_than_200_original_text_and_repeat(self):
        first, texts = self.export()
        self.assertEqual(first["messages"], 206)
        self.assertEqual(first["text_messages"], 205)
        rows = [json.loads(line) for line in texts.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["text"], "看好芯片[捂脸]1")
        self.assertEqual(rows[0]["sender"], "小葱")
        self.assertEqual(rows[0]["sent_at"], "2026-09-04T09:30:01+08:00")
        before = texts.read_bytes()
        self.export()
        self.assertEqual(before, texts.read_bytes())
        if os.name != "nt":
            self.assertEqual(texts.stat().st_mode & 0o777, 0o600)

    def test_wrong_key_and_missing_key_do_not_publish(self):
        salt = next(iter(self.keys))
        self.keys[salt] = "00" * 32
        self.save_config()
        with self.assertRaises(RuntimeError):
            self.export()
        self.assertFalse(self.out.exists())
        self.keys.pop(salt)
        self.save_config()
        with self.assertRaisesRegex(RuntimeError, "密钥缺失"):
            self.export()

    def test_ambiguous_group_stops(self):
        self.create(
            "contact/contact.db",
            "CREATE TABLE contact(username TEXT,nick_name TEXT); INSERT INTO contact VALUES('1@chatroom','示例一'),('2@chatroom','示例二');",
        )
        self.save_config()
        with self.assertRaisesRegex(RuntimeError, "2 个群"):
            self.export()
        self.assertFalse(self.out.exists())

    def test_current_room_nickname_overrides_contact_and_remark(self):
        # Both group-local identity and field numbers matter; inviter field 4
        # and the same account's name in a different group must be ignored.
        def string(field, value):
            b = value.encode()
            return bytes([field * 8 + 2, len(b)]) + b

        def member(name):
            b = string(1, "wxid_alice") + string(2, name) + string(4, "wxid_inviter")
            return bytes([10, len(b)]) + b

        blob, other = member("最新群昵称"), member("另一个群昵称")
        self.create(
            "contact/contact.db",
            "CREATE TABLE contact(username TEXT,remark TEXT,nick_name TEXT);"
            "INSERT INTO contact VALUES('123456@chatroom','','示例测试群'),('wxid_alice','旧备注','微信昵称');"
            "CREATE TABLE chat_room(username TEXT,ext_buffer BLOB);"
            f"INSERT INTO chat_room VALUES('123456@chatroom',x'{blob.hex()}'),('other@chatroom',x'{other.hex()}');",
        )
        self.save_config()
        _, texts = self.export()
        row = json.loads(texts.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["sender"], "最新群昵称")
        self.assertEqual(row["sender_name_source"], "group_nickname")
        self.assertEqual(row["sender_id"], "wxid_alice")
        with self.assertRaises(ValueError):
            parse_group_nicknames(blob[:-1])

    def test_compressed_text(self):
        import zstandard

        blob = zstandard.ZstdCompressor().compress("wxid_alice:\n下午看空，不追高".encode()).hex()
        self.create(
            "message/message_2.db",
            self.schema + f"INSERT INTO \"{self.table}\" VALUES(1,1,1,{self.stamp + 400},x'{blob}',4,12000);",
        )
        self.save_config()
        _, texts = self.export()
        last = json.loads(texts.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(last["text"], "下午看空，不追高")

    def test_incremental_catches_backfill_with_old_timestamp(self):
        manifest, _ = self.export()
        cursors = {r["shard"]: r for r in manifest["shards"] if r["table_present"]}
        unchanged, _ = export_group("示例", self.out, key_config=self.cfg, cursors=cursors)
        self.assertEqual(unchanged["messages"], 0)
        path = self.root / "message/message_0.db"
        salt = path.read_bytes()[:16].hex()
        raw = self.keys[salt] + salt
        sql = f"PRAGMA key=\"x'{raw}'\"; INSERT INTO \"{self.table}\" VALUES(206,1,1,{self.stamp - 86400},'昨天的补同步消息',0,19000);"
        with closing(sqlite.connect(str(path))) as db:
            db.executescript(sql)
            db.commit()
        changed, texts = export_group("示例", self.out, key_config=self.cfg, cursors=cursors)
        self.assertEqual(changed["messages"], 1)
        row = json.loads(texts.read_text(encoding="utf-8").strip())
        self.assertEqual(row["text"], "昨天的补同步消息")
        self.assertTrue(row["sent_at"].startswith("2026-09-03"))

    def test_rotated_shard_does_not_reuse_old_cursor(self):
        manifest, _ = self.export()
        cursors = {r["shard"]: r for r in manifest["shards"] if r["table_present"]}
        self.create(
            "message/message_0.db",
            self.schema + f"INSERT INTO \"{self.table}\" VALUES(1,1,1,{self.stamp},'换库后的消息',0,20000);",
        )
        self.save_config()
        changed, texts = export_group("示例", self.out, key_config=self.cfg, cursors=cursors)
        self.assertEqual(changed["messages"], 1)
        self.assertEqual(json.loads(texts.read_text(encoding="utf-8").strip())["text"], "换库后的消息")

    def test_committed_wal_is_included_without_writing_original(self):
        path, raw = self.create("wal.db", "CREATE TABLE items(value TEXT);")
        with closing(sqlite.connect(str(path))) as db:
            db.execute("PRAGMA key=\"x'" + raw + "'\"")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("INSERT INTO items VALUES('new committed message')")
            db.commit()
            wal = Path(str(path) + "-wal")
            self.assertTrue(wal.exists())
            before = (path.read_bytes(), wal.read_bytes())
            with snapshot(path) as copy:
                self.assertEqual(query(copy, raw, "SELECT value FROM items"), [{"value": "new committed message"}])
            self.assertEqual(before, (path.read_bytes(), wal.read_bytes()))


if __name__ == "__main__":
    unittest.main()
