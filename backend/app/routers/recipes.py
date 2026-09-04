"""API pro recepty – výpis s filtry vůči spíži + detail."""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import (
    String, and_, case, cast, func, inspect as sa_inspect, literal, or_, select, text,
)
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..db import get_db
from ..models import Ingredient, PantryItem, Recipe, RecipeIngredient, RecipeTag, Tag
from ..modules.pantry import pantry_ingredient_ids, recipe_availability
from ..modules.nutrition import recompute_recipe_kcal
from ..modules import photo_recipe, textnorm
from ..modules.ingest import persist as persist_recipe
from ..seed.starter_tags import NAMESPACE_LABELS
from ..schemas import RecipeCard, RecipeDetail, RecipeEdit, RecipeListOut

router = APIRouter(prefix="/api/recipes", tags=["recipes"])


def _availability_cols(have_ids: set[int]):
    """Dostupnost receptu vůči spíži přímo v SQL – ale LEVNĚ.

    `total` (počet napárovaných surovin) čteme z denormalizovaného
    `recipe.ing_total` (udržuje recompute_recipe_kcal + pojistka v backfillu),
    `have` agregujeme JEN přes řádky se surovinami ze spíže (index
    ix_ri_ingredient_recipe). Dřívější verze dělala GROUP BY přes CELOU
    recipe_ingredient (u 150k receptů přes milion řádků) při každém
    požadavku – hlavní stránka se pak načítala i minutu.

    Vrací (subquery|None, total_col, have_col, missing_col); subquery je None
    při prázdné spíži (pak není co joinovat). Výrazy jdou použít ve WHERE i
    ORDER BY, takže filtry a smart řazení běží v DB a LIMIT/OFFSET stránkuje.
    """
    total_col = func.coalesce(Recipe.ing_total, 0)
    if have_ids:
        sub = (
            select(
                RecipeIngredient.recipe_id.label("recipe_id"),
                func.count().label("have"),
            )
            .where(RecipeIngredient.ingredient_id.in_(have_ids))
            .group_by(RecipeIngredient.recipe_id)
            .subquery()
        )
        have_col = func.coalesce(sub.c.have, 0)
    else:
        sub = None
        have_col = literal(0)
    missing_col = total_col - have_col
    return sub, total_col, have_col, missing_col


# Fulltext hledání: MATCH..AGAINST na MariaDB (řádově rychlejší než LIKE-scan
# a hledá i v postupu), fallback na ILIKE pro SQLite / krátké dotazy / chybějící
# index. Index se staví na POZADÍ po startu (viz migrations), takže "není" je
# jen dočasný stav – dokud není hotový, kontroluje se znovu max. 1× za minutu
# a hledání mezitím jede přes ILIKE.
_FT_RECHECK_S = 60.0
_ft_state: dict = {"index": None, "checked_at": 0.0}

# Který fulltext index je k dispozici. Nad `search_text` (normalizovaný text)
# je hledání odolné vůči skloňování, nad title+instructions ne – ten zůstává
# jako mezistupeň, dokud se nový index nedostaví.
_FT_SEARCH_TEXT = "ft_recipe_search_text"
_FT_LEGACY = "ft_recipe_title_instructions"


def _fulltext_index(db: Session) -> str | None:
    """Název použitelného fulltext indexu, nebo None (→ ILIKE fallback)."""
    import time as _time

    if _ft_state["index"] == _FT_SEARCH_TEXT:
        return _FT_SEARCH_TEXT  # lepší už nebude, nemá smysl se ptát znovu
    now = _time.monotonic()
    if now - _ft_state["checked_at"] < _FT_RECHECK_S and _ft_state["checked_at"]:
        return _ft_state["index"]
    found = None
    try:
        bind = db.get_bind()
        if bind.dialect.name in ("mysql", "mariadb"):
            names = {ix["name"] for ix in sa_inspect(bind).get_indexes("recipe")}
            if _FT_SEARCH_TEXT in names:
                found = _FT_SEARCH_TEXT
            elif _FT_LEGACY in names:
                found = _FT_LEGACY
    except Exception:  # noqa: BLE001
        found = None
    _ft_state.update(index=found, checked_at=now)
    return found


def _search_clause(db: Session, q: str):
    """WHERE podmínka pro hledání: fulltext (boolean mode, prefixy), jinak LIKE.

    Nad `search_text` se dotaz prožene stejným stemmerem jako uložený text,
    takže „péct" najde „pečeme" a „kuřecí prsa" najde „kuřecích prsou".
    Prefix `*` zůstává – doříká to, co stemmer neuhlídá.
    """
    index = _fulltext_index(db)
    if index == _FT_SEARCH_TEXT:
        terms = [t for t in textnorm.tokens(q) if len(t) >= 3]
        if terms:
            return text(
                "MATCH(recipe.search_text) AGAINST (:ftq IN BOOLEAN MODE)"
            ).bindparams(ftq=" ".join(f"+{t}*" for t in terms))
    elif index == _FT_LEGACY:
        # InnoDB fulltext ignoruje tokeny kratší než ~3 znaky – ty by dotaz
        # jen tiše vyprázdnily, proto krátká slova vynecháváme; když nezbude
        # nic, spadneme na LIKE.
        cleaned = (re.sub(r"[+*<>()~@\"'-]", "", t) for t in re.split(r"\s+", q.strip()))
        terms = [t for t in cleaned if len(t) >= 3]
        if terms:
            boolean_q = " ".join(f"+{t}*" for t in terms)
            return text(
                "MATCH(recipe.title, recipe.instructions) AGAINST (:ftq IN BOOLEAN MODE)"
            ).bindparams(ftq=boolean_q)
    # Bez fulltextu (SQLite, rozestavěný index): hledá se v normalizovaném
    # sloupci přes LIKE. Skloňování to zvládne stejně, jen pomaleji.
    # Shoda v názvu se přidává jako OR kvůli receptům, které search_text
    # ještě nemají naplněný (běží backfill) – jinak by dočasně zmizely.
    norm = textnorm.normalize(q)
    by_title = Recipe.title.ilike(f"%{q}%")
    if not norm:
        return by_title
    return or_(and_(*[Recipe.search_text.like(f"%{t}%") for t in norm.split()]),
               by_title)


def _tags_by_recipe(db: Session, recipe_ids: list[int]) -> dict[int, list[Tag]]:
    """Tagy jen pro danou stránku receptů (ne pro celou DB)."""
    if not recipe_ids:
        return {}
    rows = db.execute(
        select(RecipeTag.recipe_id, Tag)
        .join(Tag, RecipeTag.tag_id == Tag.id)
        .where(RecipeTag.recipe_id.in_(recipe_ids))
    ).all()
    out: dict[int, list[Tag]] = {}
    for rid, tag in rows:
        out.setdefault(rid, []).append(tag)
    return out


@router.get("", response_model=RecipeListOut)
def list_recipes(
    db: Session = Depends(get_db),
    q: str | None = Query(None, description="hledání v názvu, postupu i surovinách"),
    only_have: bool = Query(False, description="jen co můžu uvařit teď"),
    max_missing: int | None = Query(None, ge=0),
    max_kcal: float | None = Query(None, ge=0),
    max_time: int | None = Query(None, ge=0),
    min_rating: float | None = Query(None, ge=0, le=5),
    category: str | None = Query(None, description="recepty se surovinou z kategorie"),
    tags: list[str] = Query(default=[], description="filtr 'namespace:slug' – víc namespace = AND, víc tagů v jednom = OR"),
    sort: str = Query("feed", pattern="^(feed|smart|rating|time|kcal|newest)$"),
    group: bool = Query(False, description="sloučit varianty téhož jídla do kategorie"),
    show_hidden: bool = Query(False, description="ukázat i ručně skryté recepty"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    have = pantry_ingredient_ids(db)
    _sub, total_col, have_col, missing_col = _availability_cols(have)

    # Vypnutá spíž: dostupnost není o co opřít, takže filtry i řazení podle
    # ní nedávají smysl. Místo prázdného výsledku se prostě ignorují a
    # „Nejblíž uvaření" spadne na doporučené pořadí.
    if not settings.pantry_enabled:
        only_have = False
        max_missing = None
        if sort == "smart":
            sort = "feed"

    # Obyčejné filtry (bez dostupnosti) – aplikují se na hlavní dotaz i na
    # levný COUNT, který díky tomu nemusí joinovat agregaci spíže.
    conds = []
    if not show_hidden:
        conds.append(Recipe.hidden.is_(False))
    if q:
        conds.append(_search_clause(db, q))
    if max_kcal is not None:
        conds.append(Recipe.kcal_per_serving <= max_kcal)
    if max_time is not None:
        conds.append(Recipe.total_time <= max_time)
    if min_rating is not None:
        conds.append(Recipe.rating >= min_rating)
    if category:
        sub = (
            select(RecipeIngredient.recipe_id)
            .join(Ingredient, RecipeIngredient.ingredient_id == Ingredient.id)
            .where(Ingredient.category_path.ilike(f"{category}%"))
        )
        conds.append(Recipe.id.in_(sub))
    if tags:
        by_ns: dict[str, list[str]] = {}
        for t in tags:
            if ":" not in t:
                continue
            ns, slug = t.split(":", 1)
            by_ns.setdefault(ns, []).append(slug)
        for ns, slugs in by_ns.items():
            sub = (
                select(RecipeTag.recipe_id)
                .join(Tag, RecipeTag.tag_id == Tag.id)
                .where(Tag.namespace == ns, Tag.slug.in_(slugs))
            )
            conds.append(Recipe.id.in_(sub))

    # Recept bez JEDINÉ napárované suroviny má missing = 0, což ale neznamená
    # „můžeš vařit" – znamená to „nevíme, z čeho je". Do filtrů dostupnosti
    # proto nesmí; jinak úvodní stránku obsadily návody na zdobení dortů.
    cookable = total_col > 0

    avail_conds = []
    if only_have:
        avail_conds.extend([cookable, missing_col == 0])
    if max_missing is not None:
        avail_conds.extend([cookable, missing_col <= max_missing])

    base = select(
        Recipe, total_col.label("total"), have_col.label("have"),
        missing_col.label("missing_count"),
    )
    if _sub is not None:
        base = base.outerjoin(_sub, _sub.c.recipe_id == Recipe.id)
    base = base.where(*conds).where(*avail_conds)

    if group:
        return _grouped_page(
            db, conds, avail_conds, _sub, base,
            missing_col=missing_col, sort=sort, limit=limit, offset=offset,
        )

    # Celkový počet (stejné filtry, bez řazení/limitu) – pro "Načíst další" v UI.
    # Bez filtru na dostupnost stačí COUNT přímo přes recipe (žádný join).
    if avail_conds:
        total_count = db.scalar(
            select(func.count()).select_from(base.order_by(None).subquery())
        ) or 0
    else:
        total_count = db.scalar(
            select(func.count()).select_from(Recipe).where(*conds)
        ) or 0

    if sort == "feed":
        # Předpočítané skóre (viz modules/feed.py) – čte se přes index,
        # netřídí se celá tabulka a nezávisí to na spíži.
        base = base.order_by(
            func.coalesce(Recipe.feed_score, -999).desc(), Recipe.id.desc()
        )
    elif sort == "rating":
        base = base.order_by(func.coalesce(Recipe.rating, 0).desc())
    elif sort == "time":
        base = base.order_by(func.coalesce(Recipe.total_time, 9999).asc())
    elif sort == "kcal":
        base = base.order_by(func.coalesce(Recipe.kcal_per_serving, 1_000_000_000).asc())
    elif sort == "newest":
        base = base.order_by(Recipe.id.desc())
    else:  # smart: nejdřív ty, u kterých vůbec víme z čeho jsou, pak nejmíň
        # chybějících surovin a nakonec hodnocení
        base = base.order_by(
            case((cookable, 0), else_=1).asc(),
            missing_col.asc(),
            func.coalesce(Recipe.rating, 0).desc(),
        )

    rows = db.execute(base.limit(limit).offset(offset)).all()
    tags_map = _tags_by_recipe(db, [r.Recipe.id for r in rows])
    items = [_card(r, tags_map) for r in rows]
    return RecipeListOut(items=items, total=total_count, limit=limit, offset=offset)


def _card(row, tags_map: dict[int, list[Tag]], *,
          group_key: str | None = None, variants: int = 1) -> RecipeCard:
    """Řádek dotazu (Recipe + dopočtená dostupnost) → karta pro výpis."""
    recipe = row.Recipe
    return RecipeCard(
        id=recipe.id,
        title=recipe.title,
        source_domain=recipe.source_domain,
        image_url=recipe.image_url,
        servings=recipe.servings,
        total_time=recipe.total_time,
        rating=recipe.rating,
        rating_count=recipe.rating_count,
        kcal_per_serving=recipe.kcal_per_serving,
        tags=tags_map.get(recipe.id, []),
        have=row.have,
        total=row.total,
        missing_count=row.missing_count,
        ratio=round(row.have / row.total, 3) if row.total else 0.0,
        group_key=group_key,
        variants=variants,
    )


def _grouped_page(db: Session, conds, avail_conds, avail_sub, base, *,
                  missing_col, sort: str, limit: int, offset: int) -> RecipeListOut:
    """Výpis po kategoriích: jedna karta na `title_key`, s počtem variant.

    Dvoukrokově, ať to jde stránkovat a přitom zůstane u dvou dotazů:
      1. GROUP BY title_key → klíče na této stránce (+ počet variant),
      2. dotažení receptů těchto klíčů a výběr reprezentanta v Pythonu.
    Krok 2 je levný: shluky mají jednotky členů (nejvíc 15 v korpusu).

    Recepty bez klíče (název bez slov, nedopočítaná migrace) dostanou vlastní
    umělý klíč "#id", takže jdou do výpisu samostatně. Bez toho by NULL/''
    byla jedna obří kategorie, nebo by z výpisu vypadly úplně.
    """
    key_col = case(
        (or_(Recipe.title_key.is_(None), Recipe.title_key == ""),
         literal("#") + cast(Recipe.id, String)),
        else_=Recipe.title_key,
    )

    keys_q = select(key_col.label("k"), func.count().label("variants"))
    if avail_sub is not None:
        keys_q = keys_q.outerjoin(avail_sub, avail_sub.c.recipe_id == Recipe.id)
    keys_q = keys_q.where(*conds).where(*avail_conds).group_by(key_col)

    if sort == "feed":
        keys_q = keys_q.order_by(func.max(func.coalesce(Recipe.feed_score, -999)).desc())
    elif sort == "rating":
        keys_q = keys_q.order_by(func.max(func.coalesce(Recipe.rating, 0)).desc())
    elif sort == "time":
        keys_q = keys_q.order_by(func.min(func.coalesce(Recipe.total_time, 9999)).asc())
    elif sort == "kcal":
        keys_q = keys_q.order_by(
            func.min(func.coalesce(Recipe.kcal_per_serving, 1_000_000_000)).asc())
    elif sort == "newest":
        keys_q = keys_q.order_by(func.max(Recipe.id).desc())
    else:  # smart: kategorie, kde chybí nejmíň surovin, pak nejlíp hodnocené
        keys_q = keys_q.order_by(
            func.min(case((func.coalesce(Recipe.ing_total, 0) > 0, 0), else_=1)).asc(),
            func.min(missing_col).asc(),
            func.max(func.coalesce(Recipe.rating, 0)).desc(),
        )
    keys_q = keys_q.order_by(key_col.asc())  # stabilní pořadí při shodě

    total_count = db.scalar(
        select(func.count()).select_from(keys_q.order_by(None).subquery())
    ) or 0
    page = db.execute(keys_q.limit(limit).offset(offset)).all()
    keys = [p.k for p in page]
    variants = {p.k: p.variants for p in page}
    if not keys:
        return RecipeListOut(items=[], total=total_count, limit=limit, offset=offset)

    rows = db.execute(base.where(key_col.in_(keys))).all()
    tags_map = _tags_by_recipe(db, [r.Recipe.id for r in rows])

    # Reprezentant kategorie: nejmíň chybějících surovin, pak nejlepší
    # hodnocení, pak nejstarší záznam – ať se karta mezi načteními nemění.
    best: dict[str, object] = {}
    for r in rows:
        k = r.Recipe.title_key or f"#{r.Recipe.id}"
        cur = best.get(k)
        rank = (r.missing_count, -(r.Recipe.rating or 0), r.Recipe.id)
        if cur is None or rank < cur[0]:
            best[k] = (rank, r)

    # Umělý klíč "#id" ven nepatří – navenek je to prostě samostatný recept.
    items = [
        _card(best[k][1], tags_map,
              group_key=None if k.startswith("#") else k,
              variants=variants[k])
        for k in keys if k in best
    ]
    return RecipeListOut(items=items, total=total_count, limit=limit, offset=offset)


class HiddenSet(BaseModel):
    hidden: bool = True


@router.patch("/{recipe_id}/hidden", response_model=RecipeDetail)
def set_hidden(recipe_id: int, req: HiddenSet, db: Session = Depends(get_db)):
    """Skryj (nebo vrať) recept. Nemaže se – jen zmizí z výpisů a z návrhů.

    Mazání by nepomohlo: crawler by recept při dalším průchodu stáhl znovu
    jako nový. Skrytí je trvalé a vratné.
    """
    r = db.get(Recipe, recipe_id)
    if r is None:
        raise HTTPException(404, "Recept nenalezen.")
    r.hidden = bool(req.hidden)
    db.commit()
    return get_recipe(recipe_id, db)


@router.get("/groups/{key}", response_model=RecipeListOut)
def list_group(
    key: str,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Varianty jedné kategorie – všechny recepty se stejným `title_key`.

    Deklarováno PŘED `/{recipe_id}`: to bere int a "groups" by na něm
    skončilo jako 422 místo tohohle endpointu.
    """
    have = pantry_ingredient_ids(db)
    sub, total_col, have_col, missing_col = _availability_cols(have)
    q = select(
        Recipe, total_col.label("total"), have_col.label("have"),
        missing_col.label("missing_count"),
    )
    if sub is not None:
        q = q.outerjoin(sub, sub.c.recipe_id == Recipe.id)
    q = q.where(Recipe.title_key == key)

    total_count = db.scalar(
        select(func.count()).select_from(Recipe).where(Recipe.title_key == key)
    ) or 0
    rows = db.execute(
        q.order_by(missing_col.asc(), func.coalesce(Recipe.rating, 0).desc(),
                   Recipe.id.asc())
        .limit(limit).offset(offset)
    ).all()
    tags_map = _tags_by_recipe(db, [r.Recipe.id for r in rows])
    items = [_card(r, tags_map, group_key=key, variants=total_count) for r in rows]
    return RecipeListOut(items=items, total=total_count, limit=limit, offset=offset)


@router.get("/tags")
def list_tags(db: Session = Depends(get_db)):
    """Kanonické tagy seskupené podle jmenného prostoru, s počtem receptů – pro filtr."""
    all_tags = db.scalars(select(Tag)).all()
    counts = dict(
        db.execute(select(RecipeTag.tag_id, func.count()).group_by(RecipeTag.tag_id)).all()
    )
    by_ns: dict[str, list[dict]] = {}
    for t in all_tags:
        by_ns.setdefault(t.namespace, []).append(
            {"slug": t.slug, "label": t.label_cs, "count": counts.get(t.id, 0)}
        )
    return [
        {
            "namespace": ns,
            "label": NAMESPACE_LABELS.get(ns, ns),
            "tags": sorted(items, key=lambda x: x["label"]),
        }
        for ns, items in sorted(by_ns.items())
    ]


class TagsSet(BaseModel):
    tags: list[str]  # "namespace:slug"


@router.put("/{recipe_id}/tags", response_model=RecipeDetail)
def set_recipe_tags(recipe_id: int, req: TagsSet, db: Session = Depends(get_db)):
    r = db.scalar(
        select(Recipe).where(Recipe.id == recipe_id).options(selectinload(Recipe.tags))
    )
    if r is None:
        raise HTTPException(404, "Recept nenalezen.")
    all_tags = {f"{t.namespace}:{t.slug}": t for t in db.scalars(select(Tag)).all()}
    r.tags = [all_tags[key] for key in req.tags if key in all_tags]
    db.commit()
    return get_recipe(recipe_id, db)


@router.get("/cook-from", response_model=list[RecipeCard])
def cook_from(
    ingredient_ids: list[int] = Query(default=[], description="suroviny, ze kterých chci vařit"),
    limit: int = Query(60, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Recepty, které využijí vybrané suroviny – seřazené podle nejmenšího doplnění.

    Skóre se počítá stejně jako dostupnost vůči spíži, jen místo spíže
    bereme vybrané suroviny: have = kolik klíčových surovin receptu pokrývá
    výběr, missing_count = kolik by ještě bylo třeba dokoupit.
    """
    if not ingredient_ids:
        return []
    sel = set(ingredient_ids)

    # Nejdřív v SQL zúžit na recepty, co aspoň JEDNU vybranou surovinu vůbec
    # obsahují – dřív se tahalo úplně všech ~150k receptů se všemi
    # ingrediencemi jen proto, aby se pak 99 % z nich v Pythonu zahodilo.
    candidate_ids = select(RecipeIngredient.recipe_id).where(
        RecipeIngredient.ingredient_id.in_(sel)
    ).distinct()

    _sub, total_col, have_col, missing_col = _availability_cols(sel)
    stmt = (
        select(Recipe, total_col.label("total"), have_col.label("have"), missing_col.label("missing_count"))
        .outerjoin(_sub, _sub.c.recipe_id == Recipe.id)
        .where(Recipe.id.in_(candidate_ids))
        .where(have_col > 0)
        .order_by(missing_col.asc(), have_col.desc(), func.coalesce(Recipe.rating, 0).desc())
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    recipe_ids = [r.Recipe.id for r in rows]
    tags_map = _tags_by_recipe(db, recipe_ids)

    cards: list[RecipeCard] = []
    for r in rows:
        recipe = r.Recipe
        cards.append(
            RecipeCard(
                id=recipe.id,
                title=recipe.title,
                source_domain=recipe.source_domain,
                image_url=recipe.image_url,
                servings=recipe.servings,
                total_time=recipe.total_time,
                rating=recipe.rating,
                rating_count=recipe.rating_count,
                kcal_per_serving=recipe.kcal_per_serving,
                tags=tags_map.get(recipe.id, []),
                have=r.have,
                total=r.total,
                missing_count=r.missing_count,
                ratio=round(r.have / r.total, 3) if r.total else 0.0,
            )
        )
    return cards


class PhotoRecipeSave(BaseModel):
    title: str
    instructions: str | None = None
    servings: int | None = None
    ingredients: list[str]
    image_url: str | None = None


@router.post("/from-photo")
async def recipe_from_photo(images: list[UploadFile] = File(...)):
    """Náhled receptu vyfoceného po úsecích – jen extrahuje, neukládá."""
    if not settings.ocr_model:
        raise HTTPException(
            400,
            "OCR model není nastaven. Nastav ho v Admin → Nástroje → OCR model "
            "(vision model stažený v Ollamě, např. qwen2.5vl nebo minicpm-v).",
        )
    if not images:
        raise HTTPException(400, "Nahraj alespoň jednu fotku receptu.")
    raw = [await f.read() for f in images]
    try:
        return photo_recipe.extract_draft(raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Čtení receptu selhalo: {exc}") from None


@router.post("/from-photo/save", response_model=RecipeDetail)
def save_photo_recipe(req: PhotoRecipeSave, db: Session = Depends(get_db)):
    title = req.title.strip()
    ingredients = [i.strip() for i in req.ingredients if i.strip()]
    if not title or not ingredients:
        raise HTTPException(400, "Recept musí mít název a alespoň jednu surovinu.")
    data = {
        "title": title,
        "source_url": f"photo://{uuid.uuid4()}",
        "source_domain": None,
        "image_url": req.image_url,
        "instructions": req.instructions,
        "servings": req.servings,
        "ingredients": ingredients,
    }
    recipe = persist_recipe(db, data)
    return get_recipe(recipe.id, db)


@router.get("/{recipe_id}", response_model=RecipeDetail)
def get_recipe(recipe_id: int, db: Session = Depends(get_db)):
    r = db.scalar(
        select(Recipe)
        .where(Recipe.id == recipe_id)
        .options(
            selectinload(Recipe.ingredients).selectinload(RecipeIngredient.ingredient),
            selectinload(Recipe.tags),
        )
    )
    if r is None:
        raise HTTPException(404, "Recept nenalezen")
    have = pantry_ingredient_ids(db)
    av = recipe_availability(r, have)
    detail = RecipeDetail.model_validate(r)
    detail.have = av["have"]
    detail.total = av["total"]
    detail.missing_count = av["missing_count"]
    detail.ratio = round(av["ratio"], 3)
    detail.missing_ingredient_ids = [ri.ingredient_id for ri in av["missing"]]

    # Spolehlivost výživy: podíl řádků, kde kalorie stojí na odhadu –
    # surovina vytvořená LLM (source='ollama') nebo řádek bez gramáže.
    matched = [ri for ri in r.ingredients if ri.ingredient_id is not None]
    if matched:
        estimated = sum(
            1 for ri in matched
            if (ri.ingredient is not None and ri.ingredient.source == "ollama")
            or ri.grams is None
        )
        detail.nutrition_estimated_pct = round(100 * estimated / len(matched))
    return detail


@router.patch("/{recipe_id}", response_model=RecipeDetail)
def edit_recipe(recipe_id: int, req: RecipeEdit, db: Session = Depends(get_db)):
    r = db.scalar(
        select(Recipe).where(Recipe.id == recipe_id).options(selectinload(Recipe.ingredients))
    )
    if r is None:
        raise HTTPException(404, "Recept nenalezen.")
    if req.title is not None:
        r.title = req.title.strip() or r.title
    if req.instructions is not None:
        r.instructions = req.instructions
    if req.servings is not None:
        r.servings = max(1, req.servings)
    if req.image_url is not None:
        r.image_url = req.image_url.strip() or None
    if req.user_rating is not None:
        r.user_rating = max(0, min(5, req.user_rating)) or None
    if req.user_note is not None:
        r.user_note = req.user_note.strip() or None
    if req.ingredient_texts is not None and len(req.ingredient_texts) == len(r.ingredients):
        for ri, txt in zip(r.ingredients, req.ingredient_texts):
            ri.raw_text = txt.strip()
    if req.servings is not None:
        recompute_recipe_kcal(r)
    textnorm.refresh_search_text(r)
    db.commit()
    return get_recipe(recipe_id, db)


@router.post("/{recipe_id}/retranslate", response_model=RecipeDetail)
def retranslate_one(recipe_id: int, db: Session = Depends(get_db)):
    """Znovu stáhni originál ze zdroje a přelož čerstvě (přepíše starý překlad)."""
    from ..modules import ingest

    r = db.get(Recipe, recipe_id)
    if r is None:
        raise HTTPException(404, "Recept nenalezen.")
    if not r.source_url or r.source_url.startswith(("photo://", "ai://")):
        raise HTTPException(
            400, "Tento recept nemá externí zdroj – originál se nedá znovu stáhnout."
        )
    from ..modules import llmclient

    err = llmclient.availability_error()
    if err:
        raise HTTPException(400, err)
    fresh = ingest.ingest_url(db, r.source_url)
    if fresh is None:
        raise HTTPException(502, "Stažení nebo zpracování zdrojové stránky selhalo.")
    return get_recipe(fresh.id, db)


@router.post("/{recipe_id}/cooked")
def mark_cooked(recipe_id: int, db: Session = Depends(get_db)):
    """Uvařeno – odečte suroviny receptu ze spíže (které tam jsou)."""
    r = db.scalar(
        select(Recipe).where(Recipe.id == recipe_id).options(selectinload(Recipe.ingredients))
    )
    if r is None:
        raise HTTPException(404, "Recept nenalezen.")
    used_ids = {ri.ingredient_id for ri in r.ingredients if ri.ingredient_id}
    removed = 0
    for item in db.scalars(
        select(PantryItem).where(PantryItem.ingredient_id.in_(used_ids))
    ).all():
        db.delete(item)
        removed += 1
    db.commit()
    return {"removed": removed}


@router.delete("/{recipe_id}", status_code=204)
def delete_recipe(recipe_id: int, db: Session = Depends(get_db)):
    r = db.get(Recipe, recipe_id)
    if r:
        db.delete(r)
        db.commit()
