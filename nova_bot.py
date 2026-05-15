"""
nova_bot.py — Discord bot that embodies Nova using Claude.

Triggers on a message when:
  - the bot is mentioned directly (@Nova)
  - the message is a reply to a bot message
  - the message is a DM to the bot
  - the word 'nova' appears in the text (whole word, case-insensitive)

Required setup:
  - Filled-in .env file (copy from .env.example)
  - On the Discord Developer Portal: Privileged Gateway Intents -> MESSAGE CONTENT INTENT enabled
  - Bot invited with 'Send Messages' + 'Read Message History' + 'View Channel' permissions
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
    bootstrap_memory_dir,
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
    sys.exit("Missing DISCORD_TOKEN in .env")
if not NOVA_MEMORY_DIR or str(NOVA_MEMORY_DIR) == ".":
    sys.exit("Missing NOVA_MEMORY_DIR in .env")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Trim noise: discord.py and claude-agent-sdk are very verbose at INFO/DEBUG.
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)
logger = logging.getLogger("nova")

# -----------------------------------------------------------------------------
# Memory setup + checkpoint storage
# -----------------------------------------------------------------------------
# The scope (server or DM) is resolved per message inside `on_message`.
# The root folder must exist; subfolders are created lazily.
_MEMORY_TEMPLATE_DIR = Path(__file__).parent / "memory.example"
if _MEMORY_TEMPLATE_DIR.exists():
    bootstrap_memory_dir(NOVA_MEMORY_DIR, _MEMORY_TEMPLATE_DIR)
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
    """Callback for auditing web tools. Logs then always returns 'allow'."""

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
    """Resolve the message's memory scope: ('server', guild_id, dir) or ('dm', user_id, dir)."""
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
intents.message_content = True   # requires MESSAGE CONTENT INTENT enabled on the portal
intents.messages = True
intents.guilds = True
# DMs work with default intents; no extra privileged intents needed.

client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)
register_slash_commands(tree, NOVA_MEMORY_DIR, scope_dir_for, ensure_scope_skeleton)

# Per-channel cooldown: {channel_id: last_timestamp}
_last_response_at: dict[int, float] = {}

# Regex for the 'nova' keyword as a whole word (case-insensitive)
NOVA_KEYWORD = re.compile(r"\bnova\b", re.IGNORECASE)


# -----------------------------------------------------------------------------
# Trigger logic
# -----------------------------------------------------------------------------
async def should_respond(message: discord.Message) -> tuple[bool, str]:
    """
    Decide whether Nova should reply to a message.

    Returns:
        (decision, reason) — the reason is only used for logging.
    """
    if message.author.bot:
        return False, "author is a bot"
    if not message.content and not message.attachments:
        return False, "empty message"
    if client.user is None:
        return False, "client.user not ready yet"

    # 1) Direct DM to the bot
    if isinstance(message.channel, discord.DMChannel):
        return True, "DM"

    # 2) Direct mention
    if client.user in message.mentions:
        return True, "mention"

    # 3) Reply to a Nova message
    if message.reference and message.reference.resolved:
        ref = message.reference.resolved
        if isinstance(ref, discord.Message) and ref.author.id == client.user.id:
            return True, "reply"

    # 3b) Reply but the referenced msg wasn't resolved -> fetch it
    if message.reference and not message.reference.resolved and message.reference.message_id:
        try:
            ref = await message.channel.fetch_message(message.reference.message_id)
            if ref.author.id == client.user.id:
                return True, "reply (fetched)"
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # 4) Keyword 'nova' in the text
    if NOVA_KEYWORD.search(message.content):
        return True, "keyword"

    return False, "no trigger"


def cooldown_ok(channel_id: int) -> bool:
    """Return True if the channel is past its cooldown."""
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
    Read the last `limit` messages of the channel (skipping textless ones and
    other bots' commands), convert them to the Anthropic Messages API format,
    merging consecutive user/assistant turns.

    The last item in the list is ALWAYS the current message as a user turn.
    """
    raw: list[discord.Message] = []
    try:
        async for m in channel.history(limit=limit, before=current_message):
            raw.append(m)
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.warning("Could not read channel history: %s", e)

    raw.reverse()  # chronological order

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
            # other bots: ignored so we don't confuse the model
            continue
        else:
            push("user", f"[{m.author.display_name}]: {text}")

    # Add current message
    current_text = current_message.clean_content or ""
    push("user", f"[{current_message.author.display_name}]: {current_text}")

    # Anthropic requires the first message to be 'user'. If it happens to
    # start with assistant (rare but possible), prepend a placeholder.
    if msgs and msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": "[contesto precedente]"})

    return msgs


# -----------------------------------------------------------------------------
# Splitting for Discord limit
# -----------------------------------------------------------------------------
def split_for_discord(text: str, limit: int = MAX_RESPONSE_CHARS) -> list[str]:
    """Split text into chunks <= limit, preserving line breaks when possible."""
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

        # current is full -> flush
        if current:
            chunks.append(current)
            current = ""

        # if a single line exceeds the limit, split brute-force
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line

    if current:
        chunks.append(current)
    return chunks


# -----------------------------------------------------------------------------
# Claude call (via Claude Agent SDK -> uses the claude.ai login)
# -----------------------------------------------------------------------------
def _serialize_history(messages: list[dict]) -> str:
    """
    Convert the user/assistant list into a single textual prompt.
    The last message (always a user one) is isolated as the "message to reply to".
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
    """Async call via claude-agent-sdk with scoped memory_server + read_server + web tool auditing."""
    prompt = _serialize_history(messages)

    options = ClaudeAgentOptions(
        system_prompt=system,
        model=CLAUDE_MODEL,
        mcp_servers={"nova_memory": memory_server, "nova_read": read_server},
        allowed_tools=ALL_TOOLS,
        max_turns=8,  # more headroom: web tool + read tool + final answer = several turns
        can_use_tool=can_use_tool,
        setting_sources=[],
        # NOTE: no permission_mode="bypassPermissions" — that mode skips the
        # can_use_tool callback, losing the audit. allowed_tools + can_use_tool
        # are enough: anything not in the list is rejected by the SDK.
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
    logger.info("Connected as %s (id=%s)", client.user, client.user.id if client.user else "?")
    logger.info("In %d servers:", len(client.guilds))
    for g in client.guilds:
        logger.info("  - %s (id=%s)", g.name, g.id)
    logger.info("Model: %s", CLAUDE_MODEL)
    logger.info("Base memory: %s", NOVA_MEMORY_DIR)
    logger.info("User memory (read-only): %s", USER_MEMORY_DIR)

    # Sync slash commands per guild (instantaneous, vs ~1h for global sync).
    # copy_global_to brings global commands as guild-scoped for instant propagation.
    for g in client.guilds:
        try:
            tree.copy_global_to(guild=g)
            synced = await tree.sync(guild=g)
            logger.info("Slash commands synced on %s: %d commands", g.name, len(synced))
        except discord.HTTPException as e:
            logger.error("Slash command sync failed on %s: %s", g.name, e)

    # Catch-up: read tracked channels, reply to the LAST missed trigger.
    await replay_missed_messages()


async def handle_message(message: discord.Message) -> None:
    """Full reply pipeline. Used both by on_message and by the boot replay."""
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
            logger.warning("Claude returned an empty response")
            return

        chunks = split_for_discord(reply, MAX_RESPONSE_CHARS)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await message.reply(chunk, mention_author=False)
            else:
                await message.channel.send(chunk)

        # Update checkpoint after a successful reply
        checkpoints.update(message.channel.id, message.created_at, scope_type, scope_id)

    except Exception:
        logger.exception("Error while replying to %s", message.author)
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

    # Update the checkpoint even if we don't reply, but only if the channel is
    # already tracked — so the replay doesn't re-read messages already seen.
    if checkpoints.is_tracked(message.channel.id) and message.author.id != client.user.id:
        scope_type, scope_id, _ = _resolve_scope(message)
        checkpoints.update(message.channel.id, message.created_at, scope_type, scope_id)

    if not decision:
        return

    if not cooldown_ok(message.channel.id):
        logger.debug("Cooldown active on channel %s", message.channel.id)
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
# Boot replay catch-up
# -----------------------------------------------------------------------------
async def _resolve_channel_from_checkpoint(channel_id: int):
    """Resolve a channel from its id, handling DMs too."""
    channel = client.get_channel(channel_id)
    if channel is not None:
        return channel

    entry = checkpoints.get_entry(channel_id) or {}
    if entry.get("scope") == "dm":
        try:
            user = await client.fetch_user(int(entry["scope_id"]))
            return user.dm_channel or await user.create_dm()
        except discord.HTTPException as e:
            logger.warning("Could not resolve DM channel %s: %s", channel_id, e)
            return None

    try:
        return await client.fetch_channel(channel_id)
    except discord.HTTPException as e:
        logger.warning("Could not resolve channel %s: %s", channel_id, e)
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
        logger.warning("History error on channel %s: %s", channel_id, e)
        return

    if not missed:
        return

    chan_name = getattr(channel, "name", None) or f"DM:{getattr(channel.recipient, 'display_name', '?')}"
    logger.info("Replay channel '%s' (id=%s): %d missed messages", chan_name, channel_id, len(missed))

    # Strategy B: only reply to the LAST qualifying message.
    latest_qualifying: discord.Message | None = None
    for m in missed:
        decision, _ = await should_respond(m)
        if decision:
            latest_qualifying = m

    if latest_qualifying is not None:
        logger.info(
            "Replay: replying to %s from %s in '%s'",
            latest_qualifying.author.display_name,
            latest_qualifying.created_at,
            chan_name,
        )
        _last_response_at[channel_id] = time.time()
        await handle_message(latest_qualifying)
    else:
        logger.info("Replay '%s': no trigger in %d messages", chan_name, len(missed))

    # Advance the checkpoint to the last seen message, qualifying or not
    last_msg = missed[-1]
    scope_type, scope_id, _ = _resolve_scope(last_msg)
    checkpoints.update(channel_id, last_msg.created_at, scope_type, scope_id)


async def replay_missed_messages() -> None:
    ids = checkpoints.channel_ids()
    if not ids:
        logger.info("No checkpoints, skipping replay")
        return

    logger.info("Replay catch-up on %d tracked channels", len(ids))
    for cid in ids:
        try:
            await _replay_channel(cid)
        except Exception:
            logger.exception("Error during replay on channel %s", cid)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    try:
        client.run(DISCORD_TOKEN, log_handler=None)
    except discord.LoginFailure:
        sys.exit("Discord login failed: invalid token in .env")


if __name__ == "__main__":
    main()
