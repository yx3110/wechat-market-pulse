# 编程助手安装与操作指南

适用于 Codex、Claude Code、Cursor、Cline 等具有本地终端能力的工具。程序不要求特定编程助手，也不要求 GPT。

先完成不依赖用户回答的安装、示例和检查。用户已经明确选过模型、群或授权初始化时沿用该选择，不反复要求确认。只有缺少实际必需的信息才提问，例如多个同名前缀群无法唯一确定，或首次微信系统授权需要本人操作。

## 可直接执行的起步指令

macOS 在仓库根目录执行 `bash scripts/install-macos.sh`；Windows 执行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1`。脚本会给出缺失环境的具体提示。安装后始终调用项目虚拟环境里的可执行文件，不假设全局 PATH 已修改。

新安装先完成 rules 示例。对已有安装，先检查 `wechat-pulse --json doctor`，不要用 `init --force` 修复不相关错误。生成成功后读取结果中的 `image` 路径核对实际 PNG，而不是只检查退出码。无微信或初始化受限时，`--input examples/messages.jsonl --date 2026-01-05` 仍可以验证完整流程。

输出报告时向用户说明实际使用的 provider、资料截止时间、分析群数、缺图情况和结果文件；接口 mock、示例或规则回退都要明确标识。不要承诺 Windows 真机读取已经验证，除非本次确实完成。

## 推荐操作顺序

1. 探测 OS、Python 版本与架构、Git 和已有安装。复用符合条件的 64 位 Python 3.11–3.14，不更改系统 Python。
2. 安装到仓库 `.venv`；在新的输出目录运行 `wechat-pulse --json demo`，确认 JSON `ok: true`、PNG 和 HTML 存在且可读。
3. 用 `doctor` 检查配置。已有配置不使用 `init --force`；保留用户选群、模型和数据路径。
4. 按用户已有选择配置 rules、本地服务或云端后端。未选择时先用 rules 完成可运行的示例，不索要不必要的 API Key。
5. 已有微信密钥先 `doctor --deep`。仅首次初始化或密钥失效时用 `setup-wechat`；得到相应授权后可传 `--yes`。本机密码、手机扫码由系统/微信完成。
6. 用 `groups --prefix` 找群。用户已明确群名时解析稳定 ID；多个匹配时展示名称供选择，不擅自分析全部群。
7. 指定日期、时段与群生成报告。检查 `coverage`、`analysis_method`、缺图标记与来源，再打开 PNG/HTML核对排版。
8. 用户启用标的补查时阅读 [MARKET_RESEARCH.md](MARKET_RESEARCH.md)，在现有私有配置添加 `market_research`。保留密钥与离线偏好，核对 `market_research.status` 和各标的资料缺失标记；不要把补查完成说成所有群聊说法已核实。
9. 用户指定重点成员时阅读 [FOCUS_MEMBERS.md](FOCUS_MEMBERS.md)，用 `members --group-id ... --search ...` 核对群内稳定身份，再写入 `focus_members`。不要按昵称持续匹配或自动关注别的群；多个同名结果必须确认，没发言时如实标记。

## 命令接口

全局参数放在子命令前：`wechat-pulse --home /private/runtime --json report ...`。

标准输出为 `{ "ok": true, "data": ... }`。失败退出码为 1，标准错误输出 `{ "ok": false, "error": "..." }`；模型进度也写标准错误。CLI 不打印 API Key、微信数据库密钥或认证令牌。不要让异常日志、私有截图和原始模型请求进入 issue。

`report` 的结果提供 `image`、`report`、`as_of`、`method` 和 `coverage`。`demo` 的 `fictional: true` 表示虚构资料。不要把 demo 的成功说成真实微信验证成功。

## 故障定位

| 现象 | 下一步 |
| --- | --- |
| Python 版本不对 | 选择已有 3.11+ 解释器，重建项目自己的 `.venv` |
| 缺少 SQLCipher wheel | 检查 Python/OS/架构支持；不要随机下载 DLL |
| 缺少密钥 | 运行 `setup-wechat`，或先用 JSONL 导入路径 |
| 多个微信账号目录 | 用 `--account-dir` 指定用户当前账号 |
| Windows 拒绝进程读取 | 检查当前用户与权限级别；不扫描其他账号/进程 |
| macOS 容器读取被拒绝 | 检查终端/编程工具的系统文件访问权限 |
| 模型服务连不上 | 核对 endpoint、本地服务运行状态和已安装的模型 ID |
| 模型 JSON/证据不合格 | 保留旧报告，减小范围、换模型或使用 rules |
| 图片缺失 | 查本机附件是否已同步，打开原图后重试；不猜图中股票 |
| 只改了昵称 | `refresh-names`，不必重做完整模型分析 |
| Taildrop 未确认 | 核对接收设备与回执，不盲目重发 |

## 开发约定

生产与开源版本使用 `src/marketpulse` 同一实现，实例差异放在运行配置中。测试使用虚构消息和临时加密库；模型请求使用本地 HTTP mock。真实账号数据、密钥、缓存、报告和 token 不提交。发布前运行测试、构建 wheel、从 wheel 验证 demo，再检查打包清单。
