"""诊断：流式请求能不能拿到 usage（决定问答 token 能否记账）。

跑法：
    cd backend && ./.venv/Scripts/python.exe scripts/diag_stream_usage.py

背景：OpenAI 兼容网关默认**不会**在流式响应里回 usage，必须显式请求
`stream_options.include_usage`。不同网关支持程度不一，所以先探一次，
而不是假设它一定行。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.llm.client import get_llm  # noqa: E402


async def main() -> None:
    llm = get_llm()
    tag = "diag-stream-usage"
    chunks = 0
    async for kind, text in llm.stream_events(
        [{"role": "user", "content": "用一个字回答：1+1=?"}],
        max_tokens=64,
        usage_tag=tag,
    ):
        chunks += 1
        if kind == "content":
            print("正文片段:", repr(text))
    usage = llm.pop_usage(tag)
    print("流式帧数:", chunks)
    print("用量:", usage)
    if usage.get("usage_missing"):
        print("结论：网关不回流式 usage —— 问答侧只能记 calls，token 会显示为 0")
    else:
        print("结论：网关支持 include_usage，token 可正常记账")


if __name__ == "__main__":
    asyncio.run(main())
