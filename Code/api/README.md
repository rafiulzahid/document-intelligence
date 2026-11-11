# Backend API (step 6)

FastAPI + SQLAlchemy (PostgreSQL with pgvector/pg_trgm, or SQLite) + Redis/RQ background worker.
Processing uses the `docintel` package: ingest -> extract -> validate -> store.

## Run
| Mode | Commands |
|---|---|
| Recommended for development | `docker compose up -d postgres redis` · then in two terminals (inside `Code/`, venv active): `uvicorn api.main:app --reload --port 8000` and `rq worker docintel --url redis://localhost:6379/0` |
| Full stack in Docker | `docker compose up --build` |
| No setup (SQLite + threads) | `DATABASE_URL=sqlite:///../data/app.db QUEUE_MODE=thread uvicorn api.main:app --port 8000` |

Interactive docs: http://localhost:8000/docs (click "Authorize"-free: send header `X-API-Key`).

## Endpoints
| Method | Path | Purpose |
|---|---|---|
| GET | /api/health | database, extensions, queue, workers, auth (no key needed) |
| GET | /api/stats | documents / invoices by status |
| POST | /api/documents | multi-file upload (`files` form field); stored with random names, size/extension checked, queued |
| GET | /api/documents | list with `status`, `file_type`, `q` (file name), `limit`, `offset` |
| GET | /api/documents/{id} | status, progress %, processing events, timings, errors |
| GET | /api/documents/{id}/results | invoices, fields + status/confidence/evidence, line items, validation issues, corrections (`include_text=true` adds page text) |
| GET | /api/documents/{id}/file | original file (preview) |
| POST | /api/documents/{id}/reprocess | re-queue a failed/edited document |
| DELETE | /api/documents/{id} | delete a document and its results |
| GET | /api/invoices/{id} | one invoice |
| PATCH | /api/invoices/{id} | manual correction `{"fields": {...}, "line_items": [...], "corrected_by": "..."}` -> normalised, audited, re-validated |
| GET | /api/search | `q`, `mode=keyword|semantic|hybrid`, filters `supplier, customer, invoice_number, date_from, date_to, min_total, max_total, currency, status, file_type` |
| POST | /api/cross-verify | `{"left_document_ids": [...], "right_document_ids": [...]}` -> mismatches by invoice number |

## Database tables
documents · pages (page/sheet text, method, OCR confidence) · invoices (fields, field_meta JSON, status) · line_items ·
validation_issues · invoice_keys (duplicate-detection keys) · corrections (audit trail) · processing_events ·
chunks (search text + embedding) · chunk_embeddings (pgvector, HNSW index; created when the extension exists).

## Design notes
* Duplicates are checked against the whole database (file hash; invoice number + party; invoice number + total;
  party + date + total when a number is missing). Only earlier uploads count as originals.
* Search: PostgreSQL full-text (GIN) + trigram fuzzy names for keyword; pgvector cosine (HNSW) for semantic, with a
  Python cosine fallback when pgvector is unavailable; hybrid = reciprocal rank fusion. Structured filters on invoices.
* A failing job never crashes the worker: the document is marked `failed` with an error code and can be reprocessed.
* Security: API key header, CORS allow-list, extension + content-type detection, size limit, random storage names,
  parameterised SQL (SQLAlchemy), secrets only in `.env`.
