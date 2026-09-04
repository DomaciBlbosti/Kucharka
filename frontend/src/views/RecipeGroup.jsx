import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { CookMeter, EmptyState, Meta, ReadyStamp, Spinner, Stars } from "../components/ui";

/** Varianty jednoho jídla – recepty se stejným názvem z různých zdrojů.
 *  Klíč skupiny vyrábí backend (textnorm.title_key), tady se jen zobrazuje. */
export default function RecipeGroup() {
  const { key } = useParams();
  const [items, setItems] = useState(null);
  const [total, setTotal] = useState(0);

  useEffect(() => {
    let live = true;
    setItems(null);
    api
      .recipeGroup(key)
      .then((r) => {
        if (!live) return;
        setItems(r.items);
        setTotal(r.total);
      })
      .catch(() => live && setItems([]));
    return () => {
      live = false;
    };
  }, [key]);

  const title = items?.[0]?.title || "Varianty receptu";

  return (
    <div>
      <Link to="/" className="text-sm text-ink/45 hover:text-basil">
        ← zpět na recepty
      </Link>
      <h1 className="mt-2 font-display text-2xl font-bold">{title}</h1>
      <p className="mb-6 mt-1 text-sm text-ink/55">
        {total === 1
          ? "Zatím jen jedna varianta."
          : `${total} variant tohoto jídla — nahoře ty, ke kterým chybí nejmíň surovin.`}
      </p>

      {items === null ? (
        <Spinner />
      ) : items.length === 0 ? (
        <EmptyState title="Tahle kategorie je prázdná">
          Recepty se možná mezitím změnily. Zkus se vrátit na výpis.
        </EmptyState>
      ) : (
        <div className="flex flex-col gap-3">
          {items.map((r) => (
            <Link
              key={r.id}
              to={`/recept/${r.id}`}
              className="flex gap-4 rounded-xl2 border border-line bg-white p-3 shadow-card transition hover:-translate-y-0.5 hover:shadow-lg"
            >
              <div className="relative h-24 w-32 shrink-0 overflow-hidden rounded-lg bg-basil-soft">
                {r.image_url ? (
                  <img src={r.image_url} alt="" loading="lazy" className="h-full w-full object-cover" />
                ) : (
                  <div className="flex h-full items-center justify-center text-3xl opacity-30">🍽️</div>
                )}
                <div className="absolute left-1 top-1">
                  <ReadyStamp missing={r.missing_count} total={r.total} />
                </div>
              </div>
              <div className="flex min-w-0 flex-1 flex-col gap-2">
                <h3 className="line-clamp-1 font-semibold leading-snug">{r.title}</h3>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <Stars rating={r.rating} count={r.rating_count} />
                  <Meta icon="⏱">{r.total_time ? `${r.total_time} min` : null}</Meta>
                  <Meta icon="🔥">
                    {r.kcal_per_serving ? `${Math.round(r.kcal_per_serving)} kcal` : null}
                  </Meta>
                  {r.source_domain && (
                    <span className="text-xs text-ink/40">{r.source_domain}</span>
                  )}
                </div>
                <div className="mt-auto">
                  <CookMeter have={r.have} total={r.total} size="sm" />
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
