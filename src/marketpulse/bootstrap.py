"""Explicit own-account initialization; normal report reads never scan processes."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from .bulk import KEY_CONFIG, read_config, snapshot, key_for, query, message_shards
from .core import ROOT


def discover_accounts():
    home = Path.home()
    roots = [
        home / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files",
        home / "Documents/xwechat_files",
    ]
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            ) as key:
                documents = os.path.expandvars(winreg.QueryValueEx(key, "Personal")[0])
                roots.append(Path(documents) / "xwechat_files")
        except OSError:
            pass
        folder = Path(os.environ.get("APPDATA", home)) / "Tencent/xwechat/config"
        for path in folder.glob("*.ini"):
            try:
                content = path.read_text(encoding="utf-8", errors="ignore").strip("\x00\r\n ")
                if content and "\n" not in content and len(content) < 2000:
                    roots.extend([Path(content), Path(content) / "xwechat_files"])
            except OSError:
                pass
    result = set()
    for root in roots:
        if root.is_dir():
            for account in root.iterdir():
                if (account / "db_storage/contact/contact.db").is_file():
                    result.add(account.resolve())
    return sorted(result)


def verify_page(raw_key, page):
    if len(raw_key) != 32 or len(page) != 4096:
        return False
    mac_salt = bytes(b ^ 0x3A for b in page[:16])
    mac_key = hashlib.pbkdf2_hmac("sha512", raw_key, mac_salt, 2, 32)
    expected = hmac.new(mac_key, page[16:4032] + struct.pack("<I", 1), "sha512").digest()
    return hmac.compare_digest(expected, page[4032:])


def validated_candidates(chunk, pages):
    result = {}
    for candidate in re.findall(rb"x'([0-9a-fA-F]{96})'", chunk):
        raw = bytes.fromhex(candidate.decode("ascii"))
        salt = raw[32:].hex()
        if salt in pages and verify_page(raw[:32], pages[salt]):
            result[salt] = raw[:32].hex()
    return result


def windows_memory_chunks(pid, deadline):
    """Read-only Windows API, scoped to a verified same-user WeChat process."""
    if sys.platform != "win32":
        raise RuntimeError("Windows 初始化只能在 Windows 上运行")
    import ctypes as c
    from ctypes import wintypes as w

    if c.sizeof(c.c_void_p) != 8:
        raise RuntimeError("请使用 64 位 Python")

    class MBI(c.Structure):
        _fields_ = [
            ("base", c.c_void_p),
            ("allocation", c.c_void_p),
            ("allocation_protect", w.DWORD),
            ("partition", w.WORD),
            ("size", c.c_size_t),
            ("state", w.DWORD),
            ("protect", w.DWORD),
            ("kind", w.DWORD),
        ]

    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.VirtualQueryEx.argtypes = [w.HANDLE, c.c_void_p, c.POINTER(MBI), c.c_size_t]
    kernel.VirtualQueryEx.restype = c.c_size_t
    kernel.ReadProcessMemory.argtypes = [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)]
    kernel.ReadProcessMemory.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        raise RuntimeError("无法只读访问微信进程；请确认微信与终端由同一用户、同一权限级别运行")
    try:
        address = 0
        while time.monotonic() < deadline:
            info = MBI()
            if not kernel.VirtualQueryEx(handle, address, c.byref(info), c.sizeof(info)):
                break
            base, size = info.base or 0, info.size
            if not size or base + size <= address:
                break
            address = base + size
            if info.state != 0x1000 or info.protect & 0x101 or info.protect & 0xFF not in (0x04, 0x08, 0x40, 0x80):
                continue
            offset, carry = 0, b""
            while offset < size and time.monotonic() < deadline:
                amount = min(2 * 1024 * 1024, size - offset)
                buffer = c.create_string_buffer(amount)
                read = c.c_size_t()
                kernel.ReadProcessMemory(handle, base + offset, buffer, amount, c.byref(read))
                if read.value:
                    data = carry + buffer.raw[: read.value]
                    yield data
                    carry = data[-128:]
                else:
                    carry = b""
                offset += amount
    finally:
        kernel.CloseHandle(handle)


def _image_samples(account):
    samples = []
    for i, path in enumerate((account / "msg/attach").glob("*/*/Img/*_t.dat")):
        if i >= 500 or len(samples) >= 8:
            break
        try:
            data = path.read_bytes()
            if data.startswith(b"\x07\x08V2\x08\x07") and len(data) > 31:
                samples.append(data)
        except OSError:
            pass
    return samples


def verified_image_key(chunk, samples, seen):
    from Crypto.Cipher import AES
    from .media import decode_dat, image_from_bytes

    for match in re.finditer(rb"(?<![A-Za-z0-9])[A-Za-z0-9]{16}(?:[A-Za-z0-9]{16})?(?![A-Za-z0-9])", chunk):
        key = match[0][:16]
        if key in seen:
            continue
        seen.add(key)
        for sample in samples:
            header = AES.new(key, AES.MODE_ECB).decrypt(sample[15:31])
            if not header.startswith((b"\xff\xd8\xff", b"\x89PNG", b"wxgf")):
                continue
            xor_size = struct.unpack("<I", sample[10:14])[0]
            if xor_size < 2:
                continue  # This sample cannot establish the account's XOR key.
            if header.startswith(b"\xff\xd8\xff"):
                guess = sample[-2] ^ 0xFF
                guesses = [guess] if sample[-1] ^ guess == 0xD9 else []
            elif header.startswith(b"\x89PNG") and xor_size >= 8:
                ending = b"IEND\xaeB`\x82"
                guess = sample[-1] ^ ending[-1]
                guesses = [guess] if bytes(b ^ guess for b in sample[-8:]) == ending else []
            else:
                continue
            for xor in guesses:
                try:
                    image_from_bytes(decode_dat(sample, key, xor))
                    return key.decode("ascii"), xor
                except (ValueError, OSError, ImportError, StopIteration):
                    pass
    return None


def setup_windows(account, destination=KEY_CONFIG, timeout=180):
    import psutil
    from .briefing import save_json

    account = Path(account).expanduser().resolve()
    root = account / "db_storage" if (account / "db_storage").is_dir() else account
    if not (root / "contact/contact.db").is_file():
        raise ValueError("目录不是微信 4.x 账号数据目录")
    account = root.parent
    pages = {}
    for path in root.rglob("*.db"):
        with path.open("rb") as file:
            page = file.read(4096)
        if len(page) == 4096:
            pages[page[:16].hex()] = page
    owner = psutil.Process().username().casefold()
    pids = []
    for proc in psutil.process_iter(["name", "username"]):
        try:
            if (proc.info["name"] or "").casefold() in ("wechat.exe", "weixin.exe") and (
                proc.info["username"] or ""
            ).casefold() == owner:
                pids.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if not pids:
        raise RuntimeError("没有当前用户的已运行微信进程；登录微信并打开一个聊天后重试")
    deadline = time.monotonic() + timeout
    keys = {}
    samples = _image_samples(account)
    image = None
    seen = set()
    for pid in pids:
        for chunk in windows_memory_chunks(pid, deadline):
            keys.update(validated_candidates(chunk, pages))
            if samples and image is None and len(seen) < 500000:
                image = verified_image_key(chunk, samples, seen)
            if len(keys) == len(pages) and (not samples or image):
                break
    if not keys:
        raise RuntimeError("未取得经数据库验证的密钥；请打开聊天再重试，或使用已有 JSONL 导出；此微信版本可能未适配")
    state = {"schema_version": 2, "db_root": str(account), "keys": keys}
    if image:
        state.update(image_key=image[0], image_xor_key=image[1])
    save_json(destination, state)
    return dict(verified_databases=len(keys), databases=len(pages), image_key_ready=bool(image))


def setup_mac(account=None, destination=KEY_CONFIG):
    if sys.platform != "darwin":
        raise RuntimeError("macOS 初始化只能在 macOS 上运行")
    from .briefing import save_json

    if not shutil.which("go") or not shutil.which("git"):
        raise RuntimeError("首次构建需要 Go 1.26.5+ 和 Git；参见 docs/INSTALL.md")
    binary = ROOT / "tools/wxkey"
    binary.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not binary.exists():
        revision = "9b70eecdde47a7172b19465c3f977c86b6050e8a"
        with tempfile.TemporaryDirectory(prefix="wechat-pulse-setup-") as tmp:
            tmp = Path(tmp)
            archive = tmp / "source.tar.gz"
            urllib.request.urlretrieve("https://api.github.com/repos/r266-tech/wxkey/tarball/" + revision, archive)
            source = tmp / "source"
            source.mkdir()
            with tarfile.open(archive) as tar:
                for item in tar:
                    relative = Path(*Path(item.name).parts[1:])
                    if not item.isfile() or relative.is_absolute() or ".." in relative.parts:
                        continue
                    if relative.suffix not in (".go", ".mod", ".sum") and relative.name != "LICENSE":
                        continue
                    dest = source / relative
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(tar.extractfile(item).read())
            patch = Path(__file__).parent / "assets/wxkey-authorization.patch"
            subprocess.run(["git", "apply", str(patch)], cwd=source, check=True, capture_output=True)
            subprocess.run(["go", "build", "-trimpath", "-o", str(binary), "./cmd/wxkey"], cwd=source, check=True)
            binary.chmod(0o700)
    env = {k: v for k, v in os.environ.items() if k in ("HOME", "USER", "LOGNAME", "PATH", "TMPDIR")}
    env["WXKEY_BOOTSTRAP_ORIGINAL_WECHAT"] = "0"
    command = [str(binary), "bootstrap", "--config", str(destination)]
    if account:
        command += ["--root", str(account)]
    # Password entry, when needed, belongs to the system authorization dialog.
    subprocess.run(command, env=env, check=True, stdout=sys.stderr)
    Path(destination).chmod(0o600)
    return doctor(destination)


def doctor(key_config=KEY_CONFIG):
    root, keys = read_config(key_config)
    verified, missing = [], []
    for file in [root / "contact/contact.db", *message_shards(root)]:
        try:
            with snapshot(file) as copy:
                query(copy, key_for(copy, keys), "SELECT count(*) FROM sqlite_master")
            verified.append(file.name)
        except RuntimeError:
            missing.append(file.name)
    return dict(ready=not missing and bool(verified), verified_databases=len(verified), missing_databases=missing)
