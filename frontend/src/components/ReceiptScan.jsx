import { useState } from "react";
import { api } from "../api";
import { IngredientPicker } from "./IngredientPicker";
import { PhotoCapture } from "./PhotoCapture";
import { Button, Spinner } from "./ui";

/** Skenování účtenky ve více úsecích ("panoramaticky") + review před přidáním do spíže. */
export function ReceiptScan({ onClose, onAdded }) {
  const [step, setStep] = useState("capture"); // capture | processing | review | error
  const [items, setItems] = useState([]);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const process = async (files) => {
    setStep("processing");
    setError(null);
    try {
      const r = await api.scanReceipt(files);
      setItems(
        r.items.map((it) => ({
          ...it,
          include: true,
          new_name: it.ingredient_id ? "" : it.raw_name,
        }))
      );
      setStep("review");
    } catch (e) {
      setError(e?.message || "Čtení účtenky selhalo.");
      setStep("error");
    }
  };

  const setItem = (i, patch) =>
    setItems((cur) => cur.map((it, j) => (j === i ? { ...it, ...patch } : it)));

  const confirm = async () => {
    setBusy(true);
    try {
      const payload = items.map((it) => ({
        raw_name: it.raw_name,
        ingredient_id: it.ingredient_id || null,
        new_name: it.ingredient_id ? null : it.new_name,
        include: it.include,
      }));
      const r = await api.confirmReceipt(payload);
      onAdded(r);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-ink/40 p-0 sm:items-center sm:p-4" onClick={onClose}>
      <div
        className="flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden rounded-t-2xl bg-white shadow-xl sm:rounded-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-line p-4">
          <h2 className="text-lg font-bold">🧾 Skenovat účtenku</h2>
          <button onClick={onClose} className="text-2xl leading-none text-ink/40 hover:text-ink">×</button>
        </div>

        <div className="flex-1 overflow-auto p-4">
          {step === "capture" && (
            <PhotoCapture
              hint="Dlouhou účtenku vyfoť po úsecích odshora dolů, s malým přesahem mezi snímky — přeskládám je automaticky. Pořadí lze upravit šipkami."
              onProcess={process}
              processLabel="Zpracovat"
            />
          )}

          {step === "processing" && (
            <div className="py-10">
              <Spinner label="Čtu účtenku… (může trvat i déle)" />
            </div>
          )}

          {step === "error" && (
            <div className="py-6 text-center">
              <p className="text-sm text-miss">{error}</p>
              <Button variant="ghost" onClick={() => setStep("capture")} className="mt-4">Zpět</Button>
            </div>
          )}

          {step === "review" && (
            <>
              <p className="mb-3 text-sm text-ink/60">
                Nalezeno {items.length} položek. Zkontroluj a uprav, co sedí.
              </p>
              {items.length === 0 ? (
                <p className="py-6 text-center text-sm text-ink/40">
                  Nic nenalezeno — zkus ostřejší fotky nebo větší přesah mezi úseky.
                </p>
              ) : (
                <ul className="space-y-2">
                  {items.map((it, i) => (
                    <li key={i} className="rounded-lg border border-line/70 p-3">
                      <div className="flex items-start gap-2">
                        <input
                          type="checkbox"
                          className="mt-1 accent-basil"
                          checked={it.include}
                          onChange={(e) => setItem(i, { include: e.target.checked })}
                        />
                        <div className="flex-1">
                          <p className="text-sm font-medium">{it.raw_name}</p>
                          {it.ingredient_id ? (
                            <p className="mt-0.5 text-xs text-have">✓ napárováno: {it.ingredient_name}</p>
                          ) : (
                            <div className="mt-1.5 flex flex-col gap-1.5 sm:flex-row">
                              <div className="flex-1">
                                <IngredientPicker
                                  placeholder="Přiřadit existující surovinu…"
                                  onPick={(o) => setItem(i, { ingredient_id: o.id, ingredient_name: o.name_cs })}
                                />
                              </div>
                              <input
                                value={it.new_name}
                                onChange={(e) => setItem(i, { new_name: e.target.value })}
                                placeholder="…nebo název nové suroviny"
                                className="w-full rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil sm:w-48"
                              />
                            </div>
                          )}
                        </div>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>

        {step === "review" && (
          <div className="flex items-center gap-2 border-t border-line p-3">
            <Button variant="ghost" onClick={() => setStep("capture")}>Zpět</Button>
            <Button onClick={confirm} disabled={busy}>
              {busy ? "Přidávám…" : "Přidat do spíže"}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
