"""
nova_bot.py — Bot Discord che incarna Nova usando Claude.

Trigger su un messaggio quando:
  - il bot e' menzionato direttamente (@Nova)
  - il messaggio e' un reply a un messaggio del bot
  - il messaggio e' un DM al bot
  - la parola 'nova' compare nel testo (parola intera, case-insensitive)

Setup necessario:
  - File .env compilato (copia da .env.example)
  - Sul Developer Portal Discord: Privileged Gateway Intents -> MESSAGE CONTENT INTENT abilitato
  - Bot invitato col permesso 'Send Messages' + 'Read Message History' + 'View Channel'
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path

import discord
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    TextBlock,
    query,
)
from dotenv import load_dotenv

from checkpoints import ChannelCheckpoints
from memory import (
    ensure_scope_skeleton,
    ensure_shared_skeleton,
    load_scope_memory,
    load_shared_memory,
    load_user_memory,
    scope_dir_for,
)
from nova_mcp import build_memory_server
from nova_read import audit_web, build_read_server
from personality import build_system_prompt
from slash_commands import register_slash_commands


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6").strip()
NOVA_MEMORY_DIR = Path(os.getenv("NOVA_MEMORY_DIR", "")).expanduser()
USER_MEMORY_DIR = Path(os.getenv("USER_MEMORY_DIR", "")).expanduser()
HISTORY_MESSAGES = int(os.getenv("HISTORY_MESSAGES", "10"))
CHANNEL_COOLDOWN_SECONDS = float(os.getenv("CHANNEL_COOLDOWN_SECONDS", "2"))
MAX_RESPONSE_CHARS = int(os.getenv("MAX_RESPONSE_CHARS", "1900"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

if not DISCORD_TOKEN:
    sys.exit("Manca DISCORD_TOKEN in .env")
if not NOVA_MEMORY_DIR or str(NOVA_MEMORY_DIR) == ".":
    sys.exit("Manca NOVA_MEMORY_DIR in .env")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Tagliare il rumore: discord.py e claude-agent-sdk sono molto verbosi a INFO/DEBUG.
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)
logger = logging.getLogger("nova")

# -----------------------------------------------------------------------------
# Setup memoria + checkpoint storage
# -----------------------------------------------------------------------------
# Lo scope (server o DM) viene risolto per ogni messaggio in `on_message`.
# La cartella radice deve esistere, le sottocartelle nascono lazy.
NOVA_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
ensure_shared_skeleton(NOVA_MEMORY_DIR)

checkpoints = ChannelCheckpoints(NOVA_MEMORY_DIR / "checkpoints.json")

MEMORY_TOOLS = [
    "mcp__nova_memory__note_remember",
    "mcp__nova_memory__memory_append",
]
READ_TOOLS = [
    "mcp__nova_read__list_channels",
    "mcp__nova_read__read_channel_history",
    "mcp__nova_read__search_in_channel",
    "mcp__nova_read__search_members",
    "mcp__nova_read__get_member_info",
]
WEB_TOOLS = ["WebFetch", "WebSearch"]
ALL_TOOLS = MEMORY_TOOLS + READ_TOOLS + WEB_TOOLS


def _build_can_use_tool(scope_dir: Path, requester: str):
    """Callback per audit dei web tool. Logga e poi sempre 'allow'."""

    async def can_use_tool(tool_name: str, tool_input: dict, context):
        if tool_name == "WebFetch":
            url = str(tool_input.get("url") or "<no url>")
            audit_web(scope_dir, requester, "WebFetch", url[:300])
        elif tool_name == "WebSearch":
            query_str = str(tool_input.get("query") or "<no query>")
            audit_web(scope_dir, requester, "WebSearch", query_str[:300])
        return PermissionResultAllow()

    return can_use_tool


def _resolve_scope(message: discord.Message) -> tuple[str, int, Path]:
    """Risolve lo scope memoria del messaggio: ('server', guild_id, dir) o ('dm', user_id, dir)."""
    if message.guild is not None:
        scope_type = "server"
        scope_id = message.guild.id
    else:
        scope_type = "dm"
        scope_id = message.author.id
    scope_dir = scope_dir_for(NOVA_MEMORY_DIR, scope_type, scope_id)
    return scope_type, scope_id, scope_dir

# -----------------------------------------------------------------------------
# Client setup
# -----------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True   # serve MESSAGE CONTENT INTENT abilitato sul portale
intents.messages = True
intents.guilds = True
# DM funzionano con default intents; non servono privileged extra.

client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)
register_slash_commands(tree, NOVA_MEMORY_DIR, scope_dir_for, ensure_scope_skeleton)

# Cooldown per canale: {channel_id: last_timestamp}
_last_response_at: dict[int, float] = {}

# Regex per la keyword 'nova' come parola intera (case-insensitive)
NOVA_KEYWORD = re.compile(r"\bnova\b", re.IGNORECASE)


# -----------------------------------------------------------------------------
# Trigger logic
# -----------------------------------------------------------------------------
async def should_respond(message: discord.Message) -> tuple[bool, str]:
    """
    Decide se Nova deve rispondere a un messaggio.

    Returns:
        (decisione, motivo) — il motivo serve solo per i log.
    """
    if message.author.bot:
        return False, "autore e' un bot"
    if not message.content and not message.attachments:
        return False, "messaggio vuoto"
    if client.user is None:
        return False, "client.user non ancora pronto"

    # 1) DM diretto al bot
    if isinstance(message.channel, discord.DMChannel):
        return True, "DM"

    # 2) Menzione diretta
    if client.user in message.mentions:
        return True, "mention"

    # 3) Reply a un messaggio di Nova
    if message.reference and message.reference.resolved:
        ref = message.reference.resolved
        if isinstance(ref, discord.Message) and ref.author.id == client.user.id:
            return True, "reply"

    # 3b) Reply ma il msg non e' stato risolto -> fetch
    if message.reference and not message.reference.resolved and message.reference.message_id:
        try:
            ref = await message.channel.fetch_message(message.reference.message_id)
            if ref.author.id == client.user.id:
                return True, "reply (fetched)"
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # 4) Keyword 'nova' nel testo
    if NOVA_KEYWORD.search(message.content):
        return True, "keyword"

    return False, "nessun trigger"


def cooldown_ok(channel_id: int) -> bool:
    """Restituisce True se il canale e' fuori cooldown."""
    last = _last_response_at.get(channel_id, 0.0)
    return (time.time() - last) >= CHANNEL_COOLDOWN_SECONDS


# -----------------------------------------------------------------------------
# History -> messages API
# -----------------------------------------------------------------------------
async def collect_history(
    channel: discord.abc.Messageable,
    bot_user: discord.ClientUser,
    current_message: discord.Message,
    limit: int,
) -> list[dict]:
    """
    Legge gli ultimi `limit` messaggi del canale (esclusi quelli senza testo
    e i comandi di altri bot), li trasforma nel formato Anthropic Messages API,
    accorpando user/assistant consecutivi.

    L'ultimo elemento della lista e' SEMPRE il messaggio corrente come user.
    """
    raw: list[discord.Message] = []
    try:
        async for m in channel.history(limit=limit, before=current_message):
            raw.append(m)
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.warning("Impossibile leggere history del canale: %s", e)

    raw.reverse()  # cronologico

    msgs: list[dict] = []

    def push(role: str, content: str):
        if not content.strip():
            return
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + content
        else:
            msgs.append({"role": role, "content": content})

    for m in raw:
        text = m.clean_content or ""
        if not text.strip():
            continue
        if m.author.id == bot_user.id:
            push("assistant", text)
        elif m.author.bot:
            # altri bot: li ignoriamo per non confondere il modello
            continue
        else:
            push("user", f"[{m.author.display_name}]: {text}")

    # Aggiungi messaggio corrente
    current_text = current_message.clean_content or ""
    push("user", f"[{current_message.author.display_name}]: {current_text}")

    # Anthropic richiede che il primo messaggio sia 'user'. Se per caso
    # iniziasse con assistant (raro ma possibile), prependiamo un placeholder.
    if msgs and msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": "[contesto precedente]"})

    return msgs


# -----------------------------------------------------------------------------
# Splitting per limite Discord
# -----------------------------------------------------------------------------
def split_for_discord(text: str, limit: int = MAX_RESPONSE_CHARS) -> list[str]:
    """Divide un testo in chunks <= limit, preservando per quanto possibile righe."""
    text = text.rstrip()
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    current = ""

    for line in text.split("\n"):
        addition = (("\n" if current else "") + line)
        if len(current) + len(addition) <= limit:
            current += addition
            continue

        # current pieno -> flush
        if current:
            chunks.append(current)
            current = ""

        # se la singola riga e' piu' lunga del limite, spezza brutalmente
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line

    if current:
        chunks.append(current)
    return chunks


# -----------------------------------------------------------------------------
# Chiamata Claude (via Claude Agent SDK -> usa il login claude.ai)
# -----------------------------------------------------------------------------
def _serialize_history(messages: list[dict]) -> str:
    """
    Trasforma la lista user/assistant in un singolo prompt testuale.
    L'ultimo messaggio (sempre user) e' isolato come "messaggio a cui rispondere".
    """
    if not messages:
        return ""
    history = messages[:-1]
    last = messages[-1]["content"]

    if not history:
        return last

    lines: list[str] = []
    for m in history:
        if m["role"] == "assistant":
            lines.append(f"Nova: {m['content']}")
        else:
            lines.append(m["content"])

    return (
        "## Storico chat recente\n"
        + "\n\n".join(lines)
        + "\n\n## Nuovo messaggio (rispondi solo a questo, in stile Nova)\n"
        + last
    )


async def call_claude(system: str, messages: list[dict], memory_server, read_server, can_use_tool) -> str:
    """Chiamata async via claude-agent-sdk con memory_server + read_server scoped + audit dei web tool."""
    prompt = _serialize_history(messages)

    options = ClaudeAgentOptions(
        system_prompt=system,
        model=CLAUDE_MODEL,
        mcp_servers={"nova_memory": memory_server, "nova_read": read_server},
        allowed_tools=ALL_TOOLS,
        max_turns=8,  # piu' margine: web tool + read tool + risposta finale = vari turn
        can_use_tool=can_use_tool,
        setting_sources=[],
        # NOTE: niente permission_mode="bypassPermissions" — quel mode salta il
        # callback can_use_tool, perdendo l'audit. allowed_tools + can_use_tool
        # bastano: tutto cio' che non e' nella lista viene rifiutato dal SDK.
    )

    output_parts: list[str] = []
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    output_parts.append(block.text)

    return "\n".join(output_parts).strip()


# -----------------------------------------------------------------------------
# Discord events
# -----------------------------------------------------------------------------
@client.event
async def on_ready():
    logger.info("Connessa come %s (id=%s)", client.user, client.user.id if client.user else "?")
    logger.info("In %d server:", len(client.guilds))
    for g in client.guilds:
        logger.info("  - %s (id=%s)", g.name, g.id)
    logger.info("Modello: %s", CLAUDE_MODEL)
    logger.info("Memoria base: %s", NOVA_MEMORY_DIR)
    logger.info("Memoria utente (read-only): %s", USER_MEMORY_DIR)

    # Sync slash commands per ogni guild (instantaneo, vs ~1h del global sync).
    # copy_global_to porta i comandi global come guild-scoped per propagazione istantanea.
    for g in client.guilds:
        try:
            tree.copy_global_to(guild=g)
            synced = await tree.sync(guild=g)
            logger.info("Slash commands sync su %s: %d comandi", g.name, len(synced))
        except discord.HTTPException as e:
            logger.error("Sync slash commands fallito su %s: %s", g.name, e)

    # Catch-up: leggi i canali tracciati, rispondi all'ULTIMO trigger perso.
    await replay_missed_messages()


async def handle_message(message: discord.Message) -> None:
    """Pipeline completa di risposta. Usata sia da on_message che dal replay al boot."""
    try:
        scope_type, scope_id, scope_dir = _resolve_scope(message)
        ensure_scope_skeleton(scope_dir, scope_type)

        async with message.channel.typing():
            shared_mem = load_shared_memory(NOVA_MEMORY_DIR)
            scope_mem = load_scope_memory(scope_dir)
            user_mem = load_user_memory(USER_MEMORY_DIR) if USER_MEMORY_DIR.exists() else ""

            bot_name = client.user.display_name if client.user else "Nova"
            system_prompt = build_system_prompt(
                shared_mem, scope_mem, user_mem, bot_display_name=bot_name
            )

            messages = await collect_history(
                message.channel, client.user, message, HISTORY_MESSAGES
            )

            requester = f"{message.author.display_name} (id={message.author.id})"
            memory_server = build_memory_server(scope_dir)
            read_server = build_read_server(client, message.guild, scope_dir, requester)
            audit_cb = _build_can_use_tool(scope_dir, requester)
            reply = await call_claude(system_prompt, messages, memory_server, read_server, audit_cb)

        if not reply:
            logger.warning("Claude ha restituito risposta vuota")
            return

        chunks = split_for_discord(reply, MAX_RESPONSE_CHARS)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await message.reply(chunk, mention_author=False)
            else:
                await message.channel.send(chunk)

        # Aggiorna checkpoint dopo risposta riuscita
        checkpoints.update(message.channel.id, message.created_at, scope_type, scope_id)

    except Exception:
        logger.exception("Errore mentre rispondevo a %s", message.author)
        try:
            await message.reply(
                "Si e' rotto qualcosa nel mio cervello. Riprova fra un attimo.",
                mention_author=False,
            )
        except discord.HTTPException:
            pass


@client.event
async def on_message(message: discord.Message):
    if client.user is None:
        return

    decision, reason = await should_respond(message)

    # Aggiorna checkpoint anche se non rispondiamo, ma solo se il canale e' gia'
    # tracciato — cosi' al replay non ri-leggiamo messaggi gia' visti.
    if checkpoints.is_tracked(message.channel.id) and message.author.id != client.user.id:
        scope_type, scope_id, _ = _resolve_scope(message)
        checkpoints.update(message.channel.id, message.created_at, scope_type, scope_id)

    if not decision:
        return

    if not cooldown_ok(message.channel.id):
        logger.debug("Cooldown attivo su channel %s", message.channel.id)
        return

    _last_response_at[message.channel.id] = time.time()

    logger.info(
        "Trigger=%s | %s in #%s: %s",
        reason,
        message.author.display_name,
        getattr(message.channel, "name", "DM"),
        (message.content or "")[:100],
    )

    await handle_message(message)


# -----------------------------------------------------------------------------
# Replay catch-up al boot
# -----------------------------------------------------------------------------
async def _resolve_channel_from_checkpoint(channel_id: int):
    """Risolve un channel a partire dal suo id, gestendo anche i DM."""
    channel = client.get_channel(channel_id)
    if channel is not None:
        return channel

    entry = checkpoints.get_entry(channel_id) or {}
    if entry.get("scope") == "dm":
        try:
            user = await client.fetch_user(int(entry["scope_id"]))
            return user.dm_channel or await user.create_dm()
        except discord.HTTPException as e:
            logger.warning("Impossibile risolvere DM channel %s: %s", channel_id, e)
            return None

    try:
        return await client.fetch_channel(channel_id)
    except discord.HTTPException as e:
        logger.warning("Impossibile risolvere channel %s: %s", channel_id, e)
        return None


async def _replay_channel(channel_id: int) -> None:
    last_seen = checkpoints.get(channel_id)
    if last_seen is None:
        return

    channel = await _resolve_channel_from_checkpoint(channel_id)
    if channel is None:
        return

    missed: list[discord.Message] = []
    try:
        async for m in channel.history(limit=200, after=last_seen, oldest_first=True):
            if client.user and m.author.id == client.user.id:
                continue
            missed.append(m)
    except discord.HTTPException as e:
        logger.warning("Errore history channel %s: %s", channel_id, e)
        return

    if not missed:
        return

    chan_name = getattr(channel, "name", None) or f"DM:{getattr(channel.recipient, 'display_name', '?')}"
    logger.info("Replay channel '%s' (id=%s): %d messaggi persi", chan_name, channel_id, len(missed))

    # Strategia B: rispondo solo all'ULTIMO messaggio qualificante.
    latest_qualifying: discord.Message | None = None
    for m in missed:
        decision, _ = await should_respond(m)
        if decision:
            latest_qualifying = m

    if latest_qualifying is not None:
        logger.info(
            "Replay: rispondo a %s del %s in '%s'",
            latest_qualifying.author.display_name,
            latest_qualifying.created_at,
            chan_name,
        )
        _last_response_at[channel_id] = time.time()
        await handle_message(latest_qualifying)
    else:
        logger.info("Replay '%s': nessun trigger nei %d messaggi", chan_name, len(missed))

    # Avanza checkpoint all'ultimo messaggio visto, qualificante o no
    last_msg = missed[-1]
    scope_type, scope_id, _ = _resolve_scope(last_msg)
    checkpoints.update(channel_id, last_msg.created_at, scope_type, scope_id)


async def replay_missed_messages() -> None:
    ids = checkpoints.channel_ids()
    if not ids:
        logger.info("Nessun checkpoint, niente replay")
        return

    logger.info("Replay catch-up su %d canali tracciati", len(ids))
    for cid in ids:
        try:
            await _replay_channel(cid)
        except Exception:
            logger.exception("Errore durante replay su channel %s", cid)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    try:
        client.run(DISCORD_TOKEN, log_handler=None)
    except discord.LoginFailure:
        sys.exit("Login Discord fallito: token errato in .env")


if __name__ == "__main__":
    main()
