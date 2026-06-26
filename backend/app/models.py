"""Datový model kuchařky.

ingredient                  – kanonická surovina + výživa /100 g
ingredient_alias            – slovník: lookup_key (lemmatizovaný norm. tvar) → ingredient
                              + kind (food/equipment/…) + source/confidence/verified
recipe                      – recept (zdroj, hodnocení, čas, porce, obrázek, video)
                              + crawl/enrichment/image status, lokální obrázek
recipe_ingredient           – řádek receptu navázaný na kanon + dopočet gramů a kcal
recipe_override             – per-recept ruční úpravy (název, instrukce, poznámky)
recipe_ingredient_override  – per-řádek ruční úpravy (remap surovin, vyloučení)
crawl_source                – tracking sitemap (ETag, lastmod) per doména
pantry_item                 – co mám doma
shopping_item               – ruční položky nákupního seznamu
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
    """Slovník překladu volného textu → kanonická surovina.

    `alias`       – původní raw klíč (legacy, plněný `_clean_name`)
    `lookup_key`  – nový lemmatizovaný normalizovaný tvar (simplemma), primární klíč hledání
    `kind`        – 'food' (mapuje na ingredient), nebo 'equipment'/'packaging'/'garnish'/'unknown'
                    (ingredient_id pak NULL, řádek se v receptu označí, ale nepočítá do nutričních dat)
    `source`      – kdo entry vytvořil: 'manual' / 'llm' / 'import'
    `confidence`  – jistota LLM matche (0–1), NULL u manual/import
    `verified`    – potvrzeno (ručně, nebo opakovanou LLM shodou) — kandidát na trvalé použití
    """

    __tablename__ = "ingredient_alias"
    __table_args__ = (
        UniqueConstraint("alias", name="uq_alias"),
        UniqueConstraint("lookup_key", name="uq_lookup_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alias: Mapped[str] = mapped_column(String(200), index=True)
    lookup_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    ingredient_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingredient.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(20), server_default="food")
    source: Mapped[str] = mapped_column(String(20), server_default="manual")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, server_default="0")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, server_default="0")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    ingredient: Mapped[Ingredient | None] = relationship(back_populates="aliases")


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
    kcal_per_100g: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_weight_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Stavy zpracování (pipeline ve fázích)
    crawl_status: Mapped[str] = mapped_column(String(20), server_default="scraped", index=True)
    # 'pending' (čeká na párování) / 'matching' / 'done' / 'manual_review' / 'failed'
    enrichment_status: Mapped[str] = mapped_column(String(20), server_default="pending", index=True)
    # 'pending' / 'downloading' / 'downloaded' / 'failed' / 'none' (žádný image_url)
    image_status: Mapped[str] = mapped_column(String(20), server_default="pending", index=True)
    enrichment_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    enrichment_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_enriched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Lokálně stažené obrázky (relativní cesty pod /data/images/)
    local_image_path: Mapped[str | None] = mapped_column(String(400), nullable=True)
    local_thumb_path: Mapped[str | None] = mapped_column(String(400), nullable=True)

    ingredients: Mapped[list["RecipeIngredient"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )
    override: Mapped["RecipeOverride | None"] = relationship(
        back_populates="recipe", uselist=False, cascade="all, delete-orphan"
    )
    tags: Mapped[list["Tag"]] = relationship(
        secondary="recipe_tag", back_populates="recipes", lazy="selectin"
    )


class RecipeIngredient(Base):
    __tablename__ = "recipe_ingredient"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE"), index=True
    )
    raw_text: Mapped[str] = mapped_column(String(400))
    ingredient_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingredient.id"), nullable=True, index=True
    )
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    kcal: Mapped[float | None] = mapped_column(Float, nullable=True)
    optional: Mapped[bool] = mapped_column(default=False)

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


class RecipeOverride(Base):
    """Per-recept ruční úpravy. Read endpointy vracejí coalesce(override.X, recipe.X).

    Přežije re-crawl: pokud crawler recept aktualizuje, override hodnoty zůstávají.
    """

    __tablename__ = "recipe_override"

    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE"), primary_key=True
    )
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    servings: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str | None] = mapped_column(String(160), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    recipe: Mapped[Recipe] = relationship(back_populates="override")


class RecipeIngredientOverride(Base):
    """Per-řádek ruční úprava: přemapování suroviny, ručně zadané množství, nebo vyloučení."""

    __tablename__ = "recipe_ingredient_override"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("recipe_ingredient.id", ondelete="CASCADE"), unique=True
    )
    ingredient_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingredient.id", ondelete="SET NULL"), nullable=True
    )
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    excluded: Mapped[bool] = mapped_column(Boolean, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CrawlSource(Base):
    """Stav crawlování per doména. Umožní conditional GET (ETag/Last-Modified)
    a inkrementální filtr přes <lastmod> v sitemapě."""

    __tablename__ = "crawl_source"

    domain: Mapped[str] = mapped_column(String(160), primary_key=True)
    sitemap_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(200), nullable=True)
    http_last_modified: Mapped[str | None] = mapped_column(String(60), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_lastmod: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_seen: Mapped[int] = mapped_column(Integer, server_default="0")
    total_ingested: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Tag(Base):
    """Kanonický tag s namespace. Namespace odděluje různé osy klasifikace:
    `course`, `flavor`, `meal`, `technique`, `diet`, `cuisine`.

    Slug je interní (anglicky, snake_case); label_cs je co se ukáže v UI.
    """

    __tablename__ = "tag"
    __table_args__ = (UniqueConstraint("namespace", "slug", name="uq_tag_ns_slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    namespace: Mapped[str] = mapped_column(String(20), index=True)
    slug: Mapped[str] = mapped_column(String(40))
    label_cs: Mapped[str] = mapped_column(String(80))

    recipes: Mapped[list[Recipe]] = relationship(
        secondary="recipe_tag", back_populates="tags"
    )


class RecipeTag(Base):
    """Many-to-many: recipe ↔ tag. Pure association table."""

    __tablename__ = "recipe_tag"

    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("recipe.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True
    )
    # Zdroj značky — pomáhá rozlišit, co můžeme automaticky přepsat při re-enrichmentu.
    source: Mapped[str] = mapped_column(String(20), server_default="auto")
    # 'auto' / 'manual' / 'llm'
