import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { PhotoCapture } from "./PhotoCapture";
import { Button, Spinner } from "./ui";

const inp = "w-full rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil";

// {amount, unit} -> editovatelný textový "10 dkg" / "1" / "" (bez množství)
function qtyText(amount, unit) {
  if (amount == null) return "";
  const a = Number.isInteger(amount) ? String(amount) : String(amount).replace(".", ",");
  return unit ? `${a} ${unit}` : a;
}

/** Import receptu z fotek papírového/rukou psaného receptu (po úsecích). */
export function RecipeFromPhoto({ onClose }) {
  const nav = useNavigate();
  const [step, setStep] = useState("capture"); // capture | processing | review | error
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const [title, setTitle] = useState("");
  const [instructions, setInstructions] = useState("");
  const [lines, setLines] = useState([]); // [{qty, name}]
  const [imageUrl, setImageUrl] = useState(null);

  const process = async (files) => {
    setStep("processing");
    setError(null);
    try {
      const draft = await api.recipeFromPhoto(files);
      setTitle(draft.title || "");
      setInstructions(draft.instructions || "");
      setImageUrl(draft.image_url || null);
      const ing = draft.ingredients || [];
      setLines(
        ing.length
          ? ing.map((i) =>
              typeof i === "string"
                ? { qty: "", name: i }
                : { qty: qtyText(i.amount, i.unit), name: i.name || i.raw_text || "" }
            )
          : [{ qty: "", name: "" }]
      );
      setStep("review");
    } catch (e) {
      setError(e?.message || "Čtení receptu selhalo.");
      setStep("error");
    }
  };

  const setLine = (i, patch) => setLines((cur) => cur.map((x, j) => (j === i ? { ...x, ...patch } : x)));
  const addLine = () => setLines((cur) => [...cur, { qty: "", name: "" }]);
  const removeLine = (i) => setLines((cur) => cur.filter((_, j) => j !== i));

  const save = async () => {
    setBusy(true);
    try {
      const ingredients = lines
        .map((l) => `${l.qty.trim()} ${l.name.trim()}`.trim())
        .filter(Boolean);
      const r = await api.saveRecipeFromPhoto({
        title: title.trim(),
        instructions,
        ingredients,
        image_url: imageUrl,
      });
      onClose();
      nav(`/recept/${r.id}`);
    } finally {
      setBusy(false);
    }
  };

  const hasContent = lines.some((l) => l.name.trim());

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-ink/40 p-0 sm:items-center sm:p-4" onClick={onClose}>
      <div
        className="flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden rounded-t-2xl bg-white shadow-xl sm:rounded-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-line p-4">
          <h2 className="text-lg font-bold">📷 Recept z fotky</h2>
          <button onClick={onClose} className="text-2xl leading-none text-ink/40 hover:text-ink">×</button>
        </div>

        <div className="flex-1 overflow-auto p-4">
          {step === "capture" && (
            <PhotoCapture
              hint="Delší recept (víc stránek nebo dlouhý text) vyfoť po úsecích — název, suroviny a postup automaticky sloučím."
              onProcess={process}
              processLabel="Přečíst recept"
            />
          )}

          {step === "processing" && (
            <div className="py-10">
              <Spinner label="Čtu recept… (může chvíli trvat)" />
            </div>
          )}

          {step === "error" && (
            <div className="py-6 text-center">
              <p className="text-sm text-miss">{error}</p>
              <Button variant="ghost" onClick={() => setStep("capture")} className="mt-4">Zpět</Button>
            </div>
          )}

          {step === "review" && (
            <div className="space-y-4">
              <p className="text-sm text-ink/60">Zkontroluj a uprav, co sedí, pak ulož.</p>

              {imageUrl && (
                <img src={imageUrl} alt="Vyfocený recept" className="max-h-40 w-full rounded-lg border border-line object-cover" />
              )}

              <div>
                <label className="mb-1 block text-xs font-medium text-ink/55">Název</label>
                <input className={inp} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Název receptu" />
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium text-ink/55">Suroviny</label>
                <div className="mb-1 flex gap-1.5 px-0.5 text-[11px] text-ink/40">
                  <span className="w-20 shrink-0">množství</span>
                  <span className="flex-1">surovina</span>
                </div>
                <div className="space-y-1.5">
                  {lines.map((ln, i) => (
                    <div key={i} className="flex gap-1.5">
                      <input
                        className={`${inp} w-20 shrink-0`}
                        value={ln.qty}
                        onChange={(e) => setLine(i, { qty: e.target.value })}
                        placeholder="– množství –"
                      />
                      <input
                        className={inp}
                        value={ln.name}
                        onChange={(e) => setLine(i, { name: e.target.value })}
                        placeholder="surovina, nebo nadpis sekce (např. „Poleva:“)"
                      />
                      <button onClick={() => removeLine(i)} className="shrink-0 rounded-lg border border-line px-2 text-sm text-miss">✕</button>
                    </div>
                  ))}
                </div>
                <button onClick={addLine} className="mt-1.5 text-sm text-basil-dark hover:underline">+ přidat řádek</button>
                <p className="mt-1 text-xs text-ink/40">
                  Řádek bez množství, co jen pojmenovává část receptu (např. „Poleva:"), klidně nech
                  jako oddělovač, nebo smaž.
                </p>
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium text-ink/55">Postup</label>
                <textarea className={`${inp} min-h-[8rem]`} value={instructions} onChange={(e) => setInstructions(e.target.value)} />
                <p className="mt-1 text-xs text-ink/40">Každý krok na samostatný řádek.</p>
              </div>
            </div>
          )}
        </div>

        {step === "review" && (
          <div className="flex items-center gap-2 border-t border-line p-3">
            <Button variant="ghost" onClick={() => setStep("capture")}>Zpět</Button>
            <Button onClick={save} disabled={busy || !title.trim() || !hasContent}>
              {busy ? "Ukládám…" : "Uložit recept"}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
