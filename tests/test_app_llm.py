"""v2 主进程：OpenAI 兼容大模型客户端（httpx.MockTransport，不联网）。"""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from screenrecall.app import llm

KEY = "sk-test-SECRET-123456"
BASE = "https://relay.example/v1"


def ok_body(text="OK", model="m-1"):
    return {"model": model, "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1}}


def make(handler, **kw):
    sleeps = []
    c = llm.LlmClient(BASE, KEY, "m-1", transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw)
    return c, sleeps


def test_chat_sends_openai_request_and_parses_reply():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=ok_body("  OK "))

    c, _ = make(handler)
    r = c.chat([{"role": "user", "content": "hi"}], max_tokens=8, temperature=0, reasoning_effort="none")
    assert seen["url"] == BASE + "/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"] == {"model": "m-1", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8,
                            "temperature": 0, "reasoning_effort": "none"}
    assert r.text == "  OK " and r.model == "m-1" and r.endpoint_host == "relay.example"
    assert r.usage["completion_tokens"] == 1


def test_temperature_none_is_omitted():
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=ok_body())

    c, _ = make(handler)
    c.chat([{"role": "user", "content": "hi"}], temperature=None)
    assert "temperature" not in seen["body"] and "reasoning_effort" not in seen["body"]


def test_retries_429_and_5xx_with_backoff_and_retry_after():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"message": "slow down"}})
        if len(calls) == 2:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=ok_body())

    c, sleeps = make(handler, backoff_s=0.5)
    assert c.chat([{"role": "user", "content": "x"}]).text == "OK"
    assert sleeps == [2.0, 1.0]  # Retry-After 优先；之后按 0.5 * 2^1


def test_gives_up_after_max_retries():
    c, sleeps = make(lambda req: httpx.Response(502, text="bad gateway"), max_retries=2, backoff_s=0.1)
    with pytest.raises(llm.LlmHttpError) as e:
        c.chat([{"role": "user", "content": "x"}])
    assert e.value.status == 502 and len(sleeps) == 2


def test_client_errors_are_not_retried_and_key_is_redacted():
    c, sleeps = make(lambda req: httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}}))
    with pytest.raises(llm.LlmHttpError) as e:
        c.chat([{"role": "user", "content": "x"}])
    assert sleeps == [] and KEY not in str(e.value) and "***" in str(e.value)


def test_network_errors_retry_then_fail_without_key(caplog):
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    c, sleeps = make(handler, max_retries=1)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(llm.LlmError) as e:
            c.chat([{"role": "user", "content": "x"}])
    assert len(sleeps) == 1 and "ConnectError" in str(e.value)
    assert KEY not in caplog.text and KEY not in repr(c) and "/v1" not in repr(c)


def test_bad_reply_shape():
    c, _ = make(lambda req: httpx.Response(200, json={"nope": 1}))
    with pytest.raises(llm.LlmError, match="格式"):
        c.chat([{"role": "user", "content": "x"}])


def test_from_env_precedence(tmp_path):
    a, b = tmp_path / "a.env", tmp_path / "b.env"
    a.write_text("# comment\nLLM_BASE_URL=https://a.example/v1\nLLM_API_KEY='ka'\nLLM_MODEL=ma\nOTHER=1\n",
                 encoding="utf-8")
    b.write_text('LLM_MODEL="mb"\nLLM_API_KEY=\n', encoding="utf-8")
    c = llm.LlmClient.from_env([a, b, tmp_path / "missing.env"], environ={})
    assert (c.endpoint_host, c.model, c._key) == ("a.example", "mb", "ka")  # 后面的文件覆盖前面的，空值不覆盖
    c = llm.LlmClient.from_env([a, b], environ={"LLM_MODEL": "env-model"})
    assert c.model == "env-model"  # 环境变量最优先


def test_not_configured_message(tmp_path):
    with pytest.raises(llm.LlmNotConfigured) as e:
        llm.LlmClient.from_env([tmp_path / "none.env"], environ={})
    assert "LLM_BASE_URL" in str(e.value) and "LLM_API_KEY" in str(e.value)
    with pytest.raises(llm.LlmNotConfigured):
        llm.LlmClient("ftp://x", "k", "m")


def test_full_chat_url_kept():
    c = llm.LlmClient("http://h:1/v1/chat/completions/", "k", "m")
    assert c._url == "http://h:1/v1/chat/completions"
