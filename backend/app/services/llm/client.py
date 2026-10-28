"""LLM 客户端（OpenAI 兼容协议，指向 360 网关）。

四点纪律：
1. 不写死厂商 —— 全部通过 base_url + model 注入，换成 DeepSeek 只改 .env 三行。
2. 不假设上游支持 JSON mode / 流式 —— 启动探测一次，把能力写进 capabilities，
   不支持就走提示词约束 + 括号配平修复，而不是直接 500。
3. 不假设上游有 prompt 缓存 —— 成本控制靠上下文预算（检索 ≤8K 字、图谱 ≤40 条、历史 ≤6 轮）。
4. **不假设模型不是推理模型**。`z-ai/glm-5` 会把思考过程写进 `reasoning_content`，
   而 `content` 要等思考结束才有值——这两个字段共用 `max_tokens` 预算。
   实测：一句"用一句话说你好"，`max_tokens=200` 时 200 个 token 全花在思考上，
   `content` 是空串、`finish_reason=length`。图谱抽取这种结构化任务因此整块返回空。

   解法是显式关掉思考：`extra_body={"thinking": {"type": "disabled"}}`，实测 4.9s → 1.6s，
   且 reasoning 长度归零。这个开关**必须探测**而不是硬写——换到不支持它的网关时
   会直接 400，那时要能退化成"放大 max_tokens 兜住思考开销"。

   （补充：360 网关实测 `thinking_switch=False`，开关不被接受，glm-5 一直会思考。
   于是问答侧干脆把思考增量也流给前端 —— 实测首思考 1.0s、首正文 14.9s，
   中间那十几秒原本是纯空白。见 `stream_events()`。）
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

# 各家网关给思考字段起的名字不统一：DeepSeek 官方与多数中转是 reasoning_content，
# 也有网关叫 reasoning。openai SDK 对未知字段只塞进 model_extra、不设属性，
# 所以下面两种访问方式都要试一遍（实测 360 网关走属性那条路）。
_REASONING_KEYS = ("reasoning_content", "reasoning")


def _delta_reasoning(delta: Any) -> str:
    for key in _REASONING_KEYS:
        val = getattr(delta, key, None)
        if val:
            return val
    extra = getattr(delta, "model_extra", None) or {}
    for key in _REASONING_KEYS:
        val = extra.get(key)
        if val:
            return val
    return ""


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
        caps: dict[str, Any] = {
            "probed": True,
            "json_mode": False,
            "stream": False,
            "thinking_switch": False,
            "is_reasoning": False,
            "model": self.model,
        }
        # 先用"关思考"跑通一次普通调用：既能验证连通性，又能确认开关是否被接受。
        # 探针 prompt 必须是**正常句子**——实测 `"ping"` 这种超短输入，
        # 网关会绕过 thinking 开关照样推理（reasoning_len=114、content 空），
        # 用它判定会把开关误判成"不支持"，进而白白放大三倍 max_tokens。
        probe_msg = "用一句话打个招呼"
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": probe_msg}],
                max_tokens=256,
                temperature=0,
                extra_body=self._thinking_body(False),
            )
            caps["stream"] = True
            caps["reachable"] = True
            msg = resp.choices[0].message
            content = msg.content or ""
            reasoning = getattr(msg, "reasoning_content", None) or ""
            caps["sample"] = content[:40]
            # 判据：关思考后**既有正文又没有思考残留**，才算开关生效
            caps["thinking_switch"] = bool(content) and not reasoning
            caps["is_reasoning"] = bool(reasoning)
        except Exception as exc:  # noqa: BLE001
            caps["reachable"] = False
            caps["error"] = str(exc)[:300]
            # 开关不被接受时（400），回退到不带开关再试一次
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": probe_msg}],
                    max_tokens=512,
                    temperature=0,
                )
                msg = resp.choices[0].message
                caps["reachable"] = True
                caps["stream"] = True
                caps["sample"] = (msg.content or "")[:40]
                caps["is_reasoning"] = bool(getattr(msg, "reasoning_content", None)) or not msg.content
                caps["thinking_switch"] = False
                caps["error"] = None
            except Exception as exc2:  # noqa: BLE001
                caps["error"] = f"{caps['error']} | retry: {exc2}"[:400]
                self._caps = caps
                return caps

        if settings.llm_json_mode != "false":
            try:
                await self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": '输出 {"ok":true} 的 JSON'}],
                    max_tokens=200,
                    temperature=0,
                    response_format={"type": "json_object"},
                    extra_body=self._thinking_body(False),
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

    def _thinking_body(self, enable: bool | None) -> dict:
        """生成 extra_body。开关未被网关接受时返回空 dict（探测结果说了算）。"""
        if not self._caps.get("probed"):
            # 探测之前先按配置试，探测本身就用它来验证开关
            if enable is None:
                enable = settings.llm_thinking
            return {"thinking": {"type": "enabled" if enable else "disabled"}}
        if not self._caps.get("thinking_switch"):
            return {}
        if enable is None:
            enable = settings.llm_thinking
        return {"thinking": {"type": "enabled" if enable else "disabled"}}

    # ---------- 调用 ----------
    async def complete(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1500,
        json_mode: bool = False,
        model: str | None = None,
        thinking: bool | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {}
        if json_mode and self._caps.get("json_mode"):
            kwargs["response_format"] = {"type": "json_object"}
        body = self._thinking_body(thinking)
        async with self._sem:
            resp = await self.client.chat.completions.create(
                model=model or self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                extra_body=body or None,
                **kwargs,
            )
        choice = resp.choices[0]
        content = choice.message.content or ""
        if not content and getattr(choice.message, "reasoning_content", None):
            # 思考把预算吃光了。这是上游行为，不该静默返回空串让调用方以为"没内容"
            logger.warning(
                "LLM 返回空正文（reasoning 占满 max_tokens=%s, finish=%s, model=%s）",
                max_tokens,
                choice.finish_reason,
                model or self.model,
            )
        return content

    async def complete_json(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.0,
        max_tokens: int = 4000,
        thinking: bool | None = False,
        model: str | None = None,
    ) -> dict:
        text = await self.complete(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
            thinking=thinking,
            model=model,
        )
        return parse_json_loose(text)

    async def stream_events(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.6,
        # 推理模型的思考与正文共用这份预算。glm-5 实测思考会占 500-1500 token，
        # 3000 的默认值会把长回答截断，所以抬高到 4096。
        max_tokens: int = 4096,
        model: str | None = None,
        thinking: bool | None = None,
        include_reasoning: bool = True,
    ) -> AsyncIterator[tuple[str, str]]:
        """流式增量的**完整**视图：yield `(kind, text)`，kind 为 reasoning / content。

        单独留这个接口而不是给 `stream()` 加返回值，是因为两者语义不同：
        抽取、摘要这类调用方只关心正文，多出来的思考通道反而要它们自己过滤；
        问答则要拿思考过程去做"首字前的空窗期"提示。
        """
        body = self._thinking_body(thinking)
        async with self._sem:
            stream = await self.client.chat.completions.create(
                model=model or self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                extra_body=body or None,
            )
            try:
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta is None:
                        continue
                    if include_reasoning:
                        reasoning = _delta_reasoning(delta)
                        if reasoning:
                            yield "reasoning", reasoning
                    if delta.content:
                        yield "content", delta.content
            finally:
                # 客户端断开时必须关掉上游连接，否则继续计费
                await stream.close()

    async def stream(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.6,
        max_tokens: int = 4096,
        model: str | None = None,
        thinking: bool | None = None,
    ) -> AsyncIterator[str]:
        """只要正文。思考过程被丢弃——需要它请用 `stream_events()`。"""
        async for kind, text in self.stream_events(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
            thinking=thinking,
        ):
            if kind == "content":
                yield text

    async def health(self) -> dict:
        caps = await self.probe()
        return {
            "name": "llm",
            "status": "ok" if caps.get("reachable") else "down",
            "base_url": settings.llm_base_url,
            "model": self.model,
            "json_mode": caps.get("json_mode", False),
            "thinking_switch": caps.get("thinking_switch", False),
            "error": caps.get("error"),
        }


def parse_json_loose(text: str) -> dict:
    """容错解析：模型偶尔会用 ``` 包裹、前后带解释文字，或者输出被 max_tokens 截断。

    截断是最麻烦的一种：`...{"src":"马元义","dst":"封谞"...` 在字符串中间断掉，
    直接补括号没用（引号没闭合）。这里的做法是从**最后一个完整的 `}`** 往前退，
    逐个试"补 `]}` / `]` / `}`"，能闭合就收——等于丢掉被截断的最后一个元素，
    保住前面所有完整的。一份 10 个实体的结果救回 9 个，比整块作废好得多。
    """
    if not text:
        return {}
    cleaned = _JSON_FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    if start < 0:
        return {}
    end = cleaned.rfind("}")
    if end > start:
        candidate = cleaned[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            fixed = candidate
            for opener, closer in (("{", "}"), ("[", "]")):
                if fixed.count(opener) > fixed.count(closer):
                    fixed += closer * (fixed.count(opener) - fixed.count(closer))
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass

    # 截断救回：只在最后一个 } 之后没有合法结尾时才走这条
    body = cleaned[start:]
    braces = [m.start() for m in re.finditer(r"\}", body)]
    for pos in reversed(braces[-60:]):
        head = body[: pos + 1]
        for suffix in ("]}", "]", "}"):
            try:
                obj = json.loads(head + suffix)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj:
                logger.warning("JSON 被截断，已救回前半部分（丢弃末尾不完整元素）")
                return obj
    return {}


_llm: LLMClient | None = None


def get_llm() -> LLMClient:
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm
