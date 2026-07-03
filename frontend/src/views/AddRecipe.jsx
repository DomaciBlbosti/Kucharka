import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { RecipeFromPhoto } from "../components/RecipeFromPhoto";
import { Button, Spinner } from "../components/ui";

export default function AddRecipe() {
  const nav = useNavigate();
  const [url, setUrl] = useState("");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [candidates, setCandidates] = useState(null);
  const [status, setStatus] = useState({ searxng: false, ollama: false });
  const [ollama, setOllama] = useState(null);
  const [photoOpen, setPhotoOpen] = useState(false);

  useEffect(() => {
    api.searchStatus().then(setStatus).catch(() => {});
    api.ollamaStatus().then(setOllama).catch(() => {});
  }, []);

  const ingest = async (target) => {
    setBusy(true);
    setError("");
    try {
      const r = await api.ingest(target);
      if (!r) {
        setError("Recept se nepodařilo vyparsovat. Zkus jinou URL.");
      } else {
        nav(`/recept/${r.id}`);
      }
    } catch {
      setError("Stažení selhalo. Zkontroluj URL a připojení.");
    } finally {
      setBusy(false);
    }
  };

  const discover = async () => {
    setBusy(true);
    setError("");
    setCandidates(null);
    try {
      const res = await api.discover(query);
      setCandidates(res.results || []);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="max-w-xl">
      <h1 className="mb-1 text-2xl font-extrabold">Přidat recept</h1>
      <p className="mb-4 text-sm text-ink/60">
        Vlož odkaz na recept, nebo ho nech vyhledat. Suroviny se napárují a
        kalorie dopočítají automaticky.
      </p>

      <div className="mb-6 flex flex-wrap gap-2 text-xs">
        <StatusPill
          ok={ollama?.reachable && ollama?.model_ok}
          warn={ollama?.reachable && !ollama?.model_ok}
          label={
            !ollama
              ? "Ollama…"
              : !ollama.enabled
              ? "Ollama vypnutá (heuristika)"
              : !ollama.reachable
              ? "Ollama nedostupná"
              : !ollama.model_ok
              ? `Ollama: model ${ollama.model} chybí`
              : `Ollama: ${ollama.model}`
          }
        />
        <StatusPill ok={status.searxng} label={status.searxng ? "Vyhledávání zapnuté" : "Vyhledávání vypnuté"} />
      </div>

      {/* Vložení URL */}
      <section className="mb-8 rounded-xl2 border border-line bg-white p-5 shadow-card">
        <h2 className="mb-3 text-lg font-bold">Z odkazu</h2>
        <div className="flex flex-col gap-2 sm:flex-row">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://…"
            className="flex-1 rounded-full border border-line bg-paper px-4 py-2.5 text-sm outline-none focus:border-basil focus:ring-2 focus:ring-basil/20"
          />
          <Button onClick={() => ingest(url)} disabled={!url || busy}>
            Načíst
          </Button>
        </div>
      </section>

      {/* Vyhledání */}
      <section className="mb-6 rounded-xl2 border border-line bg-white p-5 shadow-card">
        <h2 className="mb-1 text-lg font-bold">Vyhledat na webu</h2>
        {!status.searxng ? (
          <p className="text-sm text-ink/50">
            Vyhledávání je vypnuté. Nastav <code className="rounded bg-line/60 px-1">SEARXNG_URL</code>{" "}
            v konfiguraci appky a zapne se.
          </p>
        ) : (
          <>
            <div className="mb-3 flex flex-col gap-2 sm:flex-row">
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && query && discover()}
                placeholder="např. svíčková, kuřecí kari…"
                className="flex-1 rounded-full border border-line bg-paper px-4 py-2.5 text-sm outline-none focus:border-basil focus:ring-2 focus:ring-basil/20"
              />
              <Button variant="ghost" onClick={discover} disabled={!query || busy}>
                Hledat
              </Button>
            </div>
            {candidates && candidates.length === 0 && (
              <p className="text-sm text-ink/50">Nic nenalezeno.</p>
            )}
            {candidates && candidates.length > 0 && (
              <ul className="divide-y divide-line">
                {candidates.map((c) => (
                  <li key={c.url} className="flex items-center gap-3 py-2.5">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium">{c.title}</p>
                      <p className="truncate text-xs text-ink/40">{c.domain}</p>
                    </div>
                    <Button variant="ghost" onClick={() => ingest(c.url)} disabled={busy}>
                      Přidat
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>

      {/* Z fotky */}
      <section className="rounded-xl2 border border-line bg-white p-5 shadow-card">
        <h2 className="mb-1 text-lg font-bold">Recept z papíru nebo rukou psaný</h2>
        <p className="mb-3 text-sm text-ink/60">
          Vyfoť babiččin sešit nebo vystřižený recept — přečtu ho a připravím k uložení.
        </p>
        <Button variant="ghost" onClick={() => setPhotoOpen(true)}>📷 Vyfotit recept</Button>
      </section>
      {photoOpen && <RecipeFromPhoto onClose={() => setPhotoOpen(false)} />}

      {busy && <Spinner label="Pracuju…" />}
      {error && (
        <p className="mt-4 rounded-lg bg-miss/10 px-4 py-3 text-sm text-miss">{error}</p>
      )}
    </div>
  );
}

function StatusPill({ ok, warn, label }) {
  const color = ok
    ? "bg-have/10 text-have"
    : warn
    ? "bg-miss/10 text-miss"
    : "bg-line/60 text-ink/50";
  const dot = ok ? "bg-have" : warn ? "bg-miss" : "bg-ink/30";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 font-medium ${color}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {label}
    </span>
  );
}
