"""Project-local configuration. Importing it does not contact a service."""

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def validate_url(value: str) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("网址格式无效。") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("请使用完整的 http:// 或 https:// 网址，网址中不要包含账号密码。")
    if any(char.isspace() for char in value) or (port is not None and port <= 0):
        raise ValueError("网址包含空白或无效端口。")
    return value


@dataclass(frozen=True)
class Settings:
    root: Path = PROJECT_ROOT
    deepseek_api_key: str = field(default="", repr=False)
    qwen_api_key: str = field(default="", repr=False)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"
    request_timeout: float = 60
    action_timeout_ms: int = 10000
    replay_timeout: float = 120
    max_actions: int = 16
    headless: bool = True

    @property
    def runtime_path(self) -> Path:
        return self.root / "runtime"

    @property
    def browsers_path(self) -> Path:
        return self.root / ".browsers"

    @classmethod
    def load(cls, env_file: str | Path | None = None) -> "Settings":
        path = Path(env_file).expanduser().resolve() if env_file else PROJECT_ROOT / ".env"
        if env_file and not path.is_file():
            raise ValueError(f"找不到配置文件：{path}")
        values = {**(dotenv_values(path) if path.is_file() else {}), **os.environ}
        flag = values.get("HEADLESS", "true").lower()
        if flag not in {"true", "false", "1", "0"}:
            raise ValueError("HEADLESS 应为 true 或 false。")
        settings = cls(
            deepseek_api_key=values.get("DEEPSEEK_API_KEY") or "",
            qwen_api_key=values.get("QWEN_API_KEY") or "",
            deepseek_base_url=values.get("DEEPSEEK_BASE_URL") or cls.deepseek_base_url,
            deepseek_model=values.get("DEEPSEEK_MODEL") or cls.deepseek_model,
            qwen_base_url=values.get("QWEN_BASE_URL") or cls.qwen_base_url,
            qwen_model=values.get("QWEN_MODEL") or cls.qwen_model,
            request_timeout=float(values.get("REQUEST_TIMEOUT", "60")),
            action_timeout_ms=int(values.get("ACTION_TIMEOUT_MS", "10000")),
            replay_timeout=float(values.get("REPLAY_TIMEOUT", "120")),
            max_actions=int(values.get("MAX_ACTIONS", "16")),
            headless=flag in {"true", "1"},
        )
        limits = [settings.request_timeout, settings.action_timeout_ms, settings.replay_timeout]
        if any(not math.isfinite(value) or value <= 0 for value in limits) or not 1 <= settings.max_actions <= 40:
            raise ValueError("超时必须为有限正数，MAX_ACTIONS 应在 1 到 40 之间。")
        os.environ.setdefault("LANGSMITH_TRACING", "false")
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
        return settings

    def require_model(self) -> None:
        if not (self.deepseek_api_key or self.qwen_api_key):
            raise ValueError("请在本项目 .env 配置 DEEPSEEK_API_KEY 或 QWEN_API_KEY。")

    def redact(self, value: str) -> str:
        for secret in (self.deepseek_api_key, self.qwen_api_key):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
