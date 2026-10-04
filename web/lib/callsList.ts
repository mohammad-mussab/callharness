// The Calls list keeps its filters in the URL and mirrors that query string
// here, so the detail page's "← Calls" link returns to the same filters and
// page instead of an unfiltered page 1. Lives outside app/calls/page.tsx
// because a Next.js page file may only export the page itself.
export const CALLS_LIST_QUERY_KEY = "callharness.callsListQuery";

export function callsListHref(): string {
  try {
    const qs = sessionStorage.getItem(CALLS_LIST_QUERY_KEY);
    return qs ? `/calls?${qs}` : "/calls";
  } catch {
    return "/calls";
  }
}
