"""Transport presets do not silently pick or download a model."""

PRESETS = {
    "rules": dict(provider="rules", local_only=True, supports_images=False),
    "codex": dict(provider="codex", local_only=False, supports_images=True),
    "openai": dict(
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        local_only=False,
        supports_images=True,
    ),
    "ollama": dict(
        provider="ollama",
        base_url="http://127.0.0.1:11434",
        local_only=True,
        supports_images=False,
        max_input_chars=12000,
    ),
    "lmstudio": dict(
        provider="openai-compatible",
        base_url="http://127.0.0.1:1234/v1",
        local_only=True,
        supports_images=False,
        structured_output="json_schema",
        max_input_chars=12000,
    ),
    "vllm": dict(
        provider="openai-compatible",
        base_url="http://127.0.0.1:8000/v1",
        local_only=True,
        supports_images=False,
        structured_output="json_schema",
        max_input_chars=24000,
    ),
    "deepseek": dict(
        provider="openai-compatible",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        local_only=False,
        supports_images=False,
        structured_output="json_object",
    ),
    "qwen": dict(
        provider="openai-compatible",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        local_only=False,
        supports_images=False,
        structured_output="json_object",
    ),
    "openai-compatible": dict(
        provider="openai-compatible",
        api_key_env="MODEL_API_KEY",
        local_only=False,
        supports_images=False,
        structured_output="json_object",
    ),
}


def apply_backend(cfg, args):
    if getattr(args, "provider", None):
        for key in ("base_url", "model", "api_key_env", "local_only", "supports_images", "structured_output"):
            cfg.pop(key, None)
        cfg.update(PRESETS[args.provider])
        cfg.pop("vision", None)
    for key in ("model", "base_url", "api_key_env", "max_input_chars"):
        if getattr(args, key, None) is not None:
            cfg[key] = getattr(args, key)
    if getattr(args, "supports_images", False):
        cfg["supports_images"] = True
    if getattr(args, "fallback_rules", False):
        cfg["fallback_rules"] = True
    if getattr(args, "vision_provider", None):
        cfg["vision"] = {**PRESETS[args.vision_provider], "model": args.vision_model, "supports_images": True}
        if getattr(args, "vision_base_url", None):
            cfg["vision"]["base_url"] = args.vision_base_url
    return cfg
