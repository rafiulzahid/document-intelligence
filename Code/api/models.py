"""Database tables."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now():
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    stored_path: Mapped[str] = mapped_column(String(1024))
    content_type: Mapped[Optional[str]] = mapped_column(String(128))
    detected_type: Mapped[Optional[str]] = mapped_column(String(16))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # queued | processing | valid | needs_review | no_invoice | failed
    error_code: Mapped[Optional[str]] = mapped_column(String(40))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    ocr_pages: Mapped[int] = mapped_column(Integer, default=0)
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    extraction_info: Mapped[Optional[dict]] = mapped_column(JSON)
    warnings: Mapped[Optional[list]] = mapped_column(JSON)
    timings: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    pages = relationship("Page", back_populates="document", cascade="all, delete-orphan", order_by="Page.page_no")
    invoices = relationship("Invoice", back_populates="document", cascade="all, delete-orphan", order_by="Invoice.idx")
    events = relationship("ProcessingEvent", back_populates="document", cascade="all, delete-orphan",
                          order_by="ProcessingEvent.id")
    chunks = relationship("Chunk", back_populates="document", cascade="all, delete-orphan")


class Page(Base):
    __tablename__ = "pages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(10), default="page")          # page | sheet
    name: Mapped[Optional[str]] = mapped_column(String(255))               # sheet name
    text: Mapped[str] = mapped_column(Text, default="")
    method: Mapped[Optional[str]] = mapped_column(String(32))
    ocr_confidence: Mapped[Optional[float]] = mapped_column(Float)
    document = relationship("Document", back_populates="pages")


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer, default=0)
    invoice_number: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    invoice_date: Mapped[Optional[str]] = mapped_column(String(10), index=True)     # ISO date
    supplier_name: Mapped[Optional[str]] = mapped_column(String(512))
    customer_name: Mapped[Optional[str]] = mapped_column(String(512))
    subtotal: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    tax: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    total: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), index=True)
    currency: Mapped[Optional[str]] = mapped_column(String(8))
    field_meta: Mapped[Optional[dict]] = mapped_column(JSON)
    source: Mapped[Optional[dict]] = mapped_column(JSON)
    method: Mapped[Optional[str]] = mapped_column(String(64))
    extraction_warnings: Mapped[Optional[list]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="valid", index=True)  # valid | needs_review
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    document = relationship("Document", back_populates="invoices")
    line_items = relationship("LineItemRow", back_populates="invoice", cascade="all, delete-orphan",
                              order_by="LineItemRow.position")
    issues = relationship("ValidationIssue", back_populates="invoice", cascade="all, delete-orphan",
                          order_by="ValidationIssue.id")
    corrections = relationship("Correction", back_populates="invoice", cascade="all, delete-orphan",
                               order_by="Correction.id")
    keys = relationship("InvoiceKey", back_populates="invoice", cascade="all, delete-orphan")


class LineItemRow(Base):
    __tablename__ = "line_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    description: Mapped[Optional[str]] = mapped_column(Text)
    quantity: Mapped[Optional[float]] = mapped_column(Float)
    unit_price: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    amount: Mapped[Optional[float]] = mapped_column(Numeric(14, 2))
    invoice = relationship("Invoice", back_populates="line_items")


class ValidationIssue(Base):
    __tablename__ = "validation_issues"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    rule: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(10))
    field: Mapped[Optional[str]] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[Optional[dict]] = mapped_column(JSON)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    invoice = relationship("Invoice", back_populates="issues")


class InvoiceKey(Base):
    """Duplicate-detection keys (file hash, invoice number + party, invoice number + total, ...)."""
    __tablename__ = "invoice_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(600), index=True)
    invoice = relationship("Invoice", back_populates="keys")


class Correction(Base):
    __tablename__ = "corrections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[Optional[str]] = mapped_column(Text)
    new_value: Mapped[Optional[str]] = mapped_column(Text)
    corrected_by: Mapped[Optional[str]] = mapped_column(String(128))
    corrected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    invoice = relationship("Invoice", back_populates="corrections")


class ProcessingEvent(Base):
    __tablename__ = "processing_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(32))       # queued | ingest | extract | validate | store | done | error
    status: Mapped[str] = mapped_column(String(16))      # started | ok | failed
    message: Mapped[Optional[str]] = mapped_column(Text)
    duration_ms: Mapped[Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    document = relationship("Document", back_populates="events")


class Chunk(Base):
    """Searchable text: one chunk per page/sheet plus one per invoice (its fields)."""
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    invoice_id: Mapped[Optional[int]] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    page_no: Mapped[Optional[int]] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(10))          # page | sheet | invoice
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Optional[list]] = mapped_column(JSON)  # always stored (fallback search); pgvector copy if available
    document = relationship("Document", back_populates="chunks")


Index("ix_invoices_number_total", Invoice.invoice_number, Invoice.total)
