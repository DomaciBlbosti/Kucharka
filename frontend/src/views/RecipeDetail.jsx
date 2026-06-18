import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { Button, CookMeter, Meta, Spinner, Stars } from "../components/ui";

export default function RecipeDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const [r, setR] = useState(null);
  const [added, setAdded] = useState(null);
  const [addedIds, setAddedIds] = useState(() => new Set());

  useEffect(() => {
    setR(null);
    api.recipe(id).then(setR).catch(() => setR(false));
  }, [id]);

  if (r === null) return <Spinner />;
  if (r === false) return <p className="py-16 text-center text-ink/50">Recept nenalezen.</p>;

  const missing = new Set(r.missing_ingredient_ids);
  const steps = (r.instructions || "")
    .split(/\n+/)
    .map((s) => s.trim())
    .filter(Boolean);

  const addMissing = async () => {
    const res = await api.shoppingFromRecipe(r.id);
    setAdded(res.added);
  };

  const addOne = async (ri) => {
    await api.addShopping({ label: ri.raw_text, ingredient_id: ri.ingredient_id || null });
    setAddedIds((cur) => new Set(cur).add(ri.id));
  };

  return (
    <div>
      <Link to="/" className="mb-4 inline-flex text-sm text-ink/50 hover:text-ink">
        ← Zpět na recepty
      </Link>

      <div className="overflow-hidden rounded-xl2 border border-line bg-white shadow-card">
        {r.image_url && (
          <div className="aspect-[21/9] w-full overflow-hidden bg-basil-soft">
            <img src={r.image_url} alt="" className="h-full w-full object-cover" />
          </div>
        )}
        <div className="p-5 sm:p-7">
          <h1 className="text-3xl font-extrabold leading-tight">{r.title}</h1>
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1">
            <Stars rating={r.rating} count={r.rating_count} />
            <Meta icon="⏱">{r.total_time ? `${r.total_time} min` : null}</Meta>
            <Meta icon="🍽">{r.servings ? `${r.servings} porce` : null}</Meta>
            <Meta icon="🔥">
              {r.kcal_per_serving ? `${Math.round(r.kcal_per_serving)} kcal/porce` : null}
            </Meta>
            {r.source_domain && (
              <a
                href={r.source_url}
                target="_blank"
                rel="noreferrer"
                className="text-sm text-basil underline-offset-2 hover:underline"
              >
                {r.source_domain} ↗
              </a>
            )}
          </div>

          <div className="mt-5 max-w-sm">
            <CookMeter have={r.have} total={r.total} />
          </div>

          <div className="mt-8 grid gap-8 md:grid-cols-[1fr_1.3fr]">
            {/* Ingredience */}
            <section>
              <div className="mb-3 flex items-center justify-between">
                <h2 className="text-xl font-bold">Suroviny</h2>
                {r.missing_count > 0 && (
                  <Button variant="ghost" onClick={addMissing}>
                    + Chybějící do nákupu
                  </Button>
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
                    <li
                      key={ri.id}
                      className="flex items-center justify-between gap-3 rounded-lg border border-line/70 px-3 py-2 text-sm"
                    >
                      <span className="flex items-center gap-2">
                        <span
                          className={`h-2 w-2 shrink-0 rounded-full ${
                            isHave ? "bg-have" : isMissing ? "bg-miss" : "bg-ink/15"
                          }`}
                        />
                        {ri.raw_text}
                      </span>
                      <span className="flex shrink-0 items-center gap-2">
                        {ri.kcal != null && (
                          <span className="nums text-xs text-ink/40">
                            {Math.round(ri.kcal)} kcal
                          </span>
                        )}
                        {addedIds.has(ri.id) ? (
                          <span
                            title="Přidáno do nákupu"
                            className="flex h-7 w-7 items-center justify-center rounded-full bg-basil-soft text-basil-dark"
                          >
                            ✓
                          </span>
                        ) : (
                          <button
                            onClick={() => addOne(ri)}
                            title="Přidat do nákupu"
                            className="flex h-7 w-7 items-center justify-center rounded-full border border-line text-ink/50 hover:border-basil hover:text-basil-dark"
                          >
                            +
                          </button>
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

            {/* Postup */}
            <section>
              <h2 className="mb-3 text-xl font-bold">Postup</h2>
              {steps.length ? (
                <ol className="space-y-3">
                  {steps.map((s, i) => (
                    <li key={i} className="flex gap-3 text-sm leading-relaxed">
                      <span className="nums mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-basil-soft text-xs font-bold text-basil-dark">
                        {i + 1}
                      </span>
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
            <Button
              variant="danger"
              onClick={async () => {
                await api.deleteRecipe(r.id);
                nav("/");
              }}
            >
              Smazat recept
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
