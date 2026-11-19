"""
Tests for API-key protection of management endpoints and the interactive docs.

Uses FastAPI's TestClient (no running server or Claude backend required).
"""

import base64

import pytest
from fastapi.testclient import TestClient

import src.auth
import src.main as main
from src.auth import redact_headers

API_KEY = "test-secret-key-1234567890"

PROTECTED_ENDPOINTS = [
    ("get", "/v1/sessions"),
    ("get", "/v1/sessions/stats"),
    ("get", "/v1/sessions/does-not-exist"),
    ("delete", "/v1/sessions/does-not-exist"),
    ("post", "/v1/debug/request"),
    ("post", "/v1/compatibility"),
    ("get", "/v1/models"),
    ("get", "/v1/tools"),
    ("get", "/v1/mcp/servers"),
    ("get", "/v1/auth/status"),
]

DOCS_ENDPOINTS = ["/docs", "/redoc", "/openapi.json"]

COMPAT_BODY = {"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": "hi"}]}


def _basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client(monkeypatch):
    # Rate limits are not under test here and would make results order-dependent.
    if main.limiter:
        monkeypatch.setattr(main.limiter, "enabled", False)
    # Keep /v1/models offline (no live Anthropic Models API call).
    monkeypatch.setenv("CLAUDE_MODELS_OVERRIDE", "claude-sonnet-4-6")
    # No lifespan (no `with`), so no CLI verification / background tasks are started.
    return TestClient(main.app)


# Other test modules call importlib.reload(src.auth), which replaces the module-level
# auth_manager. Always resolve it at fixture time so we patch the live instance.
@pytest.fixture
def with_api_key(monkeypatch):
    monkeypatch.setattr(src.auth.auth_manager, "env_api_key", API_KEY)
    monkeypatch.setattr(main, "runtime_api_key", None)
    yield


@pytest.fixture
def without_api_key(monkeypatch):
    monkeypatch.setattr(src.auth.auth_manager, "env_api_key", None)
    monkeypatch.setattr(main, "runtime_api_key", None)
    yield


def _call(client, method, path, headers=None):
    kwargs = {"headers": headers or {}}
    if method == "post":
        kwargs["json"] = COMPAT_BODY
    return getattr(client, method)(path, **kwargs)


class TestProtectedEndpoints:
    @pytest.mark.parametrize("method,path", PROTECTED_ENDPOINTS)
    def test_missing_key_rejected(self, client, with_api_key, method, path):
        assert _call(client, method, path).status_code == 401

    @pytest.mark.parametrize("method,path", PROTECTED_ENDPOINTS)
    def test_wrong_key_rejected(self, client, with_api_key, method, path):
        r = _call(client, method, path, {"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    @pytest.mark.parametrize("method,path", PROTECTED_ENDPOINTS)
    def test_valid_key_accepted(self, client, with_api_key, method, path):
        r = _call(client, method, path, {"Authorization": f"Bearer {API_KEY}"})
        assert r.status_code != 401 and r.status_code < 500

    @pytest.mark.parametrize("method,path", PROTECTED_ENDPOINTS)
    def test_open_when_no_key_configured(self, client, without_api_key, method, path):
        r = _call(client, method, path)
        assert r.status_code != 401 and r.status_code < 500

    def test_public_endpoints_stay_public(self, client, with_api_key):
        assert client.get("/health").status_code == 200
        assert client.get("/version").status_code == 200


class TestDocsProtection:
    @pytest.mark.parametrize("path", DOCS_ENDPOINTS)
    def test_docs_require_auth(self, client, with_api_key, path):
        r = client.get(path)
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Basic")

    @pytest.mark.parametrize("path", DOCS_ENDPOINTS)
    def test_docs_basic_auth_accepted(self, client, with_api_key, path):
        assert client.get(path, headers=_basic("anyone", API_KEY)).status_code == 200

    @pytest.mark.parametrize("path", DOCS_ENDPOINTS)
    def test_docs_bearer_accepted(self, client, with_api_key, path):
        r = client.get(path, headers={"Authorization": f"Bearer {API_KEY}"})
        assert r.status_code == 200

    @pytest.mark.parametrize(
        "headers",
        [
            _basic("admin", "wrong"),
            {"Authorization": "Basic !!!not-base64!!!"},
            {"Authorization": "Basic " + base64.b64encode(b"no-colon").decode()},
            {"Authorization": "Bearer wrong"},
        ],
    )
    def test_docs_bad_credentials_rejected(self, client, with_api_key, headers):
        assert client.get("/docs", headers=headers).status_code == 401

    @pytest.mark.parametrize("path", DOCS_ENDPOINTS)
    def test_docs_open_without_key(self, client, without_api_key, path):
        assert client.get(path).status_code == 200

    def test_openapi_schema_contents(self, client, without_api_key):
        schema = client.get("/openapi.json").json()
        assert "/v1/chat/completions" in schema["paths"]
        # Docs routes themselves are not part of the schema
        assert "/docs" not in schema["paths"]


class TestHeaderRedaction:
    def test_redact_headers(self):
        out = redact_headers(
            {"Authorization": "Bearer x", "Cookie": "a=b", "X-Api-Key": "k", "Accept": "*/*"}
        )
        assert out["Authorization"] == "[REDACTED]"
        assert out["Cookie"] == "[REDACTED]"
        assert out["X-Api-Key"] == "[REDACTED]"
        assert out["Accept"] == "*/*"

    def test_debug_endpoint_does_not_echo_key(self, client, with_api_key):
        r = client.post(
            "/v1/debug/request",
            json=COMPAT_BODY,
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert r.status_code == 200
        assert API_KEY not in r.text
        assert r.json()["debug_info"]["headers"]["authorization"] == "[REDACTED]"


class TestRequestSizeMiddleware:
    def test_invalid_content_length_is_400_not_500(self, client, without_api_key):
        r = client.post(
            "/v1/compatibility",
            content=b"{}",
            headers={"Content-Length": "abc", "Content-Type": "application/json"},
        )
        assert r.status_code == 400

    def test_oversized_request_rejected(self, client, without_api_key, monkeypatch):
        monkeypatch.setattr(main, "MAX_REQUEST_SIZE", 10)
        r = client.post("/v1/compatibility", json=COMPAT_BODY)
        assert r.status_code == 413


class TestBodyWithoutContentLength:
    def test_chunked_body_rejected(self, client, without_api_key):
        def gen():
            yield b'{"model": "x", '
            yield b'"messages": []}'

        r = client.post(
            "/v1/compatibility", content=gen(), headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 411

    def test_post_without_body_allowed(self, client, without_api_key):
        # e.g. `curl -X POST` without -d sends neither Content-Length nor Transfer-Encoding
        r = client.post("/v1/claude-login")  # any route; only the middleware matters here
        assert r.status_code != 411


class TestCors:
    def test_wildcard_origin_without_credentials(self, client):
        r = client.options(
            "/v1/models",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.headers.get("access-control-allow-credentials") != "true"


# ---------------------------------------------------------------------------
# Chat endpoints with a mocked Claude SDK (no server / no credentials needed)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_claude(monkeypatch):
    """Replace claude_cli.run_completion and record the arguments it was called with."""
    calls = []

    async def fake_run_completion(**kwargs):
        calls.append(kwargs)
        yield {"content": [{"type": "text", "text": "Antwort von Claude"}]}

    monkeypatch.setattr(main.claude_cli, "run_completion", fake_run_completion)
    monkeypatch.setattr(main, "validate_claude_code_auth", lambda: (True, {"method": "test"}))
    return calls


def _chat(client, content, **extra):
    body = {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": content}]}
    body.update(extra)
    return client.post("/v1/chat/completions", json=body)


class TestChatCompletions:
    def test_tools_disabled_by_default(self, client, without_api_key, fake_claude):
        r = _chat(client, "Hallo")
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "Antwort von Claude"
        call = fake_claude[0]
        assert call["allowed_tools"] is None
        assert "Bash" in call["disallowed_tools"]
        assert call["permission_mode"] is None
        assert call["max_turns"] == 1
        assert call["tools"] == []  # no built-in tools at all (deny-list is not enough)

    def test_tools_enabled_on_request(self, client, without_api_key, fake_claude):
        r = _chat(client, "Hallo", enable_tools=True)
        assert r.status_code == 200
        call = fake_claude[0]
        assert "Bash" in call["allowed_tools"]
        assert call["disallowed_tools"] is None
        assert call["tools"] is None  # default tool set
        assert call["permission_mode"] == "bypassPermissions"

    def test_permission_header_ignored_without_tools(self, client, without_api_key, fake_claude):
        r = client.post(
            "/v1/chat/completions",
            json={"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "x"}]},
            headers={"X-Claude-Permission-Mode": "bypassPermissions"},
        )
        assert r.status_code == 200
        assert fake_claude[0]["permission_mode"] is None

    def test_prompt_is_not_filtered(self, client, without_api_key, fake_claude):
        text = "Was bedeutet <question>2+2?</question> und <args>--force</args>?"
        assert _chat(client, text).status_code == 200
        assert text in fake_claude[0]["prompt"]

    def test_streaming_uses_same_preparation(self, client, without_api_key, fake_claude):
        r = _chat(client, "Hallo", stream=True)
        assert r.status_code == 200
        assert "Antwort von Claude" in r.text
        assert r.text.rstrip().endswith("data: [DONE]")
        assert fake_claude[0]["max_turns"] == 1

    def test_internal_error_is_generic(self, client, without_api_key, monkeypatch):
        async def boom(**kwargs):
            raise RuntimeError("secret internal path /root/.claude")
            yield  # pragma: no cover

        monkeypatch.setattr(main.claude_cli, "run_completion", boom)
        monkeypatch.setattr(main, "validate_claude_code_auth", lambda: (True, {}))
        r = _chat(client, "Hallo")
        assert r.status_code == 500
        assert "secret" not in r.text
        assert "request id" in r.text


class TestAnthropicMessages:
    BODY = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Hallo"}],
    }

    def test_tools_disabled_by_default(self, client, without_api_key, fake_claude):
        r = client.post("/v1/messages", json=self.BODY)
        assert r.status_code == 200
        assert r.json()["content"][0]["text"] == "Antwort von Claude"
        call = fake_claude[0]
        assert call["allowed_tools"] is None
        assert "Bash" in call["disallowed_tools"]
        assert call["tools"] == []
        assert call["permission_mode"] is None

    def test_tools_enabled_with_header(self, client, without_api_key, fake_claude):
        r = client.post("/v1/messages", json=self.BODY, headers={"X-Claude-Enable-Tools": "true"})
        assert r.status_code == 200
        call = fake_claude[0]
        assert "Bash" in call["allowed_tools"]
        assert call["permission_mode"] == "bypassPermissions"

    def test_stream_rejected(self, client, without_api_key, fake_claude):
        r = client.post("/v1/messages", json={**self.BODY, "stream": True})
        assert r.status_code == 400
        assert fake_claude == []
