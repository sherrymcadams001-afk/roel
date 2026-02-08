"""Cross-platform atomic file write utilities.

Provides a single ``atomic_write_json`` used by every subsystem that persists
state to disk (Archivist profiles, approval queues, deferred outbox, etc.).

Guarantees:
- File is written to a temporary sibling first, then atomically renamed.
- ``os.replace`` is atomic on both POSIX and Windows (Python 3.3+).
- Parent directories are created on demand.
- Encoding is always UTF-8.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> None:
    """Atomically write *data* as JSON to *path*.

    1. Serialize to a ``.tmp`` sibling.
    2. ``os.replace`` the target (atomic on all platforms).
    3. On failure the tmp file is cleaned up; the original is untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    try:
        tmp.write_text(
            json.dumps(data, indent=indent, ensure_ascii=ensure_ascii, default=str),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(path))
    except Exception:
        # Clean up partial tmp on failure; never leave orphan .tmp files.
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
