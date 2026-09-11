# 安装与首次初始化

## 1. 安装程序

支持 64 位 Python 3.11–3.14，推荐 3.13。Python、Git 已安装时：

```bash
git clone https://github.com/yx3110/wechat-market-pulse.git
cd wechat-market-pulse
```

macOS：`bash scripts/install-macos.sh`。

Windows PowerShell：`powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1`。

脚本只在仓库的 `.venv` 安装依赖并运行虚构示例，不初始化微信、不调用模型，也不更改系统 Python。缺少 Python 时请先安装 [Python 3.13](https://www.python.org/downloads/)。`sqlcipher3` 使用平台 wheel，不需自己编译 SQLCipher。安装包自带开源中文字体。

后续命令使用 `.venv/bin/wechat-pulse`（macOS）或 `.venv\Scripts\wechat-pulse.exe`（Windows）。也可以激活虚拟环境后直接使用 `wechat-pulse`。

## 2. 先验证离线路径

```bash
wechat-pulse --json demo --output ./demo-output
wechat-pulse init
wechat-pulse --json doctor
```

`demo-output/briefing.html` 应显示标明“本地规则版”的虚构多群报告。`doctor` 的 `offline_ready`、`font_ready` 和 `sqlcipher_ready` 应为 true。这一步不需要微信或任何账号。

## 3. 初始化微信读取

登录桌面微信 4.x，打开一个聊天，让本机数据库可用，然后：

```bash
wechat-pulse setup-wechat
wechat-pulse --json doctor --deep
wechat-pulse groups
```

已有有效 `~/.config/wxcli/config.json` 时直接验证并复用，不反复退出或重新登录。配置内 `key_config` 支持指定其他路径。`--deep` 验证 contact 和消息分库的真实查询；ready 不代表所有旧附件已下载。

### macOS

首次构建需要 Git、Go 1.26.5+ 和 Xcode Command Line Tools。可用自己的包管理器安装 Go；已有 `go version` 满足条件就复用。

初始化下载固定提交的 [wxkey](https://github.com/r266-tech/wxkey)，应用随包提供的系统授权补丁后本地编译。必要时创建本地签名的辅助微信副本并重新打开；原版应用和 SIP 不改动。系统可能要求管理员授权或重新确认微信登录；密码只在 macOS 系统授权框输入，程序不收集和保存密码。

首次操作会影响当前微信窗口，执行前会解释并确认。编程助手已获得用户授权时可以运行 `setup-wechat --yes`；不要把 `--yes` 当作代替系统密码或手机登录确认。若 macOS 拒绝读取容器，给运行终端/编程工具所需的完全磁盘访问权限，再重试，不要关闭 SIP。

### Windows

初始化只读扫描**当前 Windows 用户**的 `Weixin.exe` / `WeChat.exe`，对候选数据库密钥逐一进行 HMAC 验证。只请求进程查询与读取权限，不向进程注入代码或写内存。程序与微信应由同一用户、同一权限级别运行；读取权限不足会明确报错。

自定义数据目录或多个账号时：

```powershell
wechat-pulse setup-wechat --account-dir 'D:\WeChat\xwechat_files\你的账号目录'
```

目录内应包含 `db_storage\contact\contact.db` 和 `msg`。没有找到可验证密钥可能意味着账号目录不符、登录后没有打开聊天、微信构建不受支持或系统阻止读取。不要反复重启微信；先检查这些具体条件。当前 Windows 真微信版本仍需使用者验证，见 [验证记录](VALIDATION.md)。

## 4. 选群、选模型

`groups` 只列出本机群。用稳定群 ID 选择，重复传入 `--group-id` 合并多个群。需要长期运行时可将 ID 数组填入配置的 `groups`。

默认规则模式不联网。需要大模型时按 [MODELS.md](MODELS.md) 配置。不要为了换模型删除微信密钥或聊天数据库。

## 5. 无实时读取条件也能用

```bash
wechat-pulse report --input ./examples/messages.jsonl --date 2026-01-05 --provider rules
```

导入格式参见 [示例](../examples/messages.jsonl)。必要字段：`id`、`group_id`、`group_name`、`sender_id`、`sender`、含时区的 `sent_at`、`message_kind`；文字用 `text`。图片记录的 `image_path` 必须指向 JSONL 所在目录内的 PNG/JPEG 等文件，程序拒绝越界路径。ID 冲突会停止合并。其他格式先通过明确的转换器转换，不能把自由文本猜成精确的发言人和时间。

## 升级与数据位置

默认运行目录由系统用户数据目录决定，`doctor` 检查状态，`init` 返回配置路径。可用 `wechat-pulse --home '/自己的私有目录' ...` 固定位置；所有调用保持相同 `--home`。

源码和运行目录分开：升级只更新程序，不替换 `config.json`、数据库密钥或报告。后台任务升级前 `stop`，等待状态变为 stopped，升级后用原配置 `start`。电脑需保持唤醒；后台任务不会自行建立开机启动。
