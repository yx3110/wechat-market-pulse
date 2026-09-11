# Third-party components

This project's own code is MIT. Included/adapted components retain their licenses.

| Component | Revision / source | Use / license |
| --- | --- | --- |
| wechat-local-mcp | [cocohahaha/wechat-local-mcp](https://github.com/cocohahaha/wechat-local-mcp), `e77ae47d0fe9812326e583ecc69f774416f90ad6` | Message-only helpers adapted into `_vendor/wechat_message.py`; MIT, `licenses/wechat-local-mcp.txt`. Database adapters and MCP server are not included. |
| wxkey | [r266-tech/wxkey](https://github.com/r266-tech/wxkey), `9b70eecdde47a7172b19465c3f977c86b6050e8a` | Explicit macOS bootstrap downloads/builds source at this revision; MIT, `licenses/wxkey.txt`. |
| Authorization patches | [hetiankong/wechat-local-agent-mcp](https://github.com/hetiankong/wechat-local-agent-mcp), `b7c1de20462e87a80e6c45b86eb17c7d1230d500` | Bundled wxkey patch removes password storage and uses the macOS system authorization dialog; MIT, `licenses/authorization-patches.txt`. |
| Tencent OpenClaw Weixin | [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin), `7c04adc3e95775efd661ab9fba0626d86d237713` | Reference for optional owner-bound ClawBot iLink delivery; MIT, `licenses/tencent-openclaw-weixin.txt`. |
| weixin-cli | [erbanku/weixin-cli](https://github.com/erbanku/weixin-cli), `08af894594b4afd468e23e17dbd783f15403f13b` | Reference for attachment layouts, group nickname protobuf fields and Windows image-key checks; Apache-2.0, `licenses/weixin-cli.txt`. No daemon bundled. |
| Noto CJK | [googlefonts/noto-cjk](https://github.com/googlefonts/noto-cjk), `f8d157532fbfaeda587e826d4cd5b21a49186f7c` | Unmodified NotoSansCJKsc-Regular.otf bundled for offline cross-platform Chinese rendering; SIL OFL 1.1, `licenses/noto-cjk.txt`. |
| sqlcipher3 | [coleifer/sqlcipher3](https://github.com/coleifer/sqlcipher3), 0.6.2 | Python wheel dependency, MIT; includes SQLCipher with its own license notices. |

Python dependencies (Pillow, PyCryptodome, Pydantic, Zstandard, FileLock, Platformdirs, psutil, tzdata, OpenAI SDK, HTTPX and optional PyAV/FFmpeg) are installed as separate packages and retain their respective notices. System fonts are read from the user's OS and are not redistributed. No WeChat app, database, user export, login token or proprietary WCDB binary is included.
