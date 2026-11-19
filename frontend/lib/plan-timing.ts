type Milestone = "accepted" | "first-option" | "matching-trip" | "final" | "explained" | "next-step";

let submission: { start: number; session?: string; recorded: Set<Milestone> } | undefined;

export function markPlanSubmit(): void {
  submission = { start: performance.now(), recorded: new Set() };
  performance.mark?.("eagles-away:submit", { startTime: submission.start });
}

export function markPlanAccepted(session: string): void {
  if (!submission) return;
  submission.session = session;
  markPlanMilestone(session, "accepted");
}

export function markPlanMilestone(session: string, milestone: Milestone): void {
  if (!submission || submission.session !== session || submission.recorded.has(milestone)) return;
  submission.recorded.add(milestone);
  const end = performance.now();
  performance.mark?.(`eagles-away:${milestone}`, { startTime: end });
  performance.measure?.(`eagles-away:submit-to-${milestone}`, { start: submission.start, end });
}
