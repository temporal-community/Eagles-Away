import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { Itinerary, Trip } from "../lib/api";
import { ItineraryView } from "./itinerary-view";

const railNote = "Change between Warsaw Chopin Airport and Warsaw Central separately. Rail fares are not included.";
const quote = { amount: { amount: "219", currency: "GBP" }, scope: "round_trip", unit: "party", taxes: "unknown", additional_taxes: null, caveats: ["Rounded planning price; baggage may be extra."], observed_at: "2026-11-18T10:00:00Z" };
const flight = { id: "out", kind: "transport", mode: "flight", origin: "LHR", destination: "WAW", departs_at: "2026-12-09T06:30:00+00:00", arrives_at: "2026-12-09T10:00:00+01:00", operator: "LOT", service_number: "LO 286", quote, offer: { booking_url: "https://www.google.com/travel/flights", link_kind: "contextual_search", evidence: { underlying_source: "Google Flights", provider: "SearchApi" } }, caveats: ["Select and confirm this itinerary on Google Flights."] };
const trip = {
  fixture: { id: "bialystok", city: "Białystok", opponent: "Jagiellonia Białystok", kickoff_at: "2026-12-10T17:45:00Z", timezone: "Europe/Warsaw", venue: "Bialystok City Stadium", venue_status: "provisional" },
  journey: { outbound: [flight], inbound: [{ ...flight, id: "back", origin: "WAW", destination: "LHR", departs_at: "2026-12-11T11:00:00+01:00", arrives_at: "2026-12-11T13:00:00+00:00" }] },
  stay: null,
  known_total: null,
  price_coverage: 0.3,
  feasibility: "needs_checks",
  summary: "",
  gaps: [railNote, "Confirm late check-in and post-match access to the accommodation."],
  checks: [
    { headline: "Airport → Warsaw Central → Białystok train", detail: railNote },
    { headline: "Late check-in & post-match access", detail: "Confirm late check-in and post-match access to the accommodation." },
  ],
  transfers: [{ title: "Warsaw and Białystok rail connection", summary: "Warsaw → Białystok by rail", description: railNote, source_url: "https://www.skm.warszawa.pl/en/faq-2/", reviewed_at: "2026-10-05T00:00:00Z" }],
  maps_transfers: [{ title: "London centre to departure point", summary: "London centre → LHR Airport", description: "Start from London city centre and check travel to LHR Airport.", url: "https://www.google.com/maps/dir/?api=1" }],
  alternatives: [],
} as unknown as Trip;
const itinerary = { generated_at: "2026-11-18T10:00:00Z", trips: [trip], caveats: [], ranking_policy_version: "1" } as unknown as Itinerary;

describe("itinerary view", () => {
  afterEach(cleanup);

  it("puts short checks and transfer links under the summary, with full text one tap away", () => {
    render(<ItineraryView itinerary={itinerary} />);
    const card = screen.getByRole("region", { name: /Before you book/ });
    expect(within(card).getByText("2 checks")).toBeVisible();
    expect(screen.getByText("Links open a fresh search. You may need to reselect.")).toBeVisible();
    expect(screen.getAllByText("Getting around")).toHaveLength(1);
    expect(within(card).getByText("Airport → Warsaw Central → Białystok train")).toBeVisible();
    expect(within(card).getByRole("link", { name: "Warsaw and Białystok rail connection: Official guidance" })).toHaveAttribute("href", "https://www.skm.warszawa.pl/en/faq-2/");
    expect(within(card).getByRole("link", { name: /London centre to departure point/ })).toHaveTextContent("London centre → LHR Airport");
    // It comes before the itinerary rather than after it.
    expect(card.compareDocumentPosition(screen.getByRole("list", { name: "Itinerary" })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    fireEvent.click(within(card).getByRole("button", { name: /Details/ }));
    // The rail note is both a check and a transfer description; it is shown once.
    expect(within(card).getAllByText(railNote)).toHaveLength(1);
    expect(within(card).getByText("Start from London city centre and check travel to LHR Airport.")).toBeVisible();
  });

  it("folds each row's caveats into one expandable checks pill", () => {
    render(<ItineraryView itinerary={itinerary} />);
    expect(screen.queryByText("Taxes not confirmed")).toBeNull();
    const [pill] = screen.getAllByRole("button", { name: "4 checks for LHR to WAW" });
    expect(pill).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(pill);
    expect(pill).toHaveAttribute("aria-expanded", "true");
    for (const text of ["Taxes not confirmed", "Rounded planning price; baggage may be extra.", "Select and confirm this itinerary on Google Flights.", "The link opens a fresh search; you may need to reselect this option."]) {
      expect(screen.getAllByText(text)[0]).toBeVisible();
    }
  });

  it("falls back to the first sentence of each gap for trips saved before headlines existed", () => {
    render(<ItineraryView itinerary={{ ...itinerary, trips: [{ ...trip, checks: [], transfers: [], maps_transfers: [] }] }} />);
    const card = screen.getByRole("region", { name: /Before you book/ });
    expect(within(card).getByText("Change between Warsaw Chopin Airport and Warsaw Central separately")).toBeVisible();
  });
});
