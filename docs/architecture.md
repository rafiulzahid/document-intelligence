# Architecture

## Components

```mermaid
flowchart LR
    U[Browser] -->|HTTP| W[Next.js web app<br/>pages + server-side proxy]
    W -->|"/api/* + X-API-Key"| A[FastAPI API]
    A -->|store file| S[(Upload storage<br/>random names)]
    A -->|rows| P[(PostgreSQL 16<br/>+ pgvector + pg_trgm)]
    A -->|enqueue job| R[(Redis)]
    R --> K1[RQ worker 1]
    R --> K2[RQ worker N]
    K1 & K2 -->|read file| S
    K1 & K2 -->|"docintel.process"| D[docintel library]
    D -->|OCR| T[Tesseract / EasyOCR]
    D -->|extract JSON| L[LLM via Ollama<br/>qwen2.5 7B, GPU]
    K1 & K2 -->|embeddings| E[nomic-embed-text<br/>via Ollama]
    K1 & K2 -->|results, issues, chunks, vectors| P
```

The API never does heavy work in the request: an upload is stored, recorded and queued, and returns immediately.
Workers are stateless, so throughput scales by starting more of them.

## Processing pipeline (one document)

```mermaid
flowchart TD
    F[file] --> DT{detect type by content}
    DT -->|pdf| PDF[text layer per page]
    PDF -->|page has no text| OCR[render 300 DPI + OCR]
    DT -->|image / tiff| PRE[grayscale · upscale · deskew · denoise] --> OCR
    DT -->|xlsx / xls / csv| SH[read all sheets<br/>encoding + delimiter detection]
    DT -->|empty / corrupt / encrypted| ERR[failed + error code]
    PDF --> SEG[split into invoices<br/>by invoice number per page]
    OCR --> SEG
    SH --> MAP{clear table structure?}
    MAP -->|yes| TM[deterministic table mapping]
    MAP -->|no| SEG
    SEG --> LLM[LLM: value + evidence + confidence]
    LLM --> GR[grounding: discard values not in the text<br/>normalise dates, amounts, currency]
    GR --> FILL[fill empty fields from rules baseline]
    TM --> VAL
    FILL --> VAL[validation rules]
    VAL --> DUP[duplicate check against the database]
    DUP --> ST[(store pages, invoices, line items,<br/>issues, keys, chunks + embeddings)]
    ST --> STATUS[valid / needs_review / no_invoice]
```

## Technology choices

| Layer | Choice | Why (vs. the suggested stack) |
|---|---|---|
| Backend | Python FastAPI | OCR, PDF and LLM tooling are Python-native, so one language covers the pipeline and the API; Pydantic schemas and OpenAPI docs come for free (instead of Node.js/NestJS). |
| Queue | Redis + RQ | The Python equivalent of BullMQ: retries, failure registry, horizontally scalable workers. A thread mode is kept for setups without Redis. |
| Database | PostgreSQL 16 + pgvector + pg_trgm | One store for relational data, full-text search, fuzzy name search and vectors; no separate vector database to operate. |
| OCR | Tesseract 5 (EasyOCR optional on GPU) | Robust, easy to install; the OCR backend is pluggable. |
| LLM | Ollama (qwen2.5 7B, 8k context) via an OpenAI-compatible client | Runs locally on the GPU (private, no per-call cost); the same client works with OpenAI, Gemini, vLLM or Anthropic by changing `.env`. |
| Embeddings | nomic-embed-text via Ollama | Local; pluggable (sentence-transformers or a hash fallback). |
| Frontend | Next.js 15 (App Router) | As suggested; a server-side route handler proxies the API so the API key stays on the server. |
| Deployment | Docker Compose | Five services; Ollama stays on the host to use the GPU directly. |

## Failure handling

- Ingestion converts every problem into an error code; a worker never crashes on a bad file.
- A job that raises an unexpected exception marks the document `failed` with `INTERNAL_ERROR` and keeps the trace in
  the processing events. RQ keeps failed jobs, and `POST /api/documents/{id}/reprocess` re-queues any document.
- If the LLM is unreachable or returns invalid JSON (after retries), the rules baseline is used and a warning is stored.
- If embeddings fail, the document is still stored and keyword search still works.
