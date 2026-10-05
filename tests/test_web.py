import json
import shutil
import subprocess
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import unquote

import pytest
import yaml
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from inboxping.ai.client import AnalyzedMessage, TokenUsage
from inboxping.ai.schema import AnalysisResult
from inboxping.config import Settings
from inboxping.mail.source_fetch import FetchedEml
from inboxping.models import Analysis, Message, Notification
from inboxping.web.app import create_app


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed to run the renderer")
def test_markdown_autolinks_stop_at_chinese_punctuation() -> None:
    cases = [
        ("请访问 https://example.org/login，点击“忘记密码”并继续。", "https://example.org/login"),
        ("详情见 https://example.org/page#section。", "https://example.org/page#section"),
        ("请访问 https://example.org/中文?q=确认，继续。", "https://example.org/中文?q=确认"),
        ("请访问 [详情，继续](https://example.org/中文，路径)。", "https://example.org/中文，路径"),
    ]
    script = """
        const fs = require('node:fs');
        const vm = require('node:vm');
        const path = require('node:path');
        const input = JSON.parse(fs.readFileSync(0, 'utf8'));
        const context = vm.createContext({
            window: {},
            marked: require(path.join(input.root, 'vendor/marked/marked.umd.js')),
        });
        vm.runInContext(fs.readFileSync(path.join(input.root, 'message.js'), 'utf8'), context);
        const html = input.sources.map(source => {
            context.source = source;
            return vm.runInContext('messageMarkdown.parse(source)', context);
        });
        process.stdout.write(JSON.stringify(html));
    """
    result = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(
            {
                "root": str(Path(__file__).resolve().parents[1] / "src/inboxping/web/static"),
                "sources": [source for source, _ in cases],
            }
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    for html, (source, expected_url) in zip(json.loads(result.stdout), cases, strict=True):
        soup = BeautifulSoup(html, "html.parser")
        links = soup.find_all("a")
        assert len(links) == 1
        assert unquote(links[0]["href"]) == expected_url
        if not source.startswith("请访问 ["):
            assert links[0].get_text() == expected_url
            assert soup.get_text().strip() == source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed to run dashboard")
def test_dashboard_selection_and_bulk_actions() -> None:
    script = """
        const assert = require('node:assert/strict');
        const fs = require('node:fs');
        const vm = require('node:vm');
        let items = [
            { id: 1, subject: 'First', should_push: true },
            { id: 2, subject: 'Second', should_push: false },
            { id: 3, subject: 'Third', should_push: null },
            { id: 4, subject: 'Fourth', should_push: true },
        ];
        const calls = [];
        let releaseFirst;
        const context = {
            window: {}, URLSearchParams,
            history: { replaceState() {} }, location: { pathname: '/' },
            InboxPing: {
                async apiFetch(url, options = {}) {
                    calls.push({ url, method: options.method || 'GET' });
                    if (url === '/api/v1/overview') return { stats: {} };
                    if (url.startsWith('/api/v1/messages?')) return { items, total: 40 };
                    if (url === '/api/v1/messages/1/reanalyze') {
                        return new Promise(resolve => { releaseFirst = resolve; });
                    }
                    if (url === '/api/v1/messages/2/reanalyze') return { status: 'failed' };
                    if (url === '/api/v1/messages/3/reanalyze') {
                        throw new Error('Connection failed');
                    }
                    if (url === '/api/v1/messages/4/notify') throw new Error('Delivery failed');
                    return { status: 'completed' };
                },
            },
        };
        vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
        const page = context.window.dashboardPage();
        const ids = () => Array.from(page.selectedIds);
        const posts = () => calls.filter(call => call.method === 'POST').map(call => call.url);
        (async () => {
            await page.loadMessages();
            assert.equal(page.allSelected, false);
            page.toggleSelection(1, true);
            page.toggleSelection(1, true);
            assert.deepEqual(ids(), [1]);
            assert.equal(page.partiallySelected, true);
            page.toggleSelection(1, false);
            assert.deepEqual(ids(), []);
            page.toggleAll(true);
            assert.deepEqual(ids(), [1, 2, 3, 4]);
            assert.equal(page.allSelected, true);
            assert.equal(page.partiallySelected, false);
            assert.equal(page.selectedPushCount, 2);

            const analyzing = page.runBulkAction('reanalyze');
            assert.equal(page.bulkProgress, '正在分析 1/4 封');
            assert.equal(page.activeMessageId, 1);
            await page.runBulkAction('reanalyze');
            await page.runBulkAction('notify');
            await page.runBulkAction('reanalyze', [items[3]]);
            await page.refresh();
            page.toggleAll(false);
            page.toggleSelection(1, false);
            page.goToPage(2);
            assert.deepEqual(ids(), [1, 2, 3, 4]);
            assert.equal(page.offset, 0);
            assert.deepEqual(posts(), ['/api/v1/messages/1/reanalyze']);
            releaseFirst({ status: 'completed' });
            await analyzing;
            assert.deepEqual(posts(), [1, 2, 3, 4].map(id => `/api/v1/messages/${id}/reanalyze`));
            assert.equal(page.bulkResult, '分析完成：成功 2 封，失败 2 封。');
            assert.deepEqual(Array.from(page.bulkErrors, item => item.id), [2, 3]);
            assert.equal(page.bulkAction, null);
            assert.equal(page.activeMessageId, null);
            assert.equal(page.bulkProgress, '');
            assert.deepEqual(ids(), [1, 2, 3, 4]);

            await page.runBulkAction('notify');
            assert.deepEqual(posts().filter(url => url.endsWith('/notify')), [
                '/api/v1/messages/1/notify', '/api/v1/messages/4/notify',
            ]);
            assert.equal(page.bulkResult, '推送完成：成功 1 封，失败 1 封，跳过 2 封。');
            assert.equal(page.bulkErrors[0].message, 'Delivery failed');
            assert.equal(page.bulkAction, null);
            page.toggleAll(false);

            const previousPostCount = posts().length;
            await page.runBulkAction('reanalyze', [items[3]]);
            await page.runBulkAction('notify', [items[0]]);
            await page.runBulkAction('notify', [items[1]]);
            assert.deepEqual(posts().slice(previousPostCount), [
                '/api/v1/messages/4/reanalyze', '/api/v1/messages/1/notify',
            ]);
            assert.deepEqual(ids(), []);
            assert.equal(page.bulkResult, '推送完成：成功 1 封，失败 0 封。');
            const count = calls.length;
            await page.runBulkAction('notify');
            await page.runBulkAction('reanalyze');
            assert.equal(calls.length, count);

            page.toggleAll(true);
            items = items.slice(0, 3);
            await page.loadMessages();
            assert.deepEqual(ids(), [1, 2, 3]);
            for (const navigate of [
                () => page.goToPage(2), () => page.changePageSize(), () => page.applyFilters(),
            ]) {
                page.toggleAll(true);
                navigate();
                assert.deepEqual(ids(), []);
                assert.equal(page.bulkResult, '');
                assert.equal(page.bulkErrors.length, 0);
                await new Promise(setImmediate);
            }
            process.stdout.write('completed');
        })().catch(error => { console.error(error); process.exitCode = 1; });
    """
    result = subprocess.run(
        ["node", "-e", script, str(Path("src/inboxping/web/static/dashboard.js").resolve())],
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "completed"


def test_dashboard_and_health(tmp_path) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        ai={"base_url": "https://api.openai.com/v1?region=test"},
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "InboxPing" in dashboard.text
        assert "邮件首页" in dashboard.text
        assert 'href="/static/style.css?v=' in dashboard.text
        assert 'src="/static/dashboard.js?v=' in dashboard.text
        assert 'href="/static/vendor/bootstrap/bootstrap.min.css?v=5.3.8"' in dashboard.text
        assert (
            'href="/static/vendor/bootstrap-icons/bootstrap-icons.min.css?v=1.13.1"'
            in dashboard.text
        )
        assert 'src="/static/vendor/alpine/alpine.min.js?v=3.15.12"' in dashboard.text
        assert "http://testserver/static/" not in dashboard.text
        assert 'x-data="dashboardPage()"' in dashboard.text
        assert 'x-show.important="error"' in dashboard.text
        assert "每页" in dashboard.text
        assert "切换页码" in dashboard.text
        assert "全选当前页" in dashboard.text
        assert "分析选中" in dashboard.text
        assert "推送选中" in dashboard.text
        assert 'class="score-badge score-importance"' in dashboard.text
        assert 'x-for="(mail, index) in messages"' in dashboard.text
        assert 'x-text="offset + index + 1"' in dashboard.text
        assert 'class="mail-recipient-summary"' in dashboard.text
        assert 'class="mail-recipient-ellipsis"' in dashboard.text
        assert 'x-text="`共 ${mail.recipient_count} 人`"' in dashboard.text
        assert 'class="score-badge score-risk"' in dashboard.text
        assert 'class="score-badge score-decision"' in dashboard.text
        assert 'x-show="mail.should_push"' not in dashboard.text
        assert "<span>重要</span>" in dashboard.text
        assert "bi-bookmark-star" in dashboard.text
        assert "bi-shield-exclamation" in dashboard.text
        assert "匿名访问" in dashboard.text
        assert ">退出<" not in dashboard.text
        assert client.get("/static/vendor/bootstrap/bootstrap.min.css").status_code == 200
        assert client.get("/static/vendor/alpine/alpine.min.js").status_code == 200
        assert (
            client.get("/static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2").status_code
            == 200
        )
        overview = client.get("/api/v1/overview").json()
        assert overview["stats"]["total"] == 0
        assert overview["services"] == {
            "ai_enabled": False,
            "ai_model": "gpt-4.1-mini",
            "ai_base_url": "https://api.openai.com/v1",
            "ai_response_format": "json_schema",
            "notifications_enabled": False,
            "notification_channels": [],
        }
        assert client.get("/api/v1/accounts").json() == []
        events = client.get("/api/v1/events").json()
        assert [event["kind"] for event in events] == ["service_started"]


def test_account_overview_includes_mailbox_address(tmp_path) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        mail={
            "accounts": [
                {
                    "id": "university",
                    "name": "学校邮箱",
                    "username": "student@example.edu",
                    "protocol": "imap_poll",
                    "imap": {"host": "imap.example.edu"},
                    "enabled": False,
                }
            ]
        },
    )
    with TestClient(create_app(settings)) as client:
        account = client.get("/api/v1/accounts").json()[0]
        assert account["username"] == "student@example.edu"
        assert account["status"] == "disabled"


def test_web_shows_push_title_channel_and_delivery_status(tmp_path) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        mail={"accounts": []},
        notifications={
            "default_channels": ["wecom_app"],
            "channels": [
                {
                    "id": "wecom_app",
                    "type": "wecom_app",
                    "corp_id": "corp",
                    "corp_secret": "secret",
                    "agent_id": 1000001,
                    "to_user": ["demo-user"],
                }
            ],
        },
    )
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.session() as session:
            message = Message(
                account_id="university",
                uid_validity=1,
                uid=1,
                subject="原始主题",
                sender_address="sender@example.com",
                recipients=", ".join(f"person{i}@example.org" for i in range(5)),
                received_at=datetime.now(UTC),
                status="completed",
            )
            session.add(message)
            session.flush()
            session.add(
                Analysis(
                    message_id=message.id,
                    model="model",
                    prompt_version="test",
                    title_zh="课程安排有更新",
                    summary_zh="摘要",
                    should_push=True,
                    push_reason="需要即时查看",
                    raw_json="{}",
                )
            )
            session.add(
                Notification(
                    message_id=message.id,
                    channel="wecom_app",
                    status="sent",
                    attempts=1,
                    sent_at=datetime.now(UTC),
                )
            )
            session.add(
                Message(
                    account_id="university",
                    uid_validity=1,
                    uid=2,
                    subject="未推送邮件",
                    sender_address="other@example.com",
                    received_at=datetime.now(UTC),
                    status="completed",
                )
            )
            message_id = message.id

        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "/static/dashboard.js?v=" in dashboard.text

        messages = client.get("/api/v1/messages").json()
        assert messages["total"] == 2
        pushed = next(item for item in messages["items"] if item["id"] == message_id)
        assert pushed["analysis"]["title"] == "课程安排有更新"
        assert pushed["should_push"] is True
        assert pushed["analysis"]["push_reason"] == "需要即时查看"
        assert pushed["recipient_count"] == 5
        assert "person2@example.org" in pushed["recipient_preview"]
        assert "person4@example.org" not in json.dumps(pushed)
        assert "recipients" not in pushed

        push_only = client.get("/api/v1/messages?push=true")
        assert push_only.status_code == 200
        assert [item["id"] for item in push_only.json()["items"]] == [message_id]
        risky_only = client.get("/api/v1/messages?min_risk_score=0.5")
        assert risky_only.status_code == 200
        assert risky_only.json()["items"] == []

        detail = client.get(f"/messages/{message_id}")
        assert 'class="score-badge score-risk"' in detail.text
        assert 'class="score-badge score-importance"' in detail.text
        assert 'class="score-badge score-decision"' in detail.text
        assert "'✓' : '✕'" in detail.text
        assert 'class="analysis-badges"' in detail.text
        assert 'class="analysis-meta"' in detail.text
        assert "/static/message.js?v=" in detail.text
        assert "/static/vendor/marked/marked.umd.js?v=18.0.14" in detail.text
        assert "/static/vendor/dompurify/purify.min.js?v=3.4.16" in detail.text
        assert 'x-html="renderMarkdown(message.text_body' in detail.text
        assert 'x-html="renderMarkdown(translationDraft' in detail.text
        assert client.get("/static/vendor/marked/marked.umd.js").status_code == 200
        assert client.get("/static/vendor/dompurify/purify.min.js").status_code == 200
        assert f'x-data="messagePage({message_id})"' in detail.text
        assert "返回首页" in detail.text
        assert "下载 EML" in detail.text
        assert "展开全部" in detail.text
        assert 'class="recipient-summary"' in detail.text
        assert 'class="recipient-ellipsis"' in detail.text
        assert 'x-for="(address, index) in recipientList"' in detail.text
        assert "复制全部地址" in detail.text
        assert '@click="runTranslation()"' in detail.text
        assert ':disabled="translating || action !== null"' in detail.text
        assert ':disabled="action !== null"' in detail.text
        assert ':disabled="action"' not in detail.text
        assert "←" not in detail.text
        api_detail = client.get(f"/api/v1/messages/{message_id}").json()
        assert api_detail["should_push"] is True
        assert api_detail["recipient_count"] == 5
        assert "person4@example.org" in api_detail["recipient_addresses"]
        assert "recipients" not in api_detail
        assert api_detail["notifications"][0] == {
            "channel": "wecom_app",
            "channel_type": "企业微信应用",
            "target": "demo-user",
            "status": "sent",
            "attempts": 1,
            "sent_at": api_detail["notifications"][0]["sent_at"],
            "last_error": None,
        }


def test_manual_analysis_supports_first_analysis_failure_and_retry(tmp_path, monkeypatch) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        ai={"enabled": True, "base_url": "https://api.example.com/v1"},
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.session() as session:
            message = Message(
                account_id="demo",
                uid_validity=1,
                uid=1,
                subject="Meeting update",
                sender_address="sender@example.com",
                text_body="The meeting starts at 10:00.",
                status="stored",
            )
            session.add(message)
            session.flush()
            message_id = message.id

        analysis_calls = []
        notification_calls = []
        push_decision = True

        async def analyze(message, prompt):
            analysis_calls.append(message.text_body)
            return AnalyzedMessage(
                result=AnalysisResult(
                    title_zh="会议时间更新",
                    summary_zh="会议于 10:00 开始。",
                    category="administrative",
                    importance_score=0.7,
                    risk={"score": 0.1},
                    should_push=push_decision,
                    push_reason="会议安排需要查看",
                ),
                usage=TokenUsage(prompt_tokens=100, completion_tokens=50),
            )

        async def send_notification(*args, **kwargs):
            notification_calls.append((args, kwargs))

        monkeypatch.setattr(client.app.state.pipeline.ai, "analyze", analyze)
        monkeypatch.setattr(client.app.state.pipeline, "send_notification", send_notification)

        before = client.get(f"/api/v1/messages/{message_id}").json()
        assert before["status"] == "stored"
        assert before["analysis"] is None
        assert before["should_push"] is None

        for _ in range(2):
            response = client.post(f"/api/v1/messages/{message_id}/reanalyze")
            assert response.status_code == 200
            assert response.json()["status"] == "completed"
            detail = client.get(f"/api/v1/messages/{message_id}").json()
            assert detail["status"] == "analyzed"
            assert detail["analysis"]["title"] == "会议时间更新"
            assert detail["analysis"]["prompt_tokens"] == 100
            assert detail["should_push"] is True
            assert detail["text_body"] == before["text_body"]
            assert detail["notifications"] == []

        assert analysis_calls == [before["text_body"], before["text_body"]]
        assert notification_calls == []

        async def failing_analysis(message, prompt):
            raise TimeoutError("AI service unavailable")

        monkeypatch.setattr(client.app.state.pipeline.ai, "analyze", failing_analysis)
        failed = client.post(f"/api/v1/messages/{message_id}/reanalyze")
        assert failed.status_code == 200
        assert failed.json() == {"status": "failed", "message_id": message_id}
        detail = client.get(f"/api/v1/messages/{message_id}").json()
        assert detail["status"] == "analysis_failed"
        assert detail["should_push"] is None
        assert notification_calls == []

        monkeypatch.setattr(client.app.state.pipeline.ai, "analyze", analyze)
        push_decision = False
        retried = client.post(f"/api/v1/messages/{message_id}/reanalyze")
        assert retried.json()["status"] == "completed"
        assert client.get(f"/api/v1/messages/{message_id}").json()["should_push"] is False
        assert notification_calls == []
        assert client.post("/api/v1/messages/99999/reanalyze").status_code == 404


def test_message_translation_is_cached_and_can_be_refreshed(tmp_path, monkeypatch) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        ai={"enabled": True, "base_url": "https://api.example.com/v1"},
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.session() as session:
            message = Message(
                account_id="demo",
                uid_validity=1,
                uid=7,
                subject="Meeting update",
                sender_address="sender@example.com",
                text_body=(
                    "The meeting starts at **10:00**.\n\n"
                    "[Details](https://example.org/?a=1&b=2) <script>alert(1)</script>\n\n"
                    "```\n<example>&text</example>\n```"
                ),
                received_at=datetime.now(UTC),
                status="completed",
            )
            session.add(message)
            session.flush()
            message_id = message.id

        translate_calls = 0

        async def stream_translation(message, prompt, *, on_usage=None):
            nonlocal translate_calls
            translate_calls += 1
            assert "**10:00**" in prompt.render(message)
            if on_usage:
                on_usage(TokenUsage(prompt_tokens=20, completion_tokens=8))
            yield "会议于"
            yield "上午十点开始。"

        client.app.state.pipeline.ai.stream_translation = stream_translation

        first = client.post(f"/api/v1/messages/{message_id}/translate")
        assert first.status_code == 200
        first_events = [json.loads(line) for line in first.text.splitlines()]
        assert [event["type"] for event in first_events] == [
            "start",
            "delta",
            "delta",
            "complete",
        ]
        assert first_events[-1]["translation"]["text"] == "会议于上午十点开始。"
        assert (
            "".join(event.get("text", "") for event in first_events if event["type"] == "delta")
            == "会议于上午十点开始。"
        )
        assert translate_calls == 1
        detail = client.get(f"/api/v1/messages/{message_id}").json()
        assert detail["translation"]["text"] == "会议于上午十点开始。"
        assert detail["translation"]["target_language"] == "zh-CN"
        assert detail["translation"]["prompt_tokens"] == 20
        assert detail["translation"]["completion_tokens"] == 8

        original_export = client.get(f"/api/v1/messages/{message_id}/export/original.md")
        assert original_export.status_code == 200
        assert "**10:00**" in original_export.text
        assert "[Details](https://example.org/?a=1&b=2)" in original_export.text
        assert "<script>alert(1)</script>" in original_export.text
        assert "```\n<example>&text</example>\n```" in original_export.text
        assert "attachment;" in original_export.headers["content-disposition"]
        original_metadata = yaml.safe_load(original_export.text.split("---", 2)[1])
        assert original_metadata["content_type"] == "original"
        assert original_metadata["sender_name"] is None
        assert original_metadata["sender_address"] == "sender@example.com"
        assert original_metadata["received_at"].endswith("+08:00")
        assert all(not isinstance(value, dict) for value in original_metadata.values())
        translation_export = client.get(f"/api/v1/messages/{message_id}/export/translation.md")
        assert translation_export.status_code == 200
        assert "会议于上午十点开始。" in translation_export.text
        translation_metadata = yaml.safe_load(translation_export.text.split("---", 2)[1])
        assert translation_metadata["content_type"] == "translation"
        assert translation_metadata["target_language"] == "zh-CN"
        assert translation_metadata["translation_model"] == settings.ai.model
        assert all(not isinstance(value, dict) for value in translation_metadata.values())
        monkeypatch.setattr(
            "inboxping.web.app.fetch_message_eml",
            lambda settings, db, current_id: FetchedEml(
                content=b"Subject: Test\r\n\r\nBody",
                subject="Meeting update",
            ),
        )
        eml_export = client.get(f"/api/v1/messages/{message_id}/export/eml")
        assert eml_export.content == b"Subject: Test\r\n\r\nBody"

        refreshed_mail = EmailMessage()
        refreshed_mail.set_content(
            "<p>The meeting starts at <strong>10:00</strong>. Updated schedule.</p>",
            subtype="html",
        )
        monkeypatch.setattr(
            "inboxping.mail.source_fetch.fetch_message_eml",
            lambda *args: FetchedEml(content=refreshed_mail.as_bytes(), subject="Meeting update"),
        )
        refreshed = client.post(f"/api/v1/messages/{message_id}/refresh-body")
        assert refreshed.status_code == 200
        assert refreshed.json() == {"status": "completed", "message_id": message_id}
        detail = client.get(f"/api/v1/messages/{message_id}").json()
        assert detail["text_body"] == "The meeting starts at **10:00**. Updated schedule."
        assert detail["status"] == "completed"
        assert detail["translation"]["text"] == "会议于上午十点开始。"
        assert detail["notifications"] == []

        def missing_original(*args):
            raise RuntimeError("原邮件已被删除")

        monkeypatch.setattr("inboxping.mail.source_fetch.fetch_message_eml", missing_original)
        failed_refresh = client.post(f"/api/v1/messages/{message_id}/refresh-body")
        assert failed_refresh.status_code == 409
        assert failed_refresh.json()["detail"] == "原邮件已被删除"
        assert client.get(f"/api/v1/messages/{message_id}").json() == detail

        def disconnected_mailbox(*args):
            raise TimeoutError("private connection details")

        monkeypatch.setattr("inboxping.mail.source_fetch.fetch_message_eml", disconnected_mailbox)
        failed_refresh = client.post(f"/api/v1/messages/{message_id}/refresh-body")
        assert failed_refresh.status_code == 502
        assert "private connection details" not in failed_refresh.text
        assert client.get(f"/api/v1/messages/{message_id}").json() == detail
        assert client.post("/api/v1/messages/99999/refresh-body").status_code == 404

        assert client.post(f"/api/v1/messages/{message_id}/translate").status_code == 200
        assert translate_calls == 1
        assert client.post(f"/api/v1/messages/{message_id}/translate?force=true").status_code == 200
        assert translate_calls == 2

        events = client.get("/api/v1/events").json()
        assert sum(event["kind"] == "translation_completed" for event in events) == 2

        deleted = client.delete(f"/api/v1/messages/{message_id}/translation")
        assert deleted.status_code == 200
        assert client.get(f"/api/v1/messages/{message_id}").json()["translation"] is None
        events = client.get("/api/v1/events").json()
        assert any(event["kind"] == "translation_deleted" for event in events)


def test_streaming_translation_reports_failure_without_caching_partial_text(tmp_path) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        ai={"enabled": True, "base_url": "https://api.example.com/v1"},
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.session() as session:
            message = Message(
                account_id="demo",
                uid_validity=1,
                uid=8,
                subject="Long message",
                text_body="A long body",
                received_at=datetime.now(UTC),
                status="completed",
            )
            session.add(message)
            session.flush()
            message_id = message.id

        async def failing_stream(message, prompt, *, on_usage=None):
            yield "部分译文"
            raise TimeoutError

        client.app.state.pipeline.ai.stream_translation = failing_stream
        response = client.post(f"/api/v1/messages/{message_id}/translate")
        events = [json.loads(line) for line in response.text.splitlines()]

        assert [event["type"] for event in events] == ["start", "delta", "error"]
        assert "TimeoutError" in events[-1]["message"]
        detail = client.get(f"/api/v1/messages/{message_id}").json()
        assert detail["translation"] is None
        stored_events = client.get("/api/v1/events").json()
        assert any(event["kind"] == "translation_failed" for event in stored_events)


def test_dashboard_orders_by_displayed_mail_time(tmp_path) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.session() as session:
            session.add_all(
                [
                    Message(
                        account_id="account",
                        uid_validity=1,
                        uid=1,
                        subject="较新的发件时间",
                        sent_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
                        received_at=datetime(2026, 9, 20, 12, 1, tzinfo=UTC),
                        status="completed",
                    ),
                    Message(
                        account_id="account",
                        uid_validity=1,
                        uid=2,
                        subject="较旧的发件时间",
                        sent_at=datetime(2026, 9, 20, 11, 0, tzinfo=UTC),
                        received_at=datetime(2026, 9, 20, 13, 0, tzinfo=UTC),
                        status="completed",
                    ),
                ]
            )

        messages = client.get("/api/v1/messages").json()["items"]

        assert [item["subject"] for item in messages] == [
            "较新的发件时间",
            "较旧的发件时间",
        ]


def test_manual_notification_cannot_bypass_ai_no_push_decision(tmp_path) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.session() as session:
            message = Message(
                account_id="account",
                uid_validity=1,
                uid=1,
                subject="危险邮件",
                received_at=datetime.now(UTC),
                status="analyzed",
            )
            session.add(message)
            session.flush()
            session.add(
                Analysis(
                    message_id=message.id,
                    model="model",
                    prompt_version="test",
                    title_zh="危险邮件",
                    summary_zh="摘要",
                    risk_score=0.9,
                    should_push=False,
                    push_reason="AI 判断无需通知",
                    raw_json="{}",
                )
            )
            message_id = message.id

        response = client.post(f"/api/v1/messages/{message_id}/notify")
        assert response.status_code == 409
        assert "AI 判断无需通知" in response.json()["detail"]


def test_authored_web_assets_do_not_use_dash_or_arrow_placeholders() -> None:
    web_dir = Path("src/inboxping/web")
    paths = [*web_dir.glob("templates/*.html"), *web_dir.glob("static/*.*")]
    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert "—" not in content, path
        assert "←" not in content, path

    dashboard_script = (web_dir / "static/dashboard.js").read_text(encoding="utf-8")
    assert "scrollIntoView" not in dashboard_script


def test_login_is_long_lived_and_preserves_username_after_failure(tmp_path) -> None:
    digest = PasswordHash.recommended().hash("correct-password")
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={
            "username": "admin",
            "password_hash": digest,
            "session_secret": "test-secret",
        },
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        failed = client.post("/login", data={"username": "admin", "password": "wrong-password"})
        assert client.post("/api/v1/messages/1/refresh-body").status_code == 401
        assert client.post("/api/v1/messages/1/reanalyze").status_code == 401
        assert client.post("/api/v1/messages/1/notify").status_code == 401
        assert failed.status_code == 401
        assert 'value="admin"' in failed.text

        logged_in = client.post(
            "/login",
            data={"username": "admin", "password": "correct-password"},
            follow_redirects=False,
        )
        assert logged_in.status_code == 303
        assert "Max-Age=34560000" in logged_in.headers["set-cookie"]

        dashboard = client.get("/")
        assert "已登录：admin" in dashboard.text
        assert "匿名访问" not in dashboard.text
        assert "Max-Age=34560000" in dashboard.headers["set-cookie"]


def test_login_lifetime_can_be_configured(tmp_path) -> None:
    digest = PasswordHash.recommended().hash("correct-password")
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={
            "username": "admin",
            "password_hash": digest,
            "session_secret": "test-secret",
            "session_max_age_days": 30,
            "session_rolling": False,
        },
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/login",
            data={"username": "admin", "password": "correct-password"},
            follow_redirects=False,
        )
        assert "Max-Age=2592000" in response.headers["set-cookie"]
        assert "set-cookie" not in client.get("/").headers
