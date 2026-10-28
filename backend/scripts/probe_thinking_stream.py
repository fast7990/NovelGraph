"""探针：确认流式下 reasoning_content 能不能拿到、长什么样。

背景：`LLMClient.stream()` 目前只转发 `delta.content`，把思考过程直接丢掉了。
要展示思考过程，必须先确认两件事：
1. 网关在 `stream=True` 时会不会把思考增量放进 delta（而不是只在非流式返回里给）；
2. openai SDK 对未知字段的暴露方式 —— `delta.reasoning_content` 还是 `delta.model_extra[...]`。

跑法：cd backend && uv run python scripts/probe_thinking_stream.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.services.llm.client import get_llm  # noqa: E402


async def main() -> None:
    llm = get_llm()
    caps = await llm.probe()
    print("caps:", {k: v for k, v in caps.items() if k != "probed"})

    msgs = [{"role": "user", "content": "简述《三国演义》里赤壁之战的起因，三句话。"}]
    for thinking in (True, False):
        t0 = time.time()
        n_reason = n_content = 0
        first_reason_at = first_content_at = None
        sample: list[str] = []
        body = llm._thinking_body(thinking)
        print(f"\n===== thinking={thinking} extra_body={body} =====")
        stream = await llm.client.chat.completions.create(
            model=settings.llm_model,
            messages=msgs,
            temperature=0.6,
            max_tokens=4096,
            stream=True,
            extra_body=body or None,
        )
        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                d = chunk.choices[0].delta
                extra = getattr(d, "model_extra", None) or {}
                rc = getattr(d, "reasoning_content", None) or extra.get("reasoning_content")
                if rc:
                    n_reason += len(rc)
                    if first_reason_at is None:
                        first_reason_at = time.time() - t0
                    if len(sample) < 12:
                        sample.append(rc)
                if d and d.content:
                    n_content += len(d.content)
                    if first_content_at is None:
                        first_content_at = time.time() - t0
                fin = chunk.choices[0].finish_reason
                if fin:
                    print("finish_reason:", fin)
        finally:
            await stream.close()
        print(f"reasoning_chars={n_reason} content_chars={n_content}")
        print(f"首思考 {first_reason_at}s / 首正文 {first_content_at}s / 总 {time.time() - t0:.1f}s")
        print("思考片段:", repr("".join(sample))[:300])


if __name__ == "__main__":
    asyncio.run(main())
