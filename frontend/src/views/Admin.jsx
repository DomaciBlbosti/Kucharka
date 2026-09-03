import { useEffect, useRef, useState } from "react";
import { api, auth, withToken } from "../api";
import { IngredientPicker } from "../components/IngredientPicker";
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

// Sbalení dlouhých seznamů: ukáže prvních `initial` položek + tlačítko
// „Zobrazit dalších N". Drží stránku administrace čitelnou na mobilu.
function useCollapse(count, initial = 10) {
  const [open, setOpen] = useState(false);
  const shown = open ? count : Math.min(initial, count);
  return { shown, hidden: count - shown, open, toggle: () => setOpen((o) => !o) };
}

function CollapseToggle({ col, noun = "položek" }) {
  if (col.hidden <= 0 && !col.open) return null;
  return (
    <button onClick={col.toggle}
      className="rounded-full border border-line px-3 py-1 text-xs text-ink/60 hover:border-basil hover:text-basil-dark">
      {col.open ? "Sbalit ↑" : `Zobrazit dalších ${col.hidden} ${noun} ↓`}
    </button>
  );
}

function ToolsCard() {
  const [s, setS] = useState(null);
  const [saved, setSaved] = useState(false);
  const [test, setTest] = useState(null);
  const [testing, setTesting] = useState(false);
  const [apiKey, setApiKey] = useState(""); // klíč se z API nikdy nevrací, drží se zvlášť
  const [apiTest, setApiTest] = useState(null);
  const [apiTesting, setApiTesting] = useState(false);
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
  const testApi = async () => {
    setApiTesting(true);
    setApiTest(null);
    try {
      setApiTest(await api.testLlmApi());
    } finally {
      setApiTesting(false);
    }
  };
  const forgetApiKey = async () => {
    const r = await api.adminSaveSettings({ llm_api_key_clear: true });
    setS({ ...s, ...r.settings });
    setApiKey("");
  };
  const save = async () => {
    const keys = ["ollama_url", "ollama_model", "ollama_fast_model", "embed_model",
      "ocr_model", "searxng_url", "translate_to_cs", "auto_ingredients", "scraper_verify_ssl",
      "rag_k", "rag_max_recipes", "ollama_keep_alive", "bg_workers",
      "llm_match_enabled", "llm_match_model", "llm_match_batch_size",
      "llm_match_min_confidence", "llm_match_num_ctx", "llm_match_temperature",
      "llm_match_timeout_s", "translate_model",
      "llm_provider", "llm_api_url", "llm_api_model"];
    const vals = Object.fromEntries(keys.map((k) => [k, s[k]]));
    if (apiKey.trim()) vals.llm_api_key = apiKey.trim();
    const r = await api.adminSaveSettings(vals);
    setS({ ...s, ...r.settings });
    if (apiKey.trim()) setApiKey("");
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
        <Field label="Rychlý model (překlad/parsování/kategorie)" hint="prázdné = stejný jako hlavní">
          <input className={input} value={s.ollama_fast_model || ""}
            onChange={(e) => set("ollama_fast_model", e.target.value)} placeholder="qwen3:1.7b" />
        </Field>
        <Field label="Model jen pro překlad receptů"
          hint="prázdné = rychlý model · zkus multilingvální (aya-expanse:8b, mistral-nemo) · při komerčním API se nepoužije">
          <input className={input} value={s.translate_model || ""}
            onChange={(e) => set("translate_model", e.target.value)} placeholder="aya-expanse:8b" />
        </Field>
        <Field label="Model pro embeddingy (RAG)">
          <input className={input} value={s.embed_model || ""}
            onChange={(e) => set("embed_model", e.target.value)} placeholder="nomic-embed-text" />
        </Field>
        <Field label="OCR model (skenování účtenek)" hint="vision model, např. qwen2.5vl, minicpm-v">
          <input className={input} value={s.ocr_model || ""}
            onChange={(e) => set("ocr_model", e.target.value)} placeholder="qwen2.5vl:7b" />
        </Field>
        <Field label="RAG – počet receptů jako kontext">
          <input type="number" className={input} value={s.rag_k ?? 6}
            onChange={(e) => set("rag_k", Number(e.target.value))} />
        </Field>
        <Field label="RAG – strop indexovaných receptů" hint="indexují se nejlépe hodnocené; 0 = vše (pomalé u 100k+)">
          <input type="number" min="0" step="1000" className={input} value={s.rag_max_recipes ?? 20000}
            onChange={(e) => set("rag_max_recipes", Number(e.target.value))} />
        </Field>
        <Field label="Držet model v paměti (keep_alive)" hint="méně reloadů = rychleji">
          <input className={input} value={s.ollama_keep_alive || ""}
            onChange={(e) => set("ollama_keep_alive", e.target.value)} placeholder="30m" />
        </Field>
        <Field label="Souběžných workerů na pozadí" hint="vyžaduje OLLAMA_NUM_PARALLEL">
          <input type="number" min="1" className={input} value={s.bg_workers ?? 2}
            onChange={(e) => set("bg_workers", Number(e.target.value))} />
        </Field>
      </div>

      <h3 className="mb-3 mt-6 text-sm font-bold text-ink/70">
        LLM pro dávkové úlohy (párování / tagy / kategorie)
      </h3>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Poskytovatel" hint="Ollama = lokální GPU · API = komerční služba (přesnější a rychlejší, platí se za tokeny)">
          <select className={input} value={s.llm_provider || "ollama"}
            onChange={(e) => set("llm_provider", e.target.value)}>
            <option value="ollama">Lokální Ollama</option>
            <option value="api">Komerční API (OpenAI-kompatibilní)</option>
          </select>
        </Field>
      </div>
      {s.llm_provider === "api" && (
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <Field label="API URL" hint="OpenAI: https://api.openai.com/v1 · DeepSeek: https://api.deepseek.com/v1 · funguje cokoliv s /chat/completions">
            <input className={input} value={s.llm_api_url || ""}
              onChange={(e) => set("llm_api_url", e.target.value)}
              placeholder="https://api.openai.com/v1" />
          </Field>
          <Field label="Model" hint="levné mini modely bohatě stačí, např. gpt-4o-mini, deepseek-chat">
            <input className={input} value={s.llm_api_model || ""}
              onChange={(e) => set("llm_api_model", e.target.value)} placeholder="gpt-4o-mini" />
          </Field>
          <Field label="API klíč"
            hint={s.llm_api_key_set ? "klíč je uložený – vyplň jen pro změnu" : "zatím žádný klíč"}>
            <input type="password" className={input} value={apiKey}
              onChange={(e) => { setApiKey(e.target.value); setSaved(false); }}
              placeholder={s.llm_api_key_set ? "•••••••• (uloženo)" : "sk-…"} />
          </Field>
          <div className="flex items-end gap-2 pb-1">
            <Button variant="ghost" onClick={testApi} disabled={apiTesting || !s.llm_api_key_set}>
              {apiTesting ? "Testuji…" : "Test API"}
            </Button>
            {s.llm_api_key_set && (
              <button onClick={forgetApiKey} className="text-sm text-miss hover:underline">
                zapomenout klíč
              </button>
            )}
          </div>
          {apiTest && (
            <p className={`sm:col-span-2 text-sm ${apiTest.ok ? "text-have" : "text-miss"}`}>
              {apiTest.ok ? `✓ API odpovídá (model ${apiTest.model})` : `✗ ${apiTest.error}`}
            </p>
          )}
        </div>
      )}

      <h3 className="mb-3 mt-6 text-sm font-bold text-ink/70">
        Dávkové dopárování surovin (LLM)
      </h3>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Model pro dávkové párování" hint="prázdné = rychlý model výše">
          <input className={input} value={s.llm_match_model || ""}
            onChange={(e) => set("llm_match_model", e.target.value)} placeholder="gemma4:12b" />
        </Field>
        <Field label="Velikost dávky" hint="surovin na jedno LLM volání">
          <input type="number" min="1" className={input} value={s.llm_match_batch_size ?? 40}
            onChange={(e) => set("llm_match_batch_size", Number(e.target.value))} />
        </Field>
        <Field label="num_ctx" hint="kontextové okno – musí pokrýt katalog + dávku">
          <input type="number" min="512" step="512" className={input} value={s.llm_match_num_ctx ?? 16384}
            onChange={(e) => set("llm_match_num_ctx", Number(e.target.value))} />
        </Field>
        <Field label="Timeout volání (s)" hint="lokální 12B model s dlouhým promptem klidně generuje minuty">
          <input type="number" min="30" step="30" className={input} value={s.llm_match_timeout_s ?? 300}
            onChange={(e) => set("llm_match_timeout_s", Number(e.target.value))} />
        </Field>
        <Field label="Temperature">
          <input type="number" min="0" max="2" step="0.1" className={input}
            value={s.llm_match_temperature ?? 0}
            onChange={(e) => set("llm_match_temperature", Number(e.target.value))} />
        </Field>
        <Field label="Minimální confidence" hint="pod touto hranicí se match zahodí">
          <input type="number" min="0" max="1" step="0.05" className={input}
            value={s.llm_match_min_confidence ?? 0.7}
            onChange={(e) => set("llm_match_min_confidence", Number(e.target.value))} />
        </Field>
      </div>
      <div className="mt-3">
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" className="accent-basil" checked={!!s.llm_match_enabled}
            onChange={(e) => set("llm_match_enabled", e.target.checked)} />
          Povolit dávkové dopárování (karta níže v administraci)
        </label>
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
              {test.ocr_model ? (
                <p className="text-ink/70">
                  OCR model <b>{test.ocr_model}</b>:{" "}
                  {test.has_ocr_model ? (
                    <span className="text-have">je k dispozici</span>
                  ) : (
                    <span className="text-miss">chybí — ollama pull {test.ocr_model}</span>
                  )}
                </p>
              ) : (
                <p className="text-ink/45">OCR model nenastaven — skenování účtenek nepůjde.</p>
              )}
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

function ServicesStatusCard() {
  const [jobs, setJobs] = useState(null);
  const [log, setLog] = useState(null);
  const [logger, setLogger] = useState(""); // filtr prefixu loggeru
  const [level, setLevel] = useState("");

  const loadJobs = () => api.sysJobs().then(setJobs).catch(() => setJobs(null));
  const loadLog = () =>
    api.sysLog({ limit: 100, logger, level }).then((r) => setLog(r.items)).catch(() => setLog([]));

  useEffect(() => { loadJobs(); loadLog(); }, [logger, level]);
  useEffect(() => {
    const t = setInterval(() => { loadJobs(); loadLog(); }, 4000);
    return () => clearInterval(t);
  }, [logger, level]);

  const fmt = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    return d.toLocaleString("cs-CZ");
  };
  const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString("cs-CZ") : "");

  const levelColor = {
    ERROR: "text-miss",
    WARNING: "text-amber-600",
    INFO: "text-ink/60",
    DEBUG: "text-ink/35",
  };

  // Předvyplněné filtry loggeru pro rychlé proklikání.
  const loggerPresets = [
    ["", "vše"],
    ["kucharka.scheduler", "plánovač"],
    ["kucharka.crawler", "crawler"],
    ["kucharka.ingest", "zpracování"],
    ["kucharka.translate", "překlad"],
    ["kucharka.llm", "LLM"],
    ["kucharka.lidl", "lidl"],
  ];

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Služby na pozadí</h2>
      <p className="mb-4 text-sm text-ink/60">
        Co je naplánované, kdy poběží příště a jestli zrovna něco běží. Log
        níž ukazuje, co se reálně děje – ať je vidět, jestli se úlohy opravdu
        spouští.
      </p>

      {jobs === null ? (
        <Spinner label="Načítám stav služeb…" />
      ) : (
        <>
          <div className="mb-2 text-xs text-ink/45">
            Plánovač: {jobs.scheduler_running
              ? <span className="text-have">běží</span>
              : <span className="text-miss">neběží</span>}
          </div>
          <div className="mb-5 grid gap-2 sm:grid-cols-2">
            {jobs.jobs.map((j) => (
              <div key={j.id} className="rounded-lg border border-line p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{j.label}</span>
                  {j.running ? (
                    <span className="rounded-full bg-basil-soft px-2 py-0.5 text-xs text-basil-dark">běží&nbsp;právě&nbsp;teď</span>
                  ) : j.scheduled ? (
                    <span className="rounded-full bg-have/10 px-2 py-0.5 text-xs text-have">naplánováno</span>
                  ) : (
                    <span className="rounded-full bg-ink/5 px-2 py-0.5 text-xs text-ink/45">vypnuto</span>
                  )}
                </div>
                <div className="mt-1 text-xs text-ink/45">
                  {j.scheduled ? `příště: ${fmt(j.next_run)}` : "není naplánováno (zapni v nastavení výš)"}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        {loggerPresets.map(([val, label]) => (
          <button
            key={label}
            onClick={() => setLogger(val)}
            className={`rounded-full border px-2.5 py-1 text-xs ${logger === val ? "border-basil text-basil-dark" : "border-line text-ink/50"}`}
          >
            {label}
          </button>
        ))}
        <span className="mx-1 text-line">|</span>
        {["", "WARNING", "ERROR"].map((lv) => (
          <button
            key={lv || "all"}
            onClick={() => setLevel(lv)}
            className={`rounded-full border px-2.5 py-1 text-xs ${level === lv ? "border-basil text-basil-dark" : "border-line text-ink/50"}`}
          >
            {lv || "všechny úrovně"}
          </button>
        ))}
      </div>

      <div className="max-h-80 overflow-auto rounded-lg border border-line bg-paper p-2 font-mono text-xs leading-relaxed">
        {log === null ? (
          <Spinner label="Načítám log…" />
        ) : log.length === 0 ? (
          <p className="text-ink/40">Žádné záznamy (podle filtru).</p>
        ) : (
          log.map((r, i) => (
            <div key={i} className="whitespace-pre-wrap break-words">
              <span className="text-ink/35">{fmtTime(r.ts)}</span>{" "}
              <span className={levelColor[r.level] || "text-ink/50"}>{r.level}</span>{" "}
              <span className="text-ink/40">{r.logger}</span>{" "}
              <span className="text-ink/75">{r.message}</span>
            </div>
          ))
        )}
      </div>
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
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <div className="mb-1 flex items-center justify-between gap-3">
        <h2 className="text-lg font-bold">Objevování receptů (ručně)</h2>
        {st && (
          <span className="nums text-xs text-ink/50">
            {st.recipes_total} receptů · {st.ingredients_total} surovin
          </span>
        )}
      </div>
      <p className="mb-4 text-sm text-ink/60">
        Jednorázově projde receptové weby (přes jejich sitemapy) a stáhne
        nové recepty, které ještě nemáš. Pravidelný běh na pozadí nastavíš
        výše v „Automatické objevování".
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

function CrawlQueueCard() {
  const [stats, setStats] = useState(null);
  const [data, setData] = useState(null); // {items, total, offset, limit}
  const [status, setStatus] = useState(""); // "" = vše
  const [domain, setDomain] = useState("");
  const [offset, setOffset] = useState(0);
  const [resyncing, setResyncing] = useState(false);
  const [resyncMsg, setResyncMsg] = useState(null);
  const [retrying, setRetrying] = useState(false);
  const [pruning, setPruning] = useState(false);
  const [pruneStale, setPruneStale] = useState(null); // počet URL k smazání po dry-runu (čeká na potvrzení)
  const LIMIT = 50;
  const domCol = useCollapse((stats?.domains || []).length, 10);
  const rowCol = useCollapse((data?.items || []).length, 10);

  const loadStats = () => api.crawlQueueStats().then(setStats).catch(() => {});
  const loadItems = () =>
    api.crawlQueue({ status, domain, limit: LIMIT, offset }).then(setData).catch(() => {});

  // Změna filtru → zpět na první stranu.
  useEffect(() => { setOffset(0); }, [status, domain]);
  useEffect(() => { loadStats(); loadItems(); }, [status, domain, offset]);
  useEffect(() => {
    const t = setInterval(() => { loadStats(); loadItems(); }, 5000);
    return () => clearInterval(t);
  }, [status, domain, offset]);

  const resync = async () => {
    setResyncing(true);
    setResyncMsg(null);
    try {
      const r = await api.crawlResync(domain ? [domain] : null);
      const added = Object.values(r.resynced).reduce((a, b) => a + (b > 0 ? b : 0), 0);
      const failed = Object.entries(r.resynced).filter(([, n]) => n < 0).map(([d]) => d);
      setResyncMsg(
        `Přidáno ${added} nových URL do fronty` +
          (failed.length ? ` · selhalo: ${failed.join(", ")}` : "")
      );
      loadStats();
      loadItems();
    } catch (e) {
      setResyncMsg(`chyba: ${e?.message || e}`);
    } finally {
      setResyncing(false);
    }
  };

  const retryErrors = async () => {
    setRetrying(true);
    setResyncMsg(null);
    try {
      const r = await api.crawlRetryErrors(domain || null);
      setResyncMsg(`Přeřazeno ${r.requeued} chybných URL zpět do fronty (pending).`);
      loadStats();
      loadItems();
    } catch (e) {
      setResyncMsg(`chyba: ${e?.message || e}`);
    } finally {
      setRetrying(false);
    }
  };

  // Prune běží na pozadí (celé sitemapy = minuty). Spustíme, pollujeme stav,
  // Prune běží na pozadí (celé sitemapy = minuty). Dry-run spočítá přebytek a
  // nabídne inline potvrzovací tlačítko (žádný window.confirm – ten prohlížeče
  // v PWA/po opakování tiše potlačují, pak to vypadá, že se nic neděje).
  const pollPrune = () =>
    new Promise((resolve, reject) => {
      const iv = setInterval(async () => {
        try {
          const st = await api.crawlPruneStatus();
          if (st.current_domain) {
            setResyncMsg(`Kontroluji sitemapy… (${st.current_domain}, ${st.checked_domains} domén hotovo)`);
          }
          if (!st.running) {
            clearInterval(iv);
            if (st.error) reject(new Error(st.error));
            else resolve(st);
          }
        } catch (e) {
          clearInterval(iv);
          reject(e);
        }
      }, 2000);
    });

  // Fáze 1: dry-run – jen spočítat, kolik by se smazalo.
  const pruneCheck = async () => {
    setPruning(true);
    setPruneStale(null);
    setResyncMsg("Spouštím kontrolu sitemap…");
    try {
      const start = await api.crawlPrune(domain || null, true);
      if (!start.started) {
        setResyncMsg("Pročištění už běží – počkej na dokončení.");
        return;
      }
      const dry = await pollPrune();
      const total = Object.values(dry.result).reduce((a, r) => a + (r.stale_removed || 0), 0);
      if (total === 0) {
        setResyncMsg("Fronta je čistá – nic k odstranění (žádné pending URL mimo sitemapu).");
        return;
      }
      setPruneStale(total); // → zobrazí se inline potvrzovací tlačítko
      setResyncMsg(null);
    } catch (e) {
      setResyncMsg(`chyba: ${e?.message || e}`);
    } finally {
      setPruning(false);
    }
  };

  // Fáze 2: potvrzeno – skutečně smazat.
  const pruneConfirm = async () => {
    setPruning(true);
    setResyncMsg("Mažu přebytečné URL…");
    try {
      const start = await api.crawlPrune(domain || null, false);
      if (!start.started) {
        setResyncMsg("Pročištění už běží – počkej na dokončení.");
        return;
      }
      const done = await pollPrune();
      setResyncMsg(`Odstraněno ${done.removed} přebytečných URL z fronty.`);
      setPruneStale(null);
      loadStats();
      loadItems();
    } catch (e) {
      setResyncMsg(`chyba: ${e?.message || e}`);
    } finally {
      setPruning(false);
    }
  };

  const badge = {
    pending: "bg-line/60 text-ink/60",
    ok: "bg-have/10 text-have",
    skip: "bg-ink/5 text-ink/45",
    error: "bg-miss/10 text-miss",
  };

  const items = data?.items ?? null;
  const total = data?.total ?? 0;
  const page = Math.floor(offset / LIMIT) + 1;
  const pages = Math.max(1, Math.ceil(total / LIMIT));

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-bold">Fronta URL (přehled)</h2>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="ghost" onClick={resync} disabled={resyncing}>
            {resyncing ? "Načítám sitemapy…" : domain ? `Resync ${domain}` : "Resync sitemap"}
          </Button>
          <Button variant="ghost" onClick={retryErrors} disabled={retrying}>
            {retrying ? "Přeřazuji…" : domain ? `Zkusit chybné znovu (${domain})` : "Zkusit chybné znovu"}
          </Button>
          <Button variant="ghost" onClick={pruneCheck} disabled={pruning}>
            {pruning ? "Kontroluji sitemapu…" : domain ? `Pročistit frontu (${domain})` : "Pročistit frontu"}
          </Button>
          <a
            href={api.crawlQueueExportUrl({ status, domain, fmt: "csv" })}
            className="rounded-lg border border-line px-3 py-2 text-sm text-basil-dark hover:bg-basil-soft"
          >
            Stáhnout mapu (CSV)
          </a>
          <a
            href={api.crawlQueueExportUrl({ status, domain, fmt: "json" })}
            className="text-sm text-ink/45 hover:underline"
          >
            JSON
          </a>
        </div>
      </div>
      <p className="mb-4 text-sm text-ink/60">
        Každá URL objevená v sitemapě se sem zapíše jen jednou a zůstává tu i
        s výsledkem – ať je vidět, co crawler zkusil, co vyšlo a co ne (a
        proč). „Resync" natáhne sitemapy hned (jinak max 1× za 6 h). Export
        stáhne celou mapu včetně odkazu na získaný recept.
      </p>
      {pruneStale != null && (
        <div className="mb-3 flex flex-wrap items-center gap-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm">
          <span className="text-ink/75">
            Nalezeno <strong className="nums">{pruneStale}</strong> čekajících URL, které už nejsou
            v aktuální sitemapě. Hotové, přeskočené ani chybné se nemažou.
          </span>
          <div className="ml-auto flex items-center gap-2">
            <Button onClick={pruneConfirm} disabled={pruning}>
              {pruning ? "Mažu…" : `Smazat ${pruneStale}`}
            </Button>
            <button
              onClick={() => { setPruneStale(null); setResyncMsg(null); }}
              className="text-sm text-ink/50 hover:underline"
            >
              zrušit
            </button>
          </div>
        </div>
      )}
      {resyncMsg && <p className="mb-3 text-sm text-ink/70">{resyncMsg}</p>}

      {stats && (
        <div className="mb-4 flex flex-wrap gap-2 text-sm">
          {[
            ["", "vše", stats.pending + stats.ok + stats.skip + stats.error, "bg-line/40"],
            ["pending", "čeká", stats.pending, badge.pending],
            ["ok", "hotovo", stats.ok, badge.ok],
            ["skip", "přeskočeno", stats.skip, badge.skip],
            ["error", "chyba", stats.error, badge.error],
          ].map(([val, label, n, cls]) => (
            <button
              key={label}
              onClick={() => setStatus(val)}
              className={`nums rounded-full px-3 py-1 ${cls} ${status === val ? "ring-2 ring-basil/40" : ""}`}
            >
              {label} {n}
            </button>
          ))}
        </div>
      )}

      {stats?.domains?.length > 0 && (
        <div className="mb-4 flex flex-wrap gap-1.5">
          <button
            onClick={() => setDomain("")}
            className={`rounded-full border px-2.5 py-1 text-xs ${!domain ? "border-basil text-basil-dark" : "border-line text-ink/50"}`}
          >
            všechny domény
          </button>
          {stats.domains.slice(0, domCol.shown).map((d) => (
            <button
              key={d.domain}
              onClick={() => setDomain(d.domain)}
              className={`rounded-full border px-2.5 py-1 text-xs ${domain === d.domain ? "border-basil text-basil-dark" : "border-line text-ink/50"}`}
              title={d.last_synced_at ? `naposledy synced: ${new Date(d.last_synced_at).toLocaleString("cs-CZ")}` : "ještě nesynced"}
            >
              {d.domain} ({d.queued})
            </button>
          ))}
          <CollapseToggle col={domCol} noun="domén" />
        </div>
      )}

      {items === null ? (
        <Spinner label="Načítám frontu…" />
      ) : items.length === 0 ? (
        <p className="text-sm text-ink/45">Nic tu není (podle aktuálního filtru).</p>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="text-xs text-ink/40">
                  <th className="pb-1 pr-3 font-medium">stav</th>
                  <th className="pb-1 pr-3 font-medium">doména</th>
                  <th className="pb-1 pr-3 font-medium">URL / recept</th>
                  <th className="pb-1 font-medium">kdy</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {items.slice(0, rowCol.shown).map((it) => (
                  <tr key={it.id}>
                    <td className="py-1.5 pr-3 align-top">
                      <span className={`rounded-full px-2 py-0.5 text-xs ${badge[it.status] || ""}`}>{it.status}</span>
                    </td>
                    <td className="py-1.5 pr-3 align-top text-ink/60">{it.domain}</td>
                    <td className="py-1.5 pr-3 align-top">
                      {it.status === "ok" && it.recipe_id ? (
                        <a href={`/recept/${it.recipe_id}`} className="text-basil-dark hover:underline">
                          {it.url}
                        </a>
                      ) : (
                        <span className="break-all">{it.url}</span>
                      )}
                      {it.error && <div className="mt-0.5 text-xs text-miss">{it.error}</div>}
                    </td>
                    <td className="py-1.5 align-top text-xs text-ink/40">
                      {it.attempted_at
                        ? new Date(it.attempted_at).toLocaleString("cs-CZ")
                        : new Date(it.discovered_at).toLocaleString("cs-CZ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-2">
            <CollapseToggle col={rowCol} noun="řádků" />
          </div>

          <div className="mt-3 flex items-center justify-between text-sm text-ink/50">
            <span className="nums">
              {offset + 1}–{Math.min(offset + LIMIT, total)} z {total}
            </span>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setOffset(Math.max(0, offset - LIMIT))}
                disabled={offset === 0}
                className="rounded-lg border border-line px-3 py-1 disabled:opacity-40"
              >
                ← předchozí
              </button>
              <span className="nums text-xs">{page}/{pages}</span>
              <button
                onClick={() => setOffset(offset + LIMIT)}
                disabled={offset + LIMIT >= total}
                className="rounded-lg border border-line px-3 py-1 disabled:opacity-40"
              >
                další →
              </button>
            </div>
          </div>
        </>
      )}
    </section>
  );
}

function MatchPanel() {
  const [st, setSt] = useState(null);
  const [manual, setManual] = useState(false);
  const [purging, setPurging] = useState(false);
  const [purgeMsg, setPurgeMsg] = useState(null);
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
  const phaseLabel = { fuzzy: "páruji proti slovníku a databázi", kcal: "přepočítávám kalorie" };

  const start = async () => {
    await api.backfill(true);
    refresh();
  };

  const purgeHeaders = async () => {
    setPurging(true);
    setPurgeMsg(null);
    try {
      const r = await api.purgeHeaders();
      setPurgeMsg(`Smazáno ${r.removed} nadpisů (např. „Marináda:“, „Na ozdobu:“).`);
      refresh();
    } finally {
      setPurging(false);
    }
  };

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <div className="mb-1 flex items-center justify-between gap-3">
        <h2 className="text-lg font-bold">Párování surovin</h2>
        <span className="nums text-xs text-ink/50">{pct}% napárováno</span>
      </div>
      <p className="mb-4 text-sm text-ink/60">
        Recepty s nenapárovanými surovinami nemají správný cook-meter ani
        kalorie. Tohle je rychlá fáze bez AI: slovník aliasů + fuzzy match.
        Co neprojde, řeší „Dávkové dopárování (LLM)" níže a katalog rozhodnutí.
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
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <div className="nums text-sm">
            <b className="text-miss">{st.rows_unmatched}</b>
            <span className="text-ink/55"> řádků čeká na vyřešení v </span>
            <b>{st.recipes_unmatched}</b>
            <span className="text-ink/55"> receptech</span>
            {st.rows_nonfood > 0 && (
              <span className="text-ink/45"> · {st.rows_nonfood} ne-surovin je vyřešených (bez kalorií záměrně)</span>
            )}
          </div>
          {st.rows_unmatched > 0 ? (
            <div className="ml-auto flex gap-2">
              <Button variant="ghost" onClick={purgeHeaders} disabled={purging}>
                {purging ? "…" : "Vyčistit nadpisy"}
              </Button>
              <Button variant="ghost" onClick={() => setManual(true)}>
                Ručně…
              </Button>
              <Button onClick={start}>
                Dopárovat (slovník + fuzzy)
              </Button>
            </div>
          ) : (
            <span className="ml-auto text-sm text-have">Vše napárováno ✓</span>
          )}
        </div>
      )}
      {purgeMsg && <p className="mt-2 text-xs text-ink/50">{purgeMsg}</p>}
      {st.error && (
        <p className="mt-2 text-xs text-miss">Poslední běh skončil chybou: {st.error}</p>
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
              Ne-suroviny (alobal…) a ignorované se tu neukazují – jsou
              v Katalogu rozhodnutí.
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
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
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

function CorpusAuditCard() {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const timer = useRef(null);
  const load = () => api.corpusAuditStatus().then(setSt).catch(() => {});
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
    setErr(null);
    try {
      const r = await api.corpusAuditRun();
      setSt(r.status);
      if (!r.started) setErr("Audit už běží.");
    } catch (e) {
      setErr(e?.message || String(e));
    }
  };

  const fmtWhen = (iso) => (iso ? new Date(iso).toLocaleString("cs-CZ") : null);
  const fmtKb = (b) => `${Math.max(1, Math.round((b || 0) / 1024))} kB`;

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Audit korpusu</h2>
      <p className="mb-4 text-sm text-ink/60">
        Spočítá profil celé databáze receptů (histogramy, rozpad po doménách,
        duplicitní názvy) a vytáhne stratifikovaný vzorek ~600 receptů
        k ručnímu posouzení kvality. Nic v databázi nemění.
      </p>
      {st?.running ? (
        <Spinner label={`Počítám… ${st.done}/${st.total} (${st.phase || ""})`} />
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <Button onClick={run}>Spustit audit</Button>
            {st?.profile_mtime && (
              <span className="text-sm text-ink/60">
                Poslední běh: {fmtWhen(st.profile_mtime)}
                {st.duration_s ? ` (${st.duration_s} s)` : ""}
              </span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <a href={st?.profile_exists ? withToken("/api/admin/corpus-audit/profile") : undefined}
              className={`rounded-full border px-4 py-2 text-sm font-semibold ${
                st?.profile_exists
                  ? "border-brand text-brand hover:bg-brand/5"
                  : "pointer-events-none border-line text-ink/30"}`}>
              Profil (JSON){st?.profile_exists ? ` · ${fmtKb(st.profile_bytes)}` : ""}
            </a>
            <a href={st?.sample_exists ? withToken("/api/admin/corpus-audit/sample") : undefined}
              className={`rounded-full border px-4 py-2 text-sm font-semibold ${
                st?.sample_exists
                  ? "border-brand text-brand hover:bg-brand/5"
                  : "pointer-events-none border-line text-ink/30"}`}>
              Vzorek (JSONL){st?.sample_exists ? ` · ${fmtKb(st.sample_bytes)}` : ""}
            </a>
          </div>
          {st?.error && <p className="text-sm text-miss">Poslední běh selhal: {st.error}</p>}
          {err && <p className="text-sm text-miss">{err}</p>}
        </div>
      )}
    </section>
  );
}

function HmiCard() {
  const [s, setS] = useState(null);
  const [token, setToken] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    api.adminSettings().then((r) => { setS(r); setToken(r.hmi_token || ""); }).catch(() => {});
  }, []);
  if (!s) return null;

  const save = async () => {
    const r = await api.adminSaveSettings({ hmi_token: token });
    setS({ ...s, ...r.settings });
    setSaved(true);
  };

  const url = `${window.location.origin}/hmi${token ? `?token=${encodeURIComponent(token)}` : ""}`;

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Kuchyňský displej / E-ink</h2>
      <p className="mb-4 text-sm text-ink/60">
        Statická stránka bez JS pro tablet/E-ink v kuchyni — ukáže dnešní
        jídelníček a nákup, nebo (po „Odeslat na displej" u receptu) velký
        recept na čtení. Nasměruj na ni displej, ať si ji sám pravidelně stahuje.
      </p>
      <Field label="Token" hint="prázdné = otevřené v rámci LAN">
        <input className={input} value={token} onChange={(e) => setToken(e.target.value)} placeholder="(volitelné)" />
      </Field>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Button onClick={save}>Uložit</Button>
        {saved && <span className="text-sm text-have">Uloženo ✓</span>}
        <a href={url} target="_blank" rel="noreferrer" className="text-sm text-basil underline-offset-2 hover:underline">
          Náhled stránky ↗
        </a>
      </div>
      <p className="mt-2 break-all text-xs text-ink/40">{url}</p>
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
        {enabled && (
          <Button variant="ghost" onClick={async () => {
            try { await api.logout(); } catch { /* i při chybě pokračuj */ }
            auth.clear();
            window.location.reload();
          }}>
            Odhlásit se (toto zařízení)
          </Button>
        )}
        {msg && <span className="text-sm text-ink/60">{msg}</span>}
      </div>
    </section>
  );
}

function TranslateCard() {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
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
    setErr(null);
    const r = await api.runTranslate();
    setSt(r.status);
    if (r.error) setErr(r.error);
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
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-3">
            <Button onClick={run} disabled={!st || !st.ollama}>
              Přeložit cizí recepty
            </Button>
            {st && !st.ollama && (
              <span className="text-sm text-miss">Ollama není dostupná.</span>
            )}
          </div>
          {err && <p className="text-sm text-miss">{err}</p>}
        </div>
      )}
    </section>
  );
}

function RetranslateOriginalsCard() {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const [domain, setDomain] = useState("");
  const timer = useRef(null);
  const load = () => api.retranslateOriginalsStatus().then(setSt).catch(() => {});
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
    setErr(null);
    const r = await api.runRetranslateOriginals(domain.trim());
    setSt(r.status);
    if (r.error) setErr(r.error);
  };

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Znovu přeložit z originálů</h2>
      <p className="mb-4 text-sm text-ink/60">
        Přeloží znovu recepty, které mají <b>uložený originál</b> — aktuální
        cestou (lepší prompt, případně jiný model / komerční API). Nic se
        nestahuje z webu, jen se překládá. Hodí se na opravu starých mizerných
        překladů; volitelně jen pro jednu doménu.
      </p>
      {st && (
        <p className="mb-3 text-sm text-ink/70">
          Receptů s uloženým originálem: <b>{st.candidates}</b>
          {st.finished_at && !st.running && (
            <> · naposledy přeloženo: <b>{st.translated}</b>
              {st.domain ? <> (doména {st.domain})</> : null}</>
          )}
        </p>
      )}
      {st?.running ? (
        <Spinner label={`Překládám z originálů… ${st.done}/${st.total} (hotovo ${st.translated})`} />
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-3">
            <input className="w-64 rounded-lg border border-line px-3 py-2 text-sm"
              value={domain} onChange={(e) => setDomain(e.target.value)}
              placeholder="jen doména, např. bbcgoodfood.com (prázdné = vše)" />
            <Button onClick={run} disabled={!st || st.candidates === 0}>
              Znovu přeložit
            </Button>
            {st && st.candidates === 0 && (
              <span className="text-sm text-ink/50">Žádné recepty s originálem.</span>
            )}
          </div>
          <p className="text-xs text-ink/50">
            Pozor: u velkého počtu receptů poběží dlouho (jeden LLM dotaz na
            recept). Nenapárované suroviny s novým textem dorovná nejbližší
            kolečko zpracování.
          </p>
          {err && <p className="text-sm text-miss">{err}</p>}
        </div>
      )}
    </section>
  );
}

function ResetTranslateCard() {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const timer = useRef(null);
  const load = () => api.retranslateResetStatus().then(setSt).catch(() => {});
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
    setErr(null);
    const r = await api.runRetranslateReset();
    setSt(r.status);
    if (r.error) setErr(r.error);
  };

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Dohledat chybějící originály</h2>
      <p className="mb-4 text-sm text-ink/60">
        Nové překlady si originál (předlohu) ukládají automaticky — v detailu
        receptu jde přepnout mezi překladem a originálem. Tohle je jen pro
        <b> staré recepty přeložené předtím</b>, než appka originál ukládala:
        znovu stáhne zdrojovou stránku a přeloží ji čerstvě i s originálem.
        Nejde použít u receptů z fotky/AI (bez zdrojové URL).
      </p>
      {st && (
        <p className="mb-3 text-sm text-ink/70">
          Recepty bez uloženého originálu: <b>{st.candidates}</b>
          {st.finished_at && !st.running && (
            <> · naposledy doplněno: <b>{st.reset}</b></>
          )}
        </p>
      )}
      {st?.running ? (
        <Spinner label={`Stahuji a překládám znovu… ${st.done}/${st.total} (doplněno ${st.reset})`} />
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-3">
            <Button onClick={run} disabled={!st || !st.ollama || st.candidates === 0}>
              Dohledat originály
            </Button>
            {st && !st.ollama && <span className="text-sm text-miss">Ollama není dostupná.</span>}
            {st && st.ollama && st.candidates === 0 && (
              <span className="text-sm text-have">Nic k obnovení ✓</span>
            )}
          </div>
          {err && <p className="text-sm text-miss">{err}</p>}
        </div>
      )}
    </section>
  );
}

function LidlAccountsCard() {
  const [accounts, setAccounts] = useState(null);
  const [form, setForm] = useState({ label: "", country: "CZ", language: "cs", refresh_token: "" });
  const [adding, setAdding] = useState(false);
  const [err, setErr] = useState(null);
  const [syncing, setSyncing] = useState(null); // id právě synchronizovaného účtu
  const [syncMsg, setSyncMsg] = useState({});

  const load = () => api.lidlAccounts().then(setAccounts).catch(() => setAccounts([]));
  useEffect(() => { load(); }, []);

  const addAccount = async () => {
    setErr(null);
    if (!form.label.trim() || !form.refresh_token.trim()) {
      setErr("Vyplň popisek a refresh token.");
      return;
    }
    setAdding(true);
    try {
      await api.lidlAddAccount(form);
      setForm({ label: "", country: form.country, language: form.language, refresh_token: "" });
      load();
    } catch (e) {
      setErr(e?.message || "Přidání selhalo.");
    } finally {
      setAdding(false);
    }
  };

  const removeAccount = async (id) => {
    await api.lidlDeleteAccount(id);
    load();
  };

  const toggleEnabled = async (acc) => {
    await api.lidlUpdateAccount(acc.id, { enabled: !acc.enabled });
    load();
  };

  const syncNow = async (id) => {
    setSyncing(id);
    setSyncMsg((m) => ({ ...m, [id]: null }));
    try {
      const r = await api.lidlSyncAccount(id);
      setSyncMsg((m) => ({
        ...m,
        [id]: `${r.tickets_new} nových účtenek, ${r.items_matched} položek do spíže${
          r.items_unmatched ? `, ${r.items_unmatched} bez shody` : ""
        }`,
      }));
      load();
    } catch (e) {
      setSyncMsg((m) => ({ ...m, [id]: `chyba: ${e?.message || e}` }));
    } finally {
      setSyncing(null);
    }
  };

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Lidl Plus účty</h2>
      <p className="mb-4 text-sm text-ink/60">
        Nákupy z propojených účtů se pravidelně stahují a napárované položky
        přidávají do spíže. Přihlašovací token se získává mimo appku (na PC:{" "}
        <code className="rounded bg-paper px-1">pip install lidl-plus</code>,
        pak <code className="rounded bg-paper px-1">lidl-plus auth</code>) –
        appka sama browser/2FA neřeší, jen s tokenem dál pracuje. Jde přidat
        víc účtů zvlášť (např. tvůj a manželčin).
      </p>

      {accounts === null ? (
        <Spinner label="Načítám účty…" />
      ) : (
        <div className="mb-4 space-y-2">
          {accounts.length === 0 && <p className="text-sm text-ink/45">Zatím žádný účet.</p>}
          {accounts.map((a) => (
            <div key={a.id} className="rounded-lg border border-line p-3">
              <div className="flex flex-wrap items-center gap-3">
                <span className="font-medium">{a.label}</span>
                <span className="text-xs text-ink/45">{a.country} · {a.language}</span>
                <label className="flex items-center gap-1.5 text-sm text-ink/60">
                  <input type="checkbox" className="accent-basil" checked={a.enabled}
                    onChange={() => toggleEnabled(a)} />
                  aktivní
                </label>
                <Button variant="ghost" onClick={() => syncNow(a.id)} disabled={syncing === a.id}>
                  {syncing === a.id ? "Synchronizuji…" : "Sync teď"}
                </Button>
                <button onClick={() => removeAccount(a.id)} className="ml-auto text-sm text-miss hover:underline">
                  odebrat
                </button>
              </div>
              <p className="mt-1 text-xs text-ink/45">
                {a.last_sync_at ? `naposledy: ${new Date(a.last_sync_at).toLocaleString("cs-CZ")}` : "ještě nesynchronizováno"}
                {a.last_sync_error && <span className="text-miss"> · chyba: {a.last_sync_error}</span>}
              </p>
              {syncMsg[a.id] && <p className="mt-1 text-xs text-ink/70">{syncMsg[a.id]}</p>}
            </div>
          ))}
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Popisek (kdo)">
          <input className={input} value={form.label}
            onChange={(e) => setForm({ ...form, label: e.target.value })}
            placeholder="např. Aleš, manželka" />
        </Field>
        <Field label="Země">
          <input className={input} value={form.country}
            onChange={(e) => setForm({ ...form, country: e.target.value.toUpperCase() })}
            placeholder="CZ" />
        </Field>
        <Field label="Refresh token" hint="Získáš přes lidl-plus auth na PC (viz výše).">
          <input className={input} value={form.refresh_token}
            onChange={(e) => setForm({ ...form, refresh_token: e.target.value })}
            placeholder="dlouhý řetězec z lidl-plus auth" />
        </Field>
        <Field label="Jazyk">
          <input className={input} value={form.language}
            onChange={(e) => setForm({ ...form, language: e.target.value.toLowerCase() })}
            placeholder="cs" />
        </Field>
      </div>
      {err && <p className="mt-2 text-sm text-miss">{err}</p>}
      <div className="mt-3">
        <Button onClick={addAccount} disabled={adding}>{adding ? "Ověřuji…" : "Přidat účet"}</Button>
      </div>
    </section>
  );
}

export default function Admin() {
  return (
    <div className="space-y-6">
      <h1 className="font-display text-2xl font-extrabold">Administrace</h1>
      <ToolsCard />
      <ServicesCard />
      <ServicesStatusCard />
      <LidlAccountsCard />
      <CrawlerCard />
      <CrawlerPanel />
      <CrawlQueueCard />
      <CorpusAuditCard />
      <MatchPanel />
      <LlmMatchCard />
      <DecisionsCard />
      <TranslateCard />
      <RetranslateOriginalsCard />
      <ResetTranslateCard />
      <CategorizeCard />
      <TagCard />
      <DomainsCard />
      <NutriCard />
      <BackupCard />
      <HmiCard />
      <SystemPanel />
      <SecurityCard />
    </div>
  );
}

function ServicesCard() {
  const [s, setS] = useState(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => { api.adminSettings().then(setS).catch(() => {}); }, []);
  if (!s) return null;
  const set = (k, v) => { setS({ ...s, [k]: v }); setSaved(false); };
  const save = async () => {
    const r = await api.adminSaveSettings({
      auto_translate_enabled: s.auto_translate_enabled,
      auto_translate_interval_min: Number(s.auto_translate_interval_min),
      auto_match_enabled: s.auto_match_enabled,
      auto_match_interval_min: Number(s.auto_match_interval_min),
      lidl_sync_enabled: s.lidl_sync_enabled,
      lidl_sync_interval_min: Number(s.lidl_sync_interval_min),
    });
    setS({ ...s, ...r.settings });
    setSaved(true);
  };
  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Služby na pozadí</h2>
      <p className="mb-4 text-sm text-ink/60">
        Automaticky průběžně překládají a párují nově stažené recepty. Běží
        jeden po druhém (nervou si GPU).
      </p>
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" className="accent-basil" checked={!!s.auto_translate_enabled}
              onChange={(e) => set("auto_translate_enabled", e.target.checked)} />
            Automatický překlad
          </label>
          <label className="flex items-center gap-1.5 text-sm text-ink/60">
            každých
            <input type="number" min="1" className={`${input} w-20`} value={s.auto_translate_interval_min ?? 180}
              onChange={(e) => set("auto_translate_interval_min", e.target.value)} />
            min
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" className="accent-basil" checked={!!s.auto_match_enabled}
              onChange={(e) => set("auto_match_enabled", e.target.checked)} />
            Automatické zpracování surovin (párování + kategorie + tagy)
          </label>
          <label className="flex items-center gap-1.5 text-sm text-ink/60">
            každých
            <input type="number" min="1" className={`${input} w-20`} value={s.auto_match_interval_min ?? 180}
              onChange={(e) => set("auto_match_interval_min", e.target.value)} />
            min
          </label>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" className="accent-basil" checked={!!s.lidl_sync_enabled}
              onChange={(e) => set("lidl_sync_enabled", e.target.checked)} />
            Synchronizace Lidl účtenek
          </label>
          <label className="flex items-center gap-1.5 text-sm text-ink/60">
            každých
            <input type="number" min="1" className={`${input} w-20`} value={s.lidl_sync_interval_min ?? 360}
              onChange={(e) => set("lidl_sync_interval_min", e.target.value)} />
            min
          </label>
        </div>
      </div>
      <div className="mt-4 flex items-center gap-3">
        <Button onClick={save}>Uložit</Button>
        {saved && <span className="text-sm text-have">Uloženo ✓ (přeplánováno)</span>}
      </div>
    </section>
  );
}

function LlmMatchCard() {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const timer = useRef(null);
  const load = () => api.llmMatchStatus().then(setSt).catch(() => {});
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
    setErr(null);
    const r = await api.runLlmMatch();
    setSt(r.status);
    if (r.error) setErr(r.error);
  };

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Dávkové dopárování (LLM)</h2>
      <p className="mb-4 text-sm text-ink/60">
        Doplní nenapárované suroviny u receptů čekajících na ruční revizi –
        dávkově, s deduplikací, přes jedno LLM volání na desítky surovin
        najednou. Vhodné pro velké množství nenamatchnutých řádků, kdy je
        běžné párování (výše) pomalé.
      </p>
      {st && (
        <p className="mb-3 text-sm text-ink/70">
          Nenapárováno: <b>{st.unmatched}</b>
        </p>
      )}
      {st?.running ? (
        <Spinner label={
          st.phase === "dictionary"
            ? `Aplikuji slovník… (${st.dict_applied} řádků bez LLM)`
            : st.phase === "embeddings"
            ? `Počítám embeddingy… ${st.embed_done}/${st.embed_total} dávek`
            : st.phase === "context"
            ? `Dořešuji v kontextu receptů… ${st.ctx_done}/${st.ctx_total} (dořešeno ${st.ctx_applied}, smazáno poznámek ${st.ctx_removed})`
            : st.phase === "nutrition"
            ? "Odhaduji výživu nově založených surovin…"
            : st.phase === "kcal"
            ? "Přepočítávám kalorie dotčených receptů…"
            : `Párování… ${st.done}/${st.total} (napárováno ${st.applied}, návrhů ${st.suggested})`
        } />
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-3">
            <Button
              onClick={run}
              disabled={!st || !st.enabled || !(st.llm_ready ?? st.ollama) || st.unmatched === 0}
            >
              Spustit dávkové dopárování
            </Button>
            {st && !st.enabled && (
              <span className="text-sm text-miss">
                Vypnuto — zapni v Administraci → Nástroje (servery) → „Povolit dávkové dopárování".
              </span>
            )}
            {st && st.enabled && !(st.llm_ready ?? st.ollama) && (
              <span className="text-sm text-miss">LLM není dostupné (Ollama / API klíč).</span>
            )}
            {st && st.enabled && (st.llm_ready ?? st.ollama) && st.unmatched === 0 && (
              <span className="text-sm text-have">Nic k dopárování ✓</span>
            )}
          </div>
          {st && (st.dict_applied || st.applied || st.suggested || st.no_match || st.nonfood || st.errors) ? (
            <p className="text-sm text-ink/60">
              Poslední běh: slovníkem {st.dict_applied} řádků · LLM napárovalo {st.applied}
              {st.created ? <> (z toho {st.created} nově založených surovin)</> : null} ·
              návrhy k potvrzení {st.suggested} · bez shody {st.no_match} · non-food {st.nonfood}
              {st.errors ? <span className="text-miss"> · chyby {st.errors}</span> : null}
              {(st.ctx_applied || st.ctx_removed) ? (
                <> · kontextem receptu dořešeno {st.ctx_applied}, smazáno poznámek/textu {st.ctx_removed}</>
              ) : null}
              {" "}<span className="text-ink/45">(návrhy a bez shody čekají v katalogu níže)</span>
            </p>
          ) : null}
          {st?.last_error && (
            <p className="text-sm text-miss">
              Poslední chyba LLM: <span className="font-mono text-xs">{st.last_error}</span>
              <span className="text-ink/45"> — u timeoutů zmenši dávku a num_ctx (model se musí vejít do VRAM),
              případně zvyš timeout, nebo přepni na komerční API v Nástrojích.</span>
            </p>
          )}
          {err && <p className="text-sm text-miss">{err}</p>}
        </div>
      )}
    </section>
  );
}

const DECISION_STATUS = {
  suggested: ["návrh", "bg-amber-100 text-amber-700"],
  no_match: ["bez shody", "bg-miss/10 text-miss"],
  error: ["chyba", "bg-miss/10 text-miss"],
  applied: ["napárováno", "bg-have/10 text-have"],
  nonfood: ["není surovina", "bg-ink/5 text-ink/45"],
  ignored: ["ignorováno", "bg-ink/5 text-ink/45"],
};

function DecisionRow({ d, busy, onResolve }) {
  const [newName, setNewName] = useState("");
  const [label, cls] = DECISION_STATUS[d.status] || [d.status, "bg-ink/5 text-ink/45"];
  const open = d.status === "suggested" || d.status === "no_match" || d.status === "error";
  return (
    <li className="rounded-lg border border-line/70 p-3">
      <div className="mb-1 flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">{d.sample_text}</span>
        <span className={`rounded-full px-2 py-0.5 text-xs ${cls}`}>{label}</span>
        {d.occurrences > 0 && <span className="nums text-xs text-ink/45">×{d.occurrences} řádků</span>}
        {d.confidence != null && (
          <span className="nums text-xs text-ink/45">jistota {Math.round(d.confidence * 100)} %</span>
        )}
        {d.model && <span className="text-xs text-ink/35">{d.model}</span>}
      </div>
      {(d.ingredient_name || d.suggested_name || d.error) && (
        <p className="mb-2 text-xs text-ink/55">
          {d.ingredient_name && <>návrh: <b>{d.ingredient_name}</b></>}
          {!d.ingredient_id && d.suggested_name && (
            <>návrh AI: založit novou surovinu <b>„{d.suggested_name}"</b></>
          )}
          {d.error && <span className="text-miss"> {d.error}</span>}
        </p>
      )}
      {open ? (
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          {d.status === "suggested" && d.ingredient_id && (
            <Button onClick={() => onResolve({ action: "accept" })} disabled={busy}>
              {busy ? "…" : `Přijmout „${d.ingredient_name}"`}
            </Button>
          )}
          {(d.status === "suggested" || d.status === "no_match") && !d.ingredient_id && d.suggested_name && (
            <Button onClick={() => onResolve({ action: "accept" })} disabled={busy}>
              {busy ? "…" : `Vytvořit „${d.suggested_name}"`}
            </Button>
          )}
          <div className="min-w-0 flex-1">
            <IngredientPicker
              placeholder="Přiřadit jinou surovinu…"
              onPick={(o) => onResolve({ action: "assign", ingredient_id: o.id })}
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && newName.trim()
                && onResolve({ action: "assign", new_name: newName.trim() })}
              placeholder="…nebo nová surovina"
              className="w-40 rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil"
            />
            <button onClick={() => onResolve({ action: "nonfood" })} disabled={busy}
              className="text-xs text-ink/50 hover:text-ink hover:underline">není surovina</button>
            <button onClick={() => onResolve({ action: "ignore" })} disabled={busy}
              className="text-xs text-ink/50 hover:text-ink hover:underline">ignorovat</button>
            <button onClick={() => onResolve({ action: "retry" })} disabled={busy}
              className="text-xs text-ink/50 hover:text-ink hover:underline"
              title="Smaže rozhodnutí – příští běh dopárování se AI zeptá znovu">zeptat se znovu</button>
          </div>
        </div>
      ) : (
        <div className="flex items-center gap-3 text-xs text-ink/45">
          {d.status !== "applied" && (
            <button onClick={() => onResolve({ action: "retry" })} disabled={busy}
              className="hover:text-ink hover:underline">zeptat se znovu</button>
          )}
        </div>
      )}
    </li>
  );
}

function DecisionsCard() {
  const [status, setStatus] = useState("review");
  const [q, setQ] = useState("");
  const [data, setData] = useState(null);
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(null);
  const [msg, setMsg] = useState(null);
  const LIMIT = 30;
  const rowCol = useCollapse((data?.items || []).length, 8);

  const load = () =>
    api.decisions({ status, q, limit: LIMIT, offset }).then(setData).catch(() => {});
  useEffect(() => { setOffset(0); }, [status, q]);
  useEffect(() => { load(); }, [status, q, offset]);

  const resolve = async (id, body) => {
    setBusy(id);
    setMsg(null);
    try {
      const r = await api.resolveDecision(id, body);
      if (r.rows !== undefined) {
        setMsg(`Napárováno ${r.rows} řádků${r.ingredient_name ? ` na „${r.ingredient_name}"` : ""}.`);
      }
      load();
    } catch (e) {
      setMsg(`chyba: ${e?.message || e}`);
    } finally {
      setBusy(null);
    }
  };

  const sum = data?.summary || {};
  const reviewCount = (sum.suggested || 0) + (sum.no_match || 0) + (sum.error || 0);
  const chips = [
    ["review", "k vyřešení", reviewCount],
    ["suggested", "návrhy", sum.suggested || 0],
    ["no_match", "bez shody", sum.no_match || 0],
    ["error", "chyby", sum.error || 0],
    ["applied", "napárováno", sum.applied || 0],
    ["nonfood", "není surovina", sum.nonfood || 0],
    ["ignored", "ignorováno", sum.ignored || 0],
    ["", "vše", Object.values(sum).reduce((a, b) => a + b, 0)],
  ];

  const items = data?.items ?? null;
  const total = data?.total ?? 0;
  const page = Math.floor(offset / LIMIT) + 1;
  const pages = Math.max(1, Math.ceil(total / LIMIT));

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Katalog rozhodnutí (párování)</h2>
      <p className="mb-4 text-sm text-ink/60">
        Každý výsledek dávkového dopárování se tu ukládá – i zamítnutí a chyby.
        AI se na už rozhodnuté položky znovu neptá. „Návrhy" jsou shody s nízkou
        jistotou čekající na tvoje potvrzení; „bez shody" AI nedokázala přiřadit
        a čekají na ruční výběr.
      </p>

      <div className="mb-3 flex flex-wrap items-center gap-1.5">
        {chips.map(([val, label, n]) => (
          <button key={label} onClick={() => setStatus(val)}
            className={`nums rounded-full border px-2.5 py-1 text-xs ${
              status === val ? "border-basil text-basil-dark" : "border-line text-ink/50"}`}>
            {label} {n}
          </button>
        ))}
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="hledat…"
          className="ml-auto w-40 rounded-lg border border-line bg-paper px-3 py-1.5 text-sm outline-none focus:border-basil"
        />
      </div>
      {(sum.error || 0) > 0 && (
        <div className="mb-3">
          <Button variant="ghost" disabled={busy === "retry-errors"}
            onClick={async () => {
              setBusy("retry-errors");
              setMsg(null);
              try {
                const r = await api.retryErrorDecisions();
                setMsg(`${r.reset} chybových položek vráceno do fronty – spusť dávkové dopárování.`);
                load();
              } catch (e) {
                setMsg(`chyba: ${e?.message || e}`);
              } finally {
                setBusy(null);
              }
            }}>
            Zkusit všechny chybové znovu ({sum.error})
          </Button>
        </div>
      )}
      {msg && <p className="mb-3 text-sm text-ink/70">{msg}</p>}

      {items === null ? (
        <Spinner label="Načítám katalog…" />
      ) : items.length === 0 ? (
        <p className="text-sm text-ink/45">
          {status === "review"
            ? "Nic nečeká na rozhodnutí ✓ (spusť dávkové dopárování výše, ať se katalog plní)"
            : "Nic tu není (podle filtru)."}
        </p>
      ) : (
        <>
          <ul className="space-y-2">
            {items.slice(0, rowCol.shown).map((d) => (
              <DecisionRow key={d.id} d={d} busy={busy === d.id}
                onResolve={(body) => resolve(d.id, body)} />
            ))}
          </ul>
          <div className="mt-2">
            <CollapseToggle col={rowCol} noun="položek" />
          </div>
          <div className="mt-3 flex items-center justify-between text-sm text-ink/50">
            <span className="nums">{offset + 1}–{Math.min(offset + LIMIT, total)} z {total}</span>
            <div className="flex items-center gap-2">
              <button onClick={() => setOffset(Math.max(0, offset - LIMIT))} disabled={offset === 0}
                className="rounded-lg border border-line px-3 py-1 disabled:opacity-40">← předchozí</button>
              <span className="nums text-xs">{page}/{pages}</span>
              <button onClick={() => setOffset(offset + LIMIT)} disabled={offset + LIMIT >= total}
                className="rounded-lg border border-line px-3 py-1 disabled:opacity-40">další →</button>
            </div>
          </div>
        </>
      )}
    </section>
  );
}

function CategorizeCard() {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const timer = useRef(null);
  const load = () => api.categorizeStatus().then(setSt).catch(() => {});
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
    setErr(null);
    const r = await api.runCategorize();
    setSt(r.status);
    if (r.error) setErr(r.error);
  };

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Kategorizace surovin</h2>
      <p className="mb-4 text-sm text-ink/60">
        Zařadí suroviny do hierarchie (např. „maso › drůbeží › kuřecí") pro
        snadnější hledání a filtrování. Běží jen pro dosud nezařazené.
      </p>
      {st && (
        <p className="mb-3 text-sm text-ink/70">
          Surovin celkem: <b>{st.total_ingredients}</b> · nezařazených:{" "}
          <b>{st.uncategorized}</b>
        </p>
      )}
      {st?.running ? (
        <Spinner label={`Kategorizuji… ${st.done}/${st.total}`} />
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-3">
            <Button onClick={run} disabled={!st || !(st.llm_ready ?? st.ollama) || st.uncategorized === 0}>
              Zařadit do kategorií
            </Button>
            {st && !(st.llm_ready ?? st.ollama) && <span className="text-sm text-miss">LLM není dostupné (Ollama / API klíč).</span>}
            {st && (st.llm_ready ?? st.ollama) && st.uncategorized === 0 && (
              <span className="text-sm text-have">Vše zařazeno ✓</span>
            )}
          </div>
          {st?.errors > 0 && !st.running && (
            <p className="text-xs text-miss">
              Poslední běh: {st.errors} dávek selhalo (zkusí se příště znovu).
              {st.last_error && <> Příčina: <span className="font-mono">{st.last_error}</span></>}
            </p>
          )}
          {err && <p className="text-sm text-miss">{err}</p>}
        </div>
      )}
    </section>
  );
}

function TagCard() {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const timer = useRef(null);
  const load = () => api.tagStatus().then(setSt).catch(() => {});
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
    setErr(null);
    const r = await api.runTagging();
    setSt(r.status);
    if (r.error) setErr(r.error);
  };

  return (
    <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
      <h2 className="mb-1 text-lg font-bold">Otagování receptů</h2>
      <p className="mb-4 text-sm text-ink/60">
        Přiřadí receptům tagy (chod, denní doba, chuť, technika, dieta,
        kuchyně) pro filtrování. Vybírá jen z pevného seznamu, nevymýšlí
        nové. Běží jen pro dosud neotagované.
      </p>
      {st && (
        <p className="mb-3 text-sm text-ink/70">
          Receptů celkem: <b>{st.total_recipes}</b> · neotagovaných:{" "}
          <b>{st.untagged}</b>
        </p>
      )}
      {st?.running ? (
        <Spinner label={`Otagovávám… ${st.done}/${st.total}`} />
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-3">
            <Button onClick={run} disabled={!st || !(st.llm_ready ?? st.ollama) || st.untagged === 0}>
              Otagovat recepty
            </Button>
            {st && !(st.llm_ready ?? st.ollama) && <span className="text-sm text-miss">LLM není dostupné (Ollama / API klíč).</span>}
            {st && (st.llm_ready ?? st.ollama) && st.untagged === 0 && (
              <span className="text-sm text-have">Vše otagováno ✓</span>
            )}
          </div>
          {st?.errors > 0 && !st.running && (
            <p className="text-xs text-miss">
              Poslední běh: {st.errors} dávek selhalo (zkusí se příště znovu).
              {st.last_error && <> Příčina: <span className="font-mono">{st.last_error}</span></>}
            </p>
          )}
          {err && <p className="text-sm text-miss">{err}</p>}
        </div>
      )}
    </section>
  );
}
