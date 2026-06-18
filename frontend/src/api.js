const TOKEN_KEY = "kucharka_token";
export const auth = {
  get: () => localStorage.getItem(TOKEN_KEY) || "",
  set: (t) => localStorage.setItem(TOKEN_KEY, t || ""),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

// fetch s tokenem; při 401 token zahodí a oznámí appce (login gate)
const afetch = (url, opts = {}) => {
  const t = auth.get();
  const headers = { ...(opts.headers || {}), ...(t ? { Authorization: `Bearer ${t}` } : {}) };
  return window.fetch(url, { ...opts, headers }).then((r) => {
    if (r.status === 401) {
      auth.clear();
      window.dispatchEvent(new Event("kucharka-unauth"));
    }
    return r;
  });
};

// pro <a href> stahování (export) – přidá token do query
export const withToken = (url) => {
  const t = auth.get();
  return t ? `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(t)}` : url;
};

const J = (r) => {
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.status === 204 ? null : r.json();
};

const qs = (params) => {
  const u = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") u.set(k, v);
  });
  const s = u.toString();
  return s ? `?${s}` : "";
};

export const api = {
  health: () => afetch("/api/health").then(J),
  searchStatus: () => afetch("/api/search/status").then(J),
  ollamaStatus: () => afetch("/api/search/ollama").then(J),

  crawlStatus: () => afetch("/api/crawl/status").then(J),
  crawlRun: (body) =>
    afetch("/api/crawl/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(J),

  recipes: (filters) => afetch(`/api/recipes${qs(filters)}`).then(J),
  recipe: (id) => afetch(`/api/recipes/${id}`).then(J),
  cookFrom: (ids) =>
    afetch(`/api/recipes/cook-from?${ids.map((i) => `ingredient_ids=${i}`).join("&")}`).then(J),

  mealplan: (start, days = 7) =>
    afetch(`/api/mealplan?start=${start}&days=${days}`).then(J),
  addMeal: (body) =>
    afetch("/api/mealplan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(J),
  updateMeal: (id, body) =>
    afetch(`/api/mealplan/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(J),
  removeMeal: (id) =>
    afetch(`/api/mealplan/${id}`, { method: "DELETE" }).then(J),
  mealplanShopping: (start, days = 7) =>
    afetch("/api/mealplan/shopping", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start, days }),
    }).then(J),
  suggestPlan: (body) =>
    afetch("/api/mealplan/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(J),
  suggestStatus: () => afetch("/api/mealplan/suggest-status").then(J),
  applyPlan: (body) =>
    afetch("/api/mealplan/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(J),
  deleteRecipe: (id) => afetch(`/api/recipes/${id}`, { method: "DELETE" }).then(J),

  ingest: (url) =>
    afetch("/api/search/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }).then(J),
  discover: (query) =>
    afetch("/api/search/discover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    }).then(J),

  ingredients: (q) => afetch(`/api/ingredients${qs({ q, limit: 40 })}`).then(J),

  pantry: () => afetch("/api/pantry").then(J),
  addPantry: (body) =>
    afetch("/api/pantry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(J),
  removePantry: (ingredientId) =>
    afetch(`/api/pantry/${ingredientId}`, { method: "DELETE" }).then(J),

  shopping: () => afetch("/api/shopping").then(J),
  addShopping: (body) =>
    afetch("/api/shopping", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(J),
  shoppingFromRecipe: (id) =>
    afetch(`/api/shopping/from-recipe/${id}`, { method: "POST" }).then(J),
  toggleShopping: (id) =>
    afetch(`/api/shopping/${id}/toggle`, { method: "PATCH" }).then(J),
  removeShopping: (id) =>
    afetch(`/api/shopping/${id}`, { method: "DELETE" }).then(J),

  genStatus: () => afetch("/api/generate/status").then(J),
  genIndex: (rebuild = false) =>
    afetch("/api/generate/index", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rebuild }),
    }).then(J),
  generate: (body) =>
    afetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(J),
  saveGenerated: (recipe) =>
    afetch("/api/generate/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recipe }),
    }).then(J),

  matchStatus: () => afetch("/api/maintenance/match-status").then(J),
  backfill: (createMissing = true) =>
    afetch("/api/maintenance/backfill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ create_missing: createMissing }),
    }).then(J),

  sysVersion: () => afetch("/api/system/version").then(J),
  sysCheck: () => afetch("/api/system/check", { method: "POST" }).then(J),
  sysUpdate: () => afetch("/api/system/update", { method: "POST" }).then(J),

  adminSettings: () => afetch("/api/admin/settings").then(J),
  testOllama: () => afetch("/api/admin/test-ollama").then(J),
  translateStatus: () => afetch("/api/maintenance/translate-status").then(J),
  runTranslate: () =>
    afetch("/api/maintenance/translate", { method: "POST" }).then(J),
  categorizeStatus: () => afetch("/api/maintenance/categorize-status").then(J),
  runCategorize: () =>
    afetch("/api/maintenance/categorize", { method: "POST" }).then(J),
  ingredientCategories: () => afetch("/api/ingredients/categories").then(J),
  adminSaveSettings: (values) =>
    afetch("/api/admin/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    }).then(J),
  domainsImport: (file) => {
    const fd = new FormData();
    fd.append("file", file);
    return afetch("/api/admin/recipe-domains/import", { method: "POST", body: fd }).then(J);
  },
  nutridbImport: (file, merge) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("merge", merge ? "true" : "false");
    return afetch("/api/admin/nutridb/import", { method: "POST", body: fd }).then(J);
  },
  nutridbStatus: () => afetch("/api/admin/nutridb/status").then(J),
  dbImport: (file, mode) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("mode", mode);
    return afetch("/api/admin/db/import", { method: "POST", body: fd }).then(J);
  },

  authStatus: () => afetch("/api/auth/status").then(J),
  login: (password) =>
    afetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    }).then(J),
  setPassword: (password) =>
    afetch("/api/admin/password", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    }).then(J),
};
