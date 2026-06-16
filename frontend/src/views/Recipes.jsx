import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
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

  useEffect(() => {
    let live = true;
    setRecipes(null);
    const t = setTimeout(() => {
      api
        .recipes({
          q,
          only_have: onlyHave || undefined,
          max_missing: maxMissing,
          max_time: maxTime,
          sort,
        })
        .then((r) => live && setRecipes(r))
        .catch(() => live && setRecipes([]));
    }, 200);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [q, onlyHave, maxMissing, maxTime, sort]);

  return (
    <div>
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

      {recipes === null ? (
        <Spinner />
      ) : recipes.length === 0 ? (
        <EmptyState title="Zatím tu nic není">
          Přidej první recept přes záložku <strong>Přidat</strong> — vlož URL nebo
          ho nech vyhledat.
        </EmptyState>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {recipes.map((r) => (
            <RecipeCard key={r.id} r={r} />
          ))}
        </div>
      )}
    </div>
  );
}

function RecipeCard({ r }) {
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
