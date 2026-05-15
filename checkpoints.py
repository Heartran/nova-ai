"""
checkpoints.py — Persistent storage of "last message seen" per channel.

Used for catch-up at boot: when Nova restarts, for each channel already seen,
it fetches messages after the stored timestamp and (if there's a message that
calls her) responds to the last qualifying one.

Storage: a single JSON in NOVA_MEMORY_DIR/checkpoints.json. Atomic write via
tmp + replace, so a crash mid-write doesn't corrupt the file.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


class ChannelCheckpoints:
    """Maps channel_id -> {scope, scope_id, last_seen ISO 8601 UTC}."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = {}
        self._lock = Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            logger.info("Loaded %d checkpoints from %s", len(self.data), self.path)
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Error loading checkpoints %s: %s", self.path, e)
            self.data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.path)  # atomic on Windows and POSIX
        except OSError as e:
            logger.error("Error saving checkpoints: %s", e)

    def is_tracked(self, channel_id: int) -> bool:
        return str(channel_id) in self.data

    def get(self, channel_id: int) -> datetime | None:
        entry = self.data.get(str(channel_id))
        if not entry:
            return None
        try:
            return datetime.fromisoformat(entry["last_seen"])
        except (KeyError, ValueError):
            return None

    def get_entry(self, channel_id: int) -> dict | None:
        return self.data.get(str(channel_id))

    def update(
        self,
        channel_id: int,
        message_time: datetime,
        scope_type: str,
        scope_id: int,
    ) -> None:
        with self._lock:
            self.data[str(channel_id)] = {
                "scope": scope_type,
                "scope_id": int(scope_id),
                "last_seen": message_time.astimezone(timezone.utc).isoformat(),
            }
            self._save()

    def channel_ids(self) -> list[int]:
        return [int(k) for k in self.data.keys()]
