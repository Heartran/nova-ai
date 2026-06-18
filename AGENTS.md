# Repository Guidelines

Template developed by [Heartran](https://github.com/heartran)

## Project Structure & Module Organization

- `nova_bot.py`: Main entry point - initializes the Discord bot, handles triggers and message sending.

- `personality.py`: System prompt and Nova's personality - defines her character, tone and reply style.

- `memory.py`: Memory management - loads `.md` files from `NOVA_MEMORY_DIR` and writes to `conversations.md`.

- `nova_mcp.py`: MCP (Model Context Protocol) integration to extend the bot's capabilities.

- `nova_voice.py`: Voice-channel support — join a voice channel, transcribe per-speaker audio (STT), run the same Claude pipeline (personality + memory + tools), and reply with ElevenLabs streaming TTS. Heavy deps (`discord-ext-voice-recv`, `PyNaCl`) are imported lazily so the rest of the bot runs without them; managed via the `/voce` slash commands.

- `nova_read.py`: Read/parse logic for memory and context files.

- `nova_whatsapp.py`: Shared library for the WhatsApp bridge — SQLite access, message sending, Claude call.

- `nova_whatsapp_server.py`: Standalone interactive WhatsApp server. Start with: `python nova_whatsapp_server.py`. CLI with commands `chats`, `watch`, `unwatch`, `list`, `status`, `interval`. Persistent config in `NOVA_MEMORY_DIR/whatsapp_config.json`.

- `slash_commands.py`: Definition and handling of Discord slash commands.

- `checkpoints.py`: Checkpoint management for saving and restoring state.

- `.env` / `.env.example`: Configuration (Discord token, Anthropic API key, memory path, model settings).

- `.venv/`: Python virtual environment (not committed).

## Build, Test, and Development Commands

- Install deps: `pip install -r requirements.txt` (Python 3.11+; use a `.venv`).

- Start the bot: `python nova_bot.py`.

- No automated tests yet; to test manually: start the bot and verify replies on `@Nova` mention or DM.

## Coding Style & Naming Conventions

- Python: 4-space indentation (PEP 8), UTF-8, trailing newlines. Use PascalCase for classes, snake_case for functions/variables, UPPER_SNAKE_CASE for constants.

- Prefer `pathlib.Path` over string paths. Use type hints where possible.

- Keep design docs concise and date-stamped inside the document.

## Testing Guidelines

- Place specs at the root or in a `tests/` folder (e.g. `test_nova_bot.py`). Mock Discord API and Claude calls for deterministic tests.

- Focus on core logic: memory parsing, message triggers, checkpoint handling.

- Document manual verification steps for Discord interactions (e.g. DM vs mention flows).

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

## Memory & Checkpoints

- The `.md` files in `NOVA_MEMORY_DIR` are read on every message. Keep `INDEX.md` up to date when adding new files.

- `conversations.md` is written automatically; do not edit it manually while the bot is running.

- Checkpoints are saved under `.claude/` (not committed).

## Git Identity

- Every agent should have its own git identity when committing changes in order to keep history clear and readable.

| Agent | GIT_COMMITTER_NAME / GIT_AUTHOR_NAME | GIT_COMMITTER_EMAIL / GIT_AUTHOR_EMAIL |
| --- | :---: | --- |
| Claude | Claude | [noreply@anthropic.com](mailto:noreply@anthropic.com) |
| Codex | Codex | [199175422+chatgpt-codex-connector[bot]@users.noreply.github.com](mailto:199175422+chatgpt-codex-connector[bot]@users.noreply.github.com) |
| Gemini | Gemini | [176961590+gemini-code-assist[bot]@users.noreply.github.com](mailto:176961590+gemini-code-assist[bot]@users.noreply.github.com) |
| Cascade | Cascade | [272510577+windsurf-cascade-agent[bot]@users.noreply.github.com](mailto:272510577+windsurf-cascade-agent[bot]@users.noreply.github.com) |
| GitHub Copilot | Copilot[bot] | [198982749+Copilot[bot]@users.noreply.github.com](mailto:198982749+Copilot[bot]@users.noreply.github.com) |
