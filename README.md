# Jiran — coding agent lokal dengan eksekusi terisolasi

[![CI](https://github.com/bungkot/Jiran/actions/workflows/ci.yml/badge.svg)](https://github.com/bungkot/Jiran/actions/workflows/ci.yml)

Jiran adalah coding agent lokal, stack-neutral, yang bekerja dengan model Ollama
dan menjalankan perintah verifikasi di dalam container Docker sekali pakai.
Nama paket Python-nya `coding-agent`; titik masuk CLI `coding-agent`.

## Arsitektur singkat

```
CLI / klien HTTP
      │
      ▼
ITSM (tiket, chat, audit trail)
      │
      ▼
Agent service tunggal (antrean + satu worker)
      │
      ▼
Goal loop  ── planner heuristik ──▶ todo per file
      │                         (konteks segar tiap todo)
      ▼
Agent worker (tool calling berbatas)
      │
      ├── file tools (sandbox path, secret guard, O_NOFOLLOW)
      └── run_command ──▶ Docker: read-only, cap-drop, no-new-privileges,
                          pids/mem/cpu limit, /tmp noexec, network none
```

Modul utama ada di `src/agent/`:

| File | Peran |
|---|---|
| `config.py` | `Settings` + validasi env (fail-closed) |
| `preflight.py` | Gate lingkungan (Python, workspace, Docker, Ollama, model) |
| `llm.py` | `ChatOllama` + ping |
| `agent.py` | Worker tool-calling, compaction, retry, verifikasi |
| `goal.py` | Goal loop, planner, acceptance, judge, deteksi mandek |
| `service.py` | HTTP server + antrean + satu worker per container |
| `service_client.py` | Klien submit/poll |
| `container_client.py` | Menjalankan goal di dalam image agent |
| `tools/` | Sandbox, file/edit tools, Docker runner |

## Menjalankan

### Ollama sudah berjalan di host

```bash
uv sync --extra dev
export AGENT_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run coding-agent preflight
uv run coding-agent all
uv run coding-agent goal --project demo --verify "python -m unittest discover -q" \
  --require-file calc.py "Buat calc.py dengan fungsi tambah dan kali"
```

### Menjalankan seluruh stack lewat Compose

Compose canonical ada di `docker-compose.yaml`. Jalankan dari root repository:

```bash
export AGENT_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose up -d --build
uv run coding-agent submit "Buat kalkulator CLI"
uv run coding-agent job <id>
```

`AGENT_API_TOKEN` **wajib** di Compose karena service bind ke `0.0.0.0` di dalam
container dan endpoint `/goals` dapat memicu agent menulis file serta menjalankan
perintah. Token minimal 32 karakter.

Compose menjalankan satu coding agent langsung pada port `8765` di loopback
host; load balancer dan autoscaler tidak aktif. Semua job diproses oleh satu
worker dan diserialisasi dalam antrean yang sama. CLI di host memakai
`AGENT_SERVICE_URL=http://localhost:8765`; CLI dari container memakai
`http://agent-1:8765`. Job/antrean berada di memori dan hilang saat container
dimulai ulang. Job yang menargetkan proyek yang sama diserialisasi dengan file
lock bersama. Definisi worker tambahan, Nginx, dan autoscaler tetap tersedia
di profile `legacy-scaling`, tetapi tidak dijalankan oleh Compose default.

### ITSM dan testing coding agent

Compose juga menjalankan aplikasi ITSM Next.js di `http://localhost:3000`.
Halaman utama memisahkan daftar insiden otomatis dari area uji coding agent.
Self-healing membuat atau memperbarui tiket saat layanan gagal atau metrik
resource melewati ambang batas, lalu menandai tiket pulih saat kondisi kembali
normal. Tiket service down diberi severity tinggi, sedangkan ambang resource
diberi severity sedang. Severity rendah, sedang, dan tinggi ditampilkan
masing-masing dengan warna hijau, kuning, dan merah; tiket lama tanpa severity
ditampilkan sebagai sedang. Pesan di playground mengirim job langsung ke coding
agent dan tidak membuat tiket. Tiket disimpan pada volume Docker `itsm_data`;
riwayat chat playground hanya berlaku selama halaman terbuka. Progres agent
dikirim bertahap ke chat melalui Server-Sent Events (SSE); jawaban akhir muncul
setelah job selesai.
`ITSM_SYSTEM_TOKEN` dapat diisi token khusus untuk laporan self-healing; bila
kosong, Compose memakai `AGENT_API_TOKEN`.

Port ITSM hanya bind ke loopback host (`127.0.0.1`) secara default. Jangan
mengubahnya menjadi port publik sebelum autentikasi pengguna dan HTTPS
disiapkan: akses chat dapat memicu coding agent mengubah file dan menjalankan
perintah. Token agent hanya digunakan server ITSM dan tidak dikirim ke browser.
Port dapat diubah lewat `ITSM_PORT`.

API Ollama dipublish hanya ke loopback host pada port `OLLAMA_PORT` (default
`11434`) agar CLI dan benchmark lokal dapat mengaksesnya tanpa mengeksposnya ke
jaringan.

Image Ollama default adalah `ollama/ollama:latest`. Untuk Docker dengan ruang
penyimpanan terbatas atau host tanpa GPU, `OLLAMA_IMAGE=ollama/ollama:rocm`
memilih image yang lebih kecil; backend CPU tetap tersedia.

### Monitoring Prometheus dan Grafana (opsional)

Aktifkan hanya saat dibutuhkan:

```bash
docker compose --profile monitoring up -d
```

Grafana tersedia di `http://localhost:3001` (dashboard Jiran sudah disediakan);
Prometheus tersedia di `http://localhost:9090`. Kedua port hanya bind ke
loopback host. Dashboard Grafana dapat dilihat tanpa login, tetapi aksesnya read-only.
Prometheus menyimpan data selama 7 hari.

Prometheus mengumpulkan metrik antrean, job selesai, dan durasi dari agent tunggal,
serta metrik CPU, memori, dan filesystem host dari Node Exporter. Endpoint
`/metrics` hanya tersedia di jaringan internal Compose.
Node Exporter memerlukan mount read-only `/`, `/proc`, dan `/sys` untuk membaca
metrik host. Batas memori container monitoring adalah 896 MiB total; hentikan
saat tidak diperlukan dengan
`docker compose stop grafana prometheus node-exporter`.

## Perintah CLI

| Perintah | Fungsi |
|---|---|
| `preflight`, `ping`, `tools`, `all` | Pemeriksaan lingkungan |
| `goal ...` | Jalankan goal loop lokal |
| `goal --container ...` | Goal loop di dalam image Docker |
| `goal --json ...` | Output JSON untuk otomasi/benchmark |
| `run --project ...` | Kerjakan proyek yang sudah ada di workspace |
| `serve` | Jalankan service standby |
| `submit ... [--no-wait]` | Kirim goal ke service |
| `job <id>` | Lihat status job |
| `container-build` | Build image agent |

Argumen pertama yang bukan subcommand dianggap sebagai goal dan dikirim ke
service standby: `coding-agent "Buat kalkulator CLI"`.

## Model keamanan

- **File**: semua path divalidasi terhadap workspace; traversal, path absolut,
  null byte, dan symlink escape ditolak. File sensitif (`.env*`, kredensial,
  kunci, `secrets.*`) tidak bisa dibaca/ditulis. Penulisan memakai
  `O_NOFOLLOW` + pengecekan inode untuk menutup celah TOCTOU.
- **Perintah**: dijalankan di container dengan `--read-only`, `--cap-drop=ALL`,
  `--security-opt=no-new-privileges`, batas pids/memori/CPU, `/tmp` tmpfs
  `noexec,nodev,nosuid`, network `none`, dan file sensitif ditutup dengan
  `/dev/null` atau tmpfs. Symlink yang mengarah keluar workspace **menolak**
  eksekusi.
- **Service**: bind loopback secara default. Bind non-loopback tanpa token
  ditolak. Bearer token case-insensitive, `max_iterations` dibatasi 1..50,
  antrean maksimal 100 job (429 saat penuh), error internal tidak diekspos
  lewat API.
- **Log**: `logs/run-*.jsonl` berizin `0600`; metadata job tidak menyimpan
  `goal`/`response`. Pangkas log lama dengan
  `python scripts/cleanup_logs.py --days 14 --dry-run`.

### Batas yang perlu diketahui

- `run_command` adalah shell bebas **di dalam container** dengan workspace
  mount `rw`. Prompt injection dari isi repo masih dapat mengubah file proyek.
- Compose mount Docker socket karena verifikasi berjalan di container bersarang.
  Akses ke daemon Docker setara root host; jangan ekspos endpoint `/goals`
  tanpa token dan jangan jalankan pada repo yang tidak dipercaya.
- `AGENT_CONTAINER_DOCKER_SOCKET` (default `false`) khusus untuk mode
  `goal --container`. Menyalakannya memberi container goal akses daemon host.
- `--storage-opt size=` membatasi writable layer container, **bukan** bind mount
  `/workspace`. Untuk mencegah disk host penuh, pakai
  `AGENT_SANDBOX_MIN_FREE_DISK_MB`.

## Konfigurasi penting

Lihat `.env.example` untuk daftar lengkap. Yang sering diubah:

| Env | Default | Catatan |
|---|---|---|
| `OLLAMA_IMAGE` | `ollama/ollama:latest` | Image runtime Ollama; `ollama/ollama:rocm` lebih kecil untuk storage terbatas |
| `OLLAMA_PORT` | `11434` | Port API Ollama host; bind hanya ke `127.0.0.1` |
| `OLLAMA_MODEL` | `qwen3.5:2b-q4_K_M` | Model lokal default; varian 4B lebih kuat tetapi lebih besar |
| `OLLAMA_NUM_CTX` | `8192` | Sesuaikan dengan RAM host |
| `MAX_CONTEXT_CHARS` | `12000` | Anggaran total riwayat pesan |
| `MAX_AGENT_STEPS` / `MAX_AGENT_TOOL_CALLS` | `15` / `30` | Batas worker |
| `MAX_GOAL_ITER` / `MAX_GOAL_ITER_LIMIT` | `5` / `50` | Batas putaran |
| `REQUIRE_JUDGE_ON_FAST_PATH` | `false` | `true` = judge tetap dipanggil walau verifikasi mesin lulus |
| `AGENT_SANDBOX_NETWORK` | `none` | `bridge` hanya bila memang perlu |
| `AGENT_SANDBOX_MEMORY_MB` / `AGENT_SANDBOX_CPUS` | `256` / `1` (Compose) | Batas per sandbox; naikkan hanya jika RAM tersedia |
| `AGENT_API_TOKEN` | kosong | Wajib bila bind non-loopback |
| `AGENT_SERVICE_PORT` | `8765` | Port agent di host, bind ke `127.0.0.1` |
| `AGENT_SANDBOX_MIN_FREE_DISK_MB` | `256` | Tolak tulis bila disk hampir penuh |
| `AGENT_SANDBOX_DISK_QUOTA_MB` | `0` | Kuota writable layer container |

## Pengembangan

```bash
uv sync --extra dev
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy src/agent
uv run pytest -q --cov
```

CI menjalankan gate di atas pada Python 3.11 dan 3.13. Tes integrasi
(`RUN_OLLAMA=1`, `RUN_AGENT_E2E=1`) dijalankan manual lewat
`workflow_dispatch` karena membutuhkan Ollama + Docker sungguhan.

Benchmark model:

```bash
python scripts/benchmark_models.py qwen3.5:2b-q4_K_M
```

Skrip mengembalikan exit code 1 bila ada tugas/model yang gagal sehingga bisa
dipakai sebagai gate regresi. Secara otomatis ia memakai `docker exec` ke
container `agent` (atau `jiran` pada deployment lama); set
`BENCH_AGENT_CONTAINER` untuk nama kustom, atau biarkan kosong untuk fallback
menjalankan `python -m agent.main` di host.
