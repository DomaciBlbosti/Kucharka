"""Datový model kuchařky.

ingredient          – kanonická surovina + výživa /100 g
ingredient_alias    – cache mapování volného textu → ingredient (plní normalizer)
recipe              – recept (zdroj, hodnocení, čas, porce, obrázek, video)
recipe_ingredient   – řádek receptu navázaný na kanon + dopočet gramů a kcal
pantry_item         – co mám doma
shopping_item       – ruční položky nákupního seznamu
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Ingredient(Base):
    __tablename__ = "ingredient"

    id: Mapped[int] = mapped_column(primary_key=True)
    name_cs: Mapped[str] = mapped_column(String(200), index=True)
    name_en: Mapped[str | None] = mapped_column(String(200), nullable=True)
    code: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Hierarchická kategorie, např. "maso > drůbeží > kuřecí" (plní kategorizace).
    category_path: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    # Obecnější surovina: "arborio rýže" → "rýže", "olivový olej" → "olej".
    # Díky tomu vybrání „rýže" ve „Vařím z" najde i recept s jasmínovou rýží.
    # Plní jednorázová úloha podle názvů (viz modules/ingredient_tree).
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingredient.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Výživa na 100 g
    kcal_100g: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein_100g: Mapped[float | None] = mapped_column(Float, nullable=True)
    carbs_100g: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat_100g: Mapped[float | None] = mapped_column(Float, nullable=True)
    fiber_100g: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Hustota pro převod objem→hmotnost (g na 1 ml). Voda = 1.0.
    density: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(60), nullable=True)

    aliases: Mapped[list["IngredientAlias"]] = relationship(
        back_populates="ingredient", cascade="all, delete-orphan"
    )


class IngredientAlias(Base):
    __tablename__ = "ingredient_alias"
    __table_args__ = (UniqueConstraint("alias", name="uq_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    alias: Mapped[str] = mapped_column(String(200), index=True)
    ingredient_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingredient.id", ondelete="CASCADE"), nullable=True
    )
    ingredient: Mapped[Ingredient | None] = relationship(back_populates="aliases")

    # Rozšíření pro LLM matching (llm_match.py) – přidává je migrations.py
    # (ALTER TABLE), zde chybělo ORM mapování (stejný problém jako u Recipe výše).
    lookup_key: Mapped[str | None] = mapped_column(String(200), nullable=True, unique=True)
    kind: Mapped[str] = mapped_column(String(20), server_default="food")
    source: Mapped[str] = mapped_column(String(20), server_default="manual")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    verified: Mapped[bool] = mapped_column(server_default="0")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, server_default="0")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MatchDecision(Base):
    """Katalog rozhodnutí LLM/ručního párování surovin.

    Jedna řádka na `lookup_key` (normalizovaný text suroviny). Ukládá se KAŽDÝ
    výsledek dávkového párování – i zamítnutí a chyby – takže:
      * opakované běhy se neptají LLM znovu na už rozhodnuté položky,
      * v administraci je vidět, co a proč bylo rozhodnuto (s confidence),
      * nejisté položky ("suggested"/"no_match") čekají v katalogu na ruční
        potvrzení místo tichého zahození.

    Stavy:
      applied    – alias vytvořen, řádky napárovány (finální)
      nonfood    – není surovina (equipment/garnish/packaging/unknown), finální
      suggested  – LLM má kandidáta, ale confidence < práh → čeká na člověka
      no_match   – LLM nenašlo kandidáta → čeká na člověka
      ignored    – člověk řekl "neřešit" (finální)
      error      – LLM volání selhalo; opakuje se do MAX_ATTEMPTS, pak čeká
    """

    __tablename__ = "match_decision"
    __table_args__ = (UniqueConstraint("lookup_key", name="uq_match_decision_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lookup_key: Mapped[str] = mapped_column(String(200))
    sample_text: Mapped[str] = mapped_column(String(400))
    status: Mapped[str] = mapped_column(String(20), index=True)
    # kategorie z LLM (food/equipment/garnish/packaging/unknown)
    category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ingredient_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingredient.id", ondelete="SET NULL"), nullable=True
    )
    # LLM navrhlo ZALOŽIT novou surovinu s tímhle názvem (v katalogu nebyla).
    # Člověk ji z katalogu založí jedním klikem; s auto_ingredients se
    # zakládá rovnou a tohle slouží jen jako záznam.
    suggested_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # kdo rozhodl: název modelu, nebo 'manual'
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # kolika řádků recipe_ingredient se položka týkala při posledním běhu
    occurrences: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Prošlo kontextovou fází (posouzení v rámci celého receptu)? Každá
    # položka jí projde nejvýš jednou; 'zeptat se znovu' příznak resetuje.
    ctx_tried: Mapped[bool] = mapped_column(server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    ingredient: Mapped[Ingredient | None] = relationship()


class Recipe(Base):
    __tablename__ = "recipe"
    __table_args__ = (UniqueConstraint("source_url", name="uq_source_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    source_url: Mapped[str] = mapped_column(String(600))
    source_domain: Mapped[str | None] = mapped_column(String(160), index=True)

    image_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    video_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)

    servings: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_time: Mapped[int | None] = mapped_column(Integer, nullable=True)  # minuty
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    rating_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str | None] = mapped_column(String(160), nullable=True)

    kcal_per_serving: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # vlastní hodnocení a poznámka uživatele
    user_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # originál před strojovým překladem (uloží se jen když se recept přeložil)
    original_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Název + postup + suroviny prohnané stemmerem (viz modules/textnorm.py).
    # Fulltext index jede nad tímhle sloupcem, ne nad původním textem: jinak
    # by „péct" nenašlo „pečeme" a „kuře" nenašlo „kuřecí".
    search_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Klíč pro seskupení variant téhož jídla („těstovinový salát" ×15).
    # Stemované a seřazené názvy, viz textnorm.title_key.
    title_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    # Skryto uživatelem – z výpisů i z návrhů zmizí, ale recept se nemaže
    # (jde vrátit a crawler ho při dalším průchodu znovu nenatáhne jako nový).
    hidden: Mapped[bool] = mapped_column(Boolean, server_default="0", index=True)
    # Pořadí na úvodní stránce; počítá se na pozadí (viz modules/feed.py).
    feed_score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Stavové sloupce – přidává je migrations.py (ALTER TABLE), zde jen
    # chybělo ORM mapování, takže SQLAlchemy o nich nevědělo (AttributeError
    # při select(Recipe.enrichment_status) apod.), i když ve skutečné DB byly.
    crawl_status: Mapped[str] = mapped_column(String(20), server_default="scraped")
    enrichment_status: Mapped[str] = mapped_column(String(20), server_default="pending", index=True)
    image_status: Mapped[str] = mapped_column(String(20), server_default="pending")
    enrichment_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    enrichment_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_enriched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    local_image_path: Mapped[str | None] = mapped_column(String(400), nullable=True)
    local_thumb_path: Mapped[str | None] = mapped_column(String(400), nullable=True)
    kcal_per_100g: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_weight_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Denormalizovaný počet NAPÁROVANÝCH surovin (ingredient_id IS NOT NULL).
    # Udržuje recompute_recipe_kcal + pojistný přepočet na konci backfillu;
    # historii doplňuje jednorázová migrace. Díky němu výpis receptů nemusí
    # agregovat celou recipe_ingredient (u 150k receptů přes milion řádků)
    # při každém požadavku – to dělalo z hlavní stránky minutovou operaci.
    ing_total: Mapped[int | None] = mapped_column(Integer, nullable=True)

    ingredients: Mapped[list["RecipeIngredient"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )
    tags: Mapped[list["Tag"]] = relationship(
        secondary="recipe_tag", back_populates="recipes"
    )


class RecipeIngredient(Base):
    __tablename__ = "recipe_ingredient"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE"), index=True
    )
    raw_text: Mapped[str] = mapped_column(String(400))
    # originál před strojovým překladem (uloží se jen když se řádek přeložil)
    original_raw_text: Mapped[str | None] = mapped_column(String(400), nullable=True)
    ingredient_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingredient.id"), nullable=True, index=True
    )
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    kcal: Mapped[float | None] = mapped_column(Float, nullable=True)
    optional: Mapped[bool] = mapped_column(default=False)
    # Řádek je rozhodnutá NE-surovina (alobal, "na ozdobu"…): ingredient_id
    # zůstává NULL záměrně (bez kalorií), ale řádek se už NEpočítá mezi
    # "nenapárované" a párování ho přeskakuje. Bez tohohle příznaku se
    # vyřešené ne-suroviny míchaly do počtu čekajících donekonečna.
    nonfood: Mapped[bool] = mapped_column(server_default="0")

    recipe: Mapped[Recipe] = relationship(back_populates="ingredients")
    ingredient: Mapped[Ingredient | None] = relationship()


class PantryItem(Base):
    __tablename__ = "pantry_item"
    __table_args__ = (UniqueConstraint("ingredient_id", name="uq_pantry_ing"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredient.id", ondelete="CASCADE")
    )
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    use_soon: Mapped[bool] = mapped_column(default=False)  # spotřebovat brzy
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    ingredient: Mapped[Ingredient] = relationship()


class ShoppingItem(Base):
    __tablename__ = "shopping_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(200))
    ingredient_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingredient.id"), nullable=True
    )
    checked: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ingredient: Mapped[Ingredient | None] = relationship()


class RecipeEmbedding(Base):
    """Vektorový embedding receptu (pro RAG generování). vec = float32 bytes."""

    __tablename__ = "recipe_embedding"

    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(80))
    dim: Mapped[int] = mapped_column(Integer)
    vec: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class IngredientEmbedding(Base):
    """Vektorový embedding suroviny (pro dynamický katalog v llm_match.py).

    Nahrazuje statický top-N katalog seřazený podle popularity – ten
    systematicky vynechává vzácné/neobvyklé suroviny, takže je LLM nikdy
    nemůže trefit. Místo toho se pro každou dávku vybere sémanticky
    nejbližší podmnožina (viz modules/ingredient_embed.py).
    """

    __tablename__ = "ingredient_embedding"

    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredient.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(80))
    dim: Mapped[int] = mapped_column(Integer)
    vec: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class AppSetting(Base):
    """Runtime nastavení (override env), editovatelné z administrace."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class MealPlanEntry(Base):
    """Položka jídelníčku – recept naplánovaný na konkrétní den a chod."""

    __tablename__ = "meal_plan_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[Date] = mapped_column(Date, index=True)
    meal: Mapped[str] = mapped_column(String(20), default="oběd")  # snídaně/svačina/oběd/večeře
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE"), index=True
    )
    servings: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    recipe: Mapped[Recipe] = relationship()


class BarcodeMap(Base):
    """Naučené mapování EAN/UPC kódu na kanonickou surovinu (skenování při vybalování nákupu)."""

    __tablename__ = "barcode_map"

    barcode: Mapped[str] = mapped_column(String(32), primary_key=True)
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredient.id", ondelete="CASCADE"), index=True
    )
    off_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    ingredient: Mapped[Ingredient] = relationship()


class Tag(Base):
    """Kanonický tag receptu v jmenném prostoru (chod/denní doba/chuť/technika/dieta/kuchyně)."""

    __tablename__ = "tag"
    __table_args__ = (UniqueConstraint("namespace", "slug", name="uq_tag_ns_slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    namespace: Mapped[str] = mapped_column(String(20), index=True)
    slug: Mapped[str] = mapped_column(String(60))
    label_cs: Mapped[str] = mapped_column(String(80))

    recipes: Mapped[list[Recipe]] = relationship(secondary="recipe_tag", back_populates="tags")


class RecipeTag(Base):
    __tablename__ = "recipe_tag"

    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True
    )
    # Rozlišuje původ tagu: 'auto' (classifier.py, heuristika bez LLM) vs.
    # LLM přiřazení z tagging.py. Používá classifier.py, ale sloupec tu
    # chyběl – stejný vzor jako Recipe/IngredientAlias výše v souboru.
    source: Mapped[str] = mapped_column(String(20), server_default="auto")


class CrawlUrl(Base):
    """Persistentní fronta URL objevených ze sitemap – nahrazuje dřívější
    "náhodně zamíchej a zkus" přístup. Každá URL se objeví v téhle tabulce
    jen jednou (bez ohledu na to, jak dopadla), takže crawler ví, co už
    zkoušel a s jakým výsledkem – neopakuje donekonečna stejné neúspěchy a
    dá se z toho udělat přehledová tabulka v adminu."""

    __tablename__ = "crawl_url"
    __table_args__ = (UniqueConstraint("url", name="uq_crawl_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(160), index=True)
    url: Mapped[str] = mapped_column(String(600))
    # pending = čeká na zpracování, ok = recept uložen, skip = nebyl to
    # recept / už existoval, error = zpracování selhalo (viz `error`)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    recipe_id: Mapped[int | None] = mapped_column(
        ForeignKey("recipe.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    recipe: Mapped[Recipe | None] = relationship()


class CrawlDomainState(Base):
    """Kdy byla naposledy synchronizovaná sitemapa dané domény – ať se
    nestahuje a neparsuje celá sitemapa (u velkých webů klidně tisíce URL)
    znovu při každém běhu crawleru."""

    __tablename__ = "crawl_domain_state"

    domain: Mapped[str] = mapped_column(String(160), primary_key=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_added: Mapped[int] = mapped_column(Integer, default=0)
    sitemap_urls_total: Mapped[int] = mapped_column(Integer, default=0)


class LidlAccount(Base):
    """Napojený Lidl Plus účet (může jich být víc – např. účet + účet manželky).

    `refresh_token` se získává JEDNORÁZOVĚ mimo appku (na PC s prohlížečem,
    přes CLI nástroj `lidl-plus auth` – login přes app appky vyžaduje reálný
    browser kvůli OAuth/2FA flow, což v tomhle Docker image není a nechceme
    tam tahat Chromium). Jakmile je token uložený, veškerá další komunikace
    (obnovení tokenu, seznam účtenek, detail účtenky) je čisté REST/JSON.
    """

    __tablename__ = "lidl_account"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(80))  # např. "Aleš", "manželka"
    country: Mapped[str] = mapped_column(String(5), default="CZ")
    language: Mapped[str] = mapped_column(String(5), default="cs")
    refresh_token: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LidlReceipt(Base):
    """Záznam už zpracované účtenky – pojistka proti opakovanému importu
    stejného nákupu do spíže při každém sync běhu."""

    __tablename__ = "lidl_receipt"
    __table_args__ = (UniqueConstraint("account_id", "ticket_id", name="uq_lidl_ticket"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("lidl_account.id", ondelete="CASCADE"), index=True
    )
    ticket_id: Mapped[str] = mapped_column(String(80))
    purchased_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    items_matched: Mapped[int] = mapped_column(Integer, default=0)
    items_unmatched: Mapped[int] = mapped_column(Integer, default=0)
    imported_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LlmCall(Base):
    """Jedno LLM volání dávkové úlohy – kolik stálo tokenů, jak dlouho trvalo
    a jestli prošlo. Slouží ke sledování spotřeby a spolehlivosti (Admin →
    Spotřeba LLM); appka podle něj nic neřídí, takže případný výpadek zápisu
    nesmí volání shodit (viz llm_stats.record).

    Staré záznamy maže `llm_stats.prune` – tabulka je čistě provozní telemetrie
    a nemá růst donekonečna.
    """

    __tablename__ = "llm_call"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    # kdo si o volání řekl: preklad / kategorie / tagy / parovani / …
    component: Mapped[str] = mapped_column(String(40), index=True)
    provider: Mapped[str] = mapped_column(String(20))   # ollama | api
    model: Mapped[str] = mapped_column(String(120))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class RecipeReview(Base):
    """Ruční kontrola receptu – co u něj člověk viděl a rozhodl.

    Vzniklo z potřeby projít korpus očima: metriky z `corpus_audit` řeknou,
    KOLIK receptů je podezřelých, ale co s konkrétním receptem je, pozná až
    člověk. Kontrola se dělá v appce (záložka Kontrola) a tady se drží její
    výsledek, aby se stejný recept nemusel posuzovat dvakrát.

    Štítky jsou z pevné nabídky (viz modules/review.LABELS) a ukládají se jako
    seznam oddělený čárkami – recept jich může mít víc najednou („zkontrolováno"
    plus „špatný překlad"). Vlastní tabulka místo `Tag`: tamty tagy popisují
    JÍDLO (chod, technika, dieta) a přepisuje je automatické tagování, kdežto
    tohle je poznámka o zpracování dat a nesmí ji nic přepsat.
    """

    __tablename__ = "recipe_review"
    __table_args__ = (UniqueConstraint("recipe_id", name="uq_review_recipe"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE"), index=True
    )
    labels: Mapped[str] = mapped_column(String(300), server_default="")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    recipe: Mapped[Recipe] = relationship()
