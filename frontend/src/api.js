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
  health: () => fetch("/api/health").then(J),
  searchStatus: () => fetch("/api/search/status").then(J),
  ollamaStatus: () => fetch("/api/search/ollama").then(J),

  crawlStatus: () => fetch("/api/crawl/status").then(J),
  crawlRun: (body) =>
    fetch("/api/crawl/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(J),

  recipes: (filters) => fetch(`/api/recipes${qs(filters)}`).then(J),
  recipe: (id) => fetch(`/api/recipes/${id}`).then(J),
  deleteRecipe: (id) => fetch(`/api/recipes/${id}`, { method: "DELETE" }).then(J),

  ingest: (url) =>
    fetch("/api/search/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }).then(J),
  discover: (query) =>
    fetch("/api/search/discover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    }).then(J),

  ingredients: (q) => fetch(`/api/ingredients${qs({ q, limit: 40 })}`).then(J),

  pantry: () => fetch("/api/pantry").then(J),
  addPantry: (body) =>
    fetch("/api/pantry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(J),
  removePantry: (ingredientId) =>
    fetch(`/api/pantry/${ingredientId}`, { method: "DELETE" }).then(J),

  shopping: () => fetch("/api/shopping").then(J),
  addShopping: (body) =>
    fetch("/api/shopping", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(J),
  shoppingFromRecipe: (id) =>
    fetch(`/api/shopping/from-recipe/${id}`, { method: "POST" }).then(J),
  toggleShopping: (id) =>
    fetch(`/api/shopping/${id}/toggle`, { method: "PATCH" }).then(J),
  removeShopping: (id) =>
    fetch(`/api/shopping/${id}`, { method: "DELETE" }).then(J),

  genStatus: () => fetch("/api/generate/status").then(J),
  genIndex: (rebuild = false) =>
    fetch("/api/generate/index", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rebuild }),
    }).then(J),
  generate: (body) =>
    fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(J),
  saveGenerated: (recipe) =>
    fetch("/api/generate/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recipe }),
    }).then(J),

  matchStatus: () => fetch("/api/maintenance/match-status").then(J),
  backfill: (createMissing = true) =>
    fetch("/api/maintenance/backfill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ create_missing: createMissing }),
    }).then(J),

  sysVersion: () => fetch("/api/system/version").then(J),
  sysCheck: () => fetch("/api/system/check", { method: "POST" }).then(J),
  sysUpdate: () => fetch("/api/system/update", { method: "POST" }).then(J),

  adminSettings: () => fetch("/api/admin/settings").then(J),
  adminSaveSettings: (values) =>
    fetch("/api/admin/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    }).then(J),
  domainsImport: (file) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch("/api/admin/recipe-domains/import", { method: "POST", body: fd }).then(J);
  },
  nutridbImport: (file, merge) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("merge", merge ? "true" : "false");
    return fetch("/api/admin/nutridb/import", { method: "POST", body: fd }).then(J);
  },
  nutridbStatus: () => fetch("/api/admin/nutridb/status").then(J),
  dbImport: (file, mode) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("mode", mode);
    return fetch("/api/admin/db/import", { method: "POST", body: fd }).then(J);
  },
};
