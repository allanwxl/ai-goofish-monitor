"""
统一配置管理模块
使用 Pydantic 进行类型安全的配置管理
"""
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    _USING_PYDANTIC_SETTINGS = True
except ImportError:
    from pydantic import BaseSettings
    _USING_PYDANTIC_SETTINGS = False
from pydantic import Field
from typing import Optional
import os

DEFAULT_TELEGRAM_API_BASE_URL = "https://api.telegram.org"


def _env_field(default, env_name: str, **kwargs):
    if _USING_PYDANTIC_SETTINGS:
        return Field(default, validation_alias=env_name, **kwargs)
    return Field(default, env=env_name, **kwargs)


if _USING_PYDANTIC_SETTINGS:
    class _EnvSettings(BaseSettings):
        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            protected_namespaces=(),
        )
else:
    class _EnvSettings(BaseSettings):
        class Config:
            env_file = ".env"
            env_file_encoding = "utf-8"
            extra = "ignore"
            protected_namespaces = ()


class AISettings(_EnvSettings):
    """AI模型配置"""
    api_key: Optional[str] = _env_field(None, "OPENAI_API_KEY")
    base_url: str = _env_field("", "OPENAI_BASE_URL")
    model_name: str = _env_field("", "OPENAI_MODEL_NAME")
    proxy_url: Optional[str] = _env_field(None, "PROXY_URL")
    debug_mode: bool = _env_field(False, "AI_DEBUG_MODE")
    enable_response_format: bool = _env_field(True, "ENABLE_RESPONSE_FORMAT")
    enable_thinking: bool = _env_field(False, "ENABLE_THINKING")
    max_output_tokens: int = _env_field(32768, "AI_MAX_OUTPUT_TOKENS")
    skip_analysis: bool = _env_field(False, "SKIP_AI_ANALYSIS")

    def is_configured(self) -> bool:
        """检查AI是否已正确配置"""
        return bool(self.base_url and self.model_name)


class NotificationSettings(_EnvSettings):
    """通知服务配置"""
    ntfy_topic_url: Optional[str] = _env_field(None, "NTFY_TOPIC_URL")
    pushplus_token: Optional[str] = _env_field(None, "PUSHPLUS_TOKEN")
    pushplus_topic: Optional[str] = _env_field(None, "PUSHPLUS_TOPIC")
    gotify_url: Optional[str] = _env_field(None, "GOTIFY_URL")
    gotify_token: Optional[str] = _env_field(None, "GOTIFY_TOKEN")
    bark_url: Optional[str] = _env_field(None, "BARK_URL")
    wx_bot_url: Optional[str] = _env_field(None, "WX_BOT_URL")
    telegram_bot_token: Optional[str] = _env_field(None, "TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = _env_field(None, "TELEGRAM_CHAT_ID")
    telegram_api_base_url: Optional[str] = _env_field(
        DEFAULT_TELEGRAM_API_BASE_URL,
        "TELEGRAM_API_BASE_URL",
    )
    webhook_url: Optional[str] = _env_field(None, "WEBHOOK_URL")
    webhook_method: str = _env_field("POST", "WEBHOOK_METHOD")
    webhook_headers: Optional[str] = _env_field(None, "WEBHOOK_HEADERS")
    webhook_content_type: str = _env_field("JSON", "WEBHOOK_CONTENT_TYPE")
    webhook_query_parameters: Optional[str] = _env_field(None, "WEBHOOK_QUERY_PARAMETERS")
    webhook_body: Optional[str] = _env_field(None, "WEBHOOK_BODY")
    pcurl_to_mobile: bool = _env_field(True, "PCURL_TO_MOBILE")

    def has_any_notification_enabled(self) -> bool:
        """检查是否配置了任何通知服务"""
        return any([
            self.ntfy_topic_url,
            self.pushplus_token,
            self.wx_bot_url,
            self.gotify_url and self.gotify_token,
            self.bark_url,
            self.telegram_bot_token and self.telegram_chat_id,
            self.webhook_url
        ])


class ScraperSettings(_EnvSettings):
    """爬虫相关配置"""
    run_headless: bool = _env_field(True, "RUN_HEADLESS")
    login_is_edge: bool = _env_field(False, "LOGIN_IS_EDGE")
    running_in_docker: bool = _env_field(False, "RUNNING_IN_DOCKER")
    state_file: str = _env_field("xianyu_state.json", "STATE_FILE")


class AppSettings(_EnvSettings):
    """应用主配置"""
    server_port: int = _env_field(8000, "SERVER_PORT")
    web_username: str = _env_field("admin", "WEB_USERNAME")
    web_password: str = _env_field("admin123", "WEB_PASSWORD")
    task_log_retention_days: int = _env_field(7, "TASK_LOG_RETENTION_DAYS", ge=1)

    # 文件路径配置
    config_file: str = "config.json"
    image_save_dir: str = "images"
    task_image_dir_prefix: str = "task_images_"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # 创建必要的目录
        os.makedirs(self.image_save_dir, exist_ok=True)


class MCPSettings(_EnvSettings):
    """MCP 服务配置

    安全默认值：默认关闭、默认只监听本机、未配置 API Key 时拒绝启动。
    """
    enabled: bool = _env_field(False, "MCP_ENABLED")
    host: str = _env_field("127.0.0.1", "MCP_HOST")
    port: int = _env_field(8001, "MCP_PORT")
    api_keys: str = _env_field("", "MCP_API_KEYS")
    allowed_tools: str = _env_field("", "MCP_ALLOWED_TOOLS")

    def parsed_api_keys(self) -> list[str]:
        """返回配置中的 API Key 明文列表（逗号分隔，支持 key:tenant）。"""
        result: list[str] = []
        for chunk in (self.api_keys or "").split(","):
            item = chunk.strip()
            if not item:
                continue
            result.append(item.split(":", 1)[0].strip())
        return [item for item in result if item]

    def parsed_allowed_tools(self) -> list[str]:
        """返回允许暴露的 tool 白名单；为空表示全部开放。"""
        return [
            item.strip()
            for item in (self.allowed_tools or "").split(",")
            if item.strip()
        ]


# 全局配置实例（单例模式）
_settings_instance = None

def get_settings() -> AppSettings:
    """获取全局配置实例"""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = AppSettings()
    return _settings_instance


def reload_settings() -> None:
    """重新加载全局配置实例"""
    global _settings_instance, settings, ai_settings, notification_settings, scraper_settings, mcp_settings
    from dotenv import load_dotenv
    from src.infrastructure.config.env_manager import env_manager

    load_dotenv(dotenv_path=env_manager.env_file, override=True)
    _settings_instance = None
    settings = get_settings()
    ai_settings = AISettings()
    notification_settings = NotificationSettings()
    scraper_settings = ScraperSettings()
    mcp_settings = MCPSettings()


# 导出便捷访问的配置实例
settings = get_settings()
ai_settings = AISettings()
notification_settings = NotificationSettings()
scraper_settings = ScraperSettings()
mcp_settings = MCPSettings()
