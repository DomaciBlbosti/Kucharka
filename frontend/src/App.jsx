import { useEffect, useRef, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { api, auth } from "./api";
import Recipes from "./views/Recipes";
import RecipeDetail from "./views/RecipeDetail";
import Pantry from "./views/Pantry";
import Shopping from "./views/Shopping";
import AddRecipe from "./views/AddRecipe";
import Generate from "./views/Generate";
import Admin from "./views/Admin";
import MealPlan from "./views/MealPlan";
import ShareRecipe from "./views/ShareRecipe";

const CORE_NAV = [
  { to: "/", label: "Recepty", icon: "🍲", end: true },
  { to: "/spiz", label: "Spíž", icon: "🧺" },
  { to: "/plan", label: "Plán", icon: "📅" },
  { to: "/nakup", label: "Nákup", icon: "🛒" },
];

const MORE_NAV = [
  { to: "/vymyslet", label: "Vymyslet", icon: "✨" },
  { to: "/pridat", label: "Přidat", icon: "➕" },
  { to: "/admin", label: "Admin", icon: "⚙️" },
];

function MoreMenu({ direction = "down", variant = "pill" }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const location = useLocation();
  const isActiveGroup = MORE_NAV.some((n) => location.pathname.startsWith(n.to));

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    const onEsc = (e) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);

  const panelPos = direction === "up" ? "bottom-full mb-2" : "top-full mt-2";
  const pillActive = variant === "pill" && isActiveGroup;
  const tabActive = variant === "tab" && isActiveGroup;

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-label="Další"
        aria-expanded={open}
        className={
          variant === "pill"
            ? `rounded-full px-4 py-2 text-sm font-medium transition ${
                pillActive ? "bg-basil text-white" : "text-ink/60 hover:bg-basil-soft hover:text-basil-dark"
              }`
            : `flex flex-col items-center gap-0.5 py-2.5 text-[10px] font-medium ${
                tabActive ? "text-basil-dark" : "text-ink/45"
              }`
        }
      >
        <span className={variant === "tab" ? "text-lg leading-none" : ""} aria-hidden>☰</span>
        {variant === "tab" ? "Menu" : "Více"}
      </button>
      {open && (
        <div
          className={`absolute right-0 z-40 w-48 overflow-hidden rounded-xl2 border border-line bg-white py-1.5 shadow-lg ${panelPos}`}
        >
          {MORE_NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              onClick={() => setOpen(false)}
              className={({ isActive }) =>
                `flex items-center gap-2.5 px-4 py-2.5 text-sm font-medium ${
                  isActive ? "bg-basil-soft text-basil-dark" : "text-ink/70 hover:bg-paper"
                }`
              }
            >
              <span aria-hidden>{n.icon}</span>
              {n.label}
            </NavLink>
          ))}
        </div>
      )}
    </div>
  );
}

function Brand() {
  return (
    <NavLink to="/" className="flex items-baseline gap-2">
      <span className="font-display text-2xl font-extrabold tracking-tight text-basil-dark">
        Kuchařka
      </span>
      <span className="hidden text-xs uppercase tracking-[0.2em] text-ink/40 sm:inline">
        vař z toho, co máš
      </span>
    </NavLink>
  );
}

function Login({ onOk }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState(false);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    setErr(false);
    try {
      const r = await api.login(pw);
      auth.set(r.token);
      onOk();
    } catch {
      setErr(true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm rounded-xl2 border border-line bg-white p-6 shadow-card">
        <h1 className="font-display text-2xl font-extrabold text-basil-dark">Kuchařka</h1>
        <p className="mb-4 mt-1 text-sm text-ink/55">Zadej heslo pro přístup.</p>
        <input
          type="password"
          autoFocus
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          placeholder="Heslo"
          className="w-full rounded-lg border border-line bg-paper px-3 py-2 text-sm outline-none focus:border-basil"
        />
        {err && <p className="mt-2 text-sm text-miss">Špatné heslo.</p>}
        <button
          onClick={submit}
          disabled={busy || !pw}
          className="mt-4 w-full rounded-full bg-basil px-4 py-2 text-sm font-semibold text-white hover:bg-basil-dark disabled:opacity-50"
        >
          {busy ? "Přihlašuji…" : "Přihlásit"}
        </button>
      </div>
    </div>
  );
}

export default function App() {
  const [gate, setGate] = useState({ loading: true, ok: false });

  const check = () =>
    api
      .authStatus()
      .then((s) => setGate({ loading: false, ok: !s.required || s.authenticated }))
      .catch(() => setGate({ loading: false, ok: true }));

  useEffect(() => {
    check();
    const onUnauth = () => setGate({ loading: false, ok: false });
    window.addEventListener("kucharka-unauth", onUnauth);
    return () => window.removeEventListener("kucharka-unauth", onUnauth);
  }, []);

  if (gate.loading) return null;
  if (!gate.ok) return <Login onOk={() => check()} />;

  return (
    <div className="min-h-screen pb-20 md:pb-0">
      {/* Horní lišta */}
      <header className="sticky top-0 z-30 border-b border-line bg-paper/85 backdrop-blur">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <Brand />
          <nav className="hidden items-center gap-1 md:flex">
            {CORE_NAV.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.end}
                className={({ isActive }) =>
                  `rounded-full px-4 py-2 text-sm font-medium transition ${
                    isActive
                      ? "bg-basil text-white"
                      : "text-ink/60 hover:bg-basil-soft hover:text-basil-dark"
                  }`
                }
              >
                {n.label}
              </NavLink>
            ))}
            <MoreMenu direction="down" variant="pill" />
          </nav>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-4 py-6">
        <Routes>
          <Route path="/" element={<Recipes />} />
          <Route path="/vymyslet" element={<Generate />} />
          <Route path="/recept/:id" element={<RecipeDetail />} />
          <Route path="/spiz" element={<Pantry />} />
          <Route path="/plan" element={<MealPlan />} />
          <Route path="/sdilet" element={<ShareRecipe />} />
          <Route path="/nakup" element={<Shopping />} />
          <Route path="/pridat" element={<AddRecipe />} />
          <Route path="/admin" element={<Admin />} />
        </Routes>
      </main>

      {/* Spodní taby na mobilu */}
      <nav className="fixed inset-x-0 bottom-0 z-30 border-t border-line bg-paper/95 backdrop-blur md:hidden">
        <div className="mx-auto grid max-w-5xl grid-cols-5">
          {CORE_NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                `flex flex-col items-center gap-0.5 py-2.5 text-[10px] font-medium ${
                  isActive ? "text-basil-dark" : "text-ink/45"
                }`
              }
            >
              <span className="text-lg leading-none" aria-hidden>
                {n.icon}
              </span>
              {n.label}
            </NavLink>
          ))}
          <MoreMenu direction="up" variant="tab" />
        </div>
      </nav>
    </div>
  );
}
