# Frontend (step 7) — Next.js 15

Pages: **Upload & jobs** (drag-and-drop multi-upload, live progress, reprocess, delete) · **Document detail**
(page preview next to the extracted fields, field status + evidence, inline correction of fields and line items,
validation issues, correction history, processing history) · **Review queue** (invoices that need review) ·
**Search** (keyword / semantic / hybrid + filters) · **Cross-verify** (documents vs. ledger, mismatch table).

The browser talks only to `/api/proxy/...` (a Next.js route handler). The proxy forwards to the FastAPI backend and
adds the `X-API-Key` header on the server, so the key never reaches the browser and no CORS setup is needed.

## Run (development)
    cd web
    cp .env.local.example .env.local      # API_URL=http://127.0.0.1:<api port>, API_KEY=<same as backend .env>
    npm install
    PORT=3000 npm run dev                 # pick a free port if 3000 is taken
Open http://localhost:3000

## Run (production build)
    npm run build && PORT=3000 npm start

Needs Node.js 18.18+ (tested with Node 22).
