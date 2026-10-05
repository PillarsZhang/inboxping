from email.message import EmailMessage

from inboxping.mail.parser import html_to_markdown, parse_message


def test_parse_plain_message_without_marking_side_effects() -> None:
    raw = """From: =?UTF-8?B?5pWZ5Yqh5aSE?= <office@example.edu>
To: student@example.edu
Subject: =?UTF-8?B?6YCJ6K++56Gu6K6k6YCa55+l?=
Message-ID: <one@example.edu>
Date: Sun, 20 Sep 2026 10:00:00 +0800
Content-Type: text/plain; charset=utf-8
Content-Transfer-Encoding: 8bit

请于本周五前完成选课确认。
""".encode()
    result = parse_message(raw)
    assert result.sender_name == "教务处"
    assert result.sender_address == "office@example.edu"
    assert result.subject == "选课确认通知"
    assert "本周五" in result.text_body
    assert result.message_id == "<one@example.edu>"


def test_parse_html_and_attachment_metadata() -> None:
    raw = b"""MIME-Version: 1.0
From: sender@example.com
To: student@example.com
Subject: multipart
Content-Type: multipart/mixed; boundary=x

--x
Content-Type: text/html; charset=utf-8

<p>Hello <b>world</b><script>alert(1)</script></p>
--x
Content-Type: application/pdf
Content-Disposition: attachment; filename=notice.pdf
Content-Transfer-Encoding: base64

YWJj
--x--
"""
    result = parse_message(raw)
    assert result.text_body == "Hello **world**"
    assert "alert" not in result.text_body
    assert result.attachments[0]["filename"] == "notice.pdf"
    assert result.attachments[0]["size"] == 3


def test_parse_authentication_results() -> None:
    raw = b"""From: sender@example.com
To: student@example.com
Subject: authenticated
Authentication-Results: mx.example.edu; spf=pass smtp.mailfrom=example.com;
 dkim=pass header.d=example.com; dmarc=pass header.from=example.com
Content-Type: text/plain; charset=utf-8

hello
"""
    result = parse_message(raw)
    assert result.authentication == {"spf": "pass", "dkim": "pass", "dmarc": "pass"}


def test_html_email_keeps_inline_text_in_paragraphs() -> None:
    mail = EmailMessage()
    mail.set_content(
        """
        <table><tr><td>
            <p>Hello <strong>Example User</strong>,</p>
            <p>Your profile update was approved and the changes will appear in
                Example <i>Portal</i> within one week.
                Click <a href="https://example.org/request">View Request</a> for details.</p>
            <p><strong>Request ID:</strong> 123456</p>
            <p><a href="https://example.org/help">Contact Us</a> for help.</p>
            <p>&copy; Copyright <span>2026</span> Example Organization.</p>
        </td></tr></table>
    """,
        subtype="html",
    )
    result = parse_message(mail.as_bytes())
    assert result.text_body == (
        "Hello **Example User**,\n\n"
        "Your profile update was approved and the changes will appear in "
        "Example *Portal* within one week. "
        "Click [View Request](https://example.org/request) for details.\n\n"
        "**Request ID:** 123456\n\n"
        "[Contact Us](https://example.org/help) for help.\n\n"
        "© Copyright 2026 Example Organization."
    )


def test_html_preserves_explicit_breaks_lists_and_table_rows() -> None:
    text = html_to_markdown("""
        <div>Meeting details<br>Monday at 10:00</div>
        <ul><li>Bring <b>notes</b></li><li>Confirm attendance</li></ul>
        <table>
            <tr><th>Time</th><th>Room</th></tr>
            <tr><td>10:00</td><td>A1</td></tr>
        </table>
    """)
    assert text == (
        "Meeting details  \nMonday at 10:00\n\n"
        "* Bring **notes**\n* Confirm attendance\n\n"
        "| Time | Room |\n| --- | --- |\n| 10:00 | A1 |"
    )


def test_html_preserves_preformatted_text_and_chinese_inline_punctuation() -> None:
    text = html_to_markdown(
        "<p>请<b>确认</b>，并查看<a href='https://example.org'>详情</a>。</p>"
        "<pre>    line one\n    indented\n\n\nline four</pre><p>结束。</p>"
    )
    assert text == (
        "请**确认**，并查看[详情](https://example.org)。\n\n"
        "```\n    line one\n    indented\n\n\nline four\n```\n\n结束。"
    )


def test_layout_tables_preserve_nested_data_tables_and_text_only_rows() -> None:
    text = html_to_markdown(
        "<table role='presentation'><tr><td>Details:</td></tr><tr><td>"
        "<table><tr><th>Time</th><th>Room</th></tr>"
        "<tr><td>10:00</td><td>A1</td></tr></table>"
        "</td></tr><tr><td>End.</td></tr></table>"
    )
    assert text == "Details:\n\n| Time | Room |\n| --- | --- |\n| 10:00 | A1 |\n\nEnd."
    headerless = html_to_markdown(
        "<table><tr><td>One</td><td>Two</td></tr><tr><td>1</td><td>2</td></tr></table>"
    )
    assert "| One | Two |" in headerless
    assert "| 1 | 2 |" in headerless


def test_html_conversion_removes_scripts_styles_and_remote_images() -> None:
    text = html_to_markdown(
        "<head><title>Hidden title</title></head>"
        "<style>Hidden style</style><script>Hidden script</script>"
        "<noscript>Hidden fallback</noscript>"
        "<img src='https://example.org/track' alt='Hidden tracking image'>"
        "<p>Visible <a href='https://example.org/?a=1&amp;b=2'>link</a>.</p>"
    )
    assert text == "Visible [link](https://example.org/?a=1&b=2)."


def test_multipart_prefers_plain_text_and_keeps_its_line_breaks() -> None:
    mail = EmailMessage()
    plain = "Hello Example User,\n\nPlease confirm:\n  first item\n  second item\n"
    mail.set_content(plain)
    mail.add_alternative("<p>Different HTML content</p>", subtype="html")
    assert parse_message(mail.as_bytes()).text_body == plain.strip()
