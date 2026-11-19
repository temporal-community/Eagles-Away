import { type ReactNode } from "react";
import { Collapsible, CollapsiblePanel, CollapsibleTrigger } from "@ui-sdk/react/collapsible";
import { Meter, MeterIndicator, MeterLabel, MeterTrack, MeterValue } from "@ui-sdk/react/meter";
import { Timeline, TimelineConnector, TimelineDescription, TimelineItem, TimelineMarker, TimelineTime, TimelineTitle } from "@ui-sdk/react/timeline";
import { Fixture, Itinerary, Leg, Stay, Trip, formatMoney } from "../lib/api";
import { CPFC_TICKET_URL } from "../lib/tickets";
import { day, duration, flag, kickoff, matchDay, nights, wallTime } from "../lib/trip-format";
import { Icon } from "./icons";

const scopeLabel = { round_trip: "Return", leg: "One way", stay: "Whole stay", night: "Per night", unknown: "" } as const;
const unitLabel = { party: "group", person: "person", room: "room", bed: "bed", unknown: "" } as const;

// Every caveat stays available, but behind one compact "N checks" pill per row.
function itemChecks(item: Leg | Stay): string[] {
  const q = item.quote;
  return [...new Set([
    q?.taxes === "excluded" ? "Taxes not included" : q?.taxes === "unknown" ? "Taxes not confirmed" : "",
    ...(q?.caveats ?? []),
    ...item.caveats,
    item.offer.link_kind === "contextual_search" && item.offer.booking_url ? "The link opens a fresh search; you may need to reselect this option." : "",
  ].filter(Boolean))];
}

function ChecksTrigger({ count, label }: { count: number; label: string }) {
  return <CollapsibleTrigger className="checks-pill" aria-label={`${count} check${count === 1 ? "" : "s"} for ${label}`}>
    <Icon name="alert" size={12} />{count} check{count === 1 ? "" : "s"}<Icon name="chevron" size={12} className="icon chevron" />
  </CollapsibleTrigger>;
}

function ChecksPanel({ checks }: { checks: string[] }) {
  return <CollapsiblePanel className="checks-panel"><ul>{checks.map(n => <li key={n}>{n}</li>)}</ul></CollapsiblePanel>;
}

function Price({ item, checks, label }: { item: Leg | Stay; checks: string[]; label: string }) {
  const q = item.quote;
  const source = item.offer.evidence.underlying_source;
  const scope = [scopeLabel[q?.scope ?? "unknown"], q && unitLabel[q.unit] && `per ${unitLabel[q.unit]}`].filter(Boolean).join(" · ").replace("per group", "for the group");
  return <div className="price">
    {q
      ? <><strong>{formatMoney(q.amount)}</strong><small>{[scope, q.additional_taxes && `+${formatMoney(q.additional_taxes)} tax`].filter(Boolean).join(" · ")}</small></>
      : <><strong className="unpriced">No price</strong><small>Check current fare</small></>}
    {item.offer.booking_url && <a className="chip-link" href={item.offer.booking_url} target="_blank" rel="noopener noreferrer"><span className="sr-only">View on </span>{source}<Icon name="external" size={13} /></a>}
    <span className="price-foot">
      {q && <small className="source" title={`${source} via ${item.offer.evidence.provider}, ${new Date(q.observed_at).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" })} UTC`}>Checked {new Date(q.observed_at).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })}</small>}
      {!!checks.length && <ChecksTrigger count={checks.length} label={label} />}
    </span>
  </div>;
}

// The root is the row body so the expanded checks span the full row, not the narrow price column.
function StopBody({ checks, children }: { checks: string[]; children: ReactNode }) {
  return checks.length
    ? <Collapsible className="stop-body">{children}<ChecksPanel checks={checks} /></Collapsible>
    : <div className="stop-body">{children}</div>;
}

function LegItem({ leg }: { leg: Leg }) {
  const length = duration(leg.departs_at, leg.arrives_at);
  const checks = itemChecks(leg);
  return <TimelineItem className="stop">
    <TimelineMarker><Icon name={leg.mode === "rail" ? "train" : "plane"} /></TimelineMarker>
    <TimelineConnector />
    <StopBody checks={checks}>
      <div className="stop-main">
        <TimelineTitle render={<h3 />}>{leg.origin}<Icon name="arrowRight" size={16} /><span className="sr-only"> to </span>{leg.destination}</TimelineTitle>
        <TimelineTime dateTime={leg.departs_at}>{day(leg.departs_at)} · {wallTime(leg.departs_at)}–{wallTime(leg.arrives_at)}{leg.arrives_at.slice(0, 10) !== leg.departs_at.slice(0, 10) ? " +1" : ""}</TimelineTime>
        <TimelineDescription>{[leg.operator, leg.service_number].filter(Boolean).join(" ")}{length ? ` · ${length}` : ""}</TimelineDescription>
      </div>
      <Price item={leg} checks={checks} label={`${leg.origin} to ${leg.destination}`} />
    </StopBody>
  </TimelineItem>;
}

function StayItem({ stay }: { stay: Stay }) {
  const count = nights(stay.check_in, stay.check_out);
  const facts = [stay.room_description, stay.bathroom !== "unknown" && `${stay.bathroom === "private" ? "Private" : "Shared"} bathroom`].filter(Boolean).join(" · ");
  const checks = itemChecks(stay);
  return <TimelineItem className="stop">
    <TimelineMarker><Icon name="bed" /></TimelineMarker>
    <TimelineConnector />
    <StopBody checks={checks}>
      <div className="stop-main">
        <TimelineTitle render={<h3 />}>{stay.property_name}</TimelineTitle>
        <TimelineTime dateTime={stay.check_in}>{day(stay.check_in)} – {day(stay.check_out)} · {count} night{count === 1 ? "" : "s"}</TimelineTime>
        <TimelineDescription>{facts}{stay.review_score ? <span className="score"><Icon name="star" size={12} />{stay.review_score}</span> : null}</TimelineDescription>
      </div>
      <Price item={stay} checks={checks} label={stay.property_name} />
    </StopBody>
  </TimelineItem>;
}

function MatchItem({ fixture }: { fixture: Fixture }) {
  const local = kickoff(fixture), uk = kickoff(fixture, "Europe/London");
  return <TimelineItem className="stop match-stop">
    <TimelineMarker><Icon name="ball" /></TimelineMarker>
    <TimelineConnector />
    <div className="stop-body">
      <div className="stop-main">
        <TimelineTitle render={<h3 />}>{fixture.opponent} v Crystal Palace</TimelineTitle>
        <TimelineTime dateTime={fixture.kickoff_at}>{matchDay(fixture)} · {local} local{local !== uk ? ` (${uk} UK)` : ""}</TimelineTime>
        <TimelineDescription>{fixture.venue}{fixture.venue_status === "provisional" ? " · venue provisional" : ""}</TimelineDescription>
      </div>
      <div className="price">
        <strong className="unpriced">Ticket</strong><small>Not included</small>
        <a className="chip-link" href={CPFC_TICKET_URL} target="_blank" rel="noopener noreferrer">Buy away tickets<Icon name="external" size={13} /></a>
      </div>
    </div>
  </TimelineItem>;
}

function PendingItem({ label, children }: { label: string; children?: ReactNode }) {
  return <TimelineItem className="stop pending-stop">
    <TimelineMarker><span className="spinner" /></TimelineMarker>
    <TimelineConnector />
    <div className="stop-body"><div className="stop-main">
      <TimelineTitle render={<h3 />}>{label}</TimelineTitle>
      {children}
    </div></div>
  </TimelineItem>;
}

function MatchHero({ trip, status, tone }: { trip: Trip; status: string; tone: "ready" | "checks" | "progress" }) {
  const f = trip.fixture;
  return <header className="match-hero">
    <div className="hero-top">
      <span className="eyebrow"><span aria-hidden="true">{flag(f)}</span> {f.city}<span className="competition"> · UEFA Europa League</span></span>
      <span className={`badge ${tone}`}><Icon name={tone === "ready" ? "check" : tone === "checks" ? "alert" : "clock"} size={14} />{status}</span>
    </div>
    <h2>{f.opponent} <span>v</span> Palace</h2>
    <p className="hero-meta">
      <span><Icon name="calendar" size={15} />{matchDay(f, { weekday: "short", day: "numeric", month: "short", year: "numeric" })}</span>
      <span><Icon name="clock" size={15} />{kickoff(f)} local</span>
      <span><Icon name="pin" size={15} />{f.venue}</span>
    </p>
  </header>;
}

function Stats({ trip }: { trip: Trip }) {
  const journey = trip.journey;
  const away = journey ? nights(journey.outbound[0].departs_at.slice(0, 10), journey.inbound.at(-1)!.arrives_at.slice(0, 10)) : trip.stay ? nights(trip.stay.check_in, trip.stay.check_out) : 0;
  const modes = new Set(journey ? [...journey.outbound, ...journey.inbound].map(l => l.mode) : []);
  return <dl className="stats">
    <div className="total">
      <dt>Total</dt>
      <dd>
        {trip.known_total ? formatMoney(trip.known_total) : "—"}
        <Meter className="coverage" value={Math.round(trip.price_coverage * 100)}>
          <MeterTrack><MeterIndicator /></MeterTrack>
          <span><MeterValue /> <MeterLabel>priced</MeterLabel></span>
        </Meter>
      </dd>
    </div>
    <div><dt>Away</dt><dd>{away ? `${away + 1} days` : "—"}</dd></div>
    <div><dt>Travel</dt><dd>{modes.size ? [...modes].map(m => <span key={m} className="mode"><Icon name={m === "rail" ? "train" : "plane"} />{m === "rail" ? "Train" : "Flight"}</span>) : "—"}</dd></div>
  </dl>;
}

function SearchLinkNote({ trip }: { trip: Trip }) {
  const items = [trip.stay, ...(trip.journey?.outbound ?? []), ...(trip.journey?.inbound ?? [])];
  if (!items.some(item => item?.offer.link_kind === "contextual_search" && item.offer.booking_url)) return null;
  return <p className="hint inset"><Icon name="info" size={14} />Links open a fresh search. You may need to reselect.</p>;
}

function TripDisclosure({ trip, label, partial = false }: { trip: Trip; label: string; partial?: boolean }) {
  const { journey, stay } = trip;
  return <article className="trip card" aria-label={partial ? "Partial trip results" : undefined}>
    <Collapsible className="disclosure">
      <CollapsibleTrigger><Icon name={journey ? "route" : "bed"} />{label}<Icon name="chevron" className="icon chevron" /></CollapsibleTrigger>
      <CollapsiblePanel>
        <p className="hint">{journey && stay ? "Confirm dates, current prices and availability with each provider before booking." : "Check dates and current prices before combining this option with other travel."}</p>
        <Timeline className="itinerary" aria-label={label}>
          {journey?.outbound.map(leg => <LegItem key={leg.id} leg={leg} />)}
          {stay && <StayItem stay={stay} />}
          {partial && <MatchItem fixture={trip.fixture} />}
          {journey?.inbound.map(leg => <LegItem key={leg.id} leg={leg} />)}
        </Timeline>
        <SearchLinkNote trip={trip} />
      </CollapsiblePanel>
    </Collapsible>
  </article>;
}

export function JourneyPreview({ trip, ended = false, children }: { trip: Trip; ended?: boolean; children?: ReactNode }) {
  const { journey, stay } = trip;
  if (!journey && !stay) return null;
  const paired = Boolean(journey && stay);
  if (ended) {
    const label = paired ? "Journey and stay found" : journey?.outbound[0].mode === "flight" ? "Flight found" : journey ? "Journey found" : "Stay found";
    return <TripDisclosure trip={trip} label={label} partial />;
  }
  const label = paired ? "Provisional itinerary" : journey ? "Journey found while planning continues" : "Stay found while planning continues";
  const status = paired ? "Provisional trip" : "Plan in progress";
  return <article className="trip card journey-preview" aria-label={label}>
    <MatchHero trip={trip} status={status} tone="progress" />
    {paired && <Stats trip={trip} />}
    <Timeline className="itinerary" aria-label="Itinerary">
      {!journey && <PendingItem label="Finding your journey">{children}</PendingItem>}
      {journey?.outbound.map(leg => <LegItem key={leg.id} leg={leg} />)}
      {stay ? <StayItem stay={stay} /> : <PendingItem label="Finding your stay">{children}</PendingItem>}
      <MatchItem fixture={trip.fixture} />
      {journey?.inbound.map(leg => <LegItem key={leg.id} leg={leg} />)}
      {paired && <PendingItem label="Comparing options">{children}</PendingItem>}
    </Timeline>
    {paired && <p className="hint inset">Confirm dates, current prices and availability with each provider before booking.</p>}
    <SearchLinkNote trip={trip} />
  </article>;
}

const firstSentence = (text: string) => text.split(/(?<=[.;])\s/)[0].replace(/[.;]$/, "");

function BeforeYouBook({ trip }: { trip: Trip }) {
  const checks = trip.checks?.length ? trip.checks : (trip.gaps ?? []).map(g => ({ headline: firstSentence(g), detail: g }));
  const links = [
    ...(trip.transfers ?? []).map(t => ({ title: t.title, label: t.summary || t.title, text: t.description, url: t.source_url, cta: "Official guidance" })),
    ...(trip.maps_transfers ?? []).map(t => ({ title: t.title, label: t.summary || t.title, text: t.description, url: t.url, cta: "Open in Google Maps" })),
  ];
  if (!checks.length && !links.length) return null;
  const covered = new Set(checks.map(c => c.detail));
  const details = [
    ...checks.filter(c => c.detail && c.detail !== c.headline).map(c => ({ title: c.headline, text: c.detail! })),
    ...links.filter(l => !covered.has(l.text)).map(l => ({ title: l.title, text: l.text })),
  ];
  const id = `book-${trip.fixture.id}`;
  return <section className="before-book" aria-labelledby={id}>
    {!!checks.length && <>
      <h3 id={id}><Icon name="alert" size={16} />Before you book<span>{checks.length} check{checks.length === 1 ? "" : "s"}</span></h3>
      <ul className="check-list">{checks.map(c => <li key={c.headline}>{c.headline}</li>)}</ul>
    </>}
    {!!links.length && <div className="around">
      <h4 id={checks.length ? undefined : id}>Getting around</h4>
      <ul>{links.map(l => <li key={l.url + l.title}>
        <a className="chip-link" href={l.url} target="_blank" rel="noopener noreferrer" title={l.title} aria-label={`${l.title}: ${l.cta}`}><Icon name="map" size={14} />{l.label}<Icon name="external" size={12} /></a>
      </li>)}</ul>
    </div>}
    {!!details.length && <Collapsible className="book-details">
      <CollapsibleTrigger>Details<Icon name="chevron" size={14} className="icon chevron" /></CollapsibleTrigger>
      <CollapsiblePanel><dl>{details.map(d => <div key={d.title + d.text}><dt>{d.title}</dt><dd>{d.text}</dd></div>)}</dl></CollapsiblePanel>
    </Collapsible>}
  </section>;
}

export function EarlierOptions({ trips }: { trips: Trip[] }) {
  return <div className="itineraries">
    {trips.map(trip => {
      const { journey, stay, fixture } = trip;
      const paired = journey && stay;
      const label = paired ? "Earlier matching trip" : journey ? "Earlier journey option" : "Earlier stay option";
      return <TripDisclosure key={`${fixture.id}-${journey?.id ?? ""}-${stay?.id ?? ""}`} trip={trip} label={label} />;
    })}
  </div>;
}

export function ItineraryView({ itinerary }: { itinerary: Itinerary }) {
  return <div className="itineraries">
    {itinerary.trips.map(trip => {
      return <article key={trip.fixture.id} className="trip card">
        <MatchHero trip={trip} status={trip.feasibility === "verified" ? "Ready to review" : "Checks outstanding"} tone={trip.feasibility === "verified" ? "ready" : "checks"} />
        <Stats trip={trip} />
        <BeforeYouBook trip={trip} />
        <Timeline className="itinerary" aria-label="Itinerary">
          {trip.journey?.outbound.map(leg => <LegItem key={leg.id} leg={leg} />)}
          {trip.stay && <StayItem stay={trip.stay} />}
          <MatchItem fixture={trip.fixture} />
          {trip.journey?.inbound.map(leg => <LegItem key={leg.id} leg={leg} />)}
        </Timeline>
        <SearchLinkNote trip={trip} />
        {!!trip.alternatives?.length && <Collapsible className="disclosure">
          <CollapsibleTrigger><Icon name="route" />{trip.alternatives.length} other option{trip.alternatives.length > 1 ? "s" : ""}<Icon name="chevron" className="icon chevron" /></CollapsibleTrigger>
          <CollapsiblePanel>
            <ul className="rows">{trip.alternatives.map(a => <li key={a.journey.id + a.stay.id}><div>
              <Icon name={a.journey.outbound[0].mode === "rail" ? "train" : "plane"} />
              <span><strong>{a.summary}</strong><small>{day(a.journey.outbound[0].departs_at)} {wallTime(a.journey.outbound[0].departs_at)} → {day(a.journey.inbound.at(-1)!.arrives_at)} {wallTime(a.journey.inbound.at(-1)!.arrives_at)}</small></span>
              <b>{a.known_total ? formatMoney(a.known_total) : "—"}</b>
            </div></li>)}</ul>
            <p className="hint">Some costs are missing, so totals aren’t a full comparison.</p>
          </CollapsiblePanel>
        </Collapsible>}
      </article>;
    })}
    {!!itinerary.caveats?.length && <p className="hint caveats">{itinerary.caveats.join(" ")}</p>}
  </div>;
}
