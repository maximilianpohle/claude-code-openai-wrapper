"""
Tests for the Claude login via API (src/claude_login.py + /v1/claude-login endpoints).

A fake CLI replaces `claude setup-token`: it prints an authorize URL (with terminal
escape codes, like the real UI), reads the code and prints a token for "good#state".
"""

import os
import stat
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

import src.auth
import src.main as main
from src import claude_login

FAKE_TOKEN = "sk-ant-oat01-FAKE_token-123"

FAKE_CLI = textwrap.dedent(
    f"""\
    #!{sys.executable}
    # Behaves like the input field of the real CLI: raw terminal, and an Enter that
    # arrives in the same chunk as other text counts as pasted text, not as submit.
    import os, sys, tty
    assert sys.argv[1:] == ["setup-token"], sys.argv
    print("\\x1b[1mWelcome\\x1b[0m Browser didn't open? Use the url below to sign in")
    print("https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz")
    print("Paste code here if prompted > ", end="")
    sys.stdout.flush()
    tty.setraw(0)
    code = b""
    while True:
        chunk = os.read(0, 4096)
        if chunk == b"\\r":
            break
        code += chunk.replace(b"\\r", b"")
    code = code.decode().strip()
    if code == "good#state":
        print("\\r\\nLong-lived authentication token created\\r")
        print("Your OAuth token (valid for 1 year):\\r")
        print("\\x1b[33m{FAKE_TOKEN}\\x1b[0m\\r")
    else:
        print("\\r\\nOAuth error: That code wasn't accepted. Press Enter to retry.\\r")
        sys.stdin.read()
    """
)


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    cli = tmp_path / "claude"
    cli.write_text(FAKE_CLI)
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(claude_login, "find_cli", lambda: str(cli))
    monkeypatch.setattr(claude_login, "TOKEN_FILE", tmp_path / "state" / "oauth_token")
    monkeypatch.setattr(claude_login, "_ENV_TOKEN_AT_STARTUP", None)
    monkeypatch.delenv(claude_login.TOKEN_ENV, raising=False)
    yield cli
    claude_login._discard_current()


@pytest.fixture
def client(monkeypatch):
    if main.limiter:
        monkeypatch.setattr(main.limiter, "enabled", False)
    monkeypatch.setattr(src.auth.auth_manager, "env_api_key", None)
    monkeypatch.setattr(main, "runtime_api_key", None)
    return TestClient(main.app)


def test_full_login_flow(client, fake_cli):
    assert client.get("/v1/claude-login").json()["oauth_token_source"] == "none"

    r = client.post("/v1/claude-login/start")
    assert r.status_code == 200
    assert r.json()["authorize_url"].startswith("https://claude.com/cai/oauth/authorize?")
    assert client.get("/v1/claude-login").json()["login_in_progress"] is True

    r = client.post("/v1/claude-login/complete", json={"code": "good#state"})
    assert r.status_code == 200
    assert r.json()["oauth_token_source"] == "api_login"
    assert FAKE_TOKEN not in r.text  # token is never returned

    assert os.environ[claude_login.TOKEN_ENV] == FAKE_TOKEN
    assert claude_login.TOKEN_FILE.read_text() == FAKE_TOKEN
    assert stat.S_IMODE(claude_login.TOKEN_FILE.stat().st_mode) == 0o600

    r = client.delete("/v1/claude-login")
    assert r.json()["removed"] is True
    assert r.json()["oauth_token_source"] == "none"
    assert claude_login.TOKEN_ENV not in os.environ


def test_wrong_code_is_rejected(client, fake_cli):
    client.post("/v1/claude-login/start")
    r = client.post("/v1/claude-login/complete", json={"code": "wrong"})
    assert r.status_code == 400
    assert "wasn't accepted" in r.json()["error"]["message"]
    assert not claude_login.TOKEN_FILE.exists()
    assert claude_login.TOKEN_ENV not in os.environ


def test_long_code_is_submitted(client, fake_cli):
    """Real codes are long; Enter must still be recognised (regression)."""
    client.post("/v1/claude-login/start")
    r = client.post("/v1/claude-login/complete", json={"code": "x" * 120 + "#state"})
    assert r.status_code == 400
    assert "wasn't accepted" in r.json()["error"]["message"]  # answered, not timed out


def test_complete_without_start(client, fake_cli):
    r = client.post("/v1/claude-login/complete", json={"code": "good#state"})
    assert r.status_code == 400
    assert "start" in r.json()["error"]["message"]


def test_expired_login(client, fake_cli, monkeypatch):
    client.post("/v1/claude-login/start")
    monkeypatch.setattr(claude_login, "LOGIN_TIMEOUT_SECONDS", -1)
    r = client.post("/v1/claude-login/complete", json={"code": "good#state"})
    assert r.status_code == 400
    assert "expired" in r.json()["error"]["message"]


def test_stored_token_loaded_at_startup(fake_cli):
    claude_login.TOKEN_FILE.parent.mkdir(parents=True)
    claude_login.TOKEN_FILE.write_text(FAKE_TOKEN + "\n")
    assert claude_login.load_stored_token() is True
    assert os.environ[claude_login.TOKEN_ENV] == FAKE_TOKEN


def test_logout_restores_env_token(client, fake_cli, monkeypatch):
    monkeypatch.setattr(claude_login, "_ENV_TOKEN_AT_STARTUP", "from-compose")
    client.post("/v1/claude-login/start")
    client.post("/v1/claude-login/complete", json={"code": "good#state"})
    client.delete("/v1/claude-login")
    assert os.environ[claude_login.TOKEN_ENV] == "from-compose"


def test_endpoints_require_api_key(client, fake_cli, monkeypatch):
    monkeypatch.setattr(src.auth.auth_manager, "env_api_key", "secret-key-123")
    assert client.get("/v1/claude-login").status_code == 401
    assert client.post("/v1/claude-login/start").status_code == 401
    assert client.post("/v1/claude-login/complete", json={"code": "x"}).status_code == 401
    assert client.delete("/v1/claude-login").status_code == 401


def _basic(password: str) -> dict:
    import base64

    return {"Authorization": "Basic " + base64.b64encode(f"user:{password}".encode()).decode()}


class TestLoginPage:
    def test_page_served(self, client, fake_cli):
        r = client.get("/claude-login")
        assert r.status_code == 200
        assert "Mit Claude anmelden" in r.text
        assert r.headers["x-frame-options"] == "DENY"
        assert "no-store" in r.headers["cache-control"]

    def test_page_and_api_accept_browser_login(self, client, fake_cli, monkeypatch):
        monkeypatch.setattr(src.auth.auth_manager, "env_api_key", "secret-key-123")
        r = client.get("/claude-login")
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Basic")
        assert client.get("/claude-login", headers=_basic("secret-key-123")).status_code == 200
        # The page's fetch() calls carry the same Basic credentials
        r = client.post("/v1/claude-login/start", headers=_basic("secret-key-123"))
        assert r.status_code == 200
        assert client.get("/v1/claude-login", headers=_basic("wrong")).status_code == 401

    def test_cross_origin_requests_rejected(self, client, fake_cli):
        r = client.post("/v1/claude-login/start", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = client.delete("/v1/claude-login", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        assert not claude_login.status()["login_in_progress"]

    def test_same_origin_requests_allowed(self, client, fake_cli):
        # TestClient sends Host: testserver
        r = client.post("/v1/claude-login/start", headers={"Origin": "http://testserver"})
        assert r.status_code == 200

    def test_landing_page_links_login(self, client):
        assert 'href="/claude-login"' in client.get("/").text
