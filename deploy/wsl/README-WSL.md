# Běh ve WSL (Ubuntu) — odlazení před Dockerem

Rozdělení podle **typu serveru**, ne podle vrstev:

| WSL prostředí | Co běží | Porty |
|---------------|---------|-------|
| `kucharka`    | celá appka: DB (MariaDB) + API (FastAPI) + Web (Vite) | 3306 / 8000 / 5173 |
| `ollama`      | Ollama (LLM) — *nebo použij stávající na TrueNASu* | 11434 |
| `searxng`     | SearXNG (vyhledávání) — volitelné | – |

Klíčové: DB + API + web jsou jedna logická aplikace → běží **v jednom distru**
na `localhost`, takže síť mezi distry řešit nemusíš. Ven se sahá jen na Ollamu
(LAN adresa, funguje vždy). Docker (`docker-compose.yml`) zůstává pro finální
nasazení nedotčený.

> Ollamu už máš na TrueNASu (`172.24.1.111:11434`) — samostatný WSL pro LLM
> dělat nemusíš, stačí na ni v `backend/.env` nasměrovat `OLLAMA_URL`.

---

## 1) Vytvoř distro pro appku

V PowerShellu (na Windows):

```powershell
# z libovolného základního Ubuntu:
wsl --export Ubuntu C:\wsl\ubuntu-base.tar
wsl --import kucharka C:\wsl\kucharka C:\wsl\ubuntu-base.tar
```

Importované distro běží jako `root` — pro vývoj v pohodě.

## 2) Repo a setup (jednorázově)

```bash
wsl -d kucharka
apt-get update && apt-get install -y git
git clone <tvuj-repo-url> ~/kucharka && cd ~/kucharka
bash deploy/wsl/kucharka/setup-all.sh    # DB + venv + Node + balíčky + .env
```

Pak zkontroluj `backend/.env` — hlavně `OLLAMA_URL=http://172.24.1.111:11434`
a `OLLAMA_MODEL=qwen3:8b`.

## 3) Spuštění

```bash
wsl -d kucharka
cd ~/kucharka
bash deploy/wsl/kucharka/run-all.sh
```

Spustí MariaDB, API (`:8000`) i Vite (`:5173`) najednou; `Ctrl+C` ukončí
appku (DB service běží dál). Otevři na Windows: **http://localhost:5173**

- API Swagger: http://localhost:8000/docs
- Backend si při startu vytvoří tabulky a nasype základní suroviny.

> Chceš logy API a webu zvlášť (lepší na ladění)? Místo `run-all.sh` spusť ve
> dvou terminálech `deploy/wsl/api/run.sh` a `deploy/wsl/web/run.sh`
> (a jednou `deploy/wsl/db/run.sh`). Jednotlivé skripty zůstávají k dispozici.

## Konfigurace

- API čte `backend/.env` (vzniká z `deploy/wsl/api/env.example`). Tam je
  `DATABASE_URL`, volitelně `OLLAMA_URL` / `SEARXNG_URL`.
- Frontend cílí API přes `VITE_API_TARGET` (default `http://localhost:8000`).
  Při jiné adrese API:  `VITE_API_TARGET=http://<ip>:8000 bash deploy/wsl/web/run.sh`

## Ollama (chytrá normalizace ingrediencí)

Defaultní `env.example` míří na tvou instanci `http://172.24.1.111:11434`. Z WSL
je to LAN adresa, takže funguje nezávisle na mirrored režimu.

Ověř po spuštění API:

```bash
curl http://172.24.1.111:11434/api/tags     # jaké modely máš
curl http://localhost:8000/api/search/ollama # diagnostika z pohledu appky
```

V UI (záložka **Přidat**) uvidíš stav: zda je Ollama dostupná a sedí model.
`OLLAMA_MODEL` nastav v `backend/.env` na model, který máš stažený.

Rychlý test parseru bez UI (z API instance, aktivní venv):

```bash
cd backend && . .venv/bin/activate
python -m app.modules.normalizer "2 lžíce hladké mouky" "špetka soli"
```

Bez Ollamy appka jede dál na regex fallbacku — jen hůř zvládá pády a kusové
jednotky.

## Import surovin z NutriDatabaze.cz

V API instanci (aktivuj venv) z kořene repa:

```bash
cd backend && . .venv/bin/activate
python -m app.seed.import_nutridb /mnt/c/cesta/export.csv \
    --col-name "Název potraviny v češtině"
```

---

## Když oddělíš Ollamu / SearXNG do vlastních distr

Appka (`kucharka` distro) běží sama o sobě na `localhost` a nic víc nepotřebuje.
Pokud ale dáš Ollamu nebo SearXNG do **samostatného WSL distra** a chceš je
adresovat přes `localhost`, zapni mirrored networking — vlož obsah
`wslconfig-mirrored.txt` do `%UserProfile%\.wslconfig` (Win 11 22H2+) a
`wsl --shutdown`. Pak v `backend/.env`:

```
OLLAMA_URL=http://localhost:11434
SEARXNG_URL=http://localhost:8080
```

Bez mirrored režimu adresuj přes IP daného distra (`wsl -d ollama hostname -I`).
Ollamu na TrueNASu (`172.24.1.111`) tohle neřeší — ta je dostupná vždy.

## Až bude odladěno → Docker

Nic se nepřepisuje: `docker compose up -d --build` z kořene repa použije
stejný backend i frontend (viz hlavní `README.md`). WSL skripty slouží jen
k vývoji a ladění.
