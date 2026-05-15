"""
memory.py — Read/write Nova's memory.

Layout (under NOVA_MEMORY_DIR):
  _shared/*.md                       <- ALWAYS read (global lore, rules)
  server/<guild_id>/{lore,characters,conversations,INDEX}.md
  dm/<user_id>/{conversations,INDEX}.md
  whatsapp/<jid>/{conversations,INDEX}.md

Three sources combined on every message:
  1) _shared/: global memory (lore, members, behavioral rules). Read + write.
     Read on EVERY reply, regardless of scope (Discord/DM/WhatsApp).
  2) scope memory (server, DM or WhatsApp): chat-specific notes. Read + write.
  3) USER_MEMORY_DIR: Claude's user auto-memory (.md). Read-only.

Files are read on every request (no cache) so Nova always sees the latest
version. If memory grows huge one day, a TTL cache could go here.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Hard limit to avoid saturating the context if memory grows unbounded.
# 80 KB per section is already a ton of text.
MAX_SECTION_BYTES = 80_000

# Name of the shared folder under NOVA_MEMORY_DIR.
SHARED_DIRNAME = "_shared"


def _read_md_files(directory: Path, label: str) -> str:
    """
    Read all .md files in the folder (non-recursive) and concatenate with header.

    Args:
        directory: folder Path.
        label: label used in logs.

    Returns:
        concatenated string, or "" if the folder is missing or empty.
    """
    if not directory.exists():
        logger.warning("[%s] folder not found: %s", label, directory)
        return ""
    if not directory.is_dir():
        logger.warning("[%s] not a folder: %s", label, directory)
        return ""

    chunks: list[str] = []
    total = 0

    # Stable alphabetical order, INDEX/MEMORY on top if present.
    files = sorted(
        directory.glob("*.md"),
        key=lambda p: (
            0 if p.name.lower() in ("index.md", "memory.md") else 1,
            p.name.lower(),
        ),
    )

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.error("[%s] read error %s: %s", label, path.name, e)
            continue

        header = f"### File: {path.name}\n"
        chunk = header + text.rstrip() + "\n"

        if total + len(chunk.encode("utf-8")) > MAX_SECTION_BYTES:
            logger.warning(
                "[%s] hard limit reached at %s, remaining files ignored",
                label,
                path.name,
            )
            chunks.append(f"### (more files present, truncated due to {MAX_SECTION_BYTES} byte limit)")
            break

        chunks.append(chunk)
        total += len(chunk.encode("utf-8"))

    if not chunks:
        logger.info("[%s] no .md found in %s", label, directory)
        return ""

    logger.info("[%s] read %d files from %s (%d bytes)", label, len(chunks), directory, total)
    return "\n".join(chunks)


def scope_dir_for(base: Path, scope_type: str, scope_id: int | str) -> Path:
    """
    Return the memory folder for a scope (server, DM or whatsapp).

    Args:
        base: NOVA_MEMORY_DIR
        scope_type: "server", "dm" or "whatsapp"
        scope_id: guild_id (server), user_id (dm), chat JID (whatsapp)
    """
    if scope_type not in ("server", "dm", "whatsapp"):
        raise ValueError(f"scope_type must be 'server', 'dm' or 'whatsapp', not {scope_type!r}")
    return base / scope_type / str(scope_id)


def shared_dir(base: Path) -> Path:
    """Return the shared memory folder (NOVA_MEMORY_DIR/_shared)."""
    return base / SHARED_DIRNAME


def load_scope_memory(scope_dir: Path) -> str:
    """Memory for a scope (server, DM or WhatsApp): chat-specific notes."""
    label = "/".join(scope_dir.parts[-2:]) if len(scope_dir.parts) >= 2 else scope_dir.name
    return _read_md_files(scope_dir, f"SCOPE {label}")


def load_shared_memory(base: Path) -> str:
    """
    Shared memory: all .md files under NOVA_MEMORY_DIR/_shared/.
    Read on EVERY reply, regardless of scope.
    Project lore, recurring members and global rules live here.
    """
    return _read_md_files(shared_dir(base), "SHARED")


def load_user_memory(user_memory_dir: Path) -> str:
    """Claude's auto-memory about the user (identity, preferences, context)."""
    return _read_md_files(user_memory_dir, "USER")


_SERVER_TEMPLATES = {
    "INDEX.md": (
        "# Memoria di Nova — server\n\n"
        "File in questa cartella:\n"
        "- `lore.md` — ambientazione, storia, regole del mondo\n"
        "- `characters.md` — personaggi e persone reali coinvolte\n"
        "- `conversations.md` — note salienti emerse nelle chat Discord\n"
    ),
    "lore.md": "# Lore\n\n_Aggiungi qui la storia, l'ambientazione, gli eventi rilevanti per questo server._\n",
    "characters.md": "# Personaggi & persone\n\n_Aggiungi qui chi e' chi: protagonisti, NPC, persone reali ricorrenti._\n",
    "conversations.md": "# Note dalle conversazioni\n\n_Spazio dove annotare cose dette in chat che vale la pena ricordare._\n",
}

_DM_TEMPLATES = {
    "INDEX.md": (
        "# Memoria di Nova — DM\n\n"
        "Cartella personale per la chat in privato con questo utente.\n"
        "- `conversations.md` — note salienti dei DM\n"
    ),
    "conversations.md": "# Note dei DM\n\n_Spazio dove annotare cose dette in DM che vale la pena ricordare._\n",
}

_WHATSAPP_TEMPLATES = {
    "INDEX.md": (
        "# Memoria di Nova — WhatsApp\n\n"
        "Cartella per questa chat WhatsApp.\n"
        "- `conversations.md` — note salienti emerse nella chat\n\n"
        "Il lore globale del progetto vive in `../../_shared/`, non qui.\n"
    ),
    "conversations.md": "# Note dalla chat WhatsApp\n\n_Spazio dove annotare cose dette in chat che vale la pena ricordare._\n",
}

_SHARED_TEMPLATES = {
    "INDEX.md": (
        "# Memoria condivisa di Nova\n\n"
        "Tutti i `.md` in questa cartella vengono letti a OGNI risposta,\n"
        "sia su Discord che su WhatsApp che in DM, in aggiunta alla memoria\n"
        "specifica della chat (`server/`, `dm/`, `whatsapp/`).\n\n"
        "Cosa mettere qui:\n"
        "- lore globale del progetto (es. `fnac_lore.md`)\n"
        "- chi e' chi nella cerchia, pattern ricorrenti delle persone\n"
        "- regole comportamentali valide ovunque\n\n"
        "Cosa NON mettere qui:\n"
        "- note specifiche di una singola chat (quelle nello scope)\n"
        "- segreti, token, credenziali\n"
    ),
}


def bootstrap_memory_dir(base: Path, template_dir: Path) -> bool:
    """
    Bootstrap the memory folder from a committed template on first run.

    If `base` already exists, do nothing and return False. Otherwise, copy the
    entire `template_dir` tree to `base` so the user starts with a working
    skeleton (shared INDEX, group placeholder, empty scope folders).

    Args:
        base: target NOVA_MEMORY_DIR.
        template_dir: source template (e.g. <repo>/memory.example).

    Returns:
        True if the bootstrap copy happened, False otherwise.
    """
    if base.exists():
        return False
    if not template_dir.exists() or not template_dir.is_dir():
        logger.warning("Memory template not found at %s, skipping bootstrap", template_dir)
        return False

    try:
        shutil.copytree(template_dir, base)
    except OSError as e:
        logger.error("Failed to bootstrap memory dir %s from %s: %s", base, template_dir, e)
        return False

    logger.info("Memory initialized from template %s -> %s", template_dir, base)
    return True


def ensure_scope_skeleton(scope_dir: Path, scope_type: str = "server") -> None:
    """
    Create the scope folder with template files if missing.
    scope_type: "server", "dm" or "whatsapp" (different templates).
    """
    if scope_dir.exists():
        return

    try:
        scope_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("Could not create %s: %s", scope_dir, e)
        return

    if scope_type == "server":
        templates = _SERVER_TEMPLATES
    elif scope_type == "whatsapp":
        templates = _WHATSAPP_TEMPLATES
    else:
        templates = _DM_TEMPLATES
    for name, content in templates.items():
        path = scope_dir / name
        try:
            path.write_text(content, encoding="utf-8")
            logger.info("Created template %s", path)
        except OSError as e:
            logger.error("Write error %s: %s", path, e)


def ensure_shared_skeleton(base: Path) -> None:
    """
    Create NOVA_MEMORY_DIR/_shared/ with template files if missing.
    Idempotent: if the folder is already there, nothing happens.
    """
    sd = shared_dir(base)
    if sd.exists():
        return

    try:
        sd.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("Could not create %s: %s", sd, e)
        return

    for name, content in _SHARED_TEMPLATES.items():
        path = sd / name
        try:
            path.write_text(content, encoding="utf-8")
            logger.info("Created shared template %s", path)
        except OSError as e:
            logger.error("Write error %s: %s", path, e)


def append_conversation_note(nova_memory_dir: Path, note: str, author: str = "system") -> bool:
    """
    Append a note to conversations.md. Used only when the user explicitly asks
    the bot ("remember that...", "save note...", etc.).

    Args:
        nova_memory_dir: memory folder.
        note: note text.
        author: who requested it (Discord display name).

    Returns:
        True if written, False otherwise.
    """
    target = nova_memory_dir / "conversations.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n- **[{timestamp}]** ({author}) {note.strip()}\n"

    try:
        # Create the file if missing
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# Note dalle conversazioni\n", encoding="utf-8")

        with target.open("a", encoding="utf-8") as f:
            f.write(entry)
        logger.info("Note appended to %s", target)
        return True
    except OSError as e:
        logger.error("Append error in %s: %s", target, e)
        return False
