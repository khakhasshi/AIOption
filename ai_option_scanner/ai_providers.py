from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_PROVIDER_NAME = "deepseek"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"


@dataclass(frozen=True)
class AIProvider:
    name: str
    base_url: str
    model: str
    api_key_env: str
    temperature: float = 0.25
    api_key: str | None = None
    provider_type: str = "openai"


def provider_config_path() -> Path:
    override = os.getenv("AI_OPTION_AI_PROVIDERS_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "data" / "ai_providers.json"


def legacy_provider_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "ai_providers.json"


def default_provider() -> AIProvider:
    return AIProvider(
        name=DEFAULT_PROVIDER_NAME,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        model=os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
        api_key_env="DEEPSEEK_API_KEY",
        provider_type="openai",
    )


def load_providers(path: Path | None = None) -> dict[str, AIProvider]:
    config_path = path or provider_config_path()
    providers = {DEFAULT_PROVIDER_NAME: default_provider()}
    if path is None and not config_path.exists():
        legacy_path = legacy_provider_config_path()
        if legacy_path.exists():
            config_path = legacy_path
    if not config_path.exists():
        return providers

    data = json.loads(config_path.read_text())
    for item in data.get("providers", []):
        provider = AIProvider(
            name=item["name"],
            base_url=item["base_url"],
            model=item["model"],
            api_key_env=item["api_key_env"],
            temperature=float(item.get("temperature", 0.25)),
            provider_type=normalize_provider_type(item.get("provider_type")),
        )
        providers[provider.name] = provider
    return providers


def save_providers(providers: dict[str, AIProvider], path: Path | None = None) -> Path:
    config_path = path or provider_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    custom_providers = [
        asdict(provider)
        for name, provider in sorted(providers.items())
        if name != DEFAULT_PROVIDER_NAME
    ]
    config_path.write_text(json.dumps({"providers": custom_providers}, ensure_ascii=False, indent=2) + "\n")
    return config_path


def add_provider(
    name: str,
    base_url: str,
    model: str,
    api_key_env: str,
    temperature: float = 0.25,
    provider_type: str = "openai",
    path: Path | None = None,
) -> Path:
    providers = load_providers(path)
    providers[name] = AIProvider(
        name=name,
        base_url=base_url.rstrip("/"),
        model=model,
        api_key_env=api_key_env,
        temperature=temperature,
        provider_type=normalize_provider_type(provider_type),
    )
    return save_providers(providers, path)


def delete_provider(name: str, path: Path | None = None) -> Path:
    if name == DEFAULT_PROVIDER_NAME:
        raise ValueError("default deepseek provider cannot be deleted; remove DEEPSEEK_API_KEY to disable it")
    providers = load_providers(path)
    providers.pop(name, None)
    return save_providers(providers, path)


def providers_as_rows(path: Path | None = None, include_api_key_env: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for provider in load_providers(path).values():
        row = asdict(provider)
        row.pop("api_key", None)
        row["server_managed"] = True
        row["configured"] = bool(os.getenv(provider.api_key_env))
        row["provider_type"] = normalize_provider_type(row.get("provider_type"))
        if not include_api_key_env:
            row.pop("api_key_env", None)
        rows.append(row)
    return rows


def normalize_provider_type(value: str | None) -> str:
    provider_type = str(value or "openai").strip().lower().replace("_compatible", "")
    aliases = {
        "openai-compatible": "openai",
        "openai_compatible": "openai",
        "chat_completions": "openai",
        "anthropic": "claude",
        "anthropic-compatible": "claude",
        "anthropic_compatible": "claude",
        "claude-compatible": "claude",
        "claude_compatible": "claude",
    }
    provider_type = aliases.get(provider_type, provider_type)
    if provider_type not in {"openai", "claude"}:
        raise ValueError("provider_type must be openai or claude")
    return provider_type
