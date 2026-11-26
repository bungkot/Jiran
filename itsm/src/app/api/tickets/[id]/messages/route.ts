import { submitAgentGoal } from "@/lib/agent";
import {
  addTicketMessage,
  addTicketTransaction,
  getTicket,
  updateTicket,
} from "@/lib/tickets";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_MESSAGE_LENGTH = 4000;

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id } = await context.params;
  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return Response.json({ error: "Request body must be valid JSON." }, { status: 400 });
  }
  const message =
    typeof payload === "object" &&
    payload !== null &&
    "message" in payload &&
    typeof payload.message === "string"
      ? payload.message.trim()
      : "";
  if (!message) {
    return Response.json({ error: "A message is required." }, { status: 400 });
  }
  if (message.length > MAX_MESSAGE_LENGTH) {
    return Response.json(
      { error: `Messages must be ${MAX_MESSAGE_LENGTH} characters or fewer.` },
      { status: 413 },
    );
  }

  const existing = await getTicket(id);
  if (!existing) {
    return Response.json({ error: "Ticket not found." }, { status: 404 });
  }
  const contextMessages = existing.messages
    .filter((item) => item.role !== "system")
    .slice(-4)
    .map((item) => `${item.role}: ${item.content.slice(0, 1000)}`)
    .join("\n");
  const goal = [
    `Ticket ${existing.id}: ${existing.title}`,
    "Previous conversation:",
    contextMessages || "(none)",
    "Latest request:",
    message,
  ].join("\n\n");

  await updateTicket(id, (ticket) => {
    addTicketMessage(ticket, "user", message);
    ticket.status = "in_progress";
    addTicketTransaction(
      ticket,
      "Message added",
      "Follow-up request recorded.",
    );
  });
  try {
    const accepted = await submitAgentGoal(goal);
    if (!accepted.id || !accepted.status) {
      throw new Error("The coding-agent response is missing a task ID or status.");
    }
    const updated = await updateTicket(id, (ticket) => {
      ticket.jobs.push({
        id: accepted.id,
        status: accepted.status,
        progress: "Request queued for the coding agent.",
        progressSeq: 0,
        createdAt: new Date().toISOString(),
      });
      addTicketTransaction(
        ticket,
        "Sent to coding agent",
        `Task ${accepted.id} was accepted with status ${accepted.status}.`,
      );
    });
    return Response.json(updated, { status: 202 });
  } catch (error) {
    console.error(`ITSM failed to send a message for ticket ${id} to the coding agent`, error);
    const updated = await updateTicket(id, (ticket) => {
      ticket.status = "failed";
      addTicketMessage(
        ticket,
        "system",
        "The coding agent could not accept the message. Check the service connection and try again.",
      );
      addTicketTransaction(
        ticket,
        "Delivery failed",
        "The coding agent did not accept the task. The conversation was saved.",
      );
    });
    return Response.json(
      {
        ticket: updated,
        error: "The coding agent could not accept the request.",
      },
      { status: 502 },
    );
  }
}
