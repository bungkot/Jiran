"""FASE 1 — Koneksi LLM (ChatOllama) + smoke test.

Gate lulus: invoke() menghasilkan respons non-kosong dalam batas timeout.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .config import Settings
from .schemas import LLMPingResult

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def get_llm(settings: Settings | None = None, **overrides: Any) -> Any:
    """Buat ChatOllama. Temperature 0 agar keluaran agent konsisten."""
    from langchain_ollama import ChatOllama  # import lambat: ditunda

    s = settings or Settings.load()
    params: dict[str, Any] = {
        "model": s.ollama_model,
        "base_url": s.ollama_base_url,
        "temperature": s.llm_temperature,
        # Tanpa num_ctx Ollama memakai context default yang kecil dan diam-diam
        # memotong prompt (termasuk system prompt) pada riwayat tool yang panjang.
        "num_ctx": s.ollama_num_ctx,
        # Jaga model tetap di memori antar putaran; cold-load memicu timeout.
        "keep_alive": s.ollama_keep_alive,
        "client_kwargs": {"timeout": s.llm_timeout_s},
    }
    params.update(overrides)
    return ChatOllama(**params)


def _content_to_text(content: Any) -> str:
    """Isi pesan bisa berupa str atau list blok; ratakan jadi teks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


def ping_llm(
    llm: Any,
    prompt: str = "Balas hanya dengan satu kata: OK",
    timeout_s: float = 60.0,
) -> LLMPingResult:
    """Uji koneksi: kirim prompt singkat dan ukur latensi. Tidak pernah melempar exception."""
    start = time.perf_counter()
    try:
        msg = llm.invoke(prompt)
    except Exception as e:  # noqa: BLE001 - smoke test harus melaporkan apa pun
        return LLMPingResult(
            ok=False,
            latency_s=time.perf_counter() - start,
            error=f"{type(e).__name__}: {e}",
        )
    latency = time.perf_counter() - start
    text = _THINK_RE.sub("", _content_to_text(getattr(msg, "content", msg))).strip()

    if not text:
        return LLMPingResult(
            ok=False,
            latency_s=latency,
            error="respons kosong (model 'thinking'? coba model non-reasoning)",
        )
    if latency > timeout_s:
        return LLMPingResult(
            ok=False,
            latency_s=latency,
            response=text,
            error=f"terlalu lambat: {latency:.1f}s > {timeout_s:.0f}s",
        )
    return LLMPingResult(ok=True, latency_s=latency, response=text)
