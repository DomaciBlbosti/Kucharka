import { NavLink, Route, Routes } from "react-router-dom";
import Recipes from "./views/Recipes";
import RecipeDetail from "./views/RecipeDetail";
import Pantry from "./views/Pantry";
import Shopping from "./views/Shopping";
import AddRecipe from "./views/AddRecipe";

const NAV = [
  { to: "/", label: "Recepty", icon: "🍲", end: true },
  { to: "/spiz", label: "Spíž", icon: "🧺" },
  { to: "/nakup", label: "Nákup", icon: "🛒" },
  { to: "/pridat", label: "Přidat", icon: "➕" },
];

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

export default function App() {
  return (
    <div className="min-h-screen pb-20 md:pb-0">
      {/* Horní lišta */}
      <header className="sticky top-0 z-30 border-b border-line bg-paper/85 backdrop-blur">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <Brand />
          <nav className="hidden gap-1 md:flex">
            {NAV.map((n) => (
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
          </nav>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-4 py-6">
        <Routes>
          <Route path="/" element={<Recipes />} />
          <Route path="/recept/:id" element={<RecipeDetail />} />
          <Route path="/spiz" element={<Pantry />} />
          <Route path="/nakup" element={<Shopping />} />
          <Route path="/pridat" element={<AddRecipe />} />
        </Routes>
      </main>

      {/* Spodní taby na mobilu */}
      <nav className="fixed inset-x-0 bottom-0 z-30 border-t border-line bg-paper/95 backdrop-blur md:hidden">
        <div className="mx-auto grid max-w-5xl grid-cols-4">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) =>
                `flex flex-col items-center gap-0.5 py-2.5 text-[11px] font-medium ${
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
        </div>
      </nav>
    </div>
  );
}
