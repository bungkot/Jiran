import os
from types import SimpleNamespace

import pytest

from agent.llm import get_llm, ping_llm


class FakeLLM:
    def __init__(self, content=None, exc=None, delay=0.0):
        self.content, self.exc, self.delay = content, exc, delay

    def invoke(self, prompt):
        import time

        time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return SimpleNamespace(content=self.content)


def test_ping_sukses():
    r = ping_llm(FakeLLM("OK"))
    assert r.ok and r.response == "OK"


def test_ping_buang_blok_think():
    r = ping_llm(FakeLLM("<think>mikir dulu...</think>OK"))
    assert r.ok and r.response == "OK"


def test_ping_konten_berbentuk_list():
    r = ping_llm(FakeLLM([{"type": "text", "text": "OK"}]))
    assert r.ok and r.response == "OK"


def test_ping_respons_kosong_gagal():
    r = ping_llm(FakeLLM("   "))
    assert not r.ok and "kosong" in r.error


def test_ping_exception_tidak_crash():
    r = ping_llm(FakeLLM(exc=ConnectionError("refused")))
    assert not r.ok and "ConnectionError" in r.error


def test_ping_terlalu_lambat():
    r = ping_llm(FakeLLM("OK", delay=0.2), timeout_s=0.05)
    assert not r.ok and "lambat" in r.error


def test_get_llm_parameter(settings):
    llm = get_llm(settings)
    assert llm.model == settings.ollama_model
    assert llm.temperature == 0.0
    assert llm.base_url == settings.ollama_base_url


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("RUN_OLLAMA") != "1",
    reason="set RUN_OLLAMA=1 untuk uji Ollama sungguhan",
)
def test_ollama_sungguhan():
    from agent.config import Settings

    s = Settings.load()
    r = ping_llm(get_llm(s), timeout_s=s.llm_timeout_s)
    assert r.ok, r.error


def test_get_llm_meneruskan_num_ctx_dan_keep_alive(settings):
    from dataclasses import replace

    llm = get_llm(replace(settings, ollama_num_ctx=8192, ollama_keep_alive="10m"))
    assert llm.num_ctx == 8192
    assert llm.keep_alive == "10m"
