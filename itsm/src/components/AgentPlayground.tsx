"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type {
  AgentJobSnapshot,
  AgentJobSummary,
} from "@/lib/agent";

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
const STATUS_LABELS: Record<string, string> = {
  queued: "Antrean",
  running: "Berjalan",
  cancelling: "Menghentikan",
  completed: "Selesai",
  failed: "Gagal",
  cancelled: "Dibatalkan",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isAgentJobSummary(value: unknown): value is AgentJobSummary {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.status === "string" &&
    typeof value.goal === "string" &&
    typeof value.created_at === "string" &&
    (value.finished_at === undefined ||
      value.finished_at === null ||
      typeof value.finished_at === "string")
  );
}

function isAgentJobSnapshot(value: unknown): value is AgentJobSnapshot {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.status === "string" &&
    (value.goal === undefined || typeof value.goal === "string") &&
    (value.response === undefined || typeof value.response === "string") &&
    (value.reason === undefined || typeof value.reason === "string") &&
    (value.error === undefined || typeof value.error === "string") &&
    (value.project === undefined || typeof value.project === "string") &&
    (value.files === undefined || isStringArray(value.files))
  );
}

function encodeFilePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Waktu tidak tersedia"
    : new Intl.DateTimeFormat("id-ID", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(date);
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
  const [history, setHistory] = useState<AgentJobSummary[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [job, setJob] = useState<AgentJobSnapshot | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [loadingJob, setLoadingJob] = useState(false);
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState("");
  const [streamStatus, setStreamStatus] = useState<
    "connecting" | "connected" | "reconnecting" | ""
  >("");
  const historyRef = useRef(history);
  historyRef.current = history;
  const historyInitialized = useRef(false);
  const choseNewJob = useRef(false);
  const messageEnd = useRef<HTMLDivElement>(null);

  const refreshHistory = useCallback(async () => {
    const response = await fetch("/api/agent-tests", { cache: "no-store" });
    const payload: unknown = await response.json();
    if (!response.ok) {
      const detail =
        isRecord(payload) && typeof payload.error === "string"
          ? payload.error
          : `Gagal memuat riwayat (HTTP ${response.status}).`;
      throw new Error(detail);
    }
    if (
      !isRecord(payload) ||
      !Array.isArray(payload.jobs) ||
      !payload.jobs.every(isAgentJobSummary)
    ) {
      throw new Error("Respons riwayat coding agent tidak valid.");
    }
    const jobs = payload.jobs;
    setHistory(jobs);
    if (!historyInitialized.current) {
      historyInitialized.current = true;
      if (!choseNewJob.current && jobs[0]) {
        setSelectedJobId(jobs[0].id);
      }
    }
  }, []);

  useEffect(() => {
    let active = true;
    const loadHistory = async () => {
      try {
        await refreshHistory();
        if (active) setError("");
      } catch (reason) {
        if (active) {
          setError(
            reason instanceof Error
              ? reason.message
              : "Riwayat coding agent gagal dimuat.",
          );
        }
      } finally {
        if (active) setLoadingHistory(false);
      }
    };
    void loadHistory();
    const interval = window.setInterval(() => void loadHistory(), 5000);
    return () => {
      active = false;
      window.clearInterval(interval);
    };
  }, [refreshHistory]);

  useEffect(() => {
    if (!selectedJobId) return;
    let active = true;
    setLoadingJob(true);
    setJob(null);
    const loadJob = async () => {
      try {
        const response = await fetch(
          `/api/agent-tests/${encodeURIComponent(selectedJobId)}`,
          { cache: "no-store" },
        );
        const payload: unknown = await response.json();
        if (!response.ok || !isAgentJobSnapshot(payload)) {
          const detail =
            isRecord(payload) && typeof payload.error === "string"
              ? payload.error
              : `Gagal memuat pekerjaan (HTTP ${response.status}).`;
          throw new Error(detail);
        }
        if (!active) return;
        const goal =
          payload.goal ||
          historyRef.current.find((item) => item.id === selectedJobId)?.goal ||
          "Instruksi pekerjaan";
        const restoredMessages: ChatMessage[] = [
          {
            id: `goal-${payload.id}`,
            role: "user",
            content: goal,
          },
        ];
        if (!ACTIVE_STATUSES.has(payload.status)) {
          const result =
            payload.response || payload.error || payload.reason || "";
          if (result) {
            restoredMessages.push({
              id: `result-${payload.id}`,
              role: payload.status === "completed" ? "agent" : "system",
              content: result,
              jobId: payload.id,
              project: payload.project,
              files: payload.files,
            });
          }
        }
        setJob(payload);
        setMessages(restoredMessages);
        setError("");
      } catch (reason) {
        if (active) {
          setError(
            reason instanceof Error
              ? reason.message
              : "Pekerjaan coding agent gagal dimuat.",
          );
        }
      } finally {
        if (active) setLoadingJob(false);
      }
    };
    void loadJob();
    return () => {
      active = false;
    };
  }, [selectedJobId]);

  const jobId = job?.id;
  const jobStatus = job?.status;
  const jobIsActive = Boolean(jobStatus && ACTIVE_STATUSES.has(jobStatus));
  const hasActiveJob =
    jobIsActive || history.some((item) => ACTIVE_STATUSES.has(item.status));

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
        current ? { ...current, progress: payload.text } : current,
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
            if (!response.ok || !isAgentJobSnapshot(snapshot)) {
              const detail =
                isRecord(snapshot) && typeof snapshot.error === "string"
                  ? snapshot.error
                  : `Gagal memuat file hasil (HTTP ${response.status}).`;
              throw new Error(detail);
            }
            setMessages((current) =>
              current.map((message) =>
                message.id === messageId
                  ? {
                      ...message,
                      project: snapshot.project,
                      files: snapshot.files ?? [],
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
      setHistory((current) =>
        current.map((item) =>
          item.id === jobId ? { ...item, status } : item,
        ),
      );
      setStreamStatus("");
      setStopping(false);
      source.close();
      void refreshHistory().catch((reason: unknown) => {
        setError(
          reason instanceof Error
            ? reason.message
            : "Riwayat coding agent gagal diperbarui.",
        );
      });
    };
    source.addEventListener("completed", (event) => finish(event, "completed"));
    source.addEventListener("failed", (event) => finish(event, "failed"));
    source.addEventListener("cancelled", (event) => finish(event, "cancelled"));

    return () => {
      source.close();
    };
  }, [jobId, jobStatus, refreshHistory]);

  function startNewJob() {
    choseNewJob.current = true;
    setSelectedJobId(null);
    setJob(null);
    setMessages([]);
    setDraft("");
    setError("");
  }

  function selectJob(id: string) {
    if (id === selectedJobId) return;
    choseNewJob.current = false;
    setSelectedJobId(id);
    setJob(null);
    setMessages([]);
    setError("");
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const message = draft.trim();
    if (!message || sending || loadingHistory || hasActiveJob) return;

    setSending(true);
    setError("");
    setDraft("");
    setSelectedJobId(null);
    setJob(null);
    setMessages([
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
      const acceptedId = payload.id;
      const acceptedStatus = payload.status;
      setSelectedJobId(acceptedId);
      setJob({
        id: acceptedId,
        status: acceptedStatus,
        goal: message,
        progress: "Pekerjaan masuk antrean coding agent.",
      });
      setHistory((current) => [
        {
          id: acceptedId,
          status: acceptedStatus,
          goal: message,
          created_at: new Date().toISOString(),
        },
        ...current.filter((item) => item.id !== acceptedId),
      ]);
      void refreshHistory().catch((reason: unknown) => {
        setError(
          reason instanceof Error
            ? reason.message
            : "Riwayat coding agent gagal diperbarui.",
        );
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
            : `Pekerjaan belum dapat dihentikan (HTTP ${response.status}).`;
        throw new Error(detail);
      }
    } catch (reason) {
      setStopping(false);
      setError(
        reason instanceof Error
          ? reason.message
          : "Pekerjaan coding agent belum dapat dihentikan.",
      );
    }
  }

  return (
    <>
      <h1 className="visuallyHidden">Coding agent</h1>
      <section className="agentWorkspace" aria-label="Ruang kerja coding agent">
        <aside className="agentHistorySidebar">
          <div className="agentHistoryHeading">
            <div>
              <p className="eyebrow">CODING AGENT</p>
              <h2>Riwayat</h2>
            </div>
            <button
              aria-label="Instruksi baru"
              className="button agentNewJobIconButton"
              disabled={hasActiveJob}
              onClick={startNewJob}
              title="Instruksi baru"
              type="button"
            >
              <svg
                aria-hidden="true"
                fill="none"
                height="16"
                viewBox="0 0 16 16"
                width="16"
              >
                <path
                  d="M8 3.25v9.5M3.25 8h9.5"
                  stroke="currentColor"
                  strokeLinecap="round"
                  strokeWidth="1.6"
                />
              </svg>
            </button>
          </div>
          <div className="agentHistoryList" aria-label="Riwayat pekerjaan">
            {loadingHistory && history.length === 0 ? (
              <p className="emptyHint">Memuat riwayat...</p>
            ) : history.length === 0 ? (
              <div className="emptyIncident agentHistoryEmpty">
                <strong>Belum ada riwayat</strong>
              </div>
            ) : (
              history.map((item) => (
                <button
                  aria-pressed={item.id === selectedJobId}
                  className={`agentHistoryItem${item.id === selectedJobId ? " selected" : ""}`}
                  key={item.id}
                  onClick={() => selectJob(item.id)}
                  type="button"
                >
                  <span className="agentHistoryItemTop">
                    <span>{formatTime(item.created_at)}</span>
                    <span
                      className={`agentJobStatus agentJobStatus-${item.status}`}
                    >
                      {STATUS_LABELS[item.status] ?? item.status}
                    </span>
                  </span>
                  <strong>{item.goal}</strong>
                </button>
              ))
            )}
          </div>
          <div className="agentHistoryFooter">
            <span className={`statusDot${hasActiveJob ? "" : " agentIdleDot"}`} />
            <span>
              {hasActiveJob
                ? "Satu pekerjaan sedang diproses"
                : "Pekerjaan tersimpan di riwayat"}
            </span>
          </div>
        </aside>

        <section className="agentPlayground" aria-label="Chat dengan coding agent">
          <header className="playgroundHeader">
            <div>
              <h2>Coding agent</h2>
              <p>
                {job
                  ? `Job ${job.id}`
                  : selectedJobId
                    ? "Memuat pekerjaan..."
                    : "Pekerjaan baru"}
              </p>
            </div>
            <span className="agentPill">1 AGENT</span>
          </header>

          <div className="playgroundMessages" aria-live="polite">
            {loadingJob ? <p className="emptyHint">Memuat percakapan...</p> : null}
            {messages.map((message) => {
              const downloadJobId = message.jobId
                ? encodeURIComponent(message.jobId)
                : "";
              return (
                <article
                  className={`chatMessage message-${message.role}`}
                  key={message.id}
                >
                  <div className="markdownContent">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {message.content}
                    </ReactMarkdown>
                  </div>
                  {downloadJobId && message.files && message.files.length > 0 ? (
                    <section
                      className="assistantFiles"
                      aria-label="File hasil proyek"
                    >
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
                                <span className="assistantFileType">
                                  {extension}
                                </span>
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
              placeholder="Contoh: Perbaiki validasi input pada form login..."
              rows={1}
              value={draft}
            />
            <div className="composerFooter">
              <span>
                {hasActiveJob
                  ? "Tunggu pekerjaan aktif selesai sebelum mengirim instruksi baru."
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
                  disabled={
                    sending || loadingHistory || hasActiveJob || !draft.trim()
                  }
                  type="submit"
                >
                  {sending ? "Mengirim..." : "Kirim instruksi"}
                </button>
              )}
            </div>
          </form>
        </section>
      </section>
    </>
  );
}
