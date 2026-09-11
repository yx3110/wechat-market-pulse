"""Application state is outside the installed package and source checkout."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from platformdirs import user_data_dir

ROOT = Path(os.environ.get("WECHAT_PULSE_HOME", user_data_dir("wechat-market-pulse", appauthor=False))).expanduser()
TZ = ZoneInfo("Asia/Shanghai")
PERIODS = ("all", "premarket", "morning", "lunch", "afternoon", "afterhours")


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def period_at(value):
    t = datetime.fromisoformat(value).astimezone(TZ).time()
    if t < time(9, 30):
        return "premarket"
    if t < time(11, 30):
        return "morning"
    if t < time(13):
        return "lunch"
    if t < time(15):
        return "afternoon"
    return "afterhours"


def config(path=None):
    path = Path(path or ROOT / "config.json")
    if not path.is_file():
        raise ValueError("请先运行 wechat-pulse init")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("provider") not in ("codex", "openai", "openai-compatible", "ollama", "rules"):
        raise ValueError("未知 provider；运行 wechat-pulse models 查看选择")
    if data["provider"] not in ("codex", "rules") and not data.get("model"):
        raise ValueError("当前 provider 需要指定 model")
    for field, minimum in [("poll_seconds", 5), ("analysis_seconds", 60)]:
        if not isinstance(data.get(field), int) or data[field] < minimum:
            raise ValueError(f"{field} 必须 >= {minimum}")
    return data
