// In production (Railway) set VITE_API_URL to the backend service URL.
// In local dev the Vite proxy handles routing so BASE stays empty.
const BASE = import.meta.env.VITE_API_URL || "";

async function req(method, path, body) {
  const res = await fetch(BASE + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status}`);
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  // Health
  health: () => req("GET", "/health"),
  authStatus: () => req("GET", "/auth/status"),

  // Stats
  stats: () => req("GET", "/api/logs/stats"),

  // Logs
  logs: (limit = 50, type = "") =>
    req("GET", `/api/logs?limit=${limit}${type ? `&action_type=${type}` : ""}`),

  // Rules
  listRules: () => req("GET", "/api/rules"),
  createRule: (body) => req("POST", "/api/rules", body),
  updateRule: (id, body) => req("PUT", `/api/rules/${id}`, body),
  deleteRule: (id) => req("DELETE", `/api/rules/${id}`),

  // Settings
  getSettings: () => req("GET", "/api/settings"),
  setSetting: (key, value) => req("PUT", `/api/settings/${key}`, { value }),
};
