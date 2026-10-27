"""全局配置。所有可调参数集中在此，代码里不出现魔法值。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 服务 ----
    app_env: str = "dev"
    api_prefix: str = "/api/v1"
    cors_origins: str = "http://localhost:5173"
    log_level: str = "INFO"

    # ---- 数据库（PostgreSQL + pgvector，元数据与向量同库）----
    database_url: str = "postgresql+asyncpg://novel_rag:novel_rag@127.0.0.1:5432/novel_rag"

    # ---- 存储 ----
    storage_backend: str = "local"
    local_storage_dir: str = "./data/uploads"

    # ---- 向量库（pgvector）----
    vector_backend: str = "pgvector"   # pgvector | numpy（断网兜底）
    pgvector_table: str = "chunk_vectors"
    pgvector_index: str = "hnsw"       # hnsw | ivfflat | none
    # HNSW 构建参数：m 越大精度越高、内存越大；ef_construction 影响建索引时间
    pgvector_hnsw_m: int = 16
    pgvector_hnsw_ef_construction: int = 64
    # 查询期：ef_search 越大召回越高、越慢；iterative_scan 解决"带 novel_id 过滤时
    # HNSW 先取候选后过滤导致结果不足"的问题（pgvector 0.8+ 才有）
    pgvector_hnsw_ef_search: int = 100
    pgvector_iterative_scan: str = "relaxed_order"   # off | relaxed_order | strict_order

    # ---- Embedding ----
    embedding_provider: str = "auto"
    embedding_model: str = "BAAI/bge-large-zh-v1.5"
    # ModelScope 上要拉哪个仓库（BAAI 官方仓库只放了 pytorch 权重，ONNX 用 Xenova 导出）
    embedding_ms_repo: str = "Xenova/bge-large-zh-v1.5"
    # 仓库内的 ONNX 相对路径；int8 量化版体积 1/4、CPU 快 2-3 倍
    embedding_onnx_file: str = "onnx/model_quantized.onnx"
    embedding_dim: int = 1024
    embedding_batch: int = 16
    embedding_threads: int = 8
    embedding_max_tokens: int = 512
    hf_endpoint: str = "https://hf-mirror.com"
    embedding_cache_dir: str = "./data/models"

    # ---- LLM ----
    llm_base_url: str = "https://api.360.cn/v1"
    llm_api_key: str = ""
    llm_model: str = "z-ai/glm-5"
    llm_model_fast: str = ""
    llm_timeout: int = 120
    llm_max_concurrency: int = 4
    llm_json_mode: str = "auto"

    # ---- RAG ----
    chunk_parent_size: int = 1400
    chunk_child_size: int = 400
    chunk_overlap: int = 60
    retrieve_top_k: int = 20
    context_parents: int = 8
    enable_chapter_summary: bool = False

    # ---- 混合检索（P1-M6）----
    # 词法通道：SQLite FTS5 + 自建 bigram（中文两字词是 trigram 的死角，见 lexical.py）
    lexical_enabled: bool = True
    # 两路召回的候选数，取大一点给 RRF 和 rerank 留余量；过大只会拖慢，不影响准确率
    retrieve_dense_top_k: int = 50
    retrieve_sparse_top_k: int = 50
    # RRF 的 k：60 是原论文默认值，k 越大头部越平、越依赖多路共识
    rrf_k: int = 60
    # cross-encoder 精排（P1-M6 第 3 段）。模型缺失/加载失败时自动降级为"只用 RRF 排序"
    rerank_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-base"
    rerank_ms_repo: str = "Xenova/bge-reranker-base"
    rerank_onnx_file: str = "onnx/model_int8.onnx"
    rerank_input_k: int = 50   # 送进 cross-encoder 的候选数（再大收益递减、延迟线性涨）
    rerank_top_n: int = 24     # 精排后保留数，后面还要按父块去重
    rerank_max_length: int = 512
    rerank_threads: int = 8
    rerank_cache_dir: str = "./data/models"

    @field_validator("database_url", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _must_be_postgres(self) -> Settings:
        """向量和元数据都在 PG 里，SQLite 已经不支持了。

        工程从 SQLite 迁过来时 .env 很可能还留着旧的 DATABASE_URL，
        那种情况下报错要直指原因，而不是让 asyncpg 抛一句看不懂的方言错误。
        """
        url = self.database_url
        if url.startswith("sqlite"):
            raise ValueError(
                "DATABASE_URL 仍指向 SQLite。本版本已改为 PostgreSQL + pgvector，"
                "请改成 postgresql+asyncpg://用户:密码@主机:5432/库名，"
                "并用 scripts/migrate_to_pg.py 迁移既有数据。"
            )
        if not url.startswith("postgresql"):
            raise ValueError(f"DATABASE_URL 必须是 postgresql+asyncpg://，当前是 {url.split('://')[0]}://")
        return self

    # ---------- 派生属性 ----------
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def storage_dir(self) -> Path:
        p = Path(self.local_storage_dir)
        return p if p.is_absolute() else (BASE_DIR / p).resolve()

    @property
    def cache_dir(self) -> Path:
        p = Path(self.embedding_cache_dir)
        return p if p.is_absolute() else (BASE_DIR / p).resolve()

    @property
    def fast_model(self) -> str:
        return self.llm_model_fast or self.llm_model

    @property
    def asyncpg_dsn(self) -> str:
        """给 asyncpg 直连用的 DSN（去掉 SQLAlchemy 的 +asyncpg 后缀）。"""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def apply_runtime_env() -> None:
    """在导入 onnxruntime / numpy 之前限制线程数。

    CPU 4 核场景下，onnxruntime 默认会吃满所有核心并互相抢线程，
    实测吞吐反而下降。这里统一钉死到配置值。
    """
    s = get_settings()
    n = str(max(1, s.embedding_threads))
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(key, n)
    if s.hf_endpoint:
        os.environ.setdefault("HF_ENDPOINT", s.hf_endpoint)
    if s.embedding_cache_dir:
        os.environ.setdefault("FASTEMBED_CACHE_PATH", str(s.cache_dir))


settings = get_settings()
