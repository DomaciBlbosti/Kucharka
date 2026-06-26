"""Konfigurace aplikace. Vše přes proměnné prostředí, s rozumnými defaulty.

Volitelné závislosti (Ollama, SearXNG) jsou skutečně volitelné — když nejsou
nastavené, příslušné funkce se degradují (heuristický parser místo Ollamy,
ruční vkládání URL místo vyhledávání).
"""
from __future__ import annotations

import os

try:  # automatické načtení backend/.env, ať funguje i CLI/skripty bez `source`
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # noqa: BLE001
    pass


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        # --- Databáze ---------------------------------------------------
        # Buď přímo DATABASE_URL, nebo se složí z DB_* (MariaDB).
        url = _env("DATABASE_URL")
        if not url:
            host = _env("DB_HOST", "mariadb")
            port = _env("DB_PORT", "3306")
            name = _env("DB_NAME", "kucharka")
            user = _env("DB_USER", "kucharka")
            pwd = _env("DB_PASSWORD", "kucharka")
            url = f"mysql+pymysql://{user}:{pwd}@{host}:{port}/{name}?charset=utf8mb4"
        self.database_url: str = url

        # --- Volitelné: Ollama (normalizace ingrediencí) ---------------
        # Např. http://172.24.1.111:11434
        self.ollama_url: str = _env("OLLAMA_URL")
        self.ollama_model: str = _env("OLLAMA_MODEL", "qwen3:8b")
        # Rychlý model pro hromadné úlohy (překlad, parsování, párování,
        # kategorizace). Prázdné = vždy sleduje hlavní model (i po jeho změně).
        self._fast_model: str = _env("OLLAMA_FAST_MODEL", "")
        # Jak dlouho držet model v paměti mezi voláními (méně reloadů = rychleji).
        self.ollama_keep_alive: str = _env("OLLAMA_KEEP_ALIVE", "30m")
        # Počet souběžných workerů pro úlohy na pozadí (vyžaduje OLLAMA_NUM_PARALLEL).
        self.bg_workers: int = max(1, int(_env("BG_WORKERS", "2")))

        # --- Služby na pozadí (překlad / párování) ---------------------
        self.auto_translate_enabled: bool = _truthy(_env("AUTO_TRANSLATE_ENABLED", "false"))
        self.auto_translate_interval_min: int = int(_env("AUTO_TRANSLATE_INTERVAL_MIN", "180"))
        self.auto_match_enabled: bool = _truthy(_env("AUTO_MATCH_ENABLED", "false"))
        self.auto_match_interval_min: int = int(_env("AUTO_MATCH_INTERVAL_MIN", "180"))

        # --- Enrichment worker (slovník + fuzzy) -----------------------
        # Bere recepty s enrichment_status='pending' a doplňuje matching surovin.
        # Default ON, krátký interval — bez LLM je to laciné a chceme rychlou odezvu.
        self.enrichment_enabled: bool = _truthy(_env("ENRICHMENT_ENABLED", "true"))
        self.enrichment_interval_min: int = int(_env("ENRICHMENT_INTERVAL_MIN", "1"))
        self.enrichment_batch_size: int = int(_env("ENRICHMENT_BATCH_SIZE", "20"))

        # --- Image worker (stahování + resize) -------------------------
        self.image_enabled: bool = _truthy(_env("IMAGE_ENABLED", "true"))
        self.image_interval_min: int = int(_env("IMAGE_INTERVAL_MIN", "1"))
        self.image_batch_size: int = int(_env("IMAGE_BATCH_SIZE", "10"))
        self.images_dir: str = _env("IMAGES_DIR", "/data/images")

        # --- LLM match (batch párování přes Ollamu pro manual_review) -----
        # Opt-in: default OFF, dokud uživatel explicitně nezapne. Pomáhá hlavně
        # cizojazyčným webům, kde slovník/fuzzy match nezachytí EN/IT/HI suroviny.
        self.llm_match_enabled: bool = _truthy(_env("LLM_MATCH_ENABLED", "false"))
        self.llm_match_interval_min: int = int(_env("LLM_MATCH_INTERVAL_MIN", "5"))
        self.llm_match_batch_size: int = int(_env("LLM_MATCH_BATCH_SIZE", "40"))
        self.llm_match_min_confidence: float = float(_env("LLM_MATCH_MIN_CONFIDENCE", "0.7"))
        self.llm_match_model: str = _env("LLM_MATCH_MODEL", "")  # prázdné = fallback na ollama_model

        # --- Volitelné: SearXNG (discovery receptů) --------------------
        # Např. http://searxng:8080
        self.searxng_url: str = _env("SEARXNG_URL")

        # --- Scraper ----------------------------------------------------
        self.user_agent: str = _env(
            "SCRAPER_USER_AGENT",
            "Kucharka/0.1 (osobni domaci pouziti)",
        )
        # Sekund mezi requesty na stejnou doménu (slušnost).
        self.scrape_delay: float = float(_env("SCRAPE_DELAY", "1.0"))
        self.http_timeout: float = float(_env("HTTP_TIMEOUT", "20"))

        # Ověřování TLS při scrapování. "false" = vypnuto (vhodné za firemní
        # proxy, kterou nelze obejít). Jinak použij systémový CA bundle, pokud
        # existuje (zahrne i firemní CA přidanou přes update-ca-certificates).
        verify = _env("SCRAPER_VERIFY_SSL", "true").lower()
        if verify in ("0", "false", "no", "off"):
            self.scraper_verify: bool | str = False
        else:
            bundle = "/etc/ssl/certs/ca-certificates.crt"
            self.scraper_verify = bundle if os.path.exists(bundle) else True

        # Whitelist domén pro discovery. Prázdné = ber vše, co projde scraperem.
        wl = _env("RECIPE_DOMAINS")
        self.recipe_domains: set[str] = {
            d.strip().lower() for d in wl.split(",") if d.strip()
        }

        # --- Crawler (autonomní plnění DB) ----------------------------
        self.crawler_enabled: bool = _env("CRAWLER_ENABLED", "false").lower() in (
            "1", "true", "yes", "on"
        )
        self.crawler_interval_min: int = int(_env("CRAWLER_INTERVAL_MIN", "360"))
        self.crawler_max_per_run: int = int(_env("CRAWLER_MAX_PER_RUN", "15"))
        seeds = _env("CRAWLER_SEEDS")
        self.crawler_seeds: list[str] = [
            s.strip() for s in seeds.split(",") if s.strip()
        ]
        # Dorůstání databáze surovin: chybějící surovinu doplní Ollama (odhad výživy)
        self.auto_ingredients: bool = _env("AUTO_INGREDIENTS", "false").lower() in (
            "1", "true", "yes", "on"
        )
        # Překlad zahraničních receptů do češtiny (vyžaduje Ollamu)
        self.translate_to_cs: bool = _env("TRANSLATE_TO_CS", "true").lower() in (
            "1", "true", "yes", "on"
        )
        # RAG generování receptů
        self.embed_model: str = _env("EMBED_MODEL", "nomic-embed-text")
        self.rag_k: int = int(_env("RAG_K", "6"))  # kolik receptů jako kontext
        # Self-update z Gitu přes WEB UI
        self.update_enabled: bool = _env("UPDATE_ENABLED", "false").lower() in (
            "1", "true", "yes", "on"
        )
        self.repo_dir: str = _env("REPO_DIR", "")

        # Zabezpečení heslem (hash se načítá z app_setting při startu)
        self.auth_password_hash: str | None = None
        self.auth_secret: str = ""

    @property
    def ollama_enabled(self) -> bool:
        return bool(self.ollama_url)

    @property
    def searxng_enabled(self) -> bool:
        return bool(self.searxng_url)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_password_hash)

    @property
    def ollama_fast_model(self) -> str:
        return self._fast_model or self.ollama_model

    ADMIN_KEYS = (
        "ollama_url", "ollama_model", "ollama_fast_model", "embed_model", "searxng_url",
        "recipe_domains", "translate_to_cs", "auto_ingredients",
        "scraper_verify_ssl", "rag_k",
        "crawler_enabled", "crawler_interval_min", "crawler_max_per_run",
        "ollama_keep_alive", "bg_workers",
        "auto_translate_enabled", "auto_translate_interval_min",
        "auto_match_enabled", "auto_match_interval_min",
        "enrichment_enabled", "enrichment_interval_min", "enrichment_batch_size",
        "image_enabled", "image_interval_min", "image_batch_size",
        "llm_match_enabled", "llm_match_interval_min", "llm_match_batch_size",
        "llm_match_min_confidence", "llm_match_model",
    )

    CRAWLER_KEYS = ("crawler_enabled", "crawler_interval_min", "crawler_max_per_run")
    SERVICE_KEYS = (
        "auto_translate_enabled", "auto_translate_interval_min",
        "auto_match_enabled", "auto_match_interval_min",
        "enrichment_enabled", "enrichment_interval_min",
        "image_enabled", "image_interval_min",
        "llm_match_enabled", "llm_match_interval_min",
    )
    ENRICHMENT_KEYS = ("enrichment_enabled", "enrichment_interval_min")
    IMAGE_KEYS = ("image_enabled", "image_interval_min")
    LLM_MATCH_KEYS = ("llm_match_enabled", "llm_match_interval_min")

    def as_admin(self) -> dict:
        return {
            "ollama_url": self.ollama_url,
            "ollama_model": self.ollama_model,
            "embed_model": self.embed_model,
            "searxng_url": self.searxng_url,
            "recipe_domains": ",".join(sorted(self.recipe_domains)),
            "translate_to_cs": self.translate_to_cs,
            "auto_ingredients": self.auto_ingredients,
            "scraper_verify_ssl": self.scraper_verify is not False,
            "rag_k": self.rag_k,
            "crawler_enabled": self.crawler_enabled,
            "crawler_interval_min": self.crawler_interval_min,
            "crawler_max_per_run": self.crawler_max_per_run,
            "ollama_fast_model": self._fast_model,
            "ollama_fast_model_effective": self.ollama_fast_model,
            "ollama_keep_alive": self.ollama_keep_alive,
            "bg_workers": self.bg_workers,
            "auto_translate_enabled": self.auto_translate_enabled,
            "auto_translate_interval_min": self.auto_translate_interval_min,
            "auto_match_enabled": self.auto_match_enabled,
            "auto_match_interval_min": self.auto_match_interval_min,
            "enrichment_enabled": self.enrichment_enabled,
            "enrichment_interval_min": self.enrichment_interval_min,
            "enrichment_batch_size": self.enrichment_batch_size,
            "image_enabled": self.image_enabled,
            "image_interval_min": self.image_interval_min,
            "image_batch_size": self.image_batch_size,
            "images_dir": self.images_dir,
            "llm_match_enabled": self.llm_match_enabled,
            "llm_match_interval_min": self.llm_match_interval_min,
            "llm_match_batch_size": self.llm_match_batch_size,
            "llm_match_min_confidence": self.llm_match_min_confidence,
            "llm_match_model": self.llm_match_model,
            "ollama_enabled": self.ollama_enabled,
            "searxng_enabled": self.searxng_enabled,
            "auth_enabled": self.auth_enabled,
        }

    def set_admin(self, key: str, value) -> bool:
        if key not in self.ADMIN_KEYS:
            return False
        if key in ("ollama_url", "ollama_model", "embed_model", "searxng_url"):
            setattr(self, key, str(value or "").strip())
        elif key == "ollama_fast_model":
            self._fast_model = str(value or "").strip()
        elif key == "ollama_keep_alive":
            self.ollama_keep_alive = str(value or "30m").strip() or "30m"
        elif key == "recipe_domains":
            self.recipe_domains = {
                d.strip().lower()
                for d in str(value or "").replace("\n", ",").split(",")
                if d.strip()
            }
        elif key in (
            "translate_to_cs", "auto_ingredients", "crawler_enabled",
            "auto_translate_enabled", "auto_match_enabled",
            "enrichment_enabled", "image_enabled", "llm_match_enabled",
        ):
            setattr(self, key, _truthy(value))
        elif key == "scraper_verify_ssl":
            if not _truthy(value):
                self.scraper_verify = False
            else:
                bundle = "/etc/ssl/certs/ca-certificates.crt"
                self.scraper_verify = bundle if os.path.exists(bundle) else True
        elif key == "llm_match_min_confidence":
            try:
                self.llm_match_min_confidence = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                pass
        elif key == "llm_match_model":
            self.llm_match_model = str(value or "").strip()
        elif key in (
            "rag_k", "crawler_interval_min", "crawler_max_per_run", "bg_workers",
            "auto_translate_interval_min", "auto_match_interval_min",
            "enrichment_interval_min", "enrichment_batch_size",
            "image_interval_min", "image_batch_size",
            "llm_match_interval_min", "llm_match_batch_size",
        ):
            try:
                setattr(self, key, max(1, int(value)))
            except (TypeError, ValueError):
                pass
        return True


settings = Settings()
