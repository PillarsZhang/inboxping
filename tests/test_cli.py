from pwdlib import PasswordHash
from typer.testing import CliRunner

from inboxping.cli import _public_http_url, app


def test_hash_password_uses_hidden_interactive_input() -> None:
    result = CliRunner().invoke(app, ["hash-password"], input="private-value\nprivate-value\n")

    assert result.exit_code == 0
    assert "private-value" not in result.output
    digest = result.output.strip().splitlines()[-1]
    assert PasswordHash.recommended().verify("private-value", digest)


def test_public_http_url_removes_credentials_query_and_fragment() -> None:
    value = "https://user:password@api.example.com:8443/v1?token=private#fragment"

    assert _public_http_url(value) == "https://api.example.com:8443/v1"
