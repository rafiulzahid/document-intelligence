"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

const LINKS = [["/", "Upload & jobs"], ["/review", "Review queue"], ["/search", "Search"], ["/cross-verify", "Cross-verify"]];

export default function Nav() {
  const path = usePathname();
  const [h, setH] = useState(null);
  useEffect(() => {
    const load = () => api("/health").then(setH).catch((e) => setH({ status: "down", error: e.message }));
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, []);
  const ok = h?.status === "ok";
  return (
    <div className="topbar">
      <div className="inner">
        <Link href="/" className="brand">Doc<span>Intel</span></Link>
        <nav className="nav">
          {LINKS.map(([href, label]) => (
            <Link key={href} href={href} className={(href === "/" ? path === "/" : path.startsWith(href)) ? "active" : ""}>{label}</Link>
          ))}
        </nav>
        <div className="health" title={h ? JSON.stringify(h) : ""}>
          <span className={`dot ${h ? (ok ? "ok" : "bad") : ""}`} />
          {h ? (ok ? `API ok · ${h.database}${h.pgvector ? " + pgvector" : ""} · queue ${h.queue?.mode}${h.queue?.workers !== undefined ? ` (${h.queue.workers} worker${h.queue.workers === 1 ? "" : "s"})` : ""}`
                  : "API not reachable") : "checking API…"}
        </div>
      </div>
    </div>
  );
}
