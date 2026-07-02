import { useEffect, useState } from "react";
import { api } from "../api";
import { IngredientPicker } from "../components/IngredientPicker";
import { ReceiptScan } from "../components/ReceiptScan";
import { BarcodeScan } from "../components/BarcodeScan";
import { Button, EmptyState, Spinner } from "../components/ui";

export default function Pantry() {
  const [items, setItems] = useState(null);
  const [scanOpen, setScanOpen] = useState(false);
  const [barcodeOpen, setBarcodeOpen] = useState(false);
  const [scanMsg, setScanMsg] = useState(null);

  const load = () => api.pantry().then(setItems);
  useEffect(() => { load(); }, []);

  const add = async (ing) => { await api.addPantry({ ingredient_id: ing.id }); load(); };
  const remove = async (ingredientId) => { await api.removePantry(ingredientId); load(); };
  const toggleSoon = async (ingredientId) => {
    const upd = await api.toggleUseSoon(ingredientId);
    setItems((cur) => cur.map((x) => (x.ingredient_id === ingredientId ? { ...x, use_soon: upd.use_soon } : x)));
  };

  const soon = (items || []).filter((i) => i.use_soon);

  const Chip = ({ it }) => (
    <li className={`group inline-flex items-center gap-1 rounded-full border py-1.5 pl-4 pr-1.5 text-sm shadow-sm ${
      it.use_soon ? "border-miss/50 bg-miss/5" : "border-line bg-white"}`}>
      <span>{it.ingredient.name_cs}</span>
      <button onClick={() => toggleSoon(it.ingredient_id)} title={it.use_soon ? "Zrušit spotřebovat brzy" : "Označit spotřebovat brzy"}
        className={`flex h-6 w-6 items-center justify-center rounded-full ${it.use_soon ? "text-miss" : "text-ink/30 hover:text-miss"}`}>
        ⏳
      </button>
      <button onClick={() => remove(it.ingredient_id)} aria-label="Odebrat"
        className="flex h-6 w-6 items-center justify-center rounded-full text-ink/40 hover:bg-miss/10 hover:text-miss">
        ✕
      </button>
    </li>
  );

  return (
    <div>
      <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-extrabold">Moje spíž</h1>
          <p className="text-sm text-ink/60">
            Co máš doma. Recepty se podle toho řadí a počítají chybějící suroviny.
            Ikonou ⏳ označíš, co je potřeba <b>spotřebovat brzy</b>.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={() => setBarcodeOpen(true)}>📷 Čárový kód</Button>
          <Button variant="ghost" onClick={() => setScanOpen(true)}>🧾 Skenovat účtenku</Button>
        </div>
      </header>
      {scanMsg && (
        <p className="mb-4 rounded-lg bg-basil-soft px-3 py-2 text-sm text-basil-dark">{scanMsg}</p>
      )}

      <div className="mb-6 max-w-md">
        <IngredientPicker onPick={add} placeholder="Přidat surovinu do spíže…" />
      </div>

      {items === null ? (
        <Spinner />
      ) : items.length === 0 ? (
        <EmptyState title="Spíž je prázdná">
          Přidej, co máš doma — třeba mouku, vejce, cibuli. Pak ti kuchařka ukáže,
          co z toho uvaříš.
        </EmptyState>
      ) : (
        <>
          {soon.length > 0 && (
            <section className="mb-6 rounded-xl2 border border-miss/40 bg-miss/5 p-4">
              <h2 className="mb-2 text-sm font-bold text-miss">⏳ Spotřebovat brzy</h2>
              <ul className="flex flex-wrap gap-2">
                {soon.map((it) => <Chip key={it.id} it={it} />)}
              </ul>
            </section>
          )}
          <ul className="flex flex-wrap gap-2">
            {items.map((it) => <Chip key={it.id} it={it} />)}
          </ul>
        </>
      )}

      {barcodeOpen && (
        <BarcodeScan onClose={() => { setBarcodeOpen(false); load(); }} />
      )}

      {scanOpen && (
        <ReceiptScan
          onClose={() => setScanOpen(false)}
          onAdded={(r) => {
            setScanOpen(false);
            setScanMsg(
              `Přidáno ${r.added} nových položek do spíže` +
                (r.already_had ? ` (${r.already_had} už tam bylo).` : ".")
            );
            load();
          }}
        />
      )}
    </div>
  );
}
