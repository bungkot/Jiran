"use client";

import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import type { AgentJobSnapshot } from "@/lib/agent";

interface ChatMessage {
  id: string;
  role: "user" | "agent" | "system";
  content: string;
  createdAt?: string;
}

const ACTIVE_STATUSES = new Set(["queued", "running"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isJobSnapshot(value: unknown): value is AgentJobSnapshot {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    ["queued", "running", "completed", "failed"].includes(
      String(value.status),
    ) &&
    (value.progress === undefined || typeof value.progress === "string") &&
    (value.response === undefined || typeof value.response === "string") &&
    (value.reason === undefined || typeof value.reason === "string") &&
    (value.error === undefined || typeof value.error === "string")
  );
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("id-ID", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export default function AgentPlayground() {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: "welcome",
      role: "system",
      content: "Hai!",
    },
  ]);
  const [draft, setDraft] = useState("");
  const [job, setJob] = useState<AgentJobSnapshot | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const messageEnd = useRef<HTMLDivElement>(null);

  const jobId = job?.id;
  const jobStatus = job?.status;
  const jobIsActive = Boolean(jobStatus && ACTIVE_STATUSES.has(jobStatus));

  useEffect(() => {
    messageEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, job?.progress]);

  useEffect(() => {
    if (!jobId || !jobStatus || !ACTIVE_STATUSES.has(jobStatus)) return;
    let active = true;
    let timeout: number | undefined;

    const poll = async () => {
      try {
        const response = await fetch(
          `/api/agent-tests/${encodeURIComponent(jobId)}`,
          { cache: "no-store" },
        );
        const payload: unknown = await response.json();
        if (!response.ok || !isJobSnapshot(payload)) {
          const detail =
            isRecord(payload) && typeof payload.error === "string"
              ? payload.error
              : `Gagal membaca status job (HTTP ${response.status}).`;
          throw new Error(detail);
        }
        if (!active) return;
        setJob(payload);
        setError("");

        if (payload.status === "completed" || payload.status === "failed") {
          const content =
            payload.response ||
            payload.reason ||
            payload.error ||
            `Job berakhir dengan status ${payload.status}.`;
          setMessages((current) => [
            ...current,
            {
              id: `job-${payload.id}`,
              role: payload.status === "completed" ? "agent" : "system",
              content,
              createdAt: payload.finished_at || new Date().toISOString(),
            },
          ]);
        } else {
          timeout = window.setTimeout(poll, 1800);
        }
      } catch (reason) {
        if (!active) return;
        setError(
          reason instanceof Error
            ? reason.message
            : "Status coding agent belum dapat diperbarui.",
        );
        timeout = window.setTimeout(poll, 4000);
      }
    };

    timeout = window.setTimeout(poll, 900);
    return () => {
      active = false;
      if (timeout !== undefined) window.clearTimeout(timeout);
    };
  }, [jobId, jobStatus]);

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const message = draft.trim();
    if (!message || sending || jobIsActive) return;

    setSending(true);
    setError("");
    setDraft("");
    setMessages((current) => [
      ...current,
      {
        id: crypto.randomUUID(),
        role: "user",
        content: message,
        createdAt: new Date().toISOString(),
      },
    ]);

    try {
      const response = await fetch("/api/agent-tests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      const payload: unknown = await response.json();
      if (
        !response.ok ||
        !isRecord(payload) ||
        typeof payload.id !== "string" ||
        typeof payload.status !== "string"
      ) {
        const detail =
          isRecord(payload) && typeof payload.error === "string"
            ? payload.error
            : `Coding agent menolak pengujian (HTTP ${response.status}).`;
        throw new Error(detail);
      }
      setJob({
        id: payload.id,
        status: payload.status,
        progress: "Pengujian masuk antrean coding agent.",
      });
    } catch (reason) {
      const detail =
        reason instanceof Error
          ? reason.message
          : "Pengujian gagal dikirim ke coding agent.";
      setError(detail);
      setMessages((current) => [
        ...current,
        {
          id: crypto.randomUUID(),
          role: "system",
          content: detail,
          createdAt: new Date().toISOString(),
        },
      ]);
    } finally {
      setSending(false);
    }
  }

  return (
    <section className="agentPlayground" aria-label="Uji coding agent">
      <header className="playgroundHeader">
        <div>
          <h2>Coding agent</h2>
        </div>
        <span className="agentPill">1 AGENT</span>
      </header>

      <div className="playgroundMessages" aria-live="polite">
        {messages.map((message) => (
          <article
            className={`chatMessage message-${message.role}`}
            key={message.id}
          >
            <div className="messageMeta">
              <strong>
                {message.role === "user"
                  ? "Anda"
                  : message.role === "agent"
                    ? "Coding agent"
                    : "Jiran"}
              </strong>
              {message.createdAt ? (
                <time>{formatTime(message.createdAt)}</time>
              ) : null}
            </div>
            <p>{message.content}</p>
          </article>
        ))}
        {jobIsActive ? (
          <div className="runningNotice" role="status">
            <span className="statusDot" />
            <span>
              {job?.progress || "Coding agent sedang bekerja."}
            </span>
          </div>
        ) : null}
        <div ref={messageEnd} />
      </div>

      {error ? (
        <div className="errorBanner playgroundError" role="alert">
          {error}
        </div>
      ) : null}

      <form className="chatComposer" onSubmit={sendMessage}>
        <label className="visuallyHidden" htmlFor="agent-test-message">
          Instruksi untuk coding agent
        </label>
        <textarea
          id="agent-test-message"
          maxLength={300}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Tulis instruksi untuk menguji coding agent..."
          rows={1}
          value={draft}
        />
        <div className="composerFooter">
          <span>
            {jobIsActive
              ? "Tunggu job ini selesai sebelum mengirim instruksi berikutnya."
              : "Maksimal 300 karakter"}
          </span>
          <button
            className="button buttonPrimary"
            disabled={sending || jobIsActive || !draft.trim()}
            type="submit"
          >
            {sending ? "Mengirim..." : "Kirim"}
          </button>
        </div>
      </form>
    </section>
  );
}
