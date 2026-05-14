"""
nova_whatsapp_server.py — Server WhatsApp interattivo per Nova.

Avvio:  python nova_whatsapp_server.py
        python nova_whatsapp_server.py --db <path_messages.db>  (override config)

Il server carica la configurazione da .env e da whatsapp_config.json nella
NOVA_MEMORY_DIR. I JID monitorati si gestiscono live dalla CLI senza riavvio.

Comandi CLI:
  chats [query]      Lista le chat nel DB (con numero per riferimento rapido)
  watch <jid|n>      Aggiungi una chat al monitoring (JID o numero da 'chats')
  unwatch <jid|n>    Rimuovi una chat dal monitoring
  list               Lista le chat monitorate
  status             Stato del server e del bridge
  interval <n>       Cambia l'intervallo di poll in secondi
  help               Mostra questo help
  quit / exit        Arresta il server
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap: carica .env e aggiungi la cartella nova-ai al path
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
load_dotenv(_HERE / ".env")

from memory import (
    ensure_scope_skeleton,
    load_scope_memory,
    load_user_memory,
    scope_dir_for,
)
from nova_mcp import build_memory_server
from nova_whatsapp import (
    WhatsappCheckpoints,
    _build_messages_for_claude,
    _call_claude_wa,
    _fetch_history,
    _fetch_new_messages,
    _send_via_bridge,
)
from personality import build_system_prompt

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NOVA_MEMORY_DIR = Path(os.getenv("NOVA_MEMORY_DIR", "")).expanduser()
USER_MEMORY_DIR = Path(os.getenv("USER_MEMORY_DIR", "")).expanduser()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6").strip()

DEFAULT_BRIDGE_DB = str(
    Path(os.getenv(
        "WHATSAPP_BRIDGE_DB",
        r"C:\Users\Federico\repo\whatsapp-mcp\whatsapp-bridge\store\messages.db",
    )).expanduser()
)
DEFAULT_API_URL = os.getenv("WHATSAPP_API_URL", "http://localhost:8080/api").strip()
DEFAULT_POLL_INTERVAL = float(os.getenv("WHATSAPP_POLL_INTERVAL", "5"))
DEFAULT_HISTORY_LIMIT = int(os.getenv("WHATSAPP_HISTORY_LIMIT", "20"))

CONFIG_FILE = NOVA_MEMORY_DIR / "whatsapp_config.json"

# ---------------------------------------------------------------------------
# Colori ANSI minimali (zero dipendenze extra)
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"


def _c(color: str, text: str) -> str:
    return f"{color}{text}{RESET}"


# ---------------------------------------------------------------------------
# WatchConfig — config persistente dei JID monitorati
# ---------------------------------------------------------------------------

class WatchConfig:
    """Config persistente: JID monitorati + impostazioni del server."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._jids: list[str] = []
        self.poll_interval: float = DEFAULT_POLL_INTERVAL
        self.history_limit: int = DEFAULT_HISTORY_LIMIT
        self._lock = Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._jids = data.get("watched_jids", [])
            self.poll_interval = float(data.get("poll_interval", DEFAULT_POLL_INTERVAL))
            self.history_limit = int(data.get("history_limit", DEFAULT_HISTORY_LIMIT))
        except (OSError, json.JSONDecodeError, ValueError) as e:
            print(_c(YELLOW, f"[warn] Errore caricamento config: {e}"))

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "watched_jids": self._jids,
                        "poll_interval": self.poll_interval,
                        "history_limit": self.history_limit,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as e:
            print(_c(RED, f"[errore] Salvataggio config fallito: {e}"))

    @property
    def watched_jids(self) -> list[str]:
        with self._lock:
            return list(self._jids)

    def add(self, jid: str) -> bool:
        with self._lock:
            if jid in self._jids:
                return False
            self._jids.append(jid)
            self._save()
            return True

    def remove(self, jid: str) -> bool:
        with self._lock:
            if jid not in self._jids:
                return False
            self._jids.remove(jid)
            self._save()
            return True

    def set_interval(self, seconds: float) -> None:
        with self._lock:
            self.poll_interval = seconds
            self._save()


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

async def _poll_loop(
    config: WatchConfig,
    checkpoints: WhatsappCheckpoints,
    db_path: str,
    api_url: str,
    stop_event: asyncio.Event,
) -> None:
    """Gira in background finché stop_event non è settato."""
    logging.getLogger("nova.whatsapp").setLevel(logging.WARNING)  # silenzioso in CLI

    while not stop_event.is_set():
        jids = config.watched_jids
        if jids and Path(db_path).exists():
            for jid in jids:
                try:
                    await _poll_one(jid, checkpoints, config, db_path, api_url)
                except Exception as exc:
                    _print_log(_c(RED, f"[poll] Errore su {jid}: {exc}"))

        interval = config.poll_interval
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _poll_one(
    jid: str,
    checkpoints: WhatsappCheckpoints,
    config: WatchConfig,
    db_path: str,
    api_url: str,
) -> None:
    last_seen = checkpoints.get(jid)

    # Prima volta che vediamo questo JID: segna "adesso" come punto di partenza
    # e non rispondere ai messaggi già presenti.
    if last_seen is None:
        checkpoints.update(jid, datetime.now(timezone.utc))
        return

    new_msgs = await asyncio.to_thread(_fetch_new_messages, db_path, jid, last_seen, 50)
    if not new_msgs:
        return

    # Aggiorna checkpoint subito
    try:
        latest_ts = datetime.fromisoformat(new_msgs[-1]["timestamp"])
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.replace(tzinfo=timezone.utc)
    except ValueError:
        latest_ts = datetime.now(timezone.utc)
    checkpoints.update(jid, latest_ts)

    scope_dir = scope_dir_for(NOVA_MEMORY_DIR, "whatsapp", jid)
    ensure_scope_skeleton(scope_dir, "whatsapp")
    scope_mem = load_scope_memory(scope_dir)
    user_mem = load_user_memory(USER_MEMORY_DIR) if USER_MEMORY_DIR.exists() else ""

    history = await asyncio.to_thread(
        _fetch_history, db_path, jid, new_msgs[0]["timestamp"], config.history_limit
    )
    messages = _build_messages_for_claude(history, new_msgs)
    chat_name = new_msgs[0].get("chat_name") or jid

    system_prompt = build_system_prompt(scope_mem, user_mem, bot_display_name="Nova")
    system_prompt += (
        f"\n\n## Contesto attuale\n"
        f"Stai rispondendo in una chat WhatsApp: **{chat_name}**.\n"
        "Messaggi brevi e diretti, poco markdown, emoji con moderazione."
    )

    memory_server = build_memory_server(scope_dir)

    _print_log(
        _c(CYAN, f"[{chat_name}]")
        + f" {len(new_msgs)} nuovi messaggi, rispondo..."
    )

    reply = await _call_claude_wa(system_prompt, messages, memory_server, CLAUDE_MODEL)

    if not reply:
        _print_log(_c(YELLOW, f"[{chat_name}] Risposta vuota da Claude"))
        return

    ok = await asyncio.to_thread(_send_via_bridge, api_url, jid, reply)
    status = _c(GREEN, "✓") if ok else _c(RED, "✗")
    _print_log(f"{status} [{chat_name}] Risposta inviata ({len(reply)} car.)")


# ---------------------------------------------------------------------------
# CLI interattiva
# ---------------------------------------------------------------------------

_last_chats: list[dict] = []  # risultato dell'ultimo 'chats' per riferimento numerico


def _print_log(msg: str) -> None:
    """Stampa un log senza sporcare il prompt (sovrascrive la riga prompt)."""
    print(f"\r{msg}")
    print(_c(DIM, "wa> "), end="", flush=True)


def _list_chats_from_db(db_path: str, query_str: str = "", limit: int = 30) -> list[dict]:
    """Legge le chat disponibili dal DB del bridge."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        search = f"%{query_str}%" if query_str else "%"
        cursor.execute(
            """
            SELECT c.jid, c.name, c.last_message_time,
                   m.content as last_msg
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid
                AND c.last_message_time = m.timestamp
            WHERE (c.name LIKE ? OR c.jid LIKE ?)
            ORDER BY c.last_message_time DESC NULLS LAST
            LIMIT ?
            """,
            [search, search, limit],
        )
        rows = cursor.fetchall()
        return [
            {
                "jid": r[0],
                "name": r[1] or r[0],
                "last_time": r[2] or "",
                "last_msg": (r[3] or "")[:60],
            }
            for r in rows
        ]
    except sqlite3.Error as e:
        print(_c(RED, f"Errore DB: {e}"))
        return []
    finally:
        if "conn" in locals():
            conn.close()  # type: ignore[possibly-undefined]


def _resolve_jid(arg: str, config: WatchConfig, db_path: str) -> str | None:
    """
    Risolve l'argomento del comando watch/unwatch.
    Può essere un numero (da lista 'chats'), una sottostringa di nome, o un JID completo.
    """
    # Numero dalla lista precedente
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(_last_chats):
            return _last_chats[idx]["jid"]
        print(_c(RED, f"Numero {arg} non valido. Usa 'chats' per vedere la lista."))
        return None

    # JID diretto (contiene @)
    if "@" in arg:
        return arg

    # Sottostringa di nome: cerca nelle ultime chats caricate o nel DB
    candidates = [c for c in _last_chats if arg.lower() in c["name"].lower()]
    if len(candidates) == 1:
        return candidates[0]["jid"]
    if len(candidates) > 1:
        print(_c(YELLOW, f"Ambiguo — corrisponde a {len(candidates)} chat:"))
        for i, c in enumerate(candidates, 1):
            print(f"  {i}. {_c(BOLD, c['name'])} — {_c(DIM, c['jid'])}")
        return None

    # Cerca nel DB
    if Path(db_path).exists():
        results = _list_chats_from_db(db_path, arg, limit=5)
        if len(results) == 1:
            return results[0]["jid"]
        if results:
            print(_c(YELLOW, f"Nessuna corrispondenza esatta, trovate {len(results)} chat. Usa 'chats {arg}' per vedere."))
    print(_c(RED, f"'{arg}' non risolto. Usa il JID completo o un numero da 'chats'."))
    return None


def _cmd_chats(args: list[str], db_path: str) -> None:
    global _last_chats
    if not Path(db_path).exists():
        print(_c(RED, f"DB non trovato: {db_path}\nAvvia il bridge Go prima."))
        return
    query_str = " ".join(args)
    chats = _list_chats_from_db(db_path, query_str)
    _last_chats = chats
    if not chats:
        print(_c(YELLOW, "Nessuna chat trovata" + (f" per '{query_str}'" if query_str else "") + "."))
        return
    print()
    for i, c in enumerate(chats, 1):
        name = _c(BOLD, c["name"])
        jid = _c(DIM, c["jid"])
        ts = _c(DIM, c["last_time"][:16]) if c["last_time"] else ""
        last = _c(DIM, f'  "{c["last_msg"]}"') if c["last_msg"] else ""
        print(f"  {_c(CYAN, str(i).rjust(2))}. {name}  {jid}  {ts}{last}")
    print()


def _cmd_watch(args: list[str], config: WatchConfig, db_path: str) -> None:
    if not args:
        print(_c(YELLOW, "Uso: watch <jid | numero | nome>"))
        return
    jid = _resolve_jid(" ".join(args), config, db_path)
    if jid is None:
        return
    # Trova il nome dalla lista per un messaggio più leggibile
    name = next((c["name"] for c in _last_chats if c["jid"] == jid), jid)
    if config.add(jid):
        print(_c(GREEN, f"✓ Aggiunta: {_c(BOLD, name)} ({jid})"))
    else:
        print(_c(YELLOW, f"'{name}' è già monitorata."))


def _cmd_unwatch(args: list[str], config: WatchConfig, db_path: str) -> None:
    if not args:
        print(_c(YELLOW, "Uso: unwatch <jid | numero | nome>"))
        return
    jid = _resolve_jid(" ".join(args), config, db_path)
    if jid is None:
        return
    name = next((c["name"] for c in _last_chats if c["jid"] == jid), jid)
    if config.remove(jid):
        print(_c(GREEN, f"✓ Rimossa: {_c(BOLD, name)} ({jid})"))
    else:
        print(_c(YELLOW, f"'{name}' non era monitorata."))


def _cmd_list(config: WatchConfig) -> None:
    jids = config.watched_jids
    if not jids:
        print(_c(YELLOW, "Nessuna chat monitorata. Usa 'watch <jid>' per aggiungerne una."))
        return
    print()
    for i, jid in enumerate(jids, 1):
        name = next((c["name"] for c in _last_chats if c["jid"] == jid), "")
        label = f"{_c(BOLD, name)}  " if name else ""
        print(f"  {_c(GREEN, str(i).rjust(2))}. {label}{_c(DIM, jid)}")
    print()


def _cmd_status(config: WatchConfig, db_path: str, api_url: str) -> None:
    # Bridge DB
    db_ok = Path(db_path).exists()
    db_status = _c(GREEN, "✓ trovato") if db_ok else _c(RED, "✗ non trovato")

    # Bridge HTTP
    try:
        resp = requests.get(f"{api_url.rstrip('/').replace('/api', '')}/health", timeout=2)
        bridge_ok = resp.status_code == 200
    except requests.RequestException:
        bridge_ok = False
    bridge_status = _c(GREEN, "✓ risponde") if bridge_ok else _c(YELLOW, "? non raggiungibile")

    watched = config.watched_jids
    print()
    print(f"  DB bridge:   {db_status}  ({db_path})")
    print(f"  Bridge HTTP: {bridge_status}  ({api_url})")
    print(f"  Modello:     {_c(BOLD, CLAUDE_MODEL)}")
    print(f"  Memoria:     {NOVA_MEMORY_DIR}")
    print(f"  Poll:        ogni {_c(BOLD, str(config.poll_interval))}s")
    print(f"  Chat:        {_c(BOLD, str(len(watched)))} monitorate")
    print()


def _cmd_interval(args: list[str], config: WatchConfig) -> None:
    if not args or not args[0].replace(".", "", 1).isdigit():
        print(_c(YELLOW, "Uso: interval <secondi>  (es. interval 10)"))
        return
    val = float(args[0])
    if val < 1:
        print(_c(RED, "Intervallo minimo: 1 secondo"))
        return
    config.set_interval(val)
    print(_c(GREEN, f"✓ Intervallo impostato a {val}s"))


def _cmd_help() -> None:
    print(f"""
{_c(BOLD, "Comandi disponibili:")}

  {_c(CYAN, "chats [query]")}      Lista le chat nel DB del bridge (filtra per nome/JID)
  {_c(CYAN, "watch <jid|n>")}      Inizia a monitorare una chat (JID, numero da 'chats', o nome)
  {_c(CYAN, "unwatch <jid|n>")}    Smetti di monitorare una chat
  {_c(CYAN, "list")}               Mostra le chat attualmente monitorate
  {_c(CYAN, "status")}             Stato del server, bridge e config
  {_c(CYAN, "interval <n>")}       Cambia l'intervallo di polling (secondi)
  {_c(CYAN, "help")}               Mostra questo help
  {_c(CYAN, "quit")} / {_c(CYAN, "exit")}        Arresta il server

{_c(DIM, "Tip: usa i numeri di 'chats' per watch/unwatch senza copiare i JID.")}
""")


async def _command_loop(
    config: WatchConfig,
    stop_event: asyncio.Event,
    db_path: str,
    api_url: str,
) -> None:
    loop = asyncio.get_event_loop()
    while not stop_event.is_set():
        try:
            raw = await loop.run_in_executor(None, lambda: input(_c(DIM, "wa> ")))
        except (EOFError, KeyboardInterrupt):
            print()
            break

        parts = raw.strip().split()
        if not parts:
            continue
        cmd, *args = parts

        if cmd in ("quit", "exit", "q"):
            break
        elif cmd == "chats":
            _cmd_chats(args, db_path)
        elif cmd == "watch":
            _cmd_watch(args, config, db_path)
        elif cmd == "unwatch":
            _cmd_unwatch(args, config, db_path)
        elif cmd == "list":
            _cmd_list(config)
        elif cmd == "status":
            _cmd_status(config, db_path, api_url)
        elif cmd == "interval":
            _cmd_interval(args, config)
        elif cmd in ("help", "?", "h"):
            _cmd_help()
        else:
            print(_c(YELLOW, f"Comando non riconosciuto: '{cmd}'. Scrivi 'help' per la lista."))

    stop_event.set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _banner(db_path: str) -> None:
    print(f"""
{_c(BOLD + MAGENTA, "╔══════════════════════════════════╗")}
{_c(BOLD + MAGENTA, "║")}  {_c(BOLD, "Nova WhatsApp Server")}              {_c(BOLD + MAGENTA, "║")}
{_c(BOLD + MAGENTA, "╚══════════════════════════════════╝")}

  DB:      {_c(DIM, db_path)}
  Memoria: {_c(DIM, str(NOVA_MEMORY_DIR))}
  Modello: {_c(BOLD, CLAUDE_MODEL)}

Scrivi {_c(CYAN, "help")} per i comandi, {_c(CYAN, "status")} per lo stato del bridge.
""")


async def _main(db_path: str, api_url: str, interval_override: float | None = None) -> None:
    if not NOVA_MEMORY_DIR or str(NOVA_MEMORY_DIR) == ".":
        sys.exit("Manca NOVA_MEMORY_DIR in .env")
    NOVA_MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    config = WatchConfig(CONFIG_FILE)
    if interval_override is not None:
        config.set_interval(interval_override)
    checkpoints = WhatsappCheckpoints(NOVA_MEMORY_DIR / "whatsapp_checkpoints.json")
    stop_event = asyncio.Event()

    # Gestione Ctrl+C graceful
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows non supporta add_signal_handler per tutti i segnali

    _banner(db_path)

    await asyncio.gather(
        _poll_loop(config, checkpoints, db_path, api_url, stop_event),
        _command_loop(config, stop_event, db_path, api_url),
    )

    print(_c(DIM, "\nServer fermato."))


def main() -> None:
    parser = argparse.ArgumentParser(description="Nova WhatsApp Server")
    parser.add_argument(
        "--db",
        default=DEFAULT_BRIDGE_DB,
        help="Path al messages.db del bridge Go",
    )
    parser.add_argument(
        "--api",
        default=DEFAULT_API_URL,
        help="URL base API bridge (default: http://localhost:8080/api)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Override intervallo di poll in secondi",
    )
    args = parser.parse_args()

    # Sopprime logging di librerie rumorose
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger("claude_agent_sdk").setLevel(logging.ERROR)
    logging.getLogger("nova").setLevel(logging.ERROR)

    try:
        asyncio.run(_main(args.db, args.api, args.interval))
    except KeyboardInterrupt:
        print(_c(DIM, "\nInterrotto."))


if __name__ == "__main__":
    main()
