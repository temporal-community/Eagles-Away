"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Button } from "@ui-sdk/react/button";
import { CalendarDay, CalendarGrid, CalendarHeading, CalendarNext, CalendarPrevious, CalendarWeekday, CalendarWeekdays, CalendarWeeks, DatePicker, DatePickerContent, DatePickerInput, DatePickerTrigger } from "@ui-sdk/react/date-picker";
import { Field, FieldDescription, FieldLabel } from "@ui-sdk/react/field";
import { NumberInput, NumberInputControl, NumberInputDecrement, NumberInputGroup, NumberInputIncrement } from "@ui-sdk/react/number-input";
import { Radio, RadioGroup, RadioIndicator } from "@ui-sdk/react/radio";
import { SegmentedControl, SegmentedControlIndicator, SegmentedControlItem } from "@ui-sdk/react/segmented-control";
import { Select, SelectContent, SelectItem, SelectItemIndicator, SelectTrigger, SelectValue } from "@ui-sdk/react/select";
import { Switch, SwitchThumb } from "@ui-sdk/react/switch";
import { Brief, Fixture, londonDayBoundary, request } from "../lib/api";
import formCatalog from "../lib/form-catalog.json";
import { trackAnalytics } from "../lib/analytics";
import { readBrowserBrief } from "../lib/browser-brief";
import { usePlanSubmission } from "../lib/use-plan-submission";
import { CPFC_TICKET_URL } from "../lib/tickets";
import { addDays, country, dateParts, day, dayRange, daysUntil, flag, kickoff, londonDate } from "../lib/trip-format";
import { Icon } from "./icons";

const stepNames = ["Match", "Travel"] as const;
const bundledFixtures = formCatalog.fixtures as Fixture[];

type TripLength = Brief["flexibility"] | "custom";

// Mirrors validate_brief and enumerate_specs in the API: D±1 for tight and day_either_side, D±2 for two_days.
function tripLengths(matchDate: string): { value: Brief["flexibility"]; label: string; dates: string; hint: string }[] {
  const at = (offset: number) => addDays(matchDate, offset);
  return [
    { value: "tight", label: "Quick", dates: "1 night", hint: `One night: ${day(at(-1))} → ${day(matchDate)}, or ${day(matchDate)} → ${day(at(1))}` },
    { value: "day_either_side", label: "+1 day", dates: dayRange(at(-1), at(1)), hint: `Out ${day(at(-1))}, home ${day(at(1))}` },
    { value: "two_days", label: "+2 days", dates: dayRange(at(-2), at(2)), hint: `Out as early as ${day(at(-2))}, home by ${day(at(2))}` },
  ];
}
const styles: { value: Brief["budget_tier"]; price: string; title: string; help: string }[] = [
  { value: "budget", price: "£", title: "Cheap", help: "Dorms and shared bathrooms OK" },
  { value: "value", price: "££", title: "Best value", help: "Balance price and comfort" },
  { value: "comfort", price: "£££", title: "Comfort", help: "Fewer changes, nicer stays" },
];

function selectedOrFirst(current: string, available: Fixture[]): string {
  return available.some(f => f.id === current) ? current : available[0]?.id ?? "";
}

function Stepper({ label, value, onChange, min, max }: { label: string; value: number; onChange: (value: number) => void; min: number; max: number }) {
  return <Field className="stepper">
    <FieldLabel>{label}</FieldLabel>
    <NumberInput value={value} min={min} max={max} onValueChange={next => onChange(next ?? min)} required>
      <NumberInputGroup>
        <NumberInputDecrement aria-label={`Fewer ${label.toLowerCase()}`} />
        <NumberInputControl />
        <NumberInputIncrement aria-label={`More ${label.toLowerCase()}`} />
      </NumberInputGroup>
    </NumberInput>
  </Field>;
}

export function PlannerForm() {
  const router = useRouter();
  const formRef = useRef<HTMLFormElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [fixtures, setFixtures] = useState<Fixture[]>(bundledFixtures);
  const [originCities, setOriginCities] = useState<string[]>(formCatalog.origin_cities);
  const [railCities, setRailCities] = useState<string[]>(formCatalog.rail_cities);
  const [origin, setOrigin] = useState("London");
  const [departureCity, setDepartureCity] = useState<string | null>(null);
  const [transport, setTransport] = useState<Brief["transport_mode"]>(null);
  const [selected, setSelected] = useState(bundledFixtures[0]?.id ?? "");
  const [step, setStep] = useState(0);
  const [hasNavigated, setHasNavigated] = useState(false);
  const [length, setLength] = useState<TripLength>("day_either_side");
  const [range, setRange] = useState<[string, string] | null>(null);
  const [calendarAnchor, setCalendarAnchor] = useState<HTMLElement | null>(null);
  const [budget, setBudget] = useState<Brief["budget_tier"]>("value");
  const [adults, setAdults] = useState(1);
  const [rooms, setRooms] = useState(1);
  const [children, setChildren] = useState<number[]>([]);
  const [privateRoom, setPrivateRoom] = useState(false);
  const [privateBathroom, setPrivateBathroom] = useState(false);
  const [error, setError] = useState("");
  const { startPlan, starting, opening, busy } = usePlanSubmission(router.push, setError);
  const [sample, setSample] = useState(false);
  const [now, setNow] = useState<Date | null>(null);

  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams(window.location.search);
    const editSession = params.get("edit");
    const saved = editSession ? readBrowserBrief(editSession)?.brief : undefined;
    const savedFixture = saved?.fixture_ids[0];
    // The static HTML and first hydration render share the same catalog; prune past games afterward.
    queueMicrotask(() => {
      if (cancelled) return;
      const upcomingBundled = bundledFixtures.filter(f => new Date(f.kickoff_at) > new Date());
      setNow(new Date());
      setFixtures(upcomingBundled);
      setSelected(current => selectedOrFirst(current, upcomingBundled));
      if (!saved || !savedFixture) return;
      const windowForFixture = saved.windows?.find(window => window.fixture_id === savedFixture);
      setSelected(savedFixture);
      setOrigin(saved.origin_city);
      setDepartureCity(saved.departure_city ?? null);
      setTransport(saved.departure_city ? "flight" : saved.transport_mode ?? null);
      setLength(windowForFixture ? "custom" : saved.flexibility);
      setRange(windowForFixture ? [windowForFixture.earliest_departure.slice(0, 10), windowForFixture.latest_return.slice(0, 10)] : null);
      setAdults(saved.travellers.adults);
      setChildren(saved.travellers.child_ages);
      setRooms(saved.travellers.rooms);
      setBudget(saved.budget_tier);
      setPrivateRoom(saved.private_room ?? false);
      setPrivateBathroom(saved.private_bathroom ?? false);
    });
    request<{ fixtures: Fixture[]; origin_cities: string[]; rail_cities: string[]; development_mode: boolean }>("/api/catalog")
      .then(data => {
        if (cancelled || !data) return;
        const upcoming = data.fixtures.filter(f => new Date(f.kickoff_at) > new Date());
        setFixtures(upcoming);
        setOriginCities(data.origin_cities ?? ["London"]);
        setRailCities(data.rail_cities ?? ["London"]);
        setSelected(current => current === savedFixture && !upcoming.some(f => f.id === current) ? "" : selectedOrFirst(current, upcoming));
        setSample(data.development_mode);
      })
      .catch(() => { if (!cancelled) setError("The planner is unavailable. Please reload to try again."); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (hasNavigated) headingRef.current?.focus();
  }, [step, hasNavigated]);

  useEffect(() => {
    if (step === 1) router.prefetch("/plan/");
  }, [router, step]);

  function moveTo(next: number) {
    if (step === 0 && next === 1) trackAnalytics("form_match_complete");
    setHasNavigated(true);
    setError("");
    setStep(next);
  }

  function nextStep() {
    if (step === 0 && !originCities.includes(origin)) {
      setError("Choose a UK departure city.");
      return;
    }
    if (step === 0 && !selectedFixture) {
      setError("Choose an away match to continue.");
      return;
    }
    const page = formRef.current?.querySelector<HTMLElement>(`[data-form-step="${step}"]`);
    const invalid = Array.from(page?.querySelectorAll<HTMLInputElement>("input") ?? []).find(field => !field.checkValidity());
    if (invalid) { invalid.reportValidity(); return; }
    if (step === 0 && length === "custom" && !range) {
      setError("Pick the day you leave and the day you come home.");
      return;
    }
    if (step === 0 && length === "custom" && range && !(range[0] <= matchDate && matchDate <= range[1])) {
      setError(`Your dates need to include match day, ${day(matchDate)}.`);
      return;
    }
    moveTo(step + 1);
  }

  const selectedFixture = fixtures.find(f => f.id === selected);
  const matchDate = selectedFixture ? londonDate(selectedFixture.kickoff_at) : "";
  const lengths = matchDate ? tripLengths(matchDate) : [];
  const railAvailable = railCities.includes(origin) && selectedFixture?.city === "Lyon";
  const mode: Brief["transport_mode"] = railAvailable ? transport : "flight";

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (step !== 1 || !selectedFixture || busy) return;
    const customDates = length === "custom" ? range : null;
    const brief: Brief = {
      fixture_ids: [selected],
      origin_city: origin,
      ...(departureCity ? { departure_city: departureCity } : {}),
      travellers: { adults, child_ages: children, rooms },
      budget_tier: budget,
      flexibility: length === "custom" ? "day_either_side" : length,
      windows: customDates ? [{ fixture_id: selected, earliest_departure: londonDayBoundary(customDates[0]), latest_return: londonDayBoundary(customDates[1], true) }] : [],
      private_room: privateRoom,
      private_bathroom: privateBathroom,
      transport_mode: mode,
    };
    await startPlan(brief, selectedFixture.city);
  }

  const heading = (index: number, text: string) => <h2 id={step === index ? "form-step-heading" : undefined} ref={step === index ? headingRef : undefined} tabIndex={-1}>{text}</h2>;
  const searchCity = departureCity ?? origin;
  const airports = searchCity === "London" ? "LHR, LGW, STN, LTN" : `${searchCity} airport`;
  const railNote = origin === "London" ? " + Eurostar" : " + trains via London and Paris";
  const searchNote = railAvailable && !departureCity ? airports + railNote : airports;

  return <form ref={formRef} className="brief card" onSubmit={submit}>
    {sample && <div className="notice">Development preview · Synthetic trips and prices</div>}
    <nav aria-label="Trip brief progress">
      <ol className="form-progress">
        {stepNames.map((name, index) => <li key={name} aria-current={step === index ? "step" : undefined} className={index < step ? "complete" : ""}>
          <span>{name}</span>
        </li>)}
      </ol>
    </nav>
    {error && <p className="error" role="alert"><Icon name="alert" />{error}</p>}
    <fieldset disabled={busy} className={hasNavigated ? "form-pages transitioning" : "form-pages"}>
      <section className="form-page" data-form-step="0" hidden={step !== 0} aria-labelledby={step === 0 ? "form-step-heading" : undefined}>
        {heading(0, "Pick your match")}
        <Field>
          <FieldLabel>Starting from</FieldLabel>
          <Select value={origin} onValueChange={value => {
            if (!value || value === origin) return;
            setOrigin(value);
            setDepartureCity(null);
            setTransport(null);
          }} items={originCities}>
            <SelectTrigger className="select-trigger">
              <Icon name="pin" />
              <SelectValue />
              <Icon name="chevron" className="icon chevron" />
            </SelectTrigger>
            <SelectContent className="select-content">
              {originCities.map(city => <SelectItem key={city} value={city}>{city}<SelectItemIndicator /></SelectItem>)}
            </SelectContent>
          </Select>
        </Field>
        <Field>
          <FieldLabel>Away match</FieldLabel>
          <RadioGroup className="fixtures" name="fixture" value={selected} onValueChange={value => {
            if (!value) return;
            setSelected(value);
            if (!departureCity) setTransport(null);
            setRange(null);
          }}>
            {fixtures.map(f => {
              const date = dateParts(f);
              const days = now ? daysUntil(f.kickoff_at, now) : null;
              return <label key={f.id} className="fixture">
                <span className="date-tile" aria-hidden="true"><small>{date.weekday}</small><b>{date.day}</b><small>{date.month}</small></span>
                <span className="fixture-text">
                  <strong>{f.opponent}</strong>
                  <span className="fixture-meta"><span aria-hidden="true">{flag(f)}</span> {f.city}, {country(f)} · {kickoff(f, "Europe/London")} UK</span>
                  <span className="sr-only">{date.weekday} {date.day} {date.month}</span>
                </span>
                {days !== null && days <= 60 && <span className="countdown">{days <= 1 ? "Tomorrow" : `${days} days`}</span>}
                <Radio value={f.id}><RadioIndicator /></Radio>
              </label>;
            })}
          </RadioGroup>
        </Field>
        <a className="pill-link" href={CPFC_TICKET_URL} target="_blank" rel="noopener noreferrer"><Icon name="ticket" />Buy away tickets<span className="muted">CPFC members</span><Icon name="external" size={14} /></a>
        <Field>
          <FieldLabel>Trip length</FieldLabel>
          <SegmentedControl className="segmented trip-length" value={length} onValueChange={value => value && setLength(value as TripLength)}>
            <SegmentedControlIndicator />
            {lengths.map(t => <SegmentedControlItem key={t.value} value={t.value}><span>{t.label}</span><small>{t.dates}</small></SegmentedControlItem>)}
            <SegmentedControlItem value="custom"><span>Custom</span><small>{range ? dayRange(range[0], range[1]) : "Pick dates"}</small></SegmentedControlItem>
          </SegmentedControl>
          {length !== "custom" && <FieldDescription>{lengths.find(t => t.value === length)?.hint}</FieldDescription>}
        </Field>
        {length === "custom" && matchDate && <DatePicker className="range-picker" mode="range" locale="en-GB" value={range} onValueChange={value => setRange(Array.isArray(value) ? value as [string, string] : null)} min={now ? londonDate(now.toISOString()) : undefined} minDays={2} defaultMonth={matchDate.slice(0, 7)} weekStartsOn={1} timeZone="Europe/London">
          <div className="range-fields" ref={setCalendarAnchor}>
            <label className="range-end"><small>Leave</small><DatePickerInput aria-label="Leave" /></label>
            <Icon name="arrowRight" size={16} className="icon range-arrow" />
            <label className="range-end"><small>Home</small><DatePickerInput end aria-label="Home" /></label>
            <DatePickerTrigger aria-label="Open calendar"><Icon name="calendar" /></DatePickerTrigger>
          </div>
          <DatePickerContent className="calendar-pop" anchor={calendarAnchor} align="start" sideOffset={6}>
            <div className="calendar-head">
              <CalendarPrevious aria-label="Previous month"><Icon name="chevronLeft" /></CalendarPrevious>
              <CalendarHeading />
              <CalendarNext aria-label="Next month"><Icon name="chevronRight" /></CalendarNext>
            </div>
            <CalendarGrid>
              <CalendarWeekdays>{index => <CalendarWeekday index={index} />}</CalendarWeekdays>
              <CalendarWeeks>{cell => <CalendarDay value={cell.date} data-match={cell.date === matchDate ? "" : undefined} />}</CalendarWeeks>
            </CalendarGrid>
            <p className="hint"><span className="match-dot" aria-hidden="true" />Match day, {day(matchDate)}. London dates.</p>
          </DatePickerContent>
          <p className="hint"><Icon name="ball" size={14} />Your dates must include match day, {day(matchDate)}.</p>
        </DatePicker>}
        <div className="form-actions">
          <Button className="primary" onClick={nextStep}>Continue to travel preferences<Icon name="arrowRight" /></Button>
        </div>
      </section>

      <section className="form-page" data-form-step="1" hidden={step !== 1} aria-labelledby={step === 1 ? "form-step-heading" : undefined}>
        {heading(1, "How do you like to travel?")}
        <div className="steppers">
          <Stepper label="Adults" value={adults} onChange={setAdults} min={1} max={8} />
          {sample && <Stepper label="Rooms" value={rooms} onChange={setRooms} min={1} max={4} />}
          {sample && <Stepper label="Children" value={children.length} min={0} max={6} onChange={count => setChildren(Array.from({ length: count }, (_, i) => children[i] ?? 8))} />}
        </div>
        {!!children.length && <div className="steppers">{children.map((age, i) => <Stepper key={`child-${i + 1}`} label={`Child ${i + 1} age`} value={age} min={0} max={17} onChange={next => setChildren(children.map((a, n) => n === i ? next : a))} />)}</div>}
        {!sample && <p className="hint"><Icon name="users" size={14} />One shared room for your group.</p>}
        <Field>
          <FieldLabel>Travel by</FieldLabel>
          <SegmentedControl className="segmented" value={mode ?? "any"} onValueChange={value => {
            if (value !== "flight") setDepartureCity(null);
            setTransport(value === "any" ? null : value as Brief["transport_mode"]);
          }}>
            <SegmentedControlIndicator />
            <SegmentedControlItem value="any" disabled={!railAvailable}><Icon name="route" />Compare</SegmentedControlItem>
            <SegmentedControlItem value="flight"><Icon name="plane" />Flights</SegmentedControlItem>
            <SegmentedControlItem value="rail" disabled={!railAvailable}><Icon name="train" />Trains</SegmentedControlItem>
          </SegmentedControl>
          {!railAvailable && <FieldDescription>{selectedFixture?.city === "Lyon" ? `No reviewed train route from ${origin} to Lyon yet.` : "Trains are available for Lyon only."}</FieldDescription>}
        </Field>
        <Field>
          <FieldLabel>Travel style</FieldLabel>
          <RadioGroup className="styles" name="budget" value={budget} onValueChange={value => value && setBudget(value as Brief["budget_tier"])}>
            {styles.map(s => <label key={s.value} className="style-tile">
              <Radio value={s.value} />
              <b aria-hidden="true">{s.price}</b>
              <strong>{s.title}</strong>
              <small>{s.help}</small>
            </label>)}
          </RadioGroup>
        </Field>
        <div className="switches">
          <label className="switch-row"><Icon name="door" />Private room<Switch checked={privateRoom} onCheckedChange={setPrivateRoom}><SwitchThumb /></Switch></label>
          <label className="switch-row"><Icon name="bath" />Private bathroom<Switch checked={privateBathroom} onCheckedChange={setPrivateBathroom}><SwitchThumb /></Switch></label>
        </div>
        <p className="hint"><Icon name="plane" size={14} />Searching {searchNote}, nonstop first{selectedFixture?.city === "Białystok" ? ", + Warsaw–Białystok train" : ""}.</p>
        <div className="form-actions">
          <Button className="secondary" onClick={() => moveTo(0)}><Icon name="arrowLeft" />Back</Button>
          <Button type="submit" className="primary" disabled={!selectedFixture || busy}>{opening ? "Opening your plan…" : starting ? "Starting…" : <>Plan my away day<Icon name="arrowRight" /></>}</Button>
        </div>
        <p className="hint center">No account needed · <Link href="/privacy/">How we use your data</Link></p>
      </section>
    </fieldset>
  </form>;
}
