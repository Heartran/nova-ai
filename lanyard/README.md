# Lanyard self-host — per PC-Federico

Espone la presence Discord (attività, Spotify, stato online) via **API REST locale**,
leggendola dal bot **Nova**. Niente Lanyard pubblico, niente terzi.

```
GET http://localhost:4001/v1/users/<user_id>
```

## Cosa contiene questa cartella

| File | A cosa serve |
|---|---|
| `docker-compose.yml` | Definisce due container: `redis` + `phineas/lanyard:latest` |
| `.env.example` | Modello: ci va il token del bot Nova (`BOT_TOKEN`) |
| `.env` | **Solo su questa macchina** — contiene già il token (gitignored). Su PC-Federico ricrealo dal `.example` |

Immagine usata: **`phineas/lanyard:latest`** (più `redis:7-alpine`). Porta esposta: **4001**.

## Requisiti su PC-Federico

- Docker (Desktop o Engine) installato e in esecuzione.
- Il bot Nova deve condividere un server con te e avere **PRESENCE INTENT** +
  **SERVER MEMBERS INTENT** attivi (già fatto).
- Tu devi essere **online** (non invisibile): in invisibile Discord non manda la presence.

## Deploy con internet (consigliato)

Su PC-Federico, dentro questa cartella:

```bash
# 1. crea il .env col token del bot Nova
cp .env.example .env   # poi incolla il token in BOT_TOKEN=

# 2. avvia (scarica le immagini da Docker Hub la prima volta)
docker compose up -d

# 3. verifica
docker compose ps
curl http://localhost:4001/v1/users/302163577886998528
```

## Deploy offline (se PC-Federico non ha accesso a Docker Hub)

Su una macchina con Docker **e** internet, esporta le immagini in un file:

```bash
docker pull phineas/lanyard:latest
docker pull redis:7-alpine
docker save phineas/lanyard:latest redis:7-alpine -o lanyard-images.tar
```

Copia `lanyard-images.tar` su PC-Federico e caricalo:

```bash
docker load -i lanyard-images.tar
docker compose up -d
```

## Note

- La presence si popola pochi secondi dopo l'avvio (il bot riceve i `GUILD_CREATE`).
- Lanyard espone **HTTP** (non HTTPS): va benissimo in locale. Se vuoi esporlo fuori,
  mettici davanti un reverse proxy.
- Stesso token usato da `nova_bot.py`: un bot può avere più connessioni gateway, quindi
  Lanyard e il bot Python possono girare insieme senza pestarsi i piedi.
