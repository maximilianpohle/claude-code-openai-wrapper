"""
Claude subscription login through the API.

Drives the official `claude setup-token` command (bundled with claude-agent-sdk) in a
pseudo-terminal, so a container can be logged in without a browser and without mounting
the host's ~/.claude:

1. start_login()     -> returns the authorize URL to open in any browser
2. complete_login()  -> passes the code shown after sign-in to the CLI, stores the token

The resulting long-lived token is written to TOKEN_FILE (mode 600) and exported as
CLAUDE_CODE_OAUTH_TOKEN, which every Claude Agent SDK subprocess inherits.
The token itself is never returned or logged.
"""

import asyncio
import fcntl
import logging
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_FILE = Path(
    os.getenv("CLAUDE_TOKEN_FILE", str(Path.home() / ".claude-wrapper" / "oauth_token"))
)
LOGIN_TIMEOUT_SECONDS = 600  # an unfinished login is discarded after 10 minutes
URL_WAIT_SECONDS = 30
TOKEN_WAIT_SECONDS = 60
PASTE_SETTLE_SECONDS = 0.5  # pause between pasting the code and pressing Enter

# Terminal control sequences (colors, cursor movement). Replaced by a space, because the
# CLI's UI positions words with cursor moves instead of literal spaces.
_ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
_URL = re.compile(r"https://\S+/oauth/authorize\?\S+")
_TOKEN = re.compile(r"sk-ant-oat\d*-[A-Za-z0-9_\-]+")
_ERROR = re.compile(r"(?:OAuth )?[Ee]rror: (?:(?! Press Enter)[^.])*")
_TOKEN_OR_ERROR = re.compile(f"{_TOKEN.pattern}|{_ERROR.pattern}")

# Token passed in via environment (e.g. docker-compose); restored on logout
_ENV_TOKEN_AT_STARTUP = os.getenv(TOKEN_ENV)


def _redact(text: str) -> str:
    """Mask anything token-like before it goes to a log."""
    return re.sub(r"sk-ant-\S+", "[REDACTED]", text)


class LoginError(Exception):
    """Login could not be started or completed."""


def find_cli() -> str:
    """Path of the Claude CLI: the one bundled with claude-agent-sdk, else from PATH."""
    try:
        import claude_agent_sdk

        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
        if bundled.exists():
            return str(bundled)
    except ImportError:
        pass
    found = shutil.which("claude")
    if not found:
        raise LoginError("Claude CLI not found")
    return found


class _LoginProcess:
    """One running `claude setup-token` attached to a pseudo-terminal."""

    def __init__(self, cli: str):
        master, slave = pty.openpty()
        # Very wide terminal so the URL and the token are never wrapped across lines
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 1000, 0, 0))
        env = {k: v for k, v in os.environ.items() if k != TOKEN_ENV}
        env.update({"TERM": "xterm-256color", "BROWSER": "none"})
        self.proc = subprocess.Popen(
            [cli, "setup-token"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)
        self.fd = master
        self.started = time.monotonic()
        self._raw = b""

    @property
    def text(self) -> str:
        return " ".join(_ANSI.sub(" ", self._raw.decode(errors="replace")).split())

    def expired(self) -> bool:
        return time.monotonic() - self.started > LOGIN_TIMEOUT_SECONDS

    def wait_for(self, pattern: re.Pattern, timeout: float, since: int = 0) -> Optional[str]:
        """
        Read CLI output until `pattern` appears (in the output after position `since`),
        the CLI exits or the timeout passes.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = pattern.search(self.text, since)
            if match:
                return match.group(0)
            ready, _, _ = select.select([self.fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:  # CLI exited, pty closed
                    chunk = b""
                if not chunk:
                    break
                self._raw += chunk
        match = pattern.search(self.text, since)
        return match.group(0) if match else None

    def send_line(self, line: str) -> None:
        """
        Type `line` and press Enter. Enter must arrive as its own write: the CLI's input
        field treats a multi-character chunk as pasted text, so an Enter inside the same
        chunk would be swallowed and the (long) code never submitted.
        """
        os.write(self.fd, line.encode())
        time.sleep(PASTE_SETTLE_SECONDS)
        os.write(self.fd, b"\r")

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


_current: Optional[_LoginProcess] = None
_lock = asyncio.Lock()


def _discard_current() -> None:
    global _current
    if _current is not None:
        _current.close()
        _current = None


def store_token(token: str) -> None:
    """Persist the token (owner-only file) and activate it for new SDK calls."""
    TOKEN_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.environ[TOKEN_ENV] = token


def load_stored_token() -> bool:
    """At startup: a token from an earlier API login takes precedence over the env var."""
    try:
        token = TOKEN_FILE.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return False
    if not token:
        return False
    os.environ[TOKEN_ENV] = token
    logger.info("Using Claude OAuth token from earlier API login")
    return True


async def start_login() -> str:
    """Start `claude setup-token` and return the URL the user has to open."""
    global _current
    async with _lock:
        _discard_current()
        process = _LoginProcess(find_cli())
        url = await asyncio.to_thread(process.wait_for, _URL, URL_WAIT_SECONDS)
        if not url:
            output = process.text[-300:]
            process.close()
            logger.error(f"Claude login: no authorize URL from CLI. Output: {output}")
            raise LoginError("Claude CLI did not provide a login URL")
        _current = process
        return url


async def complete_login(code: str) -> None:
    """Pass the code from the browser to the CLI and store the resulting token."""
    async with _lock:
        process = _current
        if process is None:
            raise LoginError("No login in progress. Call /v1/claude-login/start first.")
        if process.expired():
            _discard_current()
            raise LoginError("Login expired. Call /v1/claude-login/start again.")
        since = len(process.text)
        await asyncio.to_thread(process.send_line, code.strip())
        result = await asyncio.to_thread(
            process.wait_for, _TOKEN_OR_ERROR, TOKEN_WAIT_SECONDS, since
        )
        output = _redact(process.text[since:])
        _discard_current()
        if not result or not _TOKEN.fullmatch(result):
            reason = result or "no answer from Claude CLI"
            logger.warning(f"Claude login failed: {reason}. CLI output: {output[-500:]}")
            raise LoginError(f"Login failed ({reason}). Call /v1/claude-login/start again.")
        store_token(result)
        logger.info("Claude login completed via API")


def status() -> dict:
    if TOKEN_FILE.exists():
        source = "api_login"
    elif os.getenv(TOKEN_ENV):
        source = "environment"
    else:
        source = "none"
    return {
        "oauth_token_source": source,
        "login_in_progress": _current is not None and not _current.expired(),
    }


async def logout() -> bool:
    """Remove the stored token. Returns True if one was stored."""
    async with _lock:
        _discard_current()
        removed = False
        try:
            TOKEN_FILE.unlink()
            removed = True
        except FileNotFoundError:
            pass
        if _ENV_TOKEN_AT_STARTUP:
            os.environ[TOKEN_ENV] = _ENV_TOKEN_AT_STARTUP
        else:
            os.environ.pop(TOKEN_ENV, None)
        return removed
