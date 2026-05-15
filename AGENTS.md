# Repository Guidelines

Template developed by [Heartran](https://github.com/heartran)

## Project Structure & Module Organization

- `nova_bot.py`: Entry point principale - inizializza il bot Discord, gestisce i trigger e l'invio dei messaggi.

- `personality.py`: System prompt e personalità di Nova - definisce il carattere, il tono e lo stile di risposta.

- `memory.py`: Gestione memoria - caricamento file `.md` da `NOVA_MEMORY_DIR` e scrittura su `conversations.md`.

- `nova_mcp.py`: Integration MCP (Model Context Protocol) per estendere le capacità del bot.

- `nova_read.py`: Logica di lettura e parsing dei file di memoria e contesto.

- `nova_whatsapp.py`: Libreria condivisa per il bridge WhatsApp — accesso SQLite, invio messaggi, chiamata Claude.

- `nova_whatsapp_server.py`: Server WhatsApp standalone interattivo. Avvio: `python nova_whatsapp_server.py`. CLI con comandi `chats`, `watch`, `unwatch`, `list`, `status`, `interval`. Config persistente in `NOVA_MEMORY_DIR/whatsapp_config.json`.

- `slash_commands.py`: Definizione e gestione dei comandi slash Discord.

- `checkpoints.py`: Gestione checkpoint per salvataggio e ripristino dello stato.

- `.env` / `.env.example`: Configurazione (token Discord, API key Anthropic, path memoria, impostazioni modello).

- `.venv/`: Virtual environment Python (non committato).

## Build, Test, and Development Commands

- Install deps: `pip install -r requirements.txt` (Python 3.11+; usare `.venv` virtuale).

- Avvio bot: `python nova_bot.py`.

- Non ci sono test automatizzati ancora; per testare manualmente: avvia il bot e verifica risposta a menzione `@Nova` o DM.

## Coding Style & Naming Conventions

- Python: 4-space indentation (PEP 8), UTF-8, trailing newlines. Use PascalCase per classi, snake_case per funzioni/variabili, UPPER_SNAKE_CASE per costanti.

- Prefer `pathlib.Path` over string paths. Usare type hints dove possibile.

- Keep design docs concise and date-stamped inside the document.

## Testing Guidelines

- Place specs in root o cartella `tests/` (es. `test_nova_bot.py`). Mock Discord API e Claude calls per test deterministici.

- Focus su logiche core: parsing memoria, trigger messaggi, gestione checkpoint.

- Documenta step di verifica manuale per interazioni Discord (es. flow DM vs menzione).

## Commit & Pull Request Guidelines

- Use short, imperative commit messages (e.g., `Add dialogue parser`, `Fix scene load order`). Keep changes scoped and commit frequently.

- PRs should include intent, key changes, and testing performed; link related tasks/issues. Add screenshots or short clips for visual changes.

- Before opening a PR, ensure docs are updated, new commands are documented, and tests (if any) pass locally.

- All commits should be done using **your own git identity**

- Do not work directly on `main` or `render/preview`: create a dedicated branch with setup prefix before committing or pushing.
  - Examples: `windsurf/feature-name`, `codex/fix-description`, `gemini/refactor-module`

- Never delete branches (no `--delete-branch` on merges) unless explicitly instructed.

- **Merge policy (MANDATORY for all agents): unless explicitly specified otherwise by the user, the merge target is ALWAYS `main`.**
  - If the target branch is not written clearly in the request, assume `main`.
  - Do not infer a different merge target from recent context/history.

## Identity & Git Hygiene

- Author/committer identity is managed by the repo owner; do not change git config locally (no `git config` commands). Use the existing configuration as-is. Use $ENV variables for agent-specific commits.

- Never use the Heartran git identity for commits or pushes.

- Keep commits small and topical; prefer multiple commits over one large drop when touching orthogonal areas.

## Memoria & Checkpoint

- I file `.md` in `NOVA_MEMORY_DIR` vengono letti ad ogni messaggio. Mantieni `INDEX.md` aggiornato con nuovi file.

- `conversations.md` viene scritto automaticamente; non modificarlo manualmente durante l'esecuzione del bot.

- I checkpoint vengono salvati in `.claude/` (non committato).

## Git Identity

- Every agent should have his own git identity when committing changes in order to have a more clear and readable history

| Agent | GIT_COMMITTER_NAME / GIT_AUTHOR_NAME | GIT_COMMITTER_EMAIL / GIT_AUTHOR_EMAIL |
| --- | :---: | --- |
| Claude | Claude | [noreply@anthropic.com](mailto:noreply@anthropic.com) |
| Codex | Codex | [199175422+chatgpt-codex-connector[bot]@users.noreply.github.com](mailto:199175422+chatgpt-codex-connector[bot]@users.noreply.github.com) |
| Gemini | Gemini | [176961590+gemini-code-assist[bot]@users.noreply.github.com](mailto:176961590+gemini-code-assist[bot]@users.noreply.github.com) |
| Cascade | Cascade | [272510577+windsurf-cascade-agent[bot]@users.noreply.github.com](mailto:272510577+windsurf-cascade-agent[bot]@users.noreply.github.com) |
| GitHub Copilot | Copilot[bot] | [198982749+Copilot[bot]@users.noreply.github.com](mailto:198982749+Copilot[bot]@users.noreply.github.com) |