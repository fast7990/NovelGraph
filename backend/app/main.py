"""FastAPI 入口。

启动顺序有讲究：线程数环境变量必须在导入 onnxruntime 之前设置，
所以 `apply_runtime_env()` 放在最顶部，任何业务 import 之前。
"""

from __future__ import annotations

from app.config import apply_runtime_env

apply_runtime_env()  # noqa: E402  必须早于 onnxruntime / numpy 的导入

import asyncio  # noqa: E402
import logging  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.api import chat, jobs, novels, system  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.workers import runner  # noqa: E402

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger("app")

FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    runner.start()
    logger.info(
        "启动完成 env=%s vector=%s embedding=%s threads=%s",
        settings.app_env,
        settings.milvus_collection,
        settings.embedding_provider,
        settings.embedding_threads,
    )
    # 预热放到后台：模型加载 1-3s，不能挡住 first request
    async def warmup() -> None:
        from app.services.embedding.factory import get_embedder
        from app.services.llm.client import get_llm
        from app.services.vector.factory import get_vector_store

        try:
            emb = get_embedder()
            await emb.embed_query("预热")
            logger.info("embedding 预热完成 dim=%s", emb.dim)
        except Exception as exc:  # noqa: BLE001
            logger.warning("embedding 预热失败：%s", exc)
        try:
            store = await get_vector_store()
            logger.info("vector backend = %s", getattr(store, "name", "?"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("向量库初始化失败：%s", exc)
        try:
            await get_llm().probe()
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 探测失败：%s", exc)

    task = asyncio.create_task(warmup())
    yield
    task.cancel()
    await runner.stop()


app = FastAPI(
    title="长篇小说 RAG 中台",
    version="0.1.0",
    description="原始文本 → 向量库（Milvus 3.0）→ RAG 问答；知识图谱在 P1 接入",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (novels.router, jobs.router, chat.router, system.router):
    app.include_router(router, prefix=settings.api_prefix)


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIST / "index.html")


@app.get("/api/v1/ping")
async def ping() -> dict:
    return {"pong": True, "env": settings.app_env}
