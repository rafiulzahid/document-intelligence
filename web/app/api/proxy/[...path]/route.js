// Server-side proxy: the browser calls /api/proxy/..., this forwards to the FastAPI backend and adds
// the X-API-Key header, so the key is never exposed to the browser (and no CORS setup is needed).
export const dynamic = "force-dynamic";

const API_URL = (process.env.API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const API_KEY = process.env.API_KEY || "";

async function forward(request, { params }) {
  const { path } = await params;
  const url = new URL(request.url);
  const target = `${API_URL}/api/${path.map(encodeURIComponent).join("/")}${url.search}`;
  const headers = {};
  const ct = request.headers.get("content-type");
  if (ct) headers["content-type"] = ct;
  if (API_KEY) headers["x-api-key"] = API_KEY;
  const init = { method: request.method, headers, cache: "no-store" };
  if (!["GET", "HEAD"].includes(request.method)) {
    init.body = request.body;
    init.duplex = "half";
  }
  let upstream;
  try {
    upstream = await fetch(target, init);
  } catch (e) {
    return Response.json({ detail: `Backend not reachable at ${API_URL} (${e.message}). Is uvicorn running?` },
                         { status: 502 });
  }
  const out = new Headers();
  for (const h of ["content-type", "content-disposition", "content-length"]) {
    const v = upstream.headers.get(h);
    if (v) out.set(h, v);
  }
  return new Response(upstream.status === 204 ? null : upstream.body, { status: upstream.status, headers: out });
}

export const GET = forward;
export const POST = forward;
export const PATCH = forward;
export const DELETE = forward;
