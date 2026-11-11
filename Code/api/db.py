"""Database engine, session and schema creation (PostgreSQL in production, SQLite for quick local runs/tests)."""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


_engine = None
_Session = None
state = {"pgvector": False, "pg_trgm": False, "vector_dim": None}


def get_engine():
    global _engine, _Session
    if _engine is None:
        url = config.settings.database_url
        kw = {"pool_pre_ping": True, "future": True}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _engine = create_engine(url, **kw)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _fk(dbapi_conn, _):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.close()
        _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine():
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine, _Session = None, None
    state.update(pgvector=False, pg_trgm=False, vector_dim=None)


@contextmanager
def session_scope():
    get_engine()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session():
    """FastAPI dependency."""
    get_engine()
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def init_db():
    from . import models  # noqa: F401  (register tables)
    eng = get_engine()
    if config.settings.is_postgres:
        for ext in ("pg_trgm", "vector"):
            try:
                with eng.begin() as c:
                    c.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
                state["pgvector" if ext == "vector" else "pg_trgm"] = True
            except Exception:  # noqa: BLE001  (extension not installed on this server)
                pass
    Base.metadata.create_all(eng)
    if config.settings.is_postgres:
        with eng.begin() as c:
            c.execute(text("CREATE INDEX IF NOT EXISTS ix_chunks_fts ON chunks USING GIN (to_tsvector('simple', text))"))
            if state["pg_trgm"]:
                c.execute(text("CREATE INDEX IF NOT EXISTS ix_inv_supplier_trgm ON invoices USING GIN (supplier_name gin_trgm_ops)"))
                c.execute(text("CREATE INDEX IF NOT EXISTS ix_inv_customer_trgm ON invoices USING GIN (customer_name gin_trgm_ops)"))
            if state["pgvector"]:
                row = c.execute(text("SELECT to_regclass('chunk_embeddings')")).scalar()
                if row:
                    dim = c.execute(text("SELECT atttypmod FROM pg_attribute WHERE attrelid = 'chunk_embeddings'::regclass "
                                         "AND attname = 'embedding'")).scalar()
                    state["vector_dim"] = dim if dim and dim > 0 else None
    return state


def ensure_vector_table(dim: int):
    """pgvector table, created on first use once the embedding size is known."""
    if not state["pgvector"] or state["vector_dim"] == dim:
        return state["pgvector"] and state["vector_dim"] == dim
    if state["vector_dim"] not in (None, dim):
        return False  # embedding model changed size; fall back to the Python search
    with get_engine().begin() as c:
        c.execute(text(f"CREATE TABLE IF NOT EXISTS chunk_embeddings (chunk_id INTEGER PRIMARY KEY "
                       f"REFERENCES chunks(id) ON DELETE CASCADE, embedding vector({dim}))"))
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_chunk_embeddings_hnsw ON chunk_embeddings "
                       "USING hnsw (embedding vector_cosine_ops)"))
    state["vector_dim"] = dim
    return True
