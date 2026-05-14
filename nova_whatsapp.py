"""
nova_whatsapp.py — Polling loop per ascoltare chat WhatsApp specifiche.

Ogni WHATSAPP_POLL_INTERVAL secondi interroga il DB SQLite del bridge Go
(whatsapp-bridge/store/messages.db) cercando nuovi messaggi nelle chat configurate.
Per ogni batch di nuovi messaggi Nova chiama Claude e invia la risposta tramite
l'API HTTP del bridge (POST http://localhost:8080/api/send).

Configurazione .env necessaria:
  WHATSAPP_WATCHED_JIDS    — JID delle chat da monitorare, separati da virgola
                             Es. 393XXXXXXXX@s.whatsapp.net,XXXXX@g.us
  WHATSAPP_BRIDGE_DB       — path assoluto al messages.db del bridge Go
  WHATSAPP_API_URL         — base URL API bridge (default: http://localhost:8080/api)
  WHATSAPP_POLL_INTERVAL   — secondi tra un poll e l'altro (default: 5)
  WHATSAPP_HISTORY_LIMIT   — messaggi di contesto storico da includere (default: 20)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import requests
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)

from memory import (
    ensure_scope_skeleton,
    load_scope_memory,
    load_user_memory,
    scope_dir_for,
)
from nova_mcp import build_memory_server
from personality import build_system_prompt

logger = logging.getLogger("nova.whatsapp")

_WA_ALLOWED_TOOLS = [
    "mcp__nova_memory__note_remember",
    "mcp__nova_memory__memory_append",
    "WebFetch",
    "WebSearch",
]


# ---------------------------------------------------------------------------
# Checkpoints per chat WhatsApp (chiavi stringa = JID)
# ---------------------------------------------------------------------------

class WhatsappCheckpoints:
    """Salva l'ultimo timestamp visto per ogni chat JID monitorata."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, str] = {}
        self._lock = Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            logger.info("WA checkpoints caricati: %d chat", len(self._data))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Errore caricamento WA checkpoints: %s", e)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            logger.error("Errore salvataggio WA checkpoints: %s", e)

    def get(self, jid: str) -> datetime | None:
        ts = self._data.get(jid)
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return None

    def update(self, jid: str, ts: datetime) -> None:
        with self._lock:
            self._data[jid] = ts.astimezone(timezone.utc).isoformat()
            self._save()


# ---------------------------------------------------------------------------
# Accesso SQLite (eseguito in thread per non bloccare l'event loop)
# ---------------------------------------------------------------------------

def _fetch_new_messages(db_path: str, chat_jid: str, after: datetime | None, limit: int) -> list[dict]:
    """Messaggi nuovi in una chat, dal più vecchio al più recente, esclusi quelli inviati da noi."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        params: list = [chat_jid]
        after_clause = ""
        if after is not None:
            after_clause = "AND m.timestamp > ?"
            params.append(after.isoformat())
        cursor.execute(
            f"""
            SELECT m.id, m.timestamp, m.sender, m.content, m.is_from_me, c.name
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.chat_jid = ? {after_clause}
              AND m.is_from_me = 0
              AND m.content IS NOT NULL AND m.content != ''
            ORDER BY m.timestamp ASC
            LIMIT ?
            """,
            params + [limit],
        )
        rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "timestamp": r[1],
                "sender": r[2],
                "content": r[3],
                "is_from_me": bool(r[4]),
                "chat_name": r[5] or chat_jid,
            }
            for r in rows
        ]
    except sqlite3.Error as e:
        logger.error("DB error leggendo nuovi messaggi (%s): %s", chat_jid, e)
        return []
    finally:
        if "conn" in locals():
            conn.close()  # type: ignore[possibly-undefined]


def _fetch_history(db_path: str, chat_jid: str, before_iso: str, limit: int) -> list[dict]:
    """Messaggi precedenti per costruire il contesto storico (entrambe le direzioni)."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT m.id, m.timestamp, m.sender, m.content, m.is_from_me, c.name
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.chat_jid = ? AND m.timestamp < ?
              AND m.content IS NOT NULL AND m.content != ''
            ORDER BY m.timestamp DESC
            LIMIT ?
            """,
            [chat_jid, before_iso, limit],
        )
        rows = cursor.fetchall()
        rows.reverse()  # riordina cronologicamente
        return [
            {
                "id": r[0],
                "timestamp": r[1],
                "sender": r[2],
                "content": r[3],
                "is_from_me": bool(r[4]),
                "chat_name": r[5] or chat_jid,
            }
            for r in rows
        ]
    except sqlite3.Error as e:
        logger.error("DB error leggendo history (%s): %s", chat_jid, e)
        return []
    finally:
        if "conn" in locals():
            conn.close()  # type: ignore[possibly-undefined]


# ---------------------------------------------------------------------------
# Invio messaggio via bridge Go
# ---------------------------------------------------------------------------

def _send_via_bridge(api_url: str, recipient_jid: str, text: str) -> bool:
    try:
        resp = requests.post(
            f"{api_url}/send",
            json={"recipient": recipient_jid, "message": text},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("success", False)
        logger.error("Bridge HTTP %s per %s: %s", resp.status_code, recipient_jid, resp.text[:200])
        return False
    except requests.RequestException as e:
        logger.error("Errore HTTP invio WA (%s): %s", recipient_jid, e)
        return False


# ---------------------------------------------------------------------------
# Costruzione messaggi per Claude
# ---------------------------------------------------------------------------

def _build_messages_for_claude(history: list[dict], new_msgs: list[dict]) -> list[dict]:
    """
    Converte i messaggi DB nel formato user/assistant della Messages API.
    is_from_me=True -> assistant, altrimenti -> user con prefisso mittente.
    """
    msgs: list[dict] = []

    def push(role: str, content: str) -> None:
        if not content.strip():
            return
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + content
        else:
            msgs.append({"role": role, "content": content})

    for m in [*history, *new_msgs]:
        if m["is_from_me"]:
            push("assistant", m["content"])
        else:
            sender_label = m.get("sender") or "?"
            push("user", f"[{sender_label}]: {m['content']}")

    if not msgs or msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": "[contesto precedente]"})

    return msgs


def _serialize_history_wa(messages: list[dict]) -> str:
    """Stesso pattern di nova_bot._serialize_history ma per il contesto WA."""
    if not messages:
        return ""
    history, last = messages[:-1], messages[-1]["content"]
    if not history:
        return last
    lines: list[str] = []
    for m in history:
        lines.append(f"Nova: {m['content']}" if m["role"] == "assistant" else m["content"])
    return (
        "## Storico chat recente\n"
        + "\n\n".join(lines)
        + "\n\n## Nuovo messaggio (rispondi in stile Nova)\n"
        + last
    )


# ---------------------------------------------------------------------------
# Chiamata Claude (versione WhatsApp — senza Discord read server)
# ---------------------------------------------------------------------------

async def _call_claude_wa(
    system: str,
    messages: list[dict],
    memory_server,
    model: str,
) -> str:
    prompt = _serialize_history_wa(messages)
    options = ClaudeAgentOptions(
        system_prompt=system,
        model=model,
        mcp_servers={"nova_memory": memory_server},
        allowed_tools=_WA_ALLOWED_TOOLS,
        max_turns=6,
        setting_sources=[],
    )
    parts: list[str] = []
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Logica di poll per una singola chat
# ---------------------------------------------------------------------------

async def _process_chat(
    *,
    jid: str,
    checkpoints: WhatsappCheckpoints,
    nova_memory_dir: Path,
    user_memory_dir: Path,
    db_path: str,
    api_url: str,
    history_limit: int,
    model: str,
) -> None:
    last_seen = checkpoints.get(jid)

    new_msgs: list[dict] = await asyncio.to_thread(
        _fetch_new_messages, db_path, jid, last_seen, 50
    )

    if not new_msgs:
        return

    # Aggiorna checkpoint immediatamente (anche se la chiamata Claude fallisce)
    latest_ts_str = new_msgs[-1]["timestamp"]
    try:
        latest_ts = datetime.fromisoformat(latest_ts_str)
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.replace(tzinfo=timezone.utc)
    except ValueError:
        latest_ts = datetime.now(timezone.utc)
    checkpoints.update(jid, latest_ts)

    # Scope memoria: whatsapp/<jid>/
    scope_dir = scope_dir_for(nova_memory_dir, "whatsapp", jid)
    ensure_scope_skeleton(scope_dir, "whatsapp")

    scope_mem = load_scope_memory(scope_dir)
    user_mem = load_user_memory(user_memory_dir) if user_memory_dir.exists() else ""

    # Recupera contesto storico precedente
    history: list[dict] = await asyncio.to_thread(
        _fetch_history, db_path, jid, new_msgs[0]["timestamp"], history_limit
    )

    messages = _build_messages_for_claude(history, new_msgs)
    chat_name = new_msgs[0].get("chat_name") or jid

    system_prompt = build_system_prompt(scope_mem, user_mem, bot_display_name="Nova")
    system_prompt += (
        f"\n\n## Contesto attuale\n"
        f"Stai rispondendo in una chat WhatsApp: **{chat_name}**.\n"
        "Adatta il tono: messaggi più brevi e diretti rispetto a Discord, "
        "poco markdown (niente intestazioni o liste pesanti), emoji con moderazione."
    )

    memory_server = build_memory_server(scope_dir)

    logger.info(
        "WA poll: %d nuovi messaggi in '%s', genero risposta...",
        len(new_msgs),
        chat_name,
    )

    try:
        reply = await _call_claude_wa(system_prompt, messages, memory_server, model)
    except Exception:
        logger.exception("Errore chiamata Claude per chat WA %s", jid)
        return

    if not reply:
        logger.warning("Claude ha restituito risposta vuota per %s", jid)
        return

    ok = await asyncio.to_thread(_send_via_bridge, api_url, jid, reply)
    if ok:
        logger.info("Risposta WA inviata a %s (%d caratteri)", chat_name, len(reply))
    else:
        logger.error("Invio WA fallito per %s", jid)


# ---------------------------------------------------------------------------
# Entry point: task da avviare con asyncio.create_task()
# ---------------------------------------------------------------------------

async def start_whatsapp_poller(
    *,
    nova_memory_dir: Path,
    user_memory_dir: Path,
    db_path: str,
    api_url: str,
    watched_jids: list[str],
    poll_interval: float,
    history_limit: int,
    model: str,
) -> None:
    """
    Loop di polling asincrono. Chiama con asyncio.create_task() dentro on_ready.

    Non ritorna mai (gira fino a cancellazione del task).
    """
    if not watched_jids:
        logger.warning("Nessun JID WhatsApp configurato, poller disabilitato")
        return

    # Verifica che il DB esista già (il bridge potrebbe non essere avviato)
    if not Path(db_path).exists():
        logger.warning(
            "WHATSAPP_BRIDGE_DB non trovato: %s — il poller aspetterà che appaia",
            db_path,
        )

    checkpoints = WhatsappCheckpoints(nova_memory_dir / "whatsapp_checkpoints.json")
    logger.info(
        "WhatsApp poller avviato | chat: %s | intervallo: %ss",
        ", ".join(watched_jids),
        poll_interval,
    )

    while True:
        if not Path(db_path).exists():
            logger.debug("DB bridge non ancora disponibile, attendo...")
            await asyncio.sleep(poll_interval)
            continue

        for jid in watched_jids:
            try:
                await _process_chat(
                    jid=jid,
                    checkpoints=checkpoints,
                    nova_memory_dir=nova_memory_dir,
                    user_memory_dir=user_memory_dir,
                    db_path=db_path,
                    api_url=api_url,
                    history_limit=history_limit,
                    model=model,
                )
            except Exception:
                logger.exception("Errore nel poll di %s", jid)

        await asyncio.sleep(poll_interval)
