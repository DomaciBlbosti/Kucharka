"""Derivace `lookup_key` ze surového textu ingredience a DB-aware lookup do slovníku.

Cíl: deterministicky převést různé tvary stejné suroviny na **identický klíč**,
aby slovník `ingredient_alias` rostl pomalu (1 záznam na surovinu, ne na každý
gramatický tvar).

Pipeline:
    "2 ks kuřecích prsou (bez kůže)"
      ── lowercase ───────────────────► "2 ks kuřecích prsou (bez kůže)"
      ── strip parens ────────────────► "2 ks kuřecích prsou"
      ── strip quantity + units ──────► "kuřecích prsou"
      ── strip stopwords ─────────────► "kuřecích prsou"
      ── lemmatize per token (cs) ────► "kuřecí prso"
      ── strip diacritics ────────────► "kureci prso"
      ── whitespace normalize ────────► "kureci prso"

Diakritika se strhne **až po** lemmatizaci, protože simplemma očekává správné
české tvary. Finální klíč je bez diakritiky pro robustnost vůči překlepům.

Lookup funkce dělá fallback: nejdřív `lookup_key`, pak `alias` (legacy
záznamy bez lookup_key). Hit zaznamenává `hit_count` a `last_seen_at`.

Per-process LRU cache (`@lru_cache`) odřízne většinu volání simplemmy
v rámci jednoho běhu crawleru.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from functools import lru_cache
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import IngredientAlias

# ─── Regexy a tabulky ────────────────────────────────────────────────────────

# Unicode zlomky → desetinné číslo (jen kvůli stripu, hodnotu nepotřebujeme)
_UNICODE_FRACTIONS = "½⅓⅔¼¾⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞"

# Krátké jednotky musí být exact match (jinak "ml" matchne "mléka").
_EXACT_UNITS = frozenset({
    # české / metrické
    "g", "kg", "mg", "dkg", "dag",
    "ml", "cl", "dl", "l",
    "ks", "kus", "kusů", "kusy",
    # anglické / imperiální (cizojazyčné weby)
    "tsp", "tbsp", "oz", "lb", "lbs", "cup", "cups", "fl",  # "fl oz" — fl je samostatný token
    "qt", "pt", "gal", "pcs", "pc", "can", "cans", "jar", "jars",
})

# Delší inflektované jednotky stripujeme prefix matchem.
_UNIT_STEMS = (
    # české
    "lžíc", "lžičk", "hrnek", "hrnk", "šálek", "šálk", "sklenic",
    "polévkov", "kávov", "čajov", "dezertn",
    "plátek", "plátk", "stroužek", "stroužk",
    "svazek", "svazk", "balíček", "balíčk", "konzerv", "lahev", "lahv",
    "špetk", "hrst", "kousek", "kousk", "dóz", "sáček", "sáčk", "kapk",
    # anglické
    "teaspoon", "tablespoon", "ounce", "pound", "pint", "quart", "gallon",
    "pinch", "dash", "clove", "slice", "stalk", "sprig", "bunch", "handful",
    "stick", "can", "jar", "bottle", "package", "packet", "piece", "pieces",
)

# Stop slova — zbytečné výplně, které nepomáhají identifikaci suroviny.
_STOP = frozenset({
    # české ingredient modifiers
    "čerstvý", "čerstvá", "čerstvé", "čerstvých", "čerstvou",
    "mletý", "mletá", "mleté", "mletých",
    "sušený", "sušená", "sušené", "sušených",
    "nakrájený", "nakrájená", "nakrájené", "nakrájených",
    "krájený", "krájená", "krájené",
    "uvařený", "uvařená", "uvařené",
    # české předložky / částice
    "na", "po", "do", "z", "ze", "s", "se", "v", "ve", "k", "ke", "u",
    "podle", "dle", "asi", "cca", "přibližně",
    "chuti", "ozdobu", "ozdobě", "ozdobení", "ozdobeni",
    "volitelně", "případně", "popřípadě",
    "navrch", "navíc", "nahoru",
    "trochu", "trocha", "trošku", "trošičku",
    "dobrá", "kvalitní",
    # anglické předložky / člány
    "of", "the", "a", "an", "to", "for", "with", "in", "on", "or", "and",
    # anglické modifiers
    "fresh", "large", "small", "medium", "big", "tiny",
    "dried", "chopped", "sliced", "diced", "minced", "grated",
    "finely", "coarsely", "roughly", "thinly", "thickly",
    "raw", "cooked", "ripe", "ground", "whole",
    "boneless", "skinless", "boneless,", "skinless,",
    "extra", "virgin", "optional", "to", "taste",
})

_FRACTION_TOKEN_RE = re.compile(rf"^[0-9{_UNICODE_FRACTIONS}/.,\-–]+$")
_NUM_PREFIX_RE = re.compile(rf"^[0-9{_UNICODE_FRACTIONS}\s/.,\-–]+")
_PARENS_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_PUNCT_RE = re.compile(r"[,;:!?\"]+")
_WHITESPACE_RE = re.compile(r"\s+")

_MAX_KEY_LEN = 200  # musí sedět s VARCHAR(200) v DB


# ─── Stripping helpers ───────────────────────────────────────────────────────

def _strip_diacritics(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def _is_unit_token(tok: str) -> bool:
    """Token vypadá jako jednotka?"""
    if not tok:
        return False
    if tok in _EXACT_UNITS:
        return True
    if len(tok) < 4:
        return False
    return any(tok.startswith(stem) for stem in _UNIT_STEMS)


def _strip_leading_quantity(s: str) -> str:
    """Odstraní vedoucí čísla, zlomky a první unit token za nimi.

    "2 lžíce mouky"     → "mouky"
    "150 g cukru"       → "cukru"
    "½ hrnku mléka"     → "mléka"
    "3-4 stroužky česneku" → "česneku"
    """
    s = _NUM_PREFIX_RE.sub("", s).strip()
    # Po číslech může následovat jednotka. Strip jeden token, pokud je to unit.
    parts = s.split(maxsplit=1)
    if parts and _is_unit_token(parts[0]):
        s = parts[1] if len(parts) > 1 else ""
    return s.strip()


def _strip_trailing_quantity(s: str) -> str:
    """Některé řádky mají formát "mouka 150 g". Odstraň trailing množství."""
    parts = s.rsplit(maxsplit=2)
    if len(parts) >= 2 and _FRACTION_TOKEN_RE.match(parts[-1].replace(",", ".")):
        # Trailing pure number
        return " ".join(parts[:-1]).strip()
    if len(parts) >= 3 and _is_unit_token(parts[-1]) and _FRACTION_TOKEN_RE.match(
        parts[-2].replace(",", ".")
    ):
        # Trailing "number unit"
        return " ".join(parts[:-2]).strip()
    return s


# ─── Lemmatizace ─────────────────────────────────────────────────────────────

_SIMPLEMMA = None
_SIMPLEMMA_LOAD_FAILED = False

_CZECH_DIACRITICS = frozenset("áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ")
# Signální slova, která téměř jistě indikují anglický recept.
_EN_SIGNAL_TOKENS = frozenset({
    # jednotky
    "tbsp", "tsp", "oz", "lb", "lbs", "cup", "cups", "fl", "qt", "pt",
    # běžné EN ingredients/modifiers
    "chicken", "beef", "pork", "fish", "salmon", "shrimp", "butter", "cheese",
    "milk", "cream", "salt", "pepper", "garlic", "onion", "olive", "tomato",
    "cilantro", "parsley", "basil", "ginger", "lemon", "lime", "honey", "sugar",
    "flour", "rice", "noodle", "noodles", "bread", "egg", "eggs",
    # časté EN slova
    "fresh", "chopped", "sliced", "diced", "minced", "grated", "ground",
    "of", "the", "and", "with", "boneless", "skinless",
})


def _detect_lang(text: str) -> str:
    """Detekuj jazyk celé suroviny pro konzistentní lemmatizaci všech tokenů."""
    if any(c in text for c in _CZECH_DIACRITICS):
        return "cs"
    tokens = set(text.lower().split())
    if tokens & _EN_SIGNAL_TOKENS:
        return "en"
    return "cs"  # default — bez diakritiky i bez EN signálů (často jednoslovné CZ "cukr", "vejce")


def _lemmatize_token(tok: str, lang: str = "cs") -> str:
    """Lemmatizuj jeden token v daném jazyce. Při selhání vrátí token beze změny."""
    global _SIMPLEMMA, _SIMPLEMMA_LOAD_FAILED
    if _SIMPLEMMA_LOAD_FAILED:
        return tok
    if _SIMPLEMMA is None:
        try:
            import simplemma  # lazy import, ~30 MB dat se načte při prvním volání
            _SIMPLEMMA = simplemma
        except ImportError:
            _SIMPLEMMA_LOAD_FAILED = True
            return tok
    try:
        return _SIMPLEMMA.lemmatize(tok, lang=lang)
    except Exception:  # noqa: BLE001
        return tok


# ─── Hlavní API ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=10_000)
def make_lookup_key(raw_text: str) -> str:
    """Převede syrový text ingredience na deterministický klíč pro slovník.

    Idempotentní: `make_lookup_key(make_lookup_key(x)) == make_lookup_key(x)`.
    Cache=10k drží paměť pod kontrolou; reset stačí restartem procesu.
    Pro testy: `make_lookup_key.cache_clear()`.
    """
    if not raw_text:
        return ""

    s = raw_text.lower().strip()
    s = _PARENS_RE.sub(" ", s)               # vyhodit "(volitelně)" atd.
    s = _PUNCT_RE.sub(" ", s)                # interpunkce na mezery
    s = _WHITESPACE_RE.sub(" ", s).strip()
    s = _strip_leading_quantity(s)
    s = _strip_trailing_quantity(s)
    s = _WHITESPACE_RE.sub(" ", s).strip()

    if not s:
        return ""

    lang = _detect_lang(raw_text)

    # Token-level: lemmatizace a stop-slova
    tokens = []
    for tok in s.split():
        if tok in _STOP:
            continue
        # Token co je pouze čísla/zlomky → vyhodit (zbytek po stripu může být)
        if _FRACTION_TOKEN_RE.match(tok):
            continue
        if _is_unit_token(tok):
            continue
        lemma = _lemmatize_token(tok, lang=lang)
        if lemma in _STOP:                   # i lemma může být stop slovo
            continue
        tokens.append(lemma)

    if not tokens:
        return ""

    # Diakritika se strhne až teď, simplemma ji potřebovala.
    key = _strip_diacritics(" ".join(tokens))
    key = _WHITESPACE_RE.sub(" ", key).strip()
    return key[:_MAX_KEY_LEN]


def lookup_alias(db: Session, raw_text: str, *, increment_stats: bool = True) -> IngredientAlias | None:
    """Najdi alias ve slovníku podle textu. Vrátí None, pokud není.

    Postup:
      1. Spočítej `lookup_key`.
      2. SELECT podle `lookup_key`.
      3. Fallback: SELECT podle `alias` (legacy data před backfillem).
      4. Při hit: hit_count += 1, last_seen_at = NOW.
    """
    key = make_lookup_key(raw_text)
    if not key:
        return None

    alias = db.scalar(
        select(IngredientAlias).where(IngredientAlias.lookup_key == key)
    )
    if alias is None:
        # Legacy fallback — staré aliasy bez lookup_key. Postupně zmizí
        # po dokončení backfillu, ale ponecháno pro robustnost.
        legacy_key = _strip_diacritics(key)
        alias = db.scalar(
            select(IngredientAlias).where(IngredientAlias.alias == legacy_key)
        )

    if alias is not None and increment_stats:
        alias.hit_count = (alias.hit_count or 0) + 1
        alias.last_seen_at = datetime.utcnow()

    return alias


def batch_make_keys(raw_texts: Iterable[str]) -> dict[str, str]:
    """Vrátí mapping `raw_text → lookup_key`. Užitečné pro hromadné operace
    (crawler enrichment worker), kde nechceme volat lookup pro každý řádek
    samostatně."""
    return {t: make_lookup_key(t) for t in raw_texts}
