import { useRef, useState, useTransition } from "react";
import { trackAnalytics } from "./analytics";
import { request, type Brief } from "./api";
import { saveBrowserBrief } from "./browser-brief";
import { markPlanAccepted, markPlanSubmit } from "./plan-timing";

export function usePlanSubmission(navigate: (href: string) => void, setError: (message: string) => void) {
  const attempt = useRef<{ id: string; token: string; body: string } | null>(null);
  const posting = useRef(false);
  const [starting, setStarting] = useState(false);
  const [opening, openPlan] = useTransition();

  async function startPlan(brief: Brief, city?: string): Promise<void> {
    if (posting.current || opening) return;
    posting.current = true;
    setStarting(true);
    setError("");
    try {
      markPlanSubmit();
      const body = JSON.stringify(brief);
      if (attempt.current?.body !== body) {
        const token = [...crypto.getRandomValues(new Uint8Array(32))].map(byte => byte.toString(16).padStart(2, "0")).join("");
        attempt.current = { id: crypto.randomUUID(), token, body };
      }
      const result = await request<{ public_session_id: string }>("/api/sessions", {
        method: "POST", headers: { "X-Submission-Token": attempt.current.token },
        body: JSON.stringify({ submission_id: attempt.current.id, brief }),
      });
      if (!result?.public_session_id) throw new Error("Your trip could not be opened. Please retry.");
      const session = result.public_session_id;
      markPlanAccepted(session);
      trackAnalytics("trip_started");
      saveBrowserBrief(session, { brief, city });
      openPlan(() => navigate(`/plan/?session=${encodeURIComponent(session)}`));
    } catch (error) {
      setError(error instanceof Error ? error.message : "Please retry.");
    } finally {
      posting.current = false;
      setStarting(false);
    }
  }

  return { startPlan, starting, opening, busy: starting || opening };
}
