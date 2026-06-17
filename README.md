# Kuchařka

Self-hosted interaktivní kuchařka: stahuje recepty z webu, páruje suroviny na
kalorické údaje, eviduje, co máš doma, a filtruje recepty podle dostupných
surovin. Postavené na FastAPI + MariaDB + React, určené k běhu jako **Custom App
na TrueNASu** (Docker).

## Co umí (v1)

- **Přidání receptu z URL** — stáhne stránku a vyparsuje ji (`recipe-scrapers`,
  přes 640 webů + `wild_mode` pro cokoli se `schema.org/Recipe`).
- **Vyhledávání receptů** přes vlastní **SearXNG** (volitelné).
- **Normalizace ingrediencí** — „2 lžíce hladké mouky" → surovina + gramy + kcal.
  S **Ollamou** chytře, bez ní heuristickým parserem (volitelné).
- **Databáze surovin** s kaloriemi — základní sada hned po startu, plná data
  importem z NutriDatabaze.cz.
- **Spíž** — co máš doma; **filtr „co uvařím teď"**, max počet chybějících,
  max čas, hodnocení.
- **Nákupní seznam** — chybějící suroviny receptu přidáš jedním klikem.

Moduly jsou odolné ve stylu MERI: Ollama i SearXNG jsou volitelné a jejich
absence appku nepoloží.

## Architektura

```
backend/app
├─ main.py            FastAPI: API /api/* + servírování SPA
├─ models.py          ORM (ingredient, recipe, pantry, …)
├─ modules/
│  ├─ discovery.py    SearXNG klient
│  ├─ scraper.py      fetch (throttle) + recipe-scrapers
│  ├─ normalizer.py   parsování řádku + párování na kanon (Ollama/heuristika)
│  ├─ nutrition.py    převod jednotek na gramy + výpočet kcal
│  ├─ pantry.py       dostupnost receptu vůči spíži
│  └─ ingest.py       URL → recept v DB (idempotentní upsert)
├─ routers/           REST endpointy
└─ seed/              starter suroviny + import NutriDatabaze
frontend/             React (Vite + Tailwind), mobile-first
```

**Datový tok:** SearXNG → scraper → normalizer (Ollama) → výpočet kcal → MariaDB.

## Nasazení na TrueNAS

### Vývoj/odlazení ve WSL (před Dockerem)

Pro lokální ladění běží DB, API a frontend nativně ve třech samostatných WSL
prostředích — viz **`deploy/wsl/README-WSL.md`**. Docker je až finální krok.

### Volba A — přes SSH (nejrychlejší)

1. Naklonuj repo na dataset, např. `/mnt/Main_pool/apps/kucharka`.
2. Zkopíruj `.env.example` na `.env` a změň hesla.
3. `docker compose up -d --build`
4. Appka běží na `http://<truenas>:8975`.

### Volba B — přes Apps UI (Install via YAML)

Build context se v Apps UI špatně předává, takže image nejdřív postav a nahraj
do svého Gitea registru:

```bash
docker build -t git.aleshulek.cz/tesla/kucharka:latest .
docker push  git.aleshulek.cz/tesla/kucharka:latest
```

Pak v **Apps → Discover → ⋮ → Install via YAML** vlož `docker-compose.yml`,
ale v něm:
- zakomentuj `build:` a odkomentuj `image: git.aleshulek.cz/tesla/kucharka:latest`,
- hesla a volitelné `OLLAMA_URL`/`SEARXNG_URL` vlož přímo do `environment`
  (UI nečte `.env`).

> Pro perzistenci DB na poolu nahraď named volume `kucharka_db` za host path,
> např. `/mnt/Main_pool/apps/kucharka/db:/var/lib/mysql`.

### Reverse proxy

Za NPM Plus nasměruj `kucharka.aleshulek.cz` → `http://<truenas>:8975`.
Do AdGuardu přidej rewrite na `172.24.1.111` (tvůj wildcard `*.aleshulek.cz`).

## Konfigurace (env)

| Proměnná        | Význam                                   | Default |
|-----------------|------------------------------------------|---------|
| `DATABASE_URL`  | připojení k DB                           | MariaDB service |
| `OLLAMA_URL`    | endpoint Ollamy (prázdné = vypnuto)      | –       |
| `OLLAMA_MODEL`  | model pro normalizaci                    | `qwen2.5:7b` |
| `SEARXNG_URL`   | endpoint SearXNG (prázdné = vypnuto)     | –       |
| `RECIPE_DOMAINS`| whitelist domén pro discovery (čárkou)   | vše     |
| `SCRAPE_DELAY`  | prodleva mezi requesty na doménu (s)     | `1.0`   |

## Import surovin z NutriDatabaze.cz

Po (bezplatné) registraci stáhni exportní balíček a naimportuj:

```bash
docker exec -it kucharka python -m app.seed.import_nutridb \
    /cesta/export.csv --col-name "Název potraviny v češtině"
```

Skript se snaží sloupce odhadnout; mapování lze přepsat přes `--col-*`.
Data NutriDatabaze podléhají jejich licenci — používej pro vlastní potřebu.

## Autonomní crawler

Kuchařka umí sama plnit databázi: projde seed dotazy přes SearXNG, stáhne
recepty, normalizuje je a uloží. Chybějící suroviny umí doplnit přes Ollamu
(odhad výživy, `source="ollama"` – import z NutriDatabaze je pak zpřesní).

- **Ručně z UI:** záložka *Přidat* → *Automatické objevování* → „Naplnit databázi".
- **Z CLI:** `python -m app.modules.crawler "svíčková" "guláš"` (bez argumentů = výchozí sada).
- **Na pozadí (plánovaně):** `CRAWLER_ENABLED=true` + `CRAWLER_INTERVAL_MIN` /
  `CRAWLER_MAX_PER_RUN`. Dorůstání surovin zapíná `AUTO_INGREDIENTS=true`.
- **API:** `POST /api/crawl/run`, `GET /api/crawl/status`.

Vyžaduje nastavený `SEARXNG_URL`. Doporučeno doplnit `RECIPE_DOMAINS`
(whitelist), ať crawler zůstane na skutečných receptových webech.

## Roadmap (další fáze)

- Per-surovina hustoty a převody (přesnější gramáž lžic/kusů)
- Kategorie/štítky, alergeny, filtr podle diety
- Dávkový crawler (plnění zásoby přes sitemapy whitelistu)
- Sdílení receptů přes Cloudflare Zero Trust (rodina)
- PWA manifest pro „instalaci" na telefon

## Licence / použití

Osobní, domácí použití. Scraping respektuje `robots.txt` a podmínky webů,
běží s rozumným rate-limitem a cache.
