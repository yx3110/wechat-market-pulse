# 验证范围

本文件区分加密测试库、接口 mock、实际模型和真实微信，不把一种验证替代另一种。

- macOS 26.6.2 / Apple Silicon / Python 3.13：SQLCipher Python wheel 已对真实已登录微信的 contact 和 11 个消息分库执行查询，12 库验证通过。
- 本地规则模式：虚构的两个群、同一账号不同群昵称，已生成 PNG/HTML/结构化报告并检查排版；不需要微信或网络。
- 单元测试：48 项，覆盖加密数据库、已提交 WAL、错误密钥、跨库导出、增量游标与未变化分库跳过、群昵称优先级、昵称缓存、图片解码、图片证据目录保留和多群完整性校验。
- Windows：读取器和初始化实现已提供；实际 Windows 微信进程/版本仍需要真机验证。CI 中的 Windows SQLCipher 库与接口测试不能证明所有微信构建均可提取密钥。
- Windows / macOS / Ubuntu × Python 3.11 / 3.13 共六组 CI 已通过测试、wheel 构建、全新环境安装和无账号 demo。Windows PowerShell 5.1 与 macOS 安装脚本也实际运行通过。[首轮全绿记录](https://github.com/yx3110/wechat-market-pulse/actions/runs/34617335967)。
- macOS 首次初始化辅助工具：固定源提交下载、授权补丁应用和 Go 编译成功。已有账号无需重新初始化，本次未为测试重启微信。
- OpenAI Responses、OpenAI-compatible、Ollama 传输均有 HTTP mock，验证 schema、图片编码、错误处理和本地地址约束；不宣称已逐家调用付费云服务。
- Ollama 0.34.0 本机模型：Qwen3 0.6B 能返回简单 JSON；4B 能生成报告但人工复核发现遗漏多群对照和不准确归纳，不能当作推荐配置。程序现已拒绝缺少多群对照的结果，可显式回退 rules。后续实际模型验证将记录在此。

已知范围：微信 4.x 数据结构；本机现存历史和已同步附件；图片支持普通格式、V1/V2 DAT 和安装媒体扩展后的静态 WXGF/HEVC。语音、视频正文、文件正文和链接全文暂不解析。不同分库分别复制，不声称是跨库同一瞬间的事务快照。
