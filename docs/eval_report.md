# Evaluation report

Generated 2026-11-11 15:22 by `Code/build_report.py` from measured runs. Main run: **openai:qwen2.5-8k** (`qwen2.5-8k`); baseline: **rules only, no LLM** (`rules_baseline`).

## 1. Summary

| Metric | LLM pipeline | Rules baseline |
|---|---|---|
| Field accuracy (all scored fields) | 98.4% | 97.6% |
| Fields scored | 1717 | 1717 |
| Hallucination rate (value returned where the document has none) | 0.0% | 0.0% |
| Line items precision / recall | 85.3% / 85.2% | 91.7% / 85.9% |
| Document success rate | 100.0% | 100.0% |
| Unexpected failure rate | 0.0% | 0.0% |
| Corrupted files rejected cleanly | 5/5 | 5/5 |
| Planted problems detected | 36/36 | 36/36 |
| Clean documents wrongly flagged (math/duplicate rules) | 21/218 (9.6%) | 64/218 (29.4%) |
| PDF↔Excel mismatches detected | 7/7 | 7/7 |

Automated tests: **77 passed, 0 failed, 0 skipped** (8.7 s) — see `docs/test_results.md`.

## 1b. Setup

| Item | Value |
|---|---|
| OS | Linux 7.0.0-42-generic |
| CPU | x86_64 |
| RAM | 31 GB |
| GPU | NVIDIA GeForce RTX 2080 Ti, 11264 MiB; NVIDIA GeForce RTX 2080 Ti, 11264 MiB |
| Python | 3.12.3 |
| OCR | tesseract 5.3.4 (Tesseract; EasyOCR optional) |
| LLM | openai:qwen2.5-8k · temperature 0 · JSON mode · prompt v2 |
| Key packages | pymupdf 2.1, fastapi 0.162.4, sqlalchemy 2.1.8, rq 3.0.0, pytesseract 0.3.13, rapidfuzz 3.14.6 |

## 2. Evaluation dataset

241 documents built by `Code/prepare_dataset.py` (fixed seed) from public datasets plus generated test files. Ground truth for every document is in `data/eval/gt/<doc_id>.json`; only labeled fields are scored, and a field labeled *null* (not printed on the document) must be predicted as null.

| Source | Category | Documents |
|---|---|---|
| generated | corrupted | 6 |
| katana | corrupted | 1 |
| katana | duplicate | 1 |
| katana | image_ocr | 77 |
| mendeley+katana_labels | digital_pdf | 30 |
| northwind | cross_verify | 1 |
| northwind | degraded_scan | 10 |
| northwind | digital_pdf | 50 |
| northwind | duplicate | 3 |
| northwind | long_pdf | 1 |
| northwind | planted_error | 10 |
| northwind | spreadsheet | 21 |
| sroie | real_scan | 30 |

| Dataset | Link | License | Real or synthetic |
|---|---|---|---|
| Katana ML invoices (labels + images) | https://huggingface.co/datasets/katanaml-org/invoices-donut-data-v1 | MIT | synthetic invoices rendered as images |
| Mendeley *Samples of electronic invoices* (PDFs) | https://data.mendeley.com/datasets/tnj49gpmtz/2 | CC BY 4.0 | synthetic; labels from the matching Katana row |
| Northwind invoices (PDF) | https://huggingface.co/datasets/AyoubChLin/northwind_invocies | Apache-2.0 | generated from the Northwind sample database |
| SROIE receipts (Voxel51 mirror) | https://huggingface.co/datasets/Voxel51/scanned_receipts | CC-BY-4.0 | **real** scanned receipts (partial labels) |
| Generated | spreadsheets, degraded scans, planted errors, duplicates, corrupted files, long PDF | – | derived from the above |

Label notes from the dataset card:

- katana: line_items unlabeled: 6
- katana: totals unlabeled: 1

## 3. Extraction accuracy

Scoring: invoice number exact (ignoring spaces, `#`, leading zeros) · date = same ISO date · names fuzzy token-set similarity ≥ 85 · amounts ±0.01 · currency same ISO code · null labels correct only if predicted null.

| Field | LLM pipeline | Rules baseline | n |
|---|---|---|---|
| invoice_number | 100.0% | 100.0% | 205 |
| invoice_date | 96.6% | 96.2% | 235 |
| supplier_name | 96.6% | 94.4% | 234 |
| customer_name | 99.0% | 99.5% | 205 |
| subtotal | 100.0% | 100.0% | 202 |
| tax | 100.0% | 100.0% | 202 |
| total | 96.1% | 93.1% | 232 |
| currency | 100.0% | 99.0% | 202 |

By document category (LLM pipeline):

| Field | Scanned invoice images (Katana) | Digital PDFs (Northwind + Mendeley) | Degraded scans | Spreadsheets (XLSX/XLS/CSV) | Real receipts (SROIE) | Planted errors | Duplicates |
|---|---|---|---|---|---|---|---|
| invoice_number | 100.0% | 100.0% | 100.0% | 100.0% | – | 100.0% | 100.0% |
| invoice_date | 100.0% | 100.0% | 100.0% | 100.0% | 73.3% | 100.0% | 100.0% |
| supplier_name | 98.7% | 98.8% | 100.0% | 100.0% | 80.0% | 100.0% | 100.0% |
| customer_name | 100.0% | 97.5% | 100.0% | 100.0% | – | 100.0% | 100.0% |
| subtotal | 100.0% | 100.0% | 100.0% | 100.0% | – | 100.0% | 100.0% |
| tax | 100.0% | 100.0% | 100.0% | 100.0% | – | 100.0% | 100.0% |
| total | 100.0% | 100.0% | 100.0% | 100.0% | 70.0% | 100.0% | 100.0% |
| currency | 100.0% | 100.0% | 100.0% | 100.0% | – | 100.0% | 100.0% |
| **all fields of a document correct** | 98.7% | 97.5% | 100.0% | 100.0% | 46.7% | 100.0% | 100.0% |

### Line items

| Category | Precision | Recall | Item count correct | Invoices |
|---|---|---|---|---|
| Degraded scans | 96.0% | 96.0% | 100.0% | 10 |
| Digital PDFs (Northwind + Mendeley) | 91.5% | 91.5% | 100.0% | 75 |
| Duplicates | 100.0% | 100.0% | 100.0% | 4 |
| Scanned invoice images (Katana) | 74.9% | 74.7% | 98.6% | 72 |
| Planted errors | 100.0% | 100.0% | 100.0% | 10 |
| Spreadsheets (XLSX/XLS/CSV) | 100.0% | 100.0% | 100.0% | 23 |

## 4. Missing information — no invented values

Northwind invoices do not print a supplier, subtotal or tax, and the labels say *null*. Any value returned for these fields would be invented.

| Field | Hallucination rate | Null labels |
|---|---|---|
| supplier_name | 0.0% | 96 |
| subtotal | 0.0% | 96 |
| tax | 0.0% | 96 |
| currency | 0.0% | 23 |

Mechanism: the LLM must quote evidence; every value is checked against the source text and discarded if it is not there (status *removed*). The review UI shows each field's status and evidence.

## 5. Validation

| Planted problem | Detected | Recall |
|---|---|---|
| line_item_math | 5/5 | 100.0% |
| total_mismatch | 10/10 | 100.0% |
| duplicate (same invoice in another file of the set) | 17/17 | 100.0% |
| duplicate | 4/4 | 100.0% |
| clean documents wrongly flagged (math/duplicate rules) | 21/218 | 9.6% |

Rules: required fields · negative amounts · invalid/future dates · quantity × unit price = line amount · line items = subtotal (or total when no tax is shown) · subtotal + tax = total · tax rate 0–30% · duplicates (file hash; invoice number + party; invoice number + total) · uncertain or discarded values → *needs review*. Money tolerance: 2 cents + 0.01%.

## 6. Cross-verification (PDF ↔ Excel ledger)

Expected mismatches detected: **7/7** · invoices matched: 49 · mismatches reported on invoices without a planted change: 0 · only in PDFs: ['10443'] · only in ledger: []

## 7. Processing time and throughput

Ingestion only (`Code/run_ingest.py`, Tesseract on CPU):

| Category | Docs | As expected | Failed (unexpected) | Pages | OCR pages | Mean OCR conf | p50 s/doc | p95 s/doc |
|---|---|---|---|---|---|---|---|---|
| corrupted | 7 | 7 | 0 | 1 | 1 | 0.94 | 0.00 | 1.17 |
| cross_verify | 1 | 1 | 0 | 0 | 0 | - | 0.01 | 0.01 |
| degraded_scan | 10 | 10 | 0 | 10 | 10 | 0.94 | 0.53 | 0.60 |
| digital_pdf | 80 | 80 | 0 | 80 | 0 | - | 0.00 | 0.00 |
| duplicate | 4 | 4 | 0 | 4 | 2 | 0.94 | 0.39 | 0.74 |
| image_ocr | 77 | 77 | 0 | 78 | 78 | 0.94 | 0.97 | 1.19 |
| long_pdf | 1 | 1 | 0 | 831 | 0 | - | 0.14 | 0.14 |
| planted_error | 10 | 10 | 0 | 10 | 0 | - | 0.00 | 0.00 |
| real_scan | 30 | 30 | 0 | 30 | 30 | 0.69 | 1.03 | 2.55 |
| spreadsheet | 21 | 21 | 0 | 0 | 0 | - | 0.00 | 0.00 |
Seconds per page - text-layer PDF: p50 0.001, p95 0.002; OCR: p50 0.96, p95 1.57
Success rate (expected-good docs processed): 236/236
Corrupted files rejected cleanly: 5/5

End-to-end per document (ingestion + extraction + validation, LLM pipeline):

| Category | p50 s/doc | p95 s/doc | Docs |
|---|---|---|---|
| corrupted | 0.0 | 21.629 | 7 |
| cross_verify | 0.009 | 0.009 | 1 |
| Degraded scans | 14.488 | 15.776 | 10 |
| Digital PDFs (Northwind + Mendeley) | 14.542 | 27.019 | 80 |
| Duplicates | 16.449 | 16.774 | 4 |
| Scanned invoice images (Katana) | 26.259 | 32.453 | 77 |
| Planted errors | 17.781 | 20.306 | 10 |
| Real receipts (SROIE) | 16.6 | 22.932 | 30 |
| Spreadsheets (XLSX/XLS/CSV) | 0.001 | 0.004 | 21 |

LLM calls: {'llm:qwen2.5-8k': 213, 'table_mapping': 24} · tokens in/out 194542/88507 · total LLM time 4165.8 s. The API scales by adding RQ workers; LLM calls run in parallel.

## 8. Failure handling

| Document | Error code | Message |
|---|---|---|
| corrupt_truncated_pdf | CORRUPT_FILE | Cannot open PDF: Failed to open file '/home/public/Documents/AI/data/eval/files/corrupted/ |
| corrupt_password_pdf | ENCRYPTED_PDF | The PDF is password-protected; provide an unlocked copy. |
| corrupt_empty_pdf | EMPTY_FILE | The file is empty (0 bytes). |
| corrupt_random_xlsx | CORRUPT_FILE | File content is not a valid .xlsx file (corrupted or wrong extension). |
| corrupt_random_tiff | CORRUPT_FILE | File content is not a valid .tiff file (corrupted or wrong extension). |

No unexpected failures: every valid file produced a result and every corrupted file was rejected with an error code.

## 9. Failure analysis

Most frequent errors (category · field · count):

- Real receipts (SROIE) · total · 9
- Real receipts (SROIE) · invoice_date · 8
- Real receipts (SROIE) · supplier_name · 6
- Digital PDFs (Northwind + Mendeley) · customer_name · 2
- Scanned invoice images (Katana) · supplier_name · 1
- Digital PDFs (Northwind + Mendeley) · supplier_name · 1

Mean OCR confidence by category: corrupted 0.94, Degraded scans 0.94, Duplicates 0.94, Scanned invoice images (Katana) 0.94, Real receipts (SROIE) 0.69

Examples:

| Document | Field | Truth | Predicted |
|---|---|---|---|
| sroie_X51007843145 | supplier_name | ESJAY FUEL ENTERPRISE | BHPetro] Permas Jaya |
| sroie_X51005361907(1) | total | 153.35 | 53.35 |
| sroie_X51006414703 | total | 98.35 | 557.0 |
| sroie_X51007846307 | invoice_date | 2018-06-12 | None |
| sroie_X51007846307 | supplier_name | UNIHAKKA INTERNATIONAL SDN BHD | BAR SARC ERGPETMAE JAYA |
| sroie_X51007846307 | total | 7.7 | 7.72 |
| mendeley_valid_32 | customer_name | Phillps-Bradford 8510 Romero Isle Apt. 679 Ca | Phillips-Bradford |
| sroie_X51006414679 | total | 21.8 | 24.8 |
| sroie_X51006619854 | invoice_date | 2016-06-04 | 2016-04-14 |
| sroie_X51006619854 | supplier_name | 99 SPEED MART S/B | 7178 GARDENTA SQUIGGLES |
| sroie_X51006619854 | total | 2.1 | None |
| sroie_X51005453729 | invoice_date | 2018-03-27 | None |
| sroie_X51005719862 | invoice_date | 2018-02-03 | None |
| sroie_X51006556726 | invoice_date | 2017-08-06 | None |
| sroie_X51006556726 | total | 68.41 | 66.41 |
| sroie_X51005719814 | supplier_name | TK DIVISION KITCHEN SDN BHD | KIKU JAPANESE RESTAURANT |
| katana_validation_010 | supplier_name | Dunn-Campbel. 36709 Santiago Viaduct Dianalan | Dunn-Campbell |
| sroie_X51005745188 | total | 48.0 | None |
| sroie_X51007339643 | invoice_date | 2017-03-30 | None |
| sroie_X51006619694 | invoice_date | 2016-02-11 | 2016-02-14 |

Observations (check against the examples above and edit if needed):

- **Real receipts (SROIE)** are the weakest category: thermal-paper scans give much lower OCR confidence than the synthetic invoices, so dates and totals are often misread or not found; shop names on receipts are frequently logos.
- **Label noise**: a few Katana labels contain a tax id instead of a seller name or typos in names; these rows were excluded or show up as errors that are not real system errors.
- **Line items**: descriptions that wrap over several lines and tables with both net and gross columns are the main source of line-item errors.
- **Spreadsheets** are mapped deterministically and reach 100% on the fields they contain.

## 10. Limitations and next steps

- Most labeled data is synthetic; real B2B invoices (e.g. the DocILE benchmark) would give a harder, more realistic test.
- Better OCR for receipts (EasyOCR/PaddleOCR on GPU, or a vision-language model) is the biggest expected gain.
- A larger LLM or a hosted model would likely improve receipts and line items; the client is provider-agnostic.
- Duplicate detection uses exact keys; near-duplicate detection on embeddings is a natural extension.

## 11. Reproduce

```bash
python Code/prepare_dataset.py                        # dataset + labels
python Code/run_ingest.py                             # ingestion report
python Code/run_pipeline.py --llm none                # rules baseline
python Code/run_pipeline.py --skip-long               # LLM pipeline
python Code/run_pipeline.py --category long_pdf --run-name <model>_long   # optional long-PDF run
python Code/build_report.py                           # this report + test results
```
