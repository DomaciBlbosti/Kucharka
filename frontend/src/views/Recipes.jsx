import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { IngredientPicker } from "../components/IngredientPicker";
import { CookMeter, EmptyState, Meta, ReadyStamp, Spinner, Stars } from "../components/ui";

const SORTS = [
  ["smart", "Nejblíž uvaření"],
  ["rating", "Hodnocení"],
  ["time", "Nejrychlejší"],
  ["kcal", "Nejméně kcal"],
  ["newest", "Nejnovější"],
];

export default function Recipes() {
  const [recipes, setRecipes] = useState(null);
  const [q, setQ] = useState("");
  const [onlyHave, setOnlyHave] = useState(false);
  const [maxMissing, setMaxMissing] = useState("");
  const [maxTime, setMaxTime] = useState("");
  const [sort, setSort] = useState("smart");

  // "Vařím z" – vybrané suroviny
  const [picked, setPicked] = useState([]);
  const cookMode = picked.length > 0;
  const pickedKey = picked.map((p) => p.id).join(",");

  const addPick = (o) =>
    setPicked((cur) => (cur.some((p) => p.id === o.id) ? cur : [...cur, o]));
  const removePick = (id) => setPicked((cur) => cur.filter((p) => p.id !== id));

  useEffect(() => {
    let live = true;
    setRecipes(null);
    const t = setTimeout(() => {
      const req = cookMode
        ? api.cookFrom(picked.map((p) => p.id))
        : api.recipes({
            q,
            only_have: onlyHave || undefined,
            max_missing: maxMissing,
            max_time: maxTime,
            sort,
          });
      req.then((r) => live && setRecipes(r)).catch(() => live && setRecipes([]));
    }, 200);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [q, onlyHave, maxMissing, maxTime, sort, cookMode, pickedKey]);

  return (
    <div>
      {/* Vařím z */}
      <div className="mb-5 rounded-xl2 border border-line bg-white p-4 shadow-card">
        <div className="mb-2 flex items-center gap-2">
          <span className="text-lg">🧑‍🍳</span>
          <h2 className="font-display text-base font-bold">Vařím z…</h2>
          <span className="text-xs text-ink/45">
            vyber suroviny a najdu recepty, které z nich uvaříš
          </span>
        </div>
        <IngredientPicker onPick={addPick} placeholder="Přidat surovinu, kterou mám…" />
        {cookMode && (
          <div className="mt-3 flex flex-wrap gap-2">
            {picked.map((p) => (
              <button
                key={p.id}
                onClick={() => removePick(p.id)}
                className="inline-flex items-center gap-1.5 rounded-full bg-basil-soft px-3 py-1 text-sm text-basil-dark hover:bg-basil/20"
              >
                {p.name_cs}
                <span className="text-base leading-none">×</span>
              </button>
            ))}
            <button
              onClick={() => setPicked([])}
              className="rounded-full px-3 py-1 text-sm text-ink/45 hover:text-miss"
            >
              vyčistit
            </button>
          </div>
        )}
      </div>

      {/* Filtry – jen mimo režim Vařím z */}
      {!cookMode && (
        <>
          <div className="mb-5 flex flex-wrap items-center gap-2">
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Hledat recept…"
              className="min-w-[12rem] flex-1 rounded-full border border-line bg-white px-4 py-2.5 text-sm outline-none focus:border-basil focus:ring-2 focus:ring-basil/20"
            />
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value)}
              className="rounded-full border border-line bg-white px-3 py-2.5 text-sm outline-none focus:border-basil"
            >
              {SORTS.map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </select>
          </div>

          <div className="mb-6 flex flex-wrap items-center gap-2 text-sm">
            <button
              onClick={() => setOnlyHave((v) => !v)}
              className={`rounded-full px-3 py-1.5 font-medium transition ${
                onlyHave
                  ? "bg-basil text-white"
                  : "bg-white border border-line text-ink/70 hover:border-basil"
              }`}
            >
              Můžu uvařit teď
            </button>
            <label className="flex items-center gap-1.5 rounded-full border border-line bg-white px-3 py-1.5 text-ink/70">
              max chybí
              <input
                type="number"
                min="0"
                value={maxMissing}
                onChange={(e) => setMaxMissing(e.target.value)}
                className="nums w-12 bg-transparent text-center outline-none"
                placeholder="–"
              />
            </label>
            <label className="flex items-center gap-1.5 rounded-full border border-line bg-white px-3 py-1.5 text-ink/70">
              do
              <input
                type="number"
                min="0"
                value={maxTime}
                onChange={(e) => setMaxTime(e.target.value)}
                className="nums w-12 bg-transparent text-center outline-none"
                placeholder="–"
              />
              min
            </label>
          </div>
        </>
      )}

      {cookMode && (
        <p className="mb-4 text-sm text-ink/55">
          Recepty využívající vybrané suroviny — nahoře ty, k nimž chybí nejmíň dalšího.
        </p>
      )}

      {recipes === null ? (
        <Spinner />
      ) : recipes.length === 0 ? (
        <EmptyState title={cookMode ? "Žádný recept z těchto surovin" : "Zatím tu nic není"}>
          {cookMode ? (
            <>Zkus přidat další surovinu nebo nějakou odebrat.</>
          ) : (
            <>
              Přidej první recept přes záložku <strong>Přidat</strong> — vlož URL
              nebo ho nech vyhledat.
            </>
          )}
        </EmptyState>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {recipes.map((r) => (
            <RecipeCard key={r.id} r={r} cookMode={cookMode} />
          ))}
        </div>
      )}
    </div>
  );
}

function RecipeCard({ r, cookMode }) {
  return (
    <Link
      to={`/recept/${r.id}`}
      className="group flex flex-col overflow-hidden rounded-xl2 border border-line bg-white shadow-card transition hover:-translate-y-0.5 hover:shadow-lg"
    >
      <div className="relative aspect-[16/10] overflow-hidden bg-basil-soft">
        {r.image_url ? (
          <img
            src={r.image_url}
            alt=""
            loading="lazy"
            className="h-full w-full object-cover transition duration-500 group-hover:scale-105"
          />
        ) : (
          <div className="flex h-full items-center justify-center text-4xl opacity-30">
            🍽️
          </div>
        )}
        <div className="absolute left-2 top-2">
          <ReadyStamp missing={r.missing_count} />
        </div>
        {cookMode && (
          <div className="absolute right-2 top-2 rounded-full bg-white/90 px-2 py-0.5 text-xs font-semibold text-basil-dark shadow-card">
            {r.have}/{r.total} z výběru
          </div>
        )}
      </div>
      <div className="flex flex-1 flex-col gap-3 p-4">
        <h3 className="line-clamp-2 text-lg font-semibold leading-snug">
          {r.title}
        </h3>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <Stars rating={r.rating} count={r.rating_count} />
          <Meta icon="⏱">{r.total_time ? `${r.total_time} min` : null}</Meta>
          <Meta icon="🔥">
            {r.kcal_per_serving ? `${Math.round(r.kcal_per_serving)} kcal` : null}
          </Meta>
        </div>
        <div className="mt-auto">
          <CookMeter have={r.have} total={r.total} size="sm" />
        </div>
      </div>
    </Link>
  );
}
