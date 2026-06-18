import { useEffect, useRef, useState } from "react";
import { api, withToken } from "../api";
import { Button, Spinner } from "../components/ui";

function Field({ label, children, hint }) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-ink/70">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-ink/45">{hint}</span>}
    </label>
  );
}

const input =
  "w-full rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil";

function ToolsCard() {
  const [s, setS] = useState(null);
  const [saved, setSaved] = useState(false);
  const [test, setTest] = useState(null);
  const [testing, setTesting] = useState(false);
  useEffect(() => {
    api.adminSettings().then(setS).catch(() => {});
  }, []);
  if (!s) return <Spinner label="Načítám nastavení…" />;

  const set = (k, v) => {
    setS({ ...s, [k]: v });
    setSaved(false);
  };
  const testOllama = async () => {
    setTesting(true);
    setTest(null);
    try {
      setTest(await api.testOllama());
    } finally {
      setTesting(false);
    }
  };
  const save = async () => {
    const keys = ["ollama_url", "ollama_model", "embed_model", "searxng_url",
      "translate_to_cs", "auto_ingredients", "scraper_verify_ssl", "rag_k"];
    const vals = Object.fromEntries(keys.map((k) => [k, s[k]]));
    const r = await api.adminSaveSettings(vals);
    setS({ ...s, ...r.settings });
    setSaved(true);
  };

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-3 text-lg font-bold">Nástroje (servery)</h2>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Ollama URL" hint={s.ollama_enabled ? "připojeno" : "nenastaveno"}>
          <input className={input} value={s.ollama_url || ""}
            onChange={(e) => set("ollama_url", e.target.value)}
            placeholder="http://host.docker.internal:11434" />
        </Field>
        <Field label="SearXNG URL" hint={s.searxng_enabled ? "připojeno" : "nepovinné"}>
          <input className={input} value={s.searxng_url || ""}
            onChange={(e) => set("searxng_url", e.target.value)}
            placeholder="http://…:8088 (nepovinné)" />
        </Field>
        <Field label="Model pro chat/generování">
          <input className={input} value={s.ollama_model || ""}
            onChange={(e) => set("ollama_model", e.target.value)} placeholder="qwen3:8b" />
        </Field>
        <Field label="Model pro embeddingy (RAG)">
          <input className={input} value={s.embed_model || ""}
            onChange={(e) => set("embed_model", e.target.value)} placeholder="nomic-embed-text" />
        </Field>
        <Field label="RAG – počet receptů jako kontext">
          <input type="number" className={input} value={s.rag_k ?? 6}
            onChange={(e) => set("rag_k", Number(e.target.value))} />
        </Field>
      </div>
      <div className="mt-4 flex flex-wrap gap-4">
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" className="accent-basil" checked={!!s.translate_to_cs}
            onChange={(e) => set("translate_to_cs", e.target.checked)} />
          Překládat cizí recepty do češtiny
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" className="accent-basil" checked={!!s.auto_ingredients}
            onChange={(e) => set("auto_ingredients", e.target.checked)} />
          Auto-dotváření surovin přes AI
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" className="accent-basil" checked={!!s.scraper_verify_ssl}
            onChange={(e) => set("scraper_verify_ssl", e.target.checked)} />
          Ověřovat SSL při stahování
        </label>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button onClick={save}>Uložit nastavení</Button>
        {saved && <span className="text-sm text-have">Uloženo ✓ (platí ihned)</span>}
        <Button variant="ghost" onClick={testOllama} disabled={testing}>
          {testing ? "Testuji…" : "Test připojení Ollama"}
        </Button>
      </div>
      {test && (
        <div className="mt-3 rounded-lg border border-line bg-paper p-3 text-sm">
          {test.reachable ? (
            <>
              <p className="text-have">✓ Ollama odpovídá ({test.url})</p>
              <p className="mt-1 text-ink/70">
                Chat model <b>{test.chat_model}</b>:{" "}
                {test.has_chat_model ? (
                  <span className="text-have">je k dispozici</span>
                ) : (
                  <span className="text-miss">chybí — ollama pull {test.chat_model}</span>
                )}
              </p>
              <p className="text-ink/70">
                Embed model <b>{test.embed_model}</b>:{" "}
                {test.has_embed_model ? (
                  <span className="text-have">je k dispozici</span>
                ) : (
                  <span className="text-miss">chybí — ollama pull {test.embed_model}</span>
                )}
              </p>
              {test.models?.length > 0 && (
                <p className="mt-1 text-xs text-ink/45">Modely: {test.models.join(", ")}</p>
              )}
            </>
          ) : (
            <p className="text-miss">
              ✗ Ollama neodpovídá{test.url ? ` (${test.url})` : ""}: {test.error}
            </p>
          )}
        </div>
      )}
    </section>
  );
}

function DomainsCard() {
  const [text, setText] = useState("");
  const [saved, setSaved] = useState(false);
  const fileRef = useRef();
  useEffect(() => {
    api.adminSettings().then((s) =>
      setText((s.recipe_domains || "").split(",").filter(Boolean).join("\n"))
    );
  }, []);
  const save = async () => {
    await api.adminSaveSettings({ recipe_domains: text });
    setSaved(true);
  };
  const onImport = async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const r = await api.domainsImport(f);
    setText((r.domains || []).join("\n"));
    setSaved(true);
  };
  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Domény receptů</h2>
      <p className="mb-3 text-sm text-ink/60">Které weby smí crawler procházet (jedna na řádek).</p>
      <textarea rows={6} className={`${input} font-mono`} value={text}
        onChange={(e) => { setText(e.target.value); setSaved(false); }} />
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Button onClick={save}>Uložit</Button>
        <a href={withToken("/api/admin/recipe-domains/export")}
          className="rounded-full bg-basil-soft px-4 py-2 text-sm font-semibold text-basil-dark hover:bg-basil/15">
          Export
        </a>
        <Button variant="ghost" onClick={() => fileRef.current?.click()}>Import ze souboru</Button>
        <input ref={fileRef} type="file" accept=".txt,.csv" hidden onChange={onImport} />
        {saved && <span className="text-sm text-have">Uloženo ✓</span>}
      </div>
    </section>
  );
}

function NutriCard() {
  const [merge, setMerge] = useState(true);
  const [st, setSt] = useState(null);
  const fileRef = useRef();
  const poll = useRef();

  const refresh = () => api.nutridbStatus().then(setSt).catch(() => {});
  useEffect(() => {
    refresh();
    return () => clearInterval(poll.current);
  }, []);

  const onFile = async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    await api.nutridbImport(f, merge);
    poll.current = setInterval(async () => {
      const s = await api.nutridbStatus();
      setSt(s);
      if (!s.running) clearInterval(poll.current);
    }, 1500);
  };

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">NutriDatabáze</h2>
      <p className="mb-3 text-sm text-ink/60">
        Nahraj CSV export z nutridatabaze.cz — zpřesní výživu surovin a přepočítá kalorie receptů.
      </p>
      <label className="mb-3 flex items-center gap-2 text-sm">
        <input type="checkbox" className="accent-basil" checked={merge}
          onChange={(e) => setMerge(e.target.checked)} />
        sloučit ollama-odhady s reálnými potravinami
      </label>
      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={() => fileRef.current?.click()} disabled={st?.running}>
          {st?.running ? "Importuji…" : "Nahrát CSV a importovat"}
        </Button>
        <input ref={fileRef} type="file" accept=".csv" hidden onChange={onFile} />
        <a href={withToken("/api/admin/ingredients/export")}
          className="rounded-full bg-basil-soft px-4 py-2 text-sm font-semibold text-basil-dark hover:bg-basil/15">
          Export surovin (CSV)
        </a>
      </div>
      {st && (st.running || st.message) && (
        <div className="mt-3 text-sm text-ink/70">
          {st.running && <span className="mr-2 inline-block h-3 w-3 animate-spin rounded-full border-2 border-basil border-t-transparent align-middle" />}
          {st.message}
          {st.finished_at && !st.running && (
            <span className="nums ml-1 text-ink/55">
              ({st.inserted} nových, {st.enriched} zpřesněných, {st.merged} sloučených, {st.recomputed} receptů přepočítáno)
            </span>
          )}
        </div>
      )}
    </section>
  );
}

function BackupCard() {
  const [mode, setMode] = useState("replace");
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef();
  const onFile = async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    if (mode === "replace" && !confirm("Režim 'nahradit' smaže současná data. Pokračovat?")) {
      e.target.value = "";
      return;
    }
    setBusy(true);
    setRes(null);
    try {
      setRes(await api.dbImport(f, mode));
    } finally {
      setBusy(false);
      e.target.value = "";
    }
  };
  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Záloha databáze</h2>
      <p className="mb-3 text-sm text-ink/60">
        Kompletní data (recepty, suroviny, spíž, embeddingy) jako jeden JSON.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <a href={withToken("/api/admin/db/export")}
          className="rounded-full bg-basil px-4 py-2 text-sm font-semibold text-white hover:bg-basil-dark">
          Exportovat zálohu
        </a>
        <select className={`${input} w-auto`} value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="replace">Import: nahradit vše</option>
          <option value="merge">Import: sloučit</option>
        </select>
        <Button variant="ghost" onClick={() => fileRef.current?.click()} disabled={busy}>
          {busy ? "Importuji…" : "Importovat zálohu"}
        </Button>
        <input ref={fileRef} type="file" accept=".json" hidden onChange={onFile} />
      </div>
      {res && (
        <p className={`mt-3 text-sm ${res.ok ? "text-have" : "text-miss"}`}>
          {res.ok
            ? `Obnoveno: ${Object.entries(res.counts).map(([k, v]) => `${k} ${v}`).join(", ")}`
            : `Chyba: ${res.error}`}
        </p>
      )}
    </section>
  );
}

function CrawlerCard() {
  const [s, setS] = useState(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    api.adminSettings().then(setS).catch(() => {});
  }, []);
  if (!s) return null;
  const set = (k, v) => { setS({ ...s, [k]: v }); setSaved(false); };
  const save = async () => {
    const r = await api.adminSaveSettings({
      crawler_enabled: s.crawler_enabled,
      crawler_interval_min: Number(s.crawler_interval_min),
      crawler_max_per_run: Number(s.crawler_max_per_run),
    });
    setS({ ...s, ...r.settings });
    setSaved(true);
  };
  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Automatické objevování (crawler)</h2>
      <p className="mb-4 text-sm text-ink/60">
        Na pozadí pravidelně prochází weby z domén výše a stahuje nové recepty.
      </p>
      <label className="mb-3 flex items-center gap-2 text-sm">
        <input type="checkbox" className="accent-basil" checked={!!s.crawler_enabled}
          onChange={(e) => set("crawler_enabled", e.target.checked)} />
        Zapnout crawler na pozadí
      </label>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Interval (minuty)" hint="jak často proběhne">
          <input type="number" className={input} value={s.crawler_interval_min ?? 360}
            onChange={(e) => set("crawler_interval_min", e.target.value)} />
        </Field>
        <Field label="Receptů za běh" hint="strop na jeden průchod">
          <input type="number" className={input} value={s.crawler_max_per_run ?? 30}
            onChange={(e) => set("crawler_max_per_run", e.target.value)} />
        </Field>
      </div>
      <div className="mt-4 flex items-center gap-3">
        <Button onClick={save}>Uložit</Button>
        {saved && <span className="text-sm text-have">Uloženo ✓ (přeplánováno)</span>}
      </div>
    </section>
  );
}

function SecurityCard() {
  const [enabled, setEnabled] = useState(null);
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [msg, setMsg] = useState(null);
  useEffect(() => {
    api.adminSettings().then((s) => setEnabled(s.auth_enabled)).catch(() => {});
  }, []);
  if (enabled === null) return null;

  const save = async () => {
    if (pw !== pw2) { setMsg("Hesla se neshodují."); return; }
    await api.setPassword(pw);
    setMsg(pw ? "Heslo nastaveno — budeš přihlášen znovu." : "Zabezpečení vypnuto.");
    setPw(""); setPw2("");
    setTimeout(() => window.location.reload(), 1200);
  };

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Zabezpečení heslem</h2>
      <p className="mb-4 text-sm text-ink/60">
        {enabled
          ? "Aplikace je chráněná heslem. Nové heslo nastavíš níže; prázdné pole zabezpečení vypne."
          : "Aplikace je teď bez hesla. Nastavením hesla zamkneš celé rozhraní."}
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label={enabled ? "Nové heslo (prázdné = vypnout)" : "Heslo"}>
          <input type="password" className={input} value={pw}
            onChange={(e) => setPw(e.target.value)} placeholder="••••••••" />
        </Field>
        <Field label="Heslo znovu">
          <input type="password" className={input} value={pw2}
            onChange={(e) => setPw2(e.target.value)} placeholder="••••••••" />
        </Field>
      </div>
      <div className="mt-4 flex items-center gap-3">
        <Button onClick={save}>{enabled ? "Změnit / vypnout" : "Nastavit heslo"}</Button>
        {msg && <span className="text-sm text-ink/60">{msg}</span>}
      </div>
    </section>
  );
}

function TranslateCard() {
  const [st, setSt] = useState(null);
  const timer = useRef(null);
  const load = () => api.translateStatus().then(setSt).catch(() => {});
  useEffect(() => {
    load();
    return () => clearInterval(timer.current);
  }, []);
  useEffect(() => {
    if (st?.running && !timer.current) {
      timer.current = setInterval(load, 2000);
    } else if (!st?.running && timer.current) {
      clearInterval(timer.current);
      timer.current = null;
    }
  }, [st?.running]);

  const run = async () => {
    const r = await api.runTranslate();
    setSt(r.status);
  };

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Překlad receptů do češtiny</h2>
      <p className="mb-4 text-sm text-ink/60">
        Zpětně přeloží cizojazyčné recepty (titul, postup i suroviny) — hodí se
        pro recepty stažené, když nebyla dostupná Ollama.
      </p>
      {st && (
        <p className="mb-3 text-sm text-ink/70">
          Receptů celkem: <b>{st.recipes_total}</b> · pravděpodobně cizích:{" "}
          <b>{st.foreign_estimate}</b>
          {st.finished_at && !st.running && (
            <> · naposledy přeloženo: <b>{st.translated}</b></>
          )}
        </p>
      )}
      {st?.running ? (
        <div className="text-sm text-ink/70">
          <Spinner label={`Překládám… ${st.done}/${st.total} (přeloženo ${st.translated})`} />
        </div>
      ) : (
        <div className="flex items-center gap-3">
          <Button onClick={run} disabled={!st || !st.ollama}>
            Přeložit cizí recepty
          </Button>
          {st && !st.ollama && (
            <span className="text-sm text-miss">Ollama není dostupná.</span>
          )}
        </div>
      )}
    </section>
  );
}

export default function Admin() {
  return (
    <div className="space-y-6">
      <h1 className="font-display text-2xl font-extrabold">Administrace</h1>
      <ToolsCard />
      <CrawlerCard />
      <TranslateCard />
      <DomainsCard />
      <NutriCard />
      <BackupCard />
      <SecurityCard />
    </div>
  );
}
