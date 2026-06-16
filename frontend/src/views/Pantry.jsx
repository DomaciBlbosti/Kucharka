import { useEffect, useState } from "react";
import { api } from "../api";
import { IngredientPicker } from "../components/IngredientPicker";
import { Button, EmptyState, Spinner } from "../components/ui";

export default function Pantry() {
  const [items, setItems] = useState(null);

  const load = () => api.pantry().then(setItems);
  useEffect(() => {
    load();
  }, []);

  const add = async (ing) => {
    await api.addPantry({ ingredient_id: ing.id });
    load();
  };
  const remove = async (ingredientId) => {
    await api.removePantry(ingredientId);
    load();
  };

  return (
    <div>
      <header className="mb-5">
        <h1 className="text-2xl font-extrabold">Moje spíž</h1>
        <p className="text-sm text-ink/60">
          Co máš doma. Recepty se podle toho řadí a počítají chybějící suroviny.
        </p>
      </header>

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
        <ul className="flex flex-wrap gap-2">
          {items.map((it) => (
            <li
              key={it.id}
              className="group inline-flex items-center gap-2 rounded-full border border-line bg-white py-1.5 pl-4 pr-1.5 text-sm shadow-sm"
            >
              <span>{it.ingredient.name_cs}</span>
              <button
                onClick={() => remove(it.ingredient_id)}
                aria-label="Odebrat"
                className="flex h-6 w-6 items-center justify-center rounded-full text-ink/40 hover:bg-miss/10 hover:text-miss"
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
