import { useEffect, useRef, useState } from "react";
import { api } from "../api";

// Našeptávač surovin z kanonické DB.
export function IngredientPicker({ onPick, placeholder = "Hledat surovinu…" }) {
  const [q, setQ] = useState("");
  const [opts, setOpts] = useState([]);
  const [open, setOpen] = useState(false);
  const box = useRef(null);

  useEffect(() => {
    if (!q.trim()) {
      setOpts([]);
      return;
    }
    let live = true;
    const t = setTimeout(() => {
      api.ingredients(q).then((r) => live && setOpts(r));
    }, 180);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [q]);

  useEffect(() => {
    const close = (e) => {
      if (box.current && !box.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, []);

  return (
    <div className="relative" ref={box}>
      <input
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        placeholder={placeholder}
        className="w-full rounded-full border border-line bg-white px-4 py-2.5 text-sm outline-none focus:border-basil focus:ring-2 focus:ring-basil/20"
      />
      {open && opts.length > 0 && (
        <ul className="absolute z-20 mt-2 max-h-72 w-full overflow-auto rounded-xl2 border border-line bg-white p-1 shadow-card">
          {opts.map((o) => (
            <li key={o.id}>
              <button
                onClick={() => {
                  onPick(o);
                  setQ("");
                  setOpts([]);
                  setOpen(false);
                }}
                className="flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-sm hover:bg-basil-soft"
              >
                <span>{o.name_cs}</span>
                {o.kcal_100g != null && (
                  <span className="nums text-xs text-ink/40">
                    {Math.round(o.kcal_100g)} kcal/100 g
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
