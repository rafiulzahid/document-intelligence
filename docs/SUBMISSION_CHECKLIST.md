# Submission checklist

| # | Deliverable | File / link | Done |
|---|---|---|---|
| 1 | Git repository with source code | GitHub repo URL | ☐ |
| 2 | README and setup instructions | `README.md` | ☐ |
| 3 | Architecture diagram and database schema | `docs/architecture.md`, `docs/db_schema.md` | ☐ |
| 4 | Working backend API and frontend | `Code/api/`, `web/`, `docker-compose.yml` | ☐ |
| 5 | PDF, image, Excel and CSV processing pipelines | `Code/docintel/ingest/` | ☐ |
| 6 | Automated tests and test results | `Code/tests/`, `docs/test_results.md`, `docs/test-results.xml` | ☐ |
| 7 | Evaluation report with actual measured metrics | `docs/eval_report.md` (run `python Code/build_report.py`) | ☐ |
| 8 | Recorded walkthrough | link at the top of `README.md` | ☐ |
| 9 | AI prompts and AI-assistance disclosure | `docs/prompts.md`, `docs/ai_disclosure.md` (review it!) | ☐ |

Final checks:
- `git status` shows no `.env`, no `data/`, no zip files, no API keys.
- From a fresh clone, the README quick start works.
- `docs/eval_report.md` was generated after your last LLM run.
- The video link opens without logging in.
