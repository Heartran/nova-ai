"""
nova_read.py — MCP server in-process per la LETTURA del server Discord.

Tools (scoped al guild dove Nova e' stata triggerata):
- list_channels():                 elenca i canali testuali leggibili (escluso blacklist)
- read_channel_history(...):       fetch dei messaggi recenti di un canale
- search_in_channel(...):          filtro keyword nei messaggi
- search_members(query, limit):    ricerca membri per nome (no Members Intent richiesto)
- get_member_info(user_id):        info su un singolo membro

Sicurezza:
- Cross-guild bloccato: tutti i channel/member id sono validati contro il guild
  catturato nel closure.
- Blacklist per-guild in `<scope>/_state/read_blacklist.json`. Nova rifiuta di
  leggere canali blacklisted anche se ha permessi Discord.
- Audit log append-only in `<scope>/_audit/reads.log`.
- Discord enforce gia' i permessi del ruolo del bot (403 -> Forbidden -> errore
  graceful).

build_read_server(client, guild, scope_dir, requester) costruisce il server con
client + guild + identita' del richiedente nel closure.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import discord
from claude_agent_sdk import create_sdk_mcp_server, tool

logger = logging.getLogger(__name__)

# Cap di sicurezza per evitare token-bomb e rate limit Discord
MAX_HISTORY_LIMIT = 200
MAX_SEARCH_LIMIT = 50
MAX_MEMBER_SEARCH_LIMIT = 25


# ---------------------------------------------------------------------------
# State: blacklist + audit log
# ---------------------------------------------------------------------------
def _state_dir(scope_dir: Path) -> Path:
    return scope_dir / "_state"


def _audit_dir(scope_dir: Path) -> Path:
    return scope_dir / "_audit"


def _blacklist_path(scope_dir: Path) -> Path:
    return _state_dir(scope_dir) / "read_blacklist.json"


def load_read_blacklist(scope_dir: Path) -> set[int]:
    path = _blacklist_path(scope_dir)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {int(x) for x in data.get("channels", [])}
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.error("Errore caricamento blacklist %s: %s", path, e)
        return set()


def save_read_blacklist(scope_dir: Path, channels: set[int]) -> None:
    path = _blacklist_path(scope_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"channels": sorted(channels)}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _audit_append(scope_dir: Path, log_name: str, requester: str, action: str, target: str, blocked: bool) -> None:
    log_path = _audit_dir(scope_dir) / log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().isoformat(timespec="seconds")
    status = "BLOCKED" if blocked else "OK"
    line = f"[{timestamp}] [{status}] {requester} | {action} | {target}\n"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        logger.error("Errore audit log %s: %s", log_path, e)


def audit_read(scope_dir: Path, requester: str, action: str, target: str, blocked: bool = False) -> None:
    """Log append-only di ogni operazione di lettura del server (canali/membri)."""
    _audit_append(scope_dir, "reads.log", requester, action, target, blocked)


def audit_web(scope_dir: Path, requester: str, action: str, target: str, blocked: bool = False) -> None:
    """Log append-only di ogni fetch/search web fatta da Nova."""
    _audit_append(scope_dir, "web.log", requester, action, target, blocked)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": f"Errore: {msg}"}]}


def _ok(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}]}


def _format_message(m: discord.Message) -> str:
    when = m.created_at.strftime("%Y-%m-%d %H:%M")
    text = (m.clean_content or "").replace("\n", " ⏎ ")
    if m.attachments:
        text += f" [+{len(m.attachments)} attachment]"
    return f"[{when}] {m.author.display_name}: {text}"


def _format_member(member: discord.Member) -> str:
    roles = [r.name for r in member.roles if r.name != "@everyone"]
    parts = [
        f"id={member.id}",
        f"name={member.name}",
        f"display={member.display_name}",
    ]
    if member.nick:
        parts.append(f"nick={member.nick}")
    if member.joined_at:
        parts.append(f"joined={member.joined_at.strftime('%Y-%m-%d')}")
    if roles:
        parts.append(f"roles=[{', '.join(roles)}]")
    if member.bot:
        parts.append("bot=true")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Server builder
# ---------------------------------------------------------------------------
def build_read_server(
    client: discord.Client,
    guild: discord.Guild | None,
    scope_dir: Path,
    requester: str,
):
    """
    Costruisce un MCP server con i tool di lettura Discord, scoped al guild.
    Se guild e' None (DM), i tool ritornano errore appropriato.
    """

    blacklist = load_read_blacklist(scope_dir) if guild is not None else set()

    def _channel_in_guild(channel_id: int) -> discord.TextChannel | None:
        if guild is None:
            return None
        ch = guild.get_channel(channel_id)
        if isinstance(ch, discord.TextChannel):
            return ch
        return None

    @tool(
        "list_channels",
        "Elenca i canali testuali del server corrente che non sono in blacklist. "
        "Ritorna id, nome e categoria di ogni canale.",
        {},
    )
    async def list_channels(args):
        if guild is None:
            audit_read(scope_dir, requester, "list_channels", "DM", blocked=True)
            return _err("Disponibile solo nei server, non in DM.")

        out = []
        skipped = 0
        for ch in guild.text_channels:
            if ch.id in blacklist:
                skipped += 1
                continue
            cat = f" / {ch.category.name}" if ch.category else ""
            out.append(f"- #{ch.name} (id={ch.id}){cat}")

        audit_read(scope_dir, requester, "list_channels", f"{len(out)} channels (+ {skipped} blacklisted)")
        if not out:
            return _ok("Nessun canale leggibile (tutti in blacklist o nessun text channel).")
        body = "\n".join(out)
        if skipped:
            body += f"\n\n_({skipped} canali in blacklist non mostrati)_"
        return _ok(body)

    @tool(
        "read_channel_history",
        f"Legge gli ultimi N messaggi di un canale del server corrente. "
        f"limit max {MAX_HISTORY_LIMIT}, default 50. Ritorna formato "
        f"'[YYYY-MM-DD HH:MM] author: text', dal piu' vecchio al piu' nuovo.",
        {"channel_id": int, "limit": int},
    )
    async def read_channel_history(args):
        if guild is None:
            audit_read(scope_dir, requester, "read_channel_history", "DM", blocked=True)
            return _err("Disponibile solo nei server.")

        cid = int(args.get("channel_id", 0) or 0)
        limit = max(1, min(int(args.get("limit") or 50), MAX_HISTORY_LIMIT))

        ch = _channel_in_guild(cid)
        if ch is None:
            audit_read(scope_dir, requester, "read_channel_history", f"unknown:{cid}", blocked=True)
            return _err(f"Canale {cid} non esiste in questo server.")

        if cid in blacklist:
            audit_read(scope_dir, requester, "read_channel_history", f"#{ch.name}", blocked=True)
            return _err(f"#{ch.name} e' in blacklist, non lo leggo.")

        try:
            msgs: list[str] = []
            async for m in ch.history(limit=limit):
                msgs.append(_format_message(m))
            msgs.reverse()  # cronologico
        except discord.Forbidden:
            audit_read(scope_dir, requester, "read_channel_history", f"#{ch.name} FORBIDDEN", blocked=True)
            return _err(f"Discord nega l'accesso a #{ch.name} (controlla i permessi del mio ruolo).")
        except discord.HTTPException as e:
            return _err(f"Errore Discord leggendo #{ch.name}: {e}")

        audit_read(scope_dir, requester, "read_channel_history", f"#{ch.name} ({len(msgs)} msg)")
        if not msgs:
            return _ok(f"#{ch.name} non ha messaggi recenti.")
        return _ok(f"**#{ch.name}** — ultimi {len(msgs)} messaggi:\n" + "\n".join(msgs))

    @tool(
        "search_in_channel",
        f"Cerca una keyword (case-insensitive, substring) negli ultimi messaggi di un canale. "
        f"limit max {MAX_SEARCH_LIMIT}, default 20. Scansiona fino a {MAX_HISTORY_LIMIT} "
        f"messaggi, ritorna i match.",
        {"channel_id": int, "query": str, "limit": int},
    )
    async def search_in_channel(args):
        if guild is None:
            return _err("Disponibile solo nei server.")

        cid = int(args.get("channel_id", 0) or 0)
        query = (args.get("query") or "").strip()
        limit = max(1, min(int(args.get("limit") or 20), MAX_SEARCH_LIMIT))
        if not query:
            return _err("Query vuota.")

        ch = _channel_in_guild(cid)
        if ch is None:
            return _err(f"Canale {cid} non esiste in questo server.")

        if cid in blacklist:
            audit_read(scope_dir, requester, "search_in_channel", f"#{ch.name} '{query}'", blocked=True)
            return _err(f"#{ch.name} e' in blacklist, non ci cerco.")

        ql = query.lower()
        try:
            hits: list[str] = []
            scanned = 0
            async for m in ch.history(limit=MAX_HISTORY_LIMIT):
                scanned += 1
                if ql in (m.clean_content or "").lower():
                    hits.append(_format_message(m))
                    if len(hits) >= limit:
                        break
            hits.reverse()
        except discord.Forbidden:
            return _err(f"Discord nega l'accesso a #{ch.name}.")
        except discord.HTTPException as e:
            return _err(f"Errore Discord: {e}")

        audit_read(
            scope_dir,
            requester,
            "search_in_channel",
            f"#{ch.name} '{query}' ({len(hits)} hits su {scanned} scansionati)",
        )
        if not hits:
            return _ok(f"Nessun match per '{query}' in #{ch.name} (scansionati {scanned} messaggi).")
        return _ok(f"**Match per '{query}' in #{ch.name}**:\n" + "\n".join(hits))

    @tool(
        "search_members",
        f"Cerca membri del server per nome o nickname (substring, case-insensitive). "
        f"limit max {MAX_MEMBER_SEARCH_LIMIT}, default 10. Non richiede Members Intent.",
        {"query": str, "limit": int},
    )
    async def search_members(args):
        if guild is None:
            return _err("Disponibile solo nei server.")

        query = (args.get("query") or "").strip()
        limit = max(1, min(int(args.get("limit") or 10), MAX_MEMBER_SEARCH_LIMIT))
        if not query:
            return _err("Query vuota.")

        try:
            members = await guild.search_members(query, limit=limit)
        except discord.HTTPException as e:
            return _err(f"Errore Discord cercando membri: {e}")

        audit_read(scope_dir, requester, "search_members", f"'{query}' ({len(members)} match)")
        if not members:
            return _ok(f"Nessun membro trovato per '{query}'.")
        lines = [_format_member(m) for m in members]
        return _ok(f"**Membri match per '{query}'** ({len(members)}):\n" + "\n".join(lines))

    @tool(
        "get_member_info",
        "Info dettagliate su un singolo membro del server (ruoli, joined_at, ecc.) dato lo user_id.",
        {"user_id": int},
    )
    async def get_member_info(args):
        if guild is None:
            return _err("Disponibile solo nei server.")

        uid = int(args.get("user_id", 0) or 0)
        if uid == 0:
            return _err("user_id mancante.")

        try:
            member = guild.get_member(uid) or await guild.fetch_member(uid)
        except discord.NotFound:
            audit_read(scope_dir, requester, "get_member_info", f"uid={uid} NOT_FOUND", blocked=True)
            return _err(f"Nessun membro con user_id={uid} in questo server.")
        except discord.HTTPException as e:
            return _err(f"Errore Discord: {e}")

        audit_read(scope_dir, requester, "get_member_info", f"uid={uid} ({member.display_name})")
        return _ok(_format_member(member))

    return create_sdk_mcp_server(
        "nova_read",
        "1.0.0",
        [list_channels, read_channel_history, search_in_channel, search_members, get_member_info],
    )
