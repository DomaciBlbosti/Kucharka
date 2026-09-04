import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Button, EmptyState, Spinner } from "../components/ui";

/** Ruční kontrola receptů.
 *
 *  Profil z auditu korpusu říká, KOLIK receptů je podezřelých; tahle stránka
 *  ukazuje PROČ. U každého receptu je vedle sebe text ze zdroje a text, který
 *  appka zobrazuje, tabulka surovin s výsledkem párování, metriky a tagy.
 *  Rozhodnutí se ukládá hned po kliknutí – žádné „uložit vše" na konci, ať se
 *  neztratí půlhodina práce kvůli zavřené záložce.
 */

const PER_PAGE = 5;

function pct(v) {
  return `${Math.round((v || 0) * 100)} %`;
}

function num(v, digits = 0) {
  if (v === null || v === undefined) return "–";
  const s = Number(v).toFixed(digits);
  return s.includes(".") ? s.replace(/\.?0+$/, "") : s;
}

/** České skloňování po číslovce: 1 surovina, 2–4 suroviny, 5+ surovin. */
function plural(n, one, few, many) {
  return `${n} ${n === 1 ? one : n >= 2 && n <= 4 ? few : many}`;
}

function Chip({ tone = "", children }) {
  const tones = {
    "": "bg-ink/5 text-ink/70",
    bad: "bg-miss/10 text-miss",
    good: "bg-basil/10 text-basil",
    ns: "bg-brand/10 text-brand",
  };
  return (
    <span className={`rounded-full px-2.5 py-0.5 text-xs ${tones[tone]}`}>
      {children}
    </span>
  );
}

function Metrics({ r }) {
  const m = r.metrics || {};
  const cov = m.ingr_coverage ?? 0;
  return (
    <div className="flex flex-wrap gap-1.5">
      <Chip tone={cov < 0.5 ? "bad" : cov >= 0.8 ? "good" : ""}>
        pokrytí surovin {pct(cov)}
      </Chip>
      <Chip>{plural(m.n_ingredients ?? 0, "surovina", "suroviny", "surovin")}</Chip>
      <Chip>{plural(m.n_steps ?? 0, "krok", "kroky", "kroků")}</Chip>
      <Chip>{plural(m.instr_chars ?? 0, "znak", "znaky", "znaků")}</Chip>
      <Chip>
        {plural(m.n_cook_verbs ?? 0, "vařicí sloveso", "vařicí slovesa", "vařicích sloves")}
      </Chip>
      {r.feed_score != null && <Chip>skóre {num(r.feed_score, 2)}</Chip>}
      {r.rating != null && (
        <Chip>
          {num(r.rating, 1)}★ ({r.rating_count || 0})
        </Chip>
      )}
      {m.has_time && <Chip>má čas</Chip>}
      {m.has_temp && <Chip>má teplotu</Chip>}
      {r.n_unmatched > 0 && <Chip tone="bad">{r.n_unmatched}× nenapárováno</Chip>}
      {m.has_no_action && <Chip tone="bad">žádná akce v postupu</Chip>}
      {r.translated && <Chip>přeloženo</Chip>}
      {r.hidden && <Chip tone="bad">skryto</Chip>}
      {(r.tags || []).map((t) => (
        <Chip key={`${t.namespace}:${t.slug}`} tone="ns">
          {t.namespace}: {t.label}
        </Chip>
      ))}
    </div>
  );
}

function Ingredients({ rows }) {
  if (!rows?.length)
    return <p className="mt-3 text-sm italic text-miss">Recept nemá žádné suroviny.</p>;
  const showOrig = rows.some((i) => i.original_raw_text);
  return (
    <div className="mt-3 overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-ink/45">
            <th className="py-1 pr-3 font-medium">řádek v receptu</th>
            {showOrig && <th className="py-1 pr-3 font-medium">originál</th>}
            <th className="py-1 pr-3 font-medium">napárováno na</th>
            <th className="py-1 pr-3 text-right font-medium">množství</th>
            <th className="py-1 pr-3 text-right font-medium">gramy</th>
            <th className="py-1 text-right font-medium">kcal</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((i, n) => (
            <tr
              key={n}
              className={`border-t border-line ${
                i.unmatched ? "bg-miss/5" : i.nonfood ? "text-ink/45" : ""
              }`}
            >
              <td className="py-1.5 pr-3">{i.raw_text}</td>
              {showOrig && <td className="py-1.5 pr-3">{i.original_raw_text || "–"}</td>}
              <td className="py-1.5 pr-3">
                {i.matched ? (
                  <>
                    {i.matched}
                    {i.matched_category && (
                      <div className="text-xs text-ink/45">{i.matched_category}</div>
                    )}
                  </>
                ) : i.nonfood ? (
                  <em>ne-surovina</em>
                ) : (
                  <strong className="text-miss">— nenapárováno —</strong>
                )}
                {i.optional && <em className="text-ink/45"> (volitelné)</em>}
              </td>
              <td className="py-1.5 pr-3 text-right whitespace-nowrap">
                {i.amount != null ? `${num(i.amount, 2)} ${i.unit || ""}`.trim() : "–"}
              </td>
              <td className="py-1.5 pr-3 text-right">{num(i.grams)}</td>
              <td className="py-1.5 text-right">{num(i.kcal)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ReviewCard({ r, labels, onSave }) {
  const [picked, setPicked] = useState(r.review?.labels || []);
  const [note, setNote] = useState(r.review?.note || "");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);
  const noteTimer = useRef(null);

  // Nová stránka = jiný recept ve stejné komponentě; bez tohohle by v ní
  // zůstalo rozhodnutí z toho předchozího.
  useEffect(() => {
    setPicked(r.review?.labels || []);
    setNote(r.review?.note || "");
    setErr(null);
  }, [r.id]);

  const persist = useCallback(
    async (nextLabels, nextNote) => {
      setSaving(true);
      setErr(null);
      try {
        await onSave(r.id, nextLabels, nextNote);
      } catch (e) {
        setErr(e?.message || String(e));
      } finally {
        setSaving(false);
      }
    },
    [r.id, onSave],
  );

  const toggle = (slug) => {
    const next = picked.includes(slug)
      ? picked.filter((s) => s !== slug)
      : [...picked, slug];
    setPicked(next);
    persist(next, note);
  };

  // Poznámka se ukládá se zpožděním, ať se neposílá požadavek na každý znak.
  const onNote = (v) => {
    setNote(v);
    clearTimeout(noteTimer.current);
    noteTimer.current = setTimeout(() => persist(picked, v), 800);
  };
  useEffect(() => () => clearTimeout(noteTimer.current), []);

  const done = picked.length > 0;

  return (
    <article
      className={`rounded-2xl border bg-paper p-4 md:p-5 ${
        done ? "border-basil/40" : "border-line"
      }`}
    >
      <div className="flex flex-wrap items-baseline gap-x-2">
        <h2 className="font-display text-lg font-bold">{r.title}</h2>
        {r.original_title && r.original_title !== r.title && (
          <span className="text-sm text-ink/45">← {r.original_title}</span>
        )}
      </div>
      <div className="mt-0.5 break-all text-xs text-ink/45">
        #{r.id} · {r.source_domain || "bez domény"} ·{" "}
        <a href={r.source_url} target="_blank" rel="noopener noreferrer"
          className="text-brand hover:underline">
          zdroj
        </a>{" "}
        ·{" "}
        <Link to={`/recept/${r.id}`} className="text-brand hover:underline">
          otevřít v appce
        </Link>
      </div>

      <div className="mt-3">
        <Metrics r={r} />
      </div>

      <div className={`mt-4 grid gap-4 ${r.original_instructions ? "md:grid-cols-2" : ""}`}>
        {r.original_instructions && (
          <div>
            <h3 className="mb-1 text-[11px] uppercase tracking-wide text-ink/45">
              originál (před překladem)
            </h3>
            <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-xl border border-line bg-ink/[0.02] p-3 font-sans text-sm">
              {r.original_instructions}
            </pre>
          </div>
        )}
        <div>
          <h3 className="mb-1 text-[11px] uppercase tracking-wide text-ink/45">
            {r.original_instructions
              ? "jak to vidíš v appce"
              : "postup (nepřekládáno – zobrazený text je originál)"}
          </h3>
          <pre
            className={`max-h-72 overflow-auto whitespace-pre-wrap rounded-xl border border-line bg-ink/[0.02] p-3 font-sans text-sm ${
              r.instructions ? "" : "italic text-miss"
            }`}
          >
            {r.instructions || "— prázdné —"}
          </pre>
        </div>
      </div>

      <Ingredients rows={r.ingredients} />

      <div className="mt-4 border-t border-line pt-3">
        <div className="flex flex-wrap gap-2">
          {labels.map((l) => {
            const on = picked.includes(l.slug);
            return (
              <button
                key={l.slug}
                type="button"
                title={l.hint}
                onClick={() => toggle(l.slug)}
                className={`rounded-full border px-3 py-1.5 text-sm font-medium transition ${
                  on
                    ? "border-brand bg-brand text-white"
                    : "border-line text-ink/70 hover:border-brand hover:text-brand"
                }`}
              >
                {l.label}
                {l.hides && !on && <span className="opacity-60"> · skryje</span>}
              </button>
            );
          })}
          {saving && <span className="self-center text-xs text-ink/45">ukládám…</span>}
          {err && <span className="self-center text-xs text-miss">{err}</span>}
        </div>
        <input
          value={note}
          onChange={(e) => onNote(e.target.value)}
          placeholder="Poznámka (nepovinné)…"
          className="mt-2 w-full rounded-xl border border-line bg-transparent px-3 py-2 text-sm"
        />
      </div>
    </article>
  );
}

export default function Review() {
  const [meta, setMeta] = useState(null);
  const [data, setData] = useState(null);
  const [stats, setStats] = useState(null);
  const [pick, setPick] = useState("random");
  const [domain, setDomain] = useState("");
  const [onlyUnreviewed, setOnlyUnreviewed] = useState(true);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const top = useRef(null);

  useEffect(() => {
    api.reviewLabels().then(setMeta).catch(() => setMeta({ labels: [], picks: {} }));
  }, []);

  const loadStats = useCallback(() => {
    api.reviewStats().then(setStats).catch(() => {});
  }, []);
  useEffect(loadStats, [loadStats]);

  useEffect(() => {
    let live = true;
    setLoading(true);
    api
      .reviewRecipes({ pick, domain, onlyUnreviewed, page, perPage: PER_PAGE })
      .then((r) => live && setData(r))
      .catch(() => live && setData({ items: [], total: 0, pages: 1, page: 1 }))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [pick, domain, onlyUnreviewed, page]);

  // Změna filtru musí vrátit na první stránku, jinak by se koukalo do prázdna.
  useEffect(() => setPage(1), [pick, domain, onlyUnreviewed]);

  const save = useCallback(
    async (id, labels, note) => {
      const res = await api.reviewSave(id, labels, note);
      // Přepiš rozhodnutí i ve stránce, ať se po překliknutí sem a zpět
      // nezobrazí stará hodnota. Recept ze stránky NEmizí ani při filtru
      // „jen nezkontrolované" – zmizel by pod rukama uprostřed čtení.
      setData((d) =>
        d && {
          ...d,
          items: d.items.map((it) =>
            it.id === id
              ? { ...it, hidden: res.hidden, review: { ...it.review, labels: res.labels, note: res.note } }
              : it,
          ),
        },
      );
      loadStats();
      return res;
    },
    [loadStats],
  );

  const goto = (p) => {
    setPage(p);
    top.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const picks = meta?.picks || {};
  const pages = data?.pages || 1;

  return (
    <div ref={top}>
      <h1 className="font-display text-2xl font-bold">Kontrola receptů</h1>
      <p className="mb-4 mt-1 text-sm text-ink/55">
        U každého receptu vidíš, co přišlo ze zdroje, vedle toho, co appka
        ukazuje, a jak dopadlo párování surovin. Rozhodnutí se ukládá hned po
        kliknutí.
      </p>

      {stats && (
        <div className="mb-4 flex flex-wrap gap-1.5">
          <Chip tone="good">zkontrolováno {stats.reviewed}</Chip>
          <Chip>zbývá {stats.remaining}</Chip>
          {(meta?.labels || [])
            .filter((l) => stats.by_label?.[l.slug])
            .map((l) => (
              <Chip key={l.slug} tone={l.hides ? "bad" : ""}>
                {l.label}: {stats.by_label[l.slug]}
              </Chip>
            ))}
        </div>
      )}

      <div className="mb-5 flex flex-wrap items-end gap-3 rounded-2xl border border-line bg-paper p-3">
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-ink/55">Výběr</span>
          <select
            value={pick}
            onChange={(e) => setPick(e.target.value)}
            className="rounded-xl border border-line bg-transparent px-3 py-2 text-sm"
          >
            {Object.entries(picks).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-ink/55">Doména</span>
          <input
            value={domain}
            onChange={(e) => setDomain(e.target.value.trim())}
            placeholder="všechny"
            className="w-40 rounded-xl border border-line bg-transparent px-3 py-2 text-sm"
          />
        </label>
        <label className="flex items-center gap-2 pb-2 text-sm">
          <input
            type="checkbox"
            checked={onlyUnreviewed}
            onChange={(e) => setOnlyUnreviewed(e.target.checked)}
          />
          jen nezkontrolované
        </label>
        {data && (
          <span className="pb-2 text-sm text-ink/55">
            {data.total} receptů ve frontě
          </span>
        )}
      </div>

      {loading && !data ? (
        <Spinner label="Načítám recepty…" />
      ) : !data?.items?.length ? (
        <EmptyState title="Nic ke kontrole">
          {onlyUnreviewed
            ? "V tomhle výběru je všechno zkontrolované. Zkus jiný výběr nebo odškrtni „jen nezkontrolované“."
            : "Tomuhle výběru neodpovídá žádný recept."}
        </EmptyState>
      ) : (
        <>
          <div className={`flex flex-col gap-4 ${loading ? "opacity-50" : ""}`}>
            {data.items.map((r) => (
              <ReviewCard key={r.id} r={r} labels={meta?.labels || []} onSave={save} />
            ))}
          </div>

          <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
            <Button
              variant="ghost"
              disabled={data.page <= 1}
              onClick={() => goto(data.page - 1)}
            >
              ← předchozí
            </Button>
            <span className="text-sm text-ink/55">
              strana {data.page} z {pages}
            </span>
            <Button
              variant="ghost"
              disabled={data.page >= pages}
              onClick={() => goto(data.page + 1)}
            >
              další →
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
