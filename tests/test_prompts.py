from pathlib import Path

from inboxping.ai.prompts import load_prompt, load_translation_prompt
from inboxping.models import Message


def test_prompt_version_changes(tmp_path: Path) -> None:
    (tmp_path / "analysis.system.txt").write_text(
        Path("prompts/analysis.system.txt").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "analysis.user.txt").write_text(
        Path("prompts/analysis.user.txt").read_text(encoding="utf-8"), encoding="utf-8"
    )
    first = load_prompt(tmp_path)
    (tmp_path / "analysis.system.txt").write_text("changed", encoding="utf-8")
    second = load_prompt(tmp_path)
    assert first.version != second.version


def test_prompt_render() -> None:
    prompt = load_prompt(Path("prompts"))
    message = Message(
        subject="Test",
        sender_name="A",
        sender_address="a@example.com",
        recipients="b@example.com",
        attachments_json="[]",
        text_body="Body",
    )
    rendered = prompt.render(message)
    assert "Test" in rendered
    assert "Body" in rendered


def test_translation_prompt_render() -> None:
    prompt = load_translation_prompt(Path("prompts"))
    message = Message(
        subject="Meeting update",
        sender_name="Example Sender",
        sender_address="sender@example.com",
        text_body="The meeting starts at 10:00.",
    )
    rendered = prompt.render(message)
    assert "Meeting update" in rendered
    assert "The meeting starts at 10:00." in rendered
    assert prompt.version


def test_missing_prompt_file_is_rejected(tmp_path: Path) -> None:
    try:
        load_prompt(tmp_path)
    except FileNotFoundError as exc:
        assert "analysis.system.txt" in str(exc)
    else:
        raise AssertionError("missing prompt should fail")
