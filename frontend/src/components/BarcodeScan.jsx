import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { IngredientPicker } from "./IngredientPicker";
import { Button } from "./ui";

const FORMATS = ["ean_13", "ean_8", "upc_a", "upc_e", "code_128"];
const RESCAN_COOLDOWN_MS = 3000;

/** Skenování čárových kódů při vybalování nákupu. Kamera (pokud prohlížeč
 * umí BarcodeDetector) + ruční zadání kódu vždy dostupné jako záloha. */
export function BarcodeScan({ onClose }) {
  const videoRef = useRef(null);
  const streamRef = useRef(null);
  const loopRef = useRef(null);
  const lastRef = useRef({ code: null, at: 0 });
  const newNameRef = useRef(null);

  const [supported] = useState(() => "BarcodeDetector" in window);
  const [camError, setCamError] = useState(null);
  const [toast, setToast] = useState(null);
  const [pending, setPending] = useState(null); // {code, off_name, brand, matched}
  const [manual, setManual] = useState("");
  const [busy, setBusy] = useState(false);

  const handleCode = async (code) => {
    const now = Date.now();
    if (lastRef.current.code === code && now - lastRef.current.at < RESCAN_COOLDOWN_MS) return;
    lastRef.current = { code, at: now };
    setPending(null);
    try {
      const r = await api.scanBarcode(code);
      if (r.added) {
        setToast(`✓ Přidáno: ${r.ingredient_name}`);
        setTimeout(() => setToast(null), 2000);
      } else {
        setPending({ code, off_name: r.off_name, brand: r.brand, matched: r.matched });
      }
    } catch {
      setToast("Skenování selhalo, zkus to znovu.");
      setTimeout(() => setToast(null), 2000);
    }
  };

  useEffect(() => {
    if (!supported) return;
    let cancelled = false;
    (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: "environment" },
        });
        if (cancelled) { stream.getTracks().forEach((t) => t.stop()); return; }
        streamRef.current = stream;
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          await videoRef.current.play();
        }
        const detector = new window.BarcodeDetector({ formats: FORMATS });
        loopRef.current = setInterval(async () => {
          if (!videoRef.current || pending) return;
          try {
            const codes = await detector.detect(videoRef.current);
            if (codes.length > 0) handleCode(codes[0].rawValue);
          } catch { /* ignore transient decode errors */ }
        }, 350);
      } catch (e) {
        setCamError(e?.message || "Kamera nedostupná.");
      }
    })();
    return () => {
      cancelled = true;
      if (loopRef.current) clearInterval(loopRef.current);
      streamRef.current?.getTracks().forEach((t) => t.stop());
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [supported, pending]);

  const confirm = async (body) => {
    setBusy(true);
    try {
      const r = await api.confirmBarcode({ code: pending.code, off_name: pending.off_name, ...body });
      setToast(`✓ Přidáno: ${r.ingredient_name}`);
      setTimeout(() => setToast(null), 2000);
      setPending(null);
    } finally {
      setBusy(false);
    }
  };

  const submitManual = () => {
    const code = manual.trim();
    if (!code) return;
    setManual("");
    handleCode(code);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-ink/40 p-0 sm:items-center sm:p-4" onClick={onClose}>
      <div
        className="flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden rounded-t-2xl bg-white shadow-xl sm:rounded-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-line p-4">
          <h2 className="text-lg font-bold">📷 Skenovat čárový kód</h2>
          <button onClick={onClose} className="text-2xl leading-none text-ink/40 hover:text-ink">×</button>
        </div>

        <div className="flex-1 overflow-auto p-4">
          {supported ? (
            camError ? (
              <p className="mb-4 rounded-lg bg-miss/10 px-3 py-2 text-sm text-miss">
                Kamera nejde spustit ({camError}). Zadej kód ručně níže.
              </p>
            ) : (
              <div className="relative mb-4 overflow-hidden rounded-xl2 bg-black">
                <video ref={videoRef} muted playsInline className="aspect-[4/3] w-full object-cover" />
                <div className="pointer-events-none absolute inset-x-8 top-1/2 h-16 -translate-y-1/2 rounded-lg border-2 border-basil/80" />
              </div>
            )
          ) : (
            <p className="mb-4 rounded-lg bg-paper px-3 py-2 text-sm text-ink/60">
              Tento prohlížeč neumí automatické čtení kódů z kamery. Zadej kód ručně.
            </p>
          )}

          {toast && (
            <p className="mb-3 rounded-lg bg-basil-soft px-3 py-2 text-sm font-medium text-basil-dark">{toast}</p>
          )}

          <div className="mb-4 flex gap-2">
            <input
              value={manual}
              onChange={(e) => setManual(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submitManual()}
              inputMode="numeric"
              placeholder="Zadat kód ručně (EAN)…"
              className="flex-1 rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil"
            />
            <Button variant="ghost" onClick={submitManual}>Přidat</Button>
          </div>

          {pending && (
            <div className="rounded-xl2 border border-line bg-paper p-3">
              <p className="mb-1 text-sm font-medium">
                {pending.off_name || `Neznámý kód: ${pending.code}`}
              </p>
              {pending.brand && <p className="mb-2 text-xs text-ink/50">{pending.brand}</p>}
              {pending.matched ? (
                <div className="flex items-center justify-between gap-2">
                  <p className="text-sm text-have">Návrh: {pending.matched.name}</p>
                  <div className="flex gap-2">
                    <Button variant="ghost" onClick={() => setPending(null)}>Přeskočit</Button>
                    <Button disabled={busy} onClick={() => confirm({ ingredient_id: pending.matched.id })}>Potvrdit</Button>
                  </div>
                </div>
              ) : (
                <div className="space-y-2">
                  <IngredientPicker
                    placeholder="Přiřadit existující surovinu…"
                    onPick={(o) => confirm({ ingredient_id: o.id })}
                  />
                  <div className="flex gap-2">
                    <input
                      ref={newNameRef}
                      defaultValue={pending.off_name || ""}
                      placeholder="…nebo název nové suroviny"
                      className="flex-1 rounded-lg border border-line bg-white px-3 py-2 text-sm outline-none focus:border-basil"
                    />
                    <Button
                      variant="ghost"
                      disabled={busy}
                      onClick={() => confirm({ new_name: newNameRef.current?.value })}
                    >
                      Vytvořit
                    </Button>
                  </div>
                  <button onClick={() => setPending(null)} className="text-xs text-ink/40 hover:text-miss">přeskočit tento kód</button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
