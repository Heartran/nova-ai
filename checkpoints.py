"""
checkpoints.py — Storage persistente di "ultimo messaggio visto" per canale.

Serve al catch-up al boot: quando Nova riparte, per ogni canale che ha gia'
visto, scarica i messaggi successivi al timestamp salvato e (se c'e' un
messaggio che la chiama) risponde all'ultimo qualificante.

Storage: un singolo JSON in NOVA_MEMORY_DIR/checkpoints.json. Scrittura
atomica via tmp + replace, cosi' un crash a meta' non corrompe il file.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


class ChannelCheckpoints:
    """Mappa channel_id -> {scope, scope_id, last_seen ISO 8601 UTC}."""

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
            logger.info("Caricati %d checkpoint da %s", len(self.data), self.path)
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Errore caricamento checkpoints %s: %s", self.path, e)
            self.data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.path)  # atomico su Windows e POSIX
        except OSError as e:
            logger.error("Errore salvataggio checkpoints: %s", e)

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
