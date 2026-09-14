"""Read one WeChat group from stable, encrypted DB/WAL copies using SQLCipher.

Keys stay local and are passed to the in-process SQLCipher API, never argv or diagnostics.
Message decoding is provided by the pinned wechat-local-mcp project.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from sqlcipher3 import dbapi2 as sqlite
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .core import ROOT, TZ, digest, now

KEY_CONFIG = Path.home() / ".config/wxcli/config.json"
CIPHER = None  # Compatibility argument; SQLCipher is provided by the Python wheel.


def read_config(path=KEY_CONFIG):
    if not Path(path).is_file():
        raise RuntimeError("尚未初始化本机数据库密钥；请先运行 wechat-pulse setup-wechat")
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    root = Path(cfg["db_root"]).expanduser()
    if (root / "db_storage").is_dir():
        root /= "db_storage"
    if not (root / "contact/contact.db").is_file():
        raise RuntimeError("密钥配置中的数据库位置不可用")
    keys = cfg.get("keys", {})
    if not isinstance(keys, dict):
        raise RuntimeError("不支持的密钥配置格式")
    return root, keys


def shards(root):
    return sorted((root / "message").glob("message_*.db"), key=lambda p: p.name)


def message_shards(root):
    return [p for p in shards(root) if re.fullmatch(r"message_\d+\.db", p.name)]


def key_for(path, keys):
    with path.open("rb") as f:
        salt = f.read(16).hex()
    key = keys.get(salt, "")
    if not isinstance(key, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", key):
        raise RuntimeError(f"{path.name} 的数据库密钥缺失；需要刷新密钥，不能宣称已完整导出")
    return key + salt


def status():
    result = {"sqlcipher_ready": True, "key_config_present": KEY_CONFIG.is_file()}
    if result["key_config_present"]:
        root, keys = read_config()
        files = [root / "contact/contact.db", *message_shards(root)]
        missing = []
        for file in files:
            try:
                key_for(file, keys)
            except RuntimeError:
                missing.append(file.name)
        result.update(required_databases=len(files), missing_keys=missing)
    return result


def fingerprint(path):
    try:
        stat = path.stat()
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except FileNotFoundError:
        return None


@contextmanager
def snapshot(source, directory=None):
    """Retry if either file changes during the pair copy. SQLite rebuilds SHM."""
    with tempfile.TemporaryDirectory(prefix="wx-snapshot-", dir=directory) as temp:
        target = Path(temp) / source.name
        originals = [source, Path(str(source) + "-wal")]
        for attempt in range(5):
            before = [fingerprint(p) for p in originals]
            if before[0] is None:
                raise RuntimeError(f"数据库不存在：{source.name}")
            for src, meta in zip(originals, before):
                dest = Path(temp) / src.name
                dest.unlink(missing_ok=True)
                if meta is not None:
                    # APFS copy-on-write clone avoids copying gigabytes per poll.
                    copy = (
                        subprocess.run(["/bin/cp", "-c", str(src), str(dest)], capture_output=True)
                        if sys.platform == "darwin"
                        else None
                    )
                    if copy is None or copy.returncode:
                        shutil.copyfile(src, dest)
                    dest.chmod(0o600)
            if before == [fingerprint(p) for p in originals]:
                yield target
                return
            time.sleep(0.2 * (attempt + 1))
        raise RuntimeError(f"{source.name} 持续写入，未取得一致的 DB/WAL 副本；稍后重试")


def query(path, raw_key, sql, cipher=None):
    if not re.fullmatch(r"[a-fA-F0-9]{96}", raw_key):
        raise RuntimeError("不支持的密钥格式")
    db = None
    try:
        # Only a disposable DB/WAL copy is opened; SQLite may create its SHM.
        db = sqlite.connect(str(path), timeout=30)
        db.execute("PRAGMA key=\"x'" + raw_key + "'\"")
        db.execute("PRAGMA cipher_compatibility=4")
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite.Row
        return [dict(row) for row in db.execute(sql)]
    except sqlite.Error:
        raise RuntimeError(f"{path.name} 解密/查询失败；请检查本机密钥或微信版本") from None
    finally:
        if db is not None:
            db.close()


def columns(path, key, table, cipher=CIPHER):
    return {r["name"] for r in query(path, key, f'PRAGMA table_info("{table}")', cipher)}


def _proto_fields(raw):
    """Read bounded protobuf wire fields; never accept a truncated member record."""
    pos = 0

    def varint():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(raw):
                raise ValueError("truncated protobuf")
            byte = raw[pos]
            pos += 1
            if shift == 63 and byte > 1:
                raise ValueError("protobuf varint overflow")
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("protobuf varint overflow")

    fields = []
    while pos < len(raw):
        tag = varint()
        field, wire = tag >> 3, tag & 7
        if not field:
            raise ValueError("invalid protobuf field")
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            length = varint() if wire == 2 else (8 if wire == 1 else 4)
            if pos + length > len(raw):
                raise ValueError("truncated protobuf")
            value = raw[pos : pos + length]
            pos += length
        else:
            raise ValueError("unsupported protobuf wire type")
        fields.append((field, wire, value))
    return fields


def parse_group_members(raw):
    """WeChat 4.x: repeated field 1 member, member field 1 UID / 2 room nickname.

    Field 4 in a member can contain the inviter's UID; it is never a nickname.
    No inference from names or substring matching is used for identity.
    """
    result = {}
    for field, wire, chunk in _proto_fields(raw):
        if (field, wire) != (1, 2):
            continue
        strings = {}
        for number, kind, value in _proto_fields(chunk):
            if kind == 2 and number in (1, 2):
                if number in strings:
                    raise ValueError("duplicate member identity/name field")
                strings[number] = value.decode("utf-8").strip()
        uid, name = strings.get(1, ""), strings.get(2, "")
        if not uid:
            continue
        if any(ord(c) < 32 for c in uid + name):
            raise ValueError("invalid member identity/name")
        if uid in result and result[uid] != name:
            raise ValueError("conflicting group nicknames")
        result[uid] = name
    return result


def parse_group_nicknames(raw):
    return {uid: name for uid, name in parse_group_members(raw).items() if name}


def _room_members(path, key, group_id, cipher=CIPHER):
    cols = columns(path, key, "chat_room", cipher)
    if not {"username", "ext_buffer"} <= cols:
        return {}
    quoted = "'" + group_id.replace("'", "''") + "'"
    rows = query(path, key, f"SELECT hex(ext_buffer) AS data FROM chat_room WHERE username={quoted}", cipher)
    if len(rows) > 1:
        raise RuntimeError("群昵称数据库有重复群记录，停止更新")
    try:
        return parse_group_members(bytes.fromhex(rows[0]["data"] or "")) if rows else {}
    except (ValueError, UnicodeError):
        raise RuntimeError("群昵称数据结构不兼容，未用猜测结果覆盖昵称") from None


def group_nicknames(path, key, group_id, cipher=CIPHER):
    return {uid: name for uid, name in _room_members(path, key, group_id, cipher).items() if name}


def read_group_members(group_id, key_config=KEY_CONFIG, cipher=CIPHER):
    """List only verified room members, including those with no room nickname."""
    root, keys = read_config(key_config)
    with snapshot(root / "contact/contact.db") as path:
        key = key_for(path, keys)
        members = _room_members(path, key, group_id, cipher)
        names = _member_names(_contact_rows(path, key, cipher), {uid: name for uid, name in members.items() if name})
        return [
            dict(sender_id=uid, **names.get(uid, {"name": "未同步昵称", "name_source": "unresolved"}))
            for uid in sorted(members)
        ]


def _contact_rows(path, key, cipher=CIPHER):
    cols = columns(path, key, "contact", cipher)
    if "username" not in cols:
        raise RuntimeError("不支持的联系人数据库结构")
    selected = [c for c in ("username", "remark", "nick_name") if c in cols]
    return query(path, key, "SELECT " + ",".join(selected) + " FROM contact", cipher)


def _member_names(rows, room_names):
    # A shared report uses the member's nickname before the owner's private remark.
    names = {
        r["username"]: {
            "name": r.get("nick_name") or r.get("remark") or r["username"],
            "name_source": "contact_nickname" if r.get("nick_name") else "contact_fallback",
        }
        for r in rows
        if r["username"] and not r["username"].endswith("@chatroom")
    }
    names.update({uid: {"name": name, "name_source": "group_nickname"} for uid, name in room_names.items()})
    return names


def read_group_names(group_id, key_config=KEY_CONFIG, cipher=CIPHER):
    """Fetch the latest names synchronized into this logged-in desktop's DB/WAL."""
    root, keys = read_config(key_config)
    with snapshot(root / "contact/contact.db") as path:
        key = key_for(path, keys)
        return _member_names(_contact_rows(path, key, cipher), group_nicknames(path, key, group_id, cipher))


def export_group(prefix, output, username=None, since=None, key_config=KEY_CONFIG, cipher=CIPHER, cursors=None):
    from ._vendor.wechat_message import _row_message, decode, split_type, table_for

    root, keys = read_config(key_config)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    began = now()
    files = message_shards(root)
    if not files:
        raise RuntimeError("未找到微信消息分库")
    # Validate key availability before producing any transcript.
    for file in [root / "contact/contact.db", *files]:
        key_for(file, keys)
    contact_source = root / "contact/contact.db"
    with snapshot(contact_source) as contact:
        key = key_for(contact, keys)
        rows = _contact_rows(contact, key, cipher)
        contacts = {
            r["username"]: r.get("remark") or r.get("nick_name") or r["username"] for r in rows if r["username"]
        }
        matches = [
            (u, name)
            for u, name in contacts.items()
            if u.endswith("@chatroom") and (u == username if username else name.startswith(prefix))
        ]
        if len(matches) != 1:
            names = "、".join(name for _, name in matches)
            raise RuntimeError(f"匹配到 {len(matches)} 个群：{names or '无'}；需要精确群名或群 ID")
        uid, group = matches[0]
        members = _member_names(rows, group_nicknames(contact, key, uid, cipher))
        contacts.update({u: entry["name"] for u, entry in members.items()})
    chat = {"username": uid, "display_name": group, "is_group": True}
    table = table_for(uid)
    records, coverage = [], []
    stamps = {p.name: [fingerprint(p), fingerprint(Path(str(p) + "-wal"))] for p in files}
    for source in files:
        previous = (cursors or {}).get(source.name)
        stamp = json.loads(json.dumps(stamps[source.name]))
        if previous and previous.get("source_stamp") == stamp:
            coverage.append({**previous, "messages": 0, "incremental": True, "unchanged": True})
            continue
        with snapshot(source) as path:
            key = key_for(path, keys)
            has = query(path, key, f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'", cipher)
            if not has:
                coverage.append({"shard": source.name, "messages": 0, "table_present": False})
                continue
            cols = columns(path, key, table, cipher)
            required = {"local_id", "local_type", "real_sender_id", "create_time", "message_content"}
            if not required <= cols:
                raise RuntimeError(f"{source.name} 消息表结构不兼容，停止本轮导出")
            maximum = query(path, key, f'SELECT MAX(local_id) AS maximum FROM "{table}"', cipher)[0]["maximum"] or 0
            previous = (cursors or {}).get(source.name)
            after_id = None
            if previous and previous.get("salt") == key[64:] and int(previous["max_local_id"]) <= maximum:
                after_id = int(previous["max_local_id"])
            if after_id == maximum:
                coverage.append(
                    {
                        "shard": source.name,
                        "messages": 0,
                        "table_present": True,
                        "max_local_id": maximum,
                        "salt": key[64:],
                        "incremental": True,
                    }
                )
                continue
            names = {
                int(r["id"]): r["user_name"]
                for r in query(
                    path, key, "SELECT rowid AS id, CAST(user_name AS TEXT) AS user_name FROM Name2Id", cipher
                )
            }
            selected = ["local_id", "local_type", "real_sender_id", "create_time"]
            selected += [
                f"hex({c}) AS {c}" if c in cols else f"NULL AS {c}"
                for c in ("message_content", "compress_content", "source")
            ]
            selected += ["WCDB_CT_message_content AS flag" if "WCDB_CT_message_content" in cols else "NULL AS flag"]
            selected += ["CAST(server_id AS TEXT) AS server_id" if "server_id" in cols else "NULL AS server_id"]
            sql = f'SELECT {",".join(selected)} FROM "{table}"'
            predicates = []
            if since is not None:
                predicates.append(f"create_time >= {int(since)}")
            if after_id is not None:
                predicates.append(f"local_id > {after_id}")
            if predicates:
                sql += " WHERE " + " AND ".join(predicates)
            sql += " ORDER BY create_time,local_id"
            rows = query(path, key, sql, cipher)
            for row in rows:
                for c in ("message_content", "compress_content", "source"):
                    row[c] = bytes.fromhex(row[c]) if row[c] else None
                item = _row_message(row, chat, contacts, names)
                raw = decode(row["message_content"], row["flag"]) or decode(row["compress_content"], row["flag"])
                if raw == "[压缩消息解码失败]":
                    raise RuntimeError(f"{source.name} 有压缩正文未成功解码，本轮未发布")
                if item["sender_username"]:
                    raw = re.sub(r"^" + re.escape(item["sender_username"]) + r":[ \t]*\r?\n", "", raw, count=1)
                stamp = datetime.fromtimestamp(item["timestamp"], TZ).isoformat()
                server = row["server_id"]
                identity = [uid, "server", server] if server and server != "0" else [uid, source.name, row["local_id"]]
                records.append(
                    {
                        **item,
                        "id": digest(identity),
                        "group_name": group,
                        "group_id": uid,
                        "shard": source.name,
                        "server_id": server,
                        "sent_at": stamp,
                        "raw_text": raw,
                        "text": raw if split_type(row["local_type"])[0] == 1 else "",
                        "sender": item["sender"] or "未知成员",
                        "sender_name_source": members.get(item["sender_username"], {}).get("name_source", "unresolved"),
                        "sender_id": item["sender_username"] or str(row["real_sender_id"]),
                    }
                )
            coverage.append(
                {
                    "shard": source.name,
                    "messages": len(rows),
                    "table_present": True,
                    "max_local_id": maximum,
                    "salt": key[64:],
                    "incremental": after_id is not None,
                }
            )
    if {p.name for p in files} != {p.name for p in message_shards(root)}:
        raise RuntimeError("导出期间新建了消息分库，请重试")
    for entry in coverage:
        source = root / "message" / entry["shard"]
        after = [fingerprint(source), fingerprint(Path(str(source) + "-wal"))]
        entry["source_stamp"] = stamps[source.name] if stamps[source.name] == after else None
    records.sort(key=lambda r: (r["timestamp"], r["id"]))
    # Duplicate server IDs can occur after migration between shards.
    unique = {}
    for record in records:
        old = unique.get(record["id"])
        if old and any(old[k] != record[k] for k in ("raw_text", "sent_at", "sender_id")):
            raise RuntimeError("同一服务器消息 ID 对应不同内容，停止导出以避免错误合并")
        unique[record["id"]] = record
    records = list(unique.values())
    text_path = output.with_name(output.stem + ".texts.jsonl")
    manifest_path = output.with_name(output.stem + ".manifest.json")
    manifest = {
        "group_name": group,
        "group_id": uid,
        "export_started": began,
        "export_finished": now(),
        "messages": len(records),
        "text_messages": sum(bool(r["text"].strip()) for r in records),
        "earliest": records[0]["sent_at"] if records else None,
        "latest": records[-1]["sent_at"] if records else None,
        "since_timestamp": since,
        "shards": coverage,
        "incremental": cursors is not None,
        "scope": "本机现存全部消息分库；包含已提交 WAL。各分库分别取一致副本，并非跨库同一瞬间快照。",
    }
    for dest, content in [
        (output, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)),
        (text_path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records if r["text"].strip())),
        (manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2)),
    ]:
        with tempfile.NamedTemporaryFile(mode="w", dir=dest.parent, delete=False, encoding="utf-8") as f:
            f.write(content)
            temp = Path(f.name)
        temp.chmod(0o600)
        os.replace(temp, dest)
    return manifest, text_path
