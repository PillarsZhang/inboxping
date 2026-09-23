from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path("config.yaml")
CONFIG_PATH_ENV = "INBOXPING_CONFIG"
LOG_LEVEL_ENV = "INBOXPING_LOG_LEVEL"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class WebConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    username: str = "admin"
    password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("change-me-before-production")
    session_max_age_days: int = Field(default=400, ge=1, le=400)
    session_rolling: bool = True


class AIConfig(StrictModel):
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr = SecretStr("")
    model: str = "gpt-4.1-mini"
    response_format: Literal["json_schema", "json_object", "none"] = "json_schema"
    connect_timeout_seconds: float = Field(default=10, gt=0)
    read_timeout_seconds: float = Field(default=90, gt=0)
    max_stream_seconds: float = Field(default=600, gt=0)
    max_output_chars: int = Field(default=100_000, ge=1000)
    stream_usage: bool = True
    # None means that the provider's default sampling parameters are preserved.
    temperature: float | None = Field(default=None, ge=0, le=2)


class NotificationChannelBase(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", max_length=30)
    enabled: bool = True


class WeComWebhookChannel(NotificationChannelBase):
    type: Literal["wecom_webhook"]
    webhook_url: SecretStr

    @model_validator(mode="after")
    def validate_secret(self) -> WeComWebhookChannel:
        if self.enabled and not self.webhook_url.get_secret_value():
            raise ValueError("启用的 wecom_webhook 必须配置 webhook_url")
        return self


class WeComAppChannel(NotificationChannelBase):
    type: Literal["wecom_app"]
    corp_id: str
    corp_secret: SecretStr
    agent_id: int = Field(gt=0)
    to_user: list[str] = Field(default_factory=lambda: ["@all"], min_length=1)

    @model_validator(mode="after")
    def validate_credentials(self) -> WeComAppChannel:
        if self.enabled and (not self.corp_id or not self.corp_secret.get_secret_value()):
            raise ValueError("启用的 wecom_app 必须配置 corp_id 和 corp_secret")
        return self


NotificationChannel = Annotated[WeComWebhookChannel | WeComAppChannel, Field(discriminator="type")]


class NotificationRetryConfig(StrictModel):
    max_attempts: int = Field(default=3, ge=1, le=10)


class NotificationsConfig(StrictModel):
    default_channels: list[str] = Field(default_factory=list)
    channels: list[NotificationChannel] = Field(default_factory=list)
    retry: NotificationRetryConfig = Field(default_factory=NotificationRetryConfig)

    @model_validator(mode="after")
    def validate_channels(self) -> NotificationsConfig:
        ids = [channel.id for channel in self.channels]
        if len(ids) != len(set(ids)):
            raise ValueError("notifications.channels 中的 id 不能重复")
        missing = set(self.default_channels) - set(ids)
        if missing:
            raise ValueError(f"default_channels 引用了不存在的通道: {sorted(missing)}")
        disabled = {channel.id for channel in self.channels if not channel.enabled} & set(
            self.default_channels
        )
        if disabled:
            raise ValueError(f"default_channels 引用了未启用的通道: {sorted(disabled)}")
        return self


class ImapProtocolConfig(StrictModel):
    host: str
    port: int = Field(default=993, ge=1, le=65535)
    folder: str = "INBOX"
    ssl: bool = True


class Pop3ProtocolConfig(StrictModel):
    host: str
    port: int = Field(default=995, ge=1, le=65535)
    ssl: bool = True


class MailAccount(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    name: str
    username: str
    password: SecretStr = SecretStr("")
    protocol: Literal["imap_idle", "imap_poll", "pop3_poll"]
    imap: ImapProtocolConfig | None = None
    pop3: Pop3ProtocolConfig | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def validate_protocol_settings(self) -> MailAccount:
        if self.protocol == "pop3_poll":
            if self.pop3 is None or self.imap is not None:
                raise ValueError("protocol=pop3_poll 时必须且只能配置 pop3")
        elif self.imap is None or self.pop3 is not None:
            raise ValueError("IMAP 协议必须且只能配置 imap")
        return self


class MailConfig(StrictModel):
    accounts: list[MailAccount] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_accounts(self) -> MailConfig:
        ids = [account.id for account in self.accounts]
        if len(ids) != len(set(ids)):
            raise ValueError("mail.accounts 中的 id 不能重复")
        return self


class TrustedAddress(StrictModel):
    value: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    note: str | None = Field(default=None, max_length=300)

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: str) -> str:
        return value.strip().lower()


class TrustedDomain(StrictModel):
    value: str = Field(pattern=r"^(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}$")
    note: str | None = Field(default=None, max_length=300)

    @field_validator("value")
    @classmethod
    def normalize_value(cls, value: str) -> str:
        return value.strip().lower()


class TrustConfig(StrictModel):
    sender_addresses: list[TrustedAddress] = Field(default_factory=list)
    sender_domains: list[TrustedDomain] = Field(default_factory=list)
    link_domains: list[TrustedDomain] = Field(default_factory=list)


class MonitorConfig(StrictModel):
    poll_interval_seconds: int = Field(default=60, ge=5)
    idle_timeout_seconds: int = Field(default=240, ge=30)
    initial_sync_limit: int = Field(default=20, ge=1)
    max_message_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)


class AnalysisConfig(StrictModel):
    body_max_chars: int = Field(default=20_000, ge=1000)
    prompt_dir: Path = Path("prompts")


class StorageConfig(StrictModel):
    database_url: str = "sqlite:///./data/inboxping.db"


class LoggingConfig(StrictModel):
    level: str = "INFO"
    json_output: bool = Field(default=False, alias="json")


class Settings(StrictModel):
    web: WebConfig = Field(default_factory=WebConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    mail: MailConfig = Field(default_factory=MailConfig)
    trust: TrustConfig = Field(default_factory=TrustConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def config_path() -> Path:
    return Path(os.getenv(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH))


def load_settings(path: Path | str | None = None) -> Settings:
    target = Path(path) if path else config_path()
    if not target.is_file():
        raise FileNotFoundError(
            f"配置文件不存在：{target}。请复制 config.example.yaml 为 config.yaml。"
        )
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件根节点必须是对象：{target}")
    if log_level := os.getenv(LOG_LEVEL_ENV):
        raw.setdefault("logging", {})["level"] = log_level.upper()
    return Settings.model_validate(raw)


@lru_cache
def get_settings() -> Settings:
    return load_settings()
