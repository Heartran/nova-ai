"""
memory.py — Lettura/scrittura della memoria di Nova.

Layout (sotto NOVA_MEMORY_DIR):
  server/<guild_id>/{lore,characters,conversations,INDEX}.md
  dm/<user_id>/{conversations,INDEX}.md

Due fonti combinate ad ogni messaggio:
  1) memoria di scope (per server o per DM): read + write
  2) USER_MEMORY_DIR: auto-memory utente di Claude (.md). Read-only.

I file vengono letti ad ogni richiesta (no cache) cosi' Nova vede sempre
l'ultima versione. Se la memoria un giorno diventasse enorme, qui si mette
una cache TTL.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Hard limit per evitare di saturare il context se la memoria cresce a dismisura.
# 80 KB per sezione e' gia' un quintale di testo.
MAX_SECTION_BYTES = 80_000


def _read_md_files(directory: Path, label: str) -> str:
    """
    Legge tutti i .md della cartella (non ricorsivo) e li concatena con header.

    Args:
        directory: Path della cartella.
        label: etichetta usata nei log.

    Returns:
        stringa concatenata, o "" se la cartella non esiste o e' vuota.
    """
    if not directory.exists():
        logger.warning("[%s] cartella non trovata: %s", label, directory)
        return ""
    if not directory.is_dir():
        logger.warning("[%s] non e' una cartella: %s", label, directory)
        return ""

    chunks: list[str] = []
    total = 0

    # Ordine alfabetico stabile, INDEX/MEMORY in cima se ci sono.
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
            logger.error("[%s] errore lettura %s: %s", label, path.name, e)
            continue

        header = f"### File: {path.name}\n"
        chunk = header + text.rstrip() + "\n"

        if total + len(chunk.encode("utf-8")) > MAX_SECTION_BYTES:
            logger.warning(
                "[%s] hard limit raggiunto a %s, file successivi ignorati",
                label,
                path.name,
            )
            chunks.append(f"### (altri file presenti, troncati per limite di {MAX_SECTION_BYTES} byte)")
            break

        chunks.append(chunk)
        total += len(chunk.encode("utf-8"))

    if not chunks:
        logger.info("[%s] nessun .md trovato in %s", label, directory)
        return ""

    logger.info("[%s] letti %d file da %s (%d byte)", label, len(chunks), directory, total)
    return "\n".join(chunks)


def scope_dir_for(base: Path, scope_type: str, scope_id: int | str) -> Path:
    """
    Ritorna la cartella memoria per uno scope (server, DM o whatsapp).

    Args:
        base: NOVA_MEMORY_DIR
        scope_type: "server", "dm" o "whatsapp"
        scope_id: guild_id (server), user_id (dm), chat JID (whatsapp)
    """
    if scope_type not in ("server", "dm", "whatsapp"):
        raise ValueError(f"scope_type deve essere 'server', 'dm' o 'whatsapp', non {scope_type!r}")
    return base / scope_type / str(scope_id)


def load_scope_memory(scope_dir: Path) -> str:
    """Memoria di uno scope (server o DM): lore, characters, conversations, ecc."""
    label = "/".join(scope_dir.parts[-2:]) if len(scope_dir.parts) >= 2 else scope_dir.name
    return _read_md_files(scope_dir, f"SCOPE {label}")


def load_user_memory(user_memory_dir: Path) -> str:
    """Auto-memory di Claude su Fede (chi e', preferenze, contesto)."""
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
        "- `conversations.md` — note salienti emerse nella chat\n"
    ),
    "conversations.md": "# Note dalla chat WhatsApp\n\n_Spazio dove annotare cose dette in chat che vale la pena ricordare._\n",
}


def ensure_scope_skeleton(scope_dir: Path, scope_type: str = "server") -> None:
    """
    Crea la cartella di scope con i file template se non esiste.
    scope_type: "server", "dm" o "whatsapp" (template diversi).
    """
    if scope_dir.exists():
        return

    try:
        scope_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("Impossibile creare %s: %s", scope_dir, e)
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
            logger.info("Creato template %s", path)
        except OSError as e:
            logger.error("Errore scrittura %s: %s", path, e)


def append_conversation_note(nova_memory_dir: Path, note: str, author: str = "system") -> bool:
    """
    Aggiunge una nota a conversations.md. Usata solo quando Fede lo chiede
    esplicitamente al bot ("ricordati che...", "salva nota...", ecc.).

    Args:
        nova_memory_dir: cartella della memoria FNAC.
        note: testo della nota.
        author: chi l'ha richiesta (display name Discord).

    Returns:
        True se ha scritto, False altrimenti.
    """
    target = nova_memory_dir / "conversations.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n- **[{timestamp}]** ({author}) {note.strip()}\n"

    try:
        # Crea il file se manca
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# Note dalle conversazioni\n", encoding="utf-8")

        with target.open("a", encoding="utf-8") as f:
            f.write(entry)
        logger.info("Nota aggiunta a %s", target)
        return True
    except OSError as e:
        logger.error("Errore append in %s: %s", target, e)
        return False
