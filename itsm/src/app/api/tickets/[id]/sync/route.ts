import { getAgentJob, type AgentJobSnapshot } from "@/lib/agent";
import {
  addTicketMessage,
  addTicketTransaction,
  getTicket,
  updateTicket,
} from "@/lib/tickets";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const ACTIVE_STATUSES = new Set(["queued", "running"]);
const TERMINAL_STATUSES = new Set(["completed", "failed"]);

export async function POST(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id } = await context.params;
  const existing = await getTicket(id);
  if (!existing) {
    return Response.json({ error: "Tiket tidak ditemukan." }, { status: 404 });
  }

  const snapshots: AgentJobSnapshot[] = [];
  try {
    for (const job of existing.jobs) {
      if (ACTIVE_STATUSES.has(job.status)) {
        snapshots.push(await getAgentJob(job.id));
      }
    }
  } catch (error) {
    console.error(`ITSM gagal menyinkronkan tiket ${id}`, error);
    return Response.json(
      { error: "Status coding agent belum dapat diperbarui." },
      { status: 502 },
    );
  }

  const updated = await updateTicket(id, (ticket) => {
    for (const snapshot of snapshots) {
      const job = ticket.jobs.find((item) => item.id === snapshot.id);
      if (!job) continue;

      const previousStatus = job.status;
      const nextStatus =
        typeof snapshot.status === "string" ? snapshot.status : previousStatus;
      const nextProgress =
        typeof snapshot.progress === "string" ? snapshot.progress : job.progress;
      const nextSequence =
        typeof snapshot.progress_seq === "number"
          ? snapshot.progress_seq
          : job.progressSeq;
      const statusChanged = nextStatus !== previousStatus;
      const progressChanged =
        nextSequence !== job.progressSeq && Boolean(nextProgress);

      job.status = nextStatus;
      job.progress = nextProgress;
      job.progressSeq = nextSequence;
      job.startedAt = snapshot.started_at ?? job.startedAt;
      job.finishedAt = snapshot.finished_at ?? job.finishedAt;

      if (statusChanged) {
        addTicketTransaction(
          ticket,
          `Status job: ${nextStatus}`,
          `Job ${job.id} berubah dari ${previousStatus} menjadi ${nextStatus}.`,
        );
      }
      if (progressChanged) {
        addTicketTransaction(ticket, "Progres coding agent", nextProgress);
      }
      if (
        TERMINAL_STATUSES.has(nextStatus) &&
        !TERMINAL_STATUSES.has(previousStatus)
      ) {
        const reply =
          snapshot.response ||
          snapshot.reason ||
          snapshot.error ||
          `Job berakhir dengan status ${nextStatus}.`;
        addTicketMessage(ticket, "agent", reply);
        addTicketTransaction(
          ticket,
          nextStatus === "completed" ? "Pekerjaan selesai" : "Pekerjaan gagal",
          reply,
        );
      }
    }

    const latestJob = ticket.jobs.at(-1);
    if (ticket.jobs.some((job) => ACTIVE_STATUSES.has(job.status))) {
      ticket.status = "in_progress";
    } else if (latestJob?.status === "completed") {
      ticket.status = "resolved";
    } else if (latestJob && TERMINAL_STATUSES.has(latestJob.status)) {
      ticket.status = "failed";
    }
  });

  return Response.json(updated, {
    headers: { "Cache-Control": "no-store" },
  });
}
