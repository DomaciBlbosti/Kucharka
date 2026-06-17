# Nasazení na TrueNAS + self-update z Gitu

## 1. Nahraj projekt do svého repa
Obsah složky `kucharka/` (tj. `backend/`, `frontend/`, `Dockerfile`,
`docker/`, `docker-compose.yml`) musí ležet v **kořeni** repa
`https://github.com/DomaciBlbosti/Kucharka.git`:

```bash
cd kucharka
git init && git remote add origin https://github.com/DomaciBlbosti/Kucharka.git
git add . && git commit -m "init" && git branch -M main && git push -u origin main
```

## 2. Spusť na TrueNAS (Docker / custom app)
```bash
# .env vedle docker-compose.yml (uprav heslo a adresu Ollamy)
cat > .env <<ENV
DB_PASSWORD=neco-silneho
DB_ROOT_PASSWORD=neco-jeste-silnejsiho
OLLAMA_URL=http://host.docker.internal:11434
APP_PORT=8080
UPDATE_ENABLED=true
ENV

docker compose up -d --build
```
- První start: `app` si **naklonuje repo do volume**, nainstaluje Python balíčky,
  **sestaví frontend** a nastartuje API. Trvá pár minut (sleduj `docker compose logs -f app`).
- Web běží na `http://truenas:8080`. Schéma i seed surovin se vytvoří samo.
- Ollama běží mimo kontejner; `host.docker.internal` míří na hostitele (díky
  `extra_hosts: host-gateway`). Když máš Ollamu jinde, přepiš `OLLAMA_URL`.

## 3. Aktualizace z Webu (jako ERI)
1. Na PC commitneš a pushneš novou verzi do `main`.
2. V appce **Přidat → Verze a aktualizace → Aktualizovat z Gitu**.
3. Kontejner ukončí API, supervisor smyčka udělá `git pull`, doinstaluje balíčky,
   **rebuildne frontend** (jen když se kód změnil) a nastartuje novou verzi.
   UI samo počká a ukáže nový commit.

> `UPDATE_ENABLED=true` zpřístupní tlačítko. Drž appku za svým reverzním proxy /
> Cloudflare Zero Trust — endpoint umí spustit `git pull` a restart.

## Import receptů a dat na TrueNAS
Příkazy spouštěj uvnitř kontejneru:
```bash
docker compose exec app bash -lc 'cd /app/backend && \
  python -m app.seed.import_nutridb /app/data/NutriDatabaze.csv --merge-ollama'
docker compose exec app bash -lc 'cd /app/backend && python -m app.modules.crawler'
```
(Soubor s NutriDatabaze CSV si do kontejneru dostaneš přes volume nebo
`docker compose cp NutriDatabaze.csv app:/app/data/`.)
