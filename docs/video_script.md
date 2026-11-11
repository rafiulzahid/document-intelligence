# Walkthrough video script (6–7 minutes)

**Before recording:**
- Start `docker compose up -d postgres redis`, the API, 2 workers and the web app.
- Delete old documents, or use a fresh database, so the list is short.
- Have these files ready in one folder:
  - `katana/katana_test_000.png`
  - one `northwind_pdf/*.pdf`
  - `spreadsheet/xlsx_multisheet.xlsx`
  - one `spreadsheet/csv_*.csv`
  - `corrupted/truncated.pdf`
  - one `planted_error/*.pdf`
  - a copy of the Northwind PDF (for the duplicate)
  - `cross_verify/northwind_ledger.xlsx`
- Recorder: OBS, or GNOME's built-in recorder (Ctrl+Shift+Alt+R). Record 1080p.

| Time | Show | Say |
|---|---|---|
| 0:00–0:40 | `docs/architecture.md` diagram | The goal; the stack (FastAPI, Postgres + pgvector, Redis/RQ workers, local LLM on the GPU, Next.js); why the API never blocks |
| 0:40–2:00 | Upload page: drop all files at once | Multi-upload, content-based type detection, background processing; watch the statuses change; the corrupted PDF fails with `CORRUPT_FILE` and doesn't crash anything |
| 2:00–3:30 | Open the scanned image, then the Northwind PDF | Preview next to the fields; found / missing / removed statuses and evidence; supplier is *missing* on Northwind because it isn't printed, so nothing is invented; line items |
| 3:30–4:15 | Open the planted-error PDF | `LINE_ITEM_MATH` / `LINE_SUM_MISMATCH`; correct the amount, save, and it's re-validated live; the correction history |
| 4:15–4:40 | Open the duplicate copy | `DUPLICATE` with a link to the original |
| 4:40–5:20 | Search page | Keyword "Hanari Carnes"; semantic with a typo ("Hanary Karnes"); a filter (min total) |
| 5:20–5:50 | Cross-verify page | PDFs vs the ledger: mismatch table with the quantity and total differences |
| 5:50–6:40 | Terminal + `docs/eval_report.md` | `pytest` passing; headline numbers (field accuracy, 0% hallucination, planted errors caught, time per page, long PDF); one honest weakness (real receipts / OCR) and the next step |

Keep the browser at 110–125% zoom so text is readable. Upload the video (YouTube unlisted or Google Drive with link
access) and put the link at the top of the README.
