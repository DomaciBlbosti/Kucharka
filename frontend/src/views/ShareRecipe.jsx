import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Button, Spinner } from "../components/ui";

// URL adresu ze share sheetu různé appky posílají různě – buď v ?url=,
// nebo schovanou uvnitř ?text=/?title= (typicky Instagram/sdílení z appek
// bez vlastního "url" pole). Zkusíme ji vytáhnout odkudkoli.
function extractUrl(params) {
  const direct = params.get("url");
  if (direct) return direct.trim();
  const blob = `${params.get("text") || ""} ${params.get("title") || ""}`;
  const match = blob.match(/https?:\/\/\S+/);
  return match ? match[0] : "";
}

export default function ShareRecipe() {
  const [params] = useSearchParams();
  const nav = useNavigate();
  const [status, setStatus] = useState("loading"); // loading | error | notfound
  const [error, setError] = useState(null);
  const [url, setUrl] = useState(() => extractUrl(params));

  const run = async (target) => {
    if (!target) {
      setStatus("error");
      setError("Ve sdílené položce nebyla nalezena žádná URL adresa.");
      return;
    }
    setStatus("loading");
    setError(null);
    try {
      const recipe = await api.ingest(target);
      if (recipe) {
        nav(`/recept/${recipe.id}`, { replace: true });
      } else {
        setStatus("notfound");
      }
    } catch (e) {
      setStatus("error");
      setError(e?.message || "Stažení receptu selhalo.");
    }
  };

  useEffect(() => {
    run(url);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="flex min-h-[70vh] flex-col items-center justify-center px-4 text-center">
      {status === "loading" && (
        <>
          <Spinner label="Stahuji sdílený recept…" />
          {url && <p className="mt-3 max-w-sm truncate text-xs text-ink/40">{url}</p>}
        </>
      )}
      {status === "notfound" && (
        <div className="max-w-sm">
          <p className="mb-4 text-sm text-ink/60">
            Ze stránky se nepodařilo vytáhnout recept (nerozpoznaný formát).
          </p>
          <Link to="/pridat"><Button variant="ghost">Zkusit ručně v Přidat</Button></Link>
        </div>
      )}
      {status === "error" && (
        <div className="w-full max-w-sm">
          <p className="mb-3 text-sm text-miss">{error}</p>
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://…"
            className="mb-3 w-full rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil"
          />
          <div className="flex justify-center gap-2">
            <Button onClick={() => run(url)}>Zkusit znovu</Button>
            <Link to="/"><Button variant="ghost">Zpět na recepty</Button></Link>
          </div>
        </div>
      )}
    </div>
  );
}
