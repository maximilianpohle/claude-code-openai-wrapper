# AGENTS.md

Guide for coding agents working in this repo. The README is for users; this file is for changes.

## What this is

FastAPI server that exposes Claude Code (via `claude-agent-sdk`) as an OpenAI-compatible
(`/v1/chat/completions`, `/v1/models`) and Anthropic-compatible (`/v1/messages`) API.
Every request starts a Claude Code CLI subprocess; there is no direct model API call.

## Commands

```bash
poetry install                         # or: python -m venv + pip, deps are in pyproject.toml
poetry run uvicorn src.main:app --port 8000
PYTHONPATH=. pytest tests -q           # run from the repo root; without PYTHONPATH "src" is not found
black src tests                        # line length 100; CI runs `black --check src tests`
bandit -r src/ -ll                     # CI security scan
```

- Many tests (`test_anthropic_messages.py`, `test_endpoints.py`, `test_basic.py`, ...) are
  integration tests against a server on `localhost:8000` and are **skipped** without one.
  `test_security_hardening.py`, `test_tool_calling.py` and `test_claude_login.py` use
  `TestClient` and a mocked `claude_cli.run_completion`; copy their fixtures for new endpoint tests.
- Tests that call `importlib.reload(src.auth)` replace `auth_manager`. Patch it as
  `src.auth.auth_manager` at fixture time, never via a name imported at module load.
- Disable the rate limiter in endpoint tests (`monkeypatch.setattr(main.limiter, "enabled", False)`).

## Layout (`src/`)

| File | Job |
|---|---|
| `main.py` | App, routes, middleware, chat flow (`prepare_chat_call`, `apply_tool_policy`, `run_tool_call`) |
| `claude_cli.py` | Wraps `claude_agent_sdk.query`; `run_completion` yields message dicts |
| `tool_calling.py` | OpenAI `tools`/`tool_choice` emulated via SDK structured output |
| `claude_login.py`, `login_page.py` | Claude subscription login through the API + browser page (`/claude-login`) |
| `auth.py` | API-key checks (`verify_api_key`, `verify_docs_access`), Claude auth detection |
| `models.py` | Pydantic request/response models (also the source of the Swagger docs) |
| `debug_dump.py` | `DEBUG_DUMP_DIR` request/response dumps |
| `mcp_client.py`, `tool_manager.py` | MCP and tool management endpoints; not wired into chat, treat as dead weight |

## Rules that are easy to break

- **Protect every new endpoint.** Call `verify_api_key(request, credentials)` (or the
  `claude_login_access` dependency for browser-facing routes). A `Depends(security)` parameter
  alone does **not** check anything. Add the route to `PROTECTED_ENDPOINTS` in `test_security_hardening.py`.
- **Tools are opt-in.** Without `enable_tools` / `X-Claude-Enable-Tools: true`, `apply_tool_policy`
  must pass `tools=[]` (no built-in tools). A deny-list is not enough: newer CLI versions add
  tools, and a call to an unknown one needs a second turn, which fails with `max_turns=1`.
  Enabled tools run with `bypassPermissions` (no prompts, Bash included): never make that the default.
- **Never filter the user's prompt.** `MessageAdapter.filter_content` is for Claude's *output* only.
- **No secrets in logs or responses.** Use `redact_headers`; the OAuth token is never returned;
  errors to clients are generic (`internal_error`), details go to the log.
- Compare keys with `hmac.compare_digest` (`_keys_match`).
- CORS: `allow_credentials` only with explicit origins, never with `*`.
- Request bodies need `Content-Length` (chunked uploads get 411); max size `MAX_REQUEST_SIZE`.
- Docs routes (`/docs`, `/redoc`, `/openapi.json`) are custom and protected; FastAPI's defaults are disabled.

## Function calling (`tool_calling.py`)

Claude Code cannot return client-side tool calls, so the client's function schemas become an
`output_format` JSON schema and the structured answer is translated to `tool_calls`.
The CLI insists on its internal `StructuredOutput` tool; models sometimes answer with plain JSON
instead, so `parse_response` also reads JSON from assistant texts (`ClaudeCodeCLI.assistant_texts`,
which skips CLI hook messages). `$defs` of all functions are hoisted to the schema root.
Debug with `DEBUG_MODE=true DEBUG_DUMP_DIR=...` (see README "Debugging").

## SDK / models

- `claude-agent-sdk` bundles the Claude Code CLI; the CLI version decides which models work.
  Bump the SDK (in `pyproject.toml` and `poetry.lock`) when new models are missing.
- `/v1/models` serves Anthropic's live list only with `ANTHROPIC_API_KEY`; otherwise the static
  `DEFAULT_CLAUDE_MODELS` in `constants.py`. Update it when new models appear.
- SDK failures do not surface as HTTP errors in streaming/plain chat (the response is still 200);
  look for `Claude Agent SDK error` in the logs.

## Docker

- Multi-stage image, runs as non-root user `app` (UID/GID build args, default 1000).
- Claude login: `/claude-login` page, `CLAUDE_CODE_OAUTH_TOKEN`, or `ANTHROPIC_API_KEY`.
  The API login stores its token in `/home/app/.claude-wrapper` (volume `claude-auth`).
- `docker` on the dev machine may be Podman: use `--userns=keep-id` when bind-mounting host dirs.
- Port is bound to `127.0.0.1` in `docker-compose.yml`; without `API_KEY` every endpoint is open.
- CI: `.github/workflows/ci.yml` (black, bandit, pytest), `docker.yml` (build, smoke test, push to ghcr.io).

## Style

Python 3.10+ compatible, black (100 cols). Keep changes small; delete dead code instead of
adding flags. Add a test next to any security-relevant change.
