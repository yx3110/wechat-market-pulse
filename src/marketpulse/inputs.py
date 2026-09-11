"""Normalize multiple groups while retaining stable, group-specific identity."""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .bulk import KEY_CONFIG, export_group, read_group_names
from .core import TZ, digest, period_at
from .media import resolve_images


def write_records(path, rows):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
        file.flush()
        os.fsync(file.fileno())
        temp = Path(file.name)
    temp.chmod(0o600)
    os.replace(temp, path)


def collect(date, gids, cfg, work):
    """Persist new local messages independently of the slower model/report job."""
    from filelock import FileLock
    from .briefing import save_json

    work = Path(work)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    total = 0
    with FileLock(str(work / "collection.lock"), timeout=0):
        for gid in sorted(set(gids)):
            stem = "group-" + digest(gid)[:16]
            source, checkpoint = work / (stem + ".jsonl"), work / (stem + ".collector.json")
            old = json.loads(checkpoint.read_text(encoding="utf-8")) if source.exists() and checkpoint.exists() else {}
            # Full reconciliation once per report interval also catches edits/revocations.
            import time

            full = time.time() - old.get("full_at", 0) >= cfg.get("analysis_seconds", 300)
            cursors = None if full else {s["shard"]: s for s in old.get("shards", [])}
            with tempfile.TemporaryDirectory(dir=work) as tmp:
                delta = Path(tmp) / "delta.jsonl"
                manifest, _ = export_group(
                    "",
                    delta,
                    username=gid,
                    since=int(datetime.fromisoformat(date).replace(tzinfo=TZ).timestamp()),
                    key_config=cfg.get("key_config", KEY_CONFIG),
                    cursors=cursors,
                )
                fresh = [json.loads(line) for line in delta.read_text(encoding="utf-8").splitlines()]
            rows = (
                {}
                if full or not source.exists()
                else {r["id"]: r for r in map(json.loads, source.read_text(encoding="utf-8").splitlines())}
            )
            rows.update({r["id"]: r for r in fresh})
            ordered = sorted(rows.values(), key=lambda r: (r["timestamp"], r["id"]))
            write_records(source, ordered)
            save_json(
                checkpoint, {"shards": manifest["shards"], "full_at": time.time() if full else old.get("full_at", 0)}
            )
            total += len(ordered)
    return total


def prepare(date, group_id, cfg, work, refresh):
    from .briefing import refresh_record_names, save_json

    requested = sorted(set([group_id] if isinstance(group_id, str) else group_id or []))
    records = []
    imported = cfg.get("input")
    if imported:
        source = Path(imported).expanduser().resolve()
        records = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
        required = {"id", "group_id", "group_name", "sender_id", "sender", "sent_at", "message_kind"}
        for record in records:
            if not required <= record.keys():
                raise ValueError("JSONL 缺少必要字段；参考 examples/messages.jsonl")
            stamp = datetime.fromisoformat(record["sent_at"])
            if stamp.tzinfo is None:
                raise ValueError("导入消息时间必须包含时区")
            record["sent_at"] = stamp.astimezone(TZ).isoformat()
            record["timestamp"] = int(stamp.timestamp())
            record.setdefault("text", "")
        if requested:
            records = [r for r in records if r["group_id"] in requested]
    else:
        if not requested:
            raise ValueError("请指定至少一个 --group-id，或在配置 groups 中选择群")
        for gid in requested:
            source = work / ("group-" + digest(gid)[:16] + ".jsonl")
            if refresh or not source.exists():
                since = int(datetime.fromisoformat(date).replace(tzinfo=TZ).timestamp())
                export_group("", source, username=gid, since=since, key_config=cfg.get("key_config", KEY_CONFIG))
            rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
            if not refresh:
                refresh_record_names(rows, read_group_names(gid, key_config=cfg.get("key_config", KEY_CONFIG)))
            records.extend(rows)
    records = [
        r
        for r in records
        if r["sent_at"].startswith(date)
        and (cfg.get("period", "all") == "all" or period_at(r["sent_at"]) == cfg["period"])
    ]
    records.sort(key=lambda r: (r["timestamp"], r["id"]))
    if not records:
        raise ValueError("所选日期/时段暂无消息")
    unique = {}
    for record in records:
        if record["id"] in unique and unique[record["id"]] != record:
            raise ValueError("重复消息 ID 对应不同内容，停止合并")
        unique[record["id"]] = record
    records = list(unique.values())
    group_ids = sorted({r["group_id"] for r in records})
    groups = []
    media = []
    for i, gid in enumerate(group_ids, 1):
        rows = [r for r in records if r["group_id"] == gid]
        group = dict(id=gid, name=rows[-1]["group_name"], alias=f"群{i}", messages=len(rows))
        groups.append(group)
        for row in rows:
            row["group_alias"] = group["alias"]
            row["participant_id"] = digest([gid, row["sender_id"]])
        if not imported:
            media.extend(
                resolve_images(rows, work / "images" / digest(gid)[:16], key_config=cfg.get("key_config", KEY_CONFIG))
            )
    if imported:
        from PIL import Image

        source_root = Path(imported).expanduser().resolve().parent
        image_dir = work / "images"
        image_dir.mkdir(exist_ok=True)
        for row in records:
            if row["message_kind"] != "image":
                continue
            m = dict(
                message_id=row["id"],
                sender=row["sender"],
                sender_id=row["sender_id"],
                sent_at=row["sent_at"],
                variants=[],
                failures=[],
            )
            if row.get("image_path"):
                path = (source_root / row["image_path"]).resolve()
                if not path.is_relative_to(source_root):
                    raise ValueError("导入配图必须位于 JSONL 所在目录中，不能通过路径访问其他文件")
                try:
                    with Image.open(path) as im:
                        if im.width * im.height > 50_000_000:
                            raise ValueError("图像过大")
                        dest = image_dir / (digest(row["id"])[:24] + ".png")
                        im.convert("RGB").save(dest)
                        dest.chmod(0o600)
                        m["image"] = dict(path=str(dest.resolve()), size=list(im.size), thumbnail=False)
                except (OSError, ValueError):
                    m["failures"].append({"error": "image_unavailable"})
            media.append(m)
    position = {r["id"]: i for i, r in enumerate(records)}
    media.sort(key=lambda m: position[m["message_id"]])
    for i, m in enumerate(media, 1):
        row = records[position[m["message_id"]]]
        m.update(id=f"I{i:02d}", group_id=row["group_id"], group_name=row["group_name"], sender_id=row["sender_id"])
    merged = work / "messages.jsonl"
    write_records(merged, records)
    save_json(work / "groups.json", groups)
    return records, media, groups
