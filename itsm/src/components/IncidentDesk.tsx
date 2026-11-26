"use client";

import { useCallback, useEffect, useState } from "react";
import type { Ticket } from "@/lib/tickets";

const STATUS_LABELS: Record<Ticket["status"], string> = {
  open: "Baru",
  in_progress: "Ditangani",
  resolved: "Pulih",
  failed: "Gagal",
};

const SEVERITY_LABELS: Record<NonNullable<Ticket["severity"]>, string> = {
  low: "Rendah",
  medium: "Sedang",
  high: "Tinggi",
};

const MESSAGE_ROLE_LABELS: Record<Ticket["messages"][number]["role"], string> = {
  user: "Pelapor",
  agent: "Agent",
  system: "Sistem",
};

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("id-ID", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isTicketMessage(
  value: unknown,
): value is Ticket["messages"][number] {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    ["user", "agent", "system"].includes(String(value.role)) &&
    typeof value.content === "string" &&
    typeof value.createdAt === "string"
  );
}

function isTicketTransaction(
  value: unknown,
): value is Ticket["transactions"][number] {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.label === "string" &&
    typeof value.detail === "string" &&
    typeof value.createdAt === "string"
  );
}

function isTicketJob(value: unknown): value is Ticket["jobs"][number] {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.status === "string" &&
    typeof value.progress === "string" &&
    typeof value.progressSeq === "number" &&
    typeof value.createdAt === "string" &&
    (value.startedAt === undefined || typeof value.startedAt === "string") &&
    (value.finishedAt === undefined || typeof value.finishedAt === "string")
  );
}

function isTicket(value: unknown): value is Ticket {
  if (!isRecord(value)) return false;
  return (
    typeof value.id === "string" &&
    typeof value.title === "string" &&
    ["open", "in_progress", "resolved", "failed"].includes(
      String(value.status),
    ) &&
    (value.severity === undefined ||
      ["low", "medium", "high"].includes(String(value.severity))) &&
    typeof value.createdAt === "string" &&
    typeof value.updatedAt === "string" &&
    Array.isArray(value.messages) &&
    value.messages.every(isTicketMessage) &&
    Array.isArray(value.transactions) &&
    value.transactions.every(isTicketTransaction) &&
    Array.isArray(value.jobs) &&
    value.jobs.every(isTicketJob)
  );
}

export default function IncidentDesk() {
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
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
    setSelectedId((current) =>
      current && payload.some((ticket) => ticket.id === current)
        ? current
        : (payload[0]?.id ?? null),
    );
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

  const selectedTicket =
    tickets.find((ticket) => ticket.id === selectedId) ?? null;

  return (
    <>
      <h1 className="visuallyHidden">Tiket insiden</h1>
      <section className="ticketWorkspace" aria-label="Ruang kerja tiket">
        <aside className="ticketSidebar">
          <div className="sidebarHeading">
            <div>
              <p className="eyebrow">ITSM</p>
              <h2>Insiden</h2>
            </div>
            <span className="incidentCount">{tickets.length}</span>
          </div>
          {error ? (
            <div className="errorBanner incidentError" role="alert">
              {error}
            </div>
          ) : null}
          <div className="ticketList" aria-label="Daftar insiden">
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
                <button
                  aria-pressed={ticket.id === selectedTicket?.id}
                  className={`incidentItem${ticket.id === selectedTicket?.id ? " selected" : ""}`}
                  key={ticket.id}
                  onClick={() => setSelectedId(ticket.id)}
                  type="button"
                >
                  <span className="incidentMeta">
                    <span className="incidentId" title={ticket.id}>
                      {ticket.id}
                    </span>
                    <span className="incidentBadges">
                      <span
                        className={`severityBadge severity-${ticket.severity ?? "medium"}`}
                      >
                        {SEVERITY_LABELS[ticket.severity ?? "medium"]}
                      </span>
                      <span className={`statusBadge status-${ticket.status}`}>
                        {STATUS_LABELS[ticket.status]}
                      </span>
                    </span>
                  </span>
                  <strong>{ticket.title}</strong>
                  <span className="ticketItemDate">
                    {ticket.source === "self-heal" ? "Self-healing" : "Sistem"}{" "}
                    · {formatTime(ticket.updatedAt)}
                  </span>
                </button>
              ))
            )}
          </div>
          <div className="agentQueueNote">
            <span className="statusDot" />
            <span>Daftar diperbarui otomatis</span>
          </div>
        </aside>

        <section className="ticketDetailPanel" aria-label="Detail insiden">
          {selectedTicket ? (
            <>
              <header className="ticketDetailHeader">
                <div className="ticketDetailTitle">
                  <p className="eyebrow">
                    INSIDEN <span aria-hidden="true">/</span>{" "}
                    {selectedTicket.id}
                  </p>
                  <h2>{selectedTicket.title}</h2>
                  <p>
                    {selectedTicket.source === "self-heal"
                      ? "Terdeteksi otomatis oleh self-healing"
                      : "Dilaporkan ke sistem"}
                  </p>
                </div>
                <div className="ticketDetailBadges">
                  <span
                    className={`severityBadge severity-${selectedTicket.severity ?? "medium"}`}
                  >
                    Severity {SEVERITY_LABELS[selectedTicket.severity ?? "medium"]}
                  </span>
                  <span
                    className={`statusBadge status-${selectedTicket.status}`}
                  >
                    {STATUS_LABELS[selectedTicket.status]}
                  </span>
                </div>
              </header>

              <div className="ticketDetailContent">
                <section className="ticketFacts" aria-label="Informasi tiket">
                  <div>
                    <span>Dibuat</span>
                    <strong>{formatTime(selectedTicket.createdAt)}</strong>
                  </div>
                  <div>
                    <span>Pembaruan terakhir</span>
                    <strong>{formatTime(selectedTicket.updatedAt)}</strong>
                  </div>
                  <div>
                    <span>Sumber</span>
                    <strong>
                      {selectedTicket.source === "self-heal"
                        ? "Self-healing"
                        : "Sistem"}
                    </strong>
                  </div>
                </section>

                <section className="ticketDetailSection">
                  <div className="detailSectionHeading">
                    <div>
                      <p className="eyebrow">KOMUNIKASI</p>
                      <h3>Pembaruan insiden</h3>
                    </div>
                    <span className="transactionCount">
                      {selectedTicket.messages.length}
                    </span>
                  </div>
                  {selectedTicket.messages.length > 0 ? (
                    <ol className="ticketMessageList">
                      {[...selectedTicket.messages].reverse().map((message) => (
                        <li key={message.id}>
                          <div className="ticketMessageHeading">
                            <strong>{MESSAGE_ROLE_LABELS[message.role]}</strong>
                            <time>{formatTime(message.createdAt)}</time>
                          </div>
                          <p>{message.content}</p>
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <p className="detailEmpty">Belum ada pembaruan.</p>
                  )}
                </section>

                <section className="ticketDetailSection">
                  <div className="detailSectionHeading">
                    <div>
                      <p className="eyebrow">AUDIT TRAIL</p>
                      <h3>Riwayat aktivitas</h3>
                    </div>
                    <span className="transactionCount">
                      {selectedTicket.transactions.length}
                    </span>
                  </div>
                  {selectedTicket.transactions.length > 0 ? (
                    <ol className="incidentTimeline">
                      {[...selectedTicket.transactions]
                        .reverse()
                        .map((transaction) => (
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
                  ) : (
                    <p className="detailEmpty">Belum ada aktivitas tercatat.</p>
                  )}
                </section>

                {selectedTicket.jobs.length > 0 ? (
                  <section className="ticketDetailSection">
                    <div className="detailSectionHeading">
                      <div>
                        <p className="eyebrow">AUTOMATION</p>
                        <h3>Job terkait</h3>
                      </div>
                      <span className="transactionCount">
                        {selectedTicket.jobs.length}
                      </span>
                    </div>
                    <ul className="ticketJobs">
                      {selectedTicket.jobs.map((job) => (
                        <li key={job.id}>
                          <div>
                            <strong>{job.id}</strong>
                            <span>{job.progress}</span>
                          </div>
                          <span className="statusBadge">{job.status}</span>
                        </li>
                      ))}
                    </ul>
                  </section>
                ) : null}
              </div>
            </>
          ) : (
            <div className="ticketDetailEmpty">
              <span className="emptyIncidentMark" aria-hidden="true">
                —
              </span>
              <h2>{loading ? "Memuat insiden" : "Pilih insiden"}</h2>
              <p>
                {loading
                  ? "Daftar tiket sedang diperbarui."
                  : "Pilih tiket di sebelah kiri untuk melihat detail dan riwayatnya."}
              </p>
            </div>
          )}
        </section>
      </section>
    </>
  );
}
