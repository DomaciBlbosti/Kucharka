import { useEffect, useMemo, useState } from "react";
import { auth } from "../api";

// Menu surovin podle kategorií: 1. úroveň jako záložky, 2. úroveň jako
// podřádek, pak chipy nejpoužívanějších surovin. Klik = přidat/odebrat.
//
// props:
//   pickedIds  – Set/pole id už vybraných surovin
//   onToggle(o) – surovina {id, name_cs, kcal_100g}; rodič rozhodne add/remove

const ICONS = {
  maso: "🥩", ryby: "🐟", "mořské plody": "🦐", zelenina: "🥦", ovoce: "🍎",
  "mléčné výrobky": "🧀", mléčné: "🧀", sýry: "🧀", vejce: "🥚", pečivo: "🍞",
  obiloviny: "🌾", mouka: "🌾", těstoviny: "🍝", rýže: "🍚", luštěniny: "🫘",
  koření: "🌶️", bylinky: "🌿", tuky: "🧈", oleje: "🫒", nápoje: "🥤",
  alkohol: "🍷", sladidla: "🍯", cukr: "🍬", čokoláda: "🍫", ořechy: "🥜",
  semena: "🌻", houby: "🍄", omáčky: "🥫", konzervy: "🥫", ostatní: "🧂",
};
const icon = (name) => {
  const k = (name || "").toLowerCase();
  for (const [key, v] of Object.entries(ICONS)) if (k.includes(key)) return v;
  return "•";
};

const get = (url) =>
  window
    .fetch(url, { headers: auth.get() ? { Authorization: `Bearer ${auth.get()}` } : {} })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))));

const LIMIT = 48;

export function IngredientBrowser({ pickedIds, onToggle }) {
  const picked = useMemo(
    () => (pickedIds instanceof Set ? pickedIds : new Set(pickedIds || [])),
    [pickedIds]
  );
  const [tree, setTree] = useState(null);
  const [cat, setCat] = useState(null); // 1. úroveň
  const [sub, setSub] = useState(null); // 2. úroveň nebo null = celá kategorie
  const [q, setQ] = useState("");
  const [items, setItems] = useState(null);
  const [total, setTotal] = useState(0);
  const [limit, setLimit] = useState(LIMIT);

  useEffect(() => {
    get("/api/ingredients/categories/tree")
      .then((t) => {
        setTree(t);
        if (t.length > 0) setCat(t[0].name);
      })
      .catch(() => setTree([]));
  }, []);

  useEffect(() => {
    setSub(null);
    setQ("");
    setLimit(LIMIT);
  }, [cat]);

  useEffect(() => {
    if (!cat) return;
    let live = true;
    setItems(null);
    const path = sub ? `${cat} > ${sub}` : cat;
    const u = new URLSearchParams({ category: path, limit: String(limit) });
    if (q.trim()) u.set("q", q.trim());
    const t = setTimeout(() => {
      get(`/api/ingredients/by-category?${u}`)
        .then((r) => {
          if (!live) return;
          setItems(r.items);
          setTotal(r.total);
        })
        .catch(() => live && setItems([]));
    }, q ? 180 : 0);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [cat, sub, q, limit]);

  if (tree === null) return <p className="py-3 text-center text-xs text-ink/40">Načítám kategorie…</p>;
  if (tree.length === 0)
    return <p className="py-3 text-center text-xs text-ink/40">Suroviny zatím nemají kategorie (Admin → kategorizace).</p>;

  const node = tree.find((n) => n.name === cat);

  return (
    <div className="rounded-xl2 border border-line bg-paper p-3">
      {/* 1. úroveň */}
      <div className="-mx-1 flex gap-1.5 overflow-x-auto px-1 pb-2" style={{ scrollbarWidth: "thin" }}>
        {tree.map((n) => (
          <button
            key={n.name}
            onClick={() => setCat(n.name)}
            className={`shrink-0 rounded-full px-3 py-1.5 text-sm font-medium transition ${
              n.name === cat ? "bg-basil text-white" : "bg-white border border-line text-ink/70 hover:border-basil"
            }`}
          >
            {icon(n.name)} {n.name}
          </button>
        ))}
      </div>

      {/* 2. úroveň */}
      {node?.children?.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          <button
            onClick={() => setSub(null)}
            className={`rounded-full px-2.5 py-1 text-xs transition ${
              sub === null ? "bg-basil-soft text-basil-dark font-semibold" : "text-ink/55 hover:bg-white"
            }`}
          >
            vše
          </button>
          {node.children.map((c) => (
            <button
              key={c.name}
              onClick={() => setSub(c.name)}
              className={`rounded-full px-2.5 py-1 text-xs transition ${
                sub === c.name ? "bg-basil-soft text-basil-dark font-semibold" : "text-ink/55 hover:bg-white"
              }`}
            >
              {c.name}
            </button>
          ))}
        </div>
      )}

      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder={`Hledat v ${sub || cat}…`}
        className="mb-2 w-full rounded-full border border-line bg-white px-3 py-1.5 text-sm outline-none focus:border-basil"
      />

      {/* suroviny */}
      {items === null ? (
        <p className="py-3 text-center text-xs text-ink/40">…</p>
      ) : items.length === 0 ? (
        <p className="py-3 text-center text-xs text-ink/40">Nic tu není.</p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {items.map((o) => {
            const on = picked.has(o.id);
            return (
              <button
                key={o.id}
                onClick={() => onToggle(o)}
                title={o.use_count ? `${o.use_count} receptů` : undefined}
                className={`rounded-full px-2.5 py-1 text-sm transition ${
                  on
                    ? "bg-basil text-white"
                    : "bg-white border border-line text-ink/80 hover:border-basil"
                }`}
              >
                {on ? "✓ " : ""}
                {o.name_cs}
                {o.use_count > 0 && (
                  <span className={`nums ml-1 text-[10px] ${on ? "text-white/70" : "text-ink/35"}`}>
                    {o.use_count}
                  </span>
                )}
              </button>
            );
          })}
          {total > items.length && (
            <button
              onClick={() => setLimit((l) => l + LIMIT)}
              className="rounded-full px-2.5 py-1 text-sm text-basil-dark hover:underline"
            >
              dalších {Math.min(LIMIT, total - items.length)}…
            </button>
          )}
        </div>
      )}
    </div>
  );
}
