"""Pydantic schémata pro API."""
from __future__ import annotations

from datetime import date as _date

from pydantic import BaseModel, ConfigDict


class IngredientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name_cs: str
    name_en: str | None = None
    category: str | None = None
    category_path: str | None = None
    kcal_100g: float | None = None


class RecipeIngredientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    raw_text: str
    ingredient_id: int | None = None
    amount: float | None = None
    unit: str | None = None
    grams: float | None = None
    kcal: float | None = None


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    namespace: str
    slug: str
    label_cs: str


class RecipeCard(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    source_domain: str | None = None
    image_url: str | None = None
    local_image_path: str | None = None
    local_thumb_path: str | None = None
    servings: int | None = None
    total_time: int | None = None
    rating: float | None = None
    rating_count: int | None = None
    kcal_per_serving: float | None = None
    kcal_per_100g: float | None = None
    total_weight_g: float | None = None
    enrichment_status: str | None = None
    image_status: str | None = None
    tags: list[TagOut] = []
    # dopočítané vůči spíži
    have: int = 0
    total: int = 0
    missing_count: int = 0
    ratio: float = 0.0


class RecipeDetail(RecipeCard):
    source_url: str
    video_url: str | None = None
    instructions: str | None = None
    category: str | None = None
    ingredients: list[RecipeIngredientOut] = []
    missing_ingredient_ids: list[int] = []


class PantryItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ingredient_id: int
    amount: float | None = None
    unit: str | None = None
    ingredient: IngredientOut


class PantryAdd(BaseModel):
    ingredient_id: int
    amount: float | None = None
    unit: str | None = None


class ShoppingItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    label: str
    ingredient_id: int | None = None
    checked: bool = False


class ShoppingAdd(BaseModel):
    label: str
    ingredient_id: int | None = None


class IngestRequest(BaseModel):
    url: str


class SearchRequest(BaseModel):
    query: str


class MealPlanAdd(BaseModel):
    date: _date
    meal: str = "oběd"
    recipe_id: int
    servings: int = 1


class MealPlanUpdate(BaseModel):
    date: _date | None = None
    meal: str | None = None
    servings: int | None = None


class MealPlanEntryOut(BaseModel):
    id: int
    date: _date
    meal: str
    servings: int
    recipe_id: int
    title: str
    image_url: str | None = None
    kcal_per_serving: float | None = None
    kcal: float | None = None  # kcal_per_serving * servings


class PlanRange(BaseModel):
    start: _date
    days: int = 7


class SuggestRequest(BaseModel):
    start: _date
    days: int = 7
    meals: list[str] = ["snídaně", "svačina", "oběd", "večeře"]
    daily_kcal: int | None = None
    preferences: str = ""


class ApplyEntry(BaseModel):
    date: _date
    meal: str
    recipe_id: int
    servings: int = 1


class ApplyRequest(BaseModel):
    start: _date
    days: int = 7
    entries: list[ApplyEntry] = []
    replace_range: bool = True
