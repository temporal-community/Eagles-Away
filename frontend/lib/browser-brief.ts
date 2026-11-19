import type { Brief } from "./api";

type SavedBrief = { brief: Brief; city?: string };

export function saveBrowserBrief(session: string, value: SavedBrief): void {
  try { sessionStorage.setItem(`cpfc-brief-${session}`, JSON.stringify(value)); } catch { /* Storage can be unavailable. */ }
}

export function readBrowserBrief(session: string): SavedBrief | null {
  try {
    const value = sessionStorage.getItem(`cpfc-brief-${session}`);
    const saved = value ? JSON.parse(value) as SavedBrief : null;
    return saved?.brief?.fixture_ids?.[0] && saved.brief.travellers ? saved : null;
  } catch { return null; }
}
