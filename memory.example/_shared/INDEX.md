# Nova shared memory

Every `.md` file in this folder is loaded on **every** response, regardless of
the scope (Discord server, DM, or WhatsApp chat). It is the place for context
that is always relevant.

## What goes here

- Global project / world lore.
- People recurring across chats (who they are, patterns, in-jokes).
- Behavioral rules valid everywhere ("always answer in English", etc.).

## What does NOT go here

- Notes specific to a single chat — those belong under `server/`, `dm/`, or
  `whatsapp/`.
- Secrets, tokens, credentials.

## Files in this folder

- `group.md` — members of the recurring group / inner circle.

Add more `.md` files as needed (e.g. `lore.md`, `rules.md`). Filenames starting
with `INDEX` or `MEMORY` are loaded first.
