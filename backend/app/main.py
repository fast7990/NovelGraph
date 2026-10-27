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
    await runner.recover_stale()  # 先把上一次残留的 RUNNING 任务翻成 FAILED，再起 worker
    runner.start()
    logger.info(
        "启动完成 env=%s vector=%s embedding=%s threads=%s",
        settings.app_env,
        settings.milvus_collection,
        settings.embedding_provider,
        settings.embedding_threads,
    )
    # Embedder 同步预热：模型缺失或维度与配置不符属于致命错误，
    # 必须让启动失败。丢到后台 task 里会被 except 吞掉，服务照常起来，
    # 等到第一次入库才炸，排查成本高得多。大模型加载 2-5s，这个代价值得。
    from app.services.embedding.errors import FatalModelError
    from app.services.embedding.factory import get_embedder

    try:
        emb = get_embedder()
        await emb.embed_query("预热")
        logger.info("embedding 预热完成 model=%s dim=%s", settings.embedding_model, emb.dim)
    except FatalModelError as exc:
        logger.critical("Embedding 配置错误，拒绝启动：%s", exc)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("embedding 预热失败：%s", exc)

    async def warmup() -> None:
        from app.services.llm.client import get_llm
        from app.services.vector.factory import get_vector_store

        try:
            store = await get_vector_store()
            logger.info("vector backend = %s collection=%s", getattr(store, "name", "?"), settings.milvus_collection)
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
