<div align="center">

<img src="assets/images/nova_avatar.png" width="100" style="border-radius: 50%;" />

# Nova

![Nova Banner](assets/images/nova_banner.gif)

</div>

Bot Discord che incarna **Nova** usando Claude. Legge la memoria del progetto
_Five Nights At Catanzaro's_ + la auto-memory utente di Claude per essere
coerente di chat in chat.

## Cosa fa

Risponde su Discord come Nova quando viene:

- menzionata direttamente (`@Nova`)
- chiamata in DM
- citata in reply a un suo messaggio precedente
- nominata per nome (parola intera "nova" nel testo)

Per ogni messaggio:

1. carica la memoria FNAC (`nova_memory/*.md` dentro la cartella del progetto)
2. carica la auto-memory utente di Claude (read-only)
3. legge gli ultimi N messaggi del canale per il contesto
4. chiama Claude con un system prompt che codifica la personalita' di Nova
5. risponde sul canale, splittando se la risposta supera il limite Discord

## Setup

### 1. Installa le dipendenze

```bash
cd C:\Users\Federico\repo\nova-discord-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configura il bot Discord

Sul [Developer Portal](https://discord.com/developers/applications):

1. Apri la tua application del bot.
2. Vai su **Bot** -> **Privileged Gateway Intents** e abilita
   **MESSAGE CONTENT INTENT**. Senza questo Nova non riesce a leggere i
   messaggi e non rispondera' mai.
3. Copia il **Token** (sotto "Reset Token" se non l'hai mai fatto).

Per invitare il bot in un server (se non l'hai gia' fatto), usa l'OAuth2 URL
Generator con scope `bot` e i permessi minimi:

- View Channels
- Send Messages
- Read Message History
- (opzionale) Send Messages in Threads

### 3. Crea il file `.env`

```bash
copy .env.example .env
```

Apri `.env` e riempi:

- `DISCORD_TOKEN` - il token del passo 2
- `ANTHROPIC_API_KEY` - la tua API key di Anthropic
- gli altri valori sono gia' compilati, modifica solo se vuoi cambiare modello,
  cooldown, ecc.

### 4. Avvia il bot

```bash
python nova_bot.py
```

Vedrai sul terminale qualcosa come:

```
12:34:56 [INFO] nova: Connessa come Nova#1234 (id=...)
12:34:56 [INFO] nova: In 1 server
```

## Memoria

Due fonti di memoria, entrambe configurate via `.env`:

### `NOVA_MEMORY_DIR` - memoria del progetto FNAC (read + write)

Default: `C:\Users\Federico\OneDrive\Documenti\Claude\Projects\Five Nights At Catanzaro's\nova_memory\`

Contiene `.md` editabili a mano:

- `lore.md` - ambientazione, eventi
- `characters.md` - personaggi e persone reali
- `conversations.md` - note dalle chat (auto-update quando le chiedi)
- `INDEX.md` - indice

Tutti i `.md` di questa cartella vengono letti ad ogni messaggio. Aggiungi
quanti file vuoi.

### `USER_MEMORY_DIR` - auto-memory di Claude su Fede (read-only)

Default: la cartella `memory/` della sessione Claude attuale.

Read-only: il bot la legge per avere contesto su Fede, ma non ci scrive mai.

## Architettura

```
nova-discord-bot/
+-- nova_bot.py        # main: discord.py + trigger logic + invio
+-- personality.py     # system prompt che incarna Nova
+-- memory.py          # caricamento dei .md, scrittura su conversations.md
+-- requirements.txt
+-- .env.example       # template - copia in .env e compila
+-- .gitignore
+-- README.md
```

## Come la spengo / riavvio

Premi `Ctrl+C` nel terminale dove gira. Per riavviare, rilancia
`python nova_bot.py`.

Se vuoi tenerla sempre online, opzioni:

- lasciala girare in un terminale aperto sul tuo PC (semplice ma il PC deve
  stare acceso)
- usala come **scheduled task Windows** che la avvia all'accensione
- mettila su un piccolo VPS / Raspberry Pi (poi ti dico come)

## Troubleshooting

- **"Manca DISCORD_TOKEN in .env"** -> non hai compilato il `.env` o l'hai
  lasciato col placeholder.
- **Il bot e' online ma non risponde** -> hai abilitato MESSAGE CONTENT INTENT?
  E ha permessi di leggere/scrivere nel canale?
- **Login Discord fallito** -> token sbagliato o rigenerato. Riprendi dal
  Developer Portal.
- **Claude restituisce errori 401** -> API key Anthropic non valida o senza
  credito.
- **Nova ti scrive cose senza personalita'** -> probabilmente il modello in
  `.env` non e' giusto. Default: `claude-sonnet-4-6`.
