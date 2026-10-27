"""全局配置。所有可调参数集中在此，代码里不出现魔法值。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
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

    # ---- 数据库 ----
    database_url: str = "sqlite+aiosqlite:///./data/app.db"

    # ---- 存储 ----
    storage_backend: str = "local"
    local_storage_dir: str = "./data/uploads"

    # ---- Milvus ----
    milvus_uri: str = ""
    milvus_token: str = ""
    milvus_db_name: str = "default"
    milvus_collection: str = "novel_chunks_v1"
    milvus_dim: int = 512
    milvus_metric: str = "COSINE"
    milvus_index_type: str = "AUTOINDEX"
    milvus_timeout: int = 30
    milvus_consistency: str = "Strong"

    # ---- Embedding ----
    embedding_provider: str = "auto"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    embedding_batch: int = 16
    embedding_threads: int = 4
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

    @field_validator("milvus_uri", "milvus_token", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

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

    def milvus_config_error(self) -> str | None:
        """返回配置错误说明；None 表示看起来没问题。

        历史坑：用户给的串是纯 hex Token，被当成 URI 填进来。
        这里显式识别，避免 pymilvus 抛一堆看不懂的 DNS 错误。
        """
        uri = (self.milvus_uri or "").strip()
        if not uri:
            return "MILVUS_URI 为空"
        if "://" not in uri:
            snippet = uri[:12] + "…" if len(uri) > 12 else uri
            return (
                f"MILVUS_URI 看起来是 Token 而不是地址（值以 {snippet} 开头，缺少 scheme/host）。"
                "请填写形如 https://in03-xxx.serverless.<region>.cloud.zilliz.com.cn 的地址，"
                "Token 放到 MILVUS_TOKEN。"
            )
        if not self.milvus_token:
            return "MILVUS_TOKEN 为空"
        return None


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
