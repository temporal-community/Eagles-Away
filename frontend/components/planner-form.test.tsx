import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createAgent } from "@ui-sdk/react/agent";
import { PlannerForm } from "./planner-form";
import { request, type Brief } from "../lib/api";
import { readBrowserBrief, saveBrowserBrief } from "../lib/browser-brief";
import formCatalog from "../lib/form-catalog.json";

const { router } = vi.hoisted(() => ({ router: { push: vi.fn(), prefetch: vi.fn() } }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));
vi.mock("../lib/api", async importOriginal => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  request: vi.fn(async (path: string) => path === "/api/sessions"
    ? { public_session_id: "planned-session" }
    : { ...(await import("../lib/form-catalog.json")).default, development_mode: false }),
}));

const agent = createAgent();
const liverpoolBrief: Brief = {
  fixture_ids: ["uel-2026-salzburg-away"], origin_city: "Liverpool", travellers: { adults: 2, rooms: 1, child_ages: [] },
  budget_tier: "comfort", flexibility: "day_either_side", private_room: true, private_bathroom: true, transport_mode: "flight",
  windows: [{ fixture_id: "uel-2026-salzburg-away", earliest_departure: "2027-01-27T00:00:00+00:00", latest_return: "2027-01-29T23:59:00+00:00" }],
};

function chooseOrigin(city: string) {
  fireEvent.click(screen.getByRole("combobox", { name: "Starting from" }));
  fireEvent.click(screen.getByRole("option", { name: city }));
}

function travelBy(name: "Compare" | "Flights" | "Trains") {
  return within(screen.getByRole("radiogroup", { name: "Travel by" })).getByRole("radio", { name });
}

async function submittedBody() {
  fireEvent.click(screen.getByRole("button", { name: /Plan my away day/ }));
  await waitFor(() => expect(vi.mocked(request).mock.calls.some(([path]) => path === "/api/sessions")).toBe(true));
  const submission = vi.mocked(request).mock.calls.find(([path]) => path === "/api/sessions");
  return JSON.parse(String(submission?.[1]?.body));
}

describe("trip brief steps", () => {
  beforeEach(() => { vi.useFakeTimers({ toFake: ["Date"] }); vi.setSystemTime(new Date("2026-10-05T12:00:00Z")); });
  afterEach(() => { cleanup(); vi.clearAllMocks(); vi.restoreAllMocks(); vi.useRealTimers(); window.history.replaceState(null, "", "/"); sessionStorage.clear(); });

  it("shows all city and match choices before the catalog request completes", async () => {
    vi.mocked(request).mockImplementationOnce(() => new Promise<never>(() => {}));
    render(<PlannerForm />);
    expect(screen.getByRole("combobox", { name: "Starting from" })).toHaveTextContent("London");
    expect(screen.getByRole("radio", { name: /Olympique Lyonnais/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /FC Red Bull Salzburg/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Buy away tickets/ })).toHaveAttribute("href", "https://www.eticketing.co.uk/cpfc/Events?preFilter=9&preFilterName=3.+Away+Matches+%2526+Coach+Travel&utm_source=website&utm_medium=referral&utm_campaign=WebStoryTickets021026");
    fireEvent.click(screen.getByRole("combobox", { name: "Starting from" }));
    expect(screen.getByRole("option", { name: "Manchester" })).toBeInTheDocument();
    await act(async () => { await Promise.resolve(); });
  });

  it("keeps a match chosen while the catalog request is pending", async () => {
    let finishRequest!: () => void;
    vi.mocked(request).mockImplementationOnce(async () => {
      await new Promise<void>(resolve => { finishRequest = resolve; });
      return { ...formCatalog, development_mode: false } as never;
    });
    render(<PlannerForm />);
    fireEvent.click(screen.getByRole("radio", { name: /Beşiktaş J.K./ }));
    await act(async () => { finishRequest(); });
    expect(screen.getByRole("radio", { name: /Beşiktaş J.K./ })).toBeChecked();
  });

  it("keeps the same major-airport city list for every match", async () => {
    render(<PlannerForm />);
    await screen.findByRole("radio", { name: /Olympique Lyonnais/ });
    chooseOrigin("Manchester");
    fireEvent.click(screen.getByRole("radio", { name: /Beşiktaş J.K./ }));
    fireEvent.click(screen.getByRole("radio", { name: /Jagiellonia Białystok/ }));
    const origin = screen.getByRole("combobox", { name: "Starting from" });
    expect(origin).toHaveTextContent("Manchester");
    fireEvent.click(origin);
    expect(screen.getByRole("option", { name: "Bristol" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Bournemouth" })).not.toBeInTheDocument();
  });

  it("keeps existing trip choices when moving forward and back", async () => {
    render(<PlannerForm />);
    await screen.findByRole("radio", { name: /Olympique Lyonnais/ });
    fireEvent.click(screen.getByRole("button", { name: /Continue to travel preferences/ }));
    expect(screen.getByRole("heading", { name: "How do you like to travel?" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "More adults" }));
    fireEvent.click(screen.getByRole("button", { name: /Back/ }));
    expect(screen.getByRole("heading", { name: "Pick your match" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Continue to travel preferences/ }));
    await waitFor(() => expect(screen.getByLabelText("Adults")).toHaveValue("2"));
  });

  it("defaults to London and offers trains from a selected UK city", async () => {
    render(<PlannerForm />);
    await screen.findByRole("radio", { name: /Olympique Lyonnais/ });
    chooseOrigin("Manchester");
    fireEvent.click(screen.getByRole("button", { name: /Continue to travel preferences/ }));
    expect(travelBy("Compare")).toBeChecked();
    expect(travelBy("Trains")).toBeEnabled();
  });

  it("explains when a Lyon departure city has no reviewed train route", async () => {
    render(<PlannerForm />);
    await screen.findByRole("radio", { name: /Olympique Lyonnais/ });
    chooseOrigin("Belfast");
    fireEvent.click(screen.getByRole("button", { name: /Continue to travel preferences/ }));
    expect(travelBy("Flights")).toBeChecked();
    expect(travelBy("Trains")).toBeDisabled();
    expect(travelBy("Compare")).toBeDisabled();
    expect(screen.getByText(/No reviewed train route from Belfast to Lyon/)).toBeVisible();
  });

  it("submits the selected city and rail mode in the brief", async () => {
    render(<PlannerForm />);
    await screen.findByRole("radio", { name: /Olympique Lyonnais/ });
    chooseOrigin("Manchester");
    fireEvent.click(screen.getByRole("button", { name: /Continue to travel preferences/ }));
    fireEvent.click(travelBy("Trains"));
    const body = await submittedBody();
    expect(body.brief).toMatchObject({ origin_city: "Manchester", transport_mode: "rail" });
    expect(router.prefetch).toHaveBeenCalledWith("/plan/");
    expect(router.push).toHaveBeenCalledWith("/plan/?session=planned-session");
    expect(readBrowserBrief("planned-session")?.brief).toEqual(body.brief);
  });

  it("restores the earlier choices without submitting a new search", async () => {
    saveBrowserBrief("original", { brief: {
      fixture_ids: ["uel-2026-lyon-away"], origin_city: "Manchester", travellers: { adults: 2, rooms: 1, child_ages: [] },
      budget_tier: "comfort", flexibility: "day_either_side", private_room: true, private_bathroom: true, transport_mode: "rail",
      windows: [{ fixture_id: "uel-2026-lyon-away", earliest_departure: "2026-10-14T00:00:00+01:00", latest_return: "2026-10-16T23:59:00+01:00" }],
    } });
    window.history.replaceState(null, "", "/?edit=original");
    render(<PlannerForm />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Starting from" })).toHaveTextContent("Manchester"));
    expect(screen.getByRole("radio", { name: /^Custom/ })).toHaveTextContent("14–16 Oct");
    fireEvent.click(screen.getByRole("button", { name: /Continue to travel preferences/ }));
    expect(screen.getByLabelText("Adults")).toHaveValue("2");
    expect(travelBy("Trains")).toBeChecked();
    expect(screen.getByRole("radio", { name: /Comfort/ })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Private room" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Private bathroom" })).toBeChecked();
    expect(vi.mocked(request).mock.calls.some(([path]) => path === "/api/sessions")).toBe(false);
  });

  it("keeps the recovery airport separate from the starting city when editing", async () => {
    const recoveredBrief = { ...liverpoolBrief, departure_city: "Manchester" };
    saveBrowserBrief("original", { brief: recoveredBrief });
    window.history.replaceState(null, "", "/?edit=original");
    render(<PlannerForm />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Starting from" })).toHaveTextContent("Liverpool"));
    expect(screen.getByRole("radio", { name: /FC Red Bull Salzburg/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /^Custom/ })).toHaveTextContent("27–29 Jan");
    fireEvent.click(screen.getByRole("button", { name: /Continue to travel preferences/ }));
    expect(screen.getByText(/Searching Manchester airport/)).toBeVisible();
    fireEvent.click(screen.getByRole("switch", { name: "Private bathroom" }));
    expect(vi.mocked(request).mock.calls.some(([path]) => path === "/api/sessions")).toBe(false);
    expect((await submittedBody()).brief).toEqual({ ...recoveredBrief, private_bathroom: false });
  });

  it.each(["starting city", "travel mode"] as const)("drops the recovery airport on a change of %s", async change => {
    saveBrowserBrief("original", { brief: {
      ...liverpoolBrief, fixture_ids: ["uel-2026-lyon-away"], departure_city: "Manchester", windows: [],
    } });
    window.history.replaceState(null, "", "/?edit=original");
    render(<PlannerForm />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Starting from" })).toHaveTextContent("Liverpool"));
    if (change === "starting city") chooseOrigin("Belfast");
    fireEvent.click(screen.getByRole("button", { name: /Continue to travel preferences/ }));
    if (change === "travel mode") fireEvent.click(travelBy("Trains"));
    const { brief } = await submittedBody();
    expect(brief).not.toHaveProperty("departure_city");
    expect(brief).toMatchObject(change === "starting city"
      ? { origin_city: "Belfast", transport_mode: "flight" }
      : { origin_city: "Liverpool", transport_mode: "rail" });
  });

  it("deduplicates a pending submit and reuses its token when the same brief is retried", async () => {
    let fail!: (error: Error) => void;
    vi.mocked(request).mockResolvedValueOnce({ ...formCatalog, development_mode: false }).mockImplementationOnce(() => new Promise((_, reject) => { fail = reject; }));
    render(<PlannerForm />);
    fireEvent.click(screen.getByRole("button", { name: /Continue to travel preferences/ }));
    const form = screen.getByRole("button", { name: /Plan my away day/ }).closest("form")!;
    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(vi.mocked(request).mock.calls.filter(([path]) => path === "/api/sessions")).toHaveLength(1);
    await act(async () => fail(new Error("Connection interrupted. Please retry.")));
    fireEvent.submit(form);
    await waitFor(() => expect(router.push).toHaveBeenCalledWith("/plan/?session=planned-session"));
    const submissions = vi.mocked(request).mock.calls.filter(([path]) => path === "/api/sessions");
    expect(submissions).toHaveLength(2);
    expect(submissions[1][1]).toEqual(submissions[0][1]);
  });

  it("labels each trip length with the dates searched for the chosen match", async () => {
    render(<PlannerForm />);
    await screen.findByRole("radio", { name: /Olympique Lyonnais/ });
    const lengths = () => within(screen.getByRole("radiogroup", { name: "Trip length" }));
    expect(lengths().getByRole("radio", { name: /^\+1 day/ })).toHaveTextContent("14–16 Oct");
    expect(lengths().getByRole("radio", { name: /^\+2 days/ })).toHaveTextContent("13–17 Oct");
    expect(screen.getByText("Out Wed 14 Oct, home Fri 16 Oct", { selector: "[data-slot=field-description]" })).toBeVisible();
    fireEvent.click(screen.getByRole("radio", { name: /FC Red Bull Salzburg/ }));
    expect(lengths().getByRole("radio", { name: /^\+2 days/ })).toHaveTextContent("26–30 Jan");
    fireEvent.click(lengths().getByRole("radio", { name: /^Quick/ }));
    expect(screen.getByText("One night: Wed 27 Jan → Thu 28 Jan, or Thu 28 Jan → Fri 29 Jan", { selector: "[data-slot=field-description]" })).toBeVisible();
  });

  it("submits custom dates only when they include match day", async () => {
    render(<PlannerForm />);
    await screen.findByRole("radio", { name: /Olympique Lyonnais/ });
    fireEvent.click(within(screen.getByRole("radiogroup", { name: "Trip length" })).getByRole("radio", { name: /^Custom/ }));
    const pick = (first: string, last: string) => {
      fireEvent.click(screen.getByRole("button", { name: "Open calendar" }));
      fireEvent.click(screen.getByRole("gridcell", { name: new RegExp(first) }));
      fireEvent.click(screen.getByRole("gridcell", { name: new RegExp(last) }));
    };
    const next = () => fireEvent.click(screen.getByRole("button", { name: /Continue to travel preferences/ }));
    next();
    expect(screen.getByRole("alert")).toHaveTextContent("Pick the day you leave and the day you come home.");
    pick("16 October 2026", "18 October 2026");
    next();
    expect(screen.getByRole("alert")).toHaveTextContent("Your dates need to include match day, Thu 15 Oct.");
    pick("14 October 2026", "16 October 2026");
    expect(within(screen.getByRole("radiogroup", { name: "Trip length" })).getByRole("radio", { name: /^Custom/ })).toHaveTextContent("14–16 Oct");
    next();
    fireEvent.click(screen.getByRole("switch", { name: "Private room" }));
    fireEvent.click(screen.getByRole("radio", { name: /Comfort/ }));
    const body = await submittedBody();
    expect(body.brief).toMatchObject({
      flexibility: "day_either_side",
      budget_tier: "comfort",
      private_room: true,
      private_bathroom: false,
      windows: [{ fixture_id: "uel-2026-lyon-away", earliest_departure: "2026-10-14T00:00:00+01:00", latest_return: "2026-10-16T23:59:00+01:00" }],
    });
  });

  it("offers one match at a time and submits only the current choice", async () => {
    render(<PlannerForm />);
    const lyon = await screen.findByRole("radio", { name: /Olympique Lyonnais/ });
    const besiktas = screen.getByRole("radio", { name: /Beşiktaş J.K./ });
    expect(lyon).toBeChecked();
    fireEvent.click(besiktas);
    expect(besiktas).toBeChecked();
    expect(lyon).not.toBeChecked();
    expect(within(screen.getByRole("radiogroup", { name: "Away match" })).getAllByRole("radio")).toHaveLength(4);
    fireEvent.click(screen.getByRole("button", { name: /Continue to travel preferences/ }));
    expect(travelBy("Flights")).toBeChecked();
    expect(travelBy("Trains")).toBeDisabled();
    expect(screen.getByText(/Trains are available for Lyon only/)).toBeVisible();
    expect((await submittedBody()).brief).toMatchObject({ fixture_ids: ["uel-2026-besiktas-away"], transport_mode: "flight" });
  });

  it("lets an agent find and change the match settings", async () => {
    render(<PlannerForm />);
    await screen.findByRole("radio", { name: /Olympique Lyonnais/ });
    const components = agent.snapshot().filter(entry => !entry.offPage && entry.component !== "more");
    for (const entry of components) expect(entry.label, `${entry.component} ${entry.ref} has no name`).toBeTruthy();
    const labels = components.map(entry => entry.label);
    for (const label of ["Starting from", "Away match", "Trip length"]) expect(labels, `${label} is missing`).toContain(label);
    const result = await agent.act({ steps: [
      { ref: "Starting from", action: "setValue", args: ["Glasgow"] },
      { ref: "Trip length", action: "setValue", args: ["tight"] },
    ] });
    expect(result.ok, result.error?.message).toBe(true);
    expect(screen.getByRole("combobox", { name: "Starting from" })).toHaveTextContent("Glasgow");
    expect(within(screen.getByRole("radiogroup", { name: "Trip length" })).getByRole("radio", { name: /^Quick/ })).toBeChecked();
  });
});
