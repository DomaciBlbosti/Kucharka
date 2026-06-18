import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { IngredientPicker } from "../components/IngredientPicker";
import { Button, Spinner } from "../components/ui";

export default function AddRecipe() {
  const nav = useNavigate();
  const [url, setUrl] = useState("");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [candidates, setCandidates] = useState(null);
  const [status, setStatus] = useState({ searxng: false, ollama: false });
  const [ollama, setOllama] = useState(null);

  useEffect(() => {
    api.searchStatus().then(setStatus).catch(() => {});
    api.ollamaStatus().then(setOllama).catch(() => {});
  }, []);

  const ingest = async (target) => {
    setBusy(true);
    setError("");
    try {
      const r = await api.ingest(target);
      if (!r) {
        setError("Recept se nepodařilo vyparsovat. Zkus jinou URL.");
      } else {
        nav(`/recept/${r.id}`);
      }
    } catch {
      setError("Stažení selhalo. Zkontroluj URL a připojení.");
    } finally {
      setBusy(false);
    }
  };

  const discover = async () => {
    setBusy(true);
    setError("");
    setCandidates(null);
    try {
      const res = await api.discover(query);
      setCandidates(res.results || []);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="max-w-xl">
      <h1 className="mb-1 text-2xl font-extrabold">Přidat recept</h1>
      <p className="mb-4 text-sm text-ink/60">
        Vlož odkaz na recept, nebo ho nech vyhledat. Suroviny se napárují a
        kalorie dopočítají automaticky.
      </p>

      <div className="mb-6 flex flex-wrap gap-2 text-xs">
        <StatusPill
          ok={ollama?.reachable && ollama?.model_ok}
          warn={ollama?.reachable && !ollama?.model_ok}
          label={
            !ollama
              ? "Ollama…"
              : !ollama.enabled
              ? "Ollama vypnutá (heuristika)"
              : !ollama.reachable
              ? "Ollama nedostupná"
              : !ollama.model_ok
              ? `Ollama: model ${ollama.model} chybí`
              : `Ollama: ${ollama.model}`
          }
        />
        <StatusPill ok={status.searxng} label={status.searxng ? "Vyhledávání zapnuté" : "Vyhledávání vypnuté"} />
      </div>

      {/* Vložení URL */}
      <section className="mb-8 rounded-xl2 border border-line bg-white p-5 shadow-card">
        <h2 className="mb-3 text-lg font-bold">Z odkazu</h2>
        <div className="flex flex-col gap-2 sm:flex-row">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://…"
            className="flex-1 rounded-full border border-line bg-paper px-4 py-2.5 text-sm outline-none focus:border-basil focus:ring-2 focus:ring-basil/20"
          />
          <Button onClick={() => ingest(url)} disabled={!url || busy}>
            Načíst
          </Button>
        </div>
      </section>

      {/* Vyhledání */}
      <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
        <h2 className="mb-1 text-lg font-bold">Vyhledat na webu</h2>
        {!status.searxng ? (
          <p className="text-sm text-ink/50">
            Vyhledávání je vypnuté. Nastav <code className="rounded bg-line/60 px-1">SEARXNG_URL</code>{" "}
            v konfiguraci appky a zapne se.
          </p>
        ) : (
          <>
            <div className="mb-3 flex flex-col gap-2 sm:flex-row">
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && query && discover()}
                placeholder="např. svíčková, kuřecí kari…"
                className="flex-1 rounded-full border border-line bg-paper px-4 py-2.5 text-sm outline-none focus:border-basil focus:ring-2 focus:ring-basil/20"
              />
              <Button variant="ghost" onClick={discover} disabled={!query || busy}>
                Hledat
              </Button>
            </div>
            {candidates && candidates.length === 0 && (
              <p className="text-sm text-ink/50">Nic nenalezeno.</p>
            )}
            {candidates && candidates.length > 0 && (
              <ul className="divide-y divide-line">
                {candidates.map((c) => (
                  <li key={c.url} className="flex items-center gap-3 py-2.5">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium">{c.title}</p>
                      <p className="truncate text-xs text-ink/40">{c.domain}</p>
                    </div>
                    <Button variant="ghost" onClick={() => ingest(c.url)} disabled={busy}>
                      Přidat
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>

      {busy && <Spinner label="Pracuju…" />}
      {error && (
        <p className="mt-4 rounded-lg bg-miss/10 px-4 py-3 text-sm text-miss">{error}</p>
      )}

      <CrawlerPanel />
      <MatchPanel />
      <SystemPanel />
    </div>
  );
}

function SystemPanel() {
  const [v, setV] = useState(null);
  const [chk, setChk] = useState(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const loadVersion = () => api.sysVersion().then(setV).catch(() => {});
  useEffect(() => {
    loadVersion();
  }, []);

  if (!v || !v.enabled) return null;

  const check = async () => {
    setBusy(true);
    setMsg(null);
    try {
      setChk(await api.sysCheck());
    } catch {
      setMsg("Kontrola selhala.");
    } finally {
      setBusy(false);
    }
  };

  const update = async () => {
    setBusy(true);
    setMsg("Spouštím aktualizaci…");
    try {
      const r = await api.sysUpdate();
      setMsg(r.message || "Aktualizuji…");
      // počkej, až se vrátí s novým commitem
      const before = v.commit;
      let tries = 0;
      const poll = setInterval(async () => {
        tries++;
        try {
          const nv = await api.sysVersion();
          if (nv.commit && nv.commit !== before) {
            clearInterval(poll);
            setV(nv);
            setChk(null);
            setMsg(`Aktualizováno na ${nv.commit} ✓`);
            setBusy(false);
          }
        } catch {
          /* API zrovna restartuje */
        }
        if (tries > 120) {
          clearInterval(poll);
          setMsg("Restart trvá déle – zkontroluj logy kontejneru.");
          setBusy(false);
        }
      }, 2000);
    } catch {
      setMsg("Aktualizace selhala.");
      setBusy(false);
    }
  };

  return (
    <section className="mt-6 rounded-xl2 border border-line bg-white p-5 shadow-card">
      <div className="mb-1 flex items-center justify-between gap-3">
        <h2 className="text-lg font-bold">Verze a aktualizace</h2>
        <span className="nums text-xs text-ink/45">
          {v.branch}@{v.commit || "?"}
        </span>
      </div>
      <p className="mb-1 text-sm text-ink/70">{v.subject || "—"}</p>
      <p className="mb-4 text-xs text-ink/45">{v.date}</p>

      {chk && (
        <p className="mb-3 text-sm">
          {chk.update_available ? (
            <span className="text-miss">
              K dispozici je {chk.behind} nových commitů — naposled: „{chk.remote_subject}"
            </span>
          ) : (
            <span className="text-have">Máš nejnovější verzi ✓</span>
          )}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <Button variant="ghost" onClick={check} disabled={busy}>
          Zkontrolovat aktualizace
        </Button>
        <Button onClick={update} disabled={busy}>
          {busy ? "Pracuji…" : "Aktualizovat z Gitu"}
        </Button>
        {msg && <span className="text-sm text-ink/60">{msg}</span>}
      </div>
      {!v.supervised && (
        <p className="mt-2 text-xs text-ink/45">
          Mimo Docker: po stažení je potřeba ručně restartovat API.
        </p>
      )}
    </section>
  );
}

function MatchPanel() {
  const [st, setSt] = useState(null);
  const [manual, setManual] = useState(false);
  const refresh = () => api.matchStatus().then(setSt).catch(() => {});
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 1500);
    return () => clearInterval(t);
  }, []);

  if (!st) return null;
  const pct =
    st.rows_total > 0
      ? Math.round(((st.rows_total - st.rows_unmatched) / st.rows_total) * 100)
      : 100;
  const phaseLabel = { fuzzy: "páruji proti databázi", llm: "doptávám se AI", kcal: "přepočítávám kalorie" };

  const start = async () => {
    await api.backfill(true);
    refresh();
  };

  return (
    <section className="mt-6 rounded-xl2 border border-line bg-white p-5 shadow-card">
      <div className="mb-1 flex items-center justify-between gap-3">
        <h2 className="text-lg font-bold">Párování surovin</h2>
        <span className="nums text-xs text-ink/50">{pct}% napárováno</span>
      </div>
      <p className="mb-4 text-sm text-ink/60">
        Recepty s nenapárovanými surovinami nemají správný cook-meter ani
        kalorie. Tohle je zkusí dopárovat a chybějící suroviny nechá doplnit AI.
      </p>

      <div className="mb-3 h-2 overflow-hidden rounded-full bg-line">
        <div className="h-full bg-basil transition-all" style={{ width: `${pct}%` }} />
      </div>

      {st.running ? (
        <div>
          <div className="mb-2 flex items-center gap-2 text-sm text-basil-dark">
            <span className="h-3 w-3 animate-spin rounded-full border-2 border-basil border-t-transparent" />
            {phaseLabel[st.phase] || "pracuji"}… {st.done}/{st.total}
          </div>
          <div className="nums flex gap-4 text-sm text-ink/55">
            <span>napárováno <b className="text-have">{st.matched}</b></span>
            <span>nově vytvořeno {st.created}</span>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <div className="nums text-sm">
            <b className="text-miss">{st.rows_unmatched}</b>
            <span className="text-ink/55"> nenapárovaných řádků v </span>
            <b>{st.recipes_unmatched}</b>
            <span className="text-ink/55"> receptech</span>
          </div>
          {st.rows_unmatched > 0 ? (
            <div className="ml-auto flex gap-2">
              <Button variant="ghost" onClick={() => setManual(true)}>
                Ručně…
              </Button>
              <Button onClick={start} disabled={!st.ollama}>
                Dopárovat přes AI
              </Button>
            </div>
          ) : (
            <span className="ml-auto text-sm text-have">Vše napárováno ✓</span>
          )}
        </div>
      )}
      {!st.ollama && st.rows_unmatched > 0 && (
        <p className="mt-2 text-xs text-ink/45">
          Pro doplnění chybějících surovin zapni Ollamu (OLLAMA_URL).
        </p>
      )}
      {manual && <ManualMatch onClose={() => { setManual(false); refresh(); }} />}
    </section>
  );
}

function ManualMatch({ onClose }) {
  const [items, setItems] = useState(null);
  const [total, setTotal] = useState(0);
  const [done, setDone] = useState(0);
  const [busy, setBusy] = useState(null); // raw_text právě zpracovávaný

  const load = () =>
    api.unmatched(60, 0).then((r) => { setItems(r.items); setTotal(r.total_texts); });
  useEffect(() => { load(); }, []);

  const assign = async (raw_text, body) => {
    setBusy(raw_text);
    try {
      await api.matchOne({ raw_text, ...body });
      setItems((cur) => cur.filter((it) => it.raw_text !== raw_text));
      setTotal((t) => Math.max(0, t - 1));
      setDone((d) => d + 1);
    } finally {
      setBusy(null);
    }
  };
  const skip = (raw_text) =>
    setItems((cur) => cur.filter((it) => it.raw_text !== raw_text));

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-ink/40 p-0 sm:items-center sm:p-4" onClick={onClose}>
      <div className="flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-t-2xl bg-white shadow-xl sm:rounded-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-line p-4">
          <div>
            <h2 className="text-lg font-bold">Ruční párování surovin</h2>
            <p className="text-sm text-ink/55">
              Zbývá <b>{total}</b> různých textů{done > 0 && <> · vyřešeno {done}</>}.
              Přiřazení platí pro všechny recepty se stejným textem.
            </p>
          </div>
          <button onClick={onClose} className="text-2xl leading-none text-ink/40 hover:text-ink">×</button>
        </div>

        <div className="flex-1 overflow-auto p-4">
          {items === null ? (
            <Spinner label="Načítám…" />
          ) : items.length === 0 ? (
            <p className="py-8 text-center text-sm text-have">Hotovo ✓ Nic dalšího k dopárování na této stránce.</p>
          ) : (
            <ul className="space-y-3">
              {items.map((it) => (
                <ManualRow key={it.raw_text} item={it} busy={busy === it.raw_text}
                  onAssign={(body) => assign(it.raw_text, body)} onSkip={() => skip(it.raw_text)} />
              ))}
            </ul>
          )}
        </div>

        <div className="border-t border-line p-3 text-right">
          <Button onClick={onClose}>Hotovo</Button>
        </div>
      </div>
    </div>
  );
}

function ManualRow({ item, busy, onAssign, onSkip }) {
  const [newName, setNewName] = useState("");
  return (
    <li className="rounded-lg border border-line/70 p-3">
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="text-sm">
          <span className="font-medium">{item.raw_text}</span>
          <span className="ml-2 text-xs text-ink/45">
            ×{item.count} · např. {item.recipe_title}
          </span>
        </div>
        <button onClick={onSkip} className="shrink-0 text-xs text-ink/40 hover:text-miss">přeskočit</button>
      </div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <div className="flex-1">
          <IngredientPicker
            placeholder="Přiřadit existující surovinu…"
            onPick={(o) => onAssign({ ingredient_id: o.id })}
          />
        </div>
        <div className="flex gap-2">
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && newName.trim() && onAssign({ new_name: newName.trim() })}
            placeholder="…nebo nová surovina"
            className="w-44 rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil"
          />
          <Button variant="ghost" disabled={busy || !newName.trim()}
            onClick={() => onAssign({ new_name: newName.trim() })}>
            {busy ? "…" : "Vytvořit"}
          </Button>
        </div>
      </div>
    </li>
  );
}

function CrawlerPanel() {
  const [st, setSt] = useState(null);

  const refresh = () => api.crawlStatus().then(setSt).catch(() => {});
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, []);

  const start = async () => {
    await api.crawlRun({ mode: "sites", max_recipes: 40 });
    refresh();
  };

  const running = st?.running;
  return (
    <section className="mt-8 rounded-xl2 border border-line bg-white p-5 shadow-card">
      <div className="mb-1 flex items-center justify-between gap-3">
        <h2 className="text-lg font-bold">Objevování receptů</h2>
        {st && (
          <span className="nums text-xs text-ink/50">
            {st.recipes_total} receptů · {st.ingredients_total} surovin
          </span>
        )}
      </div>
      <p className="mb-4 text-sm text-ink/60">
        Projde receptové weby (přes jejich sitemapy) a stahuje nové recepty,
        které ještě nemáš. Cizí přeloží do češtiny, chybějící suroviny doplní.
      </p>

      {running ? (
        <div>
          <div className="mb-3 flex items-center gap-2 text-sm text-basil-dark">
            <span className="h-3 w-3 animate-spin rounded-full border-2 border-basil border-t-transparent" />
            Procházím… {st.current_query ? st.current_query : ""}
          </div>
          <div className="nums mb-3 flex gap-4 text-sm">
            <span>přidáno <b className="text-have">{st.added}</b></span>
            <span className="text-ink/50">nalezeno {st.found}</span>
            <span className="text-ink/50">přeskočeno {st.skipped}</span>
            {st.errors > 0 && <span className="text-miss">chyby {st.errors}</span>}
          </div>
          {st.recent?.length > 0 && (
            <ul className="space-y-1 text-sm text-ink/70">
              {st.recent.slice(-6).reverse().map((r, i) => (
                <li key={i} className="truncate">
                  <span className="text-have">+</span> {r.title}{" "}
                  <span className="text-ink/40">({r.domain})</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={start}>Objevit nové recepty</Button>
          {st && st.domains_count === 0 && (
            <span className="text-xs text-ink/50">
              (bez RECIPE_DOMAINS se použije výchozí sada českých webů)
            </span>
          )}
          {st?.finished_at && (
            <span className="text-sm text-ink/50">
              Poslední běh: přidáno {st.added}, nalezeno {st.found}.
            </span>
          )}
        </div>
      )}
    </section>
  );
}

function StatusPill({ ok, warn, label }) {
  const color = ok
    ? "bg-have/10 text-have"
    : warn
    ? "bg-miss/10 text-miss"
    : "bg-line/60 text-ink/50";
  const dot = ok ? "bg-have" : warn ? "bg-miss" : "bg-ink/30";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 font-medium ${color}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {label}
    </span>
  );
}
