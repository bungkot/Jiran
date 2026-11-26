import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

export type TicketStatus = "open" | "in_progress" | "resolved" | "failed";
export type TicketMessageRole = "user" | "agent" | "system";

export interface TicketMessage {
  id: string;
  role: TicketMessageRole;
  content: string;
  createdAt: string;
}

export interface TicketTransaction {
  id: string;
  label: string;
  detail: string;
  createdAt: string;
}

export interface TicketJob {
  id: string;
  status: string;
  progress: string;
  progressSeq: number;
  createdAt: string;
  startedAt?: string;
  finishedAt?: string;
}

export interface Ticket {
  id: string;
  title: string;
  status: TicketStatus;
  source?: "self-heal";
  fingerprint?: string;
  createdAt: string;
  updatedAt: string;
  messages: TicketMessage[];
  transactions: TicketTransaction[];
  jobs: TicketJob[];
}

const dataDirectory =
  process.env.ITSM_DATA_DIR || path.join(process.cwd(), "data");
const dataFile = path.join(dataDirectory, "tickets.json");
let writeQueue: Promise<void> = Promise.resolve();

function now(): string {
  return new Date().toISOString();
}

function makeTransaction(label: string, detail: string): TicketTransaction {
  return { id: randomUUID(), label, detail, createdAt: now() };
}

function isTicket(value: unknown): value is Ticket {
  if (typeof value !== "object" || value === null) return false;
  const ticket = value as Record<string, unknown>;
  return (
    typeof ticket.id === "string" &&
    typeof ticket.title === "string" &&
    ["open", "in_progress", "resolved", "failed"].includes(
      String(ticket.status),
    ) &&
    typeof ticket.createdAt === "string" &&
    typeof ticket.updatedAt === "string" &&
    Array.isArray(ticket.messages) &&
    Array.isArray(ticket.transactions) &&
    Array.isArray(ticket.jobs)
  );
}

async function loadTickets(): Promise<Ticket[]> {
  try {
    const contents = await readFile(dataFile, "utf8");
    const tickets: unknown = JSON.parse(contents);
    if (!Array.isArray(tickets) || !tickets.every(isTicket)) {
      throw new Error("Format penyimpanan tiket tidak valid.");
    }
    return tickets;
  } catch (error) {
    if (error instanceof Error && "code" in error && error.code === "ENOENT") {
      return [];
    }
    throw error;
  }
}

async function persistTickets(tickets: Ticket[]): Promise<void> {
  await mkdir(dataDirectory, { recursive: true });
  const temporary = `${dataFile}.${randomUUID()}.tmp`;
  await writeFile(temporary, `${JSON.stringify(tickets)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  await rename(temporary, dataFile);
}

async function mutateTickets<T>(
  mutate: (tickets: Ticket[]) => T,
): Promise<T> {
  const operation = writeQueue.then(async () => {
    const tickets = await loadTickets();
    const result = mutate(tickets);
    await persistTickets(tickets);
    return result;
  });
  writeQueue = operation.then(
    () => undefined,
    () => undefined,
  );
  return operation;
}

export async function listTickets(): Promise<Ticket[]> {
  await writeQueue;
  return (await loadTickets()).sort((a, b) =>
    b.updatedAt.localeCompare(a.updatedAt),
  );
}

export async function getTicket(ticketId: string): Promise<Ticket | null> {
  const tickets = await listTickets();
  return tickets.find((ticket) => ticket.id === ticketId) ?? null;
}

export interface SystemIncidentUpdate {
  fingerprint: string;
  title: string;
  detail: string;
  active: boolean;
}

export async function syncSystemIncident(
  incident: SystemIncidentUpdate,
): Promise<Ticket | null> {
  return mutateTickets((tickets) => {
    const existing = tickets.find(
      (ticket) =>
        ticket.source === "self-heal" &&
        ticket.fingerprint === incident.fingerprint &&
        ticket.status !== "resolved",
    );

    if (!incident.active) {
      if (!existing) return null;
      existing.status = "resolved";
      addTicketMessage(existing, "system", incident.detail);
      addTicketTransaction(existing, "Insiden pulih", incident.detail);
      existing.updatedAt = now();
      return existing;
    }

    if (existing) return existing;

    const createdAt = now();
    const suffix = randomUUID().replaceAll("-", "").slice(0, 6).toUpperCase();
    const date = createdAt.slice(0, 10).replaceAll("-", "");
    const ticket: Ticket = {
      id: `INC-${date}-${suffix}`,
      title: incident.title,
      status: "in_progress",
      source: "self-heal",
      fingerprint: incident.fingerprint,
      createdAt,
      updatedAt: createdAt,
      messages: [
        {
          id: randomUUID(),
          role: "system",
          content: incident.detail,
          createdAt,
        },
      ],
      transactions: [
        makeTransaction("Insiden terdeteksi", incident.detail),
      ],
      jobs: [],
    };
    tickets.unshift(ticket);
    return ticket;
  });
}

export async function updateTicket(
  ticketId: string,
  update: (ticket: Ticket) => void,
): Promise<Ticket | null> {
  return mutateTickets((tickets) => {
    const ticket = tickets.find((item) => item.id === ticketId);
    if (!ticket) return null;
    update(ticket);
    ticket.updatedAt = now();
    return ticket;
  });
}

export function addTicketTransaction(
  ticket: Ticket,
  label: string,
  detail: string,
): void {
  ticket.transactions.push(makeTransaction(label, detail));
}

export function addTicketMessage(
  ticket: Ticket,
  role: TicketMessageRole,
  content: string,
): void {
  ticket.messages.push({
    id: randomUUID(),
    role,
    content,
    createdAt: now(),
  });
}
