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
    __tablename__ = "ingredient_alias"
    __table_args__ = (UniqueConstraint("alias", name="uq_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    alias: Mapped[str] = mapped_column(String(200), index=True)
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredient.id", ondelete="CASCADE")
    )
    ingredient: Mapped[Ingredient] = relationship(back_populates="aliases")


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
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    ingredients: Mapped[list["RecipeIngredient"]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
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
