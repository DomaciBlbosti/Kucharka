import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Button, Spinner } from "../components/ui";

const MEALS = ["snídaně", "svačina", "oběd", "večeře"];
const DOW = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"];

function iso(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}
function mondayOf(d) {
  const x = new Date(d);
  const wd = (x.getDay() + 6) % 7; // Po=0
  x.setDate(x.getDate() - wd);
  x.setHours(0, 0, 0, 0);
  return x;
}

export default function MealPlan() {
  const [anchor, setAnchor] = useState(() => mondayOf(new Date()));
  const [entries, setEntries] = useState(null);
  const [addFor, setAddFor] = useState(null); // {date}
  const [shopMsg, setShopMsg] = useState(null);

  const start = iso(anchor);
  const days = useMemo(
    () =>
      Array.from({ length: 7 }, (_, i) => {
        const d = new Date(anchor);
        d.setDate(d.getDate() + i);
        return d;
      }),
    [anchor]
  );
  const todayIso = iso(new Date());

  const load = () =>
    api.mealplan(start, 7).then(setEntries).catch(() => setEntries([]));
  useEffect(() => {
    setEntries(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [start]);

  const byDay = useMemo(() => {
    const map = {};
    (entries || []).forEach((e) => {
      (map[e.date] ??= []).push(e);
    });
    return map;
  }, [entries]);

  const dayKcal = (list) =>
    Math.round(list.reduce((s, e) => s + (e.kcal || 0), 0));

  const shift = (n) => {
    const d = new Date(anchor);
    d.setDate(d.getDate() + n * 7);
    setAnchor(d);
    setShopMsg(null);
  };

  const remove = async (id) => {
    await api.removeMeal(id);
    setEntries((cur) => cur.filter((e) => e.id !== id));
  };
  const setServings = async (e, val) => {
    const s = Math.max(1, Number(val) || 1);
    const upd = await api.updateMeal(e.id, { servings: s });
    setEntries((cur) => cur.map((x) => (x.id === e.id ? upd : x)));
  };

  const doShopping = async () => {
    const r = await api.mealplanShopping(start, 7);
    setShopMsg(
      r.added > 0
        ? `Přidáno ${r.added} surovin do nákupu (z ${r.recipes} jídel).`
        : "Vše už máš doma nebo v nákupu."
    );
  };

  const label = `${days[0].getDate()}. ${days[0].getMonth() + 1}. – ${days[6].getDate()}. ${days[6].getMonth() + 1}.`;

  return (
    <div>
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl font-extrabold">Jídelníček</h1>
        <div className="flex items-center gap-2">
          <button onClick={() => shift(-1)} className="rounded-full border border-line bg-white px-3 py-1.5 text-sm hover:border-basil">←</button>
          <span className="nums min-w-[8.5rem] text-center text-sm font-semibold">{label}</span>
          <button onClick={() => shift(1)} className="rounded-full border border-line bg-white px-3 py-1.5 text-sm hover:border-basil">→</button>
          <button onClick={() => setAnchor(mondayOf(new Date()))} className="rounded-full border border-line bg-white px-3 py-1.5 text-sm text-ink/60 hover:border-basil">dnes</button>
        </div>
      </div>

      <div className="mb-5 flex flex-wrap items-center gap-3">
        <Button onClick={doShopping}>🛒 Nákup z plánu</Button>
        {shopMsg && <span className="text-sm text-basil-dark">{shopMsg}</span>}
      </div>

      {entries === null ? (
        <Spinner />
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {days.map((d, i) => {
            const di = iso(d);
            const list = (byDay[di] || []).slice().sort(
              (a, b) => MEALS.indexOf(a.meal) - MEALS.indexOf(b.meal)
            );
            const isToday = di === todayIso;
            return (
              <section
                key={di}
                className={`rounded-xl2 border bg-white p-4 shadow-card ${
                  isToday ? "border-basil" : "border-line"
                }`}
              >
                <div className="mb-3 flex items-center justify-between">
                  <h2 className="font-bold">
                    {DOW[i]} {d.getDate()}. {d.getMonth() + 1}.
                    {isToday && <span className="ml-2 text-xs font-medium text-basil">dnes</span>}
                  </h2>
                  {list.length > 0 && (
                    <span className="nums rounded-full bg-basil-soft px-2.5 py-0.5 text-xs font-semibold text-basil-dark">
                      🔥 {dayKcal(list)} kcal
                    </span>
                  )}
                </div>

                {list.length === 0 ? (
                  <p className="mb-3 text-sm text-ink/40">Zatím nic naplánováno.</p>
                ) : (
                  <ul className="mb-3 space-y-2">
                    {list.map((e) => (
                      <li key={e.id} className="flex items-center gap-2 rounded-lg border border-line/70 px-3 py-2 text-sm">
                        <span className="w-16 shrink-0 text-xs font-medium text-ink/45">{e.meal}</span>
                        <Link to={`/recept/${e.recipe_id}`} className="flex-1 truncate hover:text-basil-dark">
                          {e.title}
                        </Link>
                        <input
                          type="number"
                          min="1"
                          value={e.servings}
                          onChange={(ev) => setServings(e, ev.target.value)}
                          title="porce"
                          className="nums w-12 rounded border border-line bg-paper px-1 py-0.5 text-center text-xs outline-none focus:border-basil"
                        />
                        {e.kcal != null && (
                          <span className="nums w-16 shrink-0 text-right text-xs text-ink/40">
                            {Math.round(e.kcal)} kcal
                          </span>
                        )}
                        <button onClick={() => remove(e.id)} title="Odebrat" className="text-ink/30 hover:text-miss">×</button>
                      </li>
                    ))}
                  </ul>
                )}

                <button
                  onClick={() => setAddFor({ date: di })}
                  className="text-sm font-medium text-basil-dark hover:underline"
                >
                  + přidat jídlo
                </button>
              </section>
            );
          })}
        </div>
      )}

      {addFor && (
        <AddMealModal
          date={addFor.date}
          onClose={() => setAddFor(null)}
          onAdded={(e) => {
            setEntries((cur) => [...(cur || []), e]);
            setAddFor(null);
          }}
        />
      )}
    </div>
  );
}

function AddMealModal({ date, onClose, onAdded }) {
  const [meal, setMeal] = useState("oběd");
  const [servings, setServings] = useState(2);
  const [q, setQ] = useState("");
  const [results, setResults] = useState([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!q.trim()) {
      setResults([]);
      return;
    }
    let live = true;
    const t = setTimeout(() => {
      api.recipes({ q, sort: "rating" }).then((r) => live && setResults(r.slice(0, 8)));
    }, 200);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [q]);

  const pick = async (r) => {
    setBusy(true);
    try {
      const e = await api.addMeal({ date, meal, recipe_id: r.id, servings: Number(servings) || 1 });
      onAdded(e);
    } finally {
      setBusy(false);
    }
  };

  const niceDate = (() => {
    const [y, m, d] = date.split("-");
    return `${Number(d)}. ${Number(m)}.`;
  })();

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-ink/40 p-0 sm:items-center sm:p-4" onClick={onClose}>
      <div
        className="max-h-[85vh] w-full max-w-md overflow-auto rounded-t-2xl bg-white p-5 shadow-xl sm:rounded-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-bold">Přidat jídlo · {niceDate}</h2>
          <button onClick={onClose} className="text-2xl leading-none text-ink/40 hover:text-ink">×</button>
        </div>

        <div className="mb-3 flex gap-2">
          <select value={meal} onChange={(e) => setMeal(e.target.value)} className="flex-1 rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil">
            {MEALS.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
          <label className="flex items-center gap-1.5 rounded-lg border border-line bg-paper px-3 py-2 text-sm text-ink/60">
            porce
            <input type="number" min="1" value={servings} onChange={(e) => setServings(e.target.value)} className="nums w-12 bg-transparent text-center outline-none" />
          </label>
        </div>

        <input
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Hledat recept…"
          className="mb-2 w-full rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil"
        />

        {q.trim() && results.length === 0 ? (
          <p className="px-1 py-2 text-sm text-ink/40">Nic nenalezeno.</p>
        ) : (
          <ul className="space-y-1">
            {results.map((r) => (
              <li key={r.id}>
                <button
                  disabled={busy}
                  onClick={() => pick(r)}
                  className="flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left text-sm hover:bg-basil-soft disabled:opacity-50"
                >
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-md bg-basil-soft">
                    {r.image_url ? <img src={r.image_url} alt="" className="h-full w-full object-cover" /> : "🍽️"}
                  </span>
                  <span className="flex-1 truncate">{r.title}</span>
                  {r.kcal_per_serving != null && (
                    <span className="nums shrink-0 text-xs text-ink/40">{Math.round(r.kcal_per_serving)} kcal</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
