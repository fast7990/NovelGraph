"""LLM 客户端（OpenAI 兼容协议，指向 360 网关）。

三点纪律：
1. 不写死厂商 —— 全部通过 base_url + model 注入，换成 DeepSeek 只改 .env 三行。
2. 不假设上游支持 JSON mode / 流式 —— 启动探测一次，把能力写进 capabilities，
   不支持就走提示词约束 + 括号配平修复，而不是直接 500。
3. 不假设上游有 prompt 缓存 —— 成本控制靠上下文预算（检索 ≤8K 字、图谱 ≤40 条、历史 ≤6 轮）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


class LLMClient:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key or "sk-placeholder",
            timeout=settings.llm_timeout,
            max_retries=2,
        )
        self.model = settings.llm_model
        self.fast_model = settings.fast_model
        self._sem = asyncio.Semaphore(max(1, settings.llm_max_concurrency))
        self._caps: dict[str, Any] = {"probed": False}

    # ---------- 能力探测 ----------
    async def probe(self) -> dict:
        if self._caps.get("probed"):
            return self._caps
        caps: dict[str, Any] = {"probed": True, "json_mode": False, "stream": False, "model": self.model}
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=8,
                temperature=0,
            )
            caps["stream"] = True
            caps["reachable"] = True
            caps["sample"] = (resp.choices[0].message.content or "")[:40]
        except Exception as exc:  # noqa: BLE001
            caps["reachable"] = False
            caps["error"] = str(exc)[:300]
            self._caps = caps
            return caps

        if settings.llm_json_mode != "false":
            try:
                await self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": '输出 {"ok":true} 的 JSON'}],
                    max_tokens=32,
                    temperature=0,
                    response_format={"type": "json_object"},
                )
                caps["json_mode"] = True
            except Exception as exc:  # noqa: BLE001
                caps["json_mode"] = False
                caps["json_error"] = str(exc)[:200]
        self._caps = caps
        logger.info("llm capabilities: %s", caps)
        return caps

    @property
    def capabilities(self) -> dict:
        return self._caps

    # ---------- 调用 ----------
    async def complete(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1500,
        json_mode: bool = False,
        model: str | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {}
        if json_mode and self._caps.get("json_mode"):
            kwargs["response_format"] = {"type": "json_object"}
        async with self._sem:
            resp = await self.client.chat.completions.create(
                model=model or self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
        return resp.choices[0].message.content or ""

    async def complete_json(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.0,
        max_tokens: int = 4000,
    ) -> dict:
        text = await self.complete(messages, temperature=temperature, max_tokens=max_tokens, json_mode=True)
        return parse_json_loose(text)

    async def stream(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.6,
        max_tokens: int = 3000,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        async with self._sem:
            stream = await self.client.chat.completions.create(
                model=model or self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )
            try:
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        yield delta.content
            finally:
                # 客户端断开时必须关掉上游连接，否则继续计费
                await stream.close()

    async def health(self) -> dict:
        caps = await self.probe()
        return {
            "name": "llm",
            "status": "ok" if caps.get("reachable") else "down",
            "base_url": settings.llm_base_url,
            "model": self.model,
            "json_mode": caps.get("json_mode", False),
            "error": caps.get("error"),
        }


def parse_json_loose(text: str) -> dict:
    """容错解析：模型偶尔会用 ``` 包裹，或前后带解释文字。"""
    if not text:
        return {}
    cleaned = _JSON_FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidate = cleaned[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # 括号配平修复：补齐截断的右括号
            fixed = candidate
            for opener, closer in (("{", "}"), ("[", "]")):
                if fixed.count(opener) > fixed.count(closer):
                    fixed += closer * (fixed.count(opener) - fixed.count(closer))
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                return {}
    return {}


_llm: LLMClient | None = None


def get_llm() -> LLMClient:
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm
