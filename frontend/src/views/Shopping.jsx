import { useEffect, useState } from "react";
import { api } from "../api";
import { IngredientPicker } from "../components/IngredientPicker";
import { EmptyState, Spinner } from "../components/ui";

export default function Shopping() {
  const [items, setItems] = useState(null);

  const load = () => api.shopping().then(setItems);
  useEffect(() => {
    load();
  }, []);

  const addManual = async (ing) => {
    await api.addShopping({ label: ing.name_cs, ingredient_id: ing.id });
    load();
  };
  const toggle = async (id) => {
    await api.toggleShopping(id);
    load();
  };
  const remove = async (id) => {
    await api.removeShopping(id);
    load();
  };

  return (
    <div>
      <header className="mb-5">
        <h1 className="text-2xl font-extrabold">Nákupní seznam</h1>
        <p className="text-sm text-ink/60">
          Chybějící suroviny z receptů se sem přidávají jedním klikem.
        </p>
      </header>

      <div className="mb-6 max-w-md">
        <IngredientPicker onPick={addManual} placeholder="Přidat položku ručně…" />
      </div>

      {items === null ? (
        <Spinner />
      ) : items.length === 0 ? (
        <EmptyState title="Nákupní seznam je prázdný">
          Otevři recept a dej <strong>+ Chybějící do nákupu</strong>, nebo přidej
          položku ručně výše.
        </EmptyState>
      ) : (
        <ul className="max-w-md divide-y divide-line overflow-hidden rounded-xl2 border border-line bg-white">
          {items.map((it) => (
            <li key={it.id} className="flex items-center gap-3 px-4 py-3">
              <button
                onClick={() => toggle(it.id)}
                className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full border-2 transition ${
                  it.checked
                    ? "border-have bg-have text-white"
                    : "border-line hover:border-basil"
                }`}
                aria-label="Odškrtnout"
              >
                {it.checked ? "✓" : ""}
              </button>
              <span
                className={`flex-1 text-sm ${
                  it.checked ? "text-ink/40 line-through" : ""
                }`}
              >
                {it.label}
              </span>
              <button
                onClick={() => remove(it.id)}
                className="text-ink/30 hover:text-miss"
                aria-label="Smazat"
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
