import pytest

from inboxping.ai.client import AIClient
from inboxping.config import Settings


def test_ai_output_limit_is_cumulative() -> None:
    client = AIClient(Settings(ai={"max_output_chars": 1000}))

    total = client._count_output(0, "a" * 600)
    assert total == 600
    with pytest.raises(RuntimeError, match="1000 字符"):
        client._count_output(total, "b" * 401)
