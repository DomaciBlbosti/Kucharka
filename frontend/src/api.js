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
};
