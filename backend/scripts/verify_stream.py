"""流式问答的端到端回归：id 从 meta 就能用，done 不再换 id。

盯四件事：
1. meta 事件里带 `message_id`，且开流时库里确实已有这条消息（内容为空）
2. 正文是**逐帧**到达的，不是末尾一次吐出（delta 帧数 > 1，首末帧有间隔）
3. done 的 message_id 与 meta 一致（前端因此不需要在末尾换 id、正文不会闪）
4. done 之后这条消息内容完整、可以直接打反馈（PATCH 返 200）

跑法：backend/.venv/Scripts/python.exe scripts/verify_stream.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"
NOVEL = "nv_ddef13761e244a2f"
QUESTION = "用两三句话说清楚关羽的性格"


async def main() -> int:
    # trust_env=False：本机设了 http_proxy，走代理测 127.0.0.1 会拿到假 502
    async with httpx.AsyncClient(timeout=300, trust_env=False) as cli:
        meta: dict = {}
        deltas: list[tuple[float, int]] = []
        done: dict = {}
        t0 = time.perf_counter()
        async with cli.stream(
            "POST", f"{BASE}/chat", json={"novel_id": NOVEL, "question": QUESTION}
        ) as resp:
            resp.raise_for_status()
            event = ""
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = json.loads(line[5:].strip())
                    el = time.perf_counter() - t0
                    if event == "meta":
                        meta = data
                        print(
                            f"[{el:6.2f}s] meta  message_id={data.get('message_id')!r} "
                            f"cites={len(data.get('citations') or [])} "
                            f"show_reasoning={data.get('show_reasoning')}"
                        )
                    elif event == "delta":
                        deltas.append((el, len(data.get("text") or "")))
                    elif event == "done":
                        done = data
                        print(
                            f"[{el:6.2f}s] done  message_id={data.get('message_id')!r} "
                            f"chars={data.get('chars')} ttft={data.get('ttft_ms')}ms"
                        )

        print()
        if deltas:
            span = deltas[-1][0] - deltas[0][0]
            print(
                f"delta 帧 {len(deltas)} 个，首帧 {deltas[0][0]:.2f}s，"
                f"末帧 {deltas[-1][0]:.2f}s，跨越 {span:.2f}s，"
                f"平均每帧 {sum(n for _, n in deltas) / len(deltas):.0f} 字"
            )

        ok = True

        def check(name: str, cond: bool, extra: str = "") -> None:
            nonlocal ok
            print(("  OK  " if cond else "  FAIL") + f"  {name}" + (f"  {extra}" if extra else ""))
            ok = ok and cond

        mid = meta.get("message_id") or ""
        check("meta 带 message_id", bool(mid), mid)
        check("delta 是多帧而非一次性", len(deltas) >= 2, f"{len(deltas)} 帧")
        if len(deltas) >= 2:
            check("首末 delta 有明显时间跨度", (deltas[-1][0] - deltas[0][0]) > 0.3)
        check("done 与 meta 的 id 一致", done.get("message_id") == mid)

        msgs = (await cli.get(f"{BASE}/chat/sessions/{meta.get('session_id')}/messages")).json()
        item = next((m for m in msgs["items"] if m["id"] == mid), None)
        check("库里能查到这条答案", item is not None)
        if item:
            check("答案内容已落库", len(item["content"]) == done.get("chars"))
        check("轮次只有一问一答", len(msgs["items"]) == 2, f'{len(msgs["items"])} 条')

        fb = await cli.patch(f"{BASE}/chat/messages/{mid}/feedback", json={"feedback": "up"})
        check("拿 meta 的 id 直接打反馈", fb.status_code == 200, str(fb.status_code))

        # 清场：把这次验证造的会话删掉，别在用户的会话列表里留垃圾
        await cli.delete(f"{BASE}/chat/sessions/{meta.get('session_id')}")
    print("\n结果：" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
