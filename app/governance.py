"""Write allow list and folder checks, from the vault restructure brief v3.3 (15.6).

The agent's write scope is exactly one Drive folder: Knowledge/reading/, the
folder READING_LIBRARY_FOLDER_ID points at. Two rules are enforced here:

1. Every file the agent creates must have that folder as its only parent.
   Anything else is refused and logged.
2. At startup the agent records the path its configured folder ID resolves to.
   A Drive ID follows its folder, so an ID that now resolves into an _archive
   path keeps working and keeps being wrong. That is a hard failure: /health
   returns 503 so the problem is loud, not quiet.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

ARCHIVE = re.compile(r"(^|/)_archive(/|$)")

_state = {"path": None, "error": None}


class WriteNotAllowed(RuntimeError):
    pass


def check_write_parents(parents, allowed_folder_id: str) -> None:
    if list(parents or []) != [allowed_folder_id]:
        msg = f"[ALLOWLIST] refused write with parents {parents}; reading-list-agent may write only to {allowed_folder_id}"
        logger.error(msg)
        raise WriteNotAllowed(msg)


def resolve_path(service, file_id: str, max_depth: int = 20) -> str:
    parts = []
    current = file_id
    for _ in range(max_depth):
        if not current:
            break
        meta = service.files().get(fileId=current, fields="name, parents").execute()
        parts.insert(0, meta.get("name", "?"))
        parents = meta.get("parents") or []
        current = parents[0] if parents else None
    return "/".join(parts)


def verify_library_folder(service, folder_id: str) -> str:
    """Resolve and record the library folder's path. Raises if it is archived."""
    path = resolve_path(service, folder_id)
    _state["path"] = path
    if ARCHIVE.search(path):
        _state["error"] = f"READING_LIBRARY_FOLDER_ID resolves to an archive: {path}"
        logger.error(_state["error"])
        raise RuntimeError(_state["error"])
    _state["error"] = None
    logger.info("Reading Library folder resolves to %s", path)
    return path


def status() -> dict:
    return dict(_state)
