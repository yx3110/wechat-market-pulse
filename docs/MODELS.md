# 云端、本地和无模型运行

配置中的 `provider` 表示接口类型，`model` 是该服务真实提供或本机已下载的模型名。`wechat-pulse models` 显示预设；预设不自动购买服务、下载权重或指定未经验证的最新模型。

## 零模型、零网络请求

```bash
wechat-pulse init --provider rules
wechat-pulse demo
wechat-pulse report --input ./examples/messages.jsonl --date 2026-01-05 --provider rules
```

规则模式按市场词、可配置 `stock_keywords` 和时段整理消息，提供来源摘录、群间共同提及、生活与其他话题。它不识别图片、不判断讽刺和真实心理、不生成行情预测。默认不悄悄回退到云端。

## Codex 订阅

先安装 [官方 Codex CLI](https://learn.chatgpt.com/docs/codex-cli)，运行 `codex login` 和 `codex login status`。配置 `provider: codex`，`model: null` 使用账号默认模型；也可指定该账号可用的明确模型名称。

程序通过官方 `codex exec --output-schema` 请求结构化结果，禁用 shell、网页搜索和工程文档加载，不读取/复制认证令牌。[非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)、[认证与计费方式](https://learn.chatgpt.com/docs/auth)。需要支持 `--ignore-user-config` 的 CLI；不支持时请升级官方 CLI。旧 CLI 不会通过开启更多工具权限来绕过错误。

## OpenAI API 或其他云端兼容接口

```bash
wechat-pulse init --provider openai --model '你的视觉模型名称'
wechat-pulse init --provider deepseek --model '该服务的模型名称'
wechat-pulse init --provider qwen --model '该服务的模型名称'
```

这些是互斥的首次配置示例，不要依次覆盖已有配置。API Key 通过 `api_key_env` 指定的环境变量读取。OpenAI 默认为 `OPENAI_API_KEY`，DeepSeek 为 `DEEPSEEK_API_KEY`，Qwen 为 `DASHSCOPE_API_KEY`。例如 PowerShell 用 `$env:MODEL_API_KEY`，macOS 用 `export MODEL_API_KEY`；请通过安全的本地方式输入实际值，不贴到 GitHub issue。

OpenAI 使用 [Responses API 结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs)，`store=false`。其他兼容服务使用 Chat Completions，支持 `structured_output: json_schema`、`json_object` 或 `none`；无论哪种模式，本地都验证 JSON schema 和来源。服务不支持所选模式时，修改配置，不自动重复提交收费请求。

Qwen 的预设为国际站 endpoint；账号所属区域不同，请按[提供方说明](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope)设置相应地址。`openai-compatible` 可配置其他 HTTPS endpoint。切换 endpoint 会改变数据接收方，应由使用者明确选择。

## Ollama 本地模型

安装 [Ollama](https://ollama.com/download)，下载适合机器内存的模型，确认 `ollama list` 和本地服务可用。只做安装烟测可使用约 523 MB 的 [`qwen3:0.6b`](https://ollama.com/library/qwen3:0.6b)；它很小，不代表足以可靠分析复杂群聊。实际分析应按机器资源选择更强的中文模型。

```bash
ollama pull qwen3:0.6b
wechat-pulse init --provider ollama --model qwen3:0.6b
```

使用 `/api/chat`、非流式响应和 JSON schema，见 [Ollama API](https://docs.ollama.com/api/chat)、[结构化输出](https://docs.ollama.com/capabilities/structured-outputs)。默认 `local_only: true` 仅连接本机回环地址，不使用 HTTP 代理，也不重定向到远程地址。

完整流程实测还包括 [`qwen3:14b`](https://ollama.com/library/qwen3:14b)（权重约 9.3 GB）：48 GiB Mac 上开启 `think: true`，约 47 秒处理虚构双群示例。权重大小不等于实际内存占用，也不是最低配置承诺；人工复核仍发现细节误读，见 [验证记录](VALIDATION.md)。本地模型和云端模型都需要核对结果，程序不会把有来源编号当作已经证明结论正确。

本地配置可以调整 `num_ctx`（默认 32768）、`max_input_chars`（预设 12000）和 `model_timeout`。`max_input_chars` 是消息资料的字符预算，并非精确 token 数；提示词和 schema 也占上下文。小模型无法稳定输出或机器内存不足时，用 rules 模式仍可整理文字。

## LM Studio / vLLM

启动本机 OpenAI-compatible 服务，选好并加载模型。LM Studio 预设 `http://127.0.0.1:1234/v1`，vLLM 预设 `http://127.0.0.1:8000/v1`。使用 `--base-url` 覆盖端口。`local_only: true` 不需要云端 API Key。

```bash
wechat-pulse init --provider lmstudio --model '服务提供的模型ID'
wechat-pulse init --provider vllm --model '服务提供的模型ID'
```

接口兼容不意味着所有权重支持视觉或严格 JSON 输出；必须按实际加载的模型配置。

## 文字和图片用不同模型

纯文字模型默认不接收图片。可单独配置 `vision`：

```json
{
  "provider": "ollama",
  "model": "已下载的文字模型",
  "base_url": "http://127.0.0.1:11434",
  "local_only": true,
  "supports_images": false,
  "vision": {
    "provider": "ollama",
    "model": "已下载的视觉模型",
    "base_url": "http://127.0.0.1:11434",
    "local_only": true,
    "supports_images": true
  }
}
```

这是需要合并进现有配置的后端片段。全配置见 `init` 的实际输出。只有支持图像的模型才能设置 `supports_images: true`；程序不会把扩展名为图片的文件当成文字直接发送。无视觉能力时报告保留未解析图片数量；规则模式始终不请求模型。

顶层 `local_only: true` 同样约束独立视觉后端。若明确选择“本地文字 + 云端视觉”，需要在顶层显式关闭该限制，并按视觉服务配置密钥。Ollama 支持的思考模型还可配置 `think: false`；该选项只在明确设置时传递，具体支持情况依模型而定。

## 长聊天、失败和缓存

资料过长时先分段提炼文字，保留证据编号，再生成综合报告。已经识别的图片目录保留名称、代码、图像类型和限制，完整传给最终模型；不会因文字压缩丢掉配图匹配依据。图片目录本身超过预算时明确要求增大 `max_input_chars` 或缩小群/时段范围。

模型无法有效压缩、引用无效或遗漏多群对照时停止发布，已有报告保留。可选 `--fallback-rules`，在文本模型调用或验证失败时生成标明回退状态的规则报告，不冒充模型分析。视觉调用失败会停止本轮；需要跳过视觉可显式改用纯文字后端或 rules。

更换 provider、model、endpoint 或视觉配置后使用独立缓存；改昵称只刷新署名。`model: null` 无法感知账号默认模型在服务端发生的变化；需要严格可复现时固定具体模型。

## 无互联网安装

联网机器先构建项目 wheel，再使用 `pip download` 下载目标 OS/Python 对应的依赖 wheels；把 wheelhouse 传到离线机器后运行 `pip install --no-index --find-links <wheelhouse> wechat-market-pulse`。模型权重也需预先下载并按模型运行器的离线导入方式准备。字体已包含在项目 wheel 中。Windows/macOS 的 wheel 不能混用；不要只拷贝另一系统的 `.venv`。
