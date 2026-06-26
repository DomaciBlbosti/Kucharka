"""Klasifikace receptů do tagů.

Vrací set kanonických tagů (`namespace:slug`) pro daný recept na základě:

  - Title + popis              → course, technique, flavor (sweet/savory), meal
  - Source domain              → cuisine
  - Title (fallback)           → cuisine, pokud doména je `international`
  - Matchnuté ingredience      → diet (vegan / vegetarian / lactose_free / gluten_free)
  - Ingredient lookup_keys     → flavor (sweet detection — hodně cukru/medu)

**Bez LLM.** Vše deterministické, run-time pod 1 ms na recept.

Konzervativní default: pokud si nejsme jisti, tag se NEpřidá (lepší chybět než
mít špatně). Diet tagy mají speciálně přísnou logiku.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Ingredient, Recipe, RecipeIngredient, Tag
from .lookup import make_lookup_key

log = logging.getLogger("kucharka.classifier")


# ─── Doména → kuchyně ────────────────────────────────────────────────────────

DOMAIN_TO_CUISINE: dict[str, str] = {
    # CZ/SK
    "recepty.cz": "czech", "toprecepty.cz": "czech", "apetitonline.cz": "czech",
    "vareni.cz": "czech", "ireceptar.cz": "czech", "klasicke-recepty.cz": "czech",
    "bestrecepty.cz": "czech", "kucharky.cz": "czech", "labuznik.cz": "czech",
    "kucharkaprodceru.cz": "czech", "gurmet.cz": "czech",
    "prima-receptar.cz": "czech", "abcrecepty.cz": "czech",
    "varecha.pravda.sk": "slovak", "dobruchut.cas.sk": "slovak",
    "recepty.sk": "slovak", "recepty.sme.sk": "slovak", "mojevarenie.sk": "slovak",
    # Indie
    "vegrecipesofindia.com": "indian", "indianhealthyrecipes.com": "indian",
    "hebbarskitchen.com": "indian", "cookwithmanali.com": "indian",
    "archanaskitchen.com": "indian", "spiceupthecurry.com": "indian",
    "tarladalal.com": "indian", "nishamadhulika.com": "indian",
    "sanjeevkapoor.com": "indian", "whiskaffair.com": "indian",
    "ministryofcurry.com": "indian", "holycowvegan.com": "indian",
    "mygingergarlickitchen.com": "indian", "chefkunalkapur.com": "indian",
    "sailusfood.com": "indian", "thebellyrulesthemind.net": "indian",
    # Thajsko
    "thaitable.com": "thai", "hot-thai-kitchen.com": "thai",
    "templeofthai.com": "thai", "tastythais.com": "thai",
    "shesimmers.com": "thai", "inquiringchef.com": "thai",
    # Čína
    "thewoksoflife.com": "chinese", "chinasichuanfood.com": "chinese",
    "omnivorescookbook.com": "chinese", "redhousespice.com": "chinese",
    "madewithlau.com": "chinese", "chinesecookingdemystified.com": "chinese",
    # Japonsko
    "justonecookbook.com": "japanese", "norecipes.com": "japanese",
    "japanesecooking101.com": "japanese", "chopstickchronicles.com": "japanese",
    "sudachirecipes.com": "japanese", "iamafoodblog.com": "japanese",
    # Korea
    "maangchi.com": "korean", "mykoreankitchen.com": "korean",
    "koreanbapsang.com": "korean", "aeriskitchen.com": "korean",
    "crazykoreancooking.com": "korean",
    # Vietnam
    "vickypham.com": "vietnamese", "helenrecipes.com": "vietnamese",
    "runawayrice.com": "vietnamese",
    # Itálie
    "giallozafferano.it": "italian", "giallozafferano.com": "italian",
    "cucchiaio.it": "italian", "lacucinaitaliana.it": "italian",
    "italianfoodforever.com": "italian", "thepastaproject.com": "italian",
    # Francie
    "marmiton.org": "french", "750g.com": "french",
    "cuisineaz.com": "french", "ricardocuisine.com": "french",
    "davidlebovitz.com": "french",
    # Středomoří + středovýchod
    "themediterraneandish.com": "mediterranean",
    "mygreekdish.com": "greek", "realgreekrecipes.com": "greek",
    "ottolenghi.co.uk": "middle_eastern", "silkroadrecipes.com": "middle_eastern",
    "amiraspantry.com": "middle_eastern", "zaatarandzaytoun.com": "middle_eastern",
    "moroccanzest.com": "middle_eastern",
    # Mexiko
    "mexicoinmykitchen.com": "mexican", "isabeleats.com": "mexican",
    "mexicanplease.com": "mexican", "patijinich.com": "mexican",
    "holajalapeno.com": "mexican",
    # UK
    "bbcgoodfood.com": "british", "jamieoliver.com": "british",
    "deliciousmagazine.co.uk": "british", "greatbritishchefs.com": "british",
    # US / international (default americký, ale specifické)
    "allrecipes.com": "american", "foodnetwork.com": "american",
    "food.com": "american", "tasteofhome.com": "american",
    "delish.com": "american", "simplyrecipes.com": "international",
    "seriouseats.com": "international", "bonappetit.com": "international",
    "epicurious.com": "international", "budgetbytes.com": "american",
    "thekitchn.com": "american", "food52.com": "international",
    "eatingwell.com": "american", "thespruceeats.com": "international",
    "smittenkitchen.com": "american", "pinchofyum.com": "american",
    "halfbakedharvest.com": "american",
    # Vegan / pečení — kuchyně z domény neidentifikovatelná, fallback z title
}

# Fallback z title pokud doména je neznámá nebo `international`
CUISINE_TITLE_KEYWORDS: dict[str, list[str]] = {
    "italian":        [r"\b(pasta|pizza|lasagn|risotto|carbonara|bolognese|tiramisu|gnocchi|focaccia|prosciutto)\b"],
    "indian":         [r"\b(curry|kari|tikka|masala|biryani|naan|samosa|chutney|dosa|paneer|tandoor|dal|daal)\b"],
    "thai":           [r"\b(pad thai|tom yum|tom kha|massaman|laksa|larb|som ?tum)\b"],
    "chinese":        [r"\b(kung pao|chow mein|lo mein|dim sum|won ?ton|szechuan|sichuan|mapo|bao)\b"],
    "japanese":       [r"\b(ramen|sushi|teriyaki|tempura|udon|miso|onigiri|donburi|tonkatsu|katsu|matcha)\b"],
    "korean":         [r"\b(kimchi|bibimbap|bulgogi|tteokbokki|gochujang|jjigae|banchan)\b"],
    "vietnamese":     [r"\b(pho|banh ?mi|spring roll|nuoc cham)\b"],
    "mexican":        [r"\b(taco|burrito|enchilad|quesadilla|guacamole|fajita|tamale|salsa|pico de gallo|mole|chimichanga)\b"],
    "french":         [r"\b(quiche|ratatouille|coq au vin|cassoulet|crepe|crêpe|bouillabaisse|tartiflette)\b"],
    "greek":          [r"\b(souvlaki|moussaka|gyros|tzatziki|baklava|spanakopita|feta)\b"],
    "middle_eastern": [r"\b(hummus|falafel|shawarma|tabbouleh|baba ?ganoush|kebab|kebap|kibbeh|fattoush)\b"],
    "czech":          [r"\b(svíčková|svickova|guláš|gulas|knedlík|knedlik|bramborák|řízek|rizek|trdelník|trdelnik|česnečka|cesnecka)\b"],
}


# ─── Course / kategorie hlavního chodu ──────────────────────────────────────

# Regexy se aplikují na lowercase title+category z webu. Match = tag.
COURSE_PATTERNS: dict[str, list[str]] = {
    "soup": [
        r"\b(polévk|vývar|bujón|soup|broth|bisque|chowder|zupa|minestr|miso)",
        r"\b(pho|ramen|tom yum|tom kha|gazpacho|borscht|borš)",
    ],
    "salad": [
        r"\bsal[áa]t\b", r"\bsalad\b", r"\binsalata\b",
    ],
    "dessert": [
        r"\b(dezert|dolce|gateau|dessert|cake|torta|torte|brownie|tiramisu|cr[èe]me)",
        r"\b(zákus|moučn[íi]k|kol[áa][čc]|sladk|sweet|kompot|pavlov|bun)",
        r"\b(buchty|vdole[čc]ek|[řr]ezy|dort|cupcake|muffin|cookie|sušenk|kolá[čc])",
        r"\b(jablečn|čokol[áa]d|piškot|piskot|sušenk)",
        r"\b(croissant|p[áa]j|tart|pudin|cr[èe]me br[ûu]l[ée]e|chouxe)",
    ],
    "appetizer": [
        r"\bp[řr]edkrm\b", r"\bappetiz", r"\bantipast", r"\bstarter\b",
        r"\btatar[áa]k", r"\bbruschett", r"\btoast\b", r"\bcanap[ée]",
    ],
    "drink": [
        r"\b(n[áa]poj|drink|cocktail|koktej|smoothie|limon[áa]d|punc|punč)\b",
        r"\b(d[žz]us\b|juice\b|coffee|kafe|kola\b|caf[ée])\b",
        r"\b(lat[ée]|caffe|mocha|frapp[ée])\b",
    ],
    "side": [
        r"\bp[řr][íi]loh", r"\bside dish", r"\bside\b",
        r"\b(coleslaw|gratin|mashed|smash|fries|hranolk)",
    ],
    "sauce": [
        r"\b(om[áa][čc]k|sauce|dip\b|dressing|salsa|chutney|mar[ií]n[áa]da|relish|gravy)",
        r"\b(aioli|pesto|tzatziki|guacamole|hummus|baba ?ganoush|ajvar)",
    ],
    "preserve": [
        r"\b(zava[řr]en|marmel[áa]d|d[žz]em|jam\b|pickle|kva[šs]en|sterilov|chutney)",
        r"\b(kimchi|sauerkraut|p[áa]len[íi])",
    ],
    "pastry": [
        r"\b(chl[ée]b|chleba|bread\b|focaccia|baguett|tortilla|naan|piad|rohl[íi]k|pita)",
        r"\b(bagel|brioche|croissant|kolá[čc])",
    ],
}

# Default course = main (pokud nic jiného nematchne)


# ─── Flavor ──────────────────────────────────────────────────────────────────

FLAVOR_PATTERNS: dict[str, list[str]] = {
    "sweet": [
        r"\b(sladk|sweet|cukr|sugar|med\b|honey|čokol[áa]d|chocolate|vanilk|vanilla)",
        r"\b(skoř|cinnamon|karamel|caramel|fond[áa]n|glaze|maple)",
    ],
    "spicy": [
        r"\b(p[áa]liv|spicy|hot\b|chili|chilli|jalape[ñn]o|sriracha|harissa|sambal)",
        r"\b(cayenne|gochujang|szechuan|sichuan)",
    ],
    "sour": [
        r"\b(kysel|sour|pickled|kvasen|vinegar|ocet)",
    ],
}

# Savory je default — ne přidává se na základě klíčových slov, jen jako fallback
# když nematchne sweet ani sour.


# ─── Meal — kdy se jí ───────────────────────────────────────────────────────

MEAL_PATTERNS: dict[str, list[str]] = {
    "breakfast": [
        r"\b(sn[íi]dan|breakfast|brunch|m[üu]sli|musli|granola|pala[čc]ink|pancake|waffle)",
        r"\b(omelet|smoothie bowl|oatmeal|po[řr][íi]d[žz]e?|po[ť]j)",
    ],
    "snack": [
        r"\b(sva[čc]in|snack\b|mlsán|finger food|appetiz)",
        r"\b(ty[čc]ink|bar\b|chip|cracker|pomaz[áa]nk|spread)",
    ],
    "lunch": [
        r"\b(ob[ěe]d\b|lunch)",
    ],
    "dinner": [
        r"\b(ve[čc]e[řr]|dinner|supper)",
    ],
    "brunch": [
        r"\bbrunch\b",
    ],
}


# ─── Technique ───────────────────────────────────────────────────────────────

TECHNIQUE_PATTERNS: dict[str, list[str]] = {
    "baking": [
        r"\b(pe[čc]en|baked|baking|roasted)",
        r"\b(kol[áa][čc]|cake|bread|chleba|chl[ée]b|focaccia|muffin|cookie|brownie)",
    ],
    "grilling": [
        r"\b(grilov|grill|barbecue|bbq|charcoal)",
    ],
    "frying": [
        r"\b(sma[žz]en|fried|fry\b|stir[- ]?fry|sauté|saut[ée]e|panfried|pan[- ]fried)",
        r"\b([řr][íi]zek|tempura|katsu)",
    ],
    "slow_cooking": [
        r"\b(pomal[ée]\s+va[řr]|slow cook|crockpot|braised|du[šs]en)",
    ],
    "roasting": [
        r"\b(pe[čc]en[ée] v troub|roast|roasted)",
    ],
    "raw": [
        r"\b(syrov|raw\b|tatar|carpacc|ceviche|crudo|sashimi)",
    ],
    "boiling": [
        r"\b(va[řr]en|boiled|simmer|broth)",
    ],
    "steaming": [
        r"\b(p[áa][řr]e|steamed|steaming|v p[áa][řr])",
    ],
    "fermenting": [
        r"\b(fermentov|fermented|fermenting|kva[šs]en|kombucha|sourdough|kv[áa]sk)",
    ],
    "no_cook": [
        r"\b(bez va[řr]en|no[- ]cook|no cooking|raw food)",
    ],
}


# ─── Diet — konzervativní inference z ingrediencí ────────────────────────────

# Klíčová slova v lookup_key suroviny, která vylučují daný tag.
# Detekce: pokud LIBOVOLNÁ surovina obsahuje LIBOVOLNÝ z těchto stemů → tag se NEpřidá.
# Cílem je minimalizovat false positives — proto široký záběr blokátorů.

_MEAT_STEMS = (
    "maso", "kure", "kurec", "kuř", "vepřov", "veprov", "hovězí", "hovezi",
    "hovězího", "hoveziho", "skopov", "jehně", "jehne", "kachn", "krůt", "krut",
    "vepřo", "vepro", "slanin", "šunk", "sunk", "chorizo", "salám", "salam",
    "klob[áa]s", "klobas", "párek", "parek", "chicken", "beef", "pork",
    "lamb", "duck", "turkey", "ham\b", "bacon", "sausage",
)
_FISH_STEMS = (
    "ryb", "losos", "tuňák", "tunak", "treska", "krevet", "garn[áa]t", "garnat",
    "kalamár", "kalamar", "chobotnic", "ústřic", "ustric", "škeb", "skeb",
    "fish", "salmon", "tuna", "shrimp", "prawn", "cod\b", "anchov",
)
_DAIRY_STEMS = (
    "ml[ée]k", "mlek", "máslo", "maslo", "smetan", "jogurt", "yogurt", "sýr",
    "syr ", "syr\b", "tvaroh", "ricotta", "mozzarell", "parmezan", "parmesan",
    "cheddar", "feta", "gorgonzol", "brie", "camembert", "halloumi",
    "milk\b", "butter", "cream\b", "cheese", "ghee",
)
_EGG_STEMS = ("vejc", "vajec", "vejce", "egg\b", "eggs\b", "albumin")
_HONEY_STEMS = ("med\b", "med ", "honey")
_GLUTEN_STEMS = (
    "pšenic", "psenic", "wheat", "mouk", "flour", "ječm", "jecm", "barley",
    "žito", "zito", "rye\b", "kuskus", "couscous", "bulgur", "krupic", "krupica",
    "krupk", "ovesn", "oats\b", "oat\b",   # ovesné — technicky bezlepkové, ale často kontaminované
    "chleb", "chleba", "bread", "rohlík", "rohlik", "knedlík", "knedlik",
    "těstov", "testov", "pasta\b", "noodle", "nudl", "soja", "soya",   # sójová omáčka má pšenici typicky
    "sójov", "sojov", "soy sauce",
)
_LACTOSE_STEMS = _DAIRY_STEMS  # zjednodušeně — laktóza = mléčné

# Vysokoproteinové: aspoň 1 výrazný zdroj proteinu (maso/ryby/tofu/luštěnina/sýr/vejce)
_HIGH_PROTEIN_STEMS = _MEAT_STEMS + _FISH_STEMS + (
    "tofu", "tempeh", "seit[áa]n", "seitan", "fazol", "bean", "[čc]o[čc]ovic",
    "cocovic", "lentil", "cizrn", "cizrna", "chickpea", "edamame",
) + _EGG_STEMS + _DAIRY_STEMS  # sýry mají hodně proteinu

# Nízkosacharidové: chybí škrobové základy
_HIGH_CARB_STEMS = (
    "pasta\b", "noodle", "nudl", "rýž", "ryz", "rice\b", "brambor", "potato",
    "knedl[íi]k", "knedlik", "tort", "chleb", "chleba", "bread", "mouk", "flour",
    "krupic", "kuskus", "couscous", "bulgur", "ovesn", "polenta", "kukuř", "kukur",
    "corn\b", "tortill", "pizza", "lasagn", "spaghetti", "ravioli",
)


def _ingredient_keys(db: Session, recipe: Recipe) -> list[str]:
    """Vrať lookup_key (nebo norm. raw_text) všech surovin receptu."""
    keys: list[str] = []
    for ri in recipe.ingredients:
        if ri.ingredient_id:
            ing = db.get(Ingredient, ri.ingredient_id)
            if ing and ing.name_cs:
                keys.append(make_lookup_key(ing.name_cs))
                continue
        # fallback na raw_text
        if ri.raw_text:
            keys.append(make_lookup_key(ri.raw_text))
    return [k for k in keys if k]


def _has_any_stem(keys: Iterable[str], stems: tuple[str, ...]) -> bool:
    """True, pokud LIBOVOLNÝ klíč matchne libovolný stem (substring nebo regex)."""
    for key in keys:
        for stem in stems:
            # Stemy končící \b → regex; jinak prosté `in` (rychlejší)
            if r"\b" in stem:
                if re.search(stem, key):
                    return True
            elif stem in key:
                return True
    return False


# ─── Hlavní vstupní bod ──────────────────────────────────────────────────────

@dataclass
class _Tag:
    namespace: str
    slug: str


def classify(db: Session, recipe: Recipe) -> set[tuple[str, str]]:
    """Vrátí set (namespace, slug) tagů pro recept. Neukládá je — to dělá caller."""
    # Ingredient keys pro detekci flavor/diet z ingrediencí (i pro recepty bez
    # explicitního "sladké"/"pálivé" v titulu).
    keys = _ingredient_keys(db, recipe)
    ingredient_blob = " ".join(keys)

    haystack = " ".join(
        filter(None, [
            (recipe.title or "").lower(),
            (recipe.category or "").lower(),
            (recipe.instructions or "")[:500].lower(),  # jen úvod, rychlost
            ingredient_blob,
        ])
    )

    result: set[tuple[str, str]] = set()

    # COURSE
    course_hits = _match_namespace(haystack, COURSE_PATTERNS)
    if course_hits:
        result.update(("course", s) for s in course_hits)
    else:
        result.add(("course", "main"))   # default

    # FLAVOR
    flavor_hits = _match_namespace(haystack, FLAVOR_PATTERNS)
    if flavor_hits:
        result.update(("flavor", s) for s in flavor_hits)
    elif "sweet" not in {s for _, s in result}:
        # Pokud nic, considera savory default — ale jen pro recepty které vypadají jako jídlo,
        # ne pro nápoje / dezerty.
        course_slugs = {s for ns, s in result if ns == "course"}
        if not course_slugs.intersection({"dessert", "drink"}):
            result.add(("flavor", "savory"))

    # Sweet auto-add pokud dessert
    if ("course", "dessert") in result:
        result.add(("flavor", "sweet"))

    # MEAL
    meal_hits = _match_namespace(haystack, MEAL_PATTERNS)
    result.update(("meal", s) for s in meal_hits)

    # TECHNIQUE
    tech_hits = _match_namespace(haystack, TECHNIQUE_PATTERNS)
    result.update(("technique", s) for s in tech_hits)

    # CUISINE — primárně z domény
    cuisine = DOMAIN_TO_CUISINE.get(recipe.source_domain or "", "")
    if not cuisine or cuisine == "international":
        for cuisine_slug, patterns in CUISINE_TITLE_KEYWORDS.items():
            if any(re.search(p, haystack) for p in patterns):
                cuisine = cuisine_slug
                break
    if not cuisine:
        cuisine = "international"
    result.add(("cuisine", cuisine))

    # DIET — konzervativní inference z ingrediencí (jen pokud máme matchnuté)
    if keys:
        has_meat   = _has_any_stem(keys, _MEAT_STEMS)
        has_fish   = _has_any_stem(keys, _FISH_STEMS)
        has_dairy  = _has_any_stem(keys, _DAIRY_STEMS)
        has_egg    = _has_any_stem(keys, _EGG_STEMS)
        has_honey  = _has_any_stem(keys, _HONEY_STEMS)
        has_gluten = _has_any_stem(keys, _GLUTEN_STEMS)

        if not (has_meat or has_fish or has_dairy or has_egg or has_honey):
            result.add(("diet", "vegan"))
            result.add(("diet", "vegetarian"))
        elif not (has_meat or has_fish):
            result.add(("diet", "vegetarian"))

        if not has_dairy:
            result.add(("diet", "lactose_free"))

        if not has_gluten:
            result.add(("diet", "gluten_free"))

        # High-protein heuristika: aspoň jedna výrazná proteinová surovina
        if _has_any_stem(keys, _HIGH_PROTEIN_STEMS):
            result.add(("diet", "high_protein"))

        # Low-carb: žádná škrobová základna
        if not _has_any_stem(keys, _HIGH_CARB_STEMS):
            result.add(("diet", "low_carb"))

    return result


def _match_namespace(haystack: str, patterns: dict[str, list[str]]) -> set[str]:
    """Vrátí množinu slugs, jejichž patterns matchují haystack."""
    hits = set()
    for slug, patterns_for_slug in patterns.items():
        for p in patterns_for_slug:
            if re.search(p, haystack):
                hits.add(slug)
                break
    return hits


# ─── Aplikace tagů na recept ────────────────────────────────────────────────

def apply_tags(db: Session, recipe: Recipe, tags: set[tuple[str, str]]) -> int:
    """Nahraď automaticky generované tagy receptu novou sadou.

    Ručně přidané tagy (`source='manual'`) zůstanou nedotčené.
    Vrací počet nově přidaných tagů.
    """
    # Smaž jen `auto` tagy — manual a llm ponech
    from sqlalchemy import delete
    from ..models import RecipeTag
    db.execute(
        delete(RecipeTag).where(
            RecipeTag.recipe_id == recipe.id,
            RecipeTag.source == "auto",
        )
    )

    if not tags:
        return 0

    # Načti tag IDs jedním dotazem
    tag_lookup = {
        (t.namespace, t.slug): t.id
        for t in db.scalars(select(Tag)).all()
    }

    added = 0
    for ns, slug in tags:
        tag_id = tag_lookup.get((ns, slug))
        if tag_id is None:
            log.warning("Tag %s:%s neexistuje v DB (chybí seed?)", ns, slug)
            continue
        db.add(RecipeTag(recipe_id=recipe.id, tag_id=tag_id, source="auto"))
        added += 1
    return added
