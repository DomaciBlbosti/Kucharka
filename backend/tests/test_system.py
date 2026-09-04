"""Testy endpointu s verzí aplikace.

Regresní scénář z produkce: karta „Verze a aktualizace" v administraci občas
zmizela. Byly za tím dvě věci a obě jsou tu pokryté:

  1. `/api/system/version` sahal na git ČTYŘIKRÁT, každé volání s timeoutem
     120 s. Zrovna po kliknutí na „Aktualizovat" dělá supervisor v tomtéž
     repozitáři `git pull`, takže se čekalo na index.lock a endpoint se
     zasekl na minuty.
  2. Frontend měl `if (!v) return null` – jeden neúspěšný dotaz kartu schoval
     a nic ji nezkusilo načíst znovu.

Backendová část se testuje tady; ta frontendová je v Admin.jsx (karta se
ukáže i s chybovou hláškou a tlačítkem „Zkusit znovu").
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmpdir = tempfile.mkdtemp(prefix="kucharka-system-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import system as sysmod  # noqa: E402

Base.metadata.create_all(engine)

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  OK  {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}" + (f" – {detail}" if detail else ""))


def main():
    orig_git = sysmod._git
    calls: list[tuple] = []

    def spy(*a, **k):
        calls.append(a)
        return orig_git(*a, **k)

    with TestClient(app) as c:
        # ── kolik dotazů na git a jak často ──
        sysmod._git = spy
        sysmod._version_cache.update(at=0.0, data=None)
        try:
            r = c.get("/api/system/version")
            first = len(calls)
            check("verze se vrátí", r.status_code == 200, str(r.status_code))
            check("stačí nejvýš dvě volání gitu (dřív čtyři)", first <= 2, str(first))

            c.get("/api/system/version")
            c.get("/api/system/version")
            check("další dotazy jedou z cache", len(calls) == first,
                  str(len(calls) - first))

            c.get("/api/system/version?refresh=true")
            check("refresh=true cache obejde", len(calls) > first)
        finally:
            sysmod._git = orig_git

        # ── nefunkční git nesmí kartu shodit ──
        # Přesně stav během `git pull`, kdy je repozitář zamčený.
        sysmod._git = lambda *a, **k: "chyba: index.lock"
        sysmod._version_cache.update(at=0.0, data=None)
        try:
            r = c.get("/api/system/version")
            body = r.json()
            check("i s rozbitým gitem odpoví 200", r.status_code == 200)
            check("'enabled' dorazí vždy – podle něj se karta zobrazuje",
                  "enabled" in body, str(body))
            check("chybová hláška z gitu se do commitu nedostane",
                  body["commit"] == "" and "chyba" not in str(body["subject"]),
                  str(body))
            check("větev má rozumný default", body["branch"] == "main", str(body))
        finally:
            sysmod._git = orig_git

        # ── jednou zjištěná verze se kvůli výpadku gitu nezahodí ──
        sysmod._version_cache.update(at=0.0, data=None)
        c.get("/api/system/version")  # naplní cache skutečnou hodnotou
        known = sysmod._version_cache["data"]["commit"]
        sysmod._git = lambda *a, **k: "chyba: index.lock"
        try:
            body = c.get("/api/system/version?refresh=true").json()
            check("při výpadku gitu zůstane poslední známý commit",
                  not known or body["commit"] == known,
                  f"{body['commit']!r} vs {known!r}")
        finally:
            sysmod._git = orig_git

    print(f"\n{PASSED} OK, {FAILED} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
