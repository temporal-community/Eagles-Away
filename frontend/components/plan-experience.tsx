"use client";
import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { Button } from "@ui-sdk/react/button";
import { Collapsible, CollapsiblePanel, CollapsibleTrigger } from "@ui-sdk/react/collapsible";
import { ApiError, Snapshot, newer, request, terminal } from "../lib/api";
import { EarlierOptions, ItineraryView, JourneyPreview } from "./itinerary-view";
import { PlanningProgress } from "./planning-progress";
import { trackAnalytics } from "../lib/analytics";
import { readBrowserBrief } from "../lib/browser-brief";
import { markPlanMilestone } from "../lib/plan-timing";
import { type RecoveryAction, recoveryBrief, recoveryTitle } from "../lib/recovery";
import { day } from "../lib/trip-format";
import { usePlanSubmission } from "../lib/use-plan-submission";
import { Icon } from "./icons";

export function PlanExperience() {
  const params = useSearchParams();
  const session = params.get("session");
  if (!session) return <div className="plan card plain"><h1>No session selected</h1><Link className="text-link" href="/">Start a trip brief<Icon name="arrowRight" size={16} /></Link></div>;
  return <PlanSession key={session} session={session} />;
}

function PlanSession({ session }: { session: string }) {
  const router = useRouter();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const current = useRef<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [recoveryError, setRecoveryError] = useState("");
  const { startPlan, busy: startingRecovery } = usePlanSubmission(router.push, setRecoveryError);
  const [chosen, setChosen] = useState<RecoveryAction | null>(null);
  const [unavailable, setUnavailable] = useState("");
  const [saved] = useState(() => readBrowserBrief(session));
  const [city] = useState<string | undefined>(() => {
    if (saved?.city) return saved.city;
    try { return sessionStorage.getItem(`cpfc-city-${session}`) ?? undefined; } catch { return undefined; }
  });
  const [showHero, setShowHero] = useState(false);
  const outcomeTracked = useRef(false);
  useEffect(() => {
    const started = performance.now();
    let stopped = false, inFlight = false, failures = 0;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      if (stopped || inFlight || document.hidden || terminal(current.current)) return;
      clearTimeout(timer); inFlight = true;
      try {
        const revision = current.current?.state_revision;
        const data = await request<Snapshot>(`/api/sessions/${session}/snapshot${revision === undefined ? "" : `?after_revision=${revision}`}`);
        if (stopped) return;
        current.current = newer(current.current, data); setSnapshot(current.current); setError(""); failures = 0;
      } catch (e) {
        if (stopped) return;
        if (e instanceof ApiError && e.status === 404) {
          stopped = true;
          setUnavailable(e.message || "This session is unavailable in this browser.");
          return;
        }
        failures++;
        setError("Reconnecting…");
      } finally {
        inFlight = false;
        if (!stopped && !terminal(current.current)) {
          const interval = performance.now() - started < 30000 ? 1000 : 2000;
          timer = setTimeout(poll, failures ? Math.min(30000, 2000 * 2 ** failures) : interval + Math.random() * 200);
        }
      }
    }
    const resume = () => { void poll(); };
    document.addEventListener("visibilitychange", resume); window.addEventListener("online", resume);
    void poll();
    return () => { stopped = true; clearTimeout(timer); document.removeEventListener("visibilitychange", resume); window.removeEventListener("online", resume); };
  }, [session]);
  // Anti-flash gate: never show the elaborate stepper for instant/recorded results.
  useEffect(() => { const t = setTimeout(() => setShowHero(true), 400); return () => clearTimeout(t); }, []);
  useEffect(() => {
    if (outcomeTracked.current || !snapshot) return;
    if (snapshot.itinerary) { trackAnalytics("plan_ready"); outcomeTracked.current = true; }
    else if (snapshot.phase === "failed") {
      trackAnalytics("plan_failed");
      if (snapshot.recovery_actions.length) trackAnalytics("plan_recovery_offered");
      outcomeTracked.current = true;
    }
  }, [snapshot]);
  useEffect(() => {
    if (unavailable || !snapshot) return;
    const frame = requestAnimationFrame(() => {
      const trips = snapshot.itinerary?.trips ?? (snapshot.preview_trip ? [snapshot.preview_trip] : []);
      if (trips.some(trip => trip.journey || trip.stay)) markPlanMilestone(session, "first-option");
      if (trips.some(trip => trip.journey && trip.stay)) markPlanMilestone(session, "matching-trip");
      if (snapshot.itinerary) markPlanMilestone(session, "final");
      if (!snapshot.itinerary && snapshot.failure_reason) markPlanMilestone(session, "explained");
      if (snapshot.phase === "failed" && snapshot.recovery_actions.length) markPlanMilestone(session, "next-step");
    });
    return () => cancelAnimationFrame(frame);
  }, [session, snapshot, unavailable]);

  const editHref = saved ? `/?edit=${encodeURIComponent(session)}` : "/";
  const origin = snapshot?.origin_city ?? saved?.brief.origin_city ?? "your city";
  const incomplete = !unavailable && terminal(snapshot) && !snapshot?.itinerary;
  const reason = !unavailable && !snapshot?.itinerary ? snapshot?.failure_reason : null;
  const actions = incomplete && snapshot?.phase === "failed" ? snapshot.recovery_actions : [];
  const departure = snapshot?.departure_city ?? saved?.brief.departure_city ?? saved?.brief.origin_city ?? "your departure airport";
  const recoveryLinks = incomplete ? snapshot?.recovery_links ?? [] : [];
  const alternateOrigin = !unavailable ? snapshot?.alternate_origin_city : null;
  const hasAlternateJourney = Boolean(alternateOrigin && (snapshot?.preview_trip?.journey || snapshot?.itinerary));
  const checkingAlternate = !unavailable && !reason && !hasAlternateJourney && !terminal(snapshot) ? alternateOrigin ?? saved?.brief.departure_city : null;
  const status = unavailable ? unavailable
    : checkingAlternate ? `Checking flights from ${checkingAlternate}…`
    : snapshot?.progress_message ?? "Connecting to your planning session…";
  const activeTrips = snapshot?.itinerary?.trips ?? (snapshot?.preview_trip ? [snapshot.preview_trip] : []);
  const earlier = (snapshot?.preview_options ?? []).filter(option => (option.journey || option.stay) && !activeTrips.some(trip =>
    option.fixture.id === trip.fixture.id
    && (!option.journey || option.journey.id === trip.journey?.id)
    && (!option.stay || option.stay.id === trip.stay?.id)
  ));
  return <div className="plan">
    {snapshot?.development_mode && <div className="notice">Development preview · Synthetic trips and prices</div>}
    <div className="plan-heading">
      <h1>Your away day</h1>
      <span className="tag"><Icon name="pin" size={14} />From {origin}</span>
    </div>
    <div className={!reason && (terminal(snapshot) || unavailable || checkingAlternate) ? "status" : "status sr-only"} role="status" aria-live="polite">{status}</div>
    {!unavailable && error && <p className="error" role="alert"><Icon name="alert" />{error}</p>}
    {unavailable && <p className="end-note"><Link className="text-link" href={editHref}>{saved ? "Edit trip brief" : "Start a trip brief"}<Icon name="arrowRight" size={16} /></Link></p>}
    {!unavailable && !reason && !snapshot?.itinerary && !snapshot?.preview_trip && !terminal(snapshot) && (showHero
      ? <PlanningProgress snapshot={snapshot} city={city} origin={checkingAlternate ?? alternateOrigin ?? origin} variant="hero" />
      : <div className="plan-connecting" aria-hidden="true"><span className="plan-connecting-mark" />Preparing your route…</div>)}
    {reason && snapshot && <section className={terminal(snapshot) ? "card recovery" : "card recovery checking"} aria-labelledby="recovery-title">
      <h2 id="recovery-title">{recoveryTitle(reason, departure)}</h2>
      <p>{!terminal(snapshot) && <span className="plan-connecting-mark" aria-hidden="true" />}{snapshot.progress_message}</p>
      {snapshot.searched_dates.length > 0 && <p className="hint"><Icon name="calendar" />Dates checked: {snapshot.searched_dates.map(pair => `${day(pair.outbound_date)} → ${day(pair.return_date)}`).join(" · ")}</p>}
      {recoveryError && <p className="error" role="alert"><Icon name="alert" />{recoveryError}</p>}
      {actions.length > 0 && <ul className="recovery-actions">
        {actions.map((action, index) => <li key={`${action.kind}-${action.departure_city ?? ""}`}>
          {saved && <Button className={index === 0 ? "primary" : "secondary"} disabled={startingRecovery} onClick={() => {
            setChosen(action);
            trackAnalytics("plan_recovery_started");
            void startPlan(recoveryBrief(saved.brief, action), saved.city);
          }}>
            {startingRecovery && chosen === action ? "Opening…" : action.label}
          </Button>}
          <small>{action.detail}</small>
        </li>)}
      </ul>}
      {terminal(snapshot) && <Link className="text-link" href={editHref}>{saved ? "Edit trip brief" : "Try another trip brief"}<Icon name="arrowRight" size={16} /></Link>}
    </section>}
    {hasAlternateJourney && <p className="notice">Flights depart from {alternateOrigin}. Arrange travel between {origin} and the departure airport separately.</p>}
    <div className={incomplete ? "recovery-details" : undefined}>
      {!unavailable && snapshot?.preview_trip && !snapshot.itinerary && <JourneyPreview trip={snapshot.preview_trip} ended={terminal(snapshot)}>{!reason && !terminal(snapshot) && <PlanningProgress snapshot={snapshot} city={snapshot.preview_trip.fixture.city} origin={origin} variant="preview" />}</JourneyPreview>}
      {!unavailable && snapshot?.itinerary && <ItineraryView itinerary={snapshot.itinerary} />}
      {!unavailable && earlier.length > 0 && <EarlierOptions trips={earlier} />}
      {recoveryLinks.length > 0 && <section className="trip card" aria-label="Provider searches">
        <Collapsible className="disclosure">
          <CollapsibleTrigger><Icon name="calendar" />Search providers directly<Icon name="chevron" className="icon chevron" /></CollapsibleTrigger>
          <CollapsiblePanel>
            <p className="hint">Confirm dates, passengers and room requirements before booking.</p>
            <ul className="rows">
              {recoveryLinks.map(({ label, url }) => <li key={url}>
                <a href={url} target="_blank" rel="noopener noreferrer"><Icon name="calendar" /><strong>{label}</strong><Icon name="external" size={14} /></a>
              </li>)}
            </ul>
          </CollapsiblePanel>
        </Collapsible>
      </section>}
    </div>
    {incomplete && !reason && <p className="end-note"><Link className="text-link" href={editHref}>{saved ? "Edit trip brief" : "Try another trip brief"}<Icon name="arrowRight" size={16} /></Link></p>}
    {!unavailable && snapshot?.itinerary && terminal(snapshot) && <p className="end-note"><Link className="text-link" href="/">Start a new trip brief<Icon name="arrowRight" size={16} /></Link></p>}
  </div>;
}
