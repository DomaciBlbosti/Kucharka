// Sdílené UI prvky.

// Podpisový prvek: měřič dostupnosti surovin (kolik z receptu mám doma).
export function CookMeter({ have, total, size = "md" }) {
  if (!total) {
    return (
      <span className="text-xs text-ink/40">suroviny nenapárované</span>
    );
  }
  const missing = total - have;
  const ready = missing === 0;
  const segGap = size === "sm" ? "gap-[3px]" : "gap-1";
  const segH = size === "sm" ? "h-1.5" : "h-2";
  return (
    <div className="flex items-center gap-2">
      <div className={`flex ${segGap} flex-1`}>
        {Array.from({ length: total }).map((_, i) => (
          <span
            key={i}
            className={`${segH} flex-1 rounded-full transition-colors ${
              i < have ? "bg-have" : "bg-miss/25"
            }`}
          />
        ))}
      </div>
      <span
        className={`nums text-xs font-semibold tabular-nums ${
          ready ? "text-have" : "text-ink/60"
        }`}
      >
        {have}/{total}
      </span>
    </div>
  );
}

export function ReadyStamp({ missing }) {
  if (missing > 0) return null;
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-basil px-2.5 py-1 text-[11px] font-semibold text-white">
      Můžeš vařit
    </span>
  );
}

export function Stars({ rating, count }) {
  if (!rating) return null;
  return (
    <span className="inline-flex items-center gap-1 text-sm text-ink/70">
      <span className="text-miss">★</span>
      <span className="nums font-medium">{rating.toFixed(1)}</span>
      {count ? <span className="text-ink/40 text-xs">({count})</span> : null}
    </span>
  );
}

export function Meta({ icon, children }) {
  if (!children) return null;
  return (
    <span className="inline-flex items-center gap-1 text-sm text-ink/60">
      <span aria-hidden>{icon}</span>
      {children}
    </span>
  );
}

export function Spinner({ label = "Načítám…" }) {
  return (
    <div className="flex items-center justify-center gap-2 py-16 text-ink/50">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-basil border-t-transparent" />
      {label}
    </div>
  );
}

export function EmptyState({ title, children }) {
  return (
    <div className="rounded-xl2 border border-dashed border-line bg-white/50 px-6 py-12 text-center">
      <h3 className="mb-1 text-lg">{title}</h3>
      <p className="mx-auto max-w-md text-sm text-ink/60">{children}</p>
    </div>
  );
}

export function Button({ variant = "primary", className = "", ...props }) {
  const base =
    "inline-flex items-center justify-center gap-1.5 rounded-full px-4 py-2 text-sm font-semibold transition active:scale-[0.98] disabled:opacity-50 disabled:pointer-events-none";
  const styles = {
    primary: "bg-basil text-white hover:bg-basil-dark shadow-sm",
    ghost: "bg-basil-soft text-basil-dark hover:bg-basil/15",
    quiet: "text-ink/60 hover:text-ink hover:bg-line/60",
    danger: "text-miss hover:bg-miss/10",
  };
  return <button className={`${base} ${styles[variant]} ${className}`} {...props} />;
}
