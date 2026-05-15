"""
nova_mcp.py — In-process MCP server for Nova's memory management.

Tools exposed to Nova:
- note_remember(note, author): append a dated note to conversations.md
- memory_append(file, content): append to a .md file in the memory dir

All writes are confined to NOVA_MEMORY_DIR. Filename validation against path
traversal: must be a basename .md only, no '/', '\\' or '..'.
"""

from __future__ import annotations

import logging
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server, tool

from memory import append_conversation_note

logger = logging.getLogger(__name__)


def build_memory_server(nova_memory_dir: Path):
    """
    Build an in-process MCP server with memory management tools.
    nova_memory_dir is closed over in the closure: tools cannot escape the dir.
    """
    base = nova_memory_dir.resolve()

    def _safe_target(filename: str) -> Path | None:
        if not filename or not filename.endswith(".md"):
            return None
        if "/" in filename or "\\" in filename or ".." in filename:
            return None
        target = (base / filename).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return None
        return target

    @tool(
        "note_remember",
        (
            "Appunta una nota datata in conversations.md. Usalo quando in chat emerge "
            "un dettaglio che vale la pena ricordare per il futuro: un fatto sul progetto, "
            "una preferenza di una persona, un evento, una decisione. Non salvare banalita'."
        ),
        {"note": str, "author": str},
    )
    async def note_remember(args):
        note = (args.get("note") or "").strip()
        author = (args.get("author") or "ignoto").strip() or "ignoto"
        if not note:
            return {"content": [{"type": "text", "text": "Errore: la nota e' vuota."}]}

        ok = append_conversation_note(base, note, author)
        if not ok:
            return {"content": [{"type": "text", "text": "Errore: non sono riuscita a scrivere su conversations.md."}]}

        preview = note if len(note) <= 80 else note[:77] + "..."
        return {
            "content": [
                {"type": "text", "text": f"Salvato in conversations.md (autore: {author}): {preview}"}
            ]
        }

    @tool(
        "memory_append",
        (
            "Appende contenuto a un file .md della memoria (es. lore.md, characters.md, "
            "o un nuovo file). Usalo per informazioni piu' strutturate, non per note volanti. "
            "Il filename deve essere solo il nome (es. 'lore.md'), niente percorsi. "
            "Crea il file se non esiste."
        ),
        {"file": str, "content": str},
    )
    async def memory_append(args):
        filename = (args.get("file") or "").strip()
        content = (args.get("content") or "").strip()
        if not content:
            return {"content": [{"type": "text", "text": "Errore: il contenuto e' vuoto."}]}

        target = _safe_target(filename)
        if target is None:
            return {
                "content": [
                    {"type": "text", "text": f"Errore: '{filename}' non valido. Deve essere un nome .md, niente percorsi."}
                ]
            }

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as f:
                f.write("\n\n" + content + "\n")
            logger.info("Memory extended in %s (+%d bytes)", target.name, len(content))
        except OSError as e:
            logger.error("Error appending to %s: %s", target, e)
            return {"content": [{"type": "text", "text": f"Errore scrittura: {e}"}]}

        preview = content if len(content) <= 80 else content[:77] + "..."
        return {"content": [{"type": "text", "text": f"Aggiunto a {target.name}: {preview}"}]}

    return create_sdk_mcp_server("nova_memory", "1.0.0", [note_remember, memory_append])
