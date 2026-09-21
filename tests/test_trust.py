from inboxping.ai.client import AIClient
from inboxping.config import Settings
from inboxping.models import Message


def make_message(sender: str, authentication_json: str) -> Message:
    return Message(
        sender_address=sender,
        authentication_json=authentication_json,
        subject="test",
        sender_name="",
        recipients="",
        attachments_json="[]",
        text_body="",
    )


def test_exact_sender_requires_authentication() -> None:
    client = AIClient(
        Settings(rules={"trusted_senders": ["person@example.com"], "trusted_domains": []})
    )
    verified = client.trust_context(make_message("person@example.com", '{"spf":"pass"}'))
    unverified = client.trust_context(make_message("person@example.com", "{}"))
    assert "精确发件人匹配且认证通过" in verified
    assert "不应直接信任" in unverified


def test_trusted_domain_requires_strong_authentication() -> None:
    client = AIClient(Settings(rules={"trusted_domains": ["example.edu"], "trusted_senders": []}))
    context = client.trust_context(make_message("office@dept.example.edu", '{"dkim":"pass"}'))
    assert "可信域名匹配且 DKIM/DMARC 通过" in context
