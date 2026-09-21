from inboxping.mail.parser import parse_message


def test_parse_plain_message_without_marking_side_effects() -> None:
    raw = """From: =?UTF-8?B?5pWZ5Yqh5aSE?= <office@example.edu.cn>
To: student@example.edu.cn
Subject: =?UTF-8?B?6YCJ6K++56Gu6K6k6YCa55+l?=
Message-ID: <one@example.edu.cn>
Date: Sun, 20 Sep 2026 10:00:00 +0800
Content-Type: text/plain; charset=utf-8
Content-Transfer-Encoding: 8bit

请于本周五前完成选课确认。
""".encode()
    result = parse_message(raw)
    assert result.sender_name == "教务处"
    assert result.sender_address == "office@example.edu.cn"
    assert result.subject == "选课确认通知"
    assert "本周五" in result.text_body
    assert result.message_id == "<one@example.edu.cn>"


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
    assert "Hello" in result.text_body
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
