import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { IngredientPicker } from "../components/IngredientPicker";
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
  const wd = (x.getDay() + 6) % 7;
  x.setDate(x.getDate() - wd);
  x.setHours(0, 0, 0, 0);
  return x;
}
function niceDate(isoStr) {
  const [, m, d] = isoStr.split("-");
  return `${Number(d)}. ${Number(m)}.`;
}

export default function MealPlan() {
  const [anchor, setAnchor] = useState(() => mondayOf(new Date()));
  const [entries, setEntries] = useState(null);
  const [addFor, setAddFor] = useState(null);
  const [shopMsg, setShopMsg] = useState(null);
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [review, setReview] = useState(null); // { start, days, recipes, byDay }

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

  const load = () => api.mealplan(start, 7).then(setEntries).catch(() => setEntries([]));
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

  const dayKcal = (list) => Math.round(list.reduce((s, e) => s + (e.kcal || 0), 0));

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

  // ---- Review režim (po AI návrhu) ----
  if (review) {
    return (
      <ReviewPanel
        review={review}
        onCancel={() => setReview(null)}
        onApplied={() => {
          setReview(null);
          // přeskoč na týden návrhu
          setAnchor(mondayOf(new Date(review.start + "T00:00:00")));
          setEntries(null);
          api.mealplan(iso(mondayOf(new Date(review.start + "T00:00:00"))), 7)
            .then(setEntries)
            .catch(() => setEntries([]));
        }}
      />
    );
  }

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
        <Button onClick={() => setSuggestOpen(true)}>✨ Navrhnout plán (AI)</Button>
        <Button variant="ghost" onClick={doShopping}>🛒 Nákup z plánu</Button>
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
              <section key={di} className={`rounded-xl2 border bg-white p-4 shadow-card ${isToday ? "border-basil" : "border-line"}`}>
                <div className="mb-3 flex items-center justify-between">
                  <h2 className="font-bold">
                    {DOW[i]} {d.getDate()}. {d.getMonth() + 1}.
                    {isToday && <span className="ml-2 text-xs font-medium text-basil">dnes</span>}
                  </h2>
                  {list.length > 0 && (
                    <span className="nums rounded-full bg-basil-soft px-2.5 py-0.5 text-xs font-semibold text-basil-dark">🔥 {dayKcal(list)} kcal</span>
                  )}
                </div>
                {list.length === 0 ? (
                  <p className="mb-3 text-sm text-ink/40">Zatím nic naplánováno.</p>
                ) : (
                  <ul className="mb-3 space-y-2">
                    {list.map((e) => (
                      <li key={e.id} className="flex items-center gap-2 rounded-lg border border-line/70 px-3 py-2 text-sm">
                        <span className="w-16 shrink-0 text-xs font-medium text-ink/45">{e.meal}</span>
                        <Link to={`/recept/${e.recipe_id}`} className="flex-1 truncate hover:text-basil-dark">{e.title}</Link>
                        <input type="number" min="1" value={e.servings} onChange={(ev) => setServings(e, ev.target.value)} title="porce" className="nums w-12 rounded border border-line bg-paper px-1 py-0.5 text-center text-xs outline-none focus:border-basil" />
                        {e.kcal != null && <span className="nums w-16 shrink-0 text-right text-xs text-ink/40">{Math.round(e.kcal)} kcal</span>}
                        <button onClick={() => remove(e.id)} title="Odebrat" className="text-ink/30 hover:text-miss">×</button>
                      </li>
                    ))}
                  </ul>
                )}
                <button onClick={() => setAddFor({ date: di })} className="text-sm font-medium text-basil-dark hover:underline">+ přidat jídlo</button>
              </section>
            );
          })}
        </div>
      )}

      {addFor && (
        <AddMealModal date={addFor.date} onClose={() => setAddFor(null)} onAdded={(e) => { setEntries((cur) => [...(cur || []), e]); setAddFor(null); }} />
      )}
      {suggestOpen && (
        <SuggestModal defaultStart={start} onClose={() => setSuggestOpen(false)} onReady={(rev) => { setSuggestOpen(false); setReview(rev); }} />
      )}
    </div>
  );
}

// ---------- AI návrh: formulář + průběh ----------
function SuggestModal({ defaultStart, onClose, onReady }) {
  const [startDate, setStartDate] = useState(defaultStart);
  const [days, setDays] = useState(7);
  const [meals, setMeals] = useState([...MEALS]);
  const [kcal, setKcal] = useState("");
  const [prefs, setPrefs] = useState("");
  const [fillEmpty, setFillEmpty] = useState(false);
  const [phase, setPhase] = useState("form"); // form | running | error
  const [progress, setProgress] = useState({ day: 0, days: 0 });
  const [err, setErr] = useState(null);

  const toggleMeal = (m) =>
    setMeals((cur) => (cur.includes(m) ? cur.filter((x) => x !== m) : [...cur, m]));

  const run = async () => {
    setErr(null);
    const r = await api.suggestPlan({
      start: startDate,
      days: Number(days) || 7,
      meals: meals.length ? meals : ["oběd"],
      daily_kcal: kcal ? Number(kcal) : null,
      preferences: prefs,
      fill_empty: fillEmpty,
    });
    if (!r.started) {
      setErr(r.error || "Plánovač už běží nebo není dostupná Ollama.");
      setPhase("error");
      return;
    }
    setPhase("running");
    const poll = setInterval(async () => {
      const s = await api.suggestStatus();
      setProgress({ day: s.day, days: s.days });
      if (s.error) { clearInterval(poll); setErr(s.error); setPhase("error"); return; }
      if (!s.running && s.proposal) {
        clearInterval(poll);
        const rec = s.proposal;
        // postav editovatelný review
        const recipes = { ...rec.recipes };
        const byDay = rec.days.map((d) => ({
          date: d.date,
          meals: Object.fromEntries(
            (rec.meals).map((m) => {
              const slot = d.meals[m];
              return [m, slot ? { recipe_id: slot.recipe_id, alternatives: slot.alternatives || [], servings: 2 } : null];
            })
          ),
        }));
        onReady({ start: rec.start, days: rec.days.length, mealsOrder: rec.meals, recipes, byDay });
      }
    }, 1200);
  };

  return (
    <Modal onClose={onClose} title="✨ Navrhnout jídelníček">
      {phase === "form" && (
        <>
          <div className="mb-3 grid grid-cols-2 gap-3">
            <Field label="Začátek">
              <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} className={inp} />
            </Field>
            <Field label="Počet dní">
              <input type="number" min="1" max="31" value={days} onChange={(e) => setDays(e.target.value)} className={inp} />
            </Field>
          </div>
          <Field label="Chody">
            <div className="flex flex-wrap gap-2">
              {MEALS.map((m) => (
                <button key={m} onClick={() => toggleMeal(m)} className={`rounded-full px-3 py-1.5 text-sm ${meals.includes(m) ? "bg-basil text-white" : "border border-line bg-white text-ink/60"}`}>{m}</button>
              ))}
            </div>
          </Field>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <Field label="Cílové kcal/den (nepovinné)">
              <input type="number" min="0" value={kcal} onChange={(e) => setKcal(e.target.value)} placeholder="např. 2000" className={inp} />
            </Field>
            <Field label="Preference / omezení">
              <input value={prefs} onChange={(e) => setPrefs(e.target.value)} placeholder="víc zeleniny, bez ryb…" className={inp} />
            </Field>
          </div>
          <label className="mt-3 flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-0.5 accent-basil" checked={fillEmpty}
              onChange={(e) => setFillEmpty(e.target.checked)} />
            <span>
              Když z tvých receptů nic nesedí, nech AI vymyslet a rovnou uložit nový recept.
              <span className="block text-xs text-ink/40">Prodlouží to sestavování plánu.</span>
            </span>
          </label>
          <p className="mt-3 text-xs text-ink/40">
            Plán sestavím z tvých receptů (reálné kcal). Není to lékařské ani dietní doporučení.
          </p>
          <div className="mt-4 flex justify-end gap-2">
            <Button variant="ghost" onClick={onClose}>Zrušit</Button>
            <Button onClick={run}>Navrhnout</Button>
          </div>
        </>
      )}
      {phase === "running" && (
        <div className="py-6">
          <Spinner label={`Sestavuji jídelníček… den ${progress.day}/${progress.days || days}`} />
          <p className="mt-3 text-center text-xs text-ink/40">AI vybírá vhodná jídla den po dni, chvíli to potrvá.</p>
        </div>
      )}
      {phase === "error" && (
        <div className="py-6 text-center">
          <p className="text-sm text-miss">{err}</p>
          <Button variant="ghost" onClick={() => setPhase("form")} className="mt-4">Zpět</Button>
        </div>
      )}
    </Modal>
  );
}

// ---------- Review navrženého plánu ----------
function ReviewPanel({ review, onCancel, onApplied }) {
  const [recipes, setRecipes] = useState(review.recipes);
  const [byDay, setByDay] = useState(review.byDay);
  const [changeFor, setChangeFor] = useState(null); // {di, meal}
  const [busy, setBusy] = useState(false);

  const order = review.mealsOrder;
  const recOf = (id) => recipes[id] || recipes[String(id)] || { title: `#${id}`, kcal_per_serving: null };

  const setSlot = (di, meal, slot) =>
    setByDay((cur) => cur.map((d) => (d.date === di ? { ...d, meals: { ...d.meals, [meal]: slot } } : d)));

  const pickAlt = (di, meal, altId, cur) => {
    // prohoď hlavní pick za alternativu (a původní dej do alternativ)
    const others = [cur.recipe_id, ...cur.alternatives.filter((a) => a !== altId)].slice(0, 2);
    setSlot(di, meal, { ...cur, recipe_id: altId, alternatives: others });
  };

  const onChangePicked = (rec) => {
    const { di, meal } = changeFor;
    setRecipes((r) => ({ ...r, [rec.id]: { title: rec.title, kcal_per_serving: rec.kcal_per_serving } }));
    const cur = byDay.find((d) => d.date === di)?.meals[meal];
    setSlot(di, meal, { recipe_id: rec.id, alternatives: cur?.alternatives || [], servings: cur?.servings || 2 });
    setChangeFor(null);
  };

  const dayKcal = (day) =>
    Math.round(
      order.reduce((s, m) => {
        const slot = day.meals[m];
        if (!slot) return s;
        const k = recOf(slot.recipe_id).kcal_per_serving;
        return s + (k ? k * slot.servings : 0);
      }, 0)
    );

  const apply = async () => {
    setBusy(true);
    try {
      const entries = [];
      byDay.forEach((d) =>
        order.forEach((m) => {
          const slot = d.meals[m];
          if (slot) entries.push({ date: d.date, meal: m, recipe_id: slot.recipe_id, servings: slot.servings });
        })
      );
      await api.applyPlan({ start: review.start, days: review.days, entries, replace_range: true });
      onApplied();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-display text-2xl font-extrabold">Návrh jídelníčku</h1>
          <p className="text-sm text-ink/55">Uprav, co chceš, pak vlož do plánu. Vložením se přepíše {review.days} dní od {niceDate(review.start)}.</p>
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={onCancel}>Zahodit</Button>
          <Button onClick={apply} disabled={busy}>{busy ? "Vkládám…" : "Vložit do plánu"}</Button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {byDay.map((d, i) => (
          <section key={d.date} className="rounded-xl2 border border-line bg-white p-4 shadow-card">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="font-bold">{niceDate(d.date)}</h2>
              <span className="nums rounded-full bg-basil-soft px-2.5 py-0.5 text-xs font-semibold text-basil-dark">🔥 {dayKcal(d)} kcal</span>
            </div>
            <ul className="space-y-2">
              {order.map((meal) => {
                const slot = d.meals[meal];
                return (
                  <li key={meal} className="rounded-lg border border-line/70 px-3 py-2 text-sm">
                    <div className="flex items-center gap-2">
                      <span className="w-16 shrink-0 text-xs font-medium text-ink/45">{meal}</span>
                      {slot ? (
                        <>
                          <span className="flex-1 truncate">
                            {recOf(slot.recipe_id).title}
                            {recOf(slot.recipe_id).generated && (
                              <span className="ml-1.5 rounded-full bg-basil-soft px-1.5 py-0.5 text-[10px] font-semibold text-basil-dark align-middle">✨ nový</span>
                            )}
                          </span>
                          <input type="number" min="1" value={slot.servings} onChange={(e) => setSlot(d.date, meal, { ...slot, servings: Math.max(1, Number(e.target.value) || 1) })} className="nums w-12 rounded border border-line bg-paper px-1 py-0.5 text-center text-xs outline-none focus:border-basil" />
                          <button onClick={() => setSlot(d.date, meal, null)} title="Vynechat" className="text-ink/30 hover:text-miss">×</button>
                        </>
                      ) : (
                        <span className="flex-1 text-ink/35">— vynecháno —</span>
                      )}
                    </div>
                    <div className="mt-1.5 flex flex-wrap items-center gap-1.5 pl-16">
                      {slot?.alternatives?.map((altId) => (
                        <button key={altId} onClick={() => pickAlt(d.date, meal, altId, slot)} className="rounded-full bg-basil-soft px-2 py-0.5 text-xs text-basil-dark hover:bg-basil/20">↺ {recOf(altId).title}</button>
                      ))}
                      <button onClick={() => setChangeFor({ di: d.date, meal })} className="rounded-full border border-line px-2 py-0.5 text-xs text-ink/50 hover:border-basil">změnit / ze suroviny</button>
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </div>

      <div className="mt-5 flex justify-end gap-2">
        <Button variant="ghost" onClick={onCancel}>Zahodit</Button>
        <Button onClick={apply} disabled={busy}>{busy ? "Vkládám…" : "Vložit do plánu"}</Button>
      </div>

      {changeFor && (
        <Modal onClose={() => setChangeFor(null)} title={`Změnit · ${changeFor.meal}`}>
          <RecipeSearchPicker onPick={onChangePicked} />
        </Modal>
      )}
    </div>
  );
}

// ---------- Přidání jednoho jídla do kalendáře ----------
function AddMealModal({ date, onClose, onAdded }) {
  const [meal, setMeal] = useState("oběd");
  const [servings, setServings] = useState(2);
  const [busy, setBusy] = useState(false);

  const pick = async (r) => {
    setBusy(true);
    try {
      const e = await api.addMeal({ date, meal, recipe_id: r.id, servings: Number(servings) || 1 });
      onAdded(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal onClose={onClose} title={`Přidat jídlo · ${niceDate(date)}`}>
      <div className="mb-3 flex gap-2">
        <select value={meal} onChange={(e) => setMeal(e.target.value)} className="flex-1 rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil">
          {MEALS.map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
        <label className="flex items-center gap-1.5 rounded-lg border border-line bg-paper px-3 py-2 text-sm text-ink/60">
          porce
          <input type="number" min="1" value={servings} onChange={(e) => setServings(e.target.value)} className="nums w-12 bg-transparent text-center outline-none" />
        </label>
      </div>
      <RecipeSearchPicker onPick={pick} disabled={busy} />
    </Modal>
  );
}

// ---------- Výběr receptu: hledání nebo "ze suroviny" ----------
function RecipeSearchPicker({ onPick, disabled }) {
  const [tab, setTab] = useState("search");
  const [q, setQ] = useState("");
  const [results, setResults] = useState([]);
  const [picked, setPicked] = useState([]);

  useEffect(() => {
    if (tab !== "search" || !q.trim()) { setResults([]); return; }
    let live = true;
    const t = setTimeout(() => {
      api.recipes({ q, sort: "rating", limit: 10 }).then((r) => live && setResults(r.items));
    }, 200);
    return () => { live = false; clearTimeout(t); };
  }, [q, tab]);

  useEffect(() => {
    if (tab !== "cookfrom" || picked.length === 0) { setResults([]); return; }
    let live = true;
    api.cookFrom(picked.map((p) => p.id)).then((r) => live && setResults(r.slice(0, 12)));
    return () => { live = false; };
  }, [picked, tab]);

  return (
    <div>
      <div className="mb-2 flex gap-1 rounded-lg bg-paper p-1 text-sm">
        <button onClick={() => setTab("search")} className={`flex-1 rounded-md py-1.5 ${tab === "search" ? "bg-white shadow-card font-medium" : "text-ink/50"}`}>Hledat recept</button>
        <button onClick={() => setTab("cookfrom")} className={`flex-1 rounded-md py-1.5 ${tab === "cookfrom" ? "bg-white shadow-card font-medium" : "text-ink/50"}`}>Ze suroviny</button>
      </div>

      {tab === "search" ? (
        <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Hledat recept…" className="mb-2 w-full rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil" />
      ) : (
        <div className="mb-2">
          <IngredientPicker onPick={(o) => setPicked((cur) => (cur.some((p) => p.id === o.id) ? cur : [...cur, o]))} placeholder="Přidat surovinu…" />
          {picked.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {picked.map((p) => (
                <button key={p.id} onClick={() => setPicked((cur) => cur.filter((x) => x.id !== p.id))} className="inline-flex items-center gap-1 rounded-full bg-basil-soft px-2.5 py-0.5 text-xs text-basil-dark">{p.name_cs} ×</button>
              ))}
            </div>
          )}
        </div>
      )}

      <ul className="max-h-72 space-y-1 overflow-auto">
        {results.map((r) => (
          <li key={r.id}>
            <button disabled={disabled} onClick={() => onPick(r)} className="flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left text-sm hover:bg-basil-soft disabled:opacity-50">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-md bg-basil-soft">
                {r.image_url ? <img src={r.image_url} alt="" className="h-full w-full object-cover" /> : "🍽️"}
              </span>
              <span className="flex-1 truncate">{r.title}</span>
              {tab === "cookfrom" && r.total != null && (
                <span className="nums shrink-0 text-xs text-basil-dark">{r.have}/{r.total}</span>
              )}
              {r.kcal_per_serving != null && <span className="nums shrink-0 text-xs text-ink/40">{Math.round(r.kcal_per_serving)} kcal</span>}
            </button>
          </li>
        ))}
        {((tab === "search" && q.trim()) || (tab === "cookfrom" && picked.length > 0)) && results.length === 0 && (
          <li className="px-1 py-2 text-sm text-ink/40">Nic nenalezeno.</li>
        )}
      </ul>
    </div>
  );
}

// ---------- malé UI helpery ----------
const inp = "w-full rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil";
function Field({ label, children }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-ink/55">{label}</span>
      {children}
    </label>
  );
}
function Modal({ title, onClose, children }) {
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-ink/40 p-0 sm:items-center sm:p-4" onClick={onClose}>
      <div className="max-h-[88vh] w-full max-w-md overflow-auto rounded-t-2xl bg-white p-5 shadow-xl sm:rounded-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-bold">{title}</h2>
          <button onClick={onClose} className="text-2xl leading-none text-ink/40 hover:text-ink">×</button>
        </div>
        {children}
      </div>
    </div>
  );
}
