import { useRef, useState } from "react";
import { Button } from "./ui";

/** Focení dlouhého dokumentu po úsecích (účtenka, recept…) s možností přeřadit/smazat. */
export function PhotoCapture({ hint, onProcess, processLabel = "Zpracovat" }) {
  const [segments, setSegments] = useState([]); // [{file, url}]
  const camInput = useRef(null);
  const galInput = useRef(null);

  const addFiles = (fileList) => {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    setSegments((cur) => [
      ...cur,
      ...files.map((f) => ({ file: f, url: URL.createObjectURL(f) })),
    ]);
  };
  const removeSeg = (i) => setSegments((cur) => cur.filter((_, j) => j !== i));
  const moveSeg = (i, dir) =>
    setSegments((cur) => {
      const j = i + dir;
      if (j < 0 || j >= cur.length) return cur;
      const next = [...cur];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });

  return (
    <>
      {hint && <p className="mb-3 text-sm text-ink/60">{hint}</p>}

      <input
        ref={camInput}
        type="file"
        accept="image/*"
        capture="environment"
        className="hidden"
        onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }}
      />
      <input
        ref={galInput}
        type="file"
        accept="image/*"
        multiple
        className="hidden"
        onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }}
      />
      <div className="mb-4 flex gap-2">
        <Button onClick={() => camInput.current?.click()}>📷 Vyfotit úsek</Button>
        <Button variant="ghost" onClick={() => galInput.current?.click()}>Nahrát fotky</Button>
      </div>

      {segments.length > 0 && (
        <ul className="mb-4 space-y-2">
          {segments.map((s, i) => (
            <li key={s.url} className="flex items-center gap-3 rounded-lg border border-line/70 p-2">
              <span className="nums flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-basil-soft text-xs font-bold text-basil-dark">
                {i + 1}
              </span>
              <img src={s.url} alt="" className="h-14 w-14 shrink-0 rounded object-cover" />
              <span className="flex-1 truncate text-xs text-ink/50">{s.file.name}</span>
              <div className="flex shrink-0 gap-1">
                <button onClick={() => moveSeg(i, -1)} disabled={i === 0} className="rounded border border-line px-1.5 text-sm disabled:opacity-30">↑</button>
                <button onClick={() => moveSeg(i, 1)} disabled={i === segments.length - 1} className="rounded border border-line px-1.5 text-sm disabled:opacity-30">↓</button>
                <button onClick={() => removeSeg(i)} className="rounded border border-line px-1.5 text-sm text-miss">✕</button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <Button onClick={() => onProcess(segments.map((s) => s.file))} disabled={segments.length === 0}>
        {processLabel} ({segments.length} {segments.length === 1 ? "úsek" : "úseků"})
      </Button>
    </>
  );
}
