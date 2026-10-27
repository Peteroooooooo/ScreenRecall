"""大模型客户端：OpenAI 兼容的 /chat/completions 接口（httpx，同步）。

配置来自 .env 文件和环境变量：LLM_BASE_URL（含 /v1 之类的前缀）、LLM_API_KEY、LLM_MODEL。
优先级：环境变量 > 后面的 .env 文件 > 前面的 .env 文件（默认先读仓库根目录的 .env，再读 app_home()/.env）。

只在空闲整理进程和主进程里用，不进录制进程。密钥不进日志、不进 repr、不进错误信息。
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlsplit

import httpx

from . import paths

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_KEYS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
log = logging.getLogger("screenrecall.app.llm")


class LlmError(RuntimeError):
    """调用大模型失败（原因已去掉密钥）。"""


class LlmNotConfigured(LlmError):
    """没有配置大模型。"""


class LlmHttpError(LlmError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class LlmReply:
    text: str
    model: str
    endpoint_host: str
    usage: dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0


def default_env_files() -> list[Path]:
    return [REPO_ROOT / ".env", paths.app_home() / ".env"]


def read_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，# 开头是注释，值两边的引号去掉；文件不存在返回空。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


def _chat_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


class LlmClient:
    """OpenAI 兼容的对话接口。transport / sleep 供测试替换。"""

    def __init__(self, base_url: str, api_key: str, model: str, *, max_retries: int = 3,
                 backoff_s: float = 1.0, max_backoff_s: float = 20.0,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep):
        missing = [k for k, v in zip(ENV_KEYS, (base_url, api_key, model)) if not v]
        if missing:
            raise LlmNotConfigured("大模型没有配置：缺少 " + "、".join(missing)
                                   + "（写在仓库根目录或 %LOCALAPPDATA%\\ScreenRecall 下的 .env 里）")
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise LlmNotConfigured("LLM_BASE_URL 应为 http(s):// 开头的地址")
        self._url = _chat_url(base_url)
        self._key = api_key
        self.model = model
        self.endpoint_host = parts.hostname
        self.max_retries, self.backoff_s, self.max_backoff_s = max_retries, backoff_s, max_backoff_s
        self._transport, self._sleep = transport, sleep

    def __repr__(self) -> str:  # 不显示密钥和地址路径
        return f"LlmClient(host={self.endpoint_host!r}, model={self.model!r})"

    @classmethod
    def from_env(cls, env_files: Iterable[Path] | None = None, environ: dict[str, str] | None = None,
                 **kwargs: Any) -> "LlmClient":
        merged: dict[str, str] = {}
        for f in (default_env_files() if env_files is None else env_files):
            merged.update({k: v for k, v in read_env_file(Path(f)).items() if k in ENV_KEYS and v})
        env = os.environ if environ is None else environ
        merged.update({k: env[k] for k in ENV_KEYS if env.get(k)})
        return cls(merged.get("LLM_BASE_URL", ""), merged.get("LLM_API_KEY", ""), merged.get("LLM_MODEL", ""),
                   **kwargs)

    def _redact(self, s: str) -> str:
        return s.replace(self._key, "***") if self._key else s

    def _wait_s(self, attempt: int, resp: httpx.Response | None) -> float:
        if resp is not None:
            ra = resp.headers.get("retry-after", "")
            try:
                return min(self.max_backoff_s, max(0.0, float(ra)))
            except ValueError:
                pass
        return min(self.max_backoff_s, self.backoff_s * (2 ** attempt))

    def chat(self, messages: Sequence[dict[str, Any]], *, max_tokens: int = 512, temperature: float = 0.2,
             timeout: float = 60.0, reasoning_effort: str | None = None, model: str | None = None) -> LlmReply:
        """发一次对话，返回第一条回答。429 / 5xx / 网络错误按指数退避重试（尊重 Retry-After）。

        reasoning_effort：思考强度（"none" / "low" / ...），None 表示用模型默认。"""
        body: dict[str, Any] = {"model": model or self.model, "messages": list(messages),
                                "max_tokens": max_tokens, "temperature": temperature}
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        t0 = time.monotonic()
        last: Exception | None = None
        with httpx.Client(timeout=timeout, transport=self._transport) as client:
            for attempt in range(self.max_retries + 1):
                resp = None
                try:
                    resp = client.post(self._url, json=body, headers=headers)
                except httpx.TransportError as exc:  # 连接失败、超时
                    last = LlmError(f"连接大模型接口失败（{self.endpoint_host}）：{type(exc).__name__}")
                else:
                    if resp.status_code < 400:
                        return self._parse(resp, time.monotonic() - t0)
                    last = LlmHttpError(resp.status_code, self._http_message(resp))
                    if resp.status_code not in RETRY_STATUS:
                        raise last
                if attempt < self.max_retries:
                    wait = self._wait_s(attempt, resp)
                    log.info("大模型接口暂时不可用（%s），%.1f 秒后重试", last, wait)
                    self._sleep(wait)
        assert last is not None
        raise last

    def _http_message(self, resp: httpx.Response) -> str:
        detail = ""
        try:
            err = resp.json().get("error")
            detail = err.get("message", "") if isinstance(err, dict) else str(err or "")
        except (ValueError, AttributeError):
            detail = resp.text[:200]
        return self._redact(f"大模型接口返回 HTTP {resp.status_code}（{self.endpoint_host}）" +
                            (f"：{detail[:300]}" if detail else ""))

    def _parse(self, resp: httpx.Response, elapsed: float) -> LlmReply:
        try:
            data = resp.json()
            msg = data["choices"][0]["message"]
            text = msg.get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise LlmError(f"大模型接口的回答格式不对（{self.endpoint_host}）") from None
        if isinstance(text, list):  # 少数兼容接口把内容拆成多段
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        return LlmReply(text=str(text), model=str(data.get("model") or self.model), endpoint_host=self.endpoint_host,
                        usage=dict(data.get("usage") or {}), elapsed_s=round(elapsed, 3))


def main() -> int:
    """python -m screenrecall.app.llm：用一句无关内容试一次接口，只打印模型名和用时。"""
    try:
        client = LlmClient.from_env()
        reply = client.chat([{"role": "user", "content": "Reply with the single word OK."}], max_tokens=16,
                            temperature=0, reasoning_effort="none")
    except LlmError as exc:
        print(f"失败：{exc}")
        return 1
    print(f"成功：模型 {reply.model}，接口 {reply.endpoint_host}，用时 {reply.elapsed_s:.2f} 秒，回答 {reply.text.strip()[:20]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
