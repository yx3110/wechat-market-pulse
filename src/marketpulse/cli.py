"""Human and agent entry point. --json emits machine-readable results."""

from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys


def parser():
    from .presets import PRESETS

    p = argparse.ArgumentParser(description="微信群文字与图片综合简报 · Windows / macOS")
    p.add_argument("--home", help="私有运行目录，默认系统用户数据目录")
    p.add_argument("--config", help="配置 JSON 路径")
    p.add_argument("--json", action="store_true", help="标准输出只打印 JSON；进度写标准错误")
    sub = p.add_subparsers(dest="command", required=True)

    def backend(q):
        q.add_argument("--provider", choices=PRESETS)
        q.add_argument("--model")
        q.add_argument("--base-url")
        q.add_argument("--api-key-env")
        q.add_argument("--supports-images", action="store_true")
        q.add_argument("--vision-provider", choices=PRESETS)
        q.add_argument("--vision-model")
        q.add_argument("--vision-base-url")
        q.add_argument("--max-input-chars", type=int)
        q.add_argument("--fallback-rules", action="store_true")

    init = sub.add_parser("init", help="创建配置；默认本地规则模式")
    backend(init)
    init.add_argument("--force", action="store_true")
    init.add_argument("--key-config")
    init.add_argument("--group-id", action="append", default=[])
    setup = sub.add_parser("setup-wechat", help="显式初始化本机已登录微信的只读访问")
    setup.add_argument("--account-dir")
    setup.add_argument("--key-config")
    setup.add_argument("--yes", action="store_true")
    doctor = sub.add_parser("doctor", help="检查环境；不打印密钥")
    doctor.add_argument("--deep", action="store_true", help="逐库验证已同步的数据库密钥")
    sub.add_parser("models", help="列出本地、云端与规则模式的配置预设")
    groups = sub.add_parser("groups", help="列出本机群聊与稳定群 ID")
    groups.add_argument("--prefix", default="")
    for command in ("report", "watch", "start"):
        q = sub.add_parser(
            command, help={"report": "生成报告", "watch": "前台持续更新", "start": "后台持续更新"}[command]
        )
        q.add_argument("--group-id", action="append")
        q.add_argument("--date")
        q.add_argument("--input")
        q.add_argument(
            "--period", choices=["all", "premarket", "morning", "lunch", "afternoon", "afterhours"], default="all"
        )
        q.add_argument("--no-refresh", action="store_true")
        q.add_argument("--output")
        backend(q)
    sub.add_parser("stop", help="停止本项目后台任务")
    sub.add_parser("status", help="读取后台任务状态")
    export = sub.add_parser("export", help="只导出指定群，不请求模型")
    export.add_argument("--group-id", required=True)
    export.add_argument("--date")
    export.add_argument("--output", required=True)
    for command in ("render", "refresh-names"):
        q = sub.add_parser(command)
        q.add_argument("--report", required=True)
        q.add_argument("--input")
    demo = sub.add_parser("demo", help="无需微信、密钥或网络，运行虚构多群示例")
    demo.add_argument("--output")
    send = sub.add_parser("send", help="经 Taildrop 发送指定原文件，不压缩")
    send.add_argument("file")
    send.add_argument("--target", required=True)
    send.add_argument("--sha256", required=True)
    return p


def run(args):
    # Home must be set before importing modules with state-path defaults.
    if args.home:
        os.environ["WECHAT_PULSE_HOME"] = str(Path(args.home).expanduser().resolve())
    from .core import ROOT, TZ, config, now, digest
    from .briefing import save_json
    from .bulk import KEY_CONFIG
    from .presets import PRESETS, apply_backend
    from datetime import datetime

    config_path = Path(args.config).expanduser() if args.config else ROOT / "config.json"
    if args.command == "models":
        return PRESETS
    if args.command == "init":
        if config_path.exists() and not args.force:
            raise ValueError("配置已存在；编辑原配置或用 --force 显式覆盖")
        cfg = dict(
            PRESETS["rules"],
            model=None,
            groups=args.group_id,
            key_config=str(Path(args.key_config).expanduser() if args.key_config else KEY_CONFIG),
            poll_seconds=30,
            analysis_seconds=300,
            max_input_chars=60000,
            model_timeout=360,
        )
        apply_backend(cfg, args)
        if cfg["provider"] not in ("rules", "codex") and not cfg.get("model"):
            raise ValueError("所选 provider 需要 --model")
        save_json(config_path, cfg)
        return dict(
            config=str(config_path),
            provider=cfg["provider"],
            next=["wechat-pulse doctor", "wechat-pulse setup-wechat", "wechat-pulse groups"],
        )
    if args.command == "demo":
        from .demo import run_demo

        return run_demo(Path(args.output or ROOT / "demo"))
    if args.command == "setup-wechat":
        from .bootstrap import discover_accounts, setup_mac, setup_windows, doctor

        existing = config(config_path) if config_path.exists() else {}
        key_config = Path(args.key_config or existing.get("key_config", KEY_CONFIG)).expanduser()
        if key_config.exists():
            ready = doctor(key_config)
            if ready["ready"]:
                return ready | {"message": "数据库访问已就绪，无需重新初始化"}
        if not args.yes:
            if not sys.stdin.isatty():
                raise ValueError("首次初始化请阅读 docs/INSTALL.md，确认后添加 --yes")
            print("将读取当前用户已登录微信的本机密钥。macOS 首次可能重开辅助副本并显示系统授权框。", file=sys.stderr)
            if input("开始初始化？[y/N] ").lower() != "y":
                return {"status": "cancelled"}
        account = Path(args.account_dir).expanduser() if args.account_dir else None
        if account is None:
            accounts = discover_accounts()
            if len(accounts) == 1:
                account = accounts[0]
            elif len(accounts) > 1:
                raise ValueError("发现多个账号目录，请用 --account-dir 指定当前登录账号")
        if sys.platform == "darwin":
            return setup_mac(account, key_config)
        if sys.platform == "win32":
            if account is None:
                raise ValueError("未发现账号目录，请用 --account-dir 指向含 db_storage 的文件夹")
            return setup_windows(account, key_config)
        raise ValueError("微信实时初始化支持 Windows/macOS；其他系统可用 demo 或 --input JSONL")
    if args.command == "doctor":
        import importlib.util, shutil, platform
        from .fonts import font_path

        info = dict(
            python=platform.python_version(),
            system=platform.system(),
            config_present=config_path.is_file(),
            sqlcipher_ready=True,
            font_ready=Path(font_path()).is_file(),
            codex_cli=bool(shutil.which("codex")),
            hevc_ready=importlib.util.find_spec("av") is not None,
            offline_ready=True,
        )
        cfg = config(config_path) if config_path.exists() else {}
        key_config = Path(cfg.get("key_config", KEY_CONFIG)).expanduser()
        info["wechat_key_config_present"] = key_config.is_file()
        if args.deep:
            from .bootstrap import doctor

            info["database_access"] = doctor(key_config)
        if not info["wechat_key_config_present"]:
            info["next"] = "wechat-pulse setup-wechat；也可以先运行 wechat-pulse demo"
        return info
    if args.command == "send":
        from .delivery import send

        return send(args.file, args.target, args.sha256)
    if args.command in ("stop", "status"):
        from .watch import service

        return service(args.command)
    cfg = config(config_path)
    if args.command == "groups":
        from .bulk import read_config, snapshot, key_for, _contact_rows

        root, keys = read_config(cfg.get("key_config", KEY_CONFIG))
        with snapshot(root / "contact/contact.db") as copy:
            rows = _contact_rows(copy, key_for(copy, keys))
        return [
            dict(id=r["username"], name=r.get("remark") or r.get("nick_name") or r["username"])
            for r in rows
            if r["username"]
            and r["username"].endswith("@chatroom")
            and (r.get("remark") or r.get("nick_name") or "").startswith(args.prefix)
        ]
    if args.command == "export":
        from .bulk import export_group

        since = int(datetime.fromisoformat(args.date).replace(tzinfo=TZ).timestamp()) if args.date else None
        manifest, _ = export_group(
            "", args.output, username=args.group_id, since=since, key_config=cfg.get("key_config", KEY_CONFIG)
        )
        return manifest
    if args.command in ("render", "refresh-names"):
        from .briefing_render import render

        path = Path(args.report).expanduser()
        path = path / "briefing.json" if path.is_dir() else path
        result = json.loads(path.read_text(encoding="utf-8"))
        changes = []
        if args.command == "refresh-names":
            from .briefing import refresh_record_names, refresh_attributions
            from .bulk import read_group_names

            source = Path(args.input or result.get("source_snapshot", ""))
            if not source.is_file():
                raise ValueError("缺少原报告消息快照；用 --input 指定原始 messages.jsonl")
            records = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
            for gid in {r["group_id"] for r in records}:
                rows = [r for r in records if r["group_id"] == gid]
                refresh_record_names(rows, read_group_names(gid, key_config=cfg.get("key_config", KEY_CONFIG)))
            changes = refresh_attributions(result, records)
            save_json(path, result)
        image = render(result, path.parent)
        return dict(image=str(image), updated_members=len(changes))
    apply_backend(cfg, args)
    cfg.update(period=args.period)
    if args.input:
        cfg["input"] = str(Path(args.input).expanduser().resolve())
    gids = args.group_id or [g.get("bulk_group_id") if isinstance(g, dict) else g for g in cfg.get("groups", [])]
    date = args.date or datetime.now(TZ).date().isoformat()
    if args.command in ("watch", "start"):
        from .watch import watch, start

        return start(cfg, gids, args) if args.command == "start" else watch(cfg, gids, args.date, args.output)
    from .briefing import generate
    from .briefing_render import render

    scope = digest([sorted(gids), cfg.get("input"), args.period])[:16]
    work = ROOT / "data/holistic" / scope / date
    out = Path(args.output).expanduser() if args.output else ROOT / "reports" / date / scope
    progress = lambda *a, **k: print(*a, file=sys.stderr, **k)
    result, out = generate(date, gids, cfg, out, not args.no_refresh, progress, work)
    image = render(result, out)
    return dict(
        image=str(image),
        report=str(out / "briefing.json"),
        as_of=result["as_of"],
        method=result.get("analysis_method"),
        coverage=result["coverage"],
    )


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        result = run(args)
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "已停止"}, ensure_ascii=False))
        raise SystemExit(130)
    except Exception as exc:
        # Avoid traceback/payload leaks for external providers and database failures.
        message = (
            str(exc)
            if isinstance(exc, (ValueError, RuntimeError))
            else type(exc).__name__ + "；运行 doctor 并查看安装文档"
        )
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
