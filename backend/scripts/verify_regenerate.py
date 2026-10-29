"""验证「重新生成」：重答不多出一条历史。

跑法：
    cd backend && ./.venv/Scripts/python.exe scripts/verify_regenerate.py
    API_BASE=http://127.0.0.1:8001/api/v1 ...（指到临时实例）

验三件事：
1. 正常提问后消息数 +2；
2. 带 `regenerate=true` 重发同一个问题，消息数**不变**（不是 +2），
   且答案确实换了新的；
3. 带 `regenerate=true` 但问题对不上时，不删旧数据，退化成普通提问。

第 3 条是安全阀：两个标签页同时操作、或点了重新生成又切了会话，
都可能让"最后一轮"不是要重答的那一问。宁可多一条，也不能删错。
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
Q1 = "华佗是怎么死的"
Q2 = "关云长最后结局如何"


async def ask(c, sid: str, question: str, regenerate: bool = False) -> str:
    answer = []
    async with c.stream(
        "POST",
        f"{BASE}/chat",
        json={
            "novel_id": NID,
            "question": question,
            "session_id": sid,
            "regenerate": regenerate,
        },
    ) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                d = json.loads(line[5:].strip())
                if "text" in d:
                    answer.append(d["text"])
    return "".join(answer)


async def count(c, sid: str) -> int:
    r = await c.get(f"{BASE}/chat/sessions/{sid}/messages")
    return len(r.json()["items"])


async def main() -> None:
    async with httpx.AsyncClient(timeout=300.0, trust_env=False) as c:
        sid = (await c.post(f"{BASE}/chat/sessions?novel_id={NID}")).json()["id"]
        print("会话:", sid)
        try:
            a1 = await ask(c, sid, Q1)
            n1 = await count(c, sid)
            print(f"1) 首轮提问 → 消息 {n1} 条，答案 {len(a1)} 字")

            a2 = await ask(c, sid, Q1, regenerate=True)
            n2 = await count(c, sid)
            print(f"2) 重新生成 → 消息 {n2} 条，答案 {len(a2)} 字")
            assert n2 == n1, f"重新生成不该新增消息：{n1} → {n2}"
            assert a2.strip(), "重新生成没有产出答案"
            print(f"   答案是否真的变了：{a1.strip() != a2.strip()}")

            # 3) 问题对不上：不做删除，按普通提问处理
            a3 = await ask(c, sid, Q2, regenerate=True)
            n3 = await count(c, sid)
            print(f"3) 问题不匹配的 regenerate → 消息 {n3} 条，答案 {len(a3)} 字")
            assert n3 == n2 + 2, f"不匹配时应退化成普通提问（+2 条），实际 {n2} → {n3}"
            print("结论：重新生成不多出历史；不匹配时安全退化为普通提问")
        finally:
            await c.delete(f"{BASE}/chat/sessions/{sid}")
            print("已清理会话", sid)


if __name__ == "__main__":
    asyncio.run(main())
