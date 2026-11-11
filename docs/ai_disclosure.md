# AI assistance disclosure

## 1. AI used to build the project

I used **Claude (Anthropic)** as an AI assistant in a chat conversation throughout the task.

| Area | What the AI did | What I did |
|---|---|---|
| Planning | Proposed the architecture, the technology stack, a time plan and the evaluation design | Chose the approach and priorities (ML core first, then backend, then UI), kept to the deadline |
| Datasets | Researched public invoice/receipt datasets and their licenses; wrote `prepare_dataset.py` to download, convert labels and generate test files | Downloaded the datasets, ran the scripts on my machine, reported problems (wrong folder layout, renamed SROIE images, missing labels) that were then fixed |
| Code | Wrote most of the code: ingestion/OCR, LLM extraction + grounding, rules baseline, validation, duplicate detection, cross-verification, FastAPI backend, RQ worker, PostgreSQL schema and search, Next.js frontend, Docker setup | Ran, tested and debugged everything on my machine (GPU, Ollama, Docker, ports, venv), and decided which changes to keep |
| Debugging | Diagnosed errors I reported (Python environment, Docker permissions, ports in use, worker not running) and bugs found in results (label errors, money tolerance, duplicate direction) | Reported the errors and evaluation outputs, verified the fixes |
| Tests and evaluation | Wrote the pytest suite (77 tests) and the evaluation / report tooling | Ran the tests and the measured evaluation on my machine; all numbers in the report come from those runs |
| Documentation | Drafted the README, architecture, schema, prompts and this disclosure | Reviewed and edited them |

How AI-generated code was verified: an automated test suite (unit, integration and API tests), a measured evaluation
against labeled ground truth with a rules-only baseline for comparison, and manual end-to-end runs of the full stack
(PostgreSQL + Redis + workers + web UI).

## 2. AI used at runtime (inside the system)

| Purpose | Model | Where |
|---|---|---|
| Invoice field + line item extraction | qwen2.5 7B Instruct (8k context) via Ollama, local GPU, temperature 0, JSON mode | `Code/docintel/extract/llm.py` |
| Semantic search embeddings | nomic-embed-text via Ollama | `Code/api/embeddings.py` |
| OCR | Tesseract 5 (not an LLM) | `Code/docintel/ingest/ocr.py` |

The exact runtime prompts are in [`prompts.md`](prompts.md). The LLM output is never trusted directly: every value
must be found in the document text (grounding), and validation rules check the result.
