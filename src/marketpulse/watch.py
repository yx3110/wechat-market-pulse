"""Single worker per application home; polling never starts overlapping model jobs."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

from filelock import FileLock
from .core import ROOT, TZ, digest, now


def watch(cfg, gids, fixed_date=None, output=None):
    from .briefing import generate, save_json
    from .briefing_render import render
    from .inputs import collect

    stop = threading.Event()
    status = ROOT / "watch-status.json"
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)

    def shutdown(*_):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown)
    scope = digest([sorted(gids), cfg.get("input"), cfg.get("period", "all")])[:16]
    state = dict(pid=os.getpid(), started_at=now(), state="running", groups=len(gids))
    worker = None
    last = 0.0
    previous = None

    def update():
        date = fixed_date or datetime.now(TZ).date().isoformat()
        out = Path(output) if output else ROOT / "reports" / date / scope
        try:
            result, destination = generate(
                date,
                gids,
                cfg,
                out,
                refresh=False,
                work_dir=ROOT / "data/holistic" / scope / date,
                progress=lambda *a, **k: print(*a, file=sys.stderr, **k),
            )
            path = render(result, destination)
            state.update(last_report=str(path), last_success=now(), as_of=result["as_of"], error=None)
        except Exception as exc:
            state.update(error=type(exc).__name__, last_failure=now())
            print(f"本轮未发布：{type(exc).__name__}；下一轮重试。", file=sys.stderr, flush=True)
        save_json(status, state)

    with FileLock(str(ROOT / "watch.lock"), timeout=0):
        (ROOT / "watch-stop.json").unlink(missing_ok=True)
        save_json(status, state)
        try:
            while not stop.is_set():
                if (ROOT / "watch-stop.json").exists():
                    break
                date = fixed_date or datetime.now(TZ).date().isoformat()
                if not cfg.get("input"):
                    try:
                        state.update(
                            collected_messages=collect(date, gids, cfg, ROOT / "data/holistic" / scope / date),
                            last_collection=now(),
                            collection_error=None,
                        )
                    except Exception as exc:
                        state.update(collection_error=type(exc).__name__)
                        save_json(status, state)
                        stop.wait(min(cfg.get("poll_seconds", 30), 30))
                        continue
                    save_json(status, state)
                if worker is None or not worker.is_alive():
                    if time.monotonic() - last >= cfg.get("analysis_seconds", 300) or previous != date:
                        last = time.monotonic()
                        previous = date
                        worker = threading.Thread(target=update)
                        worker.start()
                stop.wait(min(cfg.get("poll_seconds", 30), 30))
        finally:
            if worker:
                worker.join()
            state.update(state="stopped", stopped_at=now())
            save_json(status, state)
    return state


def service(action):
    import psutil

    path = ROOT / "watch-status.json"
    if not path.exists():
        return {"state": "stopped"}
    state = json.loads(path.read_text(encoding="utf-8"))
    try:
        proc = psutil.Process(state["pid"])
        cmd = proc.cmdline()
        valid = "marketpulse" in cmd and "watch" in cmd and proc.username() == psutil.Process().username()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        valid = False
    if action == "stop" and valid:
        from .briefing import save_json

        save_json(ROOT / "watch-stop.json", {"pid": proc.pid, "requested_at": now()})
        return {"state": "stopping", "message": "等待当前模型任务完成后退出"}
    return state | {"state": state.get("state", "running") if valid else "stopped"}


def start(cfg, gids, args):
    from .briefing import save_json

    current = service("status")
    if current.get("state") == "running":
        return current
    runtime = ROOT / "watch.local.json"
    save_json(runtime, {**cfg, "groups": gids})
    command = [sys.executable, "-m", "marketpulse", "--home", str(ROOT), "--config", str(runtime), "watch"]
    if args.date:
        command += ["--date", args.date]
    if args.output:
        command += ["--output", str(Path(args.output).resolve())]
    command += ["--period", cfg.get("period", "all")]
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with (ROOT / "watch.log").open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)
    time.sleep(0.5)
    if proc.poll() is not None:
        raise RuntimeError("后台任务启动失败；检查 watch.log")
    return {"state": "running", "pid": proc.pid, "report_seconds": cfg.get("analysis_seconds", 300)}
