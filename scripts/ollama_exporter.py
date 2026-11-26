#!/usr/bin/env python3
"""Mini exporter Ollama untuk Prometheus: polling GET /api/version di Ollama.

Mengapa ini ada: Ollama 0.40.18 tidak menyediakan metrik Prometheus native
(/metrics 404), jadi kita lihat ketersediaan server dari sisi HTTP.

Metrik yang dikeluarkan (format Prometheus text):
  ollama_up{site}            1 jika Ollama merespons HTTP 200 pada /api/version
  ollama_response_ms{site}   waktu respons HTTP dalam milidetik

Pemakaian:
    python3 scripts/ollama_exporter.py --port 9191 --url http://localhost:11434
"""

from __future__ import annotations

import argparse
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer


def poll(base_url: str, timeout_s: float) -> tuple[int, int]:
    """Return (up, response_ms); up=0 saat error/timeout."""
    start = time.perf_counter()
    try:
        url = base_url.rstrip("/") + "/api/version"
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OLLAMA_BASE_URL harus berupa URL HTTP(S) yang valid")
        req = urllib.request.Request(url)  # noqa: S310 - scheme dibatasi ke HTTP(S)
        with urllib.request.urlopen(req, timeout=timeout_s) as response:  # noqa: S310
            status = response.status
            response.read()
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return 0, int((time.perf_counter() - start) * 1000)
    ms = int((time.perf_counter() - start) * 1000)
    return (1 if status == 200 else 0), ms


class Handler(BaseHTTPRequestHandler):
    """Sajikan metrik terakhir langsung dari polling pada masing-masing GET."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        # Poll lagi saat di-scrape supaya data segar (short timeout).
        up, ms = poll(self.server.base_url, 5.0)  # type: ignore[attr-defined]
        body = (
            f"# TYPE ollama_up gauge\nollama_up {up}\n"
            f"# TYPE ollama_response_ms gauge\nollama_response_ms {ms}\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # harus diam
        return


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--host",
        default="0.0.0.0",  # noqa: S104 - hanya di-expose pada jaringan internal Compose
    )
    parser.add_argument("--port", type=int, default=9191)
    parser.add_argument("--url", default="http://localhost:11434")
    args = parser.parse_args(argv)
    server = HTTPServer((args.host, args.port), Handler)
    server.base_url = args.url  # type: ignore[attr-defined]
    server.serve_forever()


if __name__ == "__main__":
    main()
