import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import httpx
from PIL import Image
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

from marketpulse.clawbot import Client, ClawbotError, checked_url, native_upload


class ClawbotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "report.png"
        Image.new("RGB", (40, 120), "white").save(self.path)
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.requests = []
        self.send_result = {"ret": 0, "message_id": "server-123"}

        def handle(request):
            self.requests.append(request)
            path = request.url.path
            if path.endswith("getupdates"):
                return httpx.Response(
                    200,
                    json={
                        "ret": 0,
                        "get_updates_buf": "cursor",
                        "msgs": [
                            {"from_user_id": "stranger", "context_token": "wrong", "message_type": 1},
                            {
                                "from_user_id": "owner",
                                "context_token": "group-wrong",
                                "message_type": 1,
                                "group_id": "group",
                            },
                            {"from_user_id": "owner", "context_token": "owner-context", "message_type": 1},
                        ],
                    },
                )
            if path.endswith("getuploadurl"):
                return httpx.Response(200, json={"ret": 0, "upload_param": "upload-param"})
            if path.endswith("/upload"):
                return httpx.Response(200, headers={"x-encrypted-param": "download-param"})
            if path.endswith("get_bot_qrcode"):
                return httpx.Response(
                    200, json={"qrcode": "qr-secret", "qrcode_img_content": "https://example.test/qr"}
                )
            if path.endswith("sendmessage"):
                if self.send_result == "timeout":
                    raise httpx.ReadTimeout("test timeout", request=request)
                return httpx.Response(200, json=self.send_result)
            raise AssertionError(path)

        self.client = Client(self.root / "state", transport=httpx.MockTransport(handle))
        self.client.session = {
            "token": "private-token",
            "user_id": "owner",
            "bot_id": "bot",
            "base_url": "https://ilinkai.weixin.qq.com",
        }

    async def asyncTearDown(self):
        await self.client.http.aclose()
        self.tmp.cleanup()

    async def test_encrypt_send_owner_and_deduplicate(self):
        with redirect_stdout(io.StringIO()):
            receipt = await self.client.send_image(self.path, self.digest)
            again = await self.client.send_image(self.path, self.digest)
        self.assertEqual(receipt["status"], "submitted")
        self.assertEqual(receipt["server_message_id"], "server-123")
        self.assertTrue(again["duplicate_skipped"])
        self.assertEqual(len(self.requests), 4)
        sent = json.loads(self.requests[-1].content)["msg"]
        self.assertEqual(sent["to_user_id"], "owner")
        self.assertEqual(sent["context_token"], "owner-context")
        self.assertEqual(sent["item_list"][0]["type"], 2)
        image = sent["item_list"][0]["image_item"]
        key = bytes.fromhex(base64.b64decode(image["media"]["aes_key"]).decode())
        dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = dec.update(self.requests[2].content) + dec.finalize()
        unpad = padding.PKCS7(128).unpadder()
        self.assertEqual(unpad.update(padded) + unpad.finalize(), self.path.read_bytes())
        self.assertEqual(image["mid_size"], len(self.requests[2].content))
        self.assertNotIn("authorization", self.requests[2].headers)
        self.assertEqual(self.client.session_path.stat().st_mode & 0o777, 0o600)

    async def test_modified_report_stops_before_network(self):
        with self.assertRaises(ValueError):
            await self.client.send_image(self.path, "0" * 64)
        self.assertEqual(self.requests, [])

    async def test_optional_ret_is_submitted_but_not_delivered(self):
        self.send_result = {}
        with redirect_stdout(io.StringIO()):
            saved = await self.client.send_image(self.path, self.digest)
            again = await self.client.send_image(self.path, self.digest)
        self.assertEqual(saved["status"], "submitted")
        self.assertFalse(saved["delivery_confirmed"])
        self.assertIsNone(saved["api_ret"])
        self.assertEqual(saved["api_response_fields"], [])
        self.assertTrue(again["duplicate_skipped"])
        self.assertEqual(len(self.requests), 4)

    async def test_no_ack_is_uncertain_and_never_auto_repeated(self):
        self.send_result = "timeout"
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(ClawbotError):
                await self.client.send_image(self.path, self.digest)
            with self.assertRaisesRegex(ClawbotError, "未确认"):
                await self.client.send_image(self.path, self.digest)
        saved = self.client.load(self.client.receipt_path(self.digest))
        self.assertEqual(saved["status"], "uncertain")
        self.assertEqual(len(self.requests), 4)

    async def test_user_confirmed_delivery_never_resends(self):
        with redirect_stdout(io.StringIO()):
            await self.client.send_image(self.path, self.digest)
            path = self.client.receipt_path(self.digest)
            record = self.client.load(path)
            record["status"] = "delivered"
            path.write_text(json.dumps(record))
            again = await self.client.send_image(self.path, self.digest)
        self.assertTrue(again["duplicate_skipped"])
        self.assertEqual(len(self.requests), 4)

    async def test_business_error_is_not_success(self):
        self.send_result = {"ret": -2}
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(ClawbotError, "ret=-2"):
            await self.client.send_image(self.path, self.digest)
        self.assertEqual(self.client.load(self.client.receipt_path(self.digest))["status"], "rejected")
        self.assertNotIn("context_token", self.client.session)

    async def test_explicit_rejection_can_retry_after_owner_context_refresh(self):
        self.send_result = {"ret": -2}
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(ClawbotError):
                await self.client.send_image(self.path, self.digest, wait=0)
            self.send_result = {"ret": 0}
            receipt = await self.client.send_image(self.path, self.digest)
        self.assertEqual(receipt["status"], "submitted")
        self.assertEqual(self.client.session["context_token"], "owner-context")
        self.assertEqual(sum(r.url.path.endswith("sendmessage") for r in self.requests), 2)

    async def test_initial_send_can_omit_context_like_official_client(self):
        with redirect_stdout(io.StringIO()):
            receipt = await self.client.send_image(self.path, self.digest, wait=0)
        self.assertEqual(receipt["status"], "submitted")
        self.assertEqual(len(self.requests), 3)
        sent = json.loads(self.requests[-1].content)["msg"]
        self.assertEqual(sent["to_user_id"], "owner")
        self.assertNotIn("context_token", sent)

    async def test_qr_start_has_no_auth_or_base_info_and_is_private(self):
        result = await self.client.login_start()
        request = self.requests[0]
        self.assertEqual(request.url.params["bot_type"], "3")
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("base_info", json.loads(request.content))
        self.assertEqual(Path(result["qr_path"]).stat().st_mode & 0o777, 0o600)

    def test_only_ilink_and_official_cdn_hosts(self):
        self.assertTrue(checked_url("https://ilinkai2.weixin.qq.com/ilink/bot/sendmessage"))
        for url in [
            "https://filehelper.weixin.qq.com/x",
            "https://ilinkai.weixin.qq.com.evil.test/x",
            "http://ilinkai.weixin.qq.com/x",
            "https://evil.test/x",
            "https://user@ilinkai.weixin.qq.com/x",
        ]:
            with self.assertRaises(ClawbotError):
                checked_url(url)
        with self.assertRaises(ClawbotError):
            checked_url("https://ilinkai.weixin.qq.com/upload", cdn=True)

    async def test_native_upload_keeps_signed_url_out_of_argv_and_cleans_files(self):
        url = "https://novac2c.cdn.weixin.qq.com/c2c/upload?encrypted_query_param=secret-param"
        test = self

        async def spawn(*args, **kwargs):
            test.assertNotIn(url, args)
            test.assertFalse(any("secret-param" in str(arg) for arg in args))
            test.assertEqual(args[args.index("--retry") + 1], "0")
            source = Path(args[args.index("--data-binary") + 1][1:])
            test.assertEqual(source.read_bytes(), b"encrypted-data")
            test.assertEqual(source.stat().st_mode & 0o777, 0o600)
            head = Path(args[args.index("--dump-header") + 1])

            class Proc:
                returncode = 0

                async def communicate(self, payload):
                    test.assertIn(url.encode(), payload)
                    head.write_text("HTTP/2 200\r\nx-encrypted-param: download-param\r\n")
                    return b"200", b""

            return Proc()

        with patch("marketpulse.clawbot.asyncio.create_subprocess_exec", side_effect=spawn):
            result = await native_upload(url, b"encrypted-data", self.client.state)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["x-encrypted-param"], "download-param")
        diagnostic = json.loads((self.client.state / "native-upload-diagnostic.json").read_text(encoding="utf-8"))
        self.assertTrue(diagnostic["download_header_present"])
        self.assertNotIn("secret-param", json.dumps(diagnostic))
        self.assertNotIn("download-param", json.dumps(diagnostic))
        self.assertEqual(list(self.client.state.glob(".upload-*")), [])

    async def test_native_upload_records_cdn_error_without_reusing_interim_headers(self):
        async def spawn(*args, **kwargs):
            head = Path(args[args.index("--dump-header") + 1])
            body = Path(args[args.index("--output") + 1])

            class Proc:
                returncode = 0

                async def communicate(self, payload):
                    head.write_text(
                        "HTTP/1.1 100 Continue\r\nx-encrypted-param: stale-secret\r\n\r\n"
                        "HTTP/1.1 500 Internal Server Error\r\nX-ErrNo: -10001\r\n"
                        "X-TaskId: test-task\r\nX-Error-Code: request_timeout\r\nContent-Length: 0\r\n"
                    )
                    body.write_bytes(b"")
                    return b"500\t192.0.2.1\t16\t0.1\t0.2\t10\t10.1", b""

            return Proc()

        with patch("marketpulse.clawbot.asyncio.create_subprocess_exec", side_effect=spawn):
            result = await native_upload(
                "https://novac2c.cdn.weixin.qq.com/c2c/upload?secret=private", b"encrypted-data", self.client.state
            )
        self.assertEqual(result.status_code, 500)
        self.assertIsNone(result.headers.get("x-encrypted-param"))
        diagnostic = json.loads((self.client.state / "native-upload-diagnostic.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["cdn_errno"], "-10001")
        self.assertEqual(diagnostic["error_code"], "request_timeout")
        self.assertEqual(diagnostic["time_total"], "10.1")
        self.assertFalse(diagnostic["download_header_present"])
        self.assertNotIn("private", json.dumps(diagnostic))
        self.assertNotIn("stale-secret", json.dumps(diagnostic))


if __name__ == "__main__":
    unittest.main()
