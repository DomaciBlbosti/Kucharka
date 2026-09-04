"""Testy přidávání sloupců – regrese „appka hodinu nenaběhla".

Produkční scénář, který tohle pokrývá:

  Tabulka `recipe` (171k řádků) má dva FULLTEXT indexy. InnoDB kvůli nim neumí
  ADD COLUMN jinak než přestavbou celé tabulky, při které oba fulltexty znovu
  tokenizuje. Čtyři nové sloupce přidávané po jednom = čtyři takové přestavby;
  příkaz běžel přes hodinu (`STAGE 1/4, copy to tmp table, 70.6 %`) a appka po
  celou dobu visela na „Waiting for application startup". Restart rozdělaný
  ALTER odroloval, takže další pokus začal od nuly – smyčka.

Opatření a co je tu ověřené:
  1. všechny chybějící sloupce jedné tabulky JEDNÍM ALTERem (jedna přestavba)
  2. nejdřív ALGORITHM=INSTANT
  3. když neprojde, DOČASNĚ zahodit fulltext indexy a zkusit znovu
  4. eskalace INSTANT → INPLACE → nech vybrat server

Krok 3 nestačí doplnit o INSTANT: produkce vrátila `ERROR 1845: ALGORITHM=
INSTANT is not supported for this operation. Try ALGORITHM=INPLACE` i s oběma
fulltexty zahozenými (zůstává skrytý FTS_DOC_ID). Proto ten žebřík.

Testuje se proti falešnému enginu – MariaDB v CI není a chování, které nás
zajímá, je právě odmítnutí konkrétních algoritmů.
"""
from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import migrations  # noqa: E402

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  OK  {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}" + (f" – {detail}" if detail else ""))


# ─── Falešný engine ──────────────────────────────────────────────────────────

class FakeError(Exception):
    pass


class FakeConn:
    def __init__(self, db):
        self.db = db

    def execute(self, clause):
        self.db.run(str(clause))


class FakeDialect:
    def __init__(self, name):
        self.name = name


class FakeEngine:
    """Minimální náhrada Enginu: pamatuje si sloupce, indexy a vykonané SQL.

    `refuse` říká, které algoritmy server neumí – tím se simulují jednotlivé
    verze MariaDB i vliv fulltextu.
    """

    def __init__(self, *, columns, indexes, dialect="mysql", refuse=()):
        self.columns = {t: list(c) for t, c in columns.items()}
        self.indexes = {t: dict(i) for t, i in indexes.items()}  # name -> fulltext?
        self.dialect = FakeDialect(dialect)
        self.refuse = set(refuse)
        self.sql: list[str] = []

    # -- chování serveru --
    def run(self, sql: str) -> None:
        self.sql.append(sql)
        m = re.match(r"ALTER TABLE (\w+) (.+)", sql, re.S)
        if not m:
            raise FakeError(f"neznámý příkaz: {sql}")
        table, rest = m.group(1), m.group(2)

        drop = re.fullmatch(r"DROP INDEX (\w+)", rest)
        if drop:
            self.indexes[table].pop(drop.group(1))
            return

        algo_m = re.search(r", ALGORITHM=(\w+)$", rest)
        algo = algo_m.group(1) if algo_m else ""
        # INSTANT tabulka s fulltextem neumí nikdy (a po jeho zahození podle
        # `refuse`, což odpovídá skrytému FTS_DOC_ID na produkci).
        if algo == "INSTANT" and any(self.indexes[table].values()):
            raise FakeError("ALGORITHM=INSTANT is not supported for this operation.")
        if algo and algo in self.refuse:
            raise FakeError(f"ALGORITHM={algo} is not supported for this operation.")
        for name in re.findall(r"ADD COLUMN (\w+)", rest):
            self.columns[table].append(name)

    @contextmanager
    def begin(self):
        yield FakeConn(self)


class FakeInspector:
    def __init__(self, engine):
        self.e = engine

    def get_table_names(self):
        return list(self.e.columns)

    def get_columns(self, table):
        return [{"name": n} for n in self.e.columns[table]]

    def get_indexes(self, table):
        return [{"name": n} for n in self.e.indexes.get(table, {})]


def recipe_engine(*, fulltext=True, refuse=(), dialect="mysql", missing=("hidden", "feed_score")):
    """`recipe` se vším kromě `missing` – tedy přesně produkční stav."""
    cols = [s.name for s in migrations._COLUMNS
            if s.table == "recipe" and s.name not in missing]
    cols = ["id", "title", "instructions"] + cols
    idx = {}
    if fulltext:
        idx = {"ft_recipe_title_instructions": True, "ft_recipe_search_text": True}
    return FakeEngine(columns={"recipe": cols}, indexes={"recipe": idx},
                      dialect=dialect, refuse=refuse)


def add_columns(engine):
    """Zavolá _add_columns s podstrčeným inspectorem."""
    orig = migrations.inspect
    migrations.inspect = FakeInspector
    try:
        migrations._add_columns(engine, FakeInspector(engine), set(engine.columns))
    finally:
        migrations.inspect = orig


def alters(engine):
    return [s for s in engine.sql if "ADD COLUMN" in s]


def drops(engine):
    return [s for s in engine.sql if "DROP INDEX" in s]


# ─── Testy ───────────────────────────────────────────────────────────────────

def test_one_alter_per_table():
    """Regrese: čtyři sloupce po jednom = čtyři přestavby tabulky."""
    e = recipe_engine(fulltext=False,
                      missing=("hidden", "feed_score", "search_text", "title_key"))
    add_columns(e)
    check("čtyři chybějící sloupce jdou v JEDNOM ALTERu", len(alters(e)) == 1,
          f"{len(alters(e))} ALTERů: {alters(e)}")
    check("a všechny se opravdu přidají",
          {"hidden", "feed_score", "search_text", "title_key"} <= set(e.columns["recipe"]))


def test_no_fulltext_is_instant():
    """Bez fulltextu projde INSTANT hned – žádné zahazování indexů."""
    e = recipe_engine(fulltext=False)
    add_columns(e)
    check("bez fulltextu stačí jeden INSTANT ALTER",
          len(alters(e)) == 1 and "ALGORITHM=INSTANT" in alters(e)[0], str(alters(e)))
    check("nic se nezahazuje", not drops(e), str(drops(e)))


def test_fulltext_gets_dropped():
    """Produkční stav: INSTANT selže, fulltext padne, projde INPLACE."""
    e = recipe_engine(fulltext=True, refuse={"INSTANT"})
    add_columns(e)
    check("oba fulltext indexy se zahodí", len(drops(e)) == 2, str(drops(e)))
    check("sloupce se přidají", {"hidden", "feed_score"} <= set(e.columns["recipe"]))
    check("zahazuje se AŽ po neúspěšném prvním pokusu",
          e.sql[0].endswith("ALGORITHM=INSTANT"), e.sql[0])
    check("nakonec projde INPLACE (ne drahý ALTER bez algoritmu)",
          e.sql[-1].endswith("ALGORITHM=INPLACE"), e.sql[-1])
    check("žádný ALTER navíc po úspěchu", len(alters(e)) == 3, str(alters(e)))


def test_fulltext_dropped_only_once():
    """Druhý start už nic nezahazuje: sloupce existují, migrace je no-op."""
    e = recipe_engine(fulltext=True, refuse={"INSTANT"})
    add_columns(e)
    e.sql.clear()
    add_columns(e)
    check("opakovaný běh nesahá na databázi", not e.sql, str(e.sql))


def test_escalates_to_plain_alter():
    """Starý server bez INSTANT i INPLACE – musí to dojet obyčejným ALTERem."""
    e = recipe_engine(fulltext=True, refuse={"INSTANT", "INPLACE"})
    add_columns(e)
    check("dojede to bez ALGORITHM=", e.sql[-1].endswith("feed_score FLOAT NULL"),
          e.sql[-1])
    check("sloupce jsou tam i tak", {"hidden", "feed_score"} <= set(e.columns["recipe"]))


def test_dropped_fulltext_is_rebuilt_in_background():
    """Zahozený index musí `_missing_fulltext` vrátit k dostavbě na pozadí –
    jinak by hledání zůstalo natrvalo na LIKE."""
    e = recipe_engine(fulltext=True, refuse={"INSTANT"})
    add_columns(e)
    orig_marker = migrations._marker_set
    migrations._marker_set = lambda *a, **k: True  # search_text je naplněný
    try:
        pending = migrations._missing_fulltext(e, FakeInspector(e), set(e.columns))
    finally:
        migrations._marker_set = orig_marker
    check("oba zahozené fulltexty čekají na dostavbu",
          {s.name for s in pending} == {"ft_recipe_title_instructions",
                                        "ft_recipe_search_text"},
          str([s.name for s in pending]))


def test_search_text_fulltext_waits_for_backfill():
    """Index nad search_text se nesmí stavět dřív, než je sloupec naplněný –
    prázdný fulltext by znamenal, že hledání dočasně nic nenajde."""
    e = recipe_engine(fulltext=True, refuse={"INSTANT"})
    add_columns(e)
    orig_marker = migrations._marker_set
    migrations._marker_set = lambda *a, **k: False
    try:
        pending = migrations._missing_fulltext(e, FakeInspector(e), set(e.columns))
    finally:
        migrations._marker_set = orig_marker
    check("ft_recipe_search_text počká na naplnění sloupce",
          {s.name for s in pending} == {"ft_recipe_title_instructions"},
          str([s.name for s in pending]))


def test_sqlite_adds_one_by_one():
    """SQLite umí v ALTERu jen jeden ADD COLUMN a ALGORITHM nezná vůbec."""
    e = recipe_engine(fulltext=False, dialect="sqlite")
    add_columns(e)
    check("dva sloupce = dva ALTERy", len(alters(e)) == 2, str(alters(e)))
    check("bez ALGORITHM=", not any("ALGORITHM" in s for s in e.sql), str(e.sql))
    check("obojí se přidá", {"hidden", "feed_score"} <= set(e.columns["recipe"]))


def test_drop_failure_does_not_block():
    """Když zahození indexu selže (chybí právo, index drží někdo jiný), musí
    migrace přesto dojít až k obyčejnému ALTERu."""
    e = recipe_engine(fulltext=True, refuse={"INSTANT", "INPLACE"})
    orig_run = e.run

    def run(sql):
        if "DROP INDEX" in sql:
            e.sql.append(sql)
            raise FakeError("index in use")
        orig_run(sql)

    e.run = run
    add_columns(e)
    check("sloupce se přidají i tak", {"hidden", "feed_score"} <= set(e.columns["recipe"]))
    check("fulltexty zůstaly", len(e.indexes["recipe"]) == 2, str(e.indexes["recipe"]))


def main():
    for fn in (
        test_one_alter_per_table,
        test_no_fulltext_is_instant,
        test_fulltext_gets_dropped,
        test_fulltext_dropped_only_once,
        test_escalates_to_plain_alter,
        test_dropped_fulltext_is_rebuilt_in_background,
        test_search_text_fulltext_waits_for_backfill,
        test_sqlite_adds_one_by_one,
        test_drop_failure_does_not_block,
    ):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
