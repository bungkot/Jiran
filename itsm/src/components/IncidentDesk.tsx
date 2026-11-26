"use client";

import { useCallback, useEffect, useState } from "react";
import AgentPlayground from "@/components/AgentPlayground";
import type { Ticket } from "@/lib/tickets";

const STATUS_LABELS: Record<Ticket["status"], string> = {
  open: "Baru",
  in_progress: "Ditangani",
  resolved: "Pulih",
  failed: "Gagal",
};

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("id-ID", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function isTicket(value: unknown): value is Ticket {
  if (typeof value !== "object" || value === null) return false;
  return (
    "id" in value &&
    typeof value.id === "string" &&
    "title" in value &&
    typeof value.title === "string" &&
    "status" in value &&
    ["open", "in_progress", "resolved", "failed"].includes(
      String(value.status),
    ) &&
    "updatedAt" in value &&
    typeof value.updatedAt === "string" &&
    "messages" in value &&
    Array.isArray(value.messages) &&
    "transactions" in value &&
    Array.isArray(value.transactions) &&
    "jobs" in value &&
    Array.isArray(value.jobs)
  );
}

export default function IncidentDesk() {
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refreshTickets = useCallback(async () => {
    const response = await fetch("/api/tickets", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Gagal memuat insiden (HTTP ${response.status}).`);
    }
    const payload: unknown = await response.json();
    if (!Array.isArray(payload) || !payload.every(isTicket)) {
      throw new Error("Respons daftar insiden tidak valid.");
    }
    setTickets(payload);
  }, []);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        await refreshTickets();
        if (active) setError("");
      } catch (reason) {
        if (active) {
          setError(
            reason instanceof Error ? reason.message : "Gagal memuat insiden.",
          );
        }
      } finally {
        if (active) setLoading(false);
      }
    };
    void load();
    const interval = window.setInterval(() => void load(), 5000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [refreshTickets]);

  return (
    <>
      <h1 className="visuallyHidden">ITSM operations</h1>
      <section className="ticketWorkspace">
        <aside className="ticketSidebar">
          <div className="sidebarHeading">
            <div>
              <p className="eyebrow">SYSTEM</p>
              <h2>Insiden</h2>
            </div>
            <span className="incidentCount">{tickets.length}</span>
          </div>
          <p className="sidebarDescription">
            Dibuat otomatis oleh sistem self-healing.
          </p>
          {error ? (
            <div className="errorBanner incidentError" role="alert">
              {error}
            </div>
          ) : null}
          <div className="ticketList" aria-live="polite">
            {loading && tickets.length === 0 ? (
              <p className="emptyHint">Memuat insiden...</p>
            ) : tickets.length === 0 ? (
              <div className="emptyIncident">
                <span className="emptyIncidentMark" aria-hidden="true">
                  —
                </span>
                <strong>Belum ada insiden</strong>
                <p>Gangguan yang terdeteksi akan tercatat di sini.</p>
              </div>
            ) : (
              tickets.map((ticket) => (
                <details className="incidentItem" key={ticket.id}>
                  <summary className="incidentSummary">
                    <span className="incidentMeta">
                      <span>{ticket.id}</span>
                      <span className={`statusBadge status-${ticket.status}`}>
                        {STATUS_LABELS[ticket.status]}
                      </span>
                    </span>
                    <strong>{ticket.title}</strong>
                    <span className="ticketItemDate">
                      {ticket.source === "self-heal" ? "Self-healing" : "Sistem"}{" "}
                      · {formatTime(ticket.updatedAt)}
                    </span>
                  </summary>
                  <div className="incidentDetails">
                    {ticket.messages.at(-1) ? (
                      <p className="incidentLastMessage">
                        {ticket.messages.at(-1)?.content}
                      </p>
                    ) : null}
                    {ticket.transactions.length > 0 ? (
                      <ol className="incidentTimeline">
                        {[...ticket.transactions].reverse().map((transaction) => (
                          <li key={transaction.id}>
                            <span className="timelineDot" />
                            <div>
                              <strong>{transaction.label}</strong>
                              <p>{transaction.detail}</p>
                              <time>{formatTime(transaction.createdAt)}</time>
                            </div>
                          </li>
                        ))}
                      </ol>
                    ) : null}
                  </div>
                </details>
              ))
            )}
          </div>
          <div className="agentQueueNote">
            <span className="statusDot" />
            <span>Satu agent · antrean serial</span>
          </div>
        </aside>

        <AgentPlayground />
      </section>
    </>
  );
}
