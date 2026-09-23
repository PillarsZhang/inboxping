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


def test_authentication_context_reports_headers_without_trust_rules() -> None:
    client = AIClient(Settings())
    verified = client.authentication_context(make_message("person@example.com", '{"spf":"pass"}'))
    unverified = client.authentication_context(make_message("person@example.com", "{}"))
    assert verified == "SPF=pass, DKIM=unknown, DMARC=unknown"
    assert unverified == "SPF=unknown, DKIM=unknown, DMARC=unknown"


def test_invalid_authentication_context_is_unknown() -> None:
    client = AIClient(Settings())
    context = client.authentication_context(make_message("office@example.edu", "not-json"))
    assert context == "SPF=unknown, DKIM=unknown, DMARC=unknown"


def test_trust_context_matches_exact_address_and_domain_boundaries() -> None:
    client = AIClient(
        Settings(
            trust={
                "sender_addresses": [
                    {"value": "Notice@Example.org", "note": "Previously verified"}
                ],
                "sender_domains": [{"value": "updates.example.com"}],
                "link_domains": [{"value": "portal.example.com"}],
            }
        )
    )
    message = make_message("notice@example.org", '{"spf":"pass"}')
    message.text_body = "Go to https://login.portal.example.com/path."
    context = client.trust_context(message)
    assert "发件地址=notice@example.org（备注：Previously verified）" in context
    assert "链接域名=portal.example.com" in context
    assert "发件域名=" not in context

    spoofed = make_message("notice@example.org.evil.test", "{}")
    spoofed.text_body = "https://portal.example.com.evil.test/"
    assert client.trust_context(spoofed) == "未命中本地信任参考名单"


def test_trust_context_matches_sender_domain_without_note() -> None:
    client = AIClient(Settings(trust={"sender_domains": [{"value": "example.org"}]}))
    context = client.trust_context(make_message("person@sub.example.org", "{}"))
    assert context == "发件域名=example.org"


def test_malformed_link_does_not_break_trust_context() -> None:
    client = AIClient(Settings(trust={"link_domains": [{"value": "example.com"}]}))
    message = make_message("person@example.org", "{}")
    message.text_body = "https://[invalid and https://portal.example.com/path"
    assert client.trust_context(message) == "链接域名=example.com"
