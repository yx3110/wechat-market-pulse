# 把原图送到自己的手机

程序先生成本机文件，再由使用者明确选择发送。报告内容和原图片可能包含群友姓名及观点，分享前核对图像。发送不请求分析模型。

## Tailscale Taildrop

电脑与手机安装 Tailscale 并属于允许文件传输的同一网络。安卓可通过通知或 Tailscale 接收目录打开文件。

```bash
tailscale file cp --targets
wechat-pulse send '/完整报告.png' --target '自己的设备名' --sha256 '文件SHA256'
```

哈希可用 Python 计算：`python -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('报告.png').read_bytes()).hexdigest())"`。发送原文件，不缩放、不压缩。回执保存在私有运行目录，`transfer_completed` 只表示 CLI 成功完成传输，不表示手机上已阅读。相同目标与哈希不重复发送，未确认状态不自动重试。

## 可选 ClawBot iLink 会话

这不是桌面微信文件传输助手的通用 API。实现依据腾讯公开的 iLink 协议，只向扫码绑定的本人 Bot 会话发送，不枚举收件人。账号是否可绑定、上下文是否有效和 CDN 是否接受上传由微信服务端决定。

在已激活的项目虚拟环境中：

```bash
python -m marketpulse.clawbot login-start
python -m marketpulse.clawbot login-wait
python -m marketpulse.clawbot status
python -m marketpulse.clawbot send '/完整报告.png' --sha256 '文件SHA256'
```

`login-start` 生成本机二维码文件，打开后用手机微信扫码；随后在 Bot 会话发一句话，让发送器取得有效上下文。不要将二维码和会话状态上传到 issue。默认凭据在 `~/.config/marketpulse/clawbot`，可用子命令前的 `--state-dir` 指定其他私有目录。

`submitted` 表示 API 接受提交，不是送达或已读回执。请求中断会记录 `uncertain`；业务拒绝记录 `rejected`。CDN HTTP 500 或业务错误需要查看脱敏错误码，不能直接归因于图片大小，也不自动缩图或连发。客户端目前只接受已核对的 PNG、最高 25 MB；这个客户端上限不代表服务器保证接受。需要其他原文件或服务不可用时可用 Taildrop。
