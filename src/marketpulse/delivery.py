"""Optional Taildrop delivery of an explicitly chosen original file."""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from filelock import FileLock
from .core import ROOT, now


def send(path, target, sha256):
    from .briefing import save_json

    path = Path(path).expanduser().resolve()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", target) or target.startswith("-"):
        raise ValueError("无效 Taildrop 目标")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != sha256.lower():
        raise ValueError("文件与核对过的 SHA-256 不一致，未发送")
    binary = shutil.which("tailscale")
    if not binary:
        raise RuntimeError("请安装并登录 Tailscale，或直接在 reports 目录打开/复制原图")
    receipt_path = ROOT / "deliveries" / hashlib.sha256((target + actual).encode()).hexdigest()
    receipt_path = receipt_path.with_suffix(".json")
    receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(str(receipt_path) + ".lock", timeout=0):
        if receipt_path.exists():
            old = json.loads(receipt_path.read_text(encoding="utf-8"))
            if old.get("status") in ("transfer_completed", "uncertain"):
                return old
        receipt = dict(
            channel="tailscale-taildrop",
            status="uncertain",
            at=now(),
            filename=path.name,
            sha256=actual,
            bytes=path.stat().st_size,
            target=target,
        )
        save_json(receipt_path, receipt)
        try:
            result = subprocess.run(
                [binary, "file", "cp", "--update-interval=0", str(path), target.rstrip(":") + ":"],
                capture_output=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("传输状态未确认；保留 uncertain 回执，不自动重发") from None
        receipt.update(status="transfer_completed" if result.returncode == 0 else "failed", exit_code=result.returncode)
        save_json(receipt_path, receipt)
        if result.returncode:
            raise RuntimeError("Taildrop 传输失败；核对 tailscale file cp --targets")
        return receipt
