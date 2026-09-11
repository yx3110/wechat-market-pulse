"""Owner-only ClawBot delivery via Tencent's documented iLink HTTPS protocol.

Protocol reference: Tencent/openclaw-weixin@7c04adc3e95775efd661ab9fba0626d86d237713.
This client never connects to the web FileHelper service or controls desktop WeChat.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from filelock import FileLock
import shutil
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from urllib.parse import urlencode, urlparse

import httpx
import qrcode
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .files import inspect_image, private_write, write_json

BASE = "https://ilinkai.weixin.qq.com"
CDN = "https://novac2c.cdn.weixin.qq.com/c2c"
VERSION = "2.4.9"
STATE = Path.home() / ".config/marketpulse/clawbot"


class ClawbotError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def checked_url(url: str, *, cdn: bool = False) -> str:
    parsed = urlparse(url)
    host_ok = (
        parsed.hostname == "novac2c.cdn.weixin.qq.com"
        if cdn
        else bool(re.fullmatch(r"ilink[a-z0-9-]*\.weixin\.qq\.com", parsed.hostname or ""))
    )
    if parsed.scheme != "https" or not host_ok or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ClawbotError("接口返回了未识别的服务器，已停止连接。")
    return url


def encrypt_media(raw: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(raw) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(padded) + enc.finalize()


async def native_upload(url: str, data: bytes, state: Path, *, retries: int = 0) -> httpx.Response:
    """Use the system curl for CDN transport; signed URLs never enter process argv/logs."""
    checked_url(url, cdn=True)
    with tempfile.TemporaryDirectory(prefix=".upload-", dir=state) as folder:
        root = Path(folder)
        source, headers, body = root / "payload", root / "headers", root / "body"
        private_write(source, data)
        proc = await asyncio.create_subprocess_exec(
            shutil.which("curl") or "curl",
            "--http1.1",
            "--silent",
            "--show-error",
            "--proto",
            "=https",
            "--max-time",
            "120",
            "--retry",
            str(retries),
            "--retry-delay",
            "2",
            "--retry-max-time",
            "240",
            "--connect-timeout",
            "15",
            "--request",
            "POST",
            "--header",
            "Content-Type: application/octet-stream",
            "--data-binary",
            "@" + str(source),
            "--dump-header",
            str(headers),
            "--output",
            str(body),
            "--write-out",
            "%{http_code}\t%{remote_ip}\t%{size_upload}\t%{time_connect}\t%{time_appconnect}\t%{time_starttransfer}\t%{time_total}\t%{http_version}",
            "--config",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, _ = await proc.communicate(("url = " + json.dumps(url) + "\n").encode())
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        fields = output.decode(errors="replace").strip().split("\t")
        pairs = {}
        statuses = []
        for line in (headers.read_text(encoding="utf-8", errors="replace") if headers.exists() else "").splitlines():
            if line.startswith("HTTP/"):
                statuses.append(line)
                pairs = {}
            elif ":" in line:
                name, value = line.split(":", 1)
                pairs[name.strip()] = value.strip()
        lower = {k.lower(): v for k, v in pairs.items()}
        diagnostic = {
            "at": time.time(),
            "curl_exit": proc.returncode,
            "http_status": int(fields[0]) if fields[0].isdigit() else None,
            "response_statuses": statuses,
            "response_header_names": sorted(lower),
            "cdn_errno": lower.get("x-errno"),
            "cdn_rtflag": lower.get("x-rtflag"),
            "error_code": lower.get("x-error-code"),
            "cdn_task_id": lower.get("x-taskid"),
            "server": lower.get("server"),
            "content_type": lower.get("content-type"),
            "content_length": lower.get("content-length"),
            "download_header_present": bool(lower.get("x-encrypted-param")),
            "request_bytes": len(data),
            "body_bytes": body.stat().st_size if body.exists() else 0,
            "body_sha256": hashlib.sha256(body.read_bytes()).hexdigest() if body.exists() else None,
        }
        if len(fields) in (7, 8):
            diagnostic.update(
                dict(
                    zip(
                        (
                            "remote_ip",
                            "size_upload",
                            "time_connect",
                            "time_tls",
                            "time_first_byte",
                            "time_total",
                            "http_version",
                        ),
                        fields[1:],
                    )
                )
            )
        write_json(state / "native-upload-diagnostic.json", diagnostic)
        if proc.returncode or not fields[0].isdigit():
            raise ClawbotError(f"系统网络客户端上传未完成（curl={proc.returncode}），尚未提交消息。")
        return httpx.Response(int(fields[0]), headers=pairs, content=body.read_bytes()[:4096] if body.exists() else b"")


class Client:
    def __init__(self, state: Path, transport=None):
        self.state = state
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.chmod(0o700)
        self.session_path = state / "session.json"
        self.session = self.load(self.session_path)
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(40, connect=10), follow_redirects=False, transport=transport
        )

    @staticmethod
    def load(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def save(self):
        write_json(self.session_path, self.session)

    def require_login(self):
        if not all(self.session.get(k) for k in ("token", "user_id", "bot_id", "base_url")):
            raise ClawbotError("ClawBot 尚未绑定，请先执行 login-start 和 login-wait。")

    async def request(self, path: str, body: dict | None = None, *, auth=True, base=None, get=False) -> dict:
        if auth:
            self.require_login()
        url = checked_url((base or self.session.get("base_url") or BASE).rstrip("/") + "/" + path)
        headers = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": str((2 << 16) | (4 << 8) | 9)}
        if not get:
            headers.update(
                {
                    "Content-Type": "application/json",
                    "AuthorizationType": "ilink_bot_token",
                    "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
                }
            )
        payload = dict(body or {})
        if auth:
            headers["Authorization"] = "Bearer " + self.session["token"]
            payload["base_info"] = {"channel_version": VERSION, "bot_agent": "WeChatMarketPulse/1.0"}
        try:
            response = await self.http.request(
                "GET" if get else "POST", url, headers=headers, **({} if get else {"json": payload})
            )
            if response.status_code != 200:
                raise ClawbotError(f"微信接口 HTTP {response.status_code}，未确认成功。")
            data = response.json()
            if not isinstance(data, dict):
                raise ClawbotError("微信接口返回格式异常。")
            for field in ("ret", "errcode"):
                code = data.get(field)
                if code not in (None, 0):
                    safe_code = str(code) if isinstance(code, (int, float)) else "非零"
                    raise ClawbotError(f"微信接口拒绝请求（{field}={safe_code}）。", code=code)
            return data
        except httpx.TimeoutException:
            raise ClawbotError("微信接口超时，未确认请求结果。") from None
        except (httpx.HTTPError, ValueError):
            raise ClawbotError("微信接口网络或响应异常，网络细节已隐藏。") from None

    async def login_start(self):
        tokens = [self.session["token"]] if self.session.get("token") else []
        result = await self.request(
            "ilink/bot/get_bot_qrcode?bot_type=3", {"local_token_list": tokens}, auth=False, base=BASE
        )
        if not result.get("qrcode") or not result.get("qrcode_img_content"):
            raise ClawbotError("微信没有返回登录二维码。")
        image = qrcode.make(result["qrcode_img_content"], box_size=10, border=4)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        private_write(self.state / "login-qr.png", buf.getvalue())
        write_json(
            self.state / "pending-login.json", {"qrcode": result["qrcode"], "created_at": time.time(), "base_url": BASE}
        )
        return {"status": "scan_required", "qr_path": str(self.state / "login-qr.png")}

    async def login_wait(self, timeout: int):
        pending_path = self.state / "pending-login.json"
        pending = self.load(pending_path)
        if not pending.get("qrcode") or time.time() - pending["created_at"] >= 300:
            raise ClawbotError("请重新生成 ClawBot 登录二维码。")
        deadline = min(time.time() + timeout, pending["created_at"] + 300)
        previous_status = None
        while time.time() < deadline:
            query = urlencode({"qrcode": pending["qrcode"]})
            result = await self.request(
                "ilink/bot/get_qrcode_status?" + query, auth=False, get=True, base=pending["base_url"]
            )
            status = result.get("status", "wait")
            if status != previous_status:
                print(json.dumps({"login_status": status}), flush=True)
                previous_status = status
            if status == "confirmed":
                if not all(result.get(k) for k in ("bot_token", "ilink_bot_id", "ilink_user_id", "baseurl")):
                    raise ClawbotError("登录确认缺少账号信息，未保存会话。")
                self.session = {
                    "token": result["bot_token"],
                    "bot_id": result["ilink_bot_id"],
                    "user_id": result["ilink_user_id"],
                    "base_url": checked_url(result["baseurl"]),
                    "bound_at": time.time(),
                    "cursor": "",
                }
                self.save()
                pending_path.unlink(missing_ok=True)
                (self.state / "login-qr.png").unlink(missing_ok=True)
                return {"status": "logged_in", "recipient": "扫码绑定的本人 ClawBot 会话"}
            if status == "scaned_but_redirect":
                pending["base_url"] = checked_url("https://" + result.get("redirect_host", ""))
                write_json(pending_path, pending)
            elif status == "binded_redirect" and self.session.get("token"):
                return {"status": "already_bound"}
            elif status not in ("wait", "scaned", "scaned_but_redirect"):
                raise ClawbotError("二维码需要刷新或手机要求额外验证，已停止等待。")
            await asyncio.sleep(1)
        return {"status": "waiting_for_scan"}

    async def wait_context(self, timeout: int):
        self.require_login()
        if self.session.get("context_token"):
            return
        print("请在手机微信的 ClawBot 会话发一条消息（例如：发送报告），用于绑定接收会话。", flush=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = await self.request("ilink/bot/getupdates", {"get_updates_buf": self.session.get("cursor", "")})
            if result.get("get_updates_buf"):
                self.session["cursor"] = result["get_updates_buf"]
            for msg in result.get("msgs", []):
                if (
                    msg.get("from_user_id") == self.session["user_id"]
                    and not msg.get("group_id")
                    and msg.get("message_type") == 1
                    and msg.get("context_token")
                ):
                    self.session["context_token"] = msg["context_token"]
                    self.session["context_at"] = time.time()
            self.save()
            if self.session.get("context_token"):
                return
            await asyncio.sleep(1)
        raise ClawbotError("等待 ClawBot 会话消息超时，图片尚未发送。")

    def receipt_path(self, digest: str) -> Path:
        account = hashlib.sha256((self.session["bot_id"] + ":" + self.session["user_id"]).encode()).hexdigest()[:16]
        return self.state / "receipts" / account / (digest + ".json")

    async def send_image(self, path: Path, expected_sha256: str, wait: int = 240, use_native_upload=False):
        self.require_login()
        info = inspect_image(path, expected_sha256)
        info["recipient"] = "bound_owner_clawbot"
        receipt = self.receipt_path(info["sha256"])
        old = self.load(receipt)
        if old.get("status") in ("submitted", "delivered"):
            return dict(old, duplicate_skipped=True)
        if old and old.get("status") != "rejected":
            raise ClawbotError("该报告已有未确认发送记录，请先在 ClawBot 核对，程序不会自动重发。")
        if wait > 0:
            await self.wait_context(wait)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != info["sha256"]:
            raise ClawbotError("报告核对后发生变化，已停止发送。")
        key = secrets.token_bytes(16)
        encrypted = encrypt_media(raw, key)
        filekey = secrets.token_hex(16)
        upload = await self.request(
            "ilink/bot/getuploadurl",
            {
                "filekey": filekey,
                "media_type": 1,
                "to_user_id": self.session["user_id"],
                "rawsize": len(raw),
                "rawfilemd5": hashlib.md5(raw).hexdigest(),
                "filesize": len(encrypted),
                "no_need_thumb": True,
                "aeskey": key.hex(),
            },
        )
        upload_url = upload.get("upload_full_url")
        if not upload_url and upload.get("upload_param"):
            upload_url = (
                CDN + "/upload?" + urlencode({"encrypted_query_param": upload["upload_param"], "filekey": filekey})
            )
        if not upload_url:
            raise ClawbotError("微信没有返回图片上传地址。")
        checked_url(upload_url, cdn=True)
        print("正在上传已核对的总结长图。", flush=True)
        try:
            if use_native_upload:
                response = await native_upload(upload_url, encrypted, self.state)
            else:
                response = await self.http.post(
                    upload_url,
                    content=encrypted,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=httpx.Timeout(120, connect=15),
                )
        except httpx.HTTPError as exc:
            raise ClawbotError(f"图片上传未完成（{type(exc).__name__}），尚未提交消息。") from None
        download_param = response.headers.get("x-encrypted-param")
        if response.status_code != 200 or not download_param:
            details = response.headers.get("x-error-message", "")
            if not details:
                try:
                    err = response.json()
                    details = (
                        str(err.get("errmsg") or err.get("message") or err.get("error") or "")
                        if isinstance(err, dict)
                        else ""
                    )
                except ValueError:
                    pass
            details = re.sub(r"https?://\S+|[A-Za-z0-9_+/=-]{24,}", "[redacted]", details)[:240]
            write_json(
                self.state / "upload-diagnostic.json",
                {
                    "http_status": response.status_code,
                    "download_header_present": bool(download_param),
                    "error_description": details,
                    "cdn_errno": response.headers.get("x-errno"),
                    "error_code": response.headers.get("x-error-code"),
                    "cdn_task_id": response.headers.get("x-taskid"),
                    "at": time.time(),
                },
            )
            error_code = response.headers.get("x-error-code") or response.headers.get("x-errno", "未提供")
            raise ClawbotError(
                f"图片上传未确认（HTTP={response.status_code}，CDN错误码={error_code}，下载回执={bool(download_param)}），尚未提交消息。"
            )
        client_id = "marketpulse:" + secrets.token_hex(16)
        record = dict(info, status="pending", client_id=client_id, at=time.time())
        write_json(receipt, record)
        try:
            result = await self.request(
                "ilink/bot/sendmessage",
                {
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": self.session["user_id"],
                        "client_id": client_id,
                        "message_type": 2,
                        "message_state": 2,
                        **(
                            {"context_token": self.session["context_token"]}
                            if self.session.get("context_token")
                            else {}
                        ),
                        "item_list": [
                            {
                                "type": 2,
                                "image_item": {
                                    "media": {
                                        "encrypt_query_param": download_param,
                                        "aes_key": base64.b64encode(key.hex().encode()).decode(),
                                        "encrypt_type": 1,
                                    },
                                    "mid_size": len(encrypted),
                                },
                            }
                        ],
                    }
                },
            )
            record["api_response_fields"] = sorted(result)
            record["api_ret"] = result.get("ret")
            if result.get("message_id"):
                record["server_message_id"] = str(result["message_id"])
            # The official sendMessage contract makes ret optional, including {}.
            # request() already rejects nonzero business errors. This is submission,
            # not a delivery/read receipt; do not synthesize ret=0 or auto-resend.
        except ClawbotError as exc:
            status = "rejected" if exc.code is not None else "uncertain"
            write_json(receipt, dict(record, status=status, api_error=exc.code))
            if exc.code == -2:
                self.session.pop("context_token", None)
                self.session.pop("context_at", None)
                self.save()
            raise
        except BaseException:
            write_json(receipt, dict(record, status="uncertain"))
            raise
        record.update(
            status="submitted",
            delivery_confirmed=False,
            acknowledgement="explicit_ret_zero" if result.get("ret") == 0 else "optional_ret_absent",
        )
        if result.get("message_id"):
            record["server_message_id"] = str(result["message_id"])
        write_json(receipt, record)
        return record


async def run(args):
    client = Client(args.state_dir.expanduser().resolve())
    try:
        with FileLock(str(client.state / ".lock"), timeout=0):
            if args.command == "login-start":
                result = await client.login_start()
            elif args.command == "login-wait":
                result = await client.login_wait(args.timeout)
            elif args.command == "send":
                result = await client.send_image(
                    args.image.expanduser().resolve(), args.sha256, args.wait_context, args.native_upload
                )
            else:
                result = {
                    "session_saved": bool(client.session.get("token")),
                    "context_saved": bool(client.session.get("context_token")),
                    "recipient": "bound_owner_clawbot",
                    "online_verified": False,
                }
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    finally:
        await client.http.aclose()


def main():
    p = argparse.ArgumentParser(description="腾讯官方 ClawBot 通道：绑定本人、发送已核对的 PNG 长图")
    p.add_argument("--state-dir", type=Path, default=STATE)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("login-start")
    login = sub.add_parser("login-wait")
    login.add_argument("--timeout", type=int, default=240)
    sub.add_parser("status")
    send = sub.add_parser("send")
    send.add_argument("image", type=Path)
    send.add_argument("--sha256", required=True)
    send.add_argument("--wait-context", type=int, default=240)
    send.add_argument("--native-upload", action="store_true", help="使用系统 curl 上传到微信 CDN")
    args = p.parse_args()
    os.umask(0o077)
    try:
        asyncio.run(run(args))
    except (ClawbotError, ValueError) as exc:
        p.exit(1, str(exc) + "\n")
    except KeyboardInterrupt:
        p.exit(130, "已停止；如消息提交已开始，请先查看 ClawBot 再决定是否重试。\n")
    except Exception:
        p.exit(1, "ClawBot 请求未完成，具体网络内容已隐藏。\n")


if __name__ == "__main__":
    main()
