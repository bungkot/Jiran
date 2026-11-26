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
  jobId?: string;
  project?: string;
  files?: string[];
}

interface StreamMessage {
  id: string;
  text: string;
}

const ACTIVE_STATUSES = new Set(["queued", "running", "cancelling"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function encodeFilePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

function readStreamMessage(event: Event): StreamMessage | null {
  if (!(event instanceof MessageEvent) || typeof event.data !== "string") {
    return null;
  }
  try {
    const payload: unknown = JSON.parse(event.data);
    if (
      typeof payload === "object" &&
      payload !== null &&
      "text" in payload &&
      typeof payload.text === "string"
    ) {
      return {
        id: event.lastEventId,
        text: payload.text,
      };
    }
  } catch {
    return null;
  }
  return null;
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
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState("");
  const [streamStatus, setStreamStatus] = useState<
    "connecting" | "connected" | "reconnecting" | ""
  >("");
  const messageEnd = useRef<HTMLDivElement>(null);

  const jobId = job?.id;
  const jobStatus = job?.status;
  const jobIsActive = Boolean(jobStatus && ACTIVE_STATUSES.has(jobStatus));

  useEffect(() => {
    messageEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, job?.progress]);

  useEffect(() => {
    if (!jobId || !jobStatus || !ACTIVE_STATUSES.has(jobStatus)) return;
    setStreamStatus("connecting");
    const source = new EventSource(
      `/api/agent-tests/${encodeURIComponent(jobId)}/stream`,
    );

    const appendMessage = (
      event: Event,
      role: ChatMessage["role"],
    ): StreamMessage | null => {
      const payload = readStreamMessage(event);
      if (!payload) {
        setError("Pembaruan dari coding agent tidak valid.");
        return null;
      }
      const messageId = `job-${jobId}-${payload.id}`;
      setMessages((current) =>
        current.some((message) => message.id === messageId)
          ? current
          : [
              ...current,
              {
                id: messageId,
                role,
                content: payload.text,
                jobId,
              },
            ],
      );
      return payload;
    };

    source.onopen = () => {
      setStreamStatus("connected");
      setError("");
    };
    source.onerror = () => setStreamStatus("reconnecting");
    source.addEventListener("progress", (event) => {
      const payload = appendMessage(event, "system");
      if (!payload) return;
      setJob((current) =>
        current
          ? { ...current, progress: payload.text }
          : current,
      );
    });

    const finish = (
      event: Event,
      status: "completed" | "failed" | "cancelled",
    ) => {
      const payload = appendMessage(
        event,
        status === "completed" ? "agent" : "system",
      );
      if (payload) {
        const messageId = `job-${jobId}-${payload.id}`;
        void fetch(`/api/agent-tests/${encodeURIComponent(jobId)}`, {
          cache: "no-store",
        })
          .then(async (response) => {
            const snapshot: unknown = await response.json();
            if (!response.ok || !isRecord(snapshot)) {
              const detail =
                isRecord(snapshot) && typeof snapshot.error === "string"
                  ? snapshot.error
                  : `Gagal memuat file hasil (HTTP ${response.status}).`;
              throw new Error(detail);
            }
            if (snapshot.files !== undefined && !isStringArray(snapshot.files)) {
              throw new Error("Daftar file hasil dari coding agent tidak valid.");
            }
            setMessages((current) =>
              current.map((message) =>
                message.id === messageId
                  ? {
                      ...message,
                      project:
                        typeof snapshot.project === "string"
                          ? snapshot.project
                          : undefined,
                      files: isStringArray(snapshot.files)
                        ? snapshot.files
                        : [],
                    }
                  : message,
              ),
            );
          })
          .catch((reason: unknown) => {
            setError(
              reason instanceof Error
                ? reason.message
                : "File hasil coding agent belum dapat dimuat.",
            );
          });
      }
      setJob((current) =>
        current
          ? {
              ...current,
              status,
              response:
                status === "completed" && payload
                  ? payload.text
                  : current.response,
              error:
                status === "failed" && payload ? payload.text : current.error,
            }
          : current,
      );
      setStreamStatus("");
      setStopping(false);
      source.close();
    };
    source.addEventListener("completed", (event) => finish(event, "completed"));
    source.addEventListener("failed", (event) => finish(event, "failed"));
    source.addEventListener("cancelled", (event) => finish(event, "cancelled"));

    return () => {
      source.close();
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
        },
      ]);
    } finally {
      setSending(false);
    }
  }

  async function stopJob() {
    if (!jobId || !jobIsActive || stopping) return;
    setStopping(true);
    setError("");
    try {
      const response = await fetch(
        `/api/agent-tests/${encodeURIComponent(jobId)}`,
        { method: "DELETE", cache: "no-store" },
      );
      const payload: unknown = await response.json();
      if (!response.ok) {
        const detail =
          isRecord(payload) && typeof payload.error === "string"
            ? payload.error
            : `Pengujian belum dapat dihentikan (HTTP ${response.status}).`;
        throw new Error(detail);
      }
    } catch (reason) {
      setStopping(false);
      setError(
        reason instanceof Error
          ? reason.message
          : "Pengujian coding agent belum dapat dihentikan.",
      );
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
        {messages.map((message) => {
          const downloadJobId = message.jobId
            ? encodeURIComponent(message.jobId)
            : "";
          return (
            <article
              className={`chatMessage message-${message.role}`}
              key={message.id}
            >
              <p>{message.content}</p>
              {downloadJobId && message.files && message.files.length > 0 ? (
                <section className="assistantFiles" aria-label="File hasil proyek">
                  <div className="assistantFilesHeader">
                    <div className="assistantFilesTitleGroup">
                      <a
                        className="assistantZipButton"
                        download
                        href={`/api/agent-tests/${downloadJobId}/files.zip`}
                        title="Unduh seluruh proyek sebagai ZIP"
                      >
                        ZIP
                      </a>
                      <div>
                        <strong>File hasil</strong>
                        {message.project ? (
                          <span className="assistantProjectPath">
                            {message.project}
                          </span>
                        ) : null}
                      </div>
                    </div>
                    <span className="assistantFileCount">
                      {message.files.length} file
                    </span>
                  </div>
                  <ul>
                    {message.files.map((file) => {
                      const extension =
                        file.split("/").pop()?.split(".").pop()?.toUpperCase() ||
                        "FILE";
                      return (
                        <li key={file}>
                          <a
                            className="assistantFileLink"
                            download
                            href={`/api/agent-tests/${downloadJobId}/files/${encodeFilePath(file)}`}
                            title={`Unduh ${file}`}
                          >
                            <span className="assistantFileType">{extension}</span>
                            <span className="assistantFileName">{file}</span>
                          </a>
                        </li>
                      );
                    })}
                  </ul>
                </section>
              ) : null}
            </article>
          );
        })}
        {jobIsActive ? (
          <div className="runningNotice" role="status">
            <span className="statusDot" />
            <span>
              {streamStatus === "reconnecting"
                ? "Koneksi terputus; mencoba menyambung ulang..."
                : job?.progress || "Coding agent sedang bekerja."}
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
          {jobIsActive ? (
            <button
              className="button buttonStop"
              disabled={stopping || jobStatus === "cancelling"}
              onClick={stopJob}
              type="button"
            >
              {stopping || jobStatus === "cancelling"
                ? "Menghentikan..."
                : "Berhenti"}
            </button>
          ) : (
            <button
              className="button buttonPrimary"
              disabled={sending || !draft.trim()}
              type="submit"
            >
              {sending ? "Mengirim..." : "Kirim"}
            </button>
          )}
        </div>
      </form>
    </section>
  );
}
