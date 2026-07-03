import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { Button, CookMeter, Meta, Spinner, Stars } from "../components/ui";

export default function RecipeDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const [r, setR] = useState(null);
  const [added, setAdded] = useState(null);
  const [addedIds, setAddedIds] = useState(() => new Set());
  const [editing, setEditing] = useState(false);
  const [cookOpen, setCookOpen] = useState(false);
  const [cookedMsg, setCookedMsg] = useState(null);
  const [retranslating, setRetranslating] = useState(false);
  const [retranslateMsg, setRetranslateMsg] = useState(null);
  const [showOriginal, setShowOriginal] = useState(false);
  const [onDisplay, setOnDisplay] = useState(false);

  const reload = () => api.recipe(id).then(setR).catch(() => setR(false));
  useEffect(() => {
    setR(null);
    setEditing(false);
    setShowOriginal(false);
    reload();
    api.hmiCooking()
      .then((r2) => setOnDisplay(r2.recipe?.id === Number(id)))
      .catch(() => setOnDisplay(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const toggleDisplay = async () => {
    const next = onDisplay ? null : Number(id);
    await api.setHmiCooking(next);
    setOnDisplay(!onDisplay);
  };

  if (r === null) return <Spinner />;
  if (r === false) return <p className="py-16 text-center text-ink/50">Recept nenalezen.</p>;

  const hasOriginal = !!r.original_title;
  const displayTitle = showOriginal && hasOriginal ? r.original_title : r.title;
  const displayInstructions = showOriginal && hasOriginal ? r.original_instructions : r.instructions;
  const missing = new Set(r.missing_ingredient_ids);
  const steps = (displayInstructions || "").split(/\n+/).map((s) => s.trim()).filter(Boolean);

  const addMissing = async () => setAdded((await api.shoppingFromRecipe(r.id)).added);
  const addOne = async (ri) => {
    await api.addShopping({ label: ri.raw_text, ingredient_id: ri.ingredient_id || null });
    setAddedIds((cur) => new Set(cur).add(ri.id));
  };
  const cooked = async () => {
    const res = await api.markCooked(r.id);
    setCookedMsg(res.removed > 0 ? `Odečteno ${res.removed} surovin ze spíže.` : "Ve spíži nebylo nic k odečtení.");
    reload();
  };
  const setRating = async (val) => {
    const upd = await api.editRecipe(r.id, { user_rating: val });
    setR(upd);
  };
  const retranslate = async () => {
    setRetranslating(true);
    setRetranslateMsg(null);
    try {
      const upd = await api.retranslateOne(r.id);
      setR(upd);
      setRetranslateMsg("Přeloženo znovu ✓");
    } catch (e) {
      setRetranslateMsg(e?.message || "Překlad se nepodařilo obnovit.");
    } finally {
      setRetranslating(false);
    }
  };

  if (editing) return <EditRecipe recipe={r} onDone={(upd) => { if (upd) setR(upd); setEditing(false); }} />;

  return (
    <div>
      <Link to="/" className="mb-4 inline-flex text-sm text-ink/50 hover:text-ink">← Zpět na recepty</Link>

      <div className="overflow-hidden rounded-xl2 border border-line bg-white shadow-card">
        {r.image_url && (
          <div className="aspect-[21/9] w-full overflow-hidden bg-basil-soft">
            <img src={r.image_url} alt="" className="h-full w-full object-cover" />
          </div>
        )}
        <div className="p-5 sm:p-7">
          <h1 className="text-3xl font-extrabold leading-tight">{displayTitle}</h1>
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1">
            <Stars rating={r.rating} count={r.rating_count} />
            <Meta icon="⏱">{r.total_time ? `${r.total_time} min` : null}</Meta>
            <Meta icon="🍽">{r.servings ? `${r.servings} porce` : null}</Meta>
            <Meta icon="🔥">{r.kcal_per_serving ? `${Math.round(r.kcal_per_serving)} kcal/porce` : null}</Meta>
            {r.source_domain && (
              <a href={r.source_url} target="_blank" rel="noreferrer" className="text-sm text-basil underline-offset-2 hover:underline">
                {r.source_domain} ↗
              </a>
            )}
            {hasOriginal && (
              <button
                onClick={() => setShowOriginal((v) => !v)}
                className="rounded-full border border-line bg-white px-2.5 py-0.5 text-xs font-medium text-ink/60 hover:border-basil hover:text-basil-dark"
              >
                {showOriginal ? "🇨🇿 Zobrazit překlad" : "🌐 Zobrazit originál"}
              </button>
            )}
          </div>

          {r.tags?.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {r.tags.map((t) => (
                <span key={`${t.namespace}:${t.slug}`} className="rounded-full bg-basil-soft px-2.5 py-0.5 text-xs text-basil-dark">
                  {t.label_cs}
                </span>
              ))}
            </div>
          )}

          {/* akční lišta */}
          <div className="mt-4 flex flex-wrap items-center gap-2">
            {steps.length > 0 && <Button onClick={() => setCookOpen(true)}>🍳 Uvařit</Button>}
            <Button variant="ghost" onClick={cooked}>✅ Uvařeno</Button>
            <Button variant="ghost" onClick={() => setEditing(true)}>✏️ Upravit</Button>
            <Button variant={onDisplay ? "primary" : "ghost"} onClick={toggleDisplay}>
              {onDisplay ? "📺 Na displeji ✓" : "📺 Odeslat na displej"}
            </Button>
            {r.source_url?.startsWith("http") && (
              <Button variant="ghost" onClick={retranslate} disabled={retranslating}>
                {retranslating ? "Překládám…" : "🔁 Přeložit znovu"}
              </Button>
            )}
          </div>
          {cookedMsg && <p className="mt-2 text-sm text-basil-dark">{cookedMsg}</p>}
          {retranslateMsg && <p className="mt-2 text-sm text-basil-dark">{retranslateMsg}</p>}

          {/* vlastní hodnocení + poznámka */}
          <div className="mt-4 rounded-xl2 border border-line bg-paper p-4">
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-sm font-medium text-ink/70">Moje hodnocení:</span>
              <EditableStars value={r.user_rating || 0} onChange={setRating} />
            </div>
            {r.user_note ? (
              <p className="mt-2 whitespace-pre-wrap text-sm text-ink/75">{r.user_note}</p>
            ) : (
              <button onClick={() => setEditing(true)} className="mt-2 text-sm text-ink/45 hover:text-basil-dark">
                + přidat poznámku
              </button>
            )}
          </div>

          <div className="mt-5 max-w-sm"><CookMeter have={r.have} total={r.total} /></div>

          <div className="mt-8 grid gap-8 md:grid-cols-[1fr_1.3fr]">
            <section>
              <div className="mb-3 flex items-center justify-between">
                <h2 className="text-xl font-bold">Suroviny</h2>
                {r.missing_count > 0 && (
                  <Button variant="ghost" onClick={addMissing}>+ Chybějící do nákupu</Button>
                )}
              </div>
              {added != null && (
                <p className="mb-3 rounded-lg bg-basil-soft px-3 py-2 text-sm text-basil-dark">
                  Přidáno {added} položek do nákupního seznamu.
                </p>
              )}
              <ul className="space-y-1.5">
                {r.ingredients.map((ri) => {
                  const isMissing = ri.ingredient_id && missing.has(ri.ingredient_id);
                  const isHave = ri.ingredient_id && !missing.has(ri.ingredient_id);
                  return (
                    <li key={ri.id} className="flex items-center justify-between gap-3 rounded-lg border border-line/70 px-3 py-2 text-sm">
                      <span className="flex items-center gap-2">
                        <span className={`h-2 w-2 shrink-0 rounded-full ${isHave ? "bg-have" : isMissing ? "bg-miss" : "bg-ink/15"}`} />
                        {showOriginal && ri.original_raw_text ? ri.original_raw_text : ri.raw_text}
                      </span>
                      <span className="flex shrink-0 items-center gap-2">
                        {ri.kcal != null && <span className="nums text-xs text-ink/40">{Math.round(ri.kcal)} kcal</span>}
                        {addedIds.has(ri.id) ? (
                          <span title="Přidáno do nákupu" className="flex h-7 w-7 items-center justify-center rounded-full bg-basil-soft text-basil-dark">✓</span>
                        ) : (
                          <button onClick={() => addOne(ri)} title="Přidat do nákupu" className="flex h-7 w-7 items-center justify-center rounded-full border border-line text-ink/50 hover:border-basil hover:text-basil-dark">+</button>
                        )}
                      </span>
                    </li>
                  );
                })}
              </ul>
              <p className="mt-3 text-xs text-ink/40">
                <span className="text-have">●</span> mám &nbsp;
                <span className="text-miss">●</span> chybí &nbsp;
                <span className="text-ink/30">●</span> nenapárováno
              </p>
            </section>

            <section>
              <h2 className="mb-3 text-xl font-bold">Postup</h2>
              {steps.length ? (
                <ol className="space-y-3">
                  {steps.map((s, i) => (
                    <li key={i} className="flex gap-3 text-sm leading-relaxed">
                      <span className="nums mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-basil-soft text-xs font-bold text-basil-dark">{i + 1}</span>
                      <span>{s}</span>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="text-sm text-ink/50">Postup nebyl k dispozici.</p>
              )}
            </section>
          </div>

          <div className="mt-8 border-t border-line pt-4">
            <Button variant="danger" onClick={async () => { await api.deleteRecipe(r.id); nav("/"); }}>
              Smazat recept
            </Button>
          </div>
        </div>
      </div>

      {cookOpen && <CookingMode recipe={r} steps={steps} onClose={() => setCookOpen(false)} />}
    </div>
  );
}

function EditableStars({ value, onChange }) {
  const [hover, setHover] = useState(0);
  const shown = hover || value;
  return (
    <span className="flex items-center gap-0.5">
      {[1, 2, 3, 4, 5].map((n) => (
        <button
          key={n}
          onMouseEnter={() => setHover(n)}
          onMouseLeave={() => setHover(0)}
          onClick={() => onChange(n === value ? 0 : n)}
          className={`text-xl leading-none ${n <= shown ? "text-amber-500" : "text-ink/20"}`}
          title={`${n} z 5`}
        >
          ★
        </button>
      ))}
      {value > 0 && <button onClick={() => onChange(0)} className="ml-2 text-xs text-ink/40 hover:text-miss">zrušit</button>}
    </span>
  );
}

function EditRecipe({ recipe, onDone }) {
  const [title, setTitle] = useState(recipe.title);
  const [servings, setServings] = useState(recipe.servings || "");
  const [instructions, setInstructions] = useState(recipe.instructions || "");
  const [note, setNote] = useState(recipe.user_note || "");
  const [lines, setLines] = useState(recipe.ingredients.map((i) => i.raw_text));
  const [tagGroups, setTagGroups] = useState([]);
  const [selectedTags, setSelectedTags] = useState(
    (recipe.tags || []).map((t) => `${t.namespace}:${t.slug}`)
  );
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.recipeTags().then(setTagGroups).catch(() => setTagGroups([]));
  }, []);

  const toggleTag = (key) =>
    setSelectedTags((cur) => (cur.includes(key) ? cur.filter((t) => t !== key) : [...cur, key]));

  const save = async () => {
    setBusy(true);
    try {
      await api.editRecipe(recipe.id, {
        title,
        servings: servings ? Number(servings) : null,
        instructions,
        user_note: note,
        ingredient_texts: lines,
      });
      const upd = await api.setRecipeTags(recipe.id, selectedTags);
      onDone(upd);
    } finally {
      setBusy(false);
    }
  };

  const inp = "w-full rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil";
  return (
    <div>
      <button onClick={() => onDone(null)} className="mb-4 inline-flex text-sm text-ink/50 hover:text-ink">← Zrušit úpravy</button>
      <div className="space-y-4 rounded-xl2 border border-line bg-white p-5 shadow-card sm:p-7">
        <h1 className="text-2xl font-extrabold">Upravit recept</h1>
        <div>
          <label className="mb-1 block text-xs font-medium text-ink/55">Název</label>
          <input className={inp} value={title} onChange={(e) => setTitle(e.target.value)} />
        </div>
        <div className="max-w-[10rem]">
          <label className="mb-1 block text-xs font-medium text-ink/55">Porce</label>
          <input type="number" min="1" className={inp} value={servings} onChange={(e) => setServings(e.target.value)} />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-ink/55">Suroviny (jeden řádek = jedna surovina)</label>
          <div className="space-y-1.5">
            {lines.map((ln, i) => (
              <input key={i} className={inp} value={ln}
                onChange={(e) => setLines((cur) => cur.map((x, j) => (j === i ? e.target.value : x)))} />
            ))}
          </div>
          <p className="mt-1 text-xs text-ink/40">Pozn.: počet řádků měnit nelze; napárování surovin řeš přes „Ručně…" v Přidat.</p>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-ink/55">Postup</label>
          <textarea className={`${inp} min-h-[10rem]`} value={instructions} onChange={(e) => setInstructions(e.target.value)} />
          <p className="mt-1 text-xs text-ink/40">Každý krok na samostatný řádek.</p>
        </div>
        {tagGroups.length > 0 && (
          <div>
            <label className="mb-1.5 block text-xs font-medium text-ink/55">Tagy</label>
            <div className="space-y-2.5 rounded-lg border border-line bg-paper p-3">
              {tagGroups.map((g) => (
                <div key={g.namespace}>
                  <p className="mb-1 text-[11px] font-semibold text-ink/45">{g.label}</p>
                  <div className="flex flex-wrap gap-1.5">
                    {g.tags.map((t) => {
                      const key = `${g.namespace}:${t.slug}`;
                      const active = selectedTags.includes(key);
                      return (
                        <button key={key} type="button" onClick={() => toggleTag(key)}
                          className={`rounded-full px-2.5 py-1 text-xs font-medium transition ${
                            active ? "bg-basil text-white" : "border border-line bg-white text-ink/60 hover:border-basil"
                          }`}>
                          {t.label}
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
        <div>
          <label className="mb-1 block text-xs font-medium text-ink/55">Moje poznámka</label>
          <textarea className={`${inp} min-h-[5rem]`} value={note} onChange={(e) => setNote(e.target.value)} placeholder="Co bych příště udělal jinak…" />
        </div>
        <div className="flex gap-2">
          <Button onClick={save} disabled={busy}>{busy ? "Ukládám…" : "Uložit"}</Button>
          <Button variant="ghost" onClick={() => onDone(null)}>Zrušit</Button>
        </div>
      </div>
    </div>
  );
}

function CookingMode({ recipe, steps, onClose }) {
  const [idx, setIdx] = useState(0);
  const [checked, setChecked] = useState(() => new Set());
  const wakeRef = useRef(null);

  useEffect(() => {
    let released = false;
    (async () => {
      try {
        if ("wakeLock" in navigator) {
          wakeRef.current = await navigator.wakeLock.request("screen");
        }
      } catch { /* ignore */ }
    })();
    return () => {
      released = true;
      try { wakeRef.current && wakeRef.current.release(); } catch { /* ignore */ }
      void released;
    };
  }, []);

  const last = steps.length - 1;
  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-paper">
      <div className="flex items-center justify-between border-b border-line px-4 py-3">
        <span className="truncate text-sm font-semibold">{recipe.title}</span>
        <button onClick={onClose} className="rounded-full border border-line px-3 py-1 text-sm">Zavřít ✕</button>
      </div>

      <div className="flex-1 overflow-auto px-5 py-6">
        {/* checklist surovin */}
        <details className="mb-6 rounded-xl2 border border-line bg-white p-3">
          <summary className="cursor-pointer text-sm font-medium text-ink/70">Suroviny ({recipe.ingredients.length})</summary>
          <ul className="mt-2 space-y-1">
            {recipe.ingredients.map((ri) => (
              <li key={ri.id}>
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" className="accent-basil"
                    checked={checked.has(ri.id)}
                    onChange={() => setChecked((c) => { const n = new Set(c); n.has(ri.id) ? n.delete(ri.id) : n.add(ri.id); return n; })} />
                  <span className={checked.has(ri.id) ? "text-ink/35 line-through" : ""}>{ri.raw_text}</span>
                </label>
              </li>
            ))}
          </ul>
        </details>

        <div className="mx-auto max-w-2xl">
          <div className="nums mb-3 text-sm font-semibold text-basil-dark">Krok {idx + 1} / {steps.length}</div>
          <p className="text-2xl leading-relaxed sm:text-3xl">{steps[idx]}</p>
        </div>
      </div>

      <div className="flex items-center gap-3 border-t border-line px-5 py-4">
        <button onClick={() => setIdx((i) => Math.max(0, i - 1))} disabled={idx === 0}
          className="flex-1 rounded-full border border-line py-3 text-base font-semibold disabled:opacity-40">
          ← Zpět
        </button>
        {idx < last ? (
          <button onClick={() => setIdx((i) => Math.min(last, i + 1))}
            className="flex-1 rounded-full bg-basil py-3 text-base font-semibold text-white">
            Další →
          </button>
        ) : (
          <button onClick={onClose} className="flex-1 rounded-full bg-basil py-3 text-base font-semibold text-white">
            Hotovo ✓
          </button>
        )}
      </div>
    </div>
  );
}
