"use client";
import { useEffect, useState } from "react";
import type { Snapshot } from "../lib/api";
import { type Stage, stageFor, tickerLines } from "../lib/planning";
import { GoalieGame } from "./goalie-game";
import { Icon } from "./icons";

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return reduced;
}

// Rotates only while active; pauses when the tab is hidden so no timer runs unseen.
function useTicker(active: boolean, count: number): number {
  const [index, setIndex] = useState(0);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => { if (!document.hidden) setIndex(n => (n + 1) % count); }, 3500);
    return () => clearInterval(timer);
  }, [active, count]);
  return active ? index : 0;
}

// Decorative, phase-anchored progress. The .status live region in PlanExperience
// announces actual phase changes without repeating the rotating copy visually.
export function PlanningProgress({ snapshot, city, origin = "London", variant = "hero" }: {
  snapshot: Snapshot | null;
  city?: string;
  origin?: string;
  variant?: "hero" | "preview";
}) {
  const reduced = useReducedMotion();
  const stage: Stage = stageFor(snapshot);
  const preview = snapshot?.preview_trip;
  const lines = variant === "hero" ? tickerLines(stage, city, origin)
    : !preview?.stay ? [city ? `Checking places to stay in ${city}…` : "Checking places to stay…", "Comparing rooms and prices…"]
    : !preview.journey ? ["Checking return travel…", "Comparing journey times…"]
    : ["Comparing prices and journey times…", "Checking local transfers…", "Shortlisting your options…"];
  const index = useTicker((stage === "preferences" || stage === "searching") && !reduced, lines.length);
  const rotatingText = lines[index % lines.length];

  if (variant === "preview") {
    return <p className="ticker inline" aria-hidden="true"><span key={reduced ? "static" : index} className={reduced ? undefined : "tfade"}>{rotatingText}</span></p>;
  }

  const destination = city ?? "the away end";
  const stages = ["Brief", "Search", "Ready"] as const;
  const reached = stage === "done" ? 2 : stage === "searching" ? 1 : 0;

  return <div className={`plan-progress card ${stage}`}>
    <div className="journey-route" aria-hidden="true">
      <div className="journey-city"><small>From</small><strong>{origin}</strong></div>
      <div className="journey-arc"><svg viewBox="0 0 320 90" preserveAspectRatio="none" focusable="false"><path d="M 10 72 Q 160 -35 310 72" /></svg></div>
      <div className="journey-city destination"><small>To</small><strong>{destination}</strong></div>
    </div>
    <GoalieGame />
    <p className="ticker hero" aria-hidden="true"><span key={reduced ? "static" : `${stage}-${index}`} className={reduced ? undefined : "tfade"}>{rotatingText}</span></p>
    <ol className="stages" aria-hidden="true">{stages.map((name, i) => <li key={name} className={i < reached || stage === "done" ? "done" : i === reached ? "active" : ""}>{i < reached || stage === "done" ? <Icon name="check" size={12} /> : <span className="dot" />}{name}</li>)}</ol>
  </div>;
}
