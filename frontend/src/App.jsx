import { useEffect, useState } from "react";
import { getHello, usingStubs } from "./api.js";

// Bare shell: calls the backend /hello route and renders the result. Replace
// this with your own views; keep all backend calls behind api.js.
export default function App() {
  const [state, setState] = useState({ status: "loading" });

  useEffect(() => {
    getHello()
      .then((data) => setState({ status: "ok", data }))
      .catch((err) => setState({ status: "error", error: String(err) }));
  }, []);

  return (
    <div className="app">
      <header className="app-header">
        <h1>Real Estate AI Agent</h1>
        <span className="sub">React + Vite starter</span>
      </header>

      {usingStubs && (
        <div className="stub-banner">
          Stub data — no backend connected. Set <code>VITE_API_BASE</code> to use the live API.
        </div>
      )}

      <main className="panel">
        {state.status === "loading" && <div className="state">Loading…</div>}
        {state.status === "error" && <div className="state error">{state.error}</div>}
        {state.status === "ok" && <p className="mono">{state.data.message}</p>}
      </main>
    </div>
  );
}
