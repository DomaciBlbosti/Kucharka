"""Doménová pravidla pro weby, kde generický schema.org parser nestačí.

Generický scraper (recipe-scrapers, wild_mode) bere postup z JSON-LD
`recipeInstructions`. Některé redakční systémy tam ale mají jen marketingový
úvod a skutečné kroky nechávají výhradně v těle článku. Pro takové domény tu
je ruční pravidlo, které kroky vytáhne z HTML.

Záměrně bez BeautifulSoup: bs4 se do backendu dostane jen jako tranzitivní
závislost recipe-scrapers a v CI se recipe-scrapers neinstaluje (rozbitý sdist
build jstyleson). Značkování je tu jednoduché a stabilní, takže regexy stačí –
a testy pak běží bez dalších závislostí. Každé pravidlo je defenzivní: když si
není jisté, vrátí None a použije se generický výsledek.
"""
from __future__ import annotations

import html as _html
import re

# Nadpis, za kterým začíná postup („Postup přípravy lasagní", „Postup
# přípravy – Zapečené papriky", „Jak připravit nejlepší nakládané okurky?").
# Bere se všechno až po další nadpis.
_HEAD_TEXT_RE = re.compile(r"postup|jak (?:si )?připrav|jak na ", re.I)
_ANY_HEAD_RE = re.compile(r"<h[2-4][^>]*>(.*?)</h[2-4]>", re.S | re.I)
_NEXT_HEAD_RE = re.compile(r"<h[2-4][^>]*>", re.I)
_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t ]+")


def _text(fragment: str) -> str:
    """HTML fragment → holý text (bez značek, entit a zdvojených mezer)."""
    return _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub(" ", fragment))).strip()


def _steps_after_postup_heading(html: str) -> list[str] | None:
    """Kroky ze seznamu, který následuje za nadpisem „Postup…".

    Pokrývá obě podoby, které web střídá: číslovaný <ol><li>…</li></ol>
    i sérii <div class="su-list"><ul><li>ikona + text</li></ul></div>.
    V obou případech stačí posbírat <li> v úseku mezi nadpisem „Postup" a
    dalším nadpisem.
    """
    for m in _ANY_HEAD_RE.finditer(html):
        head = _text(m.group(1))
        # „…? Vyzkoušejte také:" uvozuje odkazy na jiné recepty, ne postup.
        if _HEAD_TEXT_RE.search(head) and "vyzkoušejte" not in head.lower():
            break
    else:
        return None
    start = m.end()
    nxt = _NEXT_HEAD_RE.search(html, start)
    section = html[start : nxt.start() if nxt else start + 20000]

    steps = []
    for li in _LI_RE.findall(section):
        t = _text(li)
        # Krátké položky jsou navigace nebo popisky ikon, ne krok postupu.
        if len(t) >= 20:
            steps.append(t)
    return steps or None


def _bestrecepty(html: str) -> str | None:
    """bestrecepty.cz – v JSON-LD je jediný HowToStep s marketingovým úvodem
    („Nejlepší kuře na paprice je skvělý pokrm s…"), kroky jsou jen v článku.
    Ověřeno na 10 receptech: u 8 z nich má stránka nadpis „Postup přípravy…",
    zbylé dva postup opravdu nemají a spadnou na generický výsledek."""
    steps = _steps_after_postup_heading(html)
    if not steps or len(steps) < 2:
        return None
    return "\n".join(steps)


# Doména → funkce(html) -> postup | None
INSTRUCTION_RULES = {
    "bestrecepty.cz": _bestrecepty,
}


def instructions_for(domain: str, html: str) -> str | None:
    """Postup podle doménového pravidla, nebo None (= použij generický)."""
    rule = INSTRUCTION_RULES.get((domain or "").replace("www.", ""))
    if rule is None:
        return None
    try:
        return rule(html)
    except Exception:  # noqa: BLE001 – pravidlo nesmí shodit ingest
        return None


def has_rule(domain: str) -> bool:
    return (domain or "").replace("www.", "") in INSTRUCTION_RULES
