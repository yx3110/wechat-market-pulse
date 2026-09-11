"""Resolve this group's image resources and decode local V1/V2 DAT media.

DB access uses the same verified SQLCipher + WAL snapshot path as bulk export.
Media layout: erbanku/weixin-cli src/attachment (see THIRD_PARTY.md).
No CDN requests. No key material is included in outputs or model prompts.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from PIL import Image

from .bulk import KEY_CONFIG, key_for, query, read_config, snapshot


def decode_dat(data, aes_key, xor_key):
    if data[:6] in (b"\x07\x08V1\x08\x07", b"\x07\x08V2\x08\x07"):
        if len(data) < 31:
            raise ValueError("图片附件头部不完整")
        aes_size, xor_size = struct.unpack("<II", data[6:14])
        aes_end = 15 + aes_size + 16 - aes_size % 16
        raw_end = len(data) - xor_size
        if not 15 < aes_end <= raw_end <= len(data):
            raise ValueError("图片附件分段长度不合法")
        key = b"cfcd208495d565ef" if data[3] == 49 else aes_key
        if not key or len(key) != 16 or xor_key is None or not 0 <= xor_key <= 255:
            raise ValueError("缺少有效的本机图片解码密钥")
        head = unpad(AES.new(key, AES.MODE_ECB).decrypt(data[15:aes_end]), 16)
        if len(head) != aes_size:
            raise ValueError("图片附件解密后长度不匹配")
        data = head + data[aes_end:raw_end] + bytes(x ^ xor_key for x in data[raw_end:])
    return data


def image_from_bytes(data):
    if data.startswith(b"wxgf"):
        # Static images contain an Annex-B HEVC VPS start followed by the frame.
        # Decode the first frame, never run a command embedded in the media.
        import av

        start = data.find(b"\x00\x00\x00\x01\x40\x01")
        if start < 0:
            start = data.find(b"\x00\x00\x01\x40\x01")
        if start < 0:
            raise ValueError("不支持的 WXGF 图片结构")
        with av.open(io.BytesIO(data[start:]), format="hevc") as container:
            img = next(container.decode(video=0)).to_image()
    else:
        img = Image.open(io.BytesIO(data))
        img.load()  # A valid header alone is not enough.
    if img.width * img.height > 50_000_000:
        raise ValueError("图片尺寸超过本地解析上限")
    return img.convert("RGB")


def packed_hashes(blob):
    return sorted(
        set(x.decode("ascii").lower() for x in re.findall(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])", blob))
    )


def hardlink_hashes(root, keys, targets, chat_hash):
    """Map message XML content MD5 to local filenames, within this group/month only.

    MessageResourceInfo can lag after login/backfill even when attachments exist.
    Local attachment basenames are not necessarily the message's content MD5.
    """
    wanted = {}
    for msg in targets:
        try:
            element = ET.fromstring(msg.get("raw_text", ""))
            node = element if element.tag == "img" else element.find("img")
            digest = node.get("md5", "").lower() if node is not None else ""
        except (ET.ParseError, ValueError):
            continue
        if re.fullmatch(r"[a-f0-9]{32}", digest):
            wanted[msg["id"]] = (digest, msg["sent_at"][:7])
    source = root / "hardlink/hardlink.db"
    if not wanted or not source.is_file():
        return {}
    hashes = sorted({h for h, month in wanted.values()})
    mapping = {}
    with snapshot(source) as db:
        key = key_for(db, keys)
        for start in range(0, len(hashes), 400):
            values = ",".join("'" + h + "'" for h in hashes[start : start + 400])
            rows = query(
                db,
                key,
                f"""SELECT h.md5,h.file_name,d1.username AS group_dir,d2.username AS month
                FROM image_hardlink_info_v4 h
                JOIN dir2id d1 ON h.dir1=d1.rowid JOIN dir2id d2 ON h.dir2=d2.rowid
                WHERE d1.username='{chat_hash}' AND h.md5 IN ({values})""",
            )
            for row in rows:
                match = re.fullmatch(r"([a-fA-F0-9]{32})(?:_[th])?\.dat", row["file_name"] or "")
                if row["group_dir"] == chat_hash and match:
                    mapping.setdefault((row["md5"].lower(), row["month"]), set()).add(match[1].lower())
    return {mid: mapping[pair] for mid, pair in wanted.items() if pair in mapping}


def resolve_images(records, output, key_config=KEY_CONFIG):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    root, keys = read_config(key_config)
    cfg = json.loads(Path(key_config).read_text(encoding="utf-8"))
    image_key = cfg.get("image_key", "").encode("ascii")
    xor_key = cfg.get("image_xor_key")
    targets = [r for r in records if r["message_kind"] == "image"]
    if not targets:
        return []
    group_ids = {r["group_id"] for r in records}
    if len(group_ids) != 1:
        raise ValueError("每次只解析一个群的图片")
    uid = next(iter(group_ids))
    if not re.fullmatch(r"[0-9]+@chatroom", uid):
        raise ValueError("不支持的群 ID")
    lo, hi = min(r["timestamp"] for r in targets), max(r["timestamp"] for r in targets)
    rows, lookup_failures = [], []
    try:
        with snapshot(root / "message/message_resource.db") as snap:
            rows = query(
                snap,
                key_for(snap, keys),
                f"""SELECT message_local_id,
                message_create_time,CAST(message_svr_id AS TEXT) AS server_id,hex(packed_info) AS info
                FROM MessageResourceInfo WHERE chat_id=(SELECT rowid FROM ChatName2Id WHERE user_name='{uid}')
                AND message_create_time BETWEEN {int(lo)} AND {int(hi)} AND message_local_type % 4294967296=3""",
            )
    except Exception as exc:
        lookup_failures.append({"stage": "resource_index", "error": type(exc).__name__})
    resource = {}
    for row in rows:
        resource.setdefault((row["message_local_id"], row["message_create_time"]), []).append(row)
    chat_hash = hashlib.md5(uid.encode()).hexdigest()
    try:
        linked = hardlink_hashes(root, keys, targets, chat_hash)
    except Exception as exc:
        linked = {}
        lookup_failures.append({"stage": "hardlink_index", "error": type(exc).__name__})
    items = []
    for n, msg in enumerate(targets, 1):
        item = {
            "id": f"I{n:02d}",
            "message_id": msg["id"],
            "local_id": msg["local_id"],
            "sent_at": msg["sent_at"],
            "sender": msg["sender"],
            "variants": [],
            "failures": list(lookup_failures),
        }
        matches = resource.get((msg["local_id"], msg["timestamp"]), [])
        if msg.get("server_id") not in (None, "", "0"):
            matches = [r for r in matches if r["server_id"] == msg["server_id"]]
        # Do not match on local_id alone: it is reused across message shards.
        hashes = set(h for r in matches for h in packed_hashes(bytes.fromhex(r["info"])))
        hashes.update(linked.get(msg["id"], set()))
        item["hardlink_md5_match"] = msg["id"] in linked
        month = msg["sent_at"][:7]
        imgdir = root.parent / "msg/attach" / chat_hash / month / "Img"
        candidates = [p for h in hashes for p in imgdir.glob(h + "*.dat")]
        for p in sorted(candidates, key=lambda p: p.stat().st_size, reverse=True):
            variant = "thumb" if p.name.endswith("_t.dat") else "hd" if p.name.endswith("_h.dat") else "full"
            dest = output / (msg["id"][:16] + "_" + variant + "_" + p.stem[:8] + ".png")
            try:
                if dest.exists():
                    img = Image.open(dest)
                    img.load()
                else:
                    img = image_from_bytes(decode_dat(p.read_bytes(), image_key, xor_key))
                    img.save(dest)
                    dest.chmod(0o600)
                item["variants"].append(
                    {"path": str(dest.resolve()), "size": list(img.size), "thumbnail": variant == "thumb"}
                )
            except Exception as exc:
                # Never dump encrypted payloads, key material, or embedded URLs.
                item["failures"].append({"variant": variant, "error": type(exc).__name__})
        if item["variants"]:
            item["image"] = max(item["variants"], key=lambda v: v["size"][0] * v["size"][1])
        items.append(item)
    return items
