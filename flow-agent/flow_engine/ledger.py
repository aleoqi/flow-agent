"""Flow Engine — Four-Dimensional Media Ledger.

Provides SHA-256 + client_id + project_id -> media_id deduplication.
Eliminates redundant uploads and guarantees isolation across different accounts and projects.
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("flow_engine.ledger")

DEFAULT_LEDGER_PATH = os.path.expanduser("~/.flow_media_ledger.json")


class MediaLedger:
    """Four-dimensional asset ledger to track uploaded media IDs safely."""

    def __init__(self, ledger_file: Optional[str] = None):
        self.ledger_file = ledger_file or DEFAULT_LEDGER_PATH
        self.entries: list[dict] = []
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.ledger_file):
            try:
                with open(self.ledger_file, "r", encoding="utf-8") as f:
                    self.entries = json.load(f)
            except Exception as e:
                log.warning("Failed to load media ledger from %s: %s", self.ledger_file, e)
                self.entries = []
        else:
            self.entries = []

    def _save(self) -> None:
        try:
            temp_path = f"{self.ledger_file}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, self.ledger_file)
        except Exception as e:
            log.error("Failed to save media ledger to %s: %s", self.ledger_file, e)

    @staticmethod
    def compute_sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @classmethod
    def compute_file_sha256(cls, file_path: str) -> str:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()

    def lookup(
        self,
        sha256_hash: str,
        client_id: Optional[str],
        project_id: Optional[str],
    ) -> Optional[str]:
        """Lookup existing Media ID with strict four-dimensional matching."""
        cid = client_id or ""
        pid = project_id or ""
        for e in self.entries:
            if (
                e.get("sha256") == sha256_hash
                and (e.get("client_id") or "") == cid
                and (e.get("project_id") or "") == pid
                and e.get("status") == "valid"
            ):
                return e.get("media_id")
        return None

    def record(
        self,
        sha256_hash: str,
        client_id: Optional[str],
        project_id: Optional[str],
        media_id: str,
        file_name: str = "",
    ) -> None:
        """Record newly uploaded Media ID into ledger."""
        cid = client_id or ""
        pid = project_id or ""
        # Check if already present to avoid duplication
        for e in self.entries:
            if (
                e.get("sha256") == sha256_hash
                and (e.get("client_id") or "") == cid
                and (e.get("project_id") or "") == pid
            ):
                e["media_id"] = media_id
                e["updated_at"] = time.time()
                e["status"] = "valid"
                self._save()
                return

        self.entries.append({
            "sha256": sha256_hash,
            "client_id": cid,
            "project_id": pid,
            "media_id": media_id,
            "file_name": file_name,
            "created_at": time.time(),
            "status": "valid",
        })
        self._save()


# Singleton instance
_global_ledger: Optional[MediaLedger] = None


def get_media_ledger() -> MediaLedger:
    global _global_ledger
    if _global_ledger is None:
        _global_ledger = MediaLedger()
    return _global_ledger
