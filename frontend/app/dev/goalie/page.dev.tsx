"use client";
// Dev-only playground (see pageExtensions in next.config.ts): http://localhost:3001/dev/goalie/
// Shows the goalie game on its own and inside the real search card, without a backend or a paid search.
import { useState } from "react";
import type { Snapshot } from "../../../lib/api";
import { GoalieGame } from "../../../components/goalie-game";
import { PlanningProgress } from "../../../components/planning-progress";

const stages = {
  "Brief (preferences)": null,
  "Search (comparing)": { progress_message: "Comparing routes from London…" },
} as const;

function snapshotFor(stage: keyof typeof stages): Snapshot | null {
  const extra = stages[stage];
  if (!extra) return null;
  return { development_mode: true, public_session_id: "playground", origin_city: "London", state_revision: 1, phase: "researching", itinerary: null, preview_trip: null, ...extra } as Snapshot;
}

export default function GoaliePlayground() {
  const [stage, setStage] = useState<keyof typeof stages>("Brief (preferences)");
  const [city, setCity] = useState("Białystok");
  const [key, setKey] = useState(0);
  const resetBest = () => { try { localStorage.removeItem("cpfc-goalie-best"); } catch { /* storage blocked */ } setKey(k => k + 1); };

  return <div className="plan" style={{ display: "grid", gap: 24 }}>
    <div className="notice">Dev playground · not part of the production build</div>
    <h1>Goalie game</h1>
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
      {(Object.keys(stages) as (keyof typeof stages)[]).map(s => <button key={s} type="button" className="tag" aria-pressed={stage === s} onClick={() => setStage(s)}>{s}</button>)}
      <label className="tag">To <input value={city} onChange={e => setCity(e.target.value)} style={{ width: 110, border: 0, background: "transparent", font: "inherit", color: "inherit" }} /></label>
      <button type="button" className="tag" onClick={() => setKey(k => k + 1)}>Restart</button>
      <button type="button" className="tag" onClick={resetBest}>Reset best streak</button>
    </div>

    <section style={{ display: "grid", gap: 8 }}>
      <h2 style={{ margin: 0, fontSize: 16 }}>In the search card</h2>
      <PlanningProgress key={`card-${key}`} snapshot={snapshotFor(stage)} city={city} origin="London" variant="hero" />
    </section>

    <section style={{ display: "grid", gap: 8 }}>
      <h2 style={{ margin: 0, fontSize: 16 }}>Phone width (390px)</h2>
      <div className="card" style={{ width: 390, maxWidth: "100%", padding: 16 }}><GoalieGame key={`phone-${key}`} /></div>
    </section>
  </div>;
}
