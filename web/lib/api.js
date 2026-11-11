// Small client helpers. All calls go through the server-side proxy (/api/proxy/...).
export const BASE = "/api/proxy";

export async function api(path, opts = {}) {
  const res = await fetch(`${BASE}${path}`, { cache: "no-store", ...opts,
    headers: { ...(opts.body && !(opts.body instanceof FormData) ? { "content-type": "application/json" } : {}),
               ...(opts.headers || {}) } });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    const msg = typeof d === "string" ? d : d?.message ? `${d.message}: ${JSON.stringify(d.rejected || "")}` : JSON.stringify(d || data);
    throw new Error(`${res.status} ${msg}`);
  }
  return data;
}

// Upload with progress (fetch has no upload progress, XHR does)
export function uploadFiles(files, onProgress) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    for (const f of files) fd.append("files", f, f.name);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}/documents`);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(Math.round((100 * e.loaded) / e.total));
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(`${xhr.status} ${typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data)}`));
    };
    xhr.onerror = () => reject(new Error("Network error while uploading"));
    xhr.send(fd);
  });
}

export const money = (v, cur) => (v === null || v === undefined ? "—" :
  `${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${cur ? " " + cur : ""}`);

export const when = (iso) => (iso ? new Date(iso).toLocaleString() : "—");

export const fmtBytes = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);
