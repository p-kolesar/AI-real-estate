// Single seam between the UI and the backend. Every view should call functions
// defined here and nothing else, so swapping stub data for the live API is a
// one-file change. When VITE_API_BASE is unset the app runs on stub data so it
// works with no backend; set VITE_API_BASE=/api (or the Function App URL) to
// hit the real API. See backend/function_app.py for the served routes.

const API_BASE = import.meta.env.VITE_API_BASE ?? "";
const USE_STUBS = !API_BASE;

async function get(path) {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`${path} -> ${res.status} ${res.statusText}`);
  return res.json();
}

// ---- Health: { status: "ok" }
export async function getHealth() {
  return USE_STUBS ? { status: "stub" } : get("/health");
}

// ---- Hello: { message: "Hello, World!" }
export async function getHello(name) {
  const q = name ? `?name=${encodeURIComponent(name)}` : "";
  return USE_STUBS ? { message: "Hello, World! (stub)" } : get(`/hello${q}`);
}

export const usingStubs = USE_STUBS;
