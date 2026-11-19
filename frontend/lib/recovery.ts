import type { Brief, Snapshot } from "./api";

export type RecoveryAction = Snapshot["recovery_actions"][number];
type FailureReason = NonNullable<Snapshot["failure_reason"]>;

export function recoveryBrief(brief: Brief, action: RecoveryAction): Brief {
  if (action.kind === "depart_from" && action.departure_city) return { ...brief, departure_city: action.departure_city };
  if (action.kind === "wider_dates") return { ...brief, flexibility: "two_days", windows: [] };
  return { ...brief, private_bathroom: false };
}

export function recoveryTitle(reason: FailureReason, departure: string): string {
  switch (reason) {
    case "no_flights": return `No flights from ${departure} on your dates`;
    case "flight_timing": return `No flights from ${departure} fit the match times`;
    case "flights_unverified": return `We couldn’t confirm flights from ${departure}`;
    case "connections": return `Flights from ${departure} need a connection we can’t check`;
    case "onward_rail": return "Train times to Białystok couldn’t be checked";
    case "stay": return "No stay fits this journey";
    case "private_bathroom": return "No confirmed private bathroom";
    case "dates": return "These dates don’t work for this match";
    case "unavailable": return "Travel search is busy";
    default: return "We couldn’t complete this trip";
  }
}

