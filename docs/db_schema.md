# Database schema

PostgreSQL 16 with the `pg_trgm` and `vector` (pgvector) extensions. Tables are created by SQLAlchemy at API startup
(`Code/api/models.py`, `Code/api/db.py`); the same models also run on SQLite for local use and tests.

```mermaid
erDiagram
    documents ||--o{ pages : has
    documents ||--o{ invoices : contains
    documents ||--o{ processing_events : logs
    documents ||--o{ chunks : "indexed as"
    invoices ||--o{ line_items : has
    invoices ||--o{ validation_issues : has
    invoices ||--o{ corrections : "audit trail"
    invoices ||--o{ invoice_keys : "duplicate keys"
    invoices ||--o{ chunks : "summary chunk"
    chunks ||--o| chunk_embeddings : "pgvector copy"

    documents {
        int id PK
        string filename
        string stored_path "random name on disk"
        string detected_type "pdf image tiff xlsx xls csv"
        int size_bytes
        string sha256
        string status "queued processing valid needs_review no_invoice failed"
        string error_code
        text error_message
        int page_count
        int ocr_pages
        int progress_done
        int progress_total
        int attempts
        json extraction_info
        json warnings
        json timings
        timestamptz created_at
        timestamptz updated_at
    }
    pages {
        int id PK
        int document_id FK
        int page_no
        string kind "page or sheet"
        string name "sheet name"
        text text
        string method "pdf_text ocr:tesseract sheet"
        float ocr_confidence
    }
    invoices {
        int id PK
        int document_id FK
        int idx
        string invoice_number
        string invoice_date "ISO"
        string supplier_name
        string customer_name
        numeric subtotal
        numeric tax
        numeric total
        string currency "ISO 4217"
        json field_meta "status confidence evidence raw note per field"
        json source "pages or sheet and rows"
        string method "llm:model table_mapping rules"
        json extraction_warnings
        string status "valid needs_review"
    }
    line_items {
        int id PK
        int invoice_id FK
        int position
        text description
        float quantity
        numeric unit_price
        numeric amount
    }
    validation_issues {
        int id PK
        int invoice_id FK
        string rule
        string severity "error warning info"
        string field
        text message
        json details
        bool resolved
    }
    invoice_keys {
        int id PK
        int invoice_id FK
        string key "sha / num+party / num+total / party+date+total"
    }
    corrections {
        int id PK
        int invoice_id FK
        string field
        text old_value
        text new_value
        string corrected_by
        timestamptz corrected_at
    }
    processing_events {
        int id PK
        int document_id FK
        string stage "queued ingest extract validate index done error correction"
        string status
        text message
        float duration_ms
        timestamptz created_at
    }
    chunks {
        int id PK
        int document_id FK
        int invoice_id FK
        int page_no
        string kind "page sheet invoice"
        text text
        json embedding "model tag + vector"
    }
    chunk_embeddings {
        int chunk_id PK
        vector embedding "vector(dim), HNSW cosine index"
    }
```

## Indexes

| Index | Purpose |
|---|---|
| `GIN (to_tsvector('simple', chunks.text))` | keyword full-text search |
| `GIN (invoices.supplier_name gin_trgm_ops)`, same for `customer_name` | fuzzy name search |
| `HNSW (chunk_embeddings.embedding vector_cosine_ops)` | semantic search |
| `invoice_keys(key)` | duplicate lookup across the database |
| `invoices(invoice_number)`, `(invoice_date)`, `(total)`, `(status)`, `(invoice_number, total)` | filters |
| `documents(status)`, `(sha256)`, `(created_at)` | job lists, exact-duplicate files |

All child rows use `ON DELETE CASCADE`, so deleting or reprocessing a document removes its old results cleanly.
