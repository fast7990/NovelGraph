"""验证：问答 token 是否落账到会话行（跑一轮真实 SSE）。

跑法：
    cd backend && ./.venv/Scripts/python.exe scripts/verify_chat_usage.py
    # 指到别的实例（例如临时起的 8001）：
    API_BASE=http://127.0.0.1:8001/api/v1 ./.venv/Scripts/python.exe scripts/verify_chat_usage.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000/api/v1")
NID = os.environ.get("NOVEL_ID", "nv_ddef13761e244a2f")


async def main() -> None:
    async with httpx.AsyncClient(timeout=300.0, trust_env=False) as c:
        r = await c.post(f"{BASE}/chat/sessions?novel_id={NID}")
        sid = r.json()["id"]
        print("会话:", sid)
        async with c.stream(
            "POST",
            f"{BASE}/chat",
            json={"novel_id": NID, "question": "华佗是怎么死的", "session_id": sid},
        ) as resp:
            ev = None
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    d = json.loads(line[5:].strip())
                    if ev == "meta":
                        print("meta memory=", d.get("memory"))
                    elif ev == "done":
                        print("done chars=", d.get("chars"))
                        # done 里必须直接带用量：前端靠它在这一轮答完的瞬间显示消耗，
                        # 不带的话就得再查一次列表，落账与刷新之间的时序差会让用户以为没记上
                        u = d.get("usage") or {}
                        print("done usage.round=", u.get("round"))
                        print("done usage.total=", u.get("total"))
        # 等落账
        await asyncio.sleep(3)
        lst = (await c.get(f"{BASE}/chat/sessions?novel_id={NID}")).json()["items"]
        me = next(x for x in lst if x["id"] == sid)
        print(
            f"台账: prompt={me.get('prompt_tokens')} "
            f"completion={me.get('completion_tokens')} "
            f"calls={me.get('llm_calls')} "
            f"summary_chars={me.get('summary_chars')}"
        )
        assert me.get("prompt_tokens", 0) > 0, "问答 token 没记上"
        print("结论：问答 token 已落账")


if __name__ == "__main__":
    asyncio.run(main())
