"""诊断：长对话的滚动摘要 + 会话 token 台账。

跑法：
    cd backend && ./.venv/Scripts/python.exe scripts/diag_chat_memory.py [novel_id]

它会造一个临时会话、灌 12 轮对话，走一遍真实的摘要链路，然后打印：
- 摘要正文与它覆盖到哪条消息（summary_seq）
- 下一轮真正会进 prompt 的历史块
- 会话行的 token 用量
最后删掉这个临时会话，不留垃圾。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import ChatSession, Novel  # noqa: E402
from app.services.chat import memory  # noqa: E402

# 造数据用：内容要有"可压缩的信息"，纯占位符测不出摘要质量
SCRIPT = [
    ("华佗是谁", "华佗，字元化，沛国谯郡人，是三国时期的名医。"),
    ("他擅长什么", "华佗擅长外科，能用麻沸散做剖腹手术，也精通内科与针灸。"),
    ("他给谁看过病", "曾为关羽刮骨疗毒，也为曹操诊治头风之疾。"),
    ("曹操为什么杀他", "华佗想回家，托辞妻子有病，被曹操下狱拷问，最终死于狱中。"),
    ("他的著作有哪些", "资料提到《青囊书》，临死前想传给狱吏，狱吏不敢受，华佗焚之。"),
    ("关羽和他什么关系", "华佗为关羽刮骨疗毒，二人因此有一面之缘。"),
    ("刮骨疗毒在哪一回", "第七十五回，关羽中箭后华佗前来医治。"),
    ("他死在哪一回", "第七十八回「治风疾神医身死」。"),
    ("当时曹操什么病", "头风之疾，华佗建议开颅，曹操疑心他要害自己。"),
    ("还有别的名医吗", "资料未提及与华佗齐名的其他医者。"),
    ("他和董承有关系吗", "资料未提及华佗与董承有往来。"),
    ("总结一下这个人", "名医，外科见长，因不愿做侍医而被曹操所杀，著作失传。"),
]


async def _pick_novel() -> str:
    async with SessionLocal() as db:
        novel = (await db.execute(__import__("sqlalchemy").select(Novel).limit(1))).scalars().first()
        if novel is None:
            raise SystemExit("库里没有小说，先入库一本")
        return novel.id


async def main() -> None:
    novel_id = sys.argv[1] if len(sys.argv) > 1 else await _pick_novel()
    async with SessionLocal() as db:
        sess = ChatSession(novel_id=novel_id, title="[诊断] 摘要链路")
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        sid = sess.id
    print(f"临时会话 {sid}（novel={novel_id}）")

    try:
        for q, a in SCRIPT:
            await memory.save_turn(sid, q, a, [])
        print(f"已灌入 {len(SCRIPT)} 轮对话")

        changed = await memory.maybe_summarize(sid)
        summary, covered = await memory.load_summary(sid)
        print(f"\n是否触发压缩: {changed}")
        print(f"摘要覆盖到 seq={covered}")
        print(f"摘要（{len(summary)} 字）:\n{summary or '（无）'}")

        turns = await memory.load_turns(sid)
        turns = [t for t in turns if int(t.get("seq") or 0) > covered]
        block = memory.build_memory_block(turns, summary)
        print(f"\n下一轮进 prompt 的历史块（{len(turns) // 2} 轮原文 + 摘要）:")
        print(block[:900])

        async with SessionLocal() as db:
            row = await db.get(ChatSession, sid)
            print(
                f"\ntoken 台账: prompt={row.prompt_tokens} completion={row.completion_tokens} "
                f"calls={row.llm_calls}"
            )
        print(f"\n摘要阈值: trigger={settings.chat_summary_trigger_turns} 轮 "
              f"keep={settings.chat_summary_keep_turns} 轮 model={settings.chat_summary_model}")
    finally:
        async with SessionLocal() as db:
            obj = await db.get(ChatSession, sid)
            if obj is not None:
                await db.delete(obj)
                await db.commit()
        from sqlalchemy import text

        async with SessionLocal() as db:
            await db.execute(text("DELETE FROM chat_messages WHERE session_id = :s"), {"s": sid})
            await db.commit()
        print("临时会话已清理")


if __name__ == "__main__":
    asyncio.run(main())
