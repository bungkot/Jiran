"use client";

import { useCallback, useEffect, useState } from "react";
import type { Ticket } from "@/lib/tickets";
import {
  formatAgentProgress,
  formatIncidentText,
  formatJobStatus,
} from "@/lib/displayText";

const STATUS_LABELS: Record<Ticket["status"], string> = {
  open: "New",
  in_progress: "In progress",
  resolved: "Resolved",
  failed: "Failed",
};

const SEVERITY_LABELS: Record<NonNullable<Ticket["severity"]>, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
};

const MESSAGE_ROLE_LABELS: Record<Ticket["messages"][number]["role"], string> = {
  user: "Reporter",
  agent: "Agent",
  system: "System",
};

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("en-US", {
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
      throw new Error(`Failed to load incidents (HTTP ${response.status}).`);
    }
    const payload: unknown = await response.json();
    if (!Array.isArray(payload) || !payload.every(isTicket)) {
      throw new Error("The incident list response is invalid.");
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
            reason instanceof Error ? reason.message : "Failed to load incidents.",
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
      <h1 className="visuallyHidden">Incident tickets</h1>
      <section className="ticketWorkspace" aria-label="Ticket workspace">
        <aside className="ticketSidebar">
          <div className="sidebarHeading">
            <div>
              <p className="eyebrow">ITSM</p>
              <h2>Incidents</h2>
            </div>
            <span className="incidentCount">{tickets.length}</span>
          </div>
          {error ? (
            <div className="errorBanner incidentError" role="alert">
              {error}
            </div>
          ) : null}
          <div className="ticketList" aria-label="Incident list">
            {loading && tickets.length === 0 ? (
              <p className="emptyHint">Loading incidents...</p>
            ) : tickets.length === 0 ? (
              <div className="emptyIncident">
                <span className="emptyIncidentMark" aria-hidden="true">
                  —
                </span>
                <strong>No incidents yet</strong>
                <p>Detected issues will appear here.</p>
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
                  <strong>{formatIncidentText(ticket.title)}</strong>
                  <span className="ticketItemDate">
                    {ticket.source === "self-heal" ? "Self-healing" : "System"}{" "}
                    · {formatTime(ticket.updatedAt)}
                  </span>
                </button>
              ))
            )}
          </div>
          <div className="agentQueueNote">
            <span className="statusDot" />
            <span>List updates automatically</span>
          </div>
        </aside>

        <section className="ticketDetailPanel" aria-label="Incident details">
          {selectedTicket ? (
            <>
              <header className="ticketDetailHeader">
                <div className="ticketDetailTitle">
                  <p className="eyebrow">
                    INCIDENT <span aria-hidden="true">/</span>{" "}
                    {selectedTicket.id}
                  </p>
                  <h2>{formatIncidentText(selectedTicket.title)}</h2>
                  <p>
                    {selectedTicket.source === "self-heal"
                      ? "Automatically detected by self-healing"
                      : "Reported to the system"}
                  </p>
                </div>
                <div className="ticketDetailBadges">
                  <span
                    className={`severityBadge severity-${selectedTicket.severity ?? "medium"}`}
                  >
                    {`Severity: ${SEVERITY_LABELS[selectedTicket.severity ?? "medium"]}`}
                  </span>
                  <span
                    className={`statusBadge status-${selectedTicket.status}`}
                  >
                    {STATUS_LABELS[selectedTicket.status]}
                  </span>
                </div>
              </header>

              <div className="ticketDetailContent">
                <section className="ticketFacts" aria-label="Ticket information">
                  <div>
                    <span>Created</span>
                    <strong>{formatTime(selectedTicket.createdAt)}</strong>
                  </div>
                  <div>
                    <span>Last updated</span>
                    <strong>{formatTime(selectedTicket.updatedAt)}</strong>
                  </div>
                  <div>
                    <span>Source</span>
                    <strong>
                      {selectedTicket.source === "self-heal"
                        ? "Self-healing"
                        : "System"}
                    </strong>
                  </div>
                </section>

                <section className="ticketDetailSection">
                  <div className="detailSectionHeading">
                    <div>
                      <p className="eyebrow">COMMUNICATION</p>
                      <h3>Incident updates</h3>
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
                          <p>
                            {message.role === "system"
                              ? formatIncidentText(message.content)
                              : message.content}
                          </p>
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <p className="detailEmpty">No updates yet.</p>
                  )}
                </section>

                <section className="ticketDetailSection">
                  <div className="detailSectionHeading">
                    <div>
                      <p className="eyebrow">AUDIT TRAIL</p>
                      <h3>Activity history</h3>
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
                              <strong>{formatIncidentText(transaction.label)}</strong>
                              <p>{formatIncidentText(transaction.detail)}</p>
                              <time>{formatTime(transaction.createdAt)}</time>
                            </div>
                          </li>
                        ))}
                    </ol>
                  ) : (
                    <p className="detailEmpty">No activity recorded yet.</p>
                  )}
                </section>

                {selectedTicket.jobs.length > 0 ? (
                  <section className="ticketDetailSection">
                    <div className="detailSectionHeading">
                      <div>
                        <p className="eyebrow">AUTOMATION</p>
                        <h3>Related jobs</h3>
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
                            <span>{formatAgentProgress(job.progress)}</span>
                          </div>
                          <span className="statusBadge">
                            {formatJobStatus(job.status)}
                          </span>
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
              <h2>{loading ? "Loading incident" : "Select an incident"}</h2>
              <p>
                {loading
                  ? "The ticket list is updating."
                  : "Select a ticket from the list to view its details and history."}
              </p>
            </div>
          )}
        </section>
      </section>
    </>
  );
}
