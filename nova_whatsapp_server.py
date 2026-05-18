"""
nova_whatsapp_server.py — Interactive WhatsApp server for Nova.

Start:  python nova_whatsapp_server.py
        python nova_whatsapp_server.py --db <path_messages.db>  (override config)

The server loads config from .env and from whatsapp_config.json under
NOVA_MEMORY_DIR. Watched JIDs are managed live from the CLI with no restart.

CLI commands:
  chats [query]      List chats in the DB (with a number for quick reference)
  watch <jid|n>      Add a chat to monitoring (JID or number from 'chats')
  unwatch <jid|n>    Remove a chat from monitoring
  list               List monitored chats
  status             Show server and bridge status
  interval <n>       Change the poll interval in seconds
  help               Show this help
  quit / exit        Stop the server
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap: load .env and add the nova-ai folder to the path
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
load_dotenv(_HERE / ".env")

from memory import (
    bootstrap_memory_dir,
    ensure_scope_skeleton,
    ensure_shared_skeleton,
    load_scope_memory,
    load_shared_memory,
    load_user_memory,
    scope_dir_for,
)
from nova_mcp import build_memory_server
from nova_whatsapp import (
    WhatsappCheckpoints,
    build_messages_for_claude,
    call_claude_wa,
    download_audio,
    fetch_history,
    fetch_new_messages,
    fetch_tail,
    resolve_jid_variants,
    send_via_bridge,
    serialize_history_wa,
    transcribe_audio_file,
)
from personality import build_system_prompt

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NOVA_MEMORY_DIR = Path(os.getenv("NOVA_MEMORY_DIR", "")).expanduser()
USER_MEMORY_DIR = Path(os.getenv("USER_MEMORY_DIR", "")).expanduser()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6").strip()

# Default DB path: read from env, otherwise empty. Set WHATSAPP_BRIDGE_DB in
# your .env to point at the Go bridge's messages.db, or pass --db on the CLI.
DEFAULT_BRIDGE_DB = str(
    Path(os.getenv("WHATSAPP_BRIDGE_DB", "")).expanduser()
) if os.getenv("WHATSAPP_BRIDGE_DB") else ""
DEFAULT_API_URL = os.getenv("WHATSAPP_API_URL", "http://localhost:8080/api").strip()
DEFAULT_POLL_INTERVAL = float(os.getenv("WHATSAPP_POLL_INTERVAL", "5"))
DEFAULT_HISTORY_LIMIT = int(os.getenv("WHATSAPP_HISTORY_LIMIT", "20"))
_CONTACT_MAP_PATH = Path(os.getenv("WHATSAPP_CONTACT_MAP", "")).expanduser() if os.getenv("WHATSAPP_CONTACT_MAP") else None

CONFIG_FILE = NOVA_MEMORY_DIR / "whatsapp_config.json"

# ---------------------------------------------------------------------------
# Contact map (optional, loaded from WHATSAPP_CONTACT_MAP env path)
# ---------------------------------------------------------------------------

def _load_contact_map(path: Path | None) -> dict:
    if not path or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

_CONTACT_MAP: dict = _load_contact_map(_CONTACT_MAP_PATH)


def _contact_map_jid_variants(jid: str) -> list[str]:
    """Return phone↔LID variants for a JID from the contact map."""
    if not _CONTACT_MAP:
        return [jid]
    jids = [jid]
    for entry in _CONTACT_MAP.values():
        if not isinstance(entry, dict):
            continue
        related = {
            entry.get("chat_jid"),
            entry.get("lid"),
            entry.get("legacy_phone_jid"),
            *(f"{p}@s.whatsapp.net" for p in entry.get("phone_numbers", [])),
        } - {None}
        if jid in related:
            for v in related:
                if v and v not in jids:
                    jids.append(v)
    return jids


def _contact_map_resolve_name(name: str) -> str | None:
    """Look up a contact by name substring in the contact map; return chat_jid."""
    if not _CONTACT_MAP:
        return None
    nl = name.lower()
    matches = []
    for key, entry in _CONTACT_MAP.items():
        if not isinstance(entry, dict):
            continue
        entry_name = entry.get("name", key)
        nicknames = entry.get("nicknames", [])
        if nl in entry_name.lower() or any(nl in n.lower() for n in nicknames):
            jid = entry.get("chat_jid") or entry.get("lid")
            if jid and jid not in matches:
                matches.append(jid)
    if len(matches) == 1:
        return matches[0]
    return None


# ---------------------------------------------------------------------------
# Minimal ANSI colors (zero extra dependencies)
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
# WatchConfig — persistent config of watched JIDs
# ---------------------------------------------------------------------------

class WatchConfig:
    """Persistent config: watched JIDs + server settings + per-chat options.

    Files (both in the same directory as `path`):
      whatsapp_config.json  — watched JIDs, poll settings, per-chat flags
      whatsapp_mcp.json     — global extra MCP servers (separate for easy editing)
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.mcp_path = path.with_name("whatsapp_mcp.json")
        self._jids: list[str] = []
        self.poll_interval: float = DEFAULT_POLL_INTERVAL
        self.history_limit: int = DEFAULT_HISTORY_LIMIT
        # Global extra MCP servers: name → config dict (used for all chats)
        self._mcp_servers: dict[str, dict] = {}
        # Per-chat overrides: jid → {"thinking": bool}
        self._chat_configs: dict[str, dict] = {}
        self._lock = Lock()
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._jids = data.get("watched_jids", [])
                self.poll_interval = float(data.get("poll_interval", DEFAULT_POLL_INTERVAL))
                self.history_limit = int(data.get("history_limit", DEFAULT_HISTORY_LIMIT))
                self._chat_configs = data.get("chat_configs", {})
            except (OSError, json.JSONDecodeError, ValueError) as e:
                print(_c(YELLOW, f"[warn] Failed to load config: {e}"))
        if self.mcp_path.exists():
            try:
                self._mcp_servers = json.loads(self.mcp_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                print(_c(YELLOW, f"[warn] Failed to load MCP config: {e}"))

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
                        "chat_configs": self._chat_configs,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as e:
            print(_c(RED, f"[error] Failed to save config: {e}"))

    def _save_mcp(self) -> None:
        try:
            self.mcp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.mcp_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._mcp_servers, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.mcp_path)
        except OSError as e:
            print(_c(RED, f"[error] Failed to save MCP config: {e}"))

    @property
    def watched_jids(self) -> list[str]:
        with self._lock:
            return list(self._jids)

    @property
    def mcp_servers(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._mcp_servers)

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

    def add_mcp_server(self, name: str, cfg: dict) -> None:
        with self._lock:
            self._mcp_servers[name] = cfg
            self._save_mcp()

    def remove_mcp_server(self, name: str) -> bool:
        with self._lock:
            if name not in self._mcp_servers:
                return False
            del self._mcp_servers[name]
            self._save_mcp()
            return True

    # ------------------------------------------------------------------
    # Per-chat config helpers
    # ------------------------------------------------------------------

    def get_chat_config(self, jid: str) -> dict:
        with self._lock:
            return dict(self._chat_configs.get(jid, {}))

    def set_thinking(self, jid: str, enabled: bool) -> None:
        with self._lock:
            self._chat_configs.setdefault(jid, {})["thinking"] = enabled
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
    wakeup_event: asyncio.Event | None = None,
) -> None:
    """Runs in the background until stop_event is set."""
    logging.getLogger("nova.whatsapp").setLevel(logging.WARNING)  # silent in CLI

    poll_count = 0
    while not stop_event.is_set():
        # Clear before polling so any WAL change that arrives during the poll
        # is captured and shortens the subsequent sleep rather than being lost.
        if wakeup_event is not None:
            wakeup_event.clear()

        jids = config.watched_jids
        if jids and db_path and Path(db_path).is_file():
            poll_count += 1
            for jid in jids:
                try:
                    await _poll_one(jid, checkpoints, config, db_path, api_url)
                except Exception as exc:
                    _print_log(_c(RED, f"[poll] Error on {jid}: {exc}"))
        elif jids:
            # DB not available yet
            poll_count += 1

        # Visual tick every 12 polls (~60s at the default 5s interval)
        if poll_count % 12 == 0 and jids:
            ts = datetime.now().strftime("%H:%M:%S")
            _print_log(_c(DIM, f"[{ts}] polling... ({poll_count} cycles, {len(jids)} chats)"))

        interval = config.poll_interval
        if wakeup_event is not None:
            try:
                await asyncio.wait_for(wakeup_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


async def _wal_watcher(
    db_path: str,
    wakeup_event: asyncio.Event,
    stop_event: asyncio.Event,
) -> None:
    """Watch messages.db-wal for mtime changes; trigger immediate poll on change."""
    wal_path = Path(str(db_path) + "-wal")
    last_mtime: float = 0.0
    CHECK_INTERVAL = 0.1  # 100 ms

    while not stop_event.is_set():
        try:
            if wal_path.is_file():
                mtime = wal_path.stat().st_mtime
                if mtime != last_mtime:
                    if last_mtime > 0:  # skip initial detection
                        wakeup_event.set()
                    last_mtime = mtime
        except OSError:
            pass

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL)
        except asyncio.TimeoutError:
            pass

    wakeup_event.set()  # unblock poll loop so it can exit cleanly


async def _poll_one(
    jid: str,
    checkpoints: WhatsappCheckpoints,
    config: WatchConfig,
    db_path: str,
    api_url: str,
    force: bool = False,
) -> None:
    last_seen = checkpoints.get(jid)

    # First time we see this JID: set "now" as the starting point and do not
    # reply to messages already in the DB (unless forced).
    if last_seen is None:
        if not force:
            checkpoints.update(jid, datetime.now(timezone.utc))
            return
        last_seen = datetime.now(timezone.utc) - timedelta(hours=24)

    new_msgs = await asyncio.to_thread(fetch_new_messages, db_path, jid, last_seen, 50, _CONTACT_MAP)
    if not new_msgs:
        return

    # Transcribe any voice messages before building context
    for msg in new_msgs:
        if msg.get("media_type") == "audio" and not msg.get("content"):
            local_path = await asyncio.to_thread(download_audio, api_url, msg["id"], jid)
            if local_path:
                transcript = await asyncio.to_thread(transcribe_audio_file, local_path)
                msg["content"] = f"[vocale: {transcript}]" if transcript else "[vocale: trascrizione non riuscita]"
            else:
                msg["content"] = "[vocale: download non riuscito]"

    # Drop messages that are still empty after transcription attempts
    new_msgs = [m for m in new_msgs if m.get("content", "").strip()]
    if not new_msgs:
        return

    # Advance the checkpoint immediately
    try:
        latest_ts = datetime.fromisoformat(new_msgs[-1]["timestamp"])
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.replace(tzinfo=timezone.utc)
    except ValueError:
        latest_ts = datetime.now(timezone.utc)
    checkpoints.update(jid, latest_ts)

    # Prefer the JID variant that already has a memory folder (LID migration).
    jid_variants = await asyncio.to_thread(resolve_jid_variants, db_path, jid, _CONTACT_MAP)
    scope_jid = next(
        (v for v in jid_variants if scope_dir_for(NOVA_MEMORY_DIR, "whatsapp", v).exists()),
        jid,
    )
    scope_dir = scope_dir_for(NOVA_MEMORY_DIR, "whatsapp", scope_jid)
    ensure_scope_skeleton(scope_dir, "whatsapp")
    scope_mem = load_scope_memory(scope_dir)
    shared_mem = load_shared_memory(NOVA_MEMORY_DIR)
    user_mem = load_user_memory(USER_MEMORY_DIR) if USER_MEMORY_DIR.exists() else ""

    history = await asyncio.to_thread(
        fetch_history, db_path, jid, new_msgs[0]["timestamp"], config.history_limit, _CONTACT_MAP
    )
    messages = build_messages_for_claude(history, new_msgs)
    chat_name = new_msgs[0].get("chat_name") or jid

    system_prompt = build_system_prompt(
        shared_mem, scope_mem, user_mem, bot_display_name="Nova"
    )
    system_prompt += (
        f"\n\n## Contesto attuale\n"
        f"Stai rispondendo in una chat WhatsApp: **{chat_name}**.\n"
        "Messaggi brevi e diretti, poco markdown, emoji con moderazione."
    )

    memory_server = build_memory_server(scope_dir)
    chat_cfg = config.get_chat_config(jid)

    _print_log(
        _c(CYAN, f"[{chat_name}]")
        + f" {len(new_msgs)} new messages, replying..."
        + (_c(DIM, " [thinking]") if chat_cfg.get("thinking") else "")
    )

    reply = await call_claude_wa(
        system_prompt,
        messages,
        memory_server,
        CLAUDE_MODEL,
        thinking=chat_cfg.get("thinking", False),
        extra_mcp_servers=config.mcp_servers,
    )

    if not reply:
        _print_log(_c(YELLOW, f"[{chat_name}] Empty response from Claude"))
        return

    ok = await asyncio.to_thread(send_via_bridge, api_url, jid, reply)
    status = _c(GREEN, "OK") if ok else _c(RED, "FAIL")
    _print_log(f"{status} [{chat_name}] Reply sent ({len(reply)} chars)")


# ---------------------------------------------------------------------------
# Interactive CLI
# ---------------------------------------------------------------------------

_last_chats: list[dict] = []  # result of the last 'chats' for numeric reference


def _print_log(msg: str) -> None:
    """Print a log without messing up the prompt (overwrites the prompt line)."""
    print(f"\r{msg}")
    print(_c(DIM, "wa> "), end="", flush=True)


def _list_chats_from_db(db_path: str, query_str: str = "", limit: int = 30) -> list[dict]:
    """Read available chats from the bridge DB."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=0&cache=private", uri=True)
        try:
            cursor = conn.cursor()
            search = f"%{query_str}%" if query_str else "%"
            cursor.execute(
                """
                SELECT c.jid, c.name, c.last_message_time,
                       (SELECT content FROM messages
                        WHERE chat_jid = c.jid
                        ORDER BY timestamp DESC LIMIT 1) as last_msg
                FROM chats c
                WHERE (c.name LIKE ? OR c.jid LIKE ?)
                ORDER BY COALESCE(c.last_message_time, 0) DESC
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
        finally:
            conn.close()
    except sqlite3.Error as e:
        print(_c(RED, f"DB error: {e}"))
        return []


def _resolve_jid(arg: str, config: WatchConfig, db_path: str) -> str | None:
    """
    Resolve the argument of the watch/unwatch command.
    Can be a number (from the 'chats' list), a name substring, or a full JID.
    """
    # Number from the previous list
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(_last_chats):
            return _last_chats[idx]["jid"]
        print(_c(RED, f"Invalid number {arg}. Run 'chats' to see the list."))
        return None

    # Direct JID (contains @)
    if "@" in arg:
        return arg

    # Name substring: contact map first (authoritative phone↔LID), then cached list
    contact_jid = _contact_map_resolve_name(arg)
    if contact_jid:
        return contact_jid

    candidates = [c for c in _last_chats if arg.lower() in c["name"].lower()]
    if len(candidates) == 1:
        return candidates[0]["jid"]
    if len(candidates) > 1:
        print(_c(YELLOW, f"Ambiguous — matches {len(candidates)} chats:"))
        for i, c in enumerate(candidates, 1):
            print(f"  {i}. {_c(BOLD, c['name'])} - {_c(DIM, c['jid'])}")
        return None

    # Search in the DB
    if db_path and Path(db_path).is_file():
        results = _list_chats_from_db(db_path, arg, limit=5)
        if len(results) == 1:
            return results[0]["jid"]
        if results:
            print(_c(YELLOW, f"No exact match, found {len(results)} chats. Run 'chats {arg}' to see."))
    print(_c(RED, f"'{arg}' could not be resolved. Use the full JID or a number from 'chats'."))
    return None


def _cmd_chats(args: list[str], db_path: str) -> None:
    global _last_chats
    if not Path(db_path).exists():
        print(_c(RED, f"DB not found: {db_path}\nStart the Go bridge first."))
        return
    query_str = " ".join(args)
    chats = _list_chats_from_db(db_path, query_str)
    _last_chats = chats
    if not chats:
        print(_c(YELLOW, "No chats found" + (f" for '{query_str}'" if query_str else "") + "."))
        return
    print()
    for i, c in enumerate(chats, 1):
        name = _c(BOLD, c["name"])
        jid = _c(DIM, c["jid"])
        ts = _c(DIM, c["last_time"][:16]) if c["last_time"] else ""
        last = _c(DIM, f'  "{c["last_msg"]}"') if c["last_msg"] else ""
        print(f"  {_c(CYAN, str(i).rjust(2))}. {name}  {jid}  {ts}{last}")
    print()


def _cmd_watch(args: list[str], config: WatchConfig, checkpoints: WhatsappCheckpoints, db_path: str) -> None:
    if not args:
        print(_c(YELLOW, "Usage: watch <jid | number | name>"))
        return
    jid = _resolve_jid(" ".join(args), config, db_path)
    if jid is None:
        return
    name = next((c["name"] for c in _last_chats if c["jid"] == jid), jid)
    already_watched = not config.add(jid)
    # Always reset the checkpoint to "now" — whether new or already there
    checkpoints.update(jid, datetime.now(timezone.utc))
    if already_watched:
        print(_c(GREEN, f"Checkpoint reset: {_c(BOLD, name)} - listening from now"))
    else:
        print(_c(GREEN, f"Added: {_c(BOLD, name)} ({jid}) - listening from now"))


def _cmd_unwatch(args: list[str], config: WatchConfig, db_path: str) -> None:
    if not args:
        print(_c(YELLOW, "Usage: unwatch <jid | number | name>"))
        return
    jid = _resolve_jid(" ".join(args), config, db_path)
    if jid is None:
        return
    name = next((c["name"] for c in _last_chats if c["jid"] == jid), jid)
    if config.remove(jid):
        print(_c(GREEN, f"Removed: {_c(BOLD, name)} ({jid})"))
    else:
        print(_c(YELLOW, f"'{name}' was not being monitored."))


def _cmd_list(config: WatchConfig, db_path: str) -> None:
    jids = config.watched_jids
    if not jids:
        print(_c(YELLOW, "No chats monitored. Use 'watch <jid>' to add one."))
        return
    print()
    for i, jid in enumerate(jids, 1):
        name = next((c["name"] for c in _last_chats if c["jid"] == jid), "")
        if not name and db_path and Path(db_path).is_file():
            results = _list_chats_from_db(db_path, jid, limit=1)
            name = results[0]["name"] if results else ""
        label = f"{_c(BOLD, name)}  " if name else ""
        print(f"  {_c(GREEN, str(i).rjust(2))}. {label}{_c(DIM, jid)}")
    print()


def _cmd_status(config: WatchConfig, db_path: str, api_url: str) -> None:
    # Bridge DB
    db_ok = bool(db_path) and Path(db_path).is_file()
    db_status = _c(GREEN, "found") if db_ok else _c(RED, "not found")

    # Bridge HTTP
    try:
        resp = requests.get(f"{api_url.rstrip('/').replace('/api', '')}/health", timeout=2)
        bridge_ok = resp.status_code == 200
    except requests.RequestException:
        bridge_ok = False
    bridge_status = _c(GREEN, "responding") if bridge_ok else _c(YELLOW, "unreachable")

    watched = config.watched_jids
    print()
    print(f"  Bridge DB:   {db_status}  ({db_path})")
    print(f"  Bridge HTTP: {bridge_status}  ({api_url})")
    print(f"  Model:       {_c(BOLD, CLAUDE_MODEL)}")
    print(f"  Memory:      {NOVA_MEMORY_DIR}")
    mcp_count = len(config.mcp_servers)
    mcp_label = f"{mcp_count} server{'s' if mcp_count != 1 else ''}" if mcp_count else _c(DIM, "none")
    print(f"  MCP config:  {config.mcp_path}  [{mcp_label}]")
    print(f"  Poll:        every {_c(BOLD, str(config.poll_interval))}s")
    print(f"  Chats:       {_c(BOLD, str(len(watched)))} monitored")
    print()


def _cmd_interval(args: list[str], config: WatchConfig) -> None:
    if not args or not args[0].replace(".", "", 1).isdigit():
        print(_c(YELLOW, "Usage: interval <seconds>  (e.g. interval 10)"))
        return
    val = float(args[0])
    if val < 1:
        print(_c(RED, "Minimum interval: 1 second"))
        return
    config.set_interval(val)
    print(_c(GREEN, f"Interval set to {val}s"))


def _cmd_config(args: list[str], config: WatchConfig, db_path: str) -> None:
    """Show or modify per-chat options and global MCP servers.

    Per-chat (thinking):
      config <chat>                  — show thinking setting for a chat
      config <chat> thinking on|off  — toggle extended thinking

    Global MCP servers (used by all chats):
      config mcp list                — list configured MCP servers
      config mcp add <name> <json>   — add an MCP server
      config mcp remove <name>       — remove an MCP server
    """
    if not args:
        print(_c(YELLOW, "Usage: config mcp list|add|remove  OR  config <chat> thinking on|off"))
        return

    # Global MCP commands: config mcp ...
    if args[0] == "mcp":
        sub = args[1:]
        if not sub or sub[0] == "list":
            servers = config.mcp_servers
            if not servers:
                print(_c(DIM, "No extra MCP servers configured."))
            else:
                print()
                for sname, scfg in servers.items():
                    print(f"  {_c(BOLD, sname)}: {json.dumps(scfg)}")
                print()
            return
        if sub[0] == "add":
            if len(sub) < 3:
                print(_c(YELLOW, "Usage: config mcp add <name> <json>"))
                print(_c(DIM, 'Example: config mcp add home_assistant {"type":"stdio","command":"python","args":["-m","ha_mcp"]}'))
                return
            sname = sub[1]
            try:
                scfg = json.loads(" ".join(sub[2:]))
            except json.JSONDecodeError as e:
                print(_c(RED, f"Invalid JSON: {e}"))
                return
            config.add_mcp_server(sname, scfg)
            print(_c(GREEN, f"Added MCP server '{sname}' (active for all chats)"))
            return
        if sub[0] == "remove":
            if len(sub) < 2:
                print(_c(YELLOW, "Usage: config mcp remove <name>"))
                return
            ok = config.remove_mcp_server(sub[1])
            if ok:
                print(_c(GREEN, f"Removed MCP server '{sub[1]}'"))
            else:
                print(_c(YELLOW, f"Server '{sub[1]}' not found"))
            return
        print(_c(YELLOW, f"Unknown mcp action: {sub[0]}"))
        return

    # Per-chat commands: config <chat> [thinking on|off]
    jid = _resolve_jid(args[0], config, db_path)
    if jid is None:
        return
    sub = args[1:]
    name = next((c["name"] for c in _last_chats if c["jid"] == jid), jid)

    if not sub:
        cfg = config.get_chat_config(jid)
        print(f"\n  {_c(BOLD, name)} ({jid})")
        print(f"  thinking: {_c(GREEN, 'on') if cfg.get('thinking') else _c(DIM, 'off')}")
        print()
        return

    if sub[0] == "thinking":
        if len(sub) < 2 or sub[1] not in ("on", "off"):
            print(_c(YELLOW, "Usage: config <chat> thinking on|off"))
            return
        enabled = sub[1] == "on"
        config.set_thinking(jid, enabled)
        state = _c(GREEN, "enabled") if enabled else _c(DIM, "disabled")
        print(f"Thinking {state} for {name}")
        return

    print(_c(YELLOW, f"Unknown config option: {sub[0]}"))


_DEFAULT_NOTIFY_MSG = "Ciao! Sono Nova, sono in ascolto qui. Scrivimi pure."


def _cmd_notify(args: list[str], config: WatchConfig, api_url: str) -> None:
    """Send a notification message to a watched chat (or to all if no args)."""
    watched = config.watched_jids
    if not watched:
        print(_c(YELLOW, "No chats monitored. Use 'watch' first."))
        return

    # Optional argument: specific JID/number/name
    if args:
        raw = " ".join(args)
        # May be an index from the 'list' command
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(watched):
                targets = [watched[idx]]
            else:
                print(_c(RED, f"Invalid number {raw} (you have {len(watched)} monitored chats)."))
                return
        elif "@" in raw:
            if raw not in watched:
                print(_c(YELLOW, f"'{raw}' is not in the watched list."))
                return
            targets = [raw]
        else:
            # Substring match on the name
            matches = [
                j for j in watched
                if raw.lower() in next((c["name"] for c in _last_chats if c["jid"] == j), j).lower()
            ]
            if not matches:
                print(_c(RED, f"No monitored chat matches '{raw}'."))
                return
            targets = matches
    else:
        targets = list(watched)

    async def _run() -> None:
        for jid in targets:
            name = next((c["name"] for c in _last_chats if c["jid"] == jid), jid)
            ok = await asyncio.to_thread(send_via_bridge, api_url, jid, _DEFAULT_NOTIFY_MSG)
            if ok:
                _print_log(_c(GREEN, f"Notification sent to {_c(BOLD, name)}"))
            else:
                _print_log(_c(RED, f"Send failed for {_c(BOLD, name)} ({jid})"))

    asyncio.get_running_loop().create_task(_run())


def _cmd_fetch(args: list[str], config: WatchConfig, checkpoints: WhatsappCheckpoints, db_path: str, api_url: str) -> None:
    """Read the current conversation and respond if the last message is unanswered.

    Usage:
      fetch [jid|n]          — reply only if last message is unanswered
      fetch [jid|n] force    — always reply, ignoring the already-replied check
    """
    force = False
    if args and args[-1].lower() == "force":
        force = True
        args = args[:-1]

    if args:
        jid = _resolve_jid(" ".join(args), config, db_path)
        if jid is None:
            return
        targets = [jid]
    else:
        targets = list(config.watched_jids)
        if not targets:
            print(_c(YELLOW, "No chats monitored."))
            return

    async def _run() -> None:
        for jid in targets:
            name = next((c["name"] for c in _last_chats if c["jid"] == jid), jid)
            tail = await asyncio.to_thread(fetch_tail, db_path, jid, 40, _CONTACT_MAP)
            if not tail:
                print(_c(YELLOW, f"[{name}] No messages found."))
                continue

            # Find the last inbound message and check if Nova already replied after it
            last_inbound_idx = max(
                (i for i, m in enumerate(tail) if not m["is_from_me"]), default=None
            )
            if last_inbound_idx is None:
                print(_c(YELLOW, f"[{name}] No inbound messages found."))
                continue

            already_replied = any(m["is_from_me"] for m in tail[last_inbound_idx + 1:])
            if already_replied and not force:
                print(_c(DIM, f"[{name}] Already replied — nothing to do. (use 'fetch {jid} force' to reply anyway)"))
                continue
            if already_replied and force:
                print(_c(YELLOW, f"[{name}] Forcing reply despite detected prior response."))

            # Build context: history before the last inbound + unanswered messages
            unanswered = [m for m in tail[last_inbound_idx:] if not m["is_from_me"]]
            history = tail[:last_inbound_idx]
            messages = build_messages_for_claude(history, unanswered)

            jid_variants = await asyncio.to_thread(resolve_jid_variants, db_path, jid, _CONTACT_MAP)
            scope_jid = next(
                (v for v in jid_variants if scope_dir_for(NOVA_MEMORY_DIR, "whatsapp", v).exists()),
                jid,
            )
            scope_dir = scope_dir_for(NOVA_MEMORY_DIR, "whatsapp", scope_jid)
            ensure_scope_skeleton(scope_dir, "whatsapp")
            scope_mem = load_scope_memory(scope_dir)
            shared_mem = load_shared_memory(NOVA_MEMORY_DIR)
            user_mem = load_user_memory(USER_MEMORY_DIR) if USER_MEMORY_DIR.exists() else ""
            memory_server = build_memory_server(scope_dir)

            system_prompt = build_system_prompt(shared_mem, scope_mem, user_mem, bot_display_name="Nova")

            chat_cfg = config.get_chat_config(jid)
            _print_log(
                _c(CYAN, f"[{name}] Fetching — generating reply...")
                + (_c(DIM, " [thinking]") if chat_cfg.get("thinking") else "")
            )
            reply = await call_claude_wa(
                system_prompt,
                messages,
                memory_server,
                CLAUDE_MODEL,
                thinking=chat_cfg.get("thinking", False),
                extra_mcp_servers=config.mcp_servers,
            )
            if not reply:
                _print_log(_c(YELLOW, f"[{name}] Empty response from Claude."))
                continue

            ok = await asyncio.to_thread(send_via_bridge, api_url, jid, reply)
            status = _c(GREEN, "OK") if ok else _c(RED, "FAIL")
            _print_log(f"{status} [{name}] Reply sent ({len(reply)} chars)")

            # Advance checkpoint so the poller doesn't re-trigger
            try:
                latest_ts = datetime.fromisoformat(unanswered[-1]["timestamp"])
                if latest_ts.tzinfo is None:
                    latest_ts = latest_ts.replace(tzinfo=timezone.utc)
            except ValueError:
                latest_ts = datetime.now(timezone.utc)
            checkpoints.update(jid, latest_ts)

    asyncio.get_running_loop().create_task(_run())


def _cmd_debug(args: list[str], db_path: str, checkpoints: WhatsappCheckpoints) -> None:
    """Show raw DB state for a JID to diagnose polling issues."""
    if not args:
        print(_c(YELLOW, "Usage: debug <jid>"))
        return
    jid = " ".join(args)
    checkpoint = checkpoints.get(jid)
    print(f"\n  Checkpoint:  {checkpoint.isoformat() if checkpoint else _c(YELLOW, 'None (first run)')}")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=0&cache=private", uri=True)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, timestamp, sender, content FROM messages WHERE chat_jid = ? ORDER BY rowid DESC LIMIT 3",
                [jid],
            )
            rows = cursor.fetchall()
        finally:
            conn.close()
        if not rows:
            print(_c(YELLOW, f"  No messages found for {jid}"))
            return
        print(f"  Last {len(rows)} messages (raw timestamp format):")
        for r in rows:
            print(f"    id={r[0]}  ts={_c(CYAN, str(r[1]))}  sender={r[2]}")
            content_str = str(r[3]) if r[3] is not None else "(null)"
            print(f"    content ({len(content_str)} chars): {content_str[:300]}")
            if len(content_str) > 300:
                print(f"    ... (+{len(content_str) - 300} more chars)")
    except sqlite3.Error as e:
        print(_c(RED, f"  DB error: {e}"))
    print()


def _cmd_context(args: list[str], config: WatchConfig, db_path: str) -> None:
    """Show the exact prompt that would be sent to Claude for a given chat."""
    if args:
        jid = _resolve_jid(" ".join(args), config, db_path)
        if jid is None:
            return
    else:
        watched = config.watched_jids
        if not watched:
            print(_c(YELLOW, "No chats monitored. Use 'watch' first or pass a JID."))
            return
        jid = watched[0]

    async def _run() -> None:
        name = next((c["name"] for c in _last_chats if c["jid"] == jid), jid)
        tail = await asyncio.to_thread(fetch_tail, db_path, jid, 40, _CONTACT_MAP)
        if not tail:
            print(_c(YELLOW, f"[{name}] No messages in DB."))
            return

        last_inbound_idx = max(
            (i for i, m in enumerate(tail) if not m["is_from_me"]), default=None
        )
        if last_inbound_idx is None:
            print(_c(YELLOW, f"[{name}] No inbound messages found."))
            return

        unanswered = [m for m in tail[last_inbound_idx:] if not m["is_from_me"]]
        history = tail[:last_inbound_idx]
        messages = build_messages_for_claude(history, unanswered)
        prompt = serialize_history_wa(messages)

        print(f"\n{_c(BOLD, f'[{name}] Context that would be sent to Claude:')}")
        print(f"{_c(DIM, '─' * 60)}")
        print(f"  Messages in context: {len(messages)} turns ({len(tail)} messages fetched)")
        print(f"  Prompt length: {len(prompt)} chars\n")
        print(prompt)
        print(f"{_c(DIM, '─' * 60)}\n")

    asyncio.get_running_loop().create_task(_run())


def _cmd_help() -> None:
    print(f"""
{_c(BOLD, "Available commands:")}

  {_c(CYAN, "chats [query]")}      List chats in the bridge DB (filter by name/JID)
  {_c(CYAN, "watch <jid|n>")}      Start monitoring a chat (JID, number from 'chats', or name)
  {_c(CYAN, "unwatch <jid|n>")}    Stop monitoring a chat
  {_c(CYAN, "list")}               Show currently monitored chats
  {_c(CYAN, "status")}             Show server, bridge and config status
  {_c(CYAN, "interval <n>")}       Change the polling interval (seconds)
  {_c(CYAN, "notify [jid|n]")}     Send a "Nova is listening" notice (all chats or one)
  {_c(CYAN, "fetch [jid|n]")}      Read current chat and reply if last message is unanswered
  {_c(CYAN, "context [jid|n]")}    Show the exact prompt Claude would receive for a chat
  {_c(CYAN, "config <chat> thinking on|off")}  Toggle extended thinking for a chat
  {_c(CYAN, "config mcp list")}               List global extra MCP servers
  {_c(CYAN, "config mcp add <name> <json>")}  Add a global MCP server (all chats)
  {_c(CYAN, "config mcp remove <name>")}      Remove a global MCP server
  {_c(CYAN, "debug <jid>")}        Raw DB inspection for a specific JID
  {_c(CYAN, "help")}               Show this help
  {_c(CYAN, "quit")} / {_c(CYAN, "exit")}        Stop the server

{_c(DIM, "Tip: use the numbers from 'chats' for watch/unwatch without copying JIDs.")}
""")


async def _command_loop(
    config: WatchConfig,
    checkpoints: WhatsappCheckpoints,
    stop_event: asyncio.Event,
    db_path: str,
    api_url: str,
) -> None:
    loop = asyncio.get_running_loop()
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
            _cmd_watch(args, config, checkpoints, db_path)
        elif cmd == "unwatch":
            _cmd_unwatch(args, config, db_path)
        elif cmd == "list":
            _cmd_list(config, db_path)
        elif cmd == "status":
            _cmd_status(config, db_path, api_url)
        elif cmd == "interval":
            _cmd_interval(args, config)
        elif cmd == "notify":
            _cmd_notify(args, config, api_url)
        elif cmd in ("help", "?", "h"):
            _cmd_help()
        elif cmd == "fetch":
            _cmd_fetch(args, config, checkpoints, db_path, api_url)
        elif cmd == "context":
            _cmd_context(args, config, db_path)
        elif cmd == "config":
            _cmd_config(args, config, db_path)
        elif cmd == "debug":
            _cmd_debug(args, db_path, checkpoints)
        else:
            print(_c(YELLOW, f"Unknown command: '{cmd}'. Type 'help' for the list."))

    stop_event.set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _banner(db_path: str) -> None:
    print(f"""
{_c(BOLD + MAGENTA, "+----------------------------------+")}
{_c(BOLD + MAGENTA, "|")}  {_c(BOLD, "Nova WhatsApp Server")}            {_c(BOLD + MAGENTA, "|")}
{_c(BOLD + MAGENTA, "+----------------------------------+")}

  DB:     {_c(DIM, db_path)}
  Memory: {_c(DIM, str(NOVA_MEMORY_DIR))}
  Model:  {_c(BOLD, CLAUDE_MODEL)}

Type {_c(CYAN, "help")} for commands, {_c(CYAN, "status")} for the bridge status.
""")


async def _main(db_path: str, api_url: str, interval_override: float | None = None) -> None:
    if not NOVA_MEMORY_DIR or str(NOVA_MEMORY_DIR) == ".":
        sys.exit("Missing NOVA_MEMORY_DIR in .env")
    if not db_path:
        sys.exit(
            "Missing bridge DB path. Set WHATSAPP_BRIDGE_DB in .env or pass --db <path>."
        )
    template_dir = Path(__file__).parent / "memory.example"
    if template_dir.exists():
        bootstrap_memory_dir(NOVA_MEMORY_DIR, template_dir)
    NOVA_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    ensure_shared_skeleton(NOVA_MEMORY_DIR)

    config = WatchConfig(CONFIG_FILE)
    if interval_override is not None:
        config.set_interval(interval_override)
    checkpoints = WhatsappCheckpoints(NOVA_MEMORY_DIR / "whatsapp_checkpoints.json")

    # On restart, reset checkpoints of all already-configured JIDs to "now" to
    # avoid processing messages accumulated during downtime.
    now = datetime.now(timezone.utc)
    for jid in config.watched_jids:
        checkpoints.update(jid, now)

    stop_event = asyncio.Event()

    # Graceful Ctrl+C handling
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows does not support add_signal_handler for all signals

    _banner(db_path)

    wakeup_event = asyncio.Event()
    await asyncio.gather(
        _poll_loop(config, checkpoints, db_path, api_url, stop_event, wakeup_event),
        _wal_watcher(db_path, wakeup_event, stop_event),
        _command_loop(config, checkpoints, stop_event, db_path, api_url),
    )

    print(_c(DIM, "\nServer stopped."))


def main() -> None:
    parser = argparse.ArgumentParser(description="Nova WhatsApp Server")
    parser.add_argument(
        "--db",
        default=DEFAULT_BRIDGE_DB,
        help="Path to the Go bridge's messages.db",
    )
    parser.add_argument(
        "--api",
        default=DEFAULT_API_URL,
        help="Bridge API base URL (default: http://localhost:8080/api)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Override poll interval in seconds",
    )
    args = parser.parse_args()

    # Silence noisy library loggers
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger("claude_agent_sdk").setLevel(logging.ERROR)
    logging.getLogger("nova").setLevel(logging.ERROR)

    try:
        asyncio.run(_main(args.db, args.api, args.interval))
    except KeyboardInterrupt:
        print(_c(DIM, "\nInterrupted."))


if __name__ == "__main__":
    main()
