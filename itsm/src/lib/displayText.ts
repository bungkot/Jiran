const incidentText: Record<string, string> = {
  "Insiden pulih": "Incident recovered",
  "Insiden terdeteksi": "Incident detected",
  "Pekerjaan selesai": "Task completed",
  "Pekerjaan gagal": "Task failed",
  "Progres coding agent": "Agent progress",
  "Pesan ditambahkan": "Message added",
  "Dikirim ke coding agent": "Sent to coding agent",
  "Pengiriman gagal": "Delivery failed",
  "Permintaan lanjutan dicatat.": "Follow-up request recorded.",
  "Coding agent tidak menerima job; percakapan tetap tersimpan.":
    "The coding agent did not accept the job. The conversation was saved.",
  "Coding agent tidak dapat menerima pesan. Periksa koneksi layanan lalu coba lagi.":
    "The coding agent could not accept the message. Check the service connection and try again.",
  "Coding agent tidak dapat menerima permintaan.":
    "The coding agent could not accept the request.",
};

const agentProgress: Record<string, string> = {
  "Agent dimulai": "Agent started.",
  "Job masuk antrean": "Task queued.",
  "Menganalisa goal dan menyusun tugas":
    "Analyzing the request and planning tasks.",
  "Rencana gagal; goal dikerjakan sebagai satu tugas":
    "Planning failed. The request will be handled as a single task.",
  "Respons model kosong; meminta ringkasan sekali lagi":
    "The model returned an empty response. Requesting a summary again.",
  "Judge sedang menilai hasil": "Reviewing the result.",
  "Penilaian judge diterima": "Review received.",
};

const agentProgressPatterns: Array<[RegExp, string]> = [
  [/^Goal dimulai — proyek (.+), batas (.+) putaran$/, "Goal started — project $1, up to $2 iterations."],
  [/^Rencana dibuat — (.+) tugas kecil$/, "Plan created — $1 tasks."],
  [/^Tugas (.+)\/(.+) dimulai$/, "Task $1 of $2 started."],
  [/^Tugas (.+) selesai$/, "Task $1 completed."],
  [/^Tugas (.+) belum selesai$/, "Task $1 is not complete."],
  [/^Putaran (.+) dimulai$/, "Iteration $1 started."],
  [/^Putaran (.+) selesai — tercapai$/, "Iteration $1 completed — goal met."],
  [/^Putaran (.+) selesai — belum tercapai$/, "Iteration $1 completed — goal not met."],
  [/^Putaran (.+) gagal$/, "Iteration $1 failed."],
  [/^Menunggu model \(langkah (.+), percobaan (.+)\)$/, "Waiting for the model (step $1, attempt $2)."],
  [/^Model meminta tool: (.+)$/, "Model requested tool: $1."],
  [/^Respons model diterima \(langkah (.+)\)$/, "Model response received (step $1)."],
  [/^Model gagal: (.+)$/, "Model failed: $1."],
  [/^Judge gagal: (.+)$/, "Reviewer failed: $1."],
  [/^Mencoba ulang permintaan model \(percobaan (.+)\)$/, "Retrying the model request (attempt $1)."],
  [/^Menjalankan (.+)$/, "Running $1."],
  [/^Tool (.+) selesai$/, "Tool $1 completed."],
  [/^Tool (.+) gagal$/, "Tool $1 failed."],
  [/^Memverifikasi dengan (.+)$/, "Verifying with $1."],
  [/^Verifikasi (.+): LULUS$/, "Verification with $1: passed."],
  [/^Verifikasi (.+): GAGAL$/, "Verification with $1: failed."],
  [/^Kriteria penerimaan terpenuhi(?: \(tanpa judge\))?$/, "Acceptance criteria met."],
  [/^Kriteria penerimaan belum terpenuhi(?: \(tanpa judge\))?$/, "Acceptance criteria not met."],
  [/^Goal tercapai dalam (.+) putaran$/, "Goal completed in $1 iterations."],
  [/^Goal belum tercapai \((.+)\)$/, "Goal not met ($1)."],
  [/^Agent selesai \((.+)\)$/, "Agent finished ($1)."],
];

export function formatAgentProgress(message: string): string {
  const exact = Object.hasOwn(agentProgress, message)
    ? agentProgress[message]
    : undefined;
  if (exact) return exact;

  for (const [pattern, replacement] of agentProgressPatterns) {
    if (pattern.test(message)) return message.replace(pattern, replacement);
  }
  return message;
}

export function formatIncidentText(message: string): string {
  const exact = Object.hasOwn(incidentText, message)
    ? incidentText[message]
    : undefined;
  if (exact) return exact;

  const jobStatus = message.match(/^Status job: (.+)$/);
  if (jobStatus) return `Task status: ${formatJobStatus(jobStatus[1])}`;

  const jobChanged = message.match(
    /^Job (.+) berubah dari (.+) menjadi (.+)\.$/,
  );
  if (jobChanged) {
    return `Task ${jobChanged[1]} changed from ${formatJobStatus(jobChanged[2])} to ${formatJobStatus(jobChanged[3])}.`;
  }

  const serviceDown = message.match(/^Service (.+) down$/);
  if (serviceDown) return `Service ${serviceDown[1]} is down`;

  const criticalResource = message.match(/^Resource (.+) kritis$/);
  if (criticalResource) return `Critical resource: ${criticalResource[1]}`;

  const unhealthyService = message.match(
    /^Self-healing mendeteksi service (.+) tidak sehat\.$/,
  );
  if (unhealthyService) {
    return `Self-healing detected that service ${unhealthyService[1]} is unhealthy.`;
  }

  const recoveredService = message.match(/^Service (.+) kembali sehat\.$/);
  if (recoveredService) return `Service ${recoveredService[1]} has recovered.`;

  const resourceUpdate = message.match(
    /^([a-z0-9_]+)=([\d.]+)% \((kembali normal|melewati ambang kritis); ambang ([\d.]+)%\)\.$/,
  );
  if (resourceUpdate) {
    const [, name, value, state, threshold] = resourceUpdate;
    const stateText =
      state === "kembali normal"
        ? `back to normal; threshold ${threshold}%`
        : `above the critical threshold of ${threshold}%`;
    return `${name}=${value}% (${stateText}).`;
  }

  return formatAgentProgress(message);
}

export function formatJobStatus(status: string): string {
  const labels: Record<string, string> = {
    queued: "Queued",
    running: "In progress",
    cancelling: "Stopping",
    completed: "Completed",
    failed: "Failed",
    cancelled: "Canceled",
  };
  return Object.hasOwn(labels, status) ? labels[status] : status;
}
