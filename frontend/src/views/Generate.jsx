import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Button, Spinner } from "../components/ui";

function IndexBar() {
  const [st, setSt] = useState(null);
  const refresh = () => api.genStatus().then(setSt).catch(() => {});
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 1500);
    return () => clearInterval(t);
  }, []);

  if (!st) return null;
  const pct = st.recipes_total ? Math.round((st.indexed / st.recipes_total) * 100) : 0;
  const start = async () => {
    await api.genIndex(false);
    refresh();
  };

  return (
    <div className="mb-5 rounded-xl2 border border-line bg-white p-4 text-sm shadow-card">
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="font-medium">
          Index receptů <span className="text-ink/45">({st.model})</span>
        </span>
        <span className="nums text-ink/55">
          {st.indexed}/{st.recipes_total} ({pct} %)
        </span>
      </div>
      <div className="mb-3 h-2 overflow-hidden rounded-full bg-line">
        <div className="h-full bg-basil transition-all" style={{ width: `${pct}%` }} />
      </div>
      {st.running ? (
        <div className="flex items-center gap-2 text-basil-dark">
          <span className="h-3 w-3 animate-spin rounded-full border-2 border-basil border-t-transparent" />
          Indexuji… {st.done}/{st.total}
        </div>
      ) : st.indexed < st.recipes_total ? (
        <div className="flex items-center gap-3">
          <Button variant="ghost" onClick={start}>
            Doindexovat {st.recipes_total - st.indexed} receptů
          </Button>
          <span className="text-ink/45">Bez indexu RAG vymýšlí jen z textu zadání.</span>
        </div>
      ) : (
        <span className="text-have">Vše naindexováno ✓</span>
      )}
    </div>
  );
}

export default function Generate() {
  const [prompt, setPrompt] = useState("");
  const [maxKcal, setMaxKcal] = useState("");
  const [goodOnly, setGoodOnly] = useState(false);
  const [loading, setLoading] = useState(false);
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  const [saved, setSaved] = useState(null);

  const run = async () => {
    if (!prompt.trim()) return;
    setLoading(true);
    setErr(null);
    setRes(null);
    setSaved(null);
    try {
      const out = await api.generate({
        prompt: prompt.trim(),
        max_kcal: maxKcal ? Number(maxKcal) : null,
        min_rating: goodOnly ? 4 : null,
      });
      setRes(out);
    } catch (e) {
      setErr("Generování selhalo. Běží Ollama? Zkus to znovu.");
    } finally {
      setLoading(false);
    }
  };

  const save = async () => {
    const out = await api.saveGenerated(res.recipe);
    setSaved(out);
  };

  const r = res?.recipe;
  const examples = [
    "lehký kuřecí oběd do 500 kcal",
    "vegetariánské jídlo z čočky",
    "rychlá večeře z toho, co bývá doma",
    "těstoviny s lososem",
  ];

  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between">
        <h1 className="font-display text-2xl font-extrabold">Vymyslet recept</h1>
        <span className="text-xs text-ink/45">RAG · z tvých receptů</span>
      </div>
      <p className="mb-5 text-sm text-ink/60">
        Napiš, na co máš chuť. Kuchařka najde podobné recepty ve své databázi
        a vymyslí z nich nový.
      </p>

      <IndexBar />

      <div className="rounded-xl2 border border-line bg-white p-4 shadow-card">
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) run();
          }}
          rows={2}
          placeholder="např. lehký kuřecí oběd do 500 kcal"
          className="w-full resize-none rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil"
        />
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-1.5 text-sm text-ink/70">
            do
            <input
              type="number"
              value={maxKcal}
              onChange={(e) => setMaxKcal(e.target.value)}
              placeholder="kcal"
              className="nums w-20 rounded-lg border border-line bg-paper px-2 py-1 text-sm outline-none focus:border-basil"
            />
            kcal/porce
          </label>
          <label className="flex items-center gap-1.5 text-sm text-ink/70">
            <input
              type="checkbox"
              checked={goodOnly}
              onChange={(e) => setGoodOnly(e.target.checked)}
              className="accent-basil"
            />
            jen z dobře hodnocených
          </label>
          <div className="ml-auto">
            <Button onClick={run} disabled={loading || !prompt.trim()}>
              {loading ? "Vymýšlím…" : "Vymyslet"}
            </Button>
          </div>
        </div>
        {!prompt && (
          <div className="mt-3 flex flex-wrap gap-2">
            {examples.map((ex) => (
              <button
                key={ex}
                onClick={() => setPrompt(ex)}
                className="rounded-full border border-line px-3 py-1 text-xs text-ink/60 hover:border-basil hover:text-basil-dark"
              >
                {ex}
              </button>
            ))}
          </div>
        )}
      </div>

      {loading && (
        <div className="mt-6">
          <Spinner label="Hledám podobné recepty a vymýšlím…" />
        </div>
      )}
      {err && <p className="mt-6 text-sm text-miss">{err}</p>}

      {r && (
        <div className="mt-6 rounded-xl2 border border-line bg-white p-5 shadow-card">
          <div className="mb-1 flex items-start justify-between gap-3">
            <h2 className="font-display text-xl font-bold">{r.title}</h2>
            <span className="shrink-0 rounded-full bg-basil-soft px-2.5 py-1 text-xs font-medium text-basil-dark">
              vymyšleno
            </span>
          </div>
          <div className="nums mb-4 flex flex-wrap gap-3 text-sm text-ink/55">
            {r.servings ? <span>{r.servings} porce</span> : null}
            {r.total_time ? <span>· {r.total_time} min</span> : null}
            {r.kcal_per_serving ? <span>· {Math.round(r.kcal_per_serving)} kcal/porce</span> : null}
          </div>

          <h3 className="mb-1 text-sm font-semibold text-ink/70">Suroviny</h3>
          <ul className="mb-4 space-y-1 text-sm">
            {r.ingredients?.map((it, i) => (
              <li key={i} className="flex gap-2">
                <span className="text-basil">·</span> {it}
              </li>
            ))}
          </ul>

          {r.steps?.length > 0 && (
            <>
              <h3 className="mb-1 text-sm font-semibold text-ink/70">Postup</h3>
              <ol className="mb-4 list-decimal space-y-1 pl-5 text-sm text-ink/80">
                {r.steps.map((s, i) => (
                  <li key={i}>{s}</li>
                ))}
              </ol>
            </>
          )}

          {r.note && <p className="mb-4 text-sm italic text-ink/55">{r.note}</p>}

          <div className="flex flex-wrap items-center gap-3 border-t border-line pt-4">
            {saved ? (
              <Link
                to={`/recept/${saved.id}`}
                className="text-sm font-medium text-basil-dark underline"
              >
                Uloženo → otevřít recept
              </Link>
            ) : (
              <Button variant="ghost" onClick={save}>
                Uložit do receptů
              </Button>
            )}
            <Button variant="quiet" onClick={run}>
              Vymyslet jinak
            </Button>
          </div>

          {res.sources?.length > 0 && (
            <div className="mt-4 border-t border-line pt-3 text-xs text-ink/50">
              Inspirováno:{" "}
              {res.sources.map((s, i) => (
                <span key={s.id}>
                  {i > 0 && ", "}
                  <Link to={`/recept/${s.id}`} className="hover:text-basil-dark hover:underline">
                    {s.title}
                  </Link>
                  {s.kcal_per_serving ? ` (${Math.round(s.kcal_per_serving)} kcal)` : ""}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
