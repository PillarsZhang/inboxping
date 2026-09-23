from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from inboxping.mail.recipients import summarize_recipients
from inboxping.models import Message


@dataclass(frozen=True)
class Prompt:
    system: str
    user_template: str
    version: str

    def render(
        self,
        message: Message,
        authentication_context: str = "未评估",
        trust_context: str = "未命中本地信任参考名单",
    ) -> str:
        return self.user_template.format(
            subject=message.subject,
            sender_name=message.sender_name,
            sender_address=message.sender_address,
            recipients=summarize_recipients(message.recipients or "").prompt_context,
            attachments=message.attachments_json,
            authentication_context=authentication_context,
            trust_context=trust_context,
            body=message.text_body,
        )


@dataclass(frozen=True)
class TranslationPrompt:
    system: str
    user_template: str
    version: str

    def render(self, message: Message) -> str:
        return self.user_template.format(
            subject=message.subject,
            sender_name=message.sender_name,
            sender_address=message.sender_address,
            body=message.text_body,
        )


def load_prompt(prompt_dir: Path) -> Prompt:
    system_path = prompt_dir / "analysis.system.txt"
    user_path = prompt_dir / "analysis.user.txt"
    system = _read_required(system_path)
    user = _read_required(user_path)
    version = hashlib.sha256(f"{system}\0{user}".encode()).hexdigest()[:12]
    return Prompt(system=system.strip(), user_template=user.strip(), version=version)


def load_translation_prompt(prompt_dir: Path) -> TranslationPrompt:
    system_path = prompt_dir / "translation.system.txt"
    user_path = prompt_dir / "translation.user.txt"
    system = _read_required(system_path)
    user = _read_required(user_path)
    version = hashlib.sha256(f"{system}\0{user}".encode()).hexdigest()[:12]
    return TranslationPrompt(system=system.strip(), user_template=user.strip(), version=version)


def _read_required(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"Prompt 文件不存在：{path}")
    return path.read_text("utf-8")


def validate_prompt_files(prompt_dir: Path) -> None:
    load_prompt(prompt_dir)
    load_translation_prompt(prompt_dir)
