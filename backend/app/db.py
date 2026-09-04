"""Připojení k databázi (SQLAlchemy 2.0).

Funguje s MariaDB (produkce, mysql+pymysql) i SQLite (lokální vývoj/test).
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


_connect_args: dict = {}
_pool_kw: dict = {}
if settings.database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}
else:
    # Velikost fondu podle skutečného souběhu appky (viz config.db_pool_size);
    # se SQLite se neladí – tam se podle URL použije jiný typ poolu, který
    # tyhle parametry nezná. pool_recycle drží spojení mladší než MariaDB
    # wait_timeout, ať se nesahá na zahozené spojení.
    _pool_kw = {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_recycle": 3600,
    }

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
    connect_args=_connect_args,
    **_pool_kw,
)

if settings.database_url.startswith("sqlite"):
    # SQLite cizí klíče ve výchozím stavu NEHLÍDÁ, MariaDB ano. Bez tohohle
    # se chyby v pořadí zápisů nedají odhalit testem: slučování surovin
    # mazalo surovinu dřív, než se stihly přepojit řádky receptů, na SQLite
    # to prošlo a v produkci to spadlo na `recipe_ingredient_ibfk_2`.
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_enforce_fk(dbapi_conn, _rec):  # pragma: no cover - triviální
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
