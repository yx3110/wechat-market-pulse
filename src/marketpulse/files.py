"""Private atomic files and PNG validation, shared by optional delivery adapters."""

import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from PIL import Image


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".private-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        path.chmod(0o600)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, value):
    private_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def inspect_image(path, expected_sha256=None):
    path = Path(path).expanduser().resolve(strict=True)
    raw = path.read_bytes()
    if not raw or len(raw) > 25 * 1024 * 1024:
        raise ValueError("此客户端只接受 25 MB 以内的 PNG 原文件")
    sha = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and sha != expected_sha256:
        raise ValueError("文件与已核对的 SHA-256 不一致，未发送")
    with Image.open(io.BytesIO(raw)) as im:
        if im.format != "PNG" or path.suffix.lower() != ".png":
            raise ValueError("此入口只接受 PNG 图片")
        size = im.size
        im.verify()
    return dict(
        image=str(path), sha256=sha, bytes=len(raw), width=size[0], height=size[1], recipient="bound_owner_clawbot"
    )
