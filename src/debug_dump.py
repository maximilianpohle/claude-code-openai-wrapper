"""
Optional request/response dumps for debugging (env DEBUG_DUMP_DIR).

One JSON file per event: <timestamp>_<request id>_<name>.json, owner-only (mode 600).
Dumps contain the full prompts and Claude's answers, i.e. possibly sensitive data, and are
never written unless DEBUG_DUMP_DIR is set. Authorization headers are never included.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _dump_dir() -> Optional[Path]:
    directory = os.getenv("DEBUG_DUMP_DIR", "").strip()
    return Path(directory) if directory else None


def enabled() -> bool:
    return _dump_dir() is not None


def dump(request_id: str, name: str, payload: Any) -> None:
    """Write `payload` as JSON; never raises (debugging must not break requests)."""
    directory = _dump_dir()
    if directory is None:
        return
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        safe_id = "".join(c for c in request_id if c.isalnum() or c in "-_")[:64]
        path = directory / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe_id}_{name}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=repr)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not write debug dump: {e}")
