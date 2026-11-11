# Test results

Generated 2026-11-11 14:34 · `pytest Code/tests` · 8.7 s · exit code 0

**76 passed · 0 failed · 0 skipped**

| Module | Passed | Failed | Skipped | What it covers |
|---|---|---|---|---|
| test_api | 18 | 0 | 0 | upload, background processing, results, corrections + re-validation, reprocess, search (keyword/semantic/hybrid/filters), duplicates across uploads, ledger handling, cross-verify, auth, size/type limits, previews |
| test_extract_validate | 34 | 0 | 0 | normalisation, grounding (no invented values), rules baseline, table mapping, segmentation, LLM client with a fake server + fallback, validation rules, duplicates, cross-verification |
| test_ingest | 24 | 0 | 0 | file type detection, PDF text layer + OCR, images/TIFF, XLSX/CSV, corrupted/encrypted/empty files |

Raw outputs: `docs/test-results.xml` (JUnit) and `docs/test-output.txt` (console).
All test files are generated on the fly; the API tests use a temporary SQLite database and run jobs synchronously. The same API code was also run against PostgreSQL + Redis + an RQ worker.
