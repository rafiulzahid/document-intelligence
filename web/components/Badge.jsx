const LABEL = { valid: "valid", needs_review: "needs review", failed: "failed", processing: "processing",
                queued: "queued", no_invoice: "no invoice" };
export function StatusBadge({ status }) {
  return <span className={`badge b-${status}`}>{LABEL[status] || status}</span>;
}
export function SevBadge({ severity }) {
  return <span className={`badge sev-${severity}`}>{severity}</span>;
}
export function FieldBadge({ status }) {
  return <span className={`badge fs-${status || "missing"}`}>{status || "missing"}</span>;
}
