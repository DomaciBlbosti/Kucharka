import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { IngredientPicker } from "../components/IngredientPicker";
import { CookMeter, EmptyState, Meta, ReadyStamp, Spinner, Stars } from "../components/ui";

const SORTS = [
  ["feed", "Doporučené"],
  ["smart", "Nejblíž uvaření"],
  ["rating", "Hodnocení"],
  ["time", "Nejrychlejší"],
  ["kcal", "Nejméně kcal"],
  ["newest", "Nejnovější"],
];

const PAGE_SIZE = 30;

export default function Recipes() {
  const [recipes, setRecipes] = useState(null);
  const [total, setTotal] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);

  // Filtry žijí v URL, ne ve stavu komponenty. Bez toho se po otevření
  // receptu a návratu zpět nastavení ztratilo – prohlížeč sice vrátil
  // stránku, ale komponenta se namountovala znovu s prázdnými filtry.
  // V URL je navíc výběr sdílitelný odkazem.
  const [params, setParams] = useSearchParams();
  const par = (key, def = "") => params.get(key) ?? def;
  const setPar = (patch) =>
    setParams(
      (cur) => {
        const next = new URLSearchParams(cur);
        Object.entries(patch).forEach(([k, v]) => {
          if (v === "" || v === false || v === null || v === undefined) next.delete(k);
          else if (Array.isArray(v)) {
            next.delete(k);
            v.forEach((x) => next.append(k, x));
          } else next.set(k, String(v));
        });
        return next;
      },
      { replace: true },
    );

  const q = par("q");
  const setQ = (v) => setPar({ q: v });
  const onlyHave = par("only_have") === "1";
  const setOnlyHave = (v) => setPar({ only_have: v ? "1" : "" });
  const maxMissing = par("max_missing");
  const setMaxMissing = (v) => setPar({ max_missing: v });
  const maxTime = par("max_time");
  const setMaxTime = (v) => setPar({ max_time: v });
  const sort = par("sort", "feed");
  const setSort = (v) => setPar({ sort: v === "feed" ? "" : v });
  const category = par("category");
  const setCategory = (v) => setPar({ category: v });
  const selectedTags = params.getAll("tags"); // ["namespace:slug", ...]
  const setSelectedTags = (v) => setPar({ tags: v });

  const [cats, setCats] = useState([]);
  const [tagGroups, setTagGroups] = useState([]);
  const [tagsOpen, setTagsOpen] = useState(false);
  // Sloučit varianty téhož jídla do jedné karty (12 tisíc názvů v korpusu
  // má dvě a víc variant). Volba se pamatuje mezi návštěvami.
  const groupVariants = params.has("group")
    ? params.get("group") === "1"
    : localStorage.getItem("recipes.group") !== "0";
  const setGroupVariants = (v) => {
    localStorage.setItem("recipes.group", v ? "1" : "0");
    setPar({ group: v ? "1" : "0" });
  };

  // Vypnutá spíž: dostupnost ani filtry na ni navázané nemá cenu ukazovat.
  const [pantryOn, setPantryOn] = useState(true);

  useEffect(() => {
    api.ingredientCategories().then(setCats).catch(() => setCats([]));
    api.recipeTags().then(setTagGroups).catch(() => setTagGroups([]));
    api.appConfig().then((c) => setPantryOn(c.pantry)).catch(() => {});
  }, []);

  const toggleTag = (key) =>
    setSelectedTags((cur) => (cur.includes(key) ? cur.filter((t) => t !== key) : [...cur, key]));

  // "Vařím z" – vybrané suroviny
  // Vybrané suroviny jsou v URL taky – jinak by se po návratu z receptu
  // ztratily stejně jako filtry. Názvy se drží ve stavu jen kvůli popiskům
  // na štítcích; zdrojem pravdy jsou id v URL.
  const [pickedNames, setPickedNames] = useState({});
  const pickedIds = params.getAll("ing").map(Number).filter(Boolean);
  const picked = pickedIds.map((id) => ({ id, name_cs: pickedNames[id] || `#${id}` }));
  const cookMode = pickedIds.length > 0;
  const pickedKey = pickedIds.join(",");

  const addPick = (o) => {
    setPickedNames((cur) => ({ ...cur, [o.id]: o.name_cs || o.name }));
    if (!pickedIds.includes(o.id)) setPar({ ing: [...pickedIds, o.id] });
  };
  const removePick = (id) => setPar({ ing: pickedIds.filter((p) => p !== id) });

  const filters = {
    q,
    only_have: onlyHave || undefined,
    max_missing: maxMissing,
    max_time: maxTime,
    category: category || undefined,
    tags: selectedTags,
    sort,
    group: groupVariants || undefined,
  };

  useEffect(() => {
    let live = true;
    setRecipes(null);
    setTotal(0);
    const t = setTimeout(() => {
      if (cookMode) {
        api
          .cookFrom(pickedIds, { q, tags: selectedTags })
          .then((r) => {
            if (!live) return;
            setRecipes(r);
            setTotal(r.length);
          })
          .catch(() => live && setRecipes([]));
        return;
      }
      api
        .recipes({ ...filters, limit: PAGE_SIZE, offset: 0 })
        .then((r) => {
          if (!live) return;
          setRecipes(r.items);
          setTotal(r.total);
        })
        .catch(() => live && setRecipes([]));
    }, 200);
    return () => {
      live = false;
      clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, onlyHave, maxMissing, maxTime, sort, category, selectedTags, cookMode, pickedKey,
      groupVariants]);

  const loadMore = async () => {
    if (cookMode || loadingMore || recipes === null) return;
    setLoadingMore(true);
    try {
      const r = await api.recipes({ ...filters, limit: PAGE_SIZE, offset: recipes.length });
      setRecipes((cur) => [...cur, ...r.items]);
      setTotal(r.total);
    } finally {
      setLoadingMore(false);
    }
  };

  const hasMore = !cookMode && recipes !== null && recipes.length < total;

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

      {/* Filtry. V režimu „Vařím z" se dřív schovávaly úplně, takže nešlo
          chtít „z rýže, vegetariánské a indické". Skryje se jen to, co tam
          nemá význam: řazení (řadí se podle nejmenšího doplnění), filtry na
          spíž a slučování variant. */}
      <div className="mb-5 flex flex-wrap items-center gap-2">
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Hledat recept…"
              className="min-w-[12rem] flex-1 rounded-full border border-line bg-white px-4 py-2.5 text-sm outline-none focus:border-basil focus:ring-2 focus:ring-basil/20"
            />
            {!cookMode && (
              <select
                value={sort}
                onChange={(e) => setSort(e.target.value)}
                className="rounded-full border border-line bg-white px-3 py-2.5 text-sm outline-none focus:border-basil"
              >
                {SORTS.filter(([v]) => pantryOn || v !== "smart").map(([v, l]) => (
                  <option key={v} value={v}>
                    {l}
                  </option>
                ))}
              </select>
            )}
            {cats.length > 0 && !cookMode && (
              <select
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                className="rounded-full border border-line bg-white px-3 py-2.5 text-sm outline-none focus:border-basil"
              >
                <option value="">Všechny kategorie</option>
                {cats.map((c) => (
                  <option key={c.category} value={c.category}>
                    {c.category} ({c.count})
                  </option>
                ))}
              </select>
            )}
            {tagGroups.length > 0 && (
              <button
                onClick={() => setTagsOpen((v) => !v)}
                className={`rounded-full px-3 py-2.5 text-sm font-medium transition ${
                  selectedTags.length > 0
                    ? "bg-basil text-white"
                    : "border border-line bg-white text-ink/70 hover:border-basil"
                }`}
              >
                🏷️ Tagy{selectedTags.length > 0 ? ` (${selectedTags.length})` : ""}
              </button>
            )}
          </div>

          {tagsOpen && (
            <div className="mb-5 rounded-xl2 border border-line bg-white p-4 shadow-card">
              <div className="flex items-center justify-between">
                <p className="text-xs text-ink/40">
                  Víc tagů ve stejné skupině = nebo. Víc skupin = zároveň.
                </p>
                {selectedTags.length > 0 && (
                  <button onClick={() => setSelectedTags([])} className="text-xs text-ink/45 hover:text-miss">
                    zrušit výběr
                  </button>
                )}
              </div>
              <div className="mt-2 space-y-3">
                {tagGroups.map((g) => (
                  <div key={g.namespace}>
                    <p className="mb-1.5 text-xs font-semibold text-ink/55">{g.label}</p>
                    <div className="flex flex-wrap gap-1.5">
                      {g.tags.map((t) => {
                        const key = `${g.namespace}:${t.slug}`;
                        const active = selectedTags.includes(key);
                        return (
                          <button
                            key={key}
                            onClick={() => toggleTag(key)}
                            className={`rounded-full px-2.5 py-1 text-xs font-medium transition ${
                              active
                                ? "bg-basil text-white"
                                : "border border-line bg-paper text-ink/60 hover:border-basil"
                            }`}
                          >
                            {t.label}
                            {t.count > 0 && <span className="ml-1 opacity-60">({t.count})</span>}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {!cookMode && (
          <div className="mb-6 flex flex-wrap items-center gap-2 text-sm">
            {pantryOn && (
            <button
              onClick={() => setOnlyHave(!onlyHave)}
              className={`rounded-full px-3 py-1.5 font-medium transition ${
                onlyHave
                  ? "bg-basil text-white"
                  : "bg-white border border-line text-ink/70 hover:border-basil"
              }`}
            >
              Můžu uvařit teď
            </button>
            )}
            <button
              onClick={() => setGroupVariants(!groupVariants)}
              title="Recepty se stejným názvem se ukážou jako jedna položka"
              className={`rounded-full px-3 py-1.5 font-medium transition ${
                groupVariants
                  ? "bg-basil text-white"
                  : "bg-white border border-line text-ink/70 hover:border-basil"
              }`}
            >
              Sloučit varianty
            </button>
            {pantryOn && (
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
            )}
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
        <>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {recipes.map((r) => (
              <RecipeCard key={r.id} r={r} cookMode={cookMode} pantryOn={pantryOn} />
            ))}
          </div>
          {!cookMode && (
            <div className="mt-6 flex flex-col items-center gap-2">
              <p className="nums text-xs text-ink/40">
                Zobrazeno {recipes.length} z {total}
              </p>
              {hasMore && (
                <button
                  onClick={loadMore}
                  disabled={loadingMore}
                  className="rounded-full border border-line bg-white px-5 py-2 text-sm font-medium text-ink/70 hover:border-basil disabled:opacity-50"
                >
                  {loadingMore ? "Načítám…" : "Načíst další"}
                </button>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function RecipeCard({ r, cookMode, pantryOn = true }) {
  // Karta s víc variantami vede na seznam variant, ne rovnou na jeden recept –
  // jinak by se ostatní zdroje téhož jídla nedaly proklikat.
  const grouped = r.variants > 1 && r.group_key;
  return (
    <Link
      to={grouped ? `/varianty/${encodeURIComponent(r.group_key)}` : `/recept/${r.id}`}
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
        {pantryOn && (
          <div className="absolute left-2 top-2">
            <ReadyStamp missing={r.missing_count} total={r.total} />
          </div>
        )}
        {cookMode ? (
          <div className="absolute right-2 top-2 rounded-full bg-white/90 px-2 py-0.5 text-xs font-semibold text-basil-dark shadow-card">
            {r.have}/{r.total} z výběru
          </div>
        ) : grouped ? (
          <div className="absolute right-2 top-2 rounded-full bg-white/90 px-2 py-0.5 text-xs font-semibold text-basil-dark shadow-card">
            {r.variants} variant
          </div>
        ) : null}
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
        {r.tags?.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {r.tags.slice(0, 3).map((t) => (
              <span key={`${t.namespace}:${t.slug}`} className="rounded-full bg-basil-soft px-2 py-0.5 text-[11px] text-basil-dark">
                {t.label_cs}
              </span>
            ))}
          </div>
        )}
        {pantryOn && (
          <div className="mt-auto">
            <CookMeter have={r.have} total={r.total} size="sm" />
          </div>
        )}
      </div>
    </Link>
  );
}
