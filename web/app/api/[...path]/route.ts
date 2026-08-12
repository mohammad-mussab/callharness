/**
 * Server-side proxy to the CallHarness API.
 *
 * WHY THIS EXISTS RATHER THAN A next.config REWRITE
 * Two reasons, both learned the hard way.
 *
 * 1. Auth. Write endpoints require CALLHARNESS_API_KEY once it is set, and a
 *    plain rewrite cannot attach a header. The dashboard had no way to send one,
 *    so enabling auth silently broke every write in the UI — translate,
 *    re-analyze, Analysis Settings, alert rules, evaluators, delete — all 401,
 *    surfaced to the user as unrelated errors like "check that the server has an
 *    LLM key configured". The key is read here, on the server, and never reaches
 *    the browser.
 *
 * 2. Runtime config. `next build` resolves rewrites() once and bakes the result
 *    into the routes manifest, so CALLHARNESS_API_URL had to be correct at image
 *    build time. A route handler reads process.env per request, so the same image
 *    works against any backend.
 *
 * Range requests are forwarded verbatim and the upstream body is streamed, which
 * is what lets the waveform player seek within a recording.
 */

const API = process.env.CALLHARNESS_API_URL || "http://127.0.0.1:8010";
const API_KEY = process.env.CALLHARNESS_API_KEY || "";

// Hop-by-hop and length headers must not be copied through: the body may be
// re-encoded, so a stale content-length or content-encoding corrupts the response.
const STRIP_RESPONSE = new Set([
  "content-encoding",
  "content-length",
  "transfer-encoding",
  "connection",
]);

async function proxy(req: Request, path: string[]) {
  const search = new URL(req.url).search;
  const target = `${API}/api/${path.join("/")}${search}`;

  const headers = new Headers();
  const contentType = req.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  // Seeking in the audio player depends on this reaching the backend.
  const range = req.headers.get("range");
  if (range) headers.set("range", range);
  if (API_KEY) headers.set("x-api-key", API_KEY);

  const method = req.method;
  const body =
    method === "GET" || method === "HEAD" ? undefined : await req.arrayBuffer();

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method,
      headers,
      body,
      cache: "no-store",
      // @ts-expect-error - undici option, required to stream a request body
      duplex: body ? "half" : undefined,
    });
  } catch (err) {
    // The backend being unreachable is an infrastructure fault, not a 404 —
    // say so plainly rather than letting it surface as a generic UI failure.
    return Response.json(
      { detail: `Cannot reach CallHarness API at ${API}: ${String(err)}` },
      { status: 502 }
    );
  }

  const out = new Headers();
  upstream.headers.forEach((v, k) => {
    if (!STRIP_RESPONSE.has(k.toLowerCase())) out.set(k, v);
  });

  return new Response(upstream.body, { status: upstream.status, headers: out });
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(req: Request, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}
export async function POST(req: Request, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}
export async function PUT(req: Request, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}
export async function PATCH(req: Request, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}
export async function DELETE(req: Request, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}
export async function HEAD(req: Request, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}

// Recordings are large and analysis results change; nothing here should be cached.
export const dynamic = "force-dynamic";
