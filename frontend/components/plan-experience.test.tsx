import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ApiError, request, type Brief, type Snapshot } from "../lib/api";
import { readBrowserBrief, saveBrowserBrief } from "../lib/browser-brief";
import { PlanExperience } from "./plan-experience";

const { state, router } = vi.hoisted(() => ({ state: { snapshot: null as Snapshot | null }, router: { push: vi.fn() } }));

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams("session=example"), useRouter: () => router }));
vi.mock("../lib/api", async importOriginal => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  request: vi.fn(async () => state.snapshot),
}));

const snapshot: Snapshot = {
  development_mode: false,
  public_session_id: "example",
  origin_city: "London",
  state_revision: 1,
  phase: "closed",
  progress_message: "Your trip is ready. Check the outstanding details before booking.",
  itinerary: { generated_at: "2026-10-05T12:00:00Z", trips: [], caveats: [], ranking_policy_version: "1" },
  preview_options: [],
  searched_dates: [],
  recovery_actions: [],
  recovery_links: [],
};

const leg = { id: "flight", kind: "transport", mode: "flight", origin: "LTN", destination: "SZG", departs_at: "2027-01-28T08:00:00Z", arrives_at: "2027-01-28T10:55:00+01:00", operator: "easyJet", service_number: "U2 2639", quote: null, offer: { booking_url: "https://www.google.com/travel/flights", link_kind: "contextual_search", evidence: { underlying_source: "Google Flights", provider: "SearchApi" } }, caveats: [] };
const partialTrip = {
  fixture: { id: "salzburg", city: "Salzburg", opponent: "FC Red Bull Salzburg", kickoff_at: "2027-01-28T20:00:00Z", timezone: "Europe/Vienna", venue: "Red Bull Arena", venue_status: "provisional" },
  journey: { id: "salzburg-journey", outbound: [leg], inbound: [{ ...leg, id: "return", origin: "SZG", destination: "LTN", departs_at: "2027-01-29T16:00:00+01:00", arrives_at: "2027-01-29T17:00:00Z" }] },
  stay: null,
  price_coverage: 0,
} as unknown as NonNullable<Snapshot["preview_trip"]>;
const hotel = { id: "salzburg-hotel", property_name: "Salzburg Hotel", check_in: "2027-01-28", check_out: "2027-01-29", room_description: "Single room", bathroom: "private", offer: { booking_url: "https://www.booking.com/hotel/at/example.html", evidence: { underlying_source: "Booking.com", provider: "SearchApi" } }, quote: null, caveats: [] } as unknown as NonNullable<typeof partialTrip.stay>;
const matchingTrip = { ...partialTrip, stay: hotel };
const itinerary = { ...snapshot.itinerary!, trips: [matchingTrip] };
const originalBrief: Brief = {
  fixture_ids: ["salzburg"], origin_city: "Liverpool", travellers: { adults: 2, rooms: 1, child_ages: [] },
  budget_tier: "comfort", flexibility: "day_either_side", private_room: true, private_bathroom: true, transport_mode: "flight",
  windows: [{ fixture_id: "salzburg", earliest_departure: "2027-01-27T00:00:00+00:00", latest_return: "2027-01-29T23:59:00+00:00" }],
};
const recoveryLinks = [
  { label: "Google Flights · Liverpool to Salzburg · 27–29 Jan", url: "https://www.google.com/travel/flights?q=LPL%20to%20SZG%202027-01-27%20to%202027-01-29" },
  { label: "Booking.com · Salzburg · 27–29 Jan", url: "https://www.booking.com/searchresults.html?ss=Salzburg&checkin=2027-01-27&checkout=2027-01-29" },
];

describe("trip plan", () => {
  beforeEach(() => { vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null); });
  afterEach(() => { cleanup(); vi.clearAllMocks(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); sessionStorage.clear(); state.snapshot = null; });

  it.each(["journey", "stay"] as const)("keeps the %s visible when the other search fails", async kind => {
    vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }));
    const trip = kind === "journey" ? partialTrip : { ...partialTrip, journey: null, stay: hotel };
    state.snapshot = { ...snapshot, phase: "researching", itinerary: null, preview_trip: trip };
    render(<PlanExperience />);
    const providerName = kind === "journey" ? /View on Google Flights/ : /View on Booking.com/;
    expect((await screen.findAllByRole("link", { name: providerName }))[0]).toBeVisible();
    state.snapshot = { ...state.snapshot, state_revision: 2, phase: "failed" };
    window.dispatchEvent(new Event("online"));
    const partial = await screen.findByRole("article", { name: "Partial trip results" });
    expect(within(partial).queryByRole("link", { name: providerName })).not.toBeInTheDocument();
    fireEvent.click(within(partial).getByRole("button", { name: kind === "journey" ? "Flight found" : "Stay found" }));
    expect(within(partial).getAllByRole("link", { name: providerName })[0]).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Finding your stay" })).not.toBeInTheDocument();
  });

  it.each(["journey", "stay"] as const)("restores saved %s results when a failed session is reopened", async kind => {
    state.snapshot = { ...snapshot, phase: "failed", itinerary: null, preview_trip: kind === "journey" ? partialTrip : { ...partialTrip, journey: null, stay: hotel } };
    render(<PlanExperience />);
    const partial = await screen.findByRole("article", { name: "Partial trip results" });
    fireEvent.click(within(partial).getByRole("button", { name: kind === "journey" ? "Flight found" : "Stay found" }));
    expect(screen.getAllByRole("link", { name: kind === "journey" ? /View on Google Flights/ : /View on Booking.com/ })[0]).toBeVisible();
    expect(screen.getByRole("link", { name: /Try another trip brief/ })).toHaveAttribute("href", "/");
  });

  it("shows the finished plan with a link to start another", async () => {
    state.snapshot = snapshot;
    render(<PlanExperience />);
    expect(await screen.findByText(/Your trip is ready/)).toBeVisible();
    expect(screen.getByRole("link", { name: /Start a new trip brief/ })).toHaveAttribute("href", "/");
    expect(screen.queryByText(/^Flights depart from/)).not.toBeInTheDocument();
  });

  it("explains why an unsuccessful search found no trip", async () => {
    const reason = "We couldn't verify a complete trip within the search limits. Try again or adjust your preferences.";
    state.snapshot = { ...snapshot, phase: "failed", itinerary: null, progress_message: reason };
    render(<PlanExperience />);
    expect(await screen.findByText(reason)).toBeVisible();
    expect(screen.getByRole("link", { name: /Try another trip brief/ })).toHaveAttribute("href", "/");
  });

  const timingFailure: Partial<Snapshot> = {
    origin_city: "Liverpool", departure_city: "Liverpool", phase: "failed", itinerary: null, failure_reason: "flight_timing",
    progress_message: "No flight from Liverpool lands in Warsaw early enough for a train that reaches Białystok five hours before kickoff, with a way back after the match, on the dates we checked.",
    searched_dates: [{ outbound_date: "2026-12-09", return_date: "2026-12-11" }, { outbound_date: "2026-12-10", return_date: "2026-12-11" }],
    recovery_actions: [
      { kind: "depart_from", departure_city: "Manchester", label: "Plan from Manchester", detail: "Flights from Manchester (MAN) fit the match, Wed 9 Dec to Fri 11 Dec." },
      { kind: "depart_from", departure_city: "London", label: "Plan from London", detail: "Flights from London (LHR, LGW, STN, LTN) fit the match, Wed 9 Dec to Fri 11 Dec." },
    ],
  };

  it.each([true, false])("explains the cause and offers only verified next steps (saved brief: %s)", async saved => {
    if (saved) saveBrowserBrief("example", { brief: originalBrief, city: "Białystok" });
    state.snapshot = { ...snapshot, ...timingFailure };
    render(<PlanExperience />);
    const card = await screen.findByRole("region", { name: "No flights from Liverpool fit the match times" });
    expect(within(card).getByText(timingFailure.progress_message!)).toBeVisible();
    expect(within(card).getByText("Dates checked: Wed 9 Dec → Fri 11 Dec · Thu 10 Dec → Fri 11 Dec")).toBeVisible();
    for (const action of timingFailure.recovery_actions!) expect(within(card).getByText(action.detail)).toBeVisible();
    expect(screen.getByRole("status")).toHaveClass("sr-only");
    if (saved) {
      expect(within(card).getByRole("button", { name: "Plan from Manchester" })).toHaveClass("primary");
      expect(within(card).getByRole("button", { name: "Plan from London" })).toHaveClass("secondary");
      expect(within(card).getByRole("link", { name: "Edit trip brief" })).toHaveAttribute("href", "/?edit=example");
    } else {
      expect(within(card).queryByRole("button")).not.toBeInTheDocument();
      expect(within(card).getByRole("link", { name: /Try another trip brief/ })).toHaveAttribute("href", "/");
    }
    expect(vi.mocked(request).mock.calls.some(([path]) => path === "/api/sessions")).toBe(false);
  });

  it.each([
    ["Manchester", "Manchester"],
    [null, "your departure airport"],
  ])("names only a known departure city when the tab has no saved brief", async (departure_city, headingCity) => {
    state.snapshot = {
      ...snapshot, ...timingFailure, departure_city, failure_reason: "no_flights", recovery_actions: [],
      progress_message: "No flights from Manchester were listed on the dates we checked.",
    };
    render(<PlanExperience />);
    expect(await screen.findByRole("heading", { name: `No flights from ${headingCity} on your dates` })).toBeVisible();
    expect(screen.getByText("From Liverpool")).toBeVisible();
  });

  it.each([false, true])("announces recovery progress instead of stale search copy (preview: %s)", async hasPreview => {
    vi.useFakeTimers();
    vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }));
    saveBrowserBrief("example", { brief: { ...originalBrief, departure_city: "Manchester" }, city: "Białystok" });
    const checking = "No flights from Manchester to Warsaw were listed on the dates we checked. Checking flights from London…";
    const recovery: Snapshot = {
      ...snapshot, ...timingFailure, state_revision: 2, departure_city: "Manchester",
      phase: "researching", failure_reason: "no_flights", recovery_actions: [], progress_message: checking,
      itinerary: null,
      preview_trip: hasPreview ? { ...matchingTrip, journey: null } : null,
    };
    state.snapshot = hasPreview
      ? recovery
      : { ...snapshot, phase: "researching", itinerary: null, progress_message: "Comparing options…" };
    const { container } = render(<PlanExperience />);
    await act(() => vi.advanceTimersByTimeAsync(500));
    if (!hasPreview) {
      expect(screen.getByRole("application", { name: "Play in goal with Ziggy" })).toBeVisible();
      state.snapshot = recovery;
      await act(() => { window.dispatchEvent(new Event("online")); });
    }
    const card = screen.getByRole("region", { name: "No flights from Manchester on your dates" });
    expect(card).toHaveClass("checking");
    expect(within(card).getByText(checking)).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent(checking);
    expect(screen.queryByRole("application", { name: "Play in goal with Ziggy" })).not.toBeInTheDocument();
    expect(container.querySelector(".plan-progress, .ticker, .plan-connecting")).not.toBeInTheDocument();
    expect(within(card).queryByRole("button")).not.toBeInTheDocument();
    expect(within(card).queryByRole("link")).not.toBeInTheDocument();
  });

  it.each([
    { action: timingFailure.recovery_actions![1], change: { departure_city: "London" } },
    { action: { kind: "wider_dates", label: "Search two days either side", detail: "Flights from Liverpool fit the match, Tue 8 Dec to Fri 11 Dec." }, change: { flexibility: "two_days", windows: [] } },
    { action: { kind: "without_private_bathroom", label: "Show rooms without a confirmed private bathroom", detail: "Rooms in Salzburg are available." }, change: { private_bathroom: false } },
  ] as const)("starts $action.kind only on click and reuses submission identity after a failed request", async ({ action, change }) => {
    saveBrowserBrief("example", { brief: originalBrief, city: "Salzburg" });
    state.snapshot = { ...snapshot, ...timingFailure, recovery_actions: [action] };
    render(<PlanExperience />);
    const button = await screen.findByRole("button", { name: action.label });
    const posts = () => vi.mocked(request).mock.calls.filter(([path]) => path === "/api/sessions");
    expect(posts()).toHaveLength(0);

    const pending = Promise.withResolvers<never>();
    vi.mocked(request).mockImplementationOnce(() => pending.promise);
    fireEvent.click(button);
    fireEvent.click(button);
    expect(posts()).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Opening…" })).toBeDisabled();
    await act(async () => pending.reject(new Error("Connection interrupted. Please retry.")));
    expect(screen.getByRole("alert")).toHaveTextContent("Connection interrupted.");

    vi.mocked(request).mockResolvedValueOnce({ public_session_id: "recovery-session" });
    fireEvent.click(screen.getByRole("button", { name: action.label }));
    await waitFor(() => expect(router.push).toHaveBeenCalledWith("/plan/?session=recovery-session"));
    expect(posts()).toHaveLength(2);
    expect(posts()[1][1]).toEqual(posts()[0][1]);
    const expected = { ...originalBrief, ...change };
    expect(JSON.parse(String(posts()[0][1]?.body)).brief).toEqual(expected);
    expect(readBrowserBrief("recovery-session")).toEqual({ brief: expected, city: "Salzburg" });
    expect(readBrowserBrief("example")).toEqual({ brief: originalBrief, city: "Salzburg" });
  });

  it("shows a failed nearby-airport search with manual searches and no unverified offer", async () => {
    saveBrowserBrief("example", { brief: { ...originalBrief, departure_city: "Manchester" } });
    const message = "No flights from Manchester to Salzburg were listed on the dates we checked. We also checked flights from London and found nothing that fits.";
    state.snapshot = {
      ...snapshot, origin_city: "Liverpool", phase: "failed", itinerary: null, recovery_links: recoveryLinks,
      failure_reason: "no_flights", progress_message: message,
    };
    render(<PlanExperience />);
    const card = await screen.findByRole("region", { name: "No flights from Manchester on your dates" });
    expect(within(card).getByText(message)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Plan from/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Search providers directly" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Edit trip brief" })).toHaveAttribute("href", "/?edit=example");
  });

  it.each([false, true])("offers provider searches without a saved brief (partial result: %s)", async hasPartial => {
    state.snapshot = {
      ...snapshot, origin_city: "Liverpool", phase: "failed", itinerary: null, progress_message: "No compatible journey and stay were found.",
      preview_trip: hasPartial ? { ...partialTrip, journey: null, stay: hotel } : null, recovery_links: recoveryLinks,
      preview_options: hasPartial ? [{ ...partialTrip, journey: null, stay: { ...hotel, id: "earlier-hotel" } }] : [],
    };
    render(<PlanExperience />);
    const searches = await screen.findByRole("region", { name: "Provider searches" });
    const disclosure = within(searches).getByRole("button", { name: "Search providers directly" });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(disclosure);
    expect(within(searches).getByText("Confirm dates, passengers and room requirements before booking.")).toBeVisible();
    for (const { label, url } of recoveryLinks) {
      const link = within(searches).getByRole("link", { name: label });
      expect(link).toBeVisible();
      expect(link).toHaveAttribute("href", url);
      expect(link).toHaveAttribute("target", "_blank");
    }
    if (hasPartial) {
      expect(screen.getByRole("article", { name: "Partial trip results" })).toBeVisible();
      expect(screen.getByRole("button", { name: "Earlier stay option" })).toBeVisible();
    }
    expect(screen.getByRole("link", { name: /Try another trip brief/ })).toHaveAttribute("href", "/");
  });

  it("shows alternate search progress and calls out the transfer in preview and final results", async () => {
    vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }));
    const alternateTrip = { ...matchingTrip, journey: {
      ...matchingTrip.journey!,
      outbound: matchingTrip.journey!.outbound.map(item => ({ ...item, origin: "MAN" })),
      inbound: matchingTrip.journey!.inbound.map(item => ({ ...item, destination: "MAN" })),
    } };
    saveBrowserBrief("example", { brief: { ...originalBrief, departure_city: "Manchester" } });
    state.snapshot = {
      ...snapshot, origin_city: "Liverpool", phase: "researching", itinerary: null, preview_trip: null,
      recovery_links: recoveryLinks,
    };
    render(<PlanExperience />);
    const note = "Flights depart from Manchester. Arrange travel between Liverpool and the departure airport separately.";
    expect(await screen.findByText("Checking flights from Manchester…")).toBeVisible();
    expect(screen.getByRole("status")).not.toHaveClass("sr-only");
    expect(screen.queryByText(note)).not.toBeInTheDocument();

    state.snapshot = { ...state.snapshot, state_revision: 2, alternate_origin_city: "Manchester", preview_trip: alternateTrip };
    window.dispatchEvent(new Event("online"));
    expect(await screen.findByText(note)).toBeVisible();
    expect(screen.getByText("From Liverpool")).toBeVisible();
    expect(screen.getByRole("article", { name: "Provisional itinerary" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "Provider searches" })).not.toBeInTheDocument();

    state.snapshot = { ...state.snapshot, state_revision: 3, phase: "closed", itinerary: { ...itinerary, trips: [alternateTrip] } };
    window.dispatchEvent(new Event("online"));
    expect(await screen.findByText("Checks outstanding")).toBeVisible();
    expect(screen.getByText(note)).toBeVisible();
    expect(screen.queryByRole("region", { name: "Provider searches" })).not.toBeInTheDocument();
  });

  it("shows a checked journey as an incomplete preview while hotels are pending", async () => {
    vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }));
    const outbound = { id: "outbound", kind: "transport", mode: "flight", origin: "Manchester", destination: "Lyon", departs_at: "2026-10-14T08:00:00Z", arrives_at: "2026-10-14T10:00:00Z", operator: "Example Air", service_number: "EA1", quote: null, offer: { evidence: { underlying_source: "Google Flights", provider: "SearchApi" } }, caveats: [] };
    const inbound = { ...outbound, id: "inbound", origin: "Lyon", destination: "Manchester", departs_at: "2026-10-16T08:00:00Z", arrives_at: "2026-10-16T10:00:00Z" };
    state.snapshot = { ...snapshot, phase: "researching", itinerary: null, preview_trip: { fixture: { city: "Lyon", opponent: "Olympique Lyonnais", kickoff_at: "2026-10-15T19:00:00Z", timezone: "Europe/Paris", venue: "Parc Olympique Lyonnais", venue_status: "confirmed" }, journey: { outbound: [outbound], inbound: [inbound] }, stay: null } as unknown as NonNullable<Snapshot["preview_trip"]> };
    render(<PlanExperience />);
    expect(await screen.findByRole("article", { name: "Journey found while planning continues" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Olympique Lyonnais v Palace" })).toBeVisible();
    const match = screen.getByRole("heading", { name: "Olympique Lyonnais v Crystal Palace" });
    const checking = screen.getByRole("heading", { name: "Finding your stay" });
    const returning = screen.getByRole("heading", { name: /Lyon.*Manchester/ });
    expect(match).toBeVisible();
    expect(screen.getByRole("link", { name: /Buy away tickets/ })).toHaveAttribute("href", "https://www.eticketing.co.uk/cpfc/Events?preFilter=9&preFilterName=3.+Away+Matches+%2526+Coach+Travel&utm_source=website&utm_medium=referral&utm_campaign=WebStoryTickets021026");
    expect(checking).toBeVisible();
    expect(returning).toBeVisible();
    expect(checking.compareDocumentPosition(match) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(match.compareDocumentPosition(returning) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText(/Comparing routes from/)).not.toBeInTheDocument();
  });

  it("goes from the in-progress preview straight to the itinerary without replaying the waiting card", async () => {
    vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }));
    state.snapshot = { ...snapshot, phase: "researching", itinerary: null, preview_trip: partialTrip };
    render(<PlanExperience />);
    expect(await screen.findByRole("article", { name: "Journey found while planning continues" })).toBeVisible();
    await act(() => new Promise(resolve => setTimeout(resolve, 450)));
    state.snapshot = { ...snapshot, state_revision: 2 };
    window.dispatchEvent(new Event("online"));
    await waitFor(() => expect(screen.queryByRole("article", { name: "Journey found while planning continues" })).not.toBeInTheDocument());
    expect(screen.queryByText("Your route is ready.")).not.toBeInTheDocument();
  });

  it("reveals a result on the first faster poll after the waiting hero", async () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0);
    vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: () => {}, removeEventListener: () => {} }));
    state.snapshot = { ...snapshot, phase: "researching", itinerary: null, progress_message: "Comparing transport and accommodation…" };
    render(<PlanExperience />);
    await act(() => vi.advanceTimersByTimeAsync(400));
    expect(screen.getByText("Comparing routes from London…")).toBeVisible();
    state.snapshot = { ...snapshot, itinerary, state_revision: 2 };
    await act(() => vi.advanceTimersByTimeAsync(600));
    expect(screen.getByRole("heading", { name: /FC Red Bull Salzburg v Palace/ })).toBeVisible();
    expect(screen.queryByText("Your route is ready.")).not.toBeInTheDocument();
  });

  it("sets aside an incompatible stay when a provisional itinerary becomes available", async () => {
    vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }));
    const earlier = { ...partialTrip, journey: null, stay: { ...hotel, id: "earlier-hotel", property_name: "Earlier Hotel", check_in: "2027-01-27" } };
    state.snapshot = { ...snapshot, phase: "researching", itinerary: null, preview_trip: matchingTrip, preview_options: [earlier] };
    render(<PlanExperience />);
    const preview = await screen.findByRole("article", { name: "Provisional itinerary" });
    expect(within(preview).getByRole("heading", { name: "Salzburg Hotel" })).toBeVisible();
    expect(within(preview).getByRole("heading", { name: "Comparing options" })).toBeVisible();
    expect(within(preview).getByText(/Confirm dates, current prices and availability/)).toBeVisible();
    expect(within(preview).getByText(/Links open a fresh search/)).toBeVisible();
    expect(within(preview).queryByText("Earlier Hotel")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Earlier stay option" }));
    expect(screen.getByRole("heading", { name: "Earlier Hotel" })).toBeVisible();

    const earlierPair = { ...matchingTrip, stay: { ...hotel, id: "alternative-hotel", property_name: "Alternative Hotel" } };
    state.snapshot = { ...snapshot, state_revision: 2, itinerary, preview_options: [earlier, earlierPair, matchingTrip] };
    window.dispatchEvent(new Event("online"));
    expect(await screen.findByText("Checks outstanding")).toBeVisible();
    expect(screen.getByRole("button", { name: "Earlier stay option" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Earlier matching trip" }));
    expect(screen.getByRole("heading", { name: "Alternative Hotel" })).toBeVisible();
  });

  it.each([
    "Session not found or access expired",
    "This search was interrupted. Please start it again.",
  ])("shows the API's terminal message and stops polling: %s", async message => {
    vi.useFakeTimers();
    saveBrowserBrief("example", { brief: { fixture_ids: ["salzburg"], origin_city: "Manchester", travellers: { adults: 2, rooms: 1, child_ages: [] }, budget_tier: "value", flexibility: "tight", windows: [], private_room: false, private_bathroom: false } });
    vi.mocked(request).mockRejectedValueOnce(new ApiError(404, message));
    render(<PlanExperience />);
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(screen.getByRole("status")).toHaveTextContent(message);
    expect(screen.getByRole("link", { name: /Edit trip brief/ })).toHaveAttribute("href", "/?edit=example");
    window.dispatchEvent(new Event("online"));
    await act(() => vi.advanceTimersByTimeAsync(30000));
    expect(request).toHaveBeenCalledTimes(1);
  });
});
