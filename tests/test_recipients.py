from pathlib import Path

from inboxping.ai.prompts import load_prompt
from inboxping.mail.recipients import summarize_recipients
from inboxping.models import Message


def test_many_recipients_are_summarized_for_ai() -> None:
    recipients = ", ".join(f"person{i}@example.org" for i in range(2000))
    summary = summarize_recipients(recipients)
    assert summary.count == 2000
    assert summary.preview == ", ".join(f"person{i}@example.org" for i in range(3))
    assert "example.org（2000 人）" in summary.prompt_context
    assert "其余地址未提供" in summary.prompt_context

    message = Message(
        subject="Notice",
        sender_name="Sender",
        sender_address="sender@example.com",
        recipients=recipients,
        attachments_json="[]",
        text_body="Short body",
    )
    rendered = load_prompt(Path("prompts")).render(message)
    assert "共 2000 人" in rendered
    assert "person1999@example.org" not in rendered
    assert len(rendered) < 3000


def test_empty_and_short_recipient_lists() -> None:
    assert summarize_recipients("").count == 0
    short = summarize_recipients("one@example.com, two@example.org")
    assert short.count == 2
    assert short.preview == "one@example.com, two@example.org"
    quoted = summarize_recipients('"alpha, beta"@example.org, next@example.org')
    assert quoted.addresses == ('"alpha, beta"@example.org', "next@example.org")
