"""
slash_commands.py — Discord slash commands per gestire la memoria di Nova.

Comandi (tutti scoped al server corrente, o al DM dell'utente in privato):
- /ricorda nota:                 quick note in conversations.md (admin-only nei server)
- /memoria lista                 lista i .md presenti nello scope (chiunque)
- /memoria mostra file:          mostra il contenuto di un .md (chiunque, ephemeral)
- /memoria aggiungi file: contenuto:   appende contenuto a un .md (admin)
- /dimentica indice:             rimuove l'n-esima nota da conversations.md (admin)

Permessi: nei server i comandi di scrittura richiedono `manage_guild`. In DM
i comandi sono sempre disponibili (utente parla con la sua memoria personale).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

import discord
from discord import app_commands

from nova_read import load_read_blacklist, save_read_blacklist

logger = logging.getLogger(__name__)

# Limite Discord per il content di un'interazione
DISCORD_MSG_LIMIT = 1900


def _scope_for_interaction(
    interaction: discord.Interaction,
    base: Path,
    scope_dir_for: Callable[[Path, str, int | str], Path],
    ensure_skeleton: Callable[[Path, str], None],
) -> tuple[str, int, Path]:
    if interaction.guild_id is not None:
        scope_type = "server"
        scope_id = interaction.guild_id
    else:
        scope_type = "dm"
        scope_id = interaction.user.id
    scope_dir = scope_dir_for(base, scope_type, scope_id)
    ensure_skeleton(scope_dir, scope_type)
    return scope_type, scope_id, scope_dir


def _safe_md_target(scope_dir: Path, filename: str) -> Path | None:
    """Valida il filename e restituisce il path solo se rimane dentro scope_dir."""
    if not filename or not filename.endswith(".md"):
        return None
    if "/" in filename or "\\" in filename or ".." in filename:
        return None
    target = (scope_dir / filename).resolve()
    try:
        target.relative_to(scope_dir.resolve())
    except ValueError:
        return None
    return target


def register_slash_commands(
    tree: app_commands.CommandTree,
    base: Path,
    scope_dir_for: Callable[[Path, str, int | str], Path],
    ensure_skeleton: Callable[[Path, str], None],
) -> None:
    """Registra tutti gli slash command sul tree fornito."""

    # /ricorda nota:
    @tree.command(name="ricorda", description="Salva una nota nella memoria di questo server/DM")
    @app_commands.describe(nota="Cosa devo ricordare")
    @app_commands.default_permissions(manage_guild=True)
    async def ricorda(interaction: discord.Interaction, nota: str):
        _, _, scope_dir = _scope_for_interaction(interaction, base, scope_dir_for, ensure_skeleton)
        nota = nota.strip()
        if not nota:
            await interaction.response.send_message("Nota vuota, niente da salvare.", ephemeral=True)
            return

        target = scope_dir / "conversations.md"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        author = interaction.user.display_name
        entry = f"\n- **[{timestamp}]** ({author}) {nota}\n"
        try:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# Note dalle conversazioni\n", encoding="utf-8")
            with target.open("a", encoding="utf-8") as f:
                f.write(entry)
            logger.info("[slash:ricorda] +nota su %s da %s", target, author)
            await interaction.response.send_message(f"Salvato: {nota[:120]}", ephemeral=True)
        except OSError as e:
            logger.error("[slash:ricorda] errore: %s", e)
            await interaction.response.send_message(f"Errore: {e}", ephemeral=True)

    # /memoria <subcommand>
    memoria = app_commands.Group(name="memoria", description="Gestione memoria di Nova")

    @memoria.command(name="lista", description="Lista i file di memoria di questo scope")
    async def memoria_lista(interaction: discord.Interaction):
        _, _, scope_dir = _scope_for_interaction(interaction, base, scope_dir_for, ensure_skeleton)
        if not scope_dir.exists():
            await interaction.response.send_message("Nessun file di memoria.", ephemeral=True)
            return
        files = sorted(p.name for p in scope_dir.glob("*.md"))
        if not files:
            await interaction.response.send_message("Nessun file di memoria.", ephemeral=True)
            return
        sizes = [(p.name, p.stat().st_size) for p in scope_dir.glob("*.md") if p.name in files]
        lines = [f"- `{name}` ({size} byte)" for name, size in sorted(sizes)]
        body = "**File di memoria di questo scope:**\n" + "\n".join(lines)
        await interaction.response.send_message(body[:DISCORD_MSG_LIMIT], ephemeral=True)

    @memoria.command(name="mostra", description="Mostra il contenuto di un file di memoria")
    @app_commands.describe(file="Nome del file (es. lore.md)")
    async def memoria_mostra(interaction: discord.Interaction, file: str):
        _, _, scope_dir = _scope_for_interaction(interaction, base, scope_dir_for, ensure_skeleton)
        target = _safe_md_target(scope_dir, file.strip())
        if target is None:
            await interaction.response.send_message(f"`{file}` non valido (deve essere un .md).", ephemeral=True)
            return
        if not target.exists():
            await interaction.response.send_message(f"`{file}` non esiste.", ephemeral=True)
            return
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as e:
            await interaction.response.send_message(f"Errore lettura: {e}", ephemeral=True)
            return
        body = f"**`{target.name}`**\n```md\n{text}\n```"
        if len(body) > DISCORD_MSG_LIMIT:
            body = body[:DISCORD_MSG_LIMIT - 20] + "\n... (troncato)\n```"
        await interaction.response.send_message(body, ephemeral=True)

    @memoria.command(name="aggiungi", description="Appende contenuto a un file di memoria")
    @app_commands.describe(file="Nome del file (es. lore.md)", contenuto="Cosa aggiungere")
    @app_commands.default_permissions(manage_guild=True)
    async def memoria_aggiungi(interaction: discord.Interaction, file: str, contenuto: str):
        _, _, scope_dir = _scope_for_interaction(interaction, base, scope_dir_for, ensure_skeleton)
        target = _safe_md_target(scope_dir, file.strip())
        if target is None:
            await interaction.response.send_message(f"`{file}` non valido (deve essere un .md).", ephemeral=True)
            return
        contenuto = contenuto.strip()
        if not contenuto:
            await interaction.response.send_message("Contenuto vuoto.", ephemeral=True)
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as f:
                f.write("\n\n" + contenuto + "\n")
            logger.info("[slash:memoria_aggiungi] +%d byte su %s", len(contenuto), target)
            preview = contenuto if len(contenuto) <= 120 else contenuto[:117] + "..."
            await interaction.response.send_message(f"Aggiunto a `{target.name}`: {preview}", ephemeral=True)
        except OSError as e:
            await interaction.response.send_message(f"Errore: {e}", ephemeral=True)

    tree.add_command(memoria)

    # /lettura_blacklist <subcommand> — gestione canali in blacklist (solo nei server)
    blacklist_group = app_commands.Group(
        name="lettura_blacklist",
        description="Gestisce i canali che Nova non puo' leggere (admin)",
    )

    @blacklist_group.command(name="aggiungi", description="Aggiunge un canale alla blacklist di lettura")
    @app_commands.describe(canale="Il canale da bloccare")
    @app_commands.default_permissions(manage_guild=True)
    async def blacklist_aggiungi(interaction: discord.Interaction, canale: discord.TextChannel):
        if interaction.guild_id is None:
            await interaction.response.send_message("Solo nei server.", ephemeral=True)
            return
        scope_dir = scope_dir_for(base, "server", interaction.guild_id)
        ensure_skeleton(scope_dir, "server")

        bl = load_read_blacklist(scope_dir)
        if canale.id in bl:
            await interaction.response.send_message(
                f"#{canale.name} e' gia' in blacklist.", ephemeral=True
            )
            return
        bl.add(canale.id)
        save_read_blacklist(scope_dir, bl)
        logger.info("[slash:blacklist_aggiungi] %s -> #%s (id=%s)", interaction.user, canale.name, canale.id)
        await interaction.response.send_message(
            f"Aggiunto #{canale.name} alla blacklist. Nova non lo leggera' piu'.", ephemeral=True
        )

    @blacklist_group.command(name="rimuovi", description="Rimuove un canale dalla blacklist di lettura")
    @app_commands.describe(canale="Il canale da sbloccare")
    @app_commands.default_permissions(manage_guild=True)
    async def blacklist_rimuovi(interaction: discord.Interaction, canale: discord.TextChannel):
        if interaction.guild_id is None:
            await interaction.response.send_message("Solo nei server.", ephemeral=True)
            return
        scope_dir = scope_dir_for(base, "server", interaction.guild_id)
        ensure_skeleton(scope_dir, "server")

        bl = load_read_blacklist(scope_dir)
        if canale.id not in bl:
            await interaction.response.send_message(
                f"#{canale.name} non e' in blacklist.", ephemeral=True
            )
            return
        bl.discard(canale.id)
        save_read_blacklist(scope_dir, bl)
        logger.info("[slash:blacklist_rimuovi] %s -> #%s", interaction.user, canale.name)
        await interaction.response.send_message(
            f"Rimosso #{canale.name} dalla blacklist.", ephemeral=True
        )

    @blacklist_group.command(name="lista", description="Lista i canali in blacklist di lettura")
    async def blacklist_lista(interaction: discord.Interaction):
        if interaction.guild_id is None:
            await interaction.response.send_message("Solo nei server.", ephemeral=True)
            return
        scope_dir = scope_dir_for(base, "server", interaction.guild_id)
        ensure_skeleton(scope_dir, "server")

        bl = load_read_blacklist(scope_dir)
        if not bl:
            await interaction.response.send_message("Blacklist vuota.", ephemeral=True)
            return
        guild = interaction.guild
        lines = []
        for cid in sorted(bl):
            ch = guild.get_channel(cid) if guild else None
            label = f"#{ch.name}" if ch else f"(canale eliminato/inaccessibile)"
            lines.append(f"- {label} (id={cid})")
        body = f"**Blacklist ({len(bl)}):**\n" + "\n".join(lines)
        await interaction.response.send_message(body[:DISCORD_MSG_LIMIT], ephemeral=True)

    tree.add_command(blacklist_group)

    # /dimentica indice:
    @tree.command(name="dimentica", description="Rimuove l'n-esima nota da conversations.md")
    @app_commands.describe(indice="Indice della nota (1 = prima nota; usa /memoria mostra file:conversations.md per vederle)")
    @app_commands.default_permissions(manage_guild=True)
    async def dimentica(interaction: discord.Interaction, indice: int):
        _, _, scope_dir = _scope_for_interaction(interaction, base, scope_dir_for, ensure_skeleton)
        target = scope_dir / "conversations.md"
        if not target.exists():
            await interaction.response.send_message("Nessun conversations.md.", ephemeral=True)
            return
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as e:
            await interaction.response.send_message(f"Errore lettura: {e}", ephemeral=True)
            return

        lines = text.splitlines(keepends=True)
        # Trovo le righe-nota: iniziano con "- **["
        note_indices = [i for i, line in enumerate(lines) if line.lstrip().startswith("- **[")]
        if indice < 1 or indice > len(note_indices):
            await interaction.response.send_message(
                f"Indice fuori range. Note presenti: {len(note_indices)}.", ephemeral=True
            )
            return
        line_idx = note_indices[indice - 1]
        removed = lines.pop(line_idx).strip()
        try:
            target.write_text("".join(lines), encoding="utf-8")
            logger.info("[slash:dimentica] rimossa nota %d da %s: %s", indice, target, removed[:80])
            preview = removed if len(removed) <= 120 else removed[:117] + "..."
            await interaction.response.send_message(f"Dimenticato: {preview}", ephemeral=True)
        except OSError as e:
            await interaction.response.send_message(f"Errore scrittura: {e}", ephemeral=True)
